import httpx
import re
import urllib.parse
import xml.etree.ElementTree as ET
from .web_search import web_search

async def preprint_arxiv_search(input: str) -> str:
    """
    搜索 arXiv 预印本数据库，检查论文是否存在及其上传状态。
    显式标记 arXiv 为未经同行评审（Unpublished Preprint）的可信度。
    """
    clean_input = input.strip().strip('"').strip("'")
    if not clean_input:
        return "工具执行失败：输入为空。"

    timeout = httpx.Timeout(10.0)
    # 使用 urlencode 编码查询词，防范特殊字符异常
    encoded_query = urllib.parse.quote(clean_input)
    url = f"http://export.arxiv.org/api/query?search_query=all:{encoded_query}&max_results=3"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                # 解析 XML 响应
                root = ET.fromstring(resp.content)
                # arXiv API 默认的 Atom XML 命名空间
                ns = {"atom": "http://www.w3.org/2005/Atom"}
                entries = root.findall("atom:entry", ns)

                if not entries:
                    return f"arXiv 预印本数据库中未找到与 '{clean_input}' 相关的预印本文献。建议使用 web_search 进一步查询。"

                formatted_papers = []
                for entry in entries:
                    # 获取标题，清理内部换行
                    title_node = entry.find("atom:title", ns)
                    title = title_node.text.strip().replace("\n", " ") if title_node is not None else "无标题"
                    title = re.sub(r'\s+', ' ', title)

                    # 获取发布年份
                    published_node = entry.find("atom:published", ns)
                    pub_date = published_node.text.strip() if published_node is not None else "未知"
                    year_match = re.search(r'\b(19|20)\d{2}\b', pub_date)
                    year = year_match.group(0) if year_match else pub_date

                    # 获取作者列表
                    author_nodes = entry.findall("atom:author", ns)
                    authors_list = []
                    for auth in author_nodes[:3]:
                        name_node = auth.find("atom:name", ns)
                        if name_node is not None:
                            authors_list.append(name_node.text.strip())
                    authors = ", ".join(authors_list)
                    if len(author_nodes) > 3:
                        authors += " et al."

                    # 获取摘要，截断长度
                    summary_node = entry.find("atom:summary", ns)
                    summary = summary_node.text.strip().replace("\n", " ") if summary_node is not None else ""
                    summary = re.sub(r'\s+', ' ', summary)
                    if len(summary) > 250:
                        summary = summary[:250] + "... (已截断)"

                    # 获取链接
                    id_node = entry.find("atom:id", ns)
                    link = id_node.text.strip() if id_node is not None else "无链接"

                    paper_block = (
                        f"- [TITLE]: {title}\n"
                        f"  [YEAR]: {year}\n"
                        f"  [AUTHORS]: {authors or '未知作者'}\n"
                        f"  [CREDIBILITY]: Medium (Unpublished Preprint on arXiv - No Peer Review)\n"
                        f"  [SUMMARY]: {summary}\n"
                        f"  [URL]: {link}"
                    )
                    formatted_papers.append(paper_block)

                return (
                    f"[SOURCE]: arXiv API\n"
                    f"[QUERY]: {clean_input}\n"
                    f"[RESULTS]:\n" + "\n\n".join(formatted_papers)
                )
    except Exception:
        # API 异常时触发 Fallback
        pass

    # 降级：使用 web_search 定向检索 arXiv.org 页面
    return await _arxiv_fallback_search(clean_input)

async def _arxiv_fallback_search(query: str) -> str:
    fallback_query = f"{query} site:arxiv.org"
    search_res = await web_search(fallback_query, clean_query=False)
    return (
        f"[SOURCE]: General Web Search (arXiv Fallback)\n"
        f"[QUERY]: {fallback_query}\n"
        f"[CREDIBILITY]: Medium (Unpublished Preprint on arXiv - No Peer Review)\n"
        f"[RESULTS]:\n{search_res}"
    )
