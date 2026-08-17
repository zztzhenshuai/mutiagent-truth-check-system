# 成员 A：辩论流与事件接口说明

## 1. 当前已落地能力

- `Agent.run()` 已从单次 `ReAct` 改为 `Coordinator -> Verifier -> Challenger -> Judge`
- 新增 SSE 事件：
  - `debate`
  - `summary`
  - `chat`（仅冻结结构，暂未由后端接口发射）
- `done` 事件已扩展，可携带：
  - `total_claims`
  - `summary_available`
  - `reverify_supported`
  - `claim_results`

## 2. 事件字段

### `debate`

用于时间轴展示和数据库落库。

```json
{
  "type": "debate",
  "claim_id": "c001",
  "round": 1,
  "phase": "started|argument|result",
  "role": "coordinator|verifier|challenger|judge",
  "message": "字符串说明",
  "stance": "support|challenge|neutral",
  "confidence": 0.82,
  "evidence_urls": ["https://..."],
  "details": {}
}
```

### `summary`

用于侧边栏总结卡片和历史记录概要。

```json
{
  "type": "summary",
  "total_claims": 3,
  "total_annotations": 2,
  "clean_claims": 1,
  "challenged_claims": 1,
  "revised_claims": 1,
  "error_breakdown": {
    "factual_error": 1,
    "logical_fallacy": 0,
    "contradiction": 0,
    "unsupported_claim": 1
  },
  "representative_claims": [],
  "overall_conclusion": "字符串说明",
  "reverify_supported": true
}
```

### `chat`

当前只冻结格式，供 B/C 后续接聊天接口。

```json
{
  "type": "chat",
  "session_id": "可空",
  "message_id": "可空",
  "claim_id": "可空",
  "role": "user|assistant|system",
  "message": "字符串消息",
  "reverify_target": "claim_id 或 null",
  "details": {}
}
```

### `done`

```json
{
  "type": "done",
  "total_annotations": 2,
  "total_claims": 3,
  "summary_available": true,
  "reverify_supported": true,
  "claim_results": []
}
```

## 3. 辩论流程

1. `Coordinator` 发起 `debate.started`
2. `Verifier` 执行带工具的 `ReAct`
3. `Verifier` 产出初判，并发 `debate.argument`
4. `Challenger` 基于 claim、推理和工具结果判断是否提出异议
5. 若异议成立，`Coordinator` 触发第二轮重验证
6. `Judge` 输出最终裁决，并发 `debate.result`
7. 发出最终 `annotation`
8. 所有 claim 完成后发 `summary`
9. 最后发 `done`

## 4. 给成员 B 的持久化建议

- `event_record.payload` 直接存整条事件 JSON
- `summary_record` 可以直接复用 `summary` 事件字段
- `claim_record.final_result` 可以直接复用 `done.claim_results[*]`
- `tool_call_record` 可以从 `claim_results[*].verifier.tool_calls` 和 `rebuttal.tool_calls` 拆出

## 5. 给成员 C 的前端接入建议

- `debate.phase=started` 作为回合标题
- `debate.phase=argument` 作为发言卡片
- `debate.phase=result` 作为最终裁决卡片
- `summary.representative_claims` 直接渲染总结区
- `done.reverify_supported=true` 时，为 claim 卡片保留“重新验证”按钮
