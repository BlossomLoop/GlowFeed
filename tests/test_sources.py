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
