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
import urllib.parse
import urllib.request

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
    """forecast：字典，months 为未来 3 个月数组；每个月对象须含与 priceHistory 一致的 9 个价格字段。"""
    if not isinstance(v, dict):
        return None, f"应为对象，实际 {type(v).__name__}"
    months = v.get("months")
    if not isinstance(months, list) or len(months) != 3:
        return None, f"months 应为 3 项数组，实际 {type(months).__name__} len={len(months) if hasattr(months, '__len__') else 'n/a'}"
    price_keys = {"prNdOxide", "ndOxide", "dysprosiumOxide", "terbiumOxide",
                  "metalPrNd", "metalNd", "metalPr", "metalDy", "metalTb"}
    for i, m in enumerate(months):
        if not isinstance(m, dict):
            return None, f"months[{i}] 应为对象"
        miss = price_keys - set(m.keys())
        if miss:
            return None, f"months[{i}] 缺价格字段 {sorted(miss)}（每月必须含 9 个品类预测价）"
        for k in price_keys:
            if not isinstance(m.get(k), (int, float)) or isinstance(m.get(k), bool):
                return None, f"months[{i}].{k} 应为数字"
        if not m.get("month"):
            return None, f"months[{i}] 缺 month 字段"
    return v, None


_FC_PRICE_KEYS = ["prNdOxide", "ndOxide", "dysprosiumOxide", "terbiumOxide",
                  "metalPrNd", "metalNd", "metalPr", "metalDy", "metalTb"]


def _forecast_has_prices(fc):
    """判断现有 forecast.months 是否含可绘制的 9 个价格字段。"""
    if not isinstance(fc, dict):
        return False
    months = fc.get("months")
    if not isinstance(months, list) or len(months) == 0:
        return False
    return all(isinstance(m, dict) and all(k in m and isinstance(m[k], (int, float)) for k in _FC_PRICE_KEYS)
               for m in months)


