"""
tests/test_planner.py

测试规划器的规则评分逻辑和排序行为。不依赖任何 LLM 调用。
"""

import pytest
from agent.models import Claim
from agent.planner import _compute_score, build_plan


# ---- _compute_score 单元测试 ----

def make_claim(text: str) -> Claim:
    return Claim(id="c001", text=text, position=(0, len(text)), suspicion_score=0.0)


def test_score_with_number():
    assert _compute_score("中国GDP增速为8.5%") >= 0.35


def test_score_with_percentage():
    # 使用长度 >= 8 的字符串，避免触发短文本惩罚
    assert _compute_score("该报告显示占比达到45%以上") >= 0.35


def test_score_with_citation_keyword():
    assert _compute_score("据报道该事件造成重大影响") >= 0.20


def test_score_with_absolute_keyword():
    # any() 只加一次 +0.20，断言 >= 0.20
    assert _compute_score("这是全球唯一的解决方案") >= 0.20


def test_score_with_time():
    assert _compute_score("2023年发生了重要变化") >= 0.15


def test_score_combined():
    # 数字 + 引用词 + 时间，应接近上限
    score = _compute_score("据报道2023年增速达到8.5%")
    assert score >= 0.60


def test_score_short_claim_penalty():
    # 含数字特征的短文本（< 8字）得分低于含同等特征的长文本
    score_short = _compute_score("涨8%")       # 4字，数字+0.35，短文本-0.15 → 0.20
    score_long = _compute_score("该季度同比上涨8%，超出预期")  # 不短，数字+0.35 → 0.35
    assert score_short < score_long


def test_score_clamped_to_one():
    # 堆满所有特征也不应超过 1.0
    score = _compute_score("据报道2023年全球唯一增速8.5%最大最强史上第一")
    assert score <= 1.0


def test_score_clamped_to_zero():
    # 极短文本也不应低于 0.0
    assert _compute_score("好") >= 0.0


# ---- build_plan 测试 ----

def test_build_plan_sorted_descending():
    claims = [
        Claim(id="c001", text="普通句子无特殊词汇", position=(0, 10), suspicion_score=0.0),
        Claim(id="c002", text="据报道2023年增速达到8.5%", position=(10, 20), suspicion_score=0.0),
        Claim(id="c003", text="全球唯一最大", position=(20, 30), suspicion_score=0.0),
    ]
    plan = build_plan(claims)
    scores = [c.suspicion_score for c in plan.claims]
    assert scores == sorted(scores, reverse=True)


def test_build_plan_status_all_pending():
    claims = [
        Claim(id="c001", text="2023年增速8.5%", position=(0, 10), suspicion_score=0.0),
        Claim(id="c002", text="普通句子", position=(10, 20), suspicion_score=0.0),
    ]
    plan = build_plan(claims)
    assert all(v == "pending" for v in plan.status.values())


def test_build_plan_status_keys_match_claim_ids():
    claims = [
        Claim(id="c001", text="增速8.5%", position=(0, 10), suspicion_score=0.0),
        Claim(id="c002", text="2023年事件", position=(10, 20), suspicion_score=0.0),
    ]
    plan = build_plan(claims)
    assert set(plan.status.keys()) == {"c001", "c002"}
