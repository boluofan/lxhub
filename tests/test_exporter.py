"""本地化输出回归测试（unittest，零额外依赖）。

覆盖：固定文件名与覆盖更新、TopN 排序、次优补齐、模板白名单、
只清理 manifest 记录的旧文件、manifest 与 runs 落库。

运行：python -m unittest discover -s tests -v
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from lxhub import exporter
from lxhub.config import DEFAULTS, validate_filename_template
from lxhub.db import DB

NOW = "2026-11-10T00:00:00+00:00"


def make_cfg(**over):
    e = dict(DEFAULTS["export"])
    e.update(over)
    return SimpleNamespace(export=e)


class ExporterTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.out = self.root / "outjs"
        old = os.environ.get("LXH_EXPORT_DIR")
        os.environ["LXH_EXPORT_DIR"] = str(self.out)

        def restore():
            if old is None:
                os.environ.pop("LXH_EXPORT_DIR", None)
            else:
                os.environ["LXH_EXPORT_DIR"] = old

        self.addCleanup(restore)
        self.db = DB(str(self.root / "test.db"))
        # Windows 上连接不关闭会锁住 test.db，导致 TemporaryDirectory 清理失败；
        # addCleanup 是 LIFO，这一条会在 tmp.cleanup 之前执行
        self.addCleanup(self.db._c.conn.close)

    def add_source(self, key, name, score, content, alive=True, status="usable"):
        url = f"https://example.com/{key}.js"
        self.db.upsert_source({
            "url": url,
            "name": name,
            "content_text": content,
            "sha256": f"sha-{key}",
            "first_seen": NOW,
            "last_checked": NOW,
            "last_alive": NOW,
        })
        sid = self.db._run("SELECT id FROM sources WHERE url = ?", (url,))[0]["id"]
        self.db._run(
            "UPDATE sources SET is_alive = ?, looks_valid = 1, "
            "probe_status = ?, confidence = ? WHERE id = ?",
            (1 if alive else 0, status, score, sid))
        return sid

    def read(self, name):
        return (self.out / name).read_text(encoding="utf-8")

    def manifest(self):
        return json.loads((self.out / exporter.MANIFEST_NAME).read_text(encoding="utf-8"))

    # ---------- 固定文件名 + 覆盖更新 ----------

    def test_fixed_names_and_overwrite_when_ranking_changes(self):
        self.add_source("a", "源甲", 90, "// A")
        self.add_source("b", "源乙", 70, "// B")
        self.add_source("c", "源丙", 50, "// C")
        self.add_source("d", "源丁", 30, "// D")
        cfg = make_cfg(top_n=3, fill_shortfall=False)

        r1 = exporter.run_export(cfg, self.db, trigger="test")
        self.assertEqual(r1["written"], 3)
        self.assertEqual(r1["filenames"],
                         ["lx-source-1.js", "lx-source-2.js", "lx-source-3.js"])
        self.assertEqual(self.read("lx-source-1.js"), "// A")
        self.assertEqual(self.read("lx-source-3.js"), "// C")

        # Top 榜换人：文件名不变，内容被原子覆盖成新的第一名
        url_d = "https://example.com/d.js"
        self.db._run("UPDATE sources SET confidence = 100 WHERE url = ?", (url_d,))
        r2 = exporter.run_export(cfg, self.db, trigger="test")
        self.assertEqual(r2["filenames"], r1["filenames"])
        self.assertEqual(self.read("lx-source-1.js"), "// D")
        self.assertEqual(self.read("lx-source-2.js"), "// A")
        self.assertEqual(sorted(p.name for p in self.out.glob("*.js")),
                         ["lx-source-1.js", "lx-source-2.js", "lx-source-3.js"])
        self.assertEqual(self.manifest()["files"][0]["source_id"],
                         self.db._run("SELECT id FROM sources WHERE url = ?", (url_d,))[0]["id"])

    def test_custom_template_with_zero_padding(self):
        self.add_source("a", "源甲", 90, "// A")
        self.add_source("b", "源乙", 70, "// B")
        r = exporter.run_export(make_cfg(top_n=2, filename="src-{rank:02d}.js",
                                         fill_shortfall=False), self.db)
        self.assertEqual(r["filenames"], ["src-01.js", "src-02.js"])
        self.assertEqual(self.read("src-01.js"), "// A")

    # ---------- 模板白名单 ----------

    def test_template_rejects_drifting_placeholders(self):
        for bad in ("{name}.js", "fixed.js", "{date}.js", "../{rank}.js",
                    "{rank}/{name}.js", "{rank}.txt", "{rank}{id}.js"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_filename_template(bad)
        self.assertEqual(validate_filename_template("lx-{rank:02d}.js"),
                         "lx-{rank:02d}.js")

    # ---------- 补齐与缺口 ----------

    def test_fill_shortfall_and_shortfall_counter(self):
        self.add_source("a", "源甲", 90, "// A")
        self.add_source("b", "源乙", 70, "// B")
        self.add_source("c", "源丙", 0, "// C", status="")
        self.add_source("d", "源丁", 0, "// D", status="")
        self.add_source("e", "源戊", 0, "// E", alive=False, status="")

        r = exporter.run_export(make_cfg(top_n=3), self.db)
        self.assertEqual(r["written"], 3)
        self.assertEqual(r["filled"], 1)
        self.assertEqual(r["shortfall"], 0)
        self.assertEqual(self.manifest()["files"][2]["filled"], True)

        r2 = exporter.run_export(make_cfg(top_n=3, fill_shortfall=False), self.db)
        self.assertEqual(r2["written"], 2)
        self.assertEqual(r2["shortfall"], 1)

    def test_empty_pool_still_writes_manifest_and_run(self):
        r = exporter.run_export(make_cfg(top_n=3), self.db)
        self.assertEqual(r["written"], 0)
        self.assertEqual(r["shortfall"], 3)
        self.assertEqual(self.manifest()["files"], [])
        last = self.db.last_run("export")
        self.assertTrue(last["finished_at"])

    # ---------- 清理只碰自己生成的文件 ----------

    def test_prune_stale_only_removes_manifest_files(self):
        self.add_source("a", "源甲", 90, "// A")
        self.add_source("b", "源乙", 70, "// B")
        self.add_source("c", "源丙", 50, "// C")
        exporter.run_export(make_cfg(top_n=3, fill_shortfall=False), self.db)
        (self.out / "keep.txt").write_text("user file", encoding="utf-8")
        (self.out / "other.js").write_text("// user script", encoding="utf-8")

        r = exporter.run_export(make_cfg(top_n=1, prune_stale=True,
                                         fill_shortfall=False), self.db)
        self.assertEqual(sorted(r["pruned"]), ["lx-source-2.js", "lx-source-3.js"])
        self.assertTrue((self.out / "keep.txt").is_file())
        self.assertTrue((self.out / "other.js").is_file())
        self.assertTrue((self.out / "lx-source-1.js").is_file())

    # ---------- 状态接口 ----------

    def test_local_status_exposes_dir_and_files(self):
        self.add_source("a", "源甲", 90, "// A")
        exporter.run_export(make_cfg(top_n=1, fill_shortfall=False), self.db)
        s = exporter.local_status(make_cfg(top_n=1), self.db)
        self.assertEqual(s["dir"], str(self.out.resolve()))
        self.assertTrue(s["dir_from_env"])
        self.assertEqual(s["serve_prefix"], "/local/")
        self.assertEqual(len(s["files"]), 1)
        self.assertTrue(s["last_run"]["finished_at"])


if __name__ == "__main__":
    unittest.main()
