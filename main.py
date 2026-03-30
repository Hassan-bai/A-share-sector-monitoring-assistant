"""
板块异动监控主程序
工作日 09:15 - 15:00 自动检测板块异动（涨速+大单流入+消息面）
并通过浏览器弹出通知
"""
import json
import logging
import sys
import time
import webbrowser
import threading
from datetime import datetime, date, time as dtime
from pathlib import Path
from typing import Dict, List, Optional


def _data_dir() -> Path:
    """数据目录：打包后写用户主目录下的 sector_monitor_data/，开发时写当前目录"""
    if getattr(sys, "frozen", False):
        base = Path.home() / "sector_monitor_data"
    else:
        base = Path(__file__).parent / "sector_monitor_data"
    base.mkdir(exist_ok=True)
    return base


def _log_path() -> str:
    """打包后日志写到用户桌面旁的文档目录，避免写临时目录"""
    if getattr(sys, "frozen", False):
        base = Path.home() / "sector_monitor_logs"
        base.mkdir(exist_ok=True)
        return str(base / "sector_monitor.log")
    return "sector_monitor.log"


# ── 日志设置 ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_log_path(), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ── 本地模块 ──────────────────────────────────────────────────
from sector_data import get_sector_list, get_sector_stocks, get_sector_leaders
from news_fetcher import get_all_news, match_news_for_sector
from detector import detect_anomalies, format_alert_text, detect_market_dive, detect_buck_trend
from web_server import start_server, broadcast_alerts, broadcast_log, broadcast, set_connect_callback
from weekly_report import generate_weekly_report, load_latest_weekly_report, load_weekly_report_list

# ── 配置 ─────────────────────────────────────────────────────
WEB_PORT = 18888
# 扫描间隔（秒）
SCAN_INTERVAL = 60
# 同一板块两次告警最小间隔（秒），避免重复轰炸
ALERT_COOLDOWN = 300
# 开盘/收盘时间
MARKET_OPEN  = dtime(9, 15)
MARKET_CLOSE = dtime(15, 0)


