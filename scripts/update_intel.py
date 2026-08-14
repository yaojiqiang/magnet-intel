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
  - gemini     : Google Gemini（Google AI Studio 免费额度，gemini-2.5-flash 自带 Google Search 联网）
  - cn-free    : 国内免翻墙免费组合 = 豆包搜索（联网，每月500次免费）+ 智谱 GLM-4-Flash（永久免费）

环境变量：
  LLM_PROVIDER           openai | perplexity | gemini | cn-free
  OPENAI_API_KEY         OpenAI 密钥
  PERPLEXITY_API_KEY     Perplexity 密钥
  GEMINI_API_KEY         Gemini 密钥
  DOUBAO_SEARCH_API_KEY  豆包搜索 API Key
  ZHIPU_API_KEY          智谱 API Key
  LLM_BASE_URL / LLM_MODEL / DOUBAO_SEARCH_ENDPOINT / DATA_PATH  可选

【数据安全核心原则】—— 防止免费模型把整份文档写坏：
  - LLM 只能修改“白名单字段”（updateNote、rareEarth 下的 marketSummary / currentPrices /
    priceHistory / forecast / *Note / indexHistory）。activities / companies / comparison /
    sources / news / meta 等“大块内容”一律不接收模型输出，始终保留原值。
  - 每个白名单字段在写入前必须通过“结构校验”（项数、字段名、数字类型等）。
    校验不通过的字段 → 保留原值并告警；关键字段（currentPrices）校验不通过 →
    整个更新放弃并让运行变红（exit 1），避免“假成功”写入半截数据。
  - 运行时错误（网络抖动、密钥缺失等配置错误）按既有规则处理（配置错误变红，网络错误保留原值）。
