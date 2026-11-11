"""爬虫主流程：发现仓库 -> 收集候选 .js 链接 -> 逐个抓取并静态解析 -> 入库。

发现策略（按优先级，全部失败时仍有种子仓库兜底）：
  1. GitHub 搜索 API（配置了 GITHUB_TOKEN 才稳定；无 token 时也会尝试）
     - 多页抓取（最多 3 页）+ 多维度并行（stars / forks / updated）
     - 从已知仓库作者顺藤摸瓜搜该作者其他仓库
  2. github.com 搜索结果页 HTML 解析（免配额，但可能被 429）
  3. config.json 里的种子仓库 + 种子仓库 README 中的一跳引用扩展
     + release assets 里的 .js 文件

文件发现：
  - README 里的绝对/相对 .js 链接 -> raw.githubusercontent.com
  - GitHub git trees API 列全量文件（覆盖最全，需 Token 保障配额）
  - GitHub releases assets（每个 release 的 .js 附件）
  - 拿不到时降级 jsDelivr data API（免配额，但对无 release 的仓库常常列不出文件）
"""
import asyncio
import datetime as dt
import hashlib
import json
import re
import time
from urllib.parse import quote, unquote, urlparse, parse_qs

from .fetcher import apply_mirror_config, decode_bytes, fetch_limited, new_client, probe_hosts
from .metadata import (classify_script, detect_platforms, extract_api_hosts,
                       parse_annotations, valid_tier)

RAW = "https://raw.githubusercontent.com"
JSD_DATA = "https://data.jsdelivr.com/v1/packages/gh"

_GH_LINK = re.compile(r"github\.com/([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)")
_ABS_JS = re.compile(r"https?://[^\s\)\]\"'<>]+?\.(?:js|jsx)(?:\?[^\s\)\]\"'<>]*)?")
_REL_JS = re.compile(r"\((?:\./)?([A-Za-z0-9_./\-]+\.js)\)")
_HREF_REPO = re.compile(r'href="/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+)"')

_BLOCK = {
    "features", "topics", "collections", "trending", "events", "about", "pricing",
    "team", "enterprise", "sponsors", "security", "login", "join", "contact",
    "search", "settings", "notifications", "explore", "marketplace", "pulls",
    "issues", "codespaces", "copilot", "orgs", "site", "docs", "community",
    "github", "customer-stories", "readme", "advisories", "label", "apps",
    "organizations", "account", "updates", "whats-new", "git", "media",
}

README_NAMES = ["README.md", "readme.md", "Readme.md", "README.zh-CN.md", "README_zh.md"]

# 应用内部/测试资源路径：这些位置不会放可导入的音源脚本
_APP_PATH = re.compile(
    r"(^|/)(src/main|android|ios|platforms|tests?|fixtures|demos?|docs?|examples?)(/|$)", re.I)


def _is_app_internal(path: str) -> bool:
    return bool(_APP_PATH.search(path))


# ---------- 候选文件打分 ----------
# 权重越大越优先抓取。注意这与"文件越大越重要"相反：聚合脚本、打包产物往往体积
# 巨大，而可直接导入的音源脚本通常名字直白、位于浅层目录。改权重即可调整收录取向。
_CAND_WEIGHTS = {
    "name_hit": 6,     # 文件名直接带音源特征
    "path_hit": 3,     # 仅路径带特征
    "user_js": 4,      # .user.js 是常见分发形式
    "depth": -1,       # 每多一层目录
    "size_ok": 2,      # 2KB ~ 800KB：音源脚本常见区间
    "size_tiny": -4,   # < 2KB：多为占位/空壳
    "size_huge": -2,   # > 800KB：多为打包产物
    "bundle": -6,      # dist/build/vendor/webpack 等构建产物
    "sample": -6,      # 示例 / 测试 / 备份
}

_SOURCE_HINT = re.compile(r"音源|音乐源|music|source|lx|洛雪|聚合|自定义源", re.I)
_BUNDLE_HINT = re.compile(
    r"(^|/)(dist|build|out|vendor|assets?|static|chunk|node_modules)(/|$)"
    r"|webpack|\.bundle\.|[-.]min\.", re.I)
