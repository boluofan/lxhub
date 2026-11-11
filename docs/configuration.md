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
| `fetch_mirrors` | **抓取用 GitHub 加速前缀**（每行一个，如 `https://gh-proxy.com/`）。这类服务是 URL 前缀镜像而非 HTTP 代理：抓取 raw / gist / codeload 与 release 附件、archive 压缩包时把整条 GitHub 链接拼在镜像域名后；GitHub API 搜索不走前缀。多前缀按请求轮换分摊，单条请求失败可自动补试下一条，详见下方「多前缀策略」。后台「配置管理 → 爬取设置」保存即生效（进行中的一轮沿用旧值） |
| `mirror_max_attempts` / `mirror_fail_threshold` / `mirror_quarantine_seconds` | 多加速前缀容错策略的三个可调参数（单请求最多尝试数 / 熔断阈值 / 隔离秒数），后台「配置管理 → 爬取设置」可改，详见下方「多前缀策略」 |
| `probe.*` | 沙箱实测（默认关闭）。**推荐在后台「配置管理 → 沙箱实测」一键开启并设置定时任务**，保存即生效；字段说明见 [沙箱实测](probe.md) |
| `export.*` | 本地化输出（默认关闭）：把实测置信度 TopN 的脚本按固定文件名覆盖导出，见下方「本地化输出」 |
| `max_repos_total` / `max_repos_per_keyword` / `max_files_per_repo` | 单次爬取的仓库总数 / 每个关键词取几个仓库 / 每仓库候选文件数上限 |

## 多前缀策略（`fetch_mirrors`）

配置了多条前缀时，采用「**轮换均摊 + 单请求兜底 + 轻量熔断**」：

1. **均摊**：每条文件类请求按全局轮换序号选一条前缀作起点，正常情况下流量平均分摊到各条，避免把公益镜像压到限流；配置顺序无优先级含义。
2. **兜底**：起点前缀请求失败时，**自动补试下一条**，单条请求最多尝试 `mirror_max_attempts` 条前缀（默认 2）；全部候选都失败才回退原 URL 直连一次。可重试的失败包括网络异常/超时（`status=0`）、`429`、`500/502/503/504`、`408/425`，以及 2xx 但内容为空；`404`、`403` 等确定性失败不重试（文件确实下架，换哪条都一样）。
3. **熔断**：某条前缀连续失败 `mirror_fail_threshold` 次（默认 3）会被临时摘除 `mirror_quarantine_seconds` 秒（默认 300，即 5 分钟），期间不再被选中；到期后放一条「半开」探测，成功即恢复、失败重新摘除。全部前缀都处于隔离期时直接走原 URL 直连，不打镜像。

### 可调参数（后台「配置管理 → 爬取设置」）

| 后台字段 | 默认 | 范围 | 作用 |
|---|---|---|---|
| `mirror_max_attempts` | `2` | 1–10 | 单条请求最多尝试几条前缀（含轮换起点那条）。`1` = 关闭补试，轮到坏前缀就直接回退直连；调大更抗故障，但会放大请求量并拉长最坏耗时（实际不超过已配置的前缀条数） |
| `mirror_fail_threshold` | `3` | 1–100 | 某条前缀连续失败几次后临时摘除；成功一次即清零。调小剔除更快，但偶发 `429` / 网络抖动容易误伤好镜像 |
| `mirror_quarantine_seconds` | `300` | 0–86400 | 摘除时长（秒）。调小恢复快但坏前缀会被反复探测；调大更稳，但临时抽风的前缀会被晾很久 |

三个字段也可写进 `config.json` 或 `config.override.json`。保存后**立即热更新、无需重启**（进行中的一轮任务沿用旧值）；`run_crawl` / `run_check` 每轮入口还会重新应用一次，因此 CLI 与 Web 服务读同一份配置。

**注意**：修改任一策略参数会清空已有的失败计数与隔离窗口（旧的隔离截止是按旧时长算出的绝对时间戳，不重置会残留过期语义）；只保存其他字段不会「洗白」坏前缀，移除前缀则只清掉该前缀自己的状态。

代码侧的默认值与合法范围集中在 `lxhub/fetcher.py` 的 `MIRROR_POLICY_DEFAULTS` / `POLICY_LIMITS`（后台校验复用同一份，避免两处不一致）；当前生效值可用 `fetcher.mirror_policy()` 查看，各前缀连续失败数与剩余隔离秒数用 `fetcher.mirror_health()` 查看。回归测试运行 `python -m unittest discover -s tests -v`。

## 本地化输出（export.*）

把实测置信度 TopN 的音源脚本按**固定文件名**覆盖写到本地目录，用于音源本地化：
Top 榜换人时覆盖同名文件，对外 URL 始终不变。默认关闭，后台
「配置管理 → 本地化输出」可视化配置并一键手动导出。

**导出目录是固定的**：容器内恒为 `/app/outjs`，不在后台暴露。自定义落盘位置请在
`docker-compose.yml` 里挂载宿主机/NAS 目录，而不是改应用配置：

```yaml
volumes:
  - ./data:/app/data
  - /vol1/docker/lxhub/outjs:/app/outjs   # 左边换成任意目录
```

高级场景可用环境变量 `LXH_EXPORT_DIR` 覆盖；源码裸机运行时回落到项目内 `outjs/`。

| 后台字段 | 默认 | 说明 |
|---|---|---|
| `export.enabled` | `false` | 定时导出 + 实测后自动导出的总开关；「立即导出」按钮与 CLI 不受限制 |
| `export.top_n` | `3` | 导出前几名（1–50）。按置信度降序，同内容镜像/同名变体已合并 |
| `export.filename` | `lx-source-{rank}.js` | 固定文件名模板，**只允许 `{rank}` 占位符**，可写 `{rank:02d}` 补零 |
| `export.min_confidence` | `1` | 计入「高置信」的最低分（只有实测 `usable` 才有非零分） |
| `export.interval_hours` | `24` | 定时导出周期，0 = 不定时；调度器每 15 分钟检查一次 |
| `export.after_probe` | `true` | 每轮沙箱实测结束后立即刷新，保证置信度一变文件就更新 |
| `export.fill_shortfall` | `true` | 合格数不足 N 时用次优记录补齐，保证固定数量的文件始终存在 |
| `export.prune_stale` | `false` | 缩小 N 后清理多余旧文件，**只删 manifest 记录过的 .js**，不碰其他内容 |

**为什么模板只允许 `{rank}`**：含 `{name}` / `{date}` / `{id}` 的模板会随数据变化生成
新文件名，旧文件残留、对外接口路径漂移，违背「固定文件名」的目标，因此配置保存时
直接拒绝。文件名由名次唯一决定，配合「同目录临时文件 + `os.replace`」原子替换，
任何时刻读到的都是完整脚本。

触发方式三选一或叠加：定时（`interval_hours`）、每轮实测后（`after_probe`）、
后台「立即导出」/ `python main.py export-local`（手动不受 `enabled` 限制）。
每轮结果写入导出目录的 `manifest.json`（文件名、来源、置信度、sha256、`filled`
标记），并记一条 `runs(kind='export')` 供后台展示最近导出时间。

导出的文件可通过 `GET /local/lx-source-1.js` 只读访问（白名单文件名 + 目录内校验，
60 秒短缓存）；也可直接用 Nginx / NAS 共享把挂载目录暴露出去。

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
