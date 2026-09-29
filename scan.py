#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QDII 场外基金监控 · 跟踪纳斯达克100 / 标普500 指数
功能：抓取申购状态、单日累计购买上限、阶段收益、规模、费率、持仓、公告，生成自包含 HTML 监控页
数据源：天天基金（东方财富）公开数据接口 + 腾讯行情（指数）
用法：
    python3 scan.py            # 全量抓取并生成页面
    python3 scan.py --cache DIR  # 使用 DIR 下的 funds_raw.json / news_raw.json 缓存（不联网抓基金明细）
    python3 scan.py --hist       # 强制刷新历史行情序列 / 净值序列 / 指数走势（默认各自按缓存时效复用）
"""
import requests, json, re, os, sys, time, datetime
from concurrent.futures import ThreadPoolExecutor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
RAW_DIR = os.path.join(BASE_DIR, ".cache")   # 中间缓存（原始抓取结果），不属最终交付
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(RAW_DIR, exist_ok=True)
UA_PC = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
UA_MB = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"}
API = "https://fundmobapi.eastmoney.com/FundMNewApi/"
# 监控范围：纳斯达克100 / 标普500 及其变体（纳指科技、纳指生物科技、纳指精选、标普500等权重）
INCLUDE = ["纳斯达克100", "纳指100", "标普500", "纳斯达克科技", "纳指科技",
           "纳斯达克生物科技", "纳指生物科技", "纳斯达克精选", "纳指精选"]
EXCLUDE_PREFIX = ("15", "51", "56", "58")  # 场内 ETF 代码前缀

# 跟踪标的分类：(key, 页面显示名, 匹配关键词)，按顺序优先匹配
CATS = [
    ("sp500_ew",   "标普500等权重",     ["标普500等权重", "标普500等权"]),
    ("ndx100",     "纳斯达克100",       ["纳斯达克100", "纳指100"]),
    ("ndx_tech",   "纳斯达克科技",      ["纳斯达克科技", "纳指科技"]),
    ("ndx_bio",    "纳斯达克生物科技",  ["纳斯达克生物科技", "纳指生物科技"]),
    ("ndx_active", "纳斯达克精选(主动)", ["纳斯达克精选", "纳指精选"]),
    ("sp500",      "标普500",           ["标普500"]),
]


def cat_of(name):
    for key, disp, kws in CATS:
        for kw in kws:
            if kw in name:
                return key, disp
    return "other", "其他"


# ---------------- 1. 基金清单 ----------------
def get_fund_list():
    r = requests.get("http://fund.eastmoney.com/js/fundcode_search.js", headers=UA_PC, timeout=20)
    r.encoding = "utf-8"
    data = json.loads(re.search(r"\[.*\]", r.text, re.S).group(0))
    out = []
    for code, _py, name, ftype, _ in data:
        if not any(k in name for k in INCLUDE):
            continue
        if code[:2] in EXCLUDE_PREFIX:
            continue
        key, disp = cat_of(name)
        out.append({"code": code, "name": name, "type": ftype, "cat": key, "catName": disp})
    out.sort(key=lambda x: (0 if x["cat"] == "ndx100" else 1, x["code"]))
    return out


# ---------------- 2. 明细抓取 ----------------
def fetch_one(code):
    s = requests.Session(); s.headers.update(UA_MB)
    rec = {"code": code}
    for nm, key in [("FundMNDetailInformation", "detail"), ("FundMNBasicInformation", "basic"),
                    ("FundMNInverstPosition", "pos"), ("FundMNAssetAllocationNew", "alloc"),
                    ("FundMNPeriodIncrease", "period")]:
        url = f"{API}{nm}?FCODE={code}&deviceid=Wap&plat=Wap&product=EFund&version=2.0.0"
        rec[key] = None
        for attempt in range(2):
            try:
                rec[key] = s.get(url, timeout=15).json().get("Datas")
                break
            except Exception:
                time.sleep(0.5)
    return rec


TE_RE = re.compile(r"年化跟踪误差[：:]\s*(?:</a>)?\s*([0-9]+(?:\.[0-9]+)?%)")
# 基金详情页（PC）「交易状态」区块：<span class="itemTit">交易状态：</span><span class="staticCell">限大额 (<span>单日累计购买上限10.00元</span>)</span><span class="staticCell">开放赎回</span>
PC_STATE_RE = re.compile(r'交易状态：</span>\s*<span class="staticCell">(.*?)</span>'
                         r'(?:\s*<span class="staticCell">(.*?)</span>)?', re.S)
PC_LIMIT_RE = re.compile(r"单日累计购买上限\s*([0-9]+(?:\.[0-9]+)?)\s*(万元|万份|元|份|美元)")
TAG_RE = re.compile(r"<[^>]+>")


def norm_limit(text):
    """把 PC 页的「单日累计购买上限10.00元」规范成「单日累计购买上限10元」，保留原始单位。"""
    m = PC_LIMIT_RE.search(text or "")
    if not m:
        return ""
    v = m.group(1).rstrip("0").rstrip(".") or "0"
    return f"单日累计购买上限{v}{m.group(2)}"


def fetch_pc_detail(code):
    """基金详情页（PC）一次请求取四类字段，用于与手机接口交叉验证：
    年化跟踪误差（手机接口无此字段）、交易状态（申购状态）、单日累计购买上限（含单位）、赎回状态。"""
    out = {"te": "", "state": "", "limit": "", "shzt": "", "canBuy": None, "ok": False}
    try:
        r = requests.get(f"https://fund.eastmoney.com/{code}.html",
                         headers={**UA_PC, "Referer": "https://fund.eastmoney.com/"}, timeout=15)
        r.encoding = r.apparent_encoding or "utf-8"
        t = r.text
        m = TE_RE.search(t)
        if m:
            out["te"] = m.group(1)
        m2 = PC_STATE_RE.search(t)
        if m2:
            raw = re.sub(r"\s+", "", TAG_RE.sub(" ", m2.group(1) or ""))
            hm = re.match(r"^([^（(]+)", raw)          # 例：限大额(单日累计购买上限10.00元)
            out["state"] = (hm.group(1) if hm else raw).strip()
            out["limit"] = norm_limit(m2.group(1) or "")
            out["shzt"] = re.sub(r"\s+", "", TAG_RE.sub("", m2.group(2) or ""))
            out["canBuy"] = ("choseBuyWay canBuy" in t)
            out["ok"] = True
    except Exception:
        pass
    return out


def fetch_all(codes):
    res = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(fetch_one, codes):
            res.append(r)
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r, pc in zip(res, ex.map(fetch_pc_detail, codes)):
            r["te"] = pc["te"]
            r["pc"] = pc
    return res


def fetch_notices(codes):
    H = dict(UA_PC); H["Referer"] = "https://fundf10.eastmoney.com/"
    def gg(code):
        try:
            r = requests.get(f"https://api.fund.eastmoney.com/f10/JJGG?fundcode={code}&pageIndex=1&pageSize=3&type=0",
                             headers=H, timeout=15)
            out = []
            for x in (r.json().get("Data") or []):
                ti = x.get("TITLE") or ""
                tag, brief, facts = parse_notice(ti)
                out.append({"code": code, "id": x.get("ID"), "title": ti,
                            "date": (x.get("PUBLISHDATEDesc") or "")[:10],
                            "tag": tag, "brief": brief, "facts": facts})
            return out
        except Exception:
            return []
    news = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for lst in ex.map(gg, codes):
            news += lst
    news = [n for n in news if n.get("date") and not re.search(r"(系统维护|线路维护|暂停服务|服务通知|客服热线)", n["title"])]
    news.sort(key=lambda x: x["date"], reverse=True)
    return news


NOTICE_TAGS = [
    ("暂停申购/定投", r"暂停.{0,20}?(申购|定期定额|定投)"),
    ("恢复申购", r"恢复.{0,20}?(申购|大额|定期定额|定投|转换转入)"),
    ("大额申购限额调整", r"(限制|调整|设置|取消|提高|降低|暂停).{0,14}?(大额申购|申购金额|申购上限|限额|申购业务)"),
    ("分红", r"(分红|收益分配|利润分配)"),
    ("基金经理变更", r"(基金经理|增聘|解聘|离任|代为履行)"),
    ("费率调整", r"(管理费|托管费|销售服务费|费率)"),
    ("合同/招募书更新", r"(招募说明书|基金合同|产品资料概要)"),
    ("清算/终止", r"(清算|清盘|终止|基金财产清算)"),
    ("风险提示", r"(风险提示|溢价|停牌|流动性)"),
    ("开放/上市交易", r"(开放日常|上市交易|开放申购|封闭期)"),
]

FUND_ABBR = [
    (r"交易型开放式指数证券投资基金联接基金", "ETF联接基金"),
    (r"交易型开放式指数证券投资基金", "ETF"),
    (r"发起式证券投资基金", "发起式基金"),
    (r"指数型证券投资基金", "指数基金"),
    (r"证券投资基金", "基金"),
    (r"开放式基金", "基金"),
]


def parse_notice(title):
    """从公告标题提取：变更类型标签 + 关键信息摘要 + 结构化要点。全部为标题原文信息的压缩，不做推断。"""
    t = s(title)
    tag = "产品公告"
    for name, pat in NOTICE_TAGS:
        if re.search(pat, t):
            tag = name
            break
    b = re.sub(r"的?公告$", "", t).strip(" 。；")
    b = re.sub(r"^关于", "", b)
    b = re.sub(r"^[\u4e00-\u9fa5A-Za-z0-9()（）]{2,24}?基金管理(有限公司|股份有限公司)", "", b)
    b = re.sub(r"^关于", "", b).strip(" 的，,：:")
    for pat, rep in FUND_ABBR:
        b = re.sub(pat, rep, b)
    if len(b) > 74:
        b = b[:74] + "…"
    facts = []
    for pat, lab in [(r"人民币份额", "人民币份额"), (r"美元(份额|现汇|钞)", "美元份额"),
                     (r"A类", "A类"), (r"C类", "C类"), (r"E类", "E类"), (r"F类", "F类")]:
        if re.search(pat, t) and lab not in facts:
            facts.append(lab)
    m = re.search(r"每\s*10\s*份[^，。；、]{0,12}?([0-9]+(?:\.[0-9]+)?)\s*元", t)
    if m:
        facts.append("每10份派 " + m.group(1) + " 元")
    m = re.search(r"(?:不超过|上限|限额|调整为|提高至|降至|调低至|恢复为|设置为)\s*([0-9]+(?:\.[0-9]+)?)\s*(万元|元|万份|份)", t)
    if m:
        facts.append("限额 " + m.group(1) + m.group(2))
    m = re.search(r"自\s*(\d{4}年\d{1,2}月\d{1,2}日)\s*起", t)
    if m:
        facts.append("自 " + m.group(1) + " 起")
    return tag, b, facts


IDX_DEFS = [("usNDX", "ndx", ".NDX", "纳斯达克100", "Nasdaq 100"),
            ("usINX", "inx", ".INX", "标普500", "S&P 500"),
            ("usDJI", "dji", ".DJI", "道琼斯", "Dow Jones")]

# 代码 / 口径映射（必须按「指数全名」对齐，同一指数在不同平台代码不同，极易混淆）：
#   纳斯达克100（Nasdaq-100, NDX）：腾讯 usNDX ｜ 新浪 gb_$ndx ｜ CNBC .NDX ｜ 纳斯达克官方 NDX ｜ 东财 100.NDX100
#   纳斯达克综合（Nasdaq Composite, COMP）：纳斯达克官方 COMP ｜ 东财 100.NDX
#     —— 东方财富 secid=100.NDX 的 f57 显示 "NDX"、f58 却是「纳斯达克」，其值与 COMP 完全一致，实为综合指数。
#   2026-09-18（美东）收盘实测：NDX 29644.17 ／ COMP 26522.55，两者相差数千点，绝不可混用。
IDX_CNBC = {".NDX": ".NDX", ".INX": ".INX", ".DJI": ".DJI"}
IDX_META = {}


def _fnum(v):
    """行情字符串清洗（千分位 / 正号 / 百分号 / 占位符）后转数值，失败返回 None。"""
    if v is None:
        return None
    t = str(v).strip().replace(",", "").replace("%", "").replace("+", "")
    if not t or t in ("--", "-", "N/A", "null"):
        return None
    try:
        return float(t)
    except Exception:
        return None


def cnbc_quotes(symbols):
    """CNBC 行情接口（第三方独立源，返回字段 source=Exchange），一次可取多只指数。"""
    out = {}
    try:
        url = ("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol?symbols="
               + "|".join(symbols)
               + "&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1&output=json&events=1")
        j = requests.get(url, headers=UA_PC, timeout=15).json() or {}
        for q in ((j.get("FormattedQuoteResult") or {}).get("FormattedQuote") or []):
            sym, price = s(q.get("symbol")), _fnum(q.get("last"))
            if sym and price:
                out[sym] = {"price": price, "chg": _fnum(q.get("change")), "pct": _fnum(q.get("change_pct")),
                            "open": _fnum(q.get("open")), "high": _fnum(q.get("high")), "low": _fnum(q.get("low")),
                            "prev": _fnum(q.get("previous_day_closing")),
                            "time": s(q.get("last_timedate")), "date": s(q.get("last_time")),
                            "status": s(q.get("curmktstatus"))}
    except Exception as e:
        print("  ! CNBC 行情抓取失败：", e)
    return out


def nasdaq_official(sym):
    """纳斯达克交易所官方行情 API（权威源）；指数数据仅部分提供（NDX / COMP 等）。"""
    try:
        j = requests.get(f"https://api.nasdaq.com/api/quote/{sym}/info?assetclass=index",
                         headers=UA_PC, timeout=15).json() or {}
        d = ((j.get("data") or {}).get("primaryData") or {})
        price = _fnum(d.get("lastSalePrice"))
        if price is None:
            return None
        return {"price": price, "chg": _fnum(d.get("netChange")), "pct": _fnum(d.get("percentageChange")),
                "date": s(d.get("lastTradeTimestamp"))}
    except Exception:
        return None


def eastmoney_idx(secid):
    """东方财富指数行情（点位单位「厘」，需除以 100），仅用于代码口径比对。"""
    try:
        url = (f"https://push2delay.eastmoney.com/api/qt/stock/get?secid={secid}"
               "&fields=f43,f57,f58,f169,f170&ut=fa5fd1943c7b386f172d6893dbfba10b")
        d = ((requests.get(url, headers=UA_PC, timeout=12).json() or {}).get("data") or {})
        if d.get("f43") is None:
            return None
        return {"code": s(d.get("f57")), "name": s(d.get("f58")), "price": d["f43"] / 100.0,
                "chg": (d.get("f169") or 0) / 100.0, "pct": (d.get("f170") or 0) / 100.0}
    except Exception:
        return None


def fetch_index():
    """页首三大美股指数行情（点位 / 涨跌额 / 涨跌幅 / 开高低 / 昨收 / 行情时间），四源交叉校验。

    主源：腾讯行情 qt.gtimg.cn。美股指数与 A 股字段偏移不同，2026-09-20 逐位实测（勿按 A 股口径照搬）：
        f[1] 中文名  f[2] 代码(.NDX)  f[3] 最新价  f[4] 昨收  f[5] 今开
        f[30] 行情时间（美东，YYYY-MM-DD HH:MM:SS）  f[31] 涨跌额  f[32] 涨跌幅%  f[33] 当日最高  f[34] 当日最低
        f[35] 币种(USD)  f[46] 英文名  f[48] 52周最高  f[49] 52周最低
        （f[29]/f[45] 为空串；切勿按 A 股口径把 f[31]/f[32] 当时间/涨跌额）
    校验链（各项相对偏差 ≤ 0.05% 记通过）：
        ① 自洽：最新价 − 昨收 = 涨跌额；(最新价 − 昨收) / 昨收 = 涨跌幅 → selfDev
        ② 新浪财经 hq.sinajs.cn（gb_$ndx / gb_$inx / gb_$dji）：s[1] 价 s[2] 涨跌幅% s[3] 北京时间
           s[4] 涨跌额 s[5] 今开 s[6] 最高 s[7] 最低 s[26] 昨收 → xDev
        ③ CNBC restQuote（返回代码 .NDX / .INX / .DJI，source=Exchange）→ cbcDev
        ④ 纳斯达克交易所官方 API（NDX，权威源）→ nqDev
    另读取纳斯达克综合指数（COMP）与东财 100.NDX / 100.NDX100 用于口径辨别，结果写入 IDX_META。
    """
    H = dict(UA_PC)
    primary, second = {}, {}
    try:
        r = requests.get("https://qt.gtimg.cn/q=usNDX,usINX,usDJI", headers=H, timeout=12)
        r.encoding = "gbk"
        for line in r.text.strip().split(";"):
            if '"' not in line:
                continue
            f = line.split('"')[1].split("~")
            if len(f) >= 36 and s(f[2]):
                primary[f[2]] = f
    except Exception:
        pass
    try:
        h2 = dict(H)
        h2["Referer"] = "https://finance.sina.com.cn"
        r2 = requests.get("https://hq.sinajs.cn/list=gb_$ndx,gb_$inx,gb_$dji", headers=h2, timeout=12)
        r2.encoding = "gbk"
        for line in r2.text.strip().split("\n"):
            if '"' not in line:
                continue
            key = line.split("=")[0].replace("var hq_str_gb_$", "").strip()
            ss = line.split('"')[1].split(",")
            if len(ss) > 26:
                second[key] = ss
    except Exception:
        pass

    # 第三方 / 权威源：一次抓取，供每只指数多源比对（任一源失败不影响主流程）
    cb = cnbc_quotes(list(IDX_CNBC.keys()))
    nq = nasdaq_official("NDX")
    comp = nasdaq_official("COMP")
    em_ndx = eastmoney_idx("100.NDX")
    em_ndx100 = eastmoney_idx("100.NDX100")
    if cb:
        print("  · 指数交叉校验源：CNBC", " ".join(sorted(cb.keys())),
              "｜纳斯达克官方", "NDX" if nq else "—", "COMP" if comp else "")
    out = []
    for tcode, skey, sym, cn, en in IDX_DEFS:
        f = primary.get(sym)
        if not f:
            continue
        row = {"name": cn, "enName": en, "code": tcode, "sym": sym,
               "price": num(f[3]), "prev": num(f[4]), "open": num(f[5]),
               "chg": num(f[31]), "chgPct": num(f[32]), "high": num(f[33]), "low": num(f[34]),
               "cur": s(f[35]), "time": s(f[30]), "date": s(f[30])[:10],
               "high52": num(f[48]) if len(f) > 48 else None,
               "low52": num(f[49]) if len(f) > 49 else None,
               "src": "腾讯行情 qt.gtimg.cn", "xSrc": "新浪财经 hq.sinajs.cn"}
        # 自洽校验：最新价 - 昨收 = 涨跌额；(价 - 昨收) / 昨收 = 涨跌幅
        if row["price"] is not None and row["prev"]:
            row["chgCalc"] = round(row["price"] - row["prev"], 2)
            row["pctCalc"] = round((row["price"] - row["prev"]) / row["prev"] * 100, 4)
            if row["chgPct"] is not None:
                row["selfDev"] = round(abs(row["pctCalc"] - row["chgPct"]), 4)
        # 跨源校验：新浪实时价 / 涨跌幅
        ss = second.get(skey)
        if ss:
            row["xPrice"] = num(ss[1])
            row["xPct"] = num(ss[2])
            row["xChg"] = num(ss[4])
            row["bjTime"] = s(ss[3])
            row["xPrev"] = num(ss[26])
            if row["xPrice"] is not None and row["price"]:
                row["xDev"] = round(abs(row["xPrice"] - row["price"]) / row["price"] * 100, 6)
            if row["xPct"] is not None and row["chgPct"] is not None:
                row["xPctDev"] = round(abs(row["xPct"] - row["chgPct"]), 4)
        # 交叉源③：CNBC（第三方独立源）
        cq = cb.get(IDX_CNBC.get(sym, sym)) or {}
        if cq.get("price") and row["price"]:
            row["cbcPrice"], row["cbcPct"] = cq.get("price"), cq.get("pct")
            row["cbcTime"] = cq.get("time", "")
            row["cbcDev"] = round(abs(cq["price"] - row["price"]) / row["price"] * 100, 6)
        # 交叉源④：纳斯达克交易所官方 API（仅纳指100）
        if sym == ".NDX" and (nq or {}).get("price") and row["price"]:
            row["nqPrice"] = nq["price"]
            row["nqDev"] = round(abs(nq["price"] - row["price"]) / row["price"] * 100, 6)
        devs = [d for d in (row.get("selfDev"), row.get("xDev"), row.get("cbcDev"), row.get("nqDev")) if d is not None]
        row["devMax"] = round(max(devs), 6) if devs else None
        row["srcs"] = ["腾讯行情 qt.gtimg.cn"]
        if row.get("xPrice") is not None:
            row["srcs"].append("新浪财经 hq.sinajs.cn")
        if row.get("cbcPrice") is not None:
            row["srcs"].append("CNBC（source=Exchange）")
        if row.get("nqPrice") is not None:
            row["srcs"].append("纳斯达克官方 API")
        row["srcN"] = len(row["srcs"])
        row["ok"] = (bool(row.get("price")) and row.get("selfDev", 9) <= 0.05
                     and row.get("xDev", 0) <= 0.05 and row.get("cbcDev", 0) <= 0.05
                     and row.get("nqDev", 0) <= 0.05 and row["srcN"] >= 3)
        out.append(row)
    IDX_META.clear()

    def _et_to_bj(t):
        """美东发布时刻 → 北京时间。三月第二个周日起为夏令时 EDT（+12h），
        十一月第一个周日起为冬令时 EST（+13h）。"""
        try:
            dt = datetime.datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""
        y = dt.year
        m3 = datetime.datetime(y, 3, 1)
        dst_s = m3 + datetime.timedelta(days=((6 - m3.weekday()) % 7) + 7)
        n11 = datetime.datetime(y, 11, 1)
        dst_e = n11 + datetime.timedelta(days=((6 - n11.weekday()) % 7))
        return (dt + datetime.timedelta(hours=12 if dst_s <= dt < dst_e else 13)).strftime("%Y-%m-%d %H:%M:%S")

    _times = [x.get("time") for x in out if x.get("time")]
    _bjs = [x.get("bjTime") for x in out if x.get("bjTime")]
    IDX_META.update({
        "quoteTime": (max(_times) if _times else ""),
        "quoteBJConv": (_et_to_bj(max(_times)) if _times else ""),
        "quoteDate": (out[0].get("date") if out else ""),
        "quoteBJ": (max(_bjs) if _bjs else ""),
        "srcN": max([x.get("srcN") or 0 for x in out] or [0]),
        "srcNames": (out[0].get("srcs") if out else []),
        "devMax": round(max([x.get("devMax") or 0 for x in out] or [0]), 6),
        "okN": sum(1 for x in out if x.get("ok")), "n": len(out),
        "comp": comp, "emNDX": em_ndx, "emNDX100": em_ndx100,
        "nqNDX": nq, "cbcN": len(cb),
    })
    return out


# ---------------- 3. 整理 ----------------
def s(v):
    if v is None:
        return ""
    return str(v).strip()


def num(v):
    try:
        f = float(v)
        return f
    except Exception:
        return None


def yi(v):  # 元 -> 亿元
    f = num(v)
    return round(f / 1e8, 2) if f is not None else None


def fmt_pct(v):
    f = num(v)
    return None if f is None or s(v) in ("--", "") else round(f, 2)


def fee_sum(*vals):
    """管理费 + 托管费 + 销售服务费，输出年综合费率。"""
    tot = 0.0
    hit = False
    for v in vals:
        t = s(v).replace("%", "").strip()
        try:
            tot += float(t)
            hit = True
        except Exception:
            pass
    return f"{tot:.2f}%" if hit else "—"


def build_record(f, raw, notices):
    b = raw.get("basic") or {}
    d = raw.get("detail") or {}
    pos = raw.get("pos") or {}
    alloc = raw.get("alloc") or []
    per = raw.get("period") or []
    name = f["name"]
    idx_key = f.get("cat") or cat_of(name)[0]
    cat_name = f.get("catName") or cat_of(name)[1]
    idx_name = s(d.get("INDEXNAME")) or cat_name + "指数"
    if "等权重" in name and "等权重" not in idx_name:
        idx_name += "（等权重）"
    rank_map = {}
    for x in per:
        rank_map[x.get("title")] = {"r": x.get("rank"), "sc": x.get("sc"), "syl": x.get("syl")}
    rk = rank_map.get("Y", {})
    a = alloc[0] if alloc else {}
    currency = "美元" if ("美元" in name or "美钞" in name or "美汇" in name) else "人民币"
    stocks = []
    for st in (pos.get("fundStocks") or [])[:10]:
        stocks.append({"code": s(st.get("GPDM")), "name": s(st.get("GPJC")),
                       "pct": fmt_pct(st.get("JZBL")), "chg": s(st.get("PCTNVCHG")),
                       "chgType": s(st.get("PCTNVCHGTYPE"))})
    fofs = []
    for fo in (pos.get("fundfofs") or [])[:10]:
        fofs.append({"code": s(fo.get("GPDM")), "name": s(fo.get("GPJC")), "pct": fmt_pct(fo.get("JZBL"))})
    # —— 单日累计限额：手机接口为主，PC 详情页「交易状态」区块兜底（含单位），再退 MAXSG（仅人民币份额，单位：元）——
    limit_texts = b.get("TRADEMARKLIST") or []
    limit_text = "；".join([s(x) for x in limit_texts if s(x)]) or s(b.get("SGZTMARK"))
    limit_src = "天天基金手机接口" if limit_text else ""
    pc = raw.get("pc") or {}
    if not limit_text and s(pc.get("limit")):
        limit_text = s(pc.get("limit"))
        limit_src = "天天基金PC详情页"
    if not limit_text and currency == "人民币":
        mb = num(b.get("MAXSG"))
        if mb is not None and 0 < mb < 1e9:
            limit_text = f"单日累计购买上限{mb:g}元"
            limit_src = "天天基金手机接口（MAXSG）"
    sgzt_pc = s(pc.get("state"))
    return {
        "code": f["code"], "name": name, "ftype": f["type"], "currency": currency,
        "indexKey": idx_key, "catKey": idx_key, "catName": cat_name,
        "indexName": idx_name, "indexCode": s(d.get("INDEXCODE")),
        "te": s(raw.get("te")),
        "feeTotal": fee_sum(d.get("MGREXP"), d.get("TRUSTEXP"), d.get("SALESEXP")),
        "company": s(d.get("JJGS") or b.get("JJGS")), "manager": s(d.get("JJJL") or b.get("JJJL")),
        "risk": s(b.get("RISKLEVEL") or d.get("RISKLEVEL")),
        "estDate": s(d.get("ESTABDATE") or b.get("ESTABDATE")),
        "scale": yi(b.get("ENDNAV")), "scaleDate": s(b.get("FEGMRQ")),
        "shares": yi(b.get("FEGM")),
        "mgrFee": s(d.get("MGREXP")), "trustFee": s(d.get("TRUSTEXP")), "salesFee": s(d.get("SALESEXP")),
        "buyRate": s(b.get("RATE")), "rawBuyRate": s(b.get("SOURCERATE")),
        "sgzt": s(b.get("SGZT")), "sgztMark": s(b.get("SGZTMARK")), "shzt": s(b.get("SHZT")),
        "limitText": limit_text, "minBuy": s(b.get("MINSG")), "maxBuy": s(b.get("MAXSG")),
        "limitSrc": limit_src,
        "sgztPC": sgzt_pc, "shztPC": s(pc.get("shzt")), "pcOk": bool(pc.get("ok")),
        "canBuyPC": pc.get("canBuy"),
        "sgztMatch": (not sgzt_pc) or (sgzt_pc == s(b.get("SGZT"))) or (sgzt_pc in s(b.get("SGZT"))) or (s(b.get("SGZT")) in sgzt_pc),
        "canBuy": bool(b.get("BUY")), "confirmDays": s(b.get("YZBA")), "redeemDays": s(b.get("FBYZQ")),
        "nav": s(b.get("DWJZ")), "navDate": s(b.get("FSRQ")), "dayChg": fmt_pct(b.get("RZDF")),
        "r": {"w1": fmt_pct(b.get("SYL_Z")), "m1": fmt_pct(b.get("SYL_Y")), "m3": fmt_pct(b.get("SYL_3Y")),
              "m6": fmt_pct(b.get("SYL_6Y")), "y1": fmt_pct(b.get("SYL_1N")), "y2": fmt_pct(b.get("SYL_2N")),
              "y3": fmt_pct(b.get("SYL_3N")), "y5": fmt_pct((rank_map.get("5N") or {}).get("syl")),
              "ytd": fmt_pct(b.get("SYL_JN")), "since": fmt_pct(b.get("SYL_LN"))},
        "rank": {"m1": rk.get("r", ""), "sc": rk.get("sc", "")},
        "sharp": s(b.get("SHARP1")), "mdd": s(b.get("MAXRETRA1")), "std": s(b.get("STDDEV1")),
        "tgtEtf": {"code": s(pos.get("ETFCODE")), "name": s(pos.get("ETFSHORTNAME"))},
        "holdings": stocks, "fofHoldings": fofs,
        "asset": {"stock": s(a.get("GP")), "bond": s(a.get("ZQ")), "cash": s(a.get("HB")),
                  "fund": s(a.get("JJ")), "other": s(a.get("QT")), "net": s(a.get("JZC")), "date": s(a.get("FSRQ"))},
        "notices": notices,
    }


# ---------------- 4. 页面 ----------------
HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<!-- v3.4 防缓存：浏览器 / 移动端长缓存会让人看到旧页面，这里显式声明不缓存 -->
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="QDII 监控">
<meta name="format-detection" content="telephone=no">
<meta name="theme-color" content="#0a0a0c">
<title>QDII Fund Monitor · 纳指100 / 标普500 及变体</title>
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
/* v3.3 安全区变量：不支持的设备保持 0，支持的取真实 inset */
:root{--sat:0px;--sab:0px;--sal:0px;--sar:0px}
@supports (padding:env(safe-area-inset-top)){
  :root{--sat:env(safe-area-inset-top,0px);--sab:env(safe-area-inset-bottom,0px);
    --sal:env(safe-area-inset-left,0px);--sar:env(safe-area-inset-right,0px)}
}
html[data-theme="light"]{
  --bg:#f5f5f7;--card:#ffffff;--card2:#fbfbfd;--line:rgba(0,0,0,.08);--line2:rgba(0,0,0,.04);
  --tx:#1d1d1f;--tx2:#6e6e73;--tx3:#86868b;--pri:#0071e3;
  --up:#ff3b30;--dn:#34c759;--warn:#ff9500;--warnbg:rgba(255,149,0,.1);--warnline:rgba(255,149,0,.28);
  --okbg:rgba(52,199,89,.1);--okline:rgba(52,199,89,.3);--stopbg:rgba(255,59,48,.09);--stopline:rgba(255,59,48,.28);
  --chip:rgba(0,0,0,.04);--heroA:#0b0d12;--heroB:#1c2333;--heroTx:#fff;--glass:rgba(255,255,255,.14);
  --shadow:0 1px 2px rgba(0,0,0,.04),0 10px 30px rgba(0,0,0,.06);
}
html[data-theme="dark"]{
  --bg:#000000;--card:#1c1c1e;--card2:#2c2c2e;--line:rgba(255,255,255,.1);--line2:rgba(255,255,255,.06);
  --tx:#f5f5f7;--tx2:#a1a1a6;--tx3:#8e8e93;--pri:#0a84ff;
  --up:#ff453a;--dn:#30d158;--warn:#ff9f0a;--warnbg:rgba(255,159,10,.12);--warnline:rgba(255,159,10,.3);
  --okbg:rgba(48,209,88,.12);--okline:rgba(48,209,88,.3);--stopbg:rgba(255,69,58,.12);--stopline:rgba(255,69,58,.3);
  --chip:rgba(255,255,255,.07);--heroA:#0a0a0c;--heroB:#1b1f2a;--heroTx:#fff;--glass:rgba(255,255,255,.1);
  --shadow:0 1px 2px rgba(0,0,0,.3),0 12px 34px rgba(0,0,0,.5);
}
html,body{margin:0;padding:0;background:var(--bg);color:var(--tx);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Helvetica Neue","Microsoft YaHei",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
a{color:var(--pri);text-decoration:none}
a:hover{text-decoration:underline}
.mono{font-variant-numeric:tabular-nums;font-feature-settings:"tnum"}
.wrap{max-width:1580px;margin:0 auto;padding:22px 20px 70px}
.ic{width:16px;height:16px;display:inline-block;vertical-align:-3px}

/* ---------- Hero ---------- */
.hero{position:relative;overflow:hidden;border-radius:26px;padding:30px 30px 24px;color:var(--heroTx);
  background:
    radial-gradient(120% 140% at 88% -20%,rgba(0,113,227,.55) 0%,rgba(0,113,227,0) 55%),
    radial-gradient(90% 120% at 8% 120%,rgba(120,80,255,.35) 0%,rgba(120,80,255,0) 60%),
    linear-gradient(150deg,var(--heroA),var(--heroB));
  box-shadow:0 18px 50px rgba(10,14,30,.28)}
.hero .grid{position:absolute;inset:0;opacity:.16;background-image:
  linear-gradient(rgba(255,255,255,.5) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.5) 1px,transparent 1px);
  background-size:46px 46px;mask-image:linear-gradient(160deg,rgba(0,0,0,.9),transparent 72%)}
.hero>*{position:relative}
.hero-top{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}
.brand{display:flex;gap:14px;align-items:center}
.logo{width:46px;height:46px;border-radius:14px;display:grid;place-items:center;flex:0 0 auto;
  background:linear-gradient(140deg,rgba(255,255,255,.95),rgba(255,255,255,.7));color:#0b0d12}
.logo .ic{width:26px;height:26px}
h1{margin:0;font-size:27px;letter-spacing:-.02em;font-weight:650}
h1 em{font-style:normal;font-weight:400;opacity:.62;font-size:14px;letter-spacing:.14em;text-transform:uppercase;display:block;margin-top:2px}
.hero p.cn{margin:6px 0 0;font-size:13.5px;opacity:.82;line-height:1.6;max-width:760px}
.hero-meta{margin-top:18px;display:flex;gap:10px;flex-wrap:wrap;font-size:12px;opacity:.9}
.pillglass{background:var(--glass);border:1px solid rgba(255,255,255,.2);border-radius:999px;padding:5px 12px;backdrop-filter:blur(10px)}
/* v3.4 顶部时间标注：指数数据日期 / 本次扫描时间（北京）/ 下次更新时间，三行竖排 */
.databar{margin-top:12px;display:flex;flex-direction:column;gap:6px;font-size:12.5px;opacity:.95}
.databar .row{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;background:var(--glass);border:1px solid rgba(255,255,255,.2);border-radius:12px;padding:7px 12px;backdrop-filter:blur(10px)}
.databar .row .k{color:rgba(255,255,255,.74);min-width:172px}
.databar .row b{color:#fff;font-size:14px;font-weight:600;font-variant-numeric:tabular-nums}
.databar .row em{font-style:normal;color:rgba(255,255,255,.6);font-size:11.5px}
.icon-btn{width:38px;height:38px;border-radius:12px;border:1px solid rgba(255,255,255,.22);background:var(--glass);
  color:#fff;cursor:pointer;display:grid;place-items:center;backdrop-filter:blur(10px);transition:.2s}
.icon-btn:hover{background:rgba(255,255,255,.24);transform:translateY(-1px)}
.idxrow{display:grid;grid-template-columns:repeat(auto-fit,minmax(184px,1fr));gap:10px;margin-top:20px}
.idx{position:relative;overflow:hidden;background:var(--glass);border:1px solid rgba(255,255,255,.18);border-radius:16px;padding:12px 14px 11px;backdrop-filter:blur(12px)}
.idx:before{content:'';position:absolute;inset:0;background:linear-gradient(135deg,rgba(255,255,255,.1),transparent 58%);pointer-events:none}
.idx>*{position:relative}
.idx .n{display:flex;align-items:baseline;gap:6px;font-size:11.5px;letter-spacing:.06em;text-transform:uppercase;opacity:.72}
.idx .n em{font-style:normal;font-size:10px;letter-spacing:.04em;opacity:.62;font-variant-numeric:tabular-nums}
.idx .v{font-size:21.5px;font-weight:620;margin-top:3px;letter-spacing:-.01em;font-variant-numeric:tabular-nums}
.idx .c{font-size:12.5px;margin-top:2px;opacity:.95;font-variant-numeric:tabular-nums}
.idx .c .pc{opacity:.82}
.idx .c.up{color:#ff8a80}.idx .c.dn{color:#8ef0a8}.idx .c.flat{color:rgba(255,255,255,.78)}
.idx .k2{margin-top:6px;font-size:10.6px;line-height:1.62;opacity:.62;font-variant-numeric:tabular-nums;letter-spacing:.01em}
.idx .src{margin-top:6px;display:flex;align-items:center;gap:5px;flex-wrap:wrap;font-size:10px;opacity:.6}
.idx .vf{display:inline-flex;align-items:center;gap:3px;padding:1px 6px;border-radius:999px;background:rgba(126,231,160,.14);border:1px solid rgba(126,231,160,.3);color:#9ff0b6;font-size:9.5px;letter-spacing:.02em}
.idx .vf.warn{background:rgba(255,193,94,.14);border-color:rgba(255,193,94,.34);color:#ffd08a}
.vfy{display:block;margin:9px 0 7px;padding:8px 11px;border-radius:10px;background:rgba(127,127,127,.07);border:1px dashed var(--line);color:var(--tx2);font-size:11px;line-height:1.75}
.idxnote{margin-top:10px;padding:9px 12px;border-radius:11px;background:rgba(127,127,127,.06);border:1px solid var(--line);color:var(--tx3);font-size:11px;line-height:1.8}
.idxnote b{color:var(--tx2);font-weight:600}
.idxnote .warnp{color:#ffd08a}
.idxnote code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:10.5px;padding:0 3px;border-radius:4px;background:rgba(127,127,127,.14)}

/* ---------- Stats ---------- */
.sec-title{display:flex;align-items:baseline;gap:10px;margin:26px 4px 12px}
.sec-title h2{margin:0;font-size:16.5px;font-weight:620;letter-spacing:-.01em}
.sec-title em{font-style:normal;font-size:11.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--tx3)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:15px 16px;box-shadow:var(--shadow);display:flex;gap:12px;align-items:flex-start}
.stat .si{width:34px;height:34px;border-radius:11px;flex:0 0 auto;display:grid;place-items:center;background:var(--chip);color:var(--pri)}
.stat .k{font-size:12px;color:var(--tx2);display:flex;gap:6px;align-items:center}
.stat .v{font-size:23px;font-weight:640;letter-spacing:-.02em;margin-top:2px;line-height:1.2}
.stat .v small{font-size:12px;font-weight:500;color:var(--tx3);margin-left:3px}
.stat.wide{grid-column:span 2}
.stat .compose{font-size:12.5px;color:var(--tx2);line-height:1.85;margin-top:4px}
.stat .compose b{color:var(--tx);font-weight:600}
@media(max-width:640px){.stat.wide{grid-column:span 2}}

/* ---------- Toolbar ---------- */
.toolbar{display:flex;gap:9px;flex-wrap:wrap;background:var(--card);border:1px solid var(--line);border-radius:18px;
  padding:11px;margin:14px 0 18px;position:sticky;top:calc(8px + var(--sat));z-index:30;box-shadow:var(--shadow);backdrop-filter:saturate(180%) blur(16px)}
.toolbar .search{position:relative;flex:1;min-width:220px;display:flex;align-items:center}
.toolbar .search .ic{position:absolute;left:12px;color:var(--tx3)}
.toolbar input[type=search],.toolbar select{border:1px solid var(--line);background:var(--card2);color:var(--tx);
  border-radius:12px;padding:9px 12px;font-size:13.5px;outline:none;font-family:inherit;transition:.18s}
.toolbar input[type=search]{width:100%;padding-left:36px}
.toolbar input:focus,.toolbar select:focus{border-color:var(--pri);box-shadow:0 0 0 3.5px rgba(0,113,227,.14)}
.toolbar select{cursor:pointer}

/* ---------- Layout ---------- */
.layout{display:grid;grid-template-columns:1fr 372px;gap:18px;align-items:start}
@media(max-width:1100px){.layout{grid-template-columns:1fr}}
.cards{display:grid;gap:14px}

/* ---------- Card ---------- */
.card{position:relative;background:var(--card);border:1px solid var(--line);border-radius:20px;padding:17px 18px 17px 21px;
  box-shadow:var(--shadow);overflow:hidden;transition:transform .18s ease,box-shadow .18s ease}
.card:hover{transform:translateY(-2px);box-shadow:0 2px 4px rgba(0,0,0,.05),0 16px 40px rgba(0,0,0,.09)}
.card::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--accent,#0071e3)}
.card.changed{box-shadow:0 0 0 2px rgba(255,149,0,.35),var(--shadow)}
.card.cat-ndx100{--accent:#0071e3}
.card.cat-ndx_tech{--accent:#5856d6}
.card.cat-ndx_bio{--accent:#00b8a9}
.card.cat-ndx_active{--accent:#af52de}
.card.cat-sp500{--accent:#ff9500}
.card.cat-sp500_ew{--accent:#ff375f}
.card.cat-other{--accent:#8e8e93}
.chead{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;flex-wrap:wrap}
.ctitle h3{margin:0;font-size:16px;font-weight:620;line-height:1.4;letter-spacing:-.01em}
.cmeta{display:flex;gap:9px;flex-wrap:wrap;font-size:11.5px;color:var(--tx3);margin-top:5px}
.cmeta span{background:var(--chip);border-radius:999px;padding:2px 9px}
.badges{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end;max-width:340px}
.b{font-size:11.5px;padding:3.5px 10px;border-radius:999px;border:1px solid transparent;white-space:nowrap;display:inline-flex;gap:5px;align-items:center}
.b::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor;opacity:.75}
.b.ok{background:var(--okbg);color:var(--dn);border-color:var(--okline)}
.b.warn{background:var(--warnbg);color:var(--warn);border-color:var(--warnline)}
.b.stop{background:var(--stopbg);color:var(--up);border-color:var(--stopline)}
.b.gray{background:var(--chip);color:var(--tx2);border-color:var(--line)}
.b.gray::before{display:none}
.b.cat{background:color-mix(in srgb,var(--accent) 12%,transparent);color:var(--accent);border-color:color-mix(in srgb,var(--accent) 30%,transparent)}
.b.new{background:var(--warnbg);color:var(--warn);border-color:var(--warnline);font-weight:600}
.limit{margin-top:11px;background:var(--warnbg);border:1px dashed var(--warnline);color:var(--warn);
  border-radius:12px;padding:8px 12px;font-size:12.5px;display:flex;gap:8px;align-items:flex-start}
.limit .ic{flex:0 0 auto;margin-top:2px}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(104px,1fr));gap:9px;margin-top:13px}
.kv .i{background:var(--card2);border:1px solid var(--line2);border-radius:13px;padding:8px 11px}
.kv .i .k{font-size:11px;color:var(--tx3)}
.kv .i .v{font-size:14px;font-weight:600;margin-top:3px;word-break:break-word;letter-spacing:-.01em}
.rets{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;margin-top:12px}
.rets.four{grid-template-columns:repeat(4,1fr)}
@media(max-width:620px){.rets{grid-template-columns:repeat(3,1fr)}.rets.four{grid-template-columns:repeat(2,1fr)}}
.ret{background:var(--card2);border:1px solid var(--line2);border-radius:13px;padding:8px 6px;text-align:center}
.ret .k{font-size:10.5px;color:var(--tx3);letter-spacing:.02em}
.ret .v{font-size:14px;font-weight:620;margin-top:3px;letter-spacing:-.01em}
.up{color:var(--up)}.dn{color:var(--dn)}.flat{color:var(--tx2)}
.sec{margin-top:13px;border-top:1px solid var(--line2);padding-top:11px}
.sec h4{margin:0 0 8px;font-size:12px;color:var(--tx2);font-weight:600;display:flex;justify-content:space-between;gap:10px;
  letter-spacing:.04em;text-transform:uppercase}
.sec h4 span:last-child{text-transform:none;letter-spacing:0;color:var(--tx3);font-weight:400}
table.hold{width:100%;border-collapse:collapse;font-size:12.5px}
table.hold th{color:var(--tx3);font-weight:500;text-align:left;padding:5px 6px;border-bottom:1px solid var(--line);font-size:11.5px}
table.hold td{padding:5px 6px;border-bottom:1px solid var(--line2)}
table.hold tr:last-child td{border-bottom:none}
table.hold td.n{text-align:right;font-variant-numeric:tabular-nums}
.tags{display:flex;gap:7px;flex-wrap:wrap;font-size:11.5px;color:var(--tx2)}
.tag{background:var(--chip);border:1px solid var(--line2);border-radius:9px;padding:4px 9px}

/* ---------- News ---------- */
.news{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:16px 17px;position:sticky;top:88px;box-shadow:var(--shadow)}
@media(max-width:1100px){.news{position:static}}
.news h2{margin:0 0 12px;font-size:15px;font-weight:620;display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.news h2 em{font-style:normal;font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--tx3);font-weight:400}
.news ol{margin:0;padding:0;list-style:none;max-height:72vh;overflow:auto}
.news li{padding:10px 0;border-bottom:1px solid var(--line2)}
.news li:last-child{border-bottom:none}
.news .d{font-size:10.5px;color:var(--tx3);margin-bottom:4px;display:flex;gap:7px;flex-wrap:wrap;align-items:center}
.news .d .dt{background:var(--chip);border-radius:999px;padding:2px 8px}
.news .t{font-size:13px;line-height:1.5}
.news .f{font-size:11.5px;color:var(--pri);margin-top:4px}
.news .ntop{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin-bottom:5px}
.news .ntag{font-size:11px;padding:2px 9px;border-radius:999px;font-weight:600;border:1px solid transparent;white-space:nowrap}
.ntag.t-stop{background:var(--stopbg);color:var(--up);border-color:var(--stopline)}
.ntag.t-warn{background:var(--warnbg);color:var(--warn);border-color:var(--warnline)}
.ntag.t-ok{background:var(--okbg);color:var(--dn);border-color:var(--okline)}
.ntag.t-gray{background:var(--chip);color:var(--tx2);border-color:var(--line)}
.news .nbr{font-size:12.8px;line-height:1.55;margin:1px 0 6px;color:var(--tx)}
.news .nf{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:11.5px;color:var(--tx3)}
.news .nf b{color:var(--tx2);font-weight:600}
.news .nf a{white-space:nowrap}
.news .facts{display:flex;gap:5px;flex-wrap:wrap;margin-top:6px}
.news .fact{background:var(--chip);border:1px solid var(--line2);border-radius:8px;padding:2px 8px;font-size:11px;color:var(--tx2)}

/* ---------- Card collapse ---------- */
.chead{cursor:pointer}
.chead-r{display:flex;gap:10px;align-items:center;flex-wrap:wrap;justify-content:flex-end}
.tgl{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);background:var(--card2);color:var(--tx2);
  border-radius:999px;padding:6px 12px;font-size:12px;font-family:inherit;cursor:pointer;transition:.18s;white-space:nowrap}
.tgl:hover{border-color:var(--pri);color:var(--pri)}
.tgl .chev{transition:transform .22s ease}
.card:not(.collapsed) .tgl .chev{transform:rotate(180deg)}
.card.collapsed .detail{display:none}
.detail{margin-top:13px}
.kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:9px;margin-top:12px}
@media(max-width:1100px){.kpis{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:640px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}}
.kpi{background:var(--card2);border:1px solid var(--line2);border-radius:13px;padding:8px 11px;min-width:0}
.kpi .k{font-size:11px;color:var(--tx3)}
.kpi .v{font-size:13.5px;font-weight:600;margin-top:3px;letter-spacing:-.01em;word-break:break-word;line-height:1.35}
.kpi .v small{font-weight:400;color:var(--tx3);font-size:11px}

/* ---------- Change box ---------- */
.chg{background:var(--warnbg);border:1px solid var(--warnline);border-radius:20px;padding:15px 17px;margin-bottom:15px;box-shadow:var(--shadow)}
.chg h2{margin:0 0 9px;font-size:14.5px;color:var(--warn);display:flex;gap:9px;align-items:center;font-weight:620}
.chg ul{margin:0;padding-left:20px;font-size:13px;color:var(--tx2);line-height:1.75}
.chg li b{color:var(--tx)}
.chg.ok{background:var(--okbg);border-color:var(--okline)}
.chg.ok h2{color:var(--dn)}

/* ---------- History ---------- */
.hist{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:17px 18px;margin-top:20px;box-shadow:var(--shadow)}
.hist h2{margin:0 0 12px;font-size:15px;font-weight:620;display:flex;justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap}
.hist h2 em{font-style:normal;font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--tx3);font-weight:400}
.histwrap{max-height:480px;overflow:auto;border:1px solid var(--line2);border-radius:14px}
table.histtable{width:100%;border-collapse:collapse;font-size:12.6px}
table.histtable th{position:sticky;top:0;background:var(--card2);color:var(--tx3);font-weight:500;text-align:left;padding:9px 11px;
  border-bottom:1px solid var(--line);z-index:2;font-size:11.5px;letter-spacing:.03em}
table.histtable td{padding:9px 11px;border-bottom:1px solid var(--line2);vertical-align:top}
table.histtable tr:last-child td{border-bottom:none}
table.histtable tr.addrow td{background:rgba(52,199,89,.045)}
.bd{font-size:10.5px;padding:2.5px 8px;border-radius:999px;white-space:nowrap;border:1px solid transparent;display:inline-block;margin-left:4px}
.bd.c{background:var(--warnbg);color:var(--warn);border-color:var(--warnline)}
.bd.a{background:var(--okbg);color:var(--dn);border-color:var(--okline)}

/* ---------- Misc ---------- */
footer{margin-top:26px;font-size:11.5px;color:var(--tx3);line-height:1.85;border-top:1px solid var(--line);padding-top:16px}
footer b{color:var(--tx2)}
.empty{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:28px;text-align:center;color:var(--tx3);font-size:13px}

/* ===== MOD-INJECTED：对比 / 定投推演样式 ===== */
/* ============ 模块：基金自助对比 ============ */
.mods{margin-top:30px}
.mod{background:var(--card);border:1px solid var(--line);border-radius:22px;box-shadow:var(--shadow);padding:20px 22px 22px;margin-bottom:18px}
.modh{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.modh h2{margin:0;font-size:16.5px;font-weight:620;letter-spacing:-.01em}
.modh em{font-style:normal;font-size:11.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--tx3)}
.modh .hint{margin-left:auto;font-size:12px;color:var(--tx3)}
.modsub{margin:6px 0 15px;font-size:12.5px;color:var(--tx3);line-height:1.65}
.pick{display:grid;grid-template-columns:310px minmax(0,1fr);gap:14px}
@media(max-width:900px){.pick{grid-template-columns:1fr}}
.pickbox{border:1px solid var(--line2);border-radius:16px;overflow:hidden;background:var(--card2);display:flex;flex-direction:column}
.pickbox .pt{display:flex;gap:8px;align-items:center;padding:10px 12px;border-bottom:1px solid var(--line2)}
.pickbox .pt input{flex:1;border:0;background:transparent;color:var(--tx);font-size:13.5px;outline:none;min-width:0}
.pickbox .pt .ic{color:var(--tx3);flex:0 0 auto}
.picklist{max-height:250px;overflow:auto;padding:6px}
.pickrow{display:flex;align-items:center;gap:9px;padding:7px 9px;border-radius:10px;cursor:pointer;font-size:12.8px}
.pickrow:hover{background:var(--chip)}
.pickrow.on{background:rgba(0,113,227,.1)}
.pickrow .cb{width:15px;height:15px;border-radius:5px;border:1.4px solid var(--line);flex:0 0 auto;display:grid;place-items:center;font-size:11px;color:#fff;line-height:1}
.pickrow.on .cb{background:var(--pri);border-color:var(--pri)}
.pickrow .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pickrow .cd{color:var(--tx3);font-size:11.5px;flex:0 0 auto}
.pickcnt{padding:7px 12px;border-top:1px solid var(--line2);font-size:11.5px;color:var(--tx3)}
.picks{display:flex;flex-wrap:wrap;gap:7px;min-height:52px;align-content:flex-start;padding:11px;border:1px dashed var(--line);border-radius:14px}
.chipx{display:inline-flex;align-items:center;gap:6px;background:var(--chip);border:1px solid var(--line2);border-radius:999px;padding:5px 8px 5px 11px;font-size:12.3px}
.chipx b{font-weight:600}
.chipx span{color:var(--tx3);font-size:11.5px}
.chipx i{cursor:pointer;font-style:normal;color:var(--tx3);font-size:15px;line-height:1;padding:0 2px}
.chipx i:hover{color:var(--up)}
.pickbtns{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.cmpwrap{margin-top:15px;overflow:auto;border:1px solid var(--line2);border-radius:16px;max-height:none}
table.cmptable{border-collapse:separate;border-spacing:0;font-size:12.6px;width:100%}
table.cmptable th,table.cmptable td{border-bottom:1px solid var(--line2);padding:8px 12px;text-align:right;white-space:nowrap}
table.cmptable thead th{position:sticky;top:0;background:var(--card2);z-index:2;font-weight:600;color:var(--tx2);vertical-align:bottom}
table.cmptable thead th .sub{display:block;font-size:11px;color:var(--tx3);font-weight:400;margin-top:2px}
table.cmptable th.f,table.cmptable td.f{text-align:left;position:sticky;left:0;background:var(--card);font-weight:500;color:var(--tx2);z-index:1;min-width:112px}
table.cmptable thead th.f{left:0;z-index:3;background:var(--card2)}
table.cmptable tr.grp td{position:sticky;left:0;background:var(--card2);color:var(--tx3);font-size:11.5px;letter-spacing:.08em;text-transform:uppercase;text-align:left;padding:7px 12px}
table.cmptable td .best{color:var(--dn);font-weight:650}
table.cmptable td .worst{color:var(--up)}
.capnote{margin-top:10px;font-size:11.6px;color:var(--tx3);line-height:1.65}

/* ============ 模块：历史定投推演 ============ */
.form{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:11px}
.fld{background:var(--card2);border:1px solid var(--line2);border-radius:14px;padding:9px 12px}
.fld label{display:block;font-size:11px;color:var(--tx3);margin-bottom:5px}
.fld select,.fld input{width:100%;border:0;background:transparent;color:var(--tx);font-size:14.5px;font-weight:600;outline:none;font-variant-numeric:tabular-nums}
.fld select{cursor:pointer}
.fld .unit{font-size:11px;color:var(--tx3);font-weight:400}
.go{margin-top:15px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.btn{display:inline-flex;align-items:center;gap:7px;border:0;background:var(--pri);color:#fff;border-radius:12px;padding:10px 20px;font-size:14px;font-weight:600;cursor:pointer;transition:.2s}
.btn:hover{filter:brightness(1.08);transform:translateY(-1px)}
.btn.ghost{background:var(--card2);color:var(--tx2);border:1px solid var(--line)}
.res{margin-top:18px}
.res .empty{margin-top:4px}
.resgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:11px}
.rk{background:var(--card2);border:1px solid var(--line2);border-radius:15px;padding:11px 13px}
.rk .k{font-size:11.5px;color:var(--tx3)}
.rk .v{font-size:19px;font-weight:650;margin-top:4px;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.rk .s{font-size:11px;color:var(--tx3);margin-top:3px}
.rk.hl{background:linear-gradient(150deg,rgba(0,113,227,.1),rgba(120,80,255,.08));border-color:rgba(0,113,227,.24)}
.chartbox{margin-top:16px;border:1px solid var(--line2);border-radius:16px;padding:14px 12px 4px;background:var(--card2)}
.chartbox svg{width:100%;min-width:700px;height:auto;display:block}
.chartbox .lg{display:flex;gap:16px;flex-wrap:wrap;font-size:11.6px;color:var(--tx3);padding:8px 6px 8px}
.chartbox .lg i{display:inline-block;width:16px;height:3px;border-radius:2px;vertical-align:middle;margin-right:6px}
table.yeartable{width:100%;border-collapse:collapse;font-size:12.6px;margin-top:16px}
table.yeartable th,table.yeartable td{padding:9px 11px;border-bottom:1px solid var(--line2);text-align:right;white-space:nowrap}
table.yeartable th{background:var(--card2);color:var(--tx3);font-weight:500}
table.yeartable th:first-child,table.yeartable td:first-child{text-align:left}
table.yeartable tbody tr:hover{background:var(--card2)}
table.yeartable tr.sum td{font-weight:650;background:var(--card2)}
.simhead{display:flex;gap:10px;flex-wrap:wrap;align-items:baseline;margin-top:16px}
.simhead b{font-size:14px}
.simhead .meta{font-size:12px;color:var(--tx3)}
@media(max-width:640px){
  .mod{padding:16px 15px 18px;border-radius:18px}
  .resgrid{grid-template-columns:repeat(2,minmax(0,1fr))}
  .rk .v{font-size:16.5px}
  table.cmptable th,table.cmptable td{padding:7px 9px}
  .modh .hint{display:none}
}
/* ================================================================
   v3 新增：折叠卡片 + 对比筛选/走势图 + 推演过程动画
   ================================================================ */

/* ---------- 折叠模块卡片 ---------- */
.foldmod{background:var(--card);border:1px solid var(--line);border-radius:20px;box-shadow:var(--shadow);
  margin-bottom:14px;overflow:hidden;transition:border-color .25s ease,box-shadow .25s ease}
.foldmod.open{border-color:rgba(0,113,227,.3)}
.foldhd{display:flex;align-items:center;gap:14px;width:100%;padding:26px 18px;cursor:pointer;user-select:none;
  transition:background .18s ease}
.foldhd:hover{background:var(--card2)}
.foldhd:focus-visible{outline:2px solid var(--pri);outline-offset:-3px}
.foldhd .fico{width:42px;height:42px;border-radius:14px;display:grid;place-items:center;flex:0 0 auto;
  background:linear-gradient(150deg,rgba(0,113,227,.16),rgba(120,80,255,.14));color:var(--pri)}
.foldhd .fico .ic{width:20px;height:20px}
.foldhd .ftx{flex:1;min-width:0}
.foldhd .ftx h2{margin:0;font-size:16px;font-weight:620;letter-spacing:-.01em}
.foldhd .ftx p{margin:4px 0 0;font-size:12.2px;color:var(--tx3);line-height:1.5;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.foldhd .fst{flex:0 0 auto;font-size:11.5px;color:var(--tx2);background:var(--chip);border:1px solid var(--line2);
  border-radius:999px;padding:4px 10px;font-variant-numeric:tabular-nums;white-space:nowrap}
.foldhd .fst.on{background:var(--okbg);border-color:var(--okline);color:var(--dn)}
.foldhd .fchev{flex:0 0 auto;color:var(--tx3);width:18px;height:18px;transition:transform .32s cubic-bezier(.4,0,.2,1)}
.foldmod.open .foldhd .fchev{transform:rotate(180deg)}
.foldbd{display:grid;grid-template-rows:0fr;transition:grid-template-rows .4s cubic-bezier(.4,0,.2,1)}
.foldmod.open .foldbd{grid-template-rows:1fr}
.foldbd>.inner{overflow:hidden;min-height:0}
.foldbd .pad{padding:4px 18px 20px;border-top:1px solid var(--line2)}
.foldnote{margin:0 0 13px;font-size:12px;color:var(--tx3);line-height:1.65}

/* ---------- 对比：筛选 + 加宽列表 ---------- */
@media(min-width:901px){.pick{grid-template-columns:minmax(0,440px) minmax(0,1fr)}}
.pfilter{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;padding:8px 9px;border-bottom:1px solid var(--line2)}
.pfilter select{width:100%;border:1px solid var(--line);background:var(--card);color:var(--tx2);
  border-radius:9px;padding:6px 7px;font-size:11.8px;font-family:inherit;cursor:pointer;outline:none}
.pfilter select:focus{border-color:var(--pri)}
.picklist{max-height:380px}
.pickrow{padding:9.5px 10px;gap:10px;align-items:flex-start}
.pickrow .cb{margin-top:2px}
.pickrow .nm{flex:1;min-width:0;display:block;white-space:normal}
.pickrow .nm b{display:block;font-size:12.9px;font-weight:600;line-height:1.35;word-break:break-word}
.pickrow .nm em{display:block;font-style:normal;font-size:11.2px;color:var(--tx3);margin-top:3px;line-height:1.4}
.pickrow .nm em .up{color:var(--up)}
.pickrow .nm em .dn{color:var(--dn)}
.pickrow .b{font-size:11px;padding:2.5px 8px;margin-top:1px;flex:0 0 auto}

/* ---------- 对比：业绩走势图 ---------- */
.cmptrend{margin-top:15px;border:1px solid var(--line2);border-radius:18px;background:var(--card2);padding:14px 16px 8px;
  box-shadow:0 1px 2px rgba(0,0,0,.03),0 12px 30px -20px rgba(0,0,0,.24)}
.cmptrend .thead{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.cmptrend .thead h3{margin:0;font-size:13.8px;font-weight:640;letter-spacing:.01em;display:inline-flex;align-items:center}
.cmptrend .thead h3:before{content:'';width:3px;height:13px;border-radius:2px;background:var(--pri);margin-right:8px}
.cmptrend .thead .sub{font-size:11.5px;color:var(--tx3)}
.cmptrend .thead .thint{font-size:11px;color:var(--tx3);padding:2px 8px;border:1px solid var(--line2);border-radius:999px}
.rtabs{display:flex;gap:5px;margin-left:auto;flex-wrap:wrap}
.rtab{border:1px solid var(--line2);background:var(--card);color:var(--tx2);border-radius:999px;
  padding:4.5px 12px;font-size:12px;cursor:pointer;font-family:inherit;transition:.18s;font-variant-numeric:tabular-nums}
.rtab:hover{border-color:var(--pri);color:var(--pri)}
.rtab.on{background:var(--pri);border-color:var(--pri);color:#fff;font-weight:600;box-shadow:0 4px 12px -5px var(--pri)}
.trendwrap{position:relative;overflow:auto;border-radius:14px}
.trendwrap svg{width:100%;min-width:680px;height:auto;display:block}
.tlegend{display:flex;flex-wrap:wrap;gap:7px 16px;padding:11px 2px 7px;font-size:11.9px;max-height:110px;overflow-y:auto;
  border-top:1px solid var(--line);margin-top:4px}
.tlegend .tl{display:inline-flex;align-items:center;gap:6px;cursor:default}
.tlegend .tl i{width:15px;height:3px;border-radius:2px;display:inline-block;flex:0 0 auto}
.tlegend .tl b{font-weight:600}
.tlegend .tl span{color:var(--tx3);font-variant-numeric:tabular-nums}
.tlegend .tl span.tv{font-weight:620;color:var(--tx2)}
.tlegend .tl span.tv.up{color:#d94a45}.tlegend .tl span.tv.dn{color:#1f9d57}
.tlegend .tl.na{opacity:.55}
.ttip{position:absolute;pointer-events:none;z-index:9;min-width:172px;max-width:286px;display:none;
  background:var(--card);border:1px solid var(--line);border-radius:13px;padding:10px 12px;
  box-shadow:0 16px 38px -14px rgba(0,0,0,.42);font-size:11.8px;line-height:1.62}
.ttip .tt{font-weight:640;font-size:12.1px;margin-bottom:6px;padding-bottom:5px;border-bottom:1px solid var(--line);
  font-variant-numeric:tabular-nums}
.ttip .tr{display:flex;gap:8px;justify-content:space-between;align-items:baseline}
.ttip .tr em{font-style:normal;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ttip .tr b{font-variant-numeric:tabular-nums;flex:0 0 auto;font-weight:620}
.trendnote{margin:2px 0 6px;font-size:11.4px;color:var(--tx3);line-height:1.6}

/* ---------- 推演：过程动画 ---------- */
.chartbox .chead2{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:0 4px 8px}
.chartbox .chead2 .live{font-size:12px;color:var(--tx2);font-variant-numeric:tabular-nums;
  display:flex;gap:12px;flex-wrap:wrap;align-items:baseline}
.chartbox .chead2 .live b{font-weight:620}
.chartbox .chead2 .live .up{color:var(--up)}
.chartbox .chead2 .live .dn{color:var(--dn)}
.chartbox .chead2 .cbtns{margin-left:auto;display:flex;gap:7px}
.chartbox .chead2 .cbtns button{border:1px solid var(--line2);background:var(--card);color:var(--tx2);
  border-radius:999px;padding:4.5px 12px;font-size:11.8px;cursor:pointer;font-family:inherit;transition:.18s}
.chartbox .chead2 .cbtns button:hover{border-color:var(--pri);color:var(--pri)}
.chartbox.playing .chead2 .cbtns .skip{display:inline-block}
.chartbox .chead2 .cbtns .skip{display:none}
/* 动画播放期间彻底禁用图表区交互，动画结束后才允许悬停/拖动 */
.chartbox.playing .simwrap{pointer-events:none;cursor:default}
.chartbox.playing .chead2 .chint{opacity:.42}
.rk .v .ph{color:var(--tx3);font-weight:500}
.rk .v.counting{color:var(--pri)}
@media(max-width:640px){
  .foldhd{padding:16px 14px;gap:12px}
  .foldhd .ftx p{display:none}
  .foldbd .pad{padding:4px 13px 16px}
  .pfilter{grid-template-columns:1fr 1fr}
  .pfilter select:last-child{grid-column:span 2}
  .rtabs{margin-left:0;width:100%}
}

/* ================= v3.1 页首指数走势图（分时/日K/月K/年K） ================= */
.idxtrend{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:12px}
.idxtrend .itcard{background:var(--glass);border:1px solid rgba(255,255,255,.16);border-radius:15px;padding:9px 10px 7px;backdrop-filter:blur(10px)}
.idxtrend .ithead{display:flex;align-items:baseline;gap:7px;flex-wrap:wrap;margin-bottom:5px}
.idxtrend .itname{font-size:12.2px;font-weight:600;letter-spacing:.01em}
.idxtrend .itval{font-size:13.2px;font-weight:650;font-variant-numeric:tabular-nums}
.idxtrend .itpct{font-size:11.4px;font-variant-numeric:tabular-nums;opacity:.95}
.idxtrend .ittabs{margin-left:auto;display:flex;gap:4px}
.idxtrend .ittabs button{border:1px solid rgba(255,255,255,.24);background:transparent;color:inherit;opacity:.68;
  border-radius:999px;padding:2px 8px;font-size:10.6px;line-height:1.5;cursor:pointer;font-family:inherit;transition:all .16s ease}
.idxtrend .ittabs button:hover{opacity:1;border-color:rgba(255,255,255,.5)}
.idxtrend .ittabs button.on{background:rgba(255,255,255,.92);color:#14161c;border-color:rgba(255,255,255,.92);font-weight:640;opacity:1}
.idxtrend .itwrap{position:relative}
.idxtrend svg{width:100%;height:118px;display:block;border-radius:8px}
.idxtrend .ittip{position:absolute;pointer-events:none;z-index:30;min-width:118px;display:none;
  background:rgba(20,20,26,.92);color:#fff;border:1px solid rgba(255,255,255,.16);border-radius:10px;
  padding:6px 9px;font-size:11px;line-height:1.55;box-shadow:0 10px 26px rgba(0,0,0,.3);backdrop-filter:blur(10px)}
.idxtrend .ittip .tt{font-weight:620;margin-bottom:3px;font-variant-numeric:tabular-nums;opacity:.92}
.idxtrend .ittip .tr{display:flex;gap:12px;justify-content:space-between}
.idxtrend .ittip .tr em{font-style:normal;opacity:.72}
.idxtrend .ittip .tr b{font-variant-numeric:tabular-nums}
.idxtrend .itfoot{display:flex;justify-content:space-between;font-size:10px;opacity:.6;margin-top:3px;font-variant-numeric:tabular-nums}
.idxtrend .itfoot .mid{opacity:.85}
@media (max-width:760px){ .idxtrend{grid-template-columns:1fr} .idxtrend svg{height:130px} }

/* ================= v3.1 图表统一细化（Marvis / Apple 风） ================= */
.tlegend .tl.ovl i{width:15px;height:3px;border-radius:2px}
.tlegend .tl.ovl span.em{color:var(--tx3);font-size:10.6px}
.thint{font-size:11px;color:var(--tx3);margin-left:2px}
.chartbox .chead2 .chint{font-size:11px;color:var(--tx3);margin-left:2px}
.simwrap{position:relative}
.simwrap .ttip{min-width:172px}
/* ================= v3.3 iPhone 移动端适配（安全区 / 窄屏排布 / 触摸交互） ================= */
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
.wrap{padding-top:calc(22px + var(--sat));padding-bottom:calc(70px + var(--sab));
  padding-left:calc(20px + var(--sal));padding-right:calc(20px + var(--sar))}
/* 触摸设备：在图表上横向拖动查看数值时不滚动页面、不选中文字、不弹出长按菜单 */
.trendwrap,.simwrap,.idxtrend .itwrap{touch-action:pan-y;-webkit-user-select:none;user-select:none;-webkit-touch-callout:none}
.trendwrap svg,.simwrap svg,.idxtrend svg{touch-action:pan-y}
/* 窄屏改用与容器等宽的 viewBox，取消横向最小宽度，图表在手机上完整显示（不再左右裁切） */
.trendwrap svg.fit,.chartbox svg.fit{min-width:0}
/* 推演年度明细：窄屏可横向滚动，避免撑破卡片 */
.yearwrap{margin-top:16px;overflow:auto;border:1px solid var(--line2);border-radius:14px;-webkit-overflow-scrolling:touch}
.yearwrap table.yeartable{margin-top:0}
/* iOS：聚焦输入框时字号不小于 16px，避免页面被自动放大 */
@media(max-width:640px){
  input,select,textarea{font-size:16px}
  .wrap{padding:calc(14px + var(--sat)) calc(12px + var(--sar)) calc(46px + var(--sab)) calc(12px + var(--sal))}
  .mod{padding:15px 13px 17px;border-radius:18px}
  .toolbar{padding:10px;margin:12px 0 14px;border-radius:16px;gap:8px}
  .toolbar .search{flex:1 1 100%;min-width:0}
  .toolbar select{flex:1 1 45%;min-width:0}
  .tgl{flex:1 1 45%}
  .resgrid{gap:9px}
  .rk .v{font-size:17px}
  .ttip{max-width:min(286px,84vw)}
  .idxtrend .ittip{max-width:min(240px,78vw)}
}
@media(max-width:430px){
  .wrap{padding-left:calc(10px + var(--sal));padding-right:calc(10px + var(--sar))}
  .resgrid{grid-template-columns:repeat(2,minmax(0,1fr))}
  .rk .v{font-size:15.5px}
  table.yeartable{font-size:11.5px}
  table.yeartable th,table.yeartable td{padding:7px 8px;white-space:normal}
  .idxtrend svg{height:124px}
}

</style>
</head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <symbol id="i-chart" viewBox="0 0 24 24"><path d="M4 19h16M7 19V9m5 10V5m5 14v-7" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></symbol>
  <symbol id="i-layers" viewBox="0 0 24 24"><path d="M12 3l9 5-9 5-9-5 9-5z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M4 13l8 4.5L20 13" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></symbol>
  <symbol id="i-lock" viewBox="0 0 24 24"><rect x="5" y="10.5" width="14" height="9.5" rx="2.6" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M8.5 10.5V8a3.5 3.5 0 017 0v2.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></symbol>
  <symbol id="i-pause" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M10 9v6M14 9v6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></symbol>
  <symbol id="i-bell" viewBox="0 0 24 24"><path d="M6.5 16.5V11a5.5 5.5 0 1111 0v5.5l1.4 2.1H5.1l1.4-2.1z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M10 20.5h4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></symbol>
  <symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.2" fill="none" stroke="currentColor" stroke-width="1.9"/><path d="M15.6 15.6L20 20" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></symbol>
  <symbol id="i-moon" viewBox="0 0 24 24"><path d="M20 14.5A8.2 8.2 0 019.5 4a8.5 8.5 0 106.9 10.4c1.3 0 2.5-.3 3.6-.9z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></symbol>
  <symbol id="i-sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4.2" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M12 2.8v2.4M12 18.8v2.4M2.8 12h2.4M18.8 12h2.4M5.5 5.5l1.7 1.7M16.8 16.8l1.7 1.7M18.5 5.5l-1.7 1.7M7.2 16.8l-1.7 1.7" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></symbol>
  <symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.6" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M12 7.4V12l3.2 2" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></symbol>
  <symbol id="i-globe" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.6" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M3.6 12h16.8M12 3.4c2.4 2.4 3.6 5.3 3.6 8.6S14.4 18.2 12 20.6c-2.4-2.4-3.6-5.3-3.6-8.6S9.6 5.8 12 3.4z" fill="none" stroke="currentColor" stroke-width="1.5"/></symbol>
  <symbol id="i-shield" viewBox="0 0 24 24"><path d="M12 3.6l7 2.6v5.2c0 4.2-3 7.4-7 9-4-1.6-7-4.8-7-9V6.2l7-2.6z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></symbol>
  <symbol id="i-list" viewBox="0 0 24 24"><path d="M4 6.5h16M4 12h16M4 17.5h10" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></symbol>
  <symbol id="i-chev" viewBox="0 0 24 24"><path d="M6.5 9.5l5.5 5.5 5.5-5.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></symbol>
  <symbol id="i-doc" viewBox="0 0 24 24"><path d="M7 3.5h7l4 4v13H7z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M14 3.5v4h4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></symbol>
</svg>
<div class="wrap">
  <header class="hero">
    <div class="grid"></div>
    <div class="hero-top">
      <div class="brand">
        <div class="logo"><svg class="ic"><use href="#i-chart"/></svg></div>
        <div>
          <h1>QDII Fund Monitor<em>offshore index funds · purchase status</em></h1>
          <p class="cn">场外 QDII 基金申购状态与单日限额监控 ｜ 纳斯达克100 · 纳斯达克科技 · 纳斯达克生物科技 · 纳斯达克精选 · 标普500 · 标普500等权重</p>
        </div>
      </div>
      <button id="themeBtn" class="icon-btn" title="Light / Dark"><svg class="ic"><use href="#i-moon"/></svg></button>
    </div>
    <div class="hero-meta">
      <span class="pillglass"><svg class="ic"><use href="#i-layers"/></svg> 监控 <b>__COUNT__</b> 只基金</span>
      <span class="pillglass"><svg class="ic"><use href="#i-clock"/></svg> 扫描时间 <b>__TS__</b></span>
      <span class="pillglass"><svg class="ic"><use href="#i-globe"/></svg> 数据源：天天基金（净值 / 申购 / 限额）· 腾讯 · 新浪 · CNBC · 纳斯达克官方（指数多源交叉校验）</span>
    </div>
    <!-- v3.4 顶部时间标注：三行说明数据时间口径，避免把「未收盘 / 休市 / 调度延迟」误判为没更新 -->
    <div class="databar">
      <div class="row"><span class="k">指数数据日期（美东）</span><b>__IDXDATE__</b><em>最近已收盘交易日</em></div>
      <div class="row"><span class="k">本次扫描时间（北京时间）</span><b>__SCANBJ__</b><em>已按 UTC+8 换算</em></div>
      <div class="row"><span class="k">下次更新（北京时间）</span><b>06:30 前后</b><em>GitHub 调度可能延迟；另有 09:30 兜底补跑一次</em></div>
    </div>
    <div class="idxrow" id="idxrow"></div>
    <div class="idxnote" id="idxnote">__IDXNOTE__</div>
    <div class="idxtrend" id="idxtrend"></div>
  </header>

  <div class="sec-title"><h2>概览</h2><em>Overview</em></div>
  <div class="stats" id="stats"></div>

  <div id="chgbox" style="margin-top:15px"></div>

  <div class="mods">
  <!-- ==================== 模块 1：基金自助对比（折叠卡片） ==================== -->
  <section class="foldmod" id="cmpmod">
    <div class="foldhd" role="button" tabindex="0" aria-expanded="false" aria-controls="cmpBody">
      <span class="fico"><svg class="ic"><use href="#i-layers"/></svg></span>
      <span class="ftx"><h2>基金自助对比</h2>
        <p>不限数量多选 · 指标横向对比 · 申购状态 / 限额 / 费率 / 回撤 + 业绩走势图</p></span>
      <span class="fst" id="cmpBadge">已选 0 只</span>
      <svg class="ic fchev"><use href="#i-chev"/></svg>
    </div>
    <div class="foldbd" id="cmpBody"><div class="inner"><div class="pad">
      <p class="foldnote">从全部监控基金中任意挑选（数量不设上限），可按跟踪标的、申购状态、币种筛选后勾选，逐项横向对比申购状态、限额、各阶段收益、规模、费率、跟踪误差、回撤等指标；表格上方另附「业绩走势图」，支持近 1 月 / 3 月 / 6 月 / 1 年 / 3 年区间切换。收益类指标自动标出组合内最高（<span class="dn">绿</span>）与最低（<span class="up">红</span>），费率 / 回撤类反向标优。</p>
      <div class="pick">
        <div class="pickbox">
          <div class="pt"><svg class="ic"><use href="#i-search"/></svg><input id="cmpq" type="search" placeholder="搜索名称 / 代码 / 标的 / 公司"></div>
          <div class="pfilter">
            <select id="cmpCat" title="按跟踪标的筛选"></select>
            <select id="cmpSt" title="按申购状态筛选">
              <option value="">全部申购状态</option>
              <option value="限大额">限大额</option>
              <option value="暂停申购">暂停申购</option>
              <option value="封闭期">封闭期</option>
            </select>
            <select id="cmpCur" title="按份额币种筛选">
              <option value="">全部币种</option>
              <option value="人民币">人民币份额</option>
              <option value="美元">美元份额</option>
            </select>
          </div>
          <div class="picklist" id="cmpList"></div>
          <div class="pickcnt" id="cmpCnt">共 0 只基金</div>
        </div>
        <div>
          <div class="picks" id="cmpPicks"></div>
          <div class="pickbtns">
            <button class="btn" type="button" id="cmpGo">生成对比</button>
            <button class="tgl" type="button" id="cmpHotNdx">加入纳指100组</button>
            <button class="tgl" type="button" id="cmpHotSp">加入标普500组</button>
            <button class="tgl" type="button" id="cmpTop5">加入收益前5</button>
            <button class="tgl" type="button" id="cmpClear">清空</button>
          </div>
        </div>
      </div>
      <div id="cmpOut"></div>
    </div></div></div>
  </section>

  <!-- ==================== 模块 2：历史定投推演（折叠卡片） ==================== -->
  <section class="foldmod" id="simmod">
    <div class="foldhd" role="button" tabindex="0" aria-expanded="false" aria-controls="simBody">
      <span class="fico"><svg class="ic"><use href="#i-chart"/></svg></span>
      <span class="ftx"><h2>历史定投推演</h2>
        <p>指数 / 场外基金双口径 · 定投或一次性 · 动画回放账户从起点到今天的起起伏伏</p></span>
      <span class="fst" id="simBadge">未推演</span>
      <svg class="ic fchev"><use href="#i-chev"/></svg>
    </div>
    <div class="foldbd" id="simBody"><div class="inner"><div class="pad">
      <p class="foldnote">选择过往年份、投资标的、投入方式与金额，按历史真实行情推演至今，输出账户总结与历年收益；推演完成后会以动画逐帧回放账户从起点到今天的起伏过程，可随时重播。指数口径为价格指数（不含分红），基金口径为累计净值（含分红再投）。历史推演不代表未来收益。</p>
      <div class="form">
        <div class="fld"><label>推演起点</label><select id="simYear"></select></div>
        <div class="fld"><label>投资标的</label><select id="simTarget"></select></div>
        <div class="fld"><label>投入方式</label><select id="simMode">
          <option value="dca">每月定投</option><option value="lump">一次性投入</option></select></div>
        <div class="fld"><label>投入金额 <span class="unit" id="simUnit">（元 / 月）</span></label><input id="simAmt" type="number" min="1" step="100" value="1000" inputmode="numeric"></div>
        <div class="fld" id="simDayFld"><label>每月定投日</label><select id="simDay">
          <option value="first">每月首个交易日</option><option value="mid">每月 15 日前后</option><option value="last">每月最后交易日</option></select></div>
      </div>
      <div class="go">
        <button class="btn" type="button" id="simRun">开始推演</button>
        <button class="tgl" type="button" id="simReset">重置条件</button>
        <span class="meta" style="font-size:12px;color:var(--tx3)" id="simRange"></span>
      </div>
      <div class="res" id="simOut"><div class="empty">选择起点与标的，点击「开始推演」生成推演报告（含过程动画）。</div></div>
    </div></div></div>
  </section>
  </div>


  <div class="toolbar">
    <label class="search"><svg class="ic"><use href="#i-search"/></svg>
      <input id="q" type="search" placeholder="搜索基金名称 / 代码 / 基金公司 / 基金经理 / 跟踪标的">
    </label>
    <select id="fIdx"><option value="">全部跟踪标的</option></select>
    <select id="fSt"><option value="">全部申购状态</option><option value="open">开放申购</option><option value="limit">限大额</option><option value="stop">暂停 / 不可申购</option></select>
    <select id="fCur"><option value="">全部币种</option><option value="人民币">人民币份额</option><option value="美元">美元份额</option></select>
    <select id="fType"><option value="">全部形态</option><option value="link">ETF联接</option><option value="direct">指数 / LOF / FOF</option></select>
    <select id="sortBy">
      <option value="none">默认排序</option>
      <option value="scale">规模降序</option>
      <option value="m1">近1月收益降序</option>
      <option value="m3">近3月收益降序</option>
      <option value="m6">近6月收益降序</option>
      <option value="y1">近1年收益降序</option>
      <option value="y3">近3年收益降序</option>
      <option value="since">成立以来收益降序</option>
      <option value="mgrFee">管理费率升序</option>
      <option value="code">代码升序</option>
    </select>
    <button class="tgl" type="button" id="expAll">全部展开</button>
    <button class="tgl" type="button" id="colAll">全部收起</button>
  </div>

  <div class="layout">
    <div class="cards" id="list"></div>
    <aside class="news">
      <h2>最新公告 · 关键信息 <em>Latest Updates</em></h2>
      <ol id="newslist"></ol>
    </aside>
  </div>

  <section class="hist">
    <h2>历史变动日志 <em>Change History</em> <span style="font-size:12px;color:var(--tx3);margin-left:auto">累计 <b id="histcnt">0</b> 条 ｜ 最近在前 ｜ 最多显示最近 300 条</span></h2>
    <div class="histwrap" id="histwrap">
      <table class="histtable">
        <thead><tr><th style="width:136px">扫描时间</th><th style="width:250px">基金</th><th>变动内容</th></tr></thead>
        <tbody id="histbody"></tbody>
      </table>
    </div>
    <div class="empty" id="histempty" style="display:none">暂无变动记录。每次扫描会与上一份快照逐只比对，出现申购状态或单日限额变化时自动记入本表。</div>
  </section>

  <footer>
    <b>数据来源与交叉验证</b>：
    ① <b>基金净值 / 申购状态 / 单日累计限额 / 阶段收益</b>：天天基金（东方财富）公开数据 —— 手机接口（fundmobapi：SGZT 申购状态、TRADEMARKLIST 限额、DWJZ 单位净值、FSRQ 净值日期、RZDF 日涨跌幅、SYL_* 阶段收益率）+ PC 详情页（交易状态区块、年化跟踪误差），两者互校，页面「单日累计限额」已按来源标注；
    ② <b>指数行情</b>：主源腾讯行情（qt.gtimg.cn），交叉验证源新浪财经（hq.sinajs.cn）／CNBC／纳斯达克官方 API，各源点位相对偏差需 ≤ 0.05% 且「最新价 − 昨收 = 涨跌额」自洽校验通过，方可标记为「N 源校验通过」；行情时间戳为发布方按美东时间（EDT/EST）标注的最终快照时刻，落在常规时段（09:30–16:00）收盘后，页面同时给出折算的北京时间；
    ③ <b>指数分时 / 日K / 月K / 年K</b>：腾讯分时接口（usMinute）+ 新浪美股日线（US_MinKService），月 K / 年 K 由日线聚合，均为价格指数口径、未含汇率与费率；
    ④ <b>规模</b>为披露的净资产规模（截止日见各卡片）；阶段收益为复权净值增长率。
    <span class="vfy">__VERIFY__</span>
    「单日累计购买上限」以天天基金展示的申购限制为准，实际以基金公司公告和销售平台为准。<br>
    <b>声明</b>：本页面为数据聚合展示工具，不构成任何投资建议或要约。QDII 基金存在汇率、境外市场、额度限制等风险，请阅读基金合同与招募说明书。<br>
    <b>字段说明</b>：「年化跟踪误差」取自天天基金基金详情页档案栏（接口不提供），缺失时显示「—」；「手续费（年综合）」= 管理费 + 托管费 + 销售服务费，
    不含一次性申购费与赎回费（申购费折后 / 原价见展开详情）。<br>
    <b>交互</b>：基金卡片默认收起，仅显示名称、代码、申购状态、跟踪标的、单日累计限额、年化跟踪误差、手续费、近7日收益率；点击卡片或「展开详情」查看全部明细，工具栏支持一键全部展开 / 全部收起。
    右侧公告栏已按变更类型标注并提取关键信息，点「看原文」可跳转公告全文。<br>
    <b>Snapshot</b> ｜ 页面为静态快照，数据即扫描时刻数据；重新扫描运行 scan.py 即可更新。
  </footer>
</div>
<script id="DATA" type="application/json">__DATA__</script>
<script id="NEWS" type="application/json">__NEWS__</script>
<script id="CHANGES" type="application/json">__CHANGES__</script>
<script id="HISTORY" type="application/json">__HISTORY__</script>
<script id="IDX" type="application/json">__IDX__</script>
<script id="IDXT" type="application/json">__IDXT__</script>
<script>
var F = JSON.parse(document.getElementById('DATA').textContent);
var NEWS = JSON.parse(document.getElementById('NEWS').textContent);
var CHANGES = JSON.parse(document.getElementById('CHANGES').textContent);
var HISTORY = JSON.parse(document.getElementById('HISTORY').textContent);
var IDX = JSON.parse(document.getElementById('IDX').textContent);
var CATS = [['ndx100','纳斯达克100'],['ndx_tech','纳斯达克科技'],['ndx_bio','纳斯达克生物科技'],['ndx_active','纳斯达克精选(主动)'],['sp500','标普500'],['sp500_ew','标普500等权重']];
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function ic(n){return '<svg class="ic"><use href="#i-'+n+'"/></svg>'}
function pct(v,big){ if(v===null||v===undefined||v==='') return '<span class="flat">—</span>';
  var cls = v>0?'up':(v<0?'dn':'flat'); var ar = v>0?'▲':(v<0?'▼':'·');
  var t=(v>0?'+':'')+Number(v).toFixed(2)+'%';
  return '<span class="'+cls+'">'+(big?ar+' ':'')+t+'</span>'; }
function txt(v,d){ return (v===null||v===undefined||v==='')?(d||'—'):v; }
function stCls(s){ s=s||''; if(s.indexOf('暂停')>=0||s.indexOf('封闭')>=0) return 'stop'; if(s.indexOf('限')>=0) return 'warn';
  if(s.indexOf('开放')>=0) return 'ok'; return 'gray'; }
function curCls(c){ return c==='美元'?'cur':'gray'; }
function card(f){
  var h='';
  h+='<article class="card cat-'+(f.catKey||f.indexKey||'other')+(f.changed?' changed':'')+' collapsed">';
  h+='<div class="chead" onclick="toggleCard(this)"><div class="ctitle"><h3>'+esc(f.name)+'</h3><div class="cmeta">'+
     '<span class="mono">'+esc(f.code)+'</span><span>'+esc(txt(f.catName,f.indexName))+'</span>'+
     '<span>'+esc(f.currency)+'</span></div></div>'+
     '<div class="chead-r"><div class="badges">'+
       (f.changed?'<span class="b new">'+ic('bell')+'有变动</span>':'')+
       '<span class="b '+stCls(f.sgzt)+'">申购 ' + esc(txt(f.sgzt,'未知'))+'</span>'+
     '</div>'+
     '<button class="tgl" type="button">展开详情'+ic('chev')+'</button></div></div>';
  h+='<div class="kpis">'+
     '<div class="kpi"><div class="k">跟踪标的</div><div class="v">'+esc(txt(f.indexName,'—'))+(f.indexCode?' <small class="mono">'+esc(f.indexCode)+'</small>':'')+'</div></div>'+
     '<div class="kpi"><div class="k">单日累计限额</div><div class="v" style="font-size:12.5px">'+esc(f.limitText||'无明确限额')+'</div></div>'+
     '<div class="kpi"><div class="k">年化跟踪误差</div><div class="v">'+(f.te?esc(f.te):'<span class="flat">—</span>')+'</div></div>'+
     '<div class="kpi"><div class="k">手续费（年综合）</div><div class="v">'+esc(txt(f.feeTotal,'—'))+'</div></div>'+
     '<div class="kpi"><div class="k">近7日收益率</div><div class="v">'+pct(f.r.w1)+'</div></div>'+
     '</div>';
  h+='<div class="detail">';
  if(f.limitText) h+='<div class="limit">'+ic('lock')+'<div>单日累计购买上限 / 申购限制：'+esc(f.limitText)+'</div></div>';
  h+='<div class="kv" style="margin-top:0">'+
     '<div class="i"><div class="k">单位净值 · '+esc(txt(f.navDate,'—'))+'</div><div class="v">'+esc(txt(f.nav))+' <span style="font-size:12px">'+pct(f.dayChg)+'</span></div></div>'+
     '<div class="i"><div class="k">起购金额</div><div class="v">'+esc(txt(f.minBuy,'—'))+' <small style="font-weight:400;color:var(--tx3)">'+(f.currency==='美元'?'美元':'元')+'</small></div></div>'+
     '<div class="i"><div class="k">净资产规模</div><div class="v">'+txt(f.scale,'—')+' <small style="font-weight:400;color:var(--tx3)">亿元</small></div></div>'+
     '<div class="i"><div class="k">规模截止日</div><div class="v mono">'+esc(txt(f.scaleDate,'—'))+'</div></div>'+
     '<div class="i"><div class="k">管理费率</div><div class="v">'+esc(txt(f.mgrFee,'—'))+'</div></div>'+
     '<div class="i"><div class="k">托管费率</div><div class="v">'+esc(txt(f.trustFee,'—'))+'</div></div>'+
     '<div class="i"><div class="k">销售服务费</div><div class="v">'+esc(txt(f.salesFee,'—'))+'</div></div>'+
     '<div class="i"><div class="k">申购费（折后 / 原）</div><div class="v">'+esc(txt(f.buyRate,'—'))+' <small style="font-weight:400;color:var(--tx3)">'+esc(txt(f.rawBuyRate,'—'))+'</small></div></div>'+
     '<div class="i"><div class="k">申购确认 / 到账</div><div class="v" style="font-size:12.5px">'+esc(txt(f.confirmDays,'—'))+' ｜ '+esc(txt(f.redeemDays,'—'))+'</div></div>'+
     '<div class="i"><div class="k">近1年最大回撤</div><div class="v">'+(f.mdd?esc(f.mdd)+'%':'—')+'</div></div>'+
     '<div class="i"><div class="k">近1年夏普比率</div><div class="v">'+esc(txt(f.sharp,'—'))+'</div></div>'+
     '</div>';
  h+='<div class="rets">'+
     [['近1月',f.r.m1],['近3月',f.r.m3],['近6月',f.r.m6],['近1年',f.r.y1],['近3年',f.r.y3],['成立以来',f.r.since]]
       .map(function(x){return '<div class="ret"><div class="k">'+x[0]+'</div><div class="v">'+pct(x[1],1)+'</div></div>'}).join('')+
     '</div>';
  h+='<div class="rets four" style="margin-top:8px">'+
     [['近1周',f.r.w1],['近2年',f.r.y2],['近5年',f.r.y5],['今年以来',f.r.ytd]]
       .map(function(x){return '<div class="ret"><div class="k">'+x[0]+'</div><div class="v">'+pct(x[1])+'</div></div>'}).join('')+
     '</div>';
  h+='<div class="tags" style="margin-top:11px">'+
     '<span class="tag">赎回状态 '+esc(txt(f.shzt,'未知'))+'</span>'+
     '<span class="tag">'+esc(f.currency)+'</span>'+
     '<span class="tag">'+esc(txt(f.company,'—'))+'</span>'+
     '<span class="tag">基金经理 '+esc(txt(f.manager,'—'))+'</span>'+
     '<span class="tag">成立 '+esc(txt(f.estDate,'—'))+'</span>'+
     '<span class="tag">风险 R'+esc(txt(f.risk,'—'))+'</span>'+
     '<span class="tag">年化跟踪误差 '+esc(txt(f.te,'—'))+'</span>'+
     (f.rank&&f.rank.m1?'<span class="tag">近1月同类排名 '+esc(f.rank.m1)+'/'+esc(f.rank.sc)+'</span>':'')+
     '<span class="tag">近1年波动率 '+esc(txt(f.std,'—'))+'%</span>'+
     '<span class="tag">'+esc(f.ftype||'')+'</span>'+
     '<span class="tag">跟踪 '+esc(f.indexName)+'</span></div>';
  if(f.tgtEtf && f.tgtEtf.name) h+='<div class="sec"><h4>目标 ETF <span>target etf</span></h4><div class="tags"><span class="tag mono">'+esc(f.tgtEtf.code)+' '+esc(f.tgtEtf.name)+'</span></div></div>';
  if(f.holdings && f.holdings.length){
    h+='<div class="sec"><h4>前十大持仓 <span>top holdings ｜ '+esc(txt(f.asset.date,'—'))+'</span></h4><table class="hold"><tr><th>代码</th><th>名称</th><th style="text-align:right">占净值比</th><th style="text-align:right">较上期</th></tr>';
    f.holdings.forEach(function(x){ h+='<tr><td class="mono">'+esc(x.code)+'</td><td>'+esc(x.name)+'</td><td class="n">'+(x.pct!=null?x.pct+'%':'—')+'</td><td class="n">'+esc(x.chgType||'')+(x.chg&&x.chg!=='--'?' '+esc(x.chg)+'%':'')+'</td></tr>'; });
    h+='</table></div>';
  } else if(f.fofHoldings && f.fofHoldings.length){
    h+='<div class="sec"><h4>基金中基金（FOF）持仓 <span>fof holdings</span></h4><table class="hold"><tr><th>代码</th><th>名称</th><th style="text-align:right">占净值比</th></tr>';
    f.fofHoldings.forEach(function(x){ h+='<tr><td class="mono">'+esc(x.code)+'</td><td>'+esc(x.name)+'</td><td class="n">'+(x.pct!=null?x.pct+'%':'—')+'</td></tr>'; });
    h+='</table></div>';
  }
  var a=f.asset||{};
  var parts=[['基金',a.fund],['股票',a.stock],['债券',a.bond],['现金',a.cash],['其他',a.other]].filter(function(x){return x[1]&&x[1]!=='--'});
  if(parts.length) h+='<div class="sec"><h4>资产配置 <span>asset allocation</span></h4><div class="tags">'+parts.map(function(x){return '<span class="tag">'+x[0]+' '+esc(x[1])+'%</span>'}).join('')+(a.net?'<span class="tag">资产净值 '+esc(a.net)+' 亿元</span>':'')+'</div></div>';
  if(f.notices && f.notices.length){
    h+='<div class="sec"><h4>最新公告 <span>announcements</span></h4>'+f.notices.slice(0,3).map(function(n){return '<div style="font-size:12.3px;margin:4px 0"><span style="color:var(--tx3)" class="mono">'+esc(n.date)+'</span> '+(n.url?'<a href="'+esc(n.url)+'" target="_blank">'+esc(n.title)+'</a>':esc(n.title))+'</div>'}).join('')+'</div>';
  }
  h+='</div>';
  h+='</article>';
  return h;
}
function toggleCard(el){
  var c=el.closest?el.closest('.card'):null; if(!c) return;
  c.classList.toggle('collapsed');
  var b=c.querySelector('.tgl'); if(b) b.innerHTML=(c.classList.contains('collapsed')?'展开详情':'收起详情')+ic('chev');
}
function setAll(collapsed){
  Array.prototype.forEach.call(document.querySelectorAll('#list .card'),function(c){
    c.classList.toggle('collapsed',collapsed);
    var b=c.querySelector('.tgl'); if(b) b.innerHTML=(collapsed?'展开详情':'收起详情')+ic('chev');
  });
}
function render(){
  var q=document.getElementById('q').value.trim().toLowerCase();
  var fi=document.getElementById('fIdx').value, fs=document.getElementById('fSt').value,
      fc=document.getElementById('fCur').value, ft=document.getElementById('fType').value;
  var sb=document.getElementById('sortBy').value;
  var arr=F.filter(function(f){
    if(fi && (f.catKey||f.indexKey)!==fi) return false;
    if(fc && f.currency!==fc) return false;
    if(ft==='link' && f.name.indexOf('联接')<0) return false;
    if(ft==='direct' && f.name.indexOf('联接')>=0) return false;
    if(fs==='open' && !((f.sgzt||'').indexOf('开放')>=0)) return false;
    if(fs==='limit' && !((f.sgzt||'').indexOf('限')>=0)) return false;
    if(fs==='stop' && !((f.sgzt||'').indexOf('暂停')>=0||(f.sgzt||'').indexOf('封闭')>=0||f.canBuy===false)) return false;
    if(q){ var s=(f.name+f.code+f.company+f.manager+f.indexName+((f.catName)||'')+f.ftype).toLowerCase(); if(s.indexOf(q)<0) return false; }
    return true;
  });
  var key={'scale':function(f){return -(f.scale||0)},'m1':function(f){return -(f.r.m1||-999)},
    'm3':function(f){return -(f.r.m3||-999)},'m6':function(f){return -(f.r.m6||-999)},
    'y1':function(f){return -(f.r.y1||-999)},'y3':function(f){return -(f.r.y3||-999)},
    'since':function(f){return -(f.r.since||-999)},'mgrFee':function(f){return parseFloat(f.mgrFee)||99},
    'code':function(f){return f.code}}[sb];
  if(key) arr.sort(function(a,b){return key(a)>key(b)?1:-1});
  document.getElementById('list').innerHTML = arr.length?arr.map(card).join(''):'<div class="empty">未匹配到基金 ｜ No funds matched</div>';
  document.getElementById('statCnt').textContent=arr.length+' / '+F.length;
}
['q','fIdx','fSt','fCur','fType','sortBy'].forEach(function(id){var el=document.getElementById(id);el.addEventListener('input',render);el.addEventListener('change',render)});
document.getElementById('expAll').addEventListener('click',function(){setAll(false)});
document.getElementById('colAll').addEventListener('click',function(){setAll(true)});
document.getElementById('idxrow').innerHTML = IDX.map(function(x){
  var chg=parseFloat(x.chg), pct=parseFloat(x.chgPct);
  var cls = (isFinite(chg)&&chg<0) ? 'dn' : ((isFinite(chg)&&chg>0) ? 'up' : 'flat');
  var arrow = cls==='up' ? '▲' : (cls==='dn' ? '▼' : '—');
  function nf2(v){ return (v===null||v===undefined||isNaN(v)) ? '—' : Number(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}); }
  function sg(v){ return (v===null||v===undefined||isNaN(v)||v===0) ? '' : (v>0?'+':''); }
  function devp(v){ return (v==null||!isFinite(v)) ? '—' : (v*100).toFixed(4)+'%'; }
  var srcs = (x.srcs||[]).join(' ＋ ');
  var vfTip = (x.ok?'多源口径一致：':'多源存在差异，请留意口径或时间差：')
    + '腾讯↔新浪相对偏差 '+devp(x.xDev)
    + '，CNBC '+(x.cbcDev==null?'未取到':devp(x.cbcDev))
    + (x.nqDev==null?'':'，纳斯达克官方 '+devp(x.nqDev))
    + '，自洽校验（最新价−昨收=涨跌额）偏差 '+devp(x.selfDev)
    + '；本次比对来源：'+(srcs||'—');
  var vf = x.ok
    ? '<span class="vf" title="'+esc(vfTip)+'">✓ '+(x.srcN||2)+' 源校验通过</span>'
    : '<span class="vf warn" title="'+esc(vfTip)+'">⚠ 校验待复核</span>';
  return '<div class="idx">'
    + '<div class="n">'+esc(x.name)+'<em>'+esc(x.sym||'')+'</em></div>'
    + '<div class="v mono">'+nf2(x.price)+'</div>'
    + '<div class="c '+cls+' mono">'+arrow+' '+sg(chg)+nf2(Math.abs(chg))+' <span class="pc">('+sg(pct)+nf2(Math.abs(pct))+'%)</span></div>'
    + '<div class="k2">昨收 '+nf2(x.prev)+' ｜ 今开 '+nf2(x.open)+'<br>最高 '+nf2(x.high)+' ｜ 最低 '+nf2(x.low)+
      ((x.high52&&x.low52)?'<br>52周区间 '+nf2(x.low52)+' – '+nf2(x.high52):'')+'</div>'
    + '<div class="src">'+(x.time?'<span title="行情时间（美东）">美东 '+esc(x.time)+'</span>':'')+vf+'</div>'
    + '</div>';
}).join('');
var cnt={open:0,limit:0,stop:0}, catCnt={};
F.forEach(function(f){ var st=f.sgzt||''; if(st.indexOf('暂停')>=0||st.indexOf('封闭')>=0) cnt.stop++; else if(st.indexOf('限')>=0) cnt.limit++; else cnt.open++;
  var k=f.catKey||f.indexKey; catCnt[k]=(catCnt[k]||0)+1; });
document.getElementById('fIdx').innerHTML = '<option value="">全部跟踪标的</option>'+
  CATS.filter(function(c){return catCnt[c[0]]}).map(function(c){return '<option value="'+c[0]+'">'+c[1]+'</option>'}).join('');
document.getElementById('stats').innerHTML=
 '<div class="stat"><div class="si">'+ic('layers')+'</div><div><div class="k">监控基金总数 · total</div><div class="v">'+F.length+'</div></div></div>'+
 '<div class="stat wide"><div class="si">'+ic('chart')+'</div><div style="flex:1"><div class="k">跟踪标的构成 · universe</div><div class="compose">'+
   (CATS.filter(function(c){return catCnt[c[0]]}).map(function(c){return c[1]+' <b>'+catCnt[c[0]]+'</b>'}).join(' ｜ ')||'—')+'</div></div></div>'+
 '<div class="stat"><div class="si">'+ic('shield')+'</div><div><div class="k">开放申购 · open</div><div class="v dn">'+cnt.open+'</div></div></div>'+
 '<div class="stat"><div class="si">'+ic('lock')+'</div><div><div class="k">限大额申购 · limited</div><div class="v" style="color:var(--warn)">'+cnt.limit+'</div></div></div>'+
 '<div class="stat"><div class="si">'+ic('pause')+'</div><div><div class="k">暂停 / 封闭 · suspended</div><div class="v up">'+cnt.stop+'</div></div></div>'+
 '<div class="stat"><div class="si">'+ic('list')+'</div><div><div class="k">当前筛选结果 · filtered</div><div class="v"><span id="statCnt">'+F.length+'</span></div></div></div>';
(function(){
  var rows = HISTORY.slice().reverse().slice(0,300);
  document.getElementById('histcnt').textContent = HISTORY.length;
  if(!rows.length){ document.getElementById('histwrap').style.display='none'; document.getElementById('histempty').style.display='block'; return; }
  document.getElementById('histbody').innerHTML = rows.map(function(h){
    var isAdd = h.type==='add';
    return '<tr class="'+(isAdd?'addrow':'')+'"><td class="mono">'+esc(h.ts)+'</td>'+
      '<td>'+esc(h.name)+'<div style="color:var(--tx3);font-size:11.5px" class="mono">'+esc(h.code)+'</div></td>'+
      '<td>'+esc(h.desc)+' <span class="bd '+(isAdd?'a':'c')+'">'+(isAdd?'新纳入监控':'状态/限额变动')+'</span></td></tr>';
  }).join('');
})();
if(CHANGES.length){
  document.getElementById('chgbox').innerHTML='<div class="chg"><h2>'+ic('bell')+'本次扫描发现 '+CHANGES.length+' 项申购状态 / 限额变动</h2><ul>'+
    CHANGES.map(function(c){return '<li><b>'+esc(c.name)+'</b>（'+esc(c.code)+'）'+esc(c.desc)+'</li>'}).join('')+'</ul></div>';
}else{
  document.getElementById('chgbox').innerHTML='<div class="chg ok"><h2>'+ic('shield')+'本次扫描未发现申购状态 / 限额变动</h2>'+
   '<div style="font-size:12.5px;color:var(--tx2)">每次扫描都会与上一份快照逐只比对：申购状态（开放申购 / 限大额 / 暂停申购 / 封闭期）或单日累计购买上限发生变化时，会在本区域列出并高亮对应基金卡片。</div></div>';
}
function ntype(t){
  t=t||'';
  var rules=[['暂停申购/定投',/暂停[^，。；]{0,20}(申购|定期定额|定投)/],['恢复申购',/恢复[^，。；]{0,20}(申购|大额|定期定额|定投|转换转入)/],
   ['大额申购限额调整',/(限制|调整|设置|取消|提高|降低)[^，。；]{0,14}(大额申购|申购金额|申购上限|限额|申购业务)/],
   ['分红',/(分红|收益分配|利润分配)/],['基金经理变更',/(基金经理|增聘|解聘|离任|代为履行)/],
   ['费率调整',/(管理费|托管费|销售服务费|费率)/],['合同/招募书更新',/(招募说明书|基金合同|产品资料概要)/],
   ['清算/终止',/(清算|清盘|终止|基金财产清算)/],['风险提示',/(风险提示|溢价|停牌|流动性)/],
   ['开放/上市交易',/(开放日常|上市交易|开放申购|封闭期)/]];
  for(var i=0;i<rules.length;i++){ if(rules[i][1].test(t)) return rules[i][0]; }
  return '产品公告';
}
function nbrief(n){
  if(n.brief) return n.brief;
  var t=(n.title||'').replace(/的?公告$/,'');
  t=t.replace(/^关于/,'').replace(/^[\u4e00-\u9fa5A-Za-z0-9()（）]{2,24}?基金管理(有限公司|股份有限公司)/,'').replace(/^关于/,'');
  t=t.replace(/交易型开放式指数证券投资基金联接基金/g,'ETF联接基金').replace(/交易型开放式指数证券投资基金/g,'ETF')
     .replace(/发起式证券投资基金/g,'发起式基金').replace(/指数型证券投资基金/g,'指数基金')
     .replace(/证券投资基金/g,'基金').replace(/开放式基金/g,'基金');
  return t.length>74?t.slice(0,74)+'…':t;
}
function ntagClass(t){ if(/暂停|清算|终止/.test(t))return 't-stop'; if(/恢复|开放/.test(t))return 't-ok';
  if(/限额|费率|分红|基金经理|更新/.test(t))return 't-warn'; return 't-gray'; }
document.getElementById('newslist').innerHTML = NEWS.map(function(n){
  var tag=n.tag||ntype(n.title), br=n.brief||nbrief(n), facts=n.facts||[];
  return '<li><div class="ntop"><span class="ntag '+ntagClass(tag)+'">'+esc(tag)+'</span><span class="dt mono">'+esc(n.date)+'</span></div>'+
   '<div class="nbr">'+esc(br)+'</div>'+
   '<div class="nf">'+ic('doc')+'<b>'+esc(n.fund)+'</b><span class="mono">'+esc(n.code)+'</span>'+
   (n.url?'<a href="'+esc(n.url)+'" target="_blank">看原文 ↗</a>':'')+'</div>'+
   (facts.length?'<div class="facts">'+facts.map(function(x){return '<span class="fact">'+esc(x)+'</span>'}).join('')+'</div>':'')+
   '</li>';
}).join('');
render();
(function(){
  var root=document.documentElement, btn=document.getElementById('themeBtn');
  function paint(){ var d=root.getAttribute('data-theme')==='dark'; btn.innerHTML='<svg class="ic"><use href="#i-'+(d?'sun':'moon')+'"/></svg>'; }
  try{
    var saved=localStorage.getItem('qdii-theme');
    if(saved) root.setAttribute('data-theme',saved);
    else if(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) root.setAttribute('data-theme','dark');
    else root.setAttribute('data-theme','light');
  }catch(e){ root.setAttribute('data-theme','light'); }
  paint();
  btn.addEventListener('click',function(){
    var n=root.getAttribute('data-theme')==='dark'?'light':'dark';
    root.setAttribute('data-theme',n);
    paint();
    try{ localStorage.setItem('qdii-theme',n); }catch(e){}
  });
})();
</script>
<script id="HIST2" type="application/json">__HIST2__</script>
<script id="NAV" type="application/json">__NAV__</script>
<script>
/* ================================================================
   v3 模块：基金自助对比（折叠 · 筛选 · 业绩走势图）
            历史定投推演（折叠 · 过程动画）
   ================================================================ */

/* ---------- 0. 历史行情序列（推演用，来自 HIST2） ---------- */
var HS = (function(){
  var raw = {};
  try{ raw = JSON.parse(document.getElementById('HIST2').textContent || '{}'); }catch(e){ raw = {}; }
  var m = {}, order = ['ndx','spx','f160213','f050025'];
  (raw.series || []).forEach(function(s){
    var ds = [], cs = [], ts = [];
    String(s.pts || '').split('|').forEach(function(p){
      var a = p.split(',');
      if(a.length < 2 || a[0].length !== 8) return;
      ds.push(a[0]); cs.push(+a[1]);
      ts.push(Date.UTC(+a[0].slice(0,4), +a[0].slice(4,6)-1, +a[0].slice(6,8)));
    });
    if(!ds.length) return;
    m[s.key] = {key:s.key, meta:s, dates:ds, close:cs, ts:ts, n:ds.length};
  });
  m.order = order.filter(function(k){ return m[k]; });
  m.built = raw.built || '';
  m.lowerBound = function(s, year){
    var lo = 0, hi = s.n;
    while(lo < hi){ var mid = (lo + hi) >> 1; if(+s.dates[mid].slice(0,4) < year) lo = mid + 1; else hi = mid; }
    return lo;
  };
  m.yearList = function(s){
    var a = [], y0 = +s.dates[0].slice(0,4), y1 = +s.dates[s.n-1].slice(0,4);
    for(var y = y0; y <= y1; y++) a.push(y);
    return a;
  };
  return m;
})();

/* ---------- 0b. 基金净值序列（对比走势图用，来自 NAV） ---------- */
var NS = (function(){
  var raw = {};
  var el = document.getElementById('NAV');
  if(el){ try{ raw = JSON.parse(el.textContent || '{}'); }catch(e){ raw = {}; } }
  var ep = Date.parse((raw.epoch || '2023-01-01') + 'T00:00:00Z');
  if(isNaN(ep)) ep = Date.UTC(2023, 0, 1);
  var m = {built: raw.built || '', epoch: ep, codes: {}};
  var src = raw.codes || {};
  Object.keys(src).forEach(function(c){
    var o = src[c] || {}, ds = o.d || [], vs = o.v || [], ts = [], vv = [], i;
    for(i = 0; i < ds.length; i++){
      var val = +vs[i];
      if(!(val > 0)) continue;
      ts.push(ep + (+ds[i]) * 86400000); vv.push(val);
    }
    if(ts.length > 1) m.codes[c] = {ts: ts, v: vv, n: ts.length};
  });
  m.n = Object.keys(m.codes).length;
  return m;
})();

function pv(x){
  if(x === null || x === undefined || x === '') return null;
  var n = parseFloat(String(x).replace(/[%％,]/g, ''));
  return isNaN(n) ? null : n;
}
function pstr(v, sign){ return (sign && v > 0 ? '+' : '') + v.toFixed(2) + '%'; }
function ymdOf(t){
  var d = new Date(t);
  return d.getUTCFullYear() + '-' + ('0' + (d.getUTCMonth() + 1)).slice(-2) + '-' + ('0' + d.getUTCDate()).slice(-2);
}
function ymOf(t){ return ymdOf(t).slice(0, 7); }

/* ---------- 0c. 折叠卡片（默认收起，点击展开） ---------- */
(function(){
  var secs = document.querySelectorAll('.foldmod');
  for(var i = 0; i < secs.length; i++){
    (function(sec){
      var hd = sec.querySelector('.foldhd');
      if(!hd) return;
      function toggle(){
        var open = !sec.classList.contains('open');
        sec.classList.toggle('open', open);
        hd.setAttribute('aria-expanded', open ? 'true' : 'false');
      }
      hd.addEventListener('click', toggle);
      hd.addEventListener('keydown', function(e){
        if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); toggle(); }
      });
    })(secs[i]);
  }
})();

/* ================================================================
   模块 1：基金自助对比
   ================================================================ */
var CMP = (function(){
  var sel = [], byCode = {}, lastHit = F.length;
  F.forEach(function(f){ byCode[f.code] = f; });
  try{
    var sv = JSON.parse(localStorage.getItem('qdii-cmp') || '[]');
    if(Object.prototype.toString.call(sv) === '[object Array]') sel = sv.filter(function(c){ return byCode[c]; });
  }catch(e){}

  function el(id){ return document.getElementById(id); }
  function persist(){ try{ localStorage.setItem('qdii-cmp', JSON.stringify(sel)); }catch(e){} }
  function numF(key){ return function(f){ return f[key]; }; }
  function rr(key){ return function(f){ return (f.r || {})[key]; }; }

  var RANGES = [
    {k:'1M', d:30,   t:'近 1 月'},
    {k:'3M', d:91,   t:'近 3 月'},
    {k:'6M', d:182,  t:'近 6 月'},
    {k:'1Y', d:365,  t:'近 1 年'},
    {k:'3Y', d:1095, t:'近 3 年'}
  ];
  var curRange = '1Y';
  var PAL = ['#0071e3','#ff9500','#34c759','#af52de','#ff3b30','#00b8a9','#5856d6','#ff2d55',
             '#8e8e93','#0a84ff','#c9a227','#00c7be','#e07a00','#5ac8fa','#ff6f61','#8b5cf6'];

  var GROUPS = [
    {t:'基本信息', rows:[
      {k:'跟踪标的', raw:1, f:function(f){ return f.indexName || '—'; }},
      {k:'分类', raw:1, f:function(f){ return f.catName || '—'; }},
      {k:'基金类型', raw:1, f:function(f){ return f.ftype || '—'; }},
      {k:'币种', raw:1, f:function(f){ return f.currency || '—'; }},
      {k:'成立日', raw:1, f:function(f){ return f.estDate || '—'; }},
      {k:'基金公司', raw:1, f:function(f){ return f.company || '—'; }},
      {k:'基金经理', raw:1, f:function(f){ return f.manager || '—'; }},
      {k:'目标 ETF', raw:1, f:function(f){ return (f.tgtEtf && f.tgtEtf.code) ? (f.tgtEtf.name + ' ' + f.tgtEtf.code) : '—'; }}
    ]},
    {t:'申购与交易', rows:[
      {k:'申购状态', raw:1, cls:function(f){ return stCls(f.sgzt); }, f:function(f){ return f.sgzt || '—'; }},
      {k:'单日累计限额', raw:1, f:function(f){ return f.limitText || '无明确限额'; }},
      {k:'起购金额', raw:1, f:function(f){ return f.minBuy ? f.minBuy + (f.currency==='美元'?' 美元':' 元') : '—'; }},
      {k:'单日上限', raw:1, f:function(f){ if(f.currency==='美元') return '见详情页'; return (f.maxBuy && f.maxBuy !== '--') ? f.maxBuy + ' 元' : '不限'; }},
      {k:'确认 / 赎回到账', raw:1, f:function(f){ return (f.confirmDays || '—') + ' / ' + (f.redeemDays || '—'); }}
    ]},
    {t:'阶段收益', rows:[
      {k:'近 1 周', sign:1, hi:1, f:rr('w1')},
      {k:'近 1 月', sign:1, hi:1, f:rr('m1')},
      {k:'近 3 月', sign:1, hi:1, f:rr('m3')},
      {k:'近 6 月', sign:1, hi:1, f:rr('m6')},
      {k:'今年以来', sign:1, hi:1, f:rr('ytd')},
      {k:'近 1 年', sign:1, hi:1, f:rr('y1')},
      {k:'近 2 年', sign:1, hi:1, f:rr('y2')},
      {k:'近 3 年', sign:1, hi:1, f:rr('y3')},
      {k:'近 5 年', sign:1, hi:1, f:rr('y5')},
      {k:'成立以来', sign:1, hi:1, f:rr('since')}
    ]},
    {t:'风险与费率', rows:[
      {k:'单位净值', raw:1, f:function(f){ return (f.nav || '—') + (f.navDate ? '（' + f.navDate.slice(5) + '）' : ''); }},
      {k:'日涨跌', sign:1, hi:1, f:numF('dayChg')},
      {k:'年化跟踪误差', pc:1, lo:1, f:numF('te')},
      {k:'近 1 年最大回撤', pc:1, lo:1, f:numF('mdd')},
      {k:'夏普比率', dec:3, hi:1, f:numF('sharp')},
      {k:'年化波动率', pc:1, lo:1, f:numF('std')},
      {k:'管理费', pc:1, lo:1, f:numF('mgrFee')},
      {k:'托管费', pc:1, f:numF('trustFee')},
      {k:'销售服务费', pc:1, f:numF('salesFee')},
      {k:'年综合费率', pc:1, lo:1, f:numF('feeTotal')},
      {k:'风险等级', raw:1, f:function(f){ return f.risk ? (f.risk + ' 级') : '—'; }}
    ]},
    {t:'规模与排名', rows:[
      {k:'净资产规模（亿元）', dec:2, hi:1, f:numF('scale')},
      {k:'规模截止日', raw:1, f:function(f){ return f.scaleDate || '—'; }},
      {k:'份额（亿份）', dec:2, f:numF('shares')},
      {k:'同类排名（近 1 月）', raw:1, f:function(f){ var r = f.rank || {}; return (r.m1 || '—') + (r.sc ? ' / ' + r.sc : ''); }},
      {k:'近期公告（条）', dec:0, f:function(f){ return (f.notices || []).length; }}
    ]}
  ];

  /* ---------- 筛选 + 列表 ---------- */
  function filters(){
    return {
      q: (el('cmpq') && el('cmpq').value || '').trim().toLowerCase(),
      cat: el('cmpCat') ? el('cmpCat').value : '',
      st: el('cmpSt') ? el('cmpSt').value : '',
      cur: el('cmpCur') ? el('cmpCur').value : ''
    };
  }
  function match(f, q){
    if(q.q){
      var blob = (f.name + ' ' + f.code + ' ' + (f.indexName || '') + ' ' + (f.company || '') + ' ' + (f.catName || '')).toLowerCase();
      if(blob.indexOf(q.q) < 0) return false;
    }
    if(q.cat && f.catKey !== q.cat) return false;
    if(q.st && (f.sgzt || '') !== q.st) return false;
    if(q.cur && (f.currency || '') !== q.cur) return false;
    return true;
  }
  function stBadge(f){
    var s = f.sgzt || '未知';
    return '<span class="b ' + stCls(s) + '">' + esc(s) + '</span>';
  }
  function renderBadge(){
    var b = el('cmpBadge');
    if(!b) return;
    b.textContent = sel.length ? ('已选 ' + sel.length + ' 只') : '已选 0 只';
    b.className = 'fst' + (sel.length ? ' on' : '');
  }
  function renderList(){
    var box = el('cmpList');
    if(!box) return;
    var q = filters();
    var list = F.filter(function(f){ return match(f, q); });
    var shown = list.slice(0, 300);
    var h = shown.map(function(f){
      var on = sel.indexOf(f.code) >= 0;
      var y1 = pv((f.r || {}).y1);
      var y1h = (y1 === null) ? '<span class="flat">—</span>'
        : '<span class="' + (y1 > 0 ? 'up' : (y1 < 0 ? 'dn' : 'flat')) + '">近1年 ' + pstr(y1, true) + '</span>';
      return '<div class="pickrow' + (on ? ' on' : '') + '" data-c="' + f.code + '">' +
        '<span class="cb">' + (on ? '✓' : '') + '</span>' +
        '<span class="nm"><b>' + esc(f.name) + '</b>' +
        '<em><span class="mono">' + esc(f.code) + '</span> · ' + esc(f.catName || '—') + ' · ' + esc(f.currency || '') + ' ｜ ' + y1h + '</em></span>' +
        stBadge(f) + '</div>';
    }).join('');
    if(list.length > shown.length) h += '<div class="pickcnt">仅显示前 300 条，请用上方筛选或搜索缩小范围</div>';
    if(!list.length) h = '<div class="pickcnt" style="padding:18px 12px;text-align:center">没有符合条件的基金，试试放宽筛选条件</div>';
    var keepTop = box.scrollTop;
    box.innerHTML = h;
    box.scrollTop = keepTop;
    lastHit = list.length;
    updateCnt();
  }
  function renderPicks(){
    var box = el('cmpPicks');
    if(!box) return;
    if(!sel.length){
      box.innerHTML = '<span style="color:var(--tx3);font-size:12.5px">尚未选择基金 — 从左侧勾选，或用下方快捷按钮批量加入</span>';
      return;
    }
    box.innerHTML = sel.map(function(c){
      var f = byCode[c];
      return '<span class="chipx"><b>' + esc(f.name) + '</b><span class="mono">' + esc(f.code) + '</span>' +
        '<i data-x="' + c + '">×</i></span>';
    }).join('');
  }

  /* ---------- 业绩走势图（v3.1 视觉重做 + 指数默认叠加 + 悬停/拖拽） ---------- */
  function trendPoints(code, from){
    var s = NS.codes[code];
    if(!s || s.n < 2) return null;
    var i0 = -1, k;
    for(k = 0; k < s.n; k++){ if(s.ts[k] >= from){ i0 = k; break; } }
    if(i0 < 0) i0 = 0;
    if(s.n - i0 < 2) return null;
    var base = s.v[i0], pts = [];
    for(k = i0; k < s.n; k++) pts.push([s.ts[k], (s.v[k] - base) / base * 100]);
    return {pts: pts, base: base, from: s.ts[i0], late: s.ts[i0] > from + 6 * 86400000};
  }
  function nearest(pt, t){
    var lo = 0, hi = pt.length - 1;
    if(t <= pt[0][0]) return pt[0];
    if(t >= pt[hi][0]) return pt[hi];
    while(lo < hi){
      var mid = (lo + hi) >> 1;
      if(pt[mid][0] < t) lo = mid + 1; else hi = mid;
    }
    var a = pt[Math.max(0, lo - 1)], b = pt[lo];
    return (Math.abs(a[0] - t) <= Math.abs(b[0] - t)) ? a : b;
  }

  /* 指数叠加：跟踪纳指100 → 叠加纳指100走势；跟踪标普500 → 叠加标普500走势（两类都选则两条都叠加） */
  var OVL_IDX = {
    ndx: {hs:'ndx', name:'纳斯达克100指数', col:'#8e8e93', dash:'6 5'},
    spx: {hs:'spx', name:'标普500指数', col:'#c8a951', dash:'2.5 3'}
  };
  function ovlKeys(list){
    var ks = [];
    (list || []).forEach(function(f){
      var k = f.catKey || f.indexKey;
      if(k === 'ndx100' && ks.indexOf('ndx') < 0) ks.push('ndx');
      if(k === 'sp500' && ks.indexOf('spx') < 0) ks.push('spx');
    });
    return ks;
  }
  function ovlPoints(hsKey, from){
    var s = HS[hsKey];
    if(!s || s.n < 2) return null;
    var i0 = -1, k;
    for(k = 0; k < s.n; k++){ if(s.ts[k] >= from){ i0 = k; break; } }
    if(i0 < 0) i0 = 0;
    if(s.n - i0 < 2) return null;
    var base = s.close[i0], pts = [];
    for(k = i0; k < s.n; k++) pts.push([s.ts[k], (s.close[k] - base) / base * 100]);
    return {pts: pts, base: base, from: s.ts[i0]};
  }
  function smoothPath(pts, X, Y){
    var n = pts.length, d = 'M' + X(pts[0][0]).toFixed(1) + ' ' + Y(pts[0][1]).toFixed(1), i;
    if(n === 2) return d + 'L' + X(pts[1][0]).toFixed(1) + ' ' + Y(pts[1][1]).toFixed(1);
    for(i = 1; i < n - 1; i++){
      var mx = (X(pts[i][0]) + X(pts[i+1][0])) / 2, my = (Y(pts[i][1]) + Y(pts[i+1][1])) / 2;
      d += 'Q' + X(pts[i][0]).toFixed(1) + ' ' + Y(pts[i][1]).toFixed(1) + ' ' + mx.toFixed(1) + ' ' + my.toFixed(1);
    }
    return d + 'L' + X(pts[n-1][0]).toFixed(1) + ' ' + Y(pts[n-1][1]).toFixed(1);
  }

  var tv = {W:1000, H:404, pL:76, pR:32, pT:26, pB:52};
  /* v3.3：按容器宽度选择绘图几何。宽屏维持 1000×404；窄屏（手机）令 viewBox 宽度≈容器宽度，
     使 1 用户单位≈1 CSS 像素——图表不再横向裁切、轴标签尺寸稳定，手指拖动也不与横向滚动冲突。 */
  function pickTrendGeom(host){
    var avail = host ? host.clientWidth : 0;
    if(!avail) avail = tv.W;
    if(avail >= 700){
      return {W:tv.W, H:tv.H, pL:tv.pL, pR:tv.pR, pT:tv.pT, pB:tv.pB, narrow:false,
              fx:13.2, fy:13.2, fl:12.4, xn:5, yt:8, yin:false};
    }
    var W2 = Math.max(300, Math.round(avail));
    var H2 = Math.max(240, Math.round(W2 * 0.62));
    return {W:W2, H:H2, pL:30, pR:12, pT:16, pB:30, narrow:true,
            fx:10.5, fy:10.5, fl:10, xn:4, yt:6, yin:true};
  }

  function drawTrend(){
    var svg = el('tSvg'), legend = el('tLegend'), sub = el('tSub'), tip = el('tTip'), wrap = el('tWrap');
    if(!svg) return;
    var list = sel.map(function(c){ return byCode[c]; }).filter(Boolean);
    var R = RANGES[0], i;
    for(i = 0; i < RANGES.length; i++){ if(RANGES[i].k === curRange) R = RANGES[i]; }
    if(tip) tip.style.display = 'none';

    var endTs = 0;
    list.forEach(function(f){ var s = NS.codes[f.code]; if(s && s.ts[s.n-1] > endTs) endTs = s.ts[s.n-1]; });
    if(!endTs || !NS.n){
      if(sub) sub.textContent = '暂无历史净值数据';
      svg.innerHTML = '';
      if(legend) legend.innerHTML = '<span style="color:var(--tx3)">页面未内嵌净值序列（NAV），请重新运行 scan.py 生成页面。</span>';
      return;
    }
    var from = endTs - R.d * 86400000;
    var series = [];
    list.forEach(function(f, idx){ series.push({f:f, col:PAL[idx % PAL.length], tp:trendPoints(f.code, from), idx:idx}); });
    var ovls = [];
    ovlKeys(list).forEach(function(k){
      var op = ovlPoints(OVL_IDX[k].hs, from);
      if(op) ovls.push({key:k, name:OVL_IDX[k].name, col:OVL_IDX[k].col, dash:OVL_IDX[k].dash, tp:op});
    });

    var lo = Infinity, hi = -Infinity;
    series.forEach(function(s){ if(!s.tp) return; s.tp.pts.forEach(function(p){ if(p[1] < lo) lo = p[1]; if(p[1] > hi) hi = p[1]; }); });
    ovls.forEach(function(o){ o.tp.pts.forEach(function(p){ if(p[1] < lo) lo = p[1]; if(p[1] > hi) hi = p[1]; }); });
    if(!isFinite(lo)){ lo = -1; hi = 1; }
    if(hi - lo < 2.5){ var mid0 = (hi + lo) / 2; lo = mid0 - 1.25; hi = mid0 + 1.25; }
    var padY = (hi - lo) * 0.14; lo -= padY; hi += padY;

    var geo = pickTrendGeom(wrap);
    var W = geo.W, H = geo.H, pL = geo.pL, pR = geo.pR, pT = geo.pT, pB = geo.pB;
    var FX = geo.fx, FY = geo.fy, FL = geo.fl, XN = geo.xn, YT = geo.yt;
    try{
      svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
      if(geo.narrow){ svg.classList.add('fit'); }else{ svg.classList.remove('fit'); }
    }catch(e){}
    function X(t){ return pL + (t - from) / (endTs - from) * (W - pL - pR); }
    function Y(v){ return H - pB - (v - lo) / (hi - lo) * (H - pT - pB); }

    var snote = '区间收益走势（各基金以区间首个净值归一为 0%）';
    if(ovls.length) snote += '，已叠加 ' + ovls.map(function(o){ return o.name; }).join(' 与 ') + '（虚线）';
    if(sub) sub.textContent = snote + ' ｜ 数据截至 ' + ymdOf(endTs) +
      (list.length > 16 ? ' ｜ 当前 ' + list.length + ' 只线条较密，建议精简至 16 只以内更清晰' : '');

    /* ---------- defs：每只基金一条面积渐变 + 柔和投影 ---------- */
    var defs = '';
    series.forEach(function(s, si){
      defs += '<linearGradient id="tg' + si + '" x1="0" y1="0" x2="0" y2="1">' +
        '<stop offset="0%" stop-color="' + s.col + '" stop-opacity=".24"/>' +
        '<stop offset="62%" stop-color="' + s.col + '" stop-opacity=".07"/>' +
        '<stop offset="100%" stop-color="' + s.col + '" stop-opacity="0"/></linearGradient>';
    });
    defs += '<linearGradient id="tBg" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0%" stop-color="rgba(127,127,127,.10)"/>' +
      '<stop offset="100%" stop-color="rgba(127,127,127,.015)"/></linearGradient>';
    defs += '<filter id="tSoft" x="-12%" y="-25%" width="124%" height="150%">' +
      '<feDropShadow dx="0" dy="2.5" stdDeviation="3.2" flood-color="rgba(0,0,0,.20)" flood-opacity=".55"/></filter>';

    var h = '<defs>' + defs + '</defs>';
    /* 绘图底板 */
    h += '<rect x="' + pL + '" y="' + pT + '" width="' + (W - pL - pR) + '" height="' + (H - pT - pB) +
         '" rx="12" fill="url(#tBg)" stroke="var(--line)" stroke-width="1"/>';
    /* 横向网格（8 等分，隔线加密、零轴加重） */
    var yTicks = YT;
    for(i = 0; i <= yTicks; i++){
      var vv = lo + (hi - lo) * i / yTicks, yy = Y(vv);
      var zero = Math.abs(vv) < (hi - lo) / (yTicks * 1.7);
      var major = (i % 2 === 0);
      h += '<line x1="' + (pL + 1) + '" y1="' + yy.toFixed(1) + '" x2="' + (W - pR - 1) + '" y2="' + yy.toFixed(1) +
           '" stroke="' + (zero ? 'var(--tx3)' : 'var(--line2)') + '" stroke-width="' + (zero ? 1.3 : 1) +
           '"' + (zero ? '' : ' stroke-dasharray="' + (major ? '4 4' : '2 5') + '"') +
           ' opacity="' + (zero ? '.6' : (major ? '.7' : '.38')) + '"/>';
      if(major || zero){
        /* v3.3：窄屏把纵轴刻度画进绘图区（起始对齐 + 描边留白），避免窄画布上标签被挤出边界 */
        var lx = geo.yin ? (pL + 6) : (pL - 12);
        var ly = geo.yin ? (yy - 4.6) : (yy + 4.4);
        h += '<text x="' + lx + '" y="' + ly.toFixed(1) + '" text-anchor="' + (geo.yin ? 'start' : 'end') + '" font-size="' + FY + '" ' +
             'fill="' + (zero ? 'var(--tx2)' : 'var(--tx3)') + '" font-weight="' + (zero ? '600' : '400') + '" ' +
             (geo.yin ? 'stroke="var(--card)" stroke-width="2.6" style="font-variant-numeric:tabular-nums;paint-order:stroke"'
                      : 'style="font-variant-numeric:tabular-nums"') + '>' + (vv > 0 ? '+' : '') + vv.toFixed(1) + '%</text>';
      }
    }
    /* 纵向辅助线 + 时间刻度 */
    for(i = 0; i < XN; i++){
      var tt = from + (endTs - from) * i / (XN - 1), xx = X(tt);
      if(i > 0 && i < XN - 1){
        h += '<line x1="' + xx.toFixed(1) + '" y1="' + (pT + 1) + '" x2="' + xx.toFixed(1) + '" y2="' + (H - pB - 1) +
             '" stroke="var(--line2)" stroke-width="1" stroke-dasharray="2 6" opacity=".45"/>';
      }
      h += '<text x="' + xx.toFixed(1) + '" y="' + (H - (geo.yin ? 11 : 17)) + '" text-anchor="' + (i === 0 ? 'start' : (i === XN - 1 ? 'end' : 'middle')) +
           '" font-size="' + FX + '" fill="var(--tx3)" letter-spacing=".02em">' + (R.d <= 100 ? ymdOf(tt).slice(5) : ymOf(tt)) + '</text>';
    }
    h += '<line x1="' + pL + '" y1="' + (H - pB) + '" x2="' + (W - pR) + '" y2="' + (H - pB) + '" stroke="var(--line2)" stroke-width="1"/>';

    var drawable = series.filter(function(s){ return s.tp; });
    var lw = drawable.length > 10 ? 1.8 : (drawable.length > 5 ? 2.2 : 2.7);
    var useShadow = drawable.length <= 8;
    /* 面积渐变（线条较多时仅保留前 8 条，避免糊成一片） */
    drawable.slice(0, 8).forEach(function(s){
      var si = series.indexOf(s), spts = s.tp.pts, dp = smoothPath(spts, X, Y);
      h += '<path d="' + dp + 'L' + X(spts[spts.length-1][0]).toFixed(1) + ' ' + (H - pB) + 'L' + X(spts[0][0]).toFixed(1) + ' ' +
           (H - pB) + 'Z" fill="url(#tg' + si + ')" stroke="none"/>';
    });
    /* 主线：先描一层底色描边做留白隔离，再画彩色实线 */
    drawable.forEach(function(s, k){
      var d = smoothPath(s.tp.pts, X, Y);
      h += '<path d="' + d + '" fill="none" stroke="var(--card)" stroke-width="' + (lw + 2.6).toFixed(1) +
           '" stroke-linejoin="round" stroke-linecap="round" opacity=".5"/>';
      h += '<path class="tline" data-i="' + k + '" d="' + d + '" fill="none" stroke="' + s.col + '" stroke-width="' + lw.toFixed(1) +
           '" stroke-linejoin="round" stroke-linecap="round"' + (useShadow ? ' filter="url(#tSoft)"' : '') + ' opacity=".97"/>';
    });
    /* 末端点：外圈留白 + 实心点；线条数 ≤ 30 时绘制（过多会糊成一道边）；线条数 ≤ 4 时附末端数值 */
    if(drawable.length <= 30) drawable.forEach(function(s){
      var lp = s.tp.pts[s.tp.pts.length - 1], cx = X(lp[0]), cy = Y(lp[1]), last = lp[1];
      h += '<circle cx="' + cx.toFixed(1) + '" cy="' + cy.toFixed(1) + '" r="4.7" fill="var(--card)" stroke="' + s.col + '" stroke-width="1.5" opacity=".9"/>';
      h += '<circle cx="' + cx.toFixed(1) + '" cy="' + cy.toFixed(1) + '" r="2.2" fill="' + s.col + '"/>';
      if(drawable.length <= 4){
        h += '<text x="' + (cx - 9).toFixed(1) + '" y="' + (cy - 9).toFixed(1) + '" text-anchor="end" font-size="' + FL + '" font-weight="620" ' +
             'fill="' + s.col + '" style="font-variant-numeric:tabular-nums">' + pstr(last, true) + '</text>';
      }
    });
    /* 叠加指数：虚线 + 端点圈 */
    ovls.forEach(function(o){
      var d = smoothPath(o.tp.pts, X, Y), lp = o.tp.pts[o.tp.pts.length - 1], cx = X(lp[0]), cy = Y(lp[1]);
      h += '<path d="' + d + '" fill="none" stroke="var(--card)" stroke-width="3.8" stroke-linejoin="round" opacity=".45"/>';
      h += '<path class="tline ovl" d="' + d + '" fill="none" stroke="' + o.col + '" stroke-width="1.9" stroke-dasharray="' + o.dash +
           '" stroke-linejoin="round" stroke-linecap="round" opacity=".95"/>';
      h += '<circle cx="' + cx.toFixed(1) + '" cy="' + cy.toFixed(1) + '" r="4.3" fill="var(--card)" stroke="' + o.col + '" stroke-width="1.5"/>';
    });
    h += '<line id="tCurLine" x1="0" y1="' + pT + '" x2="0" y2="' + (H - pB) + '" stroke="var(--tx3)" stroke-width="1" stroke-dasharray="3 3" opacity="0"/>';
    h += '<g id="tDots"></g>';
    svg.innerHTML = h;
    /* 线条描画动效（错峰入场，结束后清除 dash，避免影响后续交互） */
    var tlP = svg.querySelectorAll('path.tline');
    [].forEach.call(tlP, function(p, k){
      try{
        var L = p.getTotalLength();
        if(!L) return;
        p.style.strokeDashoffset = L;
        p.style.strokeDasharray = L + ' ' + L;
        p.getBoundingClientRect();
        p.style.transition = 'stroke-dashoffset 1180ms cubic-bezier(.22,.61,.36,1) ' + (k * 42) + 'ms';
        p.style.strokeDashoffset = '0';
        p.addEventListener('transitionend', function(){
          p.style.transition = 'none'; p.style.strokeDasharray = 'none'; p.style.strokeDashoffset = '0';
        });
      }catch(e){}
    });

    var leg = '';
    series.forEach(function(s){
      var last = s.tp ? s.tp.pts[s.tp.pts.length - 1][1] : null;
      leg += '<span class="tl' + (s.tp ? '' : ' na') + '" data-i="' + s.idx + '">' +
        '<i style="background:' + s.col + '"></i><b>' + esc(s.f.name) + '</b>' +
        '<span class="mono">' + esc(s.f.code) + '</span>' +
        '<span class="tv ' + (last === null ? 'flat' : (last > 0 ? 'up' : (last < 0 ? 'dn' : 'flat'))) + '">' +
        (last === null ? '暂无净值数据' : pstr(last, true)) + '</span></span>';
    });
    ovls.forEach(function(o){
      var last = o.tp.pts[o.tp.pts.length - 1][1];
      leg += '<span class="tl ovl"><i style="background:repeating-linear-gradient(90deg,' + o.col + ' 0 4px,transparent 4px 7px)"></i>' +
        '<b>' + esc(o.name) + '</b><span class="em">叠加指数</span>' +
        '<span class="tv ' + (last > 0 ? 'up' : (last < 0 ? 'dn' : 'flat')) + '">' + pstr(last, true) + '</span></span>';
    });
    if(legend) legend.innerHTML = leg;

    /* ---- 悬停查看 + 按住拖动 ---- */
    var line = svg.querySelector('#tCurLine'), dots = svg.querySelector('#tDots');
    var dragging = false;
    var legSpan = [];
    if(legend) [].forEach.call(legend.querySelectorAll('.tl'), function(n){ legSpan.push(n.querySelector('.tv')); });
    function legendLast(){
      var v = [];
      series.forEach(function(s){ v.push(s.tp ? s.tp.pts[s.tp.pts.length - 1][1] : null); });
      ovls.forEach(function(o){ v.push(o.tp.pts[o.tp.pts.length - 1][1]); });
      return v;
    }
    function legSet(vals){
      for(var k = 0; k < legSpan.length && k < vals.length; k++){
        var sp = legSpan[k]; if(!sp) continue;
        var v = vals[k];
        sp.textContent = (v === null ? '暂无净值数据' : pstr(v, true));
        sp.className = 'tv ' + (v === null ? 'flat' : (v > 0 ? 'up' : (v < 0 ? 'dn' : 'flat')));
      }
    }
    function tOf(clientX){
      var rect = svg.getBoundingClientRect();
      if(!rect.width) return null;
      var vx = (clientX - rect.left) / rect.width * W;
      var f = Math.min(1, Math.max(0, (vx - pL) / (W - pL - pR)));
      return from + f * (endTs - from);
    }
    function hide(){
      if(line) line.setAttribute('opacity', '0');
      if(dots) dots.innerHTML = '';
      if(tip) tip.style.display = 'none';
      legSet(legendLast());
    }
    function show(ev, t){
      var xx = X(t), rows = [], dts = '', lv = [];
      series.forEach(function(s){
        if(!s.tp){ lv.push(null); return; }
        var p = nearest(s.tp.pts, t);
        if(!dts) dts = ymdOf(p[0]);
        lv.push(p[1]);
        rows.push({n:s.f.name, c:s.col, v:p[1], dash:''});
      });
      ovls.forEach(function(o){
        var p = nearest(o.tp.pts, t);
        lv.push(p[1]);
        rows.push({n:o.name, c:o.col, v:p[1], dash:o.dash});
      });
      legSet(lv);
      rows.sort(function(a, b){ return b.v - a.v; });
      if(line){
        line.setAttribute('x1', xx.toFixed(1)); line.setAttribute('x2', xx.toFixed(1));
        line.setAttribute('opacity', '.85');
      }
      if(dots){
        dots.innerHTML = rows.map(function(r){
          var cy = Y(r.v).toFixed(1);
          return '<circle cx="' + xx.toFixed(1) + '" cy="' + cy + '" r="4.5" fill="var(--card)" stroke="' + r.c + '" stroke-width="1.6" opacity=".95"/>' +
            '<circle cx="' + xx.toFixed(1) + '" cy="' + cy + '" r="2" fill="' + (r.dash ? 'var(--card)' : r.c) + '"' +
            (r.dash ? ' stroke="' + r.c + '" stroke-width="1.2"' : '') + '/>';
        }).join('');
      }
      if(tip){
        var maxn = 11, body = rows.slice(0, maxn).map(function(r){
          return '<div class="tr"><em><i style="display:inline-block;width:9px;height:8px;border-radius:2px;margin-right:6px;vertical-align:middle;' +
            (r.dash ? 'background:transparent;border-top:2px dashed ' + r.c : 'background:' + r.c) + '"></i>' + esc(r.n) + '</em>' +
            '<b class="' + (r.v > 0 ? 'up' : (r.v < 0 ? 'dn' : 'flat')) + '">' + pstr(r.v, true) + '</b></div>';
        }).join('');
        if(rows.length > maxn) body += '<div class="tr" style="color:var(--tx3);font-size:11px"><em>另有 ' + (rows.length - maxn) + ' 条未显示</em><b></b></div>';
        tip.innerHTML = '<div class="tt">' + (dts || ymOf(t)) + '</div>' + body;
        tip.style.display = 'block';
        var wrect = wrap.getBoundingClientRect();
        var left = ev.clientX - wrect.left + 16, top = ev.clientY - wrect.top - 12;
        if(left + tip.offsetWidth > wrect.width - 6) left = Math.max(4, ev.clientX - wrect.left - tip.offsetWidth - 16);
        if(top + tip.offsetHeight > wrect.height - 4) top = Math.max(4, wrect.height - tip.offsetHeight - 4);
        if(top < 4) top = 4;
        tip.style.left = left + 'px';
        tip.style.top = top + 'px';
      }
    }
    /* v3.3：鼠标悬停与手指点按/拖动统一走 pointer 事件；监听只绑一次，重绘只更新数据引用 */
    var HOST = svg.__h || (svg.__h = {});
    HOST.show = show; HOST.hide = hide; HOST.tOf = tOf;
    if(!svg.__bound){
      svg.__bound = true;
      var dragging = false;
      function isTouchEv(ev){ return ev.pointerType === 'touch' || ev.pointerType === 'pen'; }
      svg.addEventListener('pointermove', function(ev){
        var o = svg.__h; if(!o) return;
        if(dragging) ev.preventDefault();
        var t = o.tOf(ev.clientX);
        if(t === null) return;
        o.show(ev, t);
      });
      svg.addEventListener('pointerdown', function(ev){
        var o = svg.__h; if(!o) return;
        dragging = true;
        try{ svg.setPointerCapture(ev.pointerId); }catch(e){}
        var t = o.tOf(ev.clientX);
        if(t !== null) o.show(ev, t);
      });
      svg.addEventListener('pointerup', function(ev){
        dragging = false;
        if(isTouchEv(ev)){ var o = svg.__h; if(o) o.hide(); }   /* 手指抬起即复位 */
      });
      svg.addEventListener('pointercancel', function(){ dragging = false; var o = svg.__h; if(o) o.hide(); });
      svg.addEventListener('pointerleave', function(){ if(!dragging){ var o = svg.__h; if(o) o.hide(); } });
    }
    if(wrap) wrap.style.cursor = 'crosshair';
  }

  function renderTrend(){
    var host = el('cmpTrend');
    if(!host) return;
    drawTrend();
  }
  /* v3.3：供外层在视口宽度变化（含横竖屏切换）时按新宽度重排走势图 */
  window.__qdiiRenderTrend = renderTrend;
  function renderTable(){
    var out = el('cmpOut');
    if(!out) return;
    var list = sel.map(function(c){ return byCode[c]; });
    if(!list.length){
      out.innerHTML = '<div class="empty">请先在左侧勾选要对比的基金（可按跟踪标的、申购状态、币种筛选）。</div>';
      return;
    }
    var rangeBtns = RANGES.map(function(r){
      return '<button class="rtab' + (r.k === curRange ? ' on' : '') + '" type="button" data-r="' + r.k + '">' + r.t + '</button>';
    }).join('');
    var h = '<div class="cmptrend" id="cmpTrend">' +
      '<div class="thead"><h3>业绩走势图</h3><span class="sub" id="tSub"></span><span class="thint">悬停 / 拖动查看任意日期</span><div class="rtabs">' + rangeBtns + '</div></div>' +
      '<div class="trendwrap" id="tWrap"><svg id="tSvg" viewBox="0 0 1000 404" preserveAspectRatio="none" role="img"></svg>' +
      '<div class="ttip" id="tTip"></div></div>' +
      '<div class="tlegend" id="tLegend"></div>' +
      '<p class="trendnote">走势为各基金区间累计净值涨跌幅（区间首日归一为 0%），鼠标移入或按住拖动可查看任意日期的横向对比；所选基金若跟踪纳斯达克100 / 标普500，图中自动叠加对应指数走势（虚线，价格指数口径、未含汇率与费率）；基金区间起点晚于所选区间的（新基金）从实际首个净值日开始。数据来源：天天基金累计净值序列与新浪财经指数行情，仅供横向参考。</p>' +
      '</div>';

    h += '<div class="cmpwrap"><table class="cmptable"><thead><tr><th class="f">对比项</th>' +
      list.map(function(f){
        return '<th>' + esc(f.name) + '<span class="sub mono">' + esc(f.code) + ' ｜ ' + esc(f.catName || '') + ' ｜ ' + esc(f.sgzt || '') + '</span></th>';
      }).join('') + '</tr></thead><tbody>';
    GROUPS.forEach(function(g){
      h += '<tr class="grp"><td colspan="' + (list.length + 1) + '">' + esc(g.t) + '</td></tr>';
      g.rows.forEach(function(row){
        var nums = list.map(function(f){ return row.raw ? null : pv(row.f(f)); });
        var valid = nums.filter(function(x){ return x !== null; });
        var mx = null, mn = null;
        valid.forEach(function(x){ if(mx === null || x > mx) mx = x; if(mn === null || x < mn) mn = x; });
        var canMark = (row.hi || row.lo) && valid.length > 1 && mx !== mn;
        h += '<tr><td class="f">' + esc(row.k) + '</td>';
        list.forEach(function(f, i){
          var n = nums[i], best = false, worst = false;
          if(canMark && n !== null){
            if(row.hi){ best = (n === mx); worst = (n === mn); }
            else { best = (n === mn); worst = (n === mx); }
          }
          h += '<td>' + cellFor(row, f, best, worst) + '</td>';
        });
        h += '</tr>';
      });
    });
    h += '</tbody></table></div>' +
      '<p class="capnote">共对比 <b>' + list.length + '</b> 只基金。绿色为该行最优、红色为该行最差（仅在同项均可比且数值不相同时标注，费率 / 回撤 / 跟踪误差 / 波动率为「越小越优」）。' +
      '阶段收益为复权口径；年化跟踪误差与最大回撤取自天天基金披露值，缺失显示「—」。数据为扫描快照值，仅供横向参考，不构成投资建议。</p>';
    out.innerHTML = h;

    var rt = el('cmpTrend');
    if(rt){
      rt.addEventListener('click', function(e){
        var b = e.target.closest ? e.target.closest('.rtab') : null;
        if(!b) return;
        curRange = b.getAttribute('data-r');
        var bs = rt.querySelectorAll('.rtab');
        for(var i = 0; i < bs.length; i++) bs[i].classList.toggle('on', bs[i] === b);
        drawTrend();
      });
    }
    renderTrend();
  }

  function cellFor(row, f, best, worst){
    var raw = row.f(f), cls = [], out;
    if(row.cls) cls.push(row.cls(f));
    if(best) cls.push('best');
    if(worst) cls.push('worst');
    if(row.raw){
      if(raw === null || raw === undefined || raw === ''){ out = '—'; cls.push('flat'); }
      else out = esc(raw);
    }else{
      var n = pv(raw);
      if(n === null){ out = '—'; cls.push('flat'); }
      else if(row.sign){ out = (n > 0 ? '+' : '') + n.toFixed(2) + '%'; cls.push(n > 0 ? 'up' : (n < 0 ? 'dn' : 'flat')); }
      else if(row.pc){ out = n.toFixed(2) + '%'; }
      else out = n.toFixed(row.dec != null ? row.dec : 0);
    }
    return '<span class="' + cls.join(' ') + '">' + out + '</span>';
  }

  function updateCnt(){
    var cnt = el('cmpCnt');
    if(cnt) cnt.textContent = '共 ' + F.length + ' 只基金 ｜ 已选 ' + sel.length + ' 只' +
      (lastHit !== F.length ? ' ｜ 命中 ' + lastHit + ' 只' : '');
  }
  function updateRows(){
    var box = el('cmpList');
    if(!box) return;
    var rows = box.querySelectorAll('.pickrow');
    for(var i = 0; i < rows.length; i++){
      var on = sel.indexOf(rows[i].getAttribute('data-c')) >= 0;
      rows[i].classList.toggle('on', on);
      var cb = rows[i].querySelector('.cb');
      if(cb) cb.textContent = on ? '✓' : '';
    }
  }
  /* 勾选态变化只同步现有行，不重建列表：保留滚动位置、避免闪烁 */
  function refresh(){ persist(); updateRows(); updateCnt(); renderPicks(); renderBadge(); }
  function afterSel(){
    refresh();
    if(el('cmpOut') && el('cmpOut').innerHTML) renderTable();
  }
  function add(codes){
    codes.forEach(function(c){ if(byCode[c] && sel.indexOf(c) < 0) sel.push(c); });
    afterSel();
  }
  function toggle(c){
    var i = sel.indexOf(c);
    if(i >= 0) sel.splice(i, 1); else sel.push(c);
    afterSel();
  }

  /* ---------- 交互 ---------- */
  var catSel = el('cmpCat');
  if(catSel){
    catSel.innerHTML = '<option value="">全部跟踪标的</option>' +
      CATS.map(function(c){ return '<option value="' + c[0] + '">' + esc(c[1]) + '</option>'; }).join('');
    catSel.addEventListener('change', renderList);
  }
  if(el('cmpq')) el('cmpq').addEventListener('input', renderList);
  if(el('cmpSt')) el('cmpSt').addEventListener('change', renderList);
  if(el('cmpCur')) el('cmpCur').addEventListener('change', renderList);
  if(el('cmpList')) el('cmpList').addEventListener('click', function(e){
    var r = e.target.closest ? e.target.closest('.pickrow') : null;
    if(!r) return;
    toggle(r.getAttribute('data-c'));
  });
  if(el('cmpPicks')) el('cmpPicks').addEventListener('click', function(e){
    var x = e.target.getAttribute && e.target.getAttribute('data-x');
    if(!x) return;
    var i = sel.indexOf(x); if(i >= 0) sel.splice(i, 1);
    afterSel();
  });
  if(el('cmpGo')) el('cmpGo').addEventListener('click', function(){
    renderTable();
    try{ el('cmpOut').scrollIntoView({behavior:'smooth', block:'nearest'}); }catch(e){}
  });
  if(el('cmpClear')) el('cmpClear').addEventListener('click', function(){
    sel = []; refresh(); el('cmpOut').innerHTML = '';
  });
  if(el('cmpHotNdx')) el('cmpHotNdx').addEventListener('click', function(){
    add(F.filter(function(f){ return f.catKey === 'ndx100'; }).map(function(f){ return f.code; }));
  });
  if(el('cmpHotSp')) el('cmpHotSp').addEventListener('click', function(){
    add(F.filter(function(f){ return f.catKey === 'sp500' || f.catKey === 'sp500_ew'; }).map(function(f){ return f.code; }));
  });
  if(el('cmpTop5')) el('cmpTop5').addEventListener('click', function(){
    var top = F.slice().sort(function(a, b){
      var x = pv((a.r || {}).y1), y = pv((b.r || {}).y1);
      if(x === null) x = -1e9;
      if(y === null) y = -1e9;
      return y - x;
    }).slice(0, 5).map(function(f){ return f.code; });
    sel = []; add(top);
  });

  renderList(); renderPicks(); renderBadge();
  return {sel:function(){ return sel.slice(); }};
})();

/* ================================================================
   模块 2：历史定投推演（含过程动画）
   ================================================================ */
var SIM = (function(){
  function g(id){ return document.getElementById(id); }
  var tgtEl = g('simTarget'), yearEl = g('simYear'), modeEl = g('simMode'), amtEl = g('simAmt'),
      dayEl = g('simDay'), dayFld = g('simDayFld'), unitEl = g('simUnit'), rangeEl = g('simRange'), out = g('simOut');
  var DEF_AMT = {dca:1000, lump:100000};
  var anim = null;

  function fd(d){ return d.slice(0,4) + '-' + d.slice(4,6) + '-' + d.slice(6,8); }
  function fm(n){
    var neg = n < 0, s = Math.abs(n).toFixed(2), a = s.split('.');
    var i = a[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return (neg ? '-' : '') + '¥' + i + '.' + a[1];
  }
  function fp(n, sign){ return (sign && n > 0 ? '+' : '') + n.toFixed(2) + '%'; }
  function signCls(n, rev){ return n > 0 ? (rev ? 'dn' : 'up') : (n < 0 ? (rev ? 'up' : 'dn') : 'flat'); }

  if(!HS.order.length){
    out.innerHTML = '<div class="empty">未找到历史行情数据（页面缺少 HIST2 数据块），请重新运行 scan.py 生成页面。</div>';
    return {run:function(){}};
  }
  tgtEl.innerHTML = HS.order.map(function(k){
    var s = HS[k];
    var kindTx = s.meta.kind === 'index' ? '价格指数' : '场外基金·累计净值';
    return '<option value="' + k + '">' + esc(s.meta.name) + '（' + kindTx + '）</option>';
  }).join('');

  function fillYears(){
    var s = HS[tgtEl.value], ys = HS.yearList(s), prev = +yearEl.value;
    yearEl.innerHTML = ys.map(function(y){ return '<option value="' + y + '">' + y + ' 年</option>'; }).join('');
    yearEl.value = (prev && ys.indexOf(prev) >= 0) ? prev : ys[0];
    rangeEl.textContent = '数据区间 ' + fd(s.dates[0]) + ' → ' + fd(s.dates[s.n-1]) + ' ｜ 共 ' + s.n + ' 个交易日';
  }
  tgtEl.addEventListener('change', fillYears);
  fillYears();

  /* ---------- 投入计划 ---------- */
  function buildPlan(s, startIdx, mode, day, amt){
    var plan = [], i;
    if(mode === 'lump'){ plan.push({i:startIdx, amt:amt}); return plan; }
    var months = {}, order = [];
    for(i = startIdx; i < s.n; i++){
      var ym = s.dates[i].slice(0,6);
      if(!months[ym]){ months[ym] = []; order.push(ym); }
      months[ym].push(i);
    }
    order.forEach(function(ym){
      var arr = months[ym], pick = arr[0];
      if(day === 'mid'){
        var cand = arr.filter(function(k){ return +s.dates[k].slice(6,8) >= 15; });
        pick = cand.length ? cand[0] : arr[arr.length-1];
      }else if(day === 'last'){ pick = arr[arr.length-1]; }
      plan.push({i:pick, amt:amt});
    });
    return plan;
  }

  /* ---------- 年化（内部收益率，二分法） ---------- */
  function xirr(plan, s, finalVal, endIdx){
    var t0 = s.ts[plan.length ? plan[0].i : endIdx];
    var cf = plan.map(function(p){ return {t:(s.ts[p.i] - t0) / 86400000, a:-p.amt}; });
    cf.push({t:(s.ts[endIdx] - t0) / 86400000, a:finalVal});
    var zero = cf.filter(function(c){ return c.t === 0; }).length;
    if(zero === cf.length) return null;
    function npv(r){
      var sum = 0;
      for(var k = 0; k < cf.length; k++){ sum += cf[k].a / Math.pow(1 + r, cf[k].t / 365); }
      return sum;
    }
    var lo = -0.9999, hi = 5, flo = npv(lo), fhi = npv(hi);
    if(flo * fhi > 0){
      hi = 50; fhi = npv(hi);
      if(flo * fhi > 0) return null;
    }
    for(var k = 0; k < 200; k++){
      var mid = (lo + hi) / 2;
      if(npv(mid) * flo <= 0){ hi = mid; }else{ lo = mid; flo = npv(lo); }
    }
    return (lo + hi) / 2;
  }

  /* ---------- 图表（v3.1 风格重做 + 逐段绘制动画 + 悬停/拖动查看） ---------- */
  /* 图表渲染采样点：折线绘制与动画取值共用，保证圆点与已画出的曲线完全同源 */
  function sampleIdx(startIdx, n){
    var step = Math.max(1, Math.floor((n - startIdx) / 360));
    var idxs = [];
    for(var i = startIdx; i < n; i += step) idxs.push(i);
    if(idxs[idxs.length-1] !== n-1) idxs.push(n-1);
    return idxs;
  }
  /* v3.3：推演图几何。宽屏维持 1000×320；窄屏（手机）令 viewBox 宽度≈容器宽度，
     文字按真实像素渲染；按容器/视口宽度缓存，保证 chart()、run()、动画取值三处完全同源。 */
  var GEO = null;
  function simGeom(){
    var avail = out ? out.clientWidth : 0;
    if(!avail) avail = 1000;
    var key = avail + 'x' + Math.round(window.innerWidth);
    if(GEO && GEO.key === key) return GEO;
    var g;
    if(avail >= 760){
      g = {W:1000, H:320, pL:84, pR:24, pT:20, pB:40, narrow:false, fx:14.5, fy:14.5};
    }else{
      var W2 = Math.max(300, Math.round(avail) - 30);
      var H2 = Math.max(210, Math.round(W2 * 0.68));
      g = {W:W2, H:H2, pL:52, pR:12, pT:14, pB:30, narrow:true, fx:10.5, fy:10};
    }
    g.key = key; GEO = g;
    return g;
  }
  function chart(s, startIdx, n, valArr, invArr){
    var gg = simGeom();
    var W = gg.W, H = gg.H, pL = gg.pL, pR = gg.pR, pT = gg.pT, pB = gg.pB;
    var idxs = sampleIdx(startIdx, n);
    var maxV = 0;
    idxs.forEach(function(k){ if(valArr[k] > maxV) maxV = valArr[k]; if(invArr[k] > maxV) maxV = invArr[k]; });
    maxV = maxV > 0 ? maxV * 1.06 : 1;
    var span = (n - 1 - startIdx) || 1;
    function X(k){ return pL + (k - startIdx) / span * (W - pL - pR); }
    function Y(v){ return H - pB - v / maxV * (H - pT - pB); }
    var pv1 = '', pi1 = '';
    idxs.forEach(function(k, j){
      pv1 += (j ? 'L' : 'M') + X(k).toFixed(1) + ' ' + Y(valArr[k]).toFixed(1);
      pi1 += (j ? 'L' : 'M') + X(k).toFixed(1) + ' ' + Y(invArr[k]).toFixed(1);
    });
    var area = pv1 + 'L' + X(n-1).toFixed(1) + ' ' + Y(0).toFixed(1) + 'L' + X(startIdx).toFixed(1) + ' ' + Y(0).toFixed(1) + 'Z';
    var h = '<svg id="simSvg"' + (gg.narrow ? ' class="fit"' : '') + ' viewBox="0 0 ' + W + ' ' + H + '" role="img">';
    h += '<defs><linearGradient id="simGrad" x1="0" y1="0" x2="0" y2="1">' +
         '<stop offset="0%" stop-color="var(--pri)" stop-opacity=".26"/>' +
         '<stop offset="100%" stop-color="var(--pri)" stop-opacity="0"/></linearGradient>' +
         '<clipPath id="simClip"><rect id="simClipRect" x="0" y="0" width="0" height="' + H + '"/></clipPath></defs>';
    [0, .25, .5, .75, 1].forEach(function(f){
      var y = Y(maxV * f);
      h += '<line x1="' + pL + '" y1="' + y.toFixed(1) + '" x2="' + (W-pR) + '" y2="' + y.toFixed(1) + '" stroke="var(--line2)" stroke-width="1"' +
           (f === 0 ? ' stroke-dasharray="5 5"' : '') + '/>';
      h += '<text x="' + (pL-9) + '" y="' + (y+4).toFixed(1) + '" text-anchor="end" font-size="' + gg.fy + '" fill="var(--tx3)">' +
           (maxV * f >= 10000 ? (maxV * f / 10000).toFixed(1) + ' 万' : Math.round(maxV * f)) + '</text>';
    });
    h += '<line x1="' + pL + '" y1="' + (H-pB) + '" x2="' + (W-pR) + '" y2="' + (H-pB) + '" stroke="var(--line2)" stroke-width="1"/>';
    h += '<g clip-path="url(#simClip)">';
    h += '<path id="simArea" d="' + area + '" fill="url(#simGrad)"/>';
    h += '<path id="simInv" d="' + pi1 + '" fill="none" stroke="var(--tx3)" stroke-width="1.7" stroke-dasharray="6 5" opacity=".85" stroke-linejoin="round"/>';
    h += '<path id="simVal" d="' + pv1 + '" fill="none" stroke="var(--pri)" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>';
    h += '</g>';
    h += '<line id="simCurLine" x1="0" y1="' + pT + '" x2="0" y2="' + (H-pB) + '" stroke="var(--pri)" stroke-width="1" stroke-dasharray="3 3" opacity="0"/>';
    h += '<circle id="simCurDot" cx="0" cy="0" r="4.6" fill="var(--pri)" stroke="var(--card)" stroke-width="1.8" opacity="0"/>';
    [['start', startIdx], ['mid', Math.floor((startIdx + n - 1) / 2)], ['end', n-1]].forEach(function(x, j){
      var k = x[1];
      h += '<text x="' + X(k).toFixed(1) + '" y="' + (H - (gg.narrow ? 10 : 15)) + '" text-anchor="' + (j === 0 ? 'start' : (j === 2 ? 'end' : 'middle')) +
           '" font-size="' + gg.fx + '" fill="var(--tx3)">' + s.dates[k].slice(0,4) + '-' + s.dates[k].slice(4,6) + '</text>';
    });
    h += '</svg>';
    return '<div class="chartbox" id="simChartBox">' +
      '<div class="chead2"><div class="live" id="simLive"></div>' +
      '<div class="cbtns"><span class="chint">动画结束后可悬停 / 拖动查看任意日期</span>' +
      '<button type="button" class="skip" id="simSkip">跳过动画</button>' +
      '<button type="button" id="simReplay">重播动画</button></div></div>' +
      '<div class="simwrap" id="simWrap">' + h + '<div class="ttip" id="simTip"></div></div>' +
      '<div class="lg"><span><i style="background:var(--pri)"></i>账户市值</span>' +
      '<span><i style="background:var(--tx3)"></i>累计投入</span>' +
      '<span>横轴为时间，纵轴为金额（万元自动换算）</span></div></div>';
  }

  /* ---------- 过程动画（v3.1：随进度逐段绘制 + 悬停/拖动查看 + 更舒缓的节奏） ---------- */
  function setV(id, txt, cls){
    var e = g(id);
    if(!e) return;
    e.innerHTML = txt;
    if(cls !== undefined && cls !== null) e.className = e.className.replace(/\b(up|dn|flat|counting|ph)\b/g, '').replace(/\s+/g, ' ').trim() + ' ' + cls;
  }
  function setS(id, txt){ var e = g(id); if(e) e.textContent = txt; }

  function buildAnimator(st){
    var box = g('simChartBox');
    if(!box) return null;
    var valPath = g('simVal'), invPath = g('simInv'), line = g('simCurLine'), dot = g('simCurDot'),
        area = g('simArea'), live = g('simLive'), clip = g('simClipRect'), tip = g('simTip'), wrap = g('simWrap');
    if(!valPath || !invPath) return null;
    var s = st.s, startIdx = st.startIdx, n = st.n;
    var ggA = GEO || simGeom();
    var W = ggA.W, H = ggA.H, pL = ggA.pL, pR = ggA.pR;
    var raf = null, elapsed = 0, last = 0, isDone = false, curE = 0, durMs = 4200, reduceMotion = false;

    function paint(k, isFinal){
      var inv = st.invArr[k], val = st.valArr[k], p = val - inv;
      var ret = inv > 0 ? p / inv * 100 : 0;
      var dms = s.ts[k] - s.ts[startIdx], days = dms / 86400000;
      var planN = 0;
      while(planN < st.plan.length && st.plan[planN].i <= k) planN++;
      var ht = days >= 365
        ? (Math.floor(days / 365) + ' 年 ' + Math.round((days % 365) / 30.44) + ' 个月')
        : (Math.max(1, Math.round(days)) + ' 天');
      setV('k-hold', ht, isFinal ? '' : 'counting');
      setS('k-hold-s', fd(s.dates[startIdx]) + ' 起，已持有 ' + Math.round(days) + ' 天');
      setV('k-inv', fm(inv), isFinal ? '' : 'counting');
      setS('k-inv-s', '每笔 ' + fm(st.plan[0].amt) + ' × ' + planN + ' 笔');
      setV('k-pnl', (p >= 0 ? '+' : '') + fm(p), signCls(p));
      setV('k-final', fm(val), '');
      setV('k-ret', fp(ret, true), signCls(ret));
      setV('k-ann', isFinal ? st.annTx : '…', isFinal ? (st.annCls || '') : 'ph');
      setV('k-mdd', '-' + (st.ddP[k] * 100).toFixed(2) + '%', 'dn');
      setS('k-mdd-s', '标的自高点最大跌幅（截至 ' + fd(s.dates[k]) + '：' + fd(s.dates[st.ddPeakAt[k]]) + ' → ' + fd(s.dates[st.ddAt[k]]) + '）');
      var lv = st.lsV[k];
      setV('k-loss', fm(lv), lv < 0 ? 'dn' : '');
      setS('k-loss-s', lv < 0 ? ('最低于 ' + fd(s.dates[st.lsAt[k]]) + '，即账户市值低于累计投入的极值') : '至此刻账户市值未低于累计投入');
      var bd = st.bkD[k];
      setV('k-bank', bd > 0 ? (Math.round(bd / 86400000) + ' 天') : '0 天', '');
      setS('k-bank-s', bd > 0 ? ('约 ' + (bd / 86400000 / 365).toFixed(1) + ' 年 ｜ ' + fd(s.dates[st.bkS[k]]) + ' → ' + fd(s.dates[st.bkE[k]])) : '至此刻未出现账户浮亏');
      if(live){
        live.innerHTML = '<b>' + fd(s.dates[k]) + '</b>' +
          '<span>投入 <b>' + fm(inv) + '</b></span>' +
          '<span>市值 <b>' + fm(val) + '</b></span>' +
          '<span class="' + signCls(p) + '">盈亏 ' + (p >= 0 ? '+' : '') + fm(p) + '（' + fp(ret, true) + '）</span>';
      }
    }

    /* 曲线取值：在“实际画出的折线顶点”（渲染采样点）之间线性插值。
       X 轴对索引是仿射映射，故该取值对应的 Y 必然精确落在已画出的折线上，
       避免用原始逐日数据插值时在波动段与折线产生肉眼可见的偏差。 */
    function valAt(kf){
      var ds = st.dsIdx;
      if(kf <= startIdx) return st.valArr[startIdx];
      if(kf >= n - 1) return st.valArr[n - 1];
      if(!ds || ds.length < 2){
        var k0 = Math.floor(kf), k1 = Math.min(n - 1, k0 + 1);
        if(k1 <= k0) return st.valArr[k0];
        return st.valArr[k0] + (st.valArr[k1] - st.valArr[k0]) * (kf - k0);
      }
      var lo = 0, hi = ds.length - 1;
      while(hi - lo > 1){ var mid = (lo + hi) >> 1; if(ds[mid] <= kf) lo = mid; else hi = mid; }
      var a = ds[lo], b = ds[hi];
      if(b === a) return st.valArr[a];
      return st.valArr[a] + (st.valArr[b] - st.valArr[a]) * (kf - a) / (b - a);
    }
    /* 逐段绘制：裁剪窗口与圆点共用同一横坐标（连续插值），走到哪画到哪、点线严格同步 */
    function render(e, isFinal){
      var ef = Math.max(0, Math.min(1, e));
      var kf = isFinal ? (n - 1) : (startIdx + ef * (n - 1 - startIdx));
      var k = Math.round(kf);
      if(k < startIdx) k = startIdx;
      if(k > n - 1) k = n - 1;
      curE = ef;
      var x = isFinal ? st.X(n - 1) : st.X(kf);
      var y = isFinal ? st.Y(st.valArr[n - 1]) : st.Y(valAt(kf));
      if(clip) clip.setAttribute('width', Math.max(0, x).toFixed(2));
      if(area) area.style.opacity = (0.26 + 0.74 * Math.min(1, ef * 1.6)).toFixed(3);
      if(line){
        line.setAttribute('x1', x.toFixed(2)); line.setAttribute('x2', x.toFixed(2));
        line.setAttribute('opacity', '0');   /* 动画期间不显示游标线，结束后由悬停控制 */
      }
      if(dot){
        dot.setAttribute('cx', x.toFixed(2)); dot.setAttribute('cy', y.toFixed(2));
        dot.setAttribute('opacity', '1');
      }
      paint(k, isFinal);
    }

    /* ---- 悬停 / 拖动查看任意日期 ---- */
    function hoverK(ev){
      var svg = wrap ? wrap.querySelector('svg') : null;
      if(!svg) return null;
      var rect = svg.getBoundingClientRect();
      if(!rect.width) return null;
      var vx = (ev.clientX - rect.left) / rect.width * W;
      var f = Math.max(0, Math.min(1, (vx - pL) / (W - pL - pR)));
      var k = Math.round(startIdx + f * (n - 1 - startIdx));
      return Math.max(startIdx, Math.min(n - 1, k));
    }
    function showHover(ev){
      var k = hoverK(ev);
      if(k === null) return;
      var val = st.valArr[k], inv = st.invArr[k], p = val - inv, ret = inv > 0 ? p / inv * 100 : 0;
      var x = st.X(k), y = st.Y(valAt(k));
      if(line){
        line.setAttribute('x1', x.toFixed(1)); line.setAttribute('x2', x.toFixed(1));
        line.setAttribute('opacity', '.8');
      }
      if(dot){
        dot.setAttribute('cx', x.toFixed(1)); dot.setAttribute('cy', y.toFixed(1));
        dot.setAttribute('opacity', '1');
      }
      paint(k, true);
      if(!tip) return;
      var days = Math.round((s.ts[k] - s.ts[startIdx]) / 86400000);
      tip.innerHTML = '<div class="tt">' + fd(s.dates[k]) + '</div>' +
        '<div class="tr"><em>账户市值</em><b>' + fm(val) + '</b></div>' +
        '<div class="tr"><em>累计投入</em><b>' + fm(inv) + '</b></div>' +
        '<div class="tr"><em>浮动盈亏</em><b class="' + signCls(p) + '">' + (p >= 0 ? '+' : '') + fm(p) + '</b></div>' +
        '<div class="tr"><em>收益率</em><b class="' + signCls(ret) + '">' + fp(ret, true) + '</b></div>' +
        '<div class="tr"><em>已持有</em><b>' + days + ' 天</b></div>';
      tip.style.display = 'block';
      var wr = wrap.getBoundingClientRect();
      var isT = ev.pointerType === 'touch' || ev.pointerType === 'pen';
      var left = ev.clientX - wr.left + (isT ? -tip.offsetWidth / 2 : 16);
      var top = ev.clientY - wr.top + (isT ? -tip.offsetHeight - 18 : -12);
      if(left + tip.offsetWidth > wr.width - 6) left = Math.max(4, ev.clientX - wr.left - tip.offsetWidth - 16);
      if(top + tip.offsetHeight > wr.height - 4) top = Math.max(4, wr.height - tip.offsetHeight - 4);
      if(top < 4) top = 4;
      tip.style.left = left + 'px';
      tip.style.top = top + 'px';
    }
    function stopRaf(){ if(raf) cancelAnimationFrame(raf); raf = null; }
    function resume(){
      if(isDone || reduceMotion || raf) return;
      last = 0;
      raf = requestAnimationFrame(frame);
    }
    function leaveHover(){
      if(!isDone) return;          /* 动画未结束：保持当前帧，不接受任何交互 */
      if(tip) tip.style.display = 'none';
      render(1, true);
    }

    function frame(ts){
      if(isDone) return;
      if(!last) last = ts;
      var dt = ts - last; last = ts;
      elapsed += dt;
      var e = Math.min(1, elapsed / durMs);
      if(e >= 1){ finishNow(); return; }
      render(e, false);
      raf = requestAnimationFrame(frame);
    }
    function finishNow(){
      stopRaf();
      isDone = true;
      render(1, true);
      box.classList.remove('playing');
      if(wrap) wrap.style.cursor = 'crosshair';
      if(tip) tip.style.display = 'none';
    }
    function playNow(){
      var n0 = n - 1 - startIdx;
      /* 节奏再放慢：基底 7s + 每日 22ms，整体落在 15s ~ 28s */
      durMs = Math.max(15000, Math.min(28000, 7000 + n0 * 22));
      reduceMotion = false;
      try{ reduceMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches); }catch(e){}
      stopRaf();
      if(reduceMotion){ finishNow(); return; }
      isDone = false; elapsed = 0; last = 0;
      box.classList.add('playing');
      if(wrap) wrap.style.cursor = 'default';
      if(tip) tip.style.display = 'none';
      render(0, false);
      raf = requestAnimationFrame(frame);
    }
    if(wrap){
      var dragging = false;
      wrap.style.cursor = 'default';
      /* 动画播放期间：屏蔽一切悬停 / 拖动，必须等动画完全结束后才可交互 */
      wrap.addEventListener('pointerenter', function(){ if(!isDone) return; wrap.style.cursor = 'crosshair'; });
      wrap.addEventListener('pointermove', function(ev){ if(!isDone) return; showHover(ev); });
      wrap.addEventListener('pointerdown', function(ev){
        if(!isDone) return;
        dragging = true;
        try{ wrap.setPointerCapture(ev.pointerId); }catch(e){}
        showHover(ev);
      });
      wrap.addEventListener('pointerup', function(ev){
        dragging = false;
        /* v3.3：触摸设备没有 hover，手指抬起即复位游标与提示 */
        if(ev.pointerType === 'touch' || ev.pointerType === 'pen'){ leaveHover(); return; }
        var wr = wrap.getBoundingClientRect();
        var out = ev.clientX < wr.left || ev.clientX > wr.right || ev.clientY < wr.top || ev.clientY > wr.bottom;
        if(out) leaveHover();
      });
      wrap.addEventListener('pointercancel', function(){ dragging = false; leaveHover(); });
      wrap.addEventListener('pointerleave', function(){ if(!dragging) leaveHover(); });
    }
    var ctl = {play: playNow, finish: finishNow};
    anim = ctl;
    return ctl;
  }
  /* ---------- 主推演 ---------- */
  function run(){
    var key = tgtEl.value, s = HS[key], y = +yearEl.value, mode = modeEl.value;
    var amt = parseFloat(amtEl.value), day = dayEl.value;
    if(!(amt > 0)){ out.innerHTML = '<div class="empty">请输入大于 0 的投入金额。</div>'; return; }
    var startIdx = HS.lowerBound(s, y), n = s.n;
    if(startIdx >= n - 2){ out.innerHTML = '<div class="empty">所选起点之后历史数据不足，无法推演。</div>'; return; }
    var plan = buildPlan(s, startIdx, mode, day, amt);

    var invest = 0, shares = 0, p = 0, i;
    var valArr = new Array(n), invArr = new Array(n);
    var ddP = new Array(n), ddAt = new Array(n), ddPeakAt = new Array(n);
    var lsV = new Array(n), lsAt = new Array(n);
    var bkD = new Array(n), bkS = new Array(n), bkE = new Array(n);
    var vdP = new Array(n), vdAt = new Array(n);
    for(i = 0; i < startIdx; i++){
      valArr[i] = 0; invArr[i] = 0; ddP[i] = 0; ddAt[i] = startIdx; ddPeakAt[i] = startIdx;
      lsV[i] = 0; lsAt[i] = startIdx; bkD[i] = 0; bkS[i] = startIdx; bkE[i] = startIdx; vdP[i] = 0; vdAt[i] = startIdx;
    }
    var peak = -Infinity, mddP = 0, mddAt = startIdx, peakAt = startIdx, mddPeakAt = startIdx;
    var maxLoss = 0, maxLossAt = startIdx;
    var bankStart = -1, longestBank = 0, bankS = -1, bankE = -1;
    var vpeak = 0, vmdd = 0, vmddAt = startIdx, vpeakAt = startIdx;
    for(i = startIdx; i < n; i++){
      while(p < plan.length && plan[p].i === i){ invest += plan[p].amt; shares += plan[p].amt / s.close[i]; p++; }
      var val = shares * s.close[i];
      valArr[i] = val; invArr[i] = invest;
      var c = s.close[i];
      if(c > peak){ peak = c; peakAt = i; }
      var dd = (peak - c) / peak;
      if(dd > mddP){ mddP = dd; mddAt = i; mddPeakAt = peakAt; }
      if(val > vpeak){ vpeak = val; vpeakAt = i; }
      var vd = vpeak > 0 ? (vpeak - val) / vpeak : 0;
      if(vd > vmdd){ vmdd = vd; vmddAt = i; }
      var fl = val - invest;
      if(fl < maxLoss){ maxLoss = fl; maxLossAt = i; }
      if(val < invest - 1e-6){
        if(bankStart < 0) bankStart = i;
        var sp = s.ts[i] - s.ts[bankStart];
        if(sp > longestBank){ longestBank = sp; bankS = bankStart; bankE = i; }
      }else{ bankStart = -1; }
      ddP[i] = mddP; ddAt[i] = mddAt; ddPeakAt[i] = mddPeakAt;
      lsV[i] = maxLoss; lsAt[i] = maxLossAt;
      bkD[i] = longestBank; bkS[i] = (bankS < 0 ? startIdx : bankS); bkE[i] = (bankE < 0 ? startIdx : bankE);
      vdP[i] = vmdd; vdAt[i] = vmddAt;
    }

    var finalVal = valArr[n-1], totalInv = invArr[n-1];
    var profit = finalVal - totalInv;
    var totRet = totalInv > 0 ? profit / totalInv * 100 : 0;
    var ann = totalInv > 0 ? (s.ts[plan[0].i] < s.ts[n-1] ? xirr(plan, s, finalVal, n-1) : null) : null;
    var holdDays = (s.ts[n-1] - s.ts[startIdx]) / 86400000;

    /* 历年收益（Modified Dietz） */
    var years = [], prevIdx = startIdx - 1, prevEnd = 0;
    for(var yy = y; yy <= +s.dates[n-1].slice(0,4); yy++){
      var idx = -1;
      for(i = prevIdx + 1; i < n; i++){
        var ky = +s.dates[i].slice(0,4);
        if(ky === yy) idx = i; else if(ky > yy) break;
      }
      if(idx < 0) continue;
      var yInv = 0;
      plan.forEach(function(q){ if(q.i > prevIdx && q.i <= idx) yInv += q.amt; });
      var yEnd = valArr[idx];
      var denom = prevEnd + yInv / 2;
      var yRet = denom > 0 ? (yEnd - prevEnd - yInv) / denom * 100 : null;
      years.push({y:yy, inv:invArr[idx], yEnd:yEnd, ret:yRet, first:idx === startIdx || prevIdx < startIdx});
      prevEnd = yEnd; prevIdx = idx;
    }

    var modeTx = mode === 'lump' ? '一次性投入' : ('每月定投（' + (day === 'first' ? '每月首个交易日' : day === 'mid' ? '每月 15 日前后' : '每月最后交易日') + '）');
    var kindTx = s.meta.kind === 'index' ? '价格指数（不含分红）' : '场外基金累计净值（含分红再投）';

    var h = '<div class="simhead"><b>' + esc(s.meta.name) + '</b>' +
      '<span class="meta">' + esc(s.meta.short || '') + ' ｜ ' + esc(kindTx) + ' ｜ ' + esc(modeTx) + ' ｜ 单笔 ' +
      (mode === 'lump' ? fm(amt) : fm(amt) + ' / 月') + ' ｜ 共投入 ' + plan.length + ' 笔</span></div>';
    h += '<div class="resgrid" style="margin-top:12px">';
    function card(id, k, v, cls, sub, idSub, hl){
      return '<div class="rk' + (hl ? ' hl' : '') + '"><div class="k">' + k + '</div><div class="v ' + (cls||'') + '"' +
        (id ? ' id="' + id + '"' : '') + '>' + v + '</div>' + (sub ? '<div class="s"' + (idSub ? ' id="' + idSub + '"' : '') + '>' + sub + '</div>' : '') + '</div>';
    }
    var yN = Math.floor(holdDays / 365), mN = Math.round((holdDays % 365) / 30.44);
    h += card('k-hold', '持有时长', yN + ' 年 ' + (mN >= 12 ? 0 : mN) + ' 个月', '', fd(s.dates[startIdx]) + ' 起，共 ' + Math.round(holdDays) + ' 天', 'k-hold-s');
    h += card('k-inv', '累计投入金额', fm(totalInv), '', '每笔 ' + fm(plan[0].amt) + ' × ' + plan.length + ' 笔', 'k-inv-s');
    h += card('k-pnl', '累计盈亏', (profit >= 0 ? '+' : '') + fm(profit), signCls(profit), '期末市值 - 累计投入');
    h += card('k-final', '最终资产账户', fm(finalVal), '', fd(s.dates[n-1]) + ' 估值');
    h += card('k-ret', '收益率', fp(totRet, true), signCls(totRet), '单笔不折现的总回报');
    h += card('k-ann', '年化收益率', ann === null ? '—' : fp(ann * 100, true), ann === null ? '' : signCls(ann), '按每笔现金流时间加权（IRR）', null, 1);
    h += card('k-mdd', '最大回撤比例', '-' + (mddP * 100).toFixed(2) + '%', 'dn',
      '标的自高点最大跌幅（高点 ' + fd(s.dates[mddPeakAt]) + ' → 低点 ' + fd(s.dates[mddAt]) + '）', 'k-mdd-s');
    h += card('k-loss', '最大浮亏', fm(maxLoss), maxLoss < 0 ? 'dn' : '', maxLoss < 0 ? ('发生于 ' + fd(s.dates[maxLossAt]) + '，即账户市值低于累计投入的极值') : '全程账户市值未低于累计投入', 'k-loss-s');
    h += card('k-bank', '最长回本等待', longestBank > 0 ? (Math.round(longestBank / 86400000) + ' 天') : '0 天',
      '', longestBank > 0 ? ('约 ' + (longestBank / 86400000 / 365).toFixed(1) + ' 年 ｜ ' + fd(s.dates[bankS]) + ' → ' + fd(s.dates[bankE])) : '全程未出现账户浮亏', 'k-bank-s');
    h += '</div>';

    h += chart(s, startIdx, n, valArr, invArr);

    h += '<div class="yearwrap"><table class="yeartable"><thead><tr><th>年份</th><th>累计投入</th><th>年末资产</th><th>当年收益率</th></tr></thead><tbody>';
    years.forEach(function(r){
      var negEq = r.yEnd < r.inv;
      h += '<tr><td>' + r.y + (r.first ? '<span style="color:var(--tx3);font-size:11px"> ·起点</span>' : '') + '</td>' +
        '<td class="mono">' + fm(r.inv) + '</td>' +
        '<td class="mono' + (negEq ? ' dn' : '') + '">' + fm(r.yEnd) + '</td>' +
        '<td class="mono">' + (r.ret === null ? '—' : '<span class="' + signCls(r.ret) + '">' + fp(r.ret, true) + '</span>') + '</td></tr>';
    });
    h += '<tr class="sum"><td>至今（' + fd(s.dates[n-1]) + '）</td><td class="mono">' + fm(totalInv) + '</td><td class="mono">' + fm(finalVal) +
      '</td><td class="mono"><span class="' + signCls(totRet) + '">' + fp(totRet, true) + '</span></td></tr>';
    h += '</tbody></table></div>';

    h += '<p class="capnote"><b>口径说明</b>：指数口径为价格指数，不含股息分红，实际跟踪该指数的基金另有分红与费率损耗；基金口径为累计净值，等价于分红再投资。' +
      '定投按所选规则每月投入固定金额、不设上限，不计申购费与赎回费；一次性投入按起点年份首个交易日全额买入。' +
      '年化收益率为现金流内部收益率（IRR，每笔投入按持有天数折现）；最大回撤基于标的行情序列而非账户市值（账户口径最大回撤为 ' +
      (vmdd * 100).toFixed(2) + '%，发生于 ' + fd(s.dates[vmddAt]) + '）；最大浮亏与最长回本等待按账户口径统计，回本指账户市值恢复至累计投入之上。' +
      '历年收益率为 Modified Dietz 口径（当年盈亏 ÷ 当年平均占用资金）。本推演基于历史行情，不代表未来收益，不构成投资建议。</p>';

    out.innerHTML = h;

    /* 图表几何映射（供动画定位游标）——与 chart() 同源，保证窄屏下圆点仍精确落在曲线上 */
    var ggR = simGeom();
    var W = ggR.W, H = ggR.H, pL = ggR.pL, pR = ggR.pR, pT = ggR.pT, pB = ggR.pB;
    /* 量程与 chart() 完全同源：maxV 仅按“渲染采样点”统计（逐日全量峰值若未被采样点命中会更大量程，
       导致圆点纵坐标与已画出的折线不同源、出现约 1px 的肉眼可见偏差） */
    var sIdx = sampleIdx(startIdx, n), sSet = {};
    sIdx.forEach(function(k){ sSet[k] = 1; });
    var maxV = 0;
    for(i = startIdx; i < n; i++){ if(sSet[i]){ if(valArr[i] > maxV) maxV = valArr[i]; if(invArr[i] > maxV) maxV = invArr[i]; } }
    maxV = maxV > 0 ? maxV * 1.06 : 1;
    var span = (n - 1 - startIdx) || 1;
    function X(k){ return pL + (k - startIdx) / span * (W - pL - pR); }
    function Y(v){ return H - pB - v / maxV * (H - pT - pB); }
    var annTx = ann === null ? '—' : fp(ann * 100, true);
    var animator = buildAnimator({
      s:s, startIdx:startIdx, n:n, plan:plan, valArr:valArr, invArr:invArr, dsIdx:sIdx,
      ddP:ddP, ddAt:ddAt, ddPeakAt:ddPeakAt, lsV:lsV, lsAt:lsAt, bkD:bkD, bkS:bkS, bkE:bkE,
      X:X, Y:Y, annTx:annTx, annCls:ann === null ? '' : signCls(ann)
    });
    if(animator) animator.play();

    var badge = g('simBadge');
    if(badge){
      badge.textContent = s.meta.short + ' ｜ ' + y + ' 起 ｜ ' + fp(totRet, true);
      badge.className = 'fst on';
    }

    var rp = g('simReplay'), sk = g('simSkip');
    if(rp) rp.onclick = function(){ if(animator) animator.play(); };
    if(sk) sk.onclick = function(){ if(animator) animator.finish(); };

    try{ out.scrollIntoView({behavior:'smooth', block:'nearest'}); }catch(e){}
  }

  /* ---------- 交互 ---------- */
  function syncMode(){
    var m = modeEl.value;
    unitEl.textContent = m === 'lump' ? '（元，一次性）' : '（元 / 月）';
    dayFld.style.display = m === 'lump' ? 'none' : '';
    var cur = parseFloat(amtEl.value), other = DEF_AMT[m === 'lump' ? 'dca' : 'lump'];
    if(!(cur > 0) || cur === other) amtEl.value = DEF_AMT[m];
  }
  modeEl.addEventListener('change', syncMode);
  g('simRun').addEventListener('click', run);
  g('simReset').addEventListener('click', function(){
    tgtEl.value = HS.order[0]; fillYears();
    yearEl.value = HS.yearList(HS[HS.order[0]])[0];
    modeEl.value = 'dca'; amtEl.value = DEF_AMT.dca; dayEl.value = 'first'; syncMode();
    out.innerHTML = '<div class="empty">已重置。点击「开始推演」生成推演报告。</div>';
    var badge = g('simBadge');
    if(badge){ badge.textContent = '未推演'; badge.className = 'fst'; }
  });

  syncMode();
  return {run:run};
})();



/* ================================================================================
   v3.1 页首「指数走势图」：分时（默认）/ 日K / 月K / 年K
   数据源自 IDXT（腾讯当日分时 + 新浪美股指数日线聚合月/年K），三标的横排展示
   ================================================================================ */
(function(){
  var BOX = document.getElementById('idxtrend');
  if(!BOX) return;
  var data = {};
  var raw = document.getElementById('IDXT');
  if(raw){ try{ data = JSON.parse(raw.textContent || '{}'); }catch(e){ data = {}; } }
  var items = data.items || [];
  if(!items.length){ BOX.style.display = 'none'; return; }

  var TABS = [['min','分时'],['day','日K'],['mon','月K'],['yr','年K']];
  var W = 300, H = 98, pL = 2, pR = 2, pT = 9, pB = 11;
  var UP = 'var(--up)', DN = 'var(--dn)';

  function nf(v, d){
    if(v === null || v === undefined || v === '' || isNaN(v)) return '—';
    return (+v).toLocaleString('en-US', {minimumFractionDigits:d, maximumFractionDigits:d});
  }
  function sg(v, d){ return (v > 0 ? '+' : '') + nf(v, (d === undefined ? 2 : d)); }
  function dash(d){ return (d && d.length === 8) ? (d.slice(0,4) + '-' + d.slice(4,6) + '-' + d.slice(6,8)) : (d || ''); }
  function dashK(d, mode){
    if(!d) return '';
    if(mode === 'mon' && d.length === 6) return d.slice(0,4) + '-' + d.slice(4,6);
    return d;
  }
  function hhmm(i){ var m = 570 + i; return Math.floor(m / 60) + ':' + ('0' + (m % 60)).slice(-2); }
  function pct(a, b){ return (b > 0) ? (a - b) / b * 100 : 0; }

  function mapY(vals, padF){
    var lo = Infinity, hi = -Infinity, i;
    for(i = 0; i < vals.length; i++){ if(vals[i] < lo) lo = vals[i]; if(vals[i] > hi) hi = vals[i]; }
    if(!isFinite(lo)){ lo = 0; hi = 1; }
    if(hi - lo < 1e-9){ var m = (hi + lo) / 2; lo = m - 1; hi = m + 1; }
    var pad = (hi - lo) * (padF || .12); lo -= pad; hi += pad;
    return function(v){ return H - pB - (v - lo) / (hi - lo) * (H - pT - pB); };
  }

  function buildMin(it){
    var m = it.min;
    if(!m || !m.p || m.p.length < 2) return null;
    var pts = m.p, prev = (m.prev === null || m.prev === undefined) ? pts[0] : m.prev;
    var Y = mapY(pts.concat([prev]));
    function X(i){ return pL + (pts.length > 1 ? i / (pts.length - 1) : 0) * (W - pL - pR); }
    var col = pts[pts.length-1] >= prev ? UP : DN;
    var d = '', i;
    for(i = 0; i < pts.length; i++) d += (i ? 'L' : 'M') + X(i).toFixed(2) + ' ' + Y(pts[i]).toFixed(2);
    var gid = 'itg-' + it.key;
    var h = '<defs><linearGradient id="' + gid + '" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0%" stop-color="' + col + '" stop-opacity=".28"/>' +
      '<stop offset="100%" stop-color="' + col + '" stop-opacity="0"/></linearGradient></defs>';
    h += '<path d="' + d + 'L' + X(pts.length-1).toFixed(2) + ' ' + (H - pB) + 'L' + X(0).toFixed(2) + ' ' + (H - pB) + 'Z" fill="url(#' + gid + ')" stroke="none"/>';
    h += '<line x1="' + pL + '" y1="' + Y(prev).toFixed(2) + '" x2="' + (W - pR) + '" y2="' + Y(prev).toFixed(2) + '" stroke="var(--tx3)" stroke-width="1" stroke-dasharray="4 4" opacity=".5" vector-effect="non-scaling-stroke"/>';
    h += '<path d="' + d + '" fill="none" stroke="' + col + '" stroke-width="1.7" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>';
    h += '<circle cx="' + X(pts.length-1).toFixed(2) + '" cy="' + Y(pts[pts.length-1]).toFixed(2) + '" r="2.2" fill="' + col + '" vector-effect="non-scaling-stroke"/>';
    var last = pts[pts.length-1], pc = pct(last, prev);
    return {
      body: h, last: last, pc: pc, col: col,
      fA: '09:30', fM: (m.d ? dash(m.d) : '') + ' 美东时段', fB: '16:00',
      tip: function(i){
        var v = pts[i], p2 = pct(v, prev);
        return '<div class="tt">' + (m.d ? dash(m.d) + ' ' : '') + hhmm(i) + '</div>' +
          '<div class="tr"><em>点位</em><b>' + nf(v, 2) + '</b></div>' +
          '<div class="tr"><em>较昨收</em><b>' + sg(p2) + '%</b></div>';
      },
      idx: function(f){ return Math.max(0, Math.min(pts.length - 1, Math.round(f * (pts.length - 1)))); },
      cx: function(i){ return X(i); }
    };
  }

  function buildK(it, mode){
    var k = it[mode];
    if(!k || k.n < 2) return null;
    var n = k.n, i, vals = [], Y, col, h = '', d = '';
    for(i = 0; i < n; i++){ vals.push(k.h[i]); vals.push(k.l[i]); }
    Y = mapY(vals);
    var innerW = W - pL - pR, bw = innerW / n, bodyW = Math.max(1, Math.min(bw * .62, 11));
    function XK(i){ return pL + (i + .5) * bw; }
    for(i = 0; i < n; i++){
      var up = k.c[i] >= k.o[i];
      col = up ? UP : DN;
      var xx = XK(i);
      h += '<line x1="' + xx.toFixed(2) + '" y1="' + Y(k.h[i]).toFixed(2) + '" x2="' + xx.toFixed(2) + '" y2="' + Y(k.l[i]).toFixed(2) +
           '" stroke="' + col + '" stroke-width="' + (bw > 4 ? 1.2 : 1) + '" vector-effect="non-scaling-stroke"/>';
      var y1 = Y(Math.max(k.o[i], k.c[i])), y2 = Y(Math.min(k.o[i], k.c[i]));
      h += '<rect x="' + (xx - bodyW / 2).toFixed(2) + '" y="' + y1.toFixed(2) + '" width="' + bodyW.toFixed(2) +
           '" height="' + Math.max(.9, y2 - y1).toFixed(2) + '" fill="' + col + '" rx="' + (bodyW > 3 ? .7 : 0) + '"/>';
    }
    var prevC = k.c[n-2], lastC = k.c[n-1], pc = pct(lastC, prevC);
    var unit = mode === 'yr' ? '年' : (mode === 'mon' ? '月' : '日');
    var tt = {day:'日K', mon:'月K', yr:'年K'}[mode];
    return {
      body: h, last: lastC, pc: pc, col: pc >= 0 ? UP : DN,
      fA: dashK(k.d[0], mode), fM: '近 ' + n + ' 根' + tt, fB: dashK(k.d[n-1], mode),
      tip: function(i){
        var p2 = pct(k.c[i], i > 0 ? k.c[i-1] : k.o[i]);
        return '<div class="tt">' + dashK(k.d[i], mode) + '</div>' +
          '<div class="tr"><em>开</em><b>' + nf(k.o[i], 2) + '</b></div>' +
          '<div class="tr"><em>高</em><b>' + nf(k.h[i], 2) + '</b></div>' +
          '<div class="tr"><em>低</em><b>' + nf(k.l[i], 2) + '</b></div>' +
          '<div class="tr"><em>收</em><b>' + nf(k.c[i], 2) + '</b></div>' +
          '<div class="tr"><em>较' + unit + '前</em><b>' + sg(p2) + '%</b></div>';
      },
      idx: function(f){ return Math.max(0, Math.min(n - 1, Math.floor(f * n))); },
      cx: function(i){ return XK(i); }
    };
  }

  items.forEach(function(it){
    var root = document.createElement('div');
    root.className = 'itcard';
    root.innerHTML =
      '<div class="ithead"><span class="itname">' + it.name + '</span>' +
      '<span class="itval">—</span><span class="itpct">—</span>' +
      '<span class="ittabs">' + TABS.map(function(t, j){
        return '<button type="button" data-t="' + t[0] + '"' + (j === 0 ? ' class="on"' : '') + '>' + t[1] + '</button>';
      }).join('') + '</span></div>' +
      '<div class="itwrap"><svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" role="img"></svg>' +
      '<div class="ittip"></div></div>' +
      '<div class="itfoot"><span class="fa"></span><span class="fm"></span><span class="fb"></span></div>';
    BOX.appendChild(root);

    var c = {it:it, mode:'min', root:root, svg:root.querySelector('svg'), tip:root.querySelector('.ittip'),
             vEl:root.querySelector('.itval'), pEl:root.querySelector('.itpct'),
             aEl:root.querySelector('.fa'), mEl:root.querySelector('.fm'), bEl:root.querySelector('.fb'),
             geo:null};

    function paint(){
      var g = c.mode === 'min' ? buildMin(it) : buildK(it, c.mode);
      if(!g){
        c.svg.innerHTML = '<text x="' + (W/2) + '" y="' + (H/2+4) + '" text-anchor="middle" font-size="11" fill="var(--tx3)">暂无数据</text>';
        c.vEl.textContent = '—'; c.pEl.textContent = '';
        c.aEl.textContent = ''; c.mEl.textContent = '暂无数据'; c.bEl.textContent = '';
        c.geo = null;
        return;
      }
      c.svg.innerHTML = g.body;
      c.geo = g;
      c.vEl.textContent = nf(g.last, 2);
      c.pEl.textContent = sg(g.pc) + '%';
      c.pEl.style.color = g.pc >= 0 ? UP : DN;
      c.aEl.textContent = g.fA; c.mEl.textContent = g.fM; c.bEl.textContent = g.fB;
      c.tip.style.display = 'none';
    }

    function show(ev){
      var g = c.geo;
      if(!g) return;
      var r = c.svg.getBoundingClientRect();
      if(!r.width) return;
      var f = (ev.clientX - r.left) / r.width;
      var i = g.idx(f);
      c.tip.innerHTML = g.tip(i);
      c.tip.style.display = 'block';
      var wr = c.tip.parentNode.getBoundingClientRect();
      var tw = c.tip.offsetWidth, th = c.tip.offsetHeight;
      var left = (r.left - wr.left) + g.cx(i) / W * r.width + 10;
      if(left + tw > wr.width - 2) left = Math.max(2, (r.left - wr.left) + g.cx(i) / W * r.width - tw - 10);
      var top = Math.max(2, Math.min(wr.height - th - 2, (ev.clientY - wr.top) - th / 2));
      c.tip.style.left = left.toFixed(1) + 'px';
      c.tip.style.top = top.toFixed(1) + 'px';
    }

    c.svg.addEventListener('mousemove', show);
    c.svg.addEventListener('mouseleave', function(){ c.tip.style.display = 'none'; });
    /* v3.3：触摸设备没有 hover，改用 pointer 事件——手指点按或按住拖动查看当日 OHLC */
    var itHold = false;
    c.svg.addEventListener('pointerdown', function(ev){
      if(ev.pointerType === 'mouse') return;
      itHold = true;
      try{ c.svg.setPointerCapture(ev.pointerId); }catch(e){}
      show(ev);
    });
    c.svg.addEventListener('pointermove', function(ev){
      if(ev.pointerType === 'mouse') return;   /* 鼠标走 mousemove，避免重复触发 */
      if(!itHold) return;                      /* 仅在按住时显示，纵向滑动页面不误触 */
      ev.preventDefault();
      show(ev);
    });
    c.svg.addEventListener('pointerup', function(ev){
      if(ev.pointerType === 'mouse') return;
      itHold = false; c.tip.style.display = 'none';   /* 松手复位 */
    });
    c.svg.addEventListener('pointercancel', function(){ itHold = false; c.tip.style.display = 'none'; });
    c.root.addEventListener('click', function(e){
      var b = e.target.closest ? e.target.closest('.ittabs button') : null;
      if(!b) return;
      var m = b.getAttribute('data-t');
      if(m === c.mode) return;
      c.mode = m;
      var bs = c.root.querySelectorAll('.ittabs button');
      for(var i = 0; i < bs.length; i++) bs[i].classList.toggle('on', bs[i] === b);
      paint();
    });
    paint();
  });
})();
/* ================= v3.3：移动端交互提示与视口自适应重排 ================= */
(function(){
  var touchDev = false;
  try{
    touchDev = (window.matchMedia && window.matchMedia('(hover:none)').matches) ||
               ('ontouchstart' in window) || (navigator.maxTouchPoints > 0);
  }catch(e){}
  if(touchDev){
    [].forEach.call(document.querySelectorAll('.thint'), function(e){ e.textContent = '点按 / 按住拖动查看任意日期'; });
    [].forEach.call(document.querySelectorAll('.chint'), function(e){ e.textContent = '动画结束后可点按 / 按住拖动查看任意日期'; });
    var tn = document.querySelector('.trendnote');
    if(tn) tn.innerHTML = tn.innerHTML.replace('鼠标移入或按住拖动', '手机点按或按住拖动 / 桌面鼠标移入');
  }
  var rt = window.__qdiiRenderTrend;   /* 横竖屏切换 / 视口变化时按新宽度重排走势图 */
  if(typeof rt === 'function'){
    var w0 = window.innerWidth, tm = null;
    var onResize = function(){
      clearTimeout(tm);
      tm = setTimeout(function(){
        var w = window.innerWidth;
        if(Math.abs(w - w0) < 60) return;
        w0 = w;
        try{ rt(); }catch(e){}
      }, 220);
    };
    window.addEventListener('resize', onResize);
    window.addEventListener('orientationchange', onResize);
  }
})();
</script>

</body>
</html>
"""


