# backend/schemas/common.py
"""共享 Pydantic schemas."""

from pydantic import BaseModel


class ErrorResponse(BaseModel):
    error: dict  # {"code": "...", "message": "..."}
