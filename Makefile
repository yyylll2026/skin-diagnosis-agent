# =============================================================
# 皮肤诊断智能体 · 常用任务
#
# 用 .RECIPEPREFIX 把配方前缀从 Tab 改成 '>'，避免复制粘贴时
# 制表符被编辑器替换成空格导致 make 报 "missing separator"。
# =============================================================

.RECIPEPREFIX = >
.DEFAULT_GOAL := help

PY := backend/.venv/Scripts/python.exe
PY_UNIX := backend/.venv/bin/python

ifeq ($(OS),Windows_NT)
PYTHON := $(PY)
else
PYTHON := $(PY_UNIX)
endif

help: ## 显示所有可用目标
> @echo ""
> @echo "  皮肤诊断智能体 · 可用命令"
> @echo "  ----------------------------------------------------------"
> @grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
> @echo ""

venv: ## 创建后端虚拟环境并安装依赖
> python -m venv backend/.venv
> $(PYTHON) -m pip install --upgrade pip
> cd backend && ../$(PYTHON) -m pip install -e ".[dev]"

install: ## 安装后端依赖（已存在 venv 时用这个）
> cd backend && ../$(PYTHON) -m pip install -e ".[dev]"

models: ## 拉取所需的 Ollama 模型
> ollama pull qwen2.5vl:7b
> ollama pull bge-m3

ingest: ## 导入内置示例语料到知识库
> cd backend && ../$(PYTHON) -m scripts.ingest_cli ingest ./data/docs --category 皮肤疾病

reindex: ## 全量重建向量库（更换 embedding 模型后必须执行）
> cd backend && ../$(PYTHON) -m scripts.ingest_cli reindex

kb-search: ## 检索调试，用法：make kb-search Q="面部红斑伴鳞屑"
> cd backend && ../$(PYTHON) -m scripts.ingest_cli search "$(Q)"

dev: ## 启动后端开发服务器（热重载，端口 8000）
> cd backend && ../$(PYTHON) -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

fe-install: ## 安装前端依赖
> cd frontend && npm install

fe-dev: ## 启动前端开发服务器（端口 5173）
> cd frontend && npm run dev

fe-build: ## 构建前端产物
> cd frontend && npm run build

test: ## 运行后端测试
> cd backend && ../$(PYTHON) -m pytest -q

lint: ## 代码检查
> cd backend && ../$(PYTHON) -m ruff check app scripts tests

fmt: ## 代码格式化
> cd backend && ../$(PYTHON) -m ruff format app scripts tests

up: ## 容器编排启动（前后端）
> docker compose up -d --build

down: ## 停止并移除容器
> docker compose down

logs: ## 查看后端容器日志
> docker compose logs -f backend

clean: ## 清理构建缓存（保留数据与向量库）
> find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
> rm -rf backend/.pytest_cache backend/.ruff_cache backend/build 2>/dev/null || true
> @echo "已清理缓存（data/ 与向量库未受影响）"

.PHONY: help venv install models ingest reindex kb-search dev fe-install fe-dev fe-build test lint fmt up down logs clean
