# 配置（config.json）

[← 返回 README](../README.md)

配置优先级（后者覆盖前者）：

```
内置默认值  <  config.json  <  .env 文件  <  进程环境变量
```

## 常用字段

| 字段 | 说明 |
|---|---|
| `github_token` | GitHub Personal Access Token（**强烈建议配置**）。完整获取与配置流程见 [配置 GitHub Token](github-token.md) |
| `keywords` | 搜索 API / 搜索页使用的关键词 |
| `seed_repos` | 种子仓库，会爬其文件树，并沿 README 里的 GitHub 链接扩展一跳 |
| `seed_urls` | 已知的音源脚本直链，每次爬取都会检测 |
| `check_interval_hours` / `crawl_interval_hours` | 复检 / 全量爬取周期 |
| `mirror_prefixes` | 复制时可选的加速前缀，见 [加速前缀](mirror-prefix.md) |
| `fetch_mirrors` | **抓取用 GitHub 加速前缀**（每行一个，如 `https://gh-proxy.com/`）。这类服务是 URL 前缀镜像而非 HTTP 代理：抓取 raw / gist / codeload 与 release 附件、archive 压缩包时把整条 GitHub 链接拼在镜像域名后，按行轮换分摊；GitHub API 搜索不走前缀。镜像请求网络失败会自动回退原 URL 直连重试一次。后台「配置管理 → 爬取设置」保存即生效（进行中的一轮沿用旧值） |
| `probe.*` | 沙箱实测（默认关闭）。**推荐在后台「配置管理 → 沙箱实测」一键开启并设置定时任务**，保存即生效；字段说明见 [沙箱实测](probe.md) |
| `max_repos_total` / `max_repos_per_keyword` / `max_files_per_repo` | 单次爬取的仓库总数 / 每个关键词取几个仓库 / 每仓库候选文件数上限 |

## 其他字段

| 字段 | 默认值 | 说明 |
|---|---|---|
| `db_path` | `data/lxhub.db` | SQLite 数据库路径 |
| `host` / `port` | `127.0.0.1` / `8787` | 服务监听地址与端口 |
| `max_file_bytes` | `1500000` | 单个脚本文件的大小上限，超出不抓取 |
| `request_timeout` | `15` | 单次 HTTP 请求超时（秒） |
| `concurrency` | `8` | 抓取并发数 |

## 环境变量

可覆盖同名配置，适合容器部署或存放密钥：

| 环境变量 | 对应字段 |
|---|---|
| `GITHUB_TOKEN` | `github_token` |
| `LXH_HOST` | `host` |
| `LXH_PORT` | `port` |
| `LXH_DB` | `db_path` |
| `LXH_ADMIN_USER` | 网页后台账户名（默认 `admin`） |
| `LXH_ADMIN_PASS` | 网页后台密码（**留空则不启用鉴权**） |

上表所有环境变量都可以直接写在项目根目录的 `.env` 里，适合本地开发与自部署
（不支持嵌套引用）。`.env` 已被 `.gitignore` 忽略，不会进版本库。
详见 [配置 GitHub Token](github-token.md) 与 [部署方式](deploy.md)。

## 想重新收录？

编辑 `seed_repos` / `keywords` 后，删除数据库重建即可：

```bash
rm data/lxhub.db*
.venv/bin/python main.py crawl
```
