"""
Scene Sync: choose one stock clip per narrated scene, by meaning.

The default material flow searches a handful of keywords for the whole video
and fills the timeline in fixed-length pieces, so footage often has nothing to
do with the sentence being spoken. Scene Sync instead:

1. splits the narration into scenes using the real speech timings;
2. asks the LLM for one concrete visual search query per scene;
3. searches the stock provider for each query;
4. ranks every candidate's preview image against the scene with CLIP;
5. picks the best unused clip per scene and cuts it to the scene's length.

Every step degrades gracefully: without word timings the scenes are estimated
from text length, without the LLM the video keywords are reused, and without
CLIP the provider's own ranking is kept.
"""

import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from loguru import logger

from app.config import config
from app.models.schema import VideoParams
from app.services import clip_ranker, llm, material, task_artifacts, voice
from app.utils import utils
from app.utils.subtitle_reader import moviepy_subtitles

SUPPORTED_SOURCES = frozenset({"pexels", "pixabay", "coverr"})
# 短于该时长的句子并入相邻场景，避免一闪而过的画面。
MIN_SCENE_SECONDS = 1.8
CANDIDATES_PER_QUERY = 12
# 选片打分：CLIP 对视觉描述（检索词）更敏感，句子原文补充上下文；
# 视频主题作为锚点，避免只匹配某个词（如 "blue"）却与主题无关的抽象画面。
QUERY_WEIGHT = 0.4
SENTENCE_WEIGHT = 0.25
SUBJECT_WEIGHT = 0.35
REUSE_PENALTY = 0.06
OTHER_VIDEO_PENALTY = 0.08
OTHER_QUERY_PENALTY = 0.015
SHORT_CLIP_PENALTY = 0.03


@dataclass
class Shot:
    start: float
    end: float
    text: str
    query: str = ""
    scene_index: int = 0

    @property
    def duration(self) -> float:
        return max(0.1, self.end - self.start)


@dataclass
class Candidate:
    item: material.MaterialInfo
    query: str
    preview: str | None
    embedding: object = None
    uses: int = 0
    used_in_videos: set = field(default_factory=set)


def is_supported(params: VideoParams) -> bool:
    return bool(getattr(params, "scene_sync", False)) and params.video_source in SUPPORTED_SOURCES


# -----------------------------------------------------------------------------
# 1. 场景划分
# -----------------------------------------------------------------------------


def _sentence_timings(task_id, video_script, sub_maker, subtitle_path, audio_duration):
    """Return [(start, end, text)] for each spoken sentence."""
    if sub_maker is not None:
        scene_srt = os.path.join(utils.task_dir(task_id), "scenes.srt")
        try:
            voice.create_subtitle(
                sub_maker=sub_maker, text=video_script, subtitle_file=scene_srt, word_level=False
            )
            if os.path.isfile(scene_srt):
                cues = moviepy_subtitles(scene_srt)
                if cues:
                    return [(float(s), float(e), str(t)) for (s, e), t in cues]
        except Exception as exc:  # noqa: BLE001 - fall through to other timing sources
            logger.warning(f"scene sync: could not build sentence timings from TTS: {exc}")

    if subtitle_path and os.path.isfile(subtitle_path):
        try:
            cues = moviepy_subtitles(subtitle_path)
            # 逐词字幕的单条太短，只有句级字幕才适合作为场景时间轴。
            if cues and sum(len(str(t).split()) for _, t in cues) / len(cues) > 2:
                return [(float(s), float(e), str(t)) for (s, e), t in cues]
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"scene sync: could not read subtitle timings: {exc}")

    # 没有任何时间轴时按字数比例估算，至少保证画面顺序跟随文案。
    sentences = [
        line for line in utils.split_string_by_punctuations(video_script or "")
        if re.sub(r"[_\W]+", "", line)
    ]
    total_chars = sum(len(line) for line in sentences) or 1
    timings, cursor = [], 0.0
    for line in sentences:
        length = audio_duration * len(line) / total_chars
        timings.append((cursor, cursor + length, line))
        cursor += length
    return timings


