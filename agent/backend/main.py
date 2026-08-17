import asyncio
import json
import logging
import os
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .db.init_db import init_db
from .db.models import (
    AnalysisSession, ClaimRecord, EventRecord,
    ToolCallRecord, SummaryRecord,
)
from .db.session import SessionLocal

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("agent.backend")

from .services.chat_service import ChatService
from .services.session_service import SessionService
from .services.context_builder import build_chat_context

app = FastAPI(title="Agent Analysis Service")

# CORS setup for Chrome Extension
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from .routers.sessions import router as sessions_router
from .routers.rag import router as rag_router
app.include_router(sessions_router, prefix="/api/v1")
app.include_router(rag_router, prefix="/api/v1")

# Initialize Agent singelton
agent = None
db_ready = False

@app.on_event("startup")
async def startup_event():
    global agent, db_ready
    try:
        from agent import Agent, create_chat_llm, create_core_llm, create_router_llm

        # 主流程 LLM：默认 GLM；GLM 不可用时再回退 Claude/DeepSeek。
        # 这一路径覆盖 claim 提取、Verifier、Challenger、Judge。
        complex_llm = create_core_llm()
        router_llm = create_router_llm(fallback=complex_llm)

        # 初始化聊天专用 LLM（默认复用 complex_llm，可通过 .env 独立配置）
        try:
            chat_llm = create_chat_llm()
            logger.info("Chat LLM initialized: %s", os.environ.get("CHAT_LLM_PROVIDER", "glm"))
        except Exception as e:
            logger.warning("Failed to create chat LLM, will reuse complex_llm: %s", e)
            chat_llm = None

        agent = Agent(complex_llm=complex_llm, router_llm=router_llm, chat_llm=chat_llm)
        logger.info("Agent successfully initialized.")
    except Exception as e:
        logger.exception("Failed to initialize Agent: %s", e)

    try:
        init_db()
        db_ready = True
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.exception("Failed to initialize database: %s", e)

    # 预热 cross_reference 的句向量模型（首次加载 ~13-16s）。
    # 放到后台任务里跑：不阻塞启动与健康检查；失败（模型未缓存/网络等）只记日志，
    # 工具被调用时仍会惰性兜底加载。这样除非启动后立刻有人用到 cross_reference，
    # 否则首个分析请求不必再承担这笔模型加载开销。
    async def _warmup_cross_reference():
        try:
            from agent.tools.cross_reference import warmup_cross_reference_model
            msg = await warmup_cross_reference_model()
            logger.info("cross_reference warmup done: %s", msg)
        except Exception as e:
            logger.warning("cross_reference warmup failed (will lazy-load on first use): %s", e)

    asyncio.create_task(_warmup_cross_reference())

@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "agent_initialized": agent is not None,
        "db_initialized": db_ready,
    }

