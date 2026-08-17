"""
agent/scanner.py

文章扫描器：从原始文章中提取所有值得事实核查的声明。

流程：
1. 长度判断：< 8000 字整篇处理，否则按段落分批
2. 调用配置的 LLM 提取声明文本列表（LLM 只返回文本，不负责 offset）
3. 用 str.find() 在原文中精确定位 offset
4. 过滤掉 LLM 幻构的（find 返回 -1 的）声明
"""

from __future__ import annotations

import logging
import re

from json_repair import json_repair

from .llm.base import BaseLLMClient
from .models import Claim

logger = logging.getLogger("agent.scanner")

# 整篇处理的字符数上限
_SINGLE_CHUNK_LIMIT = 8000

_EXTRACT_PROMPT = """\
从以下文章中提取所有值得事实核查的声明。

提取标准（满足任意一条即提取）：
- 包含具体数字或统计数据（如增速、数量、比例）
- 描述具体历史事件或时间节点
- 引用他人观点或研究结论
- 做出因果关系断言

不提取：
- 主观评价或情感表达
- 泛泛而谈、无法证伪的说法
- 过渡句、标题

以 JSON 格式返回，只返回 JSON，不要其他解释：
{"claims": ["原文片段1", "原文片段2", ...]}

文章：
{article}
"""


def _split_paragraphs(text: str) -> list[str]:
    """按空行或段落分隔符切分文章。"""
    paragraphs = re.split(r"\n{2,}", text.strip())
    return [p.strip() for p in paragraphs if p.strip()]


def _parse_claims_json(raw: str) -> list[str]:
    """从 LLM 输出中提取 claims 列表，容错处理 JSON 格式错误。

    注意：json_repair.loads 对无法修复的输入不会抛 JSONDecodeError，
    而是返回非 dict 值（如空字符串）。必须用 isinstance 判断，否则 .get 会抛 AttributeError。
    """
    # 尝试直接解析（json_repair 会尽量修复轻微格式错误）
    try:
        data = json_repair.loads(raw)
    except Exception:
        data = None
    if isinstance(data, dict):
        claims = data.get("claims", [])
        return claims if isinstance(claims, list) else []

    # 回退：尝试从 markdown 代码块中提取 JSON（同样用 json_repair 容错大模型输出）
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if match:
        try:
            data = json_repair.loads(match.group(1))
        except Exception:
            return []
        if isinstance(data, dict):
            claims = data.get("claims", [])
            return claims if isinstance(claims, list) else []
    return []


def _resolve_offsets(article: str, claim_texts: list[str]) -> list[Claim]:
    """
    将 LLM 提取的声明文本映射到原文 offset。
    find() 返回 -1 的声明（LLM 幻构）直接丢弃。
    """
    claims = []
    counter = 1
    seen: set[int] = set()  # 避免同一位置重复

    for text in claim_texts:
        text = text.strip()
        if not text:
            continue
        start = article.find(text)
        if start == -1 or start in seen:
            continue
        seen.add(start)
        claims.append(
            Claim(
                id=f"c{counter:03d}",
                text=text,
                position=(start, start + len(text)),
                suspicion_score=0.0,  # 由 planner 填充
            )
        )
        counter += 1

    return claims


async def scan_article(article: str, llm: BaseLLMClient) -> list[Claim]:
    """
    主入口：扫描文章，返回待验证声明列表（offset 已定位，score 待 planner 填充）。
    """
    if len(article) <= _SINGLE_CHUNK_LIMIT:
        chunks = [article]
    else:
        chunks = _split_paragraphs(article)
    logger.info("扫描文章：length=%d，切分为 %d 个 chunk", len(article), len(chunks))

    all_claim_texts: list[str] = []

    for i, chunk in enumerate(chunks, start=1):
        prompt = _EXTRACT_PROMPT.replace("{article}", chunk)
        raw = await llm.complete([{"role": "user", "content": prompt}])

        logger.debug("chunk %d/%d LLM 原始响应：%s", i, len(chunks), raw)

        parsed = _parse_claims_json(raw)
        logger.debug("chunk %d/%d 解析出 %d 条声明文本", i, len(chunks), len(parsed))
        all_claim_texts.extend(parsed)

    # 去重（不同 chunk 可能提取到相同文本）
    seen_texts: set[str] = set()
    unique_texts = []
    for t in all_claim_texts:
        if t not in seen_texts:
            seen_texts.add(t)
            unique_texts.append(t)

    claims = _resolve_offsets(article, unique_texts)
    logger.info(
        "扫描去重后 %d 条文本，offset 定位成功 %d 条（丢弃 %d 条幻构）",
        len(unique_texts), len(claims), len(unique_texts) - len(claims),
    )
    return claims
