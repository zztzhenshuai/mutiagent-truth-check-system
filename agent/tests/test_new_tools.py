import pytest
import httpx
from unittest.mock import AsyncMock, patch, MagicMock
from agent.tools.registry import get_allowed_tools, TOOL_REGISTRY
from agent.tools.wikidata_lookup import wikidata_lookup
from agent.tools.official_statistics import macro_statistics_global
from agent.tools.stock_quotes import stock_market_quotes
from agent.tools.pubmed_search import pubmed_scientific_search
from agent.tools.consumer_health import consumer_health_verifier
from agent.tools.academic_search import academic_paper_search
from agent.tools.arxiv_search import preprint_arxiv_search
from agent.tools.patent_lookup import patent_status_lookup
from agent.tools.fact_check import fact_check_domestic, fact_check_global

# ==========================================
# 1. 验证 get_allowed_tools 路由过滤逻辑
# ==========================================
def test_get_allowed_tools():
    # 领域为 medical，应该允许通用工具和医学工具
    medical_tools = get_allowed_tools(["medical"])
    names = [t.name for t in medical_tools]
    assert "web_search" in names
    assert "wikipedia_lookup" in names
    assert "source_verifier" in names
    assert "cross_reference" in names
    assert "pubmed_scientific_search" in names
    assert "consumer_health_verifier" in names
    assert "macro_statistics_global" not in names
    assert "stock_market_quotes" not in names

    # 领域为 finance，应该允许通用工具和金融工具
    finance_tools = get_allowed_tools(["finance"])
    names = [t.name for t in finance_tools]
    assert "web_search" in names
    assert "macro_statistics_global" in names
    assert "stock_market_quotes" in names
    assert "pubmed_scientific_search" not in names

    # 领域为 technology，应该允许通用工具和科技工具
    tech_tools = get_allowed_tools(["technology"])
    names = [t.name for t in tech_tools]
    assert "web_search" in names
    assert "academic_paper_search" in names
    assert "preprint_arxiv_search" in names
    assert "patent_status_lookup" in names
    assert "wikidata_lookup" in names
    assert "pubmed_scientific_search" not in names

# ==========================================
# 2. 测试 wikidata_lookup
# ==========================================
@pytest.mark.asyncio
async def test_wikidata_lookup_success():
    # 模拟 HTTP 客户端响应
    mock_search_resp = MagicMock()
    mock_search_resp.status_code = 200
    mock_search_resp.json.return_value = {
        "search": [{"id": "Q1151110", "label": "南京大学", "description": "中国大学"}]
    }

    mock_entity_resp = MagicMock()
    mock_entity_resp.status_code = 200
    mock_entity_resp.raise_for_status = MagicMock()
    mock_entity_resp.json.return_value = {
        "entities": {
            "Q1151110": {
                "labels": {"zh": {"value": "南京大学"}},
                "descriptions": {"zh": {"value": "中国一流大学"}},
                "claims": {
                    "P17": [{
                        "mainsnak": {
                            "datavalue": {
                                "type": "wikibase-entityid",
                                "value": {"id": "Q148"}
                            }
                        }
                    }],
                    "P571": [{
                        "mainsnak": {
                            "datavalue": {
                                "type": "time",
                                "value": {"time": "+1902-00-00T00:00:00Z"}
                            }
                        }
                    }]
                }
            }
        }
    }

    mock_label_resp = MagicMock()
    mock_label_resp.status_code = 200
    mock_label_resp.json.return_value = {
        "entities": {
            "Q148": {"labels": {"zh": {"value": "中华人民共和国"}}}
        }
    }

    with patch("httpx.AsyncClient.get") as mock_get:
        # 按顺序模拟三次请求
        mock_get.side_effect = [mock_search_resp, mock_entity_resp, mock_label_resp]
        
        result = await wikidata_lookup("南京大学")
        assert "[SOURCE]: Wikidata" in result
        assert "[ENTITY]: 南京大学 (Q1151110)" in result
        assert "[DESCRIPTION]: 中国一流大学" in result
        assert "国家/地区: 中华人民共和国" in result
        assert "成立时间: 1902" in result

