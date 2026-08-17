import asyncio
import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.tools.registry import TOOL_REGISTRY, get_allowed_tools

async def test_new_tools():
    print("====== 1. 验证 get_allowed_tools 路由过滤 ======")
    med_tools = get_allowed_tools(["medical"])
    fin_tools = get_allowed_tools(["finance"])
    print(f"Medical 领域可用工具: {[t.name for t in med_tools]}")
    print(f"Finance 领域可用工具: {[t.name for t in fin_tools]}")

    print("\n====== 2. 测试 wikidata_lookup (维基数据) ======")
    res_wiki = await TOOL_REGISTRY["wikidata_lookup"].func("南京大学")
    print(res_wiki)

    print("\n====== 3. 测试 official_statistics_lookup (世界银行统计数据) ======")
    res_stats_online = await TOOL_REGISTRY["official_statistics_lookup"].func("CN NY.GDP.MKTP.KD.ZG 2023")
    print(res_stats_online)

    print("\n====== 4. 测试 pubmed_search (PubMed 医学文献) ======")
    res_pubmed = await TOOL_REGISTRY["pubmed_search"].func("aspirin hypertension")
    print(res_pubmed)

    print("\n====== 5. 测试 semantic_scholar_search (学术搜索引擎) ======")
    res_academic = await TOOL_REGISTRY["semantic_scholar_search"].func("Attention is all you need")
    print(res_academic)

    print("\n====== 6. 测试 fact_check_registry (辟谣数据库) ======")
    # 模拟未配置 API Key 时的 Fallback 降级搜索
    res_fc = await TOOL_REGISTRY["fact_check_registry"].func("太阳绕着地球转")
    print(res_fc)

if __name__ == "__main__":
    asyncio.run(test_new_tools())
