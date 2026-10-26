"""
The guided "Create video" flow and per-user video job tracking.

Regular users never touch the global configuration: the wizard turns a few
plain-language choices (topic, length, style, voice, format) into a complete
``VideoParams`` using the platform's configured providers, submits it to the
same generation queue as the Studio, and records the job for that user.
"""

import hashlib
import os
import re
from dataclasses import dataclass
from uuid import uuid4

from loguru import logger
from sqlalchemy import select

from app.config import config
from app.models import const
from app.models.schema import VideoParams
from app.services import llm, voice, webui_task
from app.services import state as sm
from app.utils import utils
from webui_htmx.saas import db

STOCK_SOURCES = {"pexels", "pixabay", "coverr"}

LANGUAGES = [
    ("", "Same as my topic"),
    ("en-US", "English"),
    ("hi-IN", "Hindi"),
    ("es-ES", "Spanish"),
    ("pt-BR", "Portuguese"),
    ("fr-FR", "French"),
    ("de-DE", "German"),
    ("it-IT", "Italian"),
    ("id-ID", "Indonesian"),
    ("ar-SA", "Arabic"),
    ("ja-JP", "Japanese"),
    ("ko-KR", "Korean"),
    ("zh-CN", "Chinese"),
]

# 篇幅对应文案段落数；时长为语速 1.0 时的大致范围。
LENGTHS = {
    "short": {"label": "Short", "hint": "about 20–30 seconds", "paragraphs": 1},
    "medium": {"label": "Medium", "hint": "about 40–50 seconds", "paragraphs": 2},
    "long": {"label": "Long", "hint": "about 60+ seconds", "paragraphs": 3},
}

# Edge 的 Multilingual 音色能朗读几乎所有语言，因此对所有语言提供同一组精选音色。
VOICES = {
    "andrew": {"label": "Andrew", "hint": "Warm male", "gender": "Male", "rank": 0,
               "name": "en-US-AndrewMultilingualNeural-Male"},
    "ava": {"label": "Ava", "hint": "Bright female", "gender": "Female", "rank": 0,
            "name": "en-US-AvaMultilingualNeural-Female"},
    "brian": {"label": "Brian", "hint": "Casual male", "gender": "Male", "rank": 1,
              "name": "en-US-BrianMultilingualNeural-Male"},
    "emma": {"label": "Emma", "hint": "Friendly female", "gender": "Female", "rank": 1,
             "name": "en-US-EmmaMultilingualNeural-Female"},
}


def resolve_voice(voice_key: str, language: str) -> str:
    """English uses the curated voices; other languages a native voice of the same gender."""
    spec = VOICES[voice_key]
    if not language or language.startswith("en-"):
        return spec["name"]
    try:
        native = [
            name for name in voice.get_all_azure_voices(filter_locals=None)
            if name.startswith(language) and "V2" not in name and name.endswith(spec["gender"])
        ]
    except Exception:  # noqa: BLE001 - fall back to the multilingual voice
        native = []
    if not native:
        return spec["name"]
    return native[min(spec["rank"], len(native) - 1)]

STYLES = {
    "viral": {
        "label": "Viral Shorts",
        "icon": "bolt",
        "hint": "Bold captions that light up word by word, fast pace, upbeat music.",
        "params": {
            "subtitle_display_mode": "karaoke",
            "subtitle_highlight_color": "#FFD60A",
            "subtitle_position": "custom",
            "custom_position": 68.0,
            "font_size": 78,
            "stroke_width": 3.0,
            "voice_rate": 1.1,
            "bgm_volume": 0.15,
            "video_clip_duration": 3,
        },
    },
    "documentary": {
        "label": "Documentary",
        "icon": "movie",
        "hint": "Calm narration, classic subtitles, longer shots.",
        "params": {
            "subtitle_display_mode": "sentence",
            "subtitle_position": "bottom",
            "font_size": 60,
            "stroke_width": 1.5,
            "voice_rate": 1.0,
            "bgm_volume": 0.1,
            "video_clip_duration": 5,
        },
    },
    "bigwords": {
        "label": "Big Words",
        "icon": "text_fields",
        "hint": "One big word at a time that pops in. Great for motivation and lists.",
        "params": {
            "subtitle_display_mode": "word_by_word",
            "subtitle_animation": "pop_spring",
            "subtitle_position": "center",
            "font_size": 96,
            "stroke_width": 3.0,
            "voice_rate": 1.05,
            "bgm_volume": 0.15,
            "video_clip_duration": 3,
        },
    },
}

