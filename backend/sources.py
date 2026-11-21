"""URL ingestion for YouTube / Twitch / Kick and 1000+ other sites via yt-dlp.

Strategy for long VODs (e.g. 7-hour Twitch streams):
  1. Resolve metadata only (fast, no download).
  2. Prefer subtitle/auto-caption tracks when available — instant, no transcription.
  3. Otherwise download AUDIO ONLY (small) and transcribe locally.
  4. Render each selected clip by downloading just that short SECTION from the
     source — never the whole VOD. This keeps bandwidth and disk usage low.
"""
from __future__ import annotations

import os
import re
import subprocess
import shutil
from pathlib import Path
from typing import Any, Callable

import yt_dlp

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
COOKIES_FROM_BROWSER = os.getenv("YT_COOKIES_FROM_BROWSER", "").strip() or None


def _base_opts() -> dict:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        # HLS/DASH streams are split into many small fragments. Downloading
        # them in parallel turns a ~45min audio grab into a few minutes.
        "concurrent_fragment_downloads": int(os.getenv("YT_FRAGMENT_WORKERS", "8")),
        # Keep HLS fragments instead of bulk-deleting them: some sandboxes
        # block batch deletions, which stalls yt-dlp mid-download.
        "keep_fragments": True,
        "noprogress": True,
    }
    if COOKIES_FROM_BROWSER:
        opts["cookiesfrombrowser"] = (COOKIES_FROM_BROWSER,)
    return opts


def resolve(url: str) -> dict:
    """Fetch metadata for a URL without downloading media."""
    with yt_dlp.YoutubeDL({**_base_opts(), "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError("Could not resolve this link.")
    thumbnails = info.get("thumbnails") or []
    thumb = next((t["url"] for t in reversed(thumbnails) if t.get("url")), None)
    return {
        "id": info.get("id"),
        "title": info.get("title") or "Untitled video",
        "description": (info.get("description") or "")[:500],
        "duration": float(info.get("duration") or 0),
        "uploader": info.get("uploader") or "",
        "platform": info.get("extractor_key") or info.get("extractor") or "unknown",
        "thumbnail": thumb,
        "is_live": bool(info.get("is_live")),
        "was_live": bool(info.get("was_live")),
        "webpage_url": info.get("webpage_url") or url,
        "subtitles": list((info.get("subtitles") or {}).keys()),
        "auto_captions": list((info.get("automatic_captions") or {}).keys()),
    }


def _parse_vtt(text: str) -> list[dict]:
    """Parse WebVTT cues into {start,end,text} segments."""
    segs: list[dict] = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    ts_re = re.compile(r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})\.(\d{3})")
    for b in blocks:
        m = ts_re.search(b)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        lines = [ln.strip() for ln in b.split("\n")]
        text_lines = []
        for ln in lines:
            if ts_re.search(ln) or ln.upper().startswith("WEBVTT") or ln.isdigit():
                continue
            # Strip VTT inline tags like <c>, <00:00:01.000>
            clean = re.sub(r"<[^>]+>", "", ln)
            if clean.strip():
                text_lines.append(clean.strip())
        body = " ".join(text_lines)
        if body and end > start:
            segs.append({"start": round(start, 2), "end": round(end, 2), "text": body})

    # Merge consecutive duplicate/rolling captions (common in auto-captions)
    merged: list[dict] = []
    for s in segs:
        if merged and s["text"] == merged[-1]["text"] and s["start"] <= merged[-1]["end"] + 0.3:
            merged[-1]["end"] = s["end"]
        else:
            merged.append(dict(s))
    return merged


def download_transcript(url: str, work_dir: Path, langs: tuple[str, ...] = ("en", "en-US", "en-orig")) -> list[dict]:
    """Try to obtain subtitles/auto-captions. Returns transcript segments or []"""
    work_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        **_base_opts(),
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": list(langs),
        "subtitlesformat": "vtt",
        "outtmpl": str(work_dir / "subs.%(ext)s"),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception:
        return []

    best: Path | None = None
    for p in sorted(work_dir.glob("subs*.vtt"), key=lambda x: x.stat().st_size, reverse=True):
        best = p
        break
    if not best or best.stat().st_size < 50:
        return []
    try:
        text = best.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    return _parse_vtt(text)


def download_audio(url: str, out_path: Path, max_minutes: float | None = None,
                   progress_cb: Callable[[dict], None] | None = None) -> Path:
    """Download audio only (small) for local transcription."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    opts = {
        **_base_opts(),
        "format": "bestaudio/best",
        "outtmpl": str(out_path.with_suffix("")),
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "wav", "preferredquality": "64"},
        ],
        "postprocessor_args": ["-ar", "16000", "-ac", "1"],
        "overwrites": True,
    }
    if progress_cb:
        def hook(d):
            if d.get("status") == "downloading":
                progress_cb({
                    "percent": d.get("_percent_str", "").strip() or "0%",
                    "speed": d.get("_speed_str", "").strip(),
                    "eta": d.get("_eta_str", "").strip(),
                    "downloaded": d.get("downloaded_bytes") or 0,
                })
        opts["progress_hooks"] = [hook]
    if max_minutes:
        opts["download_ranges"] = lambda info, ydl: [{"start_time": 0, "end_time": max_minutes * 60}]
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        raise RuntimeError(f"Audio download failed: {e}") from e

    # ExtractAudio may produce .wav at the template path
    candidates = [out_path, out_path.with_suffix(".wav")]
    for c in candidates:
        if c.exists() and c.stat().st_size > 1000:
            return c
    raise RuntimeError("Audio download produced no file")


def download_section(url: str, start: float, end: float, out_path: Path) -> Path:
    """Download only a short section of the source as an MP4 (no full VOD download)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    span = max(2, int(end - start) + 2)
    opts = {
        **_base_opts(),
        "format": "best[height<=720]/best",
        "merge_output_format": "mp4",
        "download_sections": [f"*{int(start)}-{int(start) + span}"],
        "force_keyframes_at_cuts": True,
        "outtmpl": str(out_path.with_suffix("")),
        "overwrites": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception:
        pass
    for p in [out_path, *[out_path.with_suffix("." + e) for e in ("mp4", "mkv", "webm", "mov")]]:
        if p.exists() and p.stat().st_size > 1000:
            return p

    # Fallback for direct/generic media URLs (no HLS section support):
    # download the full file once into a cache, then cut locally with ffmpeg.
    cache = out_path.parent / (out_path.stem.split("_sec_")[0] + "_full")
    cached_file = None
    for p in [cache, *[cache.with_suffix("." + e) for e in ("mp4", "mkv", "webm", "mov")]]:
        if p.exists() and p.stat().st_size > 1000:
            cached_file = p
            break
    if cached_file is None:
        full_opts = {
            **_base_opts(),
            "format": "best[height<=720]/best",
            "merge_output_format": "mp4",
            "outtmpl": str(cache),
            "overwrites": True,
        }
        with yt_dlp.YoutubeDL(full_opts) as ydl:
            ydl.download([url])
        for p in [cache, *[cache.with_suffix("." + e) for e in ("mp4", "mkv", "webm", "mov")]]:
            if p.exists() and p.stat().st_size > 1000:
                cached_file = p
                break
    if cached_file is None:
        raise RuntimeError("Section download produced no file")

    r = subprocess.run(
        [FFMPEG, "-y", "-ss", f"{max(0, start):.3f}", "-i", str(cached_file),
         "-t", f"{span}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-shortest", str(out_path)],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not out_path.exists():
        raise RuntimeError(f"Section cut failed: {r.stderr[-300:]}")
    return out_path