def _build_forecast_fallback(price_history):
    """应急兜底【仅在联网推理完全不可用时启用】：采用“近 6 月均值附近保守持平（轻微均值回复）”，
    明确标注为非联网推理，避免被误认为据实推理的预测。正常情况下预测由 update_forecast() 联网推理生成。"""
    if not isinstance(price_history, list) or len(price_history) < 1:
        return None
    last = price_history[-1]
    n = len(price_history)
    window = price_history[-min(6, n):]
    avg = {}
    for k in _FC_PRICE_KEYS:
        vals = [w.get(k) for w in window if isinstance(w.get(k), (int, float))]
        avg[k] = (sum(vals) / len(vals)) if vals else last.get(k)
    try:
        y, m = (int(x) for x in last.get("month", "2026-08").split("-"))
        base = datetime.date(y, m, 1)
    except Exception:
        base = datetime.date(2026, 8, 1)
    months = []
    prev = {k: last.get(k) for k in _FC_PRICE_KEYS}
    for i in range(1, 4):
        d = base.replace(month=((base.month + i - 1) % 12) + 1, year=base.year + (base.month + i - 1) // 12)
        obj = {"month": d.strftime("%Y-%m"),
               "basis": "应急兜底：联网推理不可用，按近 6 月均值附近保守持平（非联网推理，仅供参考）",
               "confidence": "低", "logic": "数据不足，保守持平，非据实推理"}
        for k in _FC_PRICE_KEYS:
            target = avg.get(k)
            lv = prev.get(k)
            if isinstance(lv, (int, float)) and isinstance(target, (int, float)) and lv:
                # 向近 6 月均值轻微回复，单月幅度严格 ≤1%
                step = (target - lv) * 0.3
                step = max(min(step, abs(lv) * 0.01), -abs(lv) * 0.01)
                val = lv + step
            else:
                val = lv if isinstance(lv, (int, float)) else 0
            obj[k] = round(val, 2) if isinstance(val, float) else val
            prev[k] = obj[k]
        months.append(obj)
    return {"horizon": "近3个月（应急兜底）", "forecastDate": datetime.date.today().strftime("%Y-%m-%d"),
            "basis": "应急兜底：联网推理不可用，按近 6 月均值保守持平（非联网推理）", "months": months}



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

# 竞社动态（activities）每日增量更新相关
ACTIVITY_REQUIRED = {"company", "companyName", "dimension", "dimensionName", "date", "title", "description", "source"}
VALID_DIMENSIONS = {"market", "tech", "supply", "digital"}
ACTIVITY_MAX = 150  # 动态列表上限：保留最新的 150 条（竞社动态本就应是较长的信息流）
# 新闻动态（news）每日增量更新相关
NEWS_REQUIRED = {"date", "company", "title", "source"}
NEWS_MAX = 60  # 新闻列表上限：保留最新的 60 条（新闻流应尽可能覆盖多源信息）

# 竞社经营数据（companies）每日增量“报告刷新”相关
KNOWN_COMPANY_IDS = {"jinli", "yunsheng", "sanhuan", "zhenghai"}
FIN_BANDS = {
    "revenue": (0, 1000), "revenueYoY": (-100, 2000),
    "mainRevenue": (0, 1000), "mainRevenueYoY": (-100, 2000), "mainRevenuePct": (0, 100),
    "netProfit": (-50, 300), "netProfitYoY": (-100, 10000),
    "deductedNetProfit": (-50, 300), "deductedNetProfitYoY": (-100, 10000),
    "grossMargin": (-20, 100), "grossMarginPrev": (-20, 100), "grossMarginChange": (-50, 50),
    "eps": (-5, 15), "rdInvestment": (0, 60), "rdInvestmentYoY": (-100, 1000), "rdRevenuePct": (0, 50),
    "operatingCF": (-50, 200), "operatingCFYoY": (-200, 500), "operatingCFPrev": (-50, 200),
}
PS_BANDS = {
    "production": (0, 200000), "productionYoY": (-50, 200),
    "sales": (0, 200000), "salesYoY": (-50, 200),
    "inventory": (0, 100000), "inventoryYoY": (-50, 500),
    "capacity": (0, 200000), "actualCapacity": (0, 200000), "utilizationRate": (0, 100),
}
GEO_BANDS = {
    "domestic": (0, 500), "domesticPct": (0, 100),
    "overseas": (0, 500), "overseasPct": (0, 100),
    "usExport": (0, 200), "usExportYoY": (-100, 1000),
    "overseasGrossMargin": (-20, 100), "overseasGrossMarginChange": (-50, 50),
}
Q_BANDS = {
    "revenue": (0, 1000), "revenueYoY": (-100, 2000),
    "netProfit": (-50, 300), "netProfitYoY": (-100, 10000),
    "deductedNetProfit": (-50, 300), "deductedNetProfitYoY": (-100, 10000),
    "grossMargin": (-20, 100), "grossMarginChange": (-50, 50),
    "eps": (-5, 15), "operatingCF": (-50, 200),
}



def _norm(s):
    """用于去重归一化：去空白、转小写。"""
    return re.sub(r"\s+", "", (s or "").lower())


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
            # 价格未变：沿用上一交易日的可靠价格/单位/来源，但“报价日期/更新时间”必须推进到当天
            # （用户要求：即使价格无变化，当天查询了，更新时间就是当天）；涨跌置为持平。
            _today = datetime.date.today().strftime("%Y-%m-%d")
            it["price"] = prev.get("price", it.get("price"))
            it["unit"] = prev.get("unit", it.get("unit", "万元/吨"))
            it["source"] = prev.get("source", it.get("source"))
            it["change"] = 0
            it["changeDesc"] = "持平（与上一交易日一致）"
            it["date"] = _today
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


def validate_strategies(items):
    """
    校验正海磁材应对建议（strategies）：
      - 必须是列表，2~8 项；
      - 每项可为 字符串，或 对象 {title, detail}（兼容 action/desc/rationale 别名）；
      - title 非空且 ≤60 字，detail ≤300 字；
      - 非法项跳过，有效项不足 2 条或整体异常 → 返回 (None, 错误) 让调用方保留原值。
    """
    if not isinstance(items, list):
        return None, "非数组"
    if not (2 <= len(items) <= 8):
        return None, f"条数异常({len(items)})，应在2-8之间"
    out = []
    for it in items:
        if isinstance(it, str):
            t, d = it.strip(), ""
        elif isinstance(it, dict):
            t = str(it.get("title") or it.get("action") or "").strip()
            d = str(it.get("detail") or it.get("desc") or it.get("rationale") or "").strip()
            if not t and d:   # 只有说明没有标题时，把说明提升为标题
                t, d = d, ""
        else:
            continue
        if not t:
            continue
        if len(t) > 60 or len(d) > 300:
            return None, "单项过长（title≤60字, detail≤300字）"
        out.append({"title": t, "detail": d})
    if len(out) < 2:
        return None, "有效条数不足2条"
    return out, None


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

    # strategies（可选，正海磁材应对建议，结构校验）
    if "strategies" in new_re:
        v, err = validate_strategies(new_re["strategies"])
        if err:
            errors.append(f"strategies 校验失败：{err}（保留原值）")
        else:
            merged_re["strategies"] = v

    # currentPrices（关键字段）
    if "currentPrices" in new_re:
        v, err = validate_currentPrices(new_re["currentPrices"])
        if err:
            errors.append(f"currentPrices 校验失败：{err}")
            errors.append("currentPrices 校验失败：保留现有值，不阻断更新")
        else:
            v = reconcile_cp(v, existing_re.get("currentPrices"))
            v = reconcile_cp_dates(v, existing_re.get("currentPrices"))
            # 来源守卫必须最后执行，避免 reconcile_cp_dates 在价格持平时整条回退把被污染的旧来源又复制回来
            v = reconcile_cp_sources(v, existing_re.get("currentPrices"))
            # 最终强制：每条现价“报价日期/更新时间”必须推进到当天（用户要求：
            # 即使价格无变化，只要当天查询过，更新时间就是当天），杜绝日期回退/沿用旧日期。
            _today = datetime.date.today().strftime("%Y-%m-%d")
            for _it in v:
                if isinstance(_it, dict):
                    _it["date"] = _today
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

    # forecast（可选）：失败则用 priceHistory 兜底生成，确保预测线始终存在
    if "forecast" in new_re:
        v, err = validate_forecast(new_re["forecast"])
        if err:
            errors.append(f"forecast 校验失败：{err}（尝试兜底生成）")
            fb = _build_forecast_fallback(merged_re.get("priceHistory"))
            if fb:
                merged_re["forecast"] = fb
        else:
            merged_re["forecast"] = v
    else:
        # 模型未返回 forecast：若现有 forecast 也缺价格字段，则兜底生成
        cur = merged_re.get("forecast")
        if not cur or not _forecast_has_prices(cur):
            fb = _build_forecast_fallback(merged_re.get("priceHistory"))
            if fb:
                merged_re["forecast"] = fb

    # ★ 不处理 companies / comparison / sources / news / meta / 其它：一律保留原值
    #   （activities 由专门的 update_activities() 增量更新，见下方）
    return merged, errors, critical_fail


def merge_activities(existing, new_items):
    """
    竞社动态「增量合并」守卫（核心防写坏逻辑）：
      - 仅接收模型返回的“新动态”列表；
      - 逐条结构校验（必备字段、日期格式、维度合法性），非法项直接跳过；
      - 自动过滤禁收录企业（正海磁材）；
      - 去重：以 (company, 归一化标题) 为键，已收录的保留原值、不覆盖；
      - 合并后按日期倒序，截断到最新的 ACTIVITY_MAX(50) 条；
      - 若没有任何有效新增，则【原样返回 existing】（不重排、不改动），
        从而 data_fingerprint 不变、lastUpdated 不无辜推进。
    返回合并后的列表。
    """
    if not isinstance(new_items, list):
        log("activities 新数据非数组，保留现有")
        return existing if isinstance(existing, list) else []
    existing = existing if isinstance(existing, list) else []

    valid = []
    for i, it in enumerate(new_items):
        if not isinstance(it, dict):
            continue
        miss = ACTIVITY_REQUIRED - set(it.keys())
        if miss:
            log(f"activities 新项 {i} 缺字段 {sorted(miss)}，跳过")
            continue
        if "正海" in (it.get("companyName") or "") or str(it.get("company")).strip().lower() == "zhenghai":
            log(f"activities 新项 {i} 命中禁收录企业（正海磁材），跳过")
            continue
        date = str(it.get("date", "")).strip()
        if not re.match(r"^\d{4}-\d{1,2}-\d{1,2}$", date):
            log(f"activities 新项 {i} 日期非法 {date!r}，跳过")
            continue
        dim = it.get("dimension")
        if dim not in VALID_DIMENSIONS:
            log(f"activities 新项 {i} 维度非法 {dim!r}，跳过")
            continue
        valid.append({
            "company": str(it.get("company")).strip(),
            "companyName": str(it.get("companyName")).strip(),
            "dimension": dim,
            "dimensionName": str(it.get("dimensionName")).strip() or dim,
            "date": date,
            "title": str(it.get("title")).strip(),
            "description": str(it.get("description")).strip(),
            "source": str(it.get("source")).strip() or "公开信息",
            "sourceUrl": str(it.get("sourceUrl") or "").strip(),
        })

    if not valid:
        log("activities 无有效新项，保留现有列表")
        return existing

    existing_keys = {(a.get("company"), _norm(a.get("title"))): True
                     for a in existing if isinstance(a, dict)}
    combined = list(existing)
    added = 0
    for rec in valid:
        key = (rec["company"], _norm(rec["title"]))
        if key in existing_keys:
            continue
        if any((rec["company"], _norm(rec["title"])) == (x.get("company"), _norm(x.get("title")))
               for x in combined):
            continue
        rec["id"] = f"act-{rec['date'].replace('-', '')}-{added + 1:02d}"
        combined.append(rec)
        existing_keys[key] = True
        added += 1

    if added == 0:
        log("activities 无新增（均为已收录或重复），保持原列表与顺序，不推进日期")
        return existing

    combined.sort(key=lambda a: a.get("date", ""), reverse=True)
    if len(combined) > ACTIVITY_MAX:
        combined = combined[:ACTIVITY_MAX]
    log(f"activities 合并完成：原有 {len(existing)} + 新增 {added} = {len(combined)}（上限 {ACTIVITY_MAX}）")
    return combined


def update_activities(existing):
    """每日增量更新竞社动态：联网找最新增量 → 校验 → 去重 → 合并 → 截断到 50。"""
    prompt = build_activities_prompt(existing)
    try:
        raw = call_llm_activities(prompt)
    except Exception as e:
        log(f"activities LLM 调用失败，保留现有动态: {e}")
        return existing.get("activities") if isinstance(existing, dict) else []
    new = extract_json(raw)
    if not new or "activities" not in new or not isinstance(new["activities"], list):
        log("activities 未解析出有效 JSON（activities 数组），保留现有动态")
        return existing.get("activities") if isinstance(existing, dict) else []
    return merge_activities(
        existing.get("activities") if isinstance(existing, dict) else [],
        new["activities"],
    )


# ---------------------------------------------------------------------------
# 新闻动态（news）每日增量更新
# ---------------------------------------------------------------------------

def validate_news_item(it):
    """校验单条新闻；返回规范化字典或 None。"""
    if not isinstance(it, dict):
        return None
    miss = NEWS_REQUIRED - set(it.keys())
    if miss:
        return None
    date = str(it.get("date", "")).strip()
    if not re.match(r"^\d{4}-\d{1,2}-\d{1,2}$", date):
        return None
    title = str(it.get("title", "")).strip()
    if not title:
        return None
    return {
        "date": date,
        "company": str(it.get("company", "")).strip() or "行业",
        "title": title,
        "source": str(it.get("source", "")).strip() or "公开信息",
        "url": str(it.get("url") or "").strip(),
    }


def merge_news(existing, new_items):
    """新闻动态增量合并：校验 + 去重 + 按日期倒序 + 截断到 NEWS_MAX。"""
    if not isinstance(new_items, list):
        log("news 新数据非数组，保留现有")
        return existing if isinstance(existing, list) else []
    existing = existing if isinstance(existing, list) else []
    valid = [v for v in (validate_news_item(x) for x in new_items) if v]
    if not valid:
        log("news 无有效新项，保留现有列表")
        return existing
    existing_keys = {(n.get("company"), _norm(n.get("title"))): True
                     for n in existing if isinstance(n, dict)}
    combined = list(existing)
    added = 0
    for rec in valid:
        key = (rec["company"], _norm(rec["title"]))
        if key in existing_keys:
            continue
        if any((rec["company"], _norm(rec["title"])) == (x.get("company"), _norm(x.get("title")))
               for x in combined):
            continue
        combined.append(rec)
        existing_keys[key] = True
        added += 1
    if added == 0:
        log("news 无新增（均为已收录或重复），保持原列表与顺序，不推进日期")
        return existing
    combined.sort(key=lambda n: n.get("date", ""), reverse=True)
    if len(combined) > NEWS_MAX:
        combined = combined[:NEWS_MAX]
    log(f"news 合并完成：原有 {len(existing)} + 新增 {added} = {len(combined)}（上限 {NEWS_MAX}）")
    return combined


def update_news(existing):
    """每日增量更新新闻动态：联网找最新增量 → 校验 → 去重 → 合并 → 截断到 20。"""
    prompt = build_news_prompt(existing)
    try:
        raw = call_llm_news(prompt)
    except Exception as e:
        log(f"news LLM 调用失败，保留现有新闻: {e}")
        return existing.get("news") if isinstance(existing, dict) else []
    new = extract_json(raw)
    if not new or "news" not in new or not isinstance(new["news"], list):
        log("news 未解析出有效 JSON（news 数组），保留现有新闻")
        return existing.get("news") if isinstance(existing, dict) else []
    return merge_news(existing.get("news") if isinstance(existing, dict) else [], new["news"])


def build_news_prompt(existing):
    """构建新闻抽取的 prompt：让模型从【联网搜索参考信息】中抽出所有相关条目，
    去重交由代码完成（不再把已收录列表喂给模型让其'勿重复'，避免模型自我审查导致漏报）。
    回填模式(BACKFILL=1)放宽时间窗至 2025 年至今以充实历史。"""
    backfill = str(os.environ.get("BACKFILL", "")).lower() in ("1", "true", "yes", "backfill")
    window = (
        "请尽可能覆盖 2025 年至今（含 2025 全年及 2026 年）有重要行业/公司影响力的新闻，"
        "优先 2026 年，并回溯补齐 2025 年的关键事件，以充实历史列表。"
        if backfill else
        "优先收录 2026 年以来的最新新闻，必要时可回溯至 2025 年末以充实列表。"
    )
    return (
        "你是一名磁材（钕铁硼永磁材料）行业情报分析师，负责从下方【联网搜索参考信息】中抽取稀土永磁行业与 A股相关企业的新闻。\n"
        "下方已提供联网检索到的原始结果，请从中【提取所有相关、真实可核实的新闻条目】"
        "（不要遗漏，也不要编造；不同媒体对同一事件的报道请合并为一条，取最权威来源）。\n\n"
        "【目标范围】稀土永磁行业政策/价格/供需/出口新闻，以及 A股相关企业（金力永磁、宁波韵升、中科三环、"
        "大地熊、英洛华、正海磁材等）的新闻。\n\n"
        "【时间范围】" + window + "\n\n"
        "【输出要求】\n"
        "仅输出 JSON：{\"news\": [ 最多40条，按日期倒序（最新在前） ]}\n"
        "每条对象必须包含字段：\n"
        "  date(新闻日期，格式 YYYY-MM-DD), company(涉及企业名或\"行业\"), title(新闻标题), "
        "source(真实来源，如 证券时报/财联社/我的钢铁网/公司公告/新浪财经 等，严禁写\"网络\"等模糊来源), "
        "url(可选，原文链接；无则留空字符串)\n"
        "规则：只收录真实发生、可核实的新闻；不得编造日期、标题或来源；同一事件不要拆成多条。\n"
        "仅返回 JSON 对象，不要任何解释文字或 Markdown 围栏。"
    )


def gather_doubao_context_news(api_key):
    """新闻联网搜索：按『每家公司 + 行业多维』拆细查询，覆盖面远大于原先 3 个泛查询。"""
    queries = [
        "稀土永磁 行业 新闻 政策 价格 2026年8月 财联社 证券时报",
        "稀土 出口管制 供需 最新动态 2026年",
        "钕铁硼 稀土永磁 专利 技术 突破 2026",
        "金力永磁 2026 最新新闻 公告 业绩 扩产",
        "宁波韵升 2026 最新新闻 公告 业绩 扩产",
        "中科三环 2026 最新新闻 公告 业绩 重组",
        "大地熊 2026 最新新闻 公告 专利",
        "英洛华 2026 最新新闻 公告 业绩",
        "稀土永磁 企业 合作 订单 2026年",
        "正海磁材 稀土永磁 行业 新闻 2026年",
    ]
    blocks = []
    for q in queries:
        try:
            r = _doubao_search_once(q, api_key, count=15)
            if r:
                blocks.append(f"查询「{q}」：\n{r}")
        except Exception as e:
            log(f"豆包搜索(news)失败（{q}）：{e}")
    # 上下文长度保护：按整块累积，最多约 32000 字，避免单次输入过长
    kept, total = [], 0
    for b in blocks:
        if total + len(b) > 32000:
            break
        kept.append(b)
        total += len(b)
    ctx = "\n\n".join(kept)
    log(f"豆包搜索(news)：{len(blocks)}/{len(queries)} 个查询返回结果，上下文 {len(ctx)} 字")
    return ctx


def call_llm_news(prompt):
    provider = (os.environ.get("LLM_PROVIDER") or "openai").lower()
    if provider == "cn-free":
        ctx = gather_doubao_context_news(os.environ.get("DOUBAO_SEARCH_API_KEY"))
        if ctx:
            full = prompt + "\n\n以下是联网搜索到的参考信息（请据此核对，只输出真实可核实的增量新闻）：\n" + ctx
            return call_zhipu(full)
        # 豆包搜索无结果（Key 失效/接口异常）→ 改用智谱 web_search 自行联网检索，确保新闻段不靠幻觉
        log("news 豆包搜索无结果，改用智谱 web_search 自行联网检索最新新闻")
        return call_zhipu_websearch(prompt)
    if provider == "perplexity":
        return call_perplexity(prompt)
    if provider == "gemini":
        return call_gemini(prompt)
    return call_openai(prompt)


# ---------------------------------------------------------------------------
# 竞社经营数据（companies）每日增量“报告刷新”
# ---------------------------------------------------------------------------

def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _validate_quarterly_item(it):
    """校验单个季度数据对象；返回 True 表示可纳入。"""
    if not isinstance(it, dict) or not str(it.get("period", "")).strip():
        return False
    for k, v in it.items():
        if k in ("period", "periodLabel", "type", "note", "source", "sourceUrl"):
            continue
        if _is_num(v):
            if k in Q_BANDS and not (Q_BANDS[k][0] <= v <= Q_BANDS[k][1]):
                return False
        elif v is not None:
            return False
    return True


def deep_merge_company(existing_company, upd):
    """将单个公司的“已发布新数据”增量深度合并进原结构（数值越界/类型非法则整条放弃）。"""
    cid = upd.get("company")
    if cid not in KNOWN_COMPANY_IDS:
        log(f"companies 更新含未知公司代码 {cid!r}，跳过")
        return None
    result = copy.deepcopy(existing_company)

    fin = upd.get("financials")
    if isinstance(fin, dict):
        mfin = result.get("financials", {})
        for k, v in fin.items():
            if k in ("quarterly", "revenueStructure", "geo", "productionSales"):
                continue
            if _is_num(v):
                if k in FIN_BANDS and not (FIN_BANDS[k][0] <= v <= FIN_BANDS[k][1]):
                    log(f"companies[{cid}] financials.{k}={v} 超出合理区间，整条放弃")
                    return None
                mfin[k] = v
            elif isinstance(v, str):
                mfin[k] = v
            else:
                log(f"companies[{cid}] financials.{k} 类型非法，整条放弃")
                return None
        result["financials"] = mfin

    ps = upd.get("productionSales")
    if isinstance(ps, dict):
        mps = result.get("productionSales", {})
        for k, v in ps.items():
            if _is_num(v):
                if k in PS_BANDS and not (PS_BANDS[k][0] <= v <= PS_BANDS[k][1]):
                    log(f"companies[{cid}] productionSales.{k}={v} 超出合理区间，整条放弃")
                    return None
                mps[k] = v
            elif isinstance(v, str):
                mps[k] = v
            else:
                log(f"companies[{cid}] productionSales.{k} 类型非法，整条放弃")
                return None
        result["productionSales"] = mps

    g = upd.get("geo")
    if isinstance(g, dict):
        mg = result.get("geo", {})
        for k, v in g.items():
            if _is_num(v):
                if k in GEO_BANDS and not (GEO_BANDS[k][0] <= v <= GEO_BANDS[k][1]):
                    log(f"companies[{cid}] geo.{k}={v} 超出合理区间，整条放弃")
                    return None
                mg[k] = v
            elif isinstance(v, str):
                mg[k] = v
            else:
                log(f"companies[{cid}] geo.{k} 类型非法，整条放弃")
                return None
        result["geo"] = mg

    rs = upd.get("revenueStructure")
    if isinstance(rs, list) and rs:
        valid_rs = [x for x in rs if isinstance(x, dict) and str(x.get("segment", "")).strip()]
        if valid_rs:
            result["revenueStructure"] = valid_rs

    q = upd.get("quarterly")
    if isinstance(q, list):
        eq = list(result.get("quarterly", []))
        periods = {x.get("period") for x in eq if isinstance(x, dict)}
        for item in q:
            if not _validate_quarterly_item(item):
                log(f"companies[{cid}] quarterly 项校验失败，跳过该项")
                continue
            per = item.get("period")
            if per in periods:
                for ex in eq:
                    if ex.get("period") == per:
                        item_is_actual = (item.get("type") == "actual")
                        existing_is_actual = (ex.get("type") == "actual")
                        if existing_is_actual and not item_is_actual:
                            # 已发布实际报告的季度，不允许 LLM 回退为预告/占位；仅补充新给出的实际数值
                            log(f"companies[{cid}] {per} 已为 actual，忽略回退为 forecast 的更新")
                            for kk, vv in item.items():
                                if kk in ("period", "type", "periodLabel", "note", "source", "sourceUrl",
                                          "netProfitMin", "netProfitMax", "netProfitMid",
                                          "deductedNetProfitMin", "deductedNetProfitMax"):
                                    continue
                                elif _is_num(vv):
                                    ex[kk] = vv
                            break
                        if item_is_actual:
                            for _kdel in ("netProfitMin", "netProfitMax", "netProfitMid",
                                          "deductedNetProfitMin", "deductedNetProfitMax"):
                                ex.pop(_kdel, None)
                        for kk, vv in item.items():
                            if kk in ("period", "periodLabel", "type", "note", "source", "sourceUrl"):
                                ex[kk] = vv
                            elif _is_num(vv):
                                ex[kk] = vv
                        break
            else:
                eq.append(dict(item))
                periods.add(per)
        result["quarterly"] = eq

    return result



_CNINFO_ORGID_CACHE = {}


def _cninfo_url(code):
    """返回该公司巨潮资讯网公告页（正规、可点击直达）。"""
    num = re.sub(r'[^0-9]', '', code or '')
    if not num:
        return ''
    return f'https://www.cninfo.com.cn/new/disclosure/stock?stockCode={num}'


def _cninfo_column(code):
    """根据股票代码判断交易所板块：600/688/689 为 sse，其余为 szse。"""
    num = re.sub(r'[^0-9]', '', code or '')
    if num.startswith(('6', '688', '689')):
        return 'sse'
    return 'szse'


def _cninfo_orgid(code):
    """通过巨潮 topSearch 查询公司 orgId，结果缓存。"""
    global _CNINFO_ORGID_CACHE
    if code in _CNINFO_ORGID_CACHE:
        return _CNINFO_ORGID_CACHE[code]
    num = re.sub(r'[^0-9]', '', code or '')
    if not num:
        return None
    try:
        params = {"keyWord": num, "maxNum": "10"}
        req = urllib.request.Request(
            "https://www.cninfo.com.cn/new/information/topSearch/query",
            data=urllib.parse.urlencode(params).encode(),
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "User-Agent": "Mozilla/5.0",
            },
        )
        r = urllib.request.urlopen(req, timeout=15)
        data = json.loads(r.read().decode("utf-8"))
        for item in data or []:
            if item.get("code") == num:
                org_id = item.get("orgId")
                if org_id:
                    _CNINFO_ORGID_CACHE[code] = org_id
                    return org_id
    except Exception as e:
        log(f"cninfo orgId 查询失败 {code}: {e}")
    return None


