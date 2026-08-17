"""
backend/routers/rag.py

RAG 文档管理 API：
  POST   /api/v1/sessions/{session_id}/rag/documents          上传文档
  GET    /api/v1/sessions/{session_id}/rag/documents          文档列表
  DELETE /api/v1/sessions/{session_id}/rag/documents/{doc_id}  删除文档
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..dependencies import get_db
from ..services.rag_service import RAGService
from ..services.session_service import SessionService

logger = logging.getLogger("agent.backend.rag_router")

router = APIRouter(tags=["rag"])


# ── 请求/响应模型 ──

class RagDocumentUpload(BaseModel):
    filename: str
    content: str
    chunk_size: int = 500
    chunk_overlap: int = 100


class RagDocumentResponse(BaseModel):
    document_id: str
    chunk_count: int
    filename: str


class RagDocumentItem(BaseModel):
    document_id: str
    filename: str
    chunk_count: int
    chunk_size: int
    created_at: str


class RagDocumentListResponse(BaseModel):
    items: list[RagDocumentItem]


# ======================================================================
# POST /api/v1/sessions/{session_id}/rag/documents — 上传文档
# ======================================================================
@router.post(
    "/sessions/{session_id}/rag/documents",
    response_model=RagDocumentResponse,
    status_code=201,
)
async def upload_document(
    session_id: str,
    data: RagDocumentUpload,
    db: Session = Depends(get_db),
):
    """上传一篇 TXT 文档到指定会话。

    文档将被分块、嵌入向量化并持久化到数据库。
    后续该会话的追问会自动检索相关片段注入回答上下文。
    """
    # 验证会话存在
    sess_svc = SessionService(db)
    if sess_svc.get_session(session_id) is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "SESSION_NOT_FOUND",
                    "message": "session not found",
                }
            },
        )

    if not data.content.strip():
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "EMPTY_CONTENT",
                    "message": "document content is empty",
                }
            },
        )

    try:
        rag_svc = RAGService(db)
        result = await rag_svc.ingest_document(
            session_id=session_id,
            filename=data.filename,
            content=data.content,
            chunk_size=data.chunk_size,
            chunk_overlap=data.chunk_overlap,
        )
        return RagDocumentResponse(**result)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "INVALID_DOCUMENT", "message": str(e)}},
        )
    except Exception as e:
        logger.exception("文档上传失败")
        raise HTTPException(
            status_code=500,
            detail={"error": {"code": "UPLOAD_FAILED", "message": str(e)}},
        )


# ======================================================================
# GET /api/v1/sessions/{session_id}/rag/documents — 文档列表
# ======================================================================
@router.get(
    "/sessions/{session_id}/rag/documents",
    response_model=RagDocumentListResponse,
)
def list_documents(
    session_id: str,
    db: Session = Depends(get_db),
):
    """列出指定会话下所有已上传的 RAG 文档。"""
    sess_svc = SessionService(db)
    if sess_svc.get_session(session_id) is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "SESSION_NOT_FOUND",
                    "message": "session not found",
                }
            },
        )

    rag_svc = RAGService(db)
    items = rag_svc.list_documents(session_id)
    return RagDocumentListResponse(
        items=[RagDocumentItem(**item) for item in items]
    )


# ======================================================================
# DELETE /api/v1/sessions/{session_id}/rag/documents/{doc_id} — 删除文档
# ======================================================================
@router.delete("/sessions/{session_id}/rag/documents/{doc_id}", status_code=204)
def delete_document(
    session_id: str,
    doc_id: str,
    db: Session = Depends(get_db),
):
    """删除指定文档及其所有 chunks。"""
    sess_svc = SessionService(db)
    if sess_svc.get_session(session_id) is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "SESSION_NOT_FOUND",
                    "message": "session not found",
                }
            },
        )

    rag_svc = RAGService(db)
    deleted = rag_svc.delete_document(session_id, doc_id)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "DOCUMENT_NOT_FOUND",
                    "message": "document not found in this session",
                }
            },
        )
