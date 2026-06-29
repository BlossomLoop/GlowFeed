"""博客模块：扫描 web/blog/*.html，从每篇 <head> 实时提取列表元数据。

发布即放文件：把一个完整、自带样式的 HTML 放进 web/blog/，文件名即 slug / URL，
随仓库 git 部署。无后台、无上传、无写接口、零落盘。

列表元数据全部来自每篇 HTML 的标准 <head> 标签：
  <title> 或 <meta name="title">（后者优先）—— 必填
  <meta name="date">         发布日期，缺省用文件修改时间
  <meta name="description">  一句话摘要
  <meta name="tags">         逗号分隔的标签
  <meta name="cover">        封面图（相对文件名自动补 /blog/ 前缀）

解析用标准库 html.parser 流式进行，读到 </head> 抛 _HeadDone 提前中断（不读正文）。
实时扫描、零缓存、零落盘、不进调度器（区别于 trending/skills 那种打外网才缓存的模块）。
"""
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

BLOG_DIR = Path(__file__).parent.parent / "web" / "blog"


class _HeadDone(Exception):
    """读到 </head> 即终止解析，不读正文。"""


class _HeadParser(HTMLParser):
    """只解析 <head>：收集 <title> 文本与各 <meta name=.. content=..>。"""

    def __init__(self):
        super().__init__()
        self.meta: dict = {}     # name(小写) -> content
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            a = dict(attrs)
            name = (a.get("name") or "").strip().lower()
            if name and a.get("content") is not None and name not in self.meta:
                self.meta[name] = a["content"]

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "head":
            raise _HeadDone

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def _resolve_cover(cover: str) -> str:
    """封面图地址：绝对路径 / 外链 / data-URI 原样返回，纯文件名补 /blog/ 前缀。"""
    if cover and not cover.startswith(("/", "http://", "https://", "data:")):
        return "/blog/" + cover
    return cover


def _post_meta(file: Path) -> dict:
    parser = _HeadParser()
    try:
        parser.feed(file.read_text(encoding="utf-8", errors="replace"))
    except _HeadDone:
        pass  # 正常提前中断：已读完 <head>
    m = parser.meta
    title = (m.get("title") or parser.title or file.stem).strip()
    post_date = (m.get("date") or "").strip()
    if not post_date:
        # 缺日期用文件修改时间兜底
        post_date = date.fromtimestamp(file.stat().st_mtime).isoformat()
    tags = [t.strip() for t in (m.get("tags") or "").split(",") if t.strip()]
    return {
        "slug": file.stem,
        "url": "/blog/" + file.name,
        "title": title,
        "date": post_date,
        "description": (m.get("description") or "").strip(),
        "tags": tags,
        "cover": _resolve_cover((m.get("cover") or "").strip()),
    }


def list_posts() -> list:
    """扫描博客目录，返回按日期倒序的元数据列表。目录不存在或文件不可读时跳过。"""
    if not BLOG_DIR.is_dir():
        return []
    posts = []
    for file in BLOG_DIR.glob("*.html"):
        try:
            posts.append(_post_meta(file))
        except OSError:
            continue
    posts.sort(key=lambda p: p["date"], reverse=True)
    return posts
