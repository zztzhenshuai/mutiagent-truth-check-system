"""
tests/test_scanner.py

测试扫描器的纯逻辑部分（JSON 解析、offset 定位）。
scan_article 用 Mock LLM 测试，不消耗 API 额度。
"""

import pytest
from agent.scanner import _parse_claims_json, _resolve_offsets, scan_article
from agent.llm.base import BaseLLMClient


# ---- _parse_claims_json 测试 ----

def test_parse_valid_json():
    raw = '{"claims": ["声明一", "声明二"]}'
    assert _parse_claims_json(raw) == ["声明一", "声明二"]


def test_parse_json_in_markdown_block():
    raw = '```json\n{"claims": ["声明一"]}\n```'
    assert _parse_claims_json(raw) == ["声明一"]


def test_parse_json_in_markdown_block_no_lang():
    raw = '```\n{"claims": ["声明一"]}\n```'
    assert _parse_claims_json(raw) == ["声明一"]


def test_parse_invalid_json_returns_empty():
    assert _parse_claims_json("这不是JSON格式") == []


def test_parse_empty_claims():
    assert _parse_claims_json('{"claims": []}') == []


def test_parse_missing_claims_key():
    assert _parse_claims_json('{"result": []}') == []


# ---- _resolve_offsets 测试 ----

ARTICLE = "中国2023年GDP增速为8.5%，远超全球平均水平。据报道此数据来自官方统计。"


def test_resolve_finds_correct_offset():
    claims = _resolve_offsets(ARTICLE, ["GDP增速为8.5%"])
    assert len(claims) == 1
    c = claims[0]
    assert c.text == "GDP增速为8.5%"
    assert ARTICLE[c.position[0]:c.position[1]] == "GDP增速为8.5%"


def test_resolve_filters_hallucinated_text():
    # LLM 幻构的文本在原文中不存在，应被过滤
    claims = _resolve_offsets(ARTICLE, ["这段话根本不在原文中"])
    assert len(claims) == 0


def test_resolve_deduplicates_same_position():
    # 两条文本都 find 到同一位置（子串关系），只保留第一条
    claims = _resolve_offsets(ARTICLE, ["GDP增速为8.5%", "GDP增速为8.5%"])
    assert len(claims) == 1


def test_resolve_assigns_incremental_ids():
    claims = _resolve_offsets(ARTICLE, ["GDP增速为8.5%", "据报道此数据来自官方统计"])
    ids = [c.id for c in claims]
    assert ids == ["c001", "c002"]


def test_resolve_position_matches_text_length():
    claims = _resolve_offsets(ARTICLE, ["GDP增速为8.5%"])
    c = claims[0]
    assert c.position[1] - c.position[0] == len(c.text)


# ---- scan_article 集成测试（Mock LLM）----

class MockLLMClient(BaseLLMClient):
    """返回固定 JSON 响应的假 LLM，不消耗 API 额度。"""

    def __init__(self, response: str):
        self._response = response

    async def complete(self, messages: list[dict]) -> str:
        return self._response

    async def complete_with_tools(self, messages, tools):
        raise NotImplementedError


@pytest.mark.asyncio
async def test_scan_article_returns_claims():
    article = "中国2023年GDP增速为8.5%，远超预期。据报道此数据来自官方统计局。"
    mock_llm = MockLLMClient('{"claims": ["GDP增速为8.5%", "据报道此数据来自官方统计局"]}')

    claims = await scan_article(article, mock_llm)

    assert len(claims) == 2
    assert claims[0].text == "GDP增速为8.5%"
    assert claims[0].id == "c001"


@pytest.mark.asyncio
async def test_scan_article_filters_hallucinations():
    article = "中国2023年GDP增速为8.5%。"
    mock_llm = MockLLMClient('{"claims": ["GDP增速为8.5%", "火星上有生命"]}')

    claims = await scan_article(article, mock_llm)

    assert len(claims) == 1
    assert claims[0].text == "GDP增速为8.5%"


@pytest.mark.asyncio
async def test_scan_article_handles_llm_json_error():
    article = "任意文章内容。"
    mock_llm = MockLLMClient("这不是有效的JSON格式")

    claims = await scan_article(article, mock_llm)

    assert claims == []
