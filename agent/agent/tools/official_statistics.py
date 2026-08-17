import httpx
import json
import re
import os
from pathlib import Path

# 获取项目根目录
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CACHE_PATH = _PROJECT_ROOT / "datasets" / "world_bank_cache.json"

INDICATOR_NAME_MAP = {
    "NY.GDP.MKTP.KD.ZG": "GDP growth (annual %)",
    "FP.CPI.TOTL.ZG": "Inflation, consumer prices (annual %)",
    "SL.UEM.TOTL.ZS": "Unemployment, total (% of total labor force)",
}

COUNTRY_NAME_MAP = {
    "CN": "China",
    "CHN": "China",
    "US": "United States",
    "USA": "United States",
    "JP": "Japan",
    "JPN": "Japan",
    "DE": "Germany",
    "DEU": "Germany",
    "WLD": "World",
}

def _parse_input_query(input_str: str) -> tuple[str, str, str]:
    """
    解析输入字符串，提取国家/地区代码、指标代码和年份。
    支持输入格式：
    1. 结构化空格分隔："CN NY.GDP.MKTP.KD.ZG 2023"
    2. 智能模糊识别："中国 GDP 2023年"
    """
    clean_input = input_str.strip().upper()
    
    # 尝试结构化提取
    parts = clean_input.split()
    country = None
    indicator = None
    year = None

    # 1. 提取年份 (4位连续数字)
    year_match = re.search(r'\b(20\d{2}|19\d{2})\b', clean_input)
    if year_match:
        year = year_match.group(1)
    else:
        year = "2023"  # 默认年份
        
    # 2. 提取指标代码
    for ind in INDICATOR_NAME_MAP.keys():
        if ind in clean_input:
            indicator = ind
            break
            
    # 3. 提取国家代码
    for cnt in COUNTRY_NAME_MAP.keys():
        if cnt in clean_input:
            country = cnt
            break

    # 4. 如果没有直接匹配到代码，通过中文/英文关键词智能模糊映射
    if not country:
        if "中国" in input_str or "CHINA" in clean_input:
            country = "CN"
        elif "美国" in input_str or "UNITED STATES" in clean_input or "USA" in clean_input:
            country = "US"
        elif "日本" in input_str or "JAPAN" in clean_input:
            country = "JP"
        elif "德国" in input_str or "GERMANY" in clean_input:
            country = "DE"
        elif "世界" in input_str or "全球" in input_str or "WORLD" in clean_input:
            country = "WLD"
        else:
            country = "CN"  # 默认中国

    if not indicator:
        if "GDP" in clean_input or "增速" in input_str or "增长" in input_str or "生产总值" in input_str:
            indicator = "NY.GDP.MKTP.KD.ZG"
        elif "CPI" in clean_input or "通胀" in input_str or "通货膨胀" in input_str or "物价" in input_str:
            indicator = "FP.CPI.TOTL.ZG"
        elif "失业" in input_str or "UNEMPLOYMENT" in clean_input:
            indicator = "SL.UEM.TOTL.ZS"
        else:
            indicator = "NY.GDP.MKTP.KD.ZG"  # 默认GDP增速

    # 规范国家代码为两字母
    if country in ["CHN", "CN"]:
        country = "CN"
    elif country in ["USA", "US"]:
        country = "US"
    elif country in ["JPN", "JP"]:
        country = "JP"
    elif country in ["DEU", "DE"]:
        country = "DE"
    elif country == "WLD":
        country = "WLD"

    return country, indicator, year

def _read_from_local_cache(country: str, indicator: str, year: str) -> float | None:
    """从本地 datasets/world_bank_cache.json 读取缓存数据"""
    if not _CACHE_PATH.exists():
        return None
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
            val = cache.get(country, {}).get(indicator, {}).get(year)
            return float(val) if val is not None else None
    except Exception:
        return None

async def macro_statistics_global(input: str) -> str:
    """
    验证宏观经济与社会统计数据。
    """
    if not input.strip():
        return "工具执行失败：输入为空。"
    try:
        country, indicator, year = _parse_input_query(input)
    except Exception as e:
        return f"工具执行失败：无法解析输入格式 ({str(e)})。"

    timeout = httpx.Timeout(8.0)
    url = f"http://api.worldbank.org/v2/country/{country}/indicator/{indicator}"
    params = {"date": year, "format": "json"}

    value = None
    source = "World Bank API"

    try:
        # 1. 尝试在线查询
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                # World Bank API 成功时返回的结构通常为包含两个元素的列表，第二个元素是数据列表
                if isinstance(data, list) and len(data) > 1 and isinstance(data[1], list) and len(data[1]) > 0:
                    val = data[1][0].get("value")
                    if val is not None:
                        value = float(val)
    except Exception as e:
        # 在线查询失败，记录降级标记
        source += f" (请求失败，已降级回退。原因: {type(e).__name__})"

    # 2. 如果在线查询未获取到有效数值，尝试读取本地缓存
    if value is None:
        value = _read_from_local_cache(country, indicator, year)
        source = "Local Static Cache (World Bank Datasets)"

    if value is None:
        return (
            f"工具执行失败：未能从 {source} 获取到 {COUNTRY_NAME_MAP.get(country, country)} "
            f"在 {year} 年的 {INDICATOR_NAME_MAP.get(indicator, indicator)} 指标数据。"
        )

    # 3. 组装结构化 Observation 文本
    country_name = COUNTRY_NAME_MAP.get(country, country)
    indicator_name = INDICATOR_NAME_MAP.get(indicator, indicator)
    
    # 格式化百分比显示
    formatted_value = f"{value:.2f}%" if "%" in indicator_name.lower() or "ratio" in indicator_name.lower() or "growth" in indicator_name.lower() or "inflation" in indicator_name.lower() or "unemployment" in indicator_name.lower() else f"{value}"

    return (
        f"[SOURCE]: {source}\n"
        f"[COUNTRY]: {country_name} ({country})\n"
        f"[INDICATOR]: {indicator_name} ({indicator})\n"
        f"[YEAR]: {year}\n"
        f"[VALUE]: {formatted_value}"
    )
