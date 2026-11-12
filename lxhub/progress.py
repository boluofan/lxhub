"""任务进度对象：阶段、计数器、实时日志环形缓冲、取消标记。

爬取/检测任务在后台协程里运行，通过它向 /api/progress 提供快照。
"""
import time
from collections import deque


class Progress:
    def __init__(self, kind: str):
        self.kind = kind
        self.phase = "prepare"
        self.message = "准备中…"
        self.stats: dict = {}
        self.logs: deque = deque(maxlen=100)
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.cancelled = False
        self.skipped_reason: str | None = None

    def set_phase(self, phase: str, message: str = ""):
        self.phase = phase
        if message:
            self.message = message

    def update(self, **kv):
        self.stats.update(kv)

    def log(self, line: str):
        self.logs.append(f"[{time.strftime('%H:%M:%S')}] {line}")

    def snapshot(self) -> dict:
        return {
            "running": self.finished_at is None,
            "kind": self.kind,
            "phase": self.phase,
            "message": self.message,
            "stats": self.stats,
            "logs": list(self.logs)[-60:],
            "started_at": self.started_at,
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 1),
            "cancelled": self.cancelled,
            "skipped_reason": self.skipped_reason,
        }


class NoOpProgress:
    """CLI 场景下的空实现。"""

    kind = "none"
    cancelled = False

    def set_phase(self, phase, message=""):
        pass

    def update(self, **kv):
        pass

    def log(self, line):
        pass

    def snapshot(self):
        return {"running": False}