def _cninfo_search_pdf(code, org_id, keyword, after_date=None, before_date=None):
    """在巨潮历史公告中按标题关键词搜索，返回直接 PDF 链接。"""
    try:
        num = re.sub(r'[^0-9]', '', code or '')
        after = after_date or "2026-01-01"
        before = before_date or "2026-12-31"
        params = {
            "pageNum": "1",
            "pageSize": "100",
            "tabName": "fulltext",
            "column": _cninfo_column(code),
            "stock": f"{num},{org_id}",
            "searchkey": "",
            "secid": "",
            "plate": _cninfo_column(code),
            "category": "category_all",
            "trade": "",
            "sortName": "",
            "sortType": "",
            "limit": "",
            "seDate": f"{after}~{before}",
        }
        req = urllib.request.Request(
            "https://www.cninfo.com.cn/new/hisAnnouncement/query",
            data=urllib.parse.urlencode(params).encode(),
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleKit/537.36",
            },
        )
        r = urllib.request.urlopen(req, timeout=20)
        anns = json.loads(r.read().decode("utf-8")).get("announcements") or []
        for a in anns:
            title = a.get("announcementTitle") or ""
            if keyword in title:
                adjunct = a.get("adjunctUrl")
                if adjunct:
                    return f"http://static.cninfo.com.cn/{adjunct}"
    except Exception as e:
        log(f"cninfo PDF 查询失败 {code} {keyword}: {e}")
    return None


