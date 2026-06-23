"""热门 Skill 采集层测试：解析 + 召回后过滤 + 集合判定 + 博客抽取两分支。

全部离线：GitHub 解析喂录制夹具（tests/fixtures/），博客抽取用注入的 llm_extract。
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import skills_sources as ss

FIX = Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class TestSkillSearchParse(unittest.TestCase):
    def setUp(self):
        self.data = _load("gh_skill_search.json")

    def test_filters_out_awesome_and_guide_noise(self):
        out = ss._parse_skill_search(self.data)
        names = {e["id"].lower() for e in out}
        # 召回里混的 best-practice / awesome / guide / list 类应被剔除
        for noisy in ("shanraisshan/claude-code-best-practice",
                      "voltagent/awesome-agent-skills",
                      "zebbern/claude-code-guide"):
            self.assertNotIn(noisy, names, f"{noisy} 应被召回后过滤剔除")

    def test_sorted_by_stars_desc(self):
        out = ss._parse_skill_search(self.data)
        stars = [e["github_stars"] for e in out]
        self.assertEqual(stars, sorted(stars, reverse=True))

    def test_entry_shape(self):
        out = ss._parse_skill_search(self.data)
        self.assertTrue(out)
        e = out[0]
        self.assertEqual(set(e), {"id", "name", "url", "description", "github_stars", "pushed_at", "topics"})
        self.assertIn("/", e["id"])
        self.assertIsInstance(e["github_stars"], int)

    def test_topic_match_in_topics_field(self):
        # 名字干净但 topics 含 awesome-list 的也应剔除
        sample = {"items": [
            {"full_name": "x/clean-skill", "name": "clean-skill", "topics": ["claude-code-skills"],
             "stargazers_count": 10, "html_url": "https://github.com/x/clean-skill"},
            {"full_name": "y/sneaky", "name": "sneaky", "topics": ["awesome-list"],
             "stargazers_count": 99, "html_url": "https://github.com/y/sneaky"},
        ]}
        out = ss._parse_skill_search(sample)
        ids = {e["id"] for e in out}
        self.assertIn("x/clean-skill", ids)
        self.assertNotIn("y/sneaky", ids)

    def test_empty_payload(self):
        self.assertEqual(ss._parse_skill_search(None), [])
        self.assertEqual(ss._parse_skill_search({}), [])


class TestRepoTreeParse(unittest.TestCase):
    def test_anthropics_skills_is_collection_18(self):
        data = _load("gh_tree_anthropics_skills.json")
        out = ss._parse_repo_tree("anthropics/skills", data)
        self.assertTrue(out["is_collection"])
        self.assertEqual(len(out["children"]), 18)
        # 子 skill 名取 SKILL.md 父目录名
        self.assertIn("algorithmic-art", out["children"])

    def test_below_threshold_not_collection(self):
        data = {"tree": [
            {"path": "skills/a/SKILL.md"},
            {"path": "skills/b/SKILL.md"},
            {"path": "README.md"},
        ]}
        out = ss._parse_repo_tree("o/r", data)
        self.assertFalse(out["is_collection"])  # <5 个
        self.assertEqual(out["children"], ["a", "b"])

    def test_ignores_non_skillmd_paths(self):
        data = {"tree": [
            {"path": "src/SKILL.md.txt"},   # 不以 SKILL.md 结尾（结尾是 .txt）→ 不计
            {"path": "docs/guide.md"},
        ]}
        out = ss._parse_repo_tree("o/r", data)
        # 'src/SKILL.md.txt' 不以 'SKILL.md' 结尾 → children 空
        self.assertEqual(out["children"], [])

    def test_empty_tree(self):
        out = ss._parse_repo_tree("o/r", None)
        self.assertEqual(out, {"repo": "o/r", "is_collection": False, "children": []})


class TestBlogMentionSearch(unittest.TestCase):
    """blog_mention_search 的 LLM 有/无两分支（注入 fetch + extract，全程离线）。"""

    def setUp(self):
        self._orig_bing = ss.sources.fetch_bing
        self._orig_hn = ss.sources.fetch_hackernews
        articles = [
            {"title": "Top Claude Code skills you should try", "url": "https://blog.a.com/p1",
             "summary": "we love `pdf-tools`", "author": "alice"},
            {"title": "推荐 anthropics/skills 这个集合", "url": "https://news.b.com/p2",
             "summary": "", "author": "bob"},
        ]
        ss.sources.fetch_bing = lambda kw, days=7: list(articles)
        ss.sources.fetch_hackernews = lambda kw, days=7: []

    def tearDown(self):
        ss.sources.fetch_bing = self._orig_bing
        ss.sources.fetch_hackernews = self._orig_hn

    def test_with_injected_llm_extract(self):
        def fake_extract(arts):
            return [{"name": "pdf-tools", "source_repo": "x/pdf-tools",
                     "reason": "强", "domain": "blog.a.com", "author": "alice"}]
        out = ss.blog_mention_search(llm_extract=fake_extract)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "pdf-tools")

    def test_fallback_regex_when_llm_returns_empty(self):
        # 注入返回空的 extract → 走正则降级；p1 标题/摘要含反引号 `pdf-tools` → 应提名
        out = ss.blog_mention_search(llm_extract=lambda arts: [])
        names = {c["name"] for c in out}
        self.assertIn("pdf-tools", names)
        # 降级提名带上来源 domain/author 供口碑去重
        pdf = next(c for c in out if c["name"] == "pdf-tools")
        self.assertEqual(pdf["domain"], "blog.a.com")
        self.assertEqual(pdf["author"], "alice")

    def test_fallback_extracts_github_repo_from_title(self):
        def fake_extract(_):
            return []
        # 标题里直接含 owner/repo 文本不一定带 github.com，这里验证反引号路径已覆盖；
        # 再单独验证 _fallback_extract 对 github.com 链接的解析
        arts = [{"title": "t", "url": "https://github.com/anthropics/skills",
                 "summary": "", "author": "carol"}]
        out = ss._fallback_extract(arts)
        self.assertEqual(out[0]["source_repo"], "anthropics/skills")
        self.assertEqual(out[0]["name"], "skills")

    def test_no_articles_returns_empty(self):
        ss.sources.fetch_bing = lambda kw, days=7: []
        ss.sources.fetch_hackernews = lambda kw, days=7: []
        self.assertEqual(ss.blog_mention_search(llm_extract=lambda a: [{"name": "x"}]), [])


class TestArticleBodyExtract(unittest.TestCase):
    """正文抓取 + 逐篇抽取：HTML 剥文、抓取失败降级、domain/author 代码归属。"""

    def test_html_to_text_strips_tags_and_scripts(self):
        raw = "<html><head><style>x{}</style></head><body><script>bad()</script>" \
              "<h1>标题</h1>  <p>正文&amp;内容</p></body></html>"
        out = ss._html_to_text(raw)
        self.assertNotIn("<", out)
        self.assertNotIn("bad()", out)
        self.assertNotIn("x{}", out)
        self.assertIn("标题", out)
        self.assertIn("正文&内容", out)  # 实体已反转义

    def test_fetch_article_text_short_body_returns_empty(self):
        # 反爬空页 / JS 壳：正文过短应判失败返 ''（调用方退回摘要）
        orig = ss.http_util.get
        ss.http_util.get = lambda *a, **k: "<html><body>太短</body></html>"
        try:
            self.assertEqual(ss._fetch_article_text("https://x.com/p"), "")
        finally:
            ss.http_util.get = orig

    def test_extract_attributes_domain_author_from_article(self):
        # LLM 只回 skill 名，domain/author 由文章本身归属（保障 ≥2 跨源去重准确）
        fake_llm = type("L", (), {
            "summarize": staticmethod(
                lambda system, user, max_tokens=600: ('[{"name":"pdf-tools"}]', "x"))
        })
        orig = ss._fetch_article_text
        ss._fetch_article_text = lambda url, max_chars=5000: "正文里推荐了 pdf-tools"
        try:
            art = {"title": "t", "url": "https://blog.csdn.net/u/123", "author": "alice"}
            out = ss._extract_one_article(fake_llm, art)
        finally:
            ss._fetch_article_text = orig
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "pdf-tools")
        self.assertEqual(out[0]["domain"], "blog.csdn.net")
        self.assertEqual(out[0]["author"], "alice")


class TestFetcherFailureTolerance(unittest.TestCase):
    def test_search_network_fail_returns_empty(self):
        orig = ss.http_util.get_json
        ss.http_util.get_json = lambda *a, **k: None
        try:
            self.assertEqual(ss.github_skill_search(), [])
            self.assertEqual(ss.repo_tree_skills("o/r")["children"], [])
        finally:
            ss.http_util.get_json = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