# ==========================================
# 3. 测试 macro_statistics_global 与 stock_market_quotes
# ==========================================
@pytest.mark.asyncio
async def test_macro_statistics_global_online():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"page": 1},
        [{"value": 5.2, "date": "2023"}]
    ]

    with patch("httpx.AsyncClient.get", return_value=mock_resp):
        result = await macro_statistics_global("CN GDP 2023")
        assert "[SOURCE]: World Bank API" in result
        assert "[COUNTRY]: China (CN)" in result
        assert "[VALUE]: 5.20%" in result

@pytest.mark.asyncio
async def test_macro_statistics_global_fallback():
    # 模拟请求异常触发本地缓存降级
    with patch("httpx.AsyncClient.get", side_effect=httpx.RequestError("Timeout")):
        result = await macro_statistics_global("CN GDP 2023")
        assert "Local Static Cache" in result
        assert "[COUNTRY]: China (CN)" in result
        assert "[VALUE]: 5.20%" in result  # 从 datasets/world_bank_cache.json 读取的数值

@pytest.mark.asyncio
async def test_stock_market_quotes_success():
    mock_search_resp = MagicMock()
    mock_search_resp.status_code = 200
    mock_search_resp.json.return_value = {
        "quotes": [{"symbol": "600519.SS", "longName": "Kweichow Moutai Co., Ltd."}]
    }

    mock_quote_resp = MagicMock()
    mock_quote_resp.status_code = 200
    mock_quote_resp.json.return_value = {
        "quoteResponse": {
            "result": [{
                "longName": "Kweichow Moutai Co., Ltd.",
                "regularMarketPrice": 1700.0,
                "currency": "CNY",
                "regularMarketChange": 15.5,
                "regularMarketChangePercent": 0.92,
                "marketCap": 2100000000000,
                "fullExchangeName": "Shanghai"
            }]
        }
    }

    with patch("httpx.AsyncClient.get") as mock_get:
        mock_get.side_effect = [mock_search_resp, mock_quote_resp]
        result = await stock_market_quotes("贵州茅台")
        assert "[SOURCE]: Yahoo Finance API" in result
        assert "[COMPANY]: Kweichow Moutai Co., Ltd. (600519.SS)" in result
        assert "[PRICE]: 1700.00 CNY" in result
        assert "[CHANGE]: +15.50 (+0.92%)" in result
        assert "[MARKET CAP]: 2.10万亿 CNY" in result

@pytest.mark.asyncio
async def test_stock_market_quotes_fallback():
    # 模拟接口搜索异常直接降级到网页搜索
    with patch("httpx.AsyncClient.get", side_effect=httpx.RequestError("Timeout")), \
         patch("agent.tools.stock_quotes.web_search", return_value="网页搜到的股价信息: 1700元") as mock_web:
        result = await stock_market_quotes("贵州茅台")
        assert "[SOURCE]: General Web Search (Stock Quote Fallback)" in result
        assert "网页搜到的股价信息" in result
        mock_web.assert_called_once_with("贵州茅台 股票价格 股价 行情", clean_query=True)

# ==========================================
# 4. 测试 pubmed_search
# ==========================================
@pytest.mark.asyncio
async def test_pubmed_scientific_search():
    mock_search_resp = MagicMock()
    mock_search_resp.status_code = 200
    mock_search_resp.json.return_value = {
        "esearchresult": {"idlist": ["123456"]}
    }

    mock_summary_resp = MagicMock()
    mock_summary_resp.status_code = 200
    mock_summary_resp.json.return_value = {
        "result": {
            "123456": {
                "title": "Aspirin for prevention of cardiovascular disease",
                "source": "New England Journal of Medicine",
                "pubdate": "2023 Nov 10",
                "authors": [{"name": "Smith J"}, {"name": "Doe J"}]
            }
        }
    }

    with patch("httpx.AsyncClient.get") as mock_get:
        mock_get.side_effect = [mock_search_resp, mock_summary_resp]
        result = await pubmed_scientific_search("aspirin cardiovascular")
        assert "[SOURCE]: PubMed Database" in result
        assert "[TITLE]: Aspirin for prevention of cardiovascular disease" in result
        assert "[JOURNAL]: New England Journal of Medicine" in result
        assert "[YEAR]: 2023" in result