_SAMPLE_HINT = re.compile(
    r"(^|/)(tests?|demos?|examples?|samples?|docs?)(/|$)"
    r"|[-_](test|demo|example|sample|bak|old|backup)\b|备份|测试|示例", re.I)

# 解析出的脚本名（@name）含这些特征时视为测试/示例源，整体降权不收录为有效音源
_NAME_SAMPLE_HINT = re.compile(
    r"测试|示例|样例|试用|演示|废弃|弃用|过期|失效|演示版|测试版|"
    r"demo|test|sample|example|beta|mock|fake|dummy|练手", re.I)

# 生态经验规则：真实分发的音源几乎全部中文命名（lx 音乐是面向国内平台的应用，
# 作者发布时 @name 都写中文，如"墨澜聚合音源"）；无中文字符的名字基本是把文件名
# 当名字的模板/脚手架/客户端脚本（lx-source、runtime、Lx-Custom-Source 等），
# 它们结构上像音源但并非给人导入使用，一律不收录。
_CJK = re.compile(r"[\u4e00-\u9fff]")

# 仓库名/描述命中这些"明显是代码/示例/脚手架"的特征时，直接跳过（拦截无关仓库）。
# 注意：不要加 keep-alive / health-check 这类"看着像无关、实际可能托管真音源"的词，
# Huibq/keep-alive 就托管了有效音源 Huibq_lxmusic源，整仓库屏蔽会误杀。
_REPO_NOISE_HINT = re.compile(
    r"(hello[-_]?world|dotfiles|boilerplate|tutorial|playground|sandbox|"
    r"scratch|benchmark|crawler|spider|"
    r"test[-_]?repo|demo[-_]?repo|example[-_]?repo|"
    r"my[-_]?(test|demo|example)|sample[-_]?project)", re.I)


def score_candidate(path: str, size: int) -> int:
    """给候选 .js 路径打分：越像可导入的音源脚本分越高。"""
    w = _CAND_WEIGHTS
    low = path.lower()
    name = low.rsplit("/", 1)[-1]
    score = 0
    if _SOURCE_HINT.search(name):
        score += w["name_hit"]
    elif _SOURCE_HINT.search(low):
        score += w["path_hit"]
    if name.endswith(".user.js"):
        score += w["user_js"]
    score += w["depth"] * low.count("/")
    if size < 2048:
        score += w["size_tiny"]
    elif size <= 800_000:
        score += w["size_ok"]
    else:
        score += w["size_huge"]
    if _BUNDLE_HINT.search(low):
        score += w["bundle"]
    if _SAMPLE_HINT.search(low):
        score += w["sample"]
    return score


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _valid_repo(full: str) -> str | None:
    parts = full.strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        return None
    if parts[0].lower() in _BLOCK or parts[1].lower() in _BLOCK:
        return None
    return f"{parts[0]}/{parts[1]}"


def _repo_is_noise(full: str, description: str = "") -> bool:
    """挡掉明显与音源无关的仓库（keep-alive / 测试 / 示例 等）。

    仅做保守的“否证”拦截：仓库名或描述命中非音源特征词才跳过，避免误伤描述
    较少的合法仓库（搜索召回的仓库大多已在名称/描述里带音源关键词）。
    """
    return bool(_REPO_NOISE_HINT.search(f"{full} {description}"))


async def _fetch_readme(client, full: str) -> str:
    for name in README_NAMES:
        r = await fetch_limited(client, f"{RAW}/{full}/HEAD/{name}", 300_000)
        if r["status"] == 200 and r["content"]:
            return decode_bytes(r["content"])
    return ""


_BLOB_URL = re.compile(
    r"https?://github\.com/([^/\s\)\"'<>]+)/([^/\s\)\"'<>]+)/(?:blob|raw)/([^\s\)\"'<>]+)")


