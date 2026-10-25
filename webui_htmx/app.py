"""
FastAPI application serving the MoneyPrinterTurbo WebUI over HTMX.

Start it with ``python -m webui_htmx`` (or ``./webui-htmx.sh``). The page
logic is the same ``webui/Main.py`` the Streamlit WebUI uses; see
``runtime`` and ``stshim`` for how it runs without Streamlit.
"""

import os
import shutil
import sys
from urllib.parse import quote
from uuid import uuid4

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
# 与 webui/Main.py 相同：项目根目录必须优先于第三方依赖中的同名 app 包。
if ROOT_DIR in sys.path:
    sys.path.remove(ROOT_DIR)
sys.path.insert(0, ROOT_DIR)

# 页面在浏览器中运行。隐藏服务端桌面会话后，Main.py 会在页面内播放视频、
# 以路径提示代替在服务器上调用 xdg-open（WSLg 下会打开到另一个桌面）。
if not os.environ.get("MPT_HTMX_KEEP_DISPLAY"):
    os.environ.pop("DISPLAY", None)
    os.environ.pop("WAYLAND_DISPLAY", None)

from webui_htmx import stshim, tour_shim  # noqa: E402

sys.modules["streamlit"] = stshim
sys.modules["streamlit_tour"] = tour_shim

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402

from webui_htmx import render  # noqa: E402
from webui_htmx import runtime as rt  # noqa: E402

PACKAGE_DIR = os.path.dirname(os.path.realpath(__file__))
SESSION_HEADER = "x-mpt-session"
MORPH_SWAP = "morph:{ignoreActiveValue:true}"
# 服务重启后旧页面的会话已不存在，让浏览器整页刷新重新开始。
RELOAD_HEADERS = {"HX-Refresh": "true"}

templates = Environment(
    loader=FileSystemLoader(os.path.join(PACKAGE_DIR, "templates")),
    autoescape=select_autoescape(["html"]),
)

app = FastAPI(title="MoneyPrinterTurbo WebUI", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(PACKAGE_DIR, "static")), name="static")


def _bind_request(session: rt.Session, request: Request) -> rt.Session:
    session.headers = {key: value for key, value in request.headers.items() if key != "cookie"}
    accept_language = request.headers.get("accept-language", "")
    session.locale = accept_language.split(",")[0].split(";")[0].strip()
    return session


def _session(request: Request, sid: str | None = None) -> rt.Session | None:
    """Session of the page that sent the request (one per page load, like Streamlit)."""
    session = rt.find_session(sid or request.headers.get(SESSION_HEADER))
    return _bind_request(session, request) if session is not None else None


def _expired() -> Response:
    return Response(status_code=200, headers=RELOAD_HEADERS)


def _full_page(session: rt.Session) -> str:
    with session.lock:
        run = rt.full_run(session)
        return templates.get_template("page.html").render(
            title=run.page_title,
            sid=session.sid,
            app=render.app_html(run),
            toasts=render.toasts_html(run),
        )


def _full_app(session: rt.Session, form=None, extra_toasts=()) -> tuple[str, dict]:
    with session.lock:
        run = rt.full_run(session, form)
        toasts = list(extra_toasts) + run.toasts
        return render.app_html(run), render.toast_headers(toasts)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    session = _bind_request(rt.new_session(), request)
    body = await run_in_threadpool(_full_page, session)
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})


@app.post("/run", response_class=HTMLResponse)
async def run_page(request: Request):
    session = _session(request)
    if session is None:
        return _expired()
    form = await request.form()
    body, headers = await run_in_threadpool(_full_app, session, form)
    return HTMLResponse(body, headers=headers)


def _fragment(session: rt.Session, fid: str, form, poll: bool):
    if not session.lock.acquire(blocking=not poll):
        # 轮询期间页面正在执行其它操作（例如生成文案），跳过本次刷新即可。
        return None, None
    try:
        reg = session.fragments.get(fid)
        if reg is None:
            if poll:
                return None, None
            body, headers = _full_app(session, form)
            return body, {**headers, "HX-Retarget": "#app", "HX-Reswap": MORPH_SWAP}
        run = rt.fragment_run(session, reg, form)
        if run.escalate:
            body, headers = _full_app(session, None, extra_toasts=run.toasts)
            return body, {**headers, "HX-Retarget": "#app", "HX-Reswap": MORPH_SWAP}
        return render.fragment_html(run), render.toast_headers(run.toasts)
    finally:
        session.lock.release()


@app.post("/fragment/{fid}", response_class=HTMLResponse)
async def run_fragment(fid: str, request: Request):
    session = _session(request)
    if session is None:
        return _expired()
    form = await request.form()
    poll = form.get("_user") != "1"
    body, headers = await run_in_threadpool(_fragment, session, fid, form, poll)
    if body is None:
        return Response(status_code=204)
    return HTMLResponse(body, headers=headers)


