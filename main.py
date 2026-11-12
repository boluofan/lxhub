"""CLI 入口：

  python main.py serve [--host H] [--port P]   启动 Web 服务（内置定时任务）
  python main.py crawl [--export FILE]         手动爬取一次
  python main.py check                         手动复检一次可用性
  python main.py export [--out FILE]           导出 JSON
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from lxhub.config import load_config
from lxhub.db import DB


def main():
    ap = argparse.ArgumentParser(prog="lxhub", description="lx 音源链接聚合工具（自部署）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="启动 Web 服务")
    p_serve.add_argument("--host")
    p_serve.add_argument("--port", type=int)

    p_crawl = sub.add_parser("crawl", help="手动爬取一次")
    p_crawl.add_argument("--export", help="爬取完成后导出 JSON 到指定文件")

    sub.add_parser("check", help="手动复检一次可用性")

    p_probe = sub.add_parser("probe", help="沙箱实测音源可用性（需 Node >=20，probe.enabled=true）")
    p_probe.add_argument("--limit", type=int, help="本轮最多探测多少条")
    p_probe.add_argument("--deep", action="store_true", help="附带音质核验（flac/flac24bit）")
    p_probe.add_argument("--yes", action="store_true",
                         help="绕过 probe.enabled 开关（手动执行时明确放行）")

    p_exp = sub.add_parser("export", help="导出 JSON")
    p_exp.add_argument("--out", default="export.json")

    sub.add_parser("export-local",
                   help="按固定文件名把置信度 TopN 脚本导出到本地化目录（容器内 /app/outjs）")

    args = ap.parse_args()
    cfg = load_config()
    db = DB(cfg.db_path)

    if args.cmd == "serve":
        import uvicorn
        from lxhub.server import app
        uvicorn.run(app, host=args.host or cfg.host, port=args.port or cfg.port,
                    log_level="info")
    elif args.cmd == "crawl":
        from lxhub.discover import run_crawl
        asyncio.run(run_crawl(cfg, db))
        if args.export:
            Path(args.export).write_text(
                json.dumps(db.export(cfg.mirror_prefixes), ensure_ascii=False, indent=1),
                encoding="utf-8")
            print(f"[导出] {args.export}")
    elif args.cmd == "check":
        from lxhub.checker import run_check
        asyncio.run(run_check(cfg, db))
    elif args.cmd == "probe":
        from lxhub.probe import run_probe
        asyncio.run(run_probe(cfg, db, limit=args.limit,
                              deep=True if args.deep else None,
                              force=args.yes))
    elif args.cmd == "export-local":
        from lxhub.exporter import run_export
        result = run_export(cfg, db, trigger="cli")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.cmd == "export":
        Path(args.out).write_text(
            json.dumps(db.export(cfg.mirror_prefixes), ensure_ascii=False, indent=1),
            encoding="utf-8")
        print(f"[导出] {args.out}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
