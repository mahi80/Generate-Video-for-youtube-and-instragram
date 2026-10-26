"""PostgreSQL models for users, logins and video jobs (SQLAlchemy 2)."""

from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    func,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from webui_htmx.saas.settings import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    picture_url: Mapped[str] = mapped_column(Text, default="")
    # Google 账号的稳定标识（OpenID Connect 的 sub）；开发登录时为空。
    google_sub: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    role: Mapped[str] = mapped_column(String(20), default="user")
    # 订阅套餐，取值见 webui_htmx/saas/plans.py。
    plan: Mapped[str] = mapped_column(String(20), default="free", server_default="free")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    logins: Mapped[list["LoginEvent"]] = relationship(back_populates="user")
    jobs: Mapped[list["VideoJob"]] = relationship(back_populates="user")

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class LoginEvent(Base):
    __tablename__ = "login_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    method: Mapped[str] = mapped_column(String(20))
    ip_address: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    user: Mapped[User] = relationship(back_populates="logins")


class VideoJob(Base):
    __tablename__ = "video_jobs"

    # 与生成任务的 task_id 相同，便于在任务目录和状态存储中直接定位。
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    topic: Mapped[str] = mapped_column(Text, default="")
    style: Mapped[str] = mapped_column(String(40), default="")
    language: Mapped[str] = mapped_column(String(20), default="")
    aspect: Mapped[str] = mapped_column(String(10), default="9:16")
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    video_file: Mapped[str] = mapped_column(Text, default="")
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="jobs")


engine = (
    create_engine(settings.database_url, pool_pre_ping=True, pool_size=5, max_overflow=5)
    if settings.enabled
    else None
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False) if engine else None


@contextmanager
def session_scope():
    """Transactional scope: commit on success, roll back on error."""
    if SessionLocal is None:
        raise RuntimeError("SaaS mode is disabled (DATABASE_URL is not set)")
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def count(session, statement) -> int:
    return int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
