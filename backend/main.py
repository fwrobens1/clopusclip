"""ClipForge API - a working AI clipping website with URL ingestion + agent chat."""
from __future__ import annotations

import json
import shutil
import sys
import threading
import uuid
from pathlib import Path

# Allow both `backend.main` imports and direct execution
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

import agent  # noqa: E402
import asr  # noqa: E402
from ai import select_clips  # noqa: E402
from clips import probe_duration, render_clip  # noqa: E402
from sources import download_audio, download_section, download_transcript, resolve as resolve_url  # noqa: E402
from transcribe import transcribe_audio  # noqa: E402

UPLOAD_DIR = ROOT / "work" / "uploads"
AUDIO_DIR = ROOT / "work" / "audio"
CLIP_DIR = ROOT / "work" / "clips"
SECTION_DIR = ROOT / "work" / "sections"
SUBS_DIR = ROOT / "work" / "subs"
ASR_DIR = ROOT / "work" / "asr"
FRONTEND_DIR = ROOT / "frontend"
for d in (UPLOAD_DIR, AUDIO_DIR, CLIP_DIR, SECTION_DIR, SUBS_DIR, ASR_DIR):
    d.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="ClipForge API")

jobs: dict[str, dict] = {}


def _set(job_id: str, **kwargs):
    if job_id in jobs:
        jobs[job_id].update(kwargs)


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
        **kwargs,
    }
    return job_id


def _extract_audio(video_path: Path, audio_path: Path):
    import subprocess

    r = subprocess.run(
        [shutil.which("ffmpeg") or "ffmpeg", "-y", "-i", str(video_path),
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio_path)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"Audio extraction failed: {r.stderr[-400:]}")


def _finalize_selection(job_id: str, transcript_segments: list[dict], duration: float):
    job = jobs[job_id]
    clips = select_clips(
        transcript_segments,
        video_duration=duration,
        num_clips=int(job.get("num_clips", 5)),
        clip_length=job.get("clip_length", "medium"),
        goal=job.get("goal", ""),
    )
    if not clips:
        raise RuntimeError("The AI could not find any suitable clips in this video.")
    for i, c in enumerate(clips):
        c["index"] = i
        c["render_status"] = "pending"
        c["url"] = None
        c["error"] = None
    _set(job_id, clips=clips, transcript=transcript_segments)


def _pipeline(job_id: str):
    """Local-file pipeline."""
    try:
        job = jobs.get(job_id)
        if not job:
            return
        video_path = Path(job["video_path"])
        _set(job_id, status="processing", stage="audio", progress=8, message="Extracting audio...")
        audio_path = AUDIO_DIR / f"{job_id}.wav"
        _extract_audio(video_path, audio_path)

        _set(job_id, stage="transcribe", progress=20, message="Transcribing speech...")

        def _prog(msg):
            _set(job_id, message=msg)

        transcript = asr.transcribe(audio_path, ASR_DIR / job_id, progress_cb=_prog)
        _set(job_id, language=transcript.get("language"), asr_backend=transcript.get("backend"))

        _set(job_id, stage="analyze", progress=50, message="AI is finding your best moments...")
        _finalize_selection(job_id, transcript["segments"], float(job.get("duration") or 0))

        _render_all(job_id)
        _set(job_id, status="done", stage="done", progress=100, message="Your clips are ready!")
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", progress=100, message=str(e), error=str(e))