# ---------------------------------------------------------------------------
# Event persistence helper
# ---------------------------------------------------------------------------
from datetime import datetime, timezone

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _persist_agent_event(db, event: dict, session_id: str, seq: int) -> int:
    """Persist one SSE event to DB. Returns next seq."""
    event_type = event.get("type", "unknown")
    claim_id = event.get("claim_id")
    payload_json = json.dumps(event, ensure_ascii=False)

    # always write event_record
    evt = EventRecord(
        session_id=session_id, seq=seq,
        event_type=event_type, claim_id=claim_id, payload=payload_json,
    )
    db.add(evt)

    if event_type == "plan":
        sess = db.query(AnalysisSession).filter(AnalysisSession.id == session_id).first()
        if sess:
            sess.total_claims = event.get("total", 0)
            sess.status = "running"
            sess.started_at = _utcnow()
        for c in event.get("claims", []):
            db.add(ClaimRecord(
                session_id=session_id,
                claim_id=c.get("id", ""), text=c.get("text", ""),
                suspicion_score=c.get("suspicion_score", 0),
            ))

    elif event_type == "annotation":
        claim = db.query(ClaimRecord).filter(
            ClaimRecord.session_id == session_id,
            ClaimRecord.claim_id == claim_id,
        ).first()
        if claim:
            claim.error_type = event.get("error_type")
            claim.confidence = event.get("confidence")
            claim.reasoning = event.get("reasoning")
            claim.evidence_urls = json.dumps(event.get("evidence_urls", []), ensure_ascii=False)
            claim.verdict = "rejected" if event.get("error_type") else "verified"
            claim.start_offset = event.get("start_offset")
            claim.end_offset = event.get("end_offset")
            claim.updated_at = _utcnow()

    elif event_type == "tool_call":
        db.add(ToolCallRecord(
            session_id=session_id, claim_id=claim_id,
            tool_name=event.get("tool_name", ""),
            tool_input=event.get("tool_input", ""),
            tool_output=event.get("tool_output", ""),
            status="success",
        ))

    elif event_type == "debate":
        # debate 事件已通过 EventRecord 通用写入存储完整 JSON payload
        # 额外更新对应 ClaimRecord 的最新辩论状态
        if claim_id:
            claim = db.query(ClaimRecord).filter(
                ClaimRecord.session_id == session_id,
                ClaimRecord.claim_id == claim_id,
            ).first()
            if claim:
                phase = event.get("phase", "")
                if phase == "result":
                    claim.verdict = "rejected" if event.get("stance") == "challenge" else "verified"
                    claim.confidence = event.get("confidence")

    elif event_type == "summary":
        # SummaryEvent 包含辩论统计数据，优先创建 SummaryRecord
        existing = db.query(SummaryRecord).filter(
            SummaryRecord.session_id == session_id
        ).first()
        if existing:
            # 更新已有记录（极少数场景）
            existing.overall_conclusion = event.get("overall_conclusion")
            existing.total_claims = event.get("total_claims")
            existing.total_errors = event.get("total_annotations")
            existing.clean_claims = event.get("clean_claims")
            existing.challenged_claims = event.get("challenged_claims")
            existing.revised_claims = event.get("revised_claims")
            existing.error_breakdown = json.dumps(event.get("error_breakdown", {}), ensure_ascii=False)
            existing.representative_evidence = json.dumps(event.get("representative_claims", []), ensure_ascii=False)
            existing.summary_payload = payload_json
        else:
            db.add(SummaryRecord(
                session_id=session_id,
                overall_conclusion=event.get("overall_conclusion"),
                total_claims=event.get("total_claims"),
                total_errors=event.get("total_annotations"),
                clean_claims=event.get("clean_claims"),
                challenged_claims=event.get("challenged_claims"),
                revised_claims=event.get("revised_claims"),
                error_breakdown=json.dumps(event.get("error_breakdown", {}), ensure_ascii=False),
                representative_evidence=json.dumps(event.get("representative_claims", []), ensure_ascii=False),
                summary_payload=payload_json,
            ))

    elif event_type == "done":
        sess = db.query(AnalysisSession).filter(AnalysisSession.id == session_id).first()
        total_annotations = event.get("total_annotations", 0)
        if sess:
            sess.status = "completed"
            sess.total_annotations = total_annotations
            sess.finished_at = _utcnow()

        # 如果 summary 事件已写入 SummaryRecord，这里只补充 claim_results
        existing_summary = db.query(SummaryRecord).filter(
            SummaryRecord.session_id == session_id
        ).first()
        claim_results = event.get("claim_results", [])
        if existing_summary:
            # 将 claim_results 合并进 summary_payload
            try:
                existing_payload = json.loads(existing_summary.summary_payload or "{}")
            except Exception:
                existing_payload = {}
            existing_payload["claim_results"] = claim_results
            existing_summary.summary_payload = json.dumps(existing_payload, ensure_ascii=False)
        else:
            # 兜底：summary 事件未触发（异常流程），自行构建 SummaryRecord
            claims = db.query(ClaimRecord).filter(ClaimRecord.session_id == session_id).all()
            total_errors = sum(1 for c in claims if c.error_type)
            breakdown = {}
            for c in claims:
                if c.error_type:
                    breakdown[c.error_type] = breakdown.get(c.error_type, 0) + 1

            rep_evidence = []
            for c in claims:
                if c.error_type and c.evidence_urls:
                    try:
                        urls = json.loads(c.evidence_urls)
                    except Exception:
                        urls = []
                    if urls:
                        rep_evidence.append({"claim_id": c.claim_id, "text": c.text[:120], "evidence_urls": urls})

            db.add(SummaryRecord(
                session_id=session_id,
                overall_conclusion=f"分析完成，共 {len(claims)} 条声明，发现 {total_errors} 条错误",
                total_claims=len(claims), total_errors=total_errors,
                clean_claims=len(claims) - total_errors,
                error_breakdown=json.dumps(breakdown, ensure_ascii=False),
                representative_evidence=json.dumps(rep_evidence, ensure_ascii=False),
                summary_payload=json.dumps({"claim_results": claim_results}, ensure_ascii=False),
            ))

    elif event_type == "status":
        # 提取 skill 路由信息，回填 AnalysisSession
        stage = event.get("stage", "")
        if stage == "route":
            sess = db.query(AnalysisSession).filter(AnalysisSession.id == session_id).first()
            if sess:
                details = event.get("details", {})
                if isinstance(details, dict):
                    skill_name = details.get("skill")
                    if skill_name and not sess.skill_id:
                        sess.skill_id = skill_name
                        sess.domain = skill_name  # domain 暂用 skill name 填充

    elif event_type == "error":
        if not claim_id:
            sess = db.query(AnalysisSession).filter(AnalysisSession.id == session_id).first()
            if sess:
                sess.error_message = event.get("message", "")
                sess.status = "failed"

    db.commit()
    return seq + 1


