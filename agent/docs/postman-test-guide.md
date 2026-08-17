# Postman 手动测试指南

> 后端启动命令（在 `agent/` 目录下执行）：
> ```bash
> cd /home/lyr/Main/Softer/软工三/projects/agent
> source myenv/bin/activate
> python3 -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
> ```

---

## 基础信息

- **Base URL**: `http://localhost:8000`
- **API 前缀**: `/api/v1`
- **设备 ID Header**: `X-Device-ID: test-device-001`（必加，模拟前端插件发送）

> 将以下内容导入 Postman 或手动创建请求。每个接口均需要Header：`X-Device-ID`

---

## 1. 健康检查

| 方法 | 路径 |
|------|------|
| `GET` | `/health` |

**预期响应** (200):
```json
{
    "status": "ok",
    "agent_initialized": true,
    "db_initialized": true
}
```

> 如果 `db_initialized` 为 `false`，检查 SQLite 文件是否生成在 `agent/backend/agent.db`。

---

## 2. 创建会话

| 方法 | 路径 |
|------|------|
| `POST` | `/api/v1/sessions` |

**Headers**: `Content-Type: application/json`, `X-Device-ID: test-device-001`

**请求体**:
```json
{
    "article_title": "测试文章标题",
    "source_url": "https://example.com/test-article",
    "article_text": "这是一篇测试文章的内容，包含一些可能需要核查的声明。中国2023年GDP增速为8.5%。",
    "domain": "finance",
    "skill_id": null
}
```

**预期响应** (201):
```json
{
    "session_id": "sess_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "status": "pending",
    "created_at": "2026-06-09T12:00:00.000000"
}
```

> **保存返回的 `session_id`**，后续测试需要用到。

---

## 3. 会话列表

| 方法 | 路径 |
|------|------|
| `GET` | `/api/v1/sessions` |

**Query Params（可选）**:
- `limit=20`
- `offset=0`
- `status=pending`
- `domain=finance`

**预期响应** (200):
```json
{
    "items": [
        {
            "session_id": "sess_xxx...",
            "article_title": "测试文章标题",
            "domain": "finance",
            "status": "pending",
            "total_claims": null,
            "total_annotations": null,
            "created_at": "2026-06-09T12:00:00.000000"
        }
    ],
    "total": 1
}
```

---

## 4. 会话详情

| 方法 | 路径 |
|------|------|
| `GET` | `/api/v1/sessions/{session_id}` |

> 将 `{session_id}` 替换为步骤 2 返回的实际 ID。

**预期响应** (200):
```json
{
    "session": {
        "id": "sess_xxx...",
        "device_id": "test-device-001",
        "article_title": "测试文章标题",
        "source_url": "https://example.com/test-article",
        "domain": "finance",
        "skill_id": null,
        "status": "pending",
        "total_claims": null,
        "total_annotations": null,
        "created_at": "2026-06-09T12:00:00.000000",
        "started_at": null,
        "finished_at": null,
        "error_message": null
    },
    "summary": null,
    "claims": [],
    "recent_events": []
}
```

---

## 5. 会话事件历史

| 方法 | 路径 |
|------|------|
| `GET` | `/api/v1/sessions/{session_id}/events` |

**Query Params（可选）**:
- `limit=100`
- `after_seq=0`
- `event_type=status`

**预期响应** (200) — 新会话无事件时返回空列表:
```json
{
    "items": []
}
```

---

## 6. 会话总结

| 方法 | 路径 |
|------|------|
| `GET` | `/api/v1/sessions/{session_id}/summary` |

**预期响应** (404) — 新会话还无总结:
```json
{
    "detail": {
        "error": {
            "code": "SUMMARY_NOT_FOUND",
            "message": "summary not found for this session"
        }
    }
}
```

---

## 7. 单条声明详情

| 方法 | 路径 |
|------|------|
| `GET` | `/api/v1/sessions/{session_id}/claims/c001` |