# ---------------- 4.5 历史行情序列（供页面「历史定投推演」使用） ----------------
HIST_TARGETS = [
    ("ndx", "纳斯达克100指数", "纳指100", "index", "美元", "新浪财经", ".NDX"),
    ("spx", "标普500指数", "标普500", "index", "美元", "新浪财经", ".INX"),
    ("f160213", "国泰纳斯达克100指数(QDII)", "纳指100·场外基金", "fund", "人民币", "天天基金", "160213"),
    ("f050025", "博时标普500ETF联接A", "标普500·场外基金", "fund", "人民币", "天天基金", "050025"),
]


def sina_daily(symbol):
    """新浪美股指数日线：symbol 形如 .NDX / .INX"""
    url = ("https://stock.finance.sina.com.cn/usstock/api/jsonp_v2.php/x/"
           f"US_MinKService.getDailyK?symbol={symbol}&___qn=3")
    r = requests.get(url, headers=UA_PC, timeout=30)
    rows = json.loads(re.search(r"\((\[.*\])\)", r.text, re.S).group(1))
    pts = []
    for x in rows:
        d = (x.get("d") or "").strip().replace("-", "")
        c = (x.get("c") or "").strip()
        if d and c:
            pts.append((d, float(c)))
    return sorted(pts)


# ---------------- 4.6 基金净值序列（供「基金自助对比 → 业绩走势图」使用） ----------------
NAV_EPOCH = datetime.date(2023, 1, 1)
NAV_WIN_DAYS = 1120      # 保留窗口（约 3 年）
NAV_DAILY_DAYS = 95      # 近 95 天保留日频
NAV_STEP = 5             # 更早按 5 个交易日取 1 点，控制内嵌体积


