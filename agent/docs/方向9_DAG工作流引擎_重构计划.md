# 方向9：DAG 工作流引擎 — 重构计划

> **产出**：只出计划，不改代码。
> **日期**：2026-06-13
> **基线**：迭代四完成（方向1 复杂度路由 + 方向5 领域专家池），89 测试通过。

---

## 一、当前管线分析

### 1.1 `Agent.run()` 当前结构（~150 行，第 146–378 行）

```
scan_article → build_plan → prepare_cross_reference → route_skill → get_domain_agent
  → [并发处理 claims: _debate_claim → _react_loop → _run_challenger → _run_judge → _run_reflexion]
    → build_summary_event → DoneEvent
```

### 1.2 核心痛点

| 痛点 | 表现 |
|------|------|
| **步骤硬编码** | scan→plan→context→route→debate 固定在 `run()` 中，无分支能力 |
| **无动态调度** | 无法根据中间结果决定下一步。如"scan 返回 < 3 条声明 → 跳过跨引用预热" |
| **预处理统一** | 5 个领域共享完全相同的 scan+plan+context+route 预处理管线 |
| **无法组合** | 无法把 scan+plan 结果扇出给多个下游节点（如并行跑两个不同策略的 debate） |
| **run() 职责过重** | 编排 + 执行 + 限流 + 结果汇聚混在一起，新增步骤必然改 `run()` |
| **代码膨胀** | `_debate_claim()` 已有 ~385 行（514–898），内部揉合了 5 种策略分流 |

### 1.3 不可破坏的约束

1. **SSE 事件类型不变**：`models.py` 中 11 种事件类型外签名不变，前端依赖
2. **数据库 schema 不变**：`backend/agent.db` 7 表结构不变
3. **89 个现有测试保持通过**：重构后行为等价
4. **向后兼容**：默认 DAG 配置必须复现当前 `run()` 的完整行为
5. **与方向1/方向5正交**：`VerificationStrategy` 和 `DomainAgent` 不受影响

---

## 二、核心设计

### 2.1 架构总览

```
┌─────────────────────────────────────────────────────┐
│                    WorkflowEngine                     │
│                                                       │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐         │
│  │ DAG      │   │ TopoSort │   │ Node     │         │
│  │ Config   │──▶│ Scheduler│──▶│ Executor │         │
│  │ (YAML)   │   │          │   │ (asyncio)│         │
│  └──────────┘   └──────────┘   └──────────┘         │
│                       │                               │
│                 ┌─────▼──────┐                        │
│                 │WorkflowCtx │ ← 共享状态              │
│                 │ (TypedDict)│                        │
│                 └────────────┘                        │
└─────────────────────────────────────────────────────┘
```

### 2.2 `WorkflowNode` 协议

