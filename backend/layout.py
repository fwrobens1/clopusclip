"""Split-screen vertical layout: streamer facecam on top, gameplay below.

Matches the reference clips: a wide facecam band at the top (~35% of the
frame), a bold white hook title at the seam, gameplay filling the lower ~65%,
and karaoke captions burned in over the gameplay.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

FFMPEG = "ffmpeg"


def esc_ass(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", " ")


def render_split(
    input_path: str,
    out_path: str,
    duration: float,
    face_cx: float = 0.10,
    face_cy: float = 0.28,
    words: list[dict] | None = None,
    hook: str = "",
    caption_start: float = 0.0,
    width: int = 720,
    height: int = 1280,
    face_shift_ms: float = 0.0,
    watermark: str = "@NOCTX",
    draft: bool = False,
    encoder: str | None = None,
    encoder_args: list[str] | None = None,
    prefer_hardware: bool = True,
) -> dict:
    """Render one clip with facecam band + gameplay + hook + captions."""
    from clips import build_ass
    import hardware

    if encoder is None or encoder_args is None:
        encoder, encoder_args = hardware.encoder_args(prefer_hardware)
    if draft:
        width, height = 360, 640
        words = None
    fps = 15 if draft else 30

    face_h = int(height * 0.34)
    game_h = height - face_h

    # Facecam crop: a horizontal strip around the streamer's face. Source is
    # 16:9, so crop a wide band from the top-left and scale it to fill 720x435.
    fc_w = 0.30  # 30% of source width — head + shoulders, like the reference
    fc_h = 0.36  # 36% of source height
    fc_x = max(0.0, min(1.0 - fc_w, face_cx - fc_w * 0.32))
    fc_y = max(0.0, min(1.0 - fc_h, face_cy - fc_h * 0.48))

    # Gameplay crop: center of the frame, cropped to 9:16 then scaled.
    gw = 0.46
    gh = 1.0
    gx = 0.5 - gw / 2

    # Build ASS for captions (positioned in the gameplay area).
    ass_path = out_path + ".ass"
    made_ass = bool(words and build_ass(words, caption_start, caption_start + duration,
                                        width, height, ass_path))
    ass_filter = ""
    if made_ass:
        ass_arg = ass_path.replace("\\", "/").replace(":", "\\:")
        ass_filter = f",ass='{ass_arg}'"

    # Bold hook text at the seam, rendered as ASS (reliable on Windows).
    hook_ass_path = out_path + ".hook.ass"
    hook_filter = ""
    if hook:
        hook_y = face_h - 62
        hook_header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,Arial,34,&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,1,0,0,0,100,100,0,0,1,3,0,5,20,20,{hook_y},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 1,0:00:00.40,0:00:05.50,Hook,,0,0,0,,{{\\pos({width // 2},{face_h + 8})}}{esc_ass(hook)}
"""
        Path(hook_ass_path).write_text(hook_header, encoding="utf-8")
        hp = hook_ass_path.replace("\\", "/").replace(":", "\\:")
        hook_filter = f",ass='{hp}'"

    # Optional facecam timing correction: Twitch webcam overlays can lag the
    # mic audio by a few frames. Shifting the facecam earlier restores lip-sync
    # without touching gameplay or the audio track.
    face_pts = f",setpts=PTS-{face_shift_ms / 1000.0}" if face_shift_ms else ""

    # Watermark as ASS so it survives every frame and is easy to restyle.
    wm_path = out_path + ".wm.ass"
    wm_filter = ""
    if watermark:
        wm_header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: WM,Arial,26,&H00FFFFFF,&H00FFFFFF,&H00000000,&H60000000,1,0,0,0,100,100,0,0,1,2,0,6,20,20,24,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 2,0:00:00.00,9:59:59.99,WM,,0,0,0,,{esc_ass(watermark)}
"""
        Path(wm_path).write_text(wm_header, encoding="utf-8")
        wp = wm_path.replace("\\", "/").replace(":", "\\:")
        wm_filter = f",ass='{wp}'"

    # Use expressions with source dimensions via crop's fractional syntax.
    fc = (
        f"[0:v]split=2[cam][game];"
        f"[cam]crop=iw*{fc_w}:ih*{fc_h}:iw*{fc_x}:ih*{fc_y},"
        f"scale={width}:{face_h}:force_original_aspect_ratio=increase,"
        f"crop={width}:{face_h}{face_pts}[camout];"
        f"[game]crop=iw*{gw}:ih*{gh}:iw*{gx}:0,"
        f"scale={width}:{game_h}:force_original_aspect_ratio=increase,"
        f"crop={width}:{game_h}[gameout];"
        f"[camout][gameout]vstack=inputs=2"
        f"{hook_filter}{ass_filter}{wm_filter},fps={fps}[v]"
    )

    cmd = [
        FFMPEG, "-y", "-ss", "0", "-i", input_path, "-t", f"{duration:.3f}",
        "-filter_complex", fc, "-map", "[v]", "-map", "0:a",
        "-c:v", encoder, *encoder_args,
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", "-shortest", out_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    for p in ([ass_path] if made_ass else []) + ([hook_ass_path] if hook else []) + ([wm_path] if watermark else []):
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    if r.returncode != 0 or not os.path.exists(out_path):
        return {"ok": False, "error": r.stderr[-900:]}
    return {"ok": True, "captions": made_ass, "size": os.path.getsize(out_path)}
