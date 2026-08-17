from __future__ import annotations

from agent.dataset_loader import list_available_datasets, load_dataset
from agent.tools.registry import TOOL_REGISTRY


VALID_ERROR_TYPES = {
    "factual_error",
    "logical_fallacy",
    "contradiction",
    "unsupported_claim",
}


def test_dataset_is_listed():
    assert "factcheck_eval_v1" in list_available_datasets()


def test_dataset_summary_counts_are_consistent():
    dataset = load_dataset()

    assert dataset.version == "2.0.0"
    assert dataset.article_count == 5
    assert dataset.error_article_count == 5
    assert dataset.control_article_count == 0
    assert dataset.error_article_count + dataset.control_article_count == dataset.article_count
    assert sum(len(article.ground_truth) for article in dataset.articles) == dataset.total_annotations
    assert {metric.name for metric in dataset.evaluation_metrics} >= {
        "annotation_accuracy",
        "tool_call_chain",
    }


def test_dataset_offsets_and_confidence_ranges_are_valid():
    dataset = load_dataset()

    for article in dataset.articles:
        assert 200 <= len(article.content) <= 300, f"{article.id} 正文长度不在 200-300 字范围内"
        assert article.ground_truth, f"{article.id} 应至少包含一条标注"
        for item in article.ground_truth:
            assert item.error_type in VALID_ERROR_TYPES
            assert 0.0 <= item.expected_confidence_min <= item.expected_confidence_max <= 1.0
            assert article.content[item.start_offset:item.end_offset] == item.text
            assert item.explanation.strip()


def test_dataset_tool_call_expectations_are_valid():
    dataset = load_dataset()
    valid_tools = set(TOOL_REGISTRY)

    for article in dataset.articles:
        expectation = article.tool_call_expectation
        assert expectation is not None, f"{article.id} 缺少工具调用链路"
        assert expectation.required_tools
        assert expectation.expected_sequence
        assert expectation.success_rule.strip()
        assert set(expectation.required_tools).issubset(valid_tools)
        assert set(expectation.expected_sequence).issubset(valid_tools)
        assert set(expectation.optional_tools).issubset(valid_tools)
        required_indexes = [
            expectation.expected_sequence.index(tool)
            for tool in expectation.required_tools
            if tool in expectation.expected_sequence
        ]
        assert len(required_indexes) == len(expectation.required_tools)
