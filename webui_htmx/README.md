# HTMX WebUI

An HTMX front end for MoneyPrinterTurbo with the same features as the Streamlit
WebUI. Start it with `./webui-htmx.sh` (or `python -m webui_htmx`) and open
<http://localhost:8510>.

## How it works

There is one copy of the page logic: `webui/Main.py`. The HTMX WebUI runs that
file unchanged, so every feature, fix and new provider added to the Streamlit UI
shows up here automatically.

```
browser ──htmx POST──▶ FastAPI (app.py)
                         │  apply posted widget values to session_state,
                         │  run on_change / on_click callbacks
                         ▼
                       exec webui/Main.py  ──calls──▶ stshim ("streamlit")
                         │                             builds a node tree
                         ▼
                       render.py ──HTML──▶ idiomorph merges it into the page
```

| File | Role |
|---|---|
| `app.py` | FastAPI routes; registers `stshim` as `sys.modules["streamlit"]` |
| `runtime.py` | Per-tab sessions, runs, widget state, Streamlit-style widget cleanup |
| `stshim.py` | The Streamlit API subset used by `Main.py` (widgets, layout, fragments, dialogs, caches) |
| `tour_shim.py` | `streamlit_tour` replacement using driver.js |
| `render.py` | Node tree → HTML |
| `static/app.js` | Sends widget changes and button clicks to the server; uploads, tabs, toasts, tour |
| `static/app.css` | Light and dark themes, responsive layout |

Semantics match Streamlit:

- **Widgets** with a `key` read and write `st.session_state[key]`. Widgets that
  are not rendered in a full run lose their state.
- **Buttons** return `True` only in the run triggered by their click.
- **Fragments** (`@st.fragment(run_every=...)`) and **dialogs** (`@st.dialog`) rerun on
  their own via `/fragment/<id>`, polling when `run_every` is set.
  `st.rerun(scope="app")` inside a fragment re-renders the whole page.
- **Sessions:** each page load is a new session, as in a Streamlit tab. Settings
  persist in `config.toml`; the topic and script live only in the session.

Every widget has `form="<form id>"`. A change triggers the custom `mpt` event on
that form, and its `hx-post` sends all of the form's widgets. Responses are swapped
with `morph:{ignoreActiveValue:true}`, so the field you are typing in keeps its
focus and text.

## Notes

- The process is single-worker: tasks, sessions and the config live in memory.
- Run either this UI or the Streamlit UI, not both. Both rewrite `config.toml`.
- `DISPLAY`/`WAYLAND_DISPLAY` are cleared at startup so videos play in the
  browser rather than opening on the server desktop (WSLg). Set
  `MPT_HTMX_KEEP_DISPLAY=1` to keep the old behaviour.
- Vendored front-end libraries (no CDN needed): htmx 2.0.4, idiomorph 0.7.3,
  driver.js 1.3.1, and a subset of Material Symbols Rounded.
- Supporting a new Streamlit call in `Main.py` means adding it to `stshim.py`
  and, for new element types, a renderer in `render.py`.
