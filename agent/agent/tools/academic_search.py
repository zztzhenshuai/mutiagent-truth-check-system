import httpx
import json

async def academic_paper_search(input: str) -> str:
    """
    通过 Semantic Scholar 学术搜索引擎查询文献。
    """
    clean_input = input.strip().strip('"').strip("'")
    if not clean_input:
        return "工具执行失败：输入为空。"

    timeout = httpx.Timeout(10.0)
    headers = {"User-Agent": "ArticleFactCheckBot/1.0 (Contact: 231250129@smail.nju.edu.cn)"}
    
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {
        "query": clean_input,
        "limit": 3,
        "fields": "title,authors,year,citationCount,abstract,venue,url"
    }

    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            resp = await client.get(url, params=params)
            
            # 处理限流 429
            if resp.status_code == 429:
                return "工具执行失败：触发 Semantic Scholar 频控限制。建议使用 web_search 获取通用学术信息。"
                
            if resp.status_code != 200:
                return f"工具执行失败：Semantic Scholar 接口返回异常 (HTTP {resp.status_code})。建议使用 web_search 获取通用信息。"

            data = resp.json()
            papers = data.get("data", [])

        if not papers:
            return f"Semantic Scholar 中未找到与 '{clean_input}' 相关的学术研究论文。建议使用 web_search 获取通用信息。"

        formatted_papers = []
        for paper in papers:
            title = paper.get("title", "无标题")
            year = paper.get("year", "未知年份")
            citation_count = paper.get("citationCount", 0)
            venue = paper.get("venue", "未知会议/期刊")
            abstract = paper.get("abstract", "暂无摘要")
            paper_url = paper.get("url", "无链接")

            authors_list = paper.get("authors", [])
            authors = ", ".join([a.get("name", "") for a in authors_list[:3]])
            if len(authors_list) > 3:
                authors += " et al."

            # 截断摘要长度以防 Token 膨胀
            if len(abstract) > 300:
                abstract = abstract[:300] + "... (已截断)"

            paper_block = (
                f"- [TITLE]: {title}\n"
                f"  [YEAR]: {year}\n"
                f"  [AUTHORS]: {authors or '未知作者'}\n"
                f"  [CITATION COUNT]: {citation_count} (被引频次)\n"
                f"  [VENUE]: {venue}\n"
                f"  [ABSTRACT]: {abstract}\n"
                f"  [URL]: {paper_url}"
            )
            formatted_papers.append(paper_block)

        return (
            f"[SOURCE]: Semantic Scholar Academic Database\n"
            f"[QUERY]: {clean_input}\n"
            f"[RESULTS]:\n" + "\n\n".join(formatted_papers)
        )

    except httpx.HTTPStatusError as e:
        return f"工具执行失败：请求 Semantic Scholar 异常，HTTP 状态码 {e.response.status_code}。建议使用 web_search 获取通用信息。"
    except httpx.RequestError as e:
        return f"工具执行失败：请求 Semantic Scholar 接口超时或不可达 ({str(e)})。建议使用 web_search 获取通用信息。"
    except Exception as e:
        return f"工具执行失败：学术数据解析异常 ({str(e)})。"
