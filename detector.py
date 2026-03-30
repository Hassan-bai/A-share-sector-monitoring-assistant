"""
板块异动检测核心逻辑
"""
import logging
from typing import List, Dict

logger = logging.getLogger(__name__)

# ── 触发条件阈值（可按需调整）──────────────────────────────────
# 通用涨幅门槛（%）
CHANGE_PCT_THRESHOLD = 2.0
# 涨速 >= 此值（近1分钟涨幅 %）
RISE_SPEED_THRESHOLD = 0.3
# 主力净流入 >= 此值（元）：5000万
NET_INFLOW_THRESHOLD = 5_000_0000
# 每次最多上报板块数
MAX_ALERT_SECTORS = 8

# ── 特殊板块：降低涨幅门槛到 0.5%（受大宗商品/国际油价驱动，日内弹性小）──
# 匹配规则：板块名称包含以下任一关键词则使用宽松门槛
_LOW_THRESHOLD_KEYWORDS = [
    "油气", "能源", "天然气", "燃气", "炼化", "石油",
    "煤炭", "动力煤", "焦煤",
    "黄金", "贵金属", "有色", "工业金属", "稀土", "锂矿",
    "钢铁", "铁矿",
    "航运", "航空",
    "银行", "保险",
    "水务", "电力",
]
# 宽松门槛（%）
_LOW_CHANGE_PCT = 0.5
# 宽松涨速门槛（%/分钟）
_LOW_RISE_SPEED = 0.15
# 宽松资金门槛（元）：3000万
_LOW_INFLOW_THRESHOLD = 3_000_0000


def _is_low_volatility_sector(name: str) -> bool:
    """判断是否为低波动板块（大宗/金融/公用事业等），使用宽松检测门槛"""
    return any(kw in name for kw in _LOW_THRESHOLD_KEYWORDS)


def detect_anomalies(sectors: List[Dict]) -> List[Dict]:
    """
    从板块列表中筛选出异动板块。
    触发条件（满足其一）：
      1. 涨速 >= RISE_SPEED_THRESHOLD AND 涨幅 >= 门槛
      2. 主力净流入 >= NET_INFLOW_THRESHOLD AND 涨幅 >= 门槛

    门槛自适应：
      - 普通板块（科技/消费/医药等）：涨幅门槛 1.0%，涨速门槛 0.3%/min
      - 低波动板块（油气/能源/银行/钢铁等）：涨幅门槛 0.5%，涨速门槛 0.15%/min
        这类板块受大宗商品/国际价格驱动，日内绝对涨幅小但异动信号同样有效

    综合打分后排序，取 top MAX_ALERT_SECTORS。
    """
    candidates = []
    for sec in sectors:
        change = sec.get("change_pct", 0)
        speed = sec.get("rise_speed", 0)
        inflow = sec.get("net_inflow", 0)
        name = sec.get("name", "")

        # 根据板块特性选门槛
        if _is_low_volatility_sector(name):
            chg_floor  = _LOW_CHANGE_PCT
            spd_floor  = _LOW_RISE_SPEED
            inf_floor  = _LOW_INFLOW_THRESHOLD
        else:
            chg_floor  = 1.0
            spd_floor  = RISE_SPEED_THRESHOLD
            inf_floor  = NET_INFLOW_THRESHOLD

        if change < chg_floor:
            continue

        hit_speed  = speed >= spd_floor
        hit_inflow = inflow >= inf_floor

        if not (hit_speed or hit_inflow):
            continue

        # 综合评分：涨幅权重0.4 + 涨速权重0.3 + 流入权重0.3（归一化）
        score = (
            change * 0.4
            + min(speed / 2.0, 1.0) * 10 * 0.3
            + min(inflow / 2e8, 1.0) * 10 * 0.3
        )
        candidates.append({**sec, "_score": score, "_hit_speed": hit_speed, "_hit_inflow": hit_inflow})

    candidates.sort(key=lambda x: x["_score"], reverse=True)
    return candidates[:MAX_ALERT_SECTORS]


