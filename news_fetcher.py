"""
消息面抓取模块
数据来源（按优先级）：
  1. 东方财富 快讯 (column=294)
  2. 东方财富 财经要闻 (column=291)
  3. 同花顺 7x24 快讯
  4. 财联社 电报
  5. 新浪财经 直播快讯
  6. 通达信快讯（腾讯行情财经滚动资讯）
多路并发抓取，任一失败不影响其余，结果合并去重。
"""
import re
import time
import logging
import threading
import requests
from typing import List, Dict

logger = logging.getLogger(__name__)

# ── 公共 Headers ──────────────────────────────────────────────
_BASE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

def _make_session(referer: str = "https://finance.eastmoney.com/") -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": _BASE_UA,
        "Accept": "application/json, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": referer,
        "Connection": "keep-alive",
    })
    return s

# 每个来源独立 Session，互不影响
_EM_SESSION  = _make_session("https://data.eastmoney.com/")
_THS_SESSION = _make_session("https://news.10jqka.com.cn/")
_CLS_SESSION = _make_session("https://www.cls.cn/telegraph")
_SINA_SESSION= _make_session("https://finance.sina.com.cn/")
_TDX_SESSION = _make_session("https://www.tdx.com.cn/")   # 通达信（腾讯行情服务）

# ── 条数配置 ─────────────────────────────────────────────────
EM_NEWS_COUNT  = 50
THS_NEWS_COUNT = 30
CLS_NEWS_COUNT = 30
SINA_NEWS_COUNT= 30
TDX_NEWS_COUNT = 30   # 通达信快讯条数

# ── 工具函数 ─────────────────────────────────────────────────
def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", str(text or "")).strip()


# ══════════════════════════════════════════════════════════════
# 来源 1：东方财富快讯
# ══════════════════════════════════════════════════════════════
def get_em_flash_news(limit: int = EM_NEWS_COUNT) -> List[Dict]:
    """
    东方财富 7x24 快讯（column=294）+ 财经要闻（column=291）
    参数名是 column（不是 columns）
    """
    results: List[Dict] = []
    for column, label in [("294", "东财快讯"), ("291", "东财要闻")]:
        url = (
            "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
            f"?client=web&biz=web_stock&column={column}"
            f"&order=1&page=1&pageSize={limit}&req_trace="
        )
        try:
            resp = _EM_SESSION.get(url, timeout=10)
            resp.raise_for_status()
            resp.encoding = "utf-8"
            data = resp.json()
            raw = data.get("data") or {}
            if not isinstance(raw, dict):
                continue
            items = raw.get("list") or raw.get("data") or []
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                title = _strip_html(item.get("title", ""))
                summary = _strip_html(item.get("digest") or item.get("summary") or "")
                pub_time = str(item.get("showTime") or item.get("mTime") or "")
                art_url = str(item.get("uniqueUrl") or item.get("url") or "")
                if title:
                    results.append({
                        "title": title,
                        "summary": summary[:120],
                        "pub_time": pub_time,
                        "url": art_url,
                        "source": label,
                    })
        except Exception as e:
            logger.debug(f"{label} 获取失败: {e}")
    if not results:
        logger.warning("东方财富快讯（主）全部失败")
    return results


# ══════════════════════════════════════════════════════════════
# 来源 2：同花顺 7x24 快讯
# ══════════════════════════════════════════════════════════════
def get_ths_flash_news(limit: int = THS_NEWS_COUNT) -> List[Dict]:
    """同花顺 7x24 快讯"""
    url = (
        "https://news.10jqka.com.cn/tapp/news/push/stock/"
        f"?page=1&tag=&track=website&pagesize={limit}"
    )
    news: List[Dict] = []
    try:
        resp = _THS_SESSION.get(url, timeout=10)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        data = resp.json()
        raw = data.get("data") or {}
        if not isinstance(raw, dict):
            raw = {}
        items = raw.get("list") or []
        if not isinstance(items, list):
            items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            title = _strip_html(item.get("title") or "")
            summary = _strip_html(item.get("digest") or item.get("summary") or "")
            pub_time = str(item.get("ctime") or "")
            if title:
                news.append({
                    "title": title,
                    "summary": summary[:120],
                    "pub_time": pub_time,
                    "url": str(item.get("url") or ""),
                    "source": "同花顺快讯",
                })
    except Exception as e:
        logger.warning(f"同花顺快讯获取失败: {e}")
    return news