def _cninfo_direct_url(code, period, qtype=None):
    """返回指定期别/类型的巨潮原始 PDF 链接；找不到返回 None。"""
    org_id = _cninfo_orgid(code)
    if not org_id:
        return None
    if period == "2026Q1":
        return _cninfo_search_pdf(code, org_id, "一季度报告", "2026-03-01", "2026-05-31")
    if period == "2026H1":
        if qtype == "actual":
            return _cninfo_search_pdf(code, org_id, "半年度报告", "2026-07-01", "2026-09-30")
        if qtype == "preliminary":
            return _cninfo_search_pdf(code, org_id, "半年度业绩快报", "2026-07-01", "2026-08-31")
        if qtype == "forecast":
            return _cninfo_search_pdf(code, org_id, "半年度业绩预告", "2026-06-01", "2026-08-15")
    return None


def _normalize_company_sources(companies):
    """竞社经营数据来源统一指向巨潮资讯网，并补全可点击 sourceUrl。

    优先使用巨潮原始公告 PDF 链接；找不到时保留已存在的特定来源链接；
    最后回退到巨潮公告列表页。
    """
    for c in companies:
        if not isinstance(c, dict):
            continue
        code = c.get('code', '')
        list_url = _cninfo_url(code)
        fin = c.setdefault('financials', {})
        # 顶层 sourceUrl：优先用 H1/Q1 的原始 PDF，否则保留已有，最后回退列表页
        if list_url and not fin.get('sourceUrl'):
            fin['sourceUrl'] = list_url
        s = fin.get('source') or ''
        if list_url and '巨潮' not in s and 'cninfo' not in s:
            fin['source'] = ('巨潮资讯网·' + s) if s else '巨潮资讯网'
        # 逐季度设置 sourceUrl
        h1_q = None
        for q in fin.get('quarterly', []):
            if not isinstance(q, dict):
                continue
            per = q.get('period')
            if per == '2026H1':
                h1_q = q
            existing_url = q.get('sourceUrl') or ''
            # 如果已经是巨潮原始 PDF，不再重复查询
            direct_url = None
            if 'static.cninfo.com.cn/finalpage' not in existing_url:
                direct_url = _cninfo_direct_url(code, per, q.get('type'))
            if direct_url:
                q['sourceUrl'] = direct_url
            elif existing_url:
                # 保留已有具体来源（如公司 IR、东方财富等）
                pass
            elif list_url:
                q['sourceUrl'] = list_url
            # source 文本加巨潮前缀（未核实的数据不覆盖）
            qs = q.get('source') or ''
            if '巨潮' not in qs and 'cninfo' not in qs and '未在巨潮' not in qs:
                q['source'] = ('巨潮资讯网·' + qs) if qs else '巨潮资讯网'
        # Q2 为推算，继承 H1 的 sourceUrl
        if h1_q:
            for q in fin.get('quarterly', []):
                if isinstance(q, dict) and q.get('period') == '2026Q2' and h1_q.get('sourceUrl'):
                    q['sourceUrl'] = h1_q['sourceUrl']
    return companies


def _add_q2_to_company(c):
    """由上半年(H1)减一季度(Q1)推算二季度数据并写入 quarterly。"""
    fin = c.get('financials') or {}
    qs = {q.get('period'): q for q in fin.get('quarterly', []) if isinstance(q, dict)}
    q1 = qs.get('2026Q1'); h1 = qs.get('2026H1')
    if not q1 or not h1 or '2026Q2' in qs:
        return
    url = _cninfo_url(c.get('code'))
    q2 = {'period': '2026Q2', 'periodLabel': '2026年二季度（推算）'}
    if _is_num(q1.get('revenue')) and _is_num(h1.get('revenue')):
        q2['revenue'] = round(float(h1['revenue']) - float(q1['revenue']), 2)
    if _is_num(q1.get('netProfit')) and _is_num(h1.get('netProfit')):
        q2['netProfit'] = round(float(h1['netProfit']) - float(q1['netProfit']), 2)
    elif _is_num(q1.get('netProfit')) and _is_num(h1.get('netProfitMin')) and _is_num(h1.get('netProfitMax')):
        q2['netProfitMin'] = round(float(h1['netProfitMin']) - float(q1['netProfit']), 2)
        q2['netProfitMax'] = round(float(h1['netProfitMax']) - float(q1['netProfit']), 2)
        q2['type'] = 'forecast'
    q2['source'] = '二季度为上半年减一季度推算（非单独披露）'
    q2['sourceUrl'] = h1.get('sourceUrl') or url
    q2['note'] = '二季度数据由上半年（H1）减一季度（Q1）推算，仅供参考'
    fin['quarterly'].append(q2)


def _add_q2_all(companies):
    for c in companies:
        if isinstance(c, dict):
            _add_q2_to_company(c)
    return companies


def update_companies(existing):
    """每日增量刷新竞社经营数据：联网核对是否已发布更新的财报/产销量等，深度合并进原结构。"""
    prompt = build_companies_prompt(existing)
    try:
        raw = call_llm_companies(prompt)
    except Exception as e:
        log(f"companies LLM 调用失败，保留现有经营数据: {e}")
        return existing.get("companies") if isinstance(existing, dict) else []
    new = extract_json(raw)
    if not new or "companyUpdates" not in new or not isinstance(new["companyUpdates"], list):
        log("companies 未解析出有效 JSON（companyUpdates 数组），保留现有经营数据")
        return existing.get("companies") if isinstance(existing, dict) else []
    existing_list = existing.get("companies") if isinstance(existing, dict) else []
    by_id = {c.get("id"): c for c in existing_list if isinstance(c, dict)}
    result = [c for c in existing_list]
    changed = 0
    for upd in new["companyUpdates"]:
        if not isinstance(upd, dict):
            continue
        cid = upd.get("company")
        if cid not in by_id:
            log(f"companies 更新含未知公司 {cid!r}，跳过")
            continue
        merged = deep_merge_company(by_id[cid], upd)
        if merged is None:
            continue
        for i, c in enumerate(result):
            if c.get("id") == cid:
                result[i] = merged
                by_id[cid] = merged
                changed += 1
                break
    if changed == 0:
        log("companies 无有效新数据（均为非法/越界/重复），保留现有经营数据，不推进日期")
        return existing_list
    log(f"companies 刷新完成：{changed} 家公司经营数据有更新")
    # 统一来源为巨潮资讯网可点击链接，并补齐二季度（H1-Q1 推算）
    result = _normalize_company_sources(result)
    result = _add_q2_all(result)
    return result


