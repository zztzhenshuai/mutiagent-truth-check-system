# Agent 核心模块接口文档

> 维护人：成员 A  
> 最后更新：2026-04-08  
> 供 B、C、D、E 对接使用

---

## 1. 项目结构

```
agent/
├── models.py          # 所有数据结构和 SSE 事件类型（B/C/E 重点阅读）
├── agent.py           # Agent 主类，run() 方法
├── scanner.py         # 文章扫描器（内部模块，无需对接）
├── planner.py         # 规划器（内部模块，无需对接）
├── llm/               # LLM 客户端（内部模块）
└── tools/
    └── registry.py    # 工具注册表（B、D 重点阅读）
```

---

## 2. 环境配置

复制 `.env.example` 为 `.env`，填入对应 Key：

```bash
cp .env.example .env
```

```
ANTHROPIC_API_KEY=sk-ant-xxxxxx     # Claude API Key，找 A 获取或自行申请
GLM_API_KEY=xxxxxx.xxxxxx           # 智谱 GLM Key，在 open.bigmodel.cn 免费申请
```

> `.env` 文件已加入 `.gitignore`，**严禁提交到仓库**。

---

## 3. 安装依赖

```bash
pip install anthropic openai pydantic python-dotenv
```

---

## 4. SSE 事件格式（供 B、C、E）

Agent 运行时按顺序 yield 以下 6 种事件，B 将其包装为 SSE 流，C 消费高亮，E 记录追踪。

### 4.1 plan — 扫描完成

```json
{
  "type": "plan",
  "timestamp": "2026-04-08T10:00:00+00:00",
  "total": 5,
  "claims": [
    {"id": "c001", "text": "中国GDP增速为8.5%", "suspicion_score": 0.85},
    {"id": "c002", "text": "2023年全球最大...", "suspicion_score": 0.55}
  ]
}
```

C 可在收到此事件时初始化侧边栏进度条（共 N 条声明）。

### 4.2 thinking — 推理步骤

```json
{
  "type": "thinking",
  "timestamp": "...",
  "claim_id": "c001",
  "thought": "该数字与已知数据存在偏差，需要搜索核实"
}
```

### 4.3 tool_call — 工具调用

```json
{
  "type": "tool_call",
  "timestamp": "...",
  "claim_id": "c001",
  "tool_name": "web_search",
  "tool_input": "中国2023年GDP增速",
  "tool_output": "结论：约4.8%。来源：[https://worldbank.org/...]"
}
```

### 4.4 annotation — 标注结果（核心）

```json
{
  "type": "annotation",
  "timestamp": "...",
  "claim_id": "c001",
  "text": "中国GDP增速为8.5%",
  "start_offset": 142,
  "end_offset": 157,
  "error_type": "factual_error",
  "confidence": 0.92,
  "reasoning": "世界银行数据显示2023年增速约4.8%，与文章所称8.5%不符",
  "evidence_urls": ["https://worldbank.org/..."]
}
```

`error_type` 枚举值：

| 值 | 含义 |
|----|------|
| `factual_error` | 事实性错误 |
| `logical_fallacy` | 逻辑谬误 |
| `contradiction` | 文章内部矛盾 |
| `unsupported_claim` | 无来源断言 |
| `null` | 未发现错误（声明属实） |

C 高亮颜色建议：

```javascript
const ERROR_COLORS = {
  factual_error:     '#FFB3B3',  // 红
  logical_fallacy:   '#FFE0B3',  // 橙
  contradiction:     '#FFF9B3',  // 黄
  unsupported_claim: '#B3D4FF',  // 蓝
};
```

### 4.5 error — 异常

```json
{
  "type": "error",
  "timestamp": "...",
  "claim_id": "c001",
  "message": "工具 web_search 调用超时"
}
```

`claim_id` 为 `null` 表示全局错误（如文章扫描失败）。收到全局 error 后紧跟 `done` 事件。

### 4.6 done — 完成

```json
{
  "type": "done",
  "timestamp": "...",
  "total_annotations": 3
}
```

---

## 5. Agent 调用方式（供 B）

```python
import asyncio
from dotenv import load_dotenv
from agent import Agent
from agent.llm import ClaudeClient

load_dotenv()

agent = Agent(complex_llm=ClaudeClient())

# 在 FastAPI SSE 接口中消费
async def event_stream(article_text: str):
    async for state in agent.run(article_text):
        yield f"data: {state.model_dump_json()}\n\n"
    yield "data: \n\n"  # 关闭 SSE 流
```

`Agent` 是单例，可以在 FastAPI 的 lifespan 里初始化一次，在多个请求间复用。

---

## 6. 工具注册接口（供 B、D）

在 `agent/tools/registry.py` 的 `TOOL_REGISTRY` 中注册你的工具：

```python
# 在 registry.py 末尾追加

from agent.tools.web_search import web_search  # B 实现的工具

TOOL_REGISTRY["web_search"] = ToolSpec(
    name="web_search",
    description="搜索互联网核实事实性声明。当声明包含具体数字、统计数据或历史事件时使用。输入：待核查的声明原文。",
    input_schema={
        "type": "object",
        "properties": {
            "input": {"type": "string", "description": "待核查的声明文本"}
        },
        "required": ["input"],
    },
    func=web_search,
)
```