def build_shots(timings, audio_duration: float, target_shot_seconds: float) -> list[Shot]:
    """Turn sentence timings into contiguous shots that cover the narration."""
    if not timings:
        return [Shot(0.0, audio_duration, "")]

    # 场景首尾相接：每段延伸到下一句开始，最后一段延伸到音频结尾。
    scenes = []
    for index, (start, _end, text) in enumerate(timings):
        scene_start = 0.0 if index == 0 else start
        scene_end = timings[index + 1][0] if index + 1 < len(timings) else audio_duration
        scenes.append([scene_start, max(scene_end, scene_start + 0.1), text])

    merged = []
    for scene in scenes:
        if merged and (scene[1] - scene[0] < MIN_SCENE_SECONDS
                       or merged[-1][1] - merged[-1][0] < MIN_SCENE_SECONDS):
            merged[-1][1] = scene[1]
            merged[-1][2] = f"{merged[-1][2]} {scene[2]}".strip()
        else:
            merged.append(scene)

    # 长句拆成多个镜头，保持短视频的剪辑节奏；同一句的镜头共用检索词。
    max_shot = max(2.5, float(target_shot_seconds)) * 1.6
    shots = []
    for scene_index, (start, end, text) in enumerate(merged):
        parts = max(1, math.ceil((end - start) / max_shot))
        step = (end - start) / parts
        for part in range(parts):
            shots.append(Shot(start + part * step, start + (part + 1) * step, text,
                              scene_index=scene_index))
    return shots


# -----------------------------------------------------------------------------
# 2. 每个场景的视觉检索词
# -----------------------------------------------------------------------------


def _fallback_anchor(video_subject, fallback_terms) -> str:
    for term in list(fallback_terms or []) + [video_subject or ""]:
        words = [word for word in re.findall(r"[A-Za-z][A-Za-z'-]*", str(term))]
        if words:
            return " ".join(words[:2]).lower()
    return ""


def _scene_queries(video_subject, scene_texts, fallback_terms) -> tuple[str, list[str]]:
    """Return (anchor, queries): the core visual subject and one query per scene."""
    numbered = "\n".join(f"{i + 1}. {text}" for i, text in enumerate(scene_texts))
    prompt = f"""
# Role: Stock Footage Researcher

## Goal
For each numbered narration line of a short vertical video, write ONE search query
for a stock video library (Pexels / Pixabay) that would find footage showing it.

## Rules
1. Return ONLY a JSON object: {{"anchor": "...", "queries": [...]}}.
2. "anchor" is the core visual subject of the whole video in 1-2 English words
   (for example "octopus", "coffee", "mountain"). It must be something a camera can film.
3. "queries" has exactly {len(scene_texts)} strings, in the same order as the lines.
4. Each query is 2-4 English words naming something concrete and filmable and should
   include the anchor unless the line is clearly about something else
   (for example "octopus changing color", "octopus swimming reef").
5. Never use abstract words (fact, surprising, amazing, history, concept, process).

## Video subject
{video_subject}

## Narration lines
{numbered}
""".strip()
    for attempt in range(2):
        try:
            response = llm._generate_response(prompt)
            if response.startswith("Error: "):
                raise ValueError(response)
            try:
                payload = json.loads(llm._strip_code_fence(response))
            except ValueError:
                payload = llm._extract_embedded_json(response, "{")
            queries = payload.get("queries") if isinstance(payload, dict) else None
            anchor = str(payload.get("anchor") or "").strip().lower()[:40] if isinstance(payload, dict) else ""
            if isinstance(queries, list) and len(queries) == len(scene_texts):
                cleaned = [str(query).strip()[:60] for query in queries]
                if all(cleaned):
                    return anchor or _fallback_anchor(video_subject, fallback_terms), cleaned
            logger.warning(f"scene sync: query list has the wrong shape (attempt {attempt + 1})")
        except Exception as exc:  # noqa: BLE001 - fall back to the video keywords
            logger.warning(f"scene sync: query generation failed (attempt {attempt + 1}): {exc}")

    terms = [str(term).strip() for term in fallback_terms or [] if str(term).strip()]
    terms = terms or [str(video_subject or "nature").strip()]
    logger.info("scene sync: using video keywords as scene queries")
    return (
        _fallback_anchor(video_subject, fallback_terms),
        [terms[i % len(terms)] for i in range(len(scene_texts))],
    )


# -----------------------------------------------------------------------------
# 3-5. 检索、排序、选片
# -----------------------------------------------------------------------------


def _search_function(source):
    return {
        "pexels": material.search_videos_pexels,
        "pixabay": material.search_videos_pixabay,
        "coverr": material.search_videos_coverr,
    }[source]