def format_alert_text(alert: Dict) -> str:
    """格式化异动板块文字摘要（用于日志）"""
    name = alert.get("name", "")
    pct = alert.get("change_pct", 0)
    speed = alert.get("rise_speed", 0)
    inflow = alert.get("net_inflow", 0)
    inflow_str = _fmt_money(inflow)
    low_vol_tag = "（低波板块宽松门槛）" if _is_low_volatility_sector(name) else ""
    return (
        f"【{name}】{low_vol_tag} "
        f"涨幅+{pct:.2f}%  涨速+{speed:.2f}%/分  "
        f"主力净流入{inflow_str}"
    )


def _fmt_money(val: float) -> str:
    if abs(val) >= 1e8:
        return f"{val/1e8:.2f}亿"
    if abs(val) >= 1e4:
        return f"{val/1e4:.0f}万"
    return f"{val:.0f}"


# ══════════════════════════════════════════════════════════════
# 全市跳水检测
# ══════════════════════════════════════════════════════════════
# 全市跳水：「多数热门板块同时资金流出 + 总体下跌」
#
# 触发逻辑（同时满足）：
#   A. 全量板块中，净流出板块数 >= DIVE_OUTFLOW_RATIO（70%）
#   B. 全量板块中，下跌板块数   >= DIVE_DOWN_RATIO（60%）
#   C. 全市平均涨幅              <= DIVE_AVG_CHANGE（-0.5%）
#   D. 主力净流出总额（负值）    <= DIVE_TOTAL_OUTFLOW（-5亿）
#
# 结束条件：连续 DIVE_END_ROUNDS 轮不再满足则解除

DIVE_OUTFLOW_RATIO   = 0.70   # 净流出板块占比（>=此值触发）
DIVE_DOWN_RATIO      = 0.60   # 下跌板块占比（>=此值触发）
DIVE_AVG_CHANGE      = -0.5   # 全市平均涨跌幅（<=此值触发）
DIVE_TOTAL_OUTFLOW   = -5e8   # 全市主力净流出总和（<=此值触发，-5亿）
DIVE_END_ROUNDS      = 3      # 连续几轮不满足才解除跳水状态


def detect_market_dive(sectors: List[Dict], prev_state: Dict) -> Dict:
    """
    全市跳水检测。
    sectors:    全量板块列表（含 change_pct / net_inflow）
    prev_state: 上一次返回的 state dict（首次传 {}）

    返回 dict：
      is_diving:     bool  当前是否处于跳水状态
      just_started:  bool  本轮刚进入跳水（需推通知）
      just_ended:    bool  本轮刚结束跳水（需推通知）
      stats:         dict  {outflow_pct, down_pct, avg_change, total_outflow, sector_count}
      dive_rounds:   int   已连续跳水轮数
      calm_rounds:   int   跳水中连续平静轮数（用于解除判断）
      _sectors_outflow: list  流出板块名列表（用于日志）
    """
    n = len(sectors)
    if n == 0:
        return {**prev_state, "is_diving": False, "just_started": False, "just_ended": False}

    outflow_count = sum(1 for s in sectors if s.get("net_inflow", 0) < 0)
    down_count    = sum(1 for s in sectors if s.get("change_pct", 0) < 0)
    avg_change    = sum(s.get("change_pct", 0) for s in sectors) / n
    total_outflow = sum(s.get("net_inflow", 0) for s in sectors if s.get("net_inflow", 0) < 0)

    outflow_pct = outflow_count / n
    down_pct    = down_count / n

    stats = {
        "outflow_pct":   round(outflow_pct, 3),
        "down_pct":      round(down_pct, 3),
        "avg_change":    round(avg_change, 3),
        "total_outflow": round(total_outflow),
        "sector_count":  n,
    }

    # 判断当前轮是否满足跳水条件
    cond_met = (
        outflow_pct >= DIVE_OUTFLOW_RATIO
        and down_pct    >= DIVE_DOWN_RATIO
        and avg_change  <= DIVE_AVG_CHANGE
        and total_outflow <= DIVE_TOTAL_OUTFLOW
    )

    was_diving   = prev_state.get("is_diving", False)
    dive_rounds  = prev_state.get("dive_rounds", 0)
    calm_rounds  = prev_state.get("calm_rounds", 0)

    if cond_met:
        dive_rounds += 1
        calm_rounds  = 0
        is_diving    = True
    else:
        calm_rounds += 1
        dive_rounds  = dive_rounds if was_diving else 0
        # 连续 DIVE_END_ROUNDS 轮不满足才正式解除
        is_diving = was_diving and (calm_rounds < DIVE_END_ROUNDS)

    just_started = is_diving and not was_diving
    just_ended   = not is_diving and was_diving

    # 跳水时记录流出最多的前5个板块
    outflow_sectors = sorted(
        [s for s in sectors if s.get("net_inflow", 0) < 0],
        key=lambda s: s.get("net_inflow", 0)
    )[:5]

    return {
        "is_diving":        is_diving,
        "just_started":     just_started,
        "just_ended":       just_ended,
        "stats":            stats,
        "dive_rounds":      dive_rounds,
        "calm_rounds":      calm_rounds,
        "outflow_sectors":  [{"name": s["name"], "change_pct": s.get("change_pct", 0),
                              "net_inflow": s.get("net_inflow", 0)} for s in outflow_sectors],
    }


