"""
Rank candidate images against text with CLIP, on CPU, without PyTorch.

Scene Sync uses this to pick, for every narrated sentence, the stock clip
whose preview image best matches what is being said. It runs OpenAI's
CLIP ViT-B/32 (MIT license) exported to ONNX and quantized (Xenova's export,
about 150 MB). The model is downloaded once into ``storage/models`` on first
use; onnxruntime and tokenizers are already dependencies of faster-whisper.
"""

import io
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests
from loguru import logger
from PIL import Image

from app.utils import utils

MODEL_REPO = "Xenova/clip-vit-base-patch32"
# 固定到具体提交，避免上游仓库变更后悄悄换成另一个模型。
MODEL_REVISION = "d15189d7028b43f1d3e65039190477f6af591c2a"
MODEL_FILES = {
    "text_model_quantized.onnx": "onnx/text_model_quantized.onnx",
    "vision_model_quantized.onnx": "onnx/vision_model_quantized.onnx",
    "tokenizer.json": "tokenizer.json",
}
CONTEXT_LENGTH = 77
# CLIP 的 <|endoftext|>；HF 的 CLIP 文本模型按 input_ids 最大值定位句尾，
# 用同一个 id 填充时 argmax 仍返回第一个句尾位置。
END_OF_TEXT_ID = 49407
IMAGE_SIZE = 224
IMAGE_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
IMAGE_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
PREVIEW_DOWNLOAD_TIMEOUT = (10, 20)
MAX_PREVIEW_BYTES = 5 * 1024 * 1024

_load_lock = threading.Lock()
_ranker = None


def model_dir() -> str:
    return utils.storage_dir(os.path.join("models", "clip-vit-base-patch32"), create=True)


def _download_model_files(directory: str) -> None:
    base_url = f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}"
    for local_name, remote_path in MODEL_FILES.items():
        target = os.path.join(directory, local_name)
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            continue
        logger.info(f"downloading CLIP model file: {remote_path}")
        staged = f"{target}.part"
        with requests.get(f"{base_url}/{remote_path}", stream=True, timeout=(15, 120)) as response:
            response.raise_for_status()
            with open(staged, "wb") as file:
                for chunk in response.iter_content(1024 * 1024):
                    file.write(chunk)
        os.replace(staged, target)


class ClipRanker:
    def __init__(self, directory: str):
        import onnxruntime
        from tokenizers import Tokenizer

        options = onnxruntime.SessionOptions()
        options.log_severity_level = 3
        providers = ["CPUExecutionProvider"]
        self._text = onnxruntime.InferenceSession(
            os.path.join(directory, "text_model_quantized.onnx"), options, providers=providers
        )
        self._vision = onnxruntime.InferenceSession(
            os.path.join(directory, "vision_model_quantized.onnx"), options, providers=providers
        )
        self._tokenizer = Tokenizer.from_file(os.path.join(directory, "tokenizer.json"))
        self._tokenizer.no_padding()
        self._tokenizer.no_truncation()

    def _tokens(self, text: str) -> list[int]:
        ids = self._tokenizer.encode(str(text or "")).ids
        if len(ids) > CONTEXT_LENGTH:
            # 保留开头的 <|startoftext|>，截断后重新补上句尾标记。
            ids = ids[: CONTEXT_LENGTH - 1] + [END_OF_TEXT_ID]
        return ids + [END_OF_TEXT_ID] * (CONTEXT_LENGTH - len(ids))

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        input_ids = np.array([self._tokens(text) for text in texts], dtype=np.int64)
        (embeds,) = self._text.run(["text_embeds"], {"input_ids": input_ids})
        return _normalize(embeds)

    def embed_images(self, images: list[Image.Image]) -> np.ndarray:
        pixels = np.stack([_preprocess(image) for image in images])
        (embeds,) = self._vision.run(["image_embeds"], {"pixel_values": pixels})
        return _normalize(embeds)


def _normalize(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=-1, keepdims=True)
    return vectors / np.maximum(norms, 1e-8)


def _preprocess(image: Image.Image) -> np.ndarray:
    """CLIP preprocessing: shortest side to 224 (bicubic), center crop, normalize."""
    image = image.convert("RGB")
    width, height = image.size
    scale = IMAGE_SIZE / min(width, height)
    resized = image.resize(
        (max(IMAGE_SIZE, round(width * scale)), max(IMAGE_SIZE, round(height * scale))),
        Image.BICUBIC,
    )
    left = (resized.width - IMAGE_SIZE) // 2
    top = (resized.height - IMAGE_SIZE) // 2
    cropped = resized.crop((left, top, left + IMAGE_SIZE, top + IMAGE_SIZE))
    array = (np.asarray(cropped, dtype=np.float32) / 255.0 - IMAGE_MEAN) / IMAGE_STD
    return array.transpose(2, 0, 1)


def get_ranker() -> ClipRanker | None:
    """Load the model once per process; return None when it cannot be loaded."""
    global _ranker
    with _load_lock:
        if _ranker is not None:
            return _ranker
        try:
            directory = model_dir()
            _download_model_files(directory)
            _ranker = ClipRanker(directory)
            logger.info("CLIP ranker loaded")
        except Exception as exc:  # noqa: BLE001 - Scene Sync falls back to search order
            logger.warning(f"CLIP ranker unavailable, falling back to search order: {exc}")
            return None
        return _ranker


def fetch_preview_images(urls: list[str], proxies=None) -> dict[str, Image.Image]:
    """Download small preview images in parallel; failures are skipped."""

    def fetch(url):
        try:
            response = requests.get(url, timeout=PREVIEW_DOWNLOAD_TIMEOUT, proxies=proxies)
            response.raise_for_status()
            if len(response.content) > MAX_PREVIEW_BYTES:
                return url, None
            image = Image.open(io.BytesIO(response.content))
            image.load()
            return url, image
        except Exception as exc:  # noqa: BLE001 - one bad preview must not stop ranking
            logger.debug(f"preview image unavailable: {exc}")
            return url, None

    unique = list(dict.fromkeys(url for url in urls if url))
    if not unique:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(unique))) as executor:
        return {url: image for url, image in executor.map(fetch, unique) if image is not None}
