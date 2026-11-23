"""NoetharaClipz API — local AI clipping workspace with persistent library."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# Allow both `backend.main` imports and direct execution
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
import requests
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

import agent  # noqa: E402
import asr  # noqa: E402
import auth as supabase_auth  # noqa: E402
import cache  # noqa: E402
import cloud  # noqa: E402
import dictionary  # noqa: E402
import hardware  # noqa: E402
import library  # noqa: E402
import payoff  # noqa: E402
import sources  # noqa: E402
from ai import chat_json_strong, select_clips  # noqa: E402
from clips import probe_duration, probe_size, render_clip  # noqa: E402
from sources import download_audio, download_section, download_transcript, resolve as resolve_url  # noqa: E402

UPLOAD_DIR = ROOT / "work" / "uploads"
AUDIO_DIR = ROOT / "work" / "audio"
CLIP_DIR = ROOT / "work" / "clips"
SECTION_DIR = ROOT / "work" / "sections"
SUBS_DIR = ROOT / "work" / "subs"
ASR_DIR = ROOT / "work" / "asr"
LIB_DIR = ROOT / "work" / "library"
TRANSCRIPT_DIR = ROOT / "work" / "transcripts"
LIVE_DIR = ROOT / "work" / "live"
FRONTEND_DIR = ROOT / "frontend"
for d in (UPLOAD_DIR, AUDIO_DIR, CLIP_DIR, SECTION_DIR, SUBS_DIR, ASR_DIR, LIB_DIR, TRANSCRIPT_DIR, LIVE_DIR):
    d.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="NoetharaClipz API")

PUBLIC_API = {
    "/api/health",
    "/api/public-config",
    "/api/auth/verify",
    "/api/auth/signup",
    "/api/cloud/status",
}
_SIGNUP_ATTEMPTS: dict[tuple[str, str], list[float]] = {}
_SIGNUP_LOCK = threading.Lock()

# /api/clips is intentionally public: playback is protected by a short-lived
# signed Supabase URL, and <video> elements cannot send an Authorization header.
@app.middleware("http")
async def require_supabase_user(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in PUBLIC_API and not path.startswith("/api/clips/"):
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not token or not supabase_auth.verify(token):
            return JSONResponse({"detail": "Sign in to continue"}, status_code=401)
    return await call_next(request)


jobs: dict[str, dict] = {}
live_sessions: dict[str, dict] = {}
publications: list[dict] = []
LIVE_RECORDERS: dict[str, subprocess.Popen] = {}
RENDER_WORKERS = max(1, min(3, int(os.getenv("RENDER_WORKERS", "2"))))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _friendly_error(exc: Exception) -> str:
    msg = str(exc)
    low = msg.lower()
    if "subscriber-only" in low or "must be logged into an account that has access" in low:
        return "This VOD is subscriber-only. Use a public VOD, upload a local copy, or import a transcript."
    return msg


def _persist(job_id: str) -> None:
    job = jobs.get(job_id)
    if not job:
        return
    _ensure_clip_extras(job)
    detail = {
        "id": job_id,
        "status": job.get("status", "queued"),
        "stage": job.get("stage", "queued"),
        "progress": job.get("progress", 0),
        "message": job.get("message", ""),
        "duration": job.get("duration"),
        "clips": job.get("clips", []),
        "settings": job.get("settings", {}),
        "goal": job.get("goal", ""),
        "source_info": job.get("source_info"),
        "source_url": job.get("source_url"),
        "filename": job.get("filename"),
        "transcript_source": job.get("transcript_source"),
        "asr_backend": job.get("asr_backend"),
        "language": job.get("language"),
        "channel": job.get("channel") or library.channel_for(job.get("source_info"), job.get("source_url")),
        "title": job.get("title") or (job.get("source_info") or {}).get("title") or job.get("filename") or job.get("goal") or job_id,
        "thumbnail": job.get("thumbnail") or (job.get("source_info") or {}).get("thumbnail"),
        "created_at": job.get("created_at") or _now(),
        "parent_job": job.get("parent_job"),
        "notes": job.get("notes", {}),
        "num_clips": job.get("num_clips", 5),
        "clip_length": job.get("clip_length", "medium"),
    }
    library.save_job_detail(job_id, detail)
    library.upsert_job(detail)
    if cloud.enabled():
        try:
            cloud.save_job(detail)
            cloud.save_clips(job_id, detail.get("clips") or [])
        except Exception as e:  # noqa: BLE001 — cloud must never break a local job
            print(f"[cloud] sync failed for {job_id}: {e}", flush=True)


def _set(job_id: str, **kwargs):
    if job_id not in jobs:
        return
    jobs[job_id].update(kwargs)
    if any(k in kwargs for k in ("status", "clips", "source_info", "channel", "stage")) or kwargs.get("progress") in (0, 100):
        _persist(job_id)


def _new_job(**kwargs) -> str:
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {
        "id": job_id,
        "status": "queued",
        "stage": "queued",
        "progress": 0,
        "message": "Queued...",
        "transcript": [],
        "clips": [],
        "error": None,
        "source_url": None,
        "source_info": None,
        "goal": "",
        "created_at": _now(),
        "cancel_requested": False,
        "cancelled": False,
        "notes": {},
        "parent_job": None,
        "channel": library.channel_for(kwargs.get("source_info"), kwargs.get("source_url")),
        "draft": False,
        **kwargs,
    }
    _persist(job_id)
    return job_id


# Restore lightweight job summaries so Library + Continue survive a restart.
for _jid, _detail in library.load_job_details().items():
    jobs[_jid] = {
        "id": _jid,
        "status": _detail.get("status", "done"),
        "stage": _detail.get("stage", "done"),
        "progress": _detail.get("progress", 100),
        "message": _detail.get("message", ""),
        "clips": _detail.get("clips", []),
        "settings": _detail.get("settings", {}),
        "goal": _detail.get("goal", ""),
        "source_url": _detail.get("source_url"),
        "source_info": _detail.get("source_info"),
        "duration": _detail.get("duration"),
        "transcript": [],
        "language": _detail.get("language"),
        "transcript_source": _detail.get("transcript_source"),
        "asr_backend": _detail.get("asr_backend"),
        "channel": _detail.get("channel") or "Unsorted",
        "created_at": _detail.get("created_at") or _now(),
        "parent_job": _detail.get("parent_job"),
        "notes": _detail.get("notes", {}),
        "num_clips": _detail.get("num_clips", 5),
        "clip_length": _detail.get("clip_length", "medium"),
        "cancel_requested": False,
        "cancelled": False,
        "error": None,
    }
    _url = _detail.get("source_url")
    if _url:
        _tp = cache.transcript_path(_url)
        _cached = cache.load_json(_tp)
        if isinstance(_cached, dict) and _cached.get("segments"):
            jobs[_jid]["transcript"] = _cached["segments"]

# Live recorders do not survive a process restart. Keep the session history but
# mark any previously live session as stopped with an explicit reason.
for _sid, _session in library.load_live_sessions().items():
    if _session.get("state") == "live":
        _session["state"] = "stopped"
        _session["phase"] = "done"
        _session["stop_reason"] = "server_restart"
        _session["next_pass_at"] = None
    live_sessions[_sid] = _session

publications.extend(library.load_publications())


def _cancelled(job_id: str) -> bool:
    return bool(jobs.get(job_id, {}).get("cancel_requested"))


def _extract_audio(video_path: Path, audio_path: Path):
    import subprocess

    r = subprocess.run(
        [shutil.which("ffmpeg") or "ffmpeg", "-y", "-i", str(video_path),
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio_path)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"Audio extraction failed: {r.stderr[-400:]}")


def _make_hooks(job: dict, clips: list[dict]) -> list[dict]:
    """Generate 3 short hook alternatives per clip with one free LLM call."""
    if not clips:
        return []
    lines = "\n".join(
        f"{i}. {c.get('title', 'Moment')} — {c.get('reason', '')[:120]}"
        for i, c in enumerate(clips)
    )
    try:
        data = chat_json_strong(
            [
                {"role": "system", "content": "You write punchy TikTok/Shorts hook overlays. Short, lowercase, dry, no emojis, max 8 words."},
                {"role": "user", "content": f"Write 3 alternative hooks for each clip. Return ONLY JSON:\n{{\"hooks\":[[\"hook a\",\"hook b\",\"hook c\"]]}}\nClips:\n{lines}"},
            ],
            max_tokens=1200,
        )
        hooks = data.get("hooks") if isinstance(data, dict) else None
        if isinstance(hooks, list) and len(hooks) >= len(clips):
            return [[str(x)[:90] for x in (h or [])[:3]] for h in hooks]
    except Exception:
        pass
    return [[c.get("title", "Watch this")[:90] for _ in range(3)] for c in clips]


def _clip_excerpt(transcript: list[dict], start: float, end: float, limit: int = 360) -> str:
    """Short transcript slice used to explain why a moment was selected."""
    if not transcript or end <= start:
        return ""
    parts = []
    for seg in transcript:
        s, e = float(seg.get("start", 0)), float(seg.get("end", 0))
        if e < start - 1 or s > end + 1:
            continue
        text = str(seg.get("text", "")).strip()
        if text:
            parts.append(text)
        if sum(len(x) for x in parts) >= limit:
            break
    return " ".join(parts)[:limit]


def _default_tracks(job: dict, clip: dict, index: int) -> list[dict]:
    """Typed editor state: text + visual + caption tracks."""
    start = float(clip.get("start", 0) or 0)
    end = float(clip.get("end", start + 30) or start + 30)
    duration = max(1.0, end - start)
    hook = (clip.get("hooks") or [clip.get("title", "Watch this")])[int(clip.get("hook_index", 0) or 0)]
    job_id = job.get("id", "")
    return [
        {
            "kind": "text",
            "clips": [{
                "type": "text", "role": "hook", "text": str(hook)[:90],
                "start": 0.0, "end": min(3.0, duration),
                "style": {"font_variant": "bold", "case": "lower", "stroke": 2, "shadow": True},
            }],
        },
        {
            "kind": "visual",
            "clips": [{
                "type": "video", "media_id": f"{job_id}-{index}",
                "src": f"/api/clips/{job_id}/{index}",
                "src_start": start, "src_duration": duration,
                "layouts": [job.get("settings", {}).get("layout", "split")],
                "keyframes": [],
                "captions": bool(job.get("settings", {}).get("captions", True)),
                "volume": 1.0,
            }],
        },
        {"kind": "caption", "clips": []},
    ]


def _default_config(job: dict, clip: dict) -> dict:
    settings = job.get("settings", {})
    return {
        "aspect_ratio": 0.5625 if settings.get("vertical", True) else 16 / 9,
        "safe_area": 48,
        "reframe": {
            "enabled": bool(settings.get("vertical", True)),
            "layout": settings.get("layout", "split"),
            "speaker_focus": True,
            "allowed_layout_types": ["split", "full", "blur_pad", "facecam_bubble"],
            "focus": clip.get("focus") or {"cx": 0.10, "cy": 0.30},
        },
        "captions": {
            "enabled": bool(settings.get("captions", True)),
            "max_chars": 15,
            "max_lines": 2,
            "highlight": {"mode": "word"},
        },
        "watermark": clip.get("watermark", "@NOCTX"),
        "trim": {"silence_removal": False},
    }


def _ensure_clip_extras(job: dict) -> None:
    """Backfill explainability + editor state on old and new clips."""
    transcript = job.get("transcript") or []
    for i, c in enumerate(job.get("clips") or []):
        hooks = c.get("hooks") or [c.get("title", "Watch this")]
        c.setdefault("score_reason", c.get("reason") or "Selected for its reaction payoff.")
        c.setdefault("hook_title", hooks[int(c.get("hook_index", 0) or 0)] if hooks else c.get("title", "Watch this"))
        c.setdefault("transcript_excerpt", _clip_excerpt(
            transcript, float(c.get("start", 0)), float(c.get("end", 0))))
        c.setdefault("tracks", _default_tracks(job, c, i))
        c.setdefault("config", _default_config(job, c))
        c.setdefault("source_start_seconds", c.get("start"))
        c.setdefault("source_end_seconds", c.get("end"))


def _source_signals(url: str, audio_path: Path | None) -> list[dict]:
    """Compute audio energy buckets once per source and cache by URL."""
    sp = cache.signals_path(url)
    hit = cache.load_json(sp)
    if isinstance(hit, list):
        return hit
    if not audio_path or not audio_path.exists():
        return []
    try:
        from audio_signals import analyze_wav
        buckets = analyze_wav(str(audio_path), str(sp))
        return buckets or []
    except Exception:
        return []


def _finalize_selection(job_id: str, transcript_segments: list[dict], duration: float,
                        url: str | None = None, audio_path: Path | None = None) -> list[dict]:
    """Select, enrich, payoff-extend and persist the candidate moments."""
    job = jobs[job_id]
    dictionary.apply_segments(transcript_segments)

    num_clips = int(job.get("num_clips", 5))
    goal = job.get("goal", "")
    settings = {"clip_length": job.get("clip_length", "medium"), "captions": job.get("settings", {}).get("captions", True)}
    clips: list[dict] = []

    if url and duration >= 1800:
        def _fresh():
            try:
                import fusion
                sp = cache.signals_path(url)
                if not sp.exists():
                    _source_signals(url, audio_path)
                if not sp.exists():
                    return []
                return fusion.fuse(url, transcript_segments, duration, num_clips, str(sp), goal, prefer_gameplay=True)
            except Exception:
                return []
        clips = cache.cached_json("selection", (url, goal, settings, num_clips), _fresh) or []

    if not clips:
        clips = select_clips(
            transcript_segments,
            video_duration=duration,
            num_clips=num_clips,
            clip_length=job.get("clip_length", "medium"),
            goal=goal,
        )
    if not clips:
        raise RuntimeError("The AI could not find any suitable clips in this video.")

    audio = _source_signals(url, audio_path) if url else []
    clips = payoff.extend_to_payoff(clips, transcript_segments, audio, duration=duration)
    if num_clips >= 3:
        clips = payoff.merge_sequences(clips, transcript_segments, audio)
    clips = clips[:num_clips]

    hooks = _make_hooks(job, clips)
    channel = job.get("channel") or "Unsorted"
    preset = library.load_presets().get(channel, {})
    for i, c in enumerate(clips):
        c["index"] = i
        c["render_status"] = "pending"
        c["url"] = None
        c["error"] = None
        c["hooks"] = hooks[i] if i < len(hooks) else [c.get("title", "Watch this")]
        c["hook_index"] = 0
        c["focus"] = c.get("focus") or {
            "cx": float(preset.get("cx", 0.10)),
            "cy": float(preset.get("cy", 0.30)),
        }
        c["face_shift_ms"] = float(preset.get("face_shift_ms", 150))
        c["watermark"] = str(preset.get("watermark", "@NOCTX"))
        c["title"] = dictionary.apply_text(c.get("title", ""), dictionary.load())
        c["reason"] = dictionary.apply_text(c.get("reason", ""), dictionary.load())

    _set(job_id, clips=clips, transcript=transcript_segments)
    return clips


def _get_source_transcript(job_id: str, url: str, info: dict) -> tuple[list[dict], str, Path | None]:
    """Caption-first, audio-fallback, cached per source URL."""
    tp = cache.transcript_path(url)
    hit = cache.load_json(tp)
    if isinstance(hit, dict) and hit.get("segments"):
        return hit["segments"], hit.get("source_kind", "subtitles"), None

    segments: list[dict] = []
    if not info.get("is_live"):
        _set(job_id, progress=12, message="Looking for captions...")
        segments = download_transcript(url, SUBS_DIR / job_id)
    if segments and len(segments) >= 2:
        _set(job_id, language="captions", progress=25,
             message=f"Using {len(segments)} caption cues from the source (no transcription needed).")
        cache.save_json(tp, {"segments": segments, "source_kind": "subtitles"})
        return segments, "subtitles", None

    _set(job_id, progress=15, message="Downloading audio only (not the whole video)...")

    def _dl_progress(d):
        if d.get("downloaded", 0) > 0:
            pct = str(d.get("percent", "0")).replace("~", "").strip().strip("%")
            try:
                p = float(pct)
            except ValueError:
                p = 0.0
            _set(job_id, progress=15 + min(30, int(p * 0.3)),
                 message=f"Downloading audio · {d.get('percent')} · {d.get('speed')} · ETA {d.get('eta')}")

    audio = download_audio(url, AUDIO_DIR / f"{job_id}_src.wav", progress_cb=_dl_progress,
                           use_cache=not info.get("is_live", False))
    tr = asr.transcribe(audio, ASR_DIR / job_id, progress_cb=lambda m: _set(job_id, message=m))
    segments = tr["segments"]
    _set(job_id, language=tr.get("language"), asr_backend=tr.get("backend"))
    cache.save_json(tp, {"segments": segments, "source_kind": "asr"})
    return segments, "asr", audio


def _section_for(job_id: str, url: str, start: float, end: float) -> Path:
    """Reusable content-hash section with one fresh-URL retry."""
    target = cache.section_path(url, start, end)
    if target.exists() and target.stat().st_size > 1000:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    last = None
    for attempt in range(2):
        if _cancelled(job_id):
            raise RuntimeError("cancelled")
        try:
            tmp = target.with_suffix(f".try{attempt}.mp4")
            download_section(url, start, end, tmp)
            tmp.replace(target)
            return target
        except Exception as e:  # noqa: BLE001
            last = e
            sources._MEDIA_URL_CACHE.pop(url, None)
            time.sleep(1 + attempt)
    raise RuntimeError(f"Section fetch failed: {last}")


def _local_section(video_path: str, start: float, end: float, out_path: Path) -> Path:
    """Cut an accurate section from a local live recording."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    span = max(1.0, float(end) - float(start))
    cmd = [
        shutil.which("ffmpeg") or "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, float(start)):.3f}", "-i", video_path,
        "-t", f"{span:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-shortest", str(out_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       timeout=float(os.getenv("RENDER_TIMEOUT", "480")))
    if r.returncode != 0 or not out_path.exists() or out_path.stat().st_size < 1000:
        raise RuntimeError(f"Live section cut failed: {(r.stderr or '')[-300:]}")
    return out_path


def _live_cloud_row(session: dict) -> dict:
    return {k: v for k, v in session.items() if not k.startswith("_")}


def _persist_live(session_id: str) -> None:
    session = live_sessions.get(session_id)
    if not session:
        return
    data = library.load_live_sessions()
    data[session_id] = session
    library.save_live_sessions(data)
    if cloud.enabled():
        try:
            cloud.save_live_session(_live_cloud_row(session))
        except Exception as e:  # noqa: BLE001
            print(f"[cloud] live session sync failed for {session_id}: {e}", flush=True)


def _extract_live_audio(recording: str, start: float, end: float, wav_path: Path) -> Path:
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    span = max(1.0, float(end) - float(start))
    cmd = [
        shutil.which("ffmpeg") or "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, float(start)):.3f}", "-i", recording,
        "-t", f"{span:.3f}", "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", str(wav_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not wav_path.exists() or wav_path.stat().st_size < 1000:
        raise RuntimeError(f"Live audio extraction failed: {(r.stderr or '')[-300:]}")
    return wav_path


def _stop_live_recorder(session_id: str, reason: str) -> None:
    proc = LIVE_RECORDERS.pop(session_id, None)
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:
                pass
    session = live_sessions.get(session_id)
    if not session:
        return
    session["state"] = "ended" if reason == "source_ended" else "stopped"
    session["phase"] = "done"
    session["stop_reason"] = reason
    session["next_pass_at"] = None
    session["stop_requested"] = False
    session["updated_at"] = _now()
    _persist_live(session_id)


def _run_live_pass(session_id: str) -> dict:
    """Analyze the newest clipped window and append fresh moments to its job."""
    session = live_sessions.get(session_id)
    if not session or session.get("state") != "live":
        return {"ok": False, "error": "Session is not live"}
    job = jobs.get(session.get("job_id"))
    if not job:
        return {"ok": False, "error": "Live job not found"}

    elapsed = max(0.0, time.time() - float(session.get("started_at_epoch") or time.time()))
    clip_from = float(session.get("clipped_until_seconds") or 0)
    clip_to = min(elapsed - 1.0, clip_from + float(session.get("pass_window_seconds") or 1800))
    if clip_to - clip_from < 45:
        return {"ok": True, "skipped": True, "reason": "window_too_small"}

    pass_no = int(session.get("pass_count") or 0) + 1
    wav = LIVE_DIR / session_id / f"pass_{pass_no}.wav"
    try:
        _extract_live_audio(session["recording_path"], clip_from, clip_to, wav)
        data = asr.transcribe(wav, ASR_DIR / "live" / session_id)
        window = []
        for seg in (data.get("segments") or []):
            item = dict(seg)
            item["start"] = float(item.get("start", 0)) + clip_from
            item["end"] = float(item.get("end", 0)) + clip_from
            for word in (item.get("words") or []):
                word["start"] = float(word.get("start", 0)) + clip_from
                word["end"] = float(word.get("end", 0)) + clip_from
            window.append(item)
        if not window:
            raise RuntimeError("No speech found in this live window")

        job["transcript"] = list(job.get("transcript") or []) + window
        candidates = select_clips(
            window,
            video_duration=clip_to,
            num_clips=max(2, min(8, int(session.get("target_clips") or 5))),
            clip_length=job.get("clip_length", "medium"),
            goal=session.get("curation_prompt") or job.get("goal") or "funny reactions worth rewatching",
        )
        fresh = payoff.extend_to_payoff(
            payoff.exclude_used(candidates, job.get("clips") or []), window, duration=clip_to)
        fresh = fresh[:max(1, int(session.get("target_clips") or 5))]
        if not fresh:
            raise RuntimeError("No new moments in this live window")

        hooks = _make_hooks(job, fresh)
        channel = job.get("channel") or "Unsorted"
        preset = library.load_presets().get(channel, {})
        old_count = len(job.get("clips") or [])
        for j, c in enumerate(fresh):
            c.update({
                "index": old_count + j,
                "render_status": "pending",
                "url": None,
                "error": None,
                "hooks": hooks[j] if j < len(hooks) else [c.get("title", "Watch this")],
                "hook_index": 0,
                "focus": c.get("focus") or {"cx": float(preset.get("cx", 0.10)), "cy": float(preset.get("cy", 0.30))},
                "face_shift_ms": float(preset.get("face_shift_ms", 150)),
                "watermark": str(preset.get("watermark", "@NOCTX")),
                "source_start_seconds": c.get("start"),
                "source_end_seconds": c.get("end"),
                "batch": pass_no,
                "parent_job": job["id"],
            })
        job["clips"] = list(job.get("clips") or []) + fresh
        _set(job["id"], clips=job["clips"], status="needs_review", stage="review",
             message=f"Live pass {pass_no} found {len(fresh)} new moments.")

        session["pass_count"] = pass_no
        session["clipped_until_seconds"] = clip_to
        session["phase"] = "waiting"
        session["last_error"] = None
        total = len(job["clips"])
        if total >= int(session.get("max_clips") or 60):
            session["stop_reason"] = "clip_limit_reached"
            _stop_live_recorder(session_id, "clip_limit_reached")
        else:
            session["next_pass_at"] = time.time() + float(session.get("pass_window_seconds") or 1800)
        session["updated_at"] = _now()
        _persist_live(session_id)
        return {"ok": True, "pass": pass_no, "added": len(fresh), "from": clip_from, "until": clip_to}
    except Exception as e:  # noqa: BLE001
        session["last_error"] = str(e)[:500]
        session["phase"] = "waiting"
        session["next_pass_at"] = time.time() + float(session.get("pass_window_seconds") or 1800)
        session["updated_at"] = _now()
        _persist_live(session_id)
        return {"ok": False, "pass": pass_no, "error": str(e)[:500]}


def _record_live(session_id: str) -> None:
    """Capture a live HLS stream into a local rolling recording."""
    session = live_sessions.get(session_id)
    if not session:
        return
    try:
        media_url, header_args = sources._resolve_media_url(session["src_url"])
    except Exception as e:  # noqa: BLE001
        _stop_live_recorder(session_id, "source_unavailable")
        session["last_error"] = str(e)[:500]
        _persist_live(session_id)
        return

    recording = Path(session["recording_path"])
    recording.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        shutil.which("ffmpeg") or "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        *header_args, "-i", media_url, "-c", "copy", "-f", "mpegts", str(recording),
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:  # noqa: BLE001
        _stop_live_recorder(session_id, "repeated_errors")
        session["last_error"] = str(e)[:500]
        _persist_live(session_id)
        return
    LIVE_RECORDERS[session_id] = proc
    session["phase"] = "clipping"
    session["updated_at"] = _now()
    _persist_live(session_id)

    started = float(session.get("started_at_epoch") or time.time())
    first = float(session.get("first_pass_seconds") or 900)
    window = float(session.get("pass_window_seconds") or 1800)
    session["next_pass_at"] = started + first
    _persist_live(session_id)

    while proc.poll() is None:
        current = live_sessions.get(session_id)
        if not current or current.get("stop_requested") or current.get("state") != "live":
            break
        if time.time() >= float(current.get("next_pass_at") or 0):
            _run_live_pass(session_id)
            current = live_sessions.get(session_id)
            if not current or current.get("state") != "live":
                break
            current["next_pass_at"] = time.time() + window
            _persist_live(session_id)
        time.sleep(5)
    current = live_sessions.get(session_id)
    if current and current.get("state") == "live":
        _stop_live_recorder(session_id, "source_ended")


def _save_publication(item: dict) -> None:
    for i, existing in enumerate(publications):
        if existing.get("id") == item.get("id"):
            publications[i] = item
            break
    else:
        publications.append(item)
    library.save_publications(publications)
    if cloud.enabled():
        try:
            cloud.save_publication(item)
        except Exception as e:  # noqa: BLE001
            print(f"[cloud] publication sync failed: {e}", flush=True)


def _woopsocial_payload(item: dict) -> dict:
    job = jobs.get(item.get("job_id"), {})
    idx = int(item.get("clip_idx") or 0)
    clip = (job.get("clips") or [])[idx] if 0 <= idx < len(job.get("clips") or []) else {}
    base = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    media_url = str(clip.get("url") or "")
    if media_url.startswith("/"):
        media_url = base + media_url
    tags = item.get("hashtags") or []
    caption = str(item.get("caption") or clip.get("hook_title") or clip.get("title") or "")
    if tags:
        caption = caption.rstrip() + "\n\n" + " ".join("#" + str(t).lstrip("#") for t in tags)
    return {
        "platform": item.get("platform"),
        "account": item.get("social_account"),
        "text": caption,
        "media_url": media_url,
        "scheduled_at": item.get("scheduled_at"),
    }


def _post_to_woopsocial(item: dict) -> dict:
    """Post through WoopSocial when API credentials are configured."""
    payload = _woopsocial_payload(item)
    api = os.getenv("WOOP_SOCIAL_API_URL", "").rstrip("/")
    key = os.getenv("WOOP_SOCIAL_API_KEY", "")
    if not api or not key:
        return {
            "ok": False,
            "needs_integration": True,
            "error": "WoopSocial API is not configured. Add WOOP_SOCIAL_API_URL and WOOP_SOCIAL_API_KEY to .env.",
            "payload": payload,
        }
    try:
        r = requests.post(
            f"{api}/posts",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code >= 300:
            return {"ok": False, "error": str(data.get("message") or data.get("error") or r.text[:300]), "payload": payload}
        return {"ok": True, "post_id": data.get("id") or data.get("post_id"), "payload": payload}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:400], "payload": payload}


def _local_words(job: dict, start: float, end: float) -> list[dict]:
    out = []
    for w in _flat_words(job):
        ws, we = float(w.get("start", 0)), float(w.get("end", 0))
        if we >= start and ws <= end + 1:
            out.append({
                "word": w.get("word", ""),
                "start": max(0.0, ws - start),
                "end": max(0.0, we - start),
            })
    return out


def _mark_render_failure(clip: dict, error: str) -> None:
    """Keep a previously successful video playable if a re-render fails."""
    previous = clip.get("last_url") or clip.get("url")
    clip["render_error"] = str(error)[:1200]
    if previous and not str(previous).endswith("?draft=1"):
        clip["render_status"] = "done"
        clip["url"] = previous
        clip["error"] = "Re-render failed — showing the previous version."
    else:
        clip["render_status"] = "error"
        clip["url"] = None
        clip["error"] = str(error)


def _render_one(job_id: str, i: int, clip: dict, draft: bool = False) -> dict:
    if _cancelled(job_id):
        return {"ok": False, "error": "cancelled", "index": i}
    job = jobs[job_id]
    settings = job.get("settings", {})
    out_path = CLIP_DIR / f"{job_id}_clip_{i}.mp4"
    if draft:
        out_path = CLIP_DIR / f"{job_id}_draft_{i}.mp4"
    start, end = float(clip["start"]), float(clip["end"])
    is_url = bool(job.get("source_url"))
    is_live = bool(job.get("is_live")) and not is_url
    layout = settings.get("layout", "split" if is_url else "source")
    use_split = layout == "split" and (is_url or is_live)

    try:
        if is_url:
            sec = _section_for(job_id, job["source_url"], start, end)
            input_path = str(sec)
            local_end = probe_duration(input_path) or max(2, end - start)
        elif is_live and use_split:
            sec = _local_section(job.get("video_path"), start, end, SECTION_DIR / f"live_{job_id}_{i}.mp4")
            input_path = str(sec)
            local_end = probe_duration(input_path) or max(2, end - start)
        else:
            input_path = job.get("video_path")
            local_end = end

        words = _local_words(job, start, end)
        encoder, eargs = hardware.encoder_args(True)
        hooks = clip.get("hooks") or [clip.get("title", "Watch this")]
        hook = str(hooks[int(clip.get("hook_index", 0))] if hooks else clip.get("title", ""))[:90]

        if use_split:
            from layout import render_split
            focus = clip.get("focus") or {}
            result = render_split(
                input_path, str(out_path), local_end,
                face_cx=float(focus.get("cx", 0.10)),
                face_cy=float(focus.get("cy", 0.28)),
                words=words if settings.get("captions", True) and not draft else None,
                hook=hook,
                caption_start=0.0,
                face_shift_ms=float(clip.get("face_shift_ms", 150)),
                watermark=str(clip.get("watermark", "@NOCTX")) if not draft else "",
                draft=draft, encoder=encoder, encoder_args=eargs,
            )
        else:
            result = render_clip(
                input_path, {"start": 0.0 if (is_url or use_split) else start, "end": local_end},
                str(out_path), words=words if not draft else None,
                captions=bool(settings.get("captions", True)) and not draft,
                vertical=bool(settings.get("vertical", True)),
                draft=draft, encoder=encoder, encoder_args=eargs,
            )

        if result.get("ok"):
            clip["render_status"] = "draft" if draft else "done"
            clip["url"] = f"/api/clips/{job_id}/{i}" if not draft else f"/api/clips/{job_id}/{i}?draft=1"
            clip["last_url"] = clip["url"]
            clip["captions"] = result.get("captions", False)
            clip["error"] = None
            clip["render_error"] = None
            clip["encoder"] = result.get("encoder")
            if result.get("size"):
                clip["size_bytes"] = result["size"]
            try:
                clip["width"], clip["height"] = probe_size(str(out_path))
            except Exception:  # noqa: BLE001
                pass
            if not draft and cloud.enabled():
                try:
                    dest = cloud.upload_file(out_path, f"{job_id}/{i}.mp4")
                    clip["storage_path"] = dest
                    clip["cloud"] = True
                    # Cloud copy is verified; release local disk immediately.
                    try:
                        out_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                except Exception as e:  # noqa: BLE001
                    clip["cloud_error"] = str(e)[:400]
                    print(f"[cloud] upload failed for {job_id}/{i}: {e}", flush=True)
        else:
            _mark_render_failure(clip, result.get("error", "render failed"))
    except Exception as e:  # noqa: BLE001
        _mark_render_failure(clip, str(e))
    return {"ok": clip.get("render_status") in ("done", "draft"), "index": i}


_RENDER_LOCKS: dict[str, threading.Lock] = {}
_RENDER_LOCKS_GUARD = threading.Lock()


def _render_lock(job_id: str) -> threading.Lock:
    with _RENDER_LOCKS_GUARD:
        return _RENDER_LOCKS.setdefault(job_id, threading.Lock())


def _start_render(job_id: str, indices: list[int] | None, draft: bool = False) -> bool:
    """Start a render thread, rejecting overlapping renders on the same job."""
    lock = _render_lock(job_id)
    if not lock.acquire(blocking=False):
        return False
    t = threading.Thread(target=_render_all, args=(job_id, indices, draft, lock), daemon=True)
    t.start()
    return True


def _render_all(job_id: str, indices: list[int] | None = None, draft: bool = False,
                lock: threading.Lock | None = None) -> None:
    own_lock = False
    if lock is None:
        lock = _render_lock(job_id)
        own_lock = lock.acquire(blocking=False)
        if not own_lock:
            return
    try:
        _render_all_locked(job_id, indices, draft)
    finally:
        lock.release()


def _render_all_locked(job_id: str, indices: list[int] | None = None, draft: bool = False) -> None:
    job = jobs.get(job_id)
    if not job:
        return
    clips = job.get("clips", [])
    targets = indices if indices is not None else list(range(len(clips)))
    total = max(1, len(targets))
    done = 0
    _set(job_id, stage="render", status="processing", progress=55, message="Rendering clips...")
    with ThreadPoolExecutor(max_workers=RENDER_WORKERS) as pool:
        futures = {pool.submit(_render_one, job_id, i, clips[i], draft): i for i in targets if 0 <= i < len(clips)}
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                fut.result()
            except Exception as e:  # noqa: BLE001
                _mark_render_failure(clips[i], str(e))
            done += 1
            _set(job_id, progress=55 + int(40 * done / total),
                 message=f"Rendered {done}/{total} · {hardware.ffmpeg_label()}",
                 clips=list(clips))
            if _cancelled(job_id):
                break
    _set(job_id, clips=list(clips))
    if _cancelled(job_id):
        _set(job_id, status="cancelled", stage="cancelled", progress=100,
             message="Cancelled", cancelled=True)
    else:
        _set(job_id, stage="done", progress=100, status="done",
             message="Drafts ready — review and render finals." if draft else "Your clips are ready!")


def _flat_words(job: dict) -> list[dict]:
    words: list[dict] = []
    for seg in job.get("transcript") or []:
        words.extend(seg.get("words") or [])
    return words


def _finish_selection(job_id: str) -> None:
    """Selection-only stop point for the review screen."""
    job = jobs.get(job_id)
    if not job:
        return
    if job.get("draft"):
        _set(job_id, status="drafting", stage="render", progress=60, message="Rendering quick drafts...")
        _render_all(job_id, draft=True)
        _set(job_id, status="needs_review", message="Drafts ready — pick the clips to keep.")
    else:
        _set(job_id, status="needs_review", stage="review", progress=55,
             message="Moments ranked — review before rendering.")


def _url_pipeline(job_id: str):
    try:
        job = jobs.get(job_id)
        if not job or _cancelled(job_id):
            return
        url = job["source_url"]
        _set(job_id, status="processing", stage="resolve", progress=5,
             message="Resolving link and reading video info...")
        info = resolve_url(url)
        channel = library.channel_for(info, url)
        _set(job_id, source_info=info, duration=round(info["duration"], 2), channel=channel)

        segments, source_kind, audio = _get_source_transcript(job_id, url, info)
        _set(job_id, stage="analyze", progress=45, message="AI is finding your best moments...",
             transcript_source=source_kind)
        _finalize_selection(job_id, segments, float(info["duration"]), url=url, audio_path=audio)

        if job.get("review") or job.get("draft"):
            _finish_selection(job_id)
            return
        _render_all(job_id)
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="cancelled" if _cancelled(job_id) else "error",
             progress=100, message=_friendly_error(e), error=_friendly_error(e))


def _pipeline(job_id: str):
    """Local-file pipeline."""
    try:
        job = jobs.get(job_id)
        if not job or _cancelled(job_id):
            return
        video_path = Path(job["video_path"])
        _set(job_id, status="processing", stage="audio", progress=8, message="Extracting audio...")
        audio_path = AUDIO_DIR / f"{job_id}.wav"
        _extract_audio(video_path, audio_path)
        _set(job_id, stage="transcribe", progress=20, message="Transcribing speech...")
        tr = asr.transcribe(audio_path, ASR_DIR / job_id, progress_cb=lambda m: _set(job_id, message=m))
        _set(job_id, language=tr.get("language"), asr_backend=tr.get("backend"))
        _set(job_id, stage="analyze", progress=50, message="AI is finding your best moments...")
        _finalize_selection(job_id, tr["segments"], float(job.get("duration") or 0), audio_path=audio_path)
        if job.get("review") or job.get("draft"):
            _finish_selection(job_id)
            return
        _render_all(job_id)
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="cancelled" if _cancelled(job_id) else "error",
             progress=100, message=_friendly_error(e), error=_friendly_error(e))


