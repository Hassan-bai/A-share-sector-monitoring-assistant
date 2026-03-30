"""
板块数据获取模块
数据来源：
  主力：东方财富公开 API
  备用：通达信行情服务器（qt.gtimg.cn，腾讯行情）
"""
import re
import requests
import json
import logging
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://data.eastmoney.com/bkzj/hy.html",
    "Origin": "https://data.eastmoney.com",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "Connection": "keep-alive",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# ══════════════════════════════════════════════════════════════
# 近7日K线连板数计算（替代不可靠的 f192 字段）
# ══════════════════════════════════════════════════════════════
# 原理：拉近7条日K，从最新一天往前数，连续涨幅 >=9.5% 的天数即为连板数。
# 缓存在 _KLINE_CACHE（每次 get_sector_detail 调用时清空，避免跨刷新脏数据）。
# 只对今日涨停的股票才发请求，非涨停股连板数=0，不需要查。
# ══════════════════════════════════════════════════════════════

_KLINE_CACHE: dict = {}   # {secid: int}  本轮刷新内的缓存

_KLINE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": "https://finance.eastmoney.com/",
}


def _fetch_limit_days(code: str, market: int) -> int:
    """
    通过东财日K接口查询个股近7日中，从最新交易日往前连续涨停的天数。
    market: 0=深圳, 1=上海
    返回值: 0=今日未涨停或无数据, 1=今日首板, 2=2连板, ...
    """
    secid = f"{market}.{code}"
    if secid in _KLINE_CACHE:
        return _KLINE_CACHE[secid]

    result = 0
    try:
        url = (
            "https://push2his.eastmoney.com/api/qt/stock/kline/get"
            f"?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
            "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
            "&lmt=7&klt=101&fqt=1&end=20500101&_=1"
        )
        resp = SESSION.get(url, headers=_KLINE_HEADERS, timeout=5)
        klines = resp.json().get("data", {}).get("klines", []) or []
        # 从最新一天往前数连续涨停（涨幅 >= 9.5%）
        for k in reversed(klines):
            try:
                pct = float(k.split(",")[8])
            except (IndexError, ValueError):
                break
            if pct >= 9.5:
                result += 1
            else:
                break
    except Exception as e:
        logger.debug(f"K线查询失败 {code}: {e}")
        result = 0

    _KLINE_CACHE[secid] = result
    return result


def _batch_fetch_limit_days(stocks_today_limit: list) -> None:
    """
    并发拉取今日涨停股票的K线连板数，结果写入各 stock dict 的 'limit_days' 字段。
    stocks_today_limit: change_pct >= 9.5 的股票列表（已过滤）
    """
    if not stocks_today_limit:
        return
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _fetch_one(st: dict) -> tuple:
        days = _fetch_limit_days(st["code"], st.get("market", 0))
        return st["code"], days

    with ThreadPoolExecutor(max_workers=min(len(stocks_today_limit), 10)) as exe:
        futures = {exe.submit(_fetch_one, st): st for st in stocks_today_limit}
        for fut in as_completed(futures):
            code, days = fut.result()
            futures[fut]["limit_days"] = days  # 覆盖 f192 的值


# 通达信行情 Session（Referer 用腾讯行情）
_TDX_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://gu.qq.com/",
}
_TDX_SESSION = requests.Session()
_TDX_SESSION.headers.update(_TDX_HEADERS)


# ══════════════════════════════════════════════════════════════
# 通达信行情（qt.gtimg.cn）
# ══════════════════════════════════════════════════════════════
# 字段索引（v_xxx="market~name~code~price~yest_close~open~vol~...~change_amt~change_pct~high~low~..."）
_TDX_IDX = {
    "name":        1,
    "code":        2,
    "price":       3,
    "yest_close":  4,
    "open":        5,
    "vol":         6,      # 成交量（手）
    "amount":      37,     # 成交额（元）
    "change_amt":  31,     # 涨跌额
    "change_pct":  32,     # 涨跌幅（%）
    "high":        33,
    "low":         34,
    "turnover":    38,     # 换手率（%）
    "pe_ttm":      39,
    "mkt_cap":     45,     # 总市值（亿）
    "pb":          46,
}


