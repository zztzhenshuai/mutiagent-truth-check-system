# backend/schemas/claim.py
"""Claim-related response schemas."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class ToolCallItem(BaseModel):
    tool_name: str
    tool_input: Optional[str] = None
    tool_output: Optional[str] = None
    status: Optional[str] = None
    latency_ms: Optional[int] = None
    created_at: str


class ClaimDetailResponse(BaseModel):
    claim: dict
    tool_calls: list[ToolCallItem] = []
    debate: list[dict] = []
    annotation: Optional[dict] = None
