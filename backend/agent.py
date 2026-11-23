"""Conversational agent: turns natural language into clipping tasks and talks like a real assistant."""
from __future__ import annotations

import re
from typing import Any

from ai import chat, chat_json

URL_RE = re.compile(r"https?://[^\s<>\"']+")

# Fast, deterministic answers for common questions about the product. These keep the
# agent conversational even when a free model times out or falls back.
CAPABILITY_ANSWERS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("live", "livestream", "real time", "realtime", "in realtime"),
        "Yes — the Live view records a stream and clips it in rolling passes while it's on. "
        "It's a local MVP: the machine running it needs to stay on with disk space for the buffer. "
        "Finished VODs are still the easiest path.",
    ),
    (
        ("youtube", "twitch", "kick", "platform", "what sites", "which sites", "1000"),
        "Paste a YouTube, Twitch or Kick link — yt-dlp covers 1000+ sites. Subtitle-first when captions exist, "
        "audio-only fallback otherwise. No full VOD download.",
    ),
    (
        ("caption", "subtitle", "transcript"),
        "Captions are word-level and burned in when you ask for them. If the source has no subtitle track, "
        "Groq turbo transcribes the audio in minutes, or you can import a transcript you made elsewhere.",
    ),
    (
        ("cloud", "storage", "supabase", "where are my clips", "disk", "space"),
        "Finished clips upload to a private Supabase bucket and the dashboard plays them through signed links, "
        "so your machine doesn't hold the MP4s. Source audio and sections stay local and are cached.",
    ),
    (
        ("free", "price", "cost", "credit card", "pay"),
        "The app itself is free. AI selection runs on free NVIDIA NIM models and ASR can use a free Groq key, "
        "local Whisper, or a free Kaggle/Colab notebook. No card needed.",
    ),
    (
        ("gpu", "render", "fast", "how long", "7 hour", "seven hour", "minutes"),
        "The bottleneck is transcription, not cutting. With Groq turbo a 7-hour VOD is transcribed in minutes, "
        "then only the slices you keep get rendered — locally, with GPU encoding when available and CPU fallback.",
    ),
)

CAPABILITY_CUES = (
    "can you", "could you", "do you", "does it", "does this", "is it", "are you",
    "what can", "what do you", "how do", "how does", "why do", "do i need",
    "support", "capable", "possible", "work with", "built for", "who are you", "what are you",
)


def is_conversational(message: str) -> bool:
    """True when the message is a question/chat, not a request to produce clips."""
    text = (message or "").strip().lower()
    if not text:
        return True
    if extract_url(text):
        return False
    if any(cue in text for cue in CAPABILITY_CUES):
        return True
    # Bare questions and short greetings/chat are conversational too.
    if text.endswith("?") and not re.search(r"\b(clip|render|make|cut|create)\b\s+\d", text):
        return True
    if len(text.split()) <= 4 and not re.search(r"\b(clip|render|cut|make|rerender)\b", text):
        return True
    return False


def quick_answer(message: str) -> str | None:
    """Return a deterministic answer for common capability questions, if matched."""
    text = (message or "").lower()
    for cues, answer in CAPABILITY_ANSWERS:
        if any(cue in text for cue in cues):
            return answer
    return None


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
- "clip" ONLY when there is a URL or an explicit imperative request to make clips (e.g. "clip this", "make 5 clips of ...").
- "rerender" when the user asks to change captions/format/length of an existing result.
- "chat" for greetings, questions and capability checks — INCLUDING sentences containing the word "clip", such as "can you clip lives?", "do you support Twitch?", "what can you do?", "how do you clip?". These are questions about the app, NOT clipping tasks.
- Never mark a capability question as "clip". No URL means no new job unless the user clearly points at an existing source and asks for clips.
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
    """Generate a short conversational reply, optionally grounded in job context.

    IMPORTANT: a question about the app is a real conversation — answer it. Only
    report on a job when the user actually asks about that job or a job was just
    started/completed by this message. Never paste a stale job report into every reply.
    """
    text = (message or "").strip()
    conversational = is_conversational(text)

    if conversational:
        quick = quick_answer(text)
        if quick:
            return quick

    system = (
        "You are the NoetharaClipz agent — a concise, capable assistant for a video clipping app. "
        "Answer the user's ACTUAL message first. If they ask a question about what the app can do, "
        "answer that question directly and stay on topic — do not volunteer a report about old clips. "
        "Be direct and warm, like a helpful teammate. Never mention being an AI. "
        "Keep replies under 3 sentences. "
        "Only mention clip counts, titles or render status when the user asks about a specific job "
        "or when you have just started/finished one in this turn. "
        "Capabilities: Twitch/YouTube/Kick links via yt-dlp; subtitle-first ingestion with Groq turbo ASR "
        "or local Whisper; payoff-aware moment scoring with reasons; review before render with 360p drafts; "
        "facecam-top/gameplay-bottom 9:16 reframing; word-level captions; a local Live view that clips "
        "streams in rolling passes; clips stored in Supabase and played via signed links; "
        "a publish queue for TikTok/YouTube Shorts."
    )
    user = text
    if context and not conversational:
        user += f"\n\nRelevant current job context (use only if the user asks about it):\n{context}"
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
