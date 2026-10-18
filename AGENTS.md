# AGENTS.md

皮肤诊断智能体：`backend/`（FastAPI + LangGraph + Chroma）+ `frontend/`（Vue 3 + Vite）。设计见 `docs/系统设计文档.md`，接口契约见 `docs/接口文档.md`，**分层精读与文件地图见 `docs/代码精读指南.md`**（动大块代码前先读它）。

## 常用命令

先在 `backend/` 建好 venv（`python -m venv .venv` 后 `pip install -e ".[dev]"`）。以下都在 `backend/` 下、venv 已激活时执行：

```bash
pytest -q                                                # ~253 用例，35~60s，不连 Ollama
pytest tests/test_safety.py::test_risk_level_is_never_downgraded -q   # 跑单个用例
ruff check app scripts tests                             # 改完必跑
ruff format app scripts tests
uvicorn app.main:app --reload --port 8000
python -m scripts.ingest_cli ingest ./data/docs -c 皮肤疾病
python -m scripts.ingest_cli reindex                     # 换 embedding 后必须执行
python -m scripts.ingest_cli search "面部红斑伴鳞屑如何鉴别"  # 检索调试，不调 LLM
```

- 根目录 `Makefile` 是同一批命令的封装（`make test/lint/fmt/dev/fe-dev`），**但本机 PATH 里没有 `make`**，直接用上面的命令。Makefile 期望 venv 在 `backend/.venv`，且用 `.RECIPEPREFIX = >`（不是 Tab）。
- **CLI 必须在 `backend/` 下用 `python -m scripts.ingest_cli`**：`scripts/` 不是被安装的包，装好 venv 后的 `skin-kb` 入口在仓库根目录直接 `ModuleNotFoundError`。
- 前端只有 `npm run dev / build / preview`，**没有 lint 和测试**：改前端的验证手段就是 `npm run build`。
- **有 GitHub Actions CI**（`.github/workflows/ci.yml`，push/PR 到 main/master 触发）：后端 `ruff check` + `pytest -q`，前端 `npm ci && npm run build`，但**没有 pre-commit**。本地自检顺序与 CI 相同：`ruff check` → `pytest`（动了前端再加 `npm run build`）。

## 测试

- 单测**不依赖 Ollama**，也刻意不进 lifespan——用 `TestClient(create_app())` 而非 `with TestClient(app)`，否则预热会去建 Chroma 集合、编译图、调模型，又慢又脆。
- `tests/conftest.py` 在 `import app` **之前**用 `setdefault` 把 `SKIN_SQLITE_PATH` / `SKIN_CHROMA_DIR` 指到临时目录、`SKIN_EMBEDDING_BACKEND=chroma_default`、`SKIN_AUTH_ENABLED=false`。新增夹具保持这个顺序，否则会污染 `backend/data/`，或被本机 `.env` 里的鉴权配置打成一片 401。
- 需要特定配置的用例（如开鉴权）自己走「设环境变量 → 清 `get_settings()` 缓存 → `create_app()`」，见 `tests/test_auth_api.py`。
- pytest 配置在 `backend/pyproject.toml`：`asyncio_mode = auto`、`testpaths = ["tests"]`；ruff 配置也在那里（line-length 100，`E501`/`B008` 已忽略）。
- 模型效果评估另建带标注集的流程，**不要塞进 pytest**。

## 架构要点（改代码前必读）

- 依赖方向单向：`api/v1 → agent → services → repositories`（services 不得反向 import api/agent）。`app/dependencies.py` 是唯一装配点（服务单例、`build_nodes`、`warmup`），`app/main.py` 只做横切关注点。
- 图拓扑（`app/agent/graph.py`，改边前必读文件头的 ASCII 图）：`preprocess → route` 后分三支 `vision_analyze` / `check_completeness` / `generate`，最终都汇入 `safety_review`。
  - **融合约束**：`vision_analyze` 之后必须走 `build_query`，不能直跳 `generate`。
  - **知识问答必过 `check_completeness`**：信息不足转 `clarify` 追问，进度落 SQLite（图本身不用 checkpointer，重启不丢）。
  - 闲聊可直达 `generate`，检索可被跳过，但**所有出口都要过 `safety_review`**。
