"""信息源注册表：全部免 API key。

两类源：
- search 型：用关键词直接检索（HN / GitHub / Bing / Reddit）
- feed 型：拉取热榜或 RSS 全量，再按关键词本地过滤（百度/微博/头条/B站/36氪/IT之家/少数派）

每个 fetcher 返回统一结构的 dict 列表：
  {title, url, summary, source, author, published_at(iso或None), engagement(int 热度)}
失败容忍：任何源抛错/超时返回 []，不影响其他源。
"""
import html
import http.cookiejar
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from . import http_util, store


def _item(title, url, source, summary="", author="", published_at=None, engagement=0):
    return {
        "title": html.unescape((title or "").strip()),
        "url": (url or "").strip(),
        "summary": html.unescape(re.sub(r"<[^>]+>", " ", summary or "")).strip()[:500],
        "source": source,
        "author": (author or "").strip(),
        "published_at": published_at,
        "engagement": int(engagement or 0),
    }


def _parse_rss(text: str, source: str, limit: int = 50):
    """通用 RSS 2.0 / Atom 解析。"""
    items = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return items
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    nodes = root.findall(".//item") or root.findall(".//atom:entry", ns)
    for node in nodes[:limit]:
        def f(tag):
            el = node.find(tag) if not tag.startswith("atom:") else node.find(tag, ns)
            return el.text if el is not None and el.text else ""

        link = f("link")
        if not link:
            el = node.find("atom:link", ns)
            link = el.get("href") if el is not None else ""
        pub = f("pubDate") or f("atom:updated") or f("atom:published")
        published = None
        if pub:
            try:
                published = parsedate_to_datetime(pub).astimezone(timezone.utc).isoformat()
            except (TypeError, ValueError):
                try:
                    published = datetime.fromisoformat(pub.replace("Z", "+00:00")).isoformat()
                except ValueError:
                    pass
        items.append(_item(
            title=f("title"), url=link, source=source,
            summary=f("description") or f("atom:summary") or f("atom:content"),
            author=f("author") or f("atom:author/atom:name"),
            published_at=published,
        ))
    return items


# ---------------- search 型源（关键词检索） ----------------

def fetch_hackernews(keyword: str, days: int = 7):
    since = int(time.time()) - days * 86400
    url = (
        "https://hn.algolia.com/api/v1/search?"
        f"query={http_util.quote(keyword)}&tags=story"
        f"&numericFilters=created_at_i>{since}&hitsPerPage=30"
    )
    data = http_util.get_json(url)
    if not data:
        return []
    out = []
    for h in data.get("hits", []):
        link = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        out.append(_item(
            title=h.get("title"), url=link, source="hackernews",
            summary=(h.get("story_text") or "")[:300], author=h.get("author"),
            published_at=h.get("created_at"),
            engagement=(h.get("points") or 0) + (h.get("num_comments") or 0),
        ))
    return out


def fetch_github(keyword: str, days: int = 7):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    q = http_util.quote(f"{keyword} pushed:>{since}")
    url = f"https://api.github.com/search/repositories?q={q}&sort=stars&order=desc&per_page=20"
    data = http_util.get_json(url, headers={"Accept": "application/vnd.github+json"})
    if not data:
        return []
    out = []
    for r in data.get("items", []):
        out.append(_item(
            title=f"{r.get('full_name')}: {(r.get('description') or '')[:80]}",
            url=r.get("html_url"), source="github",
            summary=r.get("description") or "",
            author=(r.get("owner") or {}).get("login"),
            published_at=r.get("pushed_at"),
            engagement=r.get("stargazers_count") or 0,
        ))
    return out


# Bing SERP 里的词典/百科/翻译挂件域名：英文词查询（best/top 等）常被其劫持，
# 整页自然结果退化成单词释义，对资讯/口碑均无用，解析时剔除。
_BING_NOISE_HOSTS = (
    "iciba.com", "esdict.cn", "youdao.com", "dict.cn", "hujiang.com",
    "dictionary.cambridge.org", "baike.baidu.com", "cp.baidu.com",
    "lingolandedu.com", "merriam-webster.com", "collinsdictionary.com",
)


