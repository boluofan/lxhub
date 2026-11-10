"""fetch_mirrors 多前缀策略回归测试（零依赖：python -m unittest discover -s tests）。

运行：python -m unittest discover -s tests -v
"""
import unittest
from unittest import mock

from lxhub import fetcher as f

RAW = "https://raw.githubusercontent.com/o/r/HEAD/a.js"
API = "https://api.github.com/repos/o/r"
A, B, C = "https://a.com/", "https://b.com/", "https://c.com/"


def resp(status=200, body=b"ok"):
    return {"status": status, "content": body, "truncated": False,
            "content_type": "text/plain", "error": None}


class MirrorStrategyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.orig_now = f._now
        self.clock = [1000.0]
        f._now = lambda: self.clock[0]
        # 策略参数是模块级运行时值，逐用例复位，避免改过参数的用例污染后续断言
        f.set_mirror_policy(**f.MIRROR_POLICY_DEFAULTS)
        f.set_mirrors([A, B, C])
        f.reset_mirror_state()
        f._MIRROR_IDX = 0

    def tearDown(self):
        f._now = self.orig_now
        f.set_mirror_policy(**f.MIRROR_POLICY_DEFAULTS)
        f.set_mirrors([])
        f.reset_mirror_state()
        f._MIRROR_IDX = 0

    def patch_fetch(self, handler):
        calls = []

        async def fake(client, url, max_bytes):
            calls.append(url)
            return handler(url)

        return mock.patch.object(f, "_fetch_once", fake), calls

    # ---------- 基础：清洗 / 轮换 / 适用范围 ----------

    def test_clean_pool(self):
        got = f.set_mirrors([" https://a.com ", "", "https://a.com/", B, None])
        self.assertEqual(got, [A, B])

    def test_round_robin_and_non_github_url(self):
        f.set_mirrors([A, B, C])
        seq = [f.mirror_url(RAW).split("/https://")[0] + "/" for _ in range(4)]
        self.assertEqual(seq, [A, B, C, A])
        self.assertEqual(f.mirror_url(API), API)  # 不改写、也不推进序号
        self.assertEqual(f.mirror_url(RAW), B + RAW)  # 序号接着走，不因非 GitHub URL 重置

    async def test_non_github_goes_direct(self):
        p, calls = self.patch_fetch(lambda u: resp())
        with p:
            r = await f.fetch_limited(None, API, 1000)
        self.assertEqual(calls, [API])
        self.assertEqual(r["status"], 200)

    # ---------- 单请求兜底 ----------

    async def test_retryable_failure_uses_next_mirror(self):
        p, calls = self.patch_fetch(lambda u: resp(502) if u.startswith(A) else resp())
        with p:
            r = await f.fetch_limited(None, RAW, 1000)
        self.assertEqual(calls, [A + RAW, B + RAW])
        self.assertEqual(r["status"], 200)
        self.assertEqual(f.mirror_health()[A]["fails"], 1)
        self.assertEqual(f.mirror_health()[B]["fails"], 0)

    async def test_404_does_not_retry_or_fallback(self):
        p, calls = self.patch_fetch(lambda u: resp(404))
        with p:
            r = await f.fetch_limited(None, RAW, 1000)
        self.assertEqual(calls, [A + RAW])
        self.assertEqual(r["status"], 404)

    async def test_empty_2xx_is_retryable(self):
        p, calls = self.patch_fetch(
            lambda u: resp(200, b"") if u.startswith(A) else resp())
        with p:
            r = await f.fetch_limited(None, RAW, 1000)
        self.assertEqual(calls, [A + RAW, B + RAW])
        self.assertTrue(r["content"])

    async def test_all_candidates_fail_then_direct(self):
        def handler(u):
            return resp() if u == RAW else resp(502)
        p, calls = self.patch_fetch(handler)
        with p:
            r = await f.fetch_limited(None, RAW, 1000)
        self.assertEqual(calls, [A + RAW, B + RAW, RAW])
        self.assertEqual(r["status"], 200)

    async def test_network_error_is_retryable(self):
        p, calls = self.patch_fetch(
            lambda u: resp(0, b"") if u.startswith(A) else resp())
        with p:
            r = await f.fetch_limited(None, RAW, 1000)
        self.assertEqual(calls, [A + RAW, B + RAW])
        self.assertEqual(r["status"], 200)

    # ---------- 熔断 / 半开 ----------

    async def test_quarantine_after_consecutive_failures(self):
        # 只有 A 坏、B 正常：补试成功不代表 A 被洗白，累计 3 次应单独摘除 A
        p, _ = self.patch_fetch(
            lambda u: resp(502) if u.startswith(A) else resp())
        with p:
            for _ in range(3):
                f._MIRROR_IDX = 0  # 固定让 A 作起点，累计连续失败
                await f.fetch_limited(None, RAW, 1000)
        self.assertGreater(f.mirror_health()[A]["quarantine_left"], 0)
        self.assertEqual(f.mirror_health()[B]["fails"], 0)
        self.assertEqual(f.mirror_health()[B]["quarantine_left"], 0)
        # 被摘除后不再选中 A
        self.assertEqual(f.mirror_url(RAW), B + RAW)

    async def test_half_open_recovers_after_cooldown(self):
        p, _ = self.patch_fetch(lambda u: resp(502))
        with p:
            for _ in range(3):
                f._MIRROR_IDX = 0
                await f.fetch_limited(None, RAW, 1000)

        self.clock[0] += f.QUARANTINE_SECONDS + 1
        f._MIRROR_IDX = 0
        good, calls = self.patch_fetch(lambda u: resp())
        with good:
            self.assertEqual(f.mirror_url(RAW), A + RAW)  # 半开候选
            f._MIRROR_IDX = 0
            r = await f.fetch_limited(None, RAW, 1000)
        self.assertEqual(r["status"], 200)
        self.assertEqual(f.mirror_health()[A]["fails"], 0)
        self.assertEqual(f.mirror_health()[A]["quarantine_left"], 0)

    async def test_half_open_failure_re_quarantines(self):
        p, _ = self.patch_fetch(lambda u: resp(502))
        with p:
            for _ in range(3):
                f._MIRROR_IDX = 0
                await f.fetch_limited(None, RAW, 1000)
        self.clock[0] += f.QUARANTINE_SECONDS + 1
        f._MIRROR_IDX = 0
        with p:
            await f.fetch_limited(None, RAW, 1000)
        self.assertGreater(f.mirror_health()[A]["quarantine_left"], 0)

    async def test_all_quarantined_goes_direct(self):
        for prefix in (A, B, C):
            f._QUARANTINE[prefix] = self.clock[0] + f.QUARANTINE_SECONDS
        p, calls = self.patch_fetch(lambda u: resp())
        with p:
            r = await f.fetch_limited(None, RAW, 1000)
        self.assertEqual(calls, [RAW])
        self.assertEqual(r["status"], 200)

    def test_set_mirrors_clears_removed_state(self):
        f._FAILS[B] = 2
        f._QUARANTINE[B] = self.clock[0] + 100
        f.set_mirrors([A, C])
        self.assertNotIn(B, f.mirror_health())
        self.assertEqual(f.mirror_health()[A]["fails"], 0)

    # ---------- 策略参数后台热更新 ----------

    def test_set_mirror_policy_clamps_and_is_idempotent(self):
        f._FAILS[A] = 2
        got = f.set_mirror_policy(max_attempts=1)
        self.assertEqual(got["max_attempts"], 1)
        self.assertEqual(f.mirror_health()[A]["fails"], 0)  # 值变化 → 重置健康状态

        f._FAILS[A] = 2
        f.set_mirror_policy(max_attempts=1)  # 值没变 → 幂等，不洗白坏前缀
        self.assertEqual(f.mirror_health()[A]["fails"], 2)

        got = f.set_mirror_policy(max_attempts=999, fail_threshold=0,
                                  quarantine_seconds=-5)
        self.assertEqual(got["max_attempts"], 10)
        self.assertEqual(got["fail_threshold"], 1)
        self.assertEqual(got["quarantine_seconds"], 0.0)
        # 非法输入（config.json 手滑写成字符串）回落默认而不是抛异常
        self.assertEqual(f.set_mirror_policy(max_attempts="oops")["max_attempts"], 2)

    async def test_max_attempts_one_disables_fallback(self):
        f.set_mirror_policy(max_attempts=1)
        p, calls = self.patch_fetch(lambda u: resp(502))
        with p:
            r = await f.fetch_limited(None, RAW, 1000)
        self.assertEqual(calls, [A + RAW, RAW])  # 只试 1 条前缀，随后直连
        self.assertEqual(r["status"], 502)

    async def test_max_attempts_three_tries_all(self):
        f.set_mirror_policy(max_attempts=3)
        p, calls = self.patch_fetch(lambda u: resp(502))
        with p:
            await f.fetch_limited(None, RAW, 1000)
        self.assertEqual(calls, [A + RAW, B + RAW, C + RAW, RAW])

    async def test_configurable_threshold_and_quarantine(self):
        f.set_mirror_policy(fail_threshold=1, quarantine_seconds=10)
        p, _ = self.patch_fetch(lambda u: resp(502))
        with p:
            f._MIRROR_IDX = 0
            await f.fetch_limited(None, RAW, 1000)
        ha = f.mirror_health()
        self.assertGreater(ha[A]["quarantine_left"], 0)
        self.assertLessEqual(ha[A]["quarantine_left"], 10.0)  # 新隔离时长已生效
        self.assertGreater(ha[B]["quarantine_left"], 0)       # 阈值 1，补试失败也立刻摘除

    def test_apply_mirror_config_reads_cfg(self):
        class Cfg:
            fetch_mirrors = [A, B]
            mirror_max_attempts = 1
            mirror_fail_threshold = 5
            mirror_quarantine_seconds = 60

        f.apply_mirror_config(Cfg())
        self.assertEqual(f._MIRRORS, [A, B])
        self.assertEqual(f.mirror_policy(), {
            "max_attempts": 1, "fail_threshold": 5, "quarantine_seconds": 60.0})

    def test_apply_mirror_config_is_idempotent_for_health(self):
        f._FAILS[A] = 2

        class Cfg:
            fetch_mirrors = [A, B, C]
            mirror_max_attempts = 2
            mirror_fail_threshold = 3
            mirror_quarantine_seconds = 300.0

        f.apply_mirror_config(Cfg())  # 每轮任务入口都会调，不能洗白坏前缀
        self.assertEqual(f.mirror_health()[A]["fails"], 2)


if __name__ == "__main__":
    unittest.main()
