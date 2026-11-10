"""「实测可用」音源的置信度评分（0–100）。

沙箱实测只能回答"能不能取到歌"，但 usable 之间差别很大：有的 5 个平台全通且
无损核验达标，有的只有 1 个平台勉强返回 128k。这里把 probe_detail 里的证据转成
一个可解释的分数，供列表排序、同名组选主记录、副本收敛使用。

评分只对 probe_status='usable' 的记录生效，其余状态一律 0 分（不改原有
"可用 > 降级 > 未测 > 失败" 的大框架，只是在 usable 之间拉开档次）。

分项（上限合计正好 100）：
  base        25  实测确实取到真实音频
  platforms   35  成功平台数，每个 7 分，最多 5 个（广度）
  coverage    10  成功率 = 成功平台数 / 实际测过的平台数
  quality     20  L2 音质核验最佳结论；未做 L2 给中性分 8
  spec         5  真实音频规格（无损/高码率 5，仅核验到普通规格 2）
  freshness    5  实测时间新鲜度（7 天内满分，30 天以上归零）
  penalty     -5  脚本自带"请更新"提醒（当前版本可能已过期）

权重集中在本模块顶部常量，改这里即可整体调档，无需改数据库或前端。
"""
from __future__ import annotations

import datetime as dt
import json

BASE_SCORE = 25
PLATFORM_UNIT = 7
PLATFORM_CAP = 5
COVERAGE_MAX = 10
QUALITY_POINTS = {
    "matched": 20,     # 声明音质与实际容器参数一致
    "degraded": 10,    # 声明无损，实际降级
    "unknown": 8,      # 取到链但读不出规格
    "mismatch": 2,     # 规格明显对不上
    "refused": 0,      # 拒绝核验 / 取链失败
}
QUALITY_UNVERIFIED = 8  # 未开 deep 或 worker 没写 quality：中性分，不奖不罚
SPEC_HIGH = 5
SPEC_VERIFIED = 2
FRESH_MAX = 5
FRESH_FULL_DAYS = 7
FRESH_MAX_DAYS = 30
UPDATE_ALERT_PENALTY = 5
MAX_SCORE = 100

QUALITY_LABELS = {
    "matched": "达标", "degraded": "降级", "unknown": "未判定",
    "mismatch": "异常", "refused": "拒绝核验", "unverified": "未核验",
}


def _as_dict(value) -> dict:
    """probe_detail 在 DB 里是 TEXT、在内存里是 dict，两种都吃得下。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes)):
        try:
            d = json.loads(value)
            return d if isinstance(d, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _parse_time(s: str) -> dt.datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        t = dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _all_specs(detail: dict) -> list:
    """L1 的 platforms[*].spec + L2 的 l2[*].claims[*].spec。"""
    out = []
    for entry in (detail.get("platforms") or {}).values():
        if isinstance(entry, dict) and isinstance(entry.get("spec"), dict):
            out.append(entry["spec"])
    l2_all = detail.get("l2")
    for l2 in (l2_all.values() if isinstance(l2_all, dict) else []):
        if not isinstance(l2, dict):
            continue
        claims = l2.get("claims") if isinstance(l2.get("claims"), list) else []
        for claim in claims:
            if isinstance(claim, dict) and isinstance(claim.get("spec"), dict):
                out.append(claim["spec"])
    return out


def _quality_points(detail: dict, usable: list) -> tuple[int, str]:
    """取"最佳平台"的音质结论：一个平台能出真无损，即证明脚本有这个能力。"""
    verdicts = [str(r.get("quality")) for r in usable
                if isinstance(r, dict) and r.get("quality")]
    l2_all = detail.get("l2")
    for l2 in (l2_all.values() if isinstance(l2_all, dict) else []):
        if isinstance(l2, dict) and l2.get("verdict"):
            verdicts.append(str(l2["verdict"]))
    if not verdicts:
        return QUALITY_UNVERIFIED, "unverified"
    best = max(verdicts, key=lambda v: QUALITY_POINTS.get(v, 0))
    return QUALITY_POINTS.get(best, 0), best


def _spec_points(specs: list) -> tuple[int, str]:
    if not specs:
        return 0, "none"
    high = any(
        (str(s.get("container") or "").lower() == "flac" and not s.get("specUnverified"))
        or _num(s.get("bitDepth")) >= 24
        or _num(s.get("avgBitrateKbps")) >= 320
        or (_num(s.get("sampleRate")) >= 48000 and _num(s.get("bitDepth")) >= 16)
        for s in specs
    )
    if high:
        return SPEC_HIGH, "high"
    verified = any(s.get("container") or _num(s.get("bitrate")) > 0 for s in specs)
    return (SPEC_VERIFIED, "verified") if verified else (0, "none")


def _freshness(checked_at: str, now: dt.datetime) -> int:
    t = _parse_time(checked_at)
    if t is None:
        return 0
    days = (now - t).total_seconds() / 86400
    if days <= FRESH_FULL_DAYS:          # 含时钟偏差导致的"未来时间"
        return FRESH_MAX
    if days >= FRESH_MAX_DAYS:
        return 0
    return round(FRESH_MAX * (FRESH_MAX_DAYS - days) / (FRESH_MAX_DAYS - FRESH_FULL_DAYS))


def score_probe(status: str, detail=None, checked_at: str = "",
                now: dt.datetime | None = None) -> dict:
    """算出置信度分数与分项明细，结果可直接 JSON 落库。

    返回：
      {"score": int, "parts": {...}, "success": n, "tested": n,
       "quality_verdict": str, "spec_level": str, "reason": str}
    """
    if status != "usable":
        return {"score": 0, "parts": {}, "success": 0, "tested": 0,
                "quality_verdict": "", "spec_level": "",
                "reason": status or "unprobed"}

    d = _as_dict(detail)
    if not d:
        # 实测通过但明细丢失（旧库/异常 worker）：只给基础分，不凭空补音质与新鲜度
        return {"score": BASE_SCORE, "parts": {"base": BASE_SCORE},
                "success": 0, "tested": 0, "quality_verdict": "",
                "spec_level": "", "reason": "detail-missing"}
    now = now or dt.datetime.now(dt.timezone.utc)
    platforms = d.get("platforms") if isinstance(d.get("platforms"), dict) else {}
    entries = [r for r in platforms.values() if isinstance(r, dict)]
    usable = [r for r in entries if r.get("status") == "usable"]
    success = len(usable)
    # 覆盖率分母用"实际测过的平台数"：声明了 10 个但 worker 只跑了 5 个时，
    # 不能按 10 个去摊薄成功率。
    tested = len(entries) or success or 1
    quality, verdict = _quality_points(d, usable)
    spec, spec_level = _spec_points(_all_specs(d))
    fresh = _freshness(checked_at, now)
    alert = d.get("updateAlert")
    penalty = UPDATE_ALERT_PENALTY if isinstance(alert, dict) and alert.get("log") else 0

    parts = {
        "base": BASE_SCORE,
        "platforms": min(success, PLATFORM_CAP) * PLATFORM_UNIT,
        "coverage": round(COVERAGE_MAX * min(success, tested) / tested),
        "quality": quality,
        "spec": spec,
        "freshness": fresh,
        "penalty": -penalty,
    }
    score = max(0, min(MAX_SCORE, sum(parts.values())))
    return {
        "score": score,
        "parts": parts,
        "success": success,
        "tested": tested,
        "quality_verdict": verdict,
        "spec_level": spec_level,
        "reason": "detail-missing" if not d else "",
    }