async def _summarize_article(text: str, agent) -> str | None:
    """使用 chat_llm 对文章正文生成结构化摘要（≤600 字），用于追问上下文压缩。

    失败时返回 None，调用方应回退到原文截断模式。
    """
    if not agent or not agent._chat_llm:
        logger.warning("_summarize_article: chat_llm 不可用，跳过")
        return None

    prompt = (
        "你是一个专业的信息提取助手。请对以下文章生成一份结构化摘要，"
        "用于后续的事实核查追问场景。\n\n"
        "要求：\n"
        "1. 总字数不超过 600 字\n"
        "2. 必须包含：文章主题、核心论点/声明、关键数据或事实\n"
        "3. 使用中文，简洁客观，不评价真伪\n"
        "4. 按以下格式输出：\n"
        "   【主题】一句话概括文章主旨\n"
        "   【核心声明】逐条列出文章中的关键声明（每条以\"- \"开头）\n"
        "   【关键数据】列出文中出现的数字、日期、统计数据等\n\n"
        f"文章内容：\n{text[:12000]}"
    )

    try:
        summary = await agent._chat_llm.complete([
            {"role": "user", "content": prompt}
        ])
        if summary and len(summary.strip()) > 20:
            return summary.strip()
        return None
    except Exception as e:
        logger.warning("_summarize_article LLM 调用失败：%s", e)
        return None


