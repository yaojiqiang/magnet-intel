#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_intel.py
================
在 GitHub Actions（或本地）中运行：调用支持联网的 LLM 生成最新磁材竞社情报数据，
写入 data/intelligence.json 并提交，从而让托管在 GitHub Pages 的网站自动获取最新数据。

支持两种 provider（通过环境变量 LLM_PROVIDER 选择，默认 openai）：
  - openai     : OpenAI Responses API，启用 web_search 工具联网（推荐，结构化输出稳定）
  - perplexity : Perplexity sonar 模型，原生联网

环境变量：
  LLM_PROVIDER       openai | perplexity（默认 openai）
  OPENAI_API_KEY     OpenAI 密钥
  PERPLEXITY_API_KEY Perplexity 密钥
  LLM_BASE_URL       可选，OpenAI 兼容端点（如 DeepSeek / 通义 / 自建代理；注意需兼容 Responses API + web_search）
  LLM_MODEL          可选，模型名（openai 默认 gpt-4o；perplexity 默认 sonar）
  DATA_PATH          数据文件路径（默认 data/intelligence.json）

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
            "9) 返回完整 JSON（含全部历史字段），不要省略任何已有数组。\n"
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


def call_llm(prompt):
    provider = (os.environ.get("LLM_PROVIDER") or "openai").lower()
    if provider == "perplexity":
        return call_perplexity(prompt)
    return call_openai(prompt)


# 关键字段保护：LLM 返回缺失/为空时回退保留原值
PROTECTED_KEYS = ["priceHistory", "indexHistory", "activities", "news",
                  "companies", "currentPrices", "forecast", "comparison", "sources"]


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
        raw = call_llm(prompt)
    except Exception as e:
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
