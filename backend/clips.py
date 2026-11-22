"""Clip rendering with ffmpeg: accurate cuts, word-highlight captions, vertical reframe."""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
RENDER_TIMEOUT = float(os.getenv("RENDER_TIMEOUT", "480"))


def _ffmpeg(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([FFMPEG, *args], capture_output=True, text=True, timeout=RENDER_TIMEOUT)


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

    # ASR chunk boundaries can produce duplicate/overlapping words. Normalise
    # first, otherwise libass renders several captions on top of each other.
    relevant = [w for w in words if float(w.get("end", 0)) > start and float(w.get("start", 0)) < end]
    relevant.sort(key=lambda w: (float(w.get("start", 0)), float(w.get("end", 0))))
    clean: list[dict] = []
    for w in relevant:
        ws = float(w.get("start", 0))
        we = max(ws + 0.08, float(w.get("end", ws + 0.2)))
        if clean and ws < clean[-1]["_end"] - 0.05:
            # Overlapping word (another speaker or duplicated chunk) — skip
            # unless it is meaningfully later, to keep one caption line at a time.
            if clean[-1]["_word"] == str(w.get("word", "")).strip():
                continue
            ws = clean[-1]["_end"]
            we = max(we, ws + 0.08)
        item = dict(w)
        item["_start"], item["_end"] = ws, we
        item["_word"] = str(w.get("word", "")).strip()
        clean.append(item)
    if not clean:
        return False

    events: list[str] = []
    chunk: list[dict] = []
    for w in clean:
        chunk.append(w)
        chunk_dur = chunk[-1]["_end"] - chunk[0]["_start"]
        if len(chunk) >= 4 or chunk_dur >= 2.5:
            events.append(_ass_event(chunk, start, ts, _esc_ass))
            chunk = []
    if chunk:
        events.append(_ass_event(chunk, start, ts, _esc_ass))

    with open(path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(events) + "\n")
    return True


def _ass_event(chunk: list[dict], clip_start: float, ts, esc) -> str:
    line_start = float(chunk[0].get("_start", chunk[0].get("start", clip_start)))
    line_end = float(chunk[-1].get("_end", chunk[-1].get("end", line_start + 1)))
    parts = []
    for idx, w in enumerate(chunk):
        ws = float(w.get("_start", w.get("start", line_start)))
        we = float(w.get("_end", w.get("end", ws + 0.2)))
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
    draft: bool = False,
    encoder: str | None = None,
    encoder_args: list[str] | None = None,
    prefer_hardware: bool = True,
) -> dict[str, Any]:
    """Render a single clip. Returns info dict with ok/error."""
    start = float(clip["start"])
    end = float(clip["end"])
    duration = max(0.5, end - start)

    import hardware

    if encoder is None or encoder_args is None:
        encoder, encoder_args = hardware.encoder_args(prefer_hardware)

    w, h = probe_size(input_path)
    # 720x1280 is still standard 9:16 for social feeds, but ~2.25x cheaper to
    # encode than 1080x1920. Drafts drop to 360p/15fps for fast review.
    if draft:
        out_w, out_h = (360, 640) if vertical else (max(2, w // 2), max(2, h // 2))
    else:
        out_w, out_h = (720, 1280) if vertical else (w, h)
    fps = 15 if draft else 30

    # Captions are skipped in drafts — the review pass is about timing.
    ass_path = out_path + ".ass"
    made_ass = bool(captions and not draft and words and build_ass(words, start, end, out_w, out_h, ass_path))
    ass_filter = ""
    if made_ass:
        ass_arg = ass_path.replace("\\", "/").replace(":", "\\:")
        ass_filter = f"ass='{ass_arg}'"

    def build_cmd(use_captions: bool, enc: str, eargs: list[str]) -> list[str]:
        cmd = ["-y", "-ss", f"{start:.3f}", "-i", input_path, "-t", f"{duration:.3f}"]
        af = ass_filter if use_captions else ""
        # Fast path: no reframe, no captions, no draft — remux the section.
        if not vertical and not af and not draft:
            return cmd + ["-c", "copy", "-movflags", "+faststart", "-shortest", out_path]
        if vertical:
            # Punch-in reframe: crop a 9:16 window around the subject instead of
            # shrinking the whole 16:9 frame into a tiny letterbox. ``focus`` is
            # a normalised (0..1) crop centre from face detection; fallback to
            # centre. The game stays readable because we crop, not scale down.
            focus = clip.get("focus") or {}
            fx = max(0.0, min(1.0, float(focus.get("cx", 0.5))))
            fy = max(0.0, min(1.0, float(focus.get("cy", 0.45))))
            crop_w = max(2, int(h * 9 / 16) // 2 * 2)
            crop_h = h
            max_x = max(0, w - crop_w)
            x0 = int(round(fx * max_x)) // 2 * 2
            # Bias slightly upward so captions at the bottom don't cover faces.
            y0 = 0
            fc = (
                f"[0:v]crop={crop_w}:{crop_h}:{x0}:{y0},"
                f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
                f"crop={out_w}:{out_h},setsar=1,fps={fps}[v]"
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
            "-c:v", enc, *eargs,
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", "-shortest", out_path,
        ]
        return cmd

    r = _ffmpeg(build_cmd(True, encoder, encoder_args))
    if made_ass and os.path.exists(ass_path):
        try:
            os.remove(ass_path)
        except OSError:
            pass

    if r.returncode != 0 or not os.path.exists(out_path):
        if hardware.is_encoder_error(r.stderr or ""):
            # GPU driver may vanish mid-session; retry on CPU before giving up.
            encoder, encoder_args = hardware.fallback_encoder_args(encoder)
            r = _ffmpeg(build_cmd(True, encoder, encoder_args))
        if r.returncode != 0 or not os.path.exists(out_path):
            # Retry without captions (e.g. libass unavailable or bad subtitle timing)
            r2 = _ffmpeg(build_cmd(False, encoder, encoder_args))
            if r2.returncode == 0 and os.path.exists(out_path):
                return {"ok": True, "path": out_path, "captions": False,
                        "note": "Captions could not be burned in; clip rendered without them."}
            return {"ok": False, "error": (r2.stderr or r.stderr or "ffmpeg failed")[-800:]}

    return {"ok": True, "path": out_path, "captions": made_ass,
            "size": os.path.getsize(out_path), "encoder": encoder}
