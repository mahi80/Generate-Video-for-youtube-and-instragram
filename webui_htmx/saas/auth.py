"""
Sign-in with Google (OpenID Connect), login tracking and access control.

* ``/login`` shows "Continue with Google" (and a developer email login when
  ``DEV_LOGIN=1``, for local testing without Google credentials).
* Every successful sign-in upserts the user and records a ``LoginEvent``.
* Admins are the emails listed in ``ADMIN_EMAILS``; when that list is empty
  the first account ever created becomes admin, so a fresh install is usable.
* ``AuthGate`` protects every route except the login flow and static files;
  Studio routes (the full editor, settings and API keys) are admin-only.
"""

import time
from urllib.parse import quote

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from loguru import logger
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware

from webui_htmx.saas import db
from webui_htmx.saas.settings import settings

SESSION_USER_KEY = "user"
# 会话里缓存的用户信息定期回数据库校验，停用账号或角色变更最多延迟这么久生效。
SESSION_REVALIDATE_SECONDS = 300
PUBLIC_PREFIXES = ("/login", "/auth/", "/static/", "/healthz", "/favicon")
# Studio（完整编辑器）会读写全局配置和平台 API Key，只对管理员开放。
ADMIN_PREFIXES = (
    "/studio", "/run", "/fragment/", "/dialog/", "/upload", "/media/", "/download/", "/admin",
)

router = APIRouter()
oauth = OAuth()
if settings.google_enabled:
    oauth.register(
        name="google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile", "prompt": "select_account"},
    )


def current_user(request: Request) -> dict | None:
    return getattr(request.state, "user", None)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def _session_payload(user: db.User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name or user.email.split("@")[0],
        "picture": user.picture_url or "",
        "role": user.role,
        "checked_at": time.time(),
    }


def sign_in(request: Request, *, email: str, name: str, picture: str, google_sub: str | None,
            method: str) -> dict:
    """Create or update the user, record the login and start the session."""
    email = email.strip().lower()
    with db.session_scope() as session:
        user = None
        if google_sub:
            user = session.scalar(select(db.User).where(db.User.google_sub == google_sub))
        if user is None:
            user = session.scalar(select(db.User).where(db.User.email == email))
        if user is None:
            has_admin = session.scalar(select(db.User.id).where(db.User.role == "admin").limit(1))
            # 列默认值只在 INSERT 时生效，这里显式赋值，后面的启用检查才可靠。
            user = db.User(email=email, role="user", is_active=True)
            if settings.is_admin_email(email) or (not settings.admin_emails and not has_admin):
                user.role = "admin"
            session.add(user)
            logger.info(f"new user signed up: {email} (role={user.role})")
        elif settings.is_admin_email(email) and user.role != "admin":
            user.role = "admin"
        if not user.is_active:
            raise PermissionError("This account has been disabled.")
        user.email = email
        user.name = name or user.name
        user.picture_url = picture or user.picture_url
        if google_sub:
            user.google_sub = google_sub
        user.last_login_at = db.utcnow()
        session.flush()
        session.add(db.LoginEvent(
            user_id=user.id,
            method=method,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent", "")[:500],
        ))
        payload = _session_payload(user)
    request.session.clear()
    request.session[SESSION_USER_KEY] = payload
    return payload


def _login_page(request: Request, error: str = "") -> HTMLResponse:
    from webui_htmx.saas.pages import render

    return render(request, "login.html", error=error,
                  google_enabled=settings.google_enabled, dev_login=settings.dev_login)


@router.get("/login", response_class=HTMLResponse)
async def login(request: Request, error: str = ""):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return _login_page(request, error)


@router.get("/auth/google")
async def google_start(request: Request):
    if not settings.google_enabled:
        return RedirectResponse("/login?error=" + quote("Google sign-in is not configured yet."), 303)
    redirect_uri = (
        f"{settings.public_base_url}/auth/google/callback"
        if settings.public_base_url
        else str(request.url_for("google_callback"))
    )
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/auth/google/callback", name="google_callback")
async def google_callback(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as exc:
        logger.warning(f"google sign-in failed: {exc.error}")
        return RedirectResponse("/login?error=" + quote("Google sign-in was cancelled or failed."), 303)
    info = token.get("userinfo") or {}
    if not info.get("email") or not info.get("email_verified", False):
        return RedirectResponse("/login?error=" + quote("Your Google email is not verified."), 303)
    try:
        sign_in(request, email=info["email"], name=info.get("name", ""),
                picture=info.get("picture", ""), google_sub=str(info.get("sub") or ""),
                method="google")
    except PermissionError as exc:
        return RedirectResponse("/login?error=" + quote(str(exc)), 303)
    return RedirectResponse("/", status_code=303)


@router.post("/auth/dev")
async def dev_login(request: Request):
    if not settings.dev_login:
        return Response(status_code=404)
    form = await request.form()
    email = str(form.get("email") or "").strip().lower()
    if "@" not in email or len(email) > 320:
        return _login_page(request, "Enter a valid email address.")
    try:
        sign_in(request, email=email, name=email.split("@")[0], picture="", google_sub=None,
                method="dev")
    except PermissionError as exc:
        return _login_page(request, str(exc))
    return RedirectResponse("/", status_code=303)


@router.api_route("/logout", methods=["GET", "POST"])
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def _refresh_session_user(request: Request) -> dict | None:
    payload = request.session.get(SESSION_USER_KEY)
    if not isinstance(payload, dict) or not payload.get("id"):
        return None
    if time.time() - float(payload.get("checked_at", 0)) < SESSION_REVALIDATE_SECONDS:
        return payload
    with db.session_scope() as session:
        user = session.get(db.User, payload["id"])
        if user is None or not user.is_active:
            request.session.clear()
            return None
        payload = _session_payload(user)
    request.session[SESSION_USER_KEY] = payload
    return payload


class AuthGate(BaseHTTPMiddleware):
    """Require a signed-in user; keep the Studio for admins."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not settings.enabled or path.startswith(PUBLIC_PREFIXES):
            request.state.user = None
            if settings.enabled and path.startswith(("/login", "/auth/")):
                request.state.user = _refresh_session_user(request)
            return await call_next(request)

        user = _refresh_session_user(request)
        request.state.user = user
        if user is None:
            if request.headers.get("hx-request"):
                return Response(status_code=200, headers={"HX-Redirect": "/login"})
            return RedirectResponse("/login", status_code=303)
        if path.startswith(ADMIN_PREFIXES) and user.get("role") != "admin":
            if request.headers.get("hx-request"):
                return Response(status_code=200, headers={"HX-Redirect": "/create"})
            return RedirectResponse("/create", status_code=303)
        return await call_next(request)
