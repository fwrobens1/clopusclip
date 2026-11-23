"""Supabase cloud storage + metadata bridge for NoetharaClipz.

All credentials come from .env via the service_role key. Nothing secret is
exposed to the browser: clips remain behind /api/clips, which redirects to a
short-lived signed Storage URL.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import requests

URL = os.getenv("SUPABASE_URL", "").rstrip("/")
KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
BUCKET = os.getenv("SUPABASE_BUCKET", "rendered-clips")
SIGNED_TTL = int(os.getenv("SUPABASE_SIGNED_TTL", "3600"))
TIMEOUT = float(os.getenv("SUPABASE_TIMEOUT", "120"))


def enabled() -> bool:
    return bool(URL and KEY and BUCKET)


def status() -> dict[str, Any]:
    return {
        "enabled": enabled(),
        "url": URL,
        "bucket": BUCKET,
        "signed_ttl": SIGNED_TTL,
        "tables": ("noethara_jobs", "noethara_clips"),
    }


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    h = {
        "apikey": KEY,
        "Authorization": f"Bearer {KEY}",
    }
    if extra:
        h.update(extra)
    return h


def upload_file(local_path: str | Path, dest_path: str) -> str:
    """Upload a local MP4 to Supabase Storage. Returns the storage path."""
    if not enabled():
        raise RuntimeError("Supabase is not configured")
    p = Path(local_path)
    if not p.exists():
        raise FileNotFoundError(p)
    dest_path = dest_path.replace("\\", "/").lstrip("/")
    with p.open("rb") as f:
        r = requests.post(
            f"{URL}/storage/v1/object/{BUCKET}/{dest_path}",
            headers=_headers({"Content-Type": "video/mp4", "x-upsert": "true"}),
            data=f,
            timeout=TIMEOUT,
        )
    if r.status_code >= 300:
        raise RuntimeError(f"Supabase upload failed ({r.status_code}): {r.text[:300]}")
    return dest_path


def signed_url(dest_path: str, expires: int | None = None) -> str:
    """Create a temporary public URL for a private bucket object."""
    if not enabled():
        raise RuntimeError("Supabase is not configured")
    dest_path = (dest_path or "").replace("\\", "/").lstrip("/")
    r = requests.post(
        f"{URL}/storage/v1/object/sign/{BUCKET}/{dest_path}",
        headers=_headers({"Content-Type": "application/json"}),
        json={"expiresIn": int(expires or SIGNED_TTL)},
        timeout=30,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Supabase signed URL failed ({r.status_code}): {r.text[:300]}")
    data = r.json()
    path = data.get("signedURL") or data.get("signedUrl")
    if not path:
        raise RuntimeError("Supabase signed URL response was empty")
    if path.startswith("http"):
        return path
    if path.startswith("/storage/v1/"):
        return f"{URL}{path}"
    return f"{URL}/storage/v1{path}"


def file_exists(dest_path: str) -> bool:
    if not enabled():
        return False
    dest_path = (dest_path or "").replace("\\", "/").lstrip("/")
    r = requests.head(
        f"{URL}/storage/v1/object/{BUCKET}/{dest_path}",
        headers=_headers(),
        timeout=30,
    )
    return r.status_code < 300


def delete_file(dest_path: str) -> None:
    if not enabled():
        return
    dest_path = (dest_path or "").replace("\\", "/").lstrip("/")
    requests.delete(
        f"{URL}/storage/v1/object/{BUCKET}/{dest_path}",
        headers=_headers(),
        timeout=30,
    )


def _rest(table: str, payload: dict | list, conflict: str | None = None) -> None:
    if not enabled():
        return
    url = f"{URL}/rest/v1/{table}"
    if conflict:
        url += f"?on_conflict={conflict}"
    r = requests.post(
        url,
        headers=_headers({"Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"}),
        json=payload,
        timeout=30,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Supabase {table} upsert failed ({r.status_code}): {r.text[:300]}")


def save_job(detail: dict[str, Any]) -> None:
    """Persist one job summary row."""
    _rest("noethara_jobs", {
        "id": detail.get("id"),
        "channel": detail.get("channel"),
        "title": detail.get("title"),
        "source_url": detail.get("source_url"),
        "goal": detail.get("goal"),
        "status": detail.get("status"),
        "stage": detail.get("stage"),
        "progress": detail.get("progress", 0),
        "duration": detail.get("duration"),
        "clips_count": len(detail.get("clips") or []),
        "settings": detail.get("settings") or {},
        "notes": detail.get("notes") or {},
        "parent_job": detail.get("parent_job"),
        "thumbnail": detail.get("thumbnail"),
        "source_info": detail.get("source_info") or {},
        "created_at": detail.get("created_at"),
        "updated_at": detail.get("updated_at") or detail.get("created_at"),
    })


def save_clips(job_id: str, clips: list[dict[str, Any]]) -> None:
    """Persist clip rows for a job (idempotent)."""
    if not enabled() or not clips:
        return
    rows = []
    for i, c in enumerate(clips):
        rows.append({
            "job_id": job_id,
            "idx": int(c.get("index", i)),
            "title": c.get("title"),
            "reason": c.get("reason"),
            "start_time": c.get("start"),
            "end_time": c.get("end"),
            "duration": (float(c.get("end", 0)) - float(c.get("start", 0)))
            if c.get("start") is not None and c.get("end") is not None else None,
            "score": c.get("score"),
            "render_status": c.get("render_status"),
            "storage_path": c.get("storage_path"),
            "size_bytes": c.get("size_bytes"),
            "width": c.get("width"),
            "height": c.get("height"),
            "hook": (c.get("hooks") or [None])[int(c.get("hook_index", 0) or 0)]
            if c.get("hooks") else None,
            "hooks": c.get("hooks") or [],
            "focus": c.get("focus") or {},
            "note": c.get("note"),
            "error": c.get("error"),
            "encoder": c.get("encoder"),
        })
    _rest("noethara_clips", rows, conflict="job_id,idx")
