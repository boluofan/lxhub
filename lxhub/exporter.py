"""本地化输出：把实测置信度 TopN 的音源脚本按「固定文件名」覆盖写入导出目录。

设计目标（对外提供永远不变的 URL）：

* 文件名只由名次决定——模板仅允许 ``{rank}`` 占位符（见 config.validate_filename_template）。
  Top 榜换人时**覆盖同名文件**，内容自动更新，而 ``/local/lx-source-1.js``
  这类对外地址始终不变；含 name/date 的模板会漂移，直接拒绝。
* 写入走「同目录临时文件 + os.replace」原子替换：任何时刻读到的都是完整脚本，
  不会出现半截文件，也不需要停机窗口。
* 导出目录在容器内固定为 ``/app/outjs``，自部署时用 docker-compose 把宿主机/NAS
  目录挂载到该路径即可自定义落盘位置；也可用 ``LXH_EXPORT_DIR`` 覆盖。
* 每次导出写 ``manifest.json``，记录文件名对应的来源、置信度、sha256 与补齐标记；
  可选只清理 manifest 记录过的旧文件，绝不碰目录里其他内容。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile
from pathlib import Path

from .config import DEFAULTS, ROOT, validate_filename_template

MANIFEST_NAME = "manifest.json"
_SAFE_BASENAME = re.compile(r"^[A-Za-z0-9._-]+\.js$")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def export_dir() -> Path:
    """解析导出目录（配置固定，不在后台暴露）。

    优先级：LXH_EXPORT_DIR 环境变量 > 容器内 /app/outjs > 源码运行时 ROOT/outjs。
    用 /.dockerenv 或 LXH_DB 是否位于 /app 判断容器，而不是「/app 目录是否存在」——
    Windows 上 Path('/app') 会解析成 C:\\app，恰好存在时就会误判。
    """
    raw = (os.environ.get("LXH_EXPORT_DIR") or "").strip()
    if not raw:
        in_container = (Path("/.dockerenv").exists()
                        or str(os.environ.get("LXH_DB") or "").startswith("/app/"))
        raw = "/app/outjs" if in_container else str(ROOT / "outjs")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = ROOT / p
    return p.resolve()


def render_filename(template: str, rank: int) -> str:
    """用名次渲染文件名；模板已在配置层校验过，这里再做一次兜底。"""
    try:
        name = template.format(rank=rank)
    except (KeyError, IndexError, ValueError):
        name = DEFAULTS["export"]["filename"].format(rank=rank)
    name = os.path.basename(name.replace("\\", "/"))
    if not name.lower().endswith(".js"):
        name += ".js"
    return name


def _rank_key(row: dict) -> tuple:
    """排序键（配合 reverse=True）：置信度高 > 最近存活 > id 小，保证结果稳定。"""
    return (int(row.get("confidence") or 0),
            row.get("last_alive") or "",
            -int(row.get("id") or 0))


def pick_sources(db, cfg) -> list[tuple[dict, str, bool]]:
    """选出要导出的 N 条，返回 (完整记录, 脚本文本, 是否为补齐项)。

    用 list_sources() 的结果而非裸表：它已合并同内容镜像、收敛同名变体，
    否则同一脚本的多个镜像会霸占整个 TopN。
    """
    e = getattr(cfg, "export", None) or DEFAULTS["export"]
    n = max(1, int(e.get("top_n") or 3))
    min_score = int(e.get("min_confidence") or 0)
    base = [r for r in db.list_sources(limit=100_000)
            if r.get("is_alive") and r.get("looks_valid")]
    base.sort(key=_rank_key, reverse=True)
    qualified = [r for r in base
                 if (r.get("probe_status") or "") == "usable"
                 and int(r.get("confidence") or 0) >= min_score]

    pools = [(qualified, False)]
    if e.get("fill_shortfall"):
        # 合格数不足时用次优记录补齐，保证固定数量的文件始终存在
        pools.append((base, True))
    selected: list[tuple[dict, str, bool]] = []
    used: set[int] = set()
    for pool, filled in pools:
        for r in pool:
            if len(selected) >= n:
                break
            sid = int(r["id"])
            if sid in used:
                continue
            detail = db.get_source(sid) or {}
            text = detail.get("content_text") or ""
            if not text.strip():
                continue
            used.add(sid)
            selected.append((detail, text, bool(filled)))
        if len(selected) >= n:
            break
    return selected


def _atomic_write(path: Path, data: bytes) -> None:
    """同目录临时文件 + os.replace：覆盖写入不产生半截文件。"""
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _read_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _prune_stale(out: Path, old_names: set, current_names: set) -> list[str]:
    """只删除「上一版 manifest 记录过、本次不再生成」的 .js 文件。

    严格白名单：文件名必须匹配安全字符且无路径分隔符，绝不递归、绝不碰
    目录里的其他文件（包括用户自己放进去的内容）。
    """
    pruned: list[str] = []
    for name in sorted(old_names - current_names):
        if not isinstance(name, str) or not _SAFE_BASENAME.fullmatch(name):
            continue
        p = out / name
        try:
            if p.is_file():
                p.unlink()
                pruned.append(name)
        except OSError:
            pass
    return pruned


def run_export(cfg, db, trigger: str = "manual", log=print) -> dict:
    """执行一轮导出。enabled 只控制定时/自动触发，手动与 CLI 始终可执行。

    返回 {"written", "dir", "filenames", "pruned", "shortfall", ...}，
    同时写一条 runs(kind='export') 供调度器判周期、供后台展示最近导出时间。
    """
    e = getattr(cfg, "export", None) or dict(DEFAULTS["export"])
    out = export_dir()
    if out == ROOT.resolve() or out == Path(out.anchor):
        raise ValueError("导出目录不能是项目根目录或文件系统根目录")
    out.mkdir(parents=True, exist_ok=True)
    tpl = validate_filename_template(e.get("filename") or DEFAULTS["export"]["filename"])
    n = max(1, int(e.get("top_n") or 3))
    started = now()
    run_id = db.new_run("export", started)
    try:
        selected = pick_sources(db, cfg)
        manifest_path = out / MANIFEST_NAME
        old = _read_manifest(manifest_path)
        old_names = {str(f.get("filename") or "")
                     for f in (old.get("files") or []) if isinstance(f, dict)}

        files: list[dict] = []
        seen: set[str] = set()
        for rank, (row, text, filled) in enumerate(selected, 1):
            fname = render_filename(tpl, rank)
            if fname in seen:
                stem, ext = os.path.splitext(fname)
                fname = f"{stem}_{rank}{ext}"
            seen.add(fname)
            payload = text.encode("utf-8")
            _atomic_write(out / fname, payload)
            files.append({
                "filename": fname,
                "rank": rank,
                "source_id": int(row.get("id") or 0),
                "name": row.get("name") or "",
                "url": row.get("url") or "",
                "confidence": int(row.get("confidence") or 0),
                "sha256": row.get("sha256") or "",
                "bytes": len(payload),
                "filled": bool(filled),
            })

        manifest = {
            "generated_at": started,
            "trigger": trigger,
            "top_n": n,
            "filename_template": tpl,
            "files": files,
        }
        _atomic_write(manifest_path,
                      json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8"))

        pruned = _prune_stale(out, old_names, seen) if e.get("prune_stale") else []
        result = {
            "written": len(files),
            "dir": str(out),
            "filenames": [f["filename"] for f in files],
            "pruned": pruned,
            "shortfall": max(0, n - len(files)),
            "filled": sum(1 for f in files if f["filled"]),
            "generated_at": started,
            "trigger": trigger,
        }
        db.finish_run(run_id, now(), json.dumps(result, ensure_ascii=False))
        log(f"[本地化] {trigger} 导出 {len(files)} 个文件 → {out}"
            f"（补齐 {result['filled']}，缺口 {result['shortfall']}）")
        return result
    except Exception as e:
        # 不留悬空 run：否则 last_run('export') 的 finished_at 一直为空
        db.finish_run(run_id, now(), json.dumps({"error": repr(e)}, ensure_ascii=False))
        raise


def local_status(cfg, db) -> dict:
    """后台「本地化输出」页与 GET /api/export/local 用的状态快照。"""
    e = getattr(cfg, "export", None) or dict(DEFAULTS["export"])
    out = export_dir()
    manifest = _read_manifest(out / MANIFEST_NAME)
    return {
        "enabled": bool(e.get("enabled")),
        "top_n": int(e.get("top_n") or 3),
        "filename": str(e.get("filename") or DEFAULTS["export"]["filename"]),
        "interval_hours": float(e.get("interval_hours") or 0),
        "after_probe": bool(e.get("after_probe")),
        "min_confidence": int(e.get("min_confidence") or 0),
        "fill_shortfall": bool(e.get("fill_shortfall")),
        "prune_stale": bool(e.get("prune_stale")),
        "dir": str(out),
        "dir_from_env": bool((os.environ.get("LXH_EXPORT_DIR") or "").strip()),
        "serve_prefix": "/local/",
        "files": manifest.get("files") or [],
        "generated_at": manifest.get("generated_at") or "",
        "last_run": db.last_run("export"),
    }
