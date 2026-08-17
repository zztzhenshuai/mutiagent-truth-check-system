"""
backend/services/rag_service.py

RAG 服务：文档分块、嵌入、检索、录入、查询。

职责：
  - chunk_text(): 固定大小 + 重叠滑动窗口分块
  - RAGService.ingest_document(): 分块 → 嵌入 → 入库
  - RAGService.query(): 对用户 query 编码 → 余弦相似度检索 → 返回 top-k 片段
  - RAGService.list_documents(): 列出会话下所有文档
  - RAGService.delete_document(): 删除文档及所有 chunks

依赖：
  - agent.tools.embedding_utils（共享嵌入模型）
  - backend.db.models（RagDocument, RagChunk）
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from agent.tools.embedding_utils import encode_query, encode_texts

from ..db.models import RagChunk, RagDocument

logger = logging.getLogger("agent.backend.rag")

# ── 默认分块参数 ──
DEFAULT_CHUNK_SIZE = 500       # 每个 chunk 的字符数
DEFAULT_CHUNK_OVERLAP = 100    # 相邻 chunk 重叠字符数
DEFAULT_TOP_K = 5              # 检索返回的最大片段数
MIN_CHUNK_SIZE = 50            # 最小分块大小（少于此不分块）


# ── 中文/英文分句正则 ──
_SENTENCE_SPLIT = re.compile(r"([。！？!?\n]+)")


def chunk_text(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    """将文本按固定大小 + 重叠滑动窗口分块。

    优先在句号/换行等自然边界处切割，避免截断词语。

    Args:
        text: 原始文本
        chunk_size: 每个 chunk 的目标字符数
        overlap: 相邻 chunk 的重叠字符数

    Returns:
        分块文本列表
    """
    text = text.strip()
    if not text:
        return []

    if len(text) <= chunk_size:
        return [text]

    # 先按自然边界分句
    parts = _SENTENCE_SPLIT.split(text)
    sentences: list[str] = []
    buf = ""
    for part in parts:
        buf += part
        if _SENTENCE_SPLIT.match(part):
            sentences.append(buf)
            buf = ""
    if buf.strip():
        sentences.append(buf)

    # 按 chunk_size 合并句子
    chunks: list[str] = []
    current = ""
    for sent in sentences:
        if len(current) + len(sent) <= chunk_size:
            current += sent
        else:
            if len(current.strip()) >= MIN_CHUNK_SIZE:
                chunks.append(current.strip())
            # 带重叠：保留上一 chunk 尾部作为下一 chunk 的上下文
            if overlap > 0 and len(current) > overlap:
                current = current[-overlap:] + sent
            else:
                current = sent

    if len(current.strip()) >= MIN_CHUNK_SIZE:
        chunks.append(current.strip())
    elif chunks and current.strip():
        # 剩余文本过短，合并到最后一个 chunk
        chunks[-1] = chunks[-1] + current.strip()

    return chunks


class RAGService:
    """RAG 服务：封装文档录入和检索的全流程。"""

    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # 文档录入
    # ------------------------------------------------------------------

    async def ingest_document(
        self,
        session_id: str,
        filename: str,
        content: str,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    ) -> dict:
        """录入一篇文档：分块 → 嵌入 → 写入 rag_document + rag_chunk。

        Args:
            session_id: 所属会话 ID
            filename: 文档文件名（仅供参考）
            content: 文档全文
            chunk_size: 分块大小
            chunk_overlap: 分块重叠

        Returns:
            {"document_id": str, "chunk_count": int, "filename": str}
        """
        content = content.strip()
        if not content:
            raise ValueError("文档内容不能为空")

        # 1. 分块
        chunks = chunk_text(content, chunk_size=chunk_size, overlap=chunk_overlap)
        if not chunks:
            raise ValueError("分块结果为空")

        # 2. 批量嵌入
        embeddings = await encode_texts(chunks, batch_size=32, normalize=True)

        # 3. 写入 rag_document
        doc = RagDocument(
            session_id=session_id,
            filename=filename,
            content=content,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            chunk_count=len(chunks),
        )
        self.db.add(doc)
        self.db.flush()  # 获取 doc.id

        # 4. 逐块写入 rag_chunk
        for i, (chunk_text_str, emb) in enumerate(zip(chunks, embeddings)):
            chunk = RagChunk(
                document_id=doc.id,
                session_id=session_id,
                chunk_index=i,
                content=chunk_text_str,
                embedding=json.dumps(emb.tolist(), ensure_ascii=False),
            )
            self.db.add(chunk)

        self.db.commit()
        self.db.refresh(doc)

        logger.info(
            "RAG 文档录入完成：doc=%s file=%s chunks=%d",
            doc.id, filename, len(chunks),
        )
        return {
            "document_id": doc.id,
            "chunk_count": len(chunks),
            "filename": filename,
        }

    # ------------------------------------------------------------------
    # 列表
    # ------------------------------------------------------------------

    def list_documents(self, session_id: str) -> list[dict]:
        """列出会话下所有 RAG 文档。"""
        docs = (
            self.db.query(RagDocument)
            .filter(RagDocument.session_id == session_id)
            .order_by(RagDocument.created_at.desc())
            .all()
        )
        return [
            {
                "document_id": d.id,
                "filename": d.filename,
                "chunk_count": d.chunk_count,
                "chunk_size": d.chunk_size,
                "created_at": d.created_at,
            }
            for d in docs
        ]

    # ------------------------------------------------------------------
    # 删除
    # ------------------------------------------------------------------

    def delete_document(self, session_id: str, document_id: str) -> bool:
        """删除文档及其所有 chunks。返回是否删除成功。"""
        doc = (
            self.db.query(RagDocument)
            .filter(
                RagDocument.id == document_id,
                RagDocument.session_id == session_id,
            )
            .first()
        )
        if doc is None:
            return False

        # 级联删除 chunks（ORM relationship 已配置 cascade）
        self.db.delete(doc)
        self.db.commit()
        logger.info("RAG 文档已删除：doc=%s", document_id)
        return True

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    async def query(
        self,
        session_id: str,
        user_query: str,
        top_k: int = DEFAULT_TOP_K,
        similarity_threshold: float = 0.3,
    ) -> list[dict]:
        """检索与用户问题最相关的文档片段。

        Args:
            session_id: 会话 ID
            user_query: 用户问题文本
            top_k: 返回的最大片段数
            similarity_threshold: 最低余弦相似度阈值

        Returns:
            [{"content": str, "similarity": float, "document_id": str, "chunk_index": int}, ...]
        """
        # 1. 加载该会话所有 chunks
        chunks = (
            self.db.query(RagChunk)
            .filter(RagChunk.session_id == session_id)
            .all()
        )
        if not chunks:
            return []

        # 2. 提取嵌入矩阵 + 文本列表
        chunk_texts: list[str] = []
        embeddings_list: list[np.ndarray] = []
        metadata: list[dict] = []

        for ch in chunks:
            if not ch.embedding:
                continue
            try:
                emb = np.asarray(json.loads(ch.embedding), dtype=np.float32)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            chunk_texts.append(ch.content)
            embeddings_list.append(emb)
            metadata.append({
                "document_id": ch.document_id,
                "chunk_index": ch.chunk_index,
            })

        if not embeddings_list:
            return []

        # 3. 编码查询
        query_emb = await encode_query(user_query)

        # 4. 计算余弦相似度（嵌入已归一化，点积即余弦）
        emb_matrix = np.stack(embeddings_list, axis=0)  # (N, dim)
        scores = emb_matrix @ query_emb  # (N,)

        # 5. 排序取 top-k
        if len(scores) == 0:
            return []

        sorted_indices = np.argsort(scores)[::-1]

        results: list[dict] = []
        for idx in sorted_indices:
            similarity = float(scores[idx])
            if similarity < similarity_threshold:
                break
            results.append({
                "content": chunk_texts[idx],
                "similarity": round(similarity, 4),
                "document_id": metadata[idx]["document_id"],
                "chunk_index": metadata[idx]["chunk_index"],
            })
            if len(results) >= top_k:
                break

        logger.debug(
            "RAG query 完成：session=%s top_k=%d found=%d",
            session_id, top_k, len(results),
        )
        return results
