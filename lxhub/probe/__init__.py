"""沙箱实测调度：把待测音源脚本交给 Node worker 真实取歌，结果回写 probe_* 列。

状态语义：
  usable        至少一个平台取到真实音频（Range 探测 + 音频魔数通过）
  degraded      脚本可加载、完成 inited，但所有平台取歌失败
  not-loadable  脚本执行失败 / 未调用 inited
探测结果由 probe.py 持久化；内容变更（sha256 变化）会覆盖缓存强制重测。
"""
import asyncio
import datetime as dt
import json
import os
import shutil
import tempfile

from ..progress import NoOpProgress

WORKER = os.path.join(os.path.dirname(__file__), "node", "probe.mjs")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


async def probe_one(cfg, path: str, deep: bool) -> dict:
    """单脚本探测：node 子进程执行，stdout 最后一行是结果 JSON。"""
    p = cfg.probe
    env = dict(os.environ)
    env["LXH_PROBE_BUDGET"] = str(int(p["timeout"] * 1000))
    env["LXH_PROBE_QUALITIES"] = ",".join(p["qualities"])
    if p.get("allow_private"):
        env["LXH_PROBE_ALLOW_PRIVATE"] = "1"
    args = [p["node"], WORKER, path]
    if deep:
        args.append("--deep")
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=p["timeout"] + 15)
        text = (stdout or b"").decode("utf-8", "ignore").strip()
        lines = [ln for ln in text.splitlines() if ln.strip()]
        for ln in reversed(lines):
            try:
                data = json.loads(ln)
                if isinstance(data, dict) and "status" in data:
                    return data
            except json.JSONDecodeError:
                continue
        return {"status": "not-loadable", "loadable": False, "loadError": f"worker 输出异常: {text[:200]}",
                "platforms": {}, "updateAlert": None}
    except (asyncio.TimeoutError, OSError) as e:
        try:
            if proc and proc.returncode is None:
                proc.kill()
        except (OSError, NameError, UnboundLocalError):
            pass
        return {"status": "not-loadable", "loadable": False, "loadError": f"worker 异常退出: {e!r}",
                "platforms": {}, "updateAlert": None}