def _normalize_url(u: str) -> str:
    """github blob/raw 页面链接 -> raw.githubusercontent 直链；去掉跟踪参数。"""
    u = u.rstrip(".,;）)]\"'，；")
    m = _BLOB_URL.match(u)
    if m:
        path = m.group(3).split("?")[0]
        return f"{RAW}/{m.group(1)}/{m.group(2)}/HEAD/{path}"
    return u


def _extract_readme_urls(text: str, full: str) -> list:
    urls = []
    for u in _ABS_JS.findall(text):
        u = _normalize_url(u)
        if "/HEAD/" in u and _is_app_internal(u.split("/HEAD/", 1)[1]):
            continue
        urls.append(u)
    for m in _REL_JS.finditer(text):
        p = m.group(1).strip()
        if " " in p or p.startswith("//") or _is_app_internal(p):
            continue
        if p.startswith("./"):
            p = p[2:]
        urls.append(f"{RAW}/{full}/HEAD/{p}")
    return urls


def _gh_headers(cfg) -> dict:
    """GitHub API 请求头。

    只在请求 api.github.com 时带 Authorization：不要把 Token 发给
    raw.githubusercontent.com / jsDelivr 等第三方主机。
    """
    h = {"Accept": "application/vnd.github+json"}
    if getattr(cfg, "github_token", ""):
        h["Authorization"] = f"Bearer {cfg.github_token}"
    return h


def _parse_link_next(link_header: str) -> str | None:
    """从 Link 头提取 ?page=N 的下一页 URL。"""
    for part in re.findall(r"<([^>]+)>;\s*rel=\"next\"", link_header):
        return part
    return None


async def _search_page(client, cfg, kw: str, sort: str, page: int, log,
                      retries: int = 3) -> dict | None:
    """搜索一页，返回 (items, total) 或 rate-limited 信号。

    retries 限制 403/429 后的等待重试次数：无限递归会在持续限流时
    不断自我调用，最终爆栈；重试耗尽按无结果处理，交给后续兜底链路。
    """
    params = {
        "q": kw,
        "sort": sort,
        "order": "desc",
        "per_page": min(cfg.max_repos_per_keyword, 100),
        "page": page,
    }
    r = await client.get(
        "https://api.github.com/search/repositories",
        params=params,
        headers=_gh_headers(cfg))
    if r.status_code == 403 or r.status_code == 429:
        if retries <= 0:
            log(f"[搜索] {kw!r} {sort} 连续限流，放弃页 {page}")
            return None
        wait = int(r.headers.get("Retry-After", 60))
        log(f"[搜索] {kw!r} {sort} 触发限流，等待 {wait}s 后重试页 {page}（剩余 {retries} 次）")
        await asyncio.sleep(wait)
        return await _search_page(client, cfg, kw, sort, page, log, retries - 1)
    if r.status_code != 200:
        return None
    data = r.json()
    return {"items": data.get("items", []), "total": data.get("total_count", 0)}


async def _search_all_pages(client, cfg, kw: str, sort: str, log, max_pages: int = 3,
                            cancelled=None) -> list:
    """逐页抓取，直到末页或达到 max_pages 限制。

    `cancelled` 是可选的取消判断回调（如 `progress.cancelled` 的 getter）；
    每翻一页前检查一次，避免取消后还在继续翻页等 HTTP。
    """
    all_items = []
    page = 1
    while page <= max_pages:
        if cancelled and cancelled():
            break
        res = await _search_page(client, cfg, kw, sort, page, log)
        if res is None:
            break
        items = res["items"]
        all_items.extend(items)
        total = res["total"]
        per_page = min(cfg.max_repos_per_keyword, 100)
        if page * per_page >= total or not items:
            break
        page += 1
    return all_items


