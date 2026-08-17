"""
tests/test_agent.py

测试迭代三 Agent.run() 的事件序列：
- 保留 scan / plan / done 的基本保障
- 新增 debate / summary 事件覆盖
"""

import pytest

from agent.agent import Agent
from agent.agents import (
    GeneralAgent,
    MedicalAgent,
    get_domain_agent,
)
from agent.llm.base import BaseLLMClient
from agent.models import (
    AnnotationEvent,
    Claim,
    DebateEvent,
    DoneEvent,
    ErrorEvent,
    PlanEvent,
    StatusEvent,
    SummaryEvent,
    ThinkingEvent,
)
from agent.skills import load_skills


class MockLLMClient(BaseLLMClient):

    def __init__(self, responses: list[str]):
        self._responses = iter(responses)

    async def complete(self, messages: list[dict]) -> str:
        return next(self._responses, '{"stance":"support","confidence":0.2,"reasoning":"mock默认回退"}')

    async def complete_with_tools(self, messages, tools):
        raise NotImplementedError


ARTICLE = "中国2023年GDP增速为8.5%，这是一个值得核查的数字。"
ROUTE_GENERAL = '{"skill":"general","confidence":1.0}'
VERIFIER_FINAL = (
    'Thought: 需要核查该数字是否属实\n'
    'Final Answer: {"error_type": "factual_error", "confidence": 0.8, '
    '"reasoning": "该数字存疑，需进一步核实", "evidence_urls": ["https://example.com/evidence"]}'
)
CHALLENGER_SUPPORT = '{"stance":"support","confidence":0.9,"reasoning":"现有证据足以支撑初判。"}'
JUDGE_FINAL = (
    '{"error_type":"factual_error","confidence":0.86,'
    '"reasoning":"综合核查后，该数字应判为事实错误。",'
    '"evidence_urls":["https://example.com/evidence"],'
    '"debate_summary":"裁判采纳了初判，挑战方未提出足够反例。"}'
)


def build_agent_responses(*, scan_claims: str) -> list[str]:
    return [scan_claims, VERIFIER_FINAL, CHALLENGER_SUPPORT, JUDGE_FINAL]


@pytest.mark.asyncio
async def test_run_emits_plan_event_first():
    mock_llm = MockLLMClient(build_agent_responses(scan_claims='{"claims": ["GDP增速为8.5%"]}'))
    router_llm = MockLLMClient([ROUTE_GENERAL])
    agent = Agent(complex_llm=mock_llm, router_llm=router_llm)

    events = []
    async for event in agent.run(ARTICLE):
        events.append(event)

    assert isinstance(events[0], StatusEvent)
    assert any(isinstance(event, PlanEvent) for event in events)


@pytest.mark.asyncio
async def test_run_emits_done_event_last():
    mock_llm = MockLLMClient(build_agent_responses(scan_claims='{"claims": ["GDP增速为8.5%"]}'))
    router_llm = MockLLMClient([ROUTE_GENERAL])
    agent = Agent(complex_llm=mock_llm, router_llm=router_llm)

    events = []
    async for event in agent.run(ARTICLE):
        events.append(event)

    assert isinstance(events[-1], DoneEvent)
    assert events[-1].summary_available is True


@pytest.mark.asyncio
async def test_run_plan_total_matches_claims():
    mock_llm = MockLLMClient(build_agent_responses(scan_claims='{"claims": ["GDP增速为8.5%"]}'))
    router_llm = MockLLMClient([ROUTE_GENERAL])
    agent = Agent(complex_llm=mock_llm, router_llm=router_llm)

    events = []
    async for event in agent.run(ARTICLE):
        events.append(event)

    plan_event = next(event for event in events if isinstance(event, PlanEvent))
    assert plan_event.total == 1


@pytest.mark.asyncio
async def test_run_emits_debate_summary_and_annotation():
    mock_llm = MockLLMClient(build_agent_responses(scan_claims='{"claims": ["GDP增速为8.5%"]}'))
    router_llm = MockLLMClient([ROUTE_GENERAL])
    agent = Agent(complex_llm=mock_llm, router_llm=router_llm)

    events = []
    async for event in agent.run(ARTICLE):
        events.append(event)

    assert any(isinstance(event, ThinkingEvent) for event in events)
    assert any(isinstance(event, DebateEvent) for event in events)
    assert any(isinstance(event, SummaryEvent) for event in events)
    assert any(isinstance(event, AnnotationEvent) for event in events)


