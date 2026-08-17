"""
API 健康检查测试 — 测试所有 14 个工具对外部 API 的真实连通性。

这个测试**不强制全部通过**，因为系统设计了 fallback 链路。
目的仅是生成一份 API 健康状态表，方便快速排查问题。

用法:
    pytest tests/test_api_health.py -v -s          # 逐工具测试
    pytest tests/test_api_health.py -v -s -k "api" # 同上
    python tests/test_api_health.py                 # 直接运行打印报告

每个工具标记为:
    ✅ HEALTHY   — 主 API 正常
    ⚠️ DEGRADED  — 主 API 异常，降级到 fallback 可用
    ❌ UNHEALTHY — 所有层级均失败
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable

import pytest

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

# 加载 .env
try:
    from dotenv import load_dotenv

    load_dotenv(_PROJECT_ROOT / ".env")
except ImportError:
    pass


# ──────────────────────────────────────────────
# 数据模型
# ──────────────────────────────────────────────


@dataclass
class ApiCheckResult:
    tool_name: str
    display_name: str
    category: str  # "通用" | "医疗" | "金融" | "科技" | "新闻"
    primary_api: str  # 主 API 名称
    status: str = "UNTESTED"  # HEALTHY / DEGRADED / UNHEALTHY
    latency_ms: float = 0.0
    fallback_used: bool = False
    error_message: str = ""
    note: str = ""


# ──────────────────────────────────────────────
# 辅助函数
# ──────────────────────────────────────────────


async def _run_with_timeout(coro: Awaitable[str], timeout: float = 25.0) -> tuple[str, float, str | None]:
    """运行协程并计时，超时返回错误。"""
    t0 = time.monotonic()
    try:
        result = await asyncio.wait_for(coro, timeout=timeout)
        elapsed = (time.monotonic() - t0) * 1000
        return result, elapsed, None
    except asyncio.TimeoutError:
        elapsed = (time.monotonic() - t0) * 1000
        return "", elapsed, f"超时（>{timeout:.0f}s）"
    except Exception as exc:
        elapsed = (time.monotonic() - t0) * 1000
        return "", elapsed, f"{type(exc).__name__}: {exc}"


def _check_result(result: str, tool_name: str = "") -> tuple[bool, bool]:
    """
    解析工具返回结果，判断是主 API 成功还是触发了 fallback/错误。
    返回 (primary_ok, fallback_ok)。

    使用按工具分派的标记集合，避免不同工具输出格式差异导致误判。
    """
    if not result:
        return False, False

    # ── 通用失败标记 ──
    fail_markers = [
        "工具执行失败",
        "搜索服务均不可用",
        "未配置",
        "缺少依赖",
    ]
    has_failure = any(m in result for m in fail_markers)

    if has_failure:
        return False, False

    # ── 按工具的主 API 成功标记 ──
    primary_marker_map = {
        "web_search":              ["【AI 总结】"],                         # Tavily 成功
        "wikipedia_lookup":        ["维基百科(中文)检索结果", "维基百科(英文)检索结果"],
        "source_verifier":         ["页面正文摘要:"],                        # readability-lxml 成功
        "wikidata_lookup":         ["[SOURCE]: Wikidata"],
        "cross_reference":         ["相关句检索结果", "未发现明确矛盾", "未找到达到阈值"],
        "macro_statistics_global": ["[SOURCE]: World Bank API"],            # 区别于 Local Static Cache
        "stock_market_quotes":     ["[SOURCE]: Yahoo Finance API"],
        "pubmed_scientific_search":["[SOURCE]: PubMed Database"],
        "consumer_health_verifier":["[SOURCE]: Authoritative Consumer Health Portals"],
        "academic_paper_search":   ["[SOURCE]: Semantic Scholar"],
        "preprint_arxiv_search":   ["[SOURCE]: arXiv API"],
        "patent_status_lookup":    ["[SOURCE]: Authoritative Global Patent Databases"],
        "fact_check_domestic":     ["[SOURCE]: Authoritative Domestic Fact Check Portals"],
        "fact_check_global":       ["[SOURCE]: Google Fact Check API", "[SOURCE]: Authoritative Global Fact Check Portals"],
    }

    # ── 按工具的降级/fallback 标记（主 API 未命中但 fallback 可用）──
    fallback_marker_map = {
        "web_search":              ["以下为DuckDuckGo"],
        "wikipedia_lookup":        [],
        "source_verifier":         [],
        "wikidata_lookup":         [],
        "cross_reference":         [],
        "macro_statistics_global": ["Local Static Cache"],
        "stock_market_quotes":     ["Stock Quote Fallback"],
        "pubmed_scientific_search":[],
        "consumer_health_verifier":["Consumer Health Fallback"],
        "academic_paper_search":   [],  # S2 无内置降级，失败时直接返回错误信息
        "preprint_arxiv_search":   ["arXiv Fallback"],
        "patent_status_lookup":    ["Patent Status Fallback"],
        "fact_check_domestic":     ["Domestic Fact Check Fallback"],
        "fact_check_global":       ["Global Fact Check Fallback"],
    }

    primary_markers = primary_marker_map.get(tool_name, [])
    fallback_markers = fallback_marker_map.get(tool_name, [])

    has_primary = any(m in result for m in primary_markers) if primary_markers else False
    has_fallback = any(m in result for m in fallback_markers) if fallback_markers else False

    if has_primary:
        return True, True
    elif has_fallback:
        return False, True
    else:
        # 没有明确的成功/失败标记 → 通用判断
        # 有实质性输出内容就算可用
        if len(result) > 100 and "失败" not in result:
            return True, True
        return False, True


# ──────────────────────────────────────────────
# 单个工具测试用例
# ──────────────────────────────────────────────


CHECKS: list[ApiCheckResult] = []


def _make_test(
    tool_name: str,
    display_name: str,
    category: str,
    primary_api: str,
    tool_func: Callable[..., Awaitable[str]],
    test_input: str,
    extra_checks: Callable[[str], dict[str, Any]] | None = None,
):
    """工厂函数：为每个工具生成一个测试用例。"""

    @pytest.mark.asyncio
    @pytest.mark.api_health
    async def _test():
        result = ApiCheckResult(
            tool_name=tool_name,
            display_name=display_name,
            category=category,
            primary_api=primary_api,
        )

        output, latency, error = await _run_with_timeout(tool_func(test_input))
        result.latency_ms = round(latency, 1)

        if error:
            result.status = "UNHEALTHY"
            result.error_message = error
            CHECKS.append(result)
            # 不 assert False — 允许失败
            return result

        primary_ok, fallback_ok = _check_result(output, tool_name=tool_name)

        if primary_ok:
            result.status = "HEALTHY"
            result.fallback_used = False
        elif fallback_ok:
            result.status = "DEGRADED"
            result.fallback_used = True
            # 从输出中截取关键信息作为 note
            result.note = _extract_fallback_note(output)
        else:
            result.status = "UNHEALTHY"
            result.error_message = output[:200]

        if extra_checks:
            extra_info = extra_checks(output)
            if extra_info.get("note"):
                result.note = extra_info["note"]

        CHECKS.append(result)
        return result

    _test.__name__ = f"test_api_{tool_name}"
    return _test


def _extract_fallback_note(output: str) -> str:
    """从输出中提取降级信息。"""
    for line in output.split("\n"):
        if "[SOURCE]:" in line:
            return line.replace("[SOURCE]:", "").strip()
        if "降级" in line:
            return line.strip()
    return "已降级但未识别具体路径"


# ──────────────────────────────────────────────
# 工具导入
# ──────────────────────────────────────────────

from agent.tools.web_search import web_search
from agent.tools.wikipedia_lookup import wikipedia_lookup
from agent.tools.source_verifier import source_verifier
from agent.tools.wikidata_lookup import wikidata_lookup
from agent.tools.official_statistics import macro_statistics_global
from agent.tools.stock_quotes import stock_market_quotes
from agent.tools.pubmed_search import pubmed_scientific_search
from agent.tools.consumer_health import consumer_health_verifier
from agent.tools.academic_search import academic_paper_search
from agent.tools.arxiv_search import preprint_arxiv_search
from agent.tools.patent_lookup import patent_status_lookup
from agent.tools.fact_check import fact_check_domestic, fact_check_global

# cross_reference 需要预热
from agent.tools.cross_reference import cross_reference, prepare_cross_reference_context


# ──────────────────────────────────────────────
# 定义所有工具的测试
# ──────────────────────────────────────────────

# 1. web_search — Tavily → DuckDuckGo
test_api_web_search = _make_test(
    "web_search", "Web Search", "通用", "Tavily Search API",
    web_search, "Python programming language latest version 2024",
)

# 2. wikipedia_lookup — Wikipedia Action API
test_api_wikipedia_lookup = _make_test(
    "wikipedia_lookup", "Wikipedia Lookup", "通用", "Wikipedia Action API",
    wikipedia_lookup, "Python (programming language)",
)

# 3. source_verifier — readability-lxml + HTTP
test_api_source_verifier = _make_test(
    "source_verifier", "Source Verifier", "通用", "readability-lxml (HTTP)",
    source_verifier, "https://httpbin.org/html",
)

# 4. wikidata_lookup — Wikidata API
test_api_wikidata_lookup = _make_test(
    "wikidata_lookup", "Wikidata Lookup", "通用/科技", "Wikidata API",
    wikidata_lookup, "Python programming language",
)

# 5. cross_reference — sentence-transformers (HuggingFace)
@pytest.mark.asyncio
@pytest.mark.api_health
async def test_api_cross_reference():
    result = ApiCheckResult(
        tool_name="cross_reference",
        display_name="Cross Reference",
        category="通用",
        primary_api="sentence-transformers (HuggingFace)",
    )

    try:
        t0 = time.monotonic()
        # 先预热上下文
        await prepare_cross_reference_context("Python is a great programming language. Python was created by Guido van Rossum.")
        # 再测试语义检索
        output, latency, error = await _run_with_timeout(
            cross_reference("Python was created by Guido van Rossum"), timeout=30.0
        )
        result.latency_ms = round((time.monotonic() - t0) * 1000, 1)

        if error:
            result.status = "UNHEALTHY"
            result.error_message = error
        elif "工具执行失败" in output:
            result.status = "UNHEALTHY"
            result.error_message = output[:200]
        elif "未找到达到阈值" in output or "未发现明确矛盾" in output:
            # 模型正常加载并推理，只是没找到矛盾 — 算健康
            result.status = "HEALTHY"
            result.note = "模型加载+推理正常，无矛盾发现"
        else:
            result.status = "HEALTHY"
    except Exception as exc:
        result.status = "UNHEALTHY"
        result.error_message = f"{type(exc).__name__}: {exc}"

    CHECKS.append(result)
    return result


# 6. macro_statistics_global — World Bank API → 本地缓存
test_api_macro_statistics = _make_test(
    "macro_statistics_global", "Macro Statistics", "金融", "World Bank API",
    macro_statistics_global, "CN GDP 2023",
)

# 7. stock_market_quotes — Yahoo Finance API
test_api_stock_market_quotes = _make_test(
    "stock_market_quotes", "Stock Quotes", "金融", "Yahoo Finance API",
    stock_market_quotes, "AAPL",
)

# 8. pubmed_scientific_search — PubMed eutils
test_api_pubmed = _make_test(
    "pubmed_scientific_search", "PubMed Search", "医疗", "PubMed eutils API",
    pubmed_scientific_search, "aspirin cardiovascular disease",
)

# 9. consumer_health_verifier — 定向搜索 → web_search
test_api_consumer_health = _make_test(
    "consumer_health_verifier", "Consumer Health", "医疗", "定向健康网站搜索 (→Tavily/DDG)",
    consumer_health_verifier, "vitamin C common cold prevention",
)

# 10. academic_paper_search — Semantic Scholar
test_api_academic_search = _make_test(
    "academic_paper_search", "Academic Search", "科技", "Semantic Scholar API",
    academic_paper_search, "attention is all you need",
)

# 11. preprint_arxiv_search — arXiv API
test_api_arxiv_search = _make_test(
    "preprint_arxiv_search", "arXiv Search", "科技", "arXiv API",
    preprint_arxiv_search, "attention is all you need",
)

# 12. patent_status_lookup — 定向专利搜索 → web_search
test_api_patent_lookup = _make_test(
    "patent_status_lookup", "Patent Lookup", "科技", "定向专利数据库搜索 (→Tavily/DDG)",
    patent_status_lookup, "US10162738B2 artificial intelligence",
)

# 13. fact_check_domestic — 国内辟谣 → web_search
test_api_fact_check_domestic = _make_test(
    "fact_check_domestic", "Fact Check (Domestic)", "新闻", "定向辟谣平台搜索 (→Tavily/DDG)",
    fact_check_domestic, "最新交通新规下周执行",
)

# 14. fact_check_global — Google Fact Check API → 定向搜索
def _check_google_fact_check(output: str) -> dict[str, Any]:
    """检查 Google Fact Check API 是否被使用。"""
    if "[SOURCE]: Google Fact Check API" in output:
        return {"note": "主 API (Google Fact Check Tools) 正常"}
    return {}


test_api_fact_check_global = _make_test(
    "fact_check_global", "Fact Check (Global)", "新闻", "Google Fact Check Tools API",
    fact_check_global, "COVID-19 vaccine causes autism",
    extra_checks=_check_google_fact_check,
)


# ──────────────────────────────────────────────
# 报告生成
# ──────────────────────────────────────────────


_STATUS_EMOJI = {
    "HEALTHY": "✅",
    "DEGRADED": "⚠️",
    "UNHEALTHY": "❌",
    "UNTESTED": "⏳",
}


def _generate_report_table() -> str:
    """生成 API 健康报告表。"""
    lines = []
    sep = "=" * 110
    thin_sep = "-" * 110

    lines.append("")
    lines.append(sep)
    lines.append("                    VeriFact — API 健康检查报告")
    lines.append(f"                    生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(sep)
    lines.append("")

    # 表头
    header = (
        f"{'状态':<6} {'工具名称':<26} {'类别':<6} {'主 API':<36} {'延迟':<10} {'备注'}"
    )
    lines.append(header)
    lines.append(thin_sep)

    # 每条记录
    for c in CHECKS:
        emoji = _STATUS_EMOJI.get(c.status, "❓")
        latency_str = f"{c.latency_ms:.0f}ms" if c.latency_ms > 0 else "N/A"
        note = c.note or (c.error_message[:60] if c.error_message else "")
        lines.append(
            f"{emoji:<5}  {c.display_name:<24} {c.category:<6} {c.primary_api:<36} {latency_str:<10} {note}"
        )

    lines.append(thin_sep)

    # 统计
    healthy = sum(1 for c in CHECKS if c.status == "HEALTHY")
    degraded = sum(1 for c in CHECKS if c.status == "DEGRADED")
    unhealthy = sum(1 for c in CHECKS if c.status == "UNHEALTHY")
    total = len(CHECKS)

    lines.append(f"  合计: {total} 个工具 | ✅ HEALTHY: {healthy} | ⚠️ DEGRADED: {degraded} | ❌ UNHEALTHY: {unhealthy}")
    lines.append("")

    # 分类汇总
    categories: dict[str, list[ApiCheckResult]] = {}
    for c in CHECKS:
        categories.setdefault(c.category, []).append(c)

    lines.append("  分类汇总:")
    for cat, items in sorted(categories.items()):
        h = sum(1 for i in items if i.status == "HEALTHY")
        d = sum(1 for i in items if i.status == "DEGRADED")
        u = sum(1 for i in items if i.status == "UNHEALTHY")
        lines.append(f"    {cat}: {h}/{len(items)} 健康, {d} 降级, {u} 失败")

    lines.append("")

    # Key 配置状态
    lines.append("  API Key 配置状态:")
    key_checks = [
        ("ANTHROPIC_API_KEY", "核心 LLM（Claude Sonnet）"),
        ("GLM_API_KEY", "领域路由 LLM（GLM-4-Flash）"),
        ("TAVILY_API_KEY", "Web Search (Tavily)"),
        ("GOOGLE_FACT_CHECK_API_KEY", "Google Fact Check Tools"),
        ("WIKI_USER_AGENT", "Wikipedia User-Agent"),
        ("HF_ENDPOINT", "HuggingFace 镜像"),
    ]
    for key_name, desc in key_checks:
        val = os.getenv(key_name)
        if val:
            masked = val[:12] + "..." if len(val) > 12 else val
            lines.append(f"    ✅ {key_name} = {masked}  ({desc})")
        else:
            lines.append(f"    ❌ {key_name} 未配置  ({desc})")

    lines.append("")
    lines.append("  图例: ✅ HEALTHY=主API正常 | ⚠️ DEGRADED=降级可用 | ❌ UNHEALTHY=所有层级失败")
    lines.append(sep)
    lines.append("")

    return "\n".join(lines)


# ──────────────────────────────────────────────
# Pytest 钩子：所有测试完成后打印报告
# ──────────────────────────────────────────────


@pytest.fixture(scope="session", autouse=True)
def _print_report_after_all_tests():
    """在所有 API 健康测试结束后打印汇总报告。"""
    yield
    # 只对 api_health 标记的测试打印报告
    if CHECKS:
        report = _generate_report_table()
        print(report, flush=True)


# ──────────────────────────────────────────────
# 独立运行入口
# ──────────────────────────────────────────────

async def _run_all_checks():
    """非 pytest 方式运行所有检查。"""
    from agent.tools.cross_reference import prepare_cross_reference_context, cross_reference

    checks_to_run: list[tuple[str, Callable, str]] = [
        ("web_search", web_search, "Python programming language latest version"),
        ("wikipedia_lookup", wikipedia_lookup, "Python (programming language)"),
        ("source_verifier", source_verifier, "https://httpbin.org/html"),
        ("wikidata_lookup", wikidata_lookup, "Python programming language"),
        ("macro_statistics_global", macro_statistics_global, "CN GDP 2023"),
        ("stock_market_quotes", stock_market_quotes, "AAPL"),
        ("pubmed_scientific_search", pubmed_scientific_search, "aspirin cardiovascular disease"),
        ("consumer_health_verifier", consumer_health_verifier, "vitamin C common cold prevention"),
        ("academic_paper_search", academic_paper_search, "attention is all you need"),
        ("preprint_arxiv_search", preprint_arxiv_search, "attention is all you need"),
        ("patent_status_lookup", patent_status_lookup, "US10162738B2 artificial intelligence"),
        ("fact_check_domestic", fact_check_domestic, "最新交通新规下周执行"),
        ("fact_check_global", fact_check_global, "COVID-19 vaccine causes autism"),
    ]

    # Display names mapping
    display_names = {
        "web_search": ("Web Search", "通用", "Tavily Search API"),
        "wikipedia_lookup": ("Wikipedia Lookup", "通用", "Wikipedia Action API"),
        "source_verifier": ("Source Verifier", "通用", "readability-lxml (HTTP)"),
        "wikidata_lookup": ("Wikidata Lookup", "通用/科技", "Wikidata API"),
        "macro_statistics_global": ("Macro Statistics", "金融", "World Bank API"),
        "stock_market_quotes": ("Stock Quotes", "金融", "Yahoo Finance API"),
        "pubmed_scientific_search": ("PubMed Search", "医疗", "PubMed eutils API"),
        "consumer_health_verifier": ("Consumer Health", "医疗", "定向健康网站搜索"),
        "academic_paper_search": ("Academic Search", "科技", "Semantic Scholar API"),
        "preprint_arxiv_search": ("arXiv Search", "科技", "arXiv API"),
        "patent_status_lookup": ("Patent Lookup", "科技", "定向专利数据库搜索"),
        "fact_check_domestic": ("Fact Check (Domestic)", "新闻", "定向辟谣平台搜索"),
        "fact_check_global": ("Fact Check (Global)", "新闻", "Google Fact Check Tools API"),
    }

    print("\n⏳ 正在测试 14 个工具的外部 API 连通性（预计 1-2 分钟）...\n")

    for tool_name, func, test_input in checks_to_run:
        display, category, api = display_names[tool_name]
        result = ApiCheckResult(
            tool_name=tool_name,
            display_name=display,
            category=category,
            primary_api=api,
        )

        output, latency, error = await _run_with_timeout(func(test_input))
        result.latency_ms = round(latency, 1)

        if error:
            result.status = "UNHEALTHY"
            result.error_message = error
        else:
            primary_ok, fallback_ok = _check_result(output, tool_name=tool_name)
            if primary_ok:
                result.status = "HEALTHY"
            elif fallback_ok:
                result.status = "DEGRADED"
                result.fallback_used = True
                result.note = _extract_fallback_note(output)
            else:
                result.status = "UNHEALTHY"
                result.error_message = output[:200]

        CHECKS.append(result)
        emoji = _STATUS_EMOJI[result.status]
        print(f"  {emoji} {display:<26} {result.latency_ms:>7.0f}ms  {result.status}")

    # Cross reference special handling
    print("  ⏳ Cross Reference           (加载 sentence-transformers 模型...)")
    cr_result = ApiCheckResult(
        tool_name="cross_reference",
        display_name="Cross Reference",
        category="通用",
        primary_api="sentence-transformers (HuggingFace)",
    )
    try:
        t0 = time.monotonic()
        await prepare_cross_reference_context(
            "Python is a great programming language. Python was created by Guido van Rossum."
        )
        output, latency, error = await _run_with_timeout(
            cross_reference("Python was created by Guido van Rossum"), timeout=30.0
        )
        cr_result.latency_ms = round((time.monotonic() - t0) * 1000, 1)
        if error:
            cr_result.status = "UNHEALTHY"
            cr_result.error_message = error
        elif "工具执行失败" in output:
            cr_result.status = "UNHEALTHY"
            cr_result.error_message = output[:200]
        else:
            cr_result.status = "HEALTHY"
            cr_result.note = "模型加载+推理正常"
    except Exception as exc:
        cr_result.status = "UNHEALTHY"
        cr_result.error_message = f"{type(exc).__name__}: {exc}"
    CHECKS.append(cr_result)
    emoji = _STATUS_EMOJI[cr_result.status]
    print(f"  {emoji} Cross Reference             {cr_result.latency_ms:>7.0f}ms  {cr_result.status}")

    # 打印报告
    report = _generate_report_table()
    print(report)


if __name__ == "__main__":
    asyncio.run(_run_all_checks())