```python
# 新文件: agent/workflow/node.py

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Protocol

# WorkflowContext 是所有节点的共享字典
class WorkflowContext(dict):
    """类型安全的共享上下文。
    约定字段（渐进添加，TypedDict 超集）：
      - article_text: str
      - overlays: list[dict] | None
      - disabled_tools: list[str] | None
      - claims: list[Claim]
      - plan: VerificationPlan
      - skill: Skill
      - domain_agent: DomainAgent | None
      - records: list[ClaimDebateRecord]
      - annotation_count: int
      - effective_skill: Skill
      - removed_tools: tuple[str, ...]
    """
    pass


@dataclass
class NodeOutput:
    """节点执行结果。"""
    next_node: str | None = None       # 强制指定下一节点（覆盖边），None 则走边
    data: dict[str, Any] = field(default_factory=dict)  # 写入 ctx 的键值对
    events: list[Any] = field(default_factory=list)     # 本节点产出的 SSE 事件


class WorkflowNode(ABC):
    """DAG 节点抽象基类。

    每个节点是无状态的，只依赖 ctx 参数。
    节点内部不应修改非本节点的 ctx 键。
    """

    name: str                          # 唯一标识（类属性，如 "scan"）
    description: str = ""              # 节点用途说明
    input_keys: tuple[str, ...] = ()   # 必需的 ctx 键
    output_keys: tuple[str, ...] = ()  # 写入 ctx 的键（文档用，非强制）

    @abstractmethod
    async def execute(self, ctx: WorkflowContext) -> NodeOutput:
        """执行节点逻辑，返回输出。

        Args:
            ctx: 工作流共享上下文，可读写。

        Returns:
            NodeOutput: 包含下一步指示、写入 ctx 的数据、本节点产出的 SSE 事件。
        """
        ...

    def can_skip(self, ctx: WorkflowContext) -> bool:
        """是否可以跳过此节点（可选覆盖，用于动态调度）。"""
        return False


# 辅助：普通函数适配为 WorkflowNode 的简化方式
class FunctionalNode(WorkflowNode):
    """将 async 函数包装为 WorkflowNode。"""

    def __init__(
        self,
        name: str,
        fn,
        *,
        description: str = "",
        input_keys: tuple[str, ...] = (),
        output_keys: tuple[str, ...] = (),
        can_skip_fn=None,
    ):
        super().__init__()
        self.name = name
        self.description = description
        self.input_keys = input_keys
        self.output_keys = output_keys
        self._fn = fn
        self._can_skip = can_skip_fn

    async def execute(self, ctx: WorkflowContext) -> NodeOutput:
        return await self._fn(ctx)

    def can_skip(self, ctx: WorkflowContext) -> bool:
        if self._can_skip:
            return self._can_skip(ctx)
        return False
```

**设计原则**：
- `WorkflowNode` 持有 `name`（类属性），与 DAG 配置中的节点名一一对应
- `execute()` 接收 `ctx` 共享上下文，返回 `NodeOutput`
- `NodeOutput.data` 的键写入 `ctx`，下游节点可用
- 节点只负责"自己的事"，不关心它在 DAG 中的位置
- `FunctionalNode` 提供便捷包装，避免为每个小节点写类

### 2.3 DAG 配置格式（YAML）

```yaml
# 新文件: agent/workflow/default_dag.yaml
# 默认配置 = 当前 Agent.run() 的完整行为

name: default_factcheck
version: 1
description: 默认事实核查管线（与当前 Agent.run() 行为一致）

# ── 节点声明 ──
nodes:
  - name: scan
    class: agent.workflow.nodes.ScanNode
    description: 扫描文章提取声明
    input_keys: [article_text]
    output_keys: [claims]

  - name: plan
    class: agent.workflow.nodes.PlanNode
    description: 计算 suspicion_score + 复杂度分类，生成计划
    input_keys: [claims]
    output_keys: [plan]

  - name: context
    class: agent.workflow.nodes.CrossReferenceNode
    description: 准备跨引用上下文
    input_keys: [article_text]
    output_keys: [context_ready]
    skip_if: "ctx.get('plan') and len(ctx['plan'].claims) < 3"  # ★ 动态调度

  - name: route
    class: agent.workflow.nodes.RouteNode
    description: 领域路由 + DomainAgent 实例化
    input_keys: [article_text, plan]
    output_keys: [skill, effective_skill, domain_agent, removed_tools]

  - name: debate
    class: agent.workflow.nodes.DebateNode
    description: 并行处理所有声明（Verifier/Challenger/Judge）
    input_keys: [plan, effective_skill, domain_agent, removed_tools]
    output_keys: [claim_records, annotation_count]

  - name: summary
    class: agent.workflow.nodes.SummaryNode
    description: 生成总结 + DoneEvent
    input_keys: [claim_records, annotation_count, plan]
    output_keys: [summary_event, done_event]

# ── 边定义（条件边可选）──
edges:
  - from: scan
    to: plan

  - from: plan
    to: context
    # 无条件边：scan 后总是到 plan

  - from: context
    to: route
    # context 节点内部 may skip，但边本身无条件

  - from: route
    to: debate

  - from: debate
    to: summary

# ── 条件边（方向9的核心价值）──
conditional_edges:
  - from: plan
    condition: "len(ctx.get('plan', {}).claims) == 0"
    to: summary_empty    # 零声明 → 直接结束
    else: context

  - from: context
    condition: "ctx.get('context_skipped')"
    to: route
    else: route
    # context 节点标记跳过后走到 route，此处展示条件边语法
```

