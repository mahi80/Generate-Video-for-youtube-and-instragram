"""
Server-side sessions and script runs for the HTMX WebUI.

The HTMX WebUI executes the unchanged Streamlit page (``webui/Main.py``) on
top of a small Streamlit-compatible layer (``stshim``). Streamlit normally
keeps per-browser state on its server and pushes UI deltas over a WebSocket;
this module keeps the same state (session_state, widget metadata, uploaded
files, registered fragments) and turns every interaction into an ordinary
HTTP request whose response is server-rendered HTML that htmx morphs into
the page.

A *run* mirrors one Streamlit script execution:

1. apply the values the browser posted for each widget to session_state;
2. call ``on_change`` / ``on_click`` callbacks, exactly like Streamlit does
   before a rerun;
3. execute ``webui/Main.py`` (or a single fragment), collecting a node tree;
4. drop the state of widgets that were not rendered (Streamlit's widget
   cleanup), then render the tree to HTML.
"""

import contextvars
import hashlib
import os
import shutil
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4

from loguru import logger

from app.utils import utils

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
MAIN_SCRIPT_PATH = os.path.join(ROOT_DIR, "webui", "Main.py")

# 每次打开页面都是独立会话（与 Streamlit 一致）。页面打开期间任务管理器每两秒
# 轮询一次，因此超过该时长没有请求的会话可以视为标签页已关闭。
SESSION_IDLE_SECONDS = 2 * 60 * 60
# 试听音频、预览视频等媒体只在页面展示期间需要，保留有限条目即可。
MAX_SESSION_MEDIA = 40
MAX_SESSION_DOWNLOADS = 40
# 防止异常页面逻辑无限 st.rerun()。Streamlit 本身不限制，但这里会阻塞请求线程。
MAX_RERUNS_PER_REQUEST = 20

_MISSING = object()


class RerunException(Exception):
    """Raised by ``st.rerun()``; the run restarts with the current state."""

    def __init__(self, scope: str = "app"):
        super().__init__(scope)
        self.scope = scope


class StopException(Exception):
    """Raised by ``st.stop()``; the run ends and renders what it has."""