async def discover_repos(client, cfg, log, progress=None) -> dict:
    """返回 {full_name: {"kind":..., "url":..., "description":...}}

    多维度搜索策略：
     1. 热门关键词 × (stars / forks / updated) 并行，各最多 3 页
     2. 从已知种子仓库 README 提取作者名 → 顺藤搜该作者其他仓库
     3. 种子仓库 + README 一跳引用扩展 + release assets
    """
    repos: dict = {}

    def _cancelled() -> bool:
        return bool(getattr(progress, "cancelled", False))

    def add(full, kind, url="", description="", pushed_at="", stars=0):
        full = _valid_repo(full)
        if full and full not in repos and len(repos) < cfg.max_repos_total:
            repos[full] = {"kind": kind, "url": url or f"https://github.com/{full}",
                           "description": description or "",
                           "pushed_at": pushed_at or "", "stars": stars or 0}
            return True  # 新增成功；调用方用 int() 累加计数，不能返回仓库名字符串
        return False

    # ---------- 1) GitHub API 多维度并行搜索 ----------
    sort_orders = ["stars", "forks", "updated"]
    # 优先用 star/fork 排序搜高质量仓库；最后再用 updated 补充最新
    top_kws = cfg.keywords[:4]   # 前 4 个关键词质量最高，充分搜索

    async def _kw_sort(kw, sort):
        """搜索单关键词 + 单排序的多页结果。"""
        if _cancelled():
            return 0
        items = await _search_all_pages(client, cfg, kw, sort, log, max_pages=3,
                                        cancelled=_cancelled)
        added = 0
        for item in items:
            full = item["full_name"]
            desc = item.get("description") or ""
            if _repo_is_noise(full, desc):
                continue
            added += int(add(full, f"search_{sort}", description=desc,
                             pushed_at=item.get("pushed_at") or "",
                             stars=item.get("stargazers_count") or 0))
        return added

    # 前 2 个关键词多维度并行；其余串行
    _limit = asyncio.Semaphore(3)  # 同时最多 3 个搜索任务，避免 Search API 限流

    async def _limited(kw, sort):
        async with _limit:
            return await _kw_sort(kw, sort)

    tasks: list = []
    # keywords 为空（后台可清空）或只有 1 个时，top_kws[0]/[1] 会越界整轮崩溃，
    # 这里按实际个数守卫；无关键词时只靠种子仓库兜底
    if top_kws:
        for sort in sort_orders:
            tasks.append(asyncio.create_task(_limited(top_kws[0], sort)))
        await asyncio.sleep(0.5)
        if len(top_kws) > 1:
            for sort in sort_orders:
                tasks.append(asyncio.create_task(_limited(top_kws[1], sort)))
        for kw in top_kws[2:]:
            tasks.append(asyncio.create_task(_limited(kw, "updated")))
    for kw in cfg.keywords[len(top_kws):]:
        tasks.append(asyncio.create_task(_limited(kw, "updated")))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    if _cancelled():
        log("[搜索] 收到取消请求，停止后续仓库发现")
        return repos
    total_found = sum(r for r in results if isinstance(r, int))
    errs = [r for r in results if isinstance(r, Exception)]
    if errs:
        log(f"[搜索] API 异常: {errs[:3]}")
    if total_found:
        log(f"[搜索] API 命中 {len(repos)} 个仓库（去重前 {total_found} 条）")

    # 若 API 无结果，HTML 兜底
    if not repos and not _cancelled():
        for kw in cfg.keywords:
            if _cancelled():
                break
            try:
                r = await client.get(
                    f"https://github.com/search?q={quote(kw)}&type=repositories")
                if r.status_code != 200:
                    log(f"[搜索] HTML 页 {r.status_code}，跳过 {kw!r}")
                    continue
                for m in _HREF_REPO.finditer(r.text):
                    add(f"{m.group(1)}/{m.group(2)}", "search_html")
            except Exception as e:
                log(f"[搜索] HTML 页失败 {kw!r}: {e!r}")
        if repos:
            log(f"[搜索] HTML 兜底命中 {len(repos)} 个仓库")

    # ---------- 2) 从种子仓库顺藤摸瓜：README 作者 + 链接扩展 ----------
    queue = [(s, "seed") for s in dict.fromkeys(cfg.seed_repos)]
    hopped: set = set()
    seen_authors: set = set()

    async def _search_author(author: str, kind_hint: str):
        """搜该作者名下含 lx/洛雪 的仓库。"""
        if not author or author in seen_authors:
            return
        seen_authors.add(author)
        items = await _search_all_pages(
            client, cfg, f"author:{author} (lx OR 洛雪 OR music-source OR lx-music)",
            "updated", log, max_pages=2, cancelled=_cancelled)
        for item in items:
            full = item["full_name"]
            desc = item.get("description") or ""
            if _repo_is_noise(full, desc):
                continue
            add(full, f"author_{kind_hint}", description=desc,
                pushed_at=item.get("pushed_at") or "",
                stars=item.get("stargazers_count") or 0)

    while queue and len(repos) < cfg.max_repos_total:
        if _cancelled():
            log("[搜索] 收到取消请求，停止 README 顺藤摸瓜")
            break
        full, kind = queue.pop(0)
        if full in repos or not _valid_repo(full):
            continue
        if kind == "seed":
            readme = await _fetch_readme(client, full)
            if readme:
                # 一跳：README 里的 GitHub 仓库链接
                for m in _GH_LINK.finditer(readme):
                    cand = _valid_repo(m.group(1).rstrip("."))
                    if cand and cand not in repos and cand not in hopped:
                        hopped.add(cand)
                        queue.append((cand, "link"))
                # 二跳：从 repo URL 提取作者并搜该作者的其他仓库
                author_match = re.search(
                    r"github\.com/([A-Za-z0-9_.\-]+)(?:/[^/\s)\"'<>]*)?$", readme)
                if author_match:
                    await _search_author(author_match.group(1), "seed")
        elif kind == "link":
            # 链接扩展不重复搜作者，只加入队列
            pass
        add(full, kind)

    # ---------- 3) seed_urls 直接入库 ----------
    for u in cfg.seed_urls:
        # 尝试从 seed_url 提取仓库名
        m = re.search(r"github\.com/([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)", u)
        if m:
            add(m.group(1), "seed_url")

    return repos