**配置说明**：
- `nodes` 声明所有节点，`class` 是 Python 导入路径
- `edges` 定义无条件走向
- `conditional_edges` 的 `condition` 是 Python 表达式，在 `ctx` 命名空间中求值
- `skip_if` 允许节点自行声明跳过条件（引擎在调用 `execute` 前检查）
- 引擎加载时做 DAG 合法性校验（无环、所有边端点存在、无孤立节点）

### 2.4 引擎设计

```python
# 新文件: agent/workflow/engine.py

class WorkflowEngine:
    """DAG 工作流引擎。

    职责：
    1. 加载 YAML 配置
    2. 解析节点类引用
    3. 拓扑排序
    4. 按序执行节点（支持条件跳过和扇出/扇入）
    5. 汇聚 SSE 事件流
    """

    def __init__(self, config_path: str | Path):
        self._config = self._load_config(config_path)
        self._nodes: dict[str, WorkflowNode] = {}
        self._edges: dict[str, list[str]] = {}      # from → [to...]
        self._conditional_edges: list[ConditionalEdge] = []
        self._topo_order: list[str] = []

    async def run(
        self,
        ctx: WorkflowContext,
    ) -> AsyncGenerator[AgentState, None]:
        """主入口：按 DAG 顺序执行节点，yield 所有 SSE 事件。

        流程：
        1. 实例化所有节点
        2. 拓扑排序
        3. 按序执行，遇到 skip_if 跳过节点
        4. 扇出节点用 asyncio.gather 并发执行
        5. 收集所有事件并逐条 yield
        """
        ...

    def _load_config(self, path: str | Path) -> dict:
        """加载并校验 YAML 配置。"""
        ...

    def _resolve_node_class(self, class_path: str) -> type[WorkflowNode]:
        """将 'agent.workflow.nodes.ScanNode' 解析为类对象。"""
        ...

    def _verify_dag(self) -> bool:
        """校验 DAG 合法性：无环、边端点存在、无孤立节点、有唯一入口和出口。"""
        ...
```

**执行语义**：
1. 拓扑排序 → 确定执行顺序
2. 按序执行，节点用 `can_skip()` / `skip_if` 跳过
3. 扇出节点（一条边指向多个下游）→ 并发执行
4. 扇入节点（多条边指向同一节点）→ 等待所有上游完成后执行
5. `NodeOutput.data` 合并到 `ctx`
6. `NodeOutput.next_node` 覆盖条件边决策
7. 所有 `NodeOutput.events` 按到达顺序 yield

### 2.5 现有步骤拆为独立节点

| 当前代码位置 | 节点类名 | 新文件 |
|---|---|---|
| `scanner.scan_article()` | `ScanNode` | `agent/workflow/nodes/scan.py` |
| `planner.build_plan()` | `PlanNode` | `agent/workflow/nodes/plan.py` |
| `tools/cross_reference.py:prepare_cross_reference_context()` | `CrossReferenceNode` | `agent/workflow/nodes/context.py` |
| `skills/router.py:route_skill()` + `get_domain_agent()` | `RouteNode` | `agent/workflow/nodes/route.py` |
| `_debate_claim()` + `_react_loop()` + `_run_challenger()` + `_run_judge()` + `_run_reflexion()` | `DebateNode` | `agent/workflow/nodes/debate.py` |
| `build_summary_event()` + `DoneEvent` | `SummaryNode` | `agent/workflow/nodes/summary.py` |
| 零声明快速结束 | `EmptySummaryNode` | `agent/workflow/nodes/summary.py` |

**节点拆分原则**：
- 每个节点类 < 200 行
- 节点不持有 Agent 引用，通过 `ctx` 获取所需依赖（如 `ctx["_llm"]`）
- 节点内部可调用现有模块函数（`scan_article`、`build_plan` 等），不是重写而是包装
- SSE 事件通过 `NodeOutput.events` 返回，由引擎统一 yield