@pytest.mark.asyncio
async def test_run_emits_error_on_scan_failure():
    class FailingLLMClient(BaseLLMClient):
        async def complete(self, messages):
            raise RuntimeError("网络超时")

        async def complete_with_tools(self, messages, tools):
            raise NotImplementedError

    agent = Agent(complex_llm=FailingLLMClient(), router_llm=FailingLLMClient())

    events = []
    async for event in agent.run(ARTICLE):
        events.append(event)

    assert isinstance(events[1], ErrorEvent)
    assert isinstance(events[-1], DoneEvent)
    assert "扫描失败" in events[1].message


@pytest.mark.asyncio
async def test_run_no_claims_emits_summary_and_done_with_zero():
    mock_llm = MockLLMClient(['{"claims": []}'])
    router_llm = MockLLMClient([ROUTE_GENERAL])
    agent = Agent(complex_llm=mock_llm, router_llm=router_llm)

    events = []
    async for event in agent.run(ARTICLE):
        events.append(event)

    plan_event = next(event for event in events if isinstance(event, PlanEvent))
    summary_event = next(event for event in events if isinstance(event, SummaryEvent))
    assert plan_event.total == 0
    assert summary_event.total_annotations == 0
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].total_annotations == 0


# ── 方向5：领域专家 Agent 池 测试 ──


def test_domain_agent_selection_medical():
    """验证路由 medical 时获得 MedicalAgent 实例。"""
    skills = load_skills()
    medical_skill = skills["medical"]
    mock_llm = MockLLMClient([])

    agent = get_domain_agent("medical", medical_skill, mock_llm)
    assert isinstance(agent, MedicalAgent)
    assert agent.name == "medical"
    assert agent.allowed_tools == medical_skill.allowed_tools


def test_general_agent_fallback():
    """验证未注册领域回退 GeneralAgent。"""
    skills = load_skills()
    general_skill = skills["general"]
    mock_llm = MockLLMClient([])

    agent = get_domain_agent("nonexistent_domain", general_skill, mock_llm)
    assert isinstance(agent, GeneralAgent)


def test_domain_strategy_merge_modifies_params():
    """验证 MedicalAgent 的 merge_strategy 对 medium 启用 Challenger。"""
    skills = load_skills()
    medical_skill = skills["medical"]
    mock_llm = MockLLMClient([])
    agent = MedicalAgent(medical_skill, mock_llm)

    # medium 应被覆盖为启用 Challenger
    strategy = agent.merge_strategy("medium")
    assert strategy.enable_challenger is True
    assert strategy.max_react_steps == 4

    # simple 不应被覆盖
    strategy_s = agent.merge_strategy("simple")
    assert strategy_s.enable_challenger is False
    assert strategy_s.max_react_steps == 2


def test_general_agent_strategy_merge_unchanged():
    """验证 GeneralAgent 的 merge_strategy 不修改策略参数。"""
    skills = load_skills()
    general_skill = skills["general"]
    mock_llm = MockLLMClient([])
    agent = GeneralAgent(general_skill, mock_llm)

    strategy = agent.merge_strategy("medium")
    assert strategy.enable_challenger is False  # GeneralAgent 不改


def test_domain_agent_calibration_multipliers():
    """验证 MedicalAgent 返回领域特定的校准系数。"""
    skills = load_skills()
    medical_skill = skills["medical"]
    mock_llm = MockLLMClient([])
    agent = MedicalAgent(medical_skill, mock_llm)

    multipliers = agent.get_calibration_multipliers()
    assert multipliers["no_tool"] == 0.70  # 医学领域无工具惩罚更重
    assert multipliers["tool_error"] == 0.80


def test_general_agent_calibration_multipliers_empty():
    """验证 GeneralAgent 返回空校准系数。"""
    skills = load_skills()
    general_skill = skills["general"]
    mock_llm = MockLLMClient([])
    agent = GeneralAgent(general_skill, mock_llm)

    multipliers = agent.get_calibration_multipliers()
    assert multipliers == {}


# ── Reformatter 格式修复机制测试 ──

GARBAGE_NATURAL_LANG = "💭 需要核实英国首相'施凱爾'的身份及其子女信息。"


