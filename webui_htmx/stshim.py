"""
A Streamlit-compatible API for the HTMX WebUI.

``webui_htmx.app`` registers this module as ``sys.modules["streamlit"]`` so
``webui/Main.py`` and ``webui/kimi_login.py`` run unchanged. Each call adds a
node to the current run's tree (see ``runtime``); ``render`` turns the tree
into HTML. Only the subset of the Streamlit API used by the WebUI is
implemented, with the same value and state semantics:

* a keyed widget reads ``st.session_state[key]`` when present, otherwise its
  default, and writes the value back;
* buttons return True only during the run triggered by their click;
* ``on_change``/``on_click`` callbacks run before the next script run;
* fragments and dialogs rerun on their own, optionally on a timer.
"""

import copy
import functools
import hashlib
import mimetypes
import os
import re
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

from webui_htmx import runtime as rt
from webui_htmx.runtime import FragmentReg, Node, WidgetMeta

_MISSING = object()
# Streamlit 默认的 server.maxUploadSize 为 200MB。
DEFAULT_MAX_UPLOAD_MB = 200


def _run() -> rt.Run:
    return rt.current_run()


def _safe_id(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value))[:60]
    digest = hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def _parse_interval(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    match = re.fullmatch(r"([\d.]+)\s*(ms|s|m)?", text)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2) or "s"
    return number / 1000 if unit == "ms" else number * 60 if unit == "m" else number


# -----------------------------------------------------------------------------
# 上传文件
# -----------------------------------------------------------------------------


class UploadedFile:
    """Mirrors Streamlit's UploadedFile on top of a staged file on disk."""

    def __init__(self, record: dict):
        self.file_id = record["file_id"]
        self.name = record["name"]
        self.size = int(record["size"])
        self.type = record.get("type") or ""
        self._path = record["path"]
        self._handle = None

    def _file(self):
        if self._handle is None or self._handle.closed:
            self._handle = open(self._path, "rb")
        return self._handle

    def read(self, size=-1):
        return self._file().read(size)

    def readinto(self, buffer):
        return self._file().readinto(buffer)

    def seek(self, offset, whence=0):
        return self._file().seek(offset, whence)

    def tell(self):
        return self._file().tell()

    def readable(self):
        return True

    def seekable(self):
        return True

    def getvalue(self) -> bytes:
        with open(self._path, "rb") as file:
            return file.read()

    def getbuffer(self) -> memoryview:
        return memoryview(self.getvalue())

    def close(self):
        if self._handle is not None:
            self._handle.close()

    def __iter__(self):
        return iter(self._file())

    def __repr__(self):
        return f"UploadedFile(name={self.name!r}, size={self.size})"


# -----------------------------------------------------------------------------
# DeltaGenerator：所有元素和控件的统一入口
# -----------------------------------------------------------------------------


