"""统一的 HTTP 抓取工具：共享 AsyncClient + 带大小上限的流式 GET。"""
import asyncio
import time
from urllib.parse import urlsplit

import httpx

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) lx-source-hub/0.1")

# ---------- GitHub 加速前缀（抓取用） ----------
# 后台「配置管理 → 爬取设置」/ config.json 的 fetch_mirrors（每行一个，
# 如 https://gh-proxy.com/）。这类服务是 URL 前缀镜像（整条 GitHub 链接拼在
# 域名后），不是 HTTP 代理协议，因此实现为「改写文件类 GitHub URL 再请求」：
# 仅对文件下载域名（raw/gist/codeload + github.com 的 archive 与 release 附件）
# 生效；GitHub API 搜索、jsdelivr 与后端域名探测不受影响。
#
# 多前缀策略（折中：均摊 + 单请求兜底 + 轻量熔断）：
#   1. 按请求全局轮换起点，正常时负载均摊到各前缀；
#   2. 单条请求遇可重试失败（网络异常 / 429 / 5xx / 2xx 空内容），补试下一条
#      前缀，最多 MAX_MIRROR_ATTEMPTS 条；404/403 等确定性失败不重试；
#   3. 全部候选失败才回退原 URL 直连一次；
#   4. 单条前缀连续失败 FAIL_THRESHOLD 次临时摘除 QUARANTINE_SECONDS 秒，
#      到期后放一条「半开」探测，成功即恢复、失败重新摘除。

# 只匹配「文件下载」形态：路径拼接式镜像对 github.com 页面不支持
_GITHUB_FILE_HOSTS = frozenset({
    "raw.githubusercontent.com", "gist.githubusercontent.com", "codeload.github.com",
})
_GITHUB_FILE_PATHS = ("/archive/", "/releases/download/")

_MIRRORS: list = []
_MIRROR_IDX = 0
_FAILS: dict = {}          # prefix -> 连续失败次数
_QUARANTINE: dict = {}     # prefix -> 隔离截止（monotonic 秒）

MAX_MIRROR_ATTEMPTS = 2    # 单请求最多尝试几条前缀（含轮换起点那条）
FAIL_THRESHOLD = 3         # 连续失败多少次后临时摘除
QUARANTINE_SECONDS = 300.0  # 摘除时长；到期后放一条半开探测

# 可重试的 HTTP 状态：限流与网关类故障换下一条前缀通常能自愈
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def _now() -> float:
    """抽出来便于测试注入时间（熔断/半开依赖时钟）。"""
    return time.monotonic()


def set_mirrors(raw) -> list:
    """更新加速前缀池：去空白/去重、统一补尾斜杠，返回清洗后的清单。

    热更新语义：下一轮任务（run_crawl / run_check 入口会重设）或后台保存
    后的新请求生效；进行中的一轮沿用旧池。已被移除的前缀会连带清掉其
    失败计数与隔离状态；仍在池中的状态保留（不因保存配置而洗白坏前缀）。
    """
    global _MIRRORS
    seen: list = []
    for p in raw or []:
        if isinstance(p, str):
            p = p.strip()
            if not p:
                continue
            if not p.endswith("/"):
                p += "/"
            if p not in seen:
                seen.append(p)
    _MIRRORS = seen
    for state in (_FAILS, _QUARANTINE):
        for p in list(state):
            if p not in seen:
                state.pop(p, None)
    return seen


def reset_mirror_state() -> None:
    """清空失败计数与隔离状态（测试或运维手动重置用）。"""
    _FAILS.clear()
    _QUARANTINE.clear()


def mirror_health() -> dict:
    """各前缀当前健康状态（诊断用）：连续失败数、剩余隔离秒数。"""
    now = _now()
    return {
        p: {
            "fails": _FAILS.get(p, 0),
            "quarantine_left": round(max(0.0, _QUARANTINE.get(p, 0.0) - now), 1),
        }
        for p in _MIRRORS
    }


def _is_github_file_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    host = parts.hostname.lower()
    if host in _GITHUB_FILE_HOSTS:
        return True
    if host == "github.com":
        path = parts.path or ""
        return any(seg in path for seg in _GITHUB_FILE_PATHS)
    return False


