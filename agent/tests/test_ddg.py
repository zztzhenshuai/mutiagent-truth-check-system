from duckduckgo_search import DDGS
query = "本次工作不收取任何费用 虚假宣传 诈骗 常见套路"
print("Testing DDGS with lite backend...")
try:
    with DDGS() as ddgs:
        results = list(ddgs.text(query, backend="lite", region='cn-zh', max_results=3))
        for r in results:
            print(r.get('title'))
except Exception as e:
    print("Lite error:", e)

print("\nTesting DDGS with html backend...")
try:
    with DDGS() as ddgs:
        results = list(ddgs.text(query, backend="html", region='cn-zh', max_results=3))
        for r in results:
            print(r.get('title'))
except Exception as e:
    print("HTML error:", e)