class DeltaGenerator:
    def __init__(self, node: Node | None = None, open_: bool = True):
        self._node = node
        self.open = open_

    # -- 容器上下文 ---------------------------------------------------------

    def _parent(self) -> Node:
        return self._node if self._node is not None else _run().current

    def _add(self, kind: str, **props) -> Node:
        node = Node(kind, props)
        self._parent().children.append(node)
        return node

    def __enter__(self):
        if self._node is not None:
            run = _run()
            run.stack.append(self._node)
            run.form_stack.append(self._node.props.get("_form_id") or run.form_id)
            run.defer_stack.append(bool(self._node.props.get("_defer")) or run.deferred)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._node is not None:
            run = _run()
            run.stack.pop()
            run.form_stack.pop()
            run.defer_stack.pop()
        return False

    def _container(self, kind: str, **props) -> "DeltaGenerator":
        run = _run()
        props.setdefault("_form_id", run.form_id)
        props.setdefault("_defer", run.deferred)
        return DeltaGenerator(self._add(kind, **props))

    # -- 布局 -----------------------------------------------------------------

    def container(
        self,
        *,
        height=None,
        border=None,
        key=None,
        width="stretch",
        horizontal=False,
        horizontal_alignment="left",
        vertical_alignment="top",
        gap="small",
    ):
        return self._container(
            "container",
            key=None if key is None else str(key),
            height=height,
            border=bool(border),
            width=width,
            horizontal=horizontal,
            horizontal_alignment=horizontal_alignment,
            vertical_alignment=vertical_alignment,
            gap=gap,
        )

    def empty(self):
        return self.container()

    def columns(self, spec, *, gap="small", vertical_alignment="top", border=False, width="stretch"):
        weights = [1] * spec if isinstance(spec, int) else [float(item) for item in spec]
        row = self._container(
            "columns", weights=weights, gap=gap, vertical_alignment=vertical_alignment
        )
        columns = []
        for weight in weights:
            columns.append(row._container("column", weight=weight, border=border))
        return columns

    def expander(self, label, expanded=False, *, icon=None, width="stretch"):
        run = _run()
        return self._container(
            "expander",
            label=str(label),
            expanded=bool(expanded),
            eid=run.next_element_id("exp"),
        )

    def popover(self, label, *, help=None, type="secondary", icon=None, disabled=False,
                use_container_width=None, width="content", key=None):
        run = _run()
        element_id = _safe_id(f"pop-{key}") if key else run.next_element_id("pop")
        return self._container("popover", label=str(label), eid=element_id, help=help)

    def tabs(self, tabs, *, width="stretch", default=None, key=None, on_change="ignore"):
        run = _run()
        labels = [str(label) for label in tabs]
        user_key = key is not None
        key = str(key) if user_key else run.auto_key("tabs", "|".join(labels))
        store = run.session.state if user_key else run.session.anon
        initial = default if default in labels else labels[0]
        if store.get(key) not in labels:
            store[key] = initial
        selected = store[key]
        run.register_widget(
            key,
            WidgetMeta(
                "tabs",
                user_key,
                run.form_id,
                options=labels,
                labels=labels,
                on_change=on_change if callable(on_change) else None,
            ),
        )
        bar = self._container(
            "tabs",
            key=key,
            name=f"w:{key}",
            form=run.form_id,
            labels=labels,
            selected=labels.index(selected),
            wid=_safe_id(f"w-{key}"),
        )
        result = []
        for label in labels:
            panel = bar._container("tab", label=label, selected=label == selected)
            panel.open = label == selected
            result.append(panel)
        return result

    def form(self, key, clear_on_submit=False, *, enter_to_submit=True, border=True,
             width="stretch", height="content"):
        return self._container("form", key=str(key), border=border, _defer=True)

    # -- 文本与提示 -----------------------------------------------------------

    def markdown(self, body, unsafe_allow_html=False, *, help=None, width="stretch", text_alignment=None):
        text = str(body)
        if unsafe_allow_html and text.lstrip().lower().startswith("<style"):
            # Main.py 注入的是 Streamlit DOM 专用样式，HTMX 页面使用自己的样式表。
            return self
        self._add("markdown", body=text, html=bool(unsafe_allow_html), help=help, width=width)
        return self

    def write(self, *args, unsafe_allow_html=False):
        for arg in args:
            if isinstance(arg, str):
                self.markdown(arg, unsafe_allow_html=unsafe_allow_html)
            else:
                self._add("code", body=repr(arg), language=None, wrap=True)
        return self

    def caption(self, body, unsafe_allow_html=False, *, help=None, width="stretch"):
        self._add("caption", body=str(body), html=bool(unsafe_allow_html), help=help)
        return self

    def code(self, body, language="python", *, line_numbers=False, wrap_lines=False,
             height="content", width="stretch"):
        self._add("code", body=str(body), language=language, wrap=bool(wrap_lines))
        return self

    def _alert(self, level, body, icon=None):
        self._add("alert", level=level, body=str(body), icon=icon)
        return self

    def info(self, body, *, icon=None, width="stretch"):
        return self._alert("info", body, icon)

    def warning(self, body, *, icon=None, width="stretch"):
        return self._alert("warning", body, icon)

    def error(self, body, *, icon=None, width="stretch"):
        return self._alert("error", body, icon)

    def success(self, body, *, icon=None, width="stretch"):
        return self._alert("success", body, icon)

    def divider(self, *, width="stretch"):
        self._add("divider")
        return self

    def metric(self, label, value, delta=None, delta_color="normal", *, help=None, **_):
        self._add("metric", label=str(label), value="" if value is None else str(value), help=help)
        return self

    def progress(self, value, text=None, *, width="stretch"):
        number = float(value)
        percent = number if number > 1 else number * 100
        self._add("progress", value=max(0.0, min(100.0, percent)), text=text)
        return self

    @contextmanager
    def spinner(self, text="In progress...", *, show_time=False, width="content"):
        # 请求在服务端同步执行，期间页面顶部显示统一的加载状态。
        yield

    def toast(self, body, *, icon=None, duration="short"):
        _run().toasts.append((str(body), icon or ""))
        return self

    # -- 媒体 -----------------------------------------------------------------

    def _media_src(self, data, mime):
        session = _run().session
        prefix = f"/media/{session.sid}"
        if isinstance(data, UploadedFile):
            return f"{prefix}/{session.register_media('path', data._path, mime or data.type)}"
        if isinstance(data, (bytes, bytearray, memoryview)):
            return f"{prefix}/{session.register_media('bytes', bytes(data), mime)}"
        if isinstance(data, str) and os.path.isfile(data):
            guessed = mime or mimetypes.guess_type(data)[0] or "application/octet-stream"
            return f"{prefix}/{session.register_media('path', os.path.abspath(data), guessed)}"
        if hasattr(data, "read"):
            return f"{prefix}/{session.register_media('bytes', data.read(), mime)}"
        if isinstance(data, str):
            return data
        return ""

    def audio(self, data, format="audio/wav", start_time=0, *, sample_rate=None,
              end_time=None, loop=False, autoplay=False, width="stretch"):
        src = self._media_src(data, format or "audio/wav")
        self._add("audio", src=src, mime=format, autoplay=bool(autoplay), loop=bool(loop))
        return self

    def video(self, data, format="video/mp4", start_time=0, *, subtitles=None, end_time=None,
              loop=False, autoplay=False, muted=False, width="stretch"):
        src = self._media_src(data, format or "video/mp4")
        self._add("video", src=src, mime=format, autoplay=bool(autoplay), loop=bool(loop), muted=bool(muted))
        return self

    # -- 控件公共逻辑 ---------------------------------------------------------

    def _key(self, kind, key, label):
        run = _run()
        if key is None:
            return run.auto_key(kind, label), False
        return str(key), True

    def _store(self, user_key):
        session = _run().session
        return session.state if user_key else session.anon

    def _common(self, key, label, help, disabled, label_visibility, user_key):
        run = _run()
        return {
            "key": key,
            "user_key": user_key,
            "name": f"w:{key}",
            "wid": _safe_id(f"w-{key}"),
            "form": run.form_id,
            "defer": run.deferred,
            "label": str(label),
            "help": help,
            "disabled": bool(disabled),
            "label_visibility": label_visibility,
        }

    def _callbacks(self, on_change, args, kwargs):
        return {
            "on_change": on_change if callable(on_change) else None,
            "args": tuple(args or ()),
            "kwargs": dict(kwargs or {}),
        }

    # -- 输入控件 -------------------------------------------------------------

    def text_input(self, label, value="", max_chars=None, key=None, type="default", help=None,
                   autocomplete=None, on_change=None, args=None, kwargs=None, *, placeholder=None,
                   disabled=False, label_visibility="visible", icon=None, width="stretch"):
        run = _run()
        key, user_key = self._key("text_input", key, label)
        store = self._store(user_key)
        if key not in store:
            store[key] = "" if value is None else str(value)
        current = store[key]
        current = "" if current is None else str(current)
        run.register_widget(key, WidgetMeta("text_input", user_key, run.form_id,
                                            max_chars=max_chars,
                                            **self._callbacks(on_change, args, kwargs)))
        self._add("text_input", value=current, input_type=type, placeholder=placeholder,
                  max_chars=max_chars,
                  **self._common(key, label, help, disabled, label_visibility, user_key))
        return current

    def text_area(self, label, value="", height=None, max_chars=None, key=None, help=None,
                  on_change=None, args=None, kwargs=None, *, placeholder=None, disabled=False,
                  label_visibility="visible", width="stretch"):
        run = _run()
        key, user_key = self._key("text_area", key, label)
        store = self._store(user_key)
        if key not in store:
            store[key] = "" if value is None else str(value)
        current = "" if store[key] is None else str(store[key])
        run.register_widget(key, WidgetMeta("text_area", user_key, run.form_id,
                                            max_chars=max_chars,
                                            **self._callbacks(on_change, args, kwargs)))
        self._add("text_area", value=current, height=height, placeholder=placeholder,
                  max_chars=max_chars,
                  **self._common(key, label, help, disabled, label_visibility, user_key))
        return current

    def _choice(self, kind, label, options, default, format_func, key, help, on_change, args,
                kwargs, disabled, label_visibility, allow_none=False, accept_new=False,
                **extra):
        run = _run()
        options = list(options)
        key, user_key = self._key(kind, key, label)
        store = self._store(user_key)
        if key not in store:
            store[key] = default
        value = store[key]
        is_custom = accept_new and isinstance(value, str) and bool(value.strip()) and value not in options
        if value not in options and not is_custom and not (allow_none and value is None):
            value = default
            store[key] = value
        format_func = format_func or str
        labels = [str(format_func(option)) for option in options]
        run.register_widget(key, WidgetMeta(kind, user_key, run.form_id, options=options,
                                            labels=labels, allow_none=allow_none,
                                            accept_new=accept_new,
                                            **self._callbacks(on_change, args, kwargs)))
        selected = options.index(value) if value in options else None
        custom_label = ""
        if is_custom:
            try:
                custom_label = str(format_func(value))
            except Exception:  # noqa: BLE001 - display only
                custom_label = str(value)
        self._add(kind, labels=labels, selected=selected, custom_label=custom_label,
                  accept_new=accept_new, allow_none=allow_none,
                  **extra, **self._common(key, label, help, disabled, label_visibility, user_key))
        return value

    def selectbox(self, label, options, index=0, format_func=str, key=None, help=None,
                  on_change=None, args=None, kwargs=None, *, placeholder=None, disabled=False,
                  label_visibility="visible", accept_new_options=False, width="stretch"):
        options = list(options)
        default = options[index] if index is not None and options else None
        return self._choice("selectbox", label, options, default, format_func, key, help,
                            on_change, args, kwargs, disabled, label_visibility,
                            allow_none=index is None, accept_new=bool(accept_new_options),
                            placeholder=placeholder, width=width)

    def radio(self, label, options, index=0, format_func=str, key=None, help=None,
              on_change=None, args=None, kwargs=None, *, disabled=False, horizontal=False,
              captions=None, label_visibility="visible", width="content"):
        options = list(options)
        default = options[index] if index is not None and options else None
        return self._choice("radio", label, options, default, format_func, key, help,
                            on_change, args, kwargs, disabled, label_visibility,
                            allow_none=index is None, horizontal=horizontal)

    def segmented_control(self, label, options, *, selection_mode="single", default=None,
                          format_func=None, key=None, help=None, on_change=None, args=None,
                          kwargs=None, disabled=False, label_visibility="visible",
                          required=False, width="content"):
        return self._choice("segmented_control", label, list(options), default, format_func,
                            key, help, on_change, args, kwargs, disabled, label_visibility,
                            allow_none=not required, width=width)

    def multiselect(self, label, options, default=None, format_func=str, key=None, help=None,
                    on_change=None, args=None, kwargs=None, *, max_selections=None,
                    placeholder=None, disabled=False, label_visibility="visible",
                    accept_new_options=False, width="stretch"):
        run = _run()
        options = list(options)
        key, user_key = self._key("multiselect", key, label)
        store = self._store(user_key)
        if key not in store:
            store[key] = [item for item in list(default or []) if item in options]
        value = [item for item in list(store[key] or []) if item in options]
        store[key] = value
        labels = [str((format_func or str)(option)) for option in options]
        run.register_widget(key, WidgetMeta("multiselect", user_key, run.form_id,
                                            options=options, labels=labels,
                                            **self._callbacks(on_change, args, kwargs)))
        self._add("multiselect", labels=labels,
                  selected=[options.index(item) for item in value],
                  **self._common(key, label, help, disabled, label_visibility, user_key))
        return list(value)

    def checkbox(self, label, value=False, key=None, help=None, on_change=None, args=None,
                 kwargs=None, *, disabled=False, label_visibility="visible", width="content"):
        return self._bool("checkbox", label, value, key, help, on_change, args, kwargs,
                          disabled, label_visibility)

    def toggle(self, label, value=False, key=None, help=None, on_change=None, args=None,
               kwargs=None, *, disabled=False, label_visibility="visible", width="content"):
        return self._bool("toggle", label, value, key, help, on_change, args, kwargs,
                          disabled, label_visibility)

    def _bool(self, kind, label, value, key, help, on_change, args, kwargs, disabled,
              label_visibility):
        run = _run()
        key, user_key = self._key(kind, key, label)
        store = self._store(user_key)
        if key not in store:
            store[key] = bool(value)
        current = bool(store[key])
        run.register_widget(key, WidgetMeta(kind, user_key, run.form_id,
                                            **self._callbacks(on_change, args, kwargs)))
        self._add(kind, value=current,
                  **self._common(key, label, help, disabled, label_visibility, user_key))
        return current

    def _number(self, kind, label, min_value, max_value, value, step, format, key, help,
                on_change, args, kwargs, disabled, label_visibility, placeholder=None):
        run = _run()
        numbers = [item for item in (min_value, max_value, value, step)
                   if isinstance(item, (int, float)) and not isinstance(item, bool)]
        num_type = float if any(isinstance(item, float) for item in numbers) else int
        if kind == "slider":
            min_value = num_type(0 if min_value is None else min_value)
            max_value = num_type(100 if max_value is None else max_value)
            step = step if step is not None else (1 if num_type is int else 0.01)
        key, user_key = self._key(kind, key, label)
        store = self._store(user_key)
        if key not in store:
            if value is None or value == "min":
                initial = min_value if min_value is not None else 0
            else:
                initial = value
            store[key] = num_type(initial)
        try:
            current = num_type(store[key])
        except (TypeError, ValueError):
            current = num_type(min_value if min_value is not None else 0)
        if min_value is not None:
            current = max(num_type(min_value), current)
        if max_value is not None:
            current = min(num_type(max_value), current)
        store[key] = current
        run.register_widget(key, WidgetMeta(kind, user_key, run.form_id, num_type=num_type,
                                            min_value=min_value, max_value=max_value,
                                            **self._callbacks(on_change, args, kwargs)))
        self._add(kind, value=current, min_value=min_value, max_value=max_value,
                  step=step if step is not None else (1 if num_type is int else "any"),
                  format=format, placeholder=placeholder,
                  **self._common(key, label, help, disabled, label_visibility, user_key))
        return current

    def slider(self, label, min_value=None, max_value=None, value=None, step=None, format=None,
               key=None, help=None, on_change=None, args=None, kwargs=None, *, disabled=False,
               label_visibility="visible", width="stretch"):
        return self._number("slider", label, min_value, max_value, value, step, format, key,
                            help, on_change, args, kwargs, disabled, label_visibility)

    def number_input(self, label, min_value=None, max_value=None, value="min", step=None,
                     format=None, key=None, help=None, on_change=None, args=None, kwargs=None,
                     *, placeholder=None, disabled=False, label_visibility="visible",
                     icon=None, width="stretch"):
        return self._number("number_input", label, min_value, max_value, value, step, format,
                            key, help, on_change, args, kwargs, disabled, label_visibility,
                            placeholder)

    def color_picker(self, label, value=None, key=None, help=None, on_change=None, args=None,
                     kwargs=None, *, disabled=False, label_visibility="visible", width="content"):
        run = _run()
        key, user_key = self._key("color_picker", key, label)
        store = self._store(user_key)
        if key not in store:
            store[key] = value or "#000000"
        current = str(store[key] or "#000000")
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", current):
            current = "#000000"
        run.register_widget(key, WidgetMeta("color_picker", user_key, run.form_id,
                                            **self._callbacks(on_change, args, kwargs)))
        self._add("color_picker", value=current,
                  **self._common(key, label, help, disabled, label_visibility, user_key))
        return current

    def file_uploader(self, label, type=None, accept_multiple_files=False, key=None, help=None,
                      on_change=None, args=None, kwargs=None, *, max_upload_size=None,
                      disabled=False, label_visibility="visible", width="stretch"):
        run = _run()
        key, user_key = self._key("file_uploader", key, label)
        store = self._store(user_key)
        records = [item for item in list(store.get(key) or [])
                   if isinstance(item, dict) and item.get("__upload__")
                   and os.path.isfile(item.get("path", ""))]
        if records:
            store[key] = records
        else:
            store.pop(key, None)
        if isinstance(type, str):
            type = [type]
        extensions = [f".{str(item).lstrip('.')}" for item in (type or [])]
        max_bytes = int(max_upload_size or DEFAULT_MAX_UPLOAD_MB) * 1024 * 1024
        run.register_widget(key, WidgetMeta("file_uploader", user_key, run.form_id,
                                            upload_types=extensions,
                                            upload_multiple=bool(accept_multiple_files),
                                            upload_max_bytes=max_bytes,
                                            **self._callbacks(on_change, args, kwargs)))
        self._add("file_uploader", files=[(item["file_id"], item["name"], item["size"])
                                          for item in records],
                  accept=",".join(sorted(set(extensions))), multiple=bool(accept_multiple_files),
                  max_mb=max_bytes // (1024 * 1024),
                  **self._common(key, label, help, disabled, label_visibility, user_key))
        files = [UploadedFile(item) for item in records]
        if accept_multiple_files:
            return files
        return files[0] if files else None

    # -- 按钮 -----------------------------------------------------------------

    def _button_meta(self, key, user_key, on_click, args, kwargs):
        run = _run()
        run.register_widget(key, WidgetMeta("button", user_key, run.form_id,
                                            on_click=on_click if callable(on_click) else None,
                                            args=tuple(args or ()), kwargs=dict(kwargs or {})))

    def button(self, label, key=None, help=None, on_click=None, args=None, kwargs=None, *,
               type="secondary", icon=None, disabled=False, use_container_width=None,
               width="content", shortcut=None):
        run = _run()
        key, user_key = self._key("button", key, label)
        self._button_meta(key, user_key, on_click, args, kwargs)
        self._add("button", variant=type, icon=icon,
                  full=bool(use_container_width) or width == "stretch",
                  **self._common(key, label, help, disabled, "visible", user_key))
        return run.clicked == key

    def form_submit_button(self, label="Submit", help=None, on_click=None, args=None,
                           kwargs=None, *, key=None, type="secondary", icon=None, disabled=False,
                           use_container_width=None, width="content"):
        return self.button(label, key=key, help=help, on_click=on_click, args=args,
                           kwargs=kwargs, type=type, icon=icon, disabled=disabled,
                           use_container_width=use_container_width, width=width)

    def download_button(self, label, data, file_name=None, mime=None, key=None, help=None,
                        on_click="rerun", args=None, kwargs=None, *, type="secondary",
                        icon=None, disabled=False, use_container_width=None, width="content"):
        session = _run().session
        file_name = file_name or "download"
        mime = mime or mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        payload_name = getattr(data, "name", None)
        if isinstance(payload_name, str) and os.path.isfile(payload_name):
            # Main.py 在 with open(...) 中传入文件对象；记录路径，按需流式下载。
            download_id = session.register_download("path", os.path.abspath(payload_name),
                                                    file_name, mime)
        else:
            if isinstance(data, str):
                payload = data.encode("utf-8")
            elif hasattr(data, "read"):
                payload = data.read()
            else:
                payload = bytes(data)
            download_id = session.register_download("bytes", payload, file_name, mime)
        key, user_key = self._key("download_button", key, label)
        self._add("link_button", href=f"/download/{session.sid}/{download_id}", download=file_name,
                  variant=type, icon=icon, new_tab=False,
                  full=bool(use_container_width) or width == "stretch", action=None,
                  **self._common(key, label, help, disabled, "visible", user_key))
        return False

    def link_button(self, label, url, *, help=None, type="secondary", icon=None,
                    disabled=False, use_container_width=None, width="content", key=None,
                    on_click=None, args=None, kwargs=None):
        key, user_key = self._key("link_button", key, label)
        action = None
        if callable(on_click):
            self._button_meta(key, user_key, on_click, args, kwargs)
            action = key
        self._add("link_button", href=str(url), download=None, variant=type, icon=icon,
                  new_tab=True, full=bool(use_container_width) or width == "stretch",
                  action=action,
                  **self._common(key, label, help, disabled, "visible", user_key))
        return False


