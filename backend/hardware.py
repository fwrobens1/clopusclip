"""Pick the fastest available local H.264 encoder (all free, local only)."""
from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

# (encoder name, extra args). CPU fallback is always safe.
CANDIDATES = (
    ("h264_nvenc", ["-preset", "p1", "-cq", "23"]),
    ("h264_qsv", ["-preset", "fast", "-global_quality", "23"]),
    ("h264_amf", ["-quality", "balanced"]),
)


@lru_cache(maxsize=1)
def _available() -> set[str]:
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return set()
    names = set()
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].startswith("h264"):
            names.add(parts[1])
    return names


def encoder_args(prefer_hardware: bool = True) -> tuple[str, list[str]]:
    """Return (encoder, extra_args). Falls back to libx264 veryfast."""
    if prefer_hardware:
        available = _available()
        for name, args in CANDIDATES:
            if name in available:
                return name, list(args)
    return "libx264", ["-preset", "veryfast", "-crf", "23"]


def ffmpeg_label() -> str:
    enc, _ = encoder_args()
    return "CPU · libx264" if enc == "libx264" else f"GPU · {enc}"
