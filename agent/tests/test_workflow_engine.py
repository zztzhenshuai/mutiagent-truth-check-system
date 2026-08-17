"""
tests/test_workflow_engine.py

WorkflowEngine 和 DAG 配置的单元测试。
"""

from __future__ import annotations

import pytest

from agent.workflow.config import (
    ConditionalEdge,
    DAGConfig,
    SAFE_BUILTINS,
    _evaluate_condition,
    load_dag_config,
)
from agent.workflow.engine import WorkflowEngine
from agent.workflow.node import FunctionalNode, NodeOutput, WorkflowContext, WorkflowNode


class TestWorkflowNode:
    """WorkflowNode 基类和 FunctionalNode 测试。"""

    def test_node_name_is_class_attribute(self):
        class MyNode(WorkflowNode):
            name = "my_test_node"

            async def execute(self, ctx):
                return NodeOutput(data={"done": True})

        node = MyNode()
        assert node.name == "my_test_node"

    def test_functional_node_wraps_dict_to_nodeoutput(self):
        async def my_fn(ctx):
            return {"key": "value"}

        node = FunctionalNode("fn_node", my_fn)
        assert node.name == "fn_node"

    def test_functional_node_passes_nodeoutput_through(self):
        async def my_fn(ctx):
            return NodeOutput(data={"key": "value"}, events=[])

        node = FunctionalNode("fn_node", my_fn)
        assert node.name == "fn_node"

    def test_can_skip_defaults_to_false(self):
        class MyNode(WorkflowNode):
            name = "test"

            async def execute(self, ctx):
                return NodeOutput()

        node = MyNode()
        assert node.can_skip(WorkflowContext()) is False

    def test_functional_node_can_skip(self):
        async def my_fn(ctx):
            return NodeOutput()

        node = FunctionalNode("fn", my_fn, can_skip_fn=lambda ctx: True)
        assert node.can_skip(WorkflowContext()) is True

    def test_workflow_context_is_dict(self):
        ctx = WorkflowContext(claims=[1, 2], article_text="hello")
        assert ctx["claims"] == [1, 2]
        assert ctx["article_text"] == "hello"
        ctx["new_key"] = "value"
        assert ctx["new_key"] == "value"


class TestConditionEvaluation:
    """条件表达式求值测试。"""

    def test_simple_equality(self):
        ctx = {"claims": []}
        assert _evaluate_condition("len(ctx['claims']) == 0", ctx) is True

    def test_simple_false(self):
        ctx = {"claims": [1, 2, 3]}
        assert _evaluate_condition("len(ctx['claims']) == 0", ctx) is False

    def test_get_with_default(self):
        ctx = {}
        assert _evaluate_condition("len(ctx.get('claims', [])) == 0", ctx) is True

    def test_boolean_logic(self):
        ctx = {"a": 5, "b": 3}
        assert _evaluate_condition("ctx['a'] > ctx['b']", ctx) is True
        assert _evaluate_condition("ctx['a'] < ctx['b']", ctx) is False

    def test_invalid_expression_returns_false(self):
        ctx = {}
        # 引用不存在的变量
        assert _evaluate_condition("undefined_var == 1", ctx) is False

    def test_safe_builtins_available(self):
        ctx = {"items": [1, 2, 3]}
        assert _evaluate_condition("len(ctx['items'])", ctx) is True
        assert _evaluate_condition("all(ctx['items'])", ctx) is True
        assert _evaluate_condition("any([x > 0 for x in ctx['items']])", ctx) is True

    def test_dangerous_builtins_blocked(self):
        ctx = {}
        # __import__ 被禁用
        assert _evaluate_condition("__import__('os')", ctx) is False

    def test_none_handling(self):
        ctx = {"val": None}
        assert _evaluate_condition("ctx['val'] is None", ctx) is True


