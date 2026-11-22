"""Pluggable ASR backends.

Backends:
  groq  — whisper-large-v3-turbo on Groq's LPU. Audio is split into ~10min
          chunks and transcribed in parallel. A 7h VOD finishes in a couple
          of minutes (not hours). Free API key: https://console.groq.com
  local — faster-whisper on CPU. Free, private, but slow (~1.5x realtime on 4 cores).
  auto  — groq when GROQ_API_KEY is set, otherwise local.

All backends return the same shape:
  {language, duration, segments:[{start,end,text,words:[...]}], words:[...], backend}
"""
from __future__ import annotations

import os
import random
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
GROQ_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = os.getenv("GROQ_ASR_MODEL", "whisper-large-v3-turbo")
CHUNK_SECONDS = int(os.getenv("ASR_CHUNK_SECONDS", "600"))  # 10 min
# Groq's free tier throttles bursts hard: 8 workers failed ~40% of chunks,
# 4 workers failed ~60%. Gentle sequential uploads succeed ~100%. With retries
# this still finishes a 7h VOD in minutes, and avoids the slow local fallback.
# Two gentle workers with exponential backoff is the safe middle ground:
# it roughly doubles Groq throughput without the 429 storm seen at 4+ workers.
MAX_WORKERS = max(1, min(3, int(os.getenv("ASR_MAX_WORKERS", "2"))))
ASR_BACKEND = os.getenv("ASR_BACKEND", "auto").lower()


def active_backend() -> str:
    if ASR_BACKEND == "groq" and GROQ_KEY:
        return "groq"
    if ASR_BACKEND == "local":
        return "local"
    return "groq" if GROQ_KEY else "local"