async def _run_agent_stream(
    text: str, request_id: str, session_id: str,
    article_title: str | None = None,
    source_url: str | None = None,
    device_id: str | None = None,
    overlays: list | None = None,
    disabled_tools: list | None = None,
):
    """SSE stream with DB persistence. overlays 为用户自定义附加视角配置列表；disabled_tools 为用户禁用的工具名列表。"""
    def serialize_event(payload: dict) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    db = SessionLocal()
    seq = 0

    try:
        sess = db.query(AnalysisSession).filter(AnalysisSession.id == session_id).first()
        if sess:
            sess.article_text = text
            sess.article_title = article_title
            sess.source_url = source_url
            sess.status = "running"
            if device_id:
                sess.device_id = device_id
            db.commit()

        backend_evt = {"type": "status", "stage": "backend", "session_id": session_id, "message": "后端已收到分析请求"}
        seq = _persist_agent_event(db, backend_evt, session_id, seq)
        yield serialize_event(backend_evt)


        if agent is None:
            err = {"type": "error", "claim_id": None, "message": "Agent initialization failed"}
            seq = _persist_agent_event(db, err, session_id, seq)
            yield serialize_event(err)
            done = {"type": "done", "total_annotations": 0}
            seq = _persist_agent_event(db, done, session_id, seq)
            yield serialize_event(done)
            return

        yield serialize_event({"type": "status", "stage": "backend", "message": "SSE 流已建立"})
        seq += 1  # simple status not persisted

        agent_iter = agent.run(text, overlays=overlays, disabled_tools=disabled_tools).__aiter__()
        pending = asyncio.ensure_future(agent_iter.__anext__())

        while True:
            timer = asyncio.ensure_future(asyncio.sleep(15))
            done, _ = await asyncio.wait(
                [pending, timer], return_when=asyncio.FIRST_COMPLETED
            )

            if pending in done:
                timer.cancel()
                try:
                    state = pending.result()
                except StopAsyncIteration:
                    break
                event_dict = state.model_dump(mode="json")
                seq = _persist_agent_event(db, event_dict, session_id, seq)
                yield serialize_event(event_dict)
                pending = asyncio.ensure_future(agent_iter.__anext__())
            else:
                # heartbeat: keep SSE alive, prevent MV3 SW 30s idle kill
                yield serialize_event(
                    {"type": "status", "stage": "heartbeat", "message": "keepalive"}
                )
    except Exception as e:
        logger.exception("[%s] Agent execution failed", request_id)
        err = {"type": "error", "claim_id": None, "message": f"Agent execution failed: {str(e)}"}
        try:
            seq = _persist_agent_event(db, err, session_id, seq)
        except Exception:
            pass
        yield serialize_event(err)

    else:
        # ── 文章摘要：分析完成后异步生成，存入 DB 供追问上下文复用 ──
        try:
            summary_text = await _summarize_article(text, agent)
            if summary_text:
                sess = db.query(AnalysisSession).filter(AnalysisSession.id == session_id).first()
                if sess:
                    sess.article_summary = summary_text
                    db.commit()
                yield serialize_event({
                    "type": "status",
                    "stage": "article_summary",
                    "message": "文章摘要已生成",
                    "summary_length": len(summary_text),
                })
                logger.info("[%s] article summary generated: %d chars", request_id, len(summary_text))
        except Exception as e:
            logger.warning("[%s] article summary generation failed (non-critical): %s", request_id, e)

    finally:
        db.close()
        logger.info("[%s] analyze stream finished", request_id)


# 单请求允许的 overlay 数量上限
_MAX_OVERLAYS_PER_REQUEST = 10


def _extract_overlays(body: dict) -> list:
    """从请求体提取并粗筛 overlay 列表（细校验交给 agent 侧 build_overlay_skill）。"""
    overlays = body.get("overlays") or []
    if not isinstance(overlays, list):
        return []
    return overlays[:_MAX_OVERLAYS_PER_REQUEST]


# 单请求允许禁用的工具数量上限（registry 当前 14 个，留余量）
_MAX_DISABLED_TOOLS_PER_REQUEST = 50


def _extract_disabled_tools(body: dict) -> list:
    """从请求体提取用户禁用的工具名列表（粗筛：去重、截断；存在性校验交给 agent 侧 _normalize_disabled）。"""
    disabled = body.get("disabled_tools") or []
    if not isinstance(disabled, list):
        return []
    seen: list[str] = []
    for t in disabled:
        if isinstance(t, str) and t.strip() and t.strip() not in seen:
            seen.append(t.strip())
    return seen[:_MAX_DISABLED_TOOLS_PER_REQUEST]


