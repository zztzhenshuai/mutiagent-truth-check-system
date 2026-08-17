# backend/services/event_service.py
"""Event query service — 对应 /api/v1/sessions/{id}/events."""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from ..db.models import EventRecord
from ..schemas.event import EventItem, EventListResponse


class EventService:

    def __init__(self, db: Session) -> None:
        self.db = db

    def list_events(
        self,
        session_id: str,
        limit: int = 100,
        after_seq: int | None = None,
        event_type: str | None = None,
    ) -> EventListResponse:
        q = self.db.query(EventRecord).filter(
            EventRecord.session_id == session_id
        )

        if after_seq is not None:
            q = q.filter(EventRecord.seq > after_seq)

        if event_type:
            q = q.filter(EventRecord.event_type == event_type)

        rows = (
            q.order_by(EventRecord.seq.asc())
            .limit(limit)
            .all()
        )

        items = [
            EventItem(
                seq=r.seq,
                event_type=r.event_type,
                claim_id=r.claim_id,
                payload=json.loads(r.payload) if r.payload else {},
                created_at=r.created_at,
            )
            for r in rows
        ]
        return EventListResponse(items=items)