def _parse_tdx_quote(raw_line: str) -> Optional[Dict]:
    """
    解析通达信行情单行：
    v_sh600519="1~贵州茅台~600519~1403.50~1410.27~..."
    返回标准化的股票 dict，失败返回 None。
    """
    m = re.search(r'"(.*?)"', raw_line)
    if not m:
        return None
    parts = m.group(1).split("~")
    def _f(idx: int) -> float:
        try:
            return float(parts[idx]) if idx < len(parts) and parts[idx] else 0.0
        except (ValueError, TypeError):
            return 0.0
    def _s(idx: int) -> str:
        return parts[idx] if idx < len(parts) else ""
    code = _s(_TDX_IDX["code"])
    name = _s(_TDX_IDX["name"])
    if not code:
        return None
    return {
        "name":       name,
        "code":       code,
        "price":      _f(_TDX_IDX["price"]),
        "yest_close": _f(_TDX_IDX["yest_close"]),
        "change_pct": _f(_TDX_IDX["change_pct"]),
        "change_amt": _f(_TDX_IDX["change_amt"]),
        "high":       _f(_TDX_IDX["high"]),
        "low":        _f(_TDX_IDX["low"]),
        "vol":        _f(_TDX_IDX["vol"]),
        "amount":     _f(_TDX_IDX["amount"]),
        "turnover":   _f(_TDX_IDX["turnover"]),
        "pe_ttm":     _f(_TDX_IDX["pe_ttm"]),
        "mkt_cap":    _f(_TDX_IDX["mkt_cap"]),
        "source":     "tdx",
    }


def get_tdx_quotes(codes: List[str]) -> Dict[str, Dict]:
    """
    批量查询通达信行情（qt.gtimg.cn）。
    codes: 纯数字代码列表，如 ["600519", "000001"]
    自动识别 sh/sz 前缀（6开头=sh，其余=sz，30/68开头=sz）
    返回 dict：{code: quote_dict}
    最多支持 100 个代码/次，超过自动分批。
    """
    if not codes:
        return {}

    def _prefix(c: str) -> str:
        if c.startswith("6"):
            return "sh" + c
        elif c.startswith("4") or c.startswith("8"):
            return "bj" + c   # 北交所
        else:
            return "sz" + c

    results: Dict[str, Dict] = {}
    batch_size = 80
    for i in range(0, len(codes), batch_size):
        batch = codes[i: i + batch_size]
        query = ",".join(_prefix(c) for c in batch)
        url = f"http://qt.gtimg.cn/q={query}"
        try:
            resp = _TDX_SESSION.get(url, timeout=8)
            resp.encoding = "gbk"   # 通达信服务器返回 GBK
            for line in resp.text.splitlines():
                line = line.strip()
                if not line or "pv_none_match" in line:
                    continue
                q = _parse_tdx_quote(line)
                if q and q["code"]:
                    results[q["code"]] = q
        except Exception as e:
            logger.debug(f"通达信行情批量查询失败 batch={batch[:3]}...: {e}")
    return results


def get_tdx_quote_single(code: str) -> Optional[Dict]:
    """查询单只股票通达信行情，失败返回 None"""
    r = get_tdx_quotes([code])
    return r.get(code)


