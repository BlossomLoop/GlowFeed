"""核心纯函数测试：去重、关键词过滤、调度时间计算。"""
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import store as store_mod
from app.digest import _cluster, _keywords
from app.llm import merge_keywords, _parse_keywords
from app.pipeline import (dedupe, keyword_match, normalize_url, title_similarity,
                          excluded_match, score_item)
from app.scheduler import compute_next_run, compute_prev_run


class TestDedupe(unittest.TestCase):
    def test_url_normalize_strips_tracking(self):
        a = normalize_url("https://www.example.com/a/?utm_source=x&utm_medium=y")
        b = normalize_url("http://example.com/a")
        self.assertEqual(a, b)

    def test_chinese_title_similarity(self):
        self.assertGreater(title_similarity("华为发布全新折叠屏手机", "华为发布全新的折叠屏手机"), 0.72)
        self.assertLess(title_similarity("华为发布折叠屏手机", "苹果推出新款笔记本电脑"), 0.3)

    def test_dedupe_keeps_first_higher_score(self):
        items = [
            {"title": "OpenAI releases new model GPT-6", "url": "https://a.com/1", "score": 0.9},
            {"title": "OpenAI releases new model GPT-6!", "url": "https://b.com/2", "score": 0.5},
            {"title": "完全不同的另一条新闻标题", "url": "https://c.com/3", "score": 0.4},
        ]
        kept = dedupe(items)
        self.assertEqual(len(kept), 2)
        self.assertEqual(kept[0]["url"], "https://a.com/1")

    def test_dedupe_same_url(self):
        items = [
            {"title": "标题甲乙丙丁", "url": "https://x.com/p?utm_source=wb", "score": 1},
            {"title": "另一个无关标题确保不撞相似度", "url": "https://x.com/p/", "score": 1},
        ]
        self.assertEqual(len(dedupe(items)), 1)


class TestFilter(unittest.TestCase):
    def test_empty_keywords_pass_all(self):
        self.assertTrue(keyword_match("任意文本", []))

    def test_any_keyword_hits(self):
        self.assertTrue(keyword_match("Claude 发布新版本", ["claude", "gpt"]))
        self.assertFalse(keyword_match("今天天气不错", ["claude", "gpt"]))


class TestSchedule(unittest.TestCase):
    def test_interval(self):
        now = datetime(2026, 6, 13, 10, 0)
        nxt = compute_next_run("interval", 60, now)
        self.assertEqual(nxt, datetime(2026, 6, 13, 11, 0))

    def test_interval_floor_5min(self):
        now = datetime(2026, 6, 13, 10, 0)
        self.assertEqual(compute_next_run("interval", 1, now), datetime(2026, 6, 13, 10, 5))

    def test_daily_picks_next_slot_today(self):
        now = datetime(2026, 6, 13, 10, 0)
        nxt = compute_next_run("daily", ["08:00", "12:30", "20:00"], now)
        self.assertEqual(nxt, datetime(2026, 6, 13, 12, 30))

    def test_daily_rolls_to_tomorrow(self):
        now = datetime(2026, 6, 13, 21, 0)
        nxt = compute_next_run("daily", ["08:00", "20:00"], now)
        self.assertEqual(nxt, datetime(2026, 6, 14, 8, 0))

    def test_prev_daily_picks_passed_slot_today(self):
        # 启动补跑用：now=10:00 时最近一次应触发是今天 08:00
        now = datetime(2026, 6, 13, 10, 0)
        prev = compute_prev_run("daily", ["08:00", "12:30", "20:00"], now)
        self.assertEqual(prev, datetime(2026, 6, 13, 8, 0))

    def test_prev_daily_rolls_to_yesterday(self):
        # now 早于当日所有时刻 → 最近一次应触发落在昨天最后一个时刻
        now = datetime(2026, 6, 13, 7, 0)
        prev = compute_prev_run("daily", ["08:00", "20:00"], now)
        self.assertEqual(prev, datetime(2026, 6, 12, 20, 0))

    def test_prev_interval(self):
        now = datetime(2026, 6, 13, 10, 0)
        self.assertEqual(compute_prev_run("interval", 60, now), datetime(2026, 6, 13, 9, 0))


class TestDigest(unittest.TestCase):
    def _arts(self, titles):
        return [{"title": t, "url": f"https://x/{i}", "source": "baidu_hot",
                 "engagement": 0, "score": 1.0} for i, t in enumerate(titles)]

    def test_cluster_groups_similar_titles(self):
        arts = self._arts([
            "金价回落女子一口气买入90克金手镯",
            "金价回落 女子一口气买入90克金手镯",
            "今日油价大幅下调影响出行成本",
        ])
        clusters = _cluster(arts)
        sizes = sorted(len(c["items"]) for c in clusters)
        self.assertEqual(sizes, [1, 2])  # 两条金价聚合，油价独立

    def test_keywords_dedupes_substring_fragments(self):
        # "世界杯" 高频时，碎片 "世界"/"界杯" 应被去子串逻辑剔除
        arts = self._arts(["世界杯开赛"] * 4 + ["世界杯赛程"] * 4)
        kws = [w for w, _ in _keywords(arts)]
        self.assertIn("世界杯", kws)
        self.assertNotIn("界杯", kws)


