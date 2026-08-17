# Prompt for 方向9: DAG 工作流引擎 重构

请先通读仓库，理解项目全貌和已完成的架构改动，然后给出**方向9的重构计划**（只出计划，不改文件）。

---

## 项目背景

这是一个 **AI 网页文章事实核查系统**（软件工程与计算Ⅲ课程项目，6人团队）。用户浏览器插件触发分析 → FastAPI 后端 → Agent 推理核心 → SSE 流式返回错误标注。

### 目录速览

| 目录 | 职责 |
|------|------|
| `agent/` | Agent 推理核心（管线 + 模型 + 工具 + 技能） |
| `agent/agents/` | **方向5新增**：5个领域专家 Agent（组合模式，不继承 Agent） |
| `agent/llm/` | LLM 客户端（Claude Sonnet 4.6 + GLM-4-Flash） |
| `agent/skills/defs/` | 5 个领域技能定义文件（markdown + YAML frontmatter，含 persona/agent_config） |
| `agent/tools/` | 14 个核查工具 + 注册表 |
| `backend/` | FastAPI 后端 + SQLite 持久化（7 表） |
| `tests/` | 89 个测试 |

### 请务必通读的核心文件

```
agent/agent.py            # ~1250行，主 Agent 类 + run() 管线入口
agent/models.py           # 内部数据结构 + 11种SSE事件类型 + VerificationStrategy
agent/planner.py          # suspicion_score + classify_complexity() + build_plan()
agent/scanner.py          # scan_article()，LLM提取声明 + offset定位
agent/debate.py           # VerificationRecord / ChallengeRecord / ClaimDebateRecord +
                          #   各阶段 prompt builder/parser + reflexion prompt/parser
agent/agents/base.py      # DomainAgent 基类（7个可覆盖方法）
agent/agents/registry.py  # AGENT_REGISTRY + get_domain_agent() 工厂
agent/agents/medical.py   # MedicalAgent（覆盖5个方法，典型子类代表）
agent/skills/base.py      # Skill 数据模型（含 persona / agent_config 字段）
agent/skills/router.py    # route_skill()，GLM领域路由
agent/tools/registry.py   # TOOL_REGISTRY，14个 ToolSpec
agent/llm/base.py         # BaseLLMClient ABC
backend/main.py            # FastAPI 入口，Agent singleton 初始化
```

---

## 已完成的架构改动（关键上下文，必须理解）

### 方向1：复杂度自适应路由（迭代四完成）

目标：每条声明按复杂度走不同深度的验证。

**改动位置**：

1. `agent/models.py` — 新增：
   - `ComplexityLevel = Literal["simple", "medium", "complex"]`
   - `VerificationStrategy` dataclass（max_react_steps、enable_challenger、enable_judge、enable_rebuttal、enable_reflexion、tool_required、high_confidence_threshold）
   - `Claim` 新增 `complexity` / `complexity_confidence` 字段

2. `agent/planner.py` — 新增 `classify_complexity()` 规则分类器（~120行），`build_plan()` 中为每条声明打复杂度标签

3. `agent/agent.py`：
   - `_STRATEGY_MAP: dict[ComplexityLevel, VerificationStrategy]` — 三级策略常量
     - simple → 1步 ReAct，不强制工具，无辩论
     - medium → ≤3步 ReAct，跳过 Challenger，低置信度走 Judge
     - complex → ≤6步 ReAct + 完整 V→C→J 辩论 + Reflexion 反思
   - `_debate_claim()` 重构为策略驱动分流（接收 `strategy` 参数）
   - `_react_loop()` 新增 `max_steps` 和 `tool_required` 参数
   - 新增 `_run_reflexion()` — complex 专属，置信度 ∈ [0.60, 0.80) 时触发反思

4. `agent/debate.py` — 新增 `build_reflexion_prompt()` + `parse_reflexion_response()`

5. 日志可见性：`★ simple 快速通道` / `★ medium 标准验证` / `★ complex 深度辩论` / `★ 高置信度快速通道` / `触发 Reflexion`

### 方向5：领域专家 Agent 池（迭代四完成）

目标：不同领域有不同的"核查人格"（prompt + 辩论视角 + 策略偏好）。

**新增 `agent/agents/` 包**（组合模式，不继承 Agent）：

```
agent/agents/
├── __init__.py      # 导出所有类 + AGENT_REGISTRY + get_domain_agent()
├── base.py          # DomainAgent 基类，7个可覆盖方法（全部有默认实现）
├── general.py       # GeneralAgent — 纯继承，兜底
├── medical.py       # MedicalAgent — 循证医学专家
├── finance.py       # FinanceAgent — 财经数据分析师
├── technology.py    # TechAgent — 前沿科技与学术核查专家
├── news_policy.py   # NewsAgent — 时事政策编辑
└── registry.py      # AGENT_REGISTRY 字典 + get_domain_agent() 工厂
```

**DomainAgent 可覆盖方法**（默认 = 当前通用行为）：

| 方法 | 用途 |
|------|------|
| `build_system_prompt()` | Verifier 系统 prompt（核心覆盖点，包含 persona + 领域知识） |
| `build_challenger_prompt()` | Challenger 质疑 prompt（医学质疑证据等级，金融质疑数据时效） |
| `build_judge_prompt()` | Judge 裁决 prompt（医学 GRADE 优先，新闻辟谣平台一票采纳） |
| `build_reflexion_prompt()` | Reflexion 反思 prompt |
| `merge_strategy(complexity)` | 策略融合：对 `_STRATEGY_MAP` 做领域微调（如 Medical 对 medium 也启用 Challenger） |
| `get_calibration_multipliers()` | 置信度校准系数偏置（Medical 对无工具惩罚更重：0.70 vs 默认 0.80） |
| `should_skip_debate()` | 是否跳过辩论（Medical 额外要求至少一次工具调用才可跳过） |

