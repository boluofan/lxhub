"""FastAPI 服务：API + 静态页面 + 内置定时任务（爬取/复检）。"""
import asyncio
import datetime as dt
import io
import json
import os
import re
import secrets
import shutil
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import checker, discover, exporter, fetcher
from .config import (DEFAULTS, EDITABLE, NESTED_CONFIGS, cast_bool,
                     load_config, override_path, validate_filename_template)
from .db import DB
from .progress import Progress

cfg = load_config()
db = DB(cfg.db_path)
# 启动即应用 config.json / override 里的加速前缀与策略，不必等第一轮任务
fetcher.apply_mirror_config(cfg)
# Vue 前端（frontend/dist）产物目录：默认在仓库根 frontend/dist，可用环境变量覆盖
WEBUI_DIST = Path(
    os.environ.get("LXH_WEBUI_DIST")
    or Path(__file__).resolve().parent.parent / "frontend" / "dist"
)

def _webui_index() -> Response:
    """页面：返回 Vue 构建产物；未构建时提示构建命令。"""
    index = WEBUI_DIST / "index.html"
    if index.is_file():
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
    return Response(
        "后台未构建：请在 frontend/ 目录执行 npm run build",
        status_code=404,
        media_type="text/html; charset=utf-8",
    )

# ---------- 后台管理（可选，由环境变量 LXH_ADMIN_USER / LXH_ADMIN_PASS 启用） ----------
ADMIN_USER = os.environ.get("LXH_ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("LXH_ADMIN_PASS", "123456")
SESSIONS: dict = {}
SESSION_TTL = 8 * 3600
# 后台保存的覆盖配置落盘处（与数据库同目录）；加载逻辑在 config.apply_overrides，
# CLI 与 Web 启动时都会应用，保证两条轨道配置一致
OVERRIDE_PATH = override_path(cfg.db_path)

# 后台表单只填一个 URL，不填生效域名时的默认值：
# 本项目收录的链接均来自 GitHub，加速前缀也只对这些镜像站有意义，
# 不兜底的话前缀会被拼到任何链接上（万一混入非 GitHub 链接就打不开了）。
DEFAULT_MIRROR_HOSTS = ("github.com", "githubusercontent.com", "github.io")


def _norm_mirror(v):
    """归一化一条加速前缀。

    后台只填 URL（字符串）即可；name 从域名推导，hosts 缺省用 DEFAULT_MIRROR_HOSTS。
    需要自定义 hosts 的场景直接改 config.json。
    """
    if isinstance(v, str):
        p = v.strip()
        if not p:
            raise ValueError("不能为空")
        explicit_hosts = None
    elif isinstance(v, dict):
        p = str(v.get("prefix") or "").strip()
        if not p:
            raise ValueError("缺少 prefix 字段")
        explicit_hosts = [str(h).strip() for h in (v.get("hosts") or []) if str(h).strip()]
    else:
        raise ValueError("必须是 URL 字符串或对象")
    if "://" not in p and not p.endswith("="):
        # 只填了域名（如 gh1.lhl.one）时补全协议，否则拼出来的是无效相对路径
        p = "https://" + p
    if not p.endswith(("/", "=")):
        p += "/"
    name = str(v.get("name") or "") if isinstance(v, dict) else ""
    if not name:
        name = urlparse(p).netloc or p
    hosts = DEFAULT_MIRROR_HOSTS if explicit_hosts is None else explicit_hosts
    return {"name": name, "prefix": p, "hosts": list(hosts)}


def _get_value(key: str):
    """EDITABLE 平铺键 → 实际配置：NESTED_CONFIGS 里的组读子字典，其余读 cfg 属性。"""
    for group in NESTED_CONFIGS:
        prefix = group + "."
        if key.startswith(prefix):
            return (getattr(cfg, group, None) or {}).get(key[len(prefix):])
    return getattr(cfg, key)


def _set_value(key: str, value):
    for group in NESTED_CONFIGS:
        prefix = group + "."
        if key.startswith(prefix):
            block = dict(getattr(cfg, group, None) or {})
            block[key[len(prefix):]] = value
            setattr(cfg, group, block)
            return
    setattr(cfg, key, value)


def _save_overrides(data: dict):
    """把变更**合并**进既有 override 文件。

    必须合并而非覆盖：后台可能只提交部分字段，整体覆盖会清掉其余已保存配置
    （曾导致只改一个开关就把 github_token 等全部抹掉）。
    NESTED_CONFIGS 里的平铺键（probe.* / export.*）在落盘前先合并成各自的
    嵌套块，与 config.json / load_config 的读取语义保持一致。
    """
    try:
        cur: dict = {}
        try:
            cur = json.loads(OVERRIDE_PATH.read_text(encoding="utf-8"))
            if not isinstance(cur, dict):
                cur = {}
        except (OSError, json.JSONDecodeError):
            cur = {}
        blocks: dict = {}
        for k, v in data.items():
            group = next((g for g in NESTED_CONFIGS
                          if k.startswith(g + ".")), None)
            if group:
                blocks.setdefault(group, {})[k[len(group) + 1:]] = v
            else:
                cur[k] = v
        for group, patch in blocks.items():
            merged = dict(cur.get(group) or {})
            merged.update(patch)
            cur[group] = merged
        OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
        OVERRIDE_PATH.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as e:
        print(f"[admin] 保存配置失败: {e!r}")

def _clean_sessions():
    now = time.time()
    expired = [t for t, e in SESSIONS.items() if e < now]
    for t in expired:
        del SESSIONS[t]

def _check_admin(req: Request):
    """未设置 LXH_ADMIN_PASS 时放行（保持默认部署的公开触发行为）；
    设置后则要求 Bearer token，否则 401。"""
    if not ADMIN_PASS:
        return
    auth = req.headers.get("Authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    _clean_sessions()
    if not token or SESSIONS.get(token, 0) < time.time():
        raise HTTPException(401, "未登录或会话已过期")


def _base_name(url: str) -> str:
    name = (url or "").rstrip("/").split("/")[-1]
    return name or "source"


def _safe_filename(name: str) -> str:
    name = (name or "source").strip()
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(". ")
    return name[:80] or "source"


STATE = {"busy": None, "task": None}  # None | "crawl" | "check" | "probe"
PROGRESS: Progress | None = None


def _hours_since(iso: str) -> float:
    if not iso:
        return 1e9
    try:
        t = dt.datetime.fromisoformat(iso)
    except ValueError:
        return 1e9
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 3600


async def _run_task(kind: str, force: bool = False):
    global PROGRESS
    if STATE["busy"]:
        return
    STATE["busy"] = kind
    # 记住当前协程：/api/cancel 才能立刻中断进行中的 HTTP 请求，
    # 否则只能等 discover / collect 阶段自己跑到下一个检查点
    STATE["task"] = asyncio.current_task()
    PROGRESS = Progress(kind)

    def _log(msg: str):
        print(msg)
        PROGRESS.log(msg)

    try:
        if kind == "crawl":
            await discover.run_crawl(cfg, db, log=_log, progress=PROGRESS)
        elif kind == "probe":
            from .probe import run_probe
            await run_probe(cfg, db, log=_log, progress=PROGRESS, force=force)
        else:
            await checker.run_check(cfg, db, log=_log, progress=PROGRESS)
    except asyncio.CancelledError:
        # 取消动作已由 run_crawl / run_check 落库，这里只补日志。
        # 不向上抛：本协程是任务终点，抛出去会让调用方跟着被取消
        print(f"[任务] {kind} 已取消")
        PROGRESS.log("任务已取消")
    except Exception as e:
        print(f"[任务] {kind} 失败: {e!r}")
        PROGRESS.log(f"任务失败: {e!r}")
    finally:
        PROGRESS.finished_at = time.time()
        STATE["busy"] = None
        STATE["task"] = None


async def _scheduler():
    """每 15 分钟醒来检查一次是否到了爬取/复检的时间。"""
    while True:
        await asyncio.sleep(900)
        try:
            if STATE["busy"]:
                continue
            crawl_run = db.last_run("crawl")
            if crawl_run and not crawl_run["finished_at"] and _hours_since(crawl_run["started_at"]) < 6:
                pass  # 上次爬取疑似未结束，等它
            elif not crawl_run or _hours_since(crawl_run["finished_at"] or crawl_run["started_at"]) >= cfg.crawl_interval_hours:
                # 包成独立 task：_run_task 里 asyncio.current_task() 拿到的才是它自己，
                # 被 /api/cancel 取消时不会连调度器一起取消
                await asyncio.create_task(_run_task("crawl"))
                continue
            check_run = db.last_run("check")
            if not check_run or _hours_since(check_run["finished_at"] or check_run["started_at"]) >= cfg.check_interval_hours:
                await asyncio.create_task(_run_task("check"))
                continue
            # 沙箱实测定时任务：probe.enabled 时按 interval_hours 周期触发（0 = 不定时）
            if (cfg.probe or {}).get("enabled"):
                interval = float(cfg.probe.get("interval_hours") or 0)
                if interval > 0:
                    probe_run = db.last_run("probe")
                    if not probe_run or _hours_since(
                            probe_run["finished_at"] or probe_run["started_at"]) >= interval:
                        await asyncio.create_task(_run_task("probe"))
            # 本地化导出定时：纯本地 IO，不占用 STATE["busy"]；与 probe 共用 runs 表
            # 判周期，实测后自动导出会刷新 last_run，因此不会在同一轮重复写。
            if (cfg.export or {}).get("enabled"):
                interval = float(cfg.export.get("interval_hours") or 0)
                if interval > 0:
                    exp_run = db.last_run("export")
                    if not exp_run or _hours_since(
                            exp_run["finished_at"] or exp_run["started_at"]) >= interval:
                        try:
                            await asyncio.to_thread(exporter.run_export, cfg, db, "schedule")
                        except Exception as e:
                            print(f"[本地化] 定时导出失败: {e!r}")
        except Exception as e:
            print(f"[定时器] {e!r}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_scheduler())
    yield
    task.cancel()


app = FastAPI(title="LX Source Hub", lifespan=lifespan)


@app.get("/")
async def index():
    return _webui_index()


@app.get("/admin")
async def admin_page():
    return _webui_index()


@app.get("/login")
async def login_page():
    return _webui_index()


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/sources")
async def api_sources():
    return {"sources": db.list_sources(), "mirror_prefixes": cfg.mirror_prefixes}


@app.get("/api/sources/{sid}")
async def api_source_detail(sid: int):
    row = db.get_source(sid)
    if not row:
        raise HTTPException(404, "not found")
    return row


def _probe_capability() -> dict:
    """沙箱实测可用性（供 /api/status 暴露，前端据此禁用入口）。

    只做存在性探测：/api/status 会被前端高频轮询，起子进程查版本成本太高。
    版本下限（≥20）校验放在 run_probe 每轮开始时做一次，见 probe/__init__.py。
    """
    node = str((cfg.probe or {}).get("node") or "node")
    if shutil.which(node):
        return {"available": True, "node": node, "reason": None}
    return {"available": False, "node": node, "reason": "node-not-found"}


@app.get("/api/status")
async def api_status():
    return {
        "busy": STATE["busy"],
        "stats": db.stats(),
        "last_crawl": db.last_run("crawl"),
        "last_check": db.last_run("check"),
        "last_probe": db.last_run("probe"),
        "last_export": db.last_run("export"),
        "probe_capability": _probe_capability(),
    }


@app.get("/api/progress")
async def api_progress():
    if PROGRESS is None:
        return {"running": False, "kind": None}
    return PROGRESS.snapshot()


@app.post("/api/cancel")
async def api_cancel(req: Request):
    _check_admin(req)
    if not STATE["busy"]:
        raise HTTPException(409, "没有进行中的任务")
    # 先置软取消标志：让任务在自己可控的检查点优雅退出
    if PROGRESS is not None:
        PROGRESS.cancelled = True
    # 再硬取消：直接中断协程，立刻停止正在等待的 HTTP 请求。
    # 只影响任务协程本身（_run_task 会吞掉 CancelledError），调度器不受牵连
    task = STATE.get("task")
    if task is not None and not task.done():
        task.cancel()
    return {"ok": True}


@app.post("/api/crawl")
async def api_crawl(req: Request):
    _check_admin(req)
    if STATE["busy"]:
        raise HTTPException(409, f"任务进行中: {STATE['busy']}")
    asyncio.create_task(_run_task("crawl"))
    return {"started": True}


@app.post("/api/check")
async def api_check(req: Request):
    _check_admin(req)
    if STATE["busy"]:
        raise HTTPException(409, f"任务进行中: {STATE['busy']}")
    asyncio.create_task(_run_task("check"))
    return {"started": True}


@app.post("/api/probe")
async def api_probe(req: Request):
    """手动触发一轮沙箱实测。

    不要求 probe.enabled：开关只控制「定时任务 + 检测后自动补测」，
    后台手动点按钮即视为明确意图。运行环境缺 Node（或版本 <20）时
    run_probe 会整轮跳过、不落库，不会把可用音源误判为失效；
    可用性通过 /api/status 的 probe_capability 暴露给前端。
    """
    _check_admin(req)
    if STATE["busy"]:
        raise HTTPException(409, f"任务进行中: {STATE['busy']}")
    # 手动触发绕过 probe.enabled：开关只控制定时任务 + 复检后自动补测，
    # 后台点按钮即视为明确意图（与 api_probe 文档、CLI --yes 一致）
    asyncio.create_task(_run_task("probe", force=True))
    return {"started": True}


@app.get("/export.json")
async def export_json():
    return JSONResponse(db.export(cfg.mirror_prefixes))


@app.get("/api/sources/{sid}/download")
async def api_download(sid: int):
    """下载单个音源脚本（完整内容，来自数据库中保存的 content_text）。"""
    row = db.get_source(sid)
    if not row:
        raise HTTPException(404, "not found")
    content = (row.get("content_text") or "").strip()
    if not content:
        raise HTTPException(404, "该记录没有可下载的脚本内容")
    filename = _safe_filename(row.get("name") or _base_name(row["url"])) + ".js"
    return Response(
        content.encode("utf-8"),
        media_type="text/javascript; charset=utf-8",
        headers={
            "Content-Disposition":
                f"attachment; filename=\"{_ascii_fallback(filename)}\"; "
                f"filename*=UTF-8''{quote(filename)}"
        },
    )


@app.get("/api/export/zip")
async def api_export_zip(ids: str = ""):
    """将（筛选后）音源脚本打包成一个 ZIP 下载。ids 为空则导出全部。"""
    rows = db.all_sources_rows(with_content=True)
    if ids:
        want = {int(x) for x in ids.split(",") if x.strip().isdigit()}
        if want:
            rows = [r for r in rows if r["id"] in want]
    buf = io.BytesIO()
    seen: set = set()
    written = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for r in rows:
            content = (r.get("content_text") or "").strip()
            if not content:
                continue
            base = _safe_filename(r.get("name") or _base_name(r["url"]))
            fname = f"{base}.js"
            n = 1
            while fname in seen:
                fname = f"{base}_{n}.js"
                n += 1
            seen.add(fname)
            z.writestr(fname, content.encode("utf-8"))
            written += 1
    if written == 0:
        raise HTTPException(404, "没有可下载的脚本内容")
    buf.seek(0)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=\"lx-sources-{stamp}.zip\""},
    )


@app.get("/api/export/local")
async def api_local_export_status():
    """本地化输出状态：配置摘要、已导出的固定文件清单、最近一轮记录。"""
    return exporter.local_status(cfg, db)


@app.post("/api/export/local")
async def api_local_export_run(req: Request):
    """立即导出一轮（不受 export.enabled 限制，方便先验证再开自动）。"""
    _check_admin(req)
    try:
        result = await asyncio.to_thread(exporter.run_export, cfg, db, "manual")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise HTTPException(500, f"导出失败: {e!r}")
    return {"ok": True, **result}


@app.get("/local/{filename}")
async def local_source(filename: str):
    """对外提供固定文件名的只读访问（如 /local/lx-source-1.js）。

    文件名严格白名单 + 目录内校验，避免路径穿越；短缓存让覆盖写入后
    客户端能较快拿到新脚本，又不会永久缓存旧内容。
    """
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.js", filename):
        raise HTTPException(404, "Not Found")
    d = exporter.export_dir()
    target = (d / filename).resolve()
    if not target.is_relative_to(d) or not target.is_file():
        raise HTTPException(404, "Not Found")
    return FileResponse(target, media_type="text/javascript; charset=utf-8",
                        headers={"Cache-Control": "public, max-age=60"})


@app.post("/api/admin/login")
async def admin_login(req: Request):
    if not ADMIN_PASS:
        raise HTTPException(403, "后台未启用：请在环境变量设置 LXH_ADMIN_USER / LXH_ADMIN_PASS 启用网页后台")
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象")
    user = (body.get("username") or "").strip()
    passwd = (body.get("password") or "").strip()
    if user != ADMIN_USER or passwd != ADMIN_PASS:
        raise HTTPException(401, "账户名或密码错误")
    token = secrets.token_hex(24)
    SESSIONS[token] = time.time() + SESSION_TTL
    return {"token": token, "expires_in": SESSION_TTL}


@app.post("/api/admin/logout")
async def admin_logout(req: Request):
    auth = req.headers.get("Authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    SESSIONS.pop(token, None)
    return {"ok": True}


@app.get("/api/admin/config")
async def admin_get(req: Request):
    _check_admin(req)
    data = {}
    for k in EDITABLE:
        try:
            data[k] = _get_value(k)
        except Exception:
            data[k] = None
    # 不回传真实 token：否则打开后台点一下保存，环境变量里的 token 就会被明文写进数据卷
    data["github_token"] = ""
    return {
        "config": data,
        "github_token_set": bool(cfg.github_token),
        "admin_enabled": True,
    }


@app.post("/api/admin/config")
async def admin_set(req: Request):
    _check_admin(req)
    try:
        partial = await req.json()
    except Exception:
        raise HTTPException(400, "请求体不是合法 JSON")
    if not isinstance(partial, dict):
        raise HTTPException(400, "请求体必须是对象")
    # 宽容形态：fetch_mirrors 允许 curl 直接传多行字符串（后台 lines 组件发的是数组）
    if isinstance(partial.get("fetch_mirrors"), str):
        partial = dict(partial)
        partial["fetch_mirrors"] = [x.strip() for x in partial["fetch_mirrors"].splitlines()]
    applied = {}
    for k, cast in EDITABLE.items():
        if k not in partial:
            continue
        v = partial[k]
        try:
            if cast is bool:
                applied[k] = cast_bool(v)
            elif cast is list:
                if not isinstance(v, list):
                    raise ValueError("必须是数组")
                applied[k] = v
            else:
                applied[k] = cast(v)
        except (ValueError, TypeError) as e:
            raise HTTPException(400, f"字段 {k} 无效: {e}")
    if "mirror_prefixes" in applied:
        try:
            applied["mirror_prefixes"] = [_norm_mirror(x) for x in applied["mirror_prefixes"]]
        except ValueError as e:
            raise HTTPException(400, f"字段 mirror_prefixes 无效: {e}")
    if "fetch_mirrors" in applied:
        # 校验为可拼接的 http(s) 前缀：gh-proxy.com 这类服务是 URL 前缀镜像而非 HTTP 代理
        items = []
        for x in applied["fetch_mirrors"]:
            s = str(x).strip()
            if not s:
                continue
            if not s.lower().startswith(("http://", "https://")):
                raise HTTPException(400, f"fetch_mirrors 含不合法的加速前缀: {s}（应为 http(s):// 开头的镜像地址，如 https://gh-proxy.com/）")
            if not s.endswith("/"):
                s += "/"
            items.append(s)
        applied["fetch_mirrors"] = items
    if "probe.qualities" in applied:
        qs = [str(x).strip() for x in applied["probe.qualities"] if str(x).strip()]
        bad = [q for q in qs if q not in ("128k", "flac", "flac24bit")]
        if bad:
            raise HTTPException(400, f"probe.qualities 含不支持的音质: {bad}（可选 128k/flac/flac24bit）")
        applied["probe.qualities"] = qs
    if "probe.node" in applied and not str(applied["probe.node"]).strip():
        applied["probe.node"] = "node"  # 空输入回落默认，避免启动 worker 时找不到可执行文件
    _probe_mins = {"probe.concurrency": 1, "probe.max_per_run": 0, "probe.timeout": 10,
                   "probe.interval_hours": 0, "probe.refresh_hours": 0}
    for k, lo in _probe_mins.items():
        if k in applied and applied[k] < lo:
            raise HTTPException(400, f"字段 {k} 不能小于 {lo}")
    # 本地化输出：文件名模板必须只由名次决定，否则对外 URL 会随数据漂移
    if "export.filename" in applied:
        tpl = str(applied["export.filename"]).strip() or DEFAULTS["export"]["filename"]
        try:
            applied["export.filename"] = validate_filename_template(tpl)
        except ValueError as e:
            raise HTTPException(400, f"字段 export.filename 无效: {e}")
    _export_limits = {"export.top_n": (1, 50), "export.min_confidence": (0, 100),
                      "export.interval_hours": (0.0, 8760.0)}
    for k, (lo, hi) in _export_limits.items():
        if k in applied and not (lo <= applied[k] <= hi):
            raise HTTPException(400, f"字段 {k} 应在 {lo} ~ {hi} 之间")
    # 多前缀策略参数：范围与 fetcher.POLICY_LIMITS 保持单一数据源
    _mirror_policy_keys = {
        "mirror_max_attempts": "max_attempts",
        "mirror_fail_threshold": "fail_threshold",
        "mirror_quarantine_seconds": "quarantine_seconds",
    }
    for field, key in _mirror_policy_keys.items():
        if field not in applied:
            continue
        lo, hi = fetcher.POLICY_LIMITS[key]
        if not (lo <= applied[field] <= hi):
            raise HTTPException(
                400, f"字段 {field} 应在 {lo} ~ {hi} 之间（当前 {applied[field]}）")
    for k, v in applied.items():
        _set_value(k, v)
    _save_overrides(applied)
    # 前缀池 + 策略一起热更新：下一轮任务/新请求生效（进行中的一轮沿用旧池）。
    # apply_mirror_config 幂等，保存任意字段时调用也不会洗白坏前缀。
    fetcher.apply_mirror_config(cfg)
    return {"ok": True, "applied": list(applied.keys())}


def _ascii_fallback(name: str) -> str:
    """给不支持 RFC 5987 filename* 的旧客户端一个纯 ASCII 兜底文件名。"""
    return re.sub(r"[^\x20-\x7e]", "_", name) or "source.js"



if (WEBUI_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=WEBUI_DIST / "assets"), name="webui_assets")


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str, request: Request):
    """SPA history 路由兜底：/admin/overview 等任意深链刷新时返回页面本身。

    必须注册在所有 API 路由与 /assets 挂载之后（Starlette 按注册顺序匹配），
    否则会先于真实路由被命中，吞掉 API 的 404 与静态资源。

    处理顺序：
    1. dist 根目录的静态文件（icon.svg、platform-config.json 等——它们不在
       /assets 挂载范围内）：直接返回文件本体。此前这里只有页面兜底，这些文件
       被当成 index.html 返回（HTML 200），浏览器既拿不到图标也拿不到配置；
    2. 其余路径仅对接受 HTML 的请求（浏览器页面导航）回退 index.html，
       缺失的静态文件保持 404，避免把网页当图标缓存、掩盖资源缺失。
    """
    if full_path.startswith(("api/", "assets/")):
        raise HTTPException(404, "Not Found")
    root = WEBUI_DIST.resolve()
    candidate = (root / full_path).resolve()
    if candidate.is_file() and str(candidate).startswith(str(root)):
        return FileResponse(candidate)
    if "text/html" not in request.headers.get("accept", ""):
        raise HTTPException(404, "Not Found")
    return _webui_index()
