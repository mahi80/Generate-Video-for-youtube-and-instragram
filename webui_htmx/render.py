"""Render a run's node tree into HTML for htmx to morph into the page."""

import json
import re
import textwrap
from html import escape

from markdown_it import MarkdownIt

from webui_htmx import runtime as rt

_md_safe = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
_md_html = MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"])

_COLOR_PATTERN = re.compile(
    r":(blue|green|orange|red|violet|gray|grey|rainbow|primary)\[(.*?)\]", re.S
)
_ICON_PATTERN = re.compile(r":material/([a-z0-9_]+):")
_LINK_PATTERN = re.compile(r"<a href=")
_TAG_PATTERN = re.compile(r"<[^>]+>")

# Streamlit 语义色到页面样式的映射。
_ALERT_ICONS = {
    "info": "info",
    "warning": "warning",
    "error": "error",
    "success": "check_circle",
}


def _post_markdown(html: str) -> str:
    html = _COLOR_PATTERN.sub(r'<span class="c-\1">\2</span>', html)
    html = _ICON_PATTERN.sub(r'<span class="msym" aria-hidden="true">\1</span>', html)
    return _LINK_PATTERN.sub('<a target="_blank" rel="noopener noreferrer" href=', html)


def md(text, *, allow_html=False, inline=False) -> str:
    renderer = _md_html if allow_html else _md_safe
    # 与 Streamlit 一致：先去除公共缩进，避免缩进的 HTML 被当作代码块。
    source = textwrap.dedent(str(text or "")).strip()
    html = renderer.renderInline(source) if inline else renderer.render(source)
    return _post_markdown(html)


def plain(text) -> str:
    """Help text for title attributes: markdown rendered then tags stripped."""
    return _TAG_PATTERN.sub("", md(text, inline=True))


def icon(name) -> str:
    if not name:
        return ""
    match = _ICON_PATTERN.fullmatch(str(name))
    if match:
        return f'<span class="msym" aria-hidden="true">{escape(match.group(1))}</span>'
    return f'<span class="emoji" aria-hidden="true">{escape(str(name))}</span>'


def help_html(text) -> str:
    if not text:
        return ""
    return (
        '<span class="help" tabindex="0" role="note">'
        '<span class="msym" aria-hidden="true">help</span>'
        f'<span class="tip">{md(text)}</span></span>'
    )


def _attrs(**attrs) -> str:
    parts = []
    for name, value in attrs.items():
        if value is None or value is False:
            continue
        name = name.rstrip("_").replace("_", "-")
        if value is True:
            parts.append(name)
        else:
            parts.append(f'{name}="{escape(str(value), quote=True)}"')
    return " ".join(parts)


def _field_attrs(p, **extra) -> str:
    return _attrs(
        id=p["wid"],
        name=p["name"],
        form=p["form"],
        data_w="1",
        data_defer="1" if p.get("defer") else None,
        disabled=p.get("disabled"),
        **extra,
    )


def _wrap(p, kind, inner, *, label_for=True, extra_class="") -> str:
    classes = ["w", f"w-{kind}"]
    if p.get("user_key"):
        classes.append(f"st-key-{p['key']}")
    if extra_class:
        classes.append(extra_class)
    visibility = p.get("label_visibility", "visible")
    label = ""
    if p.get("label") and visibility != "collapsed":
        hidden = ' class="label sr-only"' if visibility == "hidden" else ' class="label"'
        target = f' for="{escape(p["wid"])}"' if label_for else ""
        tag = "label" if label_for else "div"
        label = (
            f"<{tag}{hidden}{target}>{md(p['label'], inline=True)}"
            f"{help_html(p.get('help'))}</{tag}>"
        )
    elif p.get("help"):
        label = f'<div class="label">{help_html(p.get("help"))}</div>'
    disabled = " is-disabled" if p.get("disabled") else ""
    return f'<div class="{" ".join(classes)}{disabled}">{label}{inner}</div>'


def _format_number(fmt, value) -> str:
    if fmt:
        try:
            return fmt % value
        except (TypeError, ValueError):
            pass
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


# -----------------------------------------------------------------------------
# 元素渲染
# -----------------------------------------------------------------------------


def render_children(node: rt.Node) -> str:
    return "".join(render_node(child) for child in node.children)


