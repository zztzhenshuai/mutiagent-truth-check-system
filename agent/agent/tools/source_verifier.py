import httpx
import re

async def source_verifier(input: str) -> str:
    """
    Fetch the content of a URL and extract the main article text using readability.
    """
    url = input.strip()
    if not url.startswith("http"):
        return f"工具执行失败：无效的URL格式 '{url}'"
        
    try:
        from readability import Document
    except ImportError:
        return "工具执行失败：缺少依赖 readability-lxml"
        
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        # Disable SSL verification in case of old certs being an issue with random URLs
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True, verify=False) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            
            # Parse with readability
            doc = Document(response.text)
            html_summary = doc.summary()
            
            # Simple HTML tag removal via regex to save installing BeautifulSoup just for text
            clean_text = re.sub(r'<[^>]+>', ' ', html_summary)
            # Remove extra whitespace
            clean_text = re.sub(r'\s+', ' ', clean_text).strip()
            
            if not clean_text:
                return "结论：成功获取页面，但未能提取到有效正文内容。"
            
            if len(clean_text) > 2000:
                clean_text = clean_text[:2000] + "... (已截断)"
                
            return f"页面正文摘要: \n{clean_text}"
            
    except httpx.HTTPStatusError as e:
        return f"工具执行失败：页面返回错误状态码 {e.response.status_code}"
    except httpx.RequestError as e:
        return f"工具执行失败：页面请求异常或超时 ({str(e)})"
    except Exception as e:
        return f"工具执行失败：内容解析异常 ({str(e)})"
