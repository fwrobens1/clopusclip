"""AI client for ClipForge.

Uses free NVIDIA NIM models with a fallback chain. All prompts ask for strict JSON,
and responses are parsed defensively (handles markdown fences, reasoning wrappers).
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

NVIDIA_KEY = os.getenv("NVIDIA_API_KEY", "")
NVIDIA_BASE = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
# Free providers can stall. Keep the UI responsive by failing over quickly.
AI_TIMEOUT = max(5.0, min(90.0, float(os.getenv("AI_TIMEOUT", "20"))))

# Ordered fallback chain. All are free on NVIDIA NIM.
MODELS = [
    # Fastest first: Nemotron with thinking disabled returns clean JSON in ~0.2s.
    ("nvidia/nemotron-3.5-lightning-30b-a3b", {"chat_template_kwargs": {"thinking": False}}),
    ("nvidia/nemotron-3-nano-omni-30b-a3b-reasoning", {"chat_template_kwargs": {"thinking": False}}),
    ("minimaxai/minimax-m3.1-flash", {}),
    ("moonshotai/kimi-k3.1-pro", {}),
]


def _extract_json(text: str) -> Any:
    """Pull the first JSON object/array out of a messy LLM response."""
    if not text:
        raise ValueError("empty response")
    text = text.strip()
    # Strip markdown code fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find first {...} or [...] block
    for opener, closer in (("{", "}"), ("[", "]")):
        i = text.find(opener)
        if i == -1:
            continue
        depth = 0
        in_str = False
        esc = False
        for j in range(i, len(text)):
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[i : j + 1])
                    except json.JSONDecodeError:
                        break
    raise ValueError(f"no valid JSON in response: {text[:200]!r}")


def _call_model(client, model, extra, messages, max_tokens, temperature):
    """Call one model; return (content, reasoning, error)."""
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        **extra,
    }
    try:
        r = client.post(
            f"{NVIDIA_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {NVIDIA_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if r.status_code != 200:
            return None, None, f"HTTP {r.status_code} {r.text[:120]}"
        data = r.json()
        msg = data.get("choices", [{}])[0].get("message", {})
        return msg.get("content"), msg.get("reasoning_content"), None
    except Exception as e:  # noqa: BLE001
        return None, None, f"{type(e).__name__}: {e}"


def chat(messages: list[dict], max_tokens: int = 1024, temperature: float = 0.2) -> str:
    """Send chat messages through the fallback chain; return content string."""
    errors = []
    with httpx.Client(timeout=AI_TIMEOUT) as client:
        for model, extra in MODELS:
            content, reasoning, err = _call_model(client, model, extra, messages, max_tokens, temperature)
            if err:
                errors.append(f"{model}: {err}")
                continue
            if content:
                return content
            if reasoning:
                return reasoning
            errors.append(f"{model}: empty content")
    raise RuntimeError("All AI models failed: " + " | ".join(errors))


def chat_json(messages: list[dict], max_tokens: int = 1024) -> Any:
    """Chat across the fallback chain until one model returns valid JSON."""
    errors = []
    with httpx.Client(timeout=AI_TIMEOUT) as client:
        for model, extra in MODELS:
            content, reasoning, err = _call_model(client, model, extra, messages, max_tokens, 0.1)
            if err:
                errors.append(f"{model}: {err}")
                continue
            for text in (content, reasoning):
                if not text:
                    continue
                try:
                    return _extract_json(text)
                except ValueError:
                    pass
            errors.append(f"{model}: no valid JSON")
    raise RuntimeError("No model returned valid JSON: " + " | ".join(errors))


STRONG_MODELS = [
    # Fast path first; only fall back to slower models when needed.
    ("nvidia/nemotron-3.5-lightning-30b-a3b", {"chat_template_kwargs": {"thinking": False}}),
    ("minimaxai/minimax-m3.1-flash", {}),
    ("moonshotai/kimi-k3.1-pro", {}),
]


def chat_json_strong(messages: list[dict], max_tokens: int = 1024) -> Any:
    """Prefer the more capable models when ranking comedic moments."""
    errors = []
    with httpx.Client(timeout=AI_TIMEOUT) as client:
        for model, extra in STRONG_MODELS:
            content, reasoning, err = _call_model(client, model, extra, messages, max_tokens, 0.1)
            if err:
                errors.append(f"{model}: {err}")
                continue
            for text in (content, reasoning):
                if not text:
                    continue
                try:
                    return _extract_json(text)
                except ValueError:
                    pass
            errors.append(f"{model}: no valid JSON")
    raise RuntimeError("No strong model returned valid JSON: " + " | ".join(errors))


def _format_transcript(transcript: list[dict]) -> str:
    lines = []
    for seg in transcript:
        start = float(seg.get("start", 0))
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        lines.append(f"[{int(start // 60):02d}:{int(start % 60):02d}] {text}")
    return "\n".join(lines) or "(no speech detected)"


def _parse_selection(data, video_duration, clip_length, num_clips) -> list[dict]:
    clips = data.get("clips", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    out = []
    for c in clips:
        try:
            start = float(c.get("start", 0))
            end = float(c.get("end", 0))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        start = max(0.0, min(start, max(0.0, video_duration - 2)))
        end = min(video_duration, max(start + 2, end))
        # Enforce the requested length band even if the model ignores the prompt.
        lo, hi = {"short": (15, 30), "medium": (30, 60), "long": (60, 90)}.get(
            clip_length, (30, 60)
        )
        if video_duration >= num_clips * lo:
            if end - start > hi:
                end = start + hi
            elif end - start < lo and start + lo <= video_duration:
                end = start + lo
        out.append(
            {
                "start": round(start, 2),
                "end": round(end, 2),
                "title": str(c.get("title", "Viral moment")).strip()[:100],
                "reason": str(c.get("reason", "")).strip()[:300],
                "score": c.get("score", 7),
            }
        )
    out.sort(key=lambda x: x["start"])
    return out[:num_clips]


def _ask_for_moments(transcript, video_duration, num_clips, target, goal, scope_note, max_tokens=1536):
    system = (
        "You are a world-class viral video editor. Given a timestamped transcript, "
        "choose the most engaging, standalone moments that would perform well as short clips. "
        "Prefer moments with a complete thought, strong hook, emotion, surprise, humor, or a clear payoff. "
        "Return ONLY a JSON object, no explanation, no markdown."
    )
    goal_line = ""
    if goal and goal.strip():
        goal_line = f"\nThe user's specific creative brief: \"{goal.strip()[:300]}\"\nPrioritize moments that satisfy this brief above generic virality.\n"
    user = f"""Video duration: {int(video_duration // 60)}:{int(video_duration % 60):02d}
{scope_note}

Transcript:
{_format_transcript(transcript)}
{goal_line}
Choose {num_clips} viral clip moments. EACH CLIP MUST BE {target} — this is a hard requirement, not a suggestion. Clips outside that range will be cut to fit.
Return ONLY this JSON shape:
{{"clips":[{{"start":12.5,"end":45.2,"title":"A catchy title","reason":"why this will go viral","score":9}}]}}
start and end must be numbers in seconds within the video duration. start < end.
Avoid overlaps when possible, but if the video is short, shorter or slightly overlapping clips are fine.
ALWAYS return the JSON object, even if the ideal length is not possible. No extra keys."""
    return chat_json(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=max_tokens,
    )


def _rank_candidates(candidates, num_clips, target, goal, video_duration, clip_length) -> list[dict]:
    """Ask the strong model to pick genuinely funny moments from a shortlist."""
    lo_band, hi_band = {"short": (15, 30), "medium": (30, 60), "long": (60, 90)}.get(
        clip_length, (30, 60)
    )
    lines = []
    for i, c in enumerate(candidates):
        start = float(c["start"])
        lines.append(
            f"{i}. [{int(start // 3600):02d}:{int(start % 3600 // 60):02d}:{int(start % 60):02d}] "
            f"signal={c['score']:.1f}\n   \"{c['excerpt'][:500]}\""
        )
    system = (
        "You are a viral comedy editor for gaming streams. You are given a shortlist of "
        "high-energy transcript moments. Pick the ones that would actually make a viewer "
        "laugh, rewatch, and send to a friend — not merely loud gameplay commentary. "
        "A good clip has a setup and a payoff: a joke, an absurd reaction, a embarrassing "
        "failure, a chaotic accident, or a genuine laugh. Reject moments that are only "
        "generic 'oh my god' / shooting narration with no comedic beat. "
        "Return ONLY a JSON object, no explanation, no markdown."
    )
    brief = f"\nThe user's brief: \"{goal.strip()[:300]}\"\n" if goal and goal.strip() else ""
    user = f"""Shortlist ({len(candidates)} candidates, spread across the video):

{chr(10).join(lines)}
{brief}
Choose exactly {num_clips} candidates with the highest comedy value. Use each candidate's index.
Prefer variety: do not pick two nearby moments from the same sequence.
Each clip must be {target}; set start/end within 6 seconds of the candidate's timestamps.
Return ONLY:
{{"clips":[{{"index":3,"start":123.0,"end":165.0,"title":"a short punchy title","reason":"why this is funny","score":9}}]}}
No extra keys."""
    data = chat_json_strong(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=2048,
    )
    picks = data.get("clips", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    out: list[dict] = []
    for p in picks:
        try:
            idx = int(p.get("index", -1))
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(candidates)):
            continue
        c = candidates[idx]
        try:
            start = float(p.get("start", c["start"]))
            end = float(p.get("end", c["end"]))
        except (TypeError, ValueError):
            start, end = float(c["start"]), float(c["end"])
        # Some models return candidate indices or wild timestamps instead of
        # seconds. Never let the pick drift away from the shortlisted moment.
        c_start, c_end = float(c["start"]), float(c["end"])
        start = max(0.0, min(max(start, c_start - 6), c_start + 6))
        end = max(start + lo_band, min(max(end, c_end - 6), c_end + 6))
        if end - start > hi_band:
            end = start + hi_band
        if end - start < lo_band:
            end = min(video_duration, start + lo_band)
        out.append({
            "start": round(start, 2),
            "end": round(end, 2),
            "title": str(p.get("title", "Funny moment")).strip()[:100],
            "reason": str(p.get("reason", "")).strip()[:300],
            "score": p.get("score", 8),
        })
    out.sort(key=lambda x: x["start"])
    return out[:num_clips]


def _high_energy_fallback(transcript, lo_s, hi_s, clip_length) -> dict:
    """Pick the loudest segment in a window when the AI call fails."""
    lo, hi = {"short": (15, 30), "medium": (30, 60), "long": (60, 90)}.get(clip_length, (30, 60))
    best, best_score = None, -1
    for seg in transcript:
        start = float(seg.get("start", 0))
        if not (lo_s <= start < hi_s):
            continue
        text = str(seg.get("text", ""))
        score = text.count("!") * 2 + text.count("?") + sum(
            text.lower().count(k) for k in ("haha", "lol", "no way", "let's go", "wow")
        )
        if score > best_score:
            best, best_score = seg, score
    if best is None:
        best = next((s for s in transcript if lo_s <= float(s.get("start", 0)) < hi_s), None)
    if best is None:
        return {"start": lo_s, "end": min(hi_s, lo_s + hi), "title": "Highlight", "reason": "", "score": 5}
    start = max(lo_s, float(best.get("start", 0)) - 4)
    return {
        "start": round(start, 2),
        "end": round(min(hi_s, start + (hi if hi_s - start >= hi else max(lo, hi_s - start))), 2),
        "title": str(best.get("text", "Highlight")).strip()[:100],
        "reason": "",
        "score": 5,
    }


def select_clips(
    transcript: list[dict],
    video_duration: float,
    num_clips: int = 5,
    clip_length: str = "medium",
    goal: str = "",
) -> list[dict]:
    """Ask the AI to pick viral moments, sampled across long videos for real variety."""
    # Adapt to short videos (e.g. 5 clips from a 30s video can't each be 30s).
    max_possible = max(4, video_duration / max(1, num_clips))
    target = f"{min(15, int(max_possible))}-{min(30, int(max_possible)) + 2} seconds"
    if video_duration >= num_clips * 30:
        target = {"short": "15-30 seconds", "medium": "30-60 seconds", "long": "60-90 seconds"}.get(
            clip_length, "30-60 seconds"
        )

    lo_band, hi_band = {"short": (15, 30), "medium": (30, 60), "long": (60, 90)}.get(
        clip_length, (30, 60)
    )

    # Comedy-first path: score the whole transcript for reaction/punchline
    # bursts, then let the stronger model rank a short shortlist. This avoids
    # the "loudest gunfire commentary" clips that a plain transcript pick returns.
    if video_duration >= 2 * 3600 and num_clips >= 2:
        try:
            import moments  # local import avoids circular imports
            candidates = moments.build_candidates(
                transcript, video_duration, num_clips,
                per_bucket=4, short=lo_band, long=hi_band,
            )
            if len(candidates) >= num_clips:
                ranked = _rank_candidates(
                    candidates, num_clips, target, goal, video_duration, clip_length
                )
                if ranked:
                    # The model occasionally returns fewer than requested —
                    # top up with unused shortlisted candidates by signal.
                    if len(ranked) < num_clips:
                        for c in sorted(candidates, key=lambda x: x["score"], reverse=True):
                            if len(ranked) >= num_clips:
                                break
                            if any(abs(c["start"] - r["start"]) < lo_band for r in ranked):
                                continue
                            ranked.append({
                                "start": c["start"], "end": c["end"],
                                "title": c["excerpt"][:80],
                                "reason": c.get("reason", "High-energy reaction beat"),
                                "score": c["score"],
                            })
                        ranked.sort(key=lambda x: x["start"])
                    return ranked[:num_clips]
        except Exception:
            pass

    # For long VODs, a single call effectively only sees the start of the transcript.
    # Split the timeline into one window per requested clip and pick from each window.
    if video_duration >= 2 * 3600 and num_clips >= 2:
        window = video_duration / num_clips
        out: list[dict] = []
        for i in range(num_clips):
            lo_s, hi_s = i * window, (i + 1) * window
            subset = [
                s for s in transcript
                if lo_s <= float(s.get("start", 0)) < hi_s and str(s.get("text", "")).strip()
            ]
            if not subset:
                continue
            scope = (
                f"You are looking ONLY at the section from {int(lo_s)}s to {int(hi_s)}s of this video. "
                f"Pick exactly ONE clip from this section. Do not pick moments outside this section."
            )
            try:
                data = _ask_for_moments(subset, video_duration, 1, target, goal, scope, max_tokens=512)
                picks = _parse_selection(data, video_duration, clip_length, 1)
                if picks:
                    start = max(lo_s, min(picks[0]["start"], hi_s - lo_band))
                    picks[0]["start"] = round(start, 2)
                    picks[0]["end"] = round(min(hi_s, max(start + lo_band, picks[0]["end"])), 2)
                    out.extend(picks)
                    continue
            except Exception:
                pass
            out.append(_high_energy_fallback(subset, lo_s, hi_s, clip_length))
        out.sort(key=lambda x: x["start"])
        return out[:num_clips]

    data = _ask_for_moments(
        transcript, video_duration, num_clips, target, goal,
        f"Choose clips from anywhere in the video. Clip length: {target}.",
    )
    return _parse_selection(data, video_duration, clip_length, num_clips)
