"""
backend/services/context_builder.py

构建聊天上下文：从数据库查询会话的所有相关信息，
组装为 Agent.chat() 可用的 session_context dict。
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from ..db.models import AnalysisSession, ClaimRecord, SummaryRecord


def build_chat_context(
    db: Session,
    session_id: str,
    max_article_chars: int = 3000,
) -> dict:
    """
    查询会话的完整上下文，返回字典：
      {
        "session_id": str,
        "article_title": str | None,
        "article_text": str (截断至 max_article_chars),
        "claims": [
          {
            "claim_id": str,
            "text": str,
            "verdict": str,
            "error_type": str | None,
            "confidence": float | None,
            "reasoning": str | None,
            "evidence_urls": list[str],
          },
          ...
        ],
        "summary": {
          "overall_conclusion": str | None,
          "total_claims": int | None,
          "total_errors": int | None,
          "error_breakdown": dict | None,
        } | None,
      }
    """
    sess = db.query(AnalysisSession).filter(
        AnalysisSession.id == session_id
    ).first()

    article_text = ""
    if sess and sess.article_text:
        article_text = sess.article_text
        if len(article_text) > max_article_chars:
            article_text = article_text[:max_article_chars] + "\n…（文章过长，已截断）"

    # 查询所有声明
    claim_rows = (
        db.query(ClaimRecord)
        .filter(ClaimRecord.session_id == session_id)
        .order_by(ClaimRecord.created_at.asc())
        .all()
    )

    claims = []
    for c in claim_rows:
        evidence_urls = []
        if c.evidence_urls:
            try:
                evidence_urls = json.loads(c.evidence_urls)
            except json.JSONDecodeError:
                pass

        claims.append({
            "claim_id": c.claim_id,
            "text": c.text,
            "verdict": c.verdict or "pending",
            "error_type": c.error_type,
            "confidence": c.confidence,
            "reasoning": c.reasoning,
            "evidence_urls": evidence_urls,
        })

    # 查询总结
    summary_row = (
        db.query(SummaryRecord)
        .filter(SummaryRecord.session_id == session_id)
        .first()
    )

    summary = None
    if summary_row:
        eb = None
        if summary_row.error_breakdown:
            try:
                eb = json.loads(summary_row.error_breakdown)
            except json.JSONDecodeError:
                pass

        summary = {
            "overall_conclusion": summary_row.overall_conclusion,
            "total_claims": summary_row.total_claims,
            "total_errors": summary_row.total_errors,
            "error_breakdown": eb,
        }

    return {
        "session_id": session_id,
        "article_title": sess.article_title if sess else None,
        "article_text": article_text,
        "article_summary": sess.article_summary if sess else None,
        "claims": claims,
        "summary": summary,
    }
