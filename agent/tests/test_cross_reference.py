"""
tests/test_cross_reference.py

测试 D 负责的 cross_reference 工具。
"""

import sys
from types import ModuleType
from pathlib import Path

import numpy as np
import pytest

from agent.llm.base import BaseLLMClient
import agent.tools.cross_reference as cross_reference_module
from agent.tools.cross_reference import CrossReferenceService, _resolve_cache_dir, _resolve_local_model_path


class FakeEmbedder:

    def encode(self, texts, **kwargs):
        vectors = []
        for text in texts:
            normalized = text.strip()
            if "北京是中国的首都" in normalized:
                vectors.append([1.0, 0.0, 0.0])
            elif "上海才是中国的首都" in normalized:
                vectors.append([0.95, 0.05, 0.0])
            elif "广州是华南城市" in normalized:
                vectors.append([0.0, 1.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0])
        array = np.asarray(vectors, dtype=np.float32)
        normalize = kwargs.get("normalize_embeddings", False)
        if normalize:
            norms = np.linalg.norm(array, axis=1, keepdims=True)
            norms[norms == 0.0] = 1.0
            array = array / norms
        return array


class FakeLLM(BaseLLMClient):

    def __init__(self, response: str):
        self._response = response

    async def complete(self, messages: list[dict]) -> str:
        return self._response

    async def complete_with_tools(self, messages, tools):
        raise NotImplementedError


def build_service() -> tuple[CrossReferenceService, str]:
    service = CrossReferenceService(
        cache_dir=Path(".cache/test-cross-reference"),
        embedder_factory=lambda model_name, cache_dir, local_files_only: FakeEmbedder(),
    )
    article = "北京是中国的首都。上海才是中国的首都。广州是华南城市。"
    return service, article


@pytest.mark.asyncio
async def test_retrieve_related_returns_top_candidate():
    service, article = build_service()
    await service.prepare_article(article)

    results = await service.retrieve_related("北京是中国的首都。", top_k=2, threshold=0.6)

    assert len(results) == 1
    assert results[0].sentence.text == "上海才是中国的首都。"
    assert results[0].similarity > 0.9


@pytest.mark.asyncio
async def test_judge_contradictions_parses_json_markdown():
    llm = FakeLLM(
        '```json\n{"contradictions": [{"sentence": "上海才是中国的首都。", "conflict": "首都表述冲突", "confidence": 0.93}]}\n```'
    )
    service, article = build_service()
    await service.prepare_article(article, llm=llm)
    candidates = await service.retrieve_related("北京是中国的首都。", top_k=2, threshold=0.6)

    judgement = await service.judge_contradictions("北京是中国的首都。", candidates)

    assert judgement["contradictions"][0]["sentence"] == "上海才是中国的首都。"
    assert judgement["contradictions"][0]["conflict"] == "首都表述冲突"


@pytest.mark.asyncio
async def test_judge_contradictions_handles_invalid_json():
    llm = FakeLLM("这不是合法 JSON")
    service, article = build_service()
    await service.prepare_article(article, llm=llm)
    candidates = await service.retrieve_related("北京是中国的首都。", top_k=2, threshold=0.6)

    judgement = await service.judge_contradictions("北京是中国的首都。", candidates)

    assert judgement["contradictions"] == []
    assert "note" in judgement


@pytest.mark.asyncio
async def test_cross_reference_tool_formats_output(monkeypatch):
    llm = FakeLLM('{"contradictions": [{"sentence": "上海才是中国的首都。", "conflict": "首都表述冲突"}]}')
    service, article = build_service()
    await service.prepare_article(article, llm=llm)
    monkeypatch.setattr(cross_reference_module, "_DEFAULT_SERVICE", service)

    output = await cross_reference_module.cross_reference('{"claim": "北京是中国的首都。", "top_k": 2, "threshold": 0.6}')

    assert "相关句检索结果" in output
    assert "上海才是中国的首都。" in output
    assert "首都表述冲突" in output


def test_resolve_cache_dir_uses_project_root_for_relative_path():
    resolved = _resolve_cache_dir(".cache/local-model")

    assert resolved.is_absolute()
    assert str(resolved).endswith(str(Path(".cache/local-model")))


def test_resolve_local_model_path_uses_project_root_for_relative_path():
    resolved = _resolve_local_model_path("models/cross-reference/paraphrase-multilingual-MiniLM-L12-v2")

    assert resolved is not None
    assert resolved.is_absolute()
    assert str(resolved).endswith(str(Path("models/cross-reference/paraphrase-multilingual-MiniLM-L12-v2")))


def test_default_embedder_factory_falls_back_to_local_model_on_timeout(monkeypatch, tmp_path):
    local_model_dir = tmp_path / "local-model"
    local_model_dir.mkdir()
    cache_dir = tmp_path / "cache"
    calls: list[tuple[str, str, bool]] = []

    class FakeSentenceTransformer:

        def __init__(self, model_ref, cache_folder=None, local_files_only=False):
            calls.append((str(model_ref), str(cache_folder), local_files_only))
            if model_ref == "remote-model":
                raise OSError("HTTPSConnectionPool(host='huggingface.co', port=443): Read timed out.")
            self.model_ref = str(model_ref)

    fake_module = ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = FakeSentenceTransformer

    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)
    monkeypatch.setenv("LOCAL_MODEL_PATH", str(local_model_dir))

    model = CrossReferenceService._default_embedder_factory("remote-model", cache_dir, local_files_only=False)

    assert calls[0] == ("remote-model", str(cache_dir), False)
    assert calls[1] == (str(local_model_dir), str(cache_dir), True)
    assert model.model_ref == str(local_model_dir)


def test_default_embedder_factory_uses_local_model_when_local_files_only(monkeypatch, tmp_path):
    local_model_dir = tmp_path / "local-model"
    local_model_dir.mkdir()
    cache_dir = tmp_path / "cache"
    calls: list[tuple[str, str, bool]] = []

    class FakeSentenceTransformer:

        def __init__(self, model_ref, cache_folder=None, local_files_only=False):
            calls.append((str(model_ref), str(cache_folder), local_files_only))
            self.model_ref = str(model_ref)

    fake_module = ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = FakeSentenceTransformer

    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)
    monkeypatch.setenv("LOCAL_MODEL_PATH", str(local_model_dir))

    model = CrossReferenceService._default_embedder_factory("remote-model", cache_dir, local_files_only=True)

    assert calls == [(str(local_model_dir), str(cache_dir), True)]
    assert model.model_ref == str(local_model_dir)
