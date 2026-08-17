import re
from .web_search import web_search

async def patent_status_lookup(input: str) -> str:
    """
    核查专利申请状态、授权状态及法律有效性（已授权、公布、失效等）。
    通过定向检索全球专利数据库（Google Patents, Lens.org 等）获取权威结论。
    """
    clean_input = input.strip().strip('"').strip("'")
    if not clean_input:
        return "工具执行失败：输入为空。"

    # 定义国内外权威专利数据检索源
    authoritative_sites = [
        "patents.google.com",  # Google Patents
        "lens.org",            # The Lens (Global patents)
        "epub.cnipa.gov.cn",   # 中国国家知识产权局专利公布公告系统
    ]

    site_filter = " OR ".join([f"site:{site}" for site in authoritative_sites])
    targeted_query = f"{clean_input} ({site_filter})"

    try:
        # 调用底层 web_search，保留 site 定向算符
        search_results = await web_search(targeted_query, clean_query=False)
        
        # 检查是否找到了有效内容，若未找到，执行 Fallback 降级
        if "网络搜索未找到相关结果" in search_results or "工具执行失败" in search_results:
            fallback_query = f"{clean_input} 专利 申请 授权 法律状态"
            search_results = await web_search(fallback_query, clean_query=True)
            source_info = "General Web Search (Patent Status Fallback)"
            credibility = "Medium (General Web Search)"
        else:
            source_info = "Authoritative Global Patent Databases (Direct Search)"
            credibility = "High (Official Patent Registry)"

        return (
            f"[SOURCE]: {source_info}\n"
            f"[QUERY]: {clean_input}\n"
            f"[CREDIBILITY]: {credibility}\n"
            f"[RESULTS]:\n{search_results}"
        )
    except Exception as e:
        return f"工具执行失败：专利数据源检索异常 ({str(e)})。"