def _host_of(url: str) -> str:
    """取 url 主机名（小写、去前导 www.），失败返空。"""
    m = re.match(r"https?://([^/]+)", url or "")
    host = (m.group(1) if m else "").lower()
    return host[4:] if host.startswith("www.") else host


def _parse_bing_html(text: str, limit: int = 20) -> list[dict]:
    """解析 Bing 网页 SERP 的自然结果（b_algo 区块）。纯函数（喂夹具可离线测）。

    标题在 <h2><a href=URL>TITLE</a>，摘要在 <div class="b_caption"><p>。
    剔除词典/百科挂件域名（见 _BING_NOISE_HOSTS）。无结果/解析失败返 []。"""
    out = []
    for block in (text or "").split('class="b_algo"')[1:]:
        block = re.sub(r"<link[^>]*>", "", block)  # 区块内夹带的 CSS link 会干扰 href 匹配
        m = re.search(r'<h2[^>]*>\s*<a[^>]*\bhref="(https?://[^"]+)"[^>]*>(.*?)</a>',
                      block, re.DOTALL)
        if not m:
            continue
        url = m.group(1)
        host = _host_of(url)
        if not host or any(host == n or host.endswith("." + n) for n in _BING_NOISE_HOSTS):
            continue
        sm = re.search(r'class="b_caption">.*?<p[^>]*>(.*?)</p>', block, re.DOTALL)
        out.append(_item(
            title=re.sub(r"<[^>]+>", "", m.group(2)), url=url, source="bing",
            summary=sm.group(1) if sm else "",
        ))
        if len(out) >= limit:
            break
    return out


def fetch_bing(keyword: str, days: int = 7):
    # 解析网页 SERP 的自然结果。format=rss 端点已退化为只返词典/即时答案卡片，弃用。
    # 用英文 UI（mkt=en-US）召回更全的开发者文章；解析阶段剔除词典/百科挂件域名。
    url = (f"https://www.bing.com/search?q={http_util.quote(keyword)}"
           f"&count=20&setlang=en&mkt=en-US")
    text = http_util.get(url, timeout=12)
    return _parse_bing_html(text) if text else []


def fetch_reddit(keyword: str, days: int = 7):
    # Reddit 国内直连不通，http_util 会自动回退到本地代理（如有）
    t = "week" if days <= 7 else "month"
    url = f"https://www.reddit.com/search.rss?q={http_util.quote(keyword)}&sort=relevance&t={t}&limit=30"
    text = http_util.get(url, via_proxy=True, timeout=15)
    return _parse_rss(text, "reddit") if text else []


# ---------------- feed 型源（热榜 / RSS，本地关键词过滤） ----------------

def fetch_baidu_hot(_keyword=None, days=None):
    url = "https://top.baidu.com/api/board?platform=wise&tab=realtime"
    data = http_util.get_json(url)
    if not data or not data.get("success"):
        return []
    out = []
    for card in data.get("data", {}).get("cards", []):
        for grp in card.get("content", []):
            rows = grp if isinstance(grp, list) else grp.get("content", [grp])
            for it in rows:
                if not isinstance(it, dict) or not it.get("word"):
                    continue
                out.append(_item(
                    title=it.get("word"), url=it.get("url") or it.get("rawUrl"),
                    source="baidu_hot", summary=it.get("desc") or "",
                    engagement=int(it.get("hotScore") or 0),
                ))
    return out


_weibo_jar = None
_weibo_jar_ts = 0.0


