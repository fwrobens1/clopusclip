"""Supabase Auth verification for the NoetharaClipz backend."""
from __future__ import annotations

import os
from typing import Any

import requests

URL = os.getenv("SUPABASE_URL", "").rstrip("/")
ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")


def configured() -> bool:
    return bool(URL and ANON_KEY)


def signup(email: str, password: str) -> dict[str, Any]:
    """Create a confirmed user through the service-role admin API.

    Supabase projects can disable public signups. The product still needs a
    working create-account flow, so the backend performs the admin create call
    and immediately lets the client exchange the password for a normal session.
    """
    if not URL or not SERVICE_KEY:
        return {"ok": False, "error": "Account creation is not configured."}
    try:
        r = requests.post(
            f"{URL}/auth/v1/admin/users",
            headers={
                "apikey": SERVICE_KEY,
                "Authorization": f"Bearer {SERVICE_KEY}",
                "Content-Type": "application/json",
            },
            json={"email": email, "password": password, "email_confirm": True},
            timeout=20,
        )
    except requests.RequestException:
        return {"ok": False, "error": "Could not reach the auth service. Try again."}
    if r.status_code not in (200, 201):
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        msg = data.get("msg") or data.get("message") or data.get("error_description") or "Could not create account."
        return {"ok": False, "error": str(msg)}
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    return {"ok": True, "user": {"id": data.get("id"), "email": data.get("email") or email}}


def public_config() -> dict[str, Any]:
    return {
        "enabled": configured(),
        "supabaseUrl": URL,
        "anonKey": ANON_KEY,
        "appPath": "/app",
    }


def verify(access_token: str) -> dict[str, Any] | None:
    """Return the Supabase user for a valid access token, else None."""
    if not configured() or not access_token:
        return None
    try:
        r = requests.get(
            f"{URL}/auth/v1/user",
            headers={"apikey": ANON_KEY, "Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    data = r.json()
    return data if isinstance(data, dict) and data.get("id") else None