### 2.6 `Agent.run()` 重构后

```python
# agent/agent.py — Agent.run() 简化为：

async def run(
    self,
    article_text: str,
    overlays: list[dict] | None = None,
    disabled_tools: list[str] | None = None,
) -> AsyncGenerator[AgentState, None]:
    ctx = WorkflowContext(
        article_text=article_text,
        overlays=overlays,
        disabled_tools=disabled_tools,
        # 注入引擎所需依赖
        _llm=self._llm,
        _router_llm=self._router_llm,
        _skills=self._skills,
        _domain_agent_cache=self._domain_agent_cache,
    )
    engine = WorkflowEngine(DEFAULT_DAG_CONFIG)
    async for event in engine.run(ctx):
        yield event
```

### 2.7 领域差异化 DAG 配置（方向5融合）

每个领域可定义自己的 DAG 覆盖，通过 `agent_config` 中的 `dag_overrides` 字段：

```yaml
# agent/skills/defs/medical.md 的 frontmatter 新增:
agent_config:
  strict_complexity: true
  challenger_for_medium: true
  calibration:
    no_tool: 0.70
    tool_error: 0.80
    no_evidence_url: 0.85
  # ★ 方向9新增：DAG 覆盖
  dag_overrides:
    nodes:
      - name: pre_verify
        class: agent.workflow.nodes.PubMedPreVerifyNode
        description: 医学声明 PubMed 预检
    edges:
      - from: plan
        to: pre_verify
      - from: pre_verify
        to: route
```

**融合规则**：
1. `RouteNode` 在 `ctx` 中注入 `domain_agent`
2. `DebateNode` 通过 `domain_agent.build_system_prompt()` 等委托领域行为
3. 领域可通过 `dag_overrides` 插入本领域专属预处理节点
4. 引擎加载时合并且校验：默认 DAG ∪ 领域覆盖 → 最终 DAG

---

## 三、涉及文件清单

### 3.1 新增文件

| 文件 | 行数估算 | 职责 |
|------|---------|------|
| `agent/workflow/__init__.py` | ~15 | 导出 WorkflowEngine, WorkflowNode, WorkflowContext, NodeOutput |
| `agent/workflow/engine.py` | ~250 | WorkflowEngine 核心：加载、校验、拓扑排序、执行调度 |
| `agent/workflow/node.py` | ~80 | WorkflowNode ABC + FunctionalNode + NodeOutput + WorkflowContext |
| `agent/workflow/config.py` | ~120 | DAG 配置加载/校验/合并（YAML parser + schema 校验） |
| `agent/workflow/default_dag.yaml` | ~60 | 默认 DAG 配置（复现当前行为） |
| `agent/workflow/nodes/__init__.py` | ~10 | 节点模块导出 |
| `agent/workflow/nodes/scan.py` | ~80 | ScanNode：包装 `scan_article()` |
| `agent/workflow/nodes/plan.py` | ~80 | PlanNode：包装 `build_plan()` |
| `agent/workflow/nodes/context.py` | ~60 | CrossReferenceNode：包装 `prepare_cross_reference_context()` |
| `agent/workflow/nodes/route.py` | ~80 | RouteNode：包装 `route_skill()` + `get_domain_agent()` |
| `agent/workflow/nodes/debate.py` | ~180 | DebateNode：包装 `_debate_claim()` + 并发控制 |
| `agent/workflow/nodes/summary.py` | ~80 | SummaryNode + EmptySummaryNode |
| `agent/workflow/nodes/domain_pre.py` | ~60 | DomainPreNode：领域专属预处理基类（如 PubMedPreVerify、WorldBankPreFetch 等） |
| **合计** | **~1155** | |

### 3.2 修改文件

| 文件 | 改动幅度 | 改动内容 |
|------|---------|---------|
| `agent/agent.py` | **重度简化** | `run()` 从 ~230 行缩到 ~30 行，只保留 ctx 构建 + 引擎调用 |
| `agent/__init__.py` | 微调 | 导出 `WorkflowEngine` |
| `agent/skills/base.py` | 微调 | `agent_config` 中 `dag_overrides` 字段的 schema 校验（仅文档+类型提示） |
| `tests/test_agent.py` | 中调 | 新增 DAG 层单元测试（配置校验、拓扑排序、节点跳过），现有测试不改 |

