# FactChecker — 多智能体网页文章事实核查系统

> 软件工程与计算 Ⅲ 课程项目 · 浏览器插件 + Agent 推理核心 + 评估平台
> Python 3.11+ · FastAPI · Chrome MV3 插件

FactChecker 是一个基于**多智能体辩论机制**的网页文章事实核查系统。以浏览器插件形态运行：用户浏览网页时触发分析，Agent 自动扫描文章中的可疑声明，经多角色交叉辩论验证后，在原文上以高亮标注方式标记四类错误（事实性错误、逻辑谬误、自相矛盾、证据不足），并在侧边栏展示完整推理链与证据来源。

本项目由 **zzt、fyl、zyy、wcp、lyr、ljy** 六人共同完成。

---

## 目录

- [核心特性](#核心特性)
- [系统架构](#系统架构)
- [仓库结构](#仓库结构)
- [快速开始](#快速开始)
- [测试](#测试)
- [项目文档](#项目文档)
- [团队成员](#团队成员)

---

## 核心特性

### 1. 多智能体辩论机制
不是单一的“提问→回答”模式，每条可疑声明经过三方辩论：

```
Verifier（有工具）→ Challenger（无工具审视）→ Rebuttal（反驳修正）→ Judge（最终裁决）
```

- **Verifier**：运行 ReAct 循环（Thought → Action → Observation），调用外部工具收集证据
- **Challenger**：仅基于推理审视证据链是否充分，提出异议或放行
- **Judge**：综合两方观点做出最终裁决，输出四类错误标注
- **高置信度快速通道**：置信度达标时自动跳过后续辩论，节省 LLM 调用

### 2. 复杂度自适应路由
根据声明的认知复杂度（Simple / Medium / Complex）自动选择验证深度，由**纯规则引擎**分类（零额外 LLM 开销），包含事件污染惩罚等细节设计。

### 3. 领域专家 Agent 池
覆盖**医疗、金融、科技、新闻政策、通用**五大领域，每个专家拥有独立 persona、辩论视角与策略偏好（如医学 Agent 使用 GRADE 证据分级、金融 Agent 校验数据口径与时效性）。基于组合模式，新增领域只需注册一行。

### 4. DAG 工作流引擎
处理管线由 YAML 定义的有向无环图驱动，支持条件分支、动态跳过、运行时路由、异常隔离与实时 SSE 流式推送——替换一个 YAML 文件即可完全自定义管线。

### 5. 14 个专业核查工具 + 全链路优雅降级
覆盖通用、医疗、金融、科技、新闻政策五大领域（web_search、wikipedia_lookup、pubmed_search、stock_quotes、fact_check_domestic/global 等），每层外部依赖都有 fallback 路径。

### 6. SSE 实时流 + 持久化
11 种事件类型通过 Server-Sent Events 实时推送到浏览器插件，同时完整落库 SQLite（7 张表），支持历史查询、会话恢复和单条声明重验证。

---

## 系统架构

```
┌──────────────────────────────────────────────────────────────────┐
│                     Chrome 浏览器插件（agent_frontend）           │
│  提取文章文本 → SSE 流消费 → 原文高亮标注 + 侧边栏推理链           │
└──────────────────────────┬───────────────────────────────────────┘
                           │ POST /analyze (SSE)
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                    FastAPI 后端（agent/backend）                  │
│  · SSE 流式中转  · Session/Claim/Event CRUD  · Chat 追问         │
│  · 15s 心跳保活 (Chrome MV3 Service Worker)                      │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│             WorkflowEngine（DAG 状态机，agent/agent/workflow）    │
│                                                                  │
│  ScanNode ──→ PlanNode ──→ CrossRefNode ──→ RouteNode           │
│                    (条件跳过)         (领域专家实例化)            │
│                         │                                        │
│  SummaryNode ←── DebateNode ←────────────────────┘              │
│  (并行Semaphore(3))   ↑                                          │
│                       策略融合 + 复杂度自适应 + 领域专家人格      │
└──────────────────────────┬───────────────────────────────────────┘
                           │
            ┌──────────────┼──────────────┐
            ▼              ▼              ▼
     ┌──────────┐  ┌──────────┐  ┌──────────┐
     │  GLM-4   │  │ DeepSeek │  │ SQLite   │
     │  Flash   │  │ fallback │  │ (7表)    │
     └──────────┘  └──────────┘  └──────────┘
```

---

## 仓库结构

```
mutiagent-truth-check-system/
├── agent/                    # Agent 推理核心 + FastAPI 后端
│   ├── agent/                #   Agent 主类、扫描器、规划器、辩论逻辑
│   │   ├── agents/           #     领域专家 Agent 池（5 大领域）
│   │   ├── workflow/         #     DAG 工作流引擎（YAML 可配置）
│   │   ├── skills/           #     领域技能系统
│   │   ├── tools/            #     14 个核查工具
│   │   └── llm/              #     LLM 客户端（GLM / Claude / DeepSeek）
│   ├── backend/              #   FastAPI 后端（REST + SSE 接口、SQLite）
│   ├── dataset/              #   评估数据集
│   ├── tests/                #   122+ 个测试（零失败）
│   ├── docs/                 #   开发设计文档
│   ├── README.md             #   ← 后端详细说明
│   └── requirements.txt
│
├── agent_frontend/           # Chrome 浏览器插件（MV3）
│   ├── manifest.json
│   ├── background/           #   Service Worker
│   ├── content/              #   内容脚本 + 高亮标注 + UI
│   ├── sidepanel/            #   侧边栏（推理链展示、聊天追问）
│   ├── popup/                #   弹窗
│   └── README.md             #   ← 前端详细说明
│
├── 文档/                     # 课程文档（需求分析、设计、优化报告）
│   ├── 迭代一/               #   Ragas 分析、评估指标设计实验报告
│   ├── 迭代二/               #   需求分析、体系结构设计、接口文档
│   ├── 迭代三/               #   详细设计、优化文档、需求分析
│   └── README.md
│
└── outputs/                  # 评估输出（错误分布图表等）
```

> 各子模块的详细说明请分别阅读 `agent/README.md`、`agent_frontend/README.md` 与 `文档/`。

---

## 快速开始

### 环境要求

- Python 3.11+
- Conda（推荐）或 venv

### 1. 安装依赖

```bash
cd agent
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，填入 API Keys（按需）：

```env
# GLM-4-Flash（默认 — 主推理、声明提取、领域路由）
GLM_API_KEY=xxxxxx.xxxxxx
GLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
CHAT_LLM_PROVIDER=glm

# Claude API（可选）
ANTHROPIC_API_KEY=sk-ant-xxxxxx
ANTHROPIC_BASE_URL=https://yunwu.ai

# Tavily Search API（可选 — 未配置时降级 DuckDuckGo）
TAVILY_API_KEY=tvly-xxxxxx
```

### 3. 启动后端服务

```bash
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

- 交互式 API 文档：http://127.0.0.1:8000/docs
- 健康检查：http://127.0.0.1:8000/health
- 分析接口：`POST http://127.0.0.1:8000/analyze`（SSE 流式）

### 4. 加载浏览器插件

1. 打开 Chrome → `chrome://extensions`
2. 开启右上角「开发者模式」
3. 点击「加载已解压的扩展程序」，选择 `agent_frontend/` 目录

### 5. Docker 部署（可选）

```bash
cd agent
docker build -t FactChecker .
docker run -p 8000:8000 --env-file .env FactChecker
```

---

## 测试

```bash
cd agent
pytest tests/ -v        # 全部测试
```

预期输出：**127 passed — 零失败**（覆盖 DAG 引擎、工具连通性、Agent 管线、规划器、领域专家等 11 个测试文件）。

---

## 项目文档

| 阶段 | 内容 |
| ---- | ---- |
| 迭代一 | Ragas 项目分析、评估指标设计实验报告 |
| 迭代二 | 需求分析文档、体系结构设计文档、模块接口文档（5 个模块） |
| 迭代三 | 详细设计文档、需求分析、优化文档（含优化前后数据对比） |

完整文档见 [`文档/`](文档/) 目录。

---

## 团队成员

本项目由以下六人共同完成：

| 成员 | 角色 |
| ---- | ---- |
| zzt  | 项目成员 |
| fyl  | 项目成员 |
| zyy  | 项目成员 |
| wcp  | 项目成员 |
| lyr  | 项目成员 |
| ljy  | 项目成员 |

---

## License

课程项目，仅供学习交流使用。
