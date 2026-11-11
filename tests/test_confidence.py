"""实测置信度评分 + 落库/排序/回填的回归测试（unittest，零额外依赖）。"""
import datetime as dt
import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from lxhub import confidence
from lxhub.db import DB, _copy_rank, _group_rank

NOW = dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.timezone.utc)
FRESH = "2026-11-09T12:00:00+00:00"
OLD = "2026-09-01T12:00:00+00:00"


def _detail(n_ok=1, n_fail=0, quality=None, spec=None, l2=None, alert=None):
    platforms = {}
    for i in range(n_ok):
        entry = {"status": "usable", "message": "返回真实音频",
                 "latencyMs": 300, "spec": spec or {"container": "mp3", "bitrate": 128}}
        if quality:
            entry["quality"] = quality
        platforms[f"p{i}"] = entry
    for i in range(n_fail):
        platforms[f"f{i}"] = {"status": "failed", "message": "接口 403"}
    d = {"status": "usable" if n_ok else "degraded", "platforms": platforms,
         "declared": list(platforms)}
    if l2 is not None:
        d["l2"] = l2
    if alert:
        d["updateAlert"] = {"log": "发现新版本，请更新"}
    return d


def _score(detail, status="usable", checked=FRESH, now=NOW):
    return confidence.score_probe(status, detail, checked, now=now)


class ScoreFormulaTest(unittest.TestCase):
    def test_non_usable_is_zero(self):
        for status in ("degraded", "not-loadable", ""):
            r = _score(_detail(3, quality="matched"), status=status)
            self.assertEqual(r["score"], 0)
            self.assertEqual(r["parts"], {})
            self.assertEqual(r["reason"], status or "unprobed")

    def test_plain_l1_single_platform(self):
        r = _score(_detail(1))
        self.assertEqual(r["success"], 1)
        self.assertEqual(r["tested"], 1)
        self.assertEqual(r["quality_verdict"], "unverified")
        self.assertEqual(r["spec_level"], "verified")
        p = r["parts"]
        self.assertEqual(p["base"], 25)
        self.assertEqual(p["platforms"], 7)
        self.assertEqual(p["coverage"], 10)
        self.assertEqual(p["quality"], 8)
        self.assertEqual(p["spec"], 2)
        self.assertEqual(p["freshness"], 5)
        self.assertEqual(r["score"], 57)

    def test_five_platforms_lossless_is_full_score(self):
        spec = {"container": "flac", "sampleRate": 96000, "bitDepth": 24}
        d = _detail(5, quality="matched", spec=spec)
        r = _score(d)
        self.assertEqual(r["success"], 5)
        self.assertEqual(r["spec_level"], "high")
        self.assertEqual(r["score"], 100)

    def test_partial_success_reduces_coverage_and_platform_points(self):
        full = _score(_detail(5))
        partial = _score(_detail(1, 4))
        self.assertEqual(full["parts"]["platforms"], 35)
        self.assertEqual(partial["parts"]["platforms"], 7)
        self.assertEqual(full["parts"]["coverage"], 10)
        self.assertEqual(partial["parts"]["coverage"], 2)
        self.assertGreater(full["score"], partial["score"])

    def test_platform_cap_at_five(self):
        a, b = _score(_detail(5)), _score(_detail(8))
        self.assertEqual(a["parts"]["platforms"], b["parts"]["platforms"])

    def test_quality_verdict_rank(self):
        pts = {q: _score(_detail(1, quality=q))["parts"]["quality"]
               for q in ("matched", "degraded", "unknown", "mismatch", "refused")}
        self.assertEqual(pts, {"matched": 20, "degraded": 10, "unknown": 8,
                               "mismatch": 2, "refused": 0})
        self.assertGreater(pts["matched"], _score(_detail(1))["parts"]["quality"])

    def test_l2_verdict_used_when_platform_missing_quality(self):
        d = _detail(1)
        d["l2"] = {"p0": {"verdict": "matched", "claims": [
            {"claimed": "flac", "verdict": "matched", "spec": {"container": "flac",
                                                               "bitDepth": 24}}]}}
        r = _score(d)
        self.assertEqual(r["quality_verdict"], "matched")
        self.assertEqual(r["spec_level"], "high")

    def test_best_platform_quality_wins(self):
        d = _detail(2)
        d["platforms"]["p0"]["quality"] = "refused"
        d["platforms"]["p1"]["quality"] = "matched"
        self.assertEqual(_score(d)["quality_verdict"], "matched")

    def test_flac_with_unverified_header_is_not_high_spec(self):
        d = _detail(1, spec={"container": "flac", "specUnverified": "头部被改写"})
        self.assertEqual(_score(d)["spec_level"], "verified")

    def test_update_alert_penalty(self):
        plain = _score(_detail(3))
        alert = _score(_detail(3, alert=True))
        self.assertEqual(alert["parts"]["penalty"], -5)
        self.assertEqual(plain["score"] - alert["score"], 5)

    def test_freshness_decay(self):
        self.assertEqual(_score(_detail(1), checked=FRESH)["parts"]["freshness"], 5)
        self.assertEqual(_score(_detail(1), checked=OLD)["parts"]["freshness"], 0)
        mid = _score(_detail(1), checked="2026-10-25T12:00:00+00:00")
        self.assertTrue(0 < mid["parts"]["freshness"] < 5)

    def test_detail_json_string_and_missing_detail(self):
        raw = json.dumps(_detail(2, quality="matched"))
        self.assertEqual(_score(raw)["success"], 2)
        empty = _score("")
        self.assertEqual(empty["score"], 25)
        self.assertEqual(empty["reason"], "detail-missing")

    def test_malformed_detail_does_not_crash(self):
        for d in ({"platforms": [], "l2": [], "updateAlert": "请更新"},
                  {"platforms": {"p0": "bad"}, "l2": {"p0": "bad"}},
                  "not-json", None, 42):
            with self.subTest(d=d):
                r = _score(d)
                self.assertIsInstance(r["score"], int)
                self.assertTrue(0 <= r["score"] <= 100)

    def test_score_never_exceeds_bounds(self):
        d = _detail(9, quality="matched", spec={"bitDepth": 24, "avgBitrateKbps": 900})
        self.assertLessEqual(_score(d)["score"], 100)
        self.assertGreaterEqual(_score(d, checked=OLD)["score"], 0)


class DBConfidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lxh-conf-")
        self.path = os.path.join(self.tmp, "test.db")
        self.db = DB(self.path)

    def tearDown(self):
        try:
            self.db._c.conn.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _add(self, name, alive=True, **kw):
        self.db.upsert_source({
            "url": f"https://example.com/{name}.js", "name": name,
            "sha256": f"hash-{name}", "is_alive": 1 if alive else 0,
            "looks_valid": 1, "first_seen": OLD, **kw,
        })
        return next(r["id"] for r in self.db.all_sources_rows()
                    if r["name"] == name)

    def test_columns_migrated_and_probe_writes_score(self):
        cols = {r[1] for r in sqlite3.connect(self.path).execute(
            "PRAGMA table_info(sources)")}
        self.assertIn("confidence", cols)
        self.assertIn("confidence_detail", cols)
        sid = self._add("A")
        lossless = {"container": "flac", "bitDepth": 24}
        self.db.update_probe(sid, "usable",
                             json.dumps(_detail(5, quality="matched", spec=lossless)),
                             "sha", FRESH)
        row = self.db.get_source(sid)
        self.assertEqual(row["confidence"], 100)
        self.assertEqual(row["confidence_detail"]["success"], 5)
        self.assertEqual(row["confidence"], row["confidence_detail"]["score"])
        # 后来实测失败，分数必须归零，不能继续顶着旧高分
        self.db.update_probe(sid, "degraded", json.dumps(_detail(0)), "sha", FRESH)
        self.assertEqual(self.db.get_source(sid)["confidence"], 0)

    def test_backfill_legacy_rows_is_idempotent(self):
        sid = self._add("legacy")
        payload = json.dumps(_detail(3, quality="matched"), ensure_ascii=False)
        # 模拟老库：有实测结果但没有置信度列的值
        self.db._run(
            "UPDATE sources SET probe_status='usable', probe_detail=?, probe_checked=?,"
            " confidence=0, confidence_detail='' WHERE id=?", (payload, FRESH, sid))
        self.db._c.conn.close()
        self.db = DB(self.path)            # 重新打开 = 服务重启
        row = self.db.get_source(sid)
        self.assertGreater(row["confidence"], 0)
        self.assertEqual(row["confidence"], row["confidence_detail"]["score"])
        self.assertEqual(self.db.backfill_confidence(), 0)  # 幂等

    def test_list_sources_orders_by_confidence(self):
        high = self._add("高分可用")
        low = self._add("低分可用")
        untested = self._add("未实测")
        dead = self._add("失效高置信", alive=False)
        lossless = {"container": "flac", "bitDepth": 24}
        self.db.update_probe(high, "usable",
                             json.dumps(_detail(5, quality="matched", spec=lossless)),
                             "s", FRESH)
        self.db.update_probe(low, "usable", json.dumps(_detail(1)), "s", FRESH)
        self.db.update_probe(dead, "usable",
                             json.dumps(_detail(5, quality="matched", spec=lossless)),
                             "s", FRESH)
        names = [r["name"] for r in self.db.list_sources()]
        self.assertEqual(names[:2], ["高分可用", "低分可用"])
        self.assertIn("未实测", names)
        self.assertEqual(names[-1], "失效高置信")

    def test_prune_same_name_copies_keeps_high_confidence(self):
        # 同一仓库的同名两份：旧快照实测满分、新快照未实测，应保留旧的那份
        base = {"name": "同名音源", "origin_repo": "owner/repo",
                "looks_valid": 1, "is_alive": 1, "last_alive": FRESH}
        old_url = "https://e/repo/V260907/a.js"
        new_url = "https://e/repo/V261010/a.js"
        self.db.upsert_source({**base, "url": old_url, "sha256": "h-old"})
        self.db.upsert_source({**base, "url": new_url, "sha256": "h-new"})
        sid = next(r["id"] for r in self.db.all_sources_rows() if r["url"] == old_url)
        self.db.update_probe(sid, "usable",
                             json.dumps(_detail(5, quality="matched",
                                                spec={"container": "flac", "bitDepth": 24})),
                             "s", FRESH)
        self.assertEqual(self.db.prune_same_name_copies(), 1)
        urls = {r["url"] for r in self.db.all_sources_rows()}
        self.assertIn(old_url, urls)
        self.assertNotIn(new_url, urls)

    def test_group_and_copy_rank_prefer_high_confidence(self):
        a = {"name": "旧版", "is_alive": True, "confidence": 20, "valid_tier": "confirmed",
             "url": "https://e/V1/a.js", "version": "9.9.9", "last_alive": OLD}
        b = {"name": "新版", "is_alive": True, "confidence": 95, "valid_tier": "probable",
             "url": "https://e/V2/a.js", "version": "1.0.0", "last_alive": FRESH}
        self.assertGreater(_group_rank(b), _group_rank(a))
        self.assertGreater(_copy_rank(b), _copy_rank(a))
        # 失效的高置信旧副本不能压过存活的低分副本
        dead_high = {"confidence": 100, "is_alive": False, "valid_tier": "confirmed",
                     "url": "https://e/V3/a.js"}
        alive_low = {"confidence": 0, "is_alive": True, "valid_tier": "probable",
                     "url": "https://e/V4/a.js"}
        self.assertGreater(_group_rank(alive_low), _group_rank(dead_high))
        self.assertGreater(_copy_rank(alive_low), _copy_rank(dead_high))


if __name__ == "__main__":
    unittest.main()