def _search(source, query, minimum_duration, video_aspect):
    try:
        return material._search_videos_with_cache(
            provider=source,
            search_videos=_search_function(source),
            search_term=query,
            minimum_duration=minimum_duration,
            video_aspect=video_aspect,
        )[:CANDIDATES_PER_QUERY]
    except Exception as exc:  # noqa: BLE001 - an empty query pool is handled below
        logger.warning(f"scene sync: search failed for {query!r}: {exc}")
        return []


def _anchored(query: str, anchor: str) -> str | None:
    """The query with the video's core subject added, when it is missing."""
    if not anchor or anchor.lower() in query.lower():
        return None
    return " ".join(f"{anchor} {query}".split()[:4])


def _gather_candidates(params, queries, shots, fallback_terms, anchor) -> dict[str, list[Candidate]]:
    longest = {}
    for shot in shots:
        longest[shot.query] = max(longest.get(shot.query, 0.0), shot.duration)

    # 每个检索词再搜一次“主题 + 检索词”，保证候选池里总有切题的素材；
    # 由 CLIP 在两者之间取舍。两次检索的结果归入同一个场景候选池。
    searches = []
    for query in dict.fromkeys(queries):
        searches.append((query, query))
        anchored = _anchored(query, anchor)
        if anchored:
            searches.append((query, anchored))
    if anchor:
        searches.append((anchor, anchor))

    def search_query(job):
        pool_key, query = job
        minimum = max(1, min(10, math.ceil(longest.get(pool_key, 3.0))))
        items = _search(params.video_source, query, minimum, params.video_aspect)
        if len(items) < 3:
            # 时长要求可能过滤掉了大部分结果；短素材会在合成时循环播放。
            items += _search(params.video_source, query, 1, params.video_aspect)
        return pool_key, items

    pools: dict[str, list[Candidate]] = {}
    seen_urls = set()
    with ThreadPoolExecutor(max_workers=4) as executor:
        for pool_key, items in executor.map(search_query, searches):
            pool = pools.setdefault(pool_key, [])
            for item in items:
                if item.url in seen_urls:
                    continue
                seen_urls.add(item.url)
                info = item.source_info if isinstance(item.source_info, dict) else {}
                pool.append(Candidate(item=item, query=pool_key, preview=info.get("preview_image")))

    if not any(pools.values()):
        # 所有场景检索都失败时，退回整条视频的关键词，保证仍能出片。
        for term in fallback_terms or []:
            _, items = search_query((term, term))
            pools.setdefault(term, []).extend(
                Candidate(item=item, query=term,
                          preview=(item.source_info or {}).get("preview_image"))
                for item in items if item.url not in seen_urls
            )
    return pools


def _rank(shots, pools, anchor):
    """Attach CLIP embeddings; return text embeddings for scoring, or None."""
    ranker = clip_ranker.get_ranker()
    candidates = [candidate for pool in pools.values() for candidate in pool]
    if ranker is None or not candidates:
        return None
    images = clip_ranker.fetch_preview_images(
        [candidate.preview for candidate in candidates], proxies=config.proxy
    )
    with_images = [candidate for candidate in candidates if candidate.preview in images]
    if not with_images:
        logger.warning("scene sync: no preview images available; keeping search order")
        return None
    embeddings = ranker.embed_images([images[candidate.preview] for candidate in with_images])
    for candidate, embedding in zip(with_images, embeddings):
        candidate.embedding = embedding
    query_embeds = ranker.embed_texts([shot.query for shot in shots])
    sentence_embeds = ranker.embed_texts([shot.text or shot.query for shot in shots])
    subject_embed = ranker.embed_texts([f"a photo of {anchor}" if anchor else shots[0].query])[0]
    logger.info(f"scene sync: ranked {len(with_images)} candidates with CLIP")
    return query_embeds, sentence_embeds, subject_embed


