"""Supabase cloud storage + metadata bridge for NoetharaClipz.

All credentials come from .env via the service_role key. Nothing secret is
exposed to the browser: clips remain behind /api/clips, which redirects to a
short-lived signed Storage URL.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
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


TABLES = (
    "noethara_jobs",
    "noethara_clips",
    "noethara_live_sessions",
    "noethara_publications",
    "noethara_tool_calls",
)


def status() -> dict[str, Any]:
    return {
        "enabled": enabled(),
        "url": URL,
        "bucket": BUCKET,
        "signed_ttl": SIGNED_TTL,
        "tables": TABLES,
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


def list_rows(table: str, *, filters: dict[str, Any] | None = None,
              limit: int = 200, order: str | None = None) -> list[dict[str, Any]]:
    """Read rows through PostgREST using the server-side key."""
    if not enabled():
        return []
    params: dict[str, Any] = {"select": "*", "limit": int(limit)}
    for key, value in (filters or {}).items():
        params[key] = f"eq.{value}"
    if order:
        params["order"] = order
    r = requests.get(
        f"{URL}/rest/v1/{table}",
        headers=_headers({"Accept": "application/json"}),
        params=params,
        timeout=30,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Supabase {table} read failed ({r.status_code}): {r.text[:300]}")
    data = r.json()
    return data if isinstance(data, list) else []


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
            "score_reason": c.get("score_reason"),
            "hook_title": c.get("hook_title") or (c.get("hooks") or [None])[int(c.get("hook_index", 0) or 0)],
            "transcript_excerpt": c.get("transcript_excerpt"),
            "tracks": c.get("tracks") or [],
            "config": c.get("config") or {},
            "source_start_seconds": c.get("source_start_seconds")
            if c.get("source_start_seconds") is not None else c.get("start"),
            "source_end_seconds": c.get("source_end_seconds")
            if c.get("source_end_seconds") is not None else c.get("end"),
        })
    _rest("noethara_clips", rows, conflict="job_id,idx")


LIVE_COLUMNS = {
    "id", "job_id", "provider", "src_url", "state", "phase", "pass_count",
    "clipped_from_seconds", "clipped_until_seconds", "next_pass_at", "stop_reason",
    "target_clips", "max_clips", "max_duration_seconds", "first_pass_seconds",
    "pass_window_seconds", "curation_prompt", "settings", "last_error",
    "recording_path", "started_at_epoch", "stop_requested", "created_at", "updated_at",
}

PUBLICATION_COLUMNS = {
    "uid", "job_id", "clip_idx", "platform", "social_account", "caption", "hashtags",
    "scheduled_at", "status", "approved", "post_id", "error", "created_at", "updated_at",
}


def _columns(row: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k in allowed}


def save_live_session(session: dict[str, Any]) -> None:
    """Persist one live clipping session row."""
    row = _columns(session, LIVE_COLUMNS)
    due = row.get("next_pass_at")
    if isinstance(due, (int, float)):
        row["next_pass_at"] = datetime.fromtimestamp(due, timezone.utc).isoformat().replace("+00:00", "Z")
    _rest("noethara_live_sessions", row, conflict="id")


def load_live_sessions() -> list[dict[str, Any]]:
    return list_rows("noethara_live_sessions", limit=100, order="created_at.desc")


def save_publication(item: dict[str, Any]) -> None:
    """Persist a scheduled or completed publication."""
    row = dict(item)
    row["uid"] = row.pop("id", None)
    _rest("noethara_publications", _columns(row, PUBLICATION_COLUMNS), conflict="uid")


def load_publications() -> list[dict[str, Any]]:
    return list_rows("noethara_publications", limit=200, order="scheduled_at.asc.nullslast")


def delete_publication(uid: str) -> None:
    if not enabled():
        return
    r = requests.delete(
        f"{URL}/rest/v1/noethara_publications",
        headers=_headers(),
        params={"uid": f"eq.{uid}"},
        timeout=30,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Supabase publication delete failed ({r.status_code}): {r.text[:300]}")


def record_tool_call(tool: str, *, args: dict[str, Any] | None = None,
                     ok: bool = True, error: str | None = None,
                     user_email: str | None = None) -> None:
    if not enabled():
        return
    try:
        _rest("noethara_tool_calls", {
            "tool": tool,
            "args": args or {},
            "ok": bool(ok),
            "error": (error or "")[:500] or None,
            "user_email": user_email,
        })
    except Exception:
        pass
