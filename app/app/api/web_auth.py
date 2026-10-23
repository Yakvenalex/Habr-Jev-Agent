import hmac
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

COOKIE_NAME = "jev_session"
router = APIRouter(prefix="/auth", include_in_schema=False)


def current_user(request: Request) -> str | None:
    settings = request.app.state.settings
    secret = settings.web_session_secret.get_secret_value()
    token = request.cookies.get(COOKIE_NAME)
    if not token or not secret or not settings.web_password.get_secret_value():
        return None
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            audience="jev-web",
            options={"require": ["exp", "iat", "sub"]},
        )
        if payload["sub"] == settings.web_username:
            return settings.web_username
    except jwt.InvalidTokenError:
        pass
    return None


def require_same_origin(request: Request):
    # SameSite protects ambient cookies; check Origin for browser mutations too.
    origin = request.headers.get("origin")
    expected = str(request.base_url).rstrip("/")
    if origin and origin != expected:
        raise HTTPException(403, "Cross-origin request rejected")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Cross-site request rejected")


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=500)


@router.post("/login")
async def login(data: LoginInput, request: Request, response: Response):
    require_same_origin(request)
    settings = request.app.state.settings
    username_ok = hmac.compare_digest(data.username.encode(), settings.web_username.encode())
    password_ok = hmac.compare_digest(
        data.password.encode(), settings.web_password.get_secret_value().encode()
    )
    if not settings.web_password.get_secret_value() or not username_ok or not password_ok:
        raise HTTPException(401, "Неверный логин или пароль")
    now = datetime.now(UTC)
    duration = timedelta(hours=settings.web_session_hours)
    token = jwt.encode(
        {"sub": settings.web_username, "aud": "jev-web", "iat": now, "exp": now + duration},
        settings.web_session_secret.get_secret_value(),
        algorithm="HS256",
    )
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(duration.total_seconds()),
        httponly=True,
        secure=settings.web_cookie_secure or request.url.scheme == "https",
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return {"username": settings.web_username}


@router.post("/logout")
async def logout(request: Request, response: Response):
    require_same_origin(request)
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    return {"status": "logged_out"}
