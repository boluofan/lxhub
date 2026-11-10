# 必须用 bookworm-slim 而非 alpine：运行阶段是 python:3.12-slim（Debian/glibc），
# 下一阶段要从这里 COPY node 二进制过去（Alpine/musl 的 node 在 glibc 镜像里跑不起来）
FROM node:24-bookworm-slim AS webui

WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim

WORKDIR /app
# 本地化输出目录：容器内固定为 /app/outjs，自部署时用 compose 把宿主机/NAS
# 目录挂载到这个路径即可自定义落盘位置（见 docker-compose.yml）
RUN mkdir -p /app/outjs
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    apt-get update && apt-get install -y --no-install-recommends curl libstdc++6 libatomic1 && \
    rm -rf /var/lib/apt/lists/*

# 沙箱实测（probe）需要 Node ≥20 运行时：从构建阶段同源拷贝 node 二进制。
# libstdc++6/libatomic1 是官方 node 二进制的运行依赖（arm64 尤其需要 libatomic1）。
# 与构建阶段同源还保证「前端构建用什么版本，运行时就是同一个版本」，无版本漂移。
COPY --from=webui /usr/local/bin/node /usr/local/bin/node

COPY lxhub ./lxhub
COPY main.py config.json ./
COPY --from=webui /web/dist ./frontend/dist

ENV LXH_DB=/app/data/lxhub.db
VOLUME ["/app/data"]
EXPOSE 8787

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -f http://localhost:8787/healthz || exit 1

# 端口交给环境变量（LXH_PORT / PORT，默认 8787），便于 Railway 等平台注入 PORT
CMD ["python", "main.py", "serve", "--host", "0.0.0.0"]