### 3.3 不改的文件

| 文件 | 原因 |
|------|------|
| `agent/models.py` | SSE 事件类型不变 |
| `agent/planner.py` | `build_plan()` 纯函数，直接包装 |
| `agent/scanner.py` | `scan_article()` 纯函数，直接包装 |
| `agent/debate.py` | 各阶段 prompt builder/parser 不变 |
| `agent/agents/*` | DomainAgent 体系不变 |
| `agent/skills/router.py` | `route_skill()` 纯函数，直接包装 |
| `agent/tools/*` | 工具体系不变 |
| `agent/llm/*` | LLM 客户端不变 |
| `backend/*` | FastAPI 接口和数据库不变 |

---

## 四、与方向1/方向5的融合设计

### 4.1 与方向1（VerificationStrategy）融合

- `PlanNode` 调用 `build_plan()` → `claim.complexity` 被填充
- `DebateNode` 在 `ctx` 中读取 `plan.claims`，每个 claim 自带 `complexity`
- `DebateNode` 内部调用 `domain_agent.merge_strategy(claim.complexity)` → 得到最终策略
- **零侵入**：`VerificationStrategy` 和 `_STRATEGY_MAP` 完全不变，只是调用方从 `Agent._debate_claim()` 变为 `DebateNode.execute()`

### 4.2 与方向5（DomainAgent）融合

- `RouteNode.execute()` 调用 `route_skill()` + `get_domain_agent()`，将 `domain_agent` 写入 `ctx`
- `DebateNode.execute()` 从 `ctx` 读取 `domain_agent`，委托 prompt 构建
- 领域预处理节点通过 `dag_overrides` 插入，仅在特定领域的 DAG 中出现
- **关键不变**：`DomainAgent` 的 7 个可覆盖方法全部不变，只是调用方从 `Agent` 变为节点

### 4.3 正交性保证

```
方向1 (策略)  ──→  PlanNode (填充 complexity) ──→  DebateNode (读 strategy)
方向5 (领域)  ──→  RouteNode (实例化 DomainAgent) ──→  DebateNode (委托 prompt)
方向9 (DAG)   ──→  WorkflowEngine (编排节点执行顺序)
```

三者在不同层次操作，互不耦合：
- 方向1 操作数据层（Claim.complexity）
- 方向5 操作行为层（DomainAgent 多态）
- 方向9 操作编排层（节点执行图）

---

## 五、条件表达式语法

`conditional_edges` 的 `condition` 和 `skip_if` 是受限 Python 表达式，求值环境为 `ctx` 命名空间：

```python
# 内置安全函数（白名单）
SAFE_BUILTINS = {
    "len": len, "isinstance": isinstance, "str": str,
    "int": int, "float": float, "bool": bool,
    "list": list, "dict": dict, "any": any, "all": all,
    "True": True, "False": False, "None": None,
}

def evaluate_condition(expr: str, ctx: WorkflowContext) -> bool:
    """在受限命名空间中求值条件表达式。"""
    namespace = {**SAFE_BUILTINS, "ctx": ctx}
    try:
        result = eval(expr, {"__builtins__": {}}, namespace)
        return bool(result)
    except Exception:
        logger.warning("条件表达式求值失败：%s", expr)
        return False  # 安全保守：求值失败 = 不跳过
```

**示例条件**：
```yaml
skip_if: "len(ctx.get('plan', {}).get('claims', [])) == 0"
skip_if: "ctx.get('plan') and len(ctx['plan'].claims) < 3"
skip_if: "ctx.get('domain_agent') and ctx['domain_agent'].skill.name != 'medical'"
condition: "len(ctx.get('claims', [])) >= 5"
```

---

## 六、风险点与缓解措施

### 风险1：并发语义变化

