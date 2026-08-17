# agent/agents/__init__.py
from .base import DomainAgent
from .finance import FinanceAgent
from .general import GeneralAgent
from .medical import MedicalAgent
from .news_policy import NewsAgent
from .registry import AGENT_REGISTRY, get_domain_agent
from .technology import TechAgent

__all__ = [
    "DomainAgent",
    "GeneralAgent",
    "MedicalAgent",
    "FinanceAgent",
    "TechAgent",
    "NewsAgent",
    "AGENT_REGISTRY",
    "get_domain_agent",
]
