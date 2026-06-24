"""信息源解析层测试：Bing 网页 SERP 解析（离线喂录制夹具）。

Bing 的 format=rss 端点已退化为只返词典/即时答案卡片，改解析正常网页 SERP 的
自然结果（b_algo 区块）。本测试用录制的真实 HTML 夹具离线验证解析与噪声过滤。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import sources

FIX = Path(__file__).parent / "fixtures"


class TestGithubTrendingParse(unittest.TestCase):
    """github.com/trending 网页解析（离线喂录制夹具）。"""

    def setUp(self):
        self.html = (FIX / "github_trending.html").read_text(encoding="utf-8")

    def test_extracts_all_repos(self):
        rows = sources._parse_github_trending(self.html, "All")
        self.assertEqual(len(rows), 16, "夹具有 16 个 Box-row")
        # 每条 owner/repo 形态、url、rank 连续
        for i, r in enumerate(rows, 1):
            self.assertEqual(r["rank"], i)
            self.assertEqual(r["name"].count("/"), 1)
            self.assertEqual(r["url"], f"https://github.com/{r['name']}")

    def test_first_repo_fields(self):
        r = sources._parse_github_trending(self.html, "All")[0]
        self.assertEqual(r["name"], "calesthio/OpenMontage")
        self.assertEqual(r["language"], "Python")
        self.assertTrue(r["description"])
        # 总 star 应远大于「当期新增 star」(score)，且都解析成真实数字（非图标 svg 里的小数）
        self.assertGreater(r["stars"], 1000)
        self.assertGreater(r["forks"], 0)
        self.assertGreater(r["score"], 0)
        self.assertGreater(r["stars"], r["score"])

    def test_unified_row_shape(self):
        r = sources._parse_github_trending(self.html)[0]
        self.assertEqual(
            set(r),
            {"rank", "name", "url", "language", "description",
             "stars", "forks", "score", "contributors"},
        )

    def test_empty_input(self):
        self.assertEqual(sources._parse_github_trending(""), [])
        self.assertEqual(sources._parse_github_trending(None), [])


class TestOssinsightParse(unittest.TestCase):
    """OSSInsight 响应解析为统一行结构。"""

    def test_parse_minimal(self):
        data = {"data": {"rows": [
            {"repo_name": "a/b", "primary_language": "Go", "description": "x",
             "stars": "100", "forks": "5", "total_score": "12.34",
             "contributor_logins": "u1,u2,u3,u4"},
            {"repo_name": "", "stars": "1"},  # 无名 → 跳过
        ]}}
        rows = sources._parse_ossinsight(data)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["name"], "a/b")
        self.assertEqual(r["stars"], 100)
        self.assertEqual(r["score"], 12.3)
        self.assertEqual(r["contributors"], ["u1", "u2", "u3"])  # 取前 3

    def test_parse_empty(self):
        self.assertEqual(sources._parse_ossinsight(None), [])
        self.assertEqual(sources._parse_ossinsight({}), [])


class TestBingHtmlParse(unittest.TestCase):
    def setUp(self):
        self.html = (FIX / "bing_skill_search.html").read_text(encoding="utf-8")

    def test_extracts_real_articles(self):
        out = sources._parse_bing_html(self.html)
        self.assertTrue(out, "应从网页 SERP 抽到自然结果")
        # 录制夹具来自 query「claude code skill 推荐」，应含知乎/CSDN 等真实文章
        hosts = {sources._host_of(r["url"]) for r in out}
        self.assertTrue(
            {"zhuanlan.zhihu.com", "blog.csdn.net"} & hosts,
            f"应包含真实博客域名，实际：{hosts}",
        )

    def test_filters_dictionary_noise(self):
        out = sources._parse_bing_html(self.html)
        hosts = {sources._host_of(r["url"]) for r in out}
        # 词典/百科挂件域名必须被剔除（英文词查询常被其劫持）
        for noisy in ("iciba.com", "baike.baidu.com", "dictionary.cambridge.org",
                      "esdict.cn", "youdao.com"):
            self.assertNotIn(noisy, hosts, f"{noisy} 应作为词典噪声被剔除")

    def test_item_shape(self):
        out = sources._parse_bing_html(self.html)
        r = out[0]
        self.assertEqual(
            set(r),
            {"title", "url", "summary", "source", "author", "published_at", "engagement"},
        )
        self.assertEqual(r["source"], "bing")
        self.assertTrue(r["title"])
        self.assertTrue(r["url"].startswith("http"))
        # 标题不应残留 HTML 标签
        self.assertNotIn("<", r["title"])

    def test_empty_input(self):
        self.assertEqual(sources._parse_bing_html(""), [])
        self.assertEqual(sources._parse_bing_html(None), [])


if __name__ == "__main__":
    unittest.main()