**预期响应** (404) — 新会话无声明:
```json
{
    "detail": {
        "error": {
            "code": "CLAIM_NOT_FOUND",
            "message": "claim not found"
        }
    }
}
```

---

## 8. 发送追问

| 方法 | 路径 |
|------|------|
| `POST` | `/api/v1/sessions/{session_id}/chat` |

**Headers**: `Content-Type: application/json`

**请求体**:
```json
{
    "message": "为什么这条声明被判错？",
    "related_claim_id": "c001",
    "mode": "explain"
}
```

**预期响应** (201):
```json
{
    "message_id": "msg_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "session_id": "sess_xxx...",
    "status": "accepted"
}
```

> **保存返回的 `message_id`**。

---

## 9. 对话历史

| 方法 | 路径 |
|------|------|
| `GET` | `/api/v1/sessions/{session_id}/chat` |

**Query Params（可选）**:
- `limit=50`
- `before_id=msg_xxx`（用于分页）

**预期响应** (200):
```json
{
    "items": [
        {
            "id": "msg_xxx...",
            "role": "user",
            "content": "为什么这条声明被判错？",
            "related_claim_id": "c001",
            "message_type": "explain",
            "created_at": "2026-06-09T12:05:00.000000"
        }
    ]
}
```

---

## 10. 重新验证

| 方法 | 路径 |
|------|------|
| `POST` | `/api/v1/sessions/{session_id}/recheck` |

**Headers**: `Content-Type: application/json`

**请求体**:
```json
{
    "claim_id": "c001",
    "reason": "用户追问后要求重新核验",
    "skill_id": null
}
```

**预期响应** (202):
```json
{
    "session_id": "sess_xxx...",
    "claim_id": "c001",
    "status": "queued"
}
```

---

## 11. 错误场景测试

### 11.1 不存在的会话

`GET /api/v1/sessions/sess_nonexistent`

**预期响应** (404):
```json
{
    "detail": {
        "error": {
            "code": "SESSION_NOT_FOUND",
            "message": "session not found"
        }
    }
}
```

### 11.2 缺少 X-Device-ID（仍正常返回，device_id 为 "unknown"）

`POST /api/v1/sessions` + 省略 `X-Device-ID` Header → 正常创建，详情中 `device_id` 为 `"unknown"`

---

## 12. 完整测试流程（按顺序执行）

```
1. GET  /health                          → 确认服务存活
2. POST /api/v1/sessions                 → 创建会话 A（带 X-Device-ID）
3. POST /api/v1/sessions                 → 创建会话 B（不带 X-Device-ID）
4. GET  /api/v1/sessions                 → 列表应含 A 和 B
5. GET  /api/v1/sessions?status=pending  → 过滤，应含 A 和 B
6. GET  /api/v1/sessions?domain=finance  → 过滤，只含 A
7. GET  /api/v1/sessions/{A的id}         → 详情含 device_id = "test-device-001"
8. GET  /api/v1/sessions/{B的id}         → 详情含 device_id = "unknown"
9. GET  /api/v1/sessions/{A的id}/summary → 404 错误
10. POST /api/v1/sessions/{A的id}/chat   → 发送追问
11. GET  /api/v1/sessions/{A的id}/chat   → 查历史，含刚发的消息
12. POST /api/v1/sessions/{A的id}/recheck → 返回 queued
13. GET  /api/v1/sessions/{A的id}/events  → 空列表
14. GET  /api/v1/sessions/{A的id}/claims/c001 → 404
15. GET  /api/v1/sessions/nonexistent    → 404
```

---

## 13. SQLite 数据验证（可选）

测试完成后，用命令行直接查看数据库：

```bash
cd /home/lyr/Main/Softer/软工三/projects/agent/backend
sqlite3 agent.db ".tables"
sqlite3 agent.db "SELECT id, status, device_id FROM analysis_session;"
sqlite3 agent.db "SELECT id, content, role FROM chat_message;"
```

预期看到 `analysis_session`、`chat_message` 等表中有你测试时创建的数据。
