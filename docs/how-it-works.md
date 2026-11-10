# 工作原理

[← 返回 README](../README.md)

```
发现仓库                 收集候选链接                逐个抓取 + 静态解析           展示
┌──────────────────┐   ┌───────────────────────┐   ┌──────────────────────┐   ┌─────────┐
│ GitHub 搜索 API   │   │ README 中的 .js 直链    │   │ HTTP 状态 / 大小 / 哈希 │   │ FastAPI │
│ 搜索页 HTML 兜底   │ → │ GitHub trees API 列文件  │ → │ @注释元数据            │ → │ SQLite  │
│ 种子仓库 + 一跳扩展 │   │ jsDelivr 兜底 + 种子直链 │   │ lx 脚本静态特征         │   │ 静态页   │
└──────────────────┘   └───────────────────────┘   └──────────────────────┘   └─────────┘
```

## 发现仓库

按优先级，全部失败时仍有种子仓库兜底：

1. GitHub 搜索 API（配置 Token 后稳定；未配置也会尝试）
2. `github.com` 搜索结果页 HTML 解析（免配额，但可能被 429）
3. `config.json` 里的种子仓库 + 种子仓库 README 中的一跳引用扩展

## 收集候选链接

- README 里的绝对 / 相对 `.js` 链接 → 转 `raw.githubusercontent.com` 直链
- **GitHub git trees API 列全量文件**（覆盖最全，需 Token 保障配额）
- 拿不到时降级 jsDelivr data API（免配额，但对没有 release 的仓库列不出文件）

应用内部路径（`src/main/`、`android/`、`node_modules/` 等）会被跳过。

## 判定与解析

- 「疑似音源脚本」按**注册结构**分级判定（不是关键词共现，那会误收仓库里的模板/脚手架）：
  - **高置信（confirmed）**：订阅了 request 事件（`on(EVENT_NAMES.request)` 等写法）
    且应答 `musicUrl`（`getMusicUrl` 方法或 `'musicUrl'` action 字符串）——结构完整的音源；
  - **待验证（probable）**：仅订阅了 request 事件，或是混淆脚本（`_0x` 标识符等特征；
    字面量被藏进字符串表，改凭明文头注释 `@name + @version/@update_url` 识别）；
  - 无任何结构特征的候选**不再入库**；
  - Node 代码（`module.exports` / 多处 `require` 的运行时框架、桥接服务）会被排除；
  - 解析出的 `@name` 含 测试/示例/废弃 等特征、或无 `@name` 且文件名是 index/main 等通用名的，不当作有效音源；
  - 名称不含任何中文的也不收录（`lx-source`、`runtime`、`Lx-Custom-Source` 这类
    模板/脚手架/客户端脚本）——该生态中真实分发的音源几乎全部中文命名；
  **非音源的存量记录会被清理**；真音源失效后保留 7 天宽限期再清除，方便看到"失效"状态；
- 「可用」= 该 URL 当前能以 HTTP 2xx 下载到文件；SHA256 用于发现脚本内容更新，
  相同内容的仓库镜像自动合并为一条主记录（`mirrors` 字段）；
  不同仓库搬运/改版产生的**同名脚本**（内容有差异）也会归并为一条，其余记入
  `variants`（同版本搬运选最优展示；"二改修复版"这类语义变体保持独立条目），
  主记录失效时下次读取自动改选组内最优；
- 复检**覆盖每一个 URL**（含镜像，不只是主记录）。主记录失效时，存活的镜像会自动
  顶替为新主记录（列表按 `is_alive DESC` 排序），失效的原链接退回 `mirrors`，
  因此不会出现「还有可用副本却显示失效」的误判；
- 「后端状态」= 从脚本里提取其声明的后端 API 域名并探测存活（仅测服务器是否响应，
  **不调用任何取歌接口**），所有**有效音源**都会探测（含结构不全/混淆判定的）；
  所有后端均不可达时标「后端·挂」，该源大概率已不可用；
  连续多轮（默认 8 轮，约 4 天）后端全部不可达的音源视同已废弃，自动清出列表；
- 可选的「沙箱实测」（默认关闭，见 [probe 文档](probe.md)）：在以上静态链路之后，
  把脚本放进 Node 沙箱真实取歌一次，产出 `usable / degraded / not-loadable` 三个级别
  与逐平台失败原因；对 `usable` 额外算 0–100 的**置信度**（成功平台数、平台覆盖率、
  L2 音质结论、真实音频规格、实测新鲜度等），实测可用按分数高低整体前置，
  其余仍按「可用 > 未测 > 失败」排列（详见 [probe 文档](probe.md)）。
  置信度也决定同名组主记录与同仓库副本收敛保留哪一条。
  复检定时任务完成后会自动对「内容更新/未测/缓存过期」的条目补测。
- 「仓库更新时间」= 来源仓库在 GitHub 的最后 push 时间（搜索结果自带，缺失时
  逐仓库查 repo API 补齐）。「更新」列与默认排序都以它为准——长期不维护的仓库，
  其音源后端大概率已失效，会自动沉底；悬停可看 内容更新/最近检测 时间；
- 平台标签（酷我 / 网易云等）来自词频猜测，仅供展示参考。

## 为什么用 trees API 而不是 jsDelivr

实测对比（同一仓库的 js 文件数）：

| 仓库 | jsDelivr | GitHub trees API |
|---|---|---|
| `Macrohard0001/lx-ikun-music-sources` | 0 | 969 |
| `guoyue2010/lxmusic-` | 0 | 274 |

jsDelivr 依赖仓库有 release / tag，而多数音源仓库不打 tag，导致整库被跳过。
trees API 不依赖 release，但要消耗核心 API 配额，因此需要 Token——这也是
[配置 Token](github-token.md) 能显著提升收录量的原因。

## 代码结构

```
main.py              CLI 入口：serve / crawl / check / probe / export
lxhub/
  config.py          配置：默认值 <- config.json <- .env <- 环境变量
  discover.py        发现仓库 → 收集 .js 候选 → 抓取静态解析 → 入库
  fetcher.py         httpx 封装：流式限大小 GET、后端域名存活探测
  metadata.py        @注释元数据、音源特征评分、类型分类、平台猜测
  checker.py         复检链接存活 + 后端探测（收尾可自动接沙箱实测）
  probe/             沙箱实测调度器
    node/env.mjs     lx 全局环境模拟（fetch/crypto/zlib/Buffer… + 私网防护）
    node/probe.mjs   独立 worker：vm 沙箱执行脚本 → 取链 → Range/魔数核验 + L2 音质
  db.py              SQLite 存储 + 镜像合并 + 失效清理（含 probe_* 列）
  progress.py        任务进度 / 日志环形缓冲 / 取消标记
  server.py          FastAPI 路由 + 内置定时调度器
```
