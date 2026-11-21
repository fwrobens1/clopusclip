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
    with httpx.Client(timeout=60.0) as client:
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
    with httpx.Client(timeout=60.0) as client:
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


def select_clips(
    transcript: list[dict],
    video_duration: float,
    num_clips: int = 5,
    clip_length: str = "medium",
    goal: str = "",
) -> list[dict]:
    """Ask the AI to pick the most viral-worthy moments from a timestamped transcript."""
    # Adapt to short videos (e.g. 5 clips from a 30s video can't each be 30s).
    max_possible = max(4, video_duration / max(1, num_clips))
    target = f"{min(15, int(max_possible))}-{min(30, int(max_possible)) + 2} seconds"
    if video_duration >= num_clips * 30:
        target = {"short": "15-30 seconds", "medium": "30-60 seconds", "long": "60-90 seconds"}.get(
            clip_length, "30-60 seconds"
        )

    # Compact transcript: [mm:ss] text
    lines = []
    for seg in transcript:
        start = float(seg.get("start", 0))
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        lines.append(f"[{int(start // 60):02d}:{int(start % 60):02d}] {text}")
    transcript_text = "\n".join(lines) or "(no speech detected)"

    goal_line = ""
    if goal and goal.strip():
        goal_line = f"\nThe user's specific creative brief: \"{goal.strip()[:300]}\"\nPrioritize moments that satisfy this brief above generic virality.\n"

    system = (
        "You are a world-class viral video editor. Given a timestamped transcript, "
        "choose the most engaging, standalone moments that would perform well as short clips. "
        "Prefer moments with a complete thought, strong hook, emotion, surprise, humor, or a clear payoff. "
        "Return ONLY a JSON object, no explanation, no markdown."
    )
    user = f"""Video duration: {int(video_duration // 60)}:{int(video_duration % 60):02d}

Transcript:
{transcript_text}
{goal_line}
Choose {num_clips} viral clip moments. EACH CLIP MUST BE {target} — this is a hard requirement, not a suggestion. Clips outside that range will be cut to fit.
Return ONLY this JSON shape:
{{"clips":[{{"start":12.5,"end":45.2,"title":"A catchy title","reason":"why this will go viral","score":9}}]}}
start and end must be numbers in seconds within the video duration. start < end.
Avoid overlaps when possible, but if the video is short, shorter or slightly overlapping clips are fine.
ALWAYS return the JSON object, even if the ideal length is not possible. No extra keys."""

    data = chat_json(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=1536,
    )
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
