# backend/dependencies.py
"""FastAPI 依赖注入 — 提取请求级别的上下文（设备 ID、DB session）。"""

from fastapi import Request

from .db.session import SessionLocal


def get_device_id(request: Request) -> str:
    """从 X-Device-ID Header 提取匿名设备标识，缺失时返回 "unknown"."""
    return request.headers.get("X-Device-ID", "unknown")


def get_db():
    """FastAPI 依赖：每个请求创建一个 DB session，结束后自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
