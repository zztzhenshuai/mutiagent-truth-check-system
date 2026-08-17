import httpx
import re

PROPERTY_MAP = {
    "P17": "国家/地区",
    "P31": "是/实例为",
    "P571": "成立时间",
    "P569": "出生日期",
    "P570": "逝世日期",
    "P19": "出生地点",
    "P27": "国籍",
    "P108": "雇主",
    "P166": "获颁奖项",
    "P276": "位置",
    "P69": "毕业院校",
}

async def wikidata_lookup(input: str) -> str:
    """
    查询维基数据实体，获取其结构化属性信息。
    """
    clean_input = input.strip().strip('"').strip("'")
    if not clean_input:
        return "工具执行失败：输入为空。"

    timeout = httpx.Timeout(10.0)
    headers = {"User-Agent": "ArticleFactCheckBot/1.0 (Contact: 231250129@smail.nju.edu.cn)"}

    async def search_entity(lang: str, q: str) -> list[dict]:
        url = "https://www.wikidata.org/w/api.php"
        params = {
            "action": "wbsearchentities",
            "search": q,
            "language": lang,
            "format": "json",
            "limit": 3
        }
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                return resp.json().get("search", [])
            return []

    try:
        # 1. 搜索实体，优先使用中文
        search_results = await search_entity("zh", clean_input)
        if not search_results:
            search_results = await search_entity("en", clean_input)

        if not search_results:
            return f"维基数据(Wikidata)未找到与 '{clean_input}' 相关的实体。建议使用 web_search 获取通用信息。"

        # 获取第一个最匹配的实体
        entity_item = search_results[0]
        entity_id = entity_item.get("id")
        
        # 2. 获取该实体的详细属性信息 (labels, descriptions, claims)
        url = "https://www.wikidata.org/w/api.php"
        params = {
            "action": "wbgetentities",
            "ids": entity_id,
            "props": "labels|descriptions|claims",
            "languages": "zh|en",
            "format": "json"
        }
        
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            entity_data = resp.json().get("entities", {}).get(entity_id, {})

        # 获取标签和描述
        label = entity_data.get("labels", {}).get("zh", {}).get("value") or \
                entity_data.get("labels", {}).get("en", {}).get("value") or entity_id
        
        description = entity_data.get("descriptions", {}).get("zh", {}).get("value") or \
                      entity_data.get("descriptions", {}).get("en", {}).get("value") or "无描述"

        claims = entity_data.get("claims", {})
        
        # 3. 收集并解析特定 Property 的数据，收集需要额外解析 Label 的 QID
        parsed_claims = {}
        target_qids = set()

        for pid, prop_name in PROPERTY_MAP.items():
            if pid not in claims:
                continue
            
            statements = claims[pid]
            values = []
            for st in statements:
                mainsnak = st.get("mainsnak", {})
                datavalue = mainsnak.get("datavalue", {})
                value_type = datavalue.get("type")
                value_content = datavalue.get("value")

                if value_type == "wikibase-entityid" and isinstance(value_content, dict):
                    qid = value_content.get("id")
                    if qid:
                        values.append(qid)
                        target_qids.add(qid)
                elif value_type == "time" and isinstance(value_content, dict):
                    time_str = value_content.get("time", "")
                    # 清洗时间格式，如 "+2023-00-00T00:00:00Z" -> "2023年"
                    clean_time = re.sub(r'^\+', '', time_str).split('T')[0]
                    clean_time = re.sub(r'-00', '', clean_time)
                    values.append(clean_time)
                elif value_type == "string" and isinstance(value_content, str):
                    values.append(value_content)
                elif value_type == "quantity" and isinstance(value_content, dict):
                    amount = value_content.get("amount", "")
                    unit = value_content.get("unit", "")
                    if unit and "http://www.wikidata.org/entity/" in unit:
                        unit_qid = unit.split("/")[-1]
                        target_qids.add(unit_qid)
                        values.append(f"{amount} {unit_qid}")
                    else:
                        values.append(amount)

            if values:
                parsed_claims[prop_name] = values

        # 4. 如果有 QID，批量查询 QID 的中文标签以丰富展示
        qid_labels = {}
        if target_qids:
            qids_chunk = list(target_qids)[:50] # 限制一次批量查50个
            params = {
                "action": "wbgetentities",
                "ids": "|".join(qids_chunk),
                "props": "labels",
                "languages": "zh|en",
                "format": "json"
            }
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    entities_res = resp.json().get("entities", {})
                    for qid in qids_chunk:
                        ent = entities_res.get(qid, {})
                        q_label = ent.get("labels", {}).get("zh", {}).get("value") or \
                                  ent.get("labels", {}).get("en", {}).get("value") or qid
                        qid_labels[qid] = q_label

        # 5. 组装结构化 Observation 文本
        formatted_res = [
            f"[SOURCE]: Wikidata (https://www.wikidata.org/wiki/{entity_id})",
            f"[ENTITY]: {label} ({entity_id})",
            f"[DESCRIPTION]: {description}",
            "[STATEMENTS]:"
        ]

        if not parsed_claims:
            formatted_res.append("  (此实体未查询到预设范围内的主要结构化事实。)")
        else:
            for prop_name, vals in parsed_claims.items():
                resolved_vals = []
                for val in vals:
                    # 尝试转换 QID
                    if isinstance(val, str) and val.startswith("Q") and val in qid_labels:
                        resolved_vals.append(qid_labels[val])
                    elif isinstance(val, str) and " " in val:
                        # 量词转换，如 "+100 Q11573" -> "100 米"
                        parts = val.split(" ")
                        if len(parts) == 2 and parts[1].startswith("Q") and parts[1] in qid_labels:
                            resolved_vals.append(f"{parts[0]} {qid_labels[parts[1]]}")
                        else:
                            resolved_vals.append(val)
                    else:
                        resolved_vals.append(str(val))
                
                formatted_res.append(f"  - {prop_name}: {', '.join(resolved_vals)}")

        return "\n".join(formatted_res)

    except httpx.HTTPStatusError as e:
        return f"工具执行失败：维基数据接口请求异常，HTTP 状态码 {e.response.status_code}。建议使用 web_search 通用检索。"
    except httpx.RequestError as e:
        return f"工具执行失败：请求维基数据接口超时或不可达 ({str(e)})。建议使用 web_search 通用检索。"
    except Exception as e:
        return f"工具执行失败：维基数据解析异常 ({str(e)})。"