@pytest.mark.asyncio
async def test_reformat_response_converts_to_final_answer():
    """验证 _reformat_response 能将自然语言 LLM 输出转换为 Final Answer 格式。"""
    REFORMATTED = (
        'Thought: 需要核实英国首相身份\n'
        'Final Answer: {"error_type": "unsupported_claim", "confidence": 0.5, '
        '"reasoning": "搜索结果显示施凯尔是英国首相，但未明确子女年龄", "evidence_urls": []}'
    )
    mock_llm = MockLLMClient([REFORMATTED])
    agent = Agent(complex_llm=mock_llm)

    result = await agent._reformat_response(GARBAGE_NATURAL_LANG, "英国首相施凱爾是一名17岁男孩的父亲")
    assert result == REFORMATTED

    # 验证 reformatter 输出可被正常解析
    parsed = agent._parse_react_response(result)
    assert parsed["type"] == "final"
    assert parsed["answer"]["error_type"] == "unsupported_claim"


@pytest.mark.asyncio
async def test_reformat_response_converts_to_action():
    """验证 _reformat_response 能将"需要搜索"的自然语言转换为 Action 格式。"""
    GARBAGE = "我需要搜索一下英国首相的信息来核实这个声明"
    REFORMATTED = (
        'Thought: 需要搜索英国首相信息进行核实\n'
        'Action: web_search\n'
        'Action Input: Keir Starmer son age 17'
    )
    mock_llm = MockLLMClient([REFORMATTED])
    agent = Agent(complex_llm=mock_llm)

    result = await agent._reformat_response(GARBAGE, "英国首相施凱爾是一名17岁男孩的父亲")
    assert result == REFORMATTED

    # 验证 ACTION 格式可正常解析
    parsed = agent._parse_react_response(result)
    assert parsed["type"] == "action"
    assert parsed["action"] == "web_search"
    assert "Starmer" in parsed["action_input"]


@pytest.mark.asyncio
async def test_reformat_response_returns_none_on_non_repairable():
    """验证 _reformat_response 在 reformatter 输出仍无法解析时返回 None。"""
    still_garbage = "this is still just garbage text"
    mock_llm = MockLLMClient([still_garbage])
    agent = Agent(complex_llm=mock_llm)

    result = await agent._reformat_response(GARBAGE_NATURAL_LANG, "claim text")
    assert result is None


@pytest.mark.asyncio
async def test_reformat_response_returns_none_on_llm_error():
    """验证 _reformat_response 在 LLM 调用异常时返回 None。"""

    class ErroringLLMClient(BaseLLMClient):
        async def complete(self, messages: list[dict]) -> str:
            raise RuntimeError("模拟网络超时")

        async def complete_with_tools(self, messages, tools):
            raise NotImplementedError

    agent = Agent(complex_llm=ErroringLLMClient())
    result = await agent._reformat_response(GARBAGE_NATURAL_LANG, "claim text")
    assert result is None


@pytest.mark.asyncio
async def test_react_loop_reformatter_fixes_output_and_emits_no_format_error():
    """集成测试：ReAct 循环遇到格式异常 → reformatter 修复 → 正常产出结果，不泄露格式错误事件。"""
    REFORMATTED = (
        'Thought: 核实声明中的英国首相身份\n'
        'Final Answer: {"error_type": null, "confidence": 0.8, '
        '"reasoning": "英国首相身份属实，信息正确", "evidence_urls": []}'
    )
    # 第 1 个响应：格式错误的原始输出；第 2 个：reformatter 的输出
    mock_llm = MockLLMClient([GARBAGE_NATURAL_LANG, REFORMATTED])
    agent = Agent(complex_llm=mock_llm)

    skills = load_skills()
    general_skill = skills["general"]

    claim = Claim(
        id="c001",
        text="英国首相施凱爾是一名17岁男孩的父亲",
        position=(0, 25),
        suspicion_score=0.7,
    )

    result_sink: dict = {}
    events: list = []
    async for event in agent._react_loop(
        claim=claim,
        skill=general_skill,
        max_steps=3,
        tool_required=True,
        result_sink=result_sink,
    ):
        events.append(event)

    # 不应产生 "格式异常" ErrorEvent（没有泄露给前端）
    format_errors = [
        e for e in events
        if isinstance(e, ErrorEvent) and "格式异常" in e.message
    ]
    assert len(format_errors) == 0, f"不应该有格式错误事件泄露，但得到了：{format_errors}"

    # 应该产出 ThinkingEvent 和 AnnotationEvent
    annotations = [e for e in events if isinstance(e, AnnotationEvent)]
    assert len(annotations) == 1
    assert annotations[0].confidence == 0.8
    assert annotations[0].reasoning == "英国首相身份属实，信息正确"