FORMATS = {
    "9:16": {"label": "Vertical", "hint": "Shorts · Reels · TikTok"},
    "16:9": {"label": "Horizontal", "hint": "YouTube"},
}

EXAMPLE_TOPICS = [
    "5 surprising facts about octopuses",
    "How to save money in your 20s",
    "The history of chai in 60 seconds",
    "Why we dream, explained simply",
]

TERMINAL = {"complete", "failed"}


@dataclass
class Choices:
    topic: str
    language: str
    length: str
    style: str
    voice: str
    aspect: str

    @classmethod
    def from_form(cls, form) -> "Choices":
        def pick(name, allowed, default):
            value = str(form.get(name) or "")
            return value if value in allowed else default

        return cls(
            topic=str(form.get("topic") or "").strip()[:300],
            language=pick("language", {code for code, _ in LANGUAGES}, ""),
            length=pick("length", LENGTHS, "short"),
            style=pick("style", STYLES, "viral"),
            voice=pick("voice", VOICES, "andrew"),
            aspect=pick("aspect", FORMATS, "9:16"),
        )


_CJK = re.compile(r"[㐀-鿿぀-ヿ가-힯]")


def write_script(choices: Choices) -> tuple[str, list[str]]:
    """Generate the narration and search keywords with the platform's LLM."""
    language = choices.language
    for attempt in range(2):
        script = llm.generate_script(
            video_subject=choices.topic,
            language=language,
            paragraph_number=LENGTHS[choices.length]["paragraphs"],
        )
        if not script or script.startswith("Error: "):
            raise RuntimeError("The AI could not write a script right now. Please try again.")
        # 部分本地模型（如 qwen2.5）偶尔会中途切换成中文。主题本身没有中日韩
        # 文字时，出现这些字符就说明跑题了：指定英文重写一次。
        if attempt == 0 and not _CJK.search(choices.topic) and _CJK.search(script) and (
            language in ("", "en-US")
        ):
            logger.warning("script drifted into CJK text; regenerating in English")
            language = "en-US"
            continue
        break
    terms = llm.generate_terms(choices.topic, script, amount=5)
    return script.strip(), [term for term in terms if isinstance(term, str)]


def estimate_seconds(script: str, voice_rate: float = 1.0) -> int:
    words = len(re.findall(r"\w+", script or ""))
    cjk = len(re.findall(r"[㐀-鿿぀-ヿ가-힯]", script or ""))
    seconds = (words / 2.6 + cjk / 4.2) / max(voice_rate, 0.1)
    return max(1, round(seconds))


def voice_sample(voice_key: str) -> str:
    """Return a cached MP3 sample for a curated voice (generated once)."""
    spec = VOICES[voice_key]
    sample_dir = utils.storage_dir("voice_samples", create=True)
    path = os.path.join(sample_dir, f"{voice_key}.mp3")
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        text = f"Hi, I'm {spec['label']}. I'll narrate your next video."
        if not voice.tts(text=text, voice_name=spec["name"], voice_rate=1.0, voice_file=path):
            raise RuntimeError("Voice preview is unavailable right now.")
    return path


def _has_keys(source: str) -> bool:
    keys = config.app.get(f"{source}_api_keys") or []
    if isinstance(keys, str):
        keys = [keys]
    return any(str(key).strip() for key in keys)


def _platform_video_source() -> str:
    """The configured stock source if it has an API key, else any stock source that does."""
    configured = str(config.app.get("video_source", "") or "")
    candidates = [configured] if configured in STOCK_SOURCES else []
    candidates += [source for source in ("pixabay", "pexels", "coverr") if source not in candidates]
    for source in candidates:
        if _has_keys(source):
            return source
    raise RuntimeError("No stock footage source is set up. An admin needs to add a Pixabay or Pexels key.")


def build_params(choices: Choices, script: str, terms: list[str]) -> VideoParams:
    style = STYLES[choices.style]["params"]
    return VideoParams(
        video_subject=choices.topic,
        video_script=script,
        video_terms=terms or None,
        video_language=choices.language,
        video_aspect=choices.aspect,
        video_source=_platform_video_source(),
        scene_sync=True,
        video_count=1,
        voice_name=resolve_voice(choices.voice, choices.language),
        voice_volume=1.0,
        bgm_type="random",
        subtitle_enabled=True,
        font_name=str(config.ui.get("font_name") or "MicrosoftYaHeiBold.ttc"),
        text_fore_color="#FFFFFF",
        stroke_color="#000000",
        n_threads=2,
        **style,
    )