# ==========================================
# 4.5. 测试 consumer_health_verifier
# ==========================================
@pytest.mark.asyncio
async def test_consumer_health_verifier_success():
    with patch("agent.tools.consumer_health.web_search", return_value="丁香医生科普: 维生素C不能根治癌症。") as mock_web:
        result = await consumer_health_verifier("维生素C 治疗癌症")
        assert "[SOURCE]: Authoritative Consumer Health Portals (Direct Search)" in result
        assert "[CREDIBILITY]: High" in result
        assert "丁香医生科普" in result
        mock_web.assert_called_once()
        args, kwargs = mock_web.call_args
        assert "site:who.int" in args[0]
        assert "site:dxy.com" in args[0]
        assert kwargs.get("clean_query") is False

@pytest.mark.asyncio
async def test_consumer_health_verifier_fallback():
    with patch("agent.tools.consumer_health.web_search") as mock_web:
        mock_web.side_effect = ["网络搜索未找到相关结果", "通用搜索结果: 维生素C抗癌没有依据"]
        result = await consumer_health_verifier("维生素C 治疗癌症")
        assert "[SOURCE]: General Web Search (Consumer Health Fallback)" in result
        assert "[CREDIBILITY]: Medium" in result
        assert "通用搜索结果" in result
        assert mock_web.call_count == 2

# ==========================================
# 5. 测试 academic_paper_search 与科技新工具
# ==========================================
@pytest.mark.asyncio
async def test_academic_paper_search():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [
            {
                "title": "Attention is All You Need",
                "year": 2017,
                "citationCount": 120000,
                "venue": "NeurIPS",
                "abstract": "We propose a new simple network architecture, the Transformer...",
                "url": "https://www.semanticscholar.org/paper/123"
            }
        ]
    }

    with patch("httpx.AsyncClient.get", return_value=mock_resp):
        result = await academic_paper_search("Attention is All You Need")
        assert "[SOURCE]: Semantic Scholar Academic Database" in result
        assert "[TITLE]: Attention is All You Need" in result
        assert "[CITATION COUNT]: 120000" in result
        assert "[VENUE]: NeurIPS" in result

@pytest.mark.asyncio
async def test_preprint_arxiv_search_success():
    xml_content = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>http://arxiv.org/abs/1706.03762v7</id>
        <published>2017-06-12T13:00:00Z</published>
        <title>Attention Is All You Need</title>
        <summary>We propose a new simple network architecture...</summary>
        <author>
          <name>Vaswani Ashish</name>
        </author>
      </entry>
    </feed>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = xml_content.encode('utf-8')

    with patch("httpx.AsyncClient.get", return_value=mock_resp):
        result = await preprint_arxiv_search("Attention Is All You Need")
        assert "[SOURCE]: arXiv API" in result
        assert "[TITLE]: Attention Is All You Need" in result
        assert "[YEAR]: 2017" in result
        assert "[CREDIBILITY]: Medium (Unpublished Preprint" in result

@pytest.mark.asyncio
async def test_preprint_arxiv_search_fallback():
    # 模拟 API 异常触发 web_search 降级
    with patch("httpx.AsyncClient.get", side_effect=httpx.RequestError("Network error")), \
         patch("agent.tools.arxiv_search.web_search", return_value="ArXiv Web Result") as mock_web:
        result = await preprint_arxiv_search("Attention Is All You Need")
        assert "arXiv Fallback" in result
        assert "ArXiv Web Result" in result
        mock_web.assert_called_once_with("Attention Is All You Need site:arxiv.org", clean_query=False)

@pytest.mark.asyncio
async def test_patent_status_lookup_success():
    with patch("agent.tools.patent_lookup.web_search", return_value="Google Patents: US99999B2 Granted") as mock_web:
        result = await patent_status_lookup("US99999B2")
        assert "[SOURCE]: Authoritative Global Patent Databases (Direct Search)" in result
        assert "[CREDIBILITY]: High (Official Patent Registry)" in result
        assert "Google Patents: US99999B2" in result
        mock_web.assert_called_once()
        args, kwargs = mock_web.call_args
        assert "site:patents.google.com" in args[0]
        assert kwargs.get("clean_query") is False

