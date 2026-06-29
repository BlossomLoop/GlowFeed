"""博客模块测试：head 元数据提取、缺省兜底、日期倒序、封面前缀、提前中断。
全部离线（tempfile 造 HTML + monkeypatch BLOG_DIR）。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import blog


def _write(d: Path, name: str, head: str, body: str = "<p>正文</p>") -> Path:
    f = d / name
    f.write_text(f"<!DOCTYPE html><html><head>{head}</head><body>{body}</body></html>",
                 encoding="utf-8")
    return f


class TestBlog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._orig = blog.BLOG_DIR
        blog.BLOG_DIR = self.dir

    def tearDown(self):
        blog.BLOG_DIR = self._orig
        self.tmp.cleanup()

    def test_full_metadata(self):
        _write(self.dir, "post-a.html",
               '<title>标题甲</title>'
               '<meta name="date" content="2026-06-20">'
               '<meta name="description" content="一句话摘要">'
               '<meta name="tags" content="标签A, 标签B ,, 标签C">'
               '<meta name="cover" content="a.svg">')
        posts = blog.list_posts()
        self.assertEqual(len(posts), 1)
        p = posts[0]
        self.assertEqual(p["title"], "标题甲")
        self.assertEqual(p["slug"], "post-a")
        self.assertEqual(p["url"], "/blog/post-a.html")
        self.assertEqual(p["date"], "2026-06-20")
        self.assertEqual(p["description"], "一句话摘要")
        self.assertEqual(p["tags"], ["标签A", "标签B", "标签C"])  # 空项被过滤
        self.assertEqual(p["cover"], "/blog/a.svg")              # 纯文件名补前缀

    def test_meta_title_preferred_over_tag(self):
        _write(self.dir, "p.html",
               '<title>标签标题</title><meta name="title" content="Meta 标题">')
        self.assertEqual(blog.list_posts()[0]["title"], "Meta 标题")

    def test_title_fallback_to_filename(self):
        _write(self.dir, "no-title.html", '<meta name="date" content="2026-01-01">')
        self.assertEqual(blog.list_posts()[0]["title"], "no-title")

    def test_date_fallback_to_mtime(self):
        f = _write(self.dir, "no-date.html", "<title>无日期</title>")
        # 设成已知 mtime（2021-09-09 左右）便于断言格式
        ts = time.mktime((2021, 9, 9, 12, 0, 0, 0, 0, -1))
        os.utime(f, (ts, ts))
        self.assertEqual(blog.list_posts()[0]["date"], "2021-09-09")

    def test_absolute_and_external_cover_untouched(self):
        _write(self.dir, "abs.html", '<title>X</title><meta name="cover" content="/img/x.png">')
        _write(self.dir, "ext.html", '<title>Y</title><meta name="cover" content="https://e/x.png">')
        covers = {p["slug"]: p["cover"] for p in blog.list_posts()}
        self.assertEqual(covers["abs"], "/img/x.png")
        self.assertEqual(covers["ext"], "https://e/x.png")

    def test_sorted_by_date_desc(self):
        _write(self.dir, "old.html", '<title>旧</title><meta name="date" content="2026-01-01">')
        _write(self.dir, "new.html", '<title>新</title><meta name="date" content="2026-12-31">')
        slugs = [p["slug"] for p in blog.list_posts()]
        self.assertEqual(slugs, ["new", "old"])

    def test_body_not_parsed(self):
        # 正文里的 <title>/<meta> 不应污染元数据（读到 </head> 即中断）
        _write(self.dir, "p.html", "<title>真标题</title>",
               body='<title>假标题</title><meta name="tags" content="不该出现">')
        p = blog.list_posts()[0]
        self.assertEqual(p["title"], "真标题")
        self.assertEqual(p["tags"], [])

    def test_missing_dir_returns_empty(self):
        blog.BLOG_DIR = self.dir / "nope"
        self.assertEqual(blog.list_posts(), [])


if __name__ == "__main__":
    unittest.main()
