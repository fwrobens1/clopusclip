"""Fuse transcript comedy + audio energy + visual gameplay into ranked clips.

This replaces transcript-only picking. Every candidate is scored on:
  - text comedy signal (relative to local baseline)
  - real audio loudness spike (crowd reaction proxy)
  - gameplay-vs-IRL preference (GTA channel wants gameplay)
  - strong-model judgement of whether the moment is actually funny
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from audio_signals import spikes_near
from ai import chat_json_strong
from moments import build_candidates
from visual_signals import analyze_url

ROOT = Path(__file__).resolve().parent.parent


def fuse(url: str, transcript: list[dict], duration: float, num_clips: int,
         audio_file: str, goal: str, prefer_gameplay: bool = True) -> list[dict]:
    audio = json.loads(Path(audio_file).read_text(encoding="utf-8"))
    candidates = build_candidates(transcript, duration, num_clips, per_bucket=5)
    if len(candidates) < num_clips:
        return []

    # Add the strongest audio spikes as extra candidates (the model's text
    # shortlist can miss pure reaction moments with little dialogue).
    top_spikes = sorted(audio, key=lambda b: b["spike"], reverse=True)[:num_clips * 3]
    for b in top_spikes:
        if b["spike"] < 4:
            continue
        start = max(0.0, b["start"] - 10)
        excerpt = " ".join(
            str(s.get("text", "")) for s in transcript
            if start <= float(s.get("start", 0)) <= start + 55
        )[:500]
        if not excerpt.strip():
            continue
        candidates.append({
            "start": round(start, 2), "end": round(start + 55, 2),
            "score": min(20.0, b["spike"] / 3),
            "excerpt": excerpt, "audio_spike": b["spike"],
        })

    # Enrich each candidate with measured signals.
    for c in candidates:
        mx, mean = spikes_near(audio, c["start"], c["end"])
        c["audio_spike"] = round(mx, 2)
        c["audio_mean"] = round(mean, 2)
        t = c["start"] + min(20, (c["end"] - c["start"]) / 2)
        vis = analyze_url(url, [t])
        c["gameplay"] = vis["gameplay"]
        c["focus"] = {"cx": vis["cx"], "cy": vis["cy"]}
        c["faces"] = vis["faces"]

    # Strong model ranks with all signals visible.
    lines = []
    for i, c in enumerate(candidates):
        lines.append(
            f"{i}. [{int(c['start']//3600):02d}:{int(c['start']%3600//60):02d}:{int(c['start']%60):02d}] "
            f"text={c['score']:.1f} audio_spike={c['audio_spike']:.1f} "
            f"gameplay={c['gameplay']:.2f} faces={c['faces']}\n   \"{c['excerpt'][:350]}\""
        )
    gp_note = (
        "STRONGLY prefer gameplay moments (gameplay above 0.5). Reject IRL talking-head "
        "segments unless the dialogue is exceptionally funny and self-contained."
        if prefer_gameplay else "Mix gameplay and IRL moments."
    )
    system = (
        "You are a viral gaming-clip editor. Pick moments a GTA 6 fan would actually laugh at "
        "and rewatch: absurd failures, genuine reactions, chaotic accidents, sharp jokes with a "
        "payoff. Reject generic shooting narration and boring conversation. Return ONLY JSON."
    )
    user = f"""Candidates with measured signals ({len(candidates)} total):

{chr(10).join(lines)}

User brief: {goal}

{gp_note}
audio_spike is loudness versus the local baseline — high values mean a real shout/laugh/crowd reaction.
Choose exactly {num_clips} candidates by index. Variety across the stream matters.
Return ONLY:
{{"clips":[{{"index":3,"title":"punchy title","reason":"why it is funny","score":9}}]}}
No extra keys."""
    data = chat_json_strong(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=2048,
    )
    picks = data.get("clips", []) if isinstance(data, dict) else []
    out = []
    used: set[int] = set()
    for p in picks:
        try:
            idx = int(p.get("index", -1))
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(candidates)) or idx in used:
            continue
        used.add(idx)
        c = candidates[idx]
        out.append({
            "start": c["start"], "end": c["end"],
            "title": str(p.get("title", "Funny moment")).strip()[:100],
            "reason": str(p.get("reason", "")).strip()[:300],
            "score": p.get("score", 8),
            "audio_spike": c["audio_spike"],
            "gameplay": c["gameplay"],
            "focus": c["focus"],
        })

    # Top up by combined signal if the model under-returns.
    if len(out) < num_clips:
        def rank(c):
            if any(abs(c["start"] - o["start"]) < 50 for o in out):
                return -999
            s = c["score"] + min(8, c["audio_spike"]) * 0.8
            if prefer_gameplay:
                s += c["gameplay"] * 5
            return s
        for c in sorted(candidates, key=rank, reverse=True):
            if len(out) >= num_clips:
                break
            if any(abs(c["start"] - o["start"]) < 50 for o in out):
                continue
            out.append({
                "start": c["start"], "end": c["end"],
                "title": f"Reaction at {int(c['start']//3600)}h{int(c['start']%3600//60):02d}m",
                "reason": "High combined audio + text signal", "score": 7,
                "audio_spike": c["audio_spike"], "gameplay": c["gameplay"],
                "focus": c["focus"],
            })
    out.sort(key=lambda x: x["start"])
    return out[:num_clips]
