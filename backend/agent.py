"""Conversational agent: turns natural language into clipping tasks and reports back."""
from __future__ import annotations

import re
from typing import Any

from ai import chat, chat_json

URL_RE = re.compile(r"https?://[^\s<>\"']+")


def extract_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    if not m:
        return None
    url = m.group(0).rstrip(".,;:!?)")
    # Keep common URL query syntax intact
    return url


def parse_request(message: str) -> dict[str, Any]:
    """Parse a user message into a structured clipping request."""
    url = extract_url(message)
    fallback = {
        "url": url,
        "goal": message.strip(),
        "num_clips": 5,
        "clip_length": "medium",
        "captions": True,
        "vertical": True,
        "action": "clip" if url else "chat",
    }
    if not message or not message.strip():
        return fallback

    prompt = f"""You are the intent parser for a video clipping app. Parse the user's message.

User message:
{message.strip()}

Return ONLY a JSON object with these exact keys:
{{
  "action": "clip" or "chat" or "rerender",
  "url": the video/livestream URL if present else null,
  "goal": a short creative brief describing what to clip (e.g. "the funniest moments"), else null,
  "num_clips": integer 1-12 (default 5),
  "clip_length": "short" | "medium" | "long" (default medium),
  "captions": true/false (default true),
  "vertical": true/false (default true)
}}
Rules:
- "clip" when there is a URL or clear request to make clips.
- "rerender" when the user asks to change captions/format/length of an existing result.
- "chat" for greetings or questions with no clipping request.
- If the user says "funny/funniest/hilarious", put that in goal.
- If the user says "shorts/vertical/9:16" set vertical true; "widescreen/horizontal" set vertical false.
- If the user says "no captions" set captions false.
- Extract number of clips if mentioned ("3 clips", "top 10").
No extra keys, no markdown."""

    try:
        data = chat_json([{"role": "user", "content": prompt}], max_tokens=400)
        if not isinstance(data, dict):
            return fallback
        result = dict(fallback)
        for k in ("action", "goal", "clip_length"):
            if data.get(k) is not None:
                result[k] = str(data[k])[:300]
        if data.get("url"):
            result["url"] = str(data["url"])
        elif url:
            result["url"] = url
        if data.get("num_clips") is not None:
            try:
                result["num_clips"] = max(1, min(12, int(data["num_clips"])))
            except (TypeError, ValueError):
                pass
        for k in ("captions", "vertical"):
            if data.get(k) is not None:
                result[k] = bool(data[k])
        if result["clip_length"] not in ("short", "medium", "long"):
            result["clip_length"] = "medium"
        if result["action"] not in ("clip", "chat", "rerender"):
            result["action"] = "clip" if result["url"] else "chat"
        return result
    except Exception:
        return fallback


def reply(message: str, context: str = "") -> str:
    """Generate a short conversational reply, optionally grounded in job context."""
    system = (
        "You are ClipForge's agent — a concise, capable video-clipping assistant. "
        "Be direct and warm. Never mention being an AI. Keep replies under 3 sentences. "
        "When a job completed, summarize what was produced and invite a follow-up."
    )
    user = message.strip()
    if context:
        user += f"\n\nCurrent job context:\n{context}"
    try:
        return chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=220,
            temperature=0.5,
        ).strip()
    except Exception as e:
        return f"I hit a snag: {e}"