class TestPreferenceSignals(unittest.TestCase):
    def test_excluded_match_hits_title_or_summary(self):
        self.assertTrue(excluded_match("某明星出轨大瓜", "", ["明星", "八卦"]))
        self.assertTrue(excluded_match("正经标题", "正文提到明星绯闻", ["明星"]))
        self.assertFalse(excluded_match("华为发布鸿蒙", "操作系统更新", ["明星"]))
        self.assertFalse(excluded_match("任意", "任意", []))  # 空排除词不拦截

    def test_score_item_preference_boost(self):
        item = {"source": "hackernews", "engagement": 0,
                "published_at": None, "title": "Claude 发布新模型"}
        base = score_item(item, [])
        boosted = score_item(item, ["claude"])  # 命中偏好词（大小写不敏感）
        self.assertGreater(boosted, base)
        self.assertLessEqual(boosted, 1.0)  # 截顶


class TestKeywordLearning(unittest.TestCase):
    def test_merge_dedupes_and_excludes_base_and_caps(self):
        existing = ["AI", "大模型"]
        new = ["AI", "Agent", "智能体", "大模型"]  # AI 与已有重复，大模型与 base 重复
        base = ["大模型"]                          # 用户手设关键词，不应被纳入学习词
        out = merge_keywords(existing, new, base, limit=3)
        self.assertEqual(out[:2], ["AI", "Agent"])      # 保序、去重
        self.assertNotIn("大模型", out)                  # 不与 base 重复
        self.assertLessEqual(len(out), 3)                # 截断到上限

    def test_parse_keywords_from_json_block(self):
        text = '说明\n```json\n{"preferred":["AI Agent","RAG"],"excluded":["八卦"]}\n```'
        pref, excl = _parse_keywords(text)
        self.assertEqual(pref, ["AI Agent", "RAG"])
        self.assertEqual(excl, ["八卦"])

    def test_parse_keywords_bad_input(self):
        self.assertEqual(_parse_keywords("模型没按格式输出"), ([], []))


class TestFeedbackStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        store_mod.init(self.tmp)

    def test_new_task_has_pref_excl_defaults(self):
        t = store_mod.create_task({"name": "T", "schedule_value": 60})
        self.assertEqual(t["preferred_keywords"], [])
        self.assertEqual(t["excluded_keywords"], [])

    def test_feedback_roundtrip_and_toggle(self):
        store_mod.set_feedback(1, "u1", "https://a/1", "标题甲", "like")
        store_mod.set_feedback(1, "u2", "https://a/2", "标题乙", "dislike")
        fb = store_mod.get_feedback(1)
        self.assertEqual(fb["u1"]["signal"], "like")
        self.assertEqual(fb["u2"]["signal"], "dislike")
        store_mod.set_feedback(1, "u1", "https://a/1", "标题甲", "none")  # 取消
        self.assertNotIn("u1", store_mod.get_feedback(1))

    def test_retention_prunes_old_unliked_keeps_liked(self):
        old = "2000-01-01 00:00:00"  # 远超保留期
        store_mod._write(store_mod._articles_path(1), [
            {"url_hash": "o1", "title": "旧未赞", "url": "https://a/o1", "source": "baidu_hot", "fetched_at": old, "score": 1},
            {"url_hash": "o2", "title": "旧已赞", "url": "https://a/o2", "source": "baidu_hot", "fetched_at": old, "score": 1},
            {"url_hash": "n1", "title": "新内容", "url": "https://a/n1", "source": "baidu_hot", "fetched_at": store_mod.now(), "score": 1},
        ])
        store_mod.set_feedback(1, "o2", "https://a/o2", "旧已赞", "like")
        store_mod.add_articles(1, [])  # 仅触发裁剪
        titles = {a["title"] for a in store_mod.list_articles(task_id=1, limit=50)}
        self.assertNotIn("旧未赞", titles)  # 超期未赞 → 清除
        self.assertIn("旧已赞", titles)     # 超期但点赞 → 保留
        self.assertIn("新内容", titles)     # 未超期 → 保留