@pytest.mark.asyncio
async def test_patent_status_lookup_fallback():
    with patch("agent.tools.patent_lookup.web_search") as mock_web:
        mock_web.side_effect = ["网络搜索未找到相关结果", "通用网页搜索: 找到相关专利申请"]
        result = await patent_status_lookup("无结果专利")
        assert "[SOURCE]: General Web Search (Patent Status Fallback)" in result
        assert "[CREDIBILITY]: Medium" in result
        assert "通用网页搜索" in result
        assert mock_web.call_count == 2

# ==========================================
# 6. 测试 fact_check_domestic 与 fact_check_global
# ==========================================
@pytest.mark.asyncio
async def test_fact_check_domestic_success():
    with patch("agent.tools.fact_check.web_search", return_value="腾讯较真: 此条消息为虚假传言") as mock_web:
        result = await fact_check_domestic("最新交通新规下周执行")
        assert "[SOURCE]: Authoritative Domestic Fact Check Portals (Direct Search)" in result
        assert "[CREDIBILITY]: High" in result
        assert "腾讯较真" in result
        mock_web.assert_called_once()
        args, kwargs = mock_web.call_args
        assert "site:piyao.org.cn" in args[0]
        assert "site:fact.qq.com" in args[0]
        assert kwargs.get("clean_query") is False

@pytest.mark.asyncio
async def test_fact_check_domestic_fallback():
    with patch("agent.tools.fact_check.web_search") as mock_web:
        mock_web.side_effect = ["网络搜索未找到相关结果", "通用搜索结果: 该传闻已被官方否定"]
        result = await fact_check_domestic("最新交通新规下周执行")
        assert "[SOURCE]: General Web Search (Domestic Fact Check Fallback)" in result
        assert "[CREDIBILITY]: Medium" in result
        assert "通用搜索结果" in result
        assert mock_web.call_count == 2

@pytest.mark.asyncio
async def test_fact_check_global_api():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "claims": [
            {
                "text": "The earth is flat.",
                "claimant": "Flat Earth Society",
                "claimReview": [
                    {
                        "publisher": {"name": "Snopes"},
                        "textualRating": "False",
                        "title": "Is the Earth Flat?",
                        "url": "https://snopes.com/earth-flat"
                    }
                ]
            }
        ]
    }

    with patch("os.getenv", return_value="FAKE_GOOGLE_KEY"), \
         patch("httpx.AsyncClient.get", return_value=mock_resp):
        result = await fact_check_global("Is the earth flat")
        assert "[SOURCE]: Google Fact Check API" in result
        assert "[原始宣称]: The earth is flat." in result
        assert "[核查结论]: False" in result
        assert "[核查机构]: Snopes" in result

@pytest.mark.asyncio
async def test_fact_check_global_fallback():
    # 模拟未配置 API Key 时自动降级到定向检索
    with patch("os.getenv", return_value=None), \
         patch("agent.tools.fact_check.web_search", return_value="Snopes output: Fake news") as mock_web:
        result = await fact_check_global("The earth is flat")
        assert "[SOURCE]: Authoritative Global Fact Check Portals (Direct Search)" in result
        assert "Snopes output" in result
        mock_web.assert_called_once()
        args, kwargs = mock_web.call_args
        assert "site:snopes.com" in args[0]
        assert kwargs.get("clean_query") is False

# ==========================================
# 7. 针对边界情况与异常鲁棒性的额外测试 (Edge Case & Robustness Tests)
# ==========================================

@pytest.mark.asyncio
async def test_tools_empty_input_handling():
    # 测试所有工具输入为空或仅包含空白字符时的处理
    for tool_func in [wikidata_lookup, macro_statistics_global, stock_market_quotes, pubmed_scientific_search, consumer_health_verifier, academic_paper_search, preprint_arxiv_search, patent_status_lookup, fact_check_domestic, fact_check_global]:
        res_empty = await tool_func("")
        assert "输入为空" in res_empty or "执行失败" in res_empty
        res_spaces = await tool_func("   ")
        assert "输入为空" in res_spaces or "执行失败" in res_spaces

