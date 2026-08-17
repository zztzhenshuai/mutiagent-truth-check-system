"""
agent/tools/embedding_utils.py

共享的嵌入模型加载器。
从 cross_reference.py 抽取，供 cross_reference 和 RAG 共用，
避免重复加载 sentence-transformers 模型（~1.2GB 内存）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import numpy as np

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_DEFAULT_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
_DEFAULT_CACHE_DIR = _PROJECT_ROOT / ".cache" / "huggingface"

_NETWORK_ERROR_MARKERS = (
    "read timed out",
    "connect timeout",
    "connection timed out",
    "timed out",
    "timeout",
    "max retries exceeded",
    "httpsconnectionpool",
    "connection error",
    "connectionerror",
    "failed to establish a new connection",
    "temporary failure in name resolution",
    "name or service not known",
    "network is unreachable",
    "couldn't connect to 'https://huggingface.co'",
    "could not connect to 'https://huggingface.co'",
    "huggingface.co",
)

logger = logging.getLogger("agent.tools.embedding_utils")

# ── 模块级单例缓存 ──
_model: Any = None
_model_lock = asyncio.Lock()


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_local_model_path(raw_path: str | Path | None) -> Path | None:
    if raw_path is None:
        return None
    stripped = str(raw_path).strip()
    if not stripped:
        return None
    path = Path(stripped)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    if not path.exists():
        return None
    return path


def _flatten_exception_messages(exc: BaseException) -> str:
    parts: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current).strip()
        if message:
            parts.append(message.lower())
        current = current.__cause__ or current.__context__
    return " | ".join(parts)


def _should_fallback_to_local_model(exc: BaseException) -> bool:
    message = _flatten_exception_messages(exc)
    return any(marker in message for marker in _NETWORK_ERROR_MARKERS)


def _create_embedder(
    model_name: str = _DEFAULT_MODEL_NAME,
    cache_dir: str | Path | None = None,
    local_files_only: bool = False,
) -> Any:
    """同步创建 SentenceTransformer 实例。"""
    cache_path = Path(cache_dir) if cache_dir else _DEFAULT_CACHE_DIR
    cache_path.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("HF_HOME", str(cache_path))
    os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", str(cache_path))

    # 防御性设置：避免 PyTorch inductor 在容器环境中因 getpass.getuser()
    # 找不到 uid 而崩溃（KeyError: 'getpwuid(): uid not found'）。
    # getpass.getuser() 优先检查 LOGNAME/USER/LNAME/USERNAME 环境变量，
    # 设置这些可以跳过 pwd.getpwuid() 调用。
    os.environ.setdefault("USER", "ci")
    os.environ.setdefault("LOGNAME", "ci")
    # 同时设置 TORCHINDUCTOR_CACHE_DIR 以避免 default_cache_dir()
    # 中的 getpass.getuser() 调用路径。
    os.environ.setdefault("TORCHINDUCTOR_CACHE_DIR", str(cache_path / "torch"))

    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError(
            "未安装 sentence-transformers。请先安装 sentence-transformers、transformers 和 torch。"
        ) from exc

    local_model_path = _resolve_local_model_path(os.getenv("LOCAL_MODEL_PATH"))
    if local_files_only and local_model_path is not None:
        return SentenceTransformer(
            str(local_model_path),
            cache_folder=str(cache_path),
            local_files_only=True,
        )

    try:
        return SentenceTransformer(
            model_name,
            cache_folder=str(cache_path),
            local_files_only=local_files_only,
        )
    except Exception as exc:
        if not local_model_path or not _should_fallback_to_local_model(exc):
            raise
        try:
            return SentenceTransformer(
                str(local_model_path),
                cache_folder=str(cache_path),
                local_files_only=True,
            )
        except Exception as local_exc:
            raise RuntimeError(
                f"连接 Hugging Face 失败，尝试回退到本地模型后仍加载失败：{local_exc}"
            ) from local_exc


async def get_embedding_model(
    model_name: str | None = None,
    cache_dir: str | Path | None = None,
) -> Any:
    """异步获取全局单例 SentenceTransformer 模型。

    首次调用时加载模型（~1-13s），后续调用直接返回缓存实例。
    线程安全（asyncio.Lock）。
    """
    global _model

    if _model is not None:
        return _model

    async with _model_lock:
        if _model is not None:
            return _model

        effective_model = model_name or os.getenv(
            "EMBEDDING_MODEL_NAME", _DEFAULT_MODEL_NAME
        )
        effective_cache = cache_dir or os.getenv(
            "EMBEDDING_CACHE_DIR", str(_DEFAULT_CACHE_DIR)
        )

        logger.info("加载嵌入模型：%s（缓存=%s）", effective_model, effective_cache)
        _model = await asyncio.to_thread(
            _create_embedder,
            effective_model,
            effective_cache,
            _env_flag("CROSS_REFERENCE_LOCAL_FILES_ONLY", default=False),
        )
        logger.info("嵌入模型就绪：%s", effective_model)
        return _model


async def warmup_embedding_model() -> str:
    """预热嵌入模型（后台任务调用）。"""
    model = await get_embedding_model()
    model_name = os.getenv("EMBEDDING_MODEL_NAME", _DEFAULT_MODEL_NAME)
    return f"嵌入模型已就绪：{model_name}"


async def encode_texts(
    texts: list[str],
    batch_size: int = 32,
    normalize: bool = True,
) -> np.ndarray:
    """对文本列表编码，返回 (N, dim) float32 数组。"""
    model = await get_embedding_model()
    embeddings = await asyncio.to_thread(
        model.encode,
        texts,
        batch_size=batch_size,
        show_progress_bar=False,
        normalize_embeddings=normalize,
    )
    return np.asarray(embeddings, dtype=np.float32)


async def encode_query(query: str) -> np.ndarray:
    """对单条查询文本编码，返回 (dim,) float32 归一化向量。"""
    embeddings = await encode_texts([query], batch_size=1, normalize=True)
    return embeddings[0]
