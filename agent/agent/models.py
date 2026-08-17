"""
agent/models.py

内部数据结构（dataclass）和 SSE 事件模型（Pydantic）。
AgentState 是所有事件类型的 Union，B/C/E 依赖此文件的 JSON Schema。

迭代四（方向1：复杂度自适应路由）新增：
- ComplexityLevel：声明验证复杂度等级
- VerificationStrategy：每种复杂度对应的验证策略参数
- Claim 新增 complexity / complexity_confidence 字段
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Union

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 复杂度自适应路由（迭代四·方向1）
# ---------------------------------------------------------------------------

# 声明验证复杂度等级
ComplexityLevel = Literal["simple", "medium", "complex"]


@dataclass(frozen=True)
class VerificationStrategy:
    """每种复杂度对应的验证策略参数。"""
    level: ComplexityLevel
    max_react_steps: int            # ReAct 最大步数
    enable_challenger: bool         # 是否启用 Challenger
    enable_judge: bool              # 是否启用 Judge
    enable_rebuttal: bool           # 是否允许 Challenger 异议后重验证
    enable_reflexion: bool          # 是否在低置信度时追加反思
    tool_required: bool             # 是否强制至少调用一次工具
    high_confidence_threshold: float  # 跳过后续辩论的置信度阈值

    @property
    def label(self) -> str:
        """中文标签，供日志和前端展示。"""
        return {"simple": "快速核验", "medium": "标准验证", "complex": "深度辩论"}[self.level]


# ---------------------------------------------------------------------------
# 内部数据结构（不发送到前端，仅 Agent 内部使用）
# ---------------------------------------------------------------------------

@dataclass
class Claim:
    id: str                          # 递增编号，如 "c001"
    text: str                        # 原始文本片段（从文章中 find 出来的）
    position: tuple[int, int]        # (start_offset, end_offset)
    suspicion_score: float           # 0.0 ~ 1.0，越高越优先验证
    complexity: ComplexityLevel = "medium"         # 迭代四新增：验证复杂度
    complexity_confidence: float = 0.5             # 迭代四新增：分类置信度（0~1）


@dataclass
class VerificationPlan:
    claims: list[Claim]              # 按 suspicion_score 降序排列
    status: dict[str, str] = field(default_factory=dict)  # claim_id -> "pending|running|done|error"


# ---------------------------------------------------------------------------
# SSE 事件模型（发送给前端的数据，B 包装为 SSE，C 消费高亮，E 记录追踪）
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PlanEvent(BaseModel):
    """扫描器完成后发出，告知前端共有多少声明待验证。"""
    type: Literal["plan"] = "plan"
    timestamp: str = Field(default_factory=_now)
    total: int                                    # 声明总数
    claims: list[dict]                            # [{"id": "c001", "text": "...", "suspicion_score": 0.8}]


class ThinkingEvent(BaseModel):
    """ReAct 循环每个 Thought 步骤。"""
    type: Literal["thinking"] = "thinking"
    timestamp: str = Field(default_factory=_now)
    claim_id: str
    thought: str


class ToolCallEvent(BaseModel):
    """工具调用及其返回结果。"""
    type: Literal["tool_call"] = "tool_call"
    timestamp: str = Field(default_factory=_now)
    claim_id: str
    tool_name: str
    tool_input: str
    tool_output: str


class AnnotationEvent(BaseModel):
    """单条声明验证完成，携带最终标注结果。C 收到后触发高亮。"""
    type: Literal["annotation"] = "annotation"
    timestamp: str = Field(default_factory=_now)
    claim_id: str
    text: str
    start_offset: int
    end_offset: int
    error_type: Literal[
        "factual_error",
        "logical_fallacy",
        "contradiction",
        "unsupported_claim",
    ] | None                                      # None 表示未发现错误
    confidence: float                             # 0.0 ~ 1.0
    reasoning: str                                # 推理摘要，展示在 Tooltip
    evidence_urls: list[str] = Field(default_factory=list)


class ErrorEvent(BaseModel):
    """工具失败、LLM 超时等异常，不中断整体流程。"""
    type: Literal["error"] = "error"
    timestamp: str = Field(default_factory=_now)
    claim_id: str | None                          # None 表示全局错误（非单条声明）
    message: str


class StatusEvent(BaseModel):
    """分阶段状态事件，用于前后端调试和进度展示。"""
    type: Literal["status"] = "status"
    timestamp: str = Field(default_factory=_now)
    stage: str
    message: str
    claim_id: str | None = None
    details: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class DebateEvent(BaseModel):
    """多 Agent 辩论事件，供前端时间轴与数据库统一消费。"""
    type: Literal["debate"] = "debate"
    timestamp: str = Field(default_factory=_now)
    claim_id: str
    round: int
    phase: Literal["started", "argument", "result"]
    role: Literal["coordinator", "verifier", "challenger", "judge"]
    message: str
    stance: Literal["support", "challenge", "neutral"] = "neutral"
    confidence: float | None = None
    evidence_urls: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class SummaryEvent(BaseModel):
    """分析总结事件，供前端总结卡片和后端落库复用。"""
    type: Literal["summary"] = "summary"
    timestamp: str = Field(default_factory=_now)
    total_claims: int
    total_annotations: int
    clean_claims: int
    challenged_claims: int
    revised_claims: int
    error_breakdown: dict[str, int] = Field(default_factory=dict)
    representative_claims: list[dict[str, Any]] = Field(default_factory=list)
    overall_conclusion: str
    reverify_supported: bool = True


class ChatEvent(BaseModel):
    """为后续对话模块预留的事件结构，当前由 B/C 在接口层接入。"""
    type: Literal["chat"] = "chat"
    timestamp: str = Field(default_factory=_now)
    session_id: str | None = None
    message_id: str | None = None
    claim_id: str | None = None
    role: Literal["user", "assistant", "system"] = "assistant"
    message: str
    reverify_target: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class DoneEvent(BaseModel):
    """所有声明处理完毕。"""
    type: Literal["done"] = "done"
    timestamp: str = Field(default_factory=_now)
    total_annotations: int                        # 发现的错误标注总数
    total_claims: int = 0
    summary_available: bool = False
    reverify_supported: bool = True
    claim_results: list[dict[str, Any]] = Field(default_factory=list)


class ChatChunkEvent(BaseModel):
    """聊天 SSE 流式输出的单个 token 片段。"""
    type: Literal["chat_chunk"] = "chat_chunk"
    timestamp: str = Field(default_factory=_now)
    content: str
    message_id: str | None = None                 # 首次 chunk 时分配，后续一致


class ChatDoneEvent(BaseModel):
    """聊天流结束，包含完整回复的 message_id 用于持久化。"""
    type: Literal["chat_done"] = "chat_done"
    timestamp: str = Field(default_factory=_now)
    message_id: str
    session_id: str
    full_content: str | None = None               # 完整回复文本（可选，用于前端回显）


# ---------------------------------------------------------------------------
# Union 类型，B 的 SSE 接口 yield 此类型
# ---------------------------------------------------------------------------

AgentState = Union[
    PlanEvent,
    ThinkingEvent,
    ToolCallEvent,
    AnnotationEvent,
    ErrorEvent,
    StatusEvent,
    DebateEvent,
    SummaryEvent,
    ChatEvent,
    DoneEvent,
    ChatChunkEvent,
    ChatDoneEvent,
]
