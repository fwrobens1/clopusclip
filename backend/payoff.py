"""Payoff-aware clip timing.

A clip that ends one second before the laugh, the punchline, or the granny
getting hit is a bad clip. This module looks just past the model's proposed
end time and extends the window when there is a real reaction there. It also
merges adjacent high-energy windows into a single "sequence" clip — the
killing-spree style moments viewers actually rewatch.
"""
from __future__ import annotations

import re
from typing import Any

LAUGH_RE = re.compile(
    r"\b(?:haha|hehe|lol|lmao|laugh(?:ing|s|ed)?|dying|lose it|lost it|hilarious)\b",
    re.I,
)
REACTION_RE = re.compile(
    r"\b(?:oh my|holy|no way|what the|wtf|damn|crazy|insane|unreal|stop it|"
    r"get out|are you serious|nah|wait wait|bro|dude|come here|granny|old lady|"
    r"lord|jesus|why|run|shoot|kill|suppressor|family friendly)\b",
    re.I,
)


def _text_after(clip: dict, transcript: list[dict], window: float) -> str:
    end = float(clip.get("end", 0))
    return " ".join(
        str(s.get("text", ""))
        for s in transcript
        if end - 1 <= float(s.get("start", 0)) <= end + window
    )


def _spike_after(clip: dict, audio: list[dict], window: float) -> float:
    if not audio:
        return 0.0
    end = float(clip.get("end", 0))
    return max(
        (float(b.get("spike", 0)) for b in audio
         if end - 2 <= float(b.get("start", 0)) <= end + window),
        default=0.0,
    )


def extend_to_payoff(
    clips: list[dict],
    transcript: list[dict],
    audio: list[dict] | None = None,
    max_extend: float = 18.0,
    duration: float | None = None,
) -> list[dict]:
    """Extend each clip forward when a reaction/payoff follows the cut."""
    audio = audio or []
    out: list[dict] = []
    for c in clips:
        item = dict(c)
        start = float(item.get("start", 0))
        end = float(item.get("end", start + 30))
        after = _text_after(item, transcript, max_extend)
        spike = _spike_after(item, audio, max_extend)
        has_reaction = bool(LAUGH_RE.search(after) or REACTION_RE.search(after))
        loud = spike >= 3.2
        if has_reaction or loud:
            # Keep the whole reacting segment plus a short tail.
            candidates = [
                float(s.get("end", 0)) + 1.5
                for s in transcript
                if end - 1 <= float(s.get("start", 0)) <= end + max_extend
                and (LAUGH_RE.search(str(s.get("text", ""))) or REACTION_RE.search(str(s.get("text", ""))))
            ]
            if loud:
                candidates.extend(
                    float(b.get("start", 0)) + 7
                    for b in audio
                    if end - 2 <= float(b.get("start", 0)) <= end + max_extend
                    and float(b.get("spike", 0)) >= 3.2
                )
            if candidates:
                new_end = max(end, min(candidates))
                item["end"] = round(min(new_end, end + max_extend, duration or new_end), 2)
                item["payoff_extended"] = True
                item["payoff_reason"] = (
                    f"extended {round(item['end'] - end, 1)}s for "
                    f"{'laugh/reaction' if has_reaction else 'audio spike'}"
                )
        out.append(item)
    return out


def merge_sequences(
    clips: list[dict],
    transcript: list[dict],
    audio: list[dict] | None = None,
    max_span: float = 85.0,
    gap: float = 14.0,
) -> list[dict]:
    """Merge neighbouring reaction clips into one longer sequence clip."""
    if len(clips) < 2:
        return clips
    ordered = sorted((dict(c) for c in clips), key=lambda c: float(c.get("start", 0)))
    audio = audio or []
    merged: list[dict] = []
    for c in ordered:
        if merged:
            prev = merged[-1]
            prev_end = float(prev.get("end", 0))
            cur_start = float(c.get("start", 0))
            between = _text_after(prev, transcript, max(gap + 4, cur_start - prev_end + 4))
            hot = bool(LAUGH_RE.search(between) or REACTION_RE.search(between))
            strong = float(c.get("score", 0)) >= 8 and float(prev.get("score", 0)) >= 8
            span_ok = float(c.get("end", 0)) - float(prev.get("start", 0)) <= max_span
            if hot and strong and span_ok and cur_start - prev_end <= gap:
                prev["end"] = round(float(c.get("end", 0)), 2)
                prev["title"] = f"{prev.get('title', 'Moment')} → {c.get('title', 'more')}"[:100]
                prev["reason"] = (prev.get("reason", "") + " · part of a chaotic sequence")[:300]
                prev["sequence"] = True
                continue
        merged.append(c)
    return merged


def exclude_used(candidates: list[dict], used: list[dict], min_gap: float = 20.0) -> list[dict]:
    """Remove candidates that overlap clips already rendered for this source."""
    out = []
    for c in candidates:
        s, e = float(c.get("start", 0)), float(c.get("end", 0))
        if any(not (e + min_gap < float(u.get("start", 0)) or s - min_gap > float(u.get("end", 0)))
               for u in used):
            continue
        out.append(c)
    return out
