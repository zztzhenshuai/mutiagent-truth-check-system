# backend/db/init_db.py
"""Initialize database — create all tables if they don't exist, and apply schema migrations."""

import logging

from sqlalchemy import inspect, text

from .models import Base
from .session import engine

logger = logging.getLogger("agent.backend.db")


def _migrate() -> None:
    """增量 schema 迁移：为已存在的表补加缺失的列。"""
    inspector = inspect(engine)

    # v1 → v2：为 analysis_session 添加 article_summary 列
    if inspector.has_table("analysis_session"):
        existing_cols = {c["name"] for c in inspector.get_columns("analysis_session")}
        if "article_summary" not in existing_cols:
            logger.info("迁移：为 analysis_session 添加 article_summary 列")
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE analysis_session ADD COLUMN article_summary TEXT"
                ))
                conn.commit()


def init_db() -> None:
    """Create all tables. Safe to call multiple times (CREATE IF NOT EXISTS)."""
    logger.info("Initializing database tables...")
    Base.metadata.create_all(bind=engine)
    _migrate()
    logger.info("Database tables ready.")
