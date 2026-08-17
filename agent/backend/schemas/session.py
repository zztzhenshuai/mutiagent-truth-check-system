# backend/schemas/session.py
"""Session-related request/response schemas."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------
class SessionCreate(BaseModel):
    article_title: Optional[str] = None
    source_url: Optional[str] = None
    article_text: Optional[str] = None
    domain: Optional[str] = None
    skill_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Response items
# ---------------------------------------------------------------------------
class SessionResponse(BaseModel):
    session_id: str
    status: str
    created_at: str


class SessionListItem(BaseModel):
    session_id: str
    article_title: Optional[str] = None
    domain: Optional[str] = None
    status: str
    total_claims: Optional[int] = None
    total_annotations: Optional[int] = None
    created_at: str


class SessionListResponse(BaseModel):
    items: list[SessionListItem]
    total: int


class SessionDetailSession(BaseModel):
    id: str
    device_id: Optional[str] = None
    article_title: Optional[str] = None
    source_url: Optional[str] = None
    domain: Optional[str] = None
    skill_id: Optional[str] = None
    status: str
    total_claims: Optional[int] = None
    total_annotations: Optional[int] = None
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error_message: Optional[str] = None


class SessionDetailSummary(BaseModel):
    overall_conclusion: Optional[str] = None
    total_claims: Optional[int] = None
    total_errors: Optional[int] = None
    error_breakdown: Optional[dict] = None
    representative_evidence: Optional[list] = None
    confidence: Optional[float] = None


class SessionDetailClaim(BaseModel):
    claim_id: str
    text: str
    error_type: Optional[str] = None
    confidence: Optional[float] = None
    verdict: str


class SessionDetailEvent(BaseModel):
    seq: int
    event_type: str
    payload: dict


class SessionDetail(BaseModel):
    session: SessionDetailSession
    summary: Optional[SessionDetailSummary] = None
    claims: list[SessionDetailClaim] = []
    recent_events: list[SessionDetailEvent] = []
