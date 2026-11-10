"""SQLite 存储层：线程级持久连接 + 写锁，WAL 模式。"""
import datetime as dt
import json
import re
import sqlite3
import threading
from pathlib import Path

from . import confidence

class _Conn:
    """单实例持久连接（线程安全读、写锁保护）。

    check_same_thread=False：定时导出经 asyncio.to_thread 在线程池里读写，
    连接却在模块导入线程创建。所有访问都走 _LOCK 保护的 _run/new_run，
    串行化已由锁保证，因此可以安全地跨线程复用这一个连接。
    """
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")


_LOCK = threading.Lock()


# ---------- 同名副本收敛 ----------
# 音源仓库常按日期/版本把同一脚本复制多份（V260322/、V260907/…），
# 这里从 URL 里取最像"快照版本号"的片段作为排序依据，越大越新。
_DATE_TOKEN = re.compile(r"(?:^|/|[-_.])[Vv]?(\d{6})(?=$|/|[-_.])")
_VER_TOKEN = re.compile(r"[Vv]?(\d+(?:\.\d+)*)")


def _snapshot_key(url: str) -> str:
    """提取日期/版本片段作为排序键；取不到返回空串。"""
    m = _DATE_TOKEN.search(url or "")
    if m:
        return m.group(1)                    # V260907 / 202609 这类日期快照
    m = _VER_TOKEN.search(url or "")
    if m:
        # 2.3.0 -> 000200030000，让字符串比较等价于逐段数值比较
        return "".join(p.zfill(4) for p in m.group(1).split("."))
    return ""


def _copy_rank(row: dict) -> tuple:
    """副本优劣：存活 > 实测置信度 > 最近存活时间 > 快照版本更新 > 路径更短。

    高置信副本即使版本略旧也不该被清掉——它是真的能取到歌的那一份。
    """
    return (1 if row.get("is_alive") else 0,
            int(row.get("confidence") or 0),
            row.get("last_alive") or "",
            _snapshot_key(row.get("url")),
            -len(row.get("url") or ""))


def _norm_name(name: str) -> str:
    """名称归一：去 emoji/空格/标点装饰，仅留中英文与数字，供跨仓库同名分组。

    只剥装饰不剥语义后缀：「长青SVIP音源(二改修复版)」与「长青SVIP音源」
    归一后仍不同组——二改版是不同的脚本，要保留各自可见性。
    """
    return re.sub(r"[\W_]+", "", (name or "").lower())


def _ver_key(d: dict) -> str:
    """组内排序键：优先取 @version 字段，拿不到再看 URL 里的版本片段。"""
    m = _VER_TOKEN.search(d.get("version") or "")
    if m:
        return "".join(p.zfill(4) for p in m.group(1).split("."))
    return _snapshot_key(d.get("url"))


def _group_rank(d: dict) -> tuple:
    """同名组内主记录优劣：存活 > 实测置信度 > 静态高置信 > 实测状态 > 版本 > 时间。

    失效的高置信旧副本不能压过存活的低分副本；置信度只在存活组内比较。
    版本优先于最近存活时间：复检时间只取决于检测顺序，不能反映脚本新旧。
    """
    alive = 1 if d.get("is_alive") else 0
    return (alive,
            int(d.get("confidence") or 0) * alive,
            d.get("valid_tier") == "confirmed",
            _probe_rank(d.get("probe_status") or ""),
            _ver_key(d),
            d.get("last_alive") or "",
            -len(d.get("url") or ""))