def _flatten_jsd_files(nodes, prefix="") -> list:
    out = []
    for n in nodes or []:
        name = n.get("name", "")
        if n.get("type") == "directory":
            out += _flatten_jsd_files(n.get("files"), f"{prefix}{name}/")
        else:
            out.append({"path": f"{prefix}{name}", "size": n.get("size") or 0})
    return out


async def _jsdelivr_versions(client, full: str) -> list:
    """拿 1~2 个可用于列文件的版本号/分支名。"""
    try:
        r = await client.get(f"{JSD_DATA}/{full}")
        if r.status_code != 200:
            return ["master", "main"]  # 退化成直接试分支
        versions = [v["version"] for v in (r.json().get("versions") or [])]
        return versions[:1] or ["master", "main"]
    except Exception:
        return []


async def _jsdelivr_files(client, full: str) -> list:
    """jsDelivr 列文件：免配额，但对没有 release/tag 的仓库常常拿不到，只能兜底。"""
    out = []
    for v in await _jsdelivr_versions(client, full):
        try:
            r = await client.get(f"{JSD_DATA}/{full}@{v}")
            if r.status_code == 200:
                out += _flatten_jsd_files(r.json().get("files"))
        except Exception:
            continue
    return out


async def _github_tree_files(client, cfg, full: str) -> list:
    """用 GitHub git trees API 列全量文件。

    覆盖最全（不依赖 release），但要消耗核心 API 配额，因此只在配置了 Token
    时才可靠；失败时返回空列表，由调用方降级到 jsDelivr。
    """
    try:
        r = await client.get(
            f"https://api.github.com/repos/{full}/git/trees/HEAD?recursive=1",
            headers=_gh_headers(cfg))
        if r.status_code != 200:
            return []
        return [{"path": t["path"], "size": t.get("size") or 0}
                for t in r.json().get("tree", [])
                if t.get("type") == "blob"]
    except Exception:
        return []