def _candidates(url: str) -> list:
    """返回本次请求的镜像候选 [(prefix, half_open)]，最多 MAX_MIRROR_ATTEMPTS 条。

    以全局轮换序号为起点重排前缀池；跳过隔离期内的前缀；隔离已过期的
    最多放 1 条「半开」探测排在最后。每次调用推进一次序号，保证均摊。
    """
    global _MIRROR_IDX
    if not _MIRRORS or not _is_github_file_url(url):
        return []
    start = _MIRROR_IDX % len(_MIRRORS)
    _MIRROR_IDX += 1
    order = _MIRRORS[start:] + _MIRRORS[:start]

    now = _now()
    active, probing = [], []
    for p in order:
        until = _QUARANTINE.get(p, 0.0)
        if now < until:
            continue
        (probing if p in _QUARANTINE else active).append(p)
    # 半开探测优先占位：否则 active 数 >= MAX_MIRROR_ATTEMPTS 时它永远排不上，
    # 被摘除的前缀再也无法恢复。每个隔离周期只探测一次（失败会重新摘除）。
    picked = []
    if probing:
        picked.append((probing[0], True))
    for p in active:
        if len(picked) >= MAX_MIRROR_ATTEMPTS:
            break
        picked.append((p, False))
    return picked


def mirror_url(url: str) -> str:
    """兼容旧接口：返回本次请求的首选改写 URL；无候选时原样返回。

    fetch_limited 不走这里（直接用 _candidates 拿多条候选），避免重复推进序号。
    """
    c = _candidates(url)
    return c[0][0] + url if c else url


def _is_good(r: dict) -> bool:
    return 200 <= r["status"] < 300 and bool(r["content"])


def _is_retryable(r: dict) -> bool:
    """是否值得换下一条前缀。404/403/400 等确定性失败换哪条都一样，不重试。"""
    s = r["status"]
    return (s == 0 or s in _RETRYABLE_STATUS
            or (200 <= s < 300 and not r["content"]))


def _record(prefix: str, ok: bool, half_open: bool = False) -> None:
    """记一次尝试结果，维护连续失败计数与隔离窗口。"""
    if ok:
        _FAILS.pop(prefix, None)
        _QUARANTINE.pop(prefix, None)
        return
    if half_open:
        # 半开探测失败：直接重新摘除，不再累加（避免刚恢复又立刻被摘）
        _QUARANTINE[prefix] = _now() + QUARANTINE_SECONDS
        _FAILS[prefix] = FAIL_THRESHOLD
        return
    fails = _FAILS.get(prefix, 0) + 1
    _FAILS[prefix] = fails
    if fails >= FAIL_THRESHOLD:
        _QUARANTINE[prefix] = _now() + QUARANTINE_SECONDS


def new_client(timeout: float = 15.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": UA, "Accept": "*/*"},
    )


async def fetch_limited(client: httpx.AsyncClient, url: str, max_bytes: int) -> dict:
    """GET 并最多读 max_bytes 字节。网络错误时 status=0，不抛异常。

    文件类 GitHub URL 走加速前缀：按轮换起点取候选，失败（网络异常 /
    429 / 5xx / 空内容）自动补试下一条，最多 MAX_MIRROR_ATTEMPTS 条；
    404 等确定性失败立即返回；全部候选失败才回退原 URL 直连一次。
    """
    candidates = _candidates(url)
    if not candidates:
        return await _fetch_once(client, url, max_bytes)
    for prefix, half_open in candidates:
        r = await _fetch_once(client, prefix + url, max_bytes)
        good = _is_good(r)
        _record(prefix, good, half_open)
        if good or not _is_retryable(r):
            return r
    return await _fetch_once(client, url, max_bytes)


async def _fetch_once(client: httpx.AsyncClient, url: str, max_bytes: int) -> dict:
    try:
        async with client.stream("GET", url) as r:
            if r.status_code >= 400:
                return {"status": r.status_code, "content": b"", "truncated": False,
                        "content_type": "", "error": None}
            buf = bytearray()
            truncated = False
            async for chunk in r.aiter_bytes(65536):
                buf.extend(chunk)
                if len(buf) >= max_bytes:
                    truncated = True
                    break
            return {"status": r.status_code, "content": bytes(buf), "truncated": truncated,
                    "content_type": r.headers.get("content-type", ""), "error": None}
    except httpx.HTTPError as e:
        return {"status": 0, "content": b"", "truncated": False,
                "content_type": "", "error": repr(e)}


def decode_bytes(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return b.decode("gbk", "ignore")
        except Exception:
            return b.decode("utf-8", "ignore")


async def probe_host(client: httpx.AsyncClient, host: str) -> bool:
    """探测一台后端服务器是否存活：仅请求站点根路径，任何 HTTP 响应都算活。"""
    for scheme in ("https", "http"):
        try:
            await client.get(f"{scheme}://{host}/", timeout=6)
            return True
        except httpx.HTTPError:
            continue
    return False


async def probe_hosts(client: httpx.AsyncClient, hosts: list) -> bool:
    """任一后端存活即视为可用（有主/备域名时只挂一个还能用）。"""
    if not hosts:
        return False
    results = await asyncio.gather(
        *(probe_host(client, h) for h in hosts),
        return_exceptions=True
    )
    return any(r is True for r in results if isinstance(r, bool))
