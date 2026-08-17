from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


_DATASET_DIR = Path(__file__).resolve().parents[1] / "datasets"


@dataclass(frozen=True)
class GroundTruthAnnotation:
    text: str
    start_offset: int
    end_offset: int
    error_type: str
    explanation: str
    expected_confidence_min: float
    expected_confidence_max: float


@dataclass(frozen=True)
class ToolCallExpectation:
    required_tools: list[str] = field(default_factory=list)
    expected_sequence: list[str] = field(default_factory=list)
    optional_tools: list[str] = field(default_factory=list)
    success_rule: str = ""


@dataclass(frozen=True)
class EvaluationMetric:
    name: str
    description: str
    success_rule: str = ""


@dataclass(frozen=True)
class DatasetArticle:
    id: str
    title: str
    category: str
    is_control: bool
    content: str
    ground_truth: list[GroundTruthAnnotation] = field(default_factory=list)
    tool_call_expectation: ToolCallExpectation | None = None


@dataclass(frozen=True)
class EvaluationDataset:
    dataset_id: str
    version: str
    language: str
    description: str
    article_count: int
    error_article_count: int
    control_article_count: int
    total_annotations: int
    evaluation_metrics: list[EvaluationMetric] = field(default_factory=list)
    articles: list[DatasetArticle] = field(default_factory=list)


def list_available_datasets(dataset_dir: Path | None = None) -> list[str]:
    base_dir = dataset_dir or _DATASET_DIR
    return sorted(path.stem for path in base_dir.glob("*.json"))


def load_dataset(dataset_id: str = "factcheck_eval_v1", dataset_dir: Path | None = None) -> EvaluationDataset:
    base_dir = dataset_dir or _DATASET_DIR
    path = base_dir / f"{dataset_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"数据集不存在：{path}")

    raw = json.loads(path.read_text(encoding="utf-8"))
    articles = [
        DatasetArticle(
            id=article["id"],
            title=article["title"],
            category=article.get("category", "unknown"),
            is_control=bool(article.get("is_control", False)),
            content=article["content"],
            ground_truth=[
                GroundTruthAnnotation(
                    text=item["text"],
                    start_offset=int(item["start_offset"]),
                    end_offset=int(item["end_offset"]),
                    error_type=item["error_type"],
                    explanation=item["explanation"],
                    expected_confidence_min=float(item["expected_confidence_min"]),
                    expected_confidence_max=float(item["expected_confidence_max"]),
                )
                for item in article.get("ground_truth", [])
            ],
            tool_call_expectation=(
                ToolCallExpectation(
                    required_tools=list(article["tool_call_expectation"].get("required_tools", [])),
                    expected_sequence=list(article["tool_call_expectation"].get("expected_sequence", [])),
                    optional_tools=list(article["tool_call_expectation"].get("optional_tools", [])),
                    success_rule=article["tool_call_expectation"].get("success_rule", ""),
                )
                if article.get("tool_call_expectation")
                else None
            ),
        )
        for article in raw.get("articles", [])
    ]

    return EvaluationDataset(
        dataset_id=raw["dataset_id"],
        version=raw["version"],
        language=raw["language"],
        description=raw["description"],
        article_count=int(raw["article_count"]),
        error_article_count=int(raw["error_article_count"]),
        control_article_count=int(raw["control_article_count"]),
        total_annotations=int(raw["total_annotations"]),
        evaluation_metrics=[
            EvaluationMetric(
                name=item["name"],
                description=item["description"],
                success_rule=item.get("success_rule", ""),
            )
            for item in raw.get("evaluation_metrics", [])
        ],
        articles=articles,
    )
