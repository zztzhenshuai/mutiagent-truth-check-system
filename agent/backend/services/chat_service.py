# backend/services/chat_service.py
"""Chat message CRUD — 对应 /api/v1/sessions/{id}/chat."""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from ..db.models import ChatMessage
from ..schemas.chat import ChatListResponse, ChatMessageResponse


class ChatService:

    def __init__(self, db: Session) -> None:
        self.db = db

    def send_message(
        self,
        session_id: str,
        content: str,
        role: str = "user",
        related_claim_id: str | None = None,
        message_type: str | None = None,
        metadata: dict | None = None,
    ) -> ChatMessage:
        msg = ChatMessage(
            session_id=session_id,
            role=role,
            content=content,
            related_claim_id=related_claim_id,
            message_type=message_type,
            extra_metadata=json.dumps(metadata) if metadata else None,
        )
        self.db.add(msg)
        self.db.commit()
        self.db.refresh(msg)
        return msg

    def list_messages(
        self,
        session_id: str,
        limit: int = 50,
        before_id: str | None = None,
    ) -> ChatListResponse:
        q = self.db.query(ChatMessage).filter(
            ChatMessage.session_id == session_id
        )

        if before_id:
            # cursor-based pagination: get messages older than before_id
            anchor = (
                self.db.query(ChatMessage)
                .filter(ChatMessage.id == before_id)
                .first()
            )
            if anchor:
                q = q.filter(ChatMessage.created_at < anchor.created_at)

        rows = (
            q.order_by(ChatMessage.created_at.asc())
            .limit(limit)
            .all()
        )

        items = [
            ChatMessageResponse(
                id=r.id,
                role=r.role,
                content=r.content,
                related_claim_id=r.related_claim_id,
                message_type=r.message_type,
                created_at=r.created_at,
            )
            for r in rows
        ]
        return ChatListResponse(items=items)
