"""配置加载：默认值 <- config.json <- 环境变量 (GITHUB_TOKEN / LXH_HOST / LXH_PORT / LXH_DB)"""
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "db_path": "data/lxhub.db",
    "host": "127.0.0.1",
    "port": 8787,
    "github_token": "",
    # 与 config.json 保持一致（config.json 存在时以其为准）
    "keywords": ["lx 音源", "洛雪 音源", "lx-music 音源", "lx music 自定义源",
                 "lxmusic", "洛雪音乐 音源", "lx音乐源", "洛雪音乐 源",
                 "lx 源脚本", "洛雪音源js"],
    "seed_repos": ["ZxwyWebSite/lx-source"],
    "seed_urls": [],
    "max_repos_per_keyword": 20,
    "max_repos_total": 60,
    "max_files_per_repo": 40,
    "max_file_bytes": 1_500_000,
    "request_timeout": 15,
    "concurrency": 8,
    "check_interval_hours": 12,
    "crawl_interval_hours": 24,
    # 复制链接时可拼接的加速前缀：[{"name":"","prefix":"https://gh1.lhl.one/","hosts":[...]}]
    "mirror_prefixes": [],
    # 抓取用 GitHub 加速前缀（如 https://gh-proxy.com/，每行一个）：
    # 抓 raw/gist/codeload 与 github.com 的 archive/release 附件时按行轮换拼接；空 = 直连
    "fetch_mirrors": [],
    # 多加速前缀容错策略（后台「配置管理 → 爬取设置」可改，见 fetcher.py 说明）：
    # 单请求最多试几条前缀 / 连续失败几次摘除 / 摘除多少秒
    "mirror_max_attempts": 2,
    "mirror_fail_threshold": 3,
    "mirror_quarantine_seconds": 300.0,

    # 沙箱实测探测（可选实验特性）。enabled=False 时保持纯静态模式
    "probe": {
        "enabled": False,           # 默认关闭：开启后会执行收录的音源脚本并测试取歌
        "node": "node",             # node 可执行文件路径（>=20）
        "interval_hours": 24,       # 定时实测周期；0 = 不定时（仅手动触发/检测后补测）
        "concurrency": 2,           # 同时实测几个脚本
        "max_per_run": 20,          # 单轮探测上限（复检后自动触发时也生效）
        "refresh_hours": 72,        # 探测结果有效期；内容变更立即重测
        "deep": False,              # 联动检测是否附带音质核验（flac/flac24bit）
        "qualities": ["flac", "flac24bit"],
        "timeout": 60,              # 单脚本总预算（秒）
        "allow_private": False,     # 允许访问内网地址（默认禁止，防 SSRF）
    },

    # 本地化输出：把实测置信度 TopN 的脚本按「固定文件名」覆盖写到容器内固定目录。
    # 目录本身不在后台配置：容器内恒为 /app/outjs，自部署时由 docker-compose
    # 把宿主机/NAS 目录挂载到该路径来实现自定义落盘位置。
    "export": {
        "enabled": False,           # 定时导出 + 实测后自动导出的总开关（手动按钮不受限）
        "top_n": 3,                 # 导出前 N 名
        "filename": "lx-source-{rank}.js",  # 只允许 {rank} 占位符，保证文件名恒定
        "interval_hours": 24.0,     # 定时导出周期；0 = 不定时
        "after_probe": True,        # 每轮实测完成后立即刷新本地文件
        "min_confidence": 1,        # 计入「高置信」的最低分（仅 usable 有非零分）
        "fill_shortfall": True,     # 合格数不足 N 时用次优记录补齐，保证 N 个文件始终存在
        "prune_stale": False,       # 缩小 N 后清理旧文件（只删 manifest 记录过的）
    },
}


# 以「xxx.field」平铺键读写、在 override 文件里合并成嵌套块的配置组。
# 后台保存、override 加载、/api/admin/config 三处共用，避免为新组各写一套分支。
NESTED_CONFIGS = ("probe", "export")


@dataclass
class Config:
    db_path: str
    host: str
    port: int
    github_token: str
    keywords: list
    seed_repos: list
    seed_urls: list
    max_repos_per_keyword: int
    max_repos_total: int
    max_files_per_repo: int
    max_file_bytes: int
    request_timeout: float
    concurrency: int
    check_interval_hours: float
    crawl_interval_hours: float
    mirror_prefixes: list
    fetch_mirrors: list
    mirror_max_attempts: int
    mirror_fail_threshold: int
    mirror_quarantine_seconds: float
    probe: dict
    export: dict



def _read_dotenv(path: Path) -> dict:
    """极简 .env 解析（KEY=VALUE），不引入额外依赖。

    .env 不进版本库，适合放 GITHUB_TOKEN 这类密钥；解析失败一律忽略。
    """
    out: dict = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("'\"")
    return out


# 允许在 .env 中设置的键（进程环境变量优先级更高，不会被 .env 覆盖）
ENV_KEYS = ("GITHUB_TOKEN", "LXH_HOST", "LXH_PORT", "PORT", "LXH_DB",
            "LXH_ADMIN_USER", "LXH_ADMIN_PASS", "LXH_EXPORT_DIR")


