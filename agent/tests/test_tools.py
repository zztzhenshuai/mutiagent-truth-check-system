import asyncio
import os
import httpx
from agent.tools.registry import TOOL_REGISTRY

async def test():
    print("====== 0. 基础网络诊断 (直接请求) ======")
    
    print("【诊断 1】尝试直接请求维基百科...")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get("https://zh.wikipedia.org/w/api.php", params={
                "action": "query", "list": "search", "srsearch": "消费者权益保护法", 
                "utf8": "1", "format": "json", "srlimit": 3
            })
            print(f"-> HTTP 状态码: {resp.status_code}")
            if resp.status_code != 200:
                print(f"-> 请求头: {resp.request.headers}")
            print(f"-> 返回数据 (前 300 字): {resp.text[:300]}")
    except Exception as e:
        print(f"-> 直接请求维基百科异常: {e.__class__.__name__}: {e}")

    print("\n【诊断 2】尝试直接请求 DuckDuckGo...")
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            # 尝试最基础的搜素
            q = "本次工作不收取任何费用 虚假宣传"
            print(f"-> 正在调用 DDGS().text(q='{q}', region='cn-zh')")
            results = list(ddgs.text(q, region='cn-zh', safesearch='off', max_results=3))
            print(f"-> DDG 直接调用返回结果条数: {len(results)}")
            if results:
                print(f"-> DDG 第一条结果: {results[0]}")
            else:
                print("-> DDG 返回为空列表 []。")
    except Exception as e:
        print(f"-> DDG 直接请求异常: {e.__class__.__name__}: {e}")

    print("\n====== 1. 测试 Wikipedia 检索 (wikipedia_lookup) ======")
    res1 = await TOOL_REGISTRY["wikipedia_lookup"].func("消费者权益保护法")
    print(f"工具完整返回值:\n{res1}")
    
    print("\n====== 2. 测试 Web Search 检索 (web_search) ======")
    bad_query = '"本次工作不收取任何费用" 虚假宣传 诈骗 常见套路'
    print(f"原始搜索词: {bad_query}")
    res3 = await TOOL_REGISTRY["web_search"].func(bad_query)
    print(f"工具完整返回值:\n{res3}")

if __name__ == "__main__":
    asyncio.run(test())