def _probe_rank(status: str) -> int:
    """实测状态排序权重（reverse=True 高者优先）：usable=2 > 未测=1 > 失败=0。"""
    if status == "usable":
        return 2
    if not status:
        return 1
    return 0


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,
    name TEXT DEFAULT '',
    version TEXT DEFAULT '',
    author TEXT DEFAULT '',
    description TEXT DEFAULT '',
    homepage TEXT DEFAULT '',
    platforms TEXT DEFAULT '[]',
    origin_repo TEXT DEFAULT '',
    origin_url TEXT DEFAULT '',
    origin_kind TEXT DEFAULT '',
    file_size INTEGER DEFAULT 0,
    sha256 TEXT DEFAULT '',
    http_status INTEGER DEFAULT 0,
    is_alive INTEGER DEFAULT 0,
    looks_valid INTEGER DEFAULT 0,
    valid_tier TEXT DEFAULT '',
    script_type TEXT DEFAULT '',
    api_hosts TEXT DEFAULT '[]',
    api_alive TEXT DEFAULT '',
    api_checked TEXT DEFAULT '',
    api_down_streak INTEGER DEFAULT 0,
    repo_pushed_at TEXT DEFAULT '',
    content_text TEXT DEFAULT '',
    first_seen TEXT DEFAULT '',
    last_checked TEXT DEFAULT '',
    last_alive TEXT DEFAULT '',
    content_updated_at TEXT DEFAULT '',
    probe_status TEXT DEFAULT '',
    probe_detail TEXT DEFAULT '',
    probe_sha TEXT DEFAULT '',
    probe_fail_streak INTEGER DEFAULT 0,
    probe_checked TEXT DEFAULT '',
    confidence INTEGER DEFAULT 0,
    confidence_detail TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sources_alive ON sources(is_alive, looks_valid);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT DEFAULT '',
    detail TEXT DEFAULT ''
);
"""

_INSERT_COLS = [
    "url", "name", "version", "author", "description", "homepage", "platforms",
    "origin_repo", "origin_url", "origin_kind", "file_size", "sha256",
    "http_status", "is_alive", "looks_valid", "valid_tier", "script_type",
    "api_hosts", "api_alive", "api_checked", "api_down_streak",
    "repo_pushed_at", "content_text",
    "first_seen", "last_checked", "last_alive", "content_updated_at",
]

_UPSERT_SQL = f"""
INSERT INTO sources ({", ".join(_INSERT_COLS)})
VALUES ({", ".join("?" * len(_INSERT_COLS))})
ON CONFLICT(url) DO UPDATE SET
    name=excluded.name,
    version=excluded.version,
    author=excluded.author,
    description=excluded.description,
    homepage=excluded.homepage,
    platforms=excluded.platforms,
    origin_repo=CASE WHEN excluded.origin_repo != '' THEN excluded.origin_repo ELSE sources.origin_repo END,
    origin_url=CASE WHEN excluded.origin_url != '' THEN excluded.origin_url ELSE sources.origin_url END,
    origin_kind=CASE WHEN excluded.origin_kind != '' THEN excluded.origin_kind ELSE sources.origin_kind END,
    file_size=excluded.file_size,
    sha256=excluded.sha256,
    http_status=excluded.http_status,
    is_alive=excluded.is_alive,
    looks_valid=excluded.looks_valid,
    valid_tier=excluded.valid_tier,
    script_type=excluded.script_type,
    api_hosts=CASE WHEN excluded.is_alive = 1 THEN excluded.api_hosts ELSE sources.api_hosts END,
    api_alive=excluded.api_alive,
    api_checked=excluded.api_checked,
    repo_pushed_at=CASE WHEN excluded.repo_pushed_at != '' THEN excluded.repo_pushed_at ELSE sources.repo_pushed_at END,
    content_text=CASE WHEN excluded.content_text != '' THEN excluded.content_text ELSE sources.content_text END,
    content_updated_at=CASE
        WHEN excluded.sha256 != '' AND sources.sha256 != '' AND excluded.sha256 != sources.sha256
            THEN excluded.last_checked
        WHEN sources.content_updated_at != '' THEN sources.content_updated_at
        ELSE sources.first_seen END,
    last_checked=excluded.last_checked,
    last_alive=CASE WHEN excluded.is_alive = 1 THEN excluded.last_alive ELSE sources.last_alive END