def fetch_weibo_hot(_keyword=None, days=None):
    # 微博热搜需要先访问主页种 visitor cookie，cookie 缓存 30 分钟
    global _weibo_jar, _weibo_jar_ts
    if _weibo_jar is None or time.time() - _weibo_jar_ts > 1800:
        jar = http.cookiejar.CookieJar()
        http_util.get("https://weibo.com", cookie_jar=jar, timeout=8)
        _weibo_jar, _weibo_jar_ts = jar, time.time()
    data = http_util.get_json(
        "https://weibo.com/ajax/side/hotSearch",
        headers={"Referer": "https://weibo.com/"}, cookie_jar=_weibo_jar,
    )
    if not data or data.get("ok") != 1:
        _weibo_jar = None  # cookie 失效，下次重新种
        return []
    out = []
    for it in data.get("data", {}).get("realtime", []):
        word = it.get("word")
        if not word or it.get("is_ad"):
            continue
        out.append(_item(
            title=word,
            url=f"https://s.weibo.com/weibo?q={http_util.quote('#' + word + '#')}",
            source="weibo_hot", summary=it.get("note") or "",
            engagement=int(it.get("num") or it.get("raw_hot") or 0),
        ))
    return out


def fetch_toutiao_hot(_keyword=None, days=None):
    data = http_util.get_json("https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc")
    if not data:
        return []
    return [
        _item(title=it.get("Title"), url=it.get("Url"), source="toutiao_hot",
              engagement=int(it.get("HotValue") or 0))
        for it in data.get("data", []) if it.get("Title")
    ]


def fetch_bilibili_pop(_keyword=None, days=None):
    data = http_util.get_json("https://api.bilibili.com/x/web-interface/popular?ps=50")
    if not data or data.get("code") != 0:
        return []
    out = []
    for v in data.get("data", {}).get("list", []):
        stat = v.get("stat") or {}
        out.append(_item(
            title=v.get("title"), url=v.get("short_link_v2") or f"https://www.bilibili.com/video/{v.get('bvid')}",
            source="bilibili", summary=v.get("desc") or "",
            author=(v.get("owner") or {}).get("name"),
            published_at=datetime.fromtimestamp(v.get("pubdate", 0), tz=timezone.utc).isoformat() if v.get("pubdate") else None,
            engagement=stat.get("view") or 0,
        ))
    return out


def _rss_source(url, source_id):
    def fetch(_keyword=None, days=None):
        text = http_util.get(url, timeout=12)
        return _parse_rss(text, source_id) if text else []
    return fetch


fetch_36kr = _rss_source("https://36kr.com/feed", "36kr")
fetch_ithome = _rss_source("https://www.ithome.com/rss/", "ithome")
fetch_sspai = _rss_source("https://sspai.com/feed", "sspai")


# GitHub 趋势榜：GitHub 无官方 trending API，用 OSSInsight（免 key，基于 GH Archive 事件算趋势分，
# 本机直连可用）。**不作为任务来源**（英文仓库会被任务的中文关键词过滤光），改由独立「趋势」专页
# 直接调用 github_trending_list()，拿到不经关键词过滤的干净榜单。
_TREND_CACHE: dict = {}     # (api_period, language) -> rows；快照式：仅 force（重启/每日08:30）时真拉，平时读缓存
_TREND_FETCHED: dict = {}   # (api_period, language) -> 真拉成功的 UTC 时间戳，供前端显示真实刷新时间
_TREND_PERIODS = {"today": "past_24_hours", "week": "past_week", "month": "past_month"}


def trending_fetched_at(period: str = "today", language: str = "All") -> str | None:
    """返回该榜单快照的真实抓取时间（store.now() 格式 UTC 串）；从未拉过则 None。"""
    return _TREND_FETCHED.get((_TREND_PERIODS.get(period, "past_24_hours"), language))