_main = DeltaGenerator()


# -----------------------------------------------------------------------------
# 模块级 API：与 ``import streamlit as st`` 的用法保持一致
# -----------------------------------------------------------------------------

for _name in (
    "container", "empty", "columns", "expander", "popover", "tabs", "form", "markdown",
    "write", "caption", "code", "info", "warning", "error", "success", "divider", "metric",
    "progress", "spinner", "toast", "audio", "video", "text_input", "text_area", "selectbox",
    "radio", "segmented_control", "multiselect", "checkbox", "toggle", "slider",
    "number_input", "color_picker", "file_uploader", "button", "form_submit_button",
    "download_button", "link_button",
):
    globals()[_name] = getattr(_main, _name)


def set_page_config(page_title=None, page_icon=None, layout="centered",
                    initial_sidebar_state="auto", menu_items=None):
    if page_title:
        _run().page_title = str(page_title)


def rerun(*, scope="app"):
    raise rt.RerunException(scope)


def stop():
    raise rt.StopException()


class _SessionStateProxy:
    def _state(self):
        return _run().session.state

    def __getitem__(self, key):
        return self._state()[key]

    def __setitem__(self, key, value):
        self._state()[key] = value

    def __delitem__(self, key):
        del self._state()[key]

    def __contains__(self, key):
        return key in self._state()

    def __iter__(self):
        return iter(list(self._state().keys()))

    def __len__(self):
        return len(self._state())

    def __getattr__(self, name):
        state = self._state()
        if hasattr(dict, name):
            return getattr(state, name)
        return getattr(state, name)

    def __setattr__(self, name, value):
        self._state()[name] = value

    def __repr__(self):
        return repr(self._state())


