# FactChecker — AI-Powered Web Article Fact-Checking Agent

> 软件工程与计算Ⅲ · 浏览器插件 + Agent 推理核心 + 评估平台  
> **127 测试 · 零失败 · Python 3.11+**

FactChecker 是一个基于多智能体辩论机制的网页文章事实核查系统。以浏览器插件形态运行，用户浏览网页时触发分析，Agent 自动扫描文章中的可疑声明，经多角色交叉辩论验证后，以高亮标注方式在原文上标记四类错误，并在侧边栏展示完整推理链与证据来源。

---

## 目录

- [核心亮点](#核心亮点)
- [系统架构](#系统架构)
- [处理管线](#处理管线)
- [项目结构](#项目结构)
- [数据库设计](#数据库设计)
- [API 接口](#api-接口)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [开发指南](#开发指南)
- [测试](#测试)

---

## 核心亮点

### 1. 多智能体辩论机制

不是单一的"提问→回答"模式。每条可疑声明经过三方辩论：

```
Verifier（有工具）→ Challenger（无工具审视）→ Rebuttal（反驳修正）→ Judge（最终裁决）
```

- **Verifier**：运行 ReAct 循环（Thought → Action → Observation），调用 14 个外部工具收集证据
- **Challenger**：仅基于推理审视 Verifier 的证据链是否充分，提出异议或放行
- **Judge**：综合两方观点做出最终裁决，输出四类错误标注
- **高置信度快速通道**：Verifier 置信度 ≥ 阈值且无工具错误时，自动跳过后续辩论，节省 LLM 调用

### 2. 复杂度自适应路由

声明并非一律等同。根据认知复杂度自动选择验证深度，由**纯规则引擎**分类（零额外 LLM 开销）：

| 复杂度      | 策略     | ReAct 步数 | 辩论流                              |
| ----------- | -------- | ---------- | ----------------------------------- |
| **Simple**  | 快速核验 | 2 轮       | 无辩论，直接输出                    |
| **Medium**  | 标准验证 | ≤3 轮      | 跳过 Challenger，低置信度启用 Judge |
| **Complex** | 深度辩论 | ≤6 轮      | 完整 V→C→J + Reflexion 反思         |

复杂度分类基于**两级信号**判决，由纯规则引擎完成：

- **Simple 信号**：纯数字/日期/专名模式、短文本、少实体
- **Complex 信号**：因果链密度、多层引用、并列复杂结构、**事件/案件/灾害关键词**（强/弱两级：`连刺|枪击|劫持` 等高度特异词为强信号，`死亡|事故|袭击` 等常见词为弱信号，需 ≥2 个弱信号协同触发）
- **事件污染惩罚**：短声明若包含事件描述关键词，对 simple_score 施加惩罚，防止 `"一名18岁男孩被连刺9刀"` 被误判为 simple 而在 2 步内来不及得出结论

### 3. 领域专家 Agent 池

不同领域启用不同"专家人格"（persona + 辩论视角 + 策略偏好），而非仅切换工具白名单：

| 专家             | 领域     | 专属能力                                                         |
| ---------------- | -------- | ---------------------------------------------------------------- |
| **MedicalAgent** | 循证医学 | GRADE 证据分级、RCT 评估、零容忍替代药物宣称                     |
| **FinanceAgent** | 财经数据 | 数据时效性（GDP 季度/CPI 月度）、口径一致性（YoY/MoM/名义/实际） |
| **TechAgent**    | 前沿科技 | 同行评审 vs 预印本、专利生命周期、API 版本时效                   |
| **NewsAgent**    | 时事政策 | 信源优先级金字塔（辟谣平台 > 官方声明 > 权威媒体 > 自媒体）      |
| **GeneralAgent** | 通用兜底 | 全默认行为，完全向后兼容                                         |

基于组合模式（DomainAgent 不继承 Agent），每个专家覆盖 system prompt、Challenger/Judge/Reflexion 提示词、策略融合和置信度校准系数。新增领域只需注册一行。

### 4. DAG 工作流引擎

处理管线从硬编码直线重构为可配置有向无环图（YAML 定义），支持：

- **条件分支**：如"声明为空 → 直接跳过所有后续步骤"
- **动态跳过**：节点自行判断是否需要执行（如"声明 < 3 条 → 跳过交叉引用预热"）
- **运行时路由**：节点输出可覆盖默认下游
- **异常隔离**：单节点失败不中断管线，自动沿边继续
- **实时 SSE 流**：支持 `execute_streaming()` 的节点可逐事件推送

替换一个 YAML 文件即可完全自定义管线，无需改代码。

### 5. Reflexion 反思机制

Complex 声明在 Judge 终裁后，若置信度处于灰色地带 [0.60, 0.80)，自动触发反思——让 Verifier 审视自身推理链中的遗漏或逻辑缺陷，修正后再输出。

### 6. Reformatter 格式修复 + 工具名自动纠错

ReAct 循环中的两类常见 LLM 输出错误通过独立的**单轮修复请求**无声解决，不污染前端事件流：

**Reformatter（格式修复）**：当 LLM 输出不符合 ReAct 协议（如自然语言 `💭 需要核实...` 而非 `Thought: / Action:`），检测到格式异常后发起一次纯格式转换请求，将内容重新组织为正确格式。修复成功后替换 messages[-1]，继续正常流程——**不消耗 ReAct 步数、不产生 ErrorEvent**。

**工具名修复（Tool Name Repair）**：LLM 经常输出错误工具名（如 `WebSearch` 而非 `web_search`、`fact_check` 而非 `fact_check_domestic`）。当工具名不在 TOOL_REGISTRY 或不在领域白名单时，自动发起一次名称匹配请求，从候选工具列表中语义匹配最接近的正确工具名。

```
ReAct 循环出错
  ├── 格式异常 (parsed["type"] == "unknown")
  │     └── _reformat_response() → 独立 LLM 格式转换
  │           ├── 成功 → messages[-1] = reformatted → 正常继续
  │           └── 失败 → ErrorEvent + correction_hint（回退）
  │
  ├── 工具名错误 (tool_name not in registry/whitelist)
  │     └── _repair_tool_name() → 独立 LLM 名称匹配
  │           ├── 成功 → tool_name = repaired → 正常执行
  │           └── 失败 → ErrorEvent（回退）
  │
  └── 步数耗尽但无结论 (step >= max_steps, 已调工具但未输出 Final Answer)
        └── 强制结论调用（Force Conclusion）→ 汇总工具输出，禁止调工具
              ├── 成功 → Final Answer → 正常产出 annotation
              └── 失败 → 兜底 annotation (error_type=None, confidence=0)
```

**强制结论调用（Force Conclusion）**：当 ReAct 循环耗尽步数但仍未给出 Final Answer（如 simple 策略 2 步内调了 `web_search` → `fact_check_global` 但未及结论），追加一次纯结论 LLM 请求——汇总已收集的所有工具输出，禁止继续调工具，强制模型基于现有证据输出 Final Answer。只在强制结论也失败时才走兜底 annotation（error_type=None, confidence=0）。

关键设计：三类修复/容错请求都是**独立新对话**（无 ReAct 上下文污染），任务极简（纯格式/纯匹配/纯结论），成功率高。

### 7. 14 个专业核查工具 + 全链路优雅降级

覆盖通用、医疗、金融、科技、新闻政策五大领域：

| 工具                       | 所属领域  | 后端服务                                 |
| -------------------------- | --------- | ---------------------------------------- |
| `web_search`               | 通用      | Tavily API → DuckDuckGo                  |
| `wikipedia_lookup`         | 通用      | Wikipedia Action API（中/英）            |
| `source_verifier`          | 通用      | readability-lxml 网页正文提取            |
| `cross_reference`          | 通用      | sentence-transformers + LLM 语义矛盾检测 |
| `wikidata_lookup`          | 通用/科技 | Wikidata 知识图谱 API                    |
| `macro_statistics_global`  | 金融      | World Bank API → 本地缓存                |
| `stock_market_quotes`      | 金融      | Yahoo Finance API                        |
| `pubmed_scientific_search` | 医疗      | PubMed eutils API                        |
| `consumer_health_verifier` | 医疗      | 权威健康网站定向搜索                     |
| `academic_paper_search`    | 科技      | Semantic Scholar API                     |
| `preprint_arxiv_search`    | 科技      | arXiv API → web search fallback          |
| `patent_status_lookup`     | 科技      | 专利数据库定向搜索                       |
| `fact_check_domestic`      | 新闻      | piyao.org.cn / fact.qq.com               |
| `fact_check_global`        | 新闻/通用 | Google Fact Check Tools API              |

每一层外部依赖都有 fallback 路径，保证系统韧性。

### 8. SSE 实时流 + 持久化

11 种事件类型通过 Server-Sent Events 实时推送到浏览器插件，同时完整落库 SQLite，支持历史查询、会话恢复和单条声明重验证。15 秒心跳保活符合 Chrome MV3 Service Worker 限制。

---

## 系统架构

```
┌──────────────────────────────────────────────────────────────────┐
│                     Chrome 浏览器插件                             │
│  提取文章文本 → SSE 流消费 → 原文高亮标注 + 侧边栏推理链           │
└──────────────────────────┬───────────────────────────────────────┘
                           │ POST /analyze (SSE)
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                    FastAPI 后端                                   │
│  · SSE 流式中转  · Session/Claim/Event CRUD  · Chat 追问         │
│  · 15s 心跳保活 (Chrome MV3 Service Worker)                      │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                   WorkflowEngine (DAG 状态机)                     │
│                                                                  │
│  ScanNode ──→ PlanNode ──→ CrossRefNode ──→ RouteNode           │
│                              (条件跳过)        (领域专家实例化)    │
│                                                   │              │
│  SummaryNode ←── DebateNode ←────────────────────┘              │
│  (并行Semaphore(3))   ↑                                          │
│                       │ 策略融合                                 │
│              ┌────────┴────────┐                                 │
│              │ VerificationStrategy │  ← 复杂度自适应             │
│              │ DomainAgent.persona  │  ← 领域专家人格             │
│              └─────────────────────┘                             │
└──────────────────────────┬───────────────────────────────────────┘
                           │
            ┌──────────────┼──────────────┐
            ▼              ▼              ▼
     ┌──────────┐  ┌──────────┐  ┌──────────┐
     │  GLM-4   │  │ DeepSeek │  │ SQLite   │
     │  Flash   │  │ fallback │  │ (7表)    │
     │ (默认)   │  │          │  │          │
     └──────────┘  └──────────┘  └──────────┘
```

---

## 处理管线

```
文章输入 (HTML → 纯文本)
  │
  ├─ [1] Scanner (GLM-4-Flash 默认)
  │     提取所有值得核查的声明 → str.find() 精确定位 offset
  │     过滤 LLM 幻构声明
  │
  ├─ [2] Planner (规则引擎，不调用 LLM)
  │     · 计算 suspicion_score（数字/+0.35、引用/+0.20、绝对词/+0.20、时间/+0.15）
  │     · classify_complexity() → simple / medium / complex
  │     · 按 suspicion_score 降序排列
  │
  ├─ [3] Cross-Reference 上下文预热
  │     sentence-transformers 句向量索引（声明 < 3 条时自动跳过）
  │
  ├─ [4] Skill Router (GLM-4-Flash)
  │     文章领域分类 → 选择领域技能 → 实例化领域专家 Agent
  │
  ├─ [5] Debate Loop (并行 asyncio，Semaphore(3))
  │     对每条声明：
  │     ┌──────────────────────────────────────────┐
  │     │ domain_agent.merge_strategy(complexity)   │
  │     │     ↓                                     │
  │     │  Verifier (ReAct 循环，工具调用)            │
  │     │     ↓                                     │
  │     │  Challenger (审视证据充分性)                │
  │     │     ↓                                     │
  │     │  Rebuttal (仅 Challenger 异议时)            │
  │     │     ↓                                     │
  │     │  Judge (最终裁决)                           │
  │     │     ↓                                     │
  │     │  Reflexion (仅 complex + 灰色地带)          │
  │     │     ↓                                     │
  │     │  置信度校准 → AnnotationEvent               │
  │     └──────────────────────────────────────────┘
  │
  └─ [6] Summary
        全局总结 + 错误分布统计 + 代表性声明
```

### 四类错误

| 类型                | 说明       | 示例                                 |
| ------------------- | ---------- | ------------------------------------ |
| `factual_error`     | 事实性错误 | 数字、日期、事件与权威来源不符       |
| `logical_fallacy`   | 逻辑谬误   | 因果倒置、偷换概念、以偏概全         |
| `contradiction`     | 自相矛盾   | 文内多处说法矛盾或与外部权威证据矛盾 |
| `unsupported_claim` | 证据不足   | 缺乏充分独立来源的交叉验证           |

---

## 项目结构

```
agent/                         # Agent 推理核心
├── __init__.py                #   包导出
├── agent.py                   #   Agent 主类（协调器，run() 仅 ~40 行）
├── models.py                  #   数据模型 + 11 种 SSE 事件 + VerificationStrategy
├── scanner.py                 #   文章扫描器
├── planner.py                 #   规划器（评分 + 复杂度分类）
├── preprocess.py              #   文本预处理
├── debate.py                  #   辩论逻辑（V/C/J prompt/parse + Reflexion）
├── dataset_loader.py          #   评估数据集加载器
│
├── agents/                    # 【方向5】领域专家 Agent 池
│   ├── base.py                #     DomainAgent 基类（7 个可覆盖方法）
│   ├── general.py             #     GeneralAgent — 兜底
│   ├── medical.py             #     MedicalAgent — 循证医学专家
│   ├── finance.py             #     FinanceAgent — 财经数据分析师
│   ├── technology.py          #     TechAgent — 前沿科技核查专家
│   ├── news_policy.py         #     NewsAgent — 时事政策编辑
│   └── registry.py            #     AGENT_REGISTRY + get_domain_agent()
│
├── workflow/                  # 【方向9】DAG 工作流引擎
│   ├── node.py                #     WorkflowNode ABC + NodeOutput + WorkflowContext
│   ├── config.py              #     DAGConfig + ConditionalEdge + load_dag_config()
│   ├── engine.py              #     WorkflowEngine — 拓扑序状态机
│   ├── default_dag.yaml       #     默认管线 DAG 定义
│   └── nodes/                 #     管线节点
│       ├── scan.py            #       ScanNode
│       ├── plan.py            #       PlanNode
│       ├── context.py         #       CrossReferenceNode（含 can_skip）
│       ├── route.py           #       RouteNode + DomainAgent 实例化
│       ├── debate.py          #       DebateNode（并行声明 + 实时 SSE）
│       └── summary.py         #       SummaryNode + EmptySummaryNode
│
├── skills/                    # 领域技能系统
│   ├── base.py                #     Skill 数据模型（含 persona/agent_config）
│   ├── router.py              #     领域路由（GLM-4-Flash 分类）
│   └── defs/                  #     5 个领域定义（Markdown + YAML frontmatter）
│
├── tools/                     # 14 个核查工具
│   ├── registry.py            #     TOOL_REGISTRY + ToolSpec
│   ├── web_search.py          #     Tavily → DuckDuckGo
│   ├── wikipedia_lookup.py    #     Wikipedia Action API
│   ├── source_verifier.py     #     readability-lxml 网页提取
│   ├── cross_reference.py     #     sentence-transformers + LLM 矛盾检测
│   ├── wikidata_lookup.py     #     Wikidata API
│   ├── official_statistics.py #     World Bank API → 本地缓存
│   ├── stock_quotes.py        #     Yahoo Finance
│   ├── pubmed_search.py       #     PubMed eutils
│   ├── consumer_health.py     #     消费者健康验证
│   ├── academic_search.py     #     Semantic Scholar
│   ├── arxiv_search.py        #     arXiv
│   ├── patent_lookup.py       #     专利状态查询
│   └── fact_check.py          #     国内外辟谣平台
│
└── llm/
    ├── base.py                #     BaseLLMClient ABC
    ├── claude.py              #     Claude Sonnet 4.6
    ├── glm.py                 #     GLM-4-Flash
    └── chat_factory.py        #     Chat LLM 工厂

backend/                       # FastAPI 后端
├── main.py                    #   应用入口 + Agent singleton
├── db/                        #   SQLAlchemy ORM（7 表）
├── routers/sessions.py        #   9 个 REST 端点
├── services/                  #   会话/聊天/事件/上下文服务
└── schemas/                   #   Pydantic 校验

datasets/                      # 评估数据集
docs/                          # 13 份设计文档
tests/                         # 122 个测试（11 个文件）
```

---

## 数据库设计

7 张表，SQLAlchemy ORM + SQLite：

| 表                 | 用途                   | 关键字段                                                                                  |
| ------------------ | ---------------------- | ----------------------------------------------------------------------------------------- |
| `analysis_session` | 分析会话根实体         | device_id, article_title, article_text, domain, skill_id, status, total_claims            |
| `claim_record`     | 每条声明的完整验证结果 | claim_id, text, offsets, verdict, error_type, confidence, reasoning, evidence_urls (JSON) |
| `event_record`     | 所有 SSE 事件持久化    | session_id, seq, type, payload (JSON)                                                     |
| `tool_call_record` | 工具调用追踪           | tool_name, input, output, status, latency_ms                                              |
| `summary_record`   | 会话总结               | overall_conclusion, error_breakdown (JSON), representative_evidence (JSON)                |
| `chat_message`     | 聊天追问历史           | role, content, related_claim_id, message_type                                             |
| `skill_record`     | 技能定义存档           | skill_name, domain, system_prompt, allowed_tools (JSON), version                          |

---

## API 接口

### SSE 流式接口

| 端点            | 说明                                               |
| --------------- | -------------------------------------------------- |
| `POST /analyze` | 核心分析接口，SSE 流式返回 11 种事件               |
| `POST /v1/run`  | 新版接口（含 domain_agent、complexity 等扩展字段） |

### REST 接口（`/api/v1/`）

| 方法   | 端点                                       | 说明                                         |
| ------ | ------------------------------------------ | -------------------------------------------- |
| POST   | `/sessions`                                | 创建分析会话                                 |
| GET    | `/sessions`                                | 列出会话（支持 device_id/title/status 过滤） |
| GET    | `/sessions/{id}`                           | 会话详情（含 claims + summary + events）     |
| DELETE | `/sessions/{id}`                           | 删除会话                                     |
| GET    | `/sessions/{id}/events`                    | 按 seq 分页查询事件（支持 type 过滤）        |
| POST   | `/sessions/{id}/chat`                      | 发送聊天追问                                 |
| GET    | `/sessions/{id}/chat`                      | 聊天历史（cursor 分页）                      |
| POST   | `/sessions/{id}/claims/{claim_id}/recheck` | 单条声明重验证                               |

### 其他端点

| 端点                 | 说明             |
| -------------------- | ---------------- |
| `GET /health`        | 健康检查         |
| `GET /skills`        | 可用领域技能列表 |
| `GET /datasets/{id}` | 获取评估数据集   |

### SSE 事件类型（11 种）

| type         | 说明                                                     |
| ------------ | -------------------------------------------------------- |
| `status`     | 分阶段状态（scan / context / route / verify / complete） |
| `plan`       | 扫描完成，包含声明总数 + 复杂度分布                      |
| `thinking`   | ReAct 推理中间步骤                                       |
| `tool_call`  | 工具调用及返回结果                                       |
| `debate`     | 辩论事件（started / argument / result）                  |
| `annotation` | 单条声明验证完成（触发前端高亮）                         |
| `summary`    | 全局分析总结（错误分布 + 代表性声明）                    |
| `chat`       | 聊天消息                                                 |
| `chat_chunk` | 聊天流式 token                                           |
| `chat_done`  | 聊天流结束                                               |
| `error`      | 错误事件（工具失败、LLM 异常等）                         |
| `done`       | 全部分析完成                                             |

---

## 快速开始

### 1. 环境要求

- Python 3.11+
- Conda（推荐）或 venv

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，填入 API Keys（按需）：

```env
# GLM-4-Flash（默认 — 主推理、声明提取、领域路由）
GLM_API_KEY=xxxxxx.xxxxxx
GLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
CHAT_LLM_PROVIDER=glm

# Claude API（可选 — 仅显式 CHAT_LLM_PROVIDER=claude 时使用）
ANTHROPIC_API_KEY=sk-ant-xxxxxx
ANTHROPIC_BASE_URL=https://yunwu.ai

# Tavily Search API（可选 — 未配置降级 DuckDuckGo）
TAVILY_API_KEY=tvly-xxxxxx

# Cross-Reference 本地模型（可选 — 网络差时使用）
LOCAL_MODEL_PATH=models/cross-reference/paraphrase-multilingual-MiniLM-L12-v2
CROSS_REFERENCE_LOCAL_FILES_ONLY=false
```

### 4. 启动后端服务

```bash
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

服务启动后：
- 交互式 API 文档：http://127.0.0.1:8000/docs
- 健康检查：http://127.0.0.1:8000/health
- 分析接口：`POST http://127.0.0.1:8000/analyze`

### 5. 运行测试

```bash
# 全部测试
pytest tests/ -v

# 按模块运行
pytest tests/test_workflow_engine.py -v   # DAG 工作流引擎
pytest tests/test_domain_agents.py -v     # 领域专家 Agent
pytest tests/test_planner.py -v           # 规划器 + 复杂度分类
pytest tests/test_new_tools.py -v         # 14 个工具连通性
pytest tests/test_agent.py -v             # Agent 管线集成
```

预期输出：**122 passed**

### 6. Docker 部署

```bash
docker build -t FactChecker .
docker run -p 8000:8000 --env-file .env FactChecker
```

---

## 配置说明

### LLM 模型分配

| 任务                       | 模型                | 环境变量                       |
| -------------------------- | ------------------- | ------------------------------ |
| 文章扫描、ReAct 推理、辩论 | GLM-4-Flash（默认） | `GLM_API_KEY` / `GLM_BASE_URL` |
| 领域路由分类               | GLM-4-Flash         | `GLM_API_KEY`                  |
| 聊天追问                   | 可配置（默认 GLM）  | `CHAT_LLM_PROVIDER`            |

### 可调参数

以下常量在 `agent/agent.py` 中定义：

| 参数                     | 默认值 | 说明                            |
| ------------------------ | ------ | ------------------------------- |
| `_MAX_CONCURRENT_CLAIMS` | 3      | 并行处理声明数（避免 API 限流） |
| `_MIN_FINAL_CONFIDENCE`  | 0.60   | 置信度低于此值 → 忽略该发现     |
| `_REFLEXION_LOW`         | 0.60   | Reflexion 灰色地带下界          |
| `_REFLEXION_HIGH`        | 0.80   | Reflexion 灰色地带上界          |

策略参数（`_STRATEGY_MAP`，可由 DomainAgent 覆盖）：

| 复杂度  | max_react_steps | enable_challenger                | high_confidence_threshold             |
| ------- | --------------- | -------------------------------- | ------------------------------------- |
| simple  | 2               | 否                               | —                                     |
| medium  | 3               | 否（医学/金融/新闻领域覆盖为是） | 0.85                                  |
| complex | 6               | 是                               | 0.90（医学/金融/新闻领域覆盖为 0.92） |

### 自定义管线

替换 DAG YAML 配置文件即可完全自定义处理管线：

```python
from agent.workflow import WorkflowEngine, WorkflowContext

ctx = WorkflowContext(article_text="...", _llm=llm, ...)
engine = WorkflowEngine("path/to/custom_dag.yaml")
async for event in engine.run(ctx):
    yield event
```

DAG 配置格式参考 `agent/workflow/default_dag.yaml`。

---

## 开发指南

### 架构决策

| 决策       | 选择                 | 理由                                                 |
| ---------- | -------------------- | ---------------------------------------------------- |
| Agent 扩展 | 组合（非继承）       | DomainAgent 不继承 Agent，避免深层继承链，测试更简单 |
| 管线编排   | DAG + YAML           | 可热替换，节点可复用，支持条件分支和动态跳过         |
| ReAct 协议 | 自研文本协议         | 比原生 tool-use 更灵活可控，正则解析容错 + 纠错反馈  |
| 置信度校准 | 乘法系数（规则）     | 不依赖 LLM 自评，基于客观信号修正                    |
| 并行策略   | asyncio.Semaphore(3) | 控制 LLM API 并发，避免限流                          |
| 容错       | 全链路 fallback      | 每层外部依赖有降级路径                               |

### 新增领域专家

```python
# 1. 新建 agent/agents/my_domain.py
from .base import DomainAgent

class MyDomainAgent(DomainAgent):
    def build_system_prompt(self, overlays, disabled_note_tools):
        return "你是 XX 领域专家..."

    def merge_strategy(self, complexity):
        s = super().merge_strategy(complexity)
        # 领域策略微调
        return s

# 2. 注册 — agent/agents/registry.py
AGENT_REGISTRY["my_domain"] = MyDomainAgent
```

### 新增管线节点

```python
from agent.workflow.node import WorkflowNode, NodeOutput

class MyNode(WorkflowNode):
    name = "my_node"

    async def execute(self, ctx):
        result = ...
        return NodeOutput(data={"my_key": result})
```

然后在 DAG YAML 中引用：`class: mypackage.MyNode`。

---

## 测试

```
127 passed — 零失败

测试文件                    测试数  覆盖内容
─────────────────────────────────────────────────
test_workflow_engine.py      34     DAG 引擎（节点/条件/拓扑/执行/异常）
test_new_tools.py            28     14 个工具连通性 + 降级路径
test_agent.py                17     Agent 管线集成 + Reformatter + 领域路由
test_planner.py              12     评分 + 复杂度分类器（含事件信号检测）
test_scanner.py               8     扫描器（JSON 解析/偏移/去重）
test_cross_reference.py       8     语义矛盾检测 + 模型缓存
test_domain_agents.py         6     领域专家（选择/策略/校准）
test_preprocess.py            4     文本预处理
test_agent_chat.py            4     聊天功能
test_dataset_loader.py        4     数据集加载
test_context_builder.py       4     上下文构建
conftest.py                  —     async 测试夹具
```
