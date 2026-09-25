# AGENTS.md

皮肤诊断智能体：`backend/`（FastAPI + LangGraph + Chroma）+ `frontend/`（Vue 3 + Vite）。完整设计见 `docs/系统设计文档.md`，接口契约见 `docs/接口文档.md`。

## 常用命令

先在 `backend/` 建好 venv（`python -m venv .venv` 后 `pip install -e ".[dev]"`）。Makefile 期望 venv 在 `backend/.venv`。

```bash
# 优先用 Makefile（根目录）
make test          # pytest -q
make lint          # ruff check app scripts tests
make fmt           # ruff format app scripts tests
make dev           # uvicorn :8000
make fe-dev        # Vite :5173，/api 代理到 8000

# 等价（在 backend/ 下，使用已激活的 venv）
pytest -q
pytest tests/test_safety.py::test_risk_level_is_never_downgraded -q
ruff check app scripts tests
python -m scripts.ingest_cli ingest ./data/docs -c 皮肤疾病
python -m scripts.ingest_cli reindex   # 换 embedding 后必须执行
```

前端没有 lint/test 脚本，只有 `npm run dev` / `build` / `preview`。

Makefile 使用 `.RECIPEPREFIX = >`（不是 Tab）。在 Windows 上 `PYTHON` 指向 `backend/.venv/Scripts/python.exe`。

## 测试

- 单测**不依赖 Ollama**，秒级可跑完；不要把模型效果评估塞进 pytest。
- `tests/conftest.py` 在 `import app` **之前**把 `SKIN_SQLITE_PATH` / `SKIN_CHROMA_DIR` 指到临时目录，并把 `SKIN_EMBEDDING_BACKEND` 设为 `chroma_default`。若新增夹具，保持这一顺序，避免污染 `backend/data/`。
- `pytest.ini` 在 `backend/pyproject.toml`：`asyncio_mode = auto`，`testpaths = ["tests"]`。

## 架构要点（改代码前必读）

- 请求链路：`app/api/v1` → `app/agent`（`preprocess → route → … → generate → safety_review`）→ `app/services`。`main.py` 只做装配与横切关注点。
- **模型后端开关**：`SKIN_LLM_BACKEND=ollama|cloud`（`app/config.py` + `app/services/model_gateway.py` + `llm_factory.py`）。业务编排只依赖工厂与网关接口；不要在节点里直接 new 客户端。云端用 `Dockerfile.cloud` / `docs/云端API部署.md`。
- **融合约束**：`vision_analyze` 之后必须经过 `build_query`（见 `app/agent/graph.py`），不能从视觉节点直接跳到 `generate`。闲聊可直达 `generate`，检索可被跳过，但所有出口都要过 `safety_review`。
- **安全不变量**（`app/services/safety_service.py`）：风险等级只上调不下调；免责声明由代码幂等追加，不依赖模型。改诊断/生成路径时不要破坏这两条（有测试锁住）。
- **静态托管顺序**：`StaticFiles` 挂在 `/` 必须在 API 路由**之后**注册，否则会吞掉 API。`tests/test_api.py` 有回归测试。
- 配置集中在 `app/config.py`：环境变量前缀 `SKIN_`；路径从 `BACKEND_DIR = Path(__file__).parents[1]` 推导，因此 Docker 里必须 `pip install -e .`（普通安装会把源码挪进 site-packages，路径全跑偏）。
- `cors_origins` 等 list 字段用了 `NoDecode`，接受逗号分隔字符串，不要改回默认 JSON 解析。

## 知识库与 Embedding

- 默认 `bge-m3`（Ollama）。**更换 `SKIN_EMBEDDING_MODEL` 后必须 `python -m scripts.ingest_cli reindex`**：Chroma 集合维度在首次创建时固定。
- 批量导入用 CLI，不要只靠 HTTP 上传：`python -m scripts.ingest_cli ingest <dir> -c <分类>`。
- 检索调试用 CLI `search`（不调 LLM），比开前端抽屉快。

## 部署（两套 Dockerfile，勿混用）

| 文件 | 用途 |
|------|------|
| `backend/Dockerfile` + `docker-compose.yml` | 多容器：API :8000、前端 :8080；Ollama 在宿主机 `host.docker.internal:11434` |
| 根目录 `Dockerfile` + `ms_deploy.json` + `deploy/entrypoint.sh` | 单容器云部署（魔搭）：Ollama+API+前端静态资源，固定端口 **7860**，数据挂 `/mnt/workspace` |

`backend/web/` 是前端构建产物的拷贝（容器构建或本地单容器验证时生成），**不要提交**。

## 明确不要做的事

- 不要假设前端有 ESLint/Vitest。
- 不要改 embedding 后不 reindex。
- 不要在未改 `safety_service` 语义的情况下“优化掉”风险只升不降或免责声明。
- 不要依赖 `backend/.env`（本地私有）；对照 `.env.example` 写文档或改默认值。
- 生产路径上的模型默认 `qwen2.5vl:7b` + `bge-m3`；单测不要拉起真实 Ollama。