def _dismiss_dialog(session: rt.Session, fid: str):
    with session.lock:
        reg = session.fragments.get(fid)
        toasts = []
        if reg is not None and reg.dialog and callable(reg.dialog.get("on_dismiss")):
            _, callback_run = rt.call_with_run(session, reg.dialog["on_dismiss"])
            toasts = callback_run.toasts
        return _full_app(session, None, extra_toasts=toasts)


@app.post("/dialog/{fid}/dismiss", response_class=HTMLResponse)
async def dismiss_dialog(fid: str, request: Request):
    session = _session(request)
    if session is None:
        return _expired()
    body, headers = await run_in_threadpool(_dismiss_dialog, session, fid)
    return HTMLResponse(body, headers=headers)


def _store_uploads(session: rt.Session, key: str, uploads) -> str | None:
    with session.lock:
        meta = session.widget_meta.get(key)
        if meta is None or meta.kind != "file_uploader":
            return "This upload field is no longer on the page. Refresh and try again."
        os.makedirs(session.upload_dir, exist_ok=True)
        records = []
        for upload in uploads:
            name = os.path.basename(upload.filename or "upload")
            extension = os.path.splitext(name)[1]
            allowed = {item.lower() for item in meta.upload_types or []}
            if allowed and extension.lower() not in allowed:
                return f"{name}: unsupported file type"
            file_id = uuid4().hex
            path = os.path.join(session.upload_dir, f"{file_id}{extension.lower()}")
            with open(path, "wb") as target:
                shutil.copyfileobj(upload.file, target, 1024 * 1024)
            size = os.path.getsize(path)
            if meta.upload_max_bytes and size > meta.upload_max_bytes:
                os.remove(path)
                return f"{name}: file is larger than {meta.upload_max_bytes // (1024 * 1024)} MB"
            records.append({
                "__upload__": True,
                "file_id": file_id,
                "name": name,
                "size": size,
                "type": upload.content_type or "",
                "path": path,
            })
        store = session.store_for(meta)
        existing = list(store.get(key) or []) if meta.upload_multiple else []
        store[key] = existing + (records if meta.upload_multiple else records[:1])
        return None


@app.post("/upload")
async def upload(request: Request):
    session = _session(request)
    if session is None:
        return _expired()
    form = await request.form()
    uploads = [item for item in form.getlist("files") if hasattr(item, "filename")]
    error = await run_in_threadpool(_store_uploads, session, str(form.get("key") or ""), uploads)
    if error:
        return Response(error, status_code=400, media_type="text/plain")
    return Response(status_code=204)


def _remove_upload(session: rt.Session, key: str, file_id: str):
    with session.lock:
        meta = session.widget_meta.get(key)
        if meta is None:
            return
        store = session.store_for(meta)
        records = list(store.get(key) or [])
        kept = [item for item in records if item.get("file_id") != file_id]
        for item in records:
            if item.get("file_id") == file_id:
                try:
                    os.remove(item["path"])
                except OSError:
                    pass
        if kept:
            store[key] = kept
        else:
            store.pop(key, None)


@app.post("/upload/remove")
async def remove_upload(request: Request):
    session = _session(request)
    if session is None:
        return _expired()
    form = await request.form()
    await run_in_threadpool(
        _remove_upload, session, str(form.get("key") or ""), str(form.get("file_id") or "")
    )
    return Response(status_code=204)


@app.get("/media/{sid}/{media_id}")
async def media(sid: str, media_id: str, request: Request):
    session = _session(request, sid)
    entry = session.media.get(media_id) if session is not None else None
    if entry is None:
        raise HTTPException(status_code=404)
    kind, payload, mime = entry
    if kind == "path":
        if not os.path.isfile(payload):
            raise HTTPException(status_code=404)
        return FileResponse(payload, media_type=mime or None)
    return Response(payload, media_type=mime or "application/octet-stream",
                    headers={"Cache-Control": "private, max-age=3600"})


@app.get("/download/{sid}/{download_id}")
async def download(sid: str, download_id: str, request: Request):
    session = _session(request, sid)
    entry = session.downloads.get(download_id) if session is not None else None
    if entry is None:
        raise HTTPException(status_code=404)
    kind, payload, file_name, mime = entry
    if kind == "path":
        if not os.path.isfile(payload):
            raise HTTPException(status_code=404)
        return FileResponse(payload, media_type=mime, filename=file_name)
    disposition = f"attachment; filename*=UTF-8''{quote(file_name)}"
    return Response(payload, media_type=mime, headers={"Content-Disposition": disposition})


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
