import os
import asyncio
import logging

logger = logging.getLogger("agent.tools.web_search")


def _do_sync_web_search(input_str: str, clean_query: bool = True) -> str:
    # First priority: Tavily
    try:
        from tavily import TavilyClient
        api_key = os.getenv("TAVILY_API_KEY")
        clean_input = input_str.strip().strip('"').strip("'")
        if api_key:
            client = TavilyClient(api_key=api_key)
            # 使用 search_depth="advanced" 可以获得更深度的搜索结果
            result = client.search(query=clean_input, max_results=5, include_answer=True)
            
            answer = result.get('answer', '未找到直接答案。')
            search_results = result.get('results', [])
            
            # 构造更丰富的返回内容，包含每个来源的标题和正文摘要
            formatted_res = [f"【AI 总结】: {answer}\n"]
            for i, r in enumerate(search_results, 1):
                title = r.get('title', '无标题')
                content = r.get('content', '无内容摘要')
                url = r.get('url', '无链接')
                formatted_res.append(f"结果 {i}: {title}\n内容摘要: {content}\n链接: {url}\n")
            
            return "\n".join(formatted_res)
        else:
            logger.warning("未配置 TAVILY_API_KEY，降级使用 DuckDuckGo 搜索")
    except ImportError:
        pass
    except Exception as e:
        logger.warning("Tavily 搜索失败，降级使用 DuckDuckGo：%s", e)
        
    # Second priority: DuckDuckGo Search
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            import re
            if clean_query:
                # 清洗标点符号和多余引号
                clean_input = re.sub(r'[^\w\s]', ' ', input_str).strip()
            else:
                # 保留常用搜索算符（冒号、引号、圆括号、点号、减号等）
                clean_input = re.sub(r'[^\w\s\.\:\(\)\"\-]', ' ', input_str).strip()
                
            results = list(ddgs.text(clean_input, safesearch='off', max_results=5))
            if not results:
                return "结论：网络搜索未找到相关结果。请尝试提取更简短的核心关键词重新搜索。"
            snippets = [f"- {r.get('title', '')}: {r.get('body', '')} ({r.get('href', '')})" for r in results]
            return "以下为DuckDuckGo网络搜索到的可能相关的线索：\n" + "\n".join(snippets)
    except Exception as e:
        return f"工具执行失败：搜索服务均不可用 ({str(e)})"

async def web_search(input: str, clean_query: bool = True) -> str:
    """
    搜索互联网核实事实性声明。
    """
    return await asyncio.to_thread(_do_sync_web_search, input, clean_query)