class TestGithubTrending(unittest.TestCase):
    """OSSInsight 解析（纯函数）+ trending_list 的落盘快照/只读盘/真实时间戳行为。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        store_mod.init(self.tmp)

    def test_parse_maps_fields(self):
        from app import sources
        sample = {"data": {"rows": [
            {"repo_name": "apple/container", "primary_language": "Swift",
             "description": "A tool", "stars": 63, "forks": 5, "total_score": 345.12,
             "contributor_logins": "alice,bob,carol,dan"},
            {"repo_name": "", "stars": 1},  # 空名应跳过
        ]}}
        out = sources._parse_ossinsight(sample)
        self.assertEqual(len(out), 1)
        a = out[0]
        self.assertEqual(a["rank"], 1)
        self.assertEqual(a["name"], "apple/container")
        self.assertEqual(a["url"], "https://github.com/apple/container")
        self.assertEqual(a["language"], "Swift")
        self.assertEqual(a["stars"], 63)
        self.assertEqual(a["score"], 345.1)            # round 到 1 位
        self.assertEqual(a["contributors"], ["alice", "bob", "carol"])  # 取前 3

    def test_contributors_list_and_empty_language(self):
        from app import sources
        out = sources._parse_ossinsight({"data": {"rows": [
            {"repo_name": "openai/whisper", "description": "ASR", "stars": 5,
             "contributor_logins": [], "primary_language": ""},
        ]}})
        self.assertEqual(out[0]["contributors"], [])
        self.assertEqual(out[0]["language"], "")

    def test_empty_payload_returns_list(self):
        from app import sources
        self.assertEqual(sources._parse_ossinsight(None), [])

    def test_snapshot_persisted_and_read_only_on_get(self):
        """force 真抓 → 落盘 + 记真实时间戳；后续 GET 只读盘、不再真抓。"""
        from app import http_util, sources
        sample = {"data": {"rows": [{"repo_name": "a/b", "stars": 1}]}}
        # 抓取前无快照、无时间戳
        self.assertIsNone(sources.trending_fetched_at("ossinsight", "today", "All"))
        orig = http_util.get_json
        http_util.get_json = lambda *a, **k: sample
        try:
            out = sources.trending_list("ossinsight", "today", "All", force=True)
        finally:
            http_util.get_json = orig
        self.assertEqual(len(out), 1)
        ts = sources.trending_fetched_at("ossinsight", "today", "All")
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        # 后续 GET（force=False）只读磁盘快照 → 即使真抓会抛错也不该被调用
        http_util.get_json = lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应真抓"))
        try:
            again = sources.trending_list("ossinsight", "today", "All")
        finally:
            http_util.get_json = orig
        self.assertEqual(len(again), 1)
        self.assertEqual(sources.trending_fetched_at("ossinsight", "today", "All"), ts)


class TestTrendSort(unittest.TestCase):
    """情报流「趋势榜」= 新鲜热度排序（store.list_articles sort='trend'）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        store_mod.init(self.tmp)

    def test_trend_favors_fresh_over_old_hot(self):
        store_mod._write(store_mod._articles_path(1), [
            {"url_hash": "old", "title": "旧但极热", "url": "https://a/old", "source": "baidu_hot",
             "fetched_at": "2000-01-01 00:00:00", "engagement": 100000, "score": 1},
            {"url_hash": "new", "title": "新且小热", "url": "https://a/new", "source": "baidu_hot",
             "fetched_at": store_mod.now(), "engagement": 10, "score": 0.1},
        ])
        trend = store_mod.list_articles(task_id=1, sort="trend", limit=10)
        self.assertEqual(trend[0]["title"], "新且小热")   # 趋势：新鲜压过绝对热度
        score = store_mod.list_articles(task_id=1, sort="score", limit=10)
        self.assertEqual(score[0]["title"], "旧但极热")   # 对比：绝对热度榜里旧热在前

    def test_trend_breaks_tie_by_engagement_when_same_age(self):
        ts = store_mod.now()
        store_mod._write(store_mod._articles_path(1), [
            {"url_hash": "a", "title": "同期低热", "url": "https://a/a", "source": "baidu_hot",
             "fetched_at": ts, "engagement": 5, "score": 1},
            {"url_hash": "b", "title": "同期高热", "url": "https://a/b", "source": "baidu_hot",
             "fetched_at": ts, "engagement": 5000, "score": 1},
        ])
        trend = store_mod.list_articles(task_id=1, sort="trend", limit=10)
        self.assertEqual(trend[0]["title"], "同期高热")   # 同新鲜度下，热度高者靠前


class TestAuth(unittest.TestCase):
    def test_correct_token_passes(self):
        from app.server import check_token
        self.assertTrue(check_token("Bearer secret123", "secret123"))

    def test_wrong_token_rejected(self):
        from app.server import check_token
        self.assertFalse(check_token("Bearer wrong", "secret123"))

    def test_missing_header_rejected(self):
        from app.server import check_token
        self.assertFalse(check_token("", "secret123"))

    def test_malformed_header_rejected(self):
        from app.server import check_token
        self.assertFalse(check_token("secret123", "secret123"))  # 缺 "Bearer " 前缀

    def test_empty_token_config_rejects_all(self):
        from app.server import check_token
        self.assertFalse(check_token("Bearer anything", ""))


class TestPublicTasks(unittest.TestCase):
    def test_strips_sensitive_fields(self):
        from app.server import _public_tasks
        out = _public_tasks([
            {"id": 1, "name": "AI 动态", "keywords": ["机密词"], "sources": ["hackernews"]},
        ])
        self.assertEqual(out, [{"id": 1, "name": "AI 动态"}])

    def test_empty_list(self):
        from app.server import _public_tasks
        self.assertEqual(_public_tasks([]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