# ══════════════════════════════════════════════════════════════
# 来源 3：财联社电报
# ══════════════════════════════════════════════════════════════
def get_cls_flash_news(limit: int = CLS_NEWS_COUNT) -> List[Dict]:
    """财联社 7x24 电报"""
    url = (
        "https://www.cls.cn/nodeapi/updateTelegraphList"
        f"?app=CLS&sv=7.7.5&os=web&rn={limit}&refresh_type=1&last_time=0"
    )
    news: List[Dict] = []
    try:
        resp = _CLS_SESSION.get(url, timeout=10)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        data = resp.json()
        # 找列表：roll_data / data / list
        items = None
        for key in ("roll_data", "data", "list"):
            v = data.get(key)
            if isinstance(v, list) and v:
                items = v
                break
            if isinstance(v, dict):
                for k2 in ("roll_data", "list", "data"):
                    v2 = v.get(k2)
                    if isinstance(v2, list) and v2:
                        items = v2
                        break
            if items:
                break
        if not items:
            logger.debug(f"财联社返回结构未识别: {list(data.keys())[:6]}")
            return news
        for item in items:
            if not isinstance(item, dict):
                continue
            title = _strip_html(item.get("title") or item.get("brief") or "")
            content = _strip_html(item.get("content") or "")
            pub_time = str(item.get("ctime") or item.get("modified_time") or "")
            if not title and content:
                title = content[:60]
            if title:
                news.append({
                    "title": title,
                    "summary": content[:120],
                    "pub_time": pub_time,
                    "url": "",
                    "source": "财联社电报",
                })
    except Exception as e:
        logger.warning(f"财联社快讯获取失败: {e}")
    return news


# ══════════════════════════════════════════════════════════════
# 来源 4：新浪财经直播快讯
# ══════════════════════════════════════════════════════════════
def get_sina_flash_news(limit: int = SINA_NEWS_COUNT) -> List[Dict]:
    """新浪财经 7x24 直播间快讯（zhibo_id=152 = 财经）"""
    url = (
        "https://zhibo.sina.com.cn/api/zhibo/feed"
        f"?zhibo_id=152&page=1&page_size={limit}&tag_id=0&dire=f&dpc=1"
    )
    news: List[Dict] = []
    try:
        resp = _SINA_SESSION.get(url, timeout=10)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        data = resp.json()
        result = data.get("result") or {}
        if not isinstance(result, dict):
            result = {}
        items = result.get("data", {})
        if isinstance(items, dict):
            items = items.get("feed") or items.get("list") or []
        if not isinstance(items, list):
            items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            title = _strip_html(item.get("rich_text") or item.get("content") or "")
            pub_time = str(item.get("created_at") or "")
            if title and len(title) > 5:
                news.append({
                    "title": title[:100],
                    "summary": "",
                    "pub_time": pub_time,
                    "url": "",
                    "source": "新浪财经快讯",
                })
    except Exception as e:
        logger.warning(f"新浪财经快讯获取失败: {e}")
    return news


# ══════════════════════════════════════════════════════════════
# 来源 5：通达信快讯（腾讯行情财经滚动资讯）
# ══════════════════════════════════════════════════════════════
def get_tdx_flash_news(limit: int = TDX_NEWS_COUNT) -> List[Dict]:
    """
    通达信快讯 —— 通达信行情底层依托腾讯行情服务。
    使用新浪 feed.mix.sina.com.cn 股市频道（lid=2516 = 股市要闻）
    作为通达信资讯终端同步的财经资讯流。
    lid 说明：
      2516 = 股市要闻（A股资讯）
      2514 = 基金要闻
      2513 = 债券要闻
    """
    url = (
        "https://feed.mix.sina.com.cn/api/roll/get"
        f"?pageid=153&lid=2516&num={limit}&page=1&callback="
    )
    news: List[Dict] = []
    try:
        resp = _TDX_SESSION.get(url, timeout=10)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        data = resp.json()
        items = data.get("result", {}).get("data", [])
        if not isinstance(items, list):
            items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            title = _strip_html(item.get("title") or "")
            summary = _strip_html(item.get("intro") or item.get("summary") or "")
            # ctime 是 Unix 时间戳（整数）
            ctime_raw = item.get("ctime") or item.get("intime") or ""
            try:
                import datetime as _dt
                ts = int(ctime_raw)
                pub_time = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pub_time = str(ctime_raw)
            art_url = str(item.get("url") or "")
            if title and len(title) > 5:
                news.append({
                    "title": title[:120],
                    "summary": summary[:120],
                    "pub_time": pub_time,
                    "url": art_url,
                    "source": "通达信快讯",
                })
    except Exception as e:
        logger.warning(f"通达信快讯获取失败: {e}")
    return news


