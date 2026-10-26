"""Routes for the guided wizard, "My videos" and the admin dashboard."""

import os
import shutil
from datetime import timedelta

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from loguru import logger
from sqlalchemy import func, select

from app.utils import utils
from webui_htmx.saas import db, jobs
from webui_htmx.saas.auth import current_user
from webui_htmx.saas.settings import settings

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "templates", "saas")
templates = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=select_autoescape(["html"]))

router = APIRouter()


def render(request: Request, template: str, status_code: int = 200, **context) -> HTMLResponse:
    page = templates.get_template(template).render(
        user=current_user(request), path=request.url.path, saas=settings, **context
    )
    return HTMLResponse(page, status_code=status_code)


def _job_view(job: db.VideoJob) -> dict:
    icon, message = jobs.stage(job)
    return {
        "id": job.id,
        "topic": job.topic,
        "style": jobs.STYLES.get(job.style, {}).get("label", job.style),
        "status": job.status,
        "progress": job.progress,
        "icon": icon,
        "message": message,
        "aspect": job.aspect,
        "created_at": job.created_at,
        "duration": job.duration_seconds,
        "done": job.status in jobs.TERMINAL,
    }


# -----------------------------------------------------------------------------
# 引导式创建
# -----------------------------------------------------------------------------


@router.get("/create", response_class=HTMLResponse)
def create_page(request: Request):
    return render(
        request, "create.html",
        languages=jobs.LANGUAGES, lengths=jobs.LENGTHS, styles=jobs.STYLES,
        voices=jobs.VOICES, formats=jobs.FORMATS, examples=jobs.EXAMPLE_TOPICS,
    )


@router.post("/create/script", response_class=HTMLResponse)
async def create_script(request: Request):
    form = await request.form()
    choices = jobs.Choices.from_form(form)
    if form.get("own") == "1":
        return render(request, "partials/script.html", script="", terms="", seconds=0)
    if len(choices.topic) < 3:
        return render(request, "partials/script.html", error="First, tell us what your video is about.")
    from starlette.concurrency import run_in_threadpool

    try:
        script, terms = await run_in_threadpool(jobs.write_script, choices)
    except Exception as exc:  # noqa: BLE001 - shown as a friendly message
        logger.warning(f"wizard script generation failed: {exc}")
        return render(request, "partials/script.html", error=str(exc) if isinstance(exc, RuntimeError)
                      else "The AI could not write a script right now. Please try again.")
    rate = jobs.STYLES[choices.style]["params"].get("voice_rate", 1.0)
    return render(request, "partials/script.html", script=script, terms=", ".join(terms),
                  seconds=jobs.estimate_seconds(script, rate))


@router.post("/create/estimate", response_class=HTMLResponse)
async def create_estimate(request: Request):
    form = await request.form()
    choices = jobs.Choices.from_form(form)
    rate = jobs.STYLES[choices.style]["params"].get("voice_rate", 1.0)
    seconds = jobs.estimate_seconds(str(form.get("script") or ""), rate)
    return HTMLResponse(f"About {seconds} seconds")


@router.get("/create/voice/{voice_key}")
def create_voice_sample(voice_key: str):
    if voice_key not in jobs.VOICES:
        return Response(status_code=404)
    try:
        path = jobs.voice_sample(voice_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"voice sample failed: {exc}")
        return Response(status_code=503)
    return FileResponse(path, media_type="audio/mpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


@router.post("/create/start", response_class=HTMLResponse)
async def create_start(request: Request):
    form = await request.form()
    choices = jobs.Choices.from_form(form)
    script = str(form.get("script") or "").strip()[:6000]
    terms = [term.strip() for term in str(form.get("terms") or "").split(",") if term.strip()][:10]
    if len(choices.topic) < 3 or len(script) < 20:
        return render(request, "partials/start_error.html",
                      error="Write or generate a script first (step 3).")
    user = current_user(request)
    from starlette.concurrency import run_in_threadpool

    try:
        task_id = await run_in_threadpool(jobs.submit, user["id"], choices, script, terms)
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"wizard submit failed: {exc}")
        return render(request, "partials/start_error.html",
                      error="We couldn't start your video. Please try again.")
    with db.session_scope() as session:
        job = session.get(db.VideoJob, task_id)
        view = _job_view(job)
    return render(request, "partials/job_card.html", job=view, big=True)


# -----------------------------------------------------------------------------
# 我的视频
# -----------------------------------------------------------------------------


@router.get("/videos", response_class=HTMLResponse)
def videos_page(request: Request):
    user = current_user(request)
    with db.session_scope() as session:
        views = [_job_view(job) for job in jobs.list_jobs(session, user)]
    return render(request, "videos.html", jobs=views)


