# backend/db/session.py
"""SQLAlchemy engine and session factory for SQLite."""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "agent.db")
DB_URL = f"sqlite:///{os.path.abspath(DB_PATH)}"

engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False},
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