# ══════════════════════════════════════════════════════════════
# 并发聚合
# ══════════════════════════════════════════════════════════════
def get_all_news() -> List[Dict]:
    """
    并发抓取所有来源，合并去重，任一失败不影响其余。
    至少有 2 个来源成功才返回有效结果，否则全部重试一次。
    """
    fetchers = [
        get_em_flash_news,
        get_ths_flash_news,
        get_cls_flash_news,
        get_sina_flash_news,
        get_tdx_flash_news,
    ]
    results_map: Dict[str, List[Dict]] = {}
    lock = threading.Lock()

    def _fetch(fn):
        try:
            items = fn()
            with lock:
                results_map[fn.__name__] = items
        except Exception as e:
            logger.debug(f"{fn.__name__} 并发异常: {e}")
            with lock:
                results_map[fn.__name__] = []

    threads = [threading.Thread(target=_fetch, args=(fn,), daemon=True) for fn in fetchers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    all_news: List[Dict] = []
    success_count = 0
    for fn in fetchers:
        items = results_map.get(fn.__name__, [])
        if items:
            success_count += 1
        all_news.extend(items)

    logger.info(f"快讯聚合完成: {success_count}/{len(fetchers)} 来源成功，共 {len(all_news)} 条")

    # 去重（按标题前30字）
    seen: set = set()
    deduped: List[Dict] = []
    for item in all_news:
        key = item.get("title", "")[:30]
        if key and key not in seen:
            seen.add(key)
            deduped.append(item)

    return deduped


# ══════════════════════════════════════════════════════════════
# 板块匹配
# ══════════════════════════════════════════════════════════════
def match_news_for_sector(sector_name: str, all_news: List[Dict],
                           max_items: int = 3) -> List[Dict]:
    """
    从快讯中匹配与指定板块相关的新闻。
    匹配策略：
      1. 精确包含板块名称关键词
      2. 包含板块内常见关键词（从板块名提取2字以上词）
    返回最多 max_items 条，最新优先。
    """
    keywords = _extract_keywords(sector_name)
    matched = []
    seen_titles: set = set()
    for news in all_news:
        title = news.get("title", "")
        summary = news.get("summary", "")
        text = title + summary
        for kw in keywords:
            if kw in text and title not in seen_titles:
                matched.append(news)
                seen_titles.add(title)
                break
    return matched[:max_items]


def _extract_keywords(sector_name: str) -> List[str]:
    """
    从板块名称中提取搜索关键词。
    例如 "新能源汽车" -> ["新能源汽车", "新能源", "汽车"]
         "半导体" -> ["半导体"]
         "人工智能" -> ["人工智能", "AI", "大模型"]
    """
    keywords = [sector_name]
    if len(sector_name) >= 4:
        keywords.append(sector_name[:len(sector_name) // 2 + 1])
    synonym_map = {
        # ── 科技 ──────────────────────────────────────────────
        "人工智能":   ["AI", "大模型", "GPT", "算力", "智算", "DeepSeek", "大语言模型"],
        "半导体":     ["芯片", "集成电路", "晶圆", "光刻机", "存储", "HBM", "先进封装"],
        "算力":       ["算力", "GPU", "AI芯片", "数据中心", "智算中心"],
        "云计算":     ["云计算", "云服务", "IaaS", "SaaS", "公有云"],
        "量子计算":   ["量子计算", "量子通信", "量子", "量子纠缠"],
        "卫星":       ["卫星", "低轨卫星", "星链", "卫星互联网", "遥感卫星", "北斗", "卫星通信"],
        "商业航天":   ["商业航天", "火箭", "卫星发射", "太空", "SpaceX", "航天发射"],
        "无人机":     ["无人机", "eVTOL", "低空经济", "飞行汽车", "通用航空"],
        "机器人":     ["机器人", "人形机器人", "工业机器人", "具身智能", "协作机器人"],
        # ── 新能源 ────────────────────────────────────────────
        "新能源汽车": ["新能源", "电动车", "比亚迪", "特斯拉", "智驾", "自动驾驶", "充电桩"],
        "光伏":       ["太阳能", "光伏", "组件", "硅料", "逆变器", "光伏装机"],
        "储能":       ["储能", "电池", "固态电池", "液流电池", "钠离子电池"],
        "锂电池":     ["锂电", "动力电池", "正极材料", "负极", "电解液", "隔膜", "宁德时代"],
        "风电":       ["风电", "风机", "海上风电", "风力发电"],
        "氢能":       ["氢能", "氢燃料", "燃料电池", "制氢", "绿氢"],
        # ── 资源/有色/金属 ────────────────────────────────────
        "有色金属":   ["有色", "铜", "铝", "锌", "镍", "锡", "铅", "有色金属"],
        "工业金属":   ["铜", "铝", "铁矿", "钢铁", "镍", "工业金属"],
        "黄金":       ["黄金", "贵金属", "金价", "黄金价格", "COMEX黄金", "避险"],
        "稀土":       ["稀土", "磁材", "永磁", "钕铁硼", "稀土价格"],
        "锂矿":       ["锂矿", "碳酸锂", "氢氧化锂", "锂资源", "锂盐"],
        "煤炭":       ["煤炭", "动力煤", "焦煤", "焦炭", "煤价", "煤炭价格"],
        "石油":       ["石油", "原油", "油价", "WTI", "布伦特", "OPEC", "天然气", "LNG", "管道气"],
        # ── 油气/能源（东财板块名常见形式）────────────────────
        "油气":       ["油气", "原油", "石油", "油价", "WTI", "布伦特", "OPEC",
                       "天然气", "LNG", "液化天然气", "气价", "中石油", "中石化",
                       "中海油", "中国海油", "能源股", "炼化", "炼油", "油田",
                       "页岩气", "非常规油气"],
        "天然气":     ["天然气", "LNG", "液化天然气", "气价", "管道气",
                       "气荒", "天然气价格", "城燃", "燃气"],
        "能源":       ["能源", "石油", "天然气", "原油", "油价", "煤炭",
                       "OPEC", "能源价格", "能源股", "炼化", "LNG"],
        "炼化":       ["炼化", "炼油", "石化", "化工品", "PX", "PTA", "乙烯", "丙烯"],
        "燃气":       ["燃气", "天然气", "LNG", "城燃", "煤气", "管道气"],
        "钢铁":       ["钢铁", "钢价", "螺纹钢", "热卷", "铁矿石"],
        # ── 军工/航天/安全 ────────────────────────────────────
        "军工":       ["军工", "国防", "航天", "兵器", "武器装备", "国防预算", "军费"],
        "网络安全":   ["网络安全", "信创", "数据安全", "防火墙", "等保"],
        "北斗":       ["北斗", "北斗导航", "北斗卫星", "卫星导航"],
        # ── 医药/医疗 ─────────────────────────────────────────
        "医药":       ["医药", "创新药", "生物医药", "CXO", "CDMO", "集采", "医保"],
        "医疗器械":   ["医疗器械", "手术机器人", "骨科", "心脏支架", "IVD"],
        "中药":       ["中药", "中成药", "中药材", "国中医"],
        # ── 金融 ─────────────────────────────────────────────
        "银行":       ["银行", "金融", "信贷", "存款利率", "LPR", "银行股"],
        "证券":       ["证券", "券商", "投行", "经纪", "融资融券"],
        "保险":       ["保险", "寿险", "财险", "险资"],
        # ── 地产/建筑 ─────────────────────────────────────────
        "房地产":     ["房地产", "地产", "楼市", "收储", "限购", "保交楼", "二手房"],
        "建筑":       ["建筑", "基建", "PPP", "城中村", "建设"],
        # ── 消费/零售 ─────────────────────────────────────────
        "消费":       ["消费", "零售", "内需", "以旧换新", "促消费"],
        "食品饮料":   ["食品", "白酒", "饮料", "茅台", "五粮液", "啤酒"],
        "白酒":       ["白酒", "茅台", "五粮液", "泸州老窖", "汾酒"],
        "电商":       ["电商", "直播电商", "跨境电商", "拼多多", "淘宝", "京东"],
        "旅游":       ["旅游", "酒店", "景区", "出境游", "航空"],
        # ── 交通/运输 ─────────────────────────────────────────
        "航运":       ["航运", "集运", "货轮", "班轮", "SCFI", "马士基", "中远海控"],
        "航空":       ["航空", "民航", "飞机", "机票", "客运量"],
        "物流":       ["物流", "快递", "顺丰", "菜鸟", "供应链"],
        # ── 农业/化工 ─────────────────────────────────────────
        "农业":       ["农业", "种子", "农药", "化肥", "粮食", "猪价"],
        "化工":       ["化工", "石化", "MDI", "化学品", "塑料"],
        # ── 传媒/游戏 ─────────────────────────────────────────
        "传媒":       ["传媒", "内容", "影视", "院线", "版权"],
        "游戏":       ["游戏", "手游", "网游", "腾讯游戏", "版号"],
        # ── 公用事业 ──────────────────────────────────────────
        "电力":       ["电力", "电网", "发电", "特高压", "电价"],
        "水务":       ["水务", "自来水", "污水处理"],
    }
    for key, synonyms in synonym_map.items():
        if key in sector_name:
            keywords.extend(synonyms)
    return list(dict.fromkeys(keywords))