**工具函数规范：**

```python
# 所有工具必须满足此签名
async def your_tool(input: str) -> str:
    try:
        # 工具逻辑
        return result_string
    except Exception as e:
        return f"工具执行失败：{str(e)}"  # 异常必须在内部捕获，不允许上抛
```

> `description` 字段非常重要，Agent 根据它决定何时调用你的工具，请认真填写。

---

## 7. 当前实现状态

| 模块 | 状态 | 备注 |
|------|------|------|
| 数据结构 / SSE 事件格式 | ✅ 完成 | `models.py` |
| LLM 客户端（Claude + GLM） | ✅ 完成 | `llm/` |
| 文章扫描器 | ✅ 完成 | `scanner.py` |
| 规划器（可疑度排序） | ✅ 完成 | `planner.py` |
| ReAct 循环（Thought-Action-Observation）| ✅ 完成 | `agent.py`，见第 7.1 节 |
| 工具注册表（stub 工具） | ✅ 完成（stub）| 真实工具等待 B/D 填充，见第 6 节 |
| Reflexion 反思机制 | 🚧 未开始 | 第3-4周完成 |

### 7.1 ReAct 循环实现细节（第2周新增）

`_react_loop` 已完整实现，逻辑如下：

```
1. 构建 system prompt（含可用工具列表）
2. 初始化 messages = [system, user("请验证：{claim.text}")]
3. 循环最多 _MAX_STEPS = 6 次：
   a. 调用 LLM（Sonnet 4.6），append assistant 回复
   b. 正则解析回复：
      - 含 "Final Answer: {...}" → 解析 JSON，yield ThinkingEvent + AnnotationEvent，结束
      - 含 "Action: xxx" → yield ThinkingEvent，调用工具，yield ToolCallEvent，
                          将 "Observation: {result}" append 进 messages，继续
      - 格式异常 → yield ErrorEvent，提示 LLM 重新格式化，继续
4. 超出 6 步未结束 → yield 兜底 AnnotationEvent（confidence=0.0）
```

**工具调用异常处理**：工具抛出异常时，将异常信息作为 `Observation` 传回 LLM（同时 yield `ErrorEvent`），循环不中断，LLM 可以换工具或直接给出 Final Answer。

**tool_output 截断**：`ToolCallEvent.tool_output` 字段截断为 500 字符（供前端展示）；传给 LLM 的 Observation 使用完整内容。

---

## 8. B/D 组待实现清单

### 8.1 B 组需要实现的工具（`agent/tools/registry.py`）

目前 `TOOL_REGISTRY` 中已有 3 个 **stub（占位）工具**，函数名以 `_stub_` 开头，全部返回固定字符串。B 组需要将其替换为真实实现。

| 工具名 | 当前状态 | B 组需要做什么 |
|--------|----------|----------------|
| `web_search` | stub，返回固定字符串 | 实现真实网络搜索（推荐 DuckDuckGo / SerpAPI），替换 `_stub_web_search` 函数，更新 `TOOL_REGISTRY["web_search"].func` |
| `wikipedia_lookup` | stub，返回固定字符串 | 调用 Wikipedia API 返回词条摘要，替换 `_stub_wikipedia_lookup` 函数 |
| `source_verifier` | stub，返回固定字符串 | 实现来源可信度判断，替换 `_stub_source_verifier` 函数 |

**替换方式**（修改 `registry.py`）：

```python
# 删除或保留 stub 函数，只需更新 func 字段即可
from agent.tools.web_search import web_search   # B 自己写的模块

TOOL_REGISTRY["web_search"] = ToolSpec(
    name="web_search",
    description="搜索互联网核实事实性声明。当声明包含具体数字、统计数据或历史事件时使用。输入：待核查的声明原文。",
    input_schema={
        "type": "object",
        "properties": {"input": {"type": "string", "description": "待核查的声明文本"}},
        "required": ["input"],
    },
    func=web_search,
)
```

> `description` 字段直接影响 Agent 选工具的准确性，请认真填写，说明"何时用、输入什么"。

### 8.2 D 组需要实现的工具

| 工具名 | 说明 |
|--------|------|
| `cross_reference` | 跨声明交叉验证。检查同一文章中不同声明之间是否存在矛盾。输入：声明文本。 |

注册方式同 8.1，在 `TOOL_REGISTRY` 中新增一条即可。

### 8.3 工具函数规范（B/D 必读）

```python
async def your_tool(input: str) -> str:
    """
    - 必须是 async 函数
    - 接受单个 str 参数（工具输入）
    - 返回 str（工具输出）
    - 所有异常必须在内部捕获，失败时返回错误字符串，不允许上抛
    """
    try:
        result = ...  # 工具逻辑
        return result
    except Exception as e:
        return f"工具执行失败：{str(e)}"
```

**验证方法**：B/D 实现工具后，可以用以下代码快速验证注册是否正确：

```python
import asyncio
from agent.tools.registry import TOOL_REGISTRY

async def test():
    result = await TOOL_REGISTRY["web_search"].func("中国2023年GDP增速")
    print(result)

asyncio.run(test())
```

---

## 9. 联系方式

对接过程中有任何格式疑问请直接联系 A。
