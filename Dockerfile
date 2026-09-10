# =============================================================================
# 单容器云部署镜像（魔搭创空间 / 任意单端口 PaaS 通用）
#
# 与 backend/Dockerfile 的区别：
#   backend/Dockerfile  → 只打包后端 API，配合 docker-compose 与独立前端使用
#   本文件              → 把 Ollama + FastAPI + 前端静态资源打进**一个容器**，
#                        只暴露一个端口（7860），适配只转发单一入口的云平台
#
# 云平台普遍要求：单容器、单公开端口、进程常驻。因此这里做三件事：
#   1. 容器内同时运行 ollama serve 与 uvicorn（entrypoint.sh 负责编排）
#   2. 前端构建产物交给 FastAPI 同源托管，前后端合并成同一入口
#   3. 模型与业务数据全部指向持久化目录，避免每次重启重下数 GB 模型
# =============================================================================

# ---------- 阶段 1：构建前端 ----------
FROM node:22-alpine AS web

WORKDIR /web

# 先只拷贝依赖清单，让 npm ci 这层能被缓存住
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build


# ---------- 阶段 2：运行时 ----------
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

# curl 用于探活与拉模型；ca-certificates 供 Ollama 访问模型仓库
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 官方安装脚本会判断发行版并解压到 /usr/local/bin/ollama
RUN curl -fsSL https://ollama.com/install.sh | sh

WORKDIR /app/backend

# 可编辑安装（-e）而不是普通安装，这一点很关键：
# config.py 用 __file__ 推导 BACKEND_DIR，普通安装后源码位于 site-packages，
# 会让 web 目录、data 目录等派生路径全部跑偏。可编辑安装保证源码仍在
# /app/backend，路径推导与本地开发行为完全一致。
COPY backend/pyproject.toml backend/README.md ./
COPY backend/app ./app
COPY backend/scripts ./scripts
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e .

# 前端产物：FastAPI 会把它挂到 "/"（见 app/main.py 的 resolved_web_dir 逻辑）
COPY --from=web /web/dist ./web

# 内置示例语料，首次启动自动入库用
COPY backend/data/docs ./seed_docs

COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
# sed 去掉可能存在的 CR：Windows 上编辑的 .sh 若带 CRLF，容器里会报
# "bad interpreter: /usr/bin/env bash\r" 这类极难排查的错误
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh

# ---------------------------------------------------------------------------
# 模型与数据都落到持久化挂载点。
# 不这么做的话，容器每次重启都要重新下载约 4.4GB 模型（3B 视觉 + 向量），
# 冷启动会变成十几分钟——这是云上跑本地模型最容易踩的坑。
# ---------------------------------------------------------------------------
ENV OLLAMA_MODELS=/mnt/workspace/ollama-models \
    OLLAMA_HOST=127.0.0.1:11434 \
    SKIN_DATA_ROOT=/mnt/workspace/data \
    SKIN_WEB_DIR=/app/backend/web \
    SKIN_OLLAMA_BASE_URL=http://127.0.0.1:11434 \
    SKIN_VISION_MODEL=qwen2.5vl:3b \
    SKIN_TEXT_MODEL=qwen2.5vl:3b \
    SKIN_FAST_MODEL=qwen2.5vl:3b \
    SKIN_EMBEDDING_BACKEND=ollama \
    SKIN_EMBEDDING_MODEL=bge-m3 \
    SKIN_MAX_IMAGE_MB=8 \
    SKIN_IMAGE_MAX_SIDE=1024 \
    SKIN_AUTO_SEED=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 免费档是 2 核 CPU，视觉模型只能靠 CPU 推理；1024 长边比 1280 明显更快，
# 且对形态学观察的影响可接受（这是本镜像主动做的性能取舍）

EXPOSE 7860

HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -fsS http://127.0.0.1:7860/api/v1/system/health || exit 1

CMD ["/usr/local/bin/entrypoint.sh"]