def render_node(node: rt.Node) -> str:
    renderer = _RENDERERS.get(node.kind)
    if renderer is None:
        return ""
    return renderer(node)


def _root(node):
    return render_children(node)


def _container(node):
    p = node.props
    classes = ["box"]
    style = []
    if p.get("border"):
        classes.append("card")
    if p.get("horizontal"):
        classes.append("row")
        classes.append(f"justify-{p.get('horizontal_alignment') or 'left'}")
        classes.append(f"align-{p.get('vertical_alignment') or 'top'}")
    if p.get("gap"):
        classes.append(f"gap-{p['gap']}")
    if p.get("width") == "content":
        classes.append("w-content")
    height = p.get("height")
    if isinstance(height, (int, float)):
        classes.append("scroll")
        style.append(f"max-height:{int(height)}px")
    element_id = None
    if p.get("key"):
        classes.append(f"st-key-{p['key']}")
        element_id = f"k-{p['key']}"
    return (
        f'<div {_attrs(id=element_id, class_=" ".join(classes), style=";".join(style) or None)}>'
        f"{render_children(node)}</div>"
    )


def _columns(node):
    p = node.props
    template = " ".join(f"minmax(0,{weight:g}fr)" for weight in p["weights"])
    classes = f"cols gap-{p.get('gap') or 'small'} valign-{p.get('vertical_alignment') or 'top'}"
    return (
        f'<div class="{classes}" style="--cols:{escape(template)}">{render_children(node)}</div>'
    )


def _column(node):
    border = " card" if node.props.get("border") else ""
    return f'<div class="col{border}">{render_children(node)}</div>'


def _expander(node):
    p = node.props
    return (
        f'<details class="expander" {_attrs(id=p["eid"], open=p.get("expanded"))}>'
        f'<summary><span class="summary-label">{md(p["label"], inline=True)}</span>'
        '<span class="msym chevron" aria-hidden="true">expand_more</span></summary>'
        f'<div class="expander-body">{render_children(node)}</div></details>'
    )


def _popover(node):
    p = node.props
    return (
        f'<details class="popover" id="{escape(p["eid"])}">'
        f'<summary class="btn btn-secondary">{md(p["label"], inline=True)}'
        '<span class="msym chevron" aria-hidden="true">expand_more</span></summary>'
        f'<div class="popover-panel">{render_children(node)}</div></details>'
    )


def _tabs(node):
    p = node.props
    buttons = []
    for index, label in enumerate(p["labels"]):
        selected = index == p["selected"]
        buttons.append(
            f'<button type="button" role="tab" class="tab{" is-active" if selected else ""}" '
            f'aria-selected="{"true" if selected else "false"}" data-tab-index="{index}" '
            f'data-tab-input="{escape(p["wid"])}">{md(label, inline=True)}</button>'
        )
    hidden = (
        f'<input type="hidden" {_attrs(id=p["wid"], name=p["name"], form=p["form"])} '
        f'value="{p["selected"]}">'
    )
    return (
        f'<div class="tabs st-key-{escape(p["key"])}">{hidden}'
        f'<div class="tab-bar" role="tablist">{"".join(buttons)}</div>'
        f"{render_children(node)}</div>"
    )


def _tab(node):
    hidden = "" if node.props.get("selected") else " hidden"
    return f'<div class="tab-panel" role="tabpanel"{hidden}>{render_children(node)}</div>'


def _form(node):
    border = " card" if node.props.get("border") else ""
    return f'<div class="st-form{border}">{render_children(node)}</div>'


def fragment_form(fid, form_id, run_every, *, url=None, target=None, indicator=True) -> str:
    trigger = "mpt"
    if run_every:
        trigger += f", every {max(run_every, 0.3):g}s [document.visibilityState === 'visible']"
    return (
        f'<form {_attrs(id=form_id, hx_post=url or f"/fragment/{fid}", hx_trigger=trigger, hx_target=target or f"#frag-{fid}", hx_swap="morph:{ignoreActiveValue:true}", hx_sync="this:queue all", hx_indicator="#busy" if indicator and not run_every else None, class_="hidden-form")}>'
        "</form>"
    )


def _fragment(node):
    p = node.props
    form = fragment_form(p["fid"], p["form_id"], p.get("run_every"))
    return (
        f'<div class="fragment" id="frag-{escape(p["fid"])}">{form}{render_children(node)}</div>'
    )


