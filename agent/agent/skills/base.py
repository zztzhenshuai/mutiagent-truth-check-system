"""
agent/skills/base.py

Skill（领域核查档案）数据结构与加载逻辑，仿 Claude Code 的 SKILL.md：
每个 skill 是一个 markdown 文件，由 YAML frontmatter + 正文构成：

    ---
    name: medical
    description: 触发用的一句话领域描述（路由模型据此选择）
    allowed_tools: [web_search, wikipedia_lookup, source_verifier]
    ---
    <正文：注入 ReAct system prompt 的领域核查指令>

- frontmatter 的 description 给路由模型看，决定何时启用该 skill。
- 正文是 prompt，拼接到通用核查 prompt 之后注入 ReAct。
- allowed_tools 是工具白名单，名字必须存在于 TOOL_REGISTRY。

约定：必须存在一个 name == "general" 的兜底 skill。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger("agent.skills")

# skill 定义目录：agent/skills/defs/
_DEFAULT_SKILL_DIR = Path(__file__).resolve().parent / "defs"

# 兜底 skill 名称
GENERAL_SKILL_NAME = "general"

# skill 角色：
#   domain  —— 领域档案，互斥，路由只选 1 个（内置全是 domain）
#   overlay —— 附加视角，不路由，由用户开关，可多个叠加（用户自建主要是这类）
KIND_DOMAIN = "domain"
KIND_OVERLAY = "overlay"
_VALID_KINDS = {KIND_DOMAIN, KIND_OVERLAY}

# 用户自建 overlay 的安全上限（防止把 system prompt 撑爆 / 互相打架）
MAX_OVERLAY_NAME_LEN = 40
MAX_OVERLAY_PROMPT_LEN = 4000

# 拆分 frontmatter 的正则：开头 --- ... --- 之间为 YAML，其余为正文
_FRONTMATTER_RE = re.compile(r"^\s*---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


@dataclass(frozen=True)
class Skill:
    name: str                  # 唯一标识，如 "medical"
    description: str           # 路由触发描述（给路由模型看；overlay 可空）
    prompt: str                # 正文：核查指令，注入 ReAct system prompt
    allowed_tools: tuple[str, ...]  # 工具白名单（名字须在 TOOL_REGISTRY 中）
    kind: str = KIND_DOMAIN    # "domain" | "overlay"，默认领域
    persona: str = ""          # 方向5新增：Agent 角色身份描述（如"循证医学专家"），可选
    agent_config: dict = field(default_factory=dict, compare=False, hash=False)  # 方向5新增：领域策略偏好


def _parse_skill_file(path: Path) -> Skill:
    """解析单个 skill markdown 文件。"""
    raw = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        raise ValueError(f"skill 文件缺少 frontmatter：{path}")

    meta = yaml.safe_load(match.group(1)) or {}
    body = match.group(2).strip()

    name = str(meta.get("name") or path.stem).strip()
    description = str(meta.get("description") or "").strip()
    kind = str(meta.get("kind") or KIND_DOMAIN).strip()
    persona = str(meta.get("persona") or "").strip()
    agent_config = meta.get("agent_config") or {}
    if not isinstance(agent_config, dict):
        agent_config = {}
    allowed = meta.get("allowed_tools") or []
    if isinstance(allowed, str):
        allowed = [t.strip() for t in allowed.split(",") if t.strip()]
    allowed_tools = tuple(str(t).strip() for t in allowed if str(t).strip())

    if kind not in _VALID_KINDS:
        raise ValueError(f"skill `{name}` 的 kind 非法：{kind}（应为 domain 或 overlay）：{path}")
    if not description:
        raise ValueError(f"skill `{name}` 缺少 description（路由依赖此字段）：{path}")
    if not body:
        raise ValueError(f"skill `{name}` 正文为空（正文即 prompt）：{path}")

    return Skill(name=name, description=description, prompt=body, allowed_tools=allowed_tools, kind=kind, persona=persona, agent_config=agent_config)


def build_overlay_skill(data: dict) -> Skill:
    """
    把用户（浏览器插件）传来的 overlay 配置校验并构造成 Skill。

    overlay 语义：附加视角，不参与路由、不扩张工具能力，仅追加 prompt 指引。
    因此 overlay：
      - kind 固定为 overlay
      - 不接受 allowed_tools（工具边界完全由选中的 domain 决定）
      - name / prompt 有长度上限，description 可选

    校验失败抛 ValueError，由调用方决定是跳过该 overlay 还是报错。
    """
    if not isinstance(data, dict):
        raise ValueError("overlay 配置必须是对象")

    name = str(data.get("name") or "").strip()
    prompt = str(data.get("prompt") or "").strip()
    description = str(data.get("description") or "").strip()

    if not name:
        raise ValueError("overlay 缺少 name")
    if len(name) > MAX_OVERLAY_NAME_LEN:
        raise ValueError(f"overlay `{name}` 的 name 过长（上限 {MAX_OVERLAY_NAME_LEN}）")
    if not prompt:
        raise ValueError(f"overlay `{name}` 的 prompt 为空")
    if len(prompt) > MAX_OVERLAY_PROMPT_LEN:
        raise ValueError(f"overlay `{name}` 的 prompt 过长（上限 {MAX_OVERLAY_PROMPT_LEN} 字）")

    return Skill(
        name=name,
        description=description,
        prompt=prompt,
        allowed_tools=(),       # overlay 不带工具白名单
        kind=KIND_OVERLAY,
    )


def load_skills(skill_dir: Path | None = None) -> dict[str, Skill]:
    """
    加载目录下所有 *.md skill，返回 {name: Skill}。

    校验：
      - 必须包含 name == "general" 的兜底 skill
      - 每个 skill 的 allowed_tools 必须都存在于 TOOL_REGISTRY
        （懒导入 TOOL_REGISTRY，避免与 tools 包形成循环依赖）
    """
    skill_dir = skill_dir or _DEFAULT_SKILL_DIR
    if not skill_dir.is_dir():
        raise FileNotFoundError(f"skill 目录不存在：{skill_dir}")

    skills: dict[str, Skill] = {}
    for path in sorted(skill_dir.glob("*.md")):
        skill = _parse_skill_file(path)
        if skill.name in skills:
            raise ValueError(f"skill 名称重复：{skill.name}（{path}）")
        skills[skill.name] = skill

    if GENERAL_SKILL_NAME not in skills:
        raise ValueError(
            f"缺少兜底 skill `{GENERAL_SKILL_NAME}`，请在 {skill_dir} 下提供 {GENERAL_SKILL_NAME}.md"
        )

    # 校验工具白名单合法性（懒导入避免循环依赖）
    from ..tools.registry import TOOL_REGISTRY
    for skill in skills.values():
        unknown = [t for t in skill.allowed_tools if t not in TOOL_REGISTRY]
        if unknown:
            raise ValueError(
                f"skill `{skill.name}` 引用了未注册的工具：{unknown}；"
                f"可用工具：{sorted(TOOL_REGISTRY)}"
            )

    logger.info(
        "已加载 %d 个 skill：%s",
        len(skills),
        [f"{s.name}({s.kind})" for s in skills.values()],
    )
    return skills
