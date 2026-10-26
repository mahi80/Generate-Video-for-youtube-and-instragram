"""
SaaS settings, read from environment variables.

The HTMX WebUI runs in one of two modes:

* **Local mode** (``DATABASE_URL`` unset): a single-user tool with no login,
  exactly as before.
* **SaaS mode** (``DATABASE_URL`` set): users sign in with Google, every login
  and video is recorded in PostgreSQL, regular users get the guided wizard and
  admins also get the Studio, Settings and the admin dashboard.

All values come from the environment so the same image runs locally, in
Docker and on Azure Container Apps (where secrets are injected as env vars).
"""

import os
import secrets
from dataclasses import dataclass, field

from loguru import logger


def _csv(value: str) -> list[str]:
    return [item.strip().lower() for item in value.split(",") if item.strip()]


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SaasSettings:
    database_url: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    session_secret: str = ""
    admin_emails: list[str] = field(default_factory=list)
    # 对外访问地址，用于生成 Google OAuth 回调地址（反向代理后必须显式设置）。
    public_base_url: str = ""
    # 仅用于本机开发：无需 Google 凭据即可用邮箱登录。生产环境禁止开启。
    dev_login: bool = False
    # 部署在 HTTPS 后时启用安全 Cookie。
    secure_cookies: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.database_url)

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    def is_admin_email(self, email: str) -> bool:
        return (email or "").strip().lower() in self.admin_emails


def _normalize_database_url(url: str) -> str:
    """Accept postgres:// and postgresql:// URLs and use the psycopg 3 driver."""
    url = url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def load_settings() -> SaasSettings:
    database_url = _normalize_database_url(os.environ.get("DATABASE_URL", ""))
    session_secret = os.environ.get("SESSION_SECRET", "").strip()
    if database_url and not session_secret:
        # 未配置时每次启动生成随机密钥：所有用户在重启后需要重新登录。
        session_secret = secrets.token_urlsafe(48)
        logger.warning("SESSION_SECRET is not set; sessions will reset when the server restarts")
    return SaasSettings(
        database_url=database_url,
        google_client_id=os.environ.get("GOOGLE_CLIENT_ID", "").strip(),
        google_client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", "").strip(),
        session_secret=session_secret,
        admin_emails=_csv(os.environ.get("ADMIN_EMAILS", "")),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/"),
        dev_login=_flag("DEV_LOGIN"),
        secure_cookies=_flag("SECURE_COOKIES"),
    )


settings = load_settings()
