"""
agent/agents/general.py

GeneralAgent — 兜底领域 Agent，完全继承 DomainAgent 默认实现。
行为与当前通用 Agent 完全一致，保证向后兼容。
"""

from .base import DomainAgent


class GeneralAgent(DomainAgent):
    """完全继承 DomainAgent 默认实现，行为与当前通用 Agent 一致。

    不覆盖任何方法 — 所有路径走 DomainAgent 的默认行为，
    即当前 agent.py 的 _build_system_prompt() 和 debate.py 的全局 prompt builder。
    """

    pass
