import asyncio
import os
from dotenv import load_dotenv
from agent.scanner import scan_article
from agent.llm.glm import GLMClient

async def debug_scanner():
    load_dotenv()
    
    # 模拟一篇典型的诈骗/虚假信息文章
    test_article = """
    【紧急通知】2026年全民补贴发放开始啦！
    根据国家最新政策，本次工作不收取任何费用。截止目前，已有超过 500 万名用户成功领取。
    据调查，该项目的年化收益率高达 300%。
    专家王教授表示，这是近十年来最大的财富机遇。
    只要扫描下方二维码，填写个人身份证信息，即可在 5 分钟内到账 5000 元。
    """
    
    # 初始化 LLM (需要确保 .env 中有 API Key)
    api_key = os.getenv("GLM_API_KEY")
    base_url = os.getenv("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/")
    
    if not api_key:
        print("错误: 请在 .env 中配置 GLM_API_KEY")
        return

    llm = GLMClient()
    
    print("====== 正在开始扫描文章 ======")
    print(f"文章长度: {len(test_article)} 字符")
    
    # 我们直接把 scan_article 里的部分逻辑拆解出来打印，方便查错
    from agent.scanner import _EXTRACT_PROMPT, _parse_claims_json, _resolve_offsets
    
    prompt = _EXTRACT_PROMPT.replace("{article}", test_article)
    print("\n[Step 1] 正在请求 LLM...")
    raw_response = await llm.complete([{"role": "user", "content": prompt}])
    print(f"\n[Step 2] LLM 原始返回内容:\n{raw_response}")
    
    claim_texts = _parse_claims_json(raw_response)
    print(f"\n[Step 3] 解析出的声明文本列表 ({len(claim_texts)} 条):")
    for i, t in enumerate(claim_texts, 1):
        print(f"  {i}. {t}")
        
    print("\n[Step 4] 尝试在原文中定位 (Offset Mapping):")
    final_claims = _resolve_offsets(test_article, claim_texts)
    
    # [Step 5] 调用 Planner 进行可疑度评分
    from agent.planner import build_plan
    plan = build_plan(final_claims)
    
    # 找出定位失败的（LLM 提取了但在原文找不着的）
    extracted_set = set(claim_texts)
    final_set = set(c.text for c in final_claims)
    failed = extracted_set - final_set
    
    print(f"\n[Step 5] 可疑度评分结果 (按分数降序排列):")
    for c in plan.claims:
        status = "🚩 [高危]" if c.suspicion_score > 0.5 else "ℹ️ [普通]"
        print(f"  {status} 分数: {c.suspicion_score:.2f} | 文本: {c.text}")
        
    if failed:
        print("\n[注意] 以下声明在定位阶段失败:")
        for f in failed:
            print(f"  ❌ {f}")

    if not final_claims:
        print("\n结论: 最终未提取到任何有效声明。")
    else:
        print(f"\n结论: 扫描与评分完成，共获取 {len(final_claims)} 条声明。")

if __name__ == "__main__":
    asyncio.run(debug_scanner())
