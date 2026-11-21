"""Speech-to-text using local faster-whisper.

Runs fully offline after the model is downloaded. Gracefully degrades if the
model cannot be loaded, so the rest of the pipeline still works.
"""
from __future__ import annotations

import os
from typing import Any

# Compatibility shim: faster-whisper 1.2.1 passes metadata_errors to av.open,
# but PyAV 19+ removed that kwarg. Strip it so transcription works on both.
try:
    import av as _av
    _orig_av_open = _av.open

    def _patched_av_open(*args, **kwargs):
        kwargs.pop("metadata_errors", None)
        return _orig_av_open(*args, **kwargs)

    _av.open = _patched_av_open
except Exception:  # noqa: BLE001 - av may not be importable yet
    pass

_model = None
_load_error: str | None = None

# Tiny is ~4x faster on CPU; set WHISPER_MODEL=base for better accuracy (slower).
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "tiny")  # tiny, base, small, medium
BEAM_SIZE = int(os.getenv("WHISPER_BEAM", "1"))


def get_model():
    global _model, _load_error
    if _model is not None:
        return _model
    if _load_error:
        raise RuntimeError(_load_error)
    try:
        from faster_whisper import WhisperModel

        print(f"[transcribe] loading faster-whisper model '{WHISPER_MODEL}' (first run downloads it)...")
        _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        print("[transcribe] model ready")
        return _model
    except Exception as e:  # noqa: BLE001
        _load_error = f"{type(e).__name__}: {e}"
        raise RuntimeError(_load_error)


def transcribe_audio(audio_path: str, progress_cb=None) -> dict[str, Any]:
    """Transcribe an audio file. Returns segments with word-level timestamps."""
    model = get_model()
    segments, info = model.transcribe(
        audio_path,
        beam_size=BEAM_SIZE,
        word_timestamps=True,
        vad_filter=True,
        language=None,  # auto-detect
    )

    out_segments = []
    words = []
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        seg_dict = {
            "start": round(float(seg.start), 2),
            "end": round(float(seg.end), 2),
            "text": text,
        }
        if seg.words:
            wlist = []
            for w in seg.words:
                if w.word is None:
                    continue
                wd = {
                    "word": w.word,
                    "start": round(float(w.start), 2) if w.start is not None else seg_dict["start"],
                    "end": round(float(w.end), 2) if w.end is not None else seg_dict["end"],
                }
                wlist.append(wd)
                words.append(wd)
            seg_dict["words"] = wlist
        out_segments.append(seg_dict)
        if progress_cb:
            progress_cb(len(out_segments))

    return {
        "language": getattr(info, "language", "unknown"),
        "duration": round(float(getattr(info, "duration", 0)), 2),
        "segments": out_segments,
        "words": words,
    }
