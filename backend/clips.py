"""Clip rendering with ffmpeg: accurate cuts, word-highlight captions, vertical reframe."""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"


def _ffmpeg(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([FFMPEG, *args], capture_output=True, text=True)


def probe_duration(path: str) -> float:
    r = _ffmpeg(["-i", path])
    import re

    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", r.stderr or "")
    if m:
        h, mi, s = m.groups()
        return int(h) * 3600 + int(mi) * 60 + float(s)
    return 0.0


def probe_size(path: str) -> tuple[int, int]:
    r = _ffmpeg(["-i", path])
    import re

    m = re.search(r"(\d{2,5})x(\d{2,5})", r.stderr or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    return 1920, 1080


def _esc_ass(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", " ")


def build_ass(words: list[dict], start: float, end: float, width: int, height: int, path: str) -> bool:
    """Write an ASS subtitle file with word-level karaoke highlighting."""
    font_size = max(24, int(min(width * 0.045, height * 0.06)))
    margin_v = max(40, int(height * 0.10))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,{font_size},&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,3,0,2,40,40,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def ts(t: float) -> str:
        t = max(0.0, t)
        cs = int((t % 1) * 100)
        total = int(t)
        h = total // 3600
        m = (total % 3600) // 60
        s = total % 60
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    events = []
    # Group words into subtitle lines of ~6 words / 3.5s
    relevant = [w for w in words if float(w.get("end", 0)) > start and float(w.get("start", 0)) < end]
    if not relevant:
        return False

    chunk: list[dict] = []
    for w in relevant:
        chunk.append(w)
        chunk_dur = float(chunk[-1].get("end", 0)) - float(chunk[0].get("start", 0))
        if len(chunk) >= 4 or chunk_dur >= 2.5:
            events.append(_ass_event(chunk, start, ts, _esc_ass))
            chunk = []
    if chunk:
        events.append(_ass_event(chunk, start, ts, _esc_ass))

    with open(path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(events) + "\n")
    return True


def _ass_event(chunk: list[dict], clip_start: float, ts, esc) -> str:
    line_start = float(chunk[0].get("start", clip_start))
    line_end = float(chunk[-1].get("end", line_start + 1))
    parts = []
    for idx, w in enumerate(chunk):
        ws = float(w.get("start", line_start))
        we = float(w.get("end", ws + 0.2))
        dur_cs = max(1, int((we - ws) * 100))
        parts.append(r"{\kf%d}%s " % (dur_cs, esc(str(w.get("word", "")))))
        if idx == 2 and len(chunk) > 3:  # wrap after 3 words to keep lines short
            parts.append(r"\N")
    return (
        f"Dialogue: 0,{ts(line_start - clip_start)},{ts(line_end - clip_start)},"
        f"Default,,0,0,0,,{''.join(parts)}"
    )


def render_clip(
    input_path: str,
    clip: dict,
    out_path: str,
    words: list[dict] | None = None,
    captions: bool = True,
    vertical: bool = False,
) -> dict[str, Any]:
    """Render a single clip. Returns info dict with ok/error."""
    start = float(clip["start"])
    end = float(clip["end"])
    duration = max(0.5, end - start)

    w, h = probe_size(input_path)
    out_w, out_h = (1080, 1920) if vertical else (w, h)

    ass_path = out_path + ".ass"
    made_ass = bool(captions and words and build_ass(words, start, end, out_w, out_h, ass_path))
    ass_filter = ""
    if made_ass:
        ass_arg = ass_path.replace("\\", "/").replace(":", "\\:")
        ass_filter = f"ass='{ass_arg}'"

    def build_cmd(use_captions: bool) -> list[str]:
        cmd = ["-y", "-ss", f"{start:.3f}", "-i", input_path, "-t", f"{duration:.3f}"]
        af = ass_filter if use_captions else ""
        if vertical:
            # Complex filtergraph needed (blurred background + overlay = 2 inputs)
            fc = (
                "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,boxblur=18:4[bg];"
                "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
                "[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1[v]"
            )
            if af:
                fc += f";[v]{af}[vout]"
                vmap = "[vout]"
            else:
                vmap = "[v]"
            cmd += ["-filter_complex", fc, "-map", vmap, "-map", "0:a"]
        elif af:
            cmd += ["-vf", af]
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", "-shortest", out_path,
        ]
        return cmd

    r = _ffmpeg(build_cmd(True))
    if made_ass and os.path.exists(ass_path):
        try:
            os.remove(ass_path)
        except OSError:
            pass

    if r.returncode != 0 or not os.path.exists(out_path):
        # Retry without captions (e.g. libass unavailable or bad subtitle timing)
        r2 = _ffmpeg(build_cmd(False))
        if r2.returncode == 0 and os.path.exists(out_path):
            return {"ok": True, "path": out_path, "captions": False,
                    "note": "Captions could not be burned in; clip rendered without them."}
        return {"ok": False, "error": (r2.stderr or r.stderr or "ffmpeg failed")[-800:]}

    return {"ok": True, "path": out_path, "captions": made_ass, "size": os.path.getsize(out_path)}
