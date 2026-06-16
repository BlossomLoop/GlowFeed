"""热门 Skill 编排层测试：归一去重、三榜排序、飙升做差/冷启动、口碑去重准入、
快照读写、冷却 / single-flight。全部离线（构造内存数据 + tempfile）。
"""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import skills_board as sb
from app import store as store_mod


def _raw(id, stars=0, **kw):
    d = {"id": id, "github_stars": stars}
    d.update(kw)
    return d


class TestNormalize(unittest.TestCase):
    def test_dedupe_and_merge_signals(self):
        entries = sb.normalize([
            _raw("o/r", stars=100, name="r", pushed_at="2026-06-01"),
            _raw("o/r", stars=120),  # 同 id，stars 更高 → 取 max
            _raw("a/b", stars=5),
        ])
        by = {e["id"]: e for e in entries}
        self.assertEqual(len(entries), 2)
        self.assertEqual(by["o/r"]["signals"]["github_stars"], 120)
        self.assertEqual(by["o/r"]["signals"]["pushed_at"], "2026-06-01")

    def test_collection_flag_from_is_collection(self):
        entries = sb.normalize([
            _raw("o/coll", stars=10, is_collection=True,
                 children=["a", "b", "c", "d", "e"]),
        ])
        self.assertEqual(entries[0]["type"], "collection")
        self.assertEqual(len(entries[0]["children"]), 5)

    def test_blog_mentions_accumulate(self):
        entries = sb.normalize([
            _raw("o/r", blog_mentions=[{"domain": "a.com", "author": "x"}]),
            _raw("o/r", blog_mentions=[{"domain": "b.com", "author": "y"}]),
        ])
        self.assertEqual(len(entries[0]["signals"]["blog_mentions"]), 2)

    def test_skips_blank_id(self):
        self.assertEqual(sb.normalize([{"id": "", "github_stars": 1}]), [])


class TestBuildHot(unittest.TestCase):
    def test_sorted_by_stars_and_collection_single_row(self):
        entries = sb.normalize([
            _raw("a/standalone", stars=50),
            _raw("o/coll", stars=200, is_collection=True,
                 children=["s1", "s2", "s3", "s4", "s5"]),
        ])
        rows = sb.build_hot(entries)
        self.assertEqual(rows[0]["id"], "o/coll")     # 200 > 50
        self.assertEqual(rows[0]["rank"], 1)
        self.assertTrue(rows[0]["is_collection"])
        # 集合单条目：children 列名，但不为每个子 skill 各占一行
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["children"], ["s1", "s2", "s3", "s4", "s5"])


class TestBuildRising(unittest.TestCase):
    def test_warming_up_when_no_history(self):
        cur = sb.normalize([_raw("o/r", stars=10)])
        self.assertEqual(sb.build_rising(cur, None),
                         {"status": "warming-up", "rows": []})
        self.assertEqual(sb.build_rising(cur, [])["status"], "warming-up")

    def test_delta_sorted_descending(self):
        cur = sb.normalize([_raw("o/a", stars=150), _raw("o/b", stars=300)])
        hist = [{"id": "o/a", "stars": 100}, {"id": "o/b", "stars": 290}]
        out = sb.build_rising(cur, hist)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["rows"][0]["id"], "o/a")   # Δ50 > Δ10
        self.assertEqual(out["rows"][0]["delta_stars"], 50)
        self.assertEqual(out["rows"][0]["rank"], 1)

    def test_new_repo_without_history_excluded(self):
        cur = sb.normalize([_raw("o/new", stars=99)])
        hist = [{"id": "o/old", "stars": 1}]
        # o/new 不在 history → 不计飙升（避免凭空跳变）
        self.assertEqual(sb.build_rising(cur, hist)["rows"], [])

    def test_history_with_signals_shape(self):
        cur = sb.normalize([_raw("o/a", stars=150)])
        hist = [{"id": "o/a", "signals": {"github_stars": 100}}]
        out = sb.build_rising(cur, hist)
        self.assertEqual(out["rows"][0]["delta_stars"], 50)


