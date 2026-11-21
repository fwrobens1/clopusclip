"""URL ingestion for YouTube / Twitch / Kick and 1000+ other sites via yt-dlp.

Strategy for long VODs (e.g. 7-hour Twitch streams):
  1. Resolve metadata only (fast, no download).
  2. Prefer subtitle/auto-caption tracks when available — instant, no transcription.
  3. Otherwise download AUDIO ONLY (small) and transcribe locally.
  4. Render each selected clip by downloading just that short SECTION from the
     source — never the whole VOD. This keeps bandwidth and disk usage low.
"""
from __future__ import annotations

import hashlib
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
        # Don't delete the downloaded file after postprocessing either — the
        # same sandbox guard blocks that single delete and hangs yt-dlp.
        "keepvideo": True,
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
                   progress_cb: Callable[[dict], None] | None = None,
                   use_cache: bool = True) -> Path:
    """Download audio only (small) for local transcription.

    Completed downloads are cached by URL so repeat jobs are instant. Live
    streams must pass use_cache=False — their downloads are partial snapshots,
    which would poison the cache for later runs.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cache = out_path.parent / ("urlcache_" + hashlib.md5(url.encode()).hexdigest()[:16] + ".wav")
    if use_cache and cache.exists() and cache.stat().st_size > 100_000:
        if progress_cb:
            progress_cb({"percent": "100%", "speed": "cached", "eta": "00:00",
                         "downloaded": cache.stat().st_size})
        return cache
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
    result: Path | None = None
    for c in candidates:
        if c.exists() and c.stat().st_size > 1000:
            result = c
            break
    if result is None:
        raise RuntimeError("Audio download produced no file")

    # Cache only complete, full-length downloads (never partial/live ranges).
    if use_cache and max_minutes is None and result.stat().st_size > 100_000:
        try:
            shutil.copy2(result, cache)
        except OSError:
            pass  # caching is best-effort; never fail a job over it
    return result


def _resolve_media_url(url: str) -> tuple[str, list[str]]:
    """Resolve the direct HLS/file URL (no download) plus ffmpeg header args."""
    with yt_dlp.YoutubeDL({**_base_opts(), "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    media_url = info.get("url")
    if not media_url:
        raise RuntimeError("Could not resolve a direct media URL for this source")
    headers = info.get("http_headers") or {}
    header_args: list[str] = []
    if headers:
        # ffmpeg expects one -headers string with CRLF-separated lines.
        header_args = ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
    return media_url, header_args


def download_section(url: str, start: float, end: float, out_path: Path) -> Path:
    """Fetch only a short section by seeking directly in the source stream.

    yt-dlp's ``download_sections`` re-downloaded the ENTIRE 7h Twitch VOD
    (10+ GB, thousands of fragments) instead of just the requested window.
    Resolving the direct HLS URL and letting ffmpeg seek to the timestamp
    fetches only the needed fragments — a 30s section takes a few seconds.
    Re-encoding (rather than -c copy) keeps cuts accurate for word captions.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    span = max(2, int(end - start) + 2)
    media_url, header_args = _resolve_media_url(url)

    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *header_args,
        "-ss", f"{max(0, start):.3f}", "-i", media_url, "-t", f"{span}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-shortest", str(out_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not out_path.exists() or out_path.stat().st_size < 1000:
        raise RuntimeError(f"Section fetch failed: {r.stderr[-400:]}")
    return out_path