session_state = _SessionStateProxy()


class _Context:
    @property
    def locale(self):
        return _run().session.locale or None

    @property
    def headers(self):
        return dict(_run().session.headers)


context = _Context()


# -- 缓存 ---------------------------------------------------------------------

_cache_store: dict = {}
_cache_lock = threading.Lock()


def _cache_decorator(func, ttl, copy_result):
    ident = (func.__qualname__, func.__code__.co_filename, func.__code__.co_firstlineno)
    ttl_seconds = _parse_interval(ttl) if ttl is not None else None
    if hasattr(ttl, "total_seconds"):
        ttl_seconds = ttl.total_seconds()

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        cache_key = (ident, repr(args), repr(sorted(kwargs.items())))
        now = time.time()
        with _cache_lock:
            hit = _cache_store.get(cache_key)
        if hit is not None and (ttl_seconds is None or now - hit[0] < ttl_seconds):
            return copy.deepcopy(hit[1]) if copy_result else hit[1]
        value = func(*args, **kwargs)
        with _cache_lock:
            _cache_store[cache_key] = (now, value)
        return copy.deepcopy(value) if copy_result else value

    def clear(*_args, **_kwargs):
        with _cache_lock:
            for cache_key in [item for item in _cache_store if item[0] == ident]:
                _cache_store.pop(cache_key, None)

    wrapper.clear = clear
    return wrapper


