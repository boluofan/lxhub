# LX Source Hub · lx 音源链接聚合（自部署）

从 GitHub 等公开渠道**发现并整理**洛雪音乐（lx-music）自定义音源脚本的发布链接，
定期检查链接可达性，并在一个 Web 页面中展示，方便复制导入。

## 定位与边界（重要）

本项目**只做下面几件事**：

1. 发现公开仓库/页面中发布的音源脚本直链（`.js`）；
2. 记录元数据（名称、版本、作者等，全部来自脚本头部注释的静态解析）；
3. 定期检查链接是否仍可下载、脚本文本是否具备 lx 音源特征（`globalThis.lx` 等静态特征）；
4. （可选）把实测置信度最高的若干个脚本按**固定文件名**覆盖导出到本地目录，方便自建长期不变的对外 URL。

本项目**不做**：不存储/解析/提供任何音乐内容；不请求任何音乐免费/付费资源的下载分页。

音源脚本本身的内容、合法性与使用风险由使用者自行承担。

### 可选的沙箱实测（默认关闭）

在默认的静态检查之上，提供一个**可选**的增强能力：把收录的音源脚本放入
Node 沙箱（node `vm` + 全局 `lx` 环境模拟）真实运行一次，看其能否返回音频地址：

- 词条为固定测试歌曲（128k 起步），只测"存活与可用性"，不验证账号/付费权限；
- 结果分级 `usable / degraded / not-loadable`，配合各平台失败原因逐一展示；
- 支持 `deep` 模式对 flac/flac24bit 声明做音质核验（Bitrate/采样率/位深）；
- 默认 `probe.enabled=false`；**后台「配置管理 → 沙箱实测」可一键开关并设置定时任务**
  （保存即生效，无需重启），开启需运行环境有 Node ≥20（官方 Docker 镜像已内置，
  源码裸机部署需自行安装）；详见 [沙箱实测](docs/probe.md)。

### 可选的本地化输出（默认关闭）

按实测置信度取 Top N（默认 3，可配）个音源脚本，以**固定文件名**（默认
`lx-source-1.js` / `-2` / `-3`）覆盖写入容器内固定目录 `/app/outjs/`，并可定时或
在每轮实测后自动刷新。文件名只由名次决定，Top 榜换人时覆盖同名文件，因此
`/local/lx-source-1.js` 这类对外地址永远不变，适合为音源本地化提供一个稳定的
导入 URL。写入走原子替换，不会读到半截脚本。

Docker 部署时在 `docker-compose.yml` 里把宿主机/NAS 目录挂载到 `/app/outjs` 即可
自定义落盘位置：

```yaml
volumes:
  - ./data:/app/data
  - /vol1/docker/lxhub/outjs:/app/outjs
```

后台「配置管理 → 本地化输出」可开关、设置 TopN / 文件名模板 / 周期，查看已导出
文件与「立即导出」；命令行也可 `python main.py export-local`。详见
[配置说明](docs/configuration.md#本地化输出export)。

## 文档

| 文档 | 内容 |
|---|---|
| [快速开始](docs/getting-started.md) | 安装、首次爬取、启动服务、命令行 |
| [配置 GitHub Token](docs/github-token.md) | **建议优先看**——不配置会严重限制收录量 |
| [配置说明](docs/configuration.md) | `config.json` 全部字段与环境变量 |
| [脚本类型](docs/script-types.md) | 音源 / 配套脚本 / 存疑 的判定依据 |
| [加速前缀](docs/mirror-prefix.md) | 复制链接时自动拼接镜像前缀 |
| [部署方式](docs/deploy.md) | Docker Compose / Railway 自部署 |
| [工作原理](docs/how-it-works.md) | 发现 → 收录 → 判定 的完整流程 |
| [沙箱实测](docs/probe.md) | 可选的"真实取歌核验"（probe）：原理、配置、风险边界 |
| [API](docs/api.md) | 接口列表 |
| [常见问题](docs/faq.md) | 收录少、链接失效等问题 |