**风险**：当前 `run()` 中 claims 通过 `asyncio.Queue + Semaphore(3)` 并发处理，事件逐条 yield。DAG 引擎若要保持此行为，DebateNode 内部的并发逻辑不可变。

**缓解**：`DebateNode` 的内部实现 = 当前 `Agent.run()` 第 266–354 行（_process_one_claim + Queue + Semaphore），一字不差地迁移到节点内。引擎只负责"在什么时机调用 DebateNode"，并发语义完全由 DebateNode 内部决定。

### 风险2：条件表达式安全性

**风险**：`eval()` 执行 YAML 中的表达式可能引入安全风险。

**缓解**：
1. 白名单 `SAFE_BUILTINS`，禁用所有危险内置函数（`__import__`、`open`、`exec` 等）
2. 配置校验阶段对条件表达式做静态检查（只允许已知安全模式）
3. 异常时默认 `False`（不跳过），失败安全侧

### 风险3：节点依赖 LLM 客户端

**风险**：节点需要 `BaseLLMClient` 引用，但节点不应持有 Agent 引用。

**缓解**：通过 `ctx` 注入（`ctx["_llm"]`、`ctx["_router_llm"]`），这是引擎与节点之间约定的依赖注入通道。节点在其 `execute()` 中从 `ctx` 获取。

### 风险4：DAG 配置复杂化

**风险**：YAML 配置增加理解成本，团队维护负担。

**缓解**：
1. 提供 `default_dag.yaml` 作为唯一真实行为源
2. 领域 DAG 覆盖是可选的，不覆盖 = 使用默认
3. 引擎提供 `validate` CLI 命令校验配置合法性
4. 配置变更走 PR review

### 风险5：测试覆盖

**风险**：重构过程中可能引入回归。

**缓解**：
1. 先写 `test_workflow_engine.py` 覆盖 DAG 加载、校验、拓扑排序、节点跳过
2. 分阶段替换 `Agent.run()`：
   - 阶段 A：新建 `workflow/` 包 + 节点类 + 引擎，不改 `Agent.run()`
   - 阶段 B：写集成测试验证 `engine.run()` == `Agent.run()` 输出
   - 阶段 C：`Agent.run()` 内切换到引擎，89 测试全绿后合并
3. 每个阶段可独立回滚

### 风险6：扇入扇出复杂性

**风险**：DAG 的扇出（一条边 → 多个下游）和扇入（多条边 → 一个节点）增加引擎复杂度。

**缓解**：
- **第一期只实现线性 DAG**（无扇出扇入），完全覆盖当前行为
- 扇出/扇入作为第二期，通过 `edges` 中多条同 `from` 边和 `await_all_upstream` 标志实现
- 默认配置只用线性拓扑

---

## 七、实施步骤（建议分 5 步，每步可独立 PR）

### Step 1：基础设施（3 文件，~350 行）
- 新建 `agent/workflow/node.py`：`WorkflowNode` ABC + `NodeOutput` + `WorkflowContext` + `FunctionalNode`
- 新建 `agent/workflow/config.py`：YAML 加载 + DAG schema 校验 + 节点类解析
- 新建 `agent/workflow/__init__.py`
- **目的**：建立协议和配置格式，无行为变更

### Step 2：引擎核心（1 文件，~250 行）
- 新建 `agent/workflow/engine.py`：`WorkflowEngine` 类（拓扑排序、执行调度、事件汇聚、条件边求值）
- 单元测试 `tests/test_workflow_engine.py`：配置校验、拓扑排序、节点跳过、错误处理
- **目的**：引擎可独立运行，与 Agent 无关

### Step 3：节点实现（6 文件，~560 行）
- 新建 `agent/workflow/default_dag.yaml`
- 新建所有 `agent/workflow/nodes/*.py`
- 每个节点包装现有函数，不引入新逻辑
- `DebateNode` 内部迁移 `Agent._debate_claim()` 的并发 + 事件产出代码
- **目的**：所有节点可独立测试

### Step 4：Agent.run() 切换（1 文件改动，~200 行删除）
- `Agent.run()` 简化为 ctx 构建 + `engine.run(ctx)`
- `_debate_claim()` 等私有方法标记 `@deprecated`（保留以防回滚，下个 release 删除）
- 群跑 89 个已有测试，确认全绿
- **目的**：行为等价切换到引擎

