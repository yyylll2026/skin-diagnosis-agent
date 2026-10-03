# ---------- 构建阶段 ----------
FROM python:3.11-slim AS builder

WORKDIR /build

# 只复制依赖描述，利用 Docker 层缓存：改代码不会重新装依赖
COPY pyproject.toml README.md ./

# 构建期需要编译工具，装完即弃（多阶段构建的意义）
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --prefix=/install .

# ---------- 运行阶段 ----------
FROM python:3.11-slim

WORKDIR /app

# curl 用于 HEALTHCHECK；其余依赖已在 builder 中装好
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

COPY app ./app
COPY scripts ./scripts
# Alembic 迁移：启动时由 app/repositories/database.py 自动 upgrade 到 head，
# 漏拷的表现是启动直接 FileNotFoundError，且只在容器里复现。
# 两个都拷：alembic/ 是必需的，alembic.ini 只在容器内手跑 CLI 时用得上。
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini

# 数据目录挂载点（向量库 / 语料 / 上传文件 / SQLite）
RUN mkdir -p /app/data/docs /app/data/chroma /app/data/uploads

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SKIN_OLLAMA_BASE_URL=http://host.docker.internal:11434 \
    SKIN_CHROMA_DIR=/app/data/chroma \
    SKIN_SQLITE_PATH=/app/data/app.db

EXPOSE 8000

# 探活走健康检查接口：即使 Ollama 未就绪也会返回 200（degraded），
# 因此这里只校验「Web 服务本身是否起来」
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/v1/system/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