@router.get("/videos/{job_id}/card", response_class=HTMLResponse)
def video_card(request: Request, job_id: str, big: int = 0):
    with db.session_scope() as session:
        job = jobs.user_job(session, job_id, current_user(request))
        if job is None:
            return Response(status_code=404)
        view = _job_view(jobs.refresh(job))
    return render(request, "partials/job_card.html", job=view, big=bool(big))


def _job_file(request: Request, job_id: str) -> tuple[str, str] | None:
    with db.session_scope() as session:
        job = jobs.user_job(session, job_id, current_user(request))
        if job is None:
            return None
        jobs.refresh(job)
        path = job.video_file
        topic = job.topic
    tasks_root = os.path.realpath(utils.task_dir())
    if not path or not os.path.isfile(path) or not os.path.realpath(path).startswith(tasks_root + os.sep):
        return None
    return path, topic


@router.api_route("/videos/{job_id}/file", methods=["GET", "HEAD"])
def video_file(request: Request, job_id: str):
    found = _job_file(request, job_id)
    if not found:
        return Response(status_code=404)
    return FileResponse(found[0], media_type="video/mp4")


@router.api_route("/videos/{job_id}/download", methods=["GET", "HEAD"])
def video_download(request: Request, job_id: str):
    found = _job_file(request, job_id)
    if not found:
        return Response(status_code=404)
    return FileResponse(found[0], media_type="video/mp4", filename=jobs.download_name(found[1]))


@router.post("/videos/{job_id}/delete")
def video_delete(request: Request, job_id: str):
    with db.session_scope() as session:
        job = jobs.user_job(session, job_id, current_user(request))
        if job is None:
            return Response(status_code=404)
        jobs.refresh(job)
        if job.status not in jobs.TERMINAL:
            return Response("This video is still being made.", status_code=409)
        task_path = os.path.realpath(utils.task_dir(job.id))
        tasks_root = os.path.realpath(utils.task_dir())
        if task_path.startswith(tasks_root + os.sep) and os.path.isdir(task_path):
            shutil.rmtree(task_path, ignore_errors=True)
        session.delete(job)
    return HTMLResponse("")


# -----------------------------------------------------------------------------
# 管理后台
# -----------------------------------------------------------------------------


@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    week_ago = db.utcnow() - timedelta(days=7)
    day_ago = db.utcnow() - timedelta(days=1)
    with db.session_scope() as session:
        video_counts = dict(session.execute(
            select(db.VideoJob.user_id, func.count()).group_by(db.VideoJob.user_id)
        ).all())
        stats = {
            "users": db.count(session, select(db.User)),
            "new_users_7d": db.count(session, select(db.User).where(db.User.created_at >= week_ago)),
            "logins_24h": db.count(session, select(db.LoginEvent).where(db.LoginEvent.created_at >= day_ago)),
            "videos_24h": db.count(session, select(db.VideoJob).where(db.VideoJob.created_at >= day_ago)),
            "videos_total": db.count(session, select(db.VideoJob)),
            "failed_total": db.count(session, select(db.VideoJob).where(db.VideoJob.status == "failed")),
        }
        users = session.scalars(select(db.User).order_by(db.User.created_at.desc()).limit(200)).all()
        logins = session.execute(
            select(db.LoginEvent, db.User.email).join(db.User)
            .order_by(db.LoginEvent.created_at.desc()).limit(50)
        ).all()
        recent_jobs = session.execute(
            select(db.VideoJob, db.User.email).join(db.User)
            .order_by(db.VideoJob.created_at.desc()).limit(50)
        ).all()
        for job, _ in recent_jobs:
            jobs.refresh(job)
        context = {
            "stats": stats,
            "users": [
                {"id": u.id, "email": u.email, "name": u.name, "role": u.role, "active": u.is_active,
                 "created_at": u.created_at, "last_login_at": u.last_login_at,
                 "videos": video_counts.get(u.id, 0)}
                for u in users
            ],
            "logins": [
                {"email": email, "method": event.method, "ip": event.ip_address,
                 "agent": event.user_agent[:80], "at": event.created_at}
                for event, email in logins
            ],
            "jobs": [
                {"id": job.id, "email": email, "topic": job.topic, "status": job.status,
                 "style": job.style, "at": job.created_at, "error": job.error}
                for job, email in recent_jobs
            ],
        }
    return render(request, "admin.html", **context)


@router.post("/admin/users/{user_id}/{action}")
def admin_user_action(request: Request, user_id: str, action: str):
    me = current_user(request)
    if user_id == me["id"]:
        return RedirectResponse("/admin", status_code=303)
    with db.session_scope() as session:
        user = session.get(db.User, user_id)
        if user is not None:
            if action == "make-admin":
                user.role = "admin"
            elif action == "make-user":
                user.role = "user"
            elif action == "disable":
                user.is_active = False
            elif action == "enable":
                user.is_active = True
            logger.info(f"admin {me['email']} applied {action} to {user.email}")
    return RedirectResponse("/admin", status_code=303)