class SectorMonitor:
    def __init__(self):
        self._last_alert: Dict[str, float] = {}   # code -> last alert timestamp
        # 历史异动记录：code -> alert dict（保留触发时的快照 + 持续更新资金）
        self._history: Dict[str, Dict] = {}
        # 持久化文件路径（按日期命名，防止跨日污染）
        self._history_file: Path = _data_dir() / f"history_{date.today().strftime('%Y%m%d')}.json"
        # 启动时加载当天历史（若已有数据则恢复）
        self._load_history()
        # ── 跳水状态（轮间持久）──────────────────────────────
        self._dive_state: Dict = {}     # detect_market_dive 上一轮的返回值
        self._dive_notified: bool = False  # 当前跳水段是否已发过一次"跳水啦"通知

    # ── 持久化 ────────────────────────────────────────────────
    def _load_history(self):
        """启动时加载当天历史异动记录"""
        if not self._history_file.exists():
            logger.info("未找到当天历史记录文件，从空白开始")
            return
        try:
            with open(self._history_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._history = data.get("history", {})
            self._last_alert = data.get("last_alert", {})
            logger.info(f"✅ 恢复历史异动记录：{len(self._history)} 个板块")
        except Exception as e:
            logger.warning(f"加载历史记录失败（将重新开始）: {e}")
            self._history = {}
            self._last_alert = {}

    def _save_history(self):
        """将历史异动记录持久化到磁盘（每天一个文件，周内累积保留）"""
        try:
            with open(self._history_file, "w", encoding="utf-8") as f:
                json.dump({
                    "history": self._history,
                    "last_alert": self._last_alert,
                    "saved_at": time.time(),
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存历史记录失败: {e}")

    def _maybe_switch_day_file(self):
        """若当前日期与文件日期不符，切换到新日期文件（不清空历史，继续当日新增）"""
        today_file = _data_dir() / f"history_{date.today().strftime('%Y%m%d')}.json"
        if today_file != self._history_file:
            logger.info(f"日期变更，切换持久化文件到 {today_file.name}（历史记录继续本日累积）")
            # 当天文件不存在时从空白开始（新的一天重新记录）
            self._history_file = today_file
            if today_file.exists():
                # 已有今天的文件（比如今天重启），加载它
                try:
                    with open(today_file, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    self._history = raw.get("history", {})
                    self._last_alert = raw.get("last_alert", {})
                    logger.info(f"加载今日已有记录 {len(self._history)} 条")
                except Exception:
                    self._history = {}
                    self._last_alert = {}
            else:
                # 新的一天，本日历史从空白开始
                self._history = {}
                self._last_alert = {}
                logger.info("新的一天，历史记录重新开始")

    def _generate_weekly_report_async(self):
        """在后台线程生成周报（周五收盘后触发，不阻塞主循环）"""
        def _do():
            try:
                broadcast_log("📊 周五收盘，正在生成本周复盘报告...")
                report = generate_weekly_report(_data_dir())
                label = report.get("week_label", "本周")
                sector_count = len(report.get("summaries", []))
                broadcast_log(f"✅ {label} 复盘报告已生成（{sector_count}个板块），可在「周报」中查看")
                # 推送给前端（type=weekly_report）
                broadcast({"type": "weekly_report", "report": report})
            except Exception as e:
                logger.error(f"生成周报失败: {e}", exc_info=True)
                broadcast_log(f"❌ 周报生成失败: {e}")
        threading.Thread(target=_do, daemon=True).start()

    # ── 时间判断 ──────────────────────────────────────────────
    @staticmethod
    def is_trading_time() -> bool:
        now = datetime.now()
        # 仅工作日
        if now.weekday() >= 5:
            return False
        t = now.time()
        return MARKET_OPEN <= t <= MARKET_CLOSE

    @staticmethod
    def seconds_to_market_open() -> int:
        """返回距下次开盘的秒数（今天或下周一）"""
        now = datetime.now()
        # 今天是工作日且还没开盘
        if now.weekday() < 5:
            open_today = now.replace(
                hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute,
                second=0, microsecond=0
            )
            if now < open_today:
                return int((open_today - now).total_seconds())
        # 计算下一个工作日
        days_ahead = 1
        while True:
            candidate = now.replace(
                hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute,
                second=0, microsecond=0
            )
            candidate = candidate.__class__(
                candidate.year, candidate.month, candidate.day,
                MARKET_OPEN.hour, MARKET_OPEN.minute
            )
            from datetime import timedelta
            candidate = now + timedelta(days=days_ahead)
            candidate = candidate.replace(
                hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute,
                second=0, microsecond=0
            )
            if candidate.weekday() < 5:
                return int((candidate - now).total_seconds())
            days_ahead += 1

    # ── 主扫描逻辑 ────────────────────────────────────────────
    def scan_once(self):
        logger.info("开始扫描板块数据...")
        broadcast_log("📡 开始扫描板块数据...")

        # 0. 日期切换检查（切换到今天的文件，每天单独累积）
        self._maybe_switch_day_file()

        # 1. 获取板块列表（包含所有板块的最新行情）
        sectors = get_sector_list()
        if not sectors:
            msg = "⚠️ 板块数据获取失败，跳过本次扫描"
            logger.warning(msg)
            broadcast_log(msg)
            return

        broadcast_log(f"✅ 获取到 {len(sectors)} 个板块")

        # 建立 code->行情 的快速查找表
        sector_map: Dict[str, Dict] = {s["code"]: s for s in sectors}

        now_ts = time.time()

        # 2. 刷新历史异动板块的实时资金数据（每轮都做）
        self._refresh_history(sector_map, now_ts)

        # 3. 异动检测
        anomalies = detect_anomalies(sectors)
        if not anomalies:
            broadcast_log("ℹ️ 暂无满足条件的新异动板块")
            # 即便没有新异动，也要把历史板块的最新资金推给前端
            self._push_updates()

        # ── 3b. 跳水检测（每轮都跑，与异动检测互不影响）────────────
        self._check_market_dive(sectors)

        if not anomalies:
            return

        broadcast_log(f"🚨 检测到 {len(anomalies)} 个异动板块，开始补充数据...")

        # 4. 过滤冷却期（只对「触发浏览器通知」做冷却，卡片本身会在历史里更新）
        to_notify = []
        for sec in anomalies:
            code = sec["code"]
            last = self._last_alert.get(code, 0)
            if now_ts - last < ALERT_COOLDOWN:
                broadcast_log(f"⏸️ {sec['name']} 冷却中（{int(ALERT_COOLDOWN - (now_ts - last))}s后可再报）")
                continue
            to_notify.append(sec)

        # 5. 获取消息面（有新异动才抓，省资源）
        all_news = get_all_news() if to_notify else []
        if to_notify:
            broadcast_log(f"📰 获取到 {len(all_news)} 条快讯")

        # 6. 补充详情并加入历史
        new_alerts = []
        for sec in to_notify:
            code = sec["code"]
            bk_type = sec.get("bk_type", 2)
            name = sec["name"]

            top_stocks = get_sector_stocks(code, bk_type, top_n=8)
            leaders = get_sector_leaders(code, top_n=5)
            news = match_news_for_sector(name, all_news, max_items=3)

            alert = {
                "sector_name": name,
                "sector_code": code,
                "bk_type": bk_type,
                # 触发时快照（不随后续更新变化）
                "trigger_change_pct": sec["change_pct"],
                "trigger_rise_speed": sec["rise_speed"],
                "trigger_inflow": sec["net_inflow"],
                "trigger_time": now_ts,
                # 实时数据（每轮刷新）
                "change_pct": sec["change_pct"],
                "rise_speed": sec["rise_speed"],
                "net_inflow": sec["net_inflow"],
                "top_stocks": top_stocks,
                "leaders": leaders,
                "news": news,
                "timestamp": now_ts,
                "is_new": True,   # 标记本轮是新触发
            }
            new_alerts.append(alert)
            self._history[code] = alert
            self._last_alert[code] = now_ts

            logger.info(format_alert_text({
                "name": name,
                "change_pct": sec["change_pct"],
                "rise_speed": sec["rise_speed"],
                "net_inflow": sec["net_inflow"],
            }))

        # 7. 推送：新异动单独广播（触发浏览器通知），然后推全量历史快照
        if new_alerts:
            broadcast_alerts(new_alerts)
            broadcast_log(f"✅ 已推送 {len(new_alerts)} 个新异动通知")
            self._save_history()   # 有新异动时立即持久化

        # 8. 无论如何推一次完整历史（带实时资金），前端据此刷新所有卡片
        self._push_updates()

    def _refresh_history(self, sector_map: Dict[str, Dict], now_ts: float):
        """用最新行情刷新历史异动板块的实时资金字段（只在收盘后才整体清空）"""
        for code, alert in self._history.items():
            latest = sector_map.get(code)
            if latest:
                alert["change_pct"] = latest.get("change_pct", alert["change_pct"])
                alert["rise_speed"] = latest.get("rise_speed", alert["rise_speed"])
                alert["net_inflow"] = latest.get("net_inflow", alert["net_inflow"])
                alert["timestamp"] = now_ts
                alert["is_new"] = False  # 非新触发

    def _push_updates(self):
        """推送全量历史异动板块的实时数据（前端据此刷新资金数字）"""
        if not self._history:
            return
        all_history = list(self._history.values())
        broadcast({"type": "update", "alerts": all_history})

    def _check_market_dive(self, sectors: List[Dict]):
        """
        全市跳水检测 + 旱地拔葱检测，每轮 scan 调用一次。
        检测结果通过 SSE 推送给前端。
        """
        dive = detect_market_dive(sectors, self._dive_state)
        self._dive_state = dive

        stats = dive["stats"]
        is_diving   = dive["is_diving"]
        just_started = dive["just_started"]
        just_ended   = dive["just_ended"]

        # 推送实时跳水状态给前端（每轮都推，前端据此更新状态栏）
        broadcast({
            "type":    "dive_status",
            "diving":  is_diving,
            "stats":   stats,
            "rounds":  dive.get("dive_rounds", 0),
        })

        if just_started:
            self._dive_notified = False
            stat = stats
            msg = (
                f"⚠️ 全市跳水！"
                f" 下跌板块 {stat['down_pct']*100:.0f}%"
                f" | 净流出板块 {stat['outflow_pct']*100:.0f}%"
                f" | 全市均涨 {stat['avg_change']:+.2f}%"
                f" | 主力净流出 {stat['total_outflow']/1e8:.1f}亿"
            )
            broadcast_log(f"🔴 {msg}")
            logger.warning(msg)

        if is_diving and not self._dive_notified:
            # 第一轮触发时推「跳水啦」通知（含流出最多板块）
            outflow_top = dive.get("outflow_sectors", [])
            broadcast({
                "type":            "dive_alert",
                "stats":           stats,
                "outflow_sectors": outflow_top,
                "dive_rounds":     dive.get("dive_rounds", 0),
            })
            self._dive_notified = True

        if just_ended:
            self._dive_notified = False
            broadcast_log("🟢 全市跳水已结束，市场情绪趋稳")
            broadcast({"type": "dive_end"})

        # 逆势偷涨检测（只在跳水中有效，门槛宽松：涨幅>0.2% 且资金净流入>0）
        if is_diving:
            bucks = detect_buck_trend(sectors, dive)
            if bucks:
                names = [f"{b['name']}(+{b.get('change_pct',0):.2f}%)" for b in bucks[:3]]
                broadcast_log(f"🌱 逆势偷涨: {' | '.join(names)}")
                mkt_avg = stats.get("avg_change", 0)
                broadcast({
                    "type":    "buck_alert",
                    "mkt_avg": mkt_avg,
                    "sectors": [
                        {
                            "name":       b["name"],
                            "code":       b.get("code", ""),
                            "bk_type":    b.get("bk_type", 2),
                            "change_pct": b.get("change_pct", 0),
                            "rise_speed": b.get("rise_speed", 0),
                            "net_inflow": b.get("net_inflow", 0),
                            "excess":     b.get("_excess", 0),
                        }
                        for b in bucks
                    ],
                })

    # ── 主循环 ────────────────────────────────────────────────
    def run(self):
        logger.info("═" * 50)
        logger.info("  板块异动监控启动")
        logger.info(f"  交易时间: 周一~五 09:15 - 15:00")
        logger.info(f"  扫描间隔: {SCAN_INTERVAL}秒")
        logger.info(f"  监控面板: http://127.0.0.1:{WEB_PORT}")
        if self._history:
            logger.info(f"  恢复 {len(self._history)} 个历史异动板块（今日持久化数据）")
        logger.info("═" * 50)

        # 标记收盘清理是否已执行（避免 15:00 后反复清）
        _cleared_today: set = set()

        while True:
            now = datetime.now()
            today_str = now.strftime("%Y%m%d")

        # ── 收盘后处理（每天存档；周五额外生成周报）──────────
            if (now.weekday() < 5
                    and now.time() >= MARKET_CLOSE
                    and today_str not in _cleared_today):
                if self._history:
                    # 每天收盘：存档当日（文件保留，不清空，下周一再换）
                    self._save_history()
                    logger.info(f"📴 今日收盘，异动记录已存档（{len(self._history)} 个板块）")
                    broadcast_log(f"📴 今日收盘，{len(self._history)} 个板块异动已存档")

                    # 周五：额外触发本周复盘+预测
                    if now.weekday() == 4:  # 周五
                        self._generate_weekly_report_async()

                _cleared_today.add(today_str)

            if not self.is_trading_time():
                wait = self.seconds_to_market_open()
                h, m = divmod(wait // 60, 60)
                logger.info(f"非交易时间，距下次开盘约 {h}小时{m}分钟，休眠等待...")
                broadcast_log(f"⏰ 非交易时间，距开盘约 {h}小时{m}分，等待中...")
                # 启动时若有历史数据，非交易时间也要推给前端（让页面刷新后能看到）
                if self._history:
                    self._push_updates()
                time.sleep(min(wait, 300))
                continue

            try:
                self.scan_once()
            except Exception as e:
                logger.error(f"扫描异常: {e}", exc_info=True)
                broadcast_log(f"❌ 扫描异常: {e}")

            time.sleep(SCAN_INTERVAL)


def main():
    # 1. 启动 Web 服务
    port = start_server(WEB_PORT)

    # 2. 自动打开浏览器
    url = f"http://127.0.0.1:{port}"
    logger.info(f"自动打开浏览器: {url}")
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    # 3. 启动监控主循环
    monitor = SectorMonitor()

    # 注册：每当有新浏览器连接时，立即推送当天历史快照
    set_connect_callback(monitor._push_updates)

    monitor.run()


if __name__ == "__main__":
    main()
