#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_intel.py
================
在 GitHub Actions（或本地）中运行：调用支持联网的 LLM 生成最新磁材竞社情报数据，
写入 data/intelligence.json 并提交，从而让托管在 GitHub Pages 的网站自动获取最新数据。

支持四种 provider（通过环境变量 LLM_PROVIDER 选择）：
  - openai     : OpenAI Responses API，启用 web_search 工具联网（需付费 Key）
  - perplexity : Perplexity sonar 模型，原生联网（需付费 Key）
  - gemini     : Google Gemini（Google AI Studio 免费额度，gemini-2.5-flash 自带 Google Search 联网，
                 单 Key、无需绑卡）★ 推荐免费方案（需能访问 Google）
  - cn-free    : 国内免翻墙免费组合 = 豆包搜索（联网，每月500次免费）+ 智谱 GLM-4-Flash（永久免费，OpenAI 兼容）
                 ★ 国内用户免翻墙首选

环境变量：
  LLM_PROVIDER           openai | perplexity | gemini | cn-free
  OPENAI_API_KEY         OpenAI 密钥
  PERPLEXITY_API_KEY     Perplexity 密钥
  GEMINI_API_KEY         Gemini 密钥（aistudio.google.com 免费获取）
  DOUBAO_SEARCH_API_KEY  豆包搜索 API Key（火山引擎控制台获取，每月500次免费）
  ZHIPU_API_KEY          智谱 API Key（open.bigmodel.cn 免费获取，glm-4-flash 永久免费）
  LLM_BASE_URL           可选，OpenAI 兼容端点
  LLM_MODEL              可选，模型名（gemini 默认 gemini-2.5-flash；cn-free 默认 glm-4-flash）
  DOUBAO_SEARCH_ENDPOINT 可选，豆包搜索端点（默认 https://open.feedcoopapi.com/search_api/web_search）
  DATA_PATH              数据文件路径（默认 data/intelligence.json）

安全/健壮性：
  - 任何 LLM 调用或解析失败都不会破坏现有数据（保留原文件，退出码 0）。
  - 对关键数组字段（priceHistory/indexHistory/activities/news/companies/currentPrices/forecast）
    做缺失保护：若 LLM 返回中缺失或为空，则回退保留原值，避免历史数据丢失。