def _url_pipeline(job_id: str):
    """URL pipeline: subtitle-first, else audio-only transcription, then section-based rendering."""
    try:
        job = jobs.get(job_id)
        if not job:
            return
        url = job["source_url"]

        _set(job_id, status="processing", stage="resolve", progress=5,
             message="Resolving link and reading video info...")
        info = resolve_url(url)
        _set(job_id, source_info=info, duration=round(info["duration"], 2))

        # 1) Prefer existing captions/subtitles (instant, no transcription)
        segments: list[dict] = []
        source_kind = "subtitles"
        if not info.get("is_live"):
            _set(job_id, progress=12, message="Looking for captions...")
            segments = download_transcript(url, SUBS_DIR / job_id)
        if segments and len(segments) >= 2:
            _set(job_id, language="captions", progress=25,
                 message=f"Using {len(segments)} caption cues from the source (no transcription needed).")
        else:
            # 2) Audio-only download + local transcription
            source_kind = "asr"
            _set(job_id, progress=15, message="Downloading audio only (not the whole video)...")

            def _dl_progress(info):
                if info.get("downloaded", 0) > 0:
                    # Live streams report "~ 12.3%" — strip both ~ and %.
                    pct = info.get("percent", "0").replace("~", "").strip().strip("%")
                    try:
                        p = float(pct)
                    except ValueError:
                        p = 0.0
                    _set(job_id, progress=15 + min(30, int(p * 0.3)),
                         message=f"Downloading audio · {info['percent']} · {info['speed']} · ETA {info['eta']}")

            audio = download_audio(
                url, AUDIO_DIR / f"{job_id}_src.wav",
                progress_cb=_dl_progress,
                # A live stream is only a partial snapshot — never cache it.
                use_cache=not info.get("is_live", False),
            )

            def _prog(msg):
                _set(job_id, message=msg)

            tr = asr.transcribe(audio, ASR_DIR / job_id, progress_cb=_prog)
            segments = tr["segments"]
            _set(job_id, language=tr.get("language"), asr_backend=tr.get("backend"))

        _set(job_id, stage="analyze", progress=45, message="AI is finding your best moments...")
        _finalize_selection(job_id, segments, info["duration"])
        _set(job_id, transcript_source=source_kind)

        _render_all(job_id)
        _set(job_id, status="done", stage="done", progress=100, message="Your clips are ready!")
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", progress=100, message=str(e), error=str(e))


def _flat_words(job: dict) -> list[dict]:
    words: list[dict] = []
    for seg in job.get("transcript") or []:
        words.extend(seg.get("words") or [])
    return words