def cache_data(func=None, *, ttl=None, max_entries=None, show_spinner=True, persist=None,
               hash_funcs=None, scope="global"):
    if callable(func):
        return _cache_decorator(func, ttl, True)
    return lambda inner: _cache_decorator(inner, ttl, True)


def cache_resource(func=None, *, ttl=None, max_entries=None, show_spinner=True,
                   validate=None, hash_funcs=None, on_release=None, scope="global"):
    if callable(func):
        return _cache_decorator(func, ttl, False)
    return lambda inner: _cache_decorator(inner, ttl, False)


# -- Fragment 与 Dialog -------------------------------------------------------


def _next_fragment_id(run: rt.Run, prefix: str, func) -> str:
    name = f"{prefix}{func.__qualname__}"
    index = run.fragment_counts.get(name, 0)
    run.fragment_counts[name] = index + 1
    return _safe_id(f"{name}-{index}")


def _execute_fragment(run: rt.Run, reg: FragmentReg, parent: Node):
    node = Node("fragment", {"fid": reg.fid, "run_every": reg.run_every, "form_id": reg.form_id})
    parent.children.append(node)
    run.fragments[reg.fid] = reg
    run.stack.append(node)
    run.form_stack.append(reg.form_id)
    run.defer_stack.append(False)
    try:
        reg.func(*reg.args, **reg.kwargs)
    finally:
        run.stack.pop()
        run.form_stack.pop()
        run.defer_stack.pop()
    return node