"""

import os
import sys
import json
import copy
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


def data_fingerprint(d):
    """忽略 lastUpdated / updateNote 等元信息，仅对“实质内容”做指纹，用于判断是否真有更新。"""
    if not isinstance(d, dict):
        return json.dumps(d, ensure_ascii=False, sort_keys=True)
    core = {k: v for k, v in d.items() if k not in ("lastUpdated", "updateNote")}
    return json.dumps(core, ensure_ascii=False, sort_keys=True)


def _repair_json(text):
    """尝试修复免费模型常见的非法 JSON：行/块注释、尾随逗号、Python 字面量。"""
    t = re.sub(r"//[^\n]*", "", text)
    t = re.sub(r"/\*.*?\*/", "", t, flags=re.DOTALL)
    t = re.sub(r",(\s*[}\]])", r"\1", t)
    t = t.replace("True", "true").replace("False", "false").replace("None", "null")
    return t


def extract_json(text):
    """从 LLM 返回文本中提取 JSON 对象，必要时尝试修复。"""
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
        log(f"JSON 解析失败（首次）: {e}，尝试修复")
        try:
            return json.loads(_repair_json(text))
        except Exception as e2:
            log(f"JSON 修复后仍解析失败: {e2}")
            return None


# ---------------------------------------------------------------------------
# 白名单 + 结构校验
# ---------------------------------------------------------------------------

# currentPrices 每个品类对象必须包含的字段
_CP_REQUIRED = {"name", "category", "price", "unit", "change", "changeDesc", "date", "source"}
# priceHistory / indexHistory 每个月份对象必须包含的字段
_PH_REQUIRED = {"month", "prNdOxide", "dysprosiumOxide", "terbiumOxide", "ndOxide",
                "metalPrNd", "metalNd", "metalPr", "metalDy", "metalTb"}


def validate_currentPrices(v):
    """currentPrices：必须是 9 个品类对象的数组，每个对象字段齐全、price 为数字、unit 非空。"""
    if not isinstance(v, list) or len(v) != 9:
        return None, f"应为 9 项数组，实际 {type(v).__name__}，len={len(v) if hasattr(v, '__len__') else 'n/a'}"
    for i, it in enumerate(v):
        if not isinstance(it, dict):
            return None, f"第 {i} 项不是对象"
        miss = _CP_REQUIRED - set(it.keys())
        if miss:
            return None, f"第 {i} 项缺字段 {sorted(miss)}"
        if not isinstance(it.get("price"), (int, float)) or isinstance(it.get("price"), bool):
            return None, f"第 {i} 项 price 非数字：{it.get('price')!r}"
        if not isinstance(it.get("unit"), str) or not it.get("unit"):
            return None, f"第 {i} 项 unit 为空或非法：{it.get('unit')!r}"
        if not isinstance(it.get("change"), (int, float)) or isinstance(it.get("change"), bool):
            return None, f"第 {i} 项 change 非数字：{it.get('change')!r}"
    # 单位归一化：全站基准为“万元/吨”。若模型返回“元/吨”（price 量级 >1000），
    # 自动换算为万元/吨，避免现价表与走势图（万元/吨）差 1 万倍。
    for it in v:
        unit = it.get("unit") or ""
        price = it.get("price")
        if isinstance(price, (int, float)) and (("元" in unit and "万" not in unit) or (price > 1000 and "万" not in unit)):
            it["price"] = round(price / 10000, 2)
            ch = it.get("change")
            if isinstance(ch, (int, float)):
                it["change"] = round(ch / 10000, 2)
            it["unit"] = "万元/吨"
    return v, None


def validate_month_array(v, existing_len, name):
    """priceHistory / indexHistory：必须是列表，且不允许缩短（防止模型截断历史）。"""
    if not isinstance(v, list):
        return None, f"应为数组，实际 {type(v).__name__}"
    if len(v) < existing_len:
        return None, f"长度 {len(v)} < 原有 {existing_len}（不允许缩短/截断）"
    for i, it in enumerate(v):
        if not isinstance(it, dict):
            return None, f"第 {i} 项不是对象"
        miss = _PH_REQUIRED - set(it.keys())
        if miss:
            return None, f"第 {i} 项缺字段 {sorted(miss)}"
    return v, None


def validate_forecast(v):
    """forecast：字典，months 为 3 个月数组。"""
    if not isinstance(v, dict):
        return None, f"应为对象，实际 {type(v).__name__}"
    months = v.get("months")
    if not isinstance(months, list) or len(months) != 3:
        return None, f"months 应为 3 项数组，实际 {type(months).__name__} len={len(months) if hasattr(months, '__len__') else 'n/a'}"
    return v, None


# 已知可靠基准价（2026-08-13 实际价，单位万元/吨）——用于基线被污染时的兜底
REF_PRICES = {
    "金属镨": 101.5, "金属钕": 94.25, "金属镨钕": 87.25, "氧化镨钕": 71.7,
    "氧化钕": 77.0, "金属镝": 167.5, "金属铽": 820.5, "氧化镝": 138.5, "氧化铽": 662.0,
}
# 各品类绝对合理区间（万元/吨），用于拦截明显的量级/单位错误
BANDS = {
    "金属镨": (80, 130), "金属钕": (70, 130), "金属镨钕": (70, 120), "氧化镨钕": (50, 110),
    "氧化钕": (50, 120), "金属镝": (140, 185), "金属铽": (500, 1200), "氧化镝": (100, 220), "氧化铽": (450, 1000),
}


# 现货价格来源白名单（可信平台）。模型若返回白名单外的来源（如编造的“中国稀土行业协会”），
# 视为来源不可信，整条回退到上一交易日的可靠数据。
SOURCE_WHITELIST = {"我的钢铁网", "亚洲金属网", "百川盈孚", "上海金属网", "上海钢联"}


def _source_in_whitelist(src):
    """宽松匹配：白名单任一名称是 src 的子串、或 src 是白名单名称的子串，均视为可信。"""
    if not isinstance(src, str) or not src.strip():
        return False
    s = src.strip()
    for w in SOURCE_WHITELIST:
        if w in s or s in w:
            return True
    return False


def reconcile_cp(new_cp, existing_cp):
    """
    两层合理性守卫（保护稀土现价不被免费模型的量级/单位错误写崩）：
      1) 绝对合理区间（BANDS）：区间内才可能被采纳；
      2) 与“可信昨日价”比对，日度偏离 >±50% 视为跳变过大，信昨日价；
      3) 若昨日价本身已被污染（不在区间），回退到已知可靠基准 REF_PRICES。
    返回（已被校正的）列表。
    """
    old = {it.get("name"): it for it in existing_cp if isinstance(it, dict)} if isinstance(existing_cp, list) else {}
    for i, it in enumerate(new_cp):
        name = it.get("name")
        newp = it.get("price")
        lo, hi = BANDS.get(name, (0, 1e9))
        ref = REF_PRICES.get(name)
        prev = old.get(name)
        prevp = prev.get("price") if isinstance(prev, dict) else None

        if isinstance(newp, (int, float)) and lo <= newp <= hi:
            # 新值在合理区间
            if isinstance(prevp, (int, float)) and lo <= prevp <= hi and prevp != 0:
                trusted = newp if abs(newp - prevp) / prevp <= 0.5 else prevp
            else:
                trusted = newp  # 昨日价不可信，直接信新值（其已在合理区间）
        else:
            # 新值不合理 → 优先昨日价，其次可靠基准
            if isinstance(prevp, (int, float)) and lo <= prevp <= hi:
                trusted = prevp
            elif ref is not None and lo <= ref <= hi:
                trusted = ref
            else:
                trusted = newp  # 都没救，保留新值（总比崩好）

        if trusted is not None and trusted != newp:
            log(f"currentPrices[{name}] 价格 {newp} 不可信（区间 {lo}~{hi}），已校正为 {trusted}")
            if trusted == prevp and isinstance(prev, dict):
                it["price"] = prev.get("price")
                it["unit"] = prev.get("unit", "万元/吨")
                it["change"] = prev.get("change", 0)
                it["changeDesc"] = prev.get("changeDesc", "")
                it["date"] = prev.get("date")
                it["source"] = prev.get("source", it.get("source"))
            else:
                it["price"] = trusted
                it["unit"] = "万元/吨"
                it["change"] = 0
                it["changeDesc"] = "持平（模型数值疑似错误，已回退至可靠基准）"
                it["source"] = "我的钢铁网"
    return new_cp


def reconcile_cp_dates(new_cp, existing_cp):
    """
    日期一致性守卫（针对现价卡片的“报价日期”）：
    若某品类价格相对上一交易日未变化（数值相等，容差内），则【整条沿用】上一交易日的对象
    （日期/涨跌/涨跌说明/来源/单位全部原样保留），不产生任何多余改动——
    这样既避免“日期被推进到今天、但价格还是老的”这种日期与内容不匹配，
    也避免仅因文案变化而误判“内容已更新”导致 lastUpdated 被无辜推进。
    仅当价格确实变动时，才采用模型给出的新报价日期与涨跌信息。
    """
    old = {it.get("name"): it for it in existing_cp if isinstance(it, dict)} if isinstance(existing_cp, list) else {}
    for i, it in enumerate(new_cp):
        name = it.get("name")
        prev = old.get(name)
        if not isinstance(prev, dict):
            continue
        prev_price = prev.get("price")
        new_price = it.get("price")
        if isinstance(prev_price, (int, float)) and isinstance(new_price, (int, float)) \
                and abs(new_price - prev_price) <= 1e-6:
            # 价格未变 → 整体沿用上一交易日对象，零多余改动
            new_cp[i] = dict(prev)
    return new_cp


def reconcile_cp_sources(new_cp, existing_cp):
    """
    来源白名单守卫（用户明确要求）：
    模型返回的来源若不在白名单内（如编造的“中国稀土行业协会”），则该条目来源不可信，
    整条回退到上一交易日的可靠数据（价格/日期/涨跌/来源一并回退）；
    若上一交易日来源也不可信或不存在，则来源默认“我的钢铁网”、价格回落到可靠基准 REF_PRICES。
    目的：免费模型再也编不出假来源，且随之而来的可疑价格（如金属镝 192.5）也一并被校正。
    """
    old = {it.get("name"): it for it in existing_cp if isinstance(it, dict)} if isinstance(existing_cp, list) else {}
    for i, it in enumerate(new_cp):
        name = it.get("name")
        src = it.get("source")
        if _source_in_whitelist(src):
            continue
        prev = old.get(name)
        prev_src = prev.get("source") if isinstance(prev, dict) else None
        if isinstance(prev, dict) and _source_in_whitelist(prev_src):
            # 上一交易日来源可信 → 整条回退到上一交易日（来源自然也是可信的）
            new_cp[i] = dict(prev)
            log(f"currentPrices[{name}] 来源“{src}”不在白名单，已回退至上一交易日可靠来源“{prev_src}”（价格/日期一并回退）")
        else:
            # 上一交易日来源也不可信或缺失 → 价格回落至可靠基准，来源默认我的钢铁网
            ref = REF_PRICES.get(name)
            log(f"currentPrices[{name}] 来源“{src}”不在白名单且无可靠上一交易日来源，已回退至可靠基准/默认来源“我的钢铁网”")
            if isinstance(prev, dict):
                it["price"] = prev.get("price")
                it["unit"] = prev.get("unit", "万元/吨")
                it["change"] = prev.get("change", 0)
                it["changeDesc"] = prev.get("changeDesc", "")
                it["date"] = prev.get("date")
            elif ref is not None:
                it["price"] = ref
                it["unit"] = "万元/吨"
                it["change"] = 0
                it["changeDesc"] = "持平（模型来源不可信，已回退至可靠基准）"
                it["date"] = it.get("date")
            it["source"] = "我的钢铁网"
    return new_cp


def safe_merge(existing, new):
    """
    把模型输出 new 合并进 existing，仅允许白名单字段且必须通过校验。
    返回 (merged, errors, critical_fail)。
    - errors: 校验未通过被跳过的字段说明列表
    - critical_fail: 关键字段（currentPrices）校验未通过 → 应整体放弃更新
    """
    merged = copy.deepcopy(existing)
    errors = []
    critical_fail = False

    # 顶层白名单：updateNote
    if "updateNote" in new and isinstance(new["updateNote"], str):
        merged["updateNote"] = new["updateNote"]

    new_re = new.get("rareEarth")
    if not isinstance(new_re, dict):
        errors.append("rareEarth 缺失或非对象，跳过 rareEarth 下所有更新")
        return merged, errors, critical_fail

    merged_re = merged.setdefault("rareEarth", {})
    existing_re = existing.get("rareEarth", {}) if isinstance(existing, dict) else {}

    # marketSummary / *Note：纯字符串，直接采用
    for k in ("marketSummary", "priceHistoryNote", "indexNote"):
        if k in new_re and isinstance(new_re[k], str):
            merged_re[k] = new_re[k]

    # currentPrices（关键字段）
    if "currentPrices" in new_re:
        v, err = validate_currentPrices(new_re["currentPrices"])
        if err:
            errors.append(f"currentPrices 校验失败：{err}")
            critical_fail = True
        else:
            v = reconcile_cp(v, existing_re.get("currentPrices"))
            v = reconcile_cp_sources(v, existing_re.get("currentPrices"))
            v = reconcile_cp_dates(v, existing_re.get("currentPrices"))
            merged_re["currentPrices"] = v
    else:
        errors.append("currentPrices 未返回（保留原值）")

    # priceHistory（可选，禁止缩短）
    if "priceHistory" in new_re:
        v, err = validate_month_array(new_re["priceHistory"],
                                      len(existing_re.get("priceHistory", [])), "priceHistory")
        if err:
            errors.append(f"priceHistory 校验失败：{err}（保留原值）")
        else:
            merged_re["priceHistory"] = v

    # indexHistory（可选，禁止缩短）
    if "indexHistory" in new_re:
        v, err = validate_month_array(new_re["indexHistory"],
                                      len(existing_re.get("indexHistory", [])), "indexHistory")
        if err:
            errors.append(f"indexHistory 校验失败：{err}（保留原值）")
        else:
            merged_re["indexHistory"] = v

    # forecast（可选）
    if "forecast" in new_re:
        v, err = validate_forecast(new_re["forecast"])
        if err:
            errors.append(f"forecast 校验失败：{err}（保留原值）")
        else:
            merged_re["forecast"] = v

    # ★ 不处理 activities / companies / comparison / sources / news / meta / 其它：一律保留原值
    return merged, errors, critical_fail


def build_prompt(existing):
    today = datetime.date.today().strftime("%Y-%m-%d")
    return (
        "你是一名磁材（钕铁硼永磁材料）行业情报分析师。请使用下方【联网搜索参考信息】核对并"
        "更新最新数据，数字的来源与日期必须与参考信息一致，不得编造；无法核实的字段保留原描述即可。\n\n"
        "【输出限制 — 极其重要】\n"
        "你【只能】输出以下字段，严禁输出 activities / companies / comparison / sources / news / meta / "
        "indexHistory 等其它字段（那些内容系统会保留原值，你写了也会被丢弃，且容易写坏结构）：\n"
        "{\n"
        '  "updateNote": "本次更新说明（一句话，含数据来源与日期）",\n'
        '  "rareEarth": {\n'
        '    "marketSummary": "稀土及磁材市场综述（一段话）",\n'
        '    "currentPrices": [ 9 个品类对象，如下 ],\n'
        '    "priceHistory": [ 完整月度数组，必须包含全部历史月份（约20条）；若无法保证完整请勿返回此字段 ],\n'
        '    "forecast": { "horizon": "...", "forecastDate": "' + today + '", "basis": "...", "months": [3个月对象] }\n'
        "  }\n"
        "}\n\n"
        "currentPrices 固定 9 个品类（顺序不限）：金属镨、金属钕、金属镨钕、氧化镨钕、氧化钕、"
        "金属镝、金属铽、氧化镝、氧化铽。\n"
        "每个对象必须包含字段：name(品类名), category(轻稀土/重稀土), price(数字, 单位万元/吨), "
        "unit(如\"万元/吨\"), change(数字, 较上次涨跌万元), changeDesc(文字说明), date(报价日期 "
        + today[:7] + "-xx), source(平台名, 如\"我的钢铁网\")。\n\n"
        "价格来源白名单（仅允许以下，严禁编造）：我的钢铁网、亚洲金属网、百川盈孚、上海金属网、上海钢联。\n"
        "参考信息若来自其它平台，按优先级就近归入上述白名单之一并如实标注；"
        "严禁填写白名单外的来源（如“中国稀土行业协会”“网络”等模糊或编造来源）。\n"
        "金属铽严禁用\"氧化铽+160.6\"估算，必须用上海钢联月报实际月均价或相邻月插值。\n"
        "forecast.months 为未来 3 个月，每个对象含 category / basis / confidence / logic 等字段。\n\n"
        "请仅返回符合上述结构的 JSON 对象，不要包含任何解释性文字或 Markdown 围栏。"
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
    log(f"豆包搜索：{len(blocks)}/{len(queries)} 个查询返回结果")
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
        max_tokens=16384,
        response_format={"type": "json_object"},
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
        log("未能解析出有效 JSON，更新失败（请检查模型输出格式）")
        sys.exit(1)   # 模型已返回内容但无法解析 => 明确失败，运行变红

    merged, errors, critical_fail = safe_merge(existing, new)
    for e in errors:
        log(f"校验告警: {e}")

    if critical_fail:
        # 关键字段（currentPrices）校验未通过：放弃本次更新，避免写入半截/错误数据。
        # 运行变红，提醒检查；原数据保持完好（不会被提交覆盖）。
        log("关键字段校验未通过，放弃本次更新以避免损坏数据；请检查模型输出或重试。运行失败（变红）。")
        sys.exit(1)

    # ★ 日期一致性守卫：仅当“实质内容”发生变化时才推进 lastUpdated；
    #   内容未变化（如价格持平、模型仅复述）则保持原日期，不提交新版本，
    #   避免“日期已更新但内容没更新”导致页面日期与内容对不上。
    if existing and data_fingerprint(merged) == data_fingerprint(existing):
        merged["lastUpdated"] = existing.get("lastUpdated")
        merged["updateNote"] = existing.get("updateNote")
        save_data(merged)
        log("数据内容与上次一致，无实质更新：保留原 lastUpdated，不推进日期（git 不会产生新提交）")
    else:
        merged["lastUpdated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        save_data(merged)
        log("更新完成（内容有变化，已推进 lastUpdated）")


if __name__ == "__main__":
    main()
