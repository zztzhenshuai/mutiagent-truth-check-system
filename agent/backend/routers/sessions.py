# backend/routers/sessions.py
"""Session-related REST API endpoints.

端点清单（对应 backend-b-interface.md §5）：

  1. POST   /api/v1/sessions                         创建会话
  2. GET    /api/v1/sessions                         会话列表
  3. GET    /api/v1/sessions/{session_id}            会话详情
  4. GET    /api/v1/sessions/{session_id}/events     事件历史
  5. GET    /api/v1/sessions/{session_id}/claims/{claim_id}  单条声明详情
  6. GET    /api/v1/sessions/{session_id}/summary    会话总结
  7. POST   /api/v1/sessions/{session_id}/chat       发送追问
  8. GET    /api/v1/sessions/{session_id}/chat       对话历史
  9. POST   /api/v1/sessions/{session_id}/recheck    重新验证
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..dependencies import get_db, get_device_id
from ..schemas.chat import ChatSend, ChatSendResponse
from ..schemas.claim import ClaimDetailResponse, ToolCallItem
from ..schemas.session import SessionCreate, SessionResponse
from ..services.chat_service import ChatService
from ..services.event_service import EventService
from ..services.session_service import SessionService

router = APIRouter(tags=["sessions"])


class RecheckRequest(BaseModel):
    claim_id: Optional[str] = None
    reason: Optional[str] = None
    skill_id: Optional[str] = None


# ======================================================================
# 1. POST /api/v1/sessions — 创建会话
# ======================================================================
@router.post("/sessions", response_model=SessionResponse, status_code=201)
def create_session(
    data: SessionCreate,
    db: Session = Depends(get_db),
    device_id: str = Depends(get_device_id),
):
    svc = SessionService(db)
    sess = svc.create_session(data, device_id)
    return SessionResponse(
        session_id=sess.id,
        status=sess.status,
        created_at=sess.created_at,
    )


# ======================================================================
# 2. GET /api/v1/sessions — 会话列表
# ======================================================================
@router.get("/sessions")
def list_sessions(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None),
    domain: str | None = Query(None),
    device_id: str | None = Query(None),
    db: Session = Depends(get_db),
):
    svc = SessionService(db)
    return svc.list_sessions(
        limit=limit, offset=offset, status=status, domain=domain, device_id=device_id
    )


# ======================================================================
# 3. GET /api/v1/sessions/{session_id} — 会话详情
# ======================================================================
@router.get("/sessions/{session_id}")
def get_session_detail(
    session_id: str,
    db: Session = Depends(get_db),
):
    svc = SessionService(db)
    detail = svc.get_session_detail(session_id)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "SESSION_NOT_FOUND", "message": "session not found"}},
        )
    return detail


# ======================================================================
# 4. GET /api/v1/sessions/{session_id}/events — 事件历史
# ======================================================================
@router.get("/sessions/{session_id}/events")
def list_session_events(
    session_id: str,
    limit: int = Query(100, ge=1, le=500),
    after_seq: int | None = Query(None),
    event_type: str | None = Query(None),
    db: Session = Depends(get_db),
):
    # verify session exists
    sess_svc = SessionService(db)
    if sess_svc.get_session(session_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "SESSION_NOT_FOUND", "message": "session not found"}},
        )
    svc = EventService(db)
    return svc.list_events(session_id, limit=limit, after_seq=after_seq, event_type=event_type)


# ======================================================================
# 5. GET /api/v1/sessions/{session_id}/claims/{claim_id} — 单条声明详情
# ======================================================================
@router.get("/sessions/{session_id}/claims/{claim_id}")
def get_claim_detail(
    session_id: str,
    claim_id: str,
    db: Session = Depends(get_db),
):
    svc = SessionService(db)
    claim = svc.get_claim(session_id, claim_id)
    if claim is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "CLAIM_NOT_FOUND", "message": "claim not found"}},
        )

    # gather related tool_calls
    tool_calls = [
        ToolCallItem(
            tool_name=t.tool_name,
            tool_input=t.tool_input,
            tool_output=t.tool_output,
            status=t.status,
            latency_ms=t.latency_ms,
            created_at=t.created_at,
        )
        for t in (claim.tool_calls or [])
    ]

    evidence_urls = []
    if claim.evidence_urls:
        try:
            evidence_urls = json.loads(claim.evidence_urls)
        except json.JSONDecodeError:
            evidence_urls = []

    return ClaimDetailResponse(
        claim={
            "claim_id": claim.claim_id,
            "text": claim.text,
            "start_offset": claim.start_offset,
            "end_offset": claim.end_offset,
            "suspicion_score": claim.suspicion_score,
            "verdict": claim.verdict,
            "error_type": claim.error_type,
            "confidence": claim.confidence,
            "reasoning": claim.reasoning,
            "evidence_urls": evidence_urls,
            "created_at": claim.created_at,
            "updated_at": claim.updated_at,
        },
        tool_calls=tool_calls,
        debate=[],
        annotation=None,
    )


# ======================================================================
# 6. GET /api/v1/sessions/{session_id}/summary — 会话总结
# ======================================================================
@router.get("/sessions/{session_id}/summary")
def get_session_summary(
    session_id: str,
    db: Session = Depends(get_db),
):
    svc = SessionService(db)
    sess = svc.get_session(session_id)
    if sess is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "SESSION_NOT_FOUND", "message": "session not found"}},
        )

    summary_row = svc.get_summary(session_id)
    if summary_row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "SUMMARY_NOT_FOUND", "message": "summary not found for this session"}},
        )

    eb = {}
    re_ev = []
    if summary_row.error_breakdown:
        try:
            eb = json.loads(summary_row.error_breakdown)
        except json.JSONDecodeError:
            pass
    if summary_row.representative_evidence:
        try:
            re_ev = json.loads(summary_row.representative_evidence)
        except json.JSONDecodeError:
            pass

    return {
        "session_id": session_id,
        "overall_conclusion": summary_row.overall_conclusion,
        "total_claims": summary_row.total_claims,
        "total_errors": summary_row.total_errors,
        "error_breakdown": eb,
        "representative_evidence": re_ev,
        "confidence": summary_row.confidence,
    }


# ======================================================================
# 7. POST /api/v1/sessions/{session_id}/chat — 发送追问
# ======================================================================
@router.post("/sessions/{session_id}/chat", status_code=201)
def send_chat_message(
    session_id: str,
    data: ChatSend,
    db: Session = Depends(get_db),
):
    sess_svc = SessionService(db)
    if sess_svc.get_session(session_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "SESSION_NOT_FOUND", "message": "session not found"}},
        )

    chat_svc = ChatService(db)
    msg = chat_svc.send_message(
        session_id=session_id,
        content=data.message,
        role="user",
        related_claim_id=data.related_claim_id,
        message_type=data.mode,
        metadata={"mode": data.mode} if data.mode else None,
    )

    return ChatSendResponse(
        message_id=msg.id,
        session_id=session_id,
        status="accepted",
    )


# ======================================================================
# 8. GET /api/v1/sessions/{session_id}/chat — 对话历史
# ======================================================================
@router.get("/sessions/{session_id}/chat")
def list_chat_messages(
    session_id: str,
    limit: int = Query(50, ge=1, le=200),
    before_id: str | None = Query(None),
    db: Session = Depends(get_db),
):
    sess_svc = SessionService(db)
    if sess_svc.get_session(session_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "SESSION_NOT_FOUND", "message": "session not found"}},
        )

    chat_svc = ChatService(db)
    return chat_svc.list_messages(session_id, limit=limit, before_id=before_id)


# ======================================================================
# 9. POST /api/v1/sessions/{session_id}/recheck — 重新验证
# ======================================================================
@router.post("/sessions/{session_id}/recheck", status_code=202)
def recheck_session(
    session_id: str,
    body: RecheckRequest,
    db: Session = Depends(get_db),
):
    """提交重新验证请求。当前返回 queued 状态，
    实际的 Agent 重新推理流水线后续对接。"""
    sess_svc = SessionService(db)
    if sess_svc.get_session(session_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "SESSION_NOT_FOUND", "message": "session not found"}},
        )

    return {
        "session_id": session_id,
        "claim_id": body.claim_id,
        "status": "queued",
    }