def _dialog(node):
    p = node.props
    close = ""
    if p.get("dismissible", True):
        close = (
            f'<button type="button" class="icon-btn modal-close" aria-label="Close" '
            f'hx-post="/dialog/{escape(p["fid"])}/dismiss" hx-target="#app" '
            f'hx-swap="morph:{{ignoreActiveValue:true}}" hx-indicator="#busy">'
            '<span class="msym" aria-hidden="true">close</span></button>'
        )
    width = escape(str(p.get("width") or "small"))
    return (
        f'<div class="modal-root" id="dlg-{escape(p["fid"])}" data-dismiss-url="/dialog/{escape(p["fid"])}/dismiss">'
        '<div class="modal-backdrop"></div>'
        f'<div class="modal modal-{width}" role="dialog" aria-modal="true" aria-label="{escape(p["title"])}">'
        f'<header class="modal-header"><h2>{md(p["title"], inline=True)}</h2>{close}</header>'
        f'<div class="modal-body">{render_children(node)}</div></div></div>'
    )


def _markdown(node):
    p = node.props
    width = " w-content" if p.get("width") == "content" else ""
    body = md(p["body"], allow_html=p.get("html"))
    if p.get("help"):
        body = (
            f'<div class="md-with-help">{md(p["body"], allow_html=p.get("html"), inline=True)}'
            f"{help_html(p['help'])}</div>"
        )
    return f'<div class="md{width}">{body}</div>'


def _caption(node):
    p = node.props
    return f'<div class="md caption">{md(p["body"], allow_html=p.get("html"))}{help_html(p.get("help"))}</div>'


def _code(node):
    p = node.props
    wrap = " wrap" if p.get("wrap") else ""
    return (
        f'<div class="code{wrap}"><button type="button" class="icon-btn copy-btn" '
        'data-copy aria-label="Copy"><span class="msym" aria-hidden="true">content_copy</span>'
        f'</button><pre><code>{escape(p["body"])}</code></pre></div>'
    )


def _alert(node):
    p = node.props
    level = p["level"]
    symbol = icon(p["icon"]) if p.get("icon") else (
        f'<span class="msym" aria-hidden="true">{_ALERT_ICONS[level]}</span>'
    )
    return (
        f'<div class="alert alert-{level}" role="{"alert" if level == "error" else "status"}">'
        f'{symbol}<div class="md">{md(p["body"])}</div></div>'
    )


def _divider(_node):
    return "<hr>"


def _metric(node):
    p = node.props
    return (
        f'<div class="metric"><div class="metric-label">{md(p["label"], inline=True)}'
        f'{help_html(p.get("help"))}</div><div class="metric-value">{escape(p["value"])}</div></div>'
    )


def _progress(node):
    p = node.props
    text = f'<div class="caption">{md(p["text"], inline=True)}</div>' if p.get("text") else ""
    return (
        f'<div class="progress-wrap">{text}<div class="progress" role="progressbar" '
        f'aria-valuenow="{p["value"]:.0f}" aria-valuemin="0" aria-valuemax="100">'
        f'<div class="progress-bar" style="width:{p["value"]:.1f}%"></div></div></div>'
    )


def _audio(node):
    p = node.props
    return (
        f'<audio class="media" {_attrs(controls=True, src=p["src"], autoplay=p.get("autoplay"), loop=p.get("loop"), preload="metadata")}></audio>'
    )


def _video(node):
    p = node.props
    return (
        f'<video class="media" {_attrs(controls=True, src=p["src"], autoplay=p.get("autoplay"), loop=p.get("loop"), muted=p.get("muted"), preload="metadata", playsinline=True)}></video>'
    )


def _exception(node):
    p = node.props
    return (
        '<div class="alert alert-error" role="alert"><span class="msym" aria-hidden="true">error</span>'
        f'<div><strong>{escape(p["message"])}</strong><details><summary>Traceback</summary>'
        f'<pre>{escape(p["trace"])}</pre></details></div></div>'
    )


# -- 控件 ---------------------------------------------------------------------


