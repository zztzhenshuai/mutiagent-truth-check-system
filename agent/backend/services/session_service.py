# backend/services/session_service.py
"""Session CRUD — 对应 /api/v1/sessions/* 的业务逻辑."""

from __future__ import annotations

import json
from typing import Optional

from sqlalchemy.orm import Session

from ..db.models import (
    AnalysisSession,
    ClaimRecord,
    EventRecord,
    SummaryRecord,
)
from ..schemas.session import (
    SessionCreate,
    SessionDetail,
    SessionDetailClaim,
    SessionDetailEvent,
    SessionDetailSession,
    SessionDetailSummary,
    SessionListItem,
    SessionListResponse,
)


class SessionService:

    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # CREATE
    # ------------------------------------------------------------------
    def create_session(
        self, data: SessionCreate, device_id: str | None
    ) -> AnalysisSession:
        session = AnalysisSession(
            device_id=device_id,
            article_title=data.article_title,
            source_url=data.source_url,
            article_text=data.article_text,
            domain=data.domain,
            skill_id=data.skill_id,
        )
        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)
        return session

    # ------------------------------------------------------------------
    # GET by ID (with 404 handling in router)
    # ------------------------------------------------------------------
    def get_session(self, session_id: str) -> AnalysisSession | None:
        return (
            self.db.query(AnalysisSession)
            .filter(AnalysisSession.id == session_id)
            .first()
        )

    # ------------------------------------------------------------------
    # LIST
    # ------------------------------------------------------------------
    def list_sessions(
        self,
        limit: int = 20,
        offset: int = 0,
        status: str | None = None,
        domain: str | None = None,
        device_id: str | None = None,
    ) -> SessionListResponse:
        q = self.db.query(AnalysisSession)
        if status:
            q = q.filter(AnalysisSession.status == status)
        if domain:
            q = q.filter(AnalysisSession.domain == domain)
        if device_id:
            q = q.filter(AnalysisSession.device_id == device_id)

        total = q.count()
        rows = (
            q.order_by(AnalysisSession.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        items = [
            SessionListItem(
                session_id=r.id,
                article_title=r.article_title,
                domain=r.domain,
                status=r.status,
                total_claims=r.total_claims,
                total_annotations=r.total_annotations,
                created_at=r.created_at,
            )
            for r in rows
        ]
        return SessionListResponse(items=items, total=total)

    # ------------------------------------------------------------------
    # DETAIL
    # ------------------------------------------------------------------
    def get_session_detail(self, session_id: str) -> SessionDetail | None:
        sess = self.get_session(session_id)
        if sess is None:
            return None

        # summary
        summary_row = (
            self.db.query(SummaryRecord)
            .filter(SummaryRecord.session_id == session_id)
            .first()
        )
        summary = None
        if summary_row:
            eb = None
            re_ev = None
            if summary_row.error_breakdown:
                try:
                    eb = json.loads(summary_row.error_breakdown)
                except json.JSONDecodeError:
                    eb = {}
            if summary_row.representative_evidence:
                try:
                    re_ev = json.loads(summary_row.representative_evidence)
                except json.JSONDecodeError:
                    re_ev = []
            summary = SessionDetailSummary(
                overall_conclusion=summary_row.overall_conclusion,
                total_claims=summary_row.total_claims,
                total_errors=summary_row.total_errors,
                error_breakdown=eb,
                representative_evidence=re_ev,
                confidence=summary_row.confidence,
            )

        # claims
        claim_rows = (
            self.db.query(ClaimRecord)
            .filter(ClaimRecord.session_id == session_id)
            .order_by(ClaimRecord.created_at.asc())
            .all()
        )
        claims = [
            SessionDetailClaim(
                claim_id=c.claim_id,
                text=c.text,
                error_type=c.error_type,
                confidence=c.confidence,
                verdict=c.verdict or "pending",
            )
            for c in claim_rows
        ]

        # recent events (last 20)
        event_rows = (
            self.db.query(EventRecord)
            .filter(EventRecord.session_id == session_id)
            .order_by(EventRecord.seq.desc())
            .limit(20)
            .all()
        )
        recent_events = [
            SessionDetailEvent(
                seq=e.seq,
                event_type=e.event_type,
                payload=json.loads(e.payload) if e.payload else {},
            )
            for e in reversed(event_rows)
        ]

        return SessionDetail(
            session=SessionDetailSession(
                id=sess.id,
                device_id=sess.device_id,
                article_title=sess.article_title,
                source_url=sess.source_url,
                domain=sess.domain,
                skill_id=sess.skill_id,
                status=sess.status,
                total_claims=sess.total_claims,
                total_annotations=sess.total_annotations,
                created_at=sess.created_at,
                started_at=sess.started_at,
                finished_at=sess.finished_at,
                error_message=sess.error_message,
            ),
            summary=summary,
            claims=claims,
            recent_events=recent_events,
        )

    # ------------------------------------------------------------------
    # UPDATE helpers
    # ------------------------------------------------------------------
    def update_session_status(
        self, session_id: str, status: str, **kwargs
    ) -> None:
        sess = self.get_session(session_id)
        if sess:
            sess.status = status
            for k, v in kwargs.items():
                setattr(sess, k, v)
            self.db.commit()

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    def get_summary(self, session_id: str) -> SummaryRecord | None:
        return (
            self.db.query(SummaryRecord)
            .filter(SummaryRecord.session_id == session_id)
            .first()
        )

    # ------------------------------------------------------------------
    # Claim detail
    # ------------------------------------------------------------------
    def get_claim(self, session_id: str, claim_id: str) -> ClaimRecord | None:
        return (
            self.db.query(ClaimRecord)
            .filter(
                ClaimRecord.session_id == session_id,
                ClaimRecord.claim_id == claim_id,
            )
            .first()
        )
