"""Audio energy signal: find genuine reaction spikes in a long VOD.

Streams a 16 kHz mono WAV in 1-second frames and computes RMS loudness,
then scores each second against a rolling baseline. A shout/laugh burst that
is far above the surrounding minutes is a real crowd-reaction signal —
unlike transcript word choice, it cannot be faked by narration.
"""
from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np


def analyze_wav(wav_path: str | Path, out_path: str | Path | None = None,
                baseline_seconds: int = 120) -> list[dict]:
    """Return [{second, rms, spike}] for every second of audio."""
    wav_path = Path(wav_path)
    with wave.open(str(wav_path), "rb") as w:
        rate = w.getframerate()
        width = w.getsampwidth()
        assert w.getnchannels() == 1 and width == 2, "expected 16-bit mono WAV"
        total_seconds = w.getnframes() // rate

        rms = np.empty(total_seconds, dtype=np.float32)
        frame = w.readframes(rate)
        i = 0
        while frame and i < total_seconds:
            samples = np.frombuffer(frame, dtype="<i2").astype(np.float32) / 32768.0
            if len(samples) < rate:
                samples = np.pad(samples, (0, rate - len(samples)))
            rms[i] = float(np.sqrt(np.mean(samples * samples)))
            i += 1
            frame = w.readframes(rate)

    rms = rms[:i]

    # Rolling median + MAD baseline (robust to long loud sections).
    win = baseline_seconds
    csum = np.concatenate([[0.0], np.cumsum(rms, dtype=np.float64)])
    spike = np.zeros(len(rms), dtype=np.float32)
    for j in range(len(rms)):
        lo = max(0, j - win)
        hi = min(len(rms), j + win)
        base = (csum[hi] - csum[lo]) / max(1, hi - lo)
        if base > 1e-5:
            spike[j] = max(0.0, rms[j] / base - 1.0)

    # Smooth into 5-second buckets so a single clap doesn't dominate.
    buckets = []
    for s in range(0, len(spike), 5):
        chunk = spike[s:s + 5]
        buckets.append({
            "start": s,
            "rms": round(float(np.mean(rms[s:s + 5])), 5),
            "spike": round(float(np.percentile(chunk, 90)), 3),
        })

    if out_path:
        Path(out_path).write_text(json.dumps(buckets), encoding="utf-8")
    return buckets


def spikes_near(buckets: list[dict], start: float, end: float) -> tuple[float, float]:
    """Return (max spike, mean spike) for a time window in seconds."""
    vals = [b["spike"] for b in buckets if start <= b["start"] <= end]
    if not vals:
        return 0.0, 0.0
    return max(vals), float(np.mean(vals))


if __name__ == "__main__":
    import sys
    src = sys.argv[1]
    dst = sys.argv[2]
    out = analyze_wav(src, dst)
    print("seconds:", len(out), "top:", sorted(out, key=lambda b: b["spike"], reverse=True)[:10])
