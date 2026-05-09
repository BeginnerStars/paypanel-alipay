from __future__ import annotations

import pyotp
from fastapi import HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .config import get_settings
from .db import settings_map

SESSION_COOKIE = "paypanel_session"


def serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="paypanel-auth")


def make_session(username: str) -> str:
    return serializer().dumps({"username": username})


def read_session(request: Request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        data = serializer().loads(token, max_age=get_settings().session_max_age)
    except BadSignature:
        return None
    username = data.get("username")
    return username if isinstance(username, str) else None


def require_login(request: Request) -> str:
    username = read_session(request)
    if not username:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return username


def verify_credentials(username: str, password: str, otp: str = "") -> bool:
    settings = get_settings()
    if username != settings.admin_username or password != settings.admin_password:
        return False
    panel_settings = settings_map()
    if panel_settings.get("enable_2fa") == "1":
        secret = panel_settings.get("totp_secret", "")
        if not secret:
            return False
        return pyotp.TOTP(secret).verify(otp, valid_window=1)
    return True