def _nav_fetch(code):
    """天天基金 pingzhongdata：Data_ACWorthTrend 为累计净值（含分红），退回单位净值。"""
    url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js?v={int(time.time() * 1000)}"
    for _ in range(3):
        try:
            r = requests.get(url, headers=UA_PC, timeout=25)
            if r.status_code != 200 or len(r.text) < 500:
                time.sleep(0.5)
                continue
            m = re.search(r"var\s+Data_ACWorthTrend\s*=\s*(\[.*?\])\s*;", r.text, re.S)
            if m:
                # 个别基金序列中存在 null（当日无估值/暂停披露），逐点过滤后再返回
                out = []
                for pt in json.loads(m.group(1)):
                    try:
                        a, b = pt[0], pt[1]
                        if b is None:
                            continue
                        out.append((int(a), float(b)))
                    except Exception:
                        continue
                return out or None
            m = re.search(r"var\s+Data_netWorthTrend\s*=\s*(\[.*?\])\s*;", r.text, re.S)
            if m:
                out = []
                for p in json.loads(m.group(1)):
                    try:
                        if p.get("y") is None:
                            continue
                        out.append((int(p["x"]), float(p["y"])))
                    except Exception:
                        continue
                return out or None
            return None
        except Exception:
            time.sleep(0.6)
    return None


