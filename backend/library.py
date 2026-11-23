"""Persistent local library: streamer folders, clip notes, presets, publish queue."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = ROOT / "work" / "library"
LIB_DIR.mkdir(parents=True, exist_ok=True)
LIBRARY_PATH = LIB_DIR / "library.json"
PRESET_PATH = LIB_DIR / "crop_presets.json"
PUBLISH_PATH = LIB_DIR / "publish_queue.json"
LIVE_PATH = LIB_DIR / "live_sessions.json"
PUBLICATIONS_PATH = LIB_DIR / "publications.json"
DICT_PATH = ROOT / "work" / "caption_dictionary.json"


def _load(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def clean_channel(name: str) -> str:
    name = re.sub(r"\s*\(?https?://.*", "", str(name or "")).strip(" -|·")
    return name or "Unsorted"


def channel_for(info: dict | None, url: str | None = None) -> str:
    info = info or {}
    for key in ("channel", "uploader", "creator", "broadcaster"):
        if info.get(key):
            return clean_channel(info[key])
    if url:
        host = urlparse(url).netloc.lower()
        if "twitch" in host:
            m = re.search(r"twitch\.tv/([^/?#]+)", url, re.I)
            if m:
                return clean_channel(m.group(1))
        if "youtube" in host:
            return "YouTube"
    return "Unsorted"


def load_library() -> dict[str, dict]:
    return _load(LIBRARY_PATH, {})


def save_library(data: dict[str, dict]) -> None:
    _save(LIBRARY_PATH, data)


def upsert_job(job: dict) -> dict:
    data = load_library()
    jid = job.get("id")
    if not jid:
        return data
    channel = job.get("channel") or channel_for(job.get("source_info"), job.get("source_url"))
    data[jid] = {
        "id": jid,
        "channel": channel,
        "title": (job.get("source_info") or {}).get("title") or job.get("filename") or job.get("goal") or "Untitled",
        "url": job.get("source_url") or "",
        "created_at": job.get("created_at") or "",
        "status": job.get("status", "queued"),
        "goal": job.get("goal", ""),
        "clips": len(job.get("clips") or []),
        "settings": job.get("settings", {}),
        "parent_job": job.get("parent_job"),
        "notes": job.get("notes", {}),
        "thumbnail": (job.get("source_info") or {}).get("thumbnail") or "",
    }
    save_library(data)
    return data


def grouped() -> list[dict]:
    data = load_library()
    groups: dict[str, list[dict]] = {}
    for item in data.values():
        groups.setdefault(item.get("channel") or "Unsorted", []).append(item)
    out = []
    for channel, jobs in groups.items():
        jobs.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        out.append({
            "channel": channel,
            "count": len(jobs),
            "clips": sum(int(j.get("clips") or 0) for j in jobs),
            "jobs": jobs,
        })
    out.sort(key=lambda g: (g["count"], g["channel"]), reverse=True)
    return out


def save_job_detail(job_id: str, detail: dict) -> None:
    """Persist lightweight job metadata (no transcript) for restart recovery."""
    detail_dir = LIB_DIR / "jobs"
    detail_dir.mkdir(parents=True, exist_ok=True)
    _save(detail_dir / f"{job_id}.json", detail)


def load_job_details() -> dict[str, dict]:
    detail_dir = LIB_DIR / "jobs"
    if not detail_dir.exists():
        return {}
    out: dict[str, dict] = {}
    for p in detail_dir.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if d.get("id"):
                out[d["id"]] = d
        except (OSError, json.JSONDecodeError):
            continue
    return out


def set_note(job_id: str, index: int, note: str) -> dict:
    data = load_library()
    item = data.get(job_id)
    if not item:
        return {}
    notes = item.setdefault("notes", {})
    notes[str(index)] = note
    save_library(data)
    return item


def load_presets() -> dict[str, dict]:
    return _load(PRESET_PATH, {})


def save_preset(channel: str, focus: dict, face_shift_ms: int = 150) -> dict:
    data = load_presets()
    data[clean_channel(channel)] = {
        "cx": float(focus.get("cx", 0.10)),
        "cy": float(focus.get("cy", 0.30)),
        "face_shift_ms": float(face_shift_ms),
        "watermark": focus.get("watermark", "@NOCTX"),
    }
    _save(PRESET_PATH, data)
    return data


def load_publish_queue() -> list[dict]:
    return _load(PUBLISH_PATH, [])


def save_publish_queue(items: list[dict]) -> list[dict]:
    _save(PUBLISH_PATH, items)
    return items


def load_live_sessions() -> dict[str, dict]:
    return _load(LIVE_PATH, {})


def save_live_sessions(items: dict[str, dict]) -> dict[str, dict]:
    _save(LIVE_PATH, items)
    return items


def load_publications() -> list[dict]:
    return _load(PUBLICATIONS_PATH, [])


def save_publications(items: list[dict]) -> list[dict]:
    _save(PUBLICATIONS_PATH, items)
    return items


def load_dictionary() -> dict[str, str]:
    return _load(DICT_PATH, {})


def save_dictionary(entries: dict[str, str]) -> dict[str, str]:
    _save(DICT_PATH, entries)
    return entries