# ══════════════════════════════════════════════════════════════
# 逆势偷涨检测（跳水中还在涨且有资金流入的板块）
# ══════════════════════════════════════════════════════════════
# 核心逻辑：跳水时大盘整体均涨为负，只要板块还在涨（哪怕微涨）
# 且有主力资金持续流入（哪怕不多），就说明有人在偷偷建仓。
#
# 触发条件（同时满足，门槛刻意放低）：
#   1. 涨幅 >= BUCK_CHANGE_MIN（+0.2%）—— 还在涨，不是惯性高位平盘
#   2. 净流入 > 0                        —— 有资金在净流入（任意金额）
#
# 额外加分项（不是硬性门槛）：
#   - 涨速 > 0：还有向上动能
#   - 超额涨幅（相对全市均值）越大越靠前
#
# 排序：超额涨幅（板块涨幅 - 全市均涨）降序 → 体现"跑赢大盘"程度

BUCK_CHANGE_MIN  = 0.2    # 最低涨幅（%），非常宽松
MAX_BUCK_SECTORS = 8      # 最多上报几个


def detect_buck_trend(sectors: List[Dict], dive_state: Dict) -> List[Dict]:
    """
    在跳水状态下，找出逆势偷涨的板块（涨幅>0 且资金净流入）。
    门槛宽松：只要还在涨 + 有人在买就上报，让用户自己判断强弱。
    按"超额涨幅"（相对全市均值）从高到低排序。
    """
    if not dive_state.get("is_diving", False):
        return []

    # 全市平均涨幅（dive_state 里已算好）
    mkt_avg = dive_state.get("stats", {}).get("avg_change", 0)

    candidates = []
    for s in sectors:
        change = s.get("change_pct", 0)
        inflow = s.get("net_inflow", 0)
        speed  = s.get("rise_speed", 0)

        # 硬性条件：涨幅达最低门槛 + 资金净流入（>0 即可）
        if change < BUCK_CHANGE_MIN or inflow <= 0:
            continue

        # 超额涨幅：相对全市均值跑了多少
        excess = change - mkt_avg

        # 综合评分：超额涨幅权重最高，再叠加资金和涨速加分
        score = (
            excess * 0.55
            + min(inflow / 5e7, 1.0) * 10 * 0.30   # 资金（5000万归一）
            + min(max(speed, 0) / 1.0, 1.0) * 10 * 0.15  # 涨速加分
        )
        candidates.append({**s, "_buck_score": score, "_excess": round(excess, 3)})

    candidates.sort(key=lambda x: x["_buck_score"], reverse=True)
    return candidates[:MAX_BUCK_SECTORS]