def fetch_nav_series(codes):
    """抓取全部基金近 3 年累计净值序列（近段日频 + 更早周频采样）。"""
    today = datetime.date.today()
    lo = today - datetime.timedelta(days=NAV_WIN_DAYS)
    dlo = today - datetime.timedelta(days=NAV_DAILY_DAYS)
    res, fail = {}, []

    def one(code):
        arr = _nav_fetch(code)
        if not arr:
            return code, None
        pts = []
        for ts, v in arr:
            try:
                d = datetime.datetime.fromtimestamp(ts / 1000, datetime.timezone.utc).date()
            except Exception:
                continue
            if d < lo or v <= 0:
                continue
            pts.append((d, v))
        out = []
        for i, (d, v) in enumerate(pts):
            if d >= dlo or not out or i % NAV_STEP == 0:
                out.append((d, v))
        return code, (out or None)

    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(one, codes))
    for code, pts in results:
        if not pts:
            fail.append(code)
            continue
        res[code] = {"d": [(d - NAV_EPOCH).days for d, _ in pts],
                     "v": [round(v, 4) for _, v in pts],
                     "n": len(pts)}
    out = {"built": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
           "epoch": NAV_EPOCH.isoformat(), "step": NAV_STEP, "daily": NAV_DAILY_DAYS, "codes": res}
    pts_n = sum(v["n"] for v in res.values())
    print(f"  · 净值序列 {len(res)}/{len(codes)} 只，点位 {pts_n}，失败 {len(fail)}: {fail}")
    return out