def _duration(path: str | Path) -> float:
    r = subprocess.run([FFMPEG, "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", r.stderr or "")
    if m:
        h, mi, s = m.groups()
        return int(h) * 3600 + int(mi) * 60 + float(s)
    return 0.0


def _split_audio(audio_path: str | Path, work_dir: Path) -> list[tuple[Path, float]]:
    """Split audio into raw WAV chunks (no re-encode — ~100x faster than MP3).

    CHUNK_SECONDS of 16 kHz mono PCM is ~19 MB, safely under Groq's 25 MB
    limit. Re-encoding a 7h WAV to MP3 used to take ~20 minutes; copying raw
    PCM takes seconds. Returns [(path, start_offset_seconds)].
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    pattern = work_dir / "chunk_%04d.wav"
    r = subprocess.run(
        [FFMPEG, "-y", "-i", str(audio_path), "-f", "segment",
         "-segment_time", str(CHUNK_SECONDS), "-c:a", "copy", str(pattern)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"Audio chunking failed: {r.stderr[-300:]}")
    chunks = sorted(work_dir.glob("chunk_*.wav"))
    return [(p, i * CHUNK_SECONDS) for i, p in enumerate(chunks)]


def _transcribe_groq_chunk(client: httpx.Client, path: Path, offset: float,
                           retries: int = 5) -> dict:
    """Transcribe one chunk, retrying throttles/5xx with exponential backoff."""
    for attempt in range(retries):
        with open(path, "rb") as f:
            r = client.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_KEY}"},
                files={"file": (path.name, f, "audio/wav")},
                data={
                    "model": GROQ_MODEL,
                    "response_format": "verbose_json",
                    "timestamp_granularities[]": ["segment", "word"],
                },
                timeout=180.0,
            )
        if r.status_code == 200:
            return {"data": r.json(), "offset": offset}
        if r.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
            wait = min(30, 3 * (2 ** attempt)) + random.uniform(0, 1.5)
            time.sleep(wait)  # 3s, 6s, 12s, 24s — capped, with jitter
            continue
        raise RuntimeError(f"Groq {r.status_code}: {r.text[:200]}")
    raise RuntimeError("Groq: exhausted retries")


def _normalize(data: dict, offset: float) -> dict:
    """Add chunk offset to all timestamps and normalize keys."""
    segs_out: list[dict] = []
    words_out: list[dict] = []
    for s in data.get("segments") or []:
        text = str(s.get("text", "")).strip()
        start = float(s.get("start", 0)) + offset
        end = float(s.get("end", start)) + offset
        if not text:
            continue
        seg = {"start": round(start, 2), "end": round(end, 2), "text": text, "words": []}
        for w in data.get("words") or []:
            ws = float(w.get("start", 0)) + offset
            we = float(w.get("end", ws)) + offset
            if start - 0.2 <= ws <= end + 0.2:
                seg["words"].append({
                    "word": str(w.get("word", "")).strip(),
                    "start": round(ws, 2), "end": round(we, 2),
                })
        segs_out.append(seg)
        words_out.extend(seg["words"])

    # If no segment/word structure came back, build one from the flat text
    if not segs_out and data.get("text"):
        text = str(data["text"]).strip()
        dur = float(data.get("duration", 0))
        segs_out = [{"start": round(offset, 2), "end": round(offset + dur, 2),
                     "text": text, "words": []}]
    return {"segments": segs_out, "words": words_out}


def transcribe_groq(audio_path: str | Path, work_dir: Path,
                    progress_cb: Callable[[str], None] | None = None) -> dict:
    """Fast path: parallel chunked transcription on Groq."""
    total = _duration(audio_path)
    chunks = _split_audio(audio_path, work_dir)
    if not chunks:
        raise RuntimeError("No audio chunks produced")
    if progress_cb:
        progress_cb(f"Groq ASR: {len(chunks)} chunks in parallel")

    results: list[dict | None] = [None] * len(chunks)
    errors: list[str] = []
    t0 = time.time()
    with httpx.Client() as client:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(_transcribe_groq_chunk, client, p, off): i
                for i, (p, off) in enumerate(chunks)
            }
            done = 0
            for fut in as_completed(futures):
                i = futures[fut]
                done += 1
                try:
                    results[i] = fut.result()
                except Exception as e:  # noqa: BLE001
                    errors.append(f"chunk {i}: {e}")
                if progress_cb and done % max(1, len(chunks) // 5) == 0:
                    progress_cb(f"Groq ASR: {done}/{len(chunks)} chunks  ({time.time()-t0:.0f}s)")

    failed = [i for i, r in enumerate(results) if r is None]
    if failed:
        # Retry throttled chunks one at a time — concurrency was the problem.
        if progress_cb:
            progress_cb(f"Retrying {len(failed)} throttled chunks one at a time...")
        with httpx.Client() as client:
            for i in failed:
                p, off = chunks[i]
                try:
                    results[i] = _transcribe_groq_chunk(client, p, off)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"retry chunk {i}: {e}")

    failed = [i for i, r in enumerate(results) if r is None]
    if failed:
        # Local fallback for the few chunks Groq still couldn't do
        if progress_cb:
            progress_cb(f"Groq missed {len(failed)} chunks — filling locally")
        from transcribe import transcribe_audio as local_transcribe
        for i in failed:
            p = chunks[i][0]
            try:
                lr = local_transcribe(str(p))
                results[i] = {"data": lr, "offset": chunks[i][1]}
            except Exception as e:  # noqa: BLE001
                errors.append(f"local fallback chunk {i}: {e}")

    segments: list[dict] = []
    words: list[dict] = []
    for r in results:
        if not r:
            continue
        norm = _normalize(r["data"], r["offset"])
        segments.extend(norm["segments"])
        words.extend(norm["words"])
    segments.sort(key=lambda s: s["start"])
    words.sort(key=lambda w: w["start"])
    if not segments:
        raise RuntimeError("Transcription produced no segments: " + " | ".join(errors[:3]))

    return {
        "language": "auto",
        "duration": round(total, 2),
        "segments": segments,
        "words": words,
        "backend": f"groq:{GROQ_MODEL}",
        "elapsed": round(time.time() - t0, 1),
        "errors": errors[:5],
    }


def transcribe_local(audio_path: str | Path,
                     progress_cb: Callable[[str], None] | None = None) -> dict:
    from transcribe import transcribe_audio as local_transcribe
    r = local_transcribe(str(audio_path))
    r["backend"] = f"local:{os.getenv('WHISPER_MODEL', 'tiny')}"
    if progress_cb:
        progress_cb("Local Whisper complete")
    return r


def transcribe(audio_path: str | Path, work_dir: Path,
               progress_cb: Callable[[str], None] | None = None) -> dict:
    backend = active_backend()
    if backend == "groq":
        try:
            return transcribe_groq(audio_path, work_dir, progress_cb)
        except Exception as e:  # noqa: BLE001
            if progress_cb:
                progress_cb(f"Groq failed ({e}); falling back to local Whisper")
            return transcribe_local(audio_path, progress_cb)
    return transcribe_local(audio_path, progress_cb)
