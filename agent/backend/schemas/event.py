# backend/schemas/event.py
"""Event-related response schemas."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class EventItem(BaseModel):
    seq: int
    event_type: str
    claim_id: Optional[str] = None
    payload: dict
    created_at: str


class EventListResponse(BaseModel):
    items: list[EventItem]