def _text_input(node):
    p = node.props
    input_type = "password" if p.get("input_type") == "password" else "text"
    field = (
        f'<input {_field_attrs(p, type=input_type, value=p["value"], placeholder=p.get("placeholder"), maxlength=p.get("max_chars"), autocomplete="off" if input_type == "password" else None, spellcheck="false" if input_type == "password" else None)}>'
    )
    if input_type == "password":
        field = (
            f'<div class="input-group">{field}<button type="button" class="icon-btn reveal" '
            f'data-reveal="{escape(p["wid"])}" aria-label="Show"><span class="msym" '
            'aria-hidden="true">preview</span></button></div>'
        )
    return _wrap(p, "text_input", field)


def _text_area(node):
    p = node.props
    height = p.get("height")
    style = f"height:{int(height)}px" if isinstance(height, (int, float)) else None
    field = (
        f'<textarea {_field_attrs(p, placeholder=p.get("placeholder"), maxlength=p.get("max_chars"), style=style)}>'
        f'{escape(p["value"])}</textarea>'
    )
    if p.get("max_chars"):
        field += f'<div class="counter" data-counter="{escape(p["wid"])}">{len(p["value"])}/{p["max_chars"]}</div>'
    return _wrap(p, "text_area", field)


def _options_html(labels, selected, allow_none, placeholder):
    options = []
    if allow_none or selected is None:
        options.append(
            f'<option value=""{" selected" if selected is None else ""}>'
            f"{escape(placeholder or 'Choose an option')}</option>"
        )
    for index, label in enumerate(labels):
        options.append(
            f'<option value="{index}"{" selected" if index == selected else ""}>{escape(label)}</option>'
        )
    return "".join(options)


def _selectbox(node):
    p = node.props
    style = f"max-width:{int(p['width'])}px" if isinstance(p.get("width"), int) else None
    if p.get("accept_new"):
        current = p["labels"][p["selected"]] if p["selected"] is not None else p.get("custom_label", "")
        options = "".join(f'<option value="{escape(label)}"></option>' for label in p["labels"])
        field = (
            f'<input {_field_attrs(p, type="text", value=current, list=p["wid"] + "-list", autocomplete="off", style=style)}>'
            f'<datalist id="{escape(p["wid"])}-list">{options}</datalist>'
        )
    else:
        field = (
            f'<div class="select"><select {_field_attrs(p, style=style)}>'
            f'{_options_html(p["labels"], p["selected"], p.get("allow_none"), p.get("placeholder"))}'
            "</select></div>"
        )
    return _wrap(p, "selectbox", field)


def _grouped_select(node):
    p = node.props
    groups = []
    for group_label, options in p["groups"]:
        items = "".join(
            f'<option value="{index}"{" selected" if index == p["selected"] else ""}>{escape(label)}</option>'
            for index, label in options
        )
        groups.append(f'<optgroup label="{escape(group_label)}">{items}</optgroup>')
    settings = ""
    if p.get("settings_label"):
        settings = (
            f'<button type="button" class="link-btn" data-action="{escape(p["settings_key"])}" '
            f'form="{escape(p["form"])}">{escape(p["settings_label"])}</button>'
        )
    return (
        f'<div class="w w-selectbox st-key-{escape(p["key"])}"><div class="label-row">'
        f'<label class="label" for="{escape(p["wid"])}">{escape(p["label"])}</label>{settings}</div>'
        f'<div class="select"><select {_field_attrs(p)}>{"".join(groups)}</select></div></div>'
    )


def _radio_group(p, kind):
    items = []
    for index, label in enumerate(p["labels"]):
        checked = " checked" if index == p["selected"] else ""
        item_id = f"{p['wid']}-{index}"
        items.append(
            f'<label class="choice" for="{escape(item_id)}"><input type="radio" '
            f'{_attrs(id=item_id, name=p["name"], form=p["form"], value=index, data_w="1", data_defer="1" if p.get("defer") else None, disabled=p.get("disabled"))}{checked}>'
            f"<span>{md(label, inline=True)}</span></label>"
        )
    layout = " horizontal" if p.get("horizontal") or kind == "segmented_control" else ""
    return _wrap(
        p, kind, f'<div class="choices {kind}{layout}" role="radiogroup">{"".join(items)}</div>',
        label_for=False,
    )


def _radio(node):
    return _radio_group(node.props, "radio")


def _segmented_control(node):
    return _radio_group(node.props, "segmented_control")


