# backend/db/models.py
"""SQLAlchemy ORM models — 对应 backend-b-interface.md §4 的 7 张表。

额外字段：
  - analysis_session.device_id   → 匿名设备标识
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, ForeignKey, Float, Integer, Text
from sqlalchemy.orm import DeclarativeBase, relationship


def _uuid(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# 4.1 analysis_session
# ---------------------------------------------------------------------------
class AnalysisSession(Base):
    __tablename__ = "analysis_session"

    id = Column(Text, primary_key=True, default=lambda: _uuid("sess_"))
    device_id = Column(Text, nullable=True, index=True)
    article_title = Column(Text, nullable=True)
    source_url = Column(Text, nullable=True)
    article_text = Column(Text, nullable=True)
    article_hash = Column(Text, nullable=True)
    domain = Column(Text, nullable=True)
    skill_id = Column(Text, nullable=True)
    status = Column(Text, nullable=False, default="pending")
    total_claims = Column(Integer, nullable=True)
    total_annotations = Column(Integer, nullable=True)
    created_at = Column(Text, nullable=False, default=_utcnow)
    started_at = Column(Text, nullable=True)
    finished_at = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    article_summary = Column(Text, nullable=True)  # 文章摘要：分析完成后由 LLM 生成，用于追问上下文压缩

    # relationships
    claims = relationship("ClaimRecord", back_populates="session", cascade="all, delete-orphan")
    events = relationship("EventRecord", back_populates="session", cascade="all, delete-orphan")
    tool_calls = relationship("ToolCallRecord", back_populates="session", cascade="all, delete-orphan")
    summary = relationship("SummaryRecord", back_populates="session", uselist=False, cascade="all, delete-orphan")
    chat_messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan")
    rag_documents = relationship("RagDocument", back_populates="session", cascade="all, delete-orphan")


# ---------------------------------------------------------------------------
# 4.2 claim_record
# ---------------------------------------------------------------------------
class ClaimRecord(Base):
    __tablename__ = "claim_record"

    id = Column(Text, primary_key=True, default=lambda: _uuid("claim_"))
    session_id = Column(Text, ForeignKey("analysis_session.id"), nullable=False, index=True)
    claim_id = Column(Text, nullable=False)  # 业务 ID e.g. "c001"
    text = Column(Text, nullable=False)
    start_offset = Column(Integer, nullable=True)
    end_offset = Column(Integer, nullable=True)
    suspicion_score = Column(Float, nullable=True)
    verdict = Column(Text, nullable=True, default="pending")
    error_type = Column(Text, nullable=True)
    confidence = Column(Float, nullable=True)
    reasoning = Column(Text, nullable=True)
    evidence_urls = Column(Text, nullable=True)  # JSON array string
    created_at = Column(Text, nullable=False, default=_utcnow)
    updated_at = Column(Text, nullable=True)

    session = relationship("AnalysisSession", back_populates="claims")
    tool_calls = relationship("ToolCallRecord", back_populates="claim", cascade="all, delete-orphan")


# ---------------------------------------------------------------------------
# 4.3 event_record
# ---------------------------------------------------------------------------
class EventRecord(Base):
    __tablename__ = "event_record"

    id = Column(Text, primary_key=True, default=lambda: _uuid("evt_"))
    session_id = Column(Text, ForeignKey("analysis_session.id"), nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    event_type = Column(Text, nullable=False)
    claim_id = Column(Text, nullable=True)
    payload = Column(Text, nullable=False)  # 事件完整 JSON
    created_at = Column(Text, nullable=False, default=_utcnow)

    session = relationship("AnalysisSession", back_populates="events")


# ---------------------------------------------------------------------------
# 4.4 tool_call_record
# ---------------------------------------------------------------------------
class ToolCallRecord(Base):
    __tablename__ = "tool_call_record"

    id = Column(Text, primary_key=True, default=lambda: _uuid("tool_"))
    session_id = Column(Text, ForeignKey("analysis_session.id"), nullable=False, index=True)
    claim_id = Column(Text, nullable=True)
    tool_name = Column(Text, nullable=False)
    tool_input = Column(Text, nullable=True)
    tool_output = Column(Text, nullable=True)
    status = Column(Text, nullable=True, default="success")
    latency_ms = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False, default=_utcnow)

    session = relationship("AnalysisSession", back_populates="tool_calls")
    # Also link to ClaimRecord via claim_id + session_id
    claim_record_id = Column(Text, ForeignKey("claim_record.id"), nullable=True)
    claim = relationship("ClaimRecord", back_populates="tool_calls")


# ---------------------------------------------------------------------------
# 4.5 summary_record
# ---------------------------------------------------------------------------
class SummaryRecord(Base):
    __tablename__ = "summary_record"

    id = Column(Text, primary_key=True, default=lambda: _uuid("sum_"))
    session_id = Column(Text, ForeignKey("analysis_session.id"), nullable=False, unique=True)
    overall_conclusion = Column(Text, nullable=True)
    total_claims = Column(Integer, nullable=True)
    total_errors = Column(Integer, nullable=True)
    clean_claims = Column(Integer, nullable=True)
    challenged_claims = Column(Integer, nullable=True)
    revised_claims = Column(Integer, nullable=True)
    error_breakdown = Column(Text, nullable=True)  # JSON object string
    representative_evidence = Column(Text, nullable=True)  # JSON array string
    confidence = Column(Float, nullable=True)
    summary_payload = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False, default=_utcnow)

    session = relationship("AnalysisSession", back_populates="summary")


# ---------------------------------------------------------------------------
# 4.6 chat_message
# ---------------------------------------------------------------------------
class ChatMessage(Base):
    __tablename__ = "chat_message"

    id = Column(Text, primary_key=True, default=lambda: _uuid("msg_"))
    session_id = Column(Text, ForeignKey("analysis_session.id"), nullable=False, index=True)
    role = Column(Text, nullable=False)  # user / assistant / system
    content = Column(Text, nullable=False)
    related_claim_id = Column(Text, nullable=True)
    message_type = Column(Text, nullable=True)  # question / answer / recheck / note
    extra_metadata = Column(Text, nullable=True)  # JSON string (renamed from 'metadata' — reserved by SQLAlchemy)
    created_at = Column(Text, nullable=False, default=_utcnow)

    session = relationship("AnalysisSession", back_populates="chat_messages")


# ---------------------------------------------------------------------------
# 4.7 rag_document — RAG 上传文档
# ---------------------------------------------------------------------------
class RagDocument(Base):
    __tablename__ = "rag_document"

    id = Column(Text, primary_key=True, default=lambda: _uuid("rdoc_"))
    session_id = Column(Text, ForeignKey("analysis_session.id"), nullable=False, index=True)
    filename = Column(Text, nullable=False)
    content = Column(Text, nullable=False)        # 原始全文
    chunk_size = Column(Integer, nullable=False, default=500)
    chunk_overlap = Column(Integer, nullable=False, default=100)
    chunk_count = Column(Integer, nullable=False, default=0)
    created_at = Column(Text, nullable=False, default=_utcnow)

    session = relationship("AnalysisSession", back_populates="rag_documents")
    chunks = relationship("RagChunk", back_populates="document", cascade="all, delete-orphan")


# ---------------------------------------------------------------------------
# 4.8 rag_chunk — RAG 文档分块 + 嵌入
# ---------------------------------------------------------------------------
class RagChunk(Base):
    __tablename__ = "rag_chunk"

    id = Column(Text, primary_key=True, default=lambda: _uuid("rchk_"))
    document_id = Column(Text, ForeignKey("rag_document.id"), nullable=False, index=True)
    session_id = Column(Text, ForeignKey("analysis_session.id"), nullable=False, index=True)
    chunk_index = Column(Integer, nullable=False)  # 从 0 开始
    content = Column(Text, nullable=False)          # 分块文本
    embedding = Column(Text, nullable=True)         # JSON 数组字符串（嵌入向量）
    created_at = Column(Text, nullable=False, default=_utcnow)

    document = relationship("RagDocument", back_populates="chunks")


# ---------------------------------------------------------------------------
# 4.9 skill_record
# ---------------------------------------------------------------------------
class SkillRecord(Base):
    __tablename__ = "skill_record"

    id = Column(Text, primary_key=True, default=lambda: _uuid("skill_"))
    skill_name = Column(Text, nullable=False)
    domain = Column(Text, nullable=True)
    system_prompt = Column(Text, nullable=True)
    claim_filter_rule = Column(Text, nullable=True)
    allowed_tools = Column(Text, nullable=True)  # JSON array string
    output_contract = Column(Text, nullable=True)
    version = Column(Text, nullable=True)
    is_active = Column(Integer, nullable=True, default=1)
    created_at = Column(Text, nullable=False, default=_utcnow)
    updated_at = Column(Text, nullable=True)