def build_companies_prompt(existing):
    today = datetime.date.today().strftime("%Y-%m-%d")
    comps = existing.get("companies", []) if isinstance(existing, dict) else []
    summary = "\n".join(
        f"- {c.get('name','')}（company=\"{c.get('id','')}\"）：最新已披露 {c.get('financials',{}).get('source','')}"
        for c in comps
    ) or "（暂无）"
    return (
        "你是一名磁材（钕铁硼永磁材料）行业情报分析师，负责跟踪A股稀土永磁上市公司的经营数据。\n"
        "请使用下方【联网搜索参考信息】，核对各公司是否已发布【比现有数据更新的】经营数据"
        "（如2026年半年度报告实际值、新的季度报告、修订后的业绩预告/快报、新披露的产销量/产能/收入结构/地区分布等）。\n\n"
        "【目标公司】（company 代码必须严格使用下列值）：\n"
        "  金力永磁 → company=\"jinli\"\n  宁波韵升 → company=\"yunsheng\"\n"
        "  中科三环 → company=\"sanhuan\"\n  正海磁材 → company=\"zhenghai\"\n\n"
        "【现有数据来源（仅供你判断是否有更新）】\n" + summary + "\n\n"
        "【输出要求 — 极重要】\n"
        "仅输出 JSON：{\"companyUpdates\": [ 仅包含【确实有新发布数据】的公司；若无任何公司有新数据，返回空数组 [] ]}\n"
        "每个公司对象结构（只填你确认有更新的字段，不要编造、不要把旧值原样抄回）：\n"
        "{\n"
        "  \"company\": \"jinli\",\n"
        "  \"financials\": { \"revenue\": 数字, \"netProfit\": 数字, \"grossMargin\": 数字, ... 仅变动的标量 },\n"
        "  \"productionSales\": { \"production\": 数字, \"sales\": 数字, \"capacity\": 数字, ... 仅变动的标量 },\n"
        "  \"geo\": { \"domestic\": 数字, \"overseas\": 数字, ... 仅变动的标量 },\n"
        "  \"revenueStructure\": [ { \"segment\": \"...\", \"revenue\": 数字, \"salesYoY\": 数字, \"note\": \"...\" } ],\n"
        "  \"quarterly\": [ { \"period\": \"2026H1\", \"type\": \"actual\", \"revenue\": 数字, \"netProfit\": 数字, ... } ]\n"
        "}\n"
        "规则：\n"
        "1) 数字单位：营收/净利/经营现金流为【亿元】，毛利率/同比为【%】，产量为【吨】；\n"
        "2) 仅报告真实发布的数据，并在对应字段旁用 source 注明来源（如\"2026年半年度报告\"）；\n"
        "3) 严禁编造；若某字段无新数据，【不要】把它写进对象（深度合并会保留原值）；\n"
        "4) revenueStructure 与 quarterly 仅在你确认整体有变/有新报告时才提供，否则省略。\n"
        "5) 若某公司已正式发布 2026 年半年度报告（非预告），必须将 financials.quarterly 中 period=\"2026H1\" 的对象 type 由 \"forecast\" 改为 \"actual\"，"
        "并按正式报告填入实际营收/净利/经营现金流等数值（不要保留 netProfitMin/Max 等预告区间字段）；"
        "同时据正式半年报修正 financials 顶层与 revenueStructure/geo 的半年口径，并将 financials.source 注明\"2026年半年度报告\"。\n"
        "仅返回 JSON 对象，不要任何解释文字或 Markdown 围栏。"
    )


def gather_doubao_context_companies(api_key):
    queries = [
        "宁波韵升 600366 2026年半年度报告 营业收入 归母净利润 经营现金流 实际数据",
        "宁波韵升 2026半年报 分产品收入 海外收入 毛利率 产能",
        "金力永磁 300748 2026年半年度报告 营业收入 归母净利润 经营现金流",
        "金力永磁 2026半年报 分产品收入 海外收入 毛利率",
        "中科三环 000970 2026年半年度报告 营业收入 归母净利润 经营现金流",
        "中科三环 2026半年报 分产品收入 海外收入 毛利率",
        "正海磁材 300224 2026年半年度报告 营业收入 归母净利润 经营现金流",
        "正海磁材 2026半年报 分产品收入 海外收入 毛利率",
    ]
    blocks = []
    for q in queries:
        try:
            r = _doubao_search_once(q, api_key)
            if r:
                blocks.append(f"查询「{q}」：\n{r}")
        except Exception as e:
            log(f"豆包搜索(companies)失败（{q}）：{e}")
    log(f"豆包搜索(companies)：{len(blocks)}/{len(queries)} 个查询返回结果")
    return "\n\n".join(blocks)


def call_llm_companies(prompt):
    provider = (os.environ.get("LLM_PROVIDER") or "openai").lower()
    if provider == "cn-free":
        ctx = gather_doubao_context_companies(os.environ.get("DOUBAO_SEARCH_API_KEY"))
        if ctx:
            full = prompt + "\n\n以下是联网搜索到的参考信息（请据此核对，只输出确有新发布的经营数据增量）：\n" + ctx
            return call_zhipu(full)
        # 豆包搜索无结果（Key 失效或接口异常）→ 改用智谱自带 web_search 自行联网检索，
        # 确保竞社经营数据在豆包不可用时仍能更新，而不是整段空白。
        log("companies 豆包搜索无结果，改用智谱 web_search 自行联网检索最新经营数据")
        return call_zhipu_websearch(prompt)
    if provider == "perplexity":
        return call_perplexity(prompt)
    if provider == "gemini":
        return call_gemini(prompt)
    return call_openai(prompt)


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
        '    "strategies": [ {"title":"对策短名(≤20字)","detail":"具体做法与理由(1-2句)"} ],\n'
        '    "currentPrices": [ 9 个品类对象，如下 ],\n'
        '    "priceHistory": [ 完整月度数组，必须包含全部历史月份（约20条）；若无法保证完整请勿返回此字段 ]\n'
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
        "价格来源优先级（严禁虚标）：①首选我的钢铁网（mysteel.com），现货/月均价/指数一律优先以其为准，来源标注\"我的钢铁网\"；②会员无法获取时改用亚洲金属网（asianmetal.cn）；③再不行改用百川盈孚（baiinfo.com）。使用替代来源须如实标注实际平台名与报价日期（如\"亚洲金属网 2026-08-xx\"），不得伪称\"我的钢铁网\"。\n"
        "时效性：我的钢铁网当日行情快讯通常上午 11:00 后发布；若个别品类当日快讯尚未披露（如周末/节假日无行情），date 须填最近一次实报价日期（如取到 8/12 快讯就填 2026-08-12），并在 updateNote 注明\"当日无该品类最新快讯，沿用最近报价\"，【严禁】把未披露品类虚标为运行当天日期。\n"
        "（注：未来 3 个月稀土价格预测由独立模块基于【联网检索 + 多因子推理】生成，不在此处输出，"
        "以免退化为对真实价格的简单线性外推。）\n\n"
        "strategies 为正海磁材（钕铁硼永磁材料制造商，约70%成本来自稀土原料，属价格敏感型下游企业）的应对建议："
        "结合当日稀土价格走势（涨跌方向、轻/重稀土分化、供给紧张度、成本传导难度），给出 4-6 条具体可执行的经营/采购/技术对策，"
        "例如：原材料锁价长协与套期保值、战略库存择时调节、提高高毛利/高牌号产品占比、推进无重稀土与晶界扩散技术降低单耗、"
        "回收料利用、向下游客户进行价格传导、拓展海外/非稀土业务对冲等。每条为对象 {\"title\":\"对策短名\",\"detail\":\"具体做法与理由（1-2句）\"}，"
        "title 简洁有力（≤20字）、detail 说明做法与理由。若市场平稳，也须给出常态化稳健经营建议；对策须与当日市场实际相符，不得空泛。\n\n"
        "请仅返回符合上述结构的 JSON 对象，不要包含任何解释性文字或 Markdown 围栏。"
    )


# ---------------------------------------------------------------------------
# 未来 3 个月稀土价格预测（独立模块）
#   核心目标：预测必须基于【联网检索的真实信息 + 多因子推理】，而【不是】对历史价格做
#   简单线性外推。故单独成模块：针对“供需 / 政策与贸易 / 下游需求 / 宏观汇率 / 季节性”
#   做定向联网检索，再把检索结果喂给模型做严格推理，最后用绝对区间 + 单月漂移上限做护栏。
# ---------------------------------------------------------------------------

# forecast 的 9 个价格字段 -> BANDS 的中文键（用于绝对合理区间校验）
_FC_TO_BAND = {
    "prNdOxide": "氧化镨钕", "ndOxide": "氧化钕", "dysprosiumOxide": "氧化镝",
    "terbiumOxide": "氧化铽", "metalPrNd": "金属镨钕", "metalNd": "金属钕",
    "metalPr": "金属镨", "metalDy": "金属镝", "metalTb": "金属铽",
}
# 9 个价格字段 -> 中文名（用于把基线价拼成可读文本）
_FC_TO_NAME = {k: v for k, v in _FC_TO_BAND.items()}