def render_fragment_body(run: rt.Run, reg: FragmentReg):
    """Entry point for a fragment-only run (see runtime.fragment_run)."""
    run.fragment_node = _execute_fragment(run, reg, run.root)


def fragment(func=None, *, run_every=None):
    def decorator(inner):
        @functools.wraps(inner)
        def wrapper(*args, **kwargs):
            run = _run()
            fid = _next_fragment_id(run, "frag-", inner)
            reg = FragmentReg(fid, inner, args, kwargs, _parse_interval(run_every), f"f-{fid}")
            _execute_fragment(run, reg, run.current)

        return wrapper

    if callable(func):
        return decorator(func)
    return decorator


def dialog(title, *, width="small", dismissible=True, on_dismiss="ignore", icon=None):
    def decorator(inner):
        @functools.wraps(inner)
        def wrapper(*args, **kwargs):
            run = _run()
            fid = _next_fragment_id(run, "dlg-", inner)
            reg = FragmentReg(
                fid, inner, args, kwargs, None, f"f-{fid}",
                dialog={"title": str(title), "width": width, "dismissible": dismissible,
                        "on_dismiss": on_dismiss},
            )
            dialog_node = Node("dialog", {"fid": fid, "title": str(title), "width": width,
                                          "dismissible": dismissible})
            run.dialogs.append(dialog_node)
            _execute_fragment(run, reg, dialog_node)

        return wrapper

    return decorator


