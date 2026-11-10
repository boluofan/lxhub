"""统一的 HTTP 抓取工具：共享 AsyncClient + 带大小上限的流式 GET。"""
import asyncio
from urllib.parse import urlsplit

import httpx

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) lx-source-hub/0.1")

# ---------- GitHub 加速前缀（抓取用） ----------
# 后台「配置管理 → 爬取设置」/ config.json 的 fetch_mirrors（每行一个，
# 如 https://gh-proxy.com/）。这类服务是 URL 前缀镜像（整条 GitHub 链接拼在
# 域名后），不是 HTTP 代理协议，因此实现为「改写文件类 GitHub URL 再请求」：
# 仅对文件下载域名（raw/gist/codeload + github.com 的 archive 与 release 附件）
# 按行轮换拼接前缀；GitHub API 搜索、jsdelivr 与后端域名探测不受影响。
# 多前缀按请求轮换分摊；某前缀请求网络失败时自动用原 URL 直连重试一次。

# 只匹配「文件下载」形态：路径拼接式镜像对 github.com 页面不支持
_GITHUB_FILE_HOSTS = frozenset({
    "raw.githubusercontent.com", "gist.githubusercontent.com", "codeload.github.com",
})
_GITHUB_FILE_PATHS = ("/archive/", "/releases/download/")

_MIRRORS: list = []
_MIRROR_IDX = 0


def set_mirrors(raw) -> list:
    """更新加速前缀池：去空白/去重、统一补尾斜杠，返回清洗后的清单。

    热更新语义：下一轮任务（run_crawl / run_check 入口会重设）或后台保存
    后的新请求生效；进行中的一轮沿用建 client 时的旧池。
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
    return seen


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


def mirror_url(url: str) -> str:
    """文件类 GitHub URL 按前缀池轮换改写；池为空或非文件 URL 原样返回。"""
    global _MIRROR_IDX
    if not _MIRRORS or not _is_github_file_url(url):
        return url
    prefix = _MIRRORS[_MIRROR_IDX % len(_MIRRORS)]
    _MIRROR_IDX += 1
    return prefix + url


def new_client(timeout: float = 15.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": UA, "Accept": "*/*"},
    )


async def fetch_limited(client: httpx.AsyncClient, url: str, max_bytes: int) -> dict:
    """GET 并最多读 max_bytes 字节。网络错误时 status=0，不抛异常。

    配置了加速前缀且命中 GitHub 文件 URL 时走改写地址；
    若镜像请求网络失败（status=0），自动回退原 URL 直连重试一次，
    避免「镜像临时挂了」拖垮整轮抓取。
    """
    mirrored = mirror_url(url)
    result = await _fetch_once(client, mirrored, max_bytes)
    if result["status"] == 0 and mirrored != url:
        result = await _fetch_once(client, url, max_bytes)
    return result


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