def build_forecast_prompt(existing):
    """构建“未来 3 个月稀土价格预测”的专用 prompt：强调联网检索 + 多因子推理，并给出最新实际基线。"""
    today = datetime.date.today()
    today_s = today.strftime("%Y-%m-%d")
    re_existing = (existing.get("rareEarth", {}) if isinstance(existing, dict) else {})
    ph = re_existing.get("priceHistory") or []
    n = len(ph)

    def fmt_vals(rec):
        return "、".join(
            f"{_FC_TO_NAME[k]}{rec.get(k)}" for k in _FC_PRICE_KEYS if isinstance(rec.get(k), (int, float))
        )

    recent = ph[-3:] if ph else []
    if recent:
        base_text = "近期实际月均价（万元/吨）：\n" + "\n".join(
            f"- {r.get('month')}：{fmt_vals(r)}" for r in recent
        )
    else:
        base_text = "（无历史月均价数据，请基于联网检索到的当前报价作为基线）"

    months_labels = []
    for i in range(1, 4):
        d = today.replace(month=(today.month + i - 1) % 12 + 1,
                          year=today.year + (today.month + i - 1) // 12)
        months_labels.append(d.strftime("%Y-%m"))

    return (
        "你是一名资深稀土 / 永磁行业价格预测分析师。请基于下方【联网检索参考信息】与你的专业知识，"
        "对未来 3 个月（" + "、".join(months_labels) + "）的 9 个稀土品类月均价做出预测。\n\n"
        "【预测方法 —— 必须严格执行】\n"
        "1) 综合研判以下多因子，再给出方向（涨 / 跌 / 震荡）与幅度：\n"
        "   · 下游需求：新能源汽车、风电、人形机器人、消费电子、工业电机等的景气与排产；\n"
        "   · 供给端：国内稀土开采 / 冶炼分离配额与指标、中国稀土集团与北方稀土排产、"
        "缅甸矿进口恢复/受限、海外 Lynas / MP Materials 供给；\n"
        "   · 政策与贸易：出口管制 / 管制清单、关税、收储与放储、环保与能耗约束；\n"
        "   · 库存与基差、宏观与汇率（美元指数、中美利差）、季节性"
        "（金九银十、年末备货、春节前后）；\n"
        "2) 对每一品类、每一未来月份，在 logic 字段给出【一句话推理依据】，说明驱动该方向的基本面原因；\n"
        "3) 预测必须是上述多因子推理的结果，【严禁】仅对历史价格做线性外推；"
        "即便你认为将延续趋势，也须在依据中写明驱动该趋势的基本面原因。\n\n"
        "【最新实际基线】\n" + base_text + "\n\n"
        "【输出要求 —— 仅输出 JSON】\n"
        "{\"forecast\": {\"horizon\": \"近3个月\", \"forecastDate\": \"" + today_s + "\", "
        "\"basis\": \"综合供需/政策/需求与价格走势的推理结论（1-2句）\", "
        "\"months\": [ 3 个对象，月份依次为 " + "、".join(months_labels) + " ]}}\n"
        "每个月份对象必须包含字段：\n"
        "  month(如 \"" + months_labels[0] + "\"), category(轻稀土/重稀土), "
        "confidence(高/中/低), basis(该月综合依据，1句), logic(该月推理要点，1句),\n"
        "  以及 9 个价格字段(数字，万元/吨)：\n"
        "  prNdOxide(氧化镨钕), ndOxide(氧化钕), dysprosiumOxide(氧化镝), terbiumOxide(氧化铽),\n"
        "  metalPrNd(金属镨钕), metalNd(金属钕), metalPr(金属镨), metalDy(金属镝), metalTb(金属铽)\n"
        "约束：预测价相对上一月（或最新实际价）的单月变化通常不超过 ±15%；数值须落在合理区间"
        "（氧化镨钕/氧化钕 50-110、金属系 70-130、氧化镝 100-220、氧化铽 450-1000、"
        "金属镝 140-185、金属铽 500-1200 万元/吨）。\n"
        "仅返回 JSON 对象，不要任何解释文字或 Markdown 围栏。"
    )


def gather_doubao_context_forecast(api_key):
    """针对“未来 3 个月稀土价格预测”的定向联网检索：覆盖供需、政策贸易、重稀土供给、下游需求、海外供给、季节性等。"""
    queries = [
        "稀土价格走势 2026年下半年 预测 氧化镨钕 镨钕金属 机构观点 分析",
        "稀土 供需 2026下半年 北方稀土 中国稀土集团 开采配额 冶炼分离指标 排产",
        "稀土 出口管制 2026 最新 影响 镝 铽 价格",
        "缅甸 稀土矿 进口 2026 停产 恢复 重稀土 供应 影响",
        "氧化镝 氧化铽 重稀土 价格 2026 后市 展望 预测",
        "钕铁硼 永磁 需求 2026 新能源汽车 风电 人形机器人 对稀土价格拉动",
        "Lynas MP Materials 稀土供应 2026 产能 价格影响",
        "稀土 收储 放储 2026 政策 对价格影响",
        "美元指数 汇率 2026 稀土价格 影响 分析",
        "稀土 价格 2026年9月 10月 11月 走势 预测 金九银十 年末备货",
    ]
    blocks = []
    for q in queries:
        try:
            r = _doubao_search_once(q, api_key, count=15)
            if r:
                blocks.append(f"查询「{q}」：\n{r}")
        except Exception as e:
            log(f"豆包搜索(forecast)失败（{q}）：{e}")
    log(f"豆包搜索(forecast)：{len(blocks)}/{len(queries)} 个查询返回结果")
    return "\n\n".join(blocks)


def call_llm_forecast(prompt):
    """预测专用 LLM 调用：cn-free 下用定向检索上下文 + 智谱合成；其它 provider 自带联网。"""
    provider = (os.environ.get("LLM_PROVIDER") or "openai").lower()
    if provider == "cn-free":
        ctx = gather_doubao_context_forecast(os.environ.get("DOUBAO_SEARCH_API_KEY"))
        if ctx:
            full = (prompt + "\n\n以下是联网检索到的稀土市场参考信息"
                    "（请据此严格推理未来 3 个月各品类价格，不要简单线性外推历史）：\n" + ctx)
            return call_zhipu(full)
        # 豆包搜索无结果 → 改用智谱 web_search 自行联网检索稀土市场信息后推理
        log("forecast 豆包搜索无结果，改用智谱 web_search 自行联网检索稀土市场信息")
        return call_zhipu_websearch(prompt)
    if provider == "perplexity":
        return call_perplexity(prompt)
    if provider == "gemini":
        return call_gemini(prompt)
    return call_openai(prompt)


def reconcile_forecast(fc, existing):
    """预测护栏：绝对合理区间 + 单月漂移上限（±18%），防止模型输出失控；保留推理出的合理差异。"""
    ph = ((existing.get("rareEarth", {}) or {}).get("priceHistory")
          if isinstance(existing, dict) else None) or []
    last = ph[-1] if ph else {}
    prev = {k: last.get(k) for k in _FC_PRICE_KEYS}
    for i, m in enumerate(fc.get("months", [])):
        if not isinstance(m, dict):
            continue
        for k in _FC_PRICE_KEYS:
            val = m.get(k)
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                val = prev.get(k) if isinstance(prev.get(k), (int, float)) else 0
                m[k] = val
            # 绝对合理区间
            band_key = _FC_TO_BAND.get(k)
            if band_key and band_key in BANDS:
                lo, hi = BANDS[band_key]
                if val < lo or val > hi:
                    val = max(lo, min(hi, val))
                    m[k] = round(val, 2)
            # 单月漂移上限 ±18%（相对上一月或最新实际价），防止失控
            ref = fc["months"][i - 1].get(k) if i > 0 else prev.get(k)
            if isinstance(ref, (int, float)) and ref:
                cap = abs(ref) * 0.18
                if abs(val - ref) > cap:
                    val = ref + max(-cap, min(cap, val - ref))
                    m[k] = round(val, 2)
            prev[k] = m[k]
    return fc


def update_forecast(existing):
    """生成基于联网检索与多因子推理的 3 个月价格预测，覆盖 safe_merge 的结果（避免退化为纯价格外推）。"""
    prompt = build_forecast_prompt(existing)
    price_history = ((existing.get("rareEarth", {}) or {}).get("priceHistory")
                     if isinstance(existing, dict) else None)
    try:
        raw = call_llm_forecast(prompt)
    except Exception as e:
        log(f"forecast LLM 调用失败，使用应急兜底: {e}")
        return _build_forecast_fallback(price_history)
    new = extract_json(raw)
    fc = None
    if isinstance(new, dict):
        fc = new.get("forecast") if isinstance(new.get("forecast"), dict) else None
        if fc is None and isinstance(new.get("months"), list):
            fc = new  # 容错：模型直接返回了 {months:[...]}
    if not isinstance(fc, dict):
        log("forecast 未解析出有效 JSON，使用应急兜底")
        return _build_forecast_fallback(price_history)
    v, err = validate_forecast(fc)
    if err:
        log(f"forecast 校验失败：{err}，使用应急兜底")
        return _build_forecast_fallback(price_history)
    v = reconcile_forecast(v, existing)
    log("forecast 已基于联网检索 + 多因子推理重新生成")
    return v


def build_activities_prompt(existing):
    """构建竞社动态抽取 prompt：让模型从【联网搜索参考信息】中抽出所有相关动态，
    去重交由代码完成（不再让模型'勿重复已收录'，避免漏报）。回填模式放宽时间窗。"""
    backfill = str(os.environ.get("BACKFILL", "")).lower() in ("1", "true", "yes", "backfill")
    window = (
        "请尽可能覆盖 2025 年至今（含 2025 全年及 2026 年）的企业公开动态，优先 2026 年，"
        "并回溯补齐 2025 年有影响力的事件（产能/订单/合作/业绩/专利/数字化等），以充实历史列表。"
        if backfill else
        "优先收录 2026 年以来的最新动态，必要时可回溯至 2025 年末以充实列表。"
    )
    return (
        "你是一名磁材（钕铁硼永磁材料）行业情报分析师，负责从下方【联网搜索参考信息】中抽取 A股稀土永磁企业的公开动态。\n"
        "下方已提供联网检索到的原始结果，请从中【提取所有相关、真实可核实的动态条目】"
        "（不要遗漏，也不要编造；不同来源报道同一事件请合并为一条）。\n\n"
        "【目标企业】（company 代码必须严格使用下列值）：\n"
        "  金力永磁 → company=\"jinli\"\n"
        "  宁波韵升 → company=\"yunsheng\"\n"
        "  中科三环 → company=\"sanhuan\"\n"
        "  大地熊   → company=\"dadi\"\n"
        "  英洛华   → company=\"yingluohua\"\n"
        "【严禁收录】正海磁材（含其任何动态，一律跳过）。\n\n"
        "【维度 dimension 取值与对应 dimensionName】：\n"
        "  market  → 市场\n  tech    → 工艺技术\n  supply  → 供应链\n  digital → 数字化\n\n"
        "【维度归类澄清】工艺/技术类内容（晶界扩散、无重稀土、磁能积提升、研发工艺突破、新产品工艺等）必须归 tech（工艺技术）；只有明确属于智能工厂、AI 质检、自动化产线、工业互联网/数字化管控等智能制造/数字化内容才归 digital（数字化），二者不可混用。\n"
        "【时间范围】" + window + "\n\n"
        "【输出要求】\n"
        "仅输出 JSON：{\"activities\": [ 最多60条，按日期倒序（最新在前） ]}\n"
        "每条对象必须包含字段：\n"
        "  company(上述代码), companyName(企业中文名), dimension(上述4个值之一), dimensionName(对应中文),\n"
        "  date(事件发生日期，格式 YYYY-MM-DD), title(动态标题), description(2-4句客观描述，含关键数字/金额/比例),\n"
        "  source(真实来源，如 公司公告/证券时报/上证报/公司官网/国家知识产权局 等，严禁写“网络”等模糊来源),\n"
        "  sourceUrl(可选，原文链接)\n"
        "规则：只收录真实发生、可核实的动态；不得编造日期、金额或来源；同一事件不要拆成多条。\n"
        "【内容充实度与来源】尽量补充新动态条目（建议累计 20 条以上），description 须充实具体（含关键数字、背景、意义），不得仅一句话带过；可重点参考各公司及行业协会微信公众号发布的动态，source 标注公众号名称、sourceUrl 填文章链接，但须真实可核验，不得编造。\n"
        "仅返回 JSON 对象，不要任何解释文字或 Markdown 围栏。"
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


def _doubao_search_once(query, api_key, count=15):
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


def gather_doubao_context_activities(api_key):
    """竞社动态联网搜索：按『每家公司 + 维度/行业』拆细查询，覆盖面远大于原先 5 个泛查询。"""
    queries = [
        "金力永磁 2026 公告 扩产 业绩 稀土永磁 合作 订单",
        "宁波韵升 2026 公告 扩产 业绩 稀土永磁 合作",
        "中科三环 2026 公告 重组 收购 扩产 业绩",
        "大地熊 2026 公告 专利 业绩 稀土永磁",
        "英洛华 2026 公告 业绩 重组 稀土永磁",
        "稀土永磁 钕铁硼 行业 企业 产能 订单 合作 2026年",
        "稀土永磁 企业 专利 技术 突破 国家知识产权局 2026",
        "稀土永磁 企业 机构调研 数字化 2026年",
        "钕铁硼 稀土 出口 企业 供应链 2026年",
        "金力永磁 2026 半年报 业绩 机构调研",
    ]
    blocks = []
    for q in queries:
        try:
            r = _doubao_search_once(q, api_key, count=15)
            if r:
                blocks.append(f"查询「{q}」：\n{r}")
        except Exception as e:
            log(f"豆包搜索(activities)失败（{q}）：{e}")
    kept, total = [], 0
    for b in blocks:
        if total + len(b) > 32000:
            break
        kept.append(b)
        total += len(b)
    ctx = "\n\n".join(kept)
    log(f"豆包搜索(activities)：{len(blocks)}/{len(queries)} 个查询返回结果，上下文 {len(ctx)} 字")
    return ctx


def call_zhipu(prompt, model=None):
    """调用智谱 GLM（OpenAI 兼容），永久免费的 glm-4-flash。"""
    from openai import OpenAI
    api_key = os.environ.get("ZHIPU_API_KEY")
    if not api_key:
        raise RuntimeError("未设置 ZHIPU_API_KEY")
    model = model or os.environ.get("LLM_MODEL") or "glm-4-flash"
    client = OpenAI(api_key=api_key, base_url="https://open.bigmodel.cn/api/paas/v4/")
    log(f"调用 智谱 GLM，模型={model}（合成 JSON）")
    import time as _t
    last_exc = None
    for _attempt in range(1, 6):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=16384,
                response_format={"type": "json_object"},
            )
            return getattr(resp.choices[0].message, "content", "") or ""
        except Exception as e:
            # 智谱 GLM 免费档经常返回 429（该模型当前访问量过大）。做有限次退避重试，
            # 避免一次限流就把整轮更新打成“假成功”（sys.exit(0) 且页面无更新）。
            _msg = str(e)
            _status = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
            _is_429 = (_status == 429) or ("429" in _msg) or ("访问量过大" in _msg) or ("rate limit" in _msg.lower())
            if _is_429 and _attempt < 5:
                _wait = 30 * _attempt
                log(f"智谱 GLM 返回 429 限流（第 {_attempt} 次），{_wait}s 后重试")
                _t.sleep(_wait); last_exc = e; continue
            raise
    raise last_exc or RuntimeError("智谱 GLM 重试后仍失败")


def call_zhipu_websearch(prompt):
    """调用智谱 GLM 并启用 web_search 工具，让模型自行联网检索后合成 JSON（不依赖豆包搜索）。"""
    from openai import OpenAI
    api_key = os.environ.get("ZHIPU_API_KEY")
    if not api_key:
        raise RuntimeError("未设置 ZHIPU_API_KEY")
    model = os.environ.get("LLM_MODEL") or "glm-4-flash"
    client = OpenAI(api_key=api_key, base_url="https://open.bigmodel.cn/api/paas/v4/")
    log(f"调用 智谱 GLM（web_search 联网检索），模型={model}")
    import time as _t
    last_exc = None
    for _attempt in range(1, 6):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=16384,
                tools=[{"type": "web_search", "web_search": {"enable": True, "search_result": True}}],
            )
            return getattr(resp.choices[0].message, "content", "") or ""
        except Exception as e:
            _msg = str(e)
            _status = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
            _is_429 = (_status == 429) or ("429" in _msg) or ("访问量过大" in _msg) or ("rate limit" in _msg.lower())
            if _is_429 and _attempt < 5:
                _wait = 30 * _attempt
                log(f"智谱 web_search 返回 429 限流（第 {_attempt} 次），{_wait}s 后重试")
                _t.sleep(_wait); last_exc = e; continue
            raise
    raise last_exc or RuntimeError("智谱 web_search 重试后仍失败")


