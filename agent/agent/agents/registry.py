"""
agent/agents/registry.py

领域 Agent 注册表 + 工厂函数。
新增领域只需在此注册一行，不改 agent.py。

约定：
  - 键名 = skill.name（如 "medical"、"finance"）
  - 工厂函数对未知名称回退 GeneralAgent
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import DomainAgent
from .finance import FinanceAgent
from .general import GeneralAgent
from .medical import MedicalAgent
from .news_policy import NewsAgent
from .technology import TechAgent

if TYPE_CHECKING:
    from ..skills import Skill

logger = logging.getLogger("agent.agents.registry")

# ── 注册表：skill_name → DomainAgent 子类 ──
AGENT_REGISTRY: dict[str, type[DomainAgent]] = {
    "general": GeneralAgent,
    "medical": MedicalAgent,
    "finance": FinanceAgent,
    "technology": TechAgent,
    "news_policy": NewsAgent,
}


def get_domain_agent(skill_name: str, skill: Skill, llm) -> DomainAgent:
    """工厂函数：根据领域名实例化对应专家 Agent。

    未注册的领域 → 回退 GeneralAgent（记录 warning 日志）。

    参数：
      skill_name: skill.name（如 "medical"）
      skill:      Skill 实例（含 allowed_tools、prompt 等）
      llm:        LLM 客户端引用（ClaudeClient）
    """
    cls = AGENT_REGISTRY.get(skill_name)
    if cls is None:
        logger.warning(
            "领域 %s 未注册专属 Agent，回退 GeneralAgent（已知领域=%s）",
            skill_name,
            list(AGENT_REGISTRY),
        )
        cls = GeneralAgent
    return cls(skill, llm)