### Step 5：进阶特性（可选，不阻塞发布）
- 领域 DAG 覆盖（`dag_overrides` 合并逻辑）
- 扇出扇入支持
- 领域专属预处理节点（`PubMedPreVerifyNode`、`WorldBankPreFetchNode`）
- DAG 可视化导出（`workflow dag --viz`）
- **目的**：释放 DAG 引擎的全部潜力

---

## 八、文件结构总览（重构后）

```
agent/
├── __init__.py
├── agent.py                 # Agent 类（大幅简化，~50 行 run()）
├── models.py                # 不变
├── planner.py               # 不变
├── scanner.py               # 不变
├── debate.py                # 不变
├── agents/
│   ├── __init__.py          # 不变
│   ├── base.py              # 不变
│   ├── general.py           # 不变
│   ├── medical.py           # 不变
│   ├── finance.py           # 不变
│   ├── technology.py        # 不变
│   ├── news_policy.py       # 不变
│   └── registry.py          # 不变
├── llm/                     # 不变
├── skills/                  # 不变（base.py 微调 agent_config 类型提示）
├── tools/                   # 不变
└── workflow/                # ★ 新增包
    ├── __init__.py
    ├── node.py              # WorkflowNode ABC + NodeOutput + WorkflowContext
    ├── engine.py            # WorkflowEngine
    ├── config.py            # DAG 配置加载/校验/合并
    ├── default_dag.yaml     # 默认 DAG 描述
    └── nodes/
        ├── __init__.py
        ├── scan.py          # ScanNode
        ├── plan.py          # PlanNode
        ├── context.py       # CrossReferenceNode
        ├── route.py         # RouteNode
        ├── debate.py        # DebateNode
        ├── summary.py       # SummaryNode + EmptySummaryNode
        └── domain_pre.py    # 领域预处理节点基类
```

---

## 九、关键设计决策记录

| 决策 | 选择 | 理由 |
|------|------|------|
| 配置格式 | YAML | 已有 yaml 依赖（skills 用），人工可读，比 JSON 更适合团队协作 |
| 节点协议 | ABC（非 Protocol） | 需要 `can_skip()` 默认实现，Protocol 不适合 |
| 条件表达式 | `eval()` + 白名单 | 实现简单，受限命名空间消除安全风险 |
| 节点并发 | 节点内部控制 | 引擎只管顺序，并发语义由各节点自行决定 |
| 领域覆盖 | YAML 合并而非代码 | 保持声明式风格，覆盖逻辑集中在 config.py |
| 向后兼容 | `default_dag.yaml` = 当前行为 | 默认不改行为，可选启用领域覆盖 |
| 包位置 | `agent/workflow/` | 与现有 `agent/agents/`、`agent/skills/` 平行，符合项目惯例 |

---

## 十、附录：与当前 `run()` 的等价映射

| `Agent.run()` 代码段 | 对应节点 | ctx 键变更 |
|---|---|---|
| L152–174: scan → yield StatusEvent → claims | `ScanNode` | `ctx["claims"]` ← 返回值 |
| L175–188: build_plan + 复杂度分布日志 | `PlanNode` | `ctx["plan"]` ← 返回值 |
| L191–197: prepare_cross_reference_context | `CrossReferenceNode` | `ctx["context_ready"]` ← True/异常 |
| L199–211: yield PlanEvent | `PlanNode`（输出 events） | - |
| L213–251: route_skill + 工具禁用 + overlay | `RouteNode` | `ctx["skill"]`, `ctx["effective_skill"]`, `ctx["domain_agent"]`, `ctx["removed_tools"]` |
| L263–278: _process_one_claim + Semaphore + Queue | `DebateNode`（内部） | `ctx["claim_records"]`, `ctx["annotation_count"]` |
| L356–377: build_summary + yield SummaryEvent + DoneEvent | `SummaryNode` | `ctx["summary_event"]`, `ctx["done_event"]` |