def _choose(shots, pools, text_embeds, video_index):
    plan = []
    previous = None
    for shot_index, shot in enumerate(shots):
        best, best_score = None, -math.inf
        for query, pool in pools.items():
            for order, candidate in enumerate(pool):
                if candidate is previous:
                    continue
                if text_embeds is not None and candidate.embedding is not None:
                    query_embeds, sentence_embeds, subject_embed = text_embeds
                    score = (QUERY_WEIGHT * float(query_embeds[shot_index] @ candidate.embedding)
                             + SENTENCE_WEIGHT * float(sentence_embeds[shot_index] @ candidate.embedding)
                             + SUBJECT_WEIGHT * float(subject_embed @ candidate.embedding))
                else:
                    # 没有 CLIP 分数时保留检索结果自身的排序。
                    score = -0.01 * order - (0 if query == shot.query else 1)
                if query != shot.query:
                    score -= OTHER_QUERY_PENALTY
                score -= REUSE_PENALTY * candidate.uses
                if candidate.used_in_videos - {video_index}:
                    score -= OTHER_VIDEO_PENALTY
                if candidate.item.duration < shot.duration:
                    score -= SHORT_CLIP_PENALTY
                if score > best_score:
                    best, best_score = candidate, score
        if best is None:
            continue
        best.uses += 1
        best.used_in_videos.add(video_index)
        previous = best
        plan.append((shot, best, best_score))
    for pool in pools.values():
        for candidate in pool:
            candidate.uses = 0
    return plan


def plan_scenes(
    task_id: str,
    params: VideoParams,
    video_script: str,
    video_terms,
    sub_maker,
    subtitle_path: str,
    audio_duration: float,
    progress_callback=None,
) -> list[list[tuple[str, float]]] | None:
    """
    Plan and download footage for every video of the task.

    Returns one ``[(clip_path, seconds), ...]`` list per video, or None when Scene
    Sync could not produce a usable plan (the caller then uses the default flow).
    """
    logger.info("\n\n## scene sync: planning footage per scene")
    timings = _sentence_timings(task_id, video_script, sub_maker, subtitle_path, audio_duration)
    shots = build_shots(timings, audio_duration, params.video_clip_duration)
    if not shots:
        return None
    scene_texts = []
    for shot in shots:
        if shot.scene_index == len(scene_texts):
            scene_texts.append(shot.text)
    if isinstance(video_terms, str):
        video_terms = [term.strip() for term in video_terms.split(",") if term.strip()]
    anchor, queries = _scene_queries(
        params.video_subject or video_script[:80], scene_texts, video_terms
    )
    for shot in shots:
        shot.query = queries[shot.scene_index]
    logger.info(
        f"scene sync: {len(scene_texts)} scenes, {len(shots)} shots, "
        f"anchor: {anchor!r}, queries: {queries}"
    )
    if progress_callback:
        progress_callback(0.2)

    pools = _gather_candidates(params, queries, shots, video_terms, anchor)
    if not any(pools.values()):
        logger.warning("scene sync: no stock candidates found")
        return None
    if progress_callback:
        progress_callback(0.4)
    text_embeds = _rank(shots, pools, anchor)
    if progress_callback:
        progress_callback(0.6)

    plans = [_choose(shots, pools, text_embeds, index) for index in range(params.video_count)]

    material_directory = config.app.get("material_directory", "").strip()
    if material_directory == "task":
        material_directory = utils.task_dir(task_id)
    elif material_directory and not os.path.isdir(material_directory):
        material_directory = ""
    chosen = {id(candidate): candidate for plan in plans for _, candidate, _ in plan}

    def download(candidate):
        try:
            return id(candidate), material.save_video(candidate.item.url, material_directory)
        except Exception as exc:  # noqa: BLE001 - a missing clip is replaced below
            logger.warning(f"scene sync: download failed: {exc}")
            return id(candidate), ""

    with ThreadPoolExecutor(max_workers=4) as executor:
        saved = dict(executor.map(download, chosen.values()))
    if progress_callback:
        progress_callback(1.0)

    results, records, report = [], [], []
    for video_index, plan in enumerate(plans):
        timeline = []
        for shot, candidate, score in plan:
            local_path = saved.get(id(candidate))
            if not local_path:
                # 下载失败的镜头沿用上一个镜头的画面，时间轴保持完整。
                if timeline:
                    timeline[-1] = (timeline[-1][0], timeline[-1][1] + shot.duration)
                continue
            timeline.append((local_path, shot.duration))
            if video_index == 0:
                records.append(material._material_source_record(candidate.item, local_path))
                report.append({
                    "start": round(shot.start, 2),
                    "end": round(shot.end, 2),
                    "text": shot.text,
                    "query": shot.query,
                    "local_file": os.path.basename(local_path),
                    "score": round(score, 4) if math.isfinite(score) else None,
                })
        if not timeline:
            return None
        results.append(timeline)

    material._persist_material_sources(task_id, records)
    task_artifacts.patch_script_data(task_id, scene_plan=report)
    logger.success(
        f"scene sync: planned {len(results)} video(s), {len(results[0])} shots in the first"
    )
    return results