async def _github_release_assets(client, cfg, full: str) -> list:
    """拉取仓库所有 release 的 .js 附件，返回 raw URL 列表。"""
    urls: list = []
    try:
        r = await client.get(
            f"https://api.github.com/repos/{full}/releases",
            params={"per_page": 30},
            headers=_gh_headers(cfg))
        if r.status_code != 200:
            return urls
        for release in r.json():
            for asset in release.get("assets", []):
                name = asset.get("name", "")
                if name.endswith(".js") and not name.startswith("index"):
                    urls.append(asset["browser_download_url"])
    except Exception:
        pass
    return urls


async def collect_repo_urls(client, cfg, full: str) -> list:
    urls: list = []
    readme = await _fetch_readme(client, full)
    if readme:
        urls += _extract_readme_urls(readme, full)

    # 优先 GitHub trees API，拿不到再降级 jsDelivr
    files = await _github_tree_files(client, cfg, full)
    if not files:
        files = await _jsdelivr_files(client, full)

    js = [f for f in files
          if f["path"].endswith(".js")
          and "node_modules" not in f["path"]
          and ".min." not in f["path"]
          and not _is_app_internal(f["path"])
          and 0 < f["size"] <= cfg.max_file_bytes]
    # 按"像不像音源脚本"排序，而不是按体积：体积倒序会优先抓到打包产物
    js.sort(key=lambda f: (-score_candidate(f["path"], f["size"]), -f["size"]))
    for f in js[:cfg.max_files_per_repo]:
        urls.append(f"{RAW}/{full}/HEAD/{f['path']}")

    # release assets 里的 .js 附件（覆盖"只发 Release 不发主分支"的情况）
    release_urls = await _github_release_assets(client, cfg, full)
    for ru in release_urls:
        ru_clean = ru.split("?")[0].rstrip("/")
        if ru_clean not in urls and not _is_app_internal(ru_clean):
            urls.append(ru_clean)

    seen, out = set(), []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out[:cfg.max_files_per_repo]


async def inspect_url(client, cfg, url: str, origin: dict) -> dict:
    r = await fetch_limited(client, url, cfg.max_file_bytes)
    alive = 200 <= r["status"] < 300
    base = url.split("?")[0].rstrip("/").split("/")[-1]
    d = {
        "url": url,
        "name": re.sub(r"\.jsx?$", "", base) or url,
        "version": "",
        "author": "",
        "description": "",
        "homepage": "",
        "http_status": r["status"],
        "is_alive": 1 if alive else 0,
        "file_size": len(r["content"]) if r["content"] else 0,
        "sha256": hashlib.sha256(r["content"]).hexdigest() if r["content"] else "",
        "content_text": "",
        "looks_valid": 0,
        "valid_tier": "",
        "platforms": "[]",
        "first_seen": now(),
        "last_checked": now(),
        "last_alive": now() if alive else "",
        "origin_repo": origin.get("repo", ""),
        "origin_url": origin.get("url", ""),
        "origin_kind": origin.get("kind", ""),
        "repo_pushed_at": origin.get("pushed_at", ""),
    }
    if r["content"]:
        head = r["content"][:600]
        is_html = ("text/html" in r["content_type"]
                   or b"<!DOCTYPE" in head or b"<!doctype" in head or b"<html" in head)
        if is_html:
            # 抓到的是网页（如 GitHub blob 页），不是脚本文件
            d["is_alive"] = 0
            d["content_text"] = ""
        else:
            text = decode_bytes(r["content"])
            d["content_text"] = text
            meta = parse_annotations(text)
            d["name"] = meta.get("name") or d["name"]
            d["version"] = meta.get("version") or d["version"]
            d["author"] = meta.get("author") or d["author"]
            d["description"] = meta.get("description") or d["description"]
            d["homepage"] = meta.get("homepage") or d["homepage"]
            tier = valid_tier(text)
            d["looks_valid"] = 1 if tier else 0
            d["valid_tier"] = tier
            # 无 @name 注释的通用文件名（index/main/app…）不当作音源
            if tier and not meta.get("name"):
                stem = base.rsplit(".", 1)[0].lower()
                if stem in ("index", "main", "app", "utils", "config", "setup"):
                    d["looks_valid"] = 0
                    d["valid_tier"] = ""
            # 解析出的名称带测试/示例/废弃特征：真实音源脚本但属测试样本，降权不收录
            if d["looks_valid"] and _NAME_SAMPLE_HINT.search(d["name"]):
                d["looks_valid"] = 0
                d["valid_tier"] = ""
                d["sample_name"] = True
            # 名称无任何中文字符：模板/脚手架/客户端脚本，不是真实分发的音源
            if d["looks_valid"] and not _CJK.search(d["name"]):
                d["looks_valid"] = 0
                d["valid_tier"] = ""
                d["name_not_cjk"] = True
            d["script_type"] = classify_script(text)
            d["api_hosts"] = json.dumps(extract_api_hosts(text))
            d["platforms"] = json.dumps(detect_platforms(text))
    return d


