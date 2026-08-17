"""
agent/tools/registry.py

工具注册表。B 实现 web_search / wikipedia_lookup / source_verifier，
D 实现 cross_reference，各自在此文件添加一行注册。

所有工具必须满足：
  - async 函数
  - 接受单个 str 参数（工具输入）
  - 返回 str（工具输出）
  - 内部捕获所有异常，不向外传播，失败时返回统一错误字符串
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .cross_reference import cross_reference


@dataclass
class ToolSpec:
    name: str
    description: str        # 给 Claude 看，影响 Agent 选工具的准确性，由实现者填写
    input_schema: dict      # JSON Schema，描述 input 字段
    func: Callable          # async def func(input: str) -> str
    domains: list[str] = field(default_factory=list)  # 适用领域，例如 ["medical", "general"]

    def to_claude_tool(self) -> dict:
        """转换为 Anthropic tool use 所需的格式。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def get_allowed_tools(active_domains: list[str]) -> list[ToolSpec]:
    """
    根据被激活的领域列表，返回可用的工具列表。
    默认包含 'general' 领域的工具。
    """
    allowed = []
    for spec in TOOL_REGISTRY.values():
        if not spec.domains or any(d in active_domains for d in spec.domains) or "general" in spec.domains:
            allowed.append(spec)
    return allowed


# ---------------------------------------------------------------------------
# 工具注册表
# B 负责实现并注册：web_search、wikipedia_lookup、source_verifier
# D 负责实现并注册：cross_reference
# ---------------------------------------------------------------------------


from .web_search import web_search
from .wikipedia_lookup import wikipedia_lookup
from .source_verifier import source_verifier
from .wikidata_lookup import wikidata_lookup
from .official_statistics import macro_statistics_global
from .stock_quotes import stock_market_quotes
from .pubmed_search import pubmed_scientific_search
from .consumer_health import consumer_health_verifier
from .academic_search import academic_paper_search
from .arxiv_search import preprint_arxiv_search
from .patent_lookup import patent_status_lookup
from .fact_check import fact_check_domestic, fact_check_global


TOOL_REGISTRY: dict[str, ToolSpec] = {
    "web_search": ToolSpec(
        name="web_search",
        description="搜索互联网，核实事实性声明。输入：提取声明中的几个核心关键词（空格分隔），绝对不要把整段长句子直接输入！",
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
        func=web_search,
        domains=["general"],
    ),
    "wikipedia_lookup": ToolSpec(
        name="wikipedia_lookup",
        description="查询维基百科词条，获取背景知识。输入必须是简短的核心实体名词，绝对不要输入完整的句子！",
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
        func=wikipedia_lookup,
        domains=["general"],
    ),
    "source_verifier": ToolSpec(
        name="source_verifier",
        description="验证声明的来源可信度。输入：声明文本或来源 URL。",
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
        func=source_verifier,
        domains=["general"],
    ),
    "cross_reference": ToolSpec(
        name="cross_reference",
        description=(
            "检索当前文章中与声明语义相近的句子，并判断是否存在内部矛盾。"
            "输入可以是原始声明文本，或 JSON 字符串："
            '{"claim": "...", "top_k": 5, "threshold": 0.6}。'
        ),
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
        func=cross_reference,
        domains=["general"],
    ),
    "wikidata_lookup": ToolSpec(
        name="wikidata_lookup",
        description="查询维基数据实体，获取其结构化属性信息。适合验证实体属性、成立年份、国籍、地理位置等。输入：简短的核心实体词。",
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
        func=wikidata_lookup,
        domains=["general", "technology"],
    ),
    "macro_statistics_global": ToolSpec(
        name="macro_statistics_global",
        description="验证全球宏观经济与社会统计数据。适合核对国家层面的GDP增速、通货膨胀率（CPI）、失业率等指标。输入：国家和年份以及指标关键字（例如 'CN GDP 2023' 或 'US CPI 2022'）。",
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
        func=macro_statistics_global,
        domains=["finance"],
    ),
    "stock_market_quotes": ToolSpec(
        name="stock_market_quotes",
        description="查询全球（含A股、美股、港股）个股实时行情。适合核对最新股价、涨跌幅、市值等财经新闻数据。输入：股票代码或公司中文/英文名称（例如 '贵州茅台' 或 'AAPL'）。",
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
        func=stock_market_quotes,
        domains=["finance"],
    ),
    "pubmed_scientific_search": ToolSpec(
        name="pubmed_scientific_search",
        description="搜索 PubMed 医学文献数据库。适合核对专业医药学研究、药物临床试验、医学分子机理、前沿学术观点等断言。输入：医学健康相关的英文学术关键词。",
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
        func=pubmed_scientific_search,
        domains=["medical"],
    ),
    "consumer_health_verifier": ToolSpec(
        name="consumer_health_verifier",
        description="核查日常健康常识、日常养生、常见食物功效、非处方药副作用、通俗病症咨询等声明。输入：健康生活相关的中英文核心关键词。",
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
        func=consumer_health_verifier,
        domains=["medical"],
    ),
    "academic_paper_search": ToolSpec(
        name="academic_paper_search",
        description="通过 Semantic Scholar 学术搜索引擎查询已发表的同行评议文献。适合核对权威学术观点、正式论文发表、被引频次及核心摘要。输入：英文学术关键词或论文标题。",
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
        func=academic_paper_search,
        domains=["technology"],
    ),
    "preprint_arxiv_search": ToolSpec(
        name="preprint_arxiv_search",
        description="检索 arXiv 预印本论文数据库。适合核查最新的、未经同行评审的预印本学术文章的发表状态与元数据。输入：论文标题或学术关键词。",
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
        func=preprint_arxiv_search,
        domains=["technology"],
    ),
    "patent_status_lookup": ToolSpec(
        name="patent_status_lookup",
        description="查询全球专利法律状态与授权状态。适合核对某项宣称技术是否真实拥有专利保护以及是申请中还是已授权。输入：发明人或专利关键词。",
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
        func=patent_status_lookup,
        domains=["technology"],
    ),
    "fact_check_domestic": ToolSpec(
        name="fact_check_domestic",
        description="检索国内权威辟谣平台记录。适合核查国内时政、社会谣言、民生流言。输入：完整的待查事实性声明。",
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
        func=fact_check_domestic,
        domains=["news_policy", "general"],
    ),
    "fact_check_global": ToolSpec(
        name="fact_check_global",
        description="检索国际权威辟谣与事实核查平台记录。优先使用官方接口，适合核查国际新闻、海外社交媒体传闻。输入：待查声明文本或英文事实描述。",
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
        func=fact_check_global,
        domains=["news_policy", "general"],
    ),
}