def get_sector_list() -> List[Dict]:
    """
    获取板块列表（行业板块 + 概念板块），含涨速、涨幅等基础信息。
    返回字段：
      - name: 板块名称
      - code: 板块代码
      - change_pct: 涨跌幅 (%)
      - rise_speed: 涨速 (分钟内涨幅，东财字段 f124)
      - total_mkt_cap: 总市值
    """
    results = []
    # 板块类型：行业=2，概念=3
    for bk_type in (2, 3):
        url = (
            "https://push2delay.eastmoney.com/api/qt/clist/get"
            f"?cb=&pn=1&pz=200&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281"
            f"&fltt=2&invt=2&wbp2u=|0|0|0|web&fid=f3&fs=m:90+t:{bk_type}"
            f"&fields=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f20,f21,f23,"
            f"f24,f25,f22,f11,f62,f128,f136,f115,f152,f124,f107,f104,f105,f140,"
            f"f141,f207,f208,f209,f222&_={{}}"
        )
        try:
            resp = SESSION.get(url, timeout=10)
            data = resp.json()
            items = data.get("data", {}).get("diff", []) or []
            for item in items:
                name = item.get("f14", "")
                code = item.get("f12", "")
                change_pct = item.get("f3", 0)        # 涨跌幅
                rise_speed = item.get("f124", 0)       # 涨速（近一分钟涨幅）
                net_inflow = item.get("f62", 0)        # 主力净流入（元）
                if name and code:
                    results.append({
                        "name": name,
                        "code": str(code),
                        "bk_type": bk_type,
                        "change_pct": _safe_float(change_pct),
                        "rise_speed": _safe_float(rise_speed),
                        "net_inflow": _safe_float(net_inflow),
                    })
        except Exception as e:
            logger.warning(f"获取板块列表失败 bk_type={bk_type}: {e}")
    return results


def get_sector_stocks(bk_code: str, bk_type: int = 2, top_n: int = 10) -> List[Dict]:
    """
    获取某板块内成分股，按主力净流入排序，返回 top_n 个。
    返回字段：
      - name: 股票名称
      - code: 股票代码
      - change_pct: 涨跌幅
      - net_inflow: 主力净流入（元）
      - price: 当前价格
    """
    # 根据板块代码构造 fs 参数
    # 行业板块 m:90+t:2+b:xxx，概念板块 m:90+t:3+b:xxx
    # 成分股用 m:0+t:80+b:{bk_code}（东财通用）
    url = (
        "https://push2delay.eastmoney.com/api/qt/clist/get"
        f"?cb=&pn=1&pz={top_n}&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281"
        f"&fltt=2&invt=2&wbp2u=|0|0|0|web&fid=f62&fs=b:{bk_code}+f:!50"
        f"&fields=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f62,f85,f115,f184,f11"
        f"&_={{}}"
    )
    stocks = []
    try:
        resp = SESSION.get(url, timeout=10)
        data = resp.json()
        items = data.get("data", {}).get("diff", []) or []
        for item in items:
            name = item.get("f14", "")
            code = item.get("f12", "")
            change_pct = item.get("f3", 0)
            net_inflow = item.get("f62", 0)
            price = item.get("f2", 0)
            pe_ratio_raw = item.get("f85", "-")
            try:
                pe_ratio = float(pe_ratio_raw)
                if abs(pe_ratio) > 9999:
                    pe_ratio = -1.0
            except (TypeError, ValueError):
                pe_ratio = -1.0
            one_year_raw = item.get("f115", "-")
            try:
                one_year_pct = float(one_year_raw)
            except (TypeError, ValueError):
                one_year_pct = 0.0
            if name and code:
                stocks.append({
                    "name": name,
                    "code": str(code),
                    "change_pct": _safe_float(change_pct),
                    "net_inflow": _safe_float(net_inflow),
                    "price": _safe_float(price),
                    "risk_tags": _detect_risk(name, pe_ratio, one_year_pct),
                })
    except Exception as e:
        logger.warning(f"获取板块成分股失败 bk_code={bk_code}: {e}")
    return stocks


def get_sector_leaders(bk_code: str, top_n: int = 5) -> List[Dict]:
    """
    获取板块龙头股（按涨幅+市值综合排序）
    """
    url = (
        "https://push2delay.eastmoney.com/api/qt/clist/get"
        f"?cb=&pn=1&pz={top_n}&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281"
        f"&fltt=2&invt=2&wbp2u=|0|0|0|web&fid=f3&fs=b:{bk_code}+f:!50"
        f"&fields=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f62,f21"
        f"&_={{}}"
    )
    leaders = []
    try:
        resp = SESSION.get(url, timeout=10)
        data = resp.json()
        items = data.get("data", {}).get("diff", []) or []
        for item in items[:top_n]:
            name = item.get("f14", "")
            code = item.get("f12", "")
            change_pct = item.get("f3", 0)
            mkt_cap = item.get("f21", 0)
            if name and code:
                leaders.append({
                    "name": name,
                    "code": str(code),
                    "change_pct": _safe_float(change_pct),
                    "mkt_cap": _safe_float(mkt_cap),
                })
    except Exception as e:
        logger.warning(f"获取板块龙头失败 bk_code={bk_code}: {e}")
    return leaders


