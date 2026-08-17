import httpx
import urllib.parse
import re
import os
import logging

logger = logging.getLogger("agent.tools.wikipedia_lookup")


async def wikipedia_lookup(input: str) -> str:
    """
    Search Wikipedia (ZH and EN) for the given query using Action API (list=search).
    """
    # 还原原生字符串，让 httpx 去处理编码
    clean_input = input.strip().strip('"').strip("'")
    timeout = httpx.Timeout(10.0)
    
    async def search_wiki(lang: str, q: str) -> str:
        url = f"https://{lang}.wikipedia.org/w/api.php"
        params = {
            "action": "query",
            "list": "search",
            "srsearch": q, 
            "utf8": "1",
            "format": "json",
            "srlimit": 3
        }
        # 优先读取环境变量，否则使用通用的学校项目标识格式
        ua = os.getenv("WIKI_USER_AGENT", "ArticleFactCheckBot/1.0 (Contact: 231250129@smail.nju.edu.cn)")
        headers = {"User-Agent": ua}
        
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            resp = await client.get(url, params=params)
            
            # 严格遵守 Robot Policy: 处理 429 Too Many Requests
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 5))
                logger.warning("触发维基百科频控（429），休眠 %d 秒后重试", retry_after)
                import asyncio
                await asyncio.sleep(retry_after)
                resp = await client.get(url, params=params) # 重试一次
                
            if resp.status_code == 200:
                data = resp.json()
                search_results = data.get("query", {}).get("search", [])
                if not search_results:
                    return ""
                
                results = []
                for item in search_results:
                    title = item.get("title", "")
                    snippet = item.get("snippet", "")
                    # Remove HTML tags from snippet
                    clean_snippet = re.sub(r'<[^>]+>', '', snippet)
                    results.append(f"【{title}】: {clean_snippet}")
                
                return "\n".join(results)
            return ""

    try:
        # Try Chinese Wikipedia
        zh_res = await search_wiki("zh", clean_input)
        if zh_res:
            return f"维基百科(中文)检索结果：\n{zh_res}"
            
        # Try English Wikipedia as Fallback
        en_res = await search_wiki("en", clean_input)
        if en_res:
            return f"维基百科(英文)检索结果：\n{en_res}"
            
        return "维基百科未找到相关条目。请尝试提取更简短的核心名词作为搜索词重新搜索。"
    except httpx.RequestError as e:
        return f"工具执行失败：请求维基百科接口异常 ({str(e)})"
    except Exception as e:
        return f"工具执行失败：维基百科查询异常 ({str(e)})"
