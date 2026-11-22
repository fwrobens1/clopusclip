"""Comedy-first moment detection.

Transcript-only selection tends to pick the loudest gameplay commentary, not
the moments people actually laugh at. This module scores transcript segments
for comedic signal relative to their local baseline, then builds a short list
of punchline-centred candidates for the AI to rank.
"""
from __future__ import annotations

import re
from typing import Any

LAUGH_RE = re.compile(
    r"\b(?:haha|hehe|hilarious|laugh(?:ing|s|ed)?|lol|lmao|lose it|lost it|dying)\b", re.I
)
SURPRISE_RE = re.compile(
    r"\b(?:holy|no way|oh my|what the|wtf|damn|crazy|insane|unreal|are you serious|"
    r"stop it|get out of here|nah|wait wait|bro|dude|chat)\b", re.I
)
PROFANITY_RE = re.compile(r"\b(?:fuck(?:ing|ed)?|shit|bitch|ass|nigga|damn it)\b", re.I)
REPEAT_RE = re.compile(r"(\w)\1{2,}")
CAPS_RE = re.compile(r"\b[A-Z]{3,}\b")


def _score_text(text: str) -> float:
    """Return a comedic-signal score for one transcript segment."""
    if not text or not text.strip():
        return 0.0
    s = 0.0
    s += min(3, len(LAUGH_RE.findall(text))) * 4.0
    s += min(4, len(SURPRISE_RE.findall(text))) * 1.4
    s += min(2, len(PROFANITY_RE.findall(text))) * 1.0
    s += min(4, text.count("!")) * 1.2
    s += min(2, text.count("?")) * 0.4
    s += min(3, len(CAPS_RE.findall(text))) * 1.0
    if REPEAT_RE.search(text):
        s += 1.5
    words = text.split()
    if 2 <= len(words) <= 12:
        s += 0.8
    return s


def _relative_scores(scores: list[float], window: int = 400) -> list[float]:
    """Z-score each segment against a broad local baseline.

    A constant stream of "oh my god bro" is normal for this streamer and must
    not look funny just because it is frequent. Only unusually dense bursts
    relative to the surrounding hour survive.
    """
    n = len(scores)
    if n == 0:
        return []
    prefix = [0.0] * (n + 1)
    for i, v in enumerate(scores):
        prefix[i + 1] = prefix[i] + v
    out = []
    for i in range(n):
        lo = max(0, i - window)
        hi = min(n, i + window)
        mean = (prefix[hi] - prefix[lo]) / max(1, hi - lo)
        # Subtract the local mean; keep only the excess above baseline.
        out.append(max(0.0, scores[i] - mean))
    return out


def build_candidates(
    transcript: list[dict],
    video_duration: float,
    num_clips: int,
    per_bucket: int = 4,
    short: float = 30.0,
    long: float = 60.0,
) -> list[dict[str, Any]]:
    """Return punchline-centred candidate moments spread across the timeline."""
    segs = [s for s in transcript if str(s.get("text", "")).strip()]
    if not segs:
        return []
    raw = [_score_text(str(s.get("text", ""))) for s in segs]
    rel = _relative_scores(raw)

    bucket_count = max(1, min(num_clips, 12))
    bucket_len = video_duration / bucket_count
    buckets: list[list[dict[str, Any]]] = [[] for _ in range(bucket_count)]

    # Seed a window at every unusually strong line, centred slightly before it
    # so the clip carries the setup and lands on the punchline.
    seeds = sorted(
        ((rel[i], i) for i in range(len(segs)) if rel[i] >= 1.5), reverse=True
    )
    for excess, i in seeds:
        seg = segs[i]
        start = max(0.0, float(seg.get("start", 0)) - 8.0)
        end = min(video_duration, start + long)
        # Pull the start forward if a neighbouring line is part of the beat.
        for j in range(max(0, i - 3), i):
            prev_start = float(segs[j].get("start", 0))
            if start - prev_start <= 9 and rel[j] > 0.4:
                start = prev_start
        if end - start < short:
            end = min(video_duration, start + short)
        # Score the whole window: peak plus density of supporting lines.
        in_window = [
            rel[k] for k in range(len(segs))
            if start <= float(segs[k].get("start", 0)) <= end
        ]
        density = sum(in_window) / max(1, len(in_window))
        score = excess + 0.35 * density + 0.05 * sum(in_window)
        idx = min(bucket_count - 1, int(start / max(1, bucket_len)))
        excerpt = " ".join(
            str(segs[k].get("text", "")).strip()
            for k in range(len(segs))
            if start <= float(segs[k].get("start", 0)) <= end
        )[:700]
        buckets[idx].append({
            "start": round(start, 2),
            "end": round(end, 2),
            "score": round(score, 2),
            "excerpt": excerpt,
        })

    candidates: list[dict[str, Any]] = []
    for bucket in buckets:
        # De-duplicate overlapping seeds inside each timeline bucket.
        bucket.sort(key=lambda c: c["score"], reverse=True)
        kept: list[dict[str, Any]] = []
        for c in bucket:
            if any(abs(c["start"] - k["start"]) < short * 0.9 for k in kept):
                continue
            kept.append(c)
            if len(kept) >= per_bucket:
                break
        candidates.extend(kept)
    candidates.sort(key=lambda c: c["start"])
    return candidates