def load_nav(codes, force=False):
    """基金净值序列：7 天内缓存复用，--hist 强制刷新；本次全部失败时沿用旧缓存。"""
    path = os.path.join(DATA_DIR, "nav_series.json")
    old = None
    if os.path.exists(path):
        try:
            old = json.load(open(path, encoding="utf-8"))
        except Exception:
            old = None
    if old and not force:
        try:
            built = datetime.datetime.strptime(old.get("built", ""), "%Y-%m-%d %H:%M")
            fresh = (datetime.datetime.now() - built).days <= 7
            have = set((old.get("codes") or {}).keys())
            if fresh and len(have & set(codes)) >= max(1, int(len(codes) * 0.9)):
                print(f"净值序列：使用缓存 {old['built']}（{len(have)} 只）")
                return old
        except Exception:
            pass
    try:
        new = fetch_nav_series(codes)
    except Exception as e:
        print("净值序列抓取异常，沿用旧缓存：", e)
        return old or {"built": "", "epoch": NAV_EPOCH.isoformat(), "codes": {}}
    if not new.get("codes") and old:
        print("净值序列本次全部抓取失败，沿用旧缓存")
        return old
    # 个别基金本次抓取失败时，沿用上一份缓存，避免对比走势图出现空洞
    if old and old.get("codes"):
        want, kept = set(codes), []
        for c, v in old["codes"].items():
            if c in want and c not in new["codes"]:
                new["codes"][c] = v
                kept.append(c)
        if kept:
            print(f"净值序列：{len(kept)} 只沿用上一份缓存（本次抓取失败）：{kept[:12]}")
    json.dump(new, open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    return new


def em_nav_history(code):
    """天天基金历史净值（优先累计净值 LJJZ，缺失时退回单位净值）。接口固定每页 20 条。"""
    PS = 20
    h = dict(UA_PC); h["Referer"] = f"https://fundf10.eastmoney.com/jjjz_{code}.html"

    def one(page):
        for _ in range(4):
            try:
                j = requests.get("https://api.fund.eastmoney.com/f10/lsjz",
                                 params={"fundCode": code, "pageIndex": page, "pageSize": PS},
                                 headers=h, timeout=25).json()
                return j["Data"]["LSJZList"], j["TotalCount"]
            except Exception:
                time.sleep(0.6)
        return [], 0

    lst, total = one(1)
    if not lst:
        return []
    pages = (total + PS - 1) // PS
    if pages > 1:
        with ThreadPoolExecutor(8) as ex:
            for rows, _ in ex.map(one, range(2, pages + 1)):
                lst += rows
    pts = []
    for x in lst:
        d = (x.get("FSRQ") or "").replace("-", "")
        v = x.get("LJJZ") or x.get("DWJZ")
        try:
            v = float(v)
        except Exception:
            continue
        if d and v > 0:
            pts.append((d, v))
    return sorted(set(pts))


def cross_check_nav_lsjz(records):
    """净值 / 日涨跌幅的独立交叉源核验：天天基金历史净值接口（f10/lsjz）最新一行。

    手机接口（fundmobapi）与历史净值接口（lsjz）是两条不同的后端链路，
    用同一只基金的 DWJZ / RZDF 与 lsjz 的 DWJZ / JZZZL 对撞，可同时校验
    「单位净值」「日涨跌幅」「净值日期」三项口径。
    """
    stat = {"n": 0, "dateN": 0, "navOk": 0, "chgOk": 0, "chgDev": 0.0, "navDev": 0.0}

    def one(r):
        code = r["code"]
        h = dict(UA_PC); h["Referer"] = f"https://fundf10.eastmoney.com/jjjz_{code}.html"
        for _ in range(3):
            try:
                j = requests.get("https://api.fund.eastmoney.com/f10/lsjz",
                                 params={"fundCode": code, "pageIndex": 1, "pageSize": 2},
                                 headers=h, timeout=25).json()
                lst = (j.get("Data") or {}).get("LSJZList") or []
                if lst:
                    return code, lst[0]
            except Exception:
                time.sleep(0.5)
        return None

    try:
        by_code = {r["code"]: r for r in records}
        with ThreadPoolExecutor(8) as ex:
            for res in ex.map(one, records):
                if not res:
                    continue
                code, row = res
                r = by_code.get(code)
                if not r:
                    continue
                stat["n"] += 1
                if (row.get("FSRQ") or "").strip() == (r.get("navDate") or "").strip():
                    stat["dateN"] += 1
                try:
                    dv = abs(float(row.get("DWJZ")) - float(r.get("nav")))
                    stat["navDev"] = max(stat["navDev"], dv)
                    if dv <= 0.00051:
                        stat["navOk"] += 1
                except Exception:
                    pass
                try:
                    if str(row.get("JZZZL") or "").strip() not in ("", "--", "None"):
                        dc = abs(float(row.get("JZZZL")) - float(r.get("dayChg")))
                        stat["chgDev"] = max(stat["chgDev"], dc)
                        if dc <= 0.011:
                            stat["chgOk"] += 1
                except Exception:
                    pass
    except Exception:
        pass
    return stat


def fetch_history():
    out = {"built": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "series": []}
    for key, name, short, kind, cur, src, sym in HIST_TARGETS:
        try:
            pts = sina_daily(sym) if kind == "index" else em_nav_history(sym)
        except Exception as e:
            print(f"  ! 历史序列 {key} 抓取失败：{e}")
            pts = []
        if not pts:
            out["series"].append({"key": key, "name": name, "short": short, "kind": kind,
                                  "cur": cur, "src": src, "first": "", "last": "", "n": 0, "pts": ""})
            continue
        pts_s = "|".join(f"{d},{c:.4f}".rstrip("0").rstrip(".") for d, c in pts)
        out["series"].append({"key": key, "name": name, "short": short, "kind": kind, "cur": cur,
                              "src": src, "first": pts[0][0], "last": pts[-1][0], "n": len(pts), "pts": pts_s})
        print(f"  · 历史序列 {key:9s} {len(pts):5d} 条  {pts[0][0]} → {pts[-1][0]}")
    return out


def load_history(force=False):
    """历史序列：默认复用 3 天内的缓存，--hist 强制刷新；抓取失败时退回旧缓存。"""
    path = os.path.join(DATA_DIR, "history.json")
    old = None
    if os.path.exists(path):
        try:
            old = json.load(open(path, encoding="utf-8"))
        except Exception:
            old = None
    if old and not force:
        try:
            built = datetime.datetime.strptime(old.get("built", ""), "%Y-%m-%d %H:%M")
            fresh = (datetime.datetime.now() - built).days <= 7
            complete = bool(old.get("series")) and all(s.get("n") for s in old["series"])
            if fresh and complete:
                print("历史序列：使用缓存", old["built"])
                return old
        except Exception:
            pass
    try:
        new = fetch_history()
    except Exception as e:
        print("历史序列抓取异常，沿用旧缓存：", e)
        return old or {"built": "", "series": []}
    if not any(s.get("n") for s in new["series"]) and old:
        print("历史序列本次全部抓取失败，沿用旧缓存")
        return old
    json.dump(new, open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    return new


# ---------------- 4.7 指数走势序列（供页首「指数走势图」：分时 / 日K / 月K / 年K） ----------------
IDX_TREND_TARGETS = [
    ("ndx", "纳斯达克100", "usNDX", ".NDX"),
    ("spx", "标普500", "usINX", ".INX"),
    ("dji", "道琼斯", "usDJI", ".DJI"),
]
IDXT_DAILY_KEEP = 120      # 日K 保留最近 120 根
IDXT_MONTH_KEEP = 72       # 月K 保留最近 72 根
IDXT_YEAR_KEEP = 30        # 年K 保留最近 30 根


def tencent_minute(code):
    """腾讯美股指数当日分时（09:30 → 16:00，每分钟一个点）。"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/usMinute/query?code={code}"
    j = requests.get(url, headers=UA_PC, timeout=25).json()
    inner = (j.get("data") or {}).get(code) or {}
    dd = inner.get("data") or {}
    ps = []
    for s in dd.get("data") or []:
        a = str(s).split()
        if len(a) < 2:
            continue
        try:
            ps.append(round(float(a[1]), 2))
        except Exception:
            pass
    if not ps:
        return None
    prev = None
    qt = (inner.get("qt") or {}).get(code) or []
    if len(qt) > 4:
        try:
            prev = round(float(qt[4]), 2)
        except Exception:
            prev = None
    return {"d": str(dd.get("date") or ""), "prev": prev, "n": len(ps), "p": ps}


def sina_daily_ohlc(symbol):
    """新浪美股指数日线 OHLC，symbol 形如 .NDX，返回 [(yyyymmdd, o, h, l, c)] 升序。"""
    url = ("https://stock.finance.sina.com.cn/usstock/api/jsonp_v2.php/x/"
           f"US_MinKService.getDailyK?symbol={symbol}&___qn=3")
    r = requests.get(url, headers=UA_PC, timeout=30)
    rows = json.loads(re.search(r"\((\[.*\])\)", r.text, re.S).group(1))
    pts = []
    for x in rows:
        d = (x.get("d") or "").strip().replace("-", "")
        try:
            o, hi, lo, c = float(x["o"]), float(x["h"]), float(x["l"]), float(x["c"])
        except Exception:
            continue
        if len(d) == 8 and c > 0:
            pts.append((d, o, hi, lo, c))
    return sorted(pts)


def _ohlc_agg(rows, span):
    """按 span（'m' 月 / 'y' 年）聚合日线：开盘取首、最高取最大、最低取最小、收盘取末。"""
    out, cur, key = [], None, None
    for d, o, hi, lo, c in rows:
        k = d[:6] if span == "m" else d[:4]
        if cur is None or k != key:
            if cur:
                out.append(cur)
            cur, key = [k, o, hi, lo, c], k
        else:
            cur[1] = o
            cur[2] = max(cur[2], hi)
            cur[3] = min(cur[3], lo)
            cur[4] = c
    if cur:
        out.append(cur)
    return out


def _klines(agg, keep):
    agg = agg[-keep:]
    return {"d": [a[0] for a in agg],
            "o": [round(a[1], 2) for a in agg],
            "h": [round(a[2], 2) for a in agg],
            "l": [round(a[3], 2) for a in agg],
            "c": [round(a[4], 2) for a in agg],
            "n": len(agg)}


def fetch_idx_trend():
    """三大指数：当日分时 + 日K / 月K / 年K（月K、年K 由日线聚合）。"""
    items = []
    for key, name, code, sym in IDX_TREND_TARGETS:
        it = {"key": key, "name": name, "code": code, "sym": sym, "min": None,
              "day": None, "mon": None, "yr": None}
        try:
            it["min"] = tencent_minute(code)
        except Exception as e:
            print(f"  ! 指数分时 {key} 抓取失败：{e}")
        try:
            rows = sina_daily_ohlc(sym)
        except Exception as e:
            print(f"  ! 指数日线 {key} 抓取失败：{e}")
            rows = []
        if rows:
            it["day"] = _klines(rows, IDXT_DAILY_KEEP)
            it["mon"] = _klines(_ohlc_agg(rows, "m"), IDXT_MONTH_KEEP)
            it["yr"] = _klines(_ohlc_agg(rows, "y"), IDXT_YEAR_KEEP)
        items.append(it)
        print(f"  · 指数走势 {key:3s} 分时 {len((it['min'] or {}).get('p') or []):4d} 点 ｜ 日K "
              f"{(it['day'] or {}).get('n', 0):4d} ｜ 月K {(it['mon'] or {}).get('n', 0):3d} ｜ 年K {(it['yr'] or {}).get('n', 0):3d}")
    return {"built": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "items": items}


def load_idx_trend(force=False):
    """指数走势序列：默认复用 40 分钟内的缓存（分时数据时效短）；本次抓取失败时沿用旧缓存。"""
    path = os.path.join(DATA_DIR, "idx_trend.json")
    old = None
    if os.path.exists(path):
        try:
            old = json.load(open(path, encoding="utf-8"))
        except Exception:
            old = None
    if old and not force:
        try:
            built = datetime.datetime.strptime(old.get("built", ""), "%Y-%m-%d %H:%M")
            fresh = (datetime.datetime.now() - built).total_seconds() <= 2400
            ok = len(old.get("items") or []) == len(IDX_TREND_TARGETS) and all(it.get("day") for it in old["items"])
            if fresh and ok:
                print("指数走势：使用缓存", old["built"])
                return old
        except Exception:
            pass
    try:
        new = fetch_idx_trend()
    except Exception as e:
        print("指数走势抓取异常，沿用旧缓存：", e)
        return old or {"built": "", "items": []}
    if not any(it.get("day") for it in new["items"]) and old:
        print("指数走势本次全部抓取失败，沿用旧缓存")
        return old
    json.dump(new, open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    return new


def gen_html(funds, news, changes, idx, ts, history=None, hist2=None, nav=None, idxt=None, verify=None):
    data = json.dumps(funds, ensure_ascii=False).replace("</", "<\\/")
    nw = json.dumps(news, ensure_ascii=False).replace("</", "<\\/")
    ch = json.dumps(changes, ensure_ascii=False).replace("</", "<\\/")
    ix = json.dumps(idx, ensure_ascii=False).replace("</", "<\\/")
    hi = json.dumps(history or [], ensure_ascii=False).replace("</", "<\\/")
    h2 = json.dumps(hist2 or {}, ensure_ascii=False).replace("</", "<\\/")
    nv = json.dumps(nav or {}, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    it_ = json.dumps(idxt or {}, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    vn = ""
    note = ""
    h = lambda t: str(t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    m = IDX_META or {}
    if m.get("quoteTime"):
        comp, em = (m.get("comp") or {}), (m.get("emNDX") or {})
        srcs = "＋".join(x.split("（")[0] for x in (m.get("srcNames") or []))
        note = (f'<b>行情口径与时刻</b>：主源 腾讯行情 qt.gtimg.cn，交叉校验源 {h(srcs)}，共 {m.get("srcN")} 源；'
                f'{m.get("okN")}/{m.get("n")} 只指数多源口径一致（最大相对偏差 {m.get("devMax", 0):.4f}%，通过门槛 0.05%）；'
                f'行情时刻（美东，三只指数取最晚） {h(m.get("quoteTime"))}'
                + (f'，即北京时间 {h(m.get("quoteBJConv"))}（当日美东为夏令时 EDT，与北京相差 12 小时）' if m.get("quoteBJConv") else "")
                + (f'；新浪同源快照自带的北京时刻 {h(m.get("quoteBJ"))}，两者按 12 小时时差互洽' if m.get("quoteBJ") else "")
                + f'。三只指数日期字段均为 {h(m.get("quoteDate"))}，为<b>最近已收盘交易日</b>；快照时刻位于美股常规时段（09:30–16:00 美东）收盘之后，属收盘后最终更新而非盘中/盘前数据。')
        if em.get("price"):
            note += (f'<br><b>口径辨析</b>：东方财富 <code>secid=100.NDX</code>（接口代码字段 f57="NDX"）点位 {em["price"]:,.2f}，'
                     f'其名称字段 f58 为「{h(em.get("name"))}」，与纳斯达克综合指数 COMP'
                     + (f'（{comp["price"]:,.2f}）' if comp.get("price") else "")
                     + ' 完全一致，实为<b>纳斯达克综合指数</b>；本页一律采用<b>纳斯达克100（NDX）</b>口径，两者相差数千点，请勿混用。')
        if m.get("emNDX100"):
            note += (f'<br>交叉印证：东财 <code>secid=100.NDX100</code> 点位 {m["emNDX100"]["price"]:,.2f}'
                     f'（涨跌幅 {m["emNDX100"].get("pct", 0):+.2f}%），与纳指100（NDX）口径一致。')
    if verify:
        ls = verify.get("limitSrc") or {}
        ls_txt = " / ".join(f"{k} {v}" for k, v in sorted(ls.items(), key=lambda kv: -kv[1]))
        idx_txt = (f"指数行情：腾讯行情为主源，新浪财经 / CNBC / 纳斯达克官方交叉校验，"
                   f"{verify.get('idxOk')}/{verify.get('idxN')} 只通过（最大相对偏差 {verify.get('idxDev', 0):.4f}%）"
                   f"，行情时刻 美东 {verify.get('idxQuote') or '—'}"
                   + (f"（即北京时间 {verify.get('idxQuoteBJConv')}，美东夏令时 +12h）" if verify.get("idxQuoteBJConv") else "")) if verify.get("idxN") else "指数行情：本次未取到"
        vn = (f"数据校验（本次扫描自动执行）："
              f"基金申购状态 / 限额与天天基金 PC 详情页交叉核对 {verify.get('sgMatch')}/{verify.get('pcOk')} 一致"
              f"（PC 页可读 {verify.get('pcOk')}/{verify.get('total')}）；"
              f"单日限额来源构成 {ls_txt}；"
              + (f"单位净值 / 日涨跌幅另经历史净值接口（lsjz）逐只对撞：净值一致 {verify.get('lsjzNavOk')}/{verify.get('lsjzN')} 只"
                 f"（最大偏差 {verify.get('lsjzNavDev', 0):.4f} 元）、日涨跌幅一致 {verify.get('lsjzChgOk')}/{verify.get('lsjzN')} 只"
                 f"（最大偏差 {verify.get('lsjzChgDev', 0):.3f} 个百分点）、净值日期一致 {verify.get('lsjzDateN')}/{verify.get('lsjzN')} 只；"
                 if verify.get("lsjzN") else "")
              + f"净值日期 {verify.get('navDates')[0] if verify.get('navDates') else '—'}"
              f"（QDII 净值按 T+1 披露，滞后于美股行情一个交易日）；"
              + (f"对比走势图所用的累计净值序列（东方财富 pingzhongdata）最新净值日较详情页披露日滞后 "
                 f"{verify.get('navSerLag') or 0} 个交易日，{verify.get('navSerNear')}/{verify.get('navSerN')} 只落在该时差内"
                 f"（属净值发布与缓存时差，不改变走势形态）；"
                 if verify.get("navSerN") else "")
              + f"{idx_txt}。")
    # v3.4 顶部标注用：指数数据日期（美东口径，多源校验后的日期字段）
    idx_date_et = h(m.get("quoteDate") or (idx[0].get("date") if idx else "")) or "—"
    html = (HTML.replace("__DATA__", data).replace("__NEWS__", nw).replace("__CHANGES__", ch)
            .replace("__HISTORY__", hi)
            .replace("__IDX__", ix).replace("__HIST2__", h2).replace("__NAV__", nv)
            .replace("__IDXT__", it_).replace("__VERIFY__", vn).replace("__IDXNOTE__", note)
            .replace("__COUNT__", str(len(funds))).replace("__TS__", ts)
            .replace("__IDXDATE__", idx_date_et).replace("__SCANBJ__", ts)
            .replace("__NEWSTIME__", ts[:10]))
    path = os.path.join(BASE_DIR, "qdii_nasdaq_sp500_monitor.html")
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(html)
    # v3.3：同时产出 GitHub Pages 站点入口（index.html）与 .nojekyll，
    # 这样仓库根目录开 Pages 即可直接访问，无需额外配置。
    index_path = os.path.join(BASE_DIR, "index.html")
    with open(index_path, "w", encoding="utf-8") as fp:
        fp.write(html)
    with open(os.path.join(BASE_DIR, ".nojekyll"), "w", encoding="utf-8") as fp:
        fp.write("")
    return path


# ---------------- 5. 主流程 ----------------
def main():
    use_cache = None
    if "--cache" in sys.argv:
        use_cache = sys.argv[sys.argv.index("--cache") + 1]
    # v3.4：页面与数据里的「扫描时间」统一改用北京时间（UTC+8）口径。
    # GitHub Actions Runner 的系统时区是 UTC，直接 now() 会比北京慢 8 小时，容易被误读为"数据没更新"；
    # 这里显式按 UTC 换算到北京时间，与运行环境时区无关（本机运行也同样是北京时间）。
    ts = (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
          + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    if use_cache:
        funds = json.load(open(os.path.join(use_cache, "fundlist.json"), encoding="utf-8"))
        raw = json.load(open(os.path.join(use_cache, "funds_raw.json"), encoding="utf-8"))
        news_raw = json.load(open(os.path.join(use_cache, "news_raw.json"), encoding="utf-8"))
    else:
        funds = get_fund_list()
        raw = fetch_all([f["code"] for f in funds])
        news_raw = fetch_notices([f["code"] for f in funds])
        json.dump(raw, open(os.path.join(RAW_DIR, "funds_raw.json"), "w", encoding="utf-8"), ensure_ascii=False)
    idx = fetch_index()
    codes = [f["code"] for f in funds]
    notice_map = {}
    for n in news_raw:
        notice_map.setdefault(n["code"], []).append(
            {"date": n["date"], "title": n["title"],
             "url": f"https://fund.eastmoney.com/gonggao/{n['code']},{n['id']}.html" if n.get("id") else ""})
    records = [build_record(f, r, notice_map.get(f["code"], [])) for f, r in zip(funds, raw)]
    # 变动检测
    snap_path = os.path.join(DATA_DIR, "snapshot.json")
    prev = {}
    if os.path.exists(snap_path):
        try:
            prev = json.load(open(snap_path, encoding="utf-8"))
        except Exception:
            prev = {}
    changes = []
    for r in records:
        p = prev.get(r["code"])
        st_new, st_old = r["sgzt"], (p or {}).get("sgzt") or ""
        lm_new, lm_old = r["limitText"] or "", (p or {}).get("limitText") or ""
        if p:
            # 仅当上期该字段已有值时才记为「变动」；空白→补充数据不计入变动日志（避免首次补齐口径刷屏）
            if st_old and st_old != st_new:
                changes.append({"code": r["code"], "name": r["name"],
                                "desc": f" 申购状态：{st_old} → {st_new}"})
            if lm_old and lm_old != lm_new:
                changes.append({"code": r["code"], "name": r["name"],
                                "desc": f" 限额调整：{lm_old} → {lm_new or '无'}"})
        r["changed"] = bool(p and ((st_old and st_old != st_new) or (lm_old and lm_old != lm_new)))
    # 历史变动日志（累积写入 data/change_history.json，保留最近 1000 条）
    hist_path = os.path.join(DATA_DIR, "change_history.json")
    history = []
    if os.path.exists(hist_path):
        try:
            history = json.load(open(hist_path, encoding="utf-8"))
        except Exception:
            history = []
    if not isinstance(history, list):
        history = []
    for c in changes:
        history.append({"ts": ts, "code": c["code"], "name": c["name"], "desc": c["desc"], "type": "change"})
    if prev:
        for r in records:
            if r["code"] not in prev:
                history.append({"ts": ts, "code": r["code"], "name": r["name"],
                                "desc": f" 新纳入监控（{r.get('catName', '')}）｜当前申购状态：{r['sgzt']}",
                                "type": "add"})
    history = history[-1000:]
    json.dump(history, open(hist_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    news = []
    for n in news_raw[:20]:
        tag, brief, facts = n.get("tag"), n.get("brief"), n.get("facts")
        if not tag:
            tag, brief, facts = parse_notice(n["title"])
        news.append({"date": n["date"], "title": n["title"], "code": n["code"],
                     "tag": tag, "brief": brief, "facts": facts or [],
                     "fund": next((x["name"] for x in funds if x["code"] == n["code"]), n["code"]),
                     "url": f"https://fund.eastmoney.com/gonggao/{n['code']},{n['id']}.html" if n.get("id") else ""})
    json.dump(records, open(os.path.join(DATA_DIR, "funds.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(news, open(os.path.join(DATA_DIR, "announcements.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(changes, open(os.path.join(DATA_DIR, "changes.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump({r["code"]: {"sgzt": r["sgzt"], "limitText": r["limitText"], "canBuy": r["canBuy"],
                          "nav": r["nav"], "ts": ts} for r in records},
              open(snap_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    hist2 = load_history(force=("--hist" in sys.argv))
    navdata = load_nav(codes, force=("--hist" in sys.argv))
    idxtrend = load_idx_trend(force=("--hist" in sys.argv))
    # 净值序列（对比走势图数据源）与详情净值日期的交叉核对
    navser_ok = navser_near = navser_n = navser_lag = 0
    try:
        _ep = datetime.date.fromisoformat(navdata.get("epoch"))
        for r in records:
            s = (navdata.get("codes") or {}).get(r["code"])
            nd = r.get("navDate")
            if not s or not s.get("d") or not nd:
                continue
            try:
                _last = _ep + datetime.timedelta(days=int(s["d"][-1]))
                _lag = abs((_last - datetime.date.fromisoformat(nd)).days)
            except Exception:
                continue
            navser_n += 1
            navser_lag = max(navser_lag, _lag)
            if _lag == 0:
                navser_ok += 1
            if _lag <= 3:
                navser_near += 1
    except Exception:
        pass
    # 净值 / 日涨跌幅：与历史净值接口（f10/lsjz）逐只对撞（独立于手机接口的第二条链路）
    navx = cross_check_nav_lsjz(records)
    # 数据交叉验证统计（供页面页脚与版本说明展示）
    pc_n = sum(1 for r in records if r.get("pcOk"))
    sg_match_n = sum(1 for r in records if r.get("pcOk") and r.get("sgztMatch"))
    limit_src_n = {}
    for r in records:
        k = r.get("limitSrc") or "无公开限额"
        limit_src_n[k] = limit_src_n.get(k, 0) + 1
    verify = {
        "pcOk": pc_n, "sgMatch": sg_match_n, "total": len(records),
        "limitSrc": limit_src_n,
        "navDateN": len({r.get("navDate") for r in records if r.get("navDate")}),
        "navDates": sorted({r.get("navDate") for r in records if r.get("navDate")})[-1:],
        "idxOk": sum(1 for x in idx if x.get("ok")), "idxN": len(idx),
        "idxDev": max([x.get("devMax") or 0 for x in idx] or [0]),
        "idxSrcN": max([x.get("srcN") or 0 for x in idx] or [0]),
        "idxQuote": (IDX_META.get("quoteTime") or (idx[0].get("time") if idx else "")),
        "idxQuoteBJ": (IDX_META.get("quoteBJ") or ""),
        "idxQuoteBJConv": (IDX_META.get("quoteBJConv") or ""),
        "navSerOk": navser_ok, "navSerNear": navser_near, "navSerN": navser_n, "navSerLag": navser_lag,
        "lsjzN": navx["n"], "lsjzDateN": navx["dateN"], "lsjzNavOk": navx["navOk"], "lsjzNavDev": navx["navDev"],
        "lsjzChgOk": navx["chgOk"], "lsjzChgDev": navx["chgDev"],
    }
    path = gen_html(records, news, changes, idx, ts, history, hist2, navdata, idxtrend, verify)
    cat_stat = {}
    for r in records:
        cat_stat[r.get("catName", "-")] = cat_stat.get(r.get("catName", "-"), 0) + 1
    print("基金数:", len(records), "| 新闻:", len(news), "| 本次变动:", len(changes), "| 历史日志累计:", len(history))
    print("分类构成:", " / ".join(f"{k} {v}" for k, v in cat_stat.items()))
    print("页面:", path)
    print("站点入口:", os.path.join(BASE_DIR, "index.html"))


if __name__ == "__main__":
    main()