"""

import os
import sys
import json
import datetime
import re

DATA_PATH = os.environ.get("DATA_PATH", "data/intelligence.json")


def log(msg):
    print(f"[update_intel] {msg}", flush=True)


def load_existing():
    try:
        with open(DATA_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"读取现有数据失败（将尝试从零生成）: {e}")
        return None


def save_data(data):
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log(f"已写入 {DATA_PATH}")


def extract_json(text):
    """从 LLM 返回文本中提取 JSON 对象。"""
    if not text:
        return None
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except Exception as e:
        log(f"JSON 解析失败: {e}")
        return None


def build_prompt(existing):
    today = datetime.date.today().strftime("%Y-%m-%d")
    if existing:
        keys = list(existing.keys())
        structure_hint = (
            f"现有数据顶层字段为：{keys}。\n"
            "请严格保持该 JSON 的【结构与字段】不变，仅更新各字段的【值】为最新（" + today + "）数据。\n"
            "关键规则：\n"
            "1) rareEarth.currentPrices 固定 9 个品类（金属镨/金属钕/金属镨钕/氧化镨钕/氧化钕/金属镝/金属铽/氧化镝/氧化铽），单位万元/吨，"
            "source 字段如实标注平台与报价日期（如'我的钢铁网 2026-08-xx'）。\n"
            "2) 价格来源优先级：我的钢铁网 → 亚洲金属网 → 百川盈孚；替代来源必须如实标注，不得虚标'我的钢铁网'。\n"
            "3) 金属铽严禁用'氧化铽+160.6'估算，必须用上海钢联月报实际月均价或相邻月插值。\n"
            "4) rareEarth.priceHistory 为月度数组（2025-01 起，每月 10 字段：month/prNdOxide/dysprosiumOxide/terbiumOxide/ndOxide/"
            "metalPrNd/metalNd/metalPr/metalDy/metalTb），请保留全部历史月份并在末尾追加/更新当月条目，不得删除任何字段或历史月。\n"
            "5) rareEarth.forecast 为未来 3 个月预测（以当月实际价为锚）：维护 horizon/forecastDate/basis/months，"
            "forecastDate 更新为今天，months 为 3 个月，各品类 confidence/logic 逐月填写。\n"
            "6) activities 竞社动态【不得收录正海磁材】；dimension 必须按实质归类为 digital/supply/market/tech（工艺技术归 tech）；"
            "建议保持 20 条以上、描述充实；保留历史条目并按日期倒序。\n"
            "7) companies[].financials.quarterly 为 2026 分季度数据，新财报追加新期间并保留历史。\n"
            "8) 所有数字与事实以联网搜索到的原文为准，不得编造；无法核实的字段保留原值。\n"
            "9) 返回 JSON 即可。说明：priceHistory、indexHistory、companies、comparison、sources、meta 等若无变化可省略不返回"
            "（系统会自动保留原值，避免免费模型输出超限）；但 activities、news 必须保留全部历史条目并追加本期新增；"
            "currentPrices、marketSummary、updateNote、forecast 必须返回最新值。\n"
        )
    else:
        structure_hint = (
            f"请生成完整的磁材竞社情报 JSON（" + today + "），结构包含：lastUpdated, updateNote, meta, "
            "rareEarth{currentPrices, priceHistory, indexHistory, marketSummary, priceHistoryNote, indexNote, forecast}, "
            "companies{financials.quarterly, productionSales, revenueStructure, geo}, comparison, activities, sources, news。"
        )
    return (
        "你是一名磁材（钕铁硼永磁材料）行业情报分析师。请使用联网搜索获取 "
        + today
        + " 最新的稀土价格行情（我的钢铁网 / 亚洲金属网 / 百川盈孚）与磁材上市公司"
        "（金力永磁 300748、宁波韵升 600366、中科三环 000970、正海磁材 300224、大地熊 688077、英洛华 000795）"
        "的最新竞社动态、财报、供应链、工艺技术等信息。\n\n"
        + structure_hint
        + "\n请仅返回符合上述结构的 JSON 对象，不要包含任何解释性文字或 Markdown 围栏。"
    )


def call_openai(prompt, model=None):
    from openai import OpenAI
    client = OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY"),
        base_url=os.environ.get("LLM_BASE_URL") or None,
    )
    model = model or os.environ.get("LLM_MODEL") or "gpt-4o"
    log(f"调用 OpenAI Responses API，模型={model}，启用 web_search")
    response = client.responses.create(
        model=model,
        input=prompt,
        tools=[{"type": "web_search_preview"}],
    )
    text = ""
    for item in response.output:
        if getattr(item, "type", "") == "message":
            for c in item.content:
                if getattr(c, "type", "") == "output_text":
                    text += c.text
    return text


def call_perplexity(prompt, model=None):
    import requests
    api_key = os.environ.get("PERPLEXITY_API_KEY")
    model = model or os.environ.get("LLM_MODEL") or "sonar"
    log(f"调用 Perplexity，模型={model}")
    resp = requests.post(
        "https://api.perplexity.ai/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": "你只返回 JSON 对象，不要任何解释文字。"},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def call_gemini(prompt, model=None):
    from google import genai
    from google.genai import types
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("未设置 GEMINI_API_KEY")
    model = model or os.environ.get("LLM_MODEL") or "gemini-2.5-flash"
    client = genai.Client(api_key=api_key)
    log(f"调用 Gemini，模型={model}，启用 google_search 工具（免费联网）")
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.3,
            max_output_tokens=32768,
        ),
    )
    return getattr(response, "text", "") or ""


def _doubao_search_once(query, api_key, count=8):
    """调用豆包搜索 Custom 版 API，返回拼接的检索结果文本。"""
    import requests
    endpoint = os.environ.get("DOUBAO_SEARCH_ENDPOINT") or "https://open.feedcoopapi.com/search_api/web_search"
    resp = requests.post(
        endpoint,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"Query": query, "SearchType": "web", "Count": count, "NeedSummary": True},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    lines = []
    web = (data.get("Result") or {}).get("WebResults") or []
    for it in web:
        title = it.get("Title", "") or ""
        summary = it.get("Summary", "") or ""
        url = it.get("Url", "") or ""
        if title or summary:
            lines.append(f"- 【{title}】{summary}（来源：{url}）")
    return "\n".join(lines)


def gather_doubao_context(api_key):
    """对若干查询调用豆包搜索，汇总为参考上下文。"""
    queries = [
        "稀土价格今日 氧化镨钕 金属镨钕 金属铽 我的钢铁网 2026年8月 报价",
        "金属镨 金属钕 氧化镝 氧化铽 氧化钕 金属镝 最新价格 2026年8月",
        "金力永磁 宁波韵升 中科三环 大地熊 英洛华 2026 最新动态 业绩 扩产 合作",
        "钕铁硼 稀土永磁 行业 最新新闻 政策 2026年8月",
    ]
    blocks = []
    for q in queries:
        try:
            r = _doubao_search_once(q, api_key)
            if r:
                blocks.append(f"查询「{q}」：\n{r}")
        except Exception as e:
            log(f"豆包搜索失败（{q}）：{e}")
    return "\n\n".join(blocks)


def call_zhipu(prompt, model=None):
    """调用智谱 GLM（OpenAI 兼容），永久免费的 glm-4-flash。"""
    from openai import OpenAI
    api_key = os.environ.get("ZHIPU_API_KEY")
    if not api_key:
        raise RuntimeError("未设置 ZHIPU_API_KEY")
    model = model or os.environ.get("LLM_MODEL") or "glm-4-flash"
    client = OpenAI(api_key=api_key, base_url="https://open.bigmodel.cn/api/paas/v4/")
    log(f"调用 智谱 GLM，模型={model}（合成 JSON）")
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=8192,
    )
    return getattr(resp.choices[0].message, "content", "") or ""


def call_cn_free(prompt):
    """国内免翻墙免费组合：豆包搜索（联网取数）+ 智谱 GLM-4-Flash（免费合成 JSON）。"""
    ctx = gather_doubao_context(os.environ.get("DOUBAO_SEARCH_API_KEY"))
    if ctx:
        full = prompt + "\n\n以下是联网搜索到的参考信息（请据此核对并更新数据，数字以参考信息原文为准，不要编造）：\n" + ctx
    else:
        full = prompt + "\n\n（联网搜索未返回结果，请基于已有数据谨慎更新，无法核实的字段保留原值）"
    return call_zhipu(full)


def call_llm(prompt):
    provider = (os.environ.get("LLM_PROVIDER") or "openai").lower()
    if provider == "perplexity":
        return call_perplexity(prompt)
    if provider == "gemini":
        return call_gemini(prompt)
    if provider == "cn-free":
        return call_cn_free(prompt)
    return call_openai(prompt)


# provider -> 必需的环境变量（密钥）。缺失即“配置错误”，应让 Actions 运行失败（变红），
# 而不是静默吞掉、误报“成功”。
REQUIRED_KEYS = {
    "openai": ["OPENAI_API_KEY"],
    "perplexity": ["PERPLEXITY_API_KEY"],
    "gemini": ["GEMINI_API_KEY"],
    "cn-free": ["DOUBAO_SEARCH_API_KEY", "ZHIPU_API_KEY"],
}


def validate_provider():
    """校验当前 provider 的必需密钥是否齐全；缺失则抛出 RuntimeError（配置错误）。"""
    provider = (os.environ.get("LLM_PROVIDER") or "openai").lower()
    need = REQUIRED_KEYS.get(provider, ["OPENAI_API_KEY"])
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            f"缺少必需密钥（provider={provider}）：{', '.join(missing)}。"
            f"请在仓库 Settings → Secrets and variables → Actions 中补充配置，"
            f"然后重新运行。"
        )


# 关键字段保护：LLM 返回缺失/为空时回退保留原值
PROTECTED_KEYS = ["priceHistory", "indexHistory", "activities", "news",
                  "companies", "currentPrices", "forecast", "comparison", "sources", "meta"]


def merge_protect(existing, new):
    if not isinstance(new, dict):
        log("LLM 返回非 JSON 对象，放弃更新")
        return None
    if not existing:
        return new
    merged = dict(existing)  # 以原数据为基底
    for k, v in new.items():
        merged[k] = v
    for key in PROTECTED_KEYS:
        if key in existing and (key not in new or not new.get(key)):
            merged[key] = existing[key]
            log(f"字段 '{key}' 在 LLM 返回中缺失/为空，已回退保留原值")
    merged["lastUpdated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    return merged


def main():
    existing = load_existing()
    prompt = build_prompt(existing)
    try:
        validate_provider()      # 配置校验：密钥缺失 => 配置错误，直接失败（运行变红）
        raw = call_llm(prompt)
    except RuntimeError as e:
        # 配置类错误（如密钥缺失）：明确失败，让 Actions 运行变红，避免“假成功”
        log(f"配置错误，终止更新: {e}")
        sys.exit(1)
    except Exception as e:
        # 运行时错误（如网络抖动）：保留现有数据，温和退出
        log(f"LLM 调用失败，保留现有数据: {e}")
        sys.exit(0)
    new = extract_json(raw)
    if not new:
        log("未能解析出有效 JSON，保留现有数据")
        sys.exit(0)
    merged = merge_protect(existing, new)
    if not merged:
        sys.exit(0)
    save_data(merged)
    log("更新完成")


if __name__ == "__main__":
    main()