def _render_all(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return
    clips = job.get("clips", [])
    flat_words = _flat_words(job)
    settings = job.get("settings", {})
    captions = bool(settings.get("captions", True))
    vertical = bool(settings.get("vertical", False))
    total = max(1, len(clips))
    is_url = bool(job.get("source_url"))

    for i, clip in enumerate(clips):
        out_path = CLIP_DIR / f"{job_id}_clip_{i}.mp4"
        input_path = job.get("video_path")
        render_start, render_end = float(clip["start"]), float(clip["end"])

        if is_url:
            _set(job_id, stage="render", progress=55 + int(30 * i / total),
                 message=f"Fetching moment {i + 1}/{total} from the source...")
            sec = download_section(job["source_url"], render_start, render_end,
                                   SECTION_DIR / f"{job_id}_sec_{i}.mp4")
            input_path = str(sec)
            render_start = 0.0
            render_end = probe_duration(input_path) or max(2, float(clip["end"]) - float(clip["start"]))
            # Shift word timestamps into the section's local time base
            flat_words = [
                {"word": w.get("word", ""),
                 "start": max(0.0, float(w.get("start", 0)) - float(clip["start"])),
                 "end": max(0.0, float(w.get("end", 0)) - float(clip["start"]))}
                for w in _flat_words(job)
                if float(w.get("end", 0)) >= float(clip["start"])
                and float(w.get("start", 0)) <= float(clip["end"]) + 1
            ]

        _set(job_id, stage="render", progress=60 + int(35 * i / total),
             message=f"Rendering clip {i + 1} of {total}...")
        result = render_clip(
            input_path, {"start": render_start, "end": render_end}, str(out_path),
            words=flat_words, captions=captions, vertical=vertical,
        )
        if result.get("ok"):
            clip["render_status"] = "done"
            clip["url"] = f"/api/clips/{job_id}/{i}"
            clip["captions"] = result.get("captions", False)
            if result.get("note"):
                clip["note"] = result["note"]
        else:
            clip["render_status"] = "error"
            clip["error"] = result.get("error", "render failed")
        _set(job_id, clips=list(clips))


# ----------------------------- API routes -----------------------------

@app.get("/api/health")
def health():
    return {"ok": True, "jobs": len(jobs)}


class UrlBody(BaseModel):
    url: str


@app.post("/api/source")
async def source_info(body: UrlBody):
    """Resolve a link and return metadata without downloading video."""
    try:
        info = resolve_url(body.url)
        return {"ok": True, **info}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Could not read that link: {e}")


@app.post("/api/clip-url")
async def clip_url(
    url: str = Form(...),
    num_clips: int = Form(5),
    clip_length: str = Form("medium"),
    captions: bool = Form(True),
    vertical: bool = Form(True),
    goal: str = Form(""),
):
    """Start clipping from a YouTube / Twitch / Kick / etc. URL."""
    job_id = _new_job(
        source_url=url,
        num_clips=max(1, min(num_clips, 12)),
        clip_length=clip_length,
        settings={"captions": captions, "vertical": vertical},
        goal=goal,
        message="Resolving link...",
    )
    threading.Thread(target=_url_pipeline, args=(job_id,), daemon=True).start()
    return {"job_id": job_id}


def _pipeline_from_transcript(job_id: str):
    """Skip ASR entirely — use an imported transcript (e.g. from a free-GPU notebook)."""
    try:
        job = jobs.get(job_id)
        if not job:
            return
        if job.get("source_url") and not job.get("source_info"):
            _set(job_id, progress=5, message="Resolving source info...")
            _set(job_id, source_info=resolve_url(job["source_url"]))
        duration = float(job.get("duration") or 0)
        _set(job_id, stage="analyze", progress=50, message="AI is finding your best moments...")
        _finalize_selection(job_id, job.get("transcript") or [], duration)
        _set(job_id, transcript_source="imported")
        _render_all(job_id)
        _set(job_id, status="done", stage="done", progress=100, message="Your clips are ready!")
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", progress=100, message=str(e), error=str(e))


@app.post("/api/import-transcript")
async def import_transcript(
    transcript: UploadFile = File(...),
    url: str = Form(""),
    num_clips: int = Form(5),
    clip_length: str = Form("medium"),
    captions: bool = Form(True),
    vertical: bool = Form(True),
    goal: str = Form(""),
):
    """Import a transcript JSON produced by the free-GPU notebook (or any ASR)."""
    try:
        raw = await transcript.read()
        data = json.loads(raw)
        segments = data.get("segments", data if isinstance(data, list) else [])
        if not segments:
            raise HTTPException(400, "Transcript has no segments")
        cleaned = []
        for s in segments:
            try:
                cleaned.append({
                    "start": float(s.get("start", 0)),
                    "end": float(s.get("end", 0)),
                    "text": str(s.get("text", "")).strip(),
                    "words": [{"word": str(w.get("word", "")),
                               "start": float(w.get("start", 0)),
                               "end": float(w.get("end", 0))}
                              for w in (s.get("words") or [])],
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
    if source_url:
        try:
            info = resolve_url(source_url)
            duration = info["duration"]
        except Exception:
            source_url = None
    if not duration:
        duration = max(float(s["end"]) for s in cleaned)

    job_id = _new_job(
        source_url=source_url,
        num_clips=max(1, min(num_clips, 12)),
        clip_length=clip_length,
        settings={"captions": captions, "vertical": vertical},
        goal=goal,
        transcript=cleaned,
        duration=round(duration, 2),
        language=data.get("language", "imported"),
        asr_backend=data.get("backend", "imported"),
        message="Using imported transcript...",
    )
    threading.Thread(target=_pipeline_from_transcript, args=(job_id,), daemon=True).start()
    return {"job_id": job_id, "segments": len(cleaned)}


@app.post("/api/upload")
async def upload(
    file: UploadFile = File(...),
    num_clips: int = Form(5),
    clip_length: str = Form("medium"),
    captions: bool = Form(True),
    vertical: bool = Form(False),
    goal: str = Form(""),
):
    """Upload a video and start the full clipping pipeline in the background."""
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
        "id": job_id,
        "filename": file.filename,
        "video_path": str(video_path),
        "duration": round(duration, 2),
        "num_clips": max(1, min(num_clips, 12)),
        "clip_length": clip_length,
        "settings": {"captions": captions, "vertical": vertical},
        "status": "queued", "stage": "queued", "progress": 0,
        "message": "Queued...", "transcript": [], "clips": [], "error": None,
        "source_url": None, "source_info": None, "goal": goal,
    }
    threading.Thread(target=_pipeline, args=(job_id,), daemon=True).start()
    return {"job_id": job_id, "duration": round(duration, 2)}


@app.post("/api/chat")
async def chat(message: str = Form(...), job_id: str | None = Form(None)):
    """Agent chat: parse intent, start/manage jobs, and reply conversationally."""
    parsed = agent.parse_request(message)
    context = ""
    job = jobs.get(job_id or "")
    if job:
        clips = job.get("clips", [])
        context = (
            f"Job {job['id']}: status={job['status']}, progress={job.get('progress')}%, "
            f"message={job.get('message', '')}, clips={len(clips)}, "
            f"source={job.get('source_info', {}).get('title') if job.get('source_info') else job.get('filename', 'uploaded file')}. "
            f"Clips: " + ", ".join(f"{c.get('title')} ({c.get('start')}-{c.get('end')}s)" for c in clips[:8])
        )

    result_job_id = job_id
    if parsed["action"] == "clip" and parsed.get("url"):
        result_job_id = _new_job(
            source_url=parsed["url"],
            num_clips=parsed.get("num_clips", 5),
            clip_length=parsed.get("clip_length", "medium"),
            settings={"captions": parsed.get("captions", True), "vertical": parsed.get("vertical", True)},
            goal=parsed.get("goal") or message,
            message="Resolving link...",
        )
        threading.Thread(target=_url_pipeline, args=(result_job_id,), daemon=True).start()
        summary = (
            f"On it. I'll pull the source, find {parsed.get('num_clips', 5)} moments"
            + (f" matching \"{parsed.get('goal')}\"" if parsed.get("goal") else "")
            + ", and render them."
        )
        reply = agent.reply(message, summary + "\n" + context)
    elif parsed["action"] == "rerender" and job and job.get("clips"):
        job["settings"] = {
            "captions": parsed.get("captions", job["settings"].get("captions", True)),
            "vertical": parsed.get("vertical", job["settings"].get("vertical", True)),
        }
        for c in job["clips"]:
            c["render_status"] = "pending"
            c["url"] = None
        threading.Thread(target=_render_all, args=(job["id"],), daemon=True).start()
        reply = agent.reply(message, "Re-rendering the existing clips with the new settings.\n" + context)
    else:
        reply = agent.reply(message, context)

    return {"reply": reply, "job_id": result_job_id, "parsed": parsed}


@app.get("/api/status/{job_id}")
def status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {
        "id": job["id"], "status": job["status"], "stage": job.get("stage"),
        "progress": job.get("progress", 0), "message": job.get("message", ""),
        "duration": job.get("duration"), "language": job.get("language"),
        "clips": job.get("clips", []), "error": job.get("error"),
        "settings": job.get("settings", {}), "goal": job.get("goal", ""),
        "source_info": job.get("source_info"),
        "transcript_source": job.get("transcript_source"),
        "asr_backend": job.get("asr_backend"),
    }


@app.get("/api/asr/status")
def asr_status():
    """Report which ASR backend is active (for the UI status chip)."""
    return {
        "active": asr.active_backend(),
        "groq_configured": bool(asr.GROQ_KEY),
        "groq_model": asr.GROQ_MODEL,
        "chunk_seconds": asr.CHUNK_SECONDS,
        "max_workers": asr.MAX_WORKERS,
    }


@app.post("/api/render/{job_id}")
def re_render(job_id: str, captions: bool = True, vertical: bool = False):
    """Re-render existing clips with new caption/format settings."""
    job = jobs.get(job_id)
    if not job or not job.get("clips"):
        raise HTTPException(404, "Job with clips not found")
    job["settings"] = {"captions": captions, "vertical": vertical}
    for c in job["clips"]:
        c["render_status"] = "pending"
        c["url"] = None
    threading.Thread(target=_render_all, args=(job_id,), daemon=True).start()
    return {"ok": True}


@app.get("/api/clips/{job_id}/{index}")
def get_clip(job_id: str, index: int):
    path = CLIP_DIR / f"{job_id}_clip_{index}.mp4"
    if not path.exists():
        raise HTTPException(404, "Clip not found")
    return FileResponse(str(path), media_type="video/mp4", filename=f"clip_{index + 1}.mp4")


@app.get("/api/transcript/{job_id}")
def get_transcript(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {"segments": job.get("transcript", []), "language": job.get("language")}


# Serve the frontend
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))
