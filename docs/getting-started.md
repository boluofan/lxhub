# 快速开始

[← 返回 README](../README.md)

## 环境准备

需要 Python 3.10+（推荐 3.12）。

```bash
# macOS / Linux
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 首次爬取

```bash
.venv/bin/python main.py crawl
```

会输出进度日志。**建议先配置 GitHub Token**（见 [配置 GitHub Token](github-token.md)），
否则容易触发 GitHub 限流，收录数量会明显偏少。

> 未配置 Token 时实测只能收录个位数；配置并修复文件树获取方式后可达几十条以上。

## 启动服务

```bash
.venv/bin/python main.py serve
```

浏览器打开 http://127.0.0.1:8787

## 命令行

| 命令 | 说明 |
|---|---|
| `python main.py serve [--host H] [--port P]` | 启动 Web 服务（内置定时任务） |
| `python main.py crawl [--export FILE]` | 手动爬取一次 |
| `python main.py check` | 手动复检一次可用性 |
| `python main.py probe [--deep] [--limit N] --yes` | 手动沙箱实测（需 Node ≥20，`--yes` 放行 probe.enabled 开关） |
| `python main.py export [--out FILE]` | 导出 JSON |

## 页面功能

- 每行带顺序编号（1、2、3…，随排序/筛选变化，输入纯数字可跳到对应行）
- 搜索 / 过滤（状态：全部 / 可访问 / 后端挂 / 失效 / 实测可用 / 实测异常；类型：音源与存疑 / 仅音源 / 仅存疑 / 全部）
- 排序：点表头列（名称 / 平台 / 版本 / 作者 / 更新 / 实测 / 置信度）；默认「存活优先 + 实测置信度高优先 + 静态高置信 + 最近更新优先」，实测可用音源按成功平台数、音质核验等评分排在最前，选择记在浏览器 localStorage
- 单条复制链接
- 详情（脚本头预览）
- 导出 TXT（每行一条链接，范围受当前筛选影响；开启加速前缀时自动拼接）
- 「加速前缀」开关（复制 / 导出时自动拼接镜像前缀），见 [加速前缀](mirror-prefix.md)
- 手动触发爬取 / 复检（带进度条与实时日志，可取消）

服务内置定时任务：每 24h 爬取一次、每 12h 复检一次，
周期可在 `config.json` 调整，见 [配置](configuration.md)。

## 下一步

- [配置说明](configuration.md) — `config.json` 各字段
- [配置 GitHub Token](github-token.md) — 提升收录覆盖面的关键
- [部署方式](deploy.md) — Docker / Railway