"""

_DEFAULTS = {
    "name": "", "version": "", "author": "", "description": "", "homepage": "",
    "platforms": "[]", "origin_repo": "", "origin_url": "", "origin_kind": "",
    "file_size": 0, "sha256": "", "http_status": 0, "is_alive": 0,
    "looks_valid": 0, "valid_tier": "", "script_type": "", "api_hosts": "[]",
    "api_alive": "", "api_checked": "", "api_down_streak": 0,
    "repo_pushed_at": "", "content_text": "",
    "first_seen": "", "last_checked": "", "last_alive": "", "content_updated_at": "",
}


class DB:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self._c = _Conn(path)
        self._migrate(self._c.conn)
        self._c.conn.commit()
        # 老库升级后补算存量「实测可用」记录的置信度（幂等，只处理未评分行）
        self.backfill_confidence()

    @staticmethod
    def _migrate(conn: sqlite3.Connection):
        """确保所有表存在并补全新列（旧库迁移）。"""
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "sources" not in tables:
            conn.execute("""
                CREATE TABLE sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT UNIQUE NOT NULL,
                    name TEXT DEFAULT '',
                    version TEXT DEFAULT '',
                    author TEXT DEFAULT '',
                    description TEXT DEFAULT '',
                    homepage TEXT DEFAULT '',
                    platforms TEXT DEFAULT '[]',
                    origin_repo TEXT DEFAULT '',
                    origin_url TEXT DEFAULT '',
                    origin_kind TEXT DEFAULT '',
                    file_size INTEGER DEFAULT 0,
                    sha256 TEXT DEFAULT '',
                    http_status INTEGER DEFAULT 0,
                    is_alive INTEGER DEFAULT 0,
                    looks_valid INTEGER DEFAULT 0,
                    script_type TEXT DEFAULT '',
                    api_hosts TEXT DEFAULT '[]',
                    api_alive TEXT DEFAULT '',
                    api_checked TEXT DEFAULT '',
                    content_text TEXT DEFAULT '',
                    first_seen TEXT DEFAULT '',
                    last_checked TEXT DEFAULT '',
                    last_alive TEXT DEFAULT '',
                    content_updated_at TEXT DEFAULT ''
                )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sources_alive ON sources(is_alive, looks_valid)")
        if "runs" not in tables:
            conn.execute("""
                CREATE TABLE runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT DEFAULT '',
                    detail TEXT DEFAULT ''
                )""")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sources)")}
        for col, ddl in (
                ("script_type", "TEXT DEFAULT ''"),
                ("api_hosts", "TEXT DEFAULT '[]'"),
                ("api_alive", "TEXT DEFAULT ''"),
                ("api_checked", "TEXT DEFAULT ''"),
                ("valid_tier", "TEXT DEFAULT ''"),
                ("api_down_streak", "INTEGER DEFAULT 0"),
                ("repo_pushed_at", "TEXT DEFAULT ''"),
                ("content_updated_at", "TEXT DEFAULT ''"),
                ("probe_status", "TEXT DEFAULT ''"),
                ("probe_detail", "TEXT DEFAULT ''"),
                ("probe_sha", "TEXT DEFAULT ''"),
                ("probe_fail_streak", "INTEGER DEFAULT 0"),
                ("probe_checked", "TEXT DEFAULT ''"),
                ("confidence", "INTEGER DEFAULT 0"),
                ("confidence_detail", "TEXT DEFAULT ''")):
            if col not in cols:
                try:
                    conn.execute(f"ALTER TABLE sources ADD COLUMN {col} {ddl}")
                    if col == "content_updated_at":
                        conn.execute(
                            "UPDATE sources SET content_updated_at = first_seen "
                            "WHERE content_updated_at = '' OR content_updated_at IS NULL")
                    if col == "valid_tier":
                        # 存量有效记录暂记中置信，待下轮爬取按新分级重新评估
                        conn.execute(
                            "UPDATE sources SET valid_tier = 'probable' "
                            "WHERE looks_valid = 1 AND (valid_tier = '' OR valid_tier IS NULL)")
                except sqlite3.OperationalError:
                    pass

    def _run(self, sql: str, params: tuple = ()) -> list:
        with _LOCK:
            cur = self._c.conn.execute(sql, params)
            self._c.conn.commit()
            return cur.fetchall()

    # ---------- sources ----------

    def upsert_source(self, d: dict):
        row = dict(_DEFAULTS)
        for k in _DEFAULTS:
            if d.get(k) is not None:
                row[k] = d[k]
        row["url"] = d["url"]
        if not row.get("content_updated_at"):
            row["content_updated_at"] = row.get("first_seen") or ""
        if isinstance(row.get("platforms"), list):
            row["platforms"] = json.dumps(row["platforms"], ensure_ascii=False)
        params = tuple(row[c] for c in _INSERT_COLS)
        self._run(_UPSERT_SQL, params)

    def _to_source_dict(self, r: sqlite3.Row, with_content: bool = False) -> dict:
        d = dict(r)
        # 旧库文件里可能还留着已废弃的标注列，统一不对外输出
        d.pop("user_mark", None)
        d.pop("marked_at", None)
        try:
            d["platforms"] = json.loads(d.get("platforms") or "[]")
        except json.JSONDecodeError:
            d["platforms"] = []
        if d.get("probe_detail"):
            try:
                d["probe_detail"] = json.loads(d["probe_detail"])
            except (json.JSONDecodeError, TypeError):
                d["probe_detail"] = {}
        if d.get("confidence_detail"):
            try:
                d["confidence_detail"] = json.loads(d["confidence_detail"])
            except (json.JSONDecodeError, TypeError):
                d["confidence_detail"] = {}
        else:
            d["confidence_detail"] = {}
        d["confidence"] = int(d.get("confidence") or 0)
        d["is_alive"] = bool(d["is_alive"])
        d["looks_valid"] = bool(d["looks_valid"])
        if not with_content:
            d.pop("content_text", None)
        return d

    def list_sources(self, valid_only: bool = False, limit: int = 5000) -> list:
        sql = "SELECT * FROM sources"
        if valid_only:
            sql += " WHERE looks_valid = 1"
        sql += (
            # 存活/有效仍是硬门槛；其后按实测置信度降序，让"多平台可用 + 音质达标"
            # 的音源整体排到未实测/降级/失败的前面。失效行的历史分数不参与排序。
            " ORDER BY is_alive DESC, looks_valid DESC,"
            " CASE WHEN is_alive = 1 THEN confidence ELSE 0 END DESC,"
            " CASE WHEN valid_tier = 'confirmed' THEN 0 ELSE 1 END,"
            " CASE probe_status WHEN 'usable' THEN 0 WHEN '' THEN 2 ELSE 1 END,"
            " last_alive DESC, first_seen DESC LIMIT ?"
        )
        rows = [self._to_source_dict(r) for r in self._run(sql, (limit,))]
        # 同内容多仓库镜像合并：保留一条主记录，其余记入 mirrors
        merged: list = []
        by_hash: dict = {}
        for d in rows:
            key = d["sha256"] or f"url:{d['id']}"
            if key in by_hash:
                by_hash[key]["mirrors"].append(d["url"])
            else:
                d["mirrors"] = []
                by_hash[key] = d
                merged.append(d)
        # 跨仓库同名分组：不同仓库搬运/改版产生的同名脚本只展示一条主记录，
        # 其余记入 variants（内容有差异，不能当镜像删掉——"二改修复版"这类
        # 变体可能比原版更好用）。非破坏性：主记录失效时下次读取自动改选最优。
        groups: dict = {}
        for i, d in enumerate(merged):
            d["variants"] = []
            key = _norm_name(d["name"])
            if key:
                groups.setdefault(key, []).append((i, d))
        skip: set = set()
        for g in groups.values():
            if len(g) < 2:
                continue
            g.sort(key=lambda x: _group_rank(x[1]), reverse=True)
            head = g[0][1]
            head["variants"] = [
                {k: v.get(k) for k in ("url", "origin_repo", "origin_url", "version",
                                       "valid_tier", "is_alive", "last_alive", "mirrors",
                                       "probe_status", "probe_checked", "confidence")}
                for _, v in g[1:]]
            skip.update(i for i, _ in g[1:])
        return [d for i, d in enumerate(merged) if i not in skip]

    def all_sources_rows(self, limit: int = 100_000, with_content: bool = False) -> list:
        """全部原始行（不合并镜像），供复检使用。

        list_sources() 会把相同 sha256 的镜像合并掉，复检若只用它就会漏掉镜像
        URL——主记录失效时无法感知镜像是否仍可用。因此复检必须遍历每一行。
        with_content=True 时一并返回完整脚本内容（用于打包导出）。
        """
        rows = self._run("SELECT * FROM sources ORDER BY last_checked ASC LIMIT ?", (limit,))
        return [self._to_source_dict(r, with_content=with_content) for r in rows]

    def get_source(self, sid: int) -> dict | None:
        rows = self._run("SELECT * FROM sources WHERE id = ?", (sid,))
        return self._to_source_dict(rows[0], with_content=True) if rows else None

    def all_urls(self) -> set:
        return {r["url"] for r in self._run("SELECT url FROM sources")}

    def update_check(self, sid: int, status: int, alive: bool, size: int,
                     sha256: str, checked_at: str):
        self._run(
            """UPDATE sources SET
                 http_status = ?,
                 is_alive = ?,
                 file_size = CASE WHEN ? > 0 THEN ? ELSE file_size END,
                 sha256 = CASE WHEN ? != '' THEN ? ELSE sha256 END,
                 content_updated_at = CASE
                     WHEN ? != '' AND sha256 != '' AND ? != sha256 THEN ?
                     WHEN content_updated_at != '' THEN content_updated_at
                     ELSE ? END,
                 last_checked = ?,
                 last_alive = CASE WHEN ? = 1 THEN ? ELSE last_alive END
               WHERE id = ?""",
            (status, 1 if alive else 0,
             size, size,
             sha256, sha256,
             sha256, sha256, checked_at, checked_at,
             checked_at,
             1 if alive else 0, checked_at,
             sid))

    def delete_url(self, url: str):
        self._run("DELETE FROM sources WHERE url = ?", (url,))

    def delete_urls(self, urls: list):
        with _LOCK:
            self._c.conn.executemany("DELETE FROM sources WHERE url = ?",
                                     [(u,) for u in urls])
            self._c.conn.commit()

    def prune_invalid(self, older_than: str) -> int:
        """删除非音源记录；失效音源保留到 older_than（ISO 时间）之后。

        - 能正常下载但不是音源脚本（普通 js）→ 立即删除
        - 曾是音源、现在下载失败 → 宽限期内保留展示"失效"，过期删除
        """
        with _LOCK:
            cur = self._c.conn.execute(
                """DELETE FROM sources WHERE looks_valid = 0 AND (
                     is_alive = 1 OR last_alive = '' OR last_alive < ?)""",
                (older_than,))
            self._c.conn.commit()
            return cur.rowcount

    def prune_same_name_copies(self) -> int:
        """同一来源仓库 + 同一脚本名的多份副本，只保留最优的一份。

        音源仓库常按日期/版本把同一脚本复制多份（V260322/、V260907/…），内容略有
        差异却各占一条，是列表噪声的主要来源。这里只收敛**同一仓库内**的同名副本；
        跨仓库的相同内容仍作为镜像保留，主链接失效时镜像仍可顶上。
        """
        rows = self._run("""SELECT id, url, name, origin_repo, is_alive, last_alive, confidence
                            FROM sources WHERE looks_valid = 1 AND TRIM(name) != ''""")
        groups: dict = {}
        for r in rows:
            key = ((r["origin_repo"] or "").lower(), (r["name"] or "").strip().lower())
            groups.setdefault(key, []).append(dict(r))
        drop = []
        for g in groups.values():
            if len(g) < 2:
                continue
            g.sort(key=_copy_rank, reverse=True)
            drop += [x["url"] for x in g[1:]]
        if drop:
            self.delete_urls(drop)
        return len(drop)

    def update_api_probe(self, sid: int, alive: str, checked_at: str):
        """记录后端探测结果，并维护连续不可达计数（up 清零，down 累加）。"""
        self._run(
            "UPDATE sources SET api_alive = ?, api_checked = ?,"
            "    api_down_streak = CASE WHEN ? = 'down' THEN api_down_streak + 1 ELSE 0 END"
            " WHERE id = ?",
            (alive, checked_at, alive, sid))

    def update_probe(self, sid: int, status: str, detail: str, sha: str, checked_at: str):
        """记录沙箱实测结果，同时写入置信度评分。

        失败连击仅对 degraded/not-loadable 累加，usable 清零；非 usable 的置信度归零，
        避免一条曾经可用、后来实测失败的音源继续顶着高分排在前面。
        """
        fail = status in ("degraded", "not-loadable")
        scored = confidence.score_probe(status, detail, checked_at)
        self._run(
            "UPDATE sources SET probe_status = ?, probe_detail = ?, probe_sha = ?,"
            " probe_checked = ?, confidence = ?, confidence_detail = ?,"
            " probe_fail_streak = CASE WHEN ? = 1"
            " THEN probe_fail_streak + 1 ELSE 0 END WHERE id = ?",
            (status, detail, sha, checked_at, scored["score"],
             json.dumps(scored, ensure_ascii=False), 1 if fail else 0, sid))

    def backfill_confidence(self) -> int:
        """为升级前已存在的 usable 记录补算置信度（幂等）。

        新列默认 0，但 0 分也可能是合法评分，所以以 confidence_detail 是否为空
        判断"尚未评分"，避免每次启动反复重算。
        """
        rows = self._run(
            "SELECT id, probe_detail, probe_checked FROM sources"
            " WHERE probe_status = 'usable' AND probe_detail != ''"
            " AND (confidence_detail = '' OR confidence_detail IS NULL)")
        now = dt.datetime.now(dt.timezone.utc)
        for r in rows:
            scored = confidence.score_probe("usable", r["probe_detail"],
                                            r["probe_checked"] or "", now=now)
            self._run("UPDATE sources SET confidence = ?, confidence_detail = ? WHERE id = ?",
                      (scored["score"], json.dumps(scored, ensure_ascii=False), r["id"]))
        return len(rows)

    def probe_candidates(self, refresh_hours: float, limit: int) -> list:
        """待实测行：有效音源 +（未测/内容变更/超过 refresh_hours）。

        失败连击达上限（4，与 prune_dead_backends 一致）的行不再排队，
        避免手动/定时轮反复消耗死源；它们会在下一轮复检后被 prune 清出。
        """
        cutoff = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(hours=refresh_hours)).isoformat(timespec="seconds")
        rows = self._run(
            """SELECT id, url, name, sha256, probe_status, probe_detail,
                      probe_sha, probe_checked, probe_fail_streak, valid_tier
               FROM sources
               WHERE is_alive = 1 AND looks_valid = 1 AND probe_fail_streak < 4
               ORDER BY CASE probe_status WHEN '' THEN 0 ELSE 1 END, last_checked ASC
               LIMIT 2000""")
        out = []
        for r in rows:
            # 未测过 / probe 结果早于 refresh 窗口 / 内容已更新（probe_sha 与当前 sha 不符）→ 重测
            fresh = (r["probe_status"] != "" and r["probe_sha"]
                     and r["probe_sha"] == (r["sha256"] or "")
                     and (r["probe_checked"] or "") >= cutoff)
            if not fresh:
                out.append(dict(r))
                if len(out) >= limit:
                    break
        return out

    def set_repo_pushed_at(self, repo: str, pushed_at: str):
        """把 GitHub 仓库最后更新时间写到该仓库收录的全部行（排序可用性先验）。"""
        self._run("UPDATE sources SET repo_pushed_at = ? WHERE origin_repo = ?",
                  (pushed_at, repo))

    def prune_dead_backends(self, min_streak: int = 8) -> int:
        """链接仍在、但后端连续 min_streak 轮全部不可达的音源，视为已废弃清出列表。

        探测是根路径 ping：拿不到任何 HTTP 响应（DNS 失败/超时/拒连）才计「挂」。
        默认 8 轮按 12h 复检间隔约 4 天，足以排除临时网络抖动；清掉的是
        "链接活着但音源早已废弃"的僵尸记录。
        实测可用（probe_status='usable'）的记录永不清理：它有比后端 ping 更强的
        存活证据。另附实测失败连击达 probe_min_streak 的记录一并清理。
        """
        with _LOCK:
            cur = self._c.conn.execute(
                """DELETE FROM sources WHERE looks_valid = 1 AND (
                       (api_down_streak >= ? AND probe_status != 'usable')
                       OR probe_fail_streak >= 4)""",
                (min_streak,))
            self._c.conn.commit()
            return cur.rowcount

    def stats(self) -> dict:
        rows = self._run(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(is_alive), 0) AS alive,
                      COALESCE(SUM(looks_valid), 0) AS valid,
                      COALESCE(SUM(CASE WHEN looks_valid = 1 AND is_alive = 1 THEN 1 ELSE 0 END), 0) AS valid_alive,
                      COALESCE(SUM(CASE WHEN probe_status = 'usable' THEN 1 ELSE 0 END), 0) AS probe_usable,
                      COALESCE(SUM(CASE WHEN probe_status = 'degraded' THEN 1 ELSE 0 END), 0) AS probe_degraded,
                      COALESCE(SUM(CASE WHEN probe_status = 'not-loadable' THEN 1 ELSE 0 END), 0) AS probe_not_loadable
               FROM sources""")
        d = dict(rows[0])
        return {k: int(v) for k, v in d.items()}

    def export(self, mirror_prefixes: list | None = None) -> dict:
        """导出全量数据（供 /export.json 接口与 CLI `main.py export` 使用）。

        mirror_prefixes 会随数据一起下发，便于第三方工具按加速前缀还原链接。
        """
        return {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "stats": self.stats(),
            "sources": self.list_sources(limit=100_000),
            "mirror_prefixes": mirror_prefixes or [],
        }

    # ---------- runs ----------

    def new_run(self, kind: str, started_at: str) -> int:
        with _LOCK:
            cur = self._c.conn.execute(
                "INSERT INTO runs (kind, started_at) VALUES (?, ?)",
                (kind, started_at))
            self._c.conn.commit()
            return cur.lastrowid

    def finish_run(self, run_id: int, finished_at: str, detail: str = ""):
        self._run("UPDATE runs SET finished_at = ?, detail = ? WHERE id = ?",
                  (finished_at, detail, run_id))

    def last_run(self, kind: str) -> dict | None:
        rows = self._run(
            "SELECT * FROM runs WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,))
        return dict(rows[0]) if rows else None
