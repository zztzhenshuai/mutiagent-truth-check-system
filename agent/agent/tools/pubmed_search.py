import httpx
import json
import re

async def pubmed_scientific_search(input: str) -> str:
    """
    搜索 PubMed 医学数据库，获取相关文献摘要和元数据。
    """
    clean_input = input.strip().strip('"').strip("'")
    if not clean_input:
        return "工具执行失败：输入为空。"

    timeout = httpx.Timeout(10.0)
    headers = {"User-Agent": "ArticleFactCheckBot/1.0 (Contact: 231250129@smail.nju.edu.cn)"}

    try:
        # 步骤 1: 调用 esearch.fcgi 获取匹配文章的 PMID 列表
        search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        search_params = {
            "db": "pubmed",
            "term": clean_input,
            "retmode": "json",
            "retmax": 3
        }

        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            search_resp = await client.get(search_url, params=search_params)
            if search_resp.status_code != 200:
                return f"工具执行失败：PubMed 搜索接口响应异常 (HTTP {search_resp.status_code})。建议使用 web_search 获取通用信息。"

            search_data = search_resp.json()
            id_list = search_data.get("esearchresult", {}).get("idlist", [])

        if not id_list:
            return f"PubMed 数据库中未找到与 '{clean_input}' 相关的医学研究文献。建议使用 web_search 获取通用信息。"

        # 步骤 2: 调用 esummary.fcgi 获取文献的详细元数据
        summary_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
        summary_params = {
            "db": "pubmed",
            "id": ",".join(id_list),
            "retmode": "json"
        }

        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            summary_resp = await client.get(summary_url, params=summary_params)
            if summary_resp.status_code != 200:
                return f"工具执行失败：PubMed 概要接口响应异常 (HTTP {summary_resp.status_code})。"

            summary_data = summary_resp.json()
            results = summary_data.get("result", {})

        # 步骤 3: 组装结构化 Observation 文本
        formatted_papers = []
        for pmid in id_list:
            paper_info = results.get(pmid)
            if not paper_info:
                continue

            title = paper_info.get("title", "无标题")
            # 清理 HTML 标签和多余符号
            title = re.sub(r'<[^>]+>', '', title).strip()
            
            journal = paper_info.get("source", "未知期刊")
            pub_date = paper_info.get("pubdate", "未知年份")
            year_match = re.search(r'\b(19|20)\d{2}\b', pub_date)
            year = year_match.group(0) if year_match else pub_date

            authors_list = paper_info.get("authors", [])
            authors = ", ".join([a.get("name", "") for a in authors_list[:3]])
            if len(authors_list) > 3:
                authors += " et al."

            url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"

            paper_block = (
                f"- [TITLE]: {title}\n"
                f"  [JOURNAL]: {journal}\n"
                f"  [YEAR]: {year}\n"
                f"  [AUTHORS]: {authors or '未知作者'}\n"
                f"  [CREDIBILITY]: High (Peer-reviewed Journal: {journal})\n"
                f"  [URL]: {url}"
            )
            formatted_papers.append(paper_block)

        return (
            f"[SOURCE]: PubMed Database\n"
            f"[QUERY]: {clean_input}\n"
            f"[RESULTS]:\n" + "\n\n".join(formatted_papers)
        )

    except httpx.HTTPStatusError as e:
        return f"工具执行失败：请求 PubMed 接口异常，HTTP 状态码 {e.response.status_code}。建议使用 web_search 获取通用信息。"
    except httpx.RequestError as e:
        return f"工具执行失败：网络连接超时或无法访问 PubMed 接口 ({str(e)})。建议使用 web_search 获取通用信息。"
    except Exception as e:
        return f"工具执行失败：PubMed 数据解析异常 ({str(e)})。"
