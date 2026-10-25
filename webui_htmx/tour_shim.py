"""``streamlit_tour`` replacement backed by driver.js in the HTMX page."""

from webui_htmx import runtime as rt


class _Step:
    def __init__(self, key, title, desc, side, align):
        self.key = key
        self.popover = {"title": title, "description": desc, "side": side, "align": align}


class Tour:
    def __init__(self, steps, key, show_progress=True, animate=True, overlay_opacity=0.5,
                 one_time_tour=True, **_kwargs):
        self.steps = list(steps)
        self.key = key
        self.show_progress = show_progress
        self.animate = animate
        self.overlay_opacity = overlay_opacity
        self.one_time_tour = one_time_tour

    @staticmethod
    def bind(key, title="", desc="", side="bottom", align="start", **_kwargs):
        return _Step(key, title, desc, side, align)

    def start(self):
        rt.current_run().scripts.append(
            {
                "type": "tour",
                "key": self.key,
                "once": self.one_time_tour,
                "showProgress": self.show_progress,
                "animate": self.animate,
                "overlayOpacity": self.overlay_opacity,
                "steps": [
                    {"element": f".st-key-{step.key}", "popover": dict(step.popover)}
                    for step in self.steps
                ],
            }
        )