def load_dotenv(path: Path | None = None) -> None:
    """把 .env 里的键合并进 os.environ（不覆盖已有的进程环境变量）。

    必须写进 os.environ 而非只返回 dict：server.py 等模块在导入时直接用
    os.environ.get() 读取 LXH_ADMIN_USER / LXH_ADMIN_PASS 这类不进 Config 的字段，
    只靠 load_config 的返回值是读不到的。
    """
    for k, v in _read_dotenv(Path(path) if path else ROOT / ".env").items():
        if k in ENV_KEYS and v:
            os.environ.setdefault(k, v)


# 在 config 模块导入时就注入，保证后续任何 os.environ.get 都能读到 .env 的值
load_dotenv()


def load_config(path: str | None = None) -> Config:
    data = dict(DEFAULTS)
    p = Path(path) if path else ROOT / "config.json"
    if p.exists():
        try:
            data.update(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as e:
            print(f"[config] 读取 {p} 失败，使用默认配置: {e!r}")
    # 优先级：进程环境变量 > .env > config.json 默认值
    env = dict(os.environ)
    for k, v in _read_dotenv(ROOT / ".env").items():
        env.setdefault(k, v)
    for env_key, cfg_key in (("GITHUB_TOKEN", "github_token"), ("LXH_HOST", "host"),
                             ("LXH_PORT", "port"), ("PORT", "port"), ("LXH_DB", "db_path")):
        if env.get(env_key):
            data[cfg_key] = env[env_key]
    data["port"] = int(data["port"])
    data["probe"] = _norm_probe(data.get("probe"))
    data["export"] = _norm_export(data.get("export"))
    fields = Config.__dataclass_fields__
    cfg = Config(**{k: v for k, v in data.items() if k in fields})
    apply_overrides(cfg)
    return cfg


def _norm_probe(raw) -> dict:
    """归一化 probe 配置：类型校验 + 与默认值合并，非法整体回落默认。"""
    out = dict(DEFAULTS["probe"])
    if isinstance(raw, dict):
        for k in list(out):
            if k not in raw:
                continue
            v = raw[k]
            if isinstance(out[k], bool):
                out[k] = bool(v)
            elif isinstance(out[k], int):
                try:
                    out[k] = int(v)
                except (TypeError, ValueError):
                    out[k] = DEFAULTS["probe"][k]
            elif isinstance(out[k], float):
                try:
                    out[k] = float(v)
                except (TypeError, ValueError):
                    out[k] = DEFAULTS["probe"][k]
            elif k == "qualities":
                if isinstance(v, str):
                    v = [s.strip() for s in v.split(",")]
                out[k] = [str(s) for s in (v or DEFAULTS["probe"]["qualities"])
                          if str(s).strip()]
            else:
                out[k] = str(v)
    for k, default in DEFAULTS["probe"].items():
        if out.get(k) in ("", None) and not isinstance(out.get(k), bool):
            out[k] = default
    # 强制下限：防手滑配置（如 concurrency=0）让调度器/信号量卡死
    out["concurrency"] = max(1, int(out["concurrency"]))
    out["max_per_run"] = max(0, int(out["max_per_run"]))
    out["timeout"] = max(10, int(out["timeout"]))
    out["interval_hours"] = max(0.0, float(out["interval_hours"]))
    out["refresh_hours"] = max(0.0, float(out["refresh_hours"]))
    return out


# ---------- 本地化输出文件名模板 ----------
# 只允许 {rank}：文件名必须只由名次决定，才能做到「Top 榜换人，对外 URL 不变」。
# 含 {name}/{id}/{date} 的模板会随数据变化产生新文件名，旧文件残留、接口漂移，故拒绝。
_TEMPLATE_TOKEN = re.compile(r"\{([^{}]*)\}")
_SAFE_LITERAL = re.compile(r"^[A-Za-z0-9._-]+$")
_RANK_SPEC = re.compile(r"0?\d*d")


def validate_filename_template(tpl: str) -> str:
    """校验并返回规范化模板；不合法抛 ValueError。

    规则：以 .js 结尾、必须含 {rank}、占位符只能是 rank（可带 :02d 这类补零格式）、
    字面部分只允许字母数字点下划线连字符。
    """
    tpl = str(tpl or "").strip()
    if not tpl:
        raise ValueError("不能为空")
    if len(tpl) > 100:
        raise ValueError("长度不能超过 100 字符")
    if not tpl.lower().endswith(".js"):
        raise ValueError("必须以 .js 结尾")
    tokens = _TEMPLATE_TOKEN.findall(tpl)
    if not tokens:
        raise ValueError("必须包含 {rank} 占位符，否则多个名次会写成同一个文件")
    for token in tokens:
        name, _, spec = token.partition(":")
        if name.strip() != "rank":
            raise ValueError(f"只允许 {{rank}} 占位符，发现 {{{token}}}")
        if spec and not _RANK_SPEC.fullmatch(spec.strip()):
            raise ValueError(f"{{rank}} 的格式说明不合法：{spec}")
    literal = _TEMPLATE_TOKEN.sub("", tpl)
    if not literal or not _SAFE_LITERAL.fullmatch(literal):
        raise ValueError("文件名只能包含字母、数字、点、下划线、连字符")
    if ".." in literal:
        raise ValueError("文件名不能包含连续的 ..")
    return tpl


def _norm_export(raw) -> dict:
    """归一化 export 配置：类型校验、范围钳制、文件名模板校验。

    与 _norm_probe 一样在 load_config 与 apply_overrides 末尾各跑一次，
    保证后台保存 / 手改 override 文件都不会绕过下限。
    """
    out = dict(DEFAULTS["export"])
    if isinstance(raw, dict):
        for k, default in DEFAULTS["export"].items():
            if k not in raw or raw[k] is None:
                continue
            v = raw[k]
            try:
                if isinstance(default, bool):
                    out[k] = cast_bool(v)
                elif isinstance(default, int):
                    out[k] = int(v)
                elif isinstance(default, float):
                    out[k] = float(v)
                else:
                    out[k] = str(v).strip()
            except (TypeError, ValueError):
                out[k] = default
    out["top_n"] = max(1, min(50, int(out["top_n"])))
    out["min_confidence"] = max(0, min(100, int(out["min_confidence"])))
    out["interval_hours"] = max(0.0, min(8760.0, float(out["interval_hours"])))
    try:
        out["filename"] = validate_filename_template(out["filename"])
    except ValueError:
        out["filename"] = DEFAULTS["export"]["filename"]
    return out


# ---------- 网页后台覆盖配置 ----------
# 后台「配置管理」保存的字段清单（读写共用）。probe.* / export.* 是平铺键，
# 落盘时按 NESTED_CONFIGS 合并成 override 文件里的嵌套块。
# 注意 github_token 亦可在此填写，但会明文落盘到数据卷。
EDITABLE = {
    "github_token": str,
    "keywords": list,
    "seed_repos": list,
    "seed_urls": list,
    "max_repos_per_keyword": int,
    "max_repos_total": int,
    "max_files_per_repo": int,
    "max_file_bytes": int,
    "request_timeout": float,
    "concurrency": int,
    "check_interval_hours": float,
    "crawl_interval_hours": float,
    "mirror_prefixes": list,
    "fetch_mirrors": list,
    "mirror_max_attempts": int,
    "mirror_fail_threshold": int,
    "mirror_quarantine_seconds": float,
    "probe.enabled": bool,
    "probe.interval_hours": float,
    "probe.deep": bool,
    "probe.qualities": list,
    "probe.max_per_run": int,
    "probe.refresh_hours": float,
    "probe.concurrency": int,
    "probe.timeout": int,
    "probe.node": str,
    "probe.allow_private": bool,
    "export.enabled": bool,
    "export.top_n": int,
    "export.filename": str,
    "export.interval_hours": float,
    "export.after_probe": bool,
    "export.min_confidence": int,
    "export.fill_shortfall": bool,
    "export.prune_stale": bool,
}


def cast_bool(v):
    """宽容解析布尔值：后台开关传 true/false，curl 等传字符串 'true'/'false' 也认。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "y", "是")
    raise ValueError("需要布尔值")


def override_path(db_path: str) -> Path:
    """override 文件位置：与数据库同目录（data/config.override.json）。"""
    return Path(db_path).resolve().parent / "config.override.json"


def apply_overrides(cfg: Config) -> None:
    """把网页后台保存的 override 合并进 cfg（找不到/读坏时静默跳过）。

    在 load_config 末尾调用，CLI 与 Web 服务因此看到同一份配置——
    曾出现「后台开了 probe.enabled，CLI 手动跑却报未启用」的双轨分裂。
    db_path/host/port 不在 EDITABLE 中，override 改不了它们，
    因此用 cfg.db_path 定位 override 文件本身是稳定的。
    """
    path = override_path(cfg.db_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    for k, v in data.items():
        # 嵌套配置块：probe / export 共用同一套平铺键校验逻辑
        if k in NESTED_CONFIGS and isinstance(v, dict):
            block = dict(getattr(cfg, k, None) or {})
            for sk, sv in v.items():
                key = f"{k}.{sk}"
                if key not in EDITABLE:
                    continue
                cast = EDITABLE[key]
                try:
                    if cast is bool:
                        sv = cast_bool(sv)
                    elif cast is list and not isinstance(sv, list):
                        continue
                    block[sk] = cast(sv)
                except (ValueError, TypeError):
                    pass
            setattr(cfg, k, block)
            continue
        if k not in EDITABLE:
            continue
        try:
            cast = EDITABLE[k]
            if cast is bool:
                v = cast_bool(v)
            elif cast is list and not isinstance(v, list):
                continue
            setattr(cfg, k, cast(v))
        except (ValueError, TypeError):
            pass
    # 无论有没有 override 都再归一化一次：手改文件也必须遵守下限与模板规则
    cfg.probe = _norm_probe(cfg.probe)
    cfg.export = _norm_export(cfg.export)