@pytest.mark.asyncio
async def test_macro_statistics_global_invalid_query():
    # 测试 macro_statistics_global 面对无法提取关键信息的无效输入时的行为 (应使用默认的中国GDP 2023并成功返回)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"page": 1},
        [{"value": 5.2, "date": "2023"}]
    ]
    with patch("httpx.AsyncClient.get", return_value=mock_resp):
        result = await macro_statistics_global("random garbage text 2023")
        assert "[COUNTRY]: China (CN)" in result
        assert "[INDICATOR]: GDP growth" in result
        assert "[VALUE]: 5.20%" in result

@pytest.mark.asyncio
async def test_academic_paper_search_rate_limit():
    # 模拟 Semantic Scholar 返回 429 频控
    mock_resp = MagicMock()
    mock_resp.status_code = 429
    with patch("httpx.AsyncClient.get", return_value=mock_resp):
        result = await academic_paper_search("Deep Learning")
        assert "触发 Semantic Scholar 频控限制" in result

@pytest.mark.asyncio
async def test_academic_paper_search_no_results():
    # 模拟 Semantic Scholar 返回空结果列表
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"data": []}
    with patch("httpx.AsyncClient.get", return_value=mock_resp):
        result = await academic_paper_search("NonExistentPaperTitleXYZ")
        assert "未找到与" in result

@pytest.mark.asyncio
async def test_wikidata_lookup_no_entity():
    # 模拟 Wikidata 查询，但实体未找到
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"search": []}
    with patch("httpx.AsyncClient.get", return_value=mock_resp):
        result = await wikidata_lookup("UnfindableEntityName")
        assert "未找到与 'UnfindableEntityName' 相关的实体" in result

@pytest.mark.asyncio
async def test_wikidata_lookup_no_matching_claims():
    # 模拟 Wikidata 实体存在但无任何预设的 P 属性 (claims 为空)
    mock_search_resp = MagicMock()
    mock_search_resp.status_code = 200
    mock_search_resp.json.return_value = {"search": [{"id": "Q999999", "label": "测试实体"}]}

    mock_entity_resp = MagicMock()
    mock_entity_resp.status_code = 200
    mock_entity_resp.json.return_value = {
        "entities": {
            "Q999999": {
                "labels": {"zh": {"value": "测试实体"}},
                "descriptions": {"zh": {"value": "测试描述"}},
                "claims": {} # 空的 claims
            }
        }
    }

    with patch("httpx.AsyncClient.get") as mock_get:
        mock_get.side_effect = [mock_search_resp, mock_entity_resp]
        result = await wikidata_lookup("测试实体")
        assert "[ENTITY]: 测试实体 (Q999999)" in result
        assert "此实体未查询到预设范围内的主要结构化事实" in result

@pytest.mark.asyncio
async def test_pubmed_search_no_results():
    # 模拟 PubMed esearch 未找到任何 PMID
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"esearchresult": {"idlist": []}}
    with patch("httpx.AsyncClient.get", return_value=mock_resp):
        result = await pubmed_scientific_search("unobtainable_medical_term")
        assert "未找到与" in result
        assert "相关的医学研究文献" in result

@pytest.mark.asyncio
async def test_pubmed_search_http_error():
    # 模拟 PubMed 在第一阶段 HTTP 请求失败
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    with patch("httpx.AsyncClient.get", return_value=mock_resp):
        result = await pubmed_scientific_search("medical term")
        assert "接口响应异常" in result

@pytest.mark.asyncio
async def test_fact_check_global_api_exception_fallback():
    # 模拟 Google Fact Check API 请求发生网络异常，期望它能自动降级至定向检索
    with patch("os.getenv", return_value="FAKE_GOOGLE_KEY"), \
         patch("httpx.AsyncClient.get", side_effect=httpx.RequestError("Network error")), \
         patch("agent.tools.fact_check.web_search", return_value="Fallback Search Result") as mock_web:
        result = await fact_check_global("Some random query")
        assert "[SOURCE]: Authoritative Global Fact Check Portals (Direct Search)" in result
        assert "Fallback Search Result" in result