@app.post("/analyze")
@app.get("/analyze")
async def analyze(request: Request):
    text = request.query_params.get("text")
    body: dict = {}
    overlays: list = []
    disabled_tools: list = []
    if request.method == "POST":
        try:
            body = await request.json()
            text = body.get("article_text", text or "")
            overlays = _extract_overlays(body)
            disabled_tools = _extract_disabled_tools(body)
        except Exception:
            pass

    if not text:
        return {"error": "No article text provided. Pass via ?text=... or JSON body."}

    article_title = body.get("article_title")
    source_url = body.get("source_url")
    device_id = request.headers.get("X-Device-ID", "unknown")

    request_id = uuid4().hex[:8]

    # 后端全权创建 session，每次 /analyze 都是新会话
    db = SessionLocal()
    try:
        sess = AnalysisSession(
            device_id=device_id, article_title=article_title,
            source_url=source_url, article_text=text, status="pending",
        )
        db.add(sess); db.commit(); db.refresh(sess)
        session_id = sess.id
        logger.info("[%s] created session: %s device=%s overlays=%d", request_id, session_id, device_id[:16], len(overlays))
    finally:
        db.close()

    return StreamingResponse(
        _run_agent_stream(text=text, request_id=request_id, session_id=session_id,
                          article_title=article_title, source_url=source_url, device_id=device_id,
                          overlays=overlays, disabled_tools=disabled_tools),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# ---------------------------------------------------------------------------
# SSE 聊天流式接口：POST /api/v1/sessions/{session_id}/chat/stream
# ---------------------------------------------------------------------------
from uuid import uuid4 as _uuid4


@app.post("/api/v1/sessions/{session_id}/chat/stream")
async def chat_stream(
    session_id: str,
    body: dict,
    request: Request,
):
    """
    SSE 流式聊天接口。
    请求体：{"message": "用户追问", "related_claim_id": null, "mode": null}
    响应：text/event-stream，逐 token 推送 chat_chunk 事件，最后发送 chat_done。
    所有错误也通过 SSE error 事件返回。
    """

    async def _stream():
        def _sse(data: dict) -> str:
            return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

        user_message = body.get("message", "").strip()
        if not user_message:
            yield _sse({"type": "error", "message": "message is required"})
            return

        related_claim_id = body.get("related_claim_id")
        mode = body.get("mode")

        db = SessionLocal()
        try:
            # 验证会话存在
            sess_svc = SessionService(db)
            session = sess_svc.get_session(session_id)
            if session is None:
                yield _sse({
                    "type": "error",
                    "message": f"session {session_id} not found",
                    "code": "SESSION_NOT_FOUND",
                })
                return

            # 1. 持久化用户消息
            chat_svc = ChatService(db)
            user_msg = chat_svc.send_message(
                session_id=session_id,
                content=user_message,
                role="user",
                related_claim_id=related_claim_id,
                message_type=mode,
                metadata={"mode": mode} if mode else None,
            )

            # 2. 构建会话上下文
            session_context = build_chat_context(db, session_id)

            # 2.5 RAG 检索：查询会话下上传的文档，注入上下文
            try:
                from .services.rag_service import RAGService
                rag_svc = RAGService(db)
                rag_results = await rag_svc.query(
                    session_id=session_id,
                    user_query=user_message,
                    top_k=5,
                )
                if rag_results:
                    # 格式化检索结果为文本注入 session_context
                    rag_lines = []
                    for i, r in enumerate(rag_results, 1):
                        rag_lines.append(
                            f"[参考片段 {i} | 相似度 {r['similarity']}] {r['content']}"
                        )
                    session_context["rag_context"] = "\n\n".join(rag_lines)
                    logger.info(
                        "[chat_stream] RAG 命中 %d 个片段 session=%s",
                        len(rag_results), session_id,
                    )
                else:
                    session_context["rag_context"] = ""
            except Exception as e:
                logger.warning("[chat_stream] RAG 检索失败，降级无 RAG：%s", e)
                session_context["rag_context"] = ""

            # 3. 获取历史对话
            history_response = chat_svc.list_messages(session_id, limit=20)
            history = [
                {"role": h.role, "content": h.content}
                for h in history_response.items
                if h.id != user_msg.id  # 排除刚插入的用户消息
            ]

            # 4. 检查 Agent 可用
            if agent is None:
                yield _sse({"type": "error", "message": "Agent not initialized"})
                return

            # 5. 流式生成回复
            message_id = _uuid4().hex[:12]
            full_content_parts: list[str] = []

            async for token in agent.chat(
                session_context=session_context,
                user_message=user_message,
                history=history,
            ):
                full_content_parts.append(token)
                yield _sse({
                    "type": "chat_chunk",
                    "content": token,
                    "message_id": message_id,
                })

            # 流结束，持久化 assistant 消息
            full_content = "".join(full_content_parts)
            if full_content.strip():
                try:
                    chat_svc.send_message(
                        session_id=session_id,
                        content=full_content,
                        role="assistant",
                        related_claim_id=related_claim_id,
                        message_type="answer",
                    )
                except Exception as e:
                    logger.exception("[chat_stream] Failed to persist assistant message")

            yield _sse({
                "type": "chat_done",
                "message_id": message_id,
                "session_id": session_id,
                "full_content": full_content,
            })

        except Exception as e:
            logger.exception("[chat_stream] Unexpected error")
            yield _sse({"type": "error", "message": f"服务器错误: {str(e)}"})

        finally:
            db.close()

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.post("/v1/run")
async def run_evaluation(request: Request):
    try:
        body = await request.json()
        text = body.get("article_text", "")
    except Exception:
        return {"error": "Invalid JSON body"}

    if not text:
        return {"error": "article_text is required"}

    overlays = _extract_overlays(body)
    disabled_tools = _extract_disabled_tools(body)
    request_id = uuid4().hex[:8]
    device_id = request.headers.get("X-Device-ID", "unknown")

    logger.info(
        "[%s] evaluation request received: text_length=%d overlays=%d disabled_tools=%d",
        request_id,
        len(text),
        len(overlays),
        len(disabled_tools),
    )

    db = SessionLocal()
    try:
        sess = AnalysisSession(device_id=device_id, article_text=text, status="pending")
        db.add(sess); db.commit(); db.refresh(sess)
        session_id = sess.id
    finally:
        db.close()

    return StreamingResponse(
        _run_agent_stream(text=text, request_id=request_id, session_id=session_id, device_id=device_id, overlays=overlays, disabled_tools=disabled_tools),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.get("/skills")
async def list_skills():
    """供浏览器插件渲染：内置领域 skill 列表 + overlay 限制。"""
    from agent.skills.base import (
        KIND_DOMAIN,
        MAX_OVERLAY_NAME_LEN,
        MAX_OVERLAY_PROMPT_LEN,
        load_skills,
    )

    from agent.tools.registry import TOOL_REGISTRY

    skills = load_skills()
    domains = [
        {"name": s.name, "description": s.description, "kind": s.kind}
        for s in skills.values()
        if s.kind == KIND_DOMAIN
    ]
    # 工具目录：供前端渲染禁用开关；used_by 反查哪些领域会用到该工具，便于提示影响范围。
    domain_tools = {
        s.name: set(s.allowed_tools) for s in skills.values() if s.kind == KIND_DOMAIN
    }
    tools = [
        {
            "name": name,
            "description": spec.description,
            "used_by": sorted(d for d, ts in domain_tools.items() if name in ts),
        }
        for name, spec in TOOL_REGISTRY.items()
    ]
    return {
        "domains": domains,
        "tools": tools,
        "overlay_limits": {
            "max_overlays_per_request": _MAX_OVERLAYS_PER_REQUEST,
            "max_name_len": MAX_OVERLAY_NAME_LEN,
            "max_prompt_len": MAX_OVERLAY_PROMPT_LEN,
        },
        "disabled_tools_limits": {
            "max_disabled_tools_per_request": _MAX_DISABLED_TOOLS_PER_REQUEST,
        },
    }

@app.get("/datasets/{dataset_id}")
async def get_dataset(dataset_id: str):
    try:
        from agent.dataset_loader import load_dataset
        import dataclasses
        ds = load_dataset(dataset_id)
        return dataclasses.asdict(ds)
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=str(e))
