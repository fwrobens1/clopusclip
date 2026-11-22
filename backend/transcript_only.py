"""Standalone transcript-only pass: download audio + ASR, save JSON, no rendering.

Usage:
    python -m backend.transcript_only <url> <video_id>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

import asr  # noqa: E402
import sources  # noqa: E402

URL = sys.argv[1]
VIDEO_ID = sys.argv[2]
AUDIO = ROOT / "work" / "audio" / f"{VIDEO_ID}_src.wav"
WORK = ROOT / "work" / "asr" / VIDEO_ID
OUT = ROOT / "work" / "transcripts" / f"{VIDEO_ID}_transcript.json"


def main() -> None:
    print(f"[1/2] downloading audio for {URL}", flush=True)
    audio = sources.download_audio(URL, AUDIO, progress_cb=lambda d: print(
        f"  audio {d.get('percent')} {d.get('speed')} eta {d.get('eta')}", flush=True
    ) if d.get("downloaded", 0) else None)
    print(f"[2/2] transcribing {AUDIO}", flush=True)
    result = asr.transcribe(audio, WORK, progress_cb=lambda m: print(f"  {m}", flush=True))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "segments": result["segments"],
        "language": result.get("language"),
        "backend": result.get("backend"),
        "duration": result.get("duration"),
    }, ensure_ascii=False), encoding="utf-8")
    print(f"DONE {OUT} segments={len(result['segments'])}", flush=True)


if __name__ == "__main__":
    main()
