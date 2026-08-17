#!/usr/bin/env python3
"""
analyze_claims.py — 跳过文章扫描，直接对声明列表执行事实核查。

用法：
    python analyze_claims.py input.json output.json

输入文件格式（input.json）：
{
    "claims": [
        "中国 2024 年 GDP 增速达到 6.5%",
        "全球新冠死亡人数已超过 700 万"
    ],
    "skill": null,
    "overlays": [],
    "disabled_tools": []
}

字段说明：
  - claims (必填):     声明文本列表
  - skill (可选):       手动指定领域，如 "medical" / "finance" / "general"；
                        null 或省略则自动从 claims 内容路由
  - overlays (可选):    附加视角配置，格式同 /analyze 接口
  - disabled_tools:     禁用的工具名列表

输出文件格式（output.json）：
{
    "skills": {"c001": "finance", "c002": "medical"},
    "summary": { ... },
    "claim_results": [
        {
            "claim_id": "c001",
            "text": "...",
            "skill": "finance",
            "verifier": { "error_type": ..., "confidence": ..., "reasoning": ..., ... },
            "challenger": { "stance": ..., "confidence": ..., "reasoning": ..., ... },
            "rebuttal": null,
            "judge": { "error_type": ..., "confidence": ..., "reasoning": ..., ... },
            "can_reverify": true
        }
    ]
}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, TextIO

# 确保项目根目录在 sys.path 中，使得 agent 包可导入
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("analyze_claims")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="对声明列表执行事实核查（跳过文章扫描）",
    )
    parser.add_argument(
        "input", type=str,
        help="输入 JSON 文件路径，包含 claims 列表",
    )
    parser.add_argument(
        "output", type=str,
        help="输出 JSON 文件路径",
    )
    parser.add_argument(
        "--skill", type=str, default=None,
        help="手动指定领域 skill（覆盖输入文件中的 skill 字段）",
    )
    parser.add_argument(
        "--no-skill-routing", action="store_true", default=False,
        help="禁用领域路由，所有 claim 强制使用 general skill（消融实验用）",
    )
    parser.add_argument(
        "--debate-mode", type=str, default="full",
        choices=["full", "verifier_only", "verifier_challenger"],
        help="辩论深度：full（默认，Verifier→Challenger→Judge）、"
             "verifier_only（仅 Verifier）、verifier_challenger（跳过 Judge）",
    )
    parser.add_argument(
        "--llm", type=str, default=None,
        choices=["glm", "claude", "deepseek"],
        help="主流程 LLM：默认读取 AGENT_LLM_PROVIDER，未设置时使用 glm；Claude 仅作为备用",
    )
    return parser.parse_args()


def load_input(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("输入 JSON 必须是对象（dict）")
    claims = data.get("claims", [])
    if not isinstance(claims, list) or len(claims) == 0:
        raise ValueError("输入 JSON 中 'claims' 必须是非空列表")
    for i, c in enumerate(claims):
        if not isinstance(c, str) or not c.strip():
            raise ValueError(f"claims[{i}] 必须是非空字符串")
    return data


def _dump_json(value: Any, fp: TextIO, indent: int = 2) -> None:
    json.dump(value, fp, ensure_ascii=False, indent=indent)


async def analyze_claims_streaming(
    agent,
    claims: list[str],
    output_path: str,
    skill_name: str | None,
    overlays: list[dict],
    disabled_tools: list[str],
    disable_skill_routing: bool,
    debate_mode: str,
) -> dict[str, Any]:
    """逐条分析 claim，并在每条完成后立即追加写入输出文件。"""
    from agent.agents import get_domain_agent
    from agent.debate import build_claim_result, build_summary_event
    from agent.models import Claim
    from agent.skills import build_overlay_skill, route_skill
    from agent.skills.base import GENERAL_SKILL_NAME

    if debate_mode not in {"full", "verifier_only", "verifier_challenger"}:
        raise ValueError(f"不支持的 debate_mode：{debate_mode}")

    active_overlays = []
    for raw_overlay in overlays or []:
        try:
            active_overlays.append(build_overlay_skill(raw_overlay))
        except (TypeError, ValueError) as exc:
            logger.warning("跳过无效 overlay：%s", exc)

    disabled = agent._normalize_disabled(disabled_tools)
    config = {
        "disable_skill_routing": disable_skill_routing,
        "debate_mode": debate_mode,
        "skill": skill_name,
        "overlays": [overlay.name for overlay in active_overlays],
        "disabled_tools": sorted(disabled),
        "streaming": True,
    }

    async def _resolve_skill(claim_text: str):
        if disable_skill_routing:
            return agent._skills[GENERAL_SKILL_NAME]
        if skill_name:
            if skill_name not in agent._skills:
                raise ValueError(
                    f"未知 skill：{skill_name}；可选值：{sorted(agent._skills)}"
                )
            return agent._skills[skill_name]
        return await route_skill(claim_text, agent._skills, agent._router_llm)

    records = []
    skill_map: dict[str, str] = {}
    claim_results: list[dict[str, Any]] = []

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("{\n  \"config\": ")
        _dump_json(config, f)
        f.write(",\n  \"claim_results\": [\n")

        for idx, text in enumerate(claims, start=1):
            claim_text = str(text).strip()
            claim = Claim(
                id=f"c{idx:03d}",
                text=claim_text,
                position=(0, len(claim_text)),
                suspicion_score=1.0,
                complexity="complex",
                complexity_confidence=1.0,
            )

            try:
                skill = await _resolve_skill(claim.text)
                removed = tuple(t for t in skill.allowed_tools if t in disabled)
                effective_skill = replace(
                    skill,
                    allowed_tools=tuple(t for t in skill.allowed_tools if t not in disabled),
                )
                domain_agent = agent._domain_agent_cache.get(effective_skill.name)
                if domain_agent is None:
                    domain_agent = get_domain_agent(
                        effective_skill.name,
                        effective_skill,
                        agent._llm,
                    )
                    agent._domain_agent_cache[effective_skill.name] = domain_agent

                skill_map[claim.id] = effective_skill.name
                strategy = domain_agent.merge_strategy("complex")

                if debate_mode == "full":
                    record_sink = {"record": None}
                    async for _event in agent._debate_claim(
                        claim,
                        effective_skill,
                        active_overlays,
                        record_sink,
                        removed,
                        strategy=strategy,
                        domain_agent=domain_agent,
                    ):
                        pass
                    record = record_sink.get("record")
                    if record is None:
                        record = agent._build_failed_claim_record(
                            claim,
                            effective_skill.name,
                            "完整辩论流程未返回记录",
                        )
                elif debate_mode == "verifier_only":
                    record = await agent._analyze_claim_verifier_only(
                        claim,
                        effective_skill,
                        active_overlays,
                        removed,
                        strategy,
                        domain_agent,
                    )
                else:
                    record = await agent._analyze_claim_verifier_challenger(
                        claim,
                        effective_skill,
                        active_overlays,
                        removed,
                        strategy,
                        domain_agent,
                    )
            except Exception as exc:
                logger.exception("[%s] 离线 claim 分析失败", claim.id)
                fallback_skill = skill_name or GENERAL_SKILL_NAME
                skill_map[claim.id] = fallback_skill
                record = agent._build_failed_claim_record(claim, fallback_skill, str(exc))

            records.append(record)
            claim_result = build_claim_result(record)
            claim_results.append(claim_result)

            if idx > 1:
                f.write(",\n")
            f.write("    ")
            _dump_json(claim_result, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
            logger.info("[%s] 已写入输出文件：%s", claim.id, output_path)

        summary_event = build_summary_event(records)
        summary = summary_event.model_dump()
        f.write("\n  ],\n  \"summary\": ")
        _dump_json(summary, f)
        f.write(",\n  \"skills\": ")
        _dump_json(skill_map, f)
        f.write("\n}\n")
        f.flush()
        os.fsync(f.fileno())

    return {
        "skills": skill_map,
        "config": config,
        "summary": summary,
        "claim_results": claim_results,
    }


async def main() -> None:
    args = parse_args()

    # 加载输入
    input_data = load_input(args.input)
    claims: list[str] = [c.strip() for c in input_data["claims"]]
    skill_name: str | None = args.skill or input_data.get("skill")
    overlays: list[dict] = input_data.get("overlays") or []
    disabled_tools: list[str] = input_data.get("disabled_tools") or []

    logger.info("输入：%d 条声明，skill=%s，overlays=%d，disabled_tools=%d",
                len(claims), skill_name, len(overlays), len(disabled_tools))

    # 初始化 Agent
    from agent import Agent, create_chat_llm, create_core_llm, create_router_llm

    complex_llm = create_core_llm(args.llm)
    router_llm = create_router_llm(fallback=complex_llm)

    try:
        chat_llm = create_chat_llm()
    except Exception:
        chat_llm = None

    agent = Agent(complex_llm=complex_llm, router_llm=router_llm, chat_llm=chat_llm)

    # 执行分析，并在每条 claim 完成后立即写入输出文件
    result = await analyze_claims_streaming(
        agent=agent,
        claims=claims,
        output_path=args.output,
        skill_name=skill_name,
        overlays=overlays,
        disabled_tools=disabled_tools,
        disable_skill_routing=args.no_skill_routing,
        debate_mode=args.debate_mode,
    )

    # 打印摘要
    output_path = args.output
    summary = result.get("summary", {})
    config = result.get("config", {})
    logger.info("完成：total_claims=%d total_annotations=%d",
                summary.get("total_claims", 0),
                summary.get("total_annotations", 0))
    print(f"\n分析完成，结果已写入：{output_path}")
    print(f"   配置：skill_routing={'off' if config.get('disable_skill_routing') else 'auto'}  debate_mode={config.get('debate_mode', 'full')}")
    print(f"   声明总数：{summary.get('total_claims', 0)}")
    print(f"   发现错误：{summary.get('total_annotations', 0)}")
    print(f"   干净声明：{summary.get('clean_claims', 0)}")


if __name__ == "__main__":
    asyncio.run(main())
