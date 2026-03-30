"""
周度复盘报告生成器
- 汇总本周每日异动板块记录
- 统计出现频次、平均涨幅、累计净流入
- 结合本周消息催化，生成文字复盘
- 基于当周规律 + 最新消息，预测下周可能活跃的板块方向
"""
import json
import logging
import time
import threading
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from news_fetcher import get_all_news

logger = logging.getLogger(__name__)


# ── 分析每日历史文件，汇总本周数据 ─────────────────────────────────

def _week_date_range(ref_date: date = None):
    """返回本周（或指定日期所在周）的周一到周五日期列表"""
    if ref_date is None:
        ref_date = date.today()
    # 找到本周一（weekday=0）
    monday = ref_date - timedelta(days=ref_date.weekday())
    return [monday + timedelta(days=i) for i in range(5)]  # 周一到周五


def load_week_history(data_dir: Path, ref_date: date = None) -> Dict[str, List[Dict]]:
    """
    加载本周每日的 history_YYYYMMDD.json 文件。
    返回：{sector_code: [alert_dict, ...]}  —— 同一板块可能本周多天都有记录
    """
    dates = _week_date_range(ref_date)
    weekly: Dict[str, List[Dict]] = defaultdict(list)

    for d in dates:
        fname = data_dir / f"history_{d.strftime('%Y%m%d')}.json"
        if not fname.exists():
            continue
        try:
            with open(fname, "r", encoding="utf-8") as f:
                raw = json.load(f)
            history = raw.get("history", {})
            for code, alert in history.items():
                if alert:  # 跳过空记录（收盘清零后的空文件）
                    # 记录是哪一天的
                    alert = dict(alert)
                    alert["_date"] = d.strftime("%Y-%m-%d")
                    weekly[code].append(alert)
        except Exception as e:
            logger.warning(f"读取 {fname.name} 失败: {e}")

    return dict(weekly)


def summarize_weekly(weekly: Dict[str, List[Dict]]) -> List[Dict]:
    """
    对每个板块的多天记录做统计，返回排序后的板块摘要列表。
    每条摘要含：
      sector_name, sector_code, days（异动天数），
      avg_change_pct（平均触发时涨幅），
      max_change_pct（最高涨幅），
      total_inflow（累计净流入，元），
      dates（哪几天），
      all_news（收集到的相关新闻，去重）
    """
    summaries = []
    for code, records in weekly.items():
        name = records[0].get("sector_name", code)
        days = len(set(r.get("_date", "") for r in records))
        change_pcts = [r.get("trigger_change_pct", r.get("change_pct", 0)) for r in records]
        avg_chg = sum(change_pcts) / len(change_pcts) if change_pcts else 0
        max_chg = max(change_pcts) if change_pcts else 0
        total_inflow = sum(r.get("trigger_inflow", r.get("net_inflow", 0)) for r in records)
        all_dates = sorted(set(r.get("_date", "") for r in records if r.get("_date")))
        # 合并所有天的新闻（去重）
        news_seen = set()
        all_news_list = []
        for r in records:
            for n in r.get("news", []):
                key = n.get("title", "")[:30]
                if key and key not in news_seen:
                    news_seen.add(key)
                    all_news_list.append(n)

        summaries.append({
            "sector_name": name,
            "sector_code": code,
            "days": days,
            "avg_change_pct": round(avg_chg, 2),
            "max_change_pct": round(max_chg, 2),
            "total_inflow": total_inflow,
            "dates": all_dates,
            "news": all_news_list[:5],
        })

    # 按异动天数降序，同天数按平均涨幅降序
    summaries.sort(key=lambda x: (-x["days"], -x["avg_change_pct"]))
    return summaries


# ── 复盘文本生成 ─────────────────────────────────────────────────

def _fmt_money(val: float) -> str:
    abs_val = abs(val)
    sign = "" if val >= 0 else "-"
    if abs_val >= 1e8:
        return f"{sign}{abs_val / 1e8:.1f}亿"
    if abs_val >= 1e4:
        return f"{sign}{abs_val / 1e4:.0f}万"
    return f"{sign}{abs_val:.0f}"