def get_sector_detail(bk_code: str, bk_type: int = 2) -> Dict:
    """
    获取板块详情：全量成分股（含连板数、近5日涨幅、量比），
    自动识别并分类：
      - longs:    龙头股（涨幅靠前 + 连板 + 大单流入）
      - followers: 跟风小弟（涨幅较大但流入偏少）
      - limit_ladder: 连板梯队（按连板天数分组 N板/首板）
    返回 dict:
      {
        "longs": [...],
        "followers": [...],
        "limit_ladder": {"N板": [...], ...},
        "all_stocks": [...]
      }
    """
    url = (
        "https://push2delay.eastmoney.com/api/qt/clist/get"
        f"?cb=&pn=1&pz=200&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281"
        f"&fltt=2&invt=2&wbp2u=|0|0|0|web&fid=f3&fs=b:{bk_code}"
        f"&fields=f2,f3,f4,f5,f6,f10,f11,f12,f13,f14,f20,f21,f62,f84,f85,f115,f184,f192,f193"
        f"&_={{}}"
    )
    stocks = []
    try:
        resp = SESSION.get(url, timeout=10)
        data = resp.json()
        items = data.get("data", {}).get("diff", []) or []
        for item in items:
            name = item.get("f14", "")
            code = item.get("f12", "")
            if not name or not code:
                continue
            change_pct   = _safe_float(item.get("f3", 0))
            net_inflow   = _safe_float(item.get("f62", 0))
            price        = _safe_float(item.get("f2", 0))
            mkt_cap      = _safe_float(item.get("f20", 0))
            volume_ratio = _safe_float(item.get("f10", 0))  # 量比
            # f85: 市盈率（TTM），亏损时为负数或"-"
            pe_ratio_raw = item.get("f85", "-")
            try:
                pe_ratio = float(pe_ratio_raw)
                if abs(pe_ratio) > 9999:
                    pe_ratio = -1.0   # 极端值视为亏损
            except (TypeError, ValueError):
                pe_ratio = -1.0
            # f115: 近一年涨跌幅（%）
            one_year_raw = item.get("f115", "-")
            try:
                one_year_pct = float(one_year_raw)
            except (TypeError, ValueError):
                one_year_pct = 0.0
            # f192: 连板天数（正数=连板数, -1=未涨停, 其他负数=跌停等）
            limit_days_raw = item.get("f192", -1)
            try:
                limit_days = int(limit_days_raw)
            except (TypeError, ValueError):
                limit_days = -1
            # f193: 近5日涨幅（字符串"-"时为无数据）
            five_day_pct_raw = item.get("f193", "-")
            try:
                five_day_pct = float(five_day_pct_raw)
            except (TypeError, ValueError):
                five_day_pct = 0.0
            market = item.get("f13", 0)   # 0=深圳 1=上海
            # 风险标签
            risk_tags = _detect_risk(name, pe_ratio, one_year_pct)
            stocks.append({
                "name": name,
                "code": str(code),
                "market": market,
                "change_pct": change_pct,
                "net_inflow": net_inflow,
                "price": price,
                "mkt_cap": mkt_cap,
                "volume_ratio": volume_ratio,
                "limit_days": limit_days,       # 连板天数
                "five_day_pct": five_day_pct,   # 近5日涨幅
                "pe_ratio": pe_ratio,
                "one_year_pct": one_year_pct,
                "risk_tags": risk_tags,         # 风险标签列表，空=无风险
            })
    except Exception as e:
        logger.warning(f"获取板块详情失败 bk_code={bk_code}: {e}")
        return {"longs": [], "followers": [], "limit_ladder": {}, "all_stocks": []}

    if not stocks:
        return {"longs": [], "followers": [], "limit_ladder": {}, "all_stocks": []}

    # ── 通达信行情补全（价格/涨幅为0的成分股，用 qt.gtimg.cn 补齐）──
    # 东财非交易时段偶尔返回0值，通达信能提供最新快照
    missing_codes = [
        s["code"] for s in stocks
        if s["price"] == 0.0 or s["change_pct"] == 0.0
    ]
    if missing_codes:
        try:
            tdx_map = get_tdx_quotes(missing_codes)
            for s in stocks:
                q = tdx_map.get(s["code"])
                if q:
                    if s["price"] == 0.0 and q["price"] > 0:
                        s["price"] = q["price"]
                    if s["change_pct"] == 0.0 and q["change_pct"] != 0.0:
                        s["change_pct"] = q["change_pct"]
                    # 附加通达信扩展字段（若东财未提供）
                    if "high" not in s:
                        s["high"] = q["high"]
                    if "low" not in s:
                        s["low"] = q["low"]
                    if "turnover" not in s:
                        s["turnover"] = q["turnover"]
            logger.debug(f"通达信补全 {len(missing_codes)} 只股票行情")
        except Exception as e:
            logger.debug(f"通达信行情补全失败（非致命）: {e}")

    # ── 排序：涨幅降序 ──
    stocks.sort(key=lambda x: x["change_pct"], reverse=True)

    # ── 用近7日K线重新计算今日涨停股的真实连板数（覆盖不可靠的 f192）──
    # 只对今日涨停的股票发请求，通常不超过10只，并发拉取耗时可控。
    _KLINE_CACHE.clear()   # 每次刷新清空，避免跨刷新脏数据
    today_limit_stocks = [s for s in stocks if s["change_pct"] >= 9.5]
    _batch_fetch_limit_days(today_limit_stocks)
    # 非涨停股 limit_days 保持 f192 原值（历史连板未封场景仍用 f192，
    # 但需经过 1~15 有效性校验，见下方连板梯队逻辑）


    # ══════════════════════════════════════════════════════════
    # 连板梯队
    # ══════════════════════════════════════════════════════════
    # 今日涨停的股：limit_days 已被 _batch_fetch_limit_days 用近7日K线覆盖，
    #   值 = 从今天往前数连续涨停天数（1=首板, 2=2连板, 3=3连板...），直接用。
    #
    # 今日未涨停的股：limit_days 仍是原始 f192，可能有异常值，
    #   只接受 [2, 15] 区间的正整数，超出范围视为无效（不进梯队）。
    #   用途：炸板/高开低走但近期有连板历史的股，标"N板(未封)"显示。
    # ══════════════════════════════════════════════════════════
    from collections import defaultdict
    ladder_map: dict = defaultdict(list)
    for st in stocks:
        ld_raw      = st["limit_days"]
        pct         = st["change_pct"]
        today_limit = pct >= 9.5

        in_ladder    = False
        display_days = 1

        if today_limit:
            # K线算出的真实连板数，直接用（已保证 >= 1）
            display_days = max(ld_raw, 1)
            in_ladder = True
        else:
            # 非涨停：f192 有效性校验，只接受 2~15
            ld_valid = ld_raw if (isinstance(ld_raw, int) and 2 <= ld_raw <= 15) else 0
            if ld_valid >= 2:
                display_days = ld_valid
                in_ladder = True

        if in_ladder:
            suffix = "" if today_limit else "(未封)"
            label = f"{display_days}板{suffix}"
            st["_today_limit"] = today_limit
            st["_display_days"] = display_days
            ladder_map[label].append(st)

    # 按板数降序排列梯队
    limit_ladder = dict(
        sorted(ladder_map.items(), key=lambda x: -_ladder_order(x[0]))
    )

    # 所有进入连板梯队的股票码（用于后续排除跟风）
    ladder_codes = {st["code"] for group in limit_ladder.values() for st in group}

    # ── 龙头识别：涨幅前列 + 大单流入为正 + (当前连板 or 近5日涨幅超15%) ──
    longs = []
    for st in stocks[:20]:   # 只在前20里找
        today_limit = st["change_pct"] >= 9.5
        ld          = st["limit_days"]
        # 连板条件：今日涨停 OR f192>=2（近期连板）
        current_limit = today_limit or ld >= 2
        is_strong = (
            st["change_pct"] >= 5.0
            and (
                current_limit
                or st["five_day_pct"] >= 15.0
                or st["net_inflow"] >= 5_000_0000
            )
        )
        if is_strong:
            tag_parts = []
            days = st.get("_display_days", ld)
            if current_limit and days >= 2:
                tag_parts.append(f"连板{days}天")
            elif current_limit and days == 1:
                tag_parts.append("首板")
            if st["five_day_pct"] >= 15.0:
                tag_parts.append(f"近5日+{st['five_day_pct']:.1f}%")
            if st["net_inflow"] >= 1e8:
                tag_parts.append("主力大单")
            st["tags"] = tag_parts
            st["role"] = "dragon"
            longs.append(st)
        if len(longs) >= 6:
            break

    long_codes = {s["code"] for s in longs}

    # ── 跟风小弟：微红即可（涨幅 > 0），但不够龙头条件（流入小、近期无连板行为）──
    # 排除：在连板梯队中的股票（不管今天有没有封板，有连板历史的不算跟风小弟）
    followers = []
    for st in stocks:
        if st["code"] in long_codes:
            continue
        if st["code"] in ladder_codes:
            continue  # 有连板历史/今日涨停 → 不算跟风小弟
        if st["change_pct"] <= 0:
            continue  # 跌的不算跟风，只要微红（>0）就纳入
        # 流入量 < 2000万 才算跟风（否则有主力介入算不上跟风）
        if st["net_inflow"] > 2_000_0000:
            continue
        st["role"] = "follower"
        st["tags"] = ["跟风小弟"]
        followers.append(st)
        if len(followers) >= 8:
            break

    # 首板标记（今日首次涨停，f192==1 或 盘中首次涨停）
    for st in stocks:
        today_limit = st["change_pct"] >= 9.5
        ld = st["limit_days"]
        # 首板：今日涨停 且 display_days==1（f192<=1，说明非连板）
        display_days = st.get("_display_days", ld)
        if today_limit and display_days == 1:
            existing = st.get("tags", [])
            if "首板" not in existing:
                st["tags"] = existing + ["首板"]

    return {
        "longs": longs,
        "followers": followers,
        "limit_ladder": limit_ladder,
        "all_stocks": stocks[:50],  # 最多返回50只
    }


