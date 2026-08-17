"""
tests/test_context_builder.py

测试 build_chat_context() — 从 DB 查询并组装聊天上下文。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.db.models import Base, AnalysisSession, ClaimRecord, SummaryRecord
from backend.services.context_builder import build_chat_context


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_build_context_empty_session(db):
    """不存在的会话应返回空的 claims/summary。"""
    ctx = build_chat_context(db, "nonexistent")
    assert ctx["session_id"] == "nonexistent"
    assert ctx["article_title"] is None
    assert ctx["claims"] == []
    assert ctx["summary"] is None


def test_build_context_with_claims(db):
    """有声明记录时应正确组装。"""
    sess = AnalysisSession(
        id="sess_1",
        article_title="测试标题",
        article_text="这是正文内容，包含一条声明。",
        status="completed",
    )
    db.add(sess)

    claim = ClaimRecord(
        session_id="sess_1",
        claim_id="c001",
        text="测试声明",
        verdict="rejected",
        error_type="factual_error",
        confidence=0.85,
        reasoning="测试推理",
        evidence_urls='["https://example.com/ev1"]',
    )
    db.add(claim)

    summary = SummaryRecord(
        session_id="sess_1",
        overall_conclusion="测试总结",
        total_claims=1,
        total_errors=1,
        error_breakdown='{"factual_error": 1}',
    )
    db.add(summary)
    db.commit()

    ctx = build_chat_context(db, "sess_1")
    assert ctx["article_title"] == "测试标题"
    assert "这是正文内容" in ctx["article_text"]
    assert len(ctx["claims"]) == 1
    assert ctx["claims"][0]["claim_id"] == "c001"
    assert ctx["claims"][0]["verdict"] == "rejected"
    assert ctx["claims"][0]["evidence_urls"] == ["https://example.com/ev1"]
    assert ctx["summary"]["overall_conclusion"] == "测试总结"


def test_build_context_truncates_long_article(db):
    """长文章应截断。"""
    sess = AnalysisSession(
        id="sess_2",
        article_text="A" * 5000,
        status="completed",
    )
    db.add(sess)
    db.commit()

    ctx = build_chat_context(db, "sess_2", max_article_chars=100)
    assert len(ctx["article_text"]) <= 100 + 50  # allowance for truncation suffix
    assert "（文章过长，已截断）" in ctx["article_text"]


def test_build_context_without_summary(db):
    """无总结记录时 summary 应为 None。"""
    sess = AnalysisSession(id="sess_3", status="completed")
    db.add(sess)
    db.commit()

    ctx = build_chat_context(db, "sess_3")
    assert ctx["summary"] is None