class SessionState(dict):
    """``st.session_state``: a dict that also allows attribute access."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def __setattr__(self, key, value):
        self[key] = value

    def __delattr__(self, key):
        try:
            del self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc


@dataclass
class Node:
    kind: str
    props: dict = field(default_factory=dict)
    children: list = field(default_factory=list)


@dataclass
class WidgetMeta:
    """What the next request needs to turn posted strings back into values."""

    kind: str
    user_key: bool
    form_id: str
    options: list | None = None
    labels: list | None = None
    allow_none: bool = False
    accept_new: bool = False
    num_type: type | None = None
    min_value: float | None = None
    max_value: float | None = None
    max_chars: int | None = None
    on_change: Callable | None = None
    on_click: Callable | None = None
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    upload_types: list | None = None
    upload_multiple: bool = False
    upload_max_bytes: int | None = None


@dataclass
class FragmentReg:
    fid: str
    func: Callable
    args: tuple
    kwargs: dict
    run_every: float | None
    form_id: str
    dialog: dict | None = None


class Session:
    def __init__(self, sid: str):
        self.sid = sid
        self.lock = threading.RLock()
        self.state = SessionState()
        # 未指定 key 的控件值不暴露在 session_state 中，与 Streamlit 一致。
        self.anon: dict[str, Any] = {}
        self.widget_meta: dict[str, WidgetMeta] = {}
        self.fragments: dict[str, FragmentReg] = {}
        self.media: OrderedDict[str, tuple] = OrderedDict()
        self.downloads: OrderedDict[str, tuple] = OrderedDict()
        self.locale = ""
        self.headers: dict[str, str] = {}
        self.last_seen = time.time()
        self.upload_dir = os.path.join(
            utils.storage_dir("temp", create=True), "webui-htmx", sid
        )

    def store_for(self, meta: WidgetMeta) -> dict:
        return self.state if meta.user_key else self.anon

    def register_media(self, kind: str, payload, mime: str) -> str:
        if kind == "path":
            digest = hashlib.sha1(
                f"{payload}:{os.path.getmtime(payload) if os.path.exists(payload) else 0}".encode()
            ).hexdigest()
        else:
            digest = hashlib.sha1(bytes(payload)).hexdigest()
        self.media[digest] = (kind, payload, mime)
        self.media.move_to_end(digest)
        while len(self.media) > MAX_SESSION_MEDIA:
            self.media.popitem(last=False)
        return digest

    def register_download(self, kind: str, payload, file_name: str, mime: str) -> str:
        if kind == "path":
            seed = f"{payload}:{file_name}"
        else:
            seed = hashlib.sha1(bytes(payload)).hexdigest() + file_name
        digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()
        self.downloads[digest] = (kind, payload, file_name, mime)
        self.downloads.move_to_end(digest)
        while len(self.downloads) > MAX_SESSION_DOWNLOADS:
            self.downloads.popitem(last=False)
        return digest

    def referenced_upload_paths(self) -> set[str]:
        paths = set()
        for store in (self.state, self.anon):
            for value in list(store.values()):
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict) and item.get("__upload__"):
                            paths.add(item["path"])
        return paths

    def cleanup_uploads(self):
        """删除已不被任何上传控件引用的暂存文件。"""
        if not os.path.isdir(self.upload_dir):
            return
        referenced = self.referenced_upload_paths()
        now = time.time()
        for name in os.listdir(self.upload_dir):
            path = os.path.join(self.upload_dir, name)
            try:
                # 刚上传、尚未进入下一次 run 的文件给出短暂宽限期。
                if path not in referenced and now - os.path.getmtime(path) > 60:
                    os.remove(path)
            except OSError:
                pass

    def destroy(self):
        shutil.rmtree(self.upload_dir, ignore_errors=True)


_sessions: dict[str, Session] = {}
_sessions_lock = threading.Lock()


def _expire_sessions(now: float):
    for stale_sid, stale in list(_sessions.items()):
        if now - stale.last_seen > SESSION_IDLE_SECONDS:
            _sessions.pop(stale_sid, None)
            stale.destroy()


def new_session() -> Session:
    now = time.time()
    with _sessions_lock:
        _expire_sessions(now)
        session = Session(uuid4().hex)
        _sessions[session.sid] = session
        return session


def find_session(sid: str | None) -> Session | None:
    now = time.time()
    with _sessions_lock:
        _expire_sessions(now)
        session = _sessions.get(sid or "")
        if session is not None:
            session.last_seen = now
        return session


class Run:
    """One execution of the page script, or of a single fragment."""

    def __init__(self, session: Session, mode: str, form_id: str):
        self.session = session
        self.mode = mode
        self.base_form_id = form_id
        self.toasts: list[tuple[str, str]] = []
        self.clicked: str | None = None
        self.triggers: dict[str, Any] = {}
        self.page_title = "MoneyPrinterTurbo"
        self.reset_tree()

    def reset_tree(self):
        self.root = Node("root")
        self.stack: list[Node] = [self.root]
        self.form_stack: list[str] = [self.base_form_id]
        self.defer_stack: list[bool] = [False]
        self.dialogs: list[Node] = []
        self.registered: dict[str, WidgetMeta] = {}
        self.auto_key_counts: dict[str, int] = {}
        self.fragment_counts: dict[str, int] = {}
        self.fragments: dict[str, FragmentReg] = {}
        self.scripts: list[dict] = []
        self.element_counter = 0

    @property
    def current(self) -> Node:
        return self.stack[-1]

    @property
    def form_id(self) -> str:
        return self.form_stack[-1]

    @property
    def deferred(self) -> bool:
        return self.defer_stack[-1]

    def next_element_id(self, prefix: str) -> str:
        self.element_counter += 1
        return f"{prefix}-{self.form_id}-{self.element_counter}"

    def auto_key(self, kind: str, label: str) -> str:
        base = f"$$auto-{kind}-{hashlib.sha1(str(label).encode()).hexdigest()[:10]}"
        count = self.auto_key_counts.get(base, 0)
        self.auto_key_counts[base] = count + 1
        return f"{base}-{count}" if count else base

    def register_widget(self, key: str, meta: WidgetMeta):
        self.registered[key] = meta
        self.session.widget_meta[key] = meta


_current_run: contextvars.ContextVar[Run | None] = contextvars.ContextVar(
    "mpt_htmx_run", default=None
)


def current_run() -> Run:
    run = _current_run.get()
    if run is None:
        raise RuntimeError("Streamlit API used outside of an HTMX WebUI run")
    return run


def has_run() -> bool:
    return _current_run.get() is not None


# -----------------------------------------------------------------------------
# 把浏览器提交的字符串还原成控件值
# -----------------------------------------------------------------------------


def _parse_widget_value(meta: WidgetMeta, values: list[str]):
    """Return ``(ok, value)``; ok=False keeps the previous value."""
    kind = meta.kind
    if kind == "multiselect":
        picked = []
        for raw in values:
            if raw == "__present__":
                continue
            try:
                picked.append(meta.options[int(raw)])
            except (ValueError, IndexError, TypeError):
                continue
        return True, picked

    raw = values[-1] if values else ""
    if kind in ("text_input", "text_area", "color_picker"):
        text = raw.replace("\r\n", "\n").replace("\r", "\n")
        if meta.max_chars:
            text = text[: meta.max_chars]
        return True, text
    if kind in ("checkbox", "toggle"):
        return True, raw in ("1", "true", "on")
    if kind in ("selectbox", "radio", "segmented_control", "tabs", "component"):
        if raw == "":
            return (True, None) if meta.allow_none else (False, None)
        if meta.accept_new:
            # 可输入新值的下拉框直接提交文本，再映射回已有选项或作为新值。
            text = raw.strip()
            if not text:
                return False, None
            for option, label in zip(meta.options or [], meta.labels or []):
                if text == label or text == str(option):
                    return True, option
            return True, text
        try:
            return True, meta.options[int(raw)]
        except (ValueError, IndexError, TypeError):
            return False, None
    if kind in ("slider", "number_input"):
        try:
            number = float(raw)
        except ValueError:
            return False, None
        if meta.min_value is not None:
            number = max(meta.min_value, number)
        if meta.max_value is not None:
            number = min(meta.max_value, number)
        if meta.num_type is int:
            return True, int(round(number))
        return True, number
    return False, None


def apply_posted(run: Run, form) -> None:
    """Write posted widget values into state and fire callbacks like Streamlit."""
    session = run.session
    grouped: dict[str, list[str]] = {}
    for name, value in form.multi_items():
        if name.startswith("w:") and isinstance(value, str):
            grouped.setdefault(name[2:], []).append(value)

    changed: list[WidgetMeta] = []
    for key, values in grouped.items():
        meta = session.widget_meta.get(key)
        if meta is None or meta.kind in ("button", "file_uploader"):
            continue
        ok, value = _parse_widget_value(meta, values)
        if not ok:
            continue
        if meta.kind == "component":
            run.triggers[key] = value
            changed.append(meta)
            continue
        store = session.store_for(meta)
        if store.get(key, _MISSING) != value:
            store[key] = value
            changed.append(meta)

    action = str(form.get("_action") or "").strip()
    run.clicked = action or None

    # Streamlit 在脚本重新执行前先调用回调，回调中读取到的是更新后的状态。
    for meta in changed:
        if callable(meta.on_change):
            meta.on_change(*meta.args, **meta.kwargs)
    if run.clicked:
        meta = session.widget_meta.get(run.clicked)
        if meta is not None and callable(meta.on_click):
            meta.on_click(*meta.args, **meta.kwargs)


def collect_garbage(run: Run) -> None:
    """Drop state of widgets that the finished full run did not render."""
    session = run.session
    for key, meta in list(session.widget_meta.items()):
        if key in run.registered:
            continue
        session.widget_meta.pop(key, None)
        session.store_for(meta).pop(key, None)
    session.fragments = dict(run.fragments)
    session.cleanup_uploads()


# -----------------------------------------------------------------------------
# 执行 webui/Main.py
# -----------------------------------------------------------------------------

_code_lock = threading.Lock()
_code_cache: tuple[float, Any] | None = None


def _main_code():
    """Compile Main.py once; recompile when the file changes on disk."""
    global _code_cache
    mtime = os.path.getmtime(MAIN_SCRIPT_PATH)
    with _code_lock:
        if _code_cache is None or _code_cache[0] != mtime:
            with open(MAIN_SCRIPT_PATH, "r", encoding="utf-8") as file:
                source = file.read()
            _code_cache = (mtime, compile(source, MAIN_SCRIPT_PATH, "exec"))
        return _code_cache[1]


def exec_main_script():
    namespace = {
        "__name__": "__main__",
        "__file__": MAIN_SCRIPT_PATH,
        "__builtins__": __builtins__,
    }
    exec(_main_code(), namespace)


def _run_with_reruns(run: Run, body: Callable[[], None]) -> str | None:
    """Execute ``body`` honouring st.rerun/st.stop; return 'app' to escalate."""
    for _ in range(MAX_RERUNS_PER_REQUEST):
        try:
            body()
            return None
        except RerunException as exc:
            run.clicked = None
            run.triggers = {}
            if exc.scope == "app" and run.mode == "fragment":
                return "app"
            run.reset_tree()
        except StopException:
            return None
        except Exception as exc:  # noqa: BLE001 - shown in the page like Streamlit
            logger.exception(f"WebUI script error: {exc}")
            import traceback

            run.current.children.append(
                Node(
                    "exception",
                    {
                        "message": f"{type(exc).__name__}: {exc}",
                        "trace": traceback.format_exc(),
                    },
                )
            )
            return None
    logger.warning("WebUI rerun limit reached; rendering the last state")
    return None


def full_run(session: Session, form=None) -> Run:
    """Run the whole page. Caller must hold ``session.lock``."""
    run = Run(session, mode="full", form_id="f-main")
    token = _current_run.set(run)
    try:
        if form is not None:
            apply_posted(run, form)
        _run_with_reruns(run, exec_main_script)
        collect_garbage(run)
        return run
    finally:
        _current_run.reset(token)


def fragment_run(session: Session, reg: FragmentReg, form) -> Run:
    """Run one fragment; ``run.escalate`` is True when it asked for a full rerun."""
    from webui_htmx import stshim

    run = Run(session, mode="fragment", form_id=reg.form_id)
    run.fragment = reg
    token = _current_run.set(run)
    try:
        if form is not None:
            apply_posted(run, form)

        def body():
            stshim.render_fragment_body(run, reg)

        run.escalate = _run_with_reruns(run, body) == "app"
        if not run.escalate:
            session.fragments.update(run.fragments)
        return run
    finally:
        _current_run.reset(token)


def call_with_run(session: Session, func: Callable, *args, **kwargs):
    """Call page callbacks (for example a dialog's on_dismiss) inside a run."""
    run = Run(session, mode="callback", form_id="f-main")
    token = _current_run.set(run)
    try:
        return func(*args, **kwargs), run
    finally:
        _current_run.reset(token)
