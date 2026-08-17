"""
agent/debate.py

成员 A 的辩论辅助模块：
- 统一保存单条声明在 verifier / challenger / judge 阶段的结构化结果
- 提供辩论提示词与容错解析
- 生成 summary 事件和可持久化的 claim 结果
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from json_repair import json_repair

from .models import AnnotationEvent, Claim, SummaryEvent, ToolCallEvent

logger = logging.getLogger("agent.debate")

ErrorType = Literal[
    "factual_error",
    "logical_fallacy",
    "contradiction",
    "unsupported_claim",
] | None

DebateStance = Literal["support", "challenge"]


@dataclass
class VerificationRecord:
    role: str
    annotation: AnnotationEvent
    tool_calls: list[ToolCallEvent] = field(default_factory=list)
    thoughts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class ChallengeRecord:
    stance: DebateStance
    confidence: float
    reasoning: str
    missing_evidence: list[str] = field(default_factory=list)
    suggested_queries: list[str] = field(default_factory=list)


@dataclass
class ClaimDebateRecord:
    claim: Claim
    skill_name: str
    verifier: VerificationRecord
    challenger: ChallengeRecord
    judge_annotation: AnnotationEvent
    rebuttal: VerificationRecord | None = None

    @property
    def revised_after_challenge(self) -> bool:
        if self.rebuttal is None:
            return False
        return (
            self.verifier.annotation.error_type != self.judge_annotation.error_type
            or self.verifier.annotation.reasoning != self.judge_annotation.reasoning
        )


def build_challenger_prompt(
    claim_text: str,
    skill_name: str,
    verifier: VerificationRecord,
) -> str:
    tool_context = "\n".join(
        f"- {call.tool_name}: {call.tool_input} => {call.tool_output}"
        for call in verifier.tool_calls
    ) or "- 无工具调用记录"
    evidence = verifier.annotation.evidence_urls or []
    return (
        "你是 Challenger Agent，职责是质疑 Verifier Agent 的结论是否证据充分。\n"
        "你不能调用工具，只能依据 claim、已有推理和已有工具输出判断是否需要重验证。\n\n"
        f"领域 skill: {skill_name}\n"
        f"声明: {claim_text}\n"
        f"Verifier 结论 error_type: {verifier.annotation.error_type}\n"
        f"Verifier confidence: {verifier.annotation.confidence}\n"
        f"Verifier reasoning: {verifier.annotation.reasoning}\n"
        f"Verifier evidence_urls: {evidence}\n"
        "Verifier 工具记录:\n"
        f"{tool_context}\n\n"
        "请只返回 JSON，不要解释，不要 markdown：\n"
        '{"stance":"support"|"challenge","confidence":0.0,'
        '"reasoning":"中文一句话","missing_evidence":["最多3条"],"suggested_queries":["最多3条"]}'
    )


def parse_challenger_response(raw: str) -> ChallengeRecord:
    try:
        data = json_repair.loads(raw)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        logger.warning("Challenger 响应无法解析为 JSON，使用默认 support 立场：%r", raw[:200])
        data = {}

    stance = str(data.get("stance") or "support").strip().lower()
    if stance not in {"support", "challenge"}:
        stance = "support"

    try:
        confidence = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    reasoning = str(data.get("reasoning") or "挑战方未提出新的有效异议。").strip()
    missing_evidence = _to_string_list(data.get("missing_evidence"))
    suggested_queries = _to_string_list(data.get("suggested_queries"))
    return ChallengeRecord(
        stance=stance,
        confidence=confidence,
        reasoning=reasoning,
        missing_evidence=missing_evidence[:3],
        suggested_queries=suggested_queries[:3],
    )


def build_rebuttal_instruction(challenge: ChallengeRecord) -> str:
    missing = "；".join(challenge.missing_evidence) or "未指明"
    queries = "；".join(challenge.suggested_queries) or "未提供"
    return (
        "Challenger Agent 对当前结论提出了异议，请你重新验证并直接修正或确认结论。\n"
        f"重点补查：{missing}\n"
        f"建议检索方向：{queries}\n"
        "若原结论成立，请补足证据；若原结论不成立，请修正 Final Answer。"
    )


def build_judge_prompt(
    claim_text: str,
    skill_name: str,
    verifier: VerificationRecord,
    challenger: ChallengeRecord,
    rebuttal: VerificationRecord | None = None,
) -> str:
    rebuttal_section = ""
    if rebuttal is not None:
        rebuttal_section = (
            f"Rebuttal error_type: {rebuttal.annotation.error_type}\n"
            f"Rebuttal confidence: {rebuttal.annotation.confidence}\n"
            f"Rebuttal reasoning: {rebuttal.annotation.reasoning}\n"
            f"Rebuttal evidence_urls: {rebuttal.annotation.evidence_urls}\n"
        )
    return (
        "你是 Judge Agent，职责是对 Verifier 和 Challenger 的辩论做最终裁决。\n"
        "必须输出可直接持久化的最终结论，不能输出其他解释。\n\n"
        f"领域 skill: {skill_name}\n"
        f"声明: {claim_text}\n"
        f"Verifier error_type: {verifier.annotation.error_type}\n"
        f"Verifier confidence: {verifier.annotation.confidence}\n"
        f"Verifier reasoning: {verifier.annotation.reasoning}\n"
        f"Verifier evidence_urls: {verifier.annotation.evidence_urls}\n"
        f"Challenger stance: {challenger.stance}\n"
        f"Challenger confidence: {challenger.confidence}\n"
        f"Challenger reasoning: {challenger.reasoning}\n"
        f"Challenger missing_evidence: {challenger.missing_evidence}\n"
        f"{rebuttal_section}\n"
        "请只返回 JSON，不要 markdown：\n"
        '{"error_type":"factual_error"|"logical_fallacy"|"contradiction"|"unsupported_claim"|null,'
        '"confidence":0.0,'
        '"reasoning":"中文推理摘要",'
        '"evidence_urls":["url1"],'
        '"debate_summary":"一句话说明最终裁决依据"}'
    )


def parse_judge_response(
    raw: str,
    claim: Claim,
    fallback: AnnotationEvent,
) -> tuple[AnnotationEvent, str]:
    try:
        data = json_repair.loads(raw)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        logger.warning(
            "[%s] Judge 响应无法解析为 JSON，回退到兜底结论：%r", claim.id, raw[:200],
        )
        data = {}

    error_type = data.get("error_type", fallback.error_type)
    if error_type not in {
        "factual_error",
        "logical_fallacy",
        "contradiction",
        "unsupported_claim",
        None,
    }:
        error_type = fallback.error_type

    try:
        confidence = float(data.get("confidence", fallback.confidence))
    except (TypeError, ValueError):
        confidence = fallback.confidence

    reasoning = str(data.get("reasoning") or fallback.reasoning).strip()
    evidence_urls = _to_string_list(data.get("evidence_urls")) or list(fallback.evidence_urls)
    debate_summary = str(data.get("debate_summary") or reasoning).strip()

    annotation = AnnotationEvent(
        claim_id=claim.id,
        text=claim.text,
        start_offset=claim.position[0],
        end_offset=claim.position[1],
        error_type=error_type,
        confidence=max(0.0, min(1.0, confidence)),
        reasoning=reasoning,
        evidence_urls=evidence_urls,
    )
    return annotation, debate_summary


def build_summary_event(records: list[ClaimDebateRecord]) -> SummaryEvent:
    total_claims = len(records)
    total_annotations = sum(1 for record in records if record.judge_annotation.error_type is not None)
    clean_claims = total_claims - total_annotations

    error_breakdown = {
        "factual_error": 0,
        "logical_fallacy": 0,
        "contradiction": 0,
        "unsupported_claim": 0,
    }
    challenged_claims = sum(1 for record in records if record.challenger.stance == "challenge")
    revised_claims = sum(1 for record in records if record.revised_after_challenge)

    for record in records:
        annotation = record.judge_annotation
        if annotation.error_type is not None:
            error_breakdown[annotation.error_type] += 1

    representative_claims: list[dict[str, Any]] = []
    sorted_records = sorted(records, key=lambda item: item.judge_annotation.confidence, reverse=True)
    for record in sorted_records[:3]:
        annotation = record.judge_annotation
        representative_claims.append(
            {
                "claim_id": record.claim.id,
                "text": record.claim.text,
                "error_type": annotation.error_type,
                "confidence": annotation.confidence,
                "reasoning": annotation.reasoning,
                "evidence_urls": annotation.evidence_urls,
                "skill": record.skill_name,
                "can_reverify": True,
            }
        )

    if total_annotations == 0:
        overall_conclusion = "本次核查未发现明确错误，但仍建议保留人工复核入口。"
    elif total_annotations >= max(1, total_claims // 2):
        overall_conclusion = "本次文章存在较高风险声明，建议优先查看高置信度错误和证据链。"
    else:
        overall_conclusion = "本次文章存在部分可疑声明，建议结合代表性证据逐条查看。"

    return SummaryEvent(
        total_claims=total_claims,
        total_annotations=total_annotations,
        clean_claims=clean_claims,
        challenged_claims=challenged_claims,
        revised_claims=revised_claims,
        error_breakdown=error_breakdown,
        representative_claims=representative_claims,
        overall_conclusion=overall_conclusion,
        reverify_supported=True,
    )


def build_claim_result(record: ClaimDebateRecord) -> dict[str, Any]:
    return {
        "claim_id": record.claim.id,
        "text": record.claim.text,
        "skill": record.skill_name,
        "verifier": {
            "error_type": record.verifier.annotation.error_type,
            "confidence": record.verifier.annotation.confidence,
            "reasoning": record.verifier.annotation.reasoning,
            "evidence_urls": list(record.verifier.annotation.evidence_urls),
            "tool_calls": [
                {
                    "tool_name": call.tool_name,
                    "tool_input": call.tool_input,
                    "tool_output": call.tool_output,
                }
                for call in record.verifier.tool_calls
            ],
        },
        "challenger": {
            "stance": record.challenger.stance,
            "confidence": record.challenger.confidence,
            "reasoning": record.challenger.reasoning,
            "missing_evidence": list(record.challenger.missing_evidence),
            "suggested_queries": list(record.challenger.suggested_queries),
        },
        "rebuttal": None if record.rebuttal is None else {
            "error_type": record.rebuttal.annotation.error_type,
            "confidence": record.rebuttal.annotation.confidence,
            "reasoning": record.rebuttal.annotation.reasoning,
            "evidence_urls": list(record.rebuttal.annotation.evidence_urls),
            "tool_calls": [
                {
                    "tool_name": call.tool_name,
                    "tool_input": call.tool_input,
                    "tool_output": call.tool_output,
                }
                for call in record.rebuttal.tool_calls
            ],
        },
        "judge": {
            "error_type": record.judge_annotation.error_type,
            "confidence": record.judge_annotation.confidence,
            "reasoning": record.judge_annotation.reasoning,
            "evidence_urls": list(record.judge_annotation.evidence_urls),
        },
        "can_reverify": True,
    }


def _to_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


# ---------------------------------------------------------------------------
# Reflexion 反思（迭代四·方向1，complex 专属）
# ---------------------------------------------------------------------------

_REFLEXION_PROMPT = """\
你是 Reflexion Agent，负责审视 Verifier 之前的推理链是否存在遗漏或逻辑缺陷。