async def _node_version_ok(node: str) -> tuple[bool, str]:
    """node -v 一次性校验（每轮一次，成本可忽略）。

    存在但版本 <20 时同样整轮跳过：老 Node 跑不动 worker（vm/fetch 等特性
    缺失），逐条探测只会全部 not-loadable，与"找不到 Node"同样处理。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            node, "-v", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        ver = (out or b"").decode("utf-8", "ignore").strip()
        major = int(ver.lstrip("vV").split(".")[0] or 0)
        return major >= 20, ver
    except (OSError, ValueError):
        return False, ""


async def run_probe(cfg, db, log=print, progress=None, limit=None, deep=None,
                    force: bool = False) -> dict:
    p = progress or NoOpProgress()
    if not cfg.probe.get("enabled") and not force:
        reason = "未启用沙箱实测：后台「配置管理 → 沙箱实测」打开开关（手动点按钮或 CLI --yes 不受此限）"
        log(f"[实测] {reason}")
        if p is not NoOpProgress:
            p.skipped_reason = reason
        return {"skipped": True, "reason": "disabled"}
    # Docker 等运行环境可能没有 Node：此时整轮跳过、不落库。
    # 若强行逐条探测会全部 not-loadable，失败连击会导致 prune 误删真实可用音源
    node_bin = str(cfg.probe.get("node") or "node")
    if not shutil.which(node_bin):
        reason = (f"找不到 Node 可执行文件（{cfg.probe.get('node')!r}）"
                  "——容器部署需在镜像内安装 Node ≥20，或关闭沙箱实测")
        log(f"[实测] 跳过：{reason}")
        if p is not NoOpProgress:
            p.skipped_reason = reason
        return {"skipped": True, "reason": "node-not-found"}
    ver_ok, ver = await _node_version_ok(node_bin)
    if not ver_ok:
        reason = f"Node 版本不满足要求（检测到 {ver or '未知版本'}），需要 ≥20"
        log(f"[实测] 跳过：{reason}")
        if p is not NoOpProgress:
            p.skipped_reason = reason
        return {"skipped": True, "reason": "node-version"}
    limit = int(limit) if limit else int(cfg.probe["max_per_run"])
    if deep is None:
        deep = bool(cfg.probe["deep"])
    rows = db.probe_candidates(float(cfg.probe["refresh_hours"]), limit)
    log(f"[实测] 待探测 {len(rows)} 条（上限 {limit}，deep={deep}）")
    if not rows:
        return {"probed": 0}
    run_id = db.new_run("probe", now())
    p.set_phase("probe", f"沙箱实测 {len(rows)} 条")
    p.update(urls_total=len(rows), urls_done=0)
    sem = asyncio.Semaphore(int(cfg.probe["concurrency"]))
    tmp = tempfile.mkdtemp(prefix="lxh-probe-")
    counters = {"done": 0, "usable": 0, "degraded": 0, "not_loadable": 0}

    async def one(row):
        async with sem:
            if p.cancelled:
                return
            fp = os.path.join(tmp, f"src-{row['id']}.js")
            try:
                detail_row = db.get_source(row["id"])
                script = (detail_row or {}).get("content_text") or ""
                if not script.strip():
                    # 空内容也必须落库，否则 probe_candidates 每轮都会选中同一行
                    counters["done"] += 1
                    counters["not_loadable"] += 1
                    p.update(urls_done=counters["done"])
                    db.update_probe(row["id"], "not-loadable",
                                    json.dumps({"status": "not-loadable", "loadable": False,
                                                "loadError": "数据库内无脚本文本（可能抓取时截断）",
                                                "platforms": {}, "updateAlert": None},
                                               ensure_ascii=False),
                                    row.get("sha256") or "", now())
                    log(f"  [实测·空内容] {row['url']}")
                    return
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(script)
                r = await probe_one(cfg, fp, deep)
            finally:
                try:
                    os.remove(fp)
                except OSError:
                    pass
            status = str(r.get("status") or "not-loadable")
            if status == "not_loadable":
                status = "not-loadable"
            counters["done"] += 1
            key = {"usable": "usable", "degraded": "degraded"}.get(status, "not_loadable")
            counters[key] += 1
            db.update_probe(row["id"], status, json.dumps(r, ensure_ascii=False),
                            row.get("sha256") or "", now())
            # 实时把可用/降级/失败计数推给前端进度，避免汇总消息永远显示「—」
            p.update(urls_done=counters["done"],
                     usable=counters["usable"],
                     degraded=counters["degraded"],
                     not_loadable=counters["not_loadable"])
            extra = ""
            if r.get("updateAlert"):
                extra = f" | 更新提醒: {r['updateAlert'].get('log', '')[:60]}"
            p.log(f"  [实测·{status}] {row['name'] or row['url']}"
                  + ("".join(f" {k}:{v['status']}" for k, v in (r.get("platforms") or {}).items()))
                  + extra)

    try:
        await asyncio.gather(*(one(r) for r in rows))
        db.finish_run(run_id, now(), json.dumps(counters, ensure_ascii=False))
        log(f"[实测] {'已取消' if p.cancelled else '完成'}: "
            + " / ".join(f"{k} {v}" for k, v in counters.items() if k != "done"))
        # 置信度刚更新，立刻刷新本地化文件；CLI / Web / 复检内嵌三条路径都覆盖
        _maybe_export(cfg, db, log)
        return {"probed": counters["done"], **counters, "cancelled": p.cancelled}
    except asyncio.CancelledError:
        db.finish_run(run_id, now(), json.dumps({**counters, "cancelled": True},
                                                ensure_ascii=False))
        raise


def _maybe_export(cfg, db, log=print) -> None:
    """实测后自动刷新本地化文件（export.enabled + export.after_probe 时）。

    导出是纯本地 IO 且很短，失败只记日志，绝不影响实测任务本身的结果。
    """
    e = getattr(cfg, "export", None) or {}
    if not e.get("enabled") or not e.get("after_probe"):
        return
    try:
        from .exporter import run_export
        r = run_export(cfg, db, trigger="after-probe", log=log)
        log(f"[本地化] 已刷新 {r['written']} 个固定文件 → {r['dir']}")
    except Exception as e:
        log(f"[本地化] 实测后导出失败: {e!r}")
