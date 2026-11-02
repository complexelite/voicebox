"""Perfex one-time-code SSO and short-lived Voicebox browser sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import Request

SESSION_COOKIE = "ce_voicebox_session"
STATE_COOKIE = "ce_voicebox_sso_state"
SESSION_SECONDS = 8 * 60 * 60
STATE_SECONDS = 5 * 60


def _secret() -> bytes:
    # Derive a distinct signing key. The parent API key remains server-side and
    # is never sent to the browser.
    value = os.environ.get("VOICEBOX_API_KEY", "").strip()
    if not value:
        return b""
    return hmac.new(value.encode("utf-8"), b"voicebox-perfex-session-v1", hashlib.sha256).digest()


def configured() -> bool:
    return len(_secret()) >= 32


def login_url(state: str) -> tuple[str, str, str]:
    base = os.environ.get("VOICEBOX_PERFEX_LOGIN_URL", "https://clients.complexelite.com/studio/login").strip()
    verifier = secrets.token_urlsafe(48)
    challenge = _b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge, f"{base}?{urlencode({'app': 'voicebox', 'state': state, 'challenge': challenge})}"


def begin_login() -> tuple[str, str, str, str]:
    state = secrets.token_hex(32)
    verifier, challenge, destination = login_url(state)
    return state, verifier, challenge, destination


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue_session(user: dict) -> str:
    secret = _secret()
    if len(secret) < 32:
        raise RuntimeError("Perfex SSO is not configured on Voicebox.")
    now = int(time.time())
    payload = {
        "sub": str(user["id"]),
        "name": str(user.get("name", ""))[:160],
        "email": str(user.get("email", ""))[:254],
        "role": str(user.get("role", "staff"))[:24],
        "iat": now,
        "exp": now + SESSION_SECONDS,
    }
    encoded = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_b64encode(signature)}"


def session_user(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token or "." not in token:
        return None
    encoded, supplied = token.rsplit(".", 1)
    secret = _secret()
    if len(secret) < 32:
        return None
    expected = _b64encode(hmac.new(secret, encoded.encode("ascii", "ignore"), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, supplied):
        return None
    try:
        payload = json.loads(_b64decode(encoded))
        if not isinstance(payload, dict) or int(payload.get("exp", 0)) <= int(time.time()):
            return None
        if not str(payload.get("sub", "")).isdigit() or payload.get("role") not in {"staff", "admin"}:
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


async def exchange_code(code: str, verifier: str) -> dict:
    endpoint = os.environ.get(
        "VOICEBOX_PERFEX_EXCHANGE_URL",
        "https://clients.complexelite.com/studio/sso/voicebox-exchange",
    ).strip()
    async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=4.0), follow_redirects=False) as client:
        response = await client.post(
            endpoint,
            data={"code": code, "verifier": verifier},
            headers={"Accept": "application/json"},
        )
    if response.status_code != 200:
        raise ValueError("Perfex sign-in link is invalid, expired, or already used. Start again from Perfex.")
    try:
        body = response.json()
        user = body["user"]
        if not body.get("ok") or not isinstance(user, dict):
            raise ValueError
        if not str(user.get("id", "")).isdigit() or user.get("role") not in {"staff", "admin"}:
            raise ValueError
        if not user.get("email"):
            raise ValueError
        return user
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("Perfex returned an invalid sign-in response.") from exc
