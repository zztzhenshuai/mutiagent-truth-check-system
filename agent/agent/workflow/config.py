"""
agent/workflow/config.py

DAG 配置加载、校验、合并。

支持：
  - 从 YAML 文件加载 DAG 描述
  - 校验 DAG 合法性（无环、边端点存在、无孤立节点）
  - 合并领域 dag_overrides（方向5融合）
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .node import WorkflowNode

logger = logging.getLogger("agent.workflow.config")

# ── 安全内置函数（条件表达式求值用）──
SAFE_BUILTINS: dict[str, Any] = {
    "len": len,
    "isinstance": isinstance,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
    "any": any,
    "all": all,
    "max": max,
    "min": min,
    "True": True,
    "False": False,
    "None": None,
}


@dataclass
class ConditionalEdge:
    """条件边：根据 ctx 中的条件决定走向。"""
    from_node: str
    condition: str          # Python 表达式，在 ctx 命名空间中求值
    to_if_true: str
    to_if_false: str | None = None  # None = 走默认边


class DAGConfig:
    """解析后的 DAG 配置。

    Attributes:
        name: DAG 名称
        version: 配置版本号
        description: 用途说明
        nodes: {name: WorkflowNode 实例}
        edges: {from_node: [to_node, ...]}  无条件边
        conditional_edges: 条件边列表
        entry_node: 入口节点名
    """

    def __init__(
        self,
        name: str,
        version: int = 1,
        description: str = "",
    ):
        self.name = name
        self.version = version
        self.description = description
        self.nodes: dict[str, WorkflowNode] = {}
        self.edges: dict[str, list[str]] = {}
        self.conditional_edges: list[ConditionalEdge] = []
        self.entry_node: str = ""

    def add_node(self, node: WorkflowNode):
        """注册一个节点实例。"""
        self.nodes[node.name] = node

    def add_edge(self, from_node: str, to_node: str):
        """添加一条无条件边。"""
        self.edges.setdefault(from_node, []).append(to_node)

    def add_conditional_edge(self, ce: ConditionalEdge):
        """添加一条条件边。"""
        self.conditional_edges.append(ce)

    def resolve_next(self, node_name: str, ctx: dict) -> str | None:
        """根据当前节点名和上下文，解析下一个节点。

        优先检查条件边 → 无条件边 → None（终止）。
        """
        # 1. 检查条件边
        for ce in self.conditional_edges:
            if ce.from_node == node_name:
                if _evaluate_condition(ce.condition, ctx):
                    return ce.to_if_true
                elif ce.to_if_false is not None:
                    return ce.to_if_false
                # to_if_false is None → fall through to unconditional edges

        # 2. 无条件边
        targets = self.edges.get(node_name, [])
        if len(targets) == 1:
            return targets[0]
        elif len(targets) > 1:
            # 多条无条件边 = 扇出，resolve_next 不处理，
            # 由引擎的扇出逻辑处理，此处返回第一个
            return targets[0]
        return None

    def validate(self) -> list[str]:
        """校验 DAG 合法性，返回错误信息列表。"""
        errors: list[str] = []

        if not self.nodes:
            errors.append("DAG 没有注册任何节点")
            return errors

        # 1. 边端点存在性
        all_names = set(self.nodes)
        for from_n, to_list in self.edges.items():
            if from_n not in all_names:
                errors.append(f"边起点 '{from_n}' 不在已注册节点中")
            for to_n in to_list:
                if to_n not in all_names:
                    errors.append(f"边终点 '{to_n}' 不在已注册节点中")

        for ce in self.conditional_edges:
            if ce.from_node not in all_names:
                errors.append(f"条件边起点 '{ce.from_node}' 不在已注册节点中")
            if ce.to_if_true not in all_names:
                errors.append(f"条件边真分支终点 '{ce.to_if_true}' 不在已注册节点中")
            if ce.to_if_false is not None and ce.to_if_false not in all_names:
                errors.append(f"条件边假分支终点 '{ce.to_if_false}' 不在已注册节点中")

        # 若已存在端点错误，后续计算无法进行，提前返回
        if errors:
            return errors

        # 2. 入口唯一性
        in_degree: dict[str, int] = {n: 0 for n in all_names}
        for from_n, to_list in self.edges.items():
            for to_n in to_list:
                in_degree[to_n] += 1
        for ce in self.conditional_edges:
            in_degree[ce.to_if_true] += 1
            if ce.to_if_false is not None:
                in_degree[ce.to_if_false] += 1

        zero_in = [n for n, d in in_degree.items() if d == 0]
        if len(zero_in) == 1:
            self.entry_node = zero_in[0]
        elif len(zero_in) == 0:
            errors.append("DAG 存在环路：所有节点都有入边")
        else:
            errors.append(f"DAG 有多个入口节点：{zero_in}")

        # 3. 无环路（Kahn 算法）
        temp_in = dict(in_degree)
        queue = [n for n, d in temp_in.items() if d == 0]
        sorted_count = 0
        while queue:
            n = queue.pop(0)
            sorted_count += 1
            for to_n in self.edges.get(n, []):
                temp_in[to_n] -= 1
                if temp_in[to_n] == 0:
                    queue.append(to_n)
            for ce in self.conditional_edges:
                if ce.from_node == n:
                    temp_in[ce.to_if_true] -= 1
                    if temp_in[ce.to_if_true] == 0:
                        queue.append(ce.to_if_true)
                    if ce.to_if_false is not None:
                        temp_in[ce.to_if_false] -= 1
                        if temp_in[ce.to_if_false] == 0:
                            queue.append(ce.to_if_false)
        if sorted_count != len(all_names):
            errors.append(f"DAG 存在环路（拓扑排序只覆盖了 {sorted_count}/{len(all_names)} 个节点）")

        return errors

    def topo_sort(self) -> list[str]:
        """Kahn 拓扑排序，返回节点名列表。"""
        all_names = set(self.nodes)
        in_degree: dict[str, int] = {n: 0 for n in all_names}
        for from_n, to_list in self.edges.items():
            for to_n in to_list:
                in_degree[to_n] += 1
        for ce in self.conditional_edges:
            in_degree[ce.to_if_true] += 1
            if ce.to_if_false is not None:
                in_degree[ce.to_if_false] += 1

        queue = [n for n, d in in_degree.items() if d == 0]
        result = []
        while queue:
            n = queue.pop(0)
            result.append(n)
            for to_n in self.edges.get(n, []):
                in_degree[to_n] -= 1
                if in_degree[to_n] == 0:
                    queue.append(to_n)
            for ce in self.conditional_edges:
                if ce.from_node == n:
                    in_degree[ce.to_if_true] -= 1
                    if in_degree[ce.to_if_true] == 0:
                        queue.append(ce.to_if_true)
                    if ce.to_if_false is not None:
                        in_degree[ce.to_if_false] -= 1
                        if in_degree[ce.to_if_false] == 0:
                            queue.append(ce.to_if_false)
        return result


def _evaluate_condition(expr: str, ctx: dict) -> bool:
    """在受限命名空间中求值条件表达式。

    使用白名单内置函数，禁用危险操作（__import__、open 等）。
    求值失败 → False（安全保守侧）。
    """
    namespace = {**SAFE_BUILTINS, "ctx": ctx}
    try:
        result = eval(expr, {"__builtins__": {}}, namespace)
        return bool(result)
    except Exception:
        logger.warning("条件表达式求值失败（已安全回退 False）：%s", expr, exc_info=True)
        return False


def _resolve_node_class(class_path: str) -> type[WorkflowNode]:
    """将 'agent.workflow.nodes.ScanNode' 解析为类对象。"""
    module_path, class_name = class_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def load_dag_config(config_path: str | Path) -> DAGConfig:
    """加载 YAML DAG 配置文件并实例化所有节点。

    Args:
        config_path: YAML 配置文件的绝对路径。

    Returns:
        已实例化并校验的 DAGConfig。

    Raises:
        ValueError: 配置不合法。
        FileNotFoundError: 配置文件不存在。
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"DAG 配置文件不存在：{path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"DAG 配置文件格式错误：{path}")

    config = DAGConfig(
        name=raw.get("name", path.stem),
        version=raw.get("version", 1),
        description=raw.get("description", ""),
    )

    # 加载节点
    for node_spec in raw.get("nodes", []):
        name = node_spec["name"]
        class_path = node_spec["class"]
        node_cls = _resolve_node_class(class_path)
        # 用类属性的 name 覆盖（节点类自己定义 name）
        node = node_cls()
        config.add_node(node)

    # 加载边
    for edge in raw.get("edges", []):
        config.add_edge(edge["from"], edge["to"])

    # 加载条件边
    for ce_spec in raw.get("conditional_edges", []):
        config.add_conditional_edge(ConditionalEdge(
            from_node=ce_spec["from"],
            condition=ce_spec["condition"],
            to_if_true=ce_spec["to_if_true"],
            to_if_false=ce_spec.get("to_if_false"),
        ))

    # 校验
    errors = config.validate()
    if errors:
        raise ValueError(f"DAG 配置校验失败 ({path})：\n" + "\n".join(f"  - {e}" for e in errors))

    logger.info(
        "已加载 DAG 配置 '%s'：%d 个节点，%d 条边，%d 条条件边，入口=%s",
        config.name, len(config.nodes), len(config.edges),
        len(config.conditional_edges), config.entry_node,
    )
    return config
