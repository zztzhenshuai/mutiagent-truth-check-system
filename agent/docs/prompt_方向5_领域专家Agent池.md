# 方向5 重构计划：领域专家 Agent 池

> 撰文：Claude（基于对仓库的完整通读）
> 日期：2026-06-13
> 状态：待评审

---

## 一、现状总结

### 1.1 当前架构（读完代码后的理解）

```
Agent.run()
  ├── scan_article()          → 提取声明列表
  ├── build_plan()            → 计算 suspicion_score + 复杂度分类（方向1）
  ├── route_skill()           → GLM 根据文章开头选 1 个领域 skill
  ├── prepare_cross_reference → 预热句向量模型
  └── 并行 _process_one_claim():
        ├── strategy = _STRATEGY_MAP[claim.complexity]   （方向1）
        └── Agent._debate_claim(claim, skill, overlays, strategy):
              ├── _react_loop()           → ReAct 核查（调用 _build_system_prompt）
              ├── _run_challenger()       → 调用 build_challenger_prompt()  from debate.py
              ├── _run_judge()            → 调用 build_judge_prompt()       from debate.py
              ├── _run_reflexion()        → 调用 build_reflexion_prompt()   from debate.py
              └── _calibrate_annotation() → 置信度客观修正
```

**领域差异目前仅体现在两处：**
1. `route_skill()` 选出的 `skill.allowed_tools` 限制了工具白名单
2. `_build_system_prompt()` 把 `skill.prompt` 作为一段 markdown 后缀拼接到统一的 Verifier system prompt 中

### 1.2 方向5核心痛点

| 问题 | 现状 | 影响 |
|------|------|------|
| Verifier 人格单一 | 所有领域共用 `_build_system_prompt` 的"你是 Verifier Agent..."前缀 | 医学和金融声明的核查者"思维模式"完全相同 |
| 辩论 prompt 无领域区分 | Challenger/Judge 的 prompt 模板在 `debate.py` 中是全局函数，仅注入 `skill_name` 字符串 | 医学 Challenger 不会问"RCT 证据等级？"，金融 Challenger 不会问"数据时效性？" |
| 策略映射全局固定 | `_STRATEGY_MAP` 是模块级常量，所有领域用同一套 simple/medium/complex 参数 | 无法让"医学对所有声明默认更严格" |
| 技能定义仅是 prompt 片段 | 5 个 `.md` 文件的正文只是核查要点列表，没有 persona 定义 | 无法表达"我是一名循证医学专家" vs "我是一名财经数据分析师" |
| 扩展需改核心代码 | 新增领域 = 新建 `.md` 文件（目前 OK），但要定制推理策略仍需改 `agent.py` | 不满足"每个领域一个 Agent 子类或工厂函数"的目标 |

### 1.3 方向1已完成的基础（方向5可复用）

- `ComplexityLevel = Literal["simple", "medium", "complex"]` 已定义
- `VerificationStrategy` dataclass 包含完整的策略参数
- `_STRATEGY_MAP: dict[ComplexityLevel, VerificationStrategy]` 全局常量
- `classify_complexity()` 规则分类器已工作
- `_debate_claim()` 已支持策略分流

---

## 二、目标架构设计

### 2.1 核心思路

**不改变** `Agent.run()` 的管线流程（scan→plan→route→debate），**不移动** `_react_loop()` 等共享基础设施。而是在"领域差异点"上引入多态：

```
Agent.run() [协调器，基本不变]
  │
  │ route_skill() → skill_name
  │
  │ domain_agent = AGENT_REGISTRY[skill_name](skill, self._llm)
  │                                        ↑ 工厂/子类，按领域实例化
  │
  └── _debate_claim(claim, domain_agent, ...)
        │
        ├── 系统 prompt：domain_agent.build_system_prompt(overlays, ...)
        │                   ↑ 每个领域完整的 persona + 知识 + 核查标准
        │
        ├── 策略映射：domain_agent.merge_strategy(claim.complexity, _STRATEGY_MAP)
        │                   ↑ 允许领域微调（如 medical 对 medium 也启用 challenger）
        │
        ├── Challenger prompt：domain_agent.build_challenger_prompt(...)
        │                   ↑ 医学质疑"证据等级"，金融质疑"数据时效"
        │
        ├── Judge prompt：domain_agent.build_judge_prompt(...)
        │                   ↑ 医学判决考虑 GRADE，金融考虑时效性
        │
        └── Reflexion prompt：domain_agent.build_reflexion_prompt(...)
                            ↑ 领域特定的反思审查角度
```

### 2.2 关键设计决策

| 决策 | 结论 | 理由 |
|------|------|------|
| 继承 vs 组合 | **组合** — `DomainAgent` 是持有 `skill` + `llm` 的独立类，不继承 `Agent` | 避免移动 `_react_loop`（500行），避免深层继承链，测试更简单 |
| `_debate_claim` 放哪 | **留在 `Agent`**，接收 `DomainAgent` 参数 | 辩论流程（V→C→J 分支）是通用逻辑，只有 prompt 和策略偏好是领域差异 |
| `_react_loop` 放哪 | **留在 `Agent`**，不变 | 纯技术机制（ReAct 格式解析、工具调用、观察注入），与领域无关 |
| prompt builder 放哪 | **移到 `DomainAgent` 方法**，`debate.py` 保留作为默认实现 | 每个领域可覆盖，未覆盖则回退默认 |
| skill 文件格式 | **扩展 frontmatter**，增加可选字段，`general` 不新增字段即可保证向后兼容 | 最小破坏，`general` 的行为完全不变 |
| 与方向1融合 | `DomainAgent` 提供 `merge_strategy()` → 对 `_STRATEGY_MAP` 做领域微调 | 不破坏方向1的架构，在 `_STRATEGY_MAP` 外层加领域适配 |

---

## 三、文件级改动清单

### 3.1 新建文件

```
agent/agents/
├── __init__.py          # 导出 AGENT_REGISTRY + get_domain_agent()
├── base.py              # DomainAgent 基类（含所有默认实现，行为=GeneralAgent）
├── general.py           # GeneralAgent(DomainAgent) — 完全向后兼容，纯继承
├── medical.py           # MedicalAgent(DomainAgent) — 覆盖系统prompt/挑战prompt/策略
├── finance.py           # FinanceAgent(DomainAgent)
├── technology.py        # TechAgent(DomainAgent)
├── news_policy.py       # NewsAgent(DomainAgent)
└── registry.py          # AGENT_REGISTRY 字典 + get_domain_agent() 工厂函数
```

#### `agent/agents/base.py` — DomainAgent 基类

```python
class DomainAgent:
    """领域专家 Agent 基类。
    
    封装：系统 prompt 构建、Challenger/Judge/Reflexion prompt 构建、
          策略映射微调、置信度校准偏好。
    
    默认行为 = 当前的 GeneralAgent 行为（完全向后兼容）。
    子类覆盖部分方法即可实现领域特化。
    """
    
    def __init__(self, skill: Skill, llm: BaseLLMClient):
        self.skill = skill
        self._llm = llm
    
    # ── 属性代理 ──
    @property
    def name(self) -> str: ...
    @property
    def allowed_tools(self) -> tuple[str, ...]: ...
    
    # ── 系统 prompt（核心覆盖点）──
    def build_system_prompt(self, overlays, disabled_note_tools) -> str:
        """构建完整 Verifier 系统 prompt（人格 + 工具 + 核查标准）。
        默认行为 = 当前 agent.py 的 _build_system_prompt()。
        """
    
    # ── 辩论阶段 prompt（可选覆盖）──
    def build_challenger_prompt(self, claim_text, verifier_record) -> str: ...
    def build_judge_prompt(self, claim_text, verifier, challenger, rebuttal) -> str: ...
    def build_reflexion_prompt(self, claim_text, error_type, confidence, reasoning, tool_summaries, errors) -> str: ...
    
    # ── 策略融合（与方向1对接）──
    def merge_strategy(self, claim_complexity: ComplexityLevel) -> VerificationStrategy:
        """基于领域偏好 + 声明复杂度，返回最终策略。
        默认：直接返回 _STRATEGY_MAP[claim_complexity]。
        子类可覆盖以调整阈值（如医学提高所有 complexity 的严格度）。
        """
    
    # ── 置信度校准偏好（可选覆盖）──
    def get_calibration_multipliers(self) -> dict[str, float]:
        """返回领域特定的校准系数修正。
        例如 finance 对 '无证据URL' 惩罚更重（时效性要求高）。
        默认返回 {}（使用 agent.py 硬编码的系数）。
        """
```

**设计要点：**
- `build_system_prompt()` 是**核心覆盖点**——从当前的"通用前缀 + skill.prompt 后缀"改为"完整的领域 persona prompt"
- 所有方法都有**默认实现**（= 当前 `agent.py` / `debate.py` 的行为），保证不覆盖=不回退
- `merge_strategy()` 是关键扩展点：让医学对所有声明默认更严格
- 不持有 `Agent` 引用，完全解耦

#### `agent/agents/general.py` — 兜底 Agent

```python
class GeneralAgent(DomainAgent):
    """完全继承 DomainAgent 默认实现，行为与当前通用 Agent 一致。"""
    pass
```

#### `agent/agents/medical.py` — 医学专家（示例）

覆盖内容：
1. **系统 prompt**：以"循证医学专家"为 persona，内置 GRADE 证据等级框架、RCT 评估标准、不良反应监测知识
2. **Challenger prompt**：质疑重点——证据等级是否足够？是否混淆相关与因果？样本量是否充分？
3. **Judge prompt**：判决依据——优先采纳 RCT/系统综述，个案报告降低采纳权重
4. **策略融合**：对 medical 领域的 `medium` 也启用 Challenger（医学声明默认为高风险）
5. **校准系数**：对"无工具调用"惩罚更重（医学声明必须查证）

#### `agent/agents/registry.py` — 注册表

```python
AGENT_REGISTRY: dict[str, type[DomainAgent]] = {
    "general": GeneralAgent,
    "medical": MedicalAgent,
    "finance": FinanceAgent,
    "technology": TechAgent,
    "news_policy": NewsAgent,
}

def get_domain_agent(skill_name: str, skill: Skill, llm: BaseLLMClient) -> DomainAgent:
    """工厂函数：根据领域名实例化对应专家 Agent。
    未注册的领域 → 回退 GeneralAgent。
    """
    cls = AGENT_REGISTRY.get(skill_name, GeneralAgent)
    return cls(skill, llm)
```

**扩展性保证**：新增领域只需 (1) 新建 `DomainAgent` 子类，(2) 在 `AGENT_REGISTRY` 加一行。不改 `agent.py`。

---

### 3.2 修改文件

#### (A) `agent/agent.py` — 协调器化

**改动量**：约 60~80 行修改（主要是参数传递 + 委托调用），核心逻辑不变

| 区域 | 改动 | 说明 |
|------|------|------|
| `__init__()` | 新增 `self._domain_agent_cache: dict[str, DomainAgent] = {}` | 惰性缓存，同一领域 Agent 只实例化一次 |
| Import | 新增 `from .agents import get_domain_agent` | |
| `run()` 第 204 行后 | 新增：`domain_agent = get_domain_agent(skill.name, effective_skill, self._llm)` | 路由后立即确定领域 Agent |
| `_process_one_claim()` | 新增 `domain_agent` 闭包参数，传给 `_debate_claim` | |
| `_debate_claim()` 签名 | 新增参数 `domain_agent: DomainAgent` | |
| `_debate_claim()` 第 517-528 行 | `self._react_loop()` 的 system prompt 来源：从 `self._build_system_prompt()` 改为 **通过 `domain_agent.build_system_prompt()` 生成并传递给 `_react_loop`** | 这是最大的单点改动 |
| `_debate_claim()` 第 648-696 行 | medium/complex 的 `strategy` 获取：从 `_STRATEGY_MAP[claim.complexity]` 改为 **`domain_agent.merge_strategy(claim.complexity)`** | |
| `_run_challenger()` | prompt 来源：从 `build_challenger_prompt()` 改为 **`domain_agent.build_challenger_prompt()`** | |
| `_run_judge()` | prompt 来源：从 `build_judge_prompt()` 改为 **`domain_agent.build_judge_prompt()`** | |
| `_run_reflexion()` | prompt 来源：从 `build_reflexion_prompt()` 改为 **`domain_agent.build_reflexion_prompt()`** | |
| `_calibrate_annotation()` | 系数：从硬编码改为 **合并 `domain_agent.get_calibration_multipliers()`** | |
| `_build_system_prompt()` | **保留但不删除**：设为 deprecated，转发到 `GeneralAgent().build_system_prompt()`，供 `_react_loop` 的向后兼容路径使用 | 避免删除导致测试大面积失效 |

**关键改动细节：`_react_loop` 如何获取 domain-aware system prompt**

当前 `_react_loop` 内部调用 `self._build_system_prompt(skill, overlays, disabled_note_tools)` 构建 messages[0]。
修改方案：**`_react_loop` 新增可选参数 `system_prompt: str | None = None`**，当传入时直接使用，否则回退调用 `self._build_system_prompt()`。

```python
async def _react_loop(self, claim, skill, overlays=None, ..., system_prompt=None):
    ...
    messages = [
        {"role": "system", "content": system_prompt or self._build_system_prompt(skill, overlays, disabled_note_tools)},
        {"role": "user", "content": user_prompt},
    ]
```

`_debate_claim` 调用时：
```python
system_prompt = domain_agent.build_system_prompt(overlays, disabled_note_tools)
async for event in self._react_loop(claim, skill, overlays, ..., system_prompt=system_prompt):
```

#### (B) `agent/debate.py` — prompt builder 转为可覆盖的默认实现

**改动量**：约 30 行

- `build_challenger_prompt()`、`build_judge_prompt()`、`build_reflexion_prompt()` 保持为**模块级函数**（作为默认实现）
- `DomainAgent` 基类的对应方法**默认调用这些模块级函数**
- 子类覆盖时直接返回自定义 prompt，不调用模块级函数
- `_REFLEXION_PROMPT` 常量保持（基类默认使用）

#### (C) `agent/skills/base.py` — Skill 数据类微调

**改动量**：约 15 行

- `Skill` dataclass 新增 2 个可选字段：
  ```python
  @dataclass(frozen=True)
  class Skill:
      name: str
      description: str
      prompt: str                    # 现有：正文（领域核查指令）
      allowed_tools: tuple[str, ...]
      kind: str = KIND_DOMAIN
      persona: str = ""              # 新增：Agent 角色身份描述（如"循证医学专家"）
      agent_config: dict = field(default_factory=dict)  # 新增：领域策略偏好（可选）
  ```
- `_parse_skill_file()` 解析新的 frontmatter 字段（可选，缺省为空）

#### (D) `agent/skills/defs/*.md` — 技能定义升级

**改动量**：每个文件新增 2~3 个 frontmatter 字段 + 正文重构

以 `medical.md` 为例：

```markdown
---
name: medical
description: 医疗健康领域。触发条件：...
allowed_tools: [pubmed_scientific_search, consumer_health_verifier, ...]
persona: |
  你是一名循证医学（Evidence-Based Medicine）事实核查专家。你精通临床流行病学、
  生物统计学和 GRADE 证据质量分级体系。你的核查原则是：
  - 优先采纳系统综述和 RCT 的证据
  - 对观察性研究（队列/病例对照）持审慎态度，标注证据等级
  - 识别"相关不等于因果"的常见认知谬误
  - 对涉及"替代药物治疗""根治"等危险宣称保持零容忍
agent_config:
  strict_complexity: true          # 所有声明默认至少 medium 严格度
  challenger_for_medium: true      # medium 声明也启用 Challenger
  calibration:
    no_tool_penalty: 0.75          # 无工具调用时惩罚更重（默认 0.80）
---
（正文保持现有核查指令，作为领域知识的第二部分）
```

**兼容性保证**：`general.md` **不改动**（无 `persona`、无 `agent_config`），`GeneralAgent` 从 `skill.prompt` 自动生成 system prompt，行为与当前完全一致。

#### (E) `agent/models.py` — 微调

**改动量**：约 5 行

- `VerificationStrategy` 新增可选字段 `label_override: str | None = None`（允许领域给策略起别名，如医学叫"审查"而非"验证"）

#### (F) `agent/__init__.py` — 导出更新

**改动量**：约 3 行

```python
from .agents import get_domain_agent, DomainAgent
__all__ = ["Agent", "AgentState", "create_chat_llm", "get_domain_agent", "DomainAgent"]
```

#### (G) `backend/main.py` — 不变

`Agent` 的构造方式不变（`Agent(complex_llm=..., router_llm=..., chat_llm=...)`）。
`Agent.run()` 的签名不变。
`GET /skills` 可选择性返回 `persona` 字段（前端不做任何处理）。

#### (H) `tests/test_agent.py` — 测试更新

**改动量**：约 20~30 行

- 现有测试用 `MockLLMClient` + 默认 `general` 路由 → 行为不变，**应全部通过**
- 新增 2~3 个测试：
  1. `test_domain_agent_selection` — 验证路由 `medical` 时获得 `MedicalAgent` 实例
  2. `test_general_agent_fallback` — 验证未知领域回退 `GeneralAgent`
  3. `test_domain_strategy_merge` — 验证 `merge_strategy()` 对策略参数的调整

---

## 四、数据流变化（图文对比）

### 4.1 当前数据流

```
article_text
  │
  ├─→ route_skill() → skill(Skill)  ─┐
  │                                   │
  └─→ build_plan() → claims           │
                                      │
  for each claim:                     │
    strategy = _STRATEGY_MAP[complexity]  │
    _debate_claim(claim, skill, overlays, strategy)
      ├─ _build_system_prompt(skill, overlays)  ← 只用 skill.prompt 做后缀
      ├─ build_challenger_prompt(claim, skill)  ← 全局函数，只注入 skill_name
      └─ build_judge_prompt(...)                ← 全局函数
```

### 4.2 方向5数据流

```
article_text
  │
  ├─→ route_skill() → skill(Skill)
  │       │
  │       └─→ get_domain_agent(skill.name) → domain_agent(DomainAgent)
  │                                              │
  └─→ build_plan() → claims                     │
                                                 │
  for each claim:                                │
    strategy = domain_agent.merge_strategy(       │
        claim.complexity, _STRATEGY_MAP           │
    )                                             │
    _debate_claim(claim, domain_agent, strategy)  │
      ├─ domain_agent.build_system_prompt(...)    │ ← 完整 persona + 知识
      ├─ domain_agent.build_challenger_prompt()   │ ← 领域特化的质疑角度
      └─ domain_agent.build_judge_prompt()        │ ← 领域特化的判决标准
```

### 4.3 SSE 事件流 — 不変

方向5**不在 SSE 事件结构上做任何改动**。`DebateEvent.details` 中可选择性增加 `domain_agent` 字段（如 `"domain_agent": "MedicalAgent"`），但这是纯增量、前向兼容的。

---

## 五、与方向1的融合方案

两者的融合点是 `merge_strategy()`：

```python
# agent/agents/base.py
def merge_strategy(self, claim_complexity: ComplexityLevel) -> VerificationStrategy:
    """默认：原样返回 _STRATEGY_MAP。子类可覆盖。"""
    from agent.agent import _STRATEGY_MAP
    return _STRATEGY_MAP[claim_complexity]

# agent/agents/medical.py
def merge_strategy(self, claim_complexity: ComplexityLevel) -> VerificationStrategy:
    from agent.agent import _STRATEGY_MAP
    base = _STRATEGY_MAP[claim_complexity]
    # 医学领域：medium 也启用 Challenger（涉及健康需双重检查）
    if claim_complexity == "medium":
        return replace(base, enable_challenger=True, max_react_steps=4)
    # complex：提高门槛
    if claim_complexity == "complex":
        return replace(base, high_confidence_threshold=0.92)
    return base
```

融合效果表（以 `medical` 为例）：

| Complexity | 原策略 | MedicalAgent 覆盖后 |
|------------|--------|---------------------|
| simple | 1步无工具无辩论 | **不变**（纯数字如"布洛芬每片200mg"，快速核验仍适用） |
| medium | 3步无Challenger | **4步+启用Challenger**（涉及人体健康，双重检查） |
| complex | 6步完整V→C→J+Reflexion | 6步完整V→C→J+Reflexion，**置信度阈值 0.92**（更严格） |

---

## 六、风险点与缓解措施

| # | 风险 | 影响范围 | 概率 | 缓解措施 |
|---|------|----------|------|----------|
| 1 | **`_react_loop` 的 system prompt 传递方式变更引入 bug** | `_debate_claim` 和 `_react_loop` | 中 | (a) `_react_loop` 新增 `system_prompt` 可选参数，未传时回退 `self._build_system_prompt()`，保证向后兼容 (b) 日志输出完整 system prompt 前 200 字符用于调试 |
| 2 | **`build_challenger_prompt` / `build_judge_prompt` 从模块级函数变为方法调用，签名不兼容** | 调用方 `_run_challenger` / `_run_judge` | 低 | (a) 方法签名保持与当前模块级函数一致 (b) 基类默认实现直接调用模块级函数 (c) 在 PR 中对比修改前后的 prompt 文本 |
| 3 | **新增 `persona` / `agent_config` 字段破坏 Skill 解析** | `load_skills()` | 低 | (a) 全用可选字段，默认空 (b) `_parse_skill_file` 用 `.get()` 取值 (c) `general.md` 不新增任何字段，作为回归测试基准 |
| 4 | **`AGENT_REGISTRY` 中忘记注册新领域** | 新领域添加时 | 低 | (a) `get_domain_agent()` 对未知名称回退 `GeneralAgent` (b) logger.warning 记录回退事件 (c) 单元测试覆盖未注册回退路径 |
| 5 | **现有 83 个测试失败** | 测试套件 | 中 | (a) `GeneralAgent` 行为与当前完全一致，默认路由选 `general` 的测试应全部通过 (b) `MockLLMClient` 不涉及 DomainAgent，mock 路径不变 (c) 先跑全量测试建立基线，逐步重构逐步验证 |
| 6 | **DomainAgent 实例化开销** | 每次 `run()` 调用 | 极低 | DomainAgent 是轻量级 dataclass-like 对象（只持有 skill + llm 引用），不加载外部资源。首次实例化 < 1ms |
| 7 | **领域 prompt 质量不可控** | 核查准确率 | 中 | (a) 新 persona prompt 基于当前 skill 正文改写，保留已验证的核查标准 (b) 每个领域的 prompt 先用评估集跑一遍，对比准确率变化 (c) GeneralAgent 不受影响，可随时回退 |

---

## 七、实施步骤（建议顺序）

### Phase 1：基础设施搭建（不改行为）
1. 新建 `agent/agents/` 包 + `base.py`（DomainAgent 基类，默认行为=当前）
2. 新建 `agent/agents/general.py`（纯继承，无覆盖）
3. 新建 `agent/agents/registry.py`（注册表 + 工厂函数）
4. 修改 `agent/__init__.py` 导出新符号
5. **跑全量测试 → 必须 83/83 通过**（此时所有路径仍走 GeneralAgent）

### Phase 2：Agent.py 协调器化
6. `_react_loop` 新增 `system_prompt` 可选参数
7. `Agent.__init__` 新增 `_domain_agent_cache`
8. `Agent.run()` 新增 `domain_agent = get_domain_agent(...)` 
9. `_debate_claim` 新增 `domain_agent` 参数，委托 prompt 构建
10. `_run_challenger` / `_run_judge` / `_run_reflexion` 改为使用 `domain_agent.build_*_prompt()`
11. `_calibrate_annotation` 合并 `domain_agent.get_calibration_multipliers()`
12. **跑全量测试 → 必须 83/83 通过**（GeneralAgent 的默认实现 = 当前行为）

### Phase 3：领域 Agent 实现
13. 实现 `MedicalAgent`（覆盖 5 个方法）
14. 实现 `FinanceAgent`
15. 实现 `TechAgent`
16. 实现 `NewsAgent`
17. 升级 `medical.md` / `finance.md` / `technology.md` / `news_policy.md` frontmatter
18. 新增领域 Agent 单元测试（3~4 个）
19. **跑全量测试 + 手动端到端验证**（用医疗/金融/科技/新闻文章各一篇）

### Phase 4：技能定义升级
20. 为每个领域编写完整 persona prompt（先在 markdown 里写，不涉及代码）
21. `Skill` dataclass 新增 `persona` / `agent_config` 字段
22. `_parse_skill_file` 解析新字段
23. `DomainAgent.build_system_prompt()` 使用 `skill.persona`（如有）构造系统 prompt
24. **跑全量测试 → 必须通过**（`general.md` 无新字段，不减损）

---

## 八、总结

| 维度 | 当前 | 方向5目标 | 本计划的方案 |
|------|------|-----------|-------------|
| Agent 数量 | 1 个 `Agent` 实例 | 5 个领域专家 | 1 个协调器 `Agent` + 5 个轻量 `DomainAgent`（组合模式） |
| 系统 prompt | 通用模板 + 领域后缀 | 完整 persona | `DomainAgent.build_system_prompt()` 多态 |
| 辩论 prompt | 全局函数 | 领域特化 | `DomainAgent` 方法覆盖全局函数 |
| 策略映射 | 全局常量 | 领域可调 | `DomainAgent.merge_strategy()` 微调 `_STRATEGY_MAP` |
| 技能定义 | markdown prompt 片段 | 完整 Agent 定义 | frontmatter 新增 `persona` + `agent_config`，正文保留 |
| 核心代码改动量 | - | - | `agent.py` ~80行 + 新建 `agents/` ~400行 + `debate.py` ~30行 |
| 测试风险 | - | - | 低（GeneralAgent = 当前行为，默认路由选 general） |
| 向后兼容 | - | - | `general.md` 不修改，`GeneralAgent` 纯继承无覆盖 |
| SSE 事件 | 不变 | 不变 | ✅ |
| DB Schema | 不变 | 不变 | ✅ |
| Backend API | 不变 | 不变 | ✅ |
