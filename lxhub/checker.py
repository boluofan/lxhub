"""可用性复检：重新 GET 每条链接，更新存活状态/大小/哈希，并探测后端域名。

只检查"链接还能不能下载到这个脚本文件"+"后端服务器是否响应"，
不执行脚本、不请求任何取歌接口。
"""
import asyncio
import hashlib
import json

from .discover import now
from .fetcher import apply_mirror_config, fetch_limited, new_client, probe_hosts

async def run_check(cfg, db, log=print, progress=None):
    from .progress import NoOpProgress
    p = progress or NoOpProgress()
    apply_mirror_config(cfg)  # 加速前缀池 + 策略热更新（见 fetcher）
    run_id = db.new_run("check", now())
    rows = db.all_sources_rows()
    log(f"[检测] 待检测 {len(rows)} 条（含镜像）")
    if not rows:
        db.finish_run(run_id, now(), '{"checked": 0}')
        return
    client = new_client(cfg.request_timeout)
    sem = asyncio.Semaphore(cfg.concurrency)
    counters = {"done": 0, "alive": 0, "dead": 0, "changed": 0}
    probe_cache = {}  # sha256 -> 后端是否存活：内容相同的镜像只探测一次
    p.update(urls_total=len(rows), urls_done=0, alive=0, dead=0)
    p.set_phase("check", f"复检 {len(rows)} 条链接")

    async def one(row):
        async with sem:
            if p.cancelled:
                return
            r = await fetch_limited(client, row["url"], cfg.max_file_bytes)
            ok = 200 <= r["status"] < 300 and bool(r["content"])
            sha = ""
            size = 0
            if r["content"] and not r["truncated"]:
                size = len(r["content"])
                sha = hashlib.sha256(r["content"]).hexdigest()
                if row.get("sha256") and sha != row["sha256"]:
                    counters["changed"] += 1
            db.update_check(row["id"], r["status"], ok, size, sha, now())
            counters["done"] += 1
            if ok:
                counters["alive"] += 1
            else:
                counters["dead"] += 1
            p.update(urls_done=counters["done"], alive=counters["alive"],
                     dead=counters["dead"])
            # 有效音源顺带探测后端域名：结果按内容哈希缓存，再写到组内每一行。
            # 不限定 script_type==source：结构不全/混淆判定的源同样需要后端信号
            if row.get("looks_valid"):
                sha_key = row.get("sha256") or f"id:{row['id']}"
                if sha_key not in probe_cache:
                    try:
                        hosts = json.loads(row.get("api_hosts") or "[]")
                    except json.JSONDecodeError:
                        hosts = []
                    probe_cache[sha_key] = await probe_hosts(client, hosts) if hosts else None
                alive_api = probe_cache[sha_key]
                if alive_api is not None:
                    db.update_api_probe(row["id"], "up" if alive_api else "down", now())
                    p.log(f"  [后端{'活' if alive_api else '挂'}] {row['name'] or row['url']}")
            if ok:
                mark = "存活"
            else:
                # 失效时带原因：HTTP 0 = 网络不通（镜像/直连都失败），404 = 文件已删，429 = 被限流
                why = f"HTTP {r['status']}" + (f"，{r['error']}" if r["error"] else "")
                mark = f"失效({why})"
            log(f"  [{mark}] {row['url']}")

    try:
        await asyncio.gather(*(one(r) for r in rows))
        # 后端连续多轮全挂的音源视同废弃，直接清出列表；实测可用（probe usable）受保护
        removed = db.prune_dead_backends()
        if removed:
            log(f"[检测] 清理后端长期不可达记录 {removed} 条")
        # 复检完成后自动追加沙箱实测（probe.enabled 时），内容更新/未测过的优先补测
        if getattr(cfg, "probe", {}).get("enabled") and not p.cancelled:
            from .probe import run_probe
            try:
                await run_probe(cfg, db, log=log, progress=p)
            except Exception as e:
                log(f"[实测] 探测异常: {e!r}")
        counters["cancelled"] = p.cancelled
        db.finish_run(run_id, now(), json.dumps(
            {"checked": counters["done"], "alive": counters["alive"],
             "dead": counters["dead"], "changed": counters["changed"],
             "cancelled": p.cancelled}, ensure_ascii=False))
        log(f"[检测] {'已取消' if p.cancelled else '完成'}: "
            f"存活 {counters['alive']} / 失效 {counters['dead']} / 内容有更新 {counters['changed']}")
    except asyncio.CancelledError:
        # 被 /api/cancel 硬取消：补一条结束记录，否则 last_run 的 finished_at 一直是空的
        db.finish_run(run_id, now(), json.dumps(
            {"checked": counters["done"], "alive": counters["alive"],
             "dead": counters["dead"], "cancelled": True}, ensure_ascii=False))
        log("[检测] 已取消")
        raise
    except Exception as e:
        db.finish_run(run_id, now(), json.dumps({"error": repr(e)}))
        log(f"[检测] 异常退出: {e!r}")
        raise
    finally:
        await client.aclose()