def github_trending_list(period: str = "today", language: str = "All",
                         force: bool = False) -> list[dict]:
    """拉 OSSInsight 趋势仓库，返回榜单视图用富结构。

    快照式缓存：有缓存就直接返回（不按时间过期），只有 force=True（重启预热 /
    每日 08:30 定时刷新）才真正请求 OSSInsight。period: today/week/month；
    language: All 或具体编程语言。失败/空返回 []（但若已有旧快照则保留旧的）。"""
    api_period = _TREND_PERIODS.get(period, "past_24_hours")
    key = (api_period, language)
    hit = _TREND_CACHE.get(key)
    if hit is not None and not force:
        return hit
    url = (f"https://api.ossinsight.io/v1/trends/repos/"
           f"?period={api_period}&language={http_util.quote(language)}")
    data = http_util.get_json(url, timeout=15)
    rows = (data or {}).get("data", {}).get("rows", [])
    out = []
    for r in rows:
        name = (r.get("repo_name") or "").strip()
        if not name:
            continue
        logins = r.get("contributor_logins")
        contribs = (logins if isinstance(logins, list)
                    else [s for s in str(logins or "").split(",") if s.strip()])
        out.append({
            "rank": len(out) + 1,
            "name": name,
            "url": f"https://github.com/{name}",
            "language": (r.get("primary_language") or "").strip(),
            "description": (r.get("description") or "").strip(),
            "stars": int(r.get("stars") or 0),
            "forks": int(r.get("forks") or 0),
            "score": round(float(r.get("total_score") or 0), 1),
            "contributors": [c.strip() for c in contribs[:3]],
        })
    if out:
        _TREND_CACHE[key] = out
        _TREND_FETCHED[key] = store.now()   # 记录本次真拉时刻，前端据此显示真实刷新时间
    elif hit is not None:
        return hit            # 这次拉空但有旧快照 → 保留旧的，别把榜单清空
    return out


def warm_trending(periods=("today", "week", "month"), language: str = "All") -> None:
    """强制刷新趋势快照。重启预热与每日 08:30 定时各调一次（force=True 真拉 OSSInsight）。"""
    for p in periods:
        try:
            n = len(github_trending_list(p, language, force=True))
            print(f"[trending] 刷新 {p}: {n} 条", flush=True)
        except Exception as e:
            print(f"[trending] 刷新 {p} 失败: {e}", flush=True)


# ---------------- 注册表 ----------------
# kind: search=关键词检索 / feed=全量拉取后本地过滤
# quality: 信噪比先验（参考 last30days signals.py），参与最终排序
SOURCES = {
    "hackernews":  {"name": "Hacker News", "region": "国外", "kind": "search", "quality": 0.85, "fn": fetch_hackernews},
    "github":      {"name": "GitHub",      "region": "国外", "kind": "search", "quality": 0.80, "fn": fetch_github},
    "bing":        {"name": "Bing 网页",   "region": "国内外", "kind": "search", "quality": 0.70, "fn": fetch_bing},
    "reddit":      {"name": "Reddit",      "region": "国外", "kind": "search", "quality": 0.65, "fn": fetch_reddit},
    "baidu_hot":   {"name": "百度热搜",    "region": "国内", "kind": "feed",   "quality": 0.55, "fn": fetch_baidu_hot},
    "weibo_hot":   {"name": "微博热搜",    "region": "国内", "kind": "feed",   "quality": 0.55, "fn": fetch_weibo_hot},
    "toutiao_hot": {"name": "头条热榜",    "region": "国内", "kind": "feed",   "quality": 0.55, "fn": fetch_toutiao_hot},
    "bilibili":    {"name": "B站热门",     "region": "国内", "kind": "feed",   "quality": 0.55, "fn": fetch_bilibili_pop},
    "36kr":        {"name": "36氪",        "region": "国内", "kind": "feed",   "quality": 0.75, "fn": fetch_36kr},
    "ithome":      {"name": "IT之家",      "region": "国内", "kind": "feed",   "quality": 0.70, "fn": fetch_ithome},
    "sspai":       {"name": "少数派",      "region": "国内", "kind": "feed",   "quality": 0.75, "fn": fetch_sspai},
}
