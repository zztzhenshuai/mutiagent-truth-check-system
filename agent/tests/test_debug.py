import httpx
import urllib.parse
import asyncio
from duckduckgo_search import DDGS

async def test_wiki():
    query = urllib.parse.quote("消费者权益保护法")
    url = f"https://zh.wikipedia.org/w/api.php?action=query&list=search&srsearch={query}&utf8=&format=json&srlimit=3"
    print("Requesting:", url)
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        print("Wiki Status:", resp.status_code)
        print("Wiki Text:", resp.text[:200])

def test_ddg():
    query = "本次工作不收取任何费用 虚假宣传 诈骗 常见套路"
    print("DDG Query:", query)
    with DDGS() as ddgs:
        results = list(ddgs.text(query, region='cn-zh', safesearch='off', max_results=3))
        for r in results:
            print("DDG Result:", r.get('title'))

asyncio.run(test_wiki())
test_ddg()
