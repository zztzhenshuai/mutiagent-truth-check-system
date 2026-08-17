"""
agent/tools/cross_reference.py

cross_reference 工具：
1. 对当前文章做预处理，建立句子级索引
2. 使用 sentence-transformers 计算句向量并进行语义检索
3. 调用 LLM 对候选句进行矛盾判断

默认将模型缓存放在仓库内的 .cache/huggingface 目录。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any, Callable

import numpy as np
from json_repair import json_repair

from ..llm.base import BaseLLMClient
from ..preprocess import ProcessedArticle, ProcessedSentence, preprocess_article
from .embedding_utils import (
    warmup_embedding_model,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
try:
    from dotenv import load_dotenv as _load_dotenv
except ImportError:
    _load_dotenv = None

if _load_dotenv is not None:
    _load_dotenv(_PROJECT_ROOT / ".env")

# _DEFAULT_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
_DEFAULT_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
_DEFAULT_CACHE_DIR = _PROJECT_ROOT / ".cache" / "huggingface"
_DEFAULT_TOP_K = 5
_DEFAULT_THRESHOLD = 0.6
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


@dataclass(frozen=True)
class RetrievedSentence:
    sentence: ProcessedSentence
    similarity: float


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _extract_json_block(raw: str) -> dict[str, Any] | None:
    candidates = [raw]

    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1))

    inline = re.search(r"(\{.*\})", raw, re.DOTALL)
    if inline:
        candidates.append(inline.group(1))

    for candidate in candidates:
        try:
            data = json_repair.loads(candidate)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return None


def _build_contradiction_prompt(claim: str, candidates: list[RetrievedSentence]) -> str:
    candidate_lines = []
    for index, item in enumerate(candidates, start=1):
        candidate_lines.append(
            f'{index}. 段落 {item.sentence.paragraph_id} | 相似度 {item.similarity:.3f} | "{item.sentence.text}"'
        )

    joined_candidates = "\n".join(candidate_lines) if candidate_lines else "无候选句。"
    return (
        "你是文章内部矛盾检测助手。请判断原始声明与候选句之间是否存在直接冲突、时间/数字不一致、"
        "前后立场相反或事实描述互斥的情况。\n\n"
        f'原始声明："{claim}"\n\n'
        f"候选句列表：\n{joined_candidates}\n\n"
        "只返回 JSON，不要返回任何额外解释。格式如下：\n"
        '{\n'
        '  "contradictions": [\n'
        '    {\n'
        '      "sentence": "与原始声明冲突的候选句",\n'
        '      "conflict": "具体冲突点",\n'
        '      "confidence": 0.0\n'
        "    }\n"
        "  ]\n"
        "}\n"
        '若没有矛盾，返回 {"contradictions": []}。'
    )


def _format_candidates(candidates: list[RetrievedSentence]) -> str:
    if not candidates:
        return "未找到达到阈值的相关句子。"
    lines = []
    for index, item in enumerate(candidates, start=1):
        sentence = item.sentence
        lines.append(
            f"{index}. 相似度={item.similarity:.3f} | 段落={sentence.paragraph_id} | "
            f"offset=({sentence.start_offset}, {sentence.end_offset}) | {sentence.text}"
        )
    return "\n".join(lines)


def _resolve_repo_path(raw_path: str | Path) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return path


def _resolve_cache_dir(raw_cache_dir: str | Path | None) -> Path:
    if raw_cache_dir is None:
        return _DEFAULT_CACHE_DIR
    return _resolve_repo_path(raw_cache_dir)


def _resolve_local_model_path(raw_local_model_path: str | Path | None) -> Path | None:
    if raw_local_model_path is None:
        return None

    stripped = str(raw_local_model_path).strip()
    if not stripped:
        return None
    return _resolve_repo_path(stripped)


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


class CrossReferenceService:

    def __init__(
        self,
        model_name: str = _DEFAULT_MODEL_NAME,
        cache_dir: Path | None = None,
        similarity_threshold: float = _DEFAULT_THRESHOLD,
        default_top_k: int = _DEFAULT_TOP_K,
        embedder_factory: Callable[[str, Path, bool], Any] | None = None,
    ) -> None:
        self._model_name = model_name
        self._cache_dir = _resolve_cache_dir(cache_dir or os.getenv("CROSS_REFERENCE_CACHE_DIR"))
        self._similarity_threshold = similarity_threshold
        self._default_top_k = default_top_k
        self._embedder_factory = embedder_factory or self._default_embedder_factory

        self._article: ProcessedArticle | None = None
        self._llm: BaseLLMClient | None = None
        self._model: Any | None = None
        self._sentence_embeddings: np.ndarray | None = None
        self._lock = asyncio.Lock()

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    async def prepare_article(self, article_text: str, llm: BaseLLMClient | None = None) -> ProcessedArticle:
        article = preprocess_article(article_text)
        async with self._lock:
            self._article = article
            self._llm = llm
            self._sentence_embeddings = None
        return article

    async def warmup_model(self) -> str:
        await self._get_model()
        return f"cross_reference 模型已就绪：{self._model_name}，缓存目录：{self._cache_dir}"

    async def retrieve_related(
        self,
        claim: str,
        top_k: int | None = None,
        threshold: float | None = None,
    ) -> list[RetrievedSentence]:
        article = self._require_article()
        sentences = article.sentences
        if not sentences:
            return []

        embeddings = await self._get_sentence_embeddings()
        model = await self._get_model()
        normalized_claim_embedding = await asyncio.to_thread(
            model.encode,
            [claim],
            batch_size=1,
            show_progress_bar=False,
            normalize_embeddings=True,
        )
        normalized_claim_embedding = np.asarray(normalized_claim_embedding, dtype=np.float32)[0]

        scores = embeddings @ normalized_claim_embedding
        sorted_indices = np.argsort(scores)[::-1]
        effective_top_k = top_k or self._default_top_k
        effective_threshold = threshold if threshold is not None else self._similarity_threshold

        results: list[RetrievedSentence] = []
        for index in sorted_indices:
            sentence = sentences[int(index)]
            similarity = float(scores[int(index)])
            if similarity < effective_threshold:
                break
            if sentence.text.strip() == claim.strip():
                continue
            results.append(RetrievedSentence(sentence=sentence, similarity=similarity))
            if len(results) >= effective_top_k:
                break
        return results

    async def judge_contradictions(
        self,
        claim: str,
        candidates: list[RetrievedSentence],
    ) -> dict[str, Any]:
        if not candidates:
            return {"contradictions": []}

        if self._llm is None:
            return {
                "contradictions": [],
                "note": "未配置 LLM，已跳过矛盾判断，只返回相似句候选。",
            }

        prompt = _build_contradiction_prompt(claim, candidates)
        raw = await self._llm.complete([{"role": "user", "content": prompt}])
        parsed = _extract_json_block(raw)
        if parsed is None:
            return {
                "contradictions": [],
                "note": "LLM 未返回合法 JSON，已降级为空结果。",
                "raw_response": raw,
            }

        contradictions = parsed.get("contradictions", [])
        if not isinstance(contradictions, list):
            contradictions = []
        return {"contradictions": contradictions}

    async def analyze(self, claim: str, top_k: int | None = None, threshold: float | None = None) -> str:
        candidates = await self.retrieve_related(claim=claim, top_k=top_k, threshold=threshold)
        judgement = await self.judge_contradictions(claim=claim, candidates=candidates)

        lines = [
            f"原始声明：{claim}",
            "相关句检索结果：",
            _format_candidates(candidates),
            "矛盾判断：",
        ]

        contradictions = judgement.get("contradictions", [])
        if contradictions:
            for index, item in enumerate(contradictions, start=1):
                sentence = item.get("sentence", "未知句子")
                conflict = item.get("conflict", "未提供冲突说明")
                confidence = item.get("confidence")
                if confidence is None:
                    lines.append(f"{index}. 句子：{sentence} | 冲突点：{conflict}")
                else:
                    lines.append(f"{index}. 句子：{sentence} | 冲突点：{conflict} | 置信度：{confidence}")
        else:
            lines.append("未发现明确矛盾。")

        note = judgement.get("note")
        if note:
            lines.append(f"备注：{note}")
        return "\n".join(lines)

    def _require_article(self) -> ProcessedArticle:
        if self._article is None:
            raise RuntimeError("cross_reference 尚未初始化文章上下文，请先调用 prepare_cross_reference_context。")
        return self._article

    async def _get_sentence_embeddings(self) -> np.ndarray:
        if self._sentence_embeddings is not None:
            return self._sentence_embeddings

        article = self._require_article()
        model = await self._get_model()
        embeddings = await asyncio.to_thread(
            model.encode,
            [sentence.text for sentence in article.sentences],
            batch_size=32,
            show_progress_bar=False,
            normalize_embeddings=True,
        )
        embeddings = np.asarray(embeddings, dtype=np.float32)

        async with self._lock:
            if self._sentence_embeddings is None:
                self._sentence_embeddings = embeddings
            return self._sentence_embeddings

    async def _get_model(self) -> Any:
        async with self._lock:
            if self._model is None:
                self._cache_dir.mkdir(parents=True, exist_ok=True)
                self._model = await asyncio.to_thread(
                    self._embedder_factory,
                    self._model_name,
                    self._cache_dir,
                    _env_flag("CROSS_REFERENCE_LOCAL_FILES_ONLY", default=False),
                )
            return self._model

    @staticmethod
    def _default_embedder_factory(model_name: str, cache_dir: Path, local_files_only: bool) -> Any:
        os.environ.setdefault("HF_HOME", str(cache_dir))
        os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", str(cache_dir))
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "未安装 sentence-transformers。请先安装 sentence-transformers、transformers 和 torch。"
            ) from exc

        local_model_path = _resolve_local_model_path(os.getenv("LOCAL_MODEL_PATH"))
        if local_files_only and local_model_path is not None:
            if not local_model_path.exists():
                raise RuntimeError(f"LOCAL_MODEL_PATH 不存在：{local_model_path}")
            return SentenceTransformer(
                str(local_model_path),
                cache_folder=str(cache_dir),
                local_files_only=True,
            )

        try:
            return SentenceTransformer(
                model_name,
                cache_folder=str(cache_dir),
                local_files_only=local_files_only,
            )
        except Exception as exc:
            if not local_model_path or not _should_fallback_to_local_model(exc):
                raise
            if not local_model_path.exists():
                raise RuntimeError(
                    f"连接 Hugging Face 失败，且 LOCAL_MODEL_PATH 不存在：{local_model_path}"
                ) from exc
            try:
                return SentenceTransformer(
                    str(local_model_path),
                    cache_folder=str(cache_dir),
                    local_files_only=True,
                )
            except Exception as local_exc:
                raise RuntimeError(
                    f"连接 Hugging Face 失败，尝试回退到本地模型后仍加载失败：{local_exc}"
                ) from local_exc


_DEFAULT_SERVICE = CrossReferenceService()


def _parse_tool_input(raw_input: str) -> tuple[str, int | None, float | None]:
    data = _extract_json_block(raw_input)
    if not data:
        return raw_input.strip(), None, None

    claim = str(data.get("claim") or data.get("input") or raw_input).strip()
    top_k = data.get("top_k")
    threshold = data.get("threshold")
    try:
        parsed_top_k = int(top_k) if top_k is not None else None
    except (TypeError, ValueError):
        parsed_top_k = None
    try:
        parsed_threshold = float(threshold) if threshold is not None else None
    except (TypeError, ValueError):
        parsed_threshold = None
    return claim, parsed_top_k, parsed_threshold


async def prepare_cross_reference_context(
    article_text: str,
    llm: BaseLLMClient | None = None,
) -> ProcessedArticle:
    return await _DEFAULT_SERVICE.prepare_article(article_text=article_text, llm=llm)


async def warmup_cross_reference_model() -> str:
    """预热 cross_reference 嵌入模型（复用全局单例）。"""
    return await warmup_embedding_model()


async def cross_reference(input: str) -> str:
    try:
        claim, top_k, threshold = _parse_tool_input(input)
        if not claim:
            return "工具执行失败：cross_reference 输入为空。"
        return await _DEFAULT_SERVICE.analyze(claim=claim, top_k=top_k, threshold=threshold)
    except Exception as exc:
        return f"工具执行失败：{exc}"


if __name__ == "__main__":
    try:
        print(asyncio.run(warmup_cross_reference_model()))
    except Exception as exc:
        print(f"cross_reference 模型预热失败：{exc}")
        raise SystemExit(1)
