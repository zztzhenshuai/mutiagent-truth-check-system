"""
agent/llm/chat_factory.py

LLM 工厂函数。

- 主流程 LLM：根据 AGENT_LLM_PROVIDER 创建，默认 glm；Claude 作为备用。
- 聊天 LLM：根据 CHAT_LLM_PROVIDER 创建，默认 glm。
"""

import logging
import os

from .base import BaseLLMClient
from .claude import ClaudeClient
from .glm import GLMClient
from .deepseek import DeepSeekClient

logger = logging.getLogger("agent.llm.factory")


_PROVIDERS = {
    "glm": GLMClient,
    "claude": ClaudeClient,
    "deepseek": DeepSeekClient,
}


def _create_provider(provider: str) -> BaseLLMClient:
    try:
        cls = _PROVIDERS[provider]
    except KeyError:
        raise ValueError(
            f"Unknown LLM provider: '{provider}'. Supported: glm, claude, deepseek"
        )
    return cls()


def create_core_llm(provider: str | None = None) -> BaseLLMClient:
    """
    创建扫描、核查、辩论等主流程 LLM。

    默认优先级：
      1. AGENT_LLM_PROVIDER 指定的 provider，默认 glm
      2. glm
      3. claude
      4. deepseek

    因此默认情况下 claim 提取、Verifier、Challenger、Judge 都优先使用 GLM，
    只有 GLM 初始化失败（例如未配置 GLM_API_KEY）时才回退 Claude。
    """
    preferred = (provider or os.environ.get("AGENT_LLM_PROVIDER", "glm")).strip().lower()
    order = [preferred] + [name for name in ("glm", "claude", "deepseek") if name != preferred]

    last_error: Exception | None = None
    for name in order:
        try:
            llm = _create_provider(name)
            logger.info("Core LLM: %s", name)
            return llm
        except Exception as exc:
            last_error = exc
            logger.warning("Core LLM %s 初始化失败：%s", name, exc)

    raise RuntimeError("No core LLM available") from last_error


def create_router_llm(fallback: BaseLLMClient | None = None) -> BaseLLMClient:
    """创建领域路由 LLM：优先 GLM，失败时复用主流程 LLM。"""
    try:
        llm = GLMClient()
        logger.info("Router LLM: glm")
        return llm
    except Exception as exc:
        logger.warning("Router LLM GLM 初始化失败：%s", exc)
        if fallback is not None:
            return fallback
        return create_core_llm()


def create_chat_llm(provider: str | None = None) -> BaseLLMClient:
    """
    根据环境变量 CHAT_LLM_PROVIDER 创建聊天专用 LLM 客户端。

    默认值：
      - 未设置 CHAT_LLM_PROVIDER → glm
      - glm 未配置 API Key → 回退到 deepseek
    """
    if provider is None:
        provider = os.environ.get("CHAT_LLM_PROVIDER", "glm").strip().lower()

    if provider == "glm":
        if os.environ.get("GLM_API_KEY"):
            return GLMClient()
        if os.environ.get("DEEPSEEK_API_KEY"):
            return DeepSeekClient()
        raise ValueError(
            "CHAT_LLM_PROVIDER=glm but GLM_API_KEY not set, "
            "and DEEPSEEK_API_KEY also not set — no chat LLM available"
        )

    if provider == "claude":
        if os.environ.get("ANTHROPIC_API_KEY"):
            return ClaudeClient()
        else:
            # Claude Key 未配置，回退到 GLM → DeepSeek
            if os.environ.get("GLM_API_KEY"):
                return GLMClient()
            if os.environ.get("DEEPSEEK_API_KEY"):
                return DeepSeekClient()
            raise ValueError(
                "CHAT_LLM_PROVIDER=claude but ANTHROPIC_API_KEY not set, "
                "and GLM_API_KEY/DEEPSEEK_API_KEY also not set — no chat LLM available"
            )

    elif provider == "deepseek":
        if os.environ.get("DEEPSEEK_API_KEY"):
            return DeepSeekClient()
        if os.environ.get("GLM_API_KEY"):
            return GLMClient()
        raise ValueError("CHAT_LLM_PROVIDER=deepseek but DEEPSEEK_API_KEY not set")

    else:
        raise ValueError(
            f"Unknown CHAT_LLM_PROVIDER: '{provider}'. Supported: claude, glm, deepseek"
        )