class TestBuildPraise(unittest.TestCase):
    def test_two_independent_sources_enters_main(self):
        cands = [
            {"name": "pdf-tools", "domain": "a.com", "author": "alice"},
            {"name": "pdf-tools", "domain": "b.com", "author": "bob"},
        ]
        out = sb.build_praise(cands)
        self.assertEqual(len(out["rows"]), 1)
        self.assertEqual(out["rows"][0]["mention_count"], 2)
        self.assertEqual(out["pending"], [])

    def test_same_domain_author_counts_once(self):
        cands = [
            {"name": "pdf-tools", "domain": "a.com", "author": "alice"},
            {"name": "pdf-tools", "domain": "a.com", "author": "alice"},  # 同对 → 计 1
        ]
        out = sb.build_praise(cands)
        # 仅 1 个独立证据 → 进 pending，不进主榜
        self.assertEqual(out["rows"], [])
        self.assertEqual(len(out["pending"]), 1)
        self.assertEqual(out["pending"][0]["mention_count"], 1)

    def test_insufficient_evidence_goes_pending(self):
        cands = [{"name": "lonely", "domain": "x.com", "author": "z"}]
        out = sb.build_praise(cands)
        self.assertEqual(out["rows"], [])
        self.assertEqual(out["pending"][0]["name"], "lonely")

    def test_sorted_by_mention_count(self):
        cands = [
            {"name": "hot", "domain": "a", "author": "1"},
            {"name": "hot", "domain": "b", "author": "2"},
            {"name": "hot", "domain": "c", "author": "3"},
            {"name": "mild", "domain": "a", "author": "1"},
            {"name": "mild", "domain": "b", "author": "2"},
        ]
        out = sb.build_praise(cands)
        self.assertEqual(out["rows"][0]["name"], "hot")     # 3 mentions
        self.assertEqual(out["rows"][1]["name"], "mild")    # 2 mentions


class TestStoreRoundtrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        store_mod.init(self.tmp)

    def test_board_save_read(self):
        data = {"rows": [{"id": "o/r"}], "snapshot_time": "t"}
        store_mod.save_skills_board("hot", "recent", data)
        self.assertEqual(store_mod.read_skills_board("hot", "recent"), data)
        self.assertIsNone(store_mod.read_skills_board("rising", "recent"))

    def test_history_archive_and_read_before(self):
        store_mod.archive_skills_history("2026-06-14", [{"id": "o/r", "stars": 10}])
        store_mod.archive_skills_history("2026-06-15", [{"id": "o/r", "stars": 20}])
        # 取 <= 2026-06-15 的最近一份 → 06-15
        got = store_mod.read_skills_history_before("2026-06-15")
        self.assertEqual(got[0]["stars"], 20)
        # 取 <= 2026-06-14 → 06-14
        self.assertEqual(store_mod.read_skills_history_before("2026-06-14")[0]["stars"], 10)
        # 早于所有 → None
        self.assertIsNone(store_mod.read_skills_history_before("2026-06-01"))


class TestWarmSkillsSingleFlight(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        store_mod.init(self.tmp)
        self._orig = sb._collect_entries
        self.calls = []

    def tearDown(self):
        sb._collect_entries = self._orig

    def test_single_flight_merges_concurrent(self):
        started = threading.Event()

        def slow_collect():
            self.calls.append(1)
            started.set()
            time.sleep(0.3)
            return sb.normalize([_raw("o/r", stars=10)])

        sb._collect_entries = slow_collect

        results = []

        def run():
            results.append(sb.warm_skills("hot", "recent"))

        t1 = threading.Thread(target=run)
        t2 = threading.Thread(target=run)
        t1.start()
        started.wait(1)
        t2.start()  # 在 t1 在途时进入 → 应合并，不重复采集
        t1.join()
        t2.join()
        # single-flight：_collect_entries 只被调一次
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(results), 2)

    def test_warm_produces_snapshot(self):
        sb._collect_entries = lambda: sb.normalize([_raw("o/r", stars=10, name="r")])
        sb.warm_skills("hot", "recent")
        snap = store_mod.read_skills_board("hot", "recent")
        self.assertIsNotNone(snap)
        self.assertEqual(snap["rows"][0]["id"], "o/r")
        self.assertIn("snapshot_time", snap)


class TestServerCooldown(unittest.TestCase):
    def setUp(self):
        from app import server
        self.server = server
        server._skills_last_refresh.clear()

    def test_cooldown_blocks_within_window(self):
        self.assertEqual(self.server._skills_cooldown_remaining("hot"), 0)
        self.server._mark_skills_refresh("hot")
        self.assertGreater(self.server._skills_cooldown_remaining("hot"), 0)
        # 不同 type 不受影响
        self.assertEqual(self.server._skills_cooldown_remaining("rising"), 0)

    def test_none_marks_all_types(self):
        self.server._mark_skills_refresh(None)
        for t in ("hot", "rising", "praise"):
            self.assertGreater(self.server._skills_cooldown_remaining(t), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
