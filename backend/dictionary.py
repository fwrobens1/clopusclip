"""Local caption/name correction dictionary.

ASR mangles names all the time ("nick eh 30", "gta 6"). Corrections are applied
to transcript text, caption words, titles and hooks before rendering, so the
viewer sees clean text without any paid post-processing.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DICT_PATH = ROOT / "work" / "caption_dictionary.json"

DEFAULTS: dict[str, str] = {
    "nick eh 30": "NickEh30",
    "nick e 30": "NickEh30",
    "nike 30": "NickEh30",
    "gta 6": "GTA 6",
    "gta6": "GTA 6",
    "gta vi": "GTA 6",
    "no ctx": "NOCTX",
    "noctx": "NOCTX",
    "twitch tv": "Twitch",
    "twitch.tv": "Twitch",
    "youtube": "YouTube",
    "kick com": "Kick",
}


def load() -> dict[str, str]:
    data = dict(DEFAULTS)
    if DICT_PATH.exists():
        try:
            data.update(json.loads(DICT_PATH.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return {k.strip().lower(): v.strip() for k, v in data.items() if str(k).strip()}


def save(entries: dict[str, str]) -> dict[str, str]:
    DICT_PATH.parent.mkdir(parents=True, exist_ok=True)
    DICT_PATH.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    return load()


def apply_text(text: str, mapping: dict[str, str] | None = None) -> str:
    if not text:
        return text
    mapping = mapping if mapping is not None else load()
    out = str(text)
    # Longest first so "nick eh 30" wins over "nick".
    for wrong in sorted(mapping, key=len, reverse=True):
        try:
            out = re.sub(re.escape(wrong), lambda m: mapping[wrong], out, flags=re.I)
        except re.error:
            continue
    return out


def apply_words(words: list[dict] | None, mapping: dict[str, str] | None = None) -> list[dict]:
    if not words:
        return words or []
    mapping = mapping if mapping is not None else load()
    for w in words:
        raw = str(w.get("word", "")).strip()
        cleaned = apply_text(raw, mapping)
        if cleaned:
            w["word"] = cleaned
    return words


def apply_segments(segments: list[dict] | None, mapping: dict[str, str] | None = None) -> list[dict]:
    if not segments:
        return segments or []
    mapping = mapping if mapping is not None else load()
    for s in segments:
        if s.get("text"):
            s["text"] = apply_text(str(s["text"]), mapping)
        apply_words(s.get("words"), mapping)
    return segments
