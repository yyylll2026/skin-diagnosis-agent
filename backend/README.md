# 皮肤诊断智能体 · 后端

基于 **Ollama（Qwen2.5-VL）+ LangGraph + Chroma + FastAPI** 的皮肤诊断智能体后端。

完整的系统设计说明见 [`../docs/系统设计文档.md`](../docs/系统设计文档.md)，
接口契约见 [`../docs/接口文档.md`](../docs/接口文档.md)。

## 快速开始

```bash
# 1. 准备模型（Ollama 需已启动）
ollama pull qwen2.5vl:7b     # 视觉 + 文本
ollama pull bge-m3           # 中文向量模型（知识库检索必需）

# 2. 安装依赖（Python ≥ 3.11）
python -m venv .venv
.venv/Scripts/activate        # Windows
# source .venv/bin/activate   # macOS / Linux
pip install -e ".[dev]"

# 3. 配置（首次运行建议从模板复制）
cp .env.example .env

# 4. 导入内置示例语料
python -m scripts.ingest_cli ingest ./data/docs --category 皮肤疾病

# 5. 启动
uvicorn app.main:app --reload --port 8000
```

启动后：

- 交互式 API 文档：http://127.0.0.1:8000/docs
- 健康检查：http://127.0.0.1:8000/api/v1/system/health

## 目录结构

```
backend/
├── app/
│   ├── main.py              # FastAPI 入口（横切关注点：日志/CORS/异常/生命周期）
│   ├── config.py            # pydantic-settings 配置中心
│   ├── dependencies.py      # 单例装配（DI）
│   ├── core/                # prompts（提示词资产）/ parsing（宽容解析）/ errors
│   ├── schemas/             # Pydantic 数据契约（= 接口契约）
│   ├── agent/               # LangGraph：state / nodes / graph / runner
│   ├── services/            # 业务能力：视觉 / 入库 / 检索 / 融合 / 安全 / Ollama 适配
│   ├── repositories/        # 数据访问：Chroma 向量库 / SQLite 元数据
│   └── api/v1/              # 路由：chat / knowledge / system
├── scripts/ingest_cli.py    # 知识库运维 CLI
├── tests/                   # pytest（不依赖 Ollama 的纯逻辑测试）
└── data/                    # docs（语料）/ chroma（向量）/ uploads / app.db
```

## 常用命令

```bash
# 知识库
python -m scripts.ingest_cli health
python -m scripts.ingest_cli ingest ./data/docs --category 皮肤疾病
python -m scripts.ingest_cli list
python -m scripts.ingest_cli search "面部红斑伴鳞屑如何鉴别"
python -m scripts.ingest_cli reindex          # 更换 embedding 模型后必须执行

# 测试与检查
pytest -q
ruff check app scripts tests
ruff format app scripts tests
```

## 关键配置

| 变量 | 说明 | 默认 |
|------|------|------|
| `SKIN_VISION_MODEL` | 视觉模型 | `qwen2.5vl:7b` |
| `SKIN_TEXT_MODEL` | 文本生成模型 | `qwen2.5vl:7b` |
| `SKIN_EMBEDDING_BACKEND` | `ollama` / `hf` / `chroma_default` | `ollama` |
| `SKIN_EMBEDDING_MODEL` | 向量模型 | `bge-m3` |
| `SKIN_RERANK_ENABLED` | LLM 重排开关（增延迟换精度） | `false` |
| `SKIN_RERANK_TOP_N` | 注入提示词的资料条数 | `4` |

> ⚠️ 更换 `SKIN_EMBEDDING_MODEL` 后**必须重建向量库**（`reindex`）：
> Chroma 集合的向量维度在首次创建时固定，不重建会直接报维度不匹配。

## 免责声明

本系统仅用于健康科普与初步参考，**不构成医疗诊断**，不能替代执业医师的面诊、
检查与诊断。请勿据此自行用药。