def _multiselect(node):
    p = node.props
    marker = f'<input type="hidden" {_attrs(name=p["name"], form=p["form"], value="__present__", disabled=p.get("disabled"))}>'
    items = []
    for index, label in enumerate(p["labels"]):
        checked = " checked" if index in p["selected"] else ""
        item_id = f"{p['wid']}-{index}"
        items.append(
            f'<label class="chip" for="{escape(item_id)}"><input type="checkbox" '
            f'{_attrs(id=item_id, name=p["name"], form=p["form"], value=index, data_w="1", disabled=p.get("disabled"))}{checked}>'
            f"<span>{escape(label)}</span></label>"
        )
    return _wrap(p, "multiselect", f'<div class="chips">{marker}{"".join(items)}</div>', label_for=False)


def _bool_widget(node, kind):
    p = node.props
    hidden = f'<input type="hidden" {_attrs(name=p["name"], form=p["form"], value="0", disabled=p.get("disabled"))}>'
    checked = " checked" if p["value"] else ""
    control = f'<input type="checkbox" {_field_attrs(p, value="1", role="switch" if kind == "toggle" else None)}{checked}>'
    track = '<span class="switch-track" aria-hidden="true"></span>' if kind == "toggle" else ""
    classes = [f"w w-{kind}"]
    if p.get("user_key"):
        classes.append(f"st-key-{p['key']}")
    if p.get("disabled"):
        classes.append("is-disabled")
    return (
        f'<div class="{" ".join(classes)}">{hidden}<label class="check" for="{escape(p["wid"])}">'
        f"{control}{track}<span>{md(p['label'], inline=True)}</span></label>{help_html(p.get('help'))}</div>"
    )


def _checkbox(node):
    return _bool_widget(node, "checkbox")


def _toggle(node):
    return _bool_widget(node, "toggle")


def _slider(node):
    p = node.props
    value_text = _format_number(p.get("format"), p["value"])
    field = (
        f'<div class="slider"><input {_field_attrs(p, type="range", min=p["min_value"], max=p["max_value"], step=p["step"], value=p["value"], data_format=p.get("format"))}>'
        f'<output for="{escape(p["wid"])}">{escape(value_text)}</output></div>'
    )
    return _wrap(p, "slider", field)


def _number_input(node):
    p = node.props
    field = (
        f'<input {_field_attrs(p, type="number", min=p.get("min_value"), max=p.get("max_value"), step=p.get("step"), value=p["value"], placeholder=p.get("placeholder"))}>'
    )
    return _wrap(p, "number_input", field)


def _color_picker(node):
    p = node.props
    field = (
        f'<div class="color"><input {_field_attrs(p, type="color", value=p["value"])}>'
        f'<code>{escape(p["value"].upper())}</code></div>'
    )
    return _wrap(p, "color_picker", field)


def _file_uploader(node):
    p = node.props
    files = []
    for file_id, name, size in p["files"]:
        size_text = f"{size / 1024 / 1024:.1f} MB" if size >= 1024 * 1024 else f"{size / 1024:.0f} KB"
        files.append(
            f'<li><span class="msym" aria-hidden="true">article</span><span class="file-name">'
            f'{escape(name)}</span><span class="file-size">{size_text}</span>'
            f'<button type="button" class="icon-btn" data-upload-remove="{escape(file_id)}" '
            f'data-upload-key="{escape(p["key"])}" data-upload-form="{escape(p["form"])}" '
            f'aria-label="Remove"{" disabled" if p.get("disabled") else ""}>'
            '<span class="msym" aria-hidden="true">close</span></button></li>'
        )
    unique_types = sorted({item.lower() for item in p["accept"].split(",") if item}) if p.get("accept") else []
    accept_text = ", ".join(unique_types)
    hint = " · ".join(item for item in (accept_text, f"≤ {p['max_mb']} MB") if item)
    field = (
        f'<label class="dropzone" for="{escape(p["wid"])}">'
        f'<span class="msym" aria-hidden="true">upload</span><span class="dz-text">'
        f'<strong>Drop files here</strong> or click to browse<small>{escape(hint)}</small></span>'
        f'<input {_attrs(id=p["wid"], type="file", accept=p.get("accept") or None, multiple=p.get("multiple"), disabled=p.get("disabled"), data_upload=p["key"], data_upload_form=p["form"], data_max_bytes=p["max_mb"] * 1024 * 1024)}>'
        "</label>"
    )
    if files:
        field += f'<ul class="files">{"".join(files)}</ul>'
    return _wrap(p, "file_uploader", field, label_for=False)