def _pipeline_from_transcript(job_id: str):
    """Skip ASR entirely — use an imported transcript (free-GPU notebook)."""
    try:
        job = jobs.get(job_id)
        if not job or _cancelled(job_id):
            return
        if job.get("source_url") and not job.get("source_info"):
            _set(job_id, progress=5, message="Resolving source info...")
            info = resolve_url(job["source_url"])
            _set(job_id, source_info=info,
                 channel=library.channel_for(info, job["source_url"]))
        duration = float(job.get("duration") or 0)
        _set(job_id, stage="analyze", progress=50, message="AI is finding your best moments...")
        _finalize_selection(job_id, job.get("transcript") or [], duration,
                            url=job.get("source_url"))
        _set(job_id, transcript_source="imported")
        if job.get("review") or job.get("draft"):
            _finish_selection(job_id)
            return
        _render_all(job_id)
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="cancelled" if _cancelled(job_id) else "error",
             progress=100, message=_friendly_error(e), error=_friendly_error(e))


# ----------------------------- API routes -----------------------------

@app.get("/api/health")
def health():
    return {"ok": True, "jobs": len(jobs), "encoder": hardware.ffmpeg_label()}


@app.get("/api/public-config")
def public_config():
    return supabase_auth.public_config()


@app.post("/api/auth/verify")
async def auth_verify(request: Request):
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    user = supabase_auth.verify(token)
    if not user:
        raise HTTPException(401, "Invalid or expired session")
    return {"ok": True, "user": {"id": user.get("id"), "email": user.get("email")}}


