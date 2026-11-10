# API

[← 返回 README](../README.md)

| 端点 | 说明 |
|---|---|
| `GET /api/sources` | 全量列表（不含脚本文本） |
| `GET /api/sources/{id}` | 详情（含脚本头预览） |
| `GET /api/status` | 统计 + 最近任务 |
| `GET /api/progress` | 当前任务进度（阶段、计数、实时日志，每秒轮询） |
| `POST /api/crawl` / `POST /api/check` | 触发爬取 / 复检 |
| `POST /api/probe` | 触发一轮沙箱实测（手动触发不受 `probe.enabled` 开关限制；未登录返回 `401`） |
| `POST /api/cancel` | 取消当前任务（协作式停止） |
| `GET /export.json` | 导出 |

## 说明

- `/api/sources` 会一并返回 `mirror_prefixes`，供前端渲染「加速前缀」开关；
- 每条音源含 `probe_status` / `probe_detail` 与 `confidence`(0–100) / `confidence_detail`；
  `confidence` 仅对 `usable` 非零，按成功平台数、覆盖率、音质核验、真实规格、新鲜度评分，
  列表默认已按它降序（存活、有效为前置硬门槛），分项见 `confidence_detail.parts`
  （`base / platforms / coverage / quality / spec / freshness / penalty`）。详见 [沙箱实测](probe.md)；
- 列表接口**不含**脚本文本，仅在 `/api/sources/{id}` 详情中返回前 6000 字符预览；
- 任务进行中时，`/api/crawl` 和 `/api/check` 会返回 `409`；`/api/probe` 同样受此限制，且 busy 值可为 `probe`；
- `/export.json` 结构与上述列表一致，供 curl / 第三方工具消费全量数据（CLI 亦可用 `main.py export` 落盘）。