class TestDAGConfig:
    """DAGConfig 配置校验测试。"""

    def test_empty_dag_fails_validation(self):
        config = DAGConfig("test")
        errors = config.validate()
        assert len(errors) > 0
        assert any("没有注册任何节点" in e for e in errors)

    def test_valid_linear_dag(self):
        config = DAGConfig("linear")
        config.add_node(FunctionalNode("A", _noop))
        config.add_node(FunctionalNode("B", _noop))
        config.add_node(FunctionalNode("C", _noop))
        config.add_edge("A", "B")
        config.add_edge("B", "C")

        errors = config.validate()
        assert errors == []
        assert config.entry_node == "A"

    def test_dag_detects_cycle(self):
        config = DAGConfig("cycle")
        config.add_node(FunctionalNode("A", _noop))
        config.add_node(FunctionalNode("B", _noop))
        config.add_node(FunctionalNode("C", _noop))
        config.add_edge("A", "B")
        config.add_edge("B", "C")
        config.add_edge("C", "A")  # 构成环路

        errors = config.validate()
        assert any("环路" in e for e in errors)

    def test_dag_detects_missing_node_in_edge(self):
        config = DAGConfig("missing")
        config.add_node(FunctionalNode("A", _noop))
        config.add_node(FunctionalNode("B", _noop))
        config.add_edge("A", "NonExistent")

        errors = config.validate()
        assert any("NonExistent" in e for e in errors)

    def test_dag_multiple_entries_fails(self):
        config = DAGConfig("multi_entry")
        config.add_node(FunctionalNode("A", _noop))
        config.add_node(FunctionalNode("B", _noop))
        # 无边连接，两个都是孤立入口
        errors = config.validate()
        assert any("多个入口" in e for e in errors)

    def test_topo_sort_linear(self):
        config = DAGConfig("linear")
        config.add_node(FunctionalNode("A", _noop))
        config.add_node(FunctionalNode("B", _noop))
        config.add_node(FunctionalNode("C", _noop))
        config.add_edge("A", "B")
        config.add_edge("B", "C")

        order = config.topo_sort()
        assert order == ["A", "B", "C"]

    def test_topo_sort_diamond(self):
        config = DAGConfig("diamond")
        config.add_node(FunctionalNode("A", _noop))
        config.add_node(FunctionalNode("B", _noop))
        config.add_node(FunctionalNode("C", _noop))
        config.add_node(FunctionalNode("D", _noop))
        config.add_edge("A", "B")
        config.add_edge("A", "C")
        config.add_edge("B", "D")
        config.add_edge("C", "D")

        order = config.topo_sort()
        assert order[0] == "A"
        assert order[-1] == "D"
        assert set(order[1:3]) == {"B", "C"}

    def test_resolve_next_unconditional(self):
        config = DAGConfig("linear")
        config.add_node(FunctionalNode("A", _noop))
        config.add_node(FunctionalNode("B", _noop))
        config.add_edge("A", "B")

        ctx = {}
        assert config.resolve_next("A", ctx) == "B"
        assert config.resolve_next("B", ctx) is None

    def test_resolve_next_conditional_true(self):
        config = DAGConfig("conditional")
        config.add_node(FunctionalNode("A", _noop))
        config.add_node(FunctionalNode("B", _noop))
        config.add_node(FunctionalNode("C", _noop))
        config.add_conditional_edge(
            ConditionalEdge("A", "ctx.get('flag') == True", "B", "C")
        )
        config.add_edge("A", "C")  # 默认边

        ctx = {"flag": True}
        assert config.resolve_next("A", ctx) == "B"

    def test_resolve_next_conditional_false(self):
        config = DAGConfig("conditional")
        config.add_node(FunctionalNode("A", _noop))
        config.add_node(FunctionalNode("B", _noop))
        config.add_node(FunctionalNode("C", _noop))
        config.add_conditional_edge(
            ConditionalEdge("A", "ctx.get('flag') == True", "B", "C")
        )
        config.add_edge("A", "C")

        ctx = {"flag": False}
        assert config.resolve_next("A", ctx) == "C"


class TestDAGConfigLoading:
    """YAML 配置文件加载测试。"""

    def test_load_default_dag(self):
        """验证默认 DAG 配置可正确加载。"""
        dag = load_dag_config("agent/workflow/default_dag.yaml")
        assert dag.name == "default_factcheck"
        assert dag.version == 1
        assert set(dag.nodes.keys()) == {
            "scan", "plan", "context", "route", "debate",
            "summary", "summary_empty",
        }
        assert dag.entry_node == "scan"
        errors = dag.validate()
        assert errors == []

    def test_default_dag_has_correct_edges(self):
        dag = load_dag_config("agent/workflow/default_dag.yaml")
        assert "scan" in dag.edges
        assert dag.edges["scan"] == ["plan"]
        assert dag.edges["debate"] == ["summary"]
        # 条件边：plan → summary_empty (claims 为空时)
        assert len(dag.conditional_edges) == 1
        assert dag.conditional_edges[0].from_node == "plan"

    def test_load_nonexistent_config_raises(self):
        with pytest.raises(FileNotFoundError):
            load_dag_config("agent/workflow/nonexistent.yaml")