def call_cn_free(prompt):
    """国内免翻墙免费组合：豆包搜索（联网取数）+ 智谱 GLM-4-Flash（免费合成 JSON）。
    豆包无结果时改用智谱 web_search 自行联网检索，避免把占位/示例文本或旧值原样保留
    （典型如 rareEarth.marketSummary 原值为 prompt 中的示例占位，豆包失效时会回退成该占位话）。"""
    ctx = gather_doubao_context(os.environ.get("DOUBAO_SEARCH_API_KEY"))
    if ctx:
        full = prompt + "\n\n以下是联网搜索到的参考信息（请据此核对并更新数据，数字以参考信息原文为准，不要编造）：\n" + ctx
        return call_zhipu(full)
    # 豆包搜索无结果（Key 失效/接口异常）→ 改用智谱 web_search 自行联网检索后合成，
    # 确保市场概况/价格/对策等主合成字段拿到真实数据，而不是回退为占位示例文本。
    log("主合成（rareEarth/市场概况）豆包搜索无结果，改用智谱 web_search 自行联网检索")
    return call_zhipu_websearch(prompt)


def call_llm(prompt):
    provider = (os.environ.get("LLM_PROVIDER") or "openai").lower()
    if provider == "perplexity":
        return call_perplexity(prompt)
    if provider == "gemini":
        return call_gemini(prompt)
    if provider == "cn-free":
        return call_cn_free(prompt)
    return call_openai(prompt)


def call_llm_activities(prompt):
    """竞社动态专用的 LLM 调用（与价格字段隔离，互不干扰）。"""
    provider = (os.environ.get("LLM_PROVIDER") or "openai").lower()
    if provider == "cn-free":
        ctx = gather_doubao_context_activities(os.environ.get("DOUBAO_SEARCH_API_KEY"))
        if ctx:
            full = prompt + "\n\n以下是联网搜索到的参考信息（请据此核对，只输出真实可核实的增量动态）：\n" + ctx
            return call_zhipu(full)
        # 豆包搜索无结果 → 改用智谱 web_search 自行联网检索，确保动态段不靠幻觉
        log("activities 豆包搜索无结果，改用智谱 web_search 自行联网检索最新动态")
        return call_zhipu_websearch(prompt)
    if provider == "perplexity":
        return call_perplexity(prompt)
    if provider == "gemini":
        return call_gemini(prompt)
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


def build_update_summary(existing, merged):
    """对比“更新前 / 更新后”两份数据，生成人类可读的中文更新摘要。
    只说明“哪些板块更新了、变动了多少”，不输出完整数据，方便邮件/日志快速浏览。"""

    if not isinstance(existing, dict):
        existing = {}
    def re(d):
        return (d.get("rareEarth") or {}) if isinstance(d, dict) else {}

    re_old, re_new = re(existing), re(merged)
    lines = []

    # —— 稀土价格（逐品类对比涨跌/新增） ——
    old_p = {p.get("name"): p for p in re_old.get("currentPrices", [])}
    new_p = {p.get("name"): p for p in re_new.get("currentPrices", [])}
    changes = []
    for name, np_ in new_p.items():
        op = old_p.get(name)
        npv, opv = np_.get("price"), (op or {}).get("price")
        if op is None:
            changes.append(f"{name} 新增 {npv} {np_.get('unit','')}")
        elif npv != opv:
            arrow = "↑" if (float(npv or 0) > float(opv or 0)) else "↓"
            changes.append(f"{name} {opv}→{npv} {np_.get('unit','')} {arrow}")
    lines.append("【稀土价格】" + ("；".join(changes) if changes else "无变动"))

    # —— 市场概况 / 应对建议 ——
    lines.append("【市场概况】" + ("已更新" if re_old.get("marketSummary") != re_new.get("marketSummary") else "无变动"))
    lines.append("【正海磁材应对建议】" + ("已更新" if re_old.get("strategies") != re_new.get("strategies") else "无变动"))

    # —— 竞社动态（按标题去重找新增） ——
    a_old = set(x.get("title", "") for x in (existing.get("activities") or []))
    a_new = merged.get("activities") or []
    a_added = [x for x in a_new if x.get("title", "") not in a_old]
    if a_added:
        lines.append("【竞社动态】新增 %d 条：" % len(a_added) + "；".join(x.get("title", "") for x in a_added[:8]))
    else:
        lines.append("【竞社动态】无变动（共 %d 条）" % len(a_new))

    # —— 新闻动态 ——
    n_old = set(x.get("title", "") for x in (existing.get("news") or []))
    n_new = merged.get("news") or []
    n_added = [x for x in n_new if x.get("title", "") not in n_old]
    if n_added:
        items = ["%s %s：%s" % (x.get("date", ""), x.get("company", ""), x.get("title", "")) for x in n_added[:8]]
        lines.append("【新闻动态】新增 %d 条：" % len(n_added) + "；".join(items))
    else:
        lines.append("【新闻动态】无变动（共 %d 条）" % len(n_new))

    # —— 竞社经营数据（按公司对比内容） ——
    c_old = {c.get("id"): c for c in (existing.get("companies") or [])}
    changed = [nc.get("name", cid) for cid, nc in
               ((c.get("id"), c) for c in (merged.get("companies") or []))
               if c_old.get(cid) != nc]
    lines.append("【竞社经营数据】" + ("更新：" + "、".join(changed) if changed else "无变动"))

    return "\n".join(lines)