def _button(node):
    p = node.props
    classes = ["btn", f"btn-{p.get('variant') or 'secondary'}"]
    if p.get("full"):
        classes.append("btn-full")
    if p.get("user_key"):
        classes.append(f"st-key-{p['key']}")
    return (
        f'<button type="button" {_attrs(class_=" ".join(classes), data_action=p["key"], form=p["form"], disabled=p.get("disabled"), title=plain(p["help"]) if p.get("help") else None)}>'
        f"{icon(p.get('icon'))}<span>{md(p['label'], inline=True)}</span></button>"
    )


def _link_button(node):
    p = node.props
    classes = ["btn", f"btn-{p.get('variant') or 'secondary'}"]
    if p.get("full"):
        classes.append("btn-full")
    if p.get("disabled"):
        classes.append("is-disabled")
    if p.get("user_key"):
        classes.append(f"st-key-{p['key']}")
    attrs = _attrs(
        class_=" ".join(classes),
        href=None if p.get("disabled") else p["href"],
        download=p.get("download"),
        target="_blank" if p.get("new_tab") else None,
        rel="noopener noreferrer" if p.get("new_tab") else None,
        data_action=p.get("action"),
        data_link="1" if p.get("action") else None,
        form=p["form"] if p.get("action") else None,
        title=plain(p["help"]) if p.get("help") else None,
    )
    return f"<a {attrs}>{icon(p.get('icon'))}<span>{md(p['label'], inline=True)}</span></a>"


_RENDERERS = {
    "root": _root,
    "container": _container,
    "columns": _columns,
    "column": _column,
    "expander": _expander,
    "popover": _popover,
    "tabs": _tabs,
    "tab": _tab,
    "form": _form,
    "fragment": _fragment,
    "dialog": _dialog,
    "markdown": _markdown,
    "caption": _caption,
    "code": _code,
    "alert": _alert,
    "divider": _divider,
    "metric": _metric,
    "progress": _progress,
    "audio": _audio,
    "video": _video,
    "exception": _exception,
    "text_input": _text_input,
    "text_area": _text_area,
    "selectbox": _selectbox,
    "grouped_select": _grouped_select,
    "radio": _radio,
    "segmented_control": _segmented_control,
    "multiselect": _multiselect,
    "checkbox": _checkbox,
    "toggle": _toggle,
    "slider": _slider,
    "number_input": _number_input,
    "color_picker": _color_picker,
    "file_uploader": _file_uploader,
    "button": _button,
    "link_button": _link_button,
}


# -----------------------------------------------------------------------------
# 页面级输出
# -----------------------------------------------------------------------------


def toasts_html(run: rt.Run) -> str:
    """Toasts of the initial page load; later toasts travel in HX-Trigger."""
    items = "".join(
        f'<div class="toast" role="status">{icon(symbol) if symbol else ""}'
        f'<div class="md">{md(text, inline=True)}</div></div>'
        for text, symbol in run.toasts
    )
    return f'<div id="toasts">{items}</div>'


def toast_headers(toasts) -> dict:
    """``HX-Trigger`` header that makes app.js show the given toasts."""
    if not toasts:
        return {}
    items = [{"text": plain(text), "icon": symbol or ""} for text, symbol in toasts]
    # ensure_ascii 保证中文等内容可以安全放入 HTTP 头。
    return {"HX-Trigger": json.dumps({"mpt-toasts": {"items": items}}, ensure_ascii=True)}


def app_html(run: rt.Run) -> str:
    main_form = fragment_form(
        "main", "f-main", None, url="/run", target="#app", indicator=True
    )
    scripts = escape(json.dumps(run.scripts, ensure_ascii=False))
    return (
        f'<div id="app" data-title="{escape(run.page_title)}" data-scripts="{scripts}">'
        f'<main class="page">{render_children(run.root)}</main>'
        f'<div id="dialogs">{"".join(render_node(node) for node in run.dialogs)}</div>'
        f"{main_form}</div>"
    )


def fragment_html(run: rt.Run) -> str:
    node = getattr(run, "fragment_node", None)
    return render_node(node) if node is not None else ""