def _ladder_order(label: str) -> int:
    """提取梯队标签中的数字用于排序"""
    import re
    m = re.search(r"\d+", label)
    return int(m.group()) if m else 0


def _detect_risk(name: str, pe_ratio: float, one_year_pct: float) -> list:
    """
    识别个股风险，返回风险标签列表（用于前端黄色高亮）。

    规则：
    1. 名称含 *ST / ST  → ["*ST"] 或 ["ST"]（特别处理风险 / 退市警示）
    2. 名称含「退」（退市整理期）→ ["退市风险"]
    3. 名称含「N」开头（新股）→ 不标记（新股PE无意义）
    4. 市盈率 ≤ 0（亏损）+ 近一年跌幅 ≤ -40% → ["亏损暴雷"]
    5. 近一年跌幅 ≤ -60%（无论PE）→ ["深度下跌"]
    """
    tags = []
    upper = name.upper()
    if "*ST" in upper:
        tags.append("*ST")
    elif "ST" in upper:
        tags.append("ST")
    if "退" in name:
        tags.append("退市风险")
    # 财务风险：亏损 + 大幅下跌
    if not tags:  # 已经有ST标签时不重复加
        if pe_ratio <= 0 and one_year_pct <= -40:
            tags.append("亏损暴雷")
        elif one_year_pct <= -60:
            tags.append("深度下跌")
    return tags


def _safe_float(val) -> float:
    try:
        v = float(val)
        if v == "-" or v is None:
            return 0.0
        # 过滤东财 API 占位无效值（如 1774321149 等超大数）
        if abs(v) > 1_000_000_000:
            return 0.0
        return v
    except (TypeError, ValueError):
        return 0.0