def submit(user_id: str, choices: Choices, script: str, terms: list[str]) -> str:
    task_id = str(uuid4())
    params = build_params(choices, script, terms)
    with db.session_scope() as session:
        session.add(db.VideoJob(
            id=task_id, user_id=user_id, topic=choices.topic, style=choices.style,
            language=choices.language, aspect=choices.aspect, status="queued",
        ))
    try:
        webui_task.submit_generation(task_id=task_id, params=params)
    except Exception:
        with db.session_scope() as session:
            job = session.get(db.VideoJob, task_id)
            if job:
                job.status, job.error = "failed", "Could not start the video. Please try again."
        raise
    logger.info(f"wizard video submitted: user={user_id} task={task_id} style={choices.style}")
    return task_id


def _final_video(task_id: str) -> str:
    task_path = utils.task_dir(task_id)
    candidate = os.path.join(task_path, "final-1.mp4")
    return candidate if os.path.isfile(candidate) else ""


def refresh(job: db.VideoJob) -> db.VideoJob:
    """Sync a job row with the generation state (call inside a session)."""
    if job.status in TERMINAL:
        return job
    task = None
    try:
        task = sm.state.get_task(job.id)
    except Exception as exc:  # noqa: BLE001 - state backend hiccups only delay the update
        logger.warning(f"could not read task state {job.id}: {exc}")
    if not task:
        if job.status != "queued":
            # 进程重启后内存中的任务状态会丢失：有成片则视为完成，否则视为中断。
            video_file = _final_video(job.id)
            job.status = "complete" if video_file else "failed"
            job.video_file = video_file
            job.error = "" if video_file else "Interrupted by a server restart. Please create it again."
            job.completed_at = db.utcnow()
        return job
    state = task.get("state")
    job.progress = max(0, min(100, int(task.get("progress", 0) or 0)))
    if state == const.TASK_STATE_COMPLETE:
        videos = [path for path in task.get("videos") or [] if os.path.isfile(path)]
        job.status = "complete" if videos else "failed"
        job.video_file = videos[0] if videos else ""
        job.error = "" if videos else "The video file was not created."
        duration = task.get("audio_duration")
        job.duration_seconds = float(duration) if isinstance(duration, (int, float)) else None
        job.completed_at = db.utcnow()
    elif state == const.TASK_STATE_FAILED:
        job.status = "failed"
        job.error = friendly_error(str(task.get("error") or ""))
        job.completed_at = db.utcnow()
    else:
        job.status = "processing" if job.progress > 0 else "queued"
    return job


def friendly_error(error: str) -> str:
    lowered = error.lower()
    if "material" in lowered or "footage" in lowered:
        return "We couldn't find enough footage for this topic. Try a more visual topic."
    if "audio" in lowered or "tts" in lowered or "voice" in lowered:
        return "The voice service is busy right now. Please try again in a few minutes."
    if "script" in lowered or "llm" in lowered:
        return "The AI could not write the script. Please try again."
    return "Something went wrong while making this video. Please try again."


def stage(job: db.VideoJob) -> tuple[str, str]:
    """A plain-language description of where the job is."""
    if job.status == "queued":
        return "schedule", "In the queue, you're up next ⏳"
    if job.status == "complete":
        return "task_alt", "It's ready! Go post it 🔥"
    if job.status == "failed":
        return "error", job.error or "Something went wrong."
    progress = job.progress
    if progress < 30:
        return "graphic_eq", "Warming up the mic 🎙️"
    if progress < 40:
        return "text_fields", "Adding captions 💬"
    if progress < 50:
        return "movie", "Finding footage for every line 🎞️"
    if progress < 95:
        return "auto_awesome", "Editing like a pro ✂️"
    return "progress_activity", "Final touches ✨"


def user_job(session, job_id: str, user: dict) -> db.VideoJob | None:
    job = session.get(db.VideoJob, job_id)
    if job is None:
        return None
    if job.user_id != user["id"] and user.get("role") != "admin":
        return None
    return job


def list_jobs(session, user: dict, limit: int = 50) -> list[db.VideoJob]:
    jobs = session.scalars(
        select(db.VideoJob).where(db.VideoJob.user_id == user["id"])
        .order_by(db.VideoJob.created_at.desc()).limit(limit)
    ).all()
    for job in jobs:
        refresh(job)
    return list(jobs)


def download_name(topic: str) -> str:
    safe = re.sub(r"[^\w\- ]+", " ", topic or "video", flags=re.UNICODE)
    safe = re.sub(r"\s+", " ", safe).strip()[:80] or "video"
    return f"{safe}.mp4"


def sample_etag(path: str) -> str:
    return hashlib.sha1(f"{path}:{os.path.getmtime(path)}".encode()).hexdigest()
