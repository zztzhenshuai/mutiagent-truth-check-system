# backend/schemas/chat.py
"""Chat-related request/response schemas."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class ChatSend(BaseModel):
    message: str
    related_claim_id: Optional[str] = None
    mode: Optional[str] = None  # explain / recheck / evidence / summary


class ChatSendResponse(BaseModel):
    message_id: str
    session_id: str
    status: str  # "accepted"


class ChatMessageResponse(BaseModel):
    id: str
    role: str
    content: str
    related_claim_id: Optional[str] = None
    message_type: Optional[str] = None
    created_at: str


class ChatListResponse(BaseModel):
    items: list[ChatMessageResponse]