- **安全不变量**（`app/services/safety_service.py`）：风险等级只上调不下调；免责声明由代码幂等追加，不依赖模型。改诊断/生成路径时别破坏这两条（`tests/test_safety.py` 锁住）。
- **静态托管顺序**：`StaticFiles` 挂 `/` 必须在 API 路由**之后**注册，否则吞掉 API；`/service-info` 又必须在 mount **之前**注册。回归测试 `tests/test_api.py`。
- **模型后端开关**：`SKIN_LLM_BACKEND=ollama|cloud`（`app/config.py` + `services/model_gateway.py` + `llm_factory.py`）。业务编排只依赖工厂与网关接口，**不要在节点里直接 new 客户端**。云端见 `docs/云端API部署.md`。
- 配置集中在 `app/config.py`：前缀 `SKIN_`；路径由 `BACKEND_DIR = Path(__file__).parents[1]` 推导，**源码必须待在 `backend/app/`**——根目录两个 Dockerfile 因此用 `pip install -e .`，换成普通安装会让数据目录跑进 site-packages。
- `cors_origins` 等 list 字段用了 `NoDecode`，接受逗号分隔字符串，不要改回默认 JSON 解析。
- **数据层全是显式 SQL，不引 ORM**；方言差异（SQLite `MIN(a,b)` vs PG `LEAST`、`LIKE` vs `ILIKE`）集中在 `app/repositories/db.py`，新增分支写在那里并测试。连接与事务收敛在 `database.py`。
- **建表/改表走 Alembic，运行时不执行 DDL**：`MetadataStore.__init__` 会自动跑 `alembic upgrade head`（默认 SQLite 场景也会）。改任何列都要**新增迁移文件到 `backend/alembic/versions/`**，别在代码里手写 `CREATE/ALTER TABLE`；SQLite 下迁移走 batch 模式（`alembic/env.py` 已开 `render_as_batch`）。切 PostgreSQL 时连接串必须带 `+psycopg` 后缀（装的是 psycopg 3）。
- `alembic.ini` 必须保持**纯 ASCII**：Alembic 用系统 locale 编码读 ini，Windows 上非 ASCII 注释会直接 `UnicodeDecodeError`（连接串改由 `SKIN_DATABASE_URL` 注入）。ruff isort 也把 `alembic` 声明为第三方包（迁移目录无 `__init__.py`），改 ruff 配置时别删。

## 鉴权

- 代码默认 `auth_enabled=false`：匿名请求视为「合成管理员」并跳过会话归属校验——开关只决定「是否校验身份」，不改变任何接口的业务语义。
- `backend/.env.example` 里写的是 `SKIN_AUTH_ENABLED=true`：照 README `cp .env.example .env` 会直接打开鉴权，之后不登录全接口 401（测试不受影响，conftest 已强制关闭）。
- 注册接口把角色硬编码为 `customer`；管理员只能用 `python -m scripts.user_cli create-admin` 创建，**别给自助注册放开 admin**。
- 本地 http 必须 `SKIN_AUTH_COOKIE_SECURE=false`，公网 HTTPS 必须 `true`（否则表现为「登录成功，下一次请求又未登录」）。

## 知识库与 Embedding

- 默认 `bge-m3`（Ollama）。**更换 `SKIN_EMBEDDING_MODEL` 后必须 `python -m scripts.ingest_cli reindex`**：Chroma 集合维度在首次创建时固定。
- `backend/data/{chroma,uploads,app.db}` 都在 `.gitignore` 里，新克隆的知识库是空的，先 `ingest` 再谈检索效果。
- 批量导入用 CLI，不要只靠 HTTP 上传；检索调试用 `search`（不调 LLM），比开前端抽屉快。

## 部署（三套镜像，勿混用）

| 文件 | 用途 |
|------|------|
| `backend/Dockerfile` + `docker-compose.yml` | 多容器：API :8000、前端 :8080（Nginx）；Ollama 在宿主机 `host.docker.internal:11434` |
| 根目录 `Dockerfile` + `ms_deploy.json` + `deploy/entrypoint.sh` | 单容器云部署（魔搭）：Ollama+API+前端静态资源，固定端口 **7860**，数据挂 `/mnt/workspace` |
| `Dockerfile.cloud` + `fly.toml` / `render.yaml` | 不跑 Ollama，`SKIN_LLM_BACKEND=cloud` 走云端 API；Fly/Render 免费档，端口由 `PORT`（默认 7860）决定 |

`backend/web/` 是前端构建产物的拷贝（容器构建或本地单容器验证时生成），**不要提交**。

## 明确不要做的事

- 不要假设前端有 ESLint/Vitest。
- 不要改 embedding 后不 reindex。
- 不要在未改 `safety_service` 语义的情况下「优化掉」风险只升不降或免责声明。
- 不要依赖 `backend/.env`（本地私有，不入库）；对照 `.env.example` 写文档或改默认值。
- 不要在单测里进 lifespan 或连真实 Ollama；生产默认 `qwen2.5vl:7b` + `bge-m3`。
- 不要提交 `.workbuddy/`、`.workbuddy-ai/`（本机会话工作状态）；`_credtest.txt`、`server.log`、`*.err` 等临时/日志残留 `.gitignore` 已忽略，新加临时文件照常入库前先清理。
