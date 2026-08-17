"""
agent/skills/router.py

领域路由：给定文章文本，从已加载的 skills 中选出最匹配的一个。
仿 Claude Code——把每个 skill 的 description 交给一个轻量模型判断该启用哪个。

设计要点：
  - 只截取文章开头若干字符即可判断领域，省 token。
  - 任何异常 / 解析失败 / 置信度过低 / 选了不存在的 skill → 回退兜底 general。
    路由错误的代价很高（全程用错 prompt/工具），因此偏向"拿不准就 general"。
"""

from __future__ import annotations

import logging

import json_repair

from ..llm.base import BaseLLMClient
from .base import GENERAL_SKILL_NAME, KIND_DOMAIN, Skill

logger = logging.getLogger("agent.router")

# 判断领域只需文章开头，截断以省 token
_ROUTE_SAMPLE_LIMIT = 800

# 低于此置信度视为"拿不准"，回退 general
_ROUTE_CONFIDENCE_FLOOR = 0.5

_ROUTE_PROMPT = """\
你是一个文章领域分类器。请根据文章内容，从下列领域中选出最匹配的一个。

可选领域：
{skill_list}

规则：
- 只能选择上面列出的领域名（name），不得编造。
- 若文章不属于任何专门领域，或难以判断，请选择 "{general}"。
- 只返回 JSON，不要任何解释：{{"skill": "领域名", "confidence": 0.0~1.0}}

文章开头：
{article}
"""


def _build_route_prompt(article: str, skills: dict[str, Skill]) -> str:
    skill_list = "\n".join(
        f"- {s.name}：{s.description}" for s in skills.values()
    )
    sample = article[:_ROUTE_SAMPLE_LIMIT]
    return _ROUTE_PROMPT.format(
        skill_list=skill_list,
        general=GENERAL_SKILL_NAME,
        article=sample,
    )


def _parse_route_response(raw: str) -> tuple[str | None, float]:
    """从模型输出中容错解析出 (skill_name, confidence)。"""
    try:
        data = json_repair.loads(raw)
    except Exception:
        return None, 0.0
    if not isinstance(data, dict):
        return None, 0.0
    name = data.get("skill")
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return (str(name) if name else None), confidence


async def route_skill(
    article: str,
    skills: dict[str, Skill],
    llm: BaseLLMClient,
) -> Skill:
    """
    在领域（domain）skill 中选出最匹配的一个。任何不确定情况一律回退到 general。
    overlay 不参与路由（由用户开关、叠加生效），这里直接过滤掉。
    保证返回值非空（general 必然存在，由 load_skills 校验）。
    """
    general = skills[GENERAL_SKILL_NAME]

    # 仅在领域 skill 中路由
    domains = {n: s for n, s in skills.items() if s.kind == KIND_DOMAIN}

    # 只有 general 一个领域，无需调用模型
    if len(domains) <= 1:
        logger.debug("仅有 general 一个领域，跳过路由模型")
        return general

    prompt = _build_route_prompt(article, domains)
    try:
        raw = await llm.complete([{"role": "user", "content": prompt}])
    except Exception:
        logger.warning("路由模型调用失败，回退 general", exc_info=True)
        return general

    name, confidence = _parse_route_response(raw)
    if name is None or name not in domains or confidence < _ROUTE_CONFIDENCE_FLOOR:
        logger.info(
            "路由回退 general：name=%s confidence=%.2f（候选领域=%s）",
            name, confidence, list(domains),
        )
        return general
    logger.info("路由命中领域 %s（confidence=%.2f）", name, confidence)
    return domains[name]