def generate_review_text(summaries: List[Dict], week_label: str) -> str:
    """生成纯文本的周复盘报告"""
    if not summaries:
        return f"# {week_label} 板块异动周报\n\n本周暂无异动记录。\n"

    lines = []
    lines.append(f"# {week_label} 板块异动周报")
    lines.append(f"\n> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"> 本周共 **{len(summaries)}** 个板块出现异动\n")

    # ── 一、本周热门板块排行 ─────────────────────────────────────
    lines.append("## 一、本周热门板块（按持续天数）\n")
    lines.append("| 板块 | 异动天数 | 最高涨幅 | 平均涨幅 | 累计净流入 | 日期 |")
    lines.append("|------|---------|---------|---------|-----------|------|")
    for s in summaries[:15]:
        days_str = f"{s['days']}天"
        dates_str = "、".join(d[5:] for d in s["dates"])  # 只显示月-日
        lines.append(
            f"| {s['sector_name']} | {days_str} | "
            f"+{s['max_change_pct']}% | "
            f"+{s['avg_change_pct']}% | "
            f"{_fmt_money(s['total_inflow'])} | "
            f"{dates_str} |"
        )

    # ── 二、板块详细复盘 ──────────────────────────────────────────
    lines.append("\n## 二、重点板块复盘\n")
    top_sectors = summaries[:8]  # 重点展示前8
    for i, s in enumerate(top_sectors, 1):
        lines.append(f"### {i}. {s['sector_name']}")
        lines.append(f"- **异动天数**：{s['days']}天（{', '.join(d[5:] for d in s['dates'])}）")
        lines.append(f"- **最高涨幅**：+{s['max_change_pct']}%  |  **平均涨幅**：+{s['avg_change_pct']}%")
        lines.append(f"- **累计净流入**：{_fmt_money(s['total_inflow'])}")
        if s["news"]:
            lines.append("- **相关催化**：")
            for n in s["news"][:3]:
                title = n.get("title", "")
                pub_time = n.get("pub_time", "")
                source = n.get("source", "")
                lines.append(f"  - [{source}] {title}（{pub_time[:10] if pub_time else ''}）")
        lines.append("")

    return "\n".join(lines)


# ── 下周预测文本生成 ─────────────────────────────────────────────

# 板块延续性评分规则（天数权重 + 涨幅权重）
def _momentum_score(s: Dict) -> float:
    """综合动量分：天数×40 + 最高涨幅×3 + 净流入归一"""
    inflow_score = min(s["total_inflow"] / 1e8, 10)  # 最多10分
    return s["days"] * 40 + s["max_change_pct"] * 3 + inflow_score


# 行业轮动规律库（基于历史经验）
_ROTATION_HINTS = {
    "人工智能":   "科技成长主线，强势后往往有2~3天调整再拉，关注消息催化（大模型/算力政策）",
    "半导体":     "与AI联动性强，美国限制/国产替代政策是核心催化",
    "机器人":     "政策驱动型，人形机器人量产节点是关键，注意板块间歇性轮动",
    "新能源汽车": "销量数据月初发布时有波动，特斯拉/比亚迪动态为催化",
    "光伏":       "装机数据+政策补贴驱动，关注硅料价格走势",
    "储能":       "与光伏、电力联动，大储订单为催化",
    "锂电池":     "碳酸锂价格是核心变量，注意产能过剩压制",
    "黄金":       "避险属性，关注美联储政策+地缘风险，强势时持续性好",
    "有色金属":   "铜价走势主导，关注美元指数+全球制造业PMI",
    "稀土":       "供给端调控预期是主要催化，波动较大",
    "军工":       "政策驱动为主，国防预算/重大装备列装为催化，上涨持续性中等",
    "医药":       "集采政策+创新药审批为主要催化，具体标的差异大",
    "银行":       "估值低位，政策护盘时有脉冲，但趋势性上涨需信贷数据改善",
    "房地产":     "政策松绑是核心催化，但基本面压制，反弹持续性差",
    "煤炭":       "季节性明显，供需数据驱动，关注保供政策",
    "航运":       "运费指数（SCFI/BDI）是领先指标，地缘冲突时有脉冲",
}


def generate_outlook_text(summaries: List[Dict], latest_news: List[Dict]) -> str:
    """
    生成下周行情展望，结合：
    1. 本周动量最强的板块（可能延续）
    2. 本周未出现但新闻催化强的板块（潜在机会）
    3. 行业轮动规律
    """
    lines = []
    lines.append("\n## 三、下周行情展望\n")

    if not summaries and not latest_news:
        lines.append("数据不足，暂无预测。\n")
        return "\n".join(lines)

    # 1. 本周强势延续预期
    lines.append("### 🔴 强势延续（本周高动量，下周关注分歧）\n")
    strong = sorted(summaries, key=_momentum_score, reverse=True)[:5]
    if strong:
        for s in strong:
            hint = _ROTATION_HINTS.get(s["sector_name"], "关注板块持续性与市场情绪")
            lines.append(f"**{s['sector_name']}**（本周{s['days']}天异动，最高+{s['max_change_pct']}%）")
            lines.append(f"> {hint}\n")
    else:
        lines.append("本周无持续性强势板块。\n")

    # 2. 消息催化的潜在板块
    lines.append("### 🟡 消息催化（新闻中的热点，关注启动机会）\n")
    # 从最新新闻中做关键词扫描，找本周没有异动过的方向
    existing_names = {s["sector_name"] for s in summaries}
    potential: Dict[str, List[str]] = defaultdict(list)
    for news_item in latest_news[:60]:
        title = news_item.get("title", "") + news_item.get("summary", "")
        for sector_kw, synonyms in _NEWS_KEYWORD_MAP.items():
            for kw in [sector_kw] + synonyms:
                if kw in title:
                    potential[sector_kw].append(news_item.get("title", "")[:50])
                    break

    # 剔除本周已经活跃过的，优先展示新方向
    new_potential = {k: v for k, v in potential.items() if k not in existing_names}
    show_potential = list(new_potential.items())[:4]
    if not show_potential:
        # 如果全都是本周已有的，那就展示新闻最多的几个
        show_potential = sorted(potential.items(), key=lambda x: -len(x[1]))[:4]

    if show_potential:
        for sector_kw, titles in show_potential:
            hint = _ROTATION_HINTS.get(sector_kw, "关注消息催化持续性")
            lines.append(f"**{sector_kw}**（本周新闻提及 {len(titles)} 次）")
            lines.append(f"> {hint}")
            for t in titles[:2]:
                lines.append(f"> - {t}")
            lines.append("")
    else:
        lines.append("当前新闻面暂无明显新方向。\n")

    # 3. 风险提示
    lines.append("### ⚠️ 风险提示\n")
    lines.append("- 以上预测基于历史规律和当前消息面，**不构成投资建议**")
    lines.append("- A股市场受政策影响大，重大会议/数据发布前后可能出现风格切换")
    lines.append("- 连续上涨板块注意高位分歧风险，建议结合量价形态综合判断\n")

    return "\n".join(lines)


# 用于新闻扫描的关键词映射（精简版，从 news_fetcher 引入思路）
_NEWS_KEYWORD_MAP: Dict[str, List[str]] = {
    "人工智能":   ["AI", "大模型", "DeepSeek", "算力", "智算", "大语言模型", "人形机器人"],
    "半导体":     ["芯片", "集成电路", "晶圆", "光刻", "存储", "先进封装", "HBM"],
    "机器人":     ["机器人", "人形机器人", "具身智能", "工业机器人"],
    "新能源汽车": ["新能源", "电动车", "智驾", "自动驾驶", "充电桩", "比亚迪", "特斯拉"],
    "光伏":       ["光伏", "太阳能", "组件", "硅料", "逆变器"],
    "储能":       ["储能", "固态电池", "液流电池", "大储"],
    "锂电池":     ["锂电", "动力电池", "正极材料", "碳酸锂", "宁德时代"],
    "黄金":       ["黄金", "贵金属", "金价", "避险", "COMEX"],
    "有色金属":   ["铜价", "铝价", "有色", "金属涨价"],
    "稀土":       ["稀土", "钕铁硼", "磁材", "稀土价格"],
    "军工":       ["军工", "国防", "军费", "武器装备", "国防预算"],
    "医药":       ["创新药", "集采", "生物医药", "CXO", "CDMO", "医保"],
    "银行":       ["LPR", "存款利率", "银行股", "信贷", "金融政策"],
    "房地产":     ["楼市", "收储", "保交楼", "限购", "房地产政策"],
    "煤炭":       ["煤炭", "动力煤", "焦煤", "煤价"],
    "黄金":       ["黄金", "金价", "避险资产"],
    "航运":       ["SCFI", "集运", "航运", "马士基", "运费"],
    "卫星通信":   ["卫星", "低轨卫星", "星链", "北斗", "卫星互联网"],
    "商业航天":   ["商业航天", "火箭", "卫星发射", "SpaceX"],
}


# ── 主入口：生成完整周报并保存 ──────────────────────────────────────

def generate_weekly_report(data_dir: Path, ref_date: date = None) -> Dict:
    """
    生成完整周报，保存到 data_dir/weekly_report_YYYYWNN.json 和 .md 文件。
    返回：{
        "week_label": str,
        "summaries": [...],
        "report_md": str,    # 复盘文本（Markdown）
        "outlook_md": str,   # 下周展望文本（Markdown）
        "generated_at": float,
    }
    """
    if ref_date is None:
        ref_date = date.today()

    # 计算周标签，例如 "2026年第13周（3/23~3/27）"
    monday = ref_date - timedelta(days=ref_date.weekday())
    friday = monday + timedelta(days=4)
    week_num = ref_date.isocalendar()[1]
    week_label = (
        f"{ref_date.year}年第{week_num}周"
        f"（{monday.month}/{monday.day}~{friday.month}/{friday.day}）"
    )

    logger.info(f"开始生成周报：{week_label}")

    # 1. 加载本周数据
    weekly = load_week_history(data_dir, ref_date)
    summaries = summarize_weekly(weekly)
    logger.info(f"本周共 {len(summaries)} 个板块有异动记录")

    # 2. 抓取最新消息（用于下周展望）
    latest_news = []
    try:
        latest_news = get_all_news()
        logger.info(f"获取到 {len(latest_news)} 条最新快讯（用于下周预测）")
    except Exception as e:
        logger.warning(f"抓取新闻失败，跳过消息面预测: {e}")

    # 3. 生成文本
    report_md = generate_review_text(summaries, week_label)
    outlook_md = generate_outlook_text(summaries, latest_news)
    full_md = report_md + outlook_md

    # 4. 保存文件
    week_key = f"{ref_date.year}W{week_num:02d}"
    json_file = data_dir / f"weekly_report_{week_key}.json"
    md_file = data_dir / f"weekly_report_{week_key}.md"

    result = {
        "week_label": week_label,
        "week_key": week_key,
        "summaries": summaries,
        "report_md": report_md,
        "outlook_md": outlook_md,
        "full_md": full_md,
        "generated_at": time.time(),
    }

    try:
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        with open(md_file, "w", encoding="utf-8") as f:
            f.write(full_md)
        logger.info(f"✅ 周报已保存：{md_file.name}")
    except Exception as e:
        logger.warning(f"保存周报失败: {e}")

    return result


def load_latest_weekly_report(data_dir: Path) -> Optional[Dict]:
    """加载最近一份周报（按文件名排序取最新）"""
    files = sorted(data_dir.glob("weekly_report_*.json"), reverse=True)
    if not files:
        return None
    try:
        with open(files[0], "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"读取周报失败: {e}")
        return None


def load_weekly_report_list(data_dir: Path) -> List[Dict]:
    """列出所有周报（只含 week_label / week_key / generated_at）"""
    result = []
    for f in sorted(data_dir.glob("weekly_report_*.json"), reverse=True):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                d = json.load(fp)
            result.append({
                "week_key": d.get("week_key", ""),
                "week_label": d.get("week_label", ""),
                "generated_at": d.get("generated_at", 0),
                "sector_count": len(d.get("summaries", [])),
            })
        except Exception:
            pass
    return result
