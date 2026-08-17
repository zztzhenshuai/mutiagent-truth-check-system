import httpx
import re
from .web_search import web_search

async def stock_market_quotes(input: str) -> str:
    """
    获取全球（含A股、美股、港股）股票的实时价格、涨跌幅、市值等基本行情。
    """
    clean_input = input.strip().strip('"').strip("'")
    if not clean_input:
        return "工具执行失败：输入为空。"

    timeout = httpx.Timeout(10.0)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    # 步骤 1: 尝试根据输入模糊搜索股票代码 (Ticker Symbol)
    search_url = "https://query2.finance.yahoo.com/v1/finance/search"
    search_params = {"q": clean_input, "quotesCount": 3, "newsCount": 0}
    
    symbol = None
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            resp = await client.get(search_url, params=search_params)
            if resp.status_code == 200:
                data = resp.json()
                quotes = data.get("quotes", [])
                if quotes:
                    symbol = quotes[0].get("symbol")
    except Exception:
        pass

    # 如果无法通过搜索解析出代码，且输入本身看起来像股票代码，直接作为代码使用
    if not symbol:
        # 常见代码格式：如 AAPL, 600519.SS, 0700.HK, 000002.SZ
        if re.match(r'^[A-Za-z0-9\.\-]+$', clean_input):
            symbol = clean_input
        else:
            # 搜索失败且不是代码格式，直接降级至通用网络搜索
            return await _stock_fallback_search(clean_input)

    # 步骤 2: 调用 Yahoo Finance Quote API 获取实时行情
    quote_url = "https://query1.finance.yahoo.com/v7/finance/quote"
    quote_params = {"symbols": symbol}
    
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            resp = await client.get(quote_url, params=quote_params)
            if resp.status_code == 200:
                data = resp.json()
                result_list = data.get("quoteResponse", {}).get("result", [])
                if result_list:
                    quote = result_list[0]
                    name = quote.get("longName") or quote.get("shortName") or symbol
                    price = quote.get("regularMarketPrice", 0.0)
                    currency = quote.get("currency", "USD")
                    change = quote.get("regularMarketChange", 0.0)
                    change_pct = quote.get("regularMarketChangePercent", 0.0)
                    market_cap = quote.get("marketCap", 0)
                    exchange = quote.get("fullExchangeName", "Unknown Exchange")
                    
                    # 格式化市值
                    if market_cap > 1e12:
                        market_cap_str = f"{market_cap / 1e12:.2f}万亿"
                    elif market_cap > 1e8:
                        market_cap_str = f"{market_cap / 1e8:.2f}亿"
                    else:
                        market_cap_str = f"{market_cap}"
                        
                    return (
                        f"[SOURCE]: Yahoo Finance API\n"
                        f"[COMPANY]: {name} ({symbol})\n"
                        f"[EXCHANGE]: {exchange}\n"
                        f"[PRICE]: {price:.2f} {currency}\n"
                        f"[CHANGE]: {change:+.2f} ({change_pct:+.2f}%)\n"
                        f"[MARKET CAP]: {market_cap_str} {currency}\n"
                        f"[CREDIBILITY]: High (Real-time Market Quote)"
                    )
    except Exception:
        pass

    # 步骤 3: 降级回退
    return await _stock_fallback_search(clean_input)

async def _stock_fallback_search(query: str) -> str:
    fallback_query = f"{query} 股票价格 股价 行情"
    search_res = await web_search(fallback_query, clean_query=True)
    return (
        f"[SOURCE]: General Web Search (Stock Quote Fallback)\n"
        f"[QUERY]: {fallback_query}\n"
        f"[RESULTS]:\n{search_res}"
    )