class SignupBody(BaseModel):
    email: str
    password: str


@app.post("/api/auth/signup")
async def auth_signup(body: SignupBody, request: Request):
    email = (body.email or "").strip().lower()
    password = body.password or ""
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(400, "Enter a valid email address")
    if len(password) < 6 or len(password) > 128:
        raise HTTPException(400, "Password must be 6–128 characters")

    ip = request.client.host if request.client else "unknown"
    key = (ip, email)
    now = time.time()
    with _SIGNUP_LOCK:
        recent = [t for t in _SIGNUP_ATTEMPTS.get(key, []) if now - t < 3600]
        if len(recent) >= 5:
            raise HTTPException(429, "Too many attempts. Try again later.")
        recent.append(now)
        _SIGNUP_ATTEMPTS[key] = recent

    result = supabase_auth.signup(email, password)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "Could not create account")
    return result


class UrlBody(BaseModel):
    url: str


@app.post("/api/source")
async def source_info(body: UrlBody):
    try:
        return {"ok": True, **resolve_url(body.url)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Could not read that link: {_friendly_error(e)}")


@app.post("/api/clip-url")
async def clip_url(
    url: str = Form(...),
    num_clips: int = Form(5),
    clip_length: str = Form("medium"),
    captions: bool = Form(True),
    vertical: bool = Form(True),
    goal: str = Form(""),
    review: bool = Form(False),
    draft: bool = Form(False),
    layout: str = Form("split"),
):
    job_id = _new_job(
        source_url=url,
        num_clips=max(1, min(num_clips, 24)),
        clip_length=clip_length,
        settings={"captions": captions, "vertical": vertical, "layout": layout},
        goal=goal,
        review=review,
        draft=draft,
        message="Resolving link...",
    )
    threading.Thread(target=_url_pipeline, args=(job_id,), daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/upload")
async def upload(
    file: UploadFile = File(...),
    num_clips: int = Form(5),
    clip_length: str = Form("medium"),
    captions: bool = Form(True),
    vertical: bool = Form(False),
    goal: str = Form(""),
    review: bool = Form(False),
    draft: bool = Form(False),
):
    if not file.filename:
        raise HTTPException(400, "No filename")
    ext = Path(file.filename).suffix.lower() or ".mp4"
    job_id = uuid.uuid4().hex[:12]
    video_path = UPLOAD_DIR / f"{job_id}{ext}"
    with open(video_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    if video_path.stat().st_size / (1024 * 1024) > 2048:
        video_path.unlink(missing_ok=True)
        raise HTTPException(400, "Video too large (max 2 GB)")
    duration = probe_duration(str(video_path))
    if duration <= 1:
        video_path.unlink(missing_ok=True)
        raise HTTPException(400, "Could not read the video. Unsupported format?")
    jobs[job_id] = {
        "id": job_id, "filename": file.filename, "video_path": str(video_path),
        "duration": round(duration, 2), "num_clips": max(1, min(num_clips, 24)),
        "clip_length": clip_length,
        "settings": {"captions": captions, "vertical": vertical, "layout": "source"},
        "status": "queued", "stage": "queued", "progress": 0, "message": "Queued...",
        "transcript": [], "clips": [], "error": None, "source_url": None,
        "source_info": None, "goal": goal, "created_at": _now(),
        "cancel_requested": False, "cancelled": False, "notes": {},
        "channel": library.clean_channel("Local uploads"), "review": review, "draft": draft,
        "parent_job": None,
    }
    _persist(job_id)
    threading.Thread(target=_pipeline, args=(job_id,), daemon=True).start()
    return {"job_id": job_id, "duration": round(duration, 2)}


@app.post("/api/import-transcript")
async def import_transcript(
    transcript: UploadFile = File(...),
    url: str = Form(""),
    num_clips: int = Form(5),
    clip_length: str = Form("medium"),
    captions: bool = Form(True),
    vertical: bool = Form(True),
    goal: str = Form(""),
    review: bool = Form(False),
    draft: bool = Form(False),
):
    try:
        data = json.loads(await transcript.read())
        segments = data.get("segments", data if isinstance(data, list) else [])
        if not segments:
            raise HTTPException(400, "Transcript has no segments")
        cleaned = []
        for s in segments:
            try:
                cleaned.append({
                    "start": float(s.get("start", 0)), "end": float(s.get("end", 0)),
                    "text": str(s.get("text", "")).strip(),
                    "words": [{"word": str(w.get("word", "")), "start": float(w.get("start", 0)),
                               "end": float(w.get("end", 0))} for w in (s.get("words") or [])],
                })
            except (TypeError, ValueError):
                continue
        if not cleaned:
            raise HTTPException(400, "No valid segments in transcript")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Invalid transcript JSON: {e}")

    source_url = url.strip() or None
    duration = 0.0
    info = None
    if source_url:
        try:
            info = resolve_url(source_url)
            duration = info["duration"]
        except Exception:
            source_url = None
    if not duration:
        duration = max(float(s["end"]) for s in cleaned)

    job_id = _new_job(
        source_url=source_url, source_info=info,
        channel=library.channel_for(info, source_url),
        num_clips=max(1, min(num_clips, 24)), clip_length=clip_length,
        settings={"captions": captions, "vertical": vertical, "layout": "split" if source_url else "source"},
        goal=goal, transcript=cleaned, duration=round(duration, 2),
        language=data.get("language", "imported"), asr_backend=data.get("backend", "imported"),
        transcript_source="imported", review=review, draft=draft,
        message="Using imported transcript...",
    )
    threading.Thread(target=_pipeline_from_transcript, args=(job_id,), daemon=True).start()
    return {"job_id": job_id, "segments": len(cleaned)}


class RenderSelectedBody(BaseModel):
    indices: list[int]
    captions: bool | None = None
    vertical: bool | None = None
    draft: bool = False
    hooks: dict[str, int] | None = None


@app.post("/api/render-selected/{job_id}")
async def render_selected(job_id: str, body: RenderSelectedBody):
    job = jobs.get(job_id)
    if not job or not job.get("clips"):
        raise HTTPException(404, "Job with clips not found")
    if body.captions is not None:
        job["settings"]["captions"] = body.captions
    if body.vertical is not None:
        job["settings"]["vertical"] = body.vertical
    for idx in body.indices:
        if 0 <= idx < len(job["clips"]):
            c = job["clips"][idx]
            c["render_status"] = "pending"
            # Keep the last good video watchable until the new render succeeds.
            if c.get("url"):
                c["last_url"] = c["url"]
            c["error"] = None
            c["selected"] = True
            if body.hooks and str(idx) in body.hooks:
                c["hook_index"] = int(body.hooks[str(idx)])
    job["draft"] = body.draft
    _set(job_id, clips=list(job["clips"]), status="processing", stage="render")
    if not _start_render(job_id, body.indices, body.draft):
        raise HTTPException(409, "A render is already running for this job. Wait or cancel it.")
    return {"ok": True, "rendering": len(body.indices)}


class ContinueBody(BaseModel):
    num_clips: int = 5
    goal: str = ""
    auto_render: bool = True


class RegenerateBody(BaseModel):
    goal: str = ""


@app.post("/api/continue/{job_id}")
async def continue_clipping(job_id: str, body: ContinueBody):
    """Find the NEXT batch from a long source, excluding moments already used."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    transcript = job.get("transcript") or []
    if not transcript and job.get("source_url"):
        hit = cache.load_json(cache.transcript_path(job["source_url"]))
        transcript = (hit or {}).get("segments", [])
        job["transcript"] = transcript
    if not transcript:
        raise HTTPException(400, "This job has no transcript to reclip")
    duration = float(job.get("duration") or max(float(s.get("end", 0)) for s in transcript))
    used = job.get("clips") or []
    want = max(1, min(body.num_clips, 24))

    candidates = select_clips(
        transcript, video_duration=duration,
        num_clips=want + len(used), clip_length=job.get("clip_length", "medium"),
        goal=body.goal or job.get("goal", ""),
    )
    fresh = payoff.exclude_used(candidates, used)
    fresh = payoff.extend_to_payoff(fresh[:want], transcript, duration=duration)
    if not fresh:
        raise HTTPException(400, "No fresh moments left in this source")

    old_count = len(used)
    hooks = _make_hooks(job, fresh)
    for j, c in enumerate(fresh):
        idx = old_count + j
        c["index"] = idx
        c["render_status"] = "pending"
        c["url"] = None
        c["error"] = None
        c["hooks"] = hooks[j] if j < len(hooks) else [c.get("title", "Watch this")]
        c["hook_index"] = 0
        c["focus"] = c.get("focus") or {"cx": 0.10, "cy": 0.30}
        c["face_shift_ms"] = 150
        c["watermark"] = "@NOCTX"
        c["parent_job"] = job_id
        c["batch"] = len([x for x in used if x.get("parent_job")]) + 1
    updated = used + fresh
    _set(job_id, clips=updated, goal=body.goal or job.get("goal", ""),
         status="processing", stage="analyze", progress=50,
         message=f"Found {len(fresh)} more moments...")
    if body.auto_render:
        if not _start_render(job_id, list(range(old_count, len(updated))), False):
            raise HTTPException(409, "A render is already running for this job. Wait or cancel it.")
    else:
        _set(job_id, status="needs_review", stage="review", message="More moments ready to review.")
    return {"job_id": job_id, "added": len(fresh), "total": len(updated)}


@app.post("/api/retry-failed/{job_id}")
async def retry_failed(job_id: str):
    """Render every failed or not-yet-rendered clip in one pass."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    indices = [
        i for i, c in enumerate(job.get("clips") or [])
        if c.get("render_status") in ("error", "pending")
    ]
    if not indices:
        return {"ok": True, "retrying": 0}
    for i in indices:
        job["clips"][i]["render_status"] = "pending"
        job["clips"][i]["error"] = None
        job["clips"][i]["render_error"] = None
    sources._MEDIA_URL_CACHE.pop(job.get("source_url"), None)
    _set(job_id, clips=list(job["clips"]), status="processing", stage="render",
         message=f"Rendering {len(indices)} remaining clips...")
    if not _start_render(job_id, indices, False):
        raise HTTPException(409, "A render is already running for this job. Wait or cancel it.")
    return {"ok": True, "retrying": len(indices)}


@app.post("/api/cancel/{job_id}")
async def cancel_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    _set(job_id, cancel_requested=True, message="Cancelling after the current clip...")
    return {"ok": True}


@app.post("/api/retry/{job_id}/{index}")
async def retry_clip(job_id: str, index: int):
    job = jobs.get(job_id)
    if not job or not (0 <= index < len(job.get("clips") or [])):
        raise HTTPException(404, "Clip not found")
    sources._MEDIA_URL_CACHE.pop(job.get("source_url"), None)
    job["clips"][index]["render_status"] = "pending"
    job["clips"][index]["error"] = None
    _set(job_id, clips=list(job["clips"]), status="processing", stage="render")
    if not _start_render(job_id, [index], False):
        raise HTTPException(409, "A render is already running for this job. Wait or cancel it.")
    return {"ok": True}


@app.post("/api/regenerate/{job_id}/{index}")
async def regenerate_clip(job_id: str, index: int, body: RegenerateBody):
    """Find another take on one moment instead of re-clipping the whole source."""
    job = jobs.get(job_id)
    if not job or not (0 <= index < len(job.get("clips") or [])):
        raise HTTPException(404, "Clip not found")
    current = dict(job["clips"][index])
    start = float(current.get("start", 0))
    end = float(current.get("end", start + 30))
    center = (start + end) / 2
    duration = float(job.get("duration") or end)

    transcript = job.get("transcript") or []
    if not transcript and job.get("source_url"):
        hit = cache.load_json(cache.transcript_path(job["source_url"]))
        transcript = (hit or {}).get("segments", [])
        job["transcript"] = transcript

    replacement = None
    if transcript:
        hint = " ".join(str(current.get(k, "")) for k in ("title", "reason")).strip()
        goal = " ".join(x for x in (body.goal.strip(), hint, job.get("goal", "")) if x).strip()
        try:
            candidates = select_clips(
                transcript,
                video_duration=duration,
                num_clips=max(8, min(14, len(transcript) // 8 or 8)),
                clip_length=job.get("clip_length", "medium"),
                goal=f"alternate take on this exact moment: {goal}",
            )
        except Exception:
            candidates = []
        best = None
        best_delta = float("inf")
        for cand in candidates:
            cs, ce = float(cand.get("start", 0)), float(cand.get("end", 0))
            if ce <= cs:
                continue
            overlap = min(end, ce) - max(start, cs)
            ccenter = (cs + ce) / 2
            delta = abs(ccenter - center)
            if overlap <= 0 or abs(cs - start) < 2:
                delta += 600
            if delta < best_delta:
                best, best_delta = cand, delta
        if best is not None and best_delta < 600:
            replacement = dict(best)

    if replacement is None:
        span = max(8.0, end - start)
        new_start = max(0.0, min(duration - span, start - span * 0.35 if center > duration / 2 else start + span * 0.35))
        replacement = {
            "start": round(new_start, 2),
            "end": round(min(duration, new_start + span), 2),
            "title": current.get("title", "Another take"),
            "reason": "Alternate window around the same moment",
            "score": current.get("score", 0),
        }

    dictionary.apply_segments(transcript)
    replacement.update({
        "index": index,
        "render_status": "pending",
        "url": None,
        "error": None,
        "hooks": [],
        "hook_index": 0,
        "focus": current.get("focus") or {"cx": 0.10, "cy": 0.30},
        "face_shift_ms": current.get("face_shift_ms", 150),
        "watermark": current.get("watermark", "@NOCTX"),
        "batch": current.get("batch", 1),
        "parent_job": job_id,
    })
    replacement["title"] = dictionary.apply_text(str(replacement.get("title", "")), dictionary.load())
    replacement["reason"] = dictionary.apply_text(str(replacement.get("reason", "")), dictionary.load())
    hooks = _make_hooks(job, [replacement])
    replacement["hooks"] = hooks[0] if hooks else [replacement.get("title", "Watch this")]
    job["clips"][index] = replacement
    _set(job_id, clips=list(job["clips"]), status="processing", stage="render",
         progress=55, message=f"Regenerating moment {index + 1}...")
    if not _start_render(job_id, [index], False):
        raise HTTPException(409, "A render is already running for this job. Wait or cancel it.")
    return {"ok": True, "index": index, "clip": replacement}


TOOL_SPECS = [
    {"name": "start_live_session", "description": "Start recording a live URL and run rolling clipping passes."},
    {"name": "stop_live_session", "description": "Stop a live recording session."},
    {"name": "get_live_status", "description": "List live sessions and their pass state."},
    {"name": "get_jobs", "description": "List saved source jobs grouped by streamer."},
    {"name": "get_clips", "description": "Get scored moments for a job."},
    {"name": "get_transcript", "description": "Get the transcript for a job."},
    {"name": "cut_clip_from_range", "description": "Add a manual moment from exact start/end seconds."},
    {"name": "render_clip", "description": "Render one or more selected clip indices."},
    {"name": "write_post_caption", "description": "Write a short platform caption with tags."},
    {"name": "schedule_publication", "description": "Schedule an approved clip post."},
    {"name": "get_usage", "description": "Summarize jobs, clips, live sessions, and scheduled posts."},
]


async def _run_tool(name: str, args: dict) -> dict:
    args = args or {}
    if name == "list_tools" or name == "get_tools":
        return {"tools": TOOL_SPECS}
    if name == "start_live_session":
        return await start_live(LiveStartBody(**args))
    if name == "stop_live_session":
        sid = str(args.get("session_id") or "")
        return await stop_live(sid)
    if name == "get_live_status":
        sid = args.get("session_id")
        return get_live_session(str(sid)) if sid else list_live_sessions()
    if name == "get_jobs":
        return {"groups": library.grouped()}
    if name == "get_clips":
        job_id = str(args.get("job_id") or "")
        if job_id not in jobs:
            raise HTTPException(404, "Job not found")
        _ensure_clip_extras(jobs[job_id])
        return {"job_id": job_id, "clips": jobs[job_id].get("clips", [])}
    if name == "get_transcript":
        return get_transcript(str(args.get("job_id") or ""))
    if name == "cut_clip_from_range":
        job_id = str(args.get("job_id") or "")
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        start = float(args.get("start", 0))
        end = float(args.get("end", start + 30))
        if end <= start:
            raise HTTPException(400, "End must be after start")
        clip = {
            "index": len(job.get("clips") or []),
            "start": round(start, 2), "end": round(end, 2),
            "title": str(args.get("title") or "Manual cut"),
            "reason": str(args.get("reason") or "Cut from an exact range in Agent."),
            "score": float(args.get("score") or 80),
            "render_status": "pending", "url": None, "error": None,
            "hooks": [str(args.get("title") or "Manual cut")], "hook_index": 0,
            "watermark": "@NOCTX", "face_shift_ms": 150,
            "focus": {"cx": 0.10, "cy": 0.30}, "batch": 0, "parent_job": job_id,
        }
        job["clips"] = list(job.get("clips") or []) + [clip]
        _set(job_id, clips=job["clips"], status="needs_review", stage="review",
             message="Manual range added. Render it when ready.")
        return {"ok": True, "index": clip["index"], "clip": clip}
    if name == "render_clip":
        job_id = str(args.get("job_id") or "")
        indices = [int(x) for x in (args.get("indices") or [])]
        return await render_selected(job_id, RenderSelectedBody(indices=indices))
    if name == "write_post_caption":
        hook = str(args.get("hook") or "watch this")
        platform = str(args.get("platform") or "tiktok").lower()
        tags = [str(x).lstrip("#") for x in (args.get("hashtags") or [])][:5]
        if not tags:
            tags = ["gta6", "gta6clips", "fyp"] if platform == "tiktok" else ["shorts", "gta6"]
        return {"caption": hook.lower().strip(), "hashtags": tags,
                "text": hook.lower().strip() + "\n\n" + " ".join("#" + t for t in tags)}
    if name == "schedule_publication":
        return await create_publication(PublicationBody(**args))
    if name == "get_usage":
        return {
            "jobs": len(jobs),
            "clips": sum(len(j.get("clips") or []) for j in jobs.values()),
            "live_sessions": len(live_sessions),
            "publications": len(publications),
        }
    raise ValueError(f"Unknown tool: {name}")


@app.get("/api/tools")
def list_tool_specs():
    return {"tools": TOOL_SPECS}


class ToolCallBody(BaseModel):
    name: str
    args: dict | None = None


@app.post("/api/tools/call")
async def call_tool(body: ToolCallBody, request: Request):
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    user = supabase_auth.verify(token) if token else None
    ok = True
    error = None
    try:
        result = await _run_tool(body.name, body.args or {})
    except HTTPException as e:
        ok, error, result = False, str(e.detail), {"detail": e.detail}
    except Exception as e:  # noqa: BLE001
        ok, error, result = False, str(e)[:400], {"detail": str(e)[:400]}
    cloud.record_tool_call(body.name, args=body.args or {}, ok=ok, error=error,
                           user_email=(user or {}).get("email"))
    return {"ok": ok, "tool": body.name, "result": result, "error": error}


def _chat_tool_request(message: str) -> tuple[str, dict] | None:
    text = message.strip()
    low = text.lower()
    if low in ("tools", "what tools", "list tools", "/tools"):
        return "list_tools", {}
    if low.startswith("live status") or low.startswith("/live status"):
        return "get_live_status", {}
    if low.startswith("stop live") or low.startswith("/stop live"):
        parts = text.split()
        return "stop_live_session", {"session_id": parts[2] if len(parts) > 2 else ""}
    if low.startswith("start live") or low.startswith("/live"):
        import re
        m = re.search(r"https?://\S+", text)
        if m:
            return "start_live_session", {"url": m.group().rstrip(".,)")}
    return None


@app.post("/api/chat")
async def chat(message: str = Form(...), job_id: str | None = Form(None)):
    parsed = agent.parse_request(message)
    tool_request = _chat_tool_request(message)
    if tool_request:
        tool_name, tool_args = tool_request
        try:
            tool_result = await _run_tool(tool_name, tool_args)
            cloud.record_tool_call(tool_name, args=tool_args, ok=True)
            brief = json.dumps(tool_result.get("result", tool_result), ensure_ascii=False)[:1600]
            reply = agent.reply(message, f"Tool {tool_name} completed. Result: {brief}")
            return {"reply": reply, "job_id": job_id, "parsed": {**parsed, "tool": tool_name}, "tool_result": tool_result}
        except Exception as e:  # noqa: BLE001
            cloud.record_tool_call(tool_name, args=tool_args, ok=False, error=str(e))
            return {"reply": f"Tool {tool_name} failed: {e}", "job_id": job_id, "parsed": parsed}
    job = jobs.get(job_id or "")
    context = ""
    if job:
        clips = job.get("clips", [])
        context = (
            f"Job {job['id']}: status={job['status']}, progress={job.get('progress')}%, "
            f"clips={len(clips)}, source="
            + str((job.get("source_info") or {}).get("title") or job.get("filename") or "source")
            + ". Clips: " + ", ".join(f"{c.get('title')} ({c.get('start')}-{c.get('end')}s)" for c in clips[:8])
        )
    result_job_id = job_id
    if parsed["action"] == "clip" and parsed.get("url"):
        result_job_id = _new_job(
            source_url=parsed["url"],
            num_clips=parsed.get("num_clips", 5),
            clip_length=parsed.get("clip_length", "medium"),
            settings={"captions": parsed.get("captions", True), "vertical": parsed.get("vertical", True), "layout": "split"},
            goal=parsed.get("goal") or message, message="Resolving link...",
        )
        threading.Thread(target=_url_pipeline, args=(result_job_id,), daemon=True).start()
        reply = agent.reply(
            message,
            f"On it. I'll pull the source, find {parsed.get('num_clips', 5)} moments with payoff-aware timing.\n" + context,
        )
    elif parsed["action"] == "rerender" and job and job.get("clips"):
        job["settings"] = {
            "captions": parsed.get("captions", job["settings"].get("captions", True)),
            "vertical": parsed.get("vertical", job["settings"].get("vertical", True)),
        }
        for c in job["clips"]:
            c["render_status"] = "pending"
            if c.get("url"):
                c["last_url"] = c["url"]
        if not _start_render(job["id"], None, False):
            raise HTTPException(409, "A render is already running for this job. Wait or cancel it.")
        reply = agent.reply(message, "Re-rendering the existing clips.\n" + context)
    else:
        reply = agent.reply(message, context)
    return {"reply": reply, "job_id": result_job_id, "parsed": parsed}


@app.get("/api/status/{job_id}")
def status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    _ensure_clip_extras(job)
    return {
        "id": job["id"], "status": job["status"], "stage": job.get("stage"),
        "progress": job.get("progress", 0), "message": job.get("message", ""),
        "duration": job.get("duration"), "language": job.get("language"),
        "clips": job.get("clips", []), "error": job.get("error"),
        "settings": job.get("settings", {}), "goal": job.get("goal", ""),
        "source_info": job.get("source_info"), "source_url": job.get("source_url"),
        "transcript_source": job.get("transcript_source"), "asr_backend": job.get("asr_backend"),
        "channel": job.get("channel"), "notes": job.get("notes", {}),
        "parent_job": job.get("parent_job"), "draft": job.get("draft", False),
        "is_live": job.get("is_live", False), "live_url": job.get("live_url"),
        "cancel_requested": job.get("cancel_requested", False),
    }


class LiveStartBody(BaseModel):
    url: str
    goal: str = ""
    num_clips: int = 5
    max_clips: int = 60
    max_duration_seconds: int = 43200
    first_pass_seconds: int = 900
    pass_window_seconds: int = 1800


@app.post("/api/live/start")
async def start_live(body: LiveStartBody):
    url = (body.url or "").strip()
    if not url:
        raise HTTPException(400, "Enter a live URL")
    try:
        info = resolve_url(url)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Could not read that live link: {_friendly_error(e)}")

    session_id = uuid.uuid4().hex[:12]
    recording = LIVE_DIR / session_id / "stream.ts"
    job_id = _new_job(
        source_url=None,
        source_info=info,
        channel=library.channel_for(info, url),
        video_path=str(recording),
        duration=float(body.max_duration_seconds),
        num_clips=max(1, min(body.num_clips, 24)),
        clip_length="medium",
        settings={"captions": True, "vertical": True, "layout": "split"},
        goal=body.goal or "funny reactions worth rewatching",
        is_live=True,
        live_url=url,
        message="Recording live stream...",
    )
    now = time.time()
    session = {
        "id": session_id,
        "job_id": job_id,
        "provider": str(info.get("platform") or "live"),
        "src_url": url,
        "state": "live",
        "phase": "clipping",
        "pass_count": 0,
        "clipped_from_seconds": 0.0,
        "clipped_until_seconds": 0.0,
        "next_pass_at": now + max(30, body.first_pass_seconds),
        "stop_reason": None,
        "target_clips": max(1, min(body.num_clips, 24)),
        "max_clips": max(1, min(body.max_clips, 200)),
        "max_duration_seconds": max(300, min(body.max_duration_seconds, 86400)),
        "first_pass_seconds": max(30, body.first_pass_seconds),
        "pass_window_seconds": max(300, body.pass_window_seconds),
        "curation_prompt": body.goal or "funny reactions worth rewatching",
        "settings": {"captions": True, "vertical": True, "layout": "split"},
        "last_error": None,
        "recording_path": str(recording),
        "started_at_epoch": now,
        "created_at": _now(),
        "updated_at": _now(),
    }
    live_sessions[session_id] = session
    _persist_live(session_id)
    threading.Thread(target=_record_live, args=(session_id,), daemon=True).start()
    return {"session_id": session_id, "job_id": job_id, "first_pass_in": session["first_pass_seconds"]}


@app.get("/api/live/sessions")
def list_live_sessions():
    items = [_live_cloud_row(s) for s in live_sessions.values()]
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return {"sessions": items}


@app.get("/api/live/{session_id}")
def get_live_session(session_id: str):
    session = live_sessions.get(session_id)
    if not session:
        raise HTTPException(404, "Live session not found")
    job_id = session.get("job_id")
    return {"session": _live_cloud_row(session), "job": status(job_id) if job_id in jobs else None}


@app.post("/api/live/{session_id}/stop")
async def stop_live(session_id: str):
    if session_id not in live_sessions:
        raise HTTPException(404, "Live session not found")
    session = live_sessions[session_id]
    if session.get("state") == "live":
        session["stop_requested"] = True
        session["phase"] = "waiting"
        session["updated_at"] = _now()
        _persist_live(session_id)
    _stop_live_recorder(session_id, "manual")
    return {"ok": True}


@app.post("/api/live/{session_id}/pass")
async def force_live_pass(session_id: str):
    session = live_sessions.get(session_id)
    if not session:
        raise HTTPException(404, "Live session not found")
    if session.get("state") != "live":
        raise HTTPException(409, "Live session is not active")
    return _run_live_pass(session_id)


@app.get("/api/asr/status")
def asr_status():
    return {
        "active": asr.active_backend(), "groq_configured": bool(asr.GROQ_KEY),
        "groq_model": asr.GROQ_MODEL, "chunk_seconds": asr.CHUNK_SECONDS,
        "max_workers": asr.MAX_WORKERS, "encoder": hardware.ffmpeg_label(),
    }


@app.get("/api/hardware")
def hardware_status():
    enc, args = hardware.encoder_args(True)
    return {"encoder": enc, "args": args, "label": hardware.ffmpeg_label(), "render_workers": RENDER_WORKERS}


@app.post("/api/render/{job_id}")
def re_render(job_id: str, captions: bool = True, vertical: bool = True, draft: bool = False):
    job = jobs.get(job_id)
    if not job or not job.get("clips"):
        raise HTTPException(404, "Job with clips not found")
    job["settings"] = {"captions": captions, "vertical": vertical, "layout": job["settings"].get("layout", "split")}
    job["draft"] = draft
    for c in job["clips"]:
        c["render_status"] = "pending"
        if c.get("url"):
            c["last_url"] = c["url"]
    _set(job_id, clips=list(job["clips"]), status="processing", stage="render")
    if not _start_render(job_id, None, draft):
        raise HTTPException(409, "A render is already running for this job. Wait or cancel it.")
    return {"ok": True}


class SelectionBody(BaseModel):
    url: str
    transcript_file: str
    selection_file: str
    captions: bool = True
    vertical: bool = True
    layout: str = "split"


@app.post("/api/render-selection")
async def render_selection(body: SelectionBody):
    """Render a pre-computed selection (legacy compatibility)."""
    t_path = ROOT / "work" / "transcripts" / Path(body.transcript_file).name
    s_path = ROOT / "work" / "transcripts" / Path(body.selection_file).name
    transcript = json.loads(t_path.read_text(encoding="utf-8"))
    selection = json.loads(s_path.read_text(encoding="utf-8"))
    info = resolve_url(body.url)
    for i, c in enumerate(selection):
        c.update({"index": i, "render_status": "pending", "url": None, "error": None})
    job_id = _new_job(
        source_url=body.url, source_info=info,
        channel=library.channel_for(info, body.url),
        duration=round(info["duration"], 2), num_clips=len(selection),
        settings={"captions": body.captions, "vertical": body.vertical, "layout": body.layout},
        goal="comedy-first selection", transcript=transcript.get("segments", []),
        language=transcript.get("language"), asr_backend=transcript.get("backend"),
        transcript_source="precomputed", clips=selection, message="Rendering selected moments...",
    )
    if not _start_render(job_id, None, False):
        raise HTTPException(409, "A render is already running for this job. Wait or cancel it.")
    return {"job_id": job_id, "clips": len(selection)}


@app.get("/api/clips/{job_id}/{index}")
def get_clip(job_id: str, index: int, draft: int = 0):
    job = jobs.get(job_id)
    clip = None
    if job and 0 <= index < len(job.get("clips") or []):
        clip = job["clips"][index]
    if not draft and clip and clip.get("storage_path") and cloud.enabled():
        try:
            return RedirectResponse(cloud.signed_url(clip["storage_path"]), status_code=302)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Could not open cloud clip: {e}")
    name = f"{job_id}_draft_{index}.mp4" if draft else f"{job_id}_clip_{index}.mp4"
    path = CLIP_DIR / name
    if not path.exists():
        path = CLIP_DIR / f"{job_id}_clip_{index}.mp4"
    if not path.exists():
        raise HTTPException(404, "Clip not found")
    return FileResponse(str(path), media_type="video/mp4", filename=f"clip_{index + 1}.mp4")


@app.get("/api/cloud/status")
def cloud_status():
    return cloud.status()


@app.get("/api/transcript/{job_id}")
def get_transcript(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.get("transcript") and job.get("source_url"):
        hit = cache.load_json(cache.transcript_path(job["source_url"]))
        return {"segments": (hit or {}).get("segments", []), "language": job.get("language")}
    return {"segments": job.get("transcript", []), "language": job.get("language")}


# ---------------- persistent library / settings ----------------

@app.get("/api/library")
def get_library():
    return {"groups": library.grouped(), "hardware": hardware.ffmpeg_label()}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        detail = library.load_job_details().get(job_id)
        if not detail:
            raise HTTPException(404, "Job not found")
        return detail
    return status(job_id)


class NoteBody(BaseModel):
    note: str


@app.put("/api/clips/{job_id}/{index}/note")
async def set_clip_note(job_id: str, index: int, body: NoteBody):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    notes = job.setdefault("notes", {})
    notes[str(index)] = body.note
    _set(job_id, notes=notes)
    library.set_note(job_id, index, body.note)
    return {"ok": True, "note": body.note}


class EditorStateBody(BaseModel):
    tracks: list[dict]
    config: dict
    trim_start: float | None = None
    trim_end: float | None = None


@app.put("/api/clips/{job_id}/{index}/editor")
async def set_editor_state(job_id: str, index: int, body: EditorStateBody):
    job = jobs.get(job_id)
    if not job or not (0 <= index < len(job.get("clips") or [])):
        raise HTTPException(404, "Clip not found")
    clip = job["clips"][index]
    clip["tracks"] = body.tracks
    clip["config"] = body.config
    if body.trim_start is not None:
        clip["trim_start"] = float(body.trim_start)
    if body.trim_end is not None:
        clip["trim_end"] = float(body.trim_end)
    _set(job_id, clips=list(job["clips"]))
    return {"ok": True, "tracks": clip["tracks"], "config": clip["config"]}


class PresetBody(BaseModel):
    channel: str
    focus: dict
    face_shift_ms: float = 150


@app.get("/api/presets")
def get_presets():
    return {"presets": library.load_presets()}


@app.post("/api/presets")
async def post_preset(body: PresetBody):
    return {"presets": library.save_preset(body.channel, body.focus, body.face_shift_ms)}


class BulkPresetsBody(BaseModel):
    presets: dict[str, dict]


@app.post("/api/presets/bulk")
async def post_presets_bulk(body: BulkPresetsBody):
    for channel, preset in body.presets.items():
        library.save_preset(
            channel,
            preset.get("focus", preset),
            float(preset.get("face_shift_ms", 150)),
        )
    return {"presets": library.load_presets()}


class DictionaryBody(BaseModel):
    entries: dict[str, str]


@app.get("/api/dictionary")
def get_dictionary():
    return {"entries": dictionary.load()}


@app.post("/api/dictionary")
async def post_dictionary(body: DictionaryBody):
    return {"entries": dictionary.save(body.entries)}


class PublishBody(BaseModel):
    items: list[dict]


@app.get("/api/publish-queue")
def get_publish_queue():
    return {"items": library.load_publish_queue()}


@app.post("/api/publish-queue")
async def post_publish_queue(body: PublishBody):
    return {"items": library.save_publish_queue(body.items)}


class PublicationBody(BaseModel):
    job_id: str
    clip_idx: int
    platform: str
    caption: str = ""
    hashtags: list[str] = []
    scheduled_at: str | None = None
    social_account: str | None = None
    approved: bool = False


@app.get("/api/publications")
def get_publications():
    items = sorted(publications, key=lambda x: x.get("scheduled_at") or "", reverse=False)
    return {"items": items}


@app.post("/api/publications")
async def create_publication(body: PublicationBody):
    job = jobs.get(body.job_id)
    if not job or not (0 <= body.clip_idx < len(job.get("clips") or [])):
        raise HTTPException(404, "Clip not found")
    platform = (body.platform or "").strip().lower()
    if platform not in ("tiktok", "youtube", "instagram", "x"):
        raise HTTPException(400, "Unsupported platform")
    item = {
        "id": uuid.uuid4().hex[:12],
        "job_id": body.job_id,
        "clip_idx": int(body.clip_idx),
        "platform": platform,
        "social_account": body.social_account,
        "caption": body.caption,
        "hashtags": [str(x).lstrip("#") for x in body.hashtags][:10],
        "scheduled_at": body.scheduled_at,
        "status": "approved" if body.approved else "scheduled",
        "approved": bool(body.approved),
        "post_id": None,
        "error": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    _save_publication(item)
    return {"item": item}


@app.post("/api/publications/{pub_id}/approve")
async def approve_publication(pub_id: str):
    item = next((p for p in publications if p.get("id") == pub_id), None)
    if not item:
        raise HTTPException(404, "Publication not found")
    item.update({"approved": True, "status": "approved", "updated_at": _now()})
    _save_publication(item)
    return {"item": item}


@app.post("/api/publications/{pub_id}/post")
async def post_publication(pub_id: str):
    item = next((p for p in publications if p.get("id") == pub_id), None)
    if not item:
        raise HTTPException(404, "Publication not found")
    if not item.get("approved"):
        raise HTTPException(409, "Approve this clip before posting")
    result = _post_to_woopsocial(item)
    if result.get("ok"):
        item.update({"status": "posted", "post_id": result.get("post_id"), "error": None, "updated_at": _now()})
    elif result.get("needs_integration"):
        item.update({"status": "needs_integration", "error": result.get("error"), "updated_at": _now()})
    else:
        item.update({"status": "failed", "error": result.get("error"), "updated_at": _now()})
    _save_publication(item)
    return {"item": item, "result": result}


@app.delete("/api/publications/{pub_id}")
async def cancel_publication(pub_id: str):
    item = next((p for p in publications if p.get("id") == pub_id), None)
    if not item:
        raise HTTPException(404, "Publication not found")
    publications[:] = [p for p in publications if p.get("id") != pub_id]
    library.save_publications(publications)
    if cloud.enabled():
        try:
            cloud.delete_publication(item["id"])
        except Exception:
            pass
    return {"ok": True}


# Serve the frontend
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
def landing():
    return FileResponse(str(FRONTEND_DIR / "landing.html"))


@app.get("/auth")
def auth_page():
    return FileResponse(str(FRONTEND_DIR / "auth.html"))


@app.get("/app")
def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))