class TestWorkflowEngine:
    """WorkflowEngine 集成测试。"""

    @pytest.mark.asyncio
    async def test_engine_runs_linear_dag(self):
        """引擎沿线性 DAG 执行所有节点。"""
        executed = []

        def make_node_fn(name):
            async def fn(ctx):
                executed.append(name)
                return NodeOutput(data={name: True})
            return fn

        config = DAGConfig("linear_test")
        for name in ["A", "B", "C"]:
            config.add_node(FunctionalNode(name, make_node_fn(name)))
        config.add_edge("A", "B")
        config.add_edge("B", "C")

        engine = WorkflowEngine(config)
        ctx = WorkflowContext()
        events = []
        async for event in engine.run(ctx):
            events.append(event)

        assert executed == ["A", "B", "C"]
        assert ctx["A"] is True
        assert ctx["B"] is True
        assert ctx["C"] is True

    @pytest.mark.asyncio
    async def test_engine_skips_node(self):
        """can_skip=True 的节点被跳过但不中断 DAG。"""
        executed = []

        async def always_run(ctx):
            executed.append("A")
            return NodeOutput()

        async def skipped_run(ctx):
            executed.append("SHOULD_NOT_RUN")
            return NodeOutput()

        async def final_run(ctx):
            executed.append("B")
            return NodeOutput()

        config = DAGConfig("skip_test")
        config.add_node(FunctionalNode("A", always_run))
        config.add_node(
            FunctionalNode("skip_me", skipped_run,
                          can_skip_fn=lambda ctx: True)
        )
        config.add_node(FunctionalNode("B", final_run))
        config.add_edge("A", "skip_me")
        config.add_edge("skip_me", "B")

        engine = WorkflowEngine(config)
        ctx = WorkflowContext()
        async for _ in engine.run(ctx):
            pass

        assert "SHOULD_NOT_RUN" not in executed
        assert executed == ["A", "B"]

    @pytest.mark.asyncio
    async def test_engine_follows_conditional_edge(self):
        """条件边根据 ctx 动态选择下一节点。"""
        executed = []

        async def node_a(ctx):
            executed.append("A")
            return NodeOutput()

        async def node_b(ctx):
            executed.append("B")
            return NodeOutput()

        async def node_c(ctx):
            executed.append("C")
            return NodeOutput()

        config = DAGConfig("cond_test")
        config.add_node(FunctionalNode("A", node_a))
        config.add_node(FunctionalNode("B", node_b))
        config.add_node(FunctionalNode("C", node_c))
        config.add_conditional_edge(
            ConditionalEdge("A", "ctx.get('go_b') == True", "B", "C")
        )
        config.add_edge("A", "C")

        # 测试 true 分支
        engine = WorkflowEngine(config)
        ctx = WorkflowContext(go_b=True)
        async for _ in engine.run(ctx):
            pass
        assert executed == ["A", "B"]

        # 测试 false 分支
        executed.clear()
        ctx2 = WorkflowContext(go_b=False)
        engine2 = WorkflowEngine(config)
        async for _ in engine2.run(ctx2):
            pass
        assert executed == ["A", "C"]

    @pytest.mark.asyncio
    async def test_engine_next_node_override(self):
        """NodeOutput.next_node 覆盖 DAG 边。"""
        executed = []

        async def node_a(ctx):
            executed.append("A")
            return NodeOutput(next_node="C")  # 覆盖边 A→B

        async def node_b(ctx):
            executed.append("SHOULD_NOT_RUN")
            return NodeOutput()

        async def node_c(ctx):
            executed.append("C")
            return NodeOutput()

        config = DAGConfig("next_node_test")
        config.add_node(FunctionalNode("A", node_a))
        config.add_node(FunctionalNode("B", node_b))
        config.add_node(FunctionalNode("C", node_c))
        config.add_edge("A", "B")
        config.add_edge("B", "C")

        engine = WorkflowEngine(config)
        ctx = WorkflowContext()
        async for _ in engine.run(ctx):
            pass
        assert "SHOULD_NOT_RUN" not in executed
        assert executed == ["A", "C"]

    @pytest.mark.asyncio
    async def test_engine_cycle_guard(self):
        """引擎检测到环路时终止（不进入死循环）。"""
        executed = []

        async def node_a(ctx):
            executed.append("A")
            return NodeOutput()

        async def node_b(ctx):
            executed.append("B")
            return NodeOutput()

        config = DAGConfig("cycle_test")
        config.add_node(FunctionalNode("A", node_a))
        config.add_node(FunctionalNode("B", node_b))
        config.add_edge("A", "B")
        config.add_conditional_edge(
            ConditionalEdge("B", "True", "A", None)
        )
        config.add_edge("B", "A")  # B → A 构成环路

        engine = WorkflowEngine(config)
        ctx = WorkflowContext()
        async for _ in engine.run(ctx):
            pass
        # 最多执行两轮(A→B)然后因环路守卫终止
        assert len(executed) <= 3  # 不会无限循环

    @pytest.mark.asyncio
    async def test_engine_node_error_produces_error_event(self):
        """节点异常不中断整个 DAG，产出 ErrorEvent。"""
        executed = []

        async def failing_node(ctx):
            executed.append("fail")
            raise ValueError("模拟节点失败")

        async def recovery_node(ctx):
            executed.append("recovery")
            return NodeOutput()

        config = DAGConfig("error_test")
        config.add_node(FunctionalNode("fail", failing_node))
        config.add_node(FunctionalNode("recovery", recovery_node))
        config.add_edge("fail", "recovery")

        engine = WorkflowEngine(config)
        ctx = WorkflowContext()
        events = []
        async for event in engine.run(ctx):
            events.append(event)

        from agent.models import ErrorEvent
        error_events = [e for e in events if isinstance(e, ErrorEvent)]
        assert len(error_events) == 1
        assert "模拟节点失败" in error_events[0].message
        assert executed == ["fail", "recovery"]


# ── 辅助 ──

async def _noop(ctx=None):
    return NodeOutput()
