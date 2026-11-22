"""Content-hash caches shared by every ClipForge job.

The goal is simple: never download or compute the same thing twice. Keys are
built from the source URL plus the exact parameters that change the result.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "work" / "cache"
SECTION_CACHE = CACHE_DIR / "sections"
JSON_CACHE = CACHE_DIR / "json"
for _d in (CACHE_DIR, SECTION_CACHE, JSON_CACHE):
    _d.mkdir(parents=True, exist_ok=True)


def key(*parts: Any) -> str:
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:20]


def load_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def cached_json(name: str, key_parts: tuple[Any, ...], producer: Callable[[], Any]) -> Any:
    """Return cached JSON for a key, computing and storing it on a miss."""
    path = JSON_CACHE / f"{name}_{key(*key_parts)}.json"
    hit = load_json(path)
    if hit is not None:
        return hit
    value = producer()
    if value is not None:
        save_json(path, value)
    return value


def section_path(url: str, start: float, end: float) -> Path:
    """Stable local path for one source section, reusable across jobs."""
    return SECTION_CACHE / f"sec_{key(url, round(float(start), 2), round(float(end), 2))}.mp4"


def transcript_path(url: str) -> Path:
    return JSON_CACHE / f"transcript_{key(url)}.json"


def signals_path(url: str) -> Path:
    return JSON_CACHE / f"signals_{key(url)}.json"


def selection_path(url: str, goal: str, settings: Any) -> Path:
    return JSON_CACHE / f"selection_{key(url, goal, settings)}.json"


def invalidate(prefix: str) -> int:
    removed = 0
    for p in list(JSON_CACHE.glob(f"{prefix}*")) + list(SECTION_CACHE.glob(f"{prefix}*")):
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed
