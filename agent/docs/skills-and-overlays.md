# 领域 Skill 与用户自定义附加视角（Overlay）

> 维护人：成员 A
> 最后更新：2026-06-07
> 供 B（后端）、C（浏览器插件）对接使用

本文件描述事实核查 Agent 的「领域 skill」机制，以及供浏览器插件实现「用户自定义」功能的请求契约。

---

## 1. 两种角色：domain vs overlay

skill 按**角色**分两类，区别在"如何生效"，而非"谁创建的"：

| 角色 | 语义 | 生效方式 | 来源 |
|------|------|----------|------|
| **domain（领域档案）** | 这篇文章属于哪个领域、按什么专业标准/证据源核查 | **互斥**，路由只选 **1 个** | 内置（`agent/skills/defs/*.md`） |
| **overlay（附加视角）** | 不论什么领域都额外叮嘱 Agent 注意的关注点 | **叠加**，可启用 **0~N 个**，全部生效 | 用户自建（浏览器插件传入） |

一次分析时，Agent 实际使用的指令这样合成：

```
system prompt =
    固定核查前缀（写死，不可被覆盖）
  + 路由选中的 1 个 domain.prompt          ← 互斥层
  + 启用的 overlay_1.prompt
  + 启用的 overlay_2.prompt …              ← 叠加层（0~N）
  + 「已禁用工具」覆盖说明（若用户禁用了本领域的工具）

工具白名单 = 选中的 domain.allowed_tools − 用户禁用的工具集合   ← 只做减法
```

### 三条安全不变量（已被单元测试锁定）

1. **overlay 不扩张工具**：overlay 不携带 `allowed_tools`，工具能力边界永远由选中的 domain 决定，叠加再多 overlay 也只是 ⊆ 内置工具。用户无法通过自定义 skill 新增任何能力或执行代码。
2. **overlay 不参与路由**：overlay 不进领域路由池，不会把内置领域"挤掉"。
3. **禁用只减不增**：用户禁用工具（`disabled_tools`）只能从选中 domain 已授予的工具里**划掉**，永远无法新增任何工具。即 `effective_tools ⊆ domain.allowed_tools ⊆ 内置工具`。禁用不影响路由（仍按文章内容选领域），只在选定领域后做减法。

---

## 2. 当前内置 domain

| name | 适用 |
|------|------|
| `general` | 兜底。通用常识，或无法明确归类 / 跨领域时使用 |
| `medical` | 医学健康、养生、医药、临床实验、流行病学 |
| `finance` | 财经新闻、宏观经济、公司财报、股票行情 |
| `technology` | 学术科研、科技前沿、科普、发明专利 |
| `news_policy` | 时政新闻、政策法规、社会突发事件 |

内置 domain 由路由模型（GLM-4-Flash，未配置则回退 Claude）根据文章开头自动选择；任何不确定情况（解析失败 / 置信度 < 0.5 / 选了不存在的领域 / LLM 异常）一律回退 `general`。

> 每个 domain 是 `agent/skills/defs/<name>.md` 一个文件，frontmatter 声明 `name` / `description` / `kind` / `allowed_tools`，正文即领域核查 prompt。**新增一个领域只需新建一个 `.md` 文件**——`load_skills()` 自动发现，`route_skill()` 自动纳入路由池，`GET /skills` 自动返回，无需改任何后端代码。各领域绑定的特化工具见 `agent/tools/registry.py`。

---

## 3. 请求契约（供 C / B）

### 3.1 `POST /analyze`（也支持 `POST /v1/run`，字段一致）

```jsonc
{
  "article_text": "待核查的文章正文……",
  "overlays": [                       // 可选。用户在插件里启用的附加视角
    {
      "name": "anti_clickbait",       // 必填，<= 40 字
      "prompt": "疑似夸大的数字都要重点标出来",  // 必填，<= 4000 字
      "description": "标题党核查"       // 可选，仅展示用
    }
  ],
  "disabled_tools": ["web_search"]    // 可选。用户在工具管理面板里禁用的内置工具名
}
```

- `overlays` 省略或为空数组 → 行为与之前完全一致（纯领域路由）。
- 单请求 `overlays` 数量上限 **10**，超出部分被截断。
- 每个 overlay 由后端逐项校验：缺 `name`/`prompt`、超长等**非法项被跳过**（不会中断分析），并通过 `status` 事件告知（见 4.2）。
- overlay **只在本次请求生效**，不落库、不影响其他用户 → 插件侧自行用 `chrome.storage.local` 持久化用户的 overlay 列表。

#### `disabled_tools`（用户禁用的工具）

- 取值为工具名数组（名字见 `GET /skills` 的 `tools`）。省略或为空 → 行为与之前完全一致。
- 语义是**对选中领域的工具白名单做减法**（见第三条安全不变量），同样**只在本次请求生效、不落库**，插件侧用 `chrome.storage.local` 持久化。
- **未知工具名静默忽略**（前向兼容：插件版本与后端工具集不同步时不报错）；单请求上限 **50**，去重后截断。
- 若某领域的工具被**禁到空集**：分析不中断，Agent 放宽"必须调用工具"的要求，基于声明本身与常识保守判定（倾向 `unsupported_claim`、低 confidence），并发一条 `status`（stage=`route`）警告。
- 禁用**不影响领域路由**：仍按文章内容选领域，只是命中领域后过滤掉被禁工具。

响应为 SSE 事件流（`text/event-stream`），事件格式见 `agent-core-interface.md`。

### 3.2 `GET /skills`

供插件渲染界面（领域列表 + 工具列表 + 限制）：

```jsonc
{
  "domains": [
    {"name": "general", "description": "...", "kind": "domain"},
    {"name": "medical", "description": "...", "kind": "domain"},
    {"name": "finance", "description": "...", "kind": "domain"},
    {"name": "technology", "description": "...", "kind": "domain"},
    {"name": "news_policy", "description": "...", "kind": "domain"}
  ],
  "tools": [                          // 供前端渲染「工具禁用开关」
    {
      "name": "pubmed_scientific_search",
      "description": "搜索 PubMed 医学文献数据库……",
      "used_by": ["medical"]          // 禁用它会影响哪些领域，便于前端提示
    }
    // …全部内置工具
  ],
  "overlay_limits": {
    "max_overlays_per_request": 10,
    "max_name_len": 40,
    "max_prompt_len": 4000
  },
  "disabled_tools_limits": {
    "max_disabled_tools_per_request": 50
  }
}
```

---

## 4. 相关 SSE 事件

### 4.1 `status`（stage = `route`）— 路由结果

领域路由完成后发出，告知本次命中的领域、启用的 overlay、以及被用户禁用后实际可用的工具：

```json
{
  "type": "status",
  "stage": "route",
  "message": "匹配领域 skill：finance",
  "details": {
    "skill": "finance",
    "allowed_tools": "stock_market_quotes, wikipedia_lookup, source_verifier, cross_reference",
    "overlays": "anti_clickbait",
    "disabled_tools": "macro_statistics_global, web_search",
    "effective_tools": "stock_market_quotes, wikipedia_lookup, source_verifier, cross_reference"
  }
}
```

> `allowed_tools` 与 `effective_tools` 均为**禁用后**的实际可用工具（两者一致，前者为兼容旧字段名保留）；`disabled_tools` 为本领域被用户划掉的工具。`details` 各值都是逗号分隔字符串（`StatusEvent.details` 不接受数组）。若某领域工具被禁到空集，会额外先发一条 `route` 警告 status。

C 可据此在侧边栏显示"本次按 finance 领域核查，已应用 1 个附加视角"。

### 4.2 `status`（stage = `route`）— 非法 overlay 提示

某个 overlay 校验失败时发出，分析继续：

```json
{
  "type": "status",
  "stage": "route",
  "message": "已跳过无效的附加视角：overlay `x` 的 prompt 为空"
}
```

> ⚠️ **前向兼容提示**：目前 `route` 事件**整篇只出现一次**（文章级路由）。未来若实现 **claim 级路由**，`route` 事件可能**按声明多次出现并携带 `claim_id`**。C 在消费时请**不要假设它只来一次**，按 `stage="route"` + 可选 `claim_id` 处理即可。

---

## 5. 插件 UI 与配置管理（供 C）

### 5.0 核心前提：后端对 overlay / disabled_tools 是「无状态」的

`overlays` 与 `disabled_tools` **后端都不持久化**，只在单次 `/analyze` 请求内生效（`Agent.run` 的入参，不落库、不影响其他用户）。这是有意为之：不需要登录、不需要服务端用户表。

**代价是持久化责任完全落在前端**：用户的"常用视角""我把股票工具关了"这类偏好，由插件自己存、并在**每次分析时随请求带上**。后端永远是「你这次发什么就用什么」。

### 5.1 界面三块

1. **领域**（单选 / 自动）：一个下拉，默认"自动识别"，也可手动指定某个 domain。选项来自 `GET /skills` 的 `domains`。
2. **附加视角 overlay**（多选开关）：用户**自建**的内容（一段 prompt）。列表 + 新建表单（字段 `name`、`prompt`、`description` 可选）+ 每条一个启用开关。提交分析时把**已启用**的 overlay 放进 `overlays`。
3. **工具开关 disabled_tools**（多选开关）：对**后端已有工具**的开关，一排开关默认全开。选项来自 `GET /skills` 的 `tools`，可借 `used_by` 提示"禁用它会影响 medical 领域"。提交分析时把**已禁用**（开关关闭）的工具名放进 `disabled_tools`。

> 两者管理粒度不同：overlay 是「用户创作的对象数组」，disabled_tools 是「对已有工具名的集合开关」。

### 5.2 本地存储结构（`chrome.storage.local`）

```js
// 用户自建的 overlay 库（可增删改）
overlays_library = [ { id, name, prompt, description, enabled: true|false } ]
// 被禁用的工具名集合
disabled_tools   = ["stock_market_quotes", "web_search"]
```

随用户、跨标签页、跨重启保留。

### 5.3 配置生命周期

1. **打开面板 → 拉 `GET /skills`**：拿到 `domains` / `tools`（含 `used_by`）/ `overlay_limits` / `disabled_tools_limits`，据此渲染领域下拉、工具开关排、各种长度与数量上限。
2. **用户操作 → 即时写回 `chrome.storage.local`**：新建/编辑/删除 overlay、拨动 overlay 或工具开关，都只改本地，**不发请求**（后端不存）。
3. **点「分析」→ 组装请求体**：从本地读出**已启用**的 overlay（仅取 `name`/`prompt`/`description`）+ 当前 `disabled_tools`，随 `POST /analyze` 带上。
4. **消费 SSE `route` 事件回显**：用 details 里的 `skill` / `overlays` / `disabled_tools` / `effective_tools`，在侧边栏显示"本次按 finance 领域核查，已应用 1 个附加视角，已禁用 1 个工具"，让用户确认配置生效。

### 5.4 前端必须处理的边界

- **表单前置校验**：overlay 的 `name`/`prompt` 限长（`overlay_limits`，40 / 4000）；`disabled_tools` 数量上限（`disabled_tools_limits`，50）。别等后端 `status` 报"已跳过无效视角"才发现。
- **工具列表会变，别写死**：每次以 `GET /skills` 的 `tools` 为准；本地 `disabled_tools` 里若有 `tools` 中已不存在的名字，渲染时忽略即可（后端 `_normalize_disabled` 也会静默丢弃，双保险）。
- **空集提醒**：用户把某领域工具全关时，后端会发空集警告 `status`，但更好的体验是开关界面就提示"已禁用全部通用工具，核查将退化为仅凭常识判断"。
- **危险开关二次确认**：禁用 `cross_reference` / `source_verifier` 这类通用核查工具影响面大，UI 上宜标注或弹确认。

---

## 6. 给 Agent 仓库维护者（成员 A 自用）

- 数据结构：`agent/skills/base.py` 的 `Skill`（含 `kind` 字段，默认 `domain`）。
- 内置 domain：`agent/skills/defs/*.md`，frontmatter 含 `name` / `description` / `kind` / `allowed_tools`，正文即领域 prompt。
- overlay 构造与校验：`build_overlay_skill(data: dict) -> Skill`（强制 `kind=overlay`、剥离工具白名单、长度上限）。
- 路由：`route_skill()` 仅在 `kind == "domain"` 的子集中选择。
- 禁用工具：`Agent._normalize_disabled()` 过滤出合法工具名 → `dataclasses.replace` 把选中 domain 收窄成 `effective_skill`（`allowed_tools` 减去禁用集合）→ 下游 `_react_loop` 守卫与系统 prompt 自动只认 effective；被划掉的工具名另作 `disabled_note_tools` 注入"已禁用工具"说明段。后端粗筛见 `_extract_disabled_tools()`。
- 注入：`Agent._build_system_prompt(domain, overlays, disabled_note_tools)` 负责"1 个 domain + N 个 overlay + 禁用说明"的拼接。
- 入口：`Agent.run(article_text, overlays=None, disabled_tools=None)`。

### 后续可扩展（暂未实现）

- **用户自建 domain**：当前用户只能加 overlay；若要支持用户自定义领域，让请求携带 `kind=domain` 的 skill 并并入路由池即可，机制已就位。
- **claim 级路由**：见 4.2 的前向兼容提示。
