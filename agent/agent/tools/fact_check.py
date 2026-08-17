import httpx
import os
import json
import urllib.parse
from .web_search import web_search

async def fact_check_domestic(input: str) -> str:
    """
    检索国内权威机构和事实核查平台的辟谣记录。
    定向查询中国互联网联合辟谣平台、腾讯较真等国内主流核查平台。
    """
    clean_input = input.strip().strip('"').strip("'")
    if not clean_input:
        return "工具执行失败：输入为空。"

    # 定向查询国内主流事实核查平台
    authoritative_sites = [
        "piyao.org.cn",  # 中国互联网联合辟谣平台
        "fact.qq.com",   # 腾讯较真
    ]

    site_filter = " OR ".join([f"site:{site}" for site in authoritative_sites])
    targeted_query = f"{clean_input} ({site_filter})"

    try:
        search_results = await web_search(targeted_query, clean_query=False)
        
        if "网络搜索未找到相关结果" in search_results or "工具执行失败" in search_results:
            # 降级：使用通用搜索，但追加“辟谣”、“事实核查”等检索词
            fallback_query = f"{clean_input} 辟谣 事实核查"
            search_results = await web_search(fallback_query, clean_query=True)
            source_info = "General Web Search (Domestic Fact Check Fallback)"
            credibility = "Medium (General Web Search)"
        else:
            source_info = "Authoritative Domestic Fact Check Portals (Direct Search)"
            credibility = "High (Authoritative Fact Check Source)"

        return (
            f"[SOURCE]: {source_info}\n"
            f"[QUERY]: {clean_input}\n"
            f"[CREDIBILITY]: {credibility}\n"
            f"[RESULTS]:\n{search_results}"
        )
    except Exception as e:
        return f"工具执行失败：国内事实核查数据库检索异常 ({str(e)})。"

async def fact_check_global(input: str) -> str:
    """
    检索国际权威机构和事实核查平台的辟谣记录。
    优先调用 Google Fact Check Tools API，降级定向查询 Snopes, PolitiFact, FactCheck.org 等。
    """
    clean_input = input.strip().strip('"').strip("'")
    if not clean_input:
        return "工具执行失败：输入为空。"

    api_key = os.getenv("GOOGLE_FACT_CHECK_API_KEY")
    timeout = httpx.Timeout(10.0)

    # 1. 优先调用 Google Fact Check API
    if api_key:
        url = "https://factchecktools.googleapis.com/v1/claims:search"
        params = {
            "query": clean_input,
            "key": api_key,
            "languageCode": "en" # 国际核查通常用英文
        }
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    claims = data.get("claims", [])
                    if claims:
                        formatted_claims = []
                        for item in claims[:2]:
                            claim_text = item.get("text", "Unknown Claim")
                            claimant = item.get("claimant", "Unknown Claimant")
                            reviews = item.get("claimReview", [])
                            review_blocks = []
                            for rev in reviews[:2]:
                                publisher = rev.get("publisher", {}).get("name", "Unknown Publisher")
                                rating = rev.get("textualRating", "Unrated")
                                title = rev.get("title", "No Title")
                                rev_url = rev.get("url", "No Link")
                                review_blocks.append(
                                    f"    - [核查机构]: {publisher}\n"
                                    f"    - [核查结论]: {rating}\n"
                                    f"    - [核查标题]: {title}\n"
                                    f"    - [证据链接]: {rev_url}"
                                )
                            
                            formatted_claims.append(
                                f"- [原始宣称]: {claim_text}\n"
                                f"  [宣称主体]: {claimant}\n"
                                f"  [核查明细]:\n" + "\n".join(review_blocks)
                            )
                        
                        return (
                            f"[SOURCE]: Google Fact Check API\n"
                            f"[QUERY]: {clean_input}\n"
                            f"[CREDIBILITY]: High (Official Fact Check Registry)\n"
                            f"[RESULTS]:\n" + "\n\n".join(formatted_claims)
                        )
        except Exception:
            pass

    # 2. API 未配置或调用失败，降级定向查询国外主流辟谣网站
    authoritative_sites = [
        "snopes.com",
        "politifact.com",
        "factcheck.org",
    ]

    site_filter = " OR ".join([f"site:{site}" for site in authoritative_sites])
    targeted_query = f"{clean_input} ({site_filter})"

    try:
        search_results = await web_search(targeted_query, clean_query=False)
        
        if "网络搜索未找到相关结果" in search_results or "工具执行失败" in search_results:
            # 终极降级：使用通用英文搜索，附加 fact check 关键词
            fallback_query = f"{clean_input} fact check hoax"
            search_results = await web_search(fallback_query, clean_query=True)
            source_info = "General Web Search (Global Fact Check Fallback)"
            credibility = "Medium (General Web Search)"
        else:
            source_info = "Authoritative Global Fact Check Portals (Direct Search)"
            credibility = "High (Authoritative Fact Check Source)"

        return (
            f"[SOURCE]: {source_info}\n"
            f"[QUERY]: {clean_input}\n"
            f"[CREDIBILITY]: {credibility}\n"
            f"[RESULTS]:\n{search_results}"
        )
    except Exception as e:
        return f"工具执行失败：全球事实核查数据库检索异常 ({str(e)})。"