**`agent/skills/` 扩展**：
- `Skill` 新增 `persona`（角色身份描述）和 `agent_config`（策略偏好）字段
- 5 个 `defs/*.md` 升级了 frontmatter，如 medical.md 新增：
  ```yaml
  persona: |
    你是一名循证医学事实核查专家。你精通临床流行病学、生物统计学和 GRADE 证据质量分级体系...
  agent_config:
    strict_complexity: true
    challenger_for_medium: true
    calibration:
      no_tool: 0.70
      tool_error: 0.80
      no_evidence_url: 0.85
  ```

**`agent/agent.py` 协调器化改动**：
- `__init__` 新增 `_domain_agent_cache` 惰性缓存
- `run()` 路由后调用 `get_domain_agent(skill.name)` 实例化领域专家
- `_debate_claim()` / `_run_challenger()` / `_run_judge()` / `_run_reflexion()` / `_calibrate_annotation()` 全部新增 `domain_agent=` 可选参数，委托 prompt 构建
- 所有委托点有 `if domain_agent is not None: ... else: 回退默认` 的兼容路径
- 日志可见性：`领域专家 Agent 已实例化：medical（类型=MedicalAgent）`、`领域 Agent MedicalAgent 策略覆盖 (complexity=medium)：enable_challenger:False→True`

---

## 当前管线（方向1+方向5融合后）

```python
Agent.run(article_text, overlays, disabled_tools)
  │
  ├─ 1. scan_article(article_text, self._llm)          → list[Claim]
  ├─ 2. build_plan(claims)                             → VerificationPlan
  │      ├─ _compute_score(claim.text)                 → suspicion_score
  │      └─ classify_complexity(claim.text)            → complexity + confidence
  ├─ 3. prepare_cross_reference_context(article_text)  → 预热句向量
  ├─ 4. route_skill(article_text)                      → Skill
  ├─ 5. get_domain_agent(skill.name)                   → DomainAgent
  │
  └─ 6. asyncio.Queue + Semaphore(3) 并行处理所有声明：
        for claim in plan.claims:
            strategy = domain_agent.merge_strategy(claim.complexity)
            _debate_claim(claim, ... strategy, domain_agent)
              ├─ Verifier (ReAct, 命令行 max_steps + tool_required)
              ├─ [可选] Challenger (根据 strategy 和 domain_agent.should_skip_debate)
              ├─ [可选] Rebuttal (Challenger 异议时)
              ├─ [可选] Judge (根据 strategy)
              └─ [可选] Reflexion (complex 且置信度灰色地带)
  │
  └─ 7. build_summary_event() → DoneEvent
```

**核心问题**：管线是 `run()` 方法里的一条硬编码直线，步骤顺序完全固定，无条件分支和动态调度能力。

---

## 方向9目标：DAG 工作流引擎

### 要解决什么问题

1. **步骤耦合**：`run()` 方法承担了编排 + 执行 + 事件汇聚所有职责，新增一个步骤就要改 `run()`
2. **无动态调度**：无法根据中间结果决定下一步（如"如果扫描结果 < 3 条声明，跳过 cross_reference 预热"）
3. **无预处理差异化**：所有领域走完全相同的预处理管线
4. **无法组合**：无法把 scan+plan 的结果喂给多个下游节点并行处理

### 目标架构

把管线从一条写死的直线变为**可配置的有向无环图（DAG）**：

```
每个节点 = 一个独立的 WorkflowNode（输入 → 处理 → 输出）
节点之间 = 条件边（上一步输出决定下一步走向）
```

### 建议的改动方向（供新对话自由裁量）

1. **定义 `WorkflowNode` 协议**：每个节点有 `name`、`input_schema`、`output_schema`、`async execute(ctx) -> NodeOutput`
2. **定义 `WorkflowContext`**：在节点间传递共享状态（article_text、claims、plan、skill、domain_agent、annotation_count 等）
3. **定义 DAG 描述格式**：YAML/JSON 配置文件描述节点列表 + 边列表 + 条件表达式
4. **实现 `WorkflowEngine`**：读取 DAG 配置，按拓扑序执行节点，处理条件分支和扇出/扇入
5. **将现有步骤拆为独立节点**：ScanNode、PlanNode、RouteNode、DebateNode、SummaryNode 等
6. **新增差异化节点**：如 Medical 领域的 `PubMedPreVerifyNode`、Finance 的 `WorldBankPreFetchNode`
7. **`Agent.run()` 简化为**：构建 WorkflowEngine → 加载配置 → run()

### 约束

- SSE 事件类型和前后端接口不变
- 数据库 schema 不变
- 89 个现有测试保持通过
- 向后兼容：默认 DAG 配置 = 当前 `run()` 的行为
- 与方向1（VerificationStrategy）和方向5（DomainAgent）正交融合

---

请先通读上述关键文件，理解当前架构，然后给出**方向9的重构计划**（只出计划，不改代码）。计划应包含：涉及文件清单、每个文件的改动内容、WorkflowNode 协议设计、DAG 描述格式、与方向1/方向5的融合点、风险点和缓解措施。