# -- 自定义组件：Main.py 只使用分组下拉框 -------------------------------------


class _Component:
    def __init__(self, name):
        self.name = name

    def __call__(self, *, key=None, data=None, default=None, **callbacks):
        if self.name != "mpt_grouped_select":
            return SimpleNamespace()
        run = _run()
        key = str(key)
        data = data or {}
        values, groups = [], []
        for group in data.get("groups", []):
            options = []
            for option in group.get("options", []):
                options.append((len(values), str(option.get("label", ""))))
                values.append(option.get("value"))
            groups.append((str(group.get("label", "")), options))
        current = data.get("value")
        selected = values.index(current) if current in values else None
        on_selected = callbacks.get("on_selected_change")
        on_settings = callbacks.get("on_settings_change")
        run.register_widget(key, WidgetMeta("component", True, run.form_id, options=values,
                                            on_change=on_selected if callable(on_selected) else None))
        settings_key = f"{key}::settings"
        run.register_widget(settings_key, WidgetMeta(
            "button", True, run.form_id, on_click=on_settings if callable(on_settings) else None))
        _main._add("grouped_select", key=key, name=f"w:{key}", wid=_safe_id(f"w-{key}"),
                   form=run.form_id, label=str(data.get("label", "")), groups=groups,
                   selected=selected, settings_label=str(data.get("settingsLabel") or ""),
                   settings_key=settings_key, defer=run.deferred, user_key=True)
        return SimpleNamespace(selected=run.triggers.get(key),
                               settings=run.clicked == settings_key)


class _ComponentsV2:
    def component(self, name, html=None, css=None, js=None, **_kwargs):
        return _Component(name)


components = SimpleNamespace(v2=_ComponentsV2())


def __getattr__(name):
    raise AttributeError(f"streamlit.{name} is not supported by the HTMX WebUI")
