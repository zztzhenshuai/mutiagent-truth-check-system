import re
from .web_search import web_search

async def consumer_health_verifier(input: str) -> str:
    """
    验证日常健康、生活常识、养生方子、非处方药等大众健康声明。
    通过定向检索国内外权威大众医学与科普数据库来获取可信度高的结论。
    """
    clean_input = input.strip().strip('"').strip("'")
    if not clean_input:
        return "工具执行失败：输入为空。"

    # 定义国内外权威的大众健康与医学科普数据源
    authoritative_sites = [
        "who.int",          # WHO
        "cdc.gov",          # US CDC
        "fda.gov",          # US FDA
        "medlineplus.gov",  # NIH MedlinePlus
        "mayoclinic.org",   # Mayo Clinic
        "dxy.com",          # 丁香医生
        "kepuchina.cn",     # 科普中国
        "nhc.gov.cn",       # 国家卫健委
        "cma.org.cn",       # 中华医学会
    ]

    # 构造带有 site 定向的高级检索语法
    site_filter = " OR ".join([f"site:{site}" for site in authoritative_sites])
    targeted_query = f"{clean_input} ({site_filter})"

    try:
        # 调用底层 web_search，禁止清洗高级检索符号
        search_results = await web_search(targeted_query, clean_query=False)
        
        # 检查是否找到了有效内容，若未找到（比如 site 限制太死），进行 Fallback 降级
        if "网络搜索未找到相关结果" in search_results or "工具执行失败" in search_results:
            # 降级：使用通用搜索，但附加“科普”、“健康”等检索词
            fallback_query = f"{clean_input} 医学科普 健康辟谣"
            search_results = await web_search(fallback_query, clean_query=True)
            source_info = "General Web Search (Consumer Health Fallback)"
            credibility = "Medium (General Web Search)"
        else:
            source_info = "Authoritative Consumer Health Portals (Direct Search)"
            credibility = "High (Authoritative Health Source)"
        
        # 返回符合 Observation 协议的结构化文本
        return (
            f"[SOURCE]: {source_info}\n"
            f"[QUERY]: {clean_input}\n"
            f"[CREDIBILITY]: {credibility}\n"
            f"[RESULTS]:\n{search_results}"
        )
    except Exception as e:
        return f"工具执行失败：日常健康数据源检索异常 ({str(e)})。"