## 背景
你的预设立场是**默认怀疑**：不要轻易接受当前结论，请主动寻找以下可能的问题：
1. 证据是否足够？——工具输出是否真的能支撑结论？
2. 推理是否有漏洞？——是否存在因果倒置、相关不等于因果等逻辑错误？
3. 是否遗漏了其他可能性？——有没有反对证据未被考虑？
4. 置信度是否合理？——如果证据链不完整，应该降低置信度。

## 原始声明
{claim_text}

## 当前结论
- 错误类型: {error_type}
- 置信度: {confidence:.0%}
- 推理摘要: {reasoning}

## 核查工具调用记录
{tool_calls_summary}

## 工具执行错误
{errors_summary}

## 输出要求
请以 JSON 格式输出修正后的结论。你可以：
- 修正 error_type（如果发现分类不准确）
- 降低 confidence（如果证据不充分）
- 补充 reasoning 中对遗漏点的说明

只返回 JSON，不要 markdown，不要解释：
{{"error_type": "factual_error"|"logical_fallacy"|"contradiction"|"unsupported_claim"|null, "confidence": 0.0~1.0, "reasoning": "修正后的推理摘要（含对遗漏点的说明，中文）"}}
"""


def build_reflexion_prompt(
    claim_text: str,
    original_error_type: str,
    original_confidence: float,
    original_reasoning: str,
    tool_calls_summary: str,
    errors_summary: str,
) -> str:
    """构建 Reflexion 反思提示词。"""
    return _REFLEXION_PROMPT.format(
        claim_text=claim_text,
        error_type=original_error_type,
        confidence=original_confidence,
        reasoning=original_reasoning,
        tool_calls_summary=tool_calls_summary,
        errors_summary=errors_summary,
    )


def parse_reflexion_response(
    raw: str,
    claim: Claim,
    fallback: AnnotationEvent,
) -> AnnotationEvent:
    """解析 Reflexion 阶段的输出，容错处理。"""
    try:
        data = json_repair.loads(raw)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        logger.warning(
            "[%s] Reflexion 响应无法解析为 JSON，沿用原结论：%r", claim.id, raw[:200],
        )
        return fallback

    error_type = data.get("error_type", fallback.error_type)
    valid_types = {"factual_error", "logical_fallacy", "contradiction", "unsupported_claim", None}
    if error_type not in valid_types:
        error_type = fallback.error_type

    try:
        confidence = float(data.get("confidence", fallback.confidence))
    except (TypeError, ValueError):
        confidence = fallback.confidence

    reasoning = str(data.get("reasoning") or fallback.reasoning).strip()
    # Reflexion 阶段不修改 evidence_urls（它不能调用工具）
    evidence_urls = list(fallback.evidence_urls)

    return AnnotationEvent(
        claim_id=claim.id,
        text=claim.text,
        start_offset=claim.position[0],
        end_offset=claim.position[1],
        error_type=error_type,
        confidence=max(0.0, min(1.0, confidence)),
        reasoning=reasoning,
        evidence_urls=evidence_urls,
    )