def append_update_history(summary, last_updated):
    """把今日更新摘要写入 data/update-history.json（按日期去重：同一天多次运行只保留最新一条）。
    供网站「每日更新记录」标签页展示，无需任何邮件/授权码配置。"""
    try:
        base = os.path.dirname(DATA_PATH) or "."
        hist_path = os.path.join(base, "update-history.json")
        day = (last_updated or "")[:10]   # 取日期部分用于去重
        try:
            with open(hist_path, encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = []
        if not isinstance(history, list):
            history = []
        entry = {"date": day, "updated_at": last_updated, "summary": summary}
        # 同一天已存在则替换，否则追加（避免双触发产生重复条目）
        if history and history[-1].get("date") == day:
            history[-1] = entry
        else:
            history.append(entry)
        history = history[-60:]   # 仅保留最近 60 条，避免无限增长
        with open(hist_path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        log("已写入每日更新记录：%s（共 %d 条）" % (hist_path, len(history)))
    except Exception as e:
        log("更新记录写入失败（不影响主数据）：%s" % e)


def _history_has_today(last_updated=None):
    """判断 update-history.json 中今天（按 last_updated 的日期，缺省取本地日期）是否已有记录。"""
    try:
        base = os.path.dirname(DATA_PATH) or "."
        hist_path = os.path.join(base, "update-history.json")
        if not os.path.exists(hist_path):
            return False
        with open(hist_path, encoding="utf-8") as f:
            history = json.load(f)
        if not isinstance(history, list) or not history:
            return False
        today = (last_updated or "")[:10] if last_updated else datetime.date.today().isoformat()
        return any(h.get("date") == today for h in history)
    except Exception:
        return False


def send_email_report(summary, last_updated, to_addr):
    """把更新摘要通过 SMTP 发送邮件。未配置 SMTP 凭据时安全跳过（不影响数据更新）。"""
    host = os.environ.get("SMTP_HOST")
    port = os.environ.get("SMTP_PORT")
    user = os.environ.get("SMTP_USER")
    pwd = os.environ.get("SMTP_PASS")
    if not (host and port and user and pwd and to_addr):
        log("未配置 SMTP 凭据（SMTP_HOST/PORT/USER/PASS/NOTIFY_EMAIL），跳过邮件发送；"
            "今日更新摘要仅记录在运行日志中。")
        return False
    try:
        import smtplib, ssl
        from email.mime.text import MIMEText
        from email.header import Header
        subject = "磁材竞社情报 每日更新摘要（%s）" % (last_updated or "")
        body = ("网站 https://yaojiqiang.github.io/magnet-intel/ 今日自动更新如下：\n\n"
                "%s\n\n（本邮件由 GitHub Actions 每日自动发送，无需人工操作）" % summary)
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = user
        msg["To"] = to_addr
        port_i = int(port)
        if port_i == 465:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port_i, timeout=30) as s:
                s.login(user, pwd)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port_i, timeout=30) as s:
                s.ehlo()
                if s.has_extn("starttls"):
                    s.starttls()
                s.login(user, pwd)
                s.send_message(msg)
        log("邮件已发送至 %s" % to_addr)
        return True
    except Exception as e:
        log("邮件发送失败（不影响数据更新）：%s" % e)
        return False


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
        # 运行时错误（如 LLM 限流/网络抖动）：保留现有数据，温和退出；
        # 但仍写一条“检查过但失败”的每日记录，避免「每日更新记录」标签页空白、让人误以为自动化没运行。
        log(f"LLM 调用失败，保留现有数据: {e}")
        try:
            _iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            append_update_history("【自动检查】LLM 调用失败（限流/网络抖动），保留现有数据，本次未更新。", _iso)
        except Exception as _he:
            log(f"写入失败记录时也出错（忽略）：{_he}")
        sys.exit(0)

    new = extract_json(raw)
    if not new:
        log("未能解析出有效 JSON，更新失败（请检查模型输出格式）")
        sys.exit(1)   # 模型已返回内容但无法解析 => 明确失败，运行变红

    merged, errors, critical_fail = safe_merge(existing, new)
    for e in errors:
        log(f"校验告警: {e}")

    # 竞社动态每日增量更新（独立于价格字段，避免相互干扰；失败也不影响价格更新）
    try:
        merged["activities"] = update_activities(existing)
    except Exception as e:
        log(f"activities 更新异常，保留现有: {e}")
        merged["activities"] = (existing.get("activities") if isinstance(existing, dict) else [])
    # 新闻动态每日增量更新（独立于价格字段；失败不影响其它更新）
    try:
        merged["news"] = update_news(existing)
    except Exception as e:
        log(f"news 更新异常，保留现有: {e}")
        merged["news"] = (existing.get("news") if isinstance(existing, dict) else [])

    # 竞社经营数据每日增量“报告刷新”（独立；失败不影响其它更新）
    try:
        merged["companies"] = update_companies(existing)
    except Exception as e:
        log(f"companies 更新异常，保留现有: {e}")
        merged["companies"] = (existing.get("companies") if isinstance(existing, dict) else [])


    # 未来 3 个月价格预测：基于联网检索 + 多因子推理独立生成，覆盖 safe_merge 结果，
    # 避免退化为“仅按真实价格线性外推”的兜底。失败则保留现有预测（或保守应急兜底）。
    try:
        fc = update_forecast(existing)
        if fc:
            merged.setdefault("rareEarth", {})["forecast"] = fc
    except Exception as e:
        log(f"forecast 更新异常，保留现有: {e}")


    if critical_fail:
        # 关键字段校验未通过：保留现有值后继续提交其余已成功更新的段落，
        # 不再整轮作废（避免单段坏输出让整轮变红、啥都不提交）。
        log("关键字段校验未通过，已保留现有值并继续提交其余更新；请检查模型输出。")

    # ★ 日期一致性守卫：仅当“实质内容”发生变化时才推进 lastUpdated；
    #   内容未变化（如价格持平、模型仅复述）则保持原日期，不提交新版本，
    #   避免“日期已更新但内容没更新”导致页面日期与内容对不上。
    if existing and data_fingerprint(merged) == data_fingerprint(existing):
        merged["updateNote"] = existing.get("updateNote")
        save_data(merged)
        changed = False
        log("数据内容与上次一致（无实质更新），仍强制推进 lastUpdated 为今天")
    else:
        # 写入带明确时区(UTC, Z)的 ISO8601，避免 GitHub Actions(UTC) 的裸时间被浏览器
        # 当成访问者本地时间解析，导致“更新于”显示偏差 8 小时。
        save_data(merged)
        changed = True
        log("更新完成（内容有变化，已推进 lastUpdated）")

    # ★ 生成“今日更新摘要”并打印到日志（无论是否配置邮件都会输出，
    #   方便在 Actions 运行日志中直接查看“今天更新了哪些部分”）
    # ★ 日期强制每日推进（无论内容有无变化）：即使价格持平/无新动态也每天刷新 lastUpdated，避免线上"更新于"冻结。
    merged["lastUpdated"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    save_data(merged)

    summary = build_update_summary(existing, merged)
    log("=== 今日更新摘要 ===")
    for line in summary.split("\n"):
        log(line)

    # ★ 把摘要写入 data/update-history.json，供网站「每日更新记录」标签页展示。
    #   无论内容有无变化都写一条：有变化写真实摘要，无变化写“已自动检查、无实质更新”，
    #   避免某天数据持平导致标签页空白、让人误以为自动化没运行。
    #   注意：当天若已有真实更新记录（双触发场景），“无变化”提示不覆盖它。
        # 无变化当天：更新记录用“今天”日期，而非沿用的旧 lastUpdated
    # （无变化时 lastUpdated 会被保留为昨日，否则 _history_has_today 误判→漏写“已自动检查”）
    today_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today_day = today_iso[:10]
    if changed:
        append_update_history(summary, merged.get("lastUpdated"))
    else:
        if not _history_has_today(today_day):
            no_change_note = "【自动检查】今日已自动检查，数据与昨日一致，无实质更新。"
            append_update_history(no_change_note, today_iso)

    # ★ 可选：把摘要通过邮件推送给指定邮箱（配置 SMTP 凭据后自动生效）
    send_email_report(summary, merged.get("lastUpdated"), os.environ.get("NOTIFY_EMAIL"))


if __name__ == "__main__":
    main()