async def backfill_repo_dates(client, cfg, db, repos: dict, log=print, progress=None) -> int:
    """为收录行补 GitHub 仓库最后更新时间（pushed_at），作为可用性排序先验。

    搜索命中的仓库自带 pushed_at，直接采用；其余缺日期的来源仓库逐个查一次
    repo API（每仓库仅 1 次请求）。结果按 origin_repo 写到该仓库全部行。
    """
    def _cancelled() -> bool:
        return bool(getattr(progress, "cancelled", False))

    dates: dict = {full: (m.get("pushed_at") or "") for full, m in repos.items()}
    todo = {r["origin_repo"] for r in db.all_sources_rows()
            if r.get("origin_repo") and not r.get("repo_pushed_at")}
    todo -= {f for f, d in dates.items() if d}
    for full in sorted(todo):
        if _cancelled():
            log("[爬取] 收到取消请求，停止回填仓库更新时间")
            break
        try:
            r = await client.get(f"https://api.github.com/repos/{full}",
                                 headers=_gh_headers(cfg))
            if r.status_code == 200:
                dates[full] = r.json().get("pushed_at") or ""
        except Exception:
            continue
    written = 0
    for full, d in dates.items():
        if d:
            db.set_repo_pushed_at(full, d)
            written += 1
    if written:
        log(f"[爬取] 仓库更新时间覆盖 {written} 个仓库")
    return written


