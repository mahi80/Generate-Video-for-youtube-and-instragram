"""
Subscription plans and monthly video quotas.

The value metric is **finished videos per month**, not credits: a creator
knows what "30 Shorts" means, and a stock-footage Short costs us roughly
$0.03–0.05 (render ≈ $0.02, Azure neural voice ≈ $0.012, hosted LLM < $0.005),
so the prices below keep a healthy margin while undercutting credit-based
AI clip generators by a wide margin.

Change prices and limits here; the pricing page, quota checks and the admin
dashboard all read from this table. Failed videos never count against quota.
"""

from datetime import datetime, timezone

from sqlalchemy import func, select

from webui_htmx.saas import db

DEFAULT_PLAN = "free"

PLANS = {
    "free": {
        "name": "Free",
        "tagline": "Try it out",
        "monthly": 0,
        "yearly": 0,
        "videos": 3,
        "features": [
            "3 finished videos per month",
            "Footage matched to every sentence",
            "All styles, voices and 12 languages",
            "1080p downloads, no watermark",
        ],
    },
    "creator": {
        "name": "Creator",
        "tagline": "For one channel",
        "monthly": 9,
        "yearly": 7,
        "videos": 30,
        "features": [
            "30 finished videos per month",
            "Everything in Free",
            "Email support",
        ],
    },
    "pro": {
        "name": "Pro",
        "tagline": "Post every day",
        "monthly": 19,
        "yearly": 15,
        "videos": 100,
        "popular": True,
        "features": [
            "100 finished videos per month",
            "Everything in Creator",
            "Priority email support",
        ],
    },
    "agency": {
        "name": "Agency",
        "tagline": "For teams and clients",
        "monthly": 49,
        "yearly": 39,
        "videos": 300,
        "features": [
            "300 finished videos per month",
            "Everything in Pro",
            "Onboarding call",
            "Volume pricing above 300 videos",
        ],
    },
}


def plan_of(user: dict | None) -> dict:
    key = (user or {}).get("plan") or DEFAULT_PLAN
    return {"id": key, **PLANS.get(key, PLANS[DEFAULT_PLAN])}


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def videos_used(session, user_id: str) -> int:
    """Videos this calendar month that did not fail."""
    return int(session.scalar(
        select(func.count()).select_from(db.VideoJob).where(
            db.VideoJob.user_id == user_id,
            db.VideoJob.created_at >= month_start(),
            db.VideoJob.status != "failed",
        )
    ) or 0)


def usage(session, user: dict) -> dict:
    # 以数据库为准：管理员调整套餐后立即生效，不必等会话缓存刷新。
    record = session.get(db.User, user["id"])
    plan = plan_of({"plan": record.plan} if record else user)
    used = videos_used(session, user["id"])
    limit = plan["videos"]
    # 管理员不受套餐额度限制，便于测试和代客操作。
    unlimited = user.get("role") == "admin"
    return {
        "plan": plan,
        "used": used,
        "limit": limit,
        "left": max(0, limit - used),
        "unlimited": unlimited,
        "exhausted": not unlimited and used >= limit,
        "percent": min(100, round(100 * used / limit)) if limit else 100,
    }
