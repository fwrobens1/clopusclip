"""Pick the fastest local H.264 encoder that actually works in this session.

A GPU encoder can be listed by ``ffmpeg -encoders`` yet still fail at runtime
(e.g. ``Cannot load nvcuda.dll`` in an RDP session). We therefore run a tiny
real encode against each candidate before trusting it. Probe results are
cached, and renderers also fall back to CPU if an encode fails mid-job.
"""
from __future__ import annotations

import os
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
CPU = ("libx264", ["-preset", "veryfast", "-crf", "23"])

PROBE_TIMEOUT = float(os.getenv("ENCODER_PROBE_TIMEOUT", "12"))


@lru_cache(maxsize=8)
def _works(name: str, args: tuple[str, ...]) -> bool:
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=64x64:r=1",
        "-frames:v", "1", "-c:v", name, *args,
        "-pix_fmt", "yuv420p", "-f", "null", "-",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def encoder_args(prefer_hardware: bool = True) -> tuple[str, list[str]]:
    """Return (encoder, extra_args). Falls back to libx264 veryfast."""
    if prefer_hardware:
        for name, args in CANDIDATES:
            if _works(name, tuple(args)):
                return name, list(args)
    return CPU[0], list(CPU[1])


def fallback_encoder_args(encoder: str | None = None) -> tuple[str, list[str]]:
    """Safe CPU encoder to retry with when a hardware encode fails."""
    return CPU[0], list(CPU[1])


def is_encoder_error(stderr: str) -> bool:
    """True when ffmpeg failed to open/use the requested video encoder."""
    low = (stderr or "").lower()
    markers = (
        "cannot load nvcuda.dll",
        "error while opening encoder",
        "could not open encoder",
        "cannot initialize hwaccel",
        "unable to initialize encoder",
        "device setup failed",
        "invalid argument",
    )
    return any(m in low for m in markers)


def ffmpeg_label() -> str:
    enc, _ = encoder_args()
    return "CPU · libx264" if enc == "libx264" else f"GPU · {enc}"


def reset_cache() -> None:
    """Allow re-probing after a driver change (used by tests)."""
    _works.cache_clear()