async def run_crawl(cfg, db, log=print, progress=None):
    from .progress import NoOpProgress
    p = progress or NoOpProgress()
    apply_mirror_config(cfg)  # 加速前缀池 + 策略热更新（见 fetcher）
    run_id = db.new_run("crawl", now())
    log("[爬取] 开始")
    client = new_client(cfg.request_timeout)
    stats = {"repos": 0, "checked": 0, "new": 0, "valid": 0, "dead": 0,
             "cancelled": False}
    try:
        p.set_phase("discover", "正在发现仓库…")
        repos = await discover_repos(client, cfg, log, p)
        stats["repos"] = len(repos)
        log(f"[爬取] 待扫描仓库 {len(repos)} 个")
        # 仓库更新时间先行回填：即使本轮 URL 未重扫，排序信号也已就位
        await backfill_repo_dates(client, cfg, db, repos, log, p)
        p.update(repos_total=len(repos), repos_done=0,
                 urls_total=len(cfg.seed_urls), urls_done=0)
        p.set_phase("collect", f"扫描 {len(repos)} 个仓库")

        known = db.all_urls()
        sem = asyncio.Semaphore(cfg.concurrency)

        async def handle(url: str, origin: dict):
            async with sem:
                if p.cancelled:
                    return
                # 应用内部路径不是音源脚本；存量行直接清除
                if "/HEAD/" in url and _is_app_internal(url.split("/HEAD/", 1)[1]):
                    if url not in known:
                        known.add(url)
                    else:
                        db.delete_url(url)
                    return
                d = await inspect_url(client, cfg, url, origin)
                is_new = url not in known
                known.add(url)
                # 音源脚本顺带探测后端域名存活（仅探测服务器是否响应，不调用取歌接口）；
                # 不再限定 script_type==source，避免结构不全/混淆判定的有效源漏探
                if d["looks_valid"]:
                    try:
                        hosts = json.loads(d.get("api_hosts") or "[]")
                    except json.JSONDecodeError:
                        hosts = []
                    if hosts:
                        d["api_alive"] = "up" if await probe_hosts(client, hosts) else "down"
                        d["api_checked"] = d["last_checked"]
                # 工具只收集真实音源：新候选若不是音源脚本，直接不入库
                if d["looks_valid"] or not is_new:
                    db.upsert_source(d)
                stats["checked"] += 1
                if is_new:
                    stats["new"] += 1
                if d["looks_valid"]:
                    stats["valid"] += 1
                if not d["is_alive"]:
                    stats["dead"] += 1
                p.update(urls_done=stats["checked"], new=stats["new"],
                         valid=stats["valid"], dead=stats["dead"])
                if d["looks_valid"]:
                    tag = "疑似音源"
                elif d.get("sample_name"):
                    tag = "测试/示例"
                elif d.get("name_not_cjk"):
                    tag = "非中文名"
                elif not d["is_alive"]:
                    tag = "失效"
                else:
                    tag = "普通js"
                suffix = f"(HTTP {d['http_status']})" if tag == "失效" else ""
                log(f"  [{tag}{suffix}] {url}")

        tasks = []
        repos_done = 0
        urls_total = len(cfg.seed_urls)
        p.update(urls_total=urls_total)
        seed_origin = {"kind": "seed", "url": "", "repo": ""}
        for u in cfg.seed_urls:
            tasks.append(asyncio.create_task(handle(u, seed_origin)))

        for full, meta in repos.items():
            if p.cancelled:
                log("[爬取] 收到取消请求，停止扫描后续仓库")
                break
            if _repo_is_noise(full, meta.get("description", "")):
                log(f"[爬取] 跳过疑似无关仓库 {full}")
                continue
            cands = await collect_repo_urls(client, cfg, full)
            log(f"[爬取] {full}: 候选链接 {len(cands)} 个")
            repos_done += 1
            urls_total += len(cands)
            p.update(repos_done=repos_done, urls_total=urls_total)
            origin = {"kind": meta["kind"], "url": meta["url"], "repo": full}
            for u in cands:
                tasks.append(asyncio.create_task(handle(u, origin)))
            if len(tasks) >= 100:
                await asyncio.gather(*tasks)
                tasks = []
        if tasks:
            await asyncio.gather(*tasks)

        # 清理应用内部路径的存量行（候选阶段已被过滤，这里兜底删除）
        bad = [u for u in db.all_urls()
               if "/HEAD/" in u and _is_app_internal(u.split("/HEAD/", 1)[1])]
        if bad:
            db.delete_urls(bad)
            log(f"[爬取] 清理应用内部路径记录 {len(bad)} 条")

        # 清理非音源记录；真音源失效后保留 7 天宽限期再清
        cutoff = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(days=7)).isoformat(timespec="seconds")
        removed = db.prune_invalid(cutoff)
        if removed:
            log(f"[爬取] 清理非音源/长期失效记录 {removed} 条")

        # 收敛同一仓库内的同名副本（按日期/版本复制的历史快照）
        dups = db.prune_same_name_copies()
        if dups:
            log(f"[爬取] 收敛同名副本 {dups} 条")

        stats["cancelled"] = p.cancelled
        db.finish_run(run_id, now(), json.dumps(stats, ensure_ascii=False))
        log(f"[爬取] {'已取消' if p.cancelled else '完成'}: "
            f"{json.dumps({k: v for k, v in stats.items() if k != 'cancelled'}, ensure_ascii=False)}")
    except asyncio.CancelledError:
        # 被 /api/cancel 硬取消：补一条结束记录，否则 last_run 的 finished_at 一直是空的
        db.finish_run(run_id, now(), json.dumps({"cancelled": True}, ensure_ascii=False))
        log("[爬取] 已取消")
        raise
    except Exception as e:
        db.finish_run(run_id, now(), json.dumps({"error": repr(e)}))
        log(f"[爬取] 异常退出: {e!r}")
        raise
    finally:
        await client.aclose()
