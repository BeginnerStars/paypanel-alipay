from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
from urllib.parse import quote

from .config import get_settings
from .db import settings_map

SESSION_COOKIE = "paypanel_session"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _signature(payload: str) -> str:
    return _b64(hmac.new(get_settings().secret_key.encode(), payload.encode(), hashlib.sha256).digest())


def make_session(username: str) -> str:
    payload = _b64(json.dumps({"username": username, "iat": int(time.time())}, separators=(",", ":")).encode())
    return f"{payload}.{_signature(payload)}"


def read_session_cookie(cookie_header: str | None) -> str | None:
    if not cookie_header:
        return None
    cookies = {}
    for item in cookie_header.split(";"):
        if "=" in item:
            key, value = item.strip().split("=", 1)
            cookies[key] = value
    token = cookies.get(SESSION_COOKIE)
    if not token or "." not in token:
        return None
    payload, supplied = token.rsplit(".", 1)
    if not hmac.compare_digest(_signature(payload), supplied):
        return None
    try:
        data = json.loads(_unb64(payload))
    except Exception:
        return None
    if int(time.time()) - int(data.get("iat", 0)) > get_settings().session_max_age:
        return None
    username = data.get("username")
    return username if isinstance(username, str) else None


def random_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_code(secret: str, for_time: int | None = None) -> str:
    if for_time is None:
        for_time = int(time.time())
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    counter = struct.pack(">Q", int(for_time / 30))
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{code % 1_000_000:06d}"


def verify_totp(secret: str, otp: str) -> bool:
    otp = "".join(ch for ch in otp if ch.isdigit())
    if len(otp) != 6:
        return False
    now = int(time.time())
    return any(hmac.compare_digest(totp_code(secret, now + drift * 30), otp) for drift in (-1, 0, 1))


def provisioning_uri(secret: str, username: str, issuer: str = "PayPanel Alipay") -> str:
    label = quote(f"{issuer}:{username}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"


def verify_credentials(username: str, password: str, otp: str = "") -> bool:
    settings = get_settings()
    if not hmac.compare_digest(username, settings.admin_username):
        return False
    if not hmac.compare_digest(password, settings.admin_password):
        return False
    panel_settings = settings_map()
    if panel_settings.get("enable_2fa") == "1":
        secret = panel_settings.get("totp_secret", "")
        return bool(secret) and verify_totp(secret, otp)
    return True
