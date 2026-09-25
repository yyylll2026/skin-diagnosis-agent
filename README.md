# 皮肤诊断智能体 · Skin Diagnosis Agent

> 基于 **Ollama（Qwen2.5-VL）+ LangGraph + Chroma + FastAPI + Vue 3** 的本地化皮肤诊断辅助系统。
> 支持**知识库问答**与**图片识别**，并把两者**融合**成有据可依的回答。

⚠️ **仅用于健康科普与初步参考，不构成医疗诊断**，不能替代执业医师的面诊、检查与诊断。

---

## 这个系统解决什么问题

普通用户面对皮肤问题时的三处痛点：**看不懂**（不认识的皮损形态）、
**判断不了轻重**（不知道该不该去医院）、**找不到可信答案**（伪科普与营销内容混杂）。

系统的处理链路是：

```
皮肤照片 ─► Qwen2.5-VL 结构化观察 ─► 转成医学检索意图 ─► Chroma 检索皮肤科知识库
                                                              │
                    回答（观察 / 方向 / 依据 / 留意 / 建议）◄──┘
```

关键点在于**不是两个功能的并列，而是两阶段串联**：
视觉模型把「图像空间」映射到「医学语义空间」（产出疾病名与形态学术语），
这恰好是向量检索最擅长匹配的东西；检索到的资料再反向约束生成措辞，
让回答从「自由的视觉描述」收敛到「有据可依的科普」。

## 核心特性

| 特性 | 说明 |
|------|------|
| 🔍 混合检索 | 向量召回（跨用词差异）+ BM25 召回（专有名词精确命中）→ RRF 融合 → 可选 LLM 重排 |
| 🖼️ 结构化视觉诊断 | `format` 传 JSON Schema 做语法约束，三级降级保证任何模型都能跑 |
| 🔗 图文融合 | 视觉结论驱动检索，知识库依据反哺生成，而非两套独立逻辑 |
| 🛡️ 三层安全防线 | 提示词约束 + 结构约束 + **22 条确定性规则兜底**（风险等级只上调不下调） |
| ⚡ 流式体验 | 文本逐 token 输出 + 节点级进度提示（「正在分析图像…」「正在检索知识库…」） |
| 🧩 全局降级 | Ollama 掉线、模型吐脏数据、知识库为空、图片格式非法，都有可读的降级结果 |
| 🔒 纯本地 | 全部推理在本地完成，图片与对话不出内网 |

## 架构一览

```
Vue 3 + Element Plus
        │ HTTP / SSE
FastAPI（chat · diagnosis · knowledge · system）
        │ AgentState
LangGraph：preprocess → route ─┬─► vision_analyze ─► build_query ─┐
                               ├─► build_query ────────────────────┤
                               └─► generate ◄──────── retrieve ◄───┘
                                        └─► safety_review
        │
Services：Vision · Ingest · Retrieval · Fusion · Safety
        │
Chroma（向量）· SQLite（元数据）· Ollama（qwen2.5vl:7b · bge-m3）
```

完整的架构说明、接口契约、链路策略与设计决策，见：

- 📐 **[系统设计文档](docs/系统设计文档.md)** — 架构、数据流、检索链路、融合方案、ADR、部署
- 🔌 **[接口文档](docs/接口文档.md)** — 全部接口定义、SSE 协议、数据模型、错误码、接入示例
- ☁️ **[云端API部署](docs/云端API部署.md)** — 不跑 Ollama，用百炼等 OpenAI 兼容 API 上免费 PaaS
- 🐳 **[魔搭创空间](deploy/README.md)** — 容器内 Ollama 单容器部署

## 快速开始

### 前置条件

- Python ≥ 3.11
- Node ≥ 18
- [Ollama](https://ollama.com/) 已安装并运行  
  **或** 改用云端 VL API（见 [`docs/云端API部署.md`](docs/云端API部署.md)，`SKIN_LLM_BACKEND=cloud`，无需本机模型）

### 1. 准备模型

```bash
ollama serve                          # 若尚未运行

ollama pull qwen2.5vl:7b              # 视觉 + 文本（约 6GB）
ollama pull bge-m3                    # 中文向量模型（约 1.2GB，知识库检索必需）

# 显存紧张时可用 3b 版本（速度更快，精度略降）
# ollama pull qwen2.5vl:3b
```

### 2. 启动后端

```bash
cd backend

python -m venv .venv
.venv/Scripts/activate                # Windows
# source .venv/bin/activate           # macOS / Linux

pip install -e ".[dev]"
cp .env.example .env                  # 按需修改

# 导入内置示例语料（4 篇皮肤科科普文档）
python -m scripts.ingest_cli ingest ./data/docs --category 皮肤疾病

# 启动
uvicorn app.main:app --reload --port 8000
```

后端就绪检查：<http://127.0.0.1:8000/api/v1/system/health>
若返回 `degraded`，按 `warnings` 里的提示操作（通常是缺模型）。

### 3. 启动前端

```bash
cd frontend
npm install
npm run dev
```

打开 <http://localhost:5173> 即可使用。

> 前端已配置 `/api` 代理到 `127.0.0.1:8000`，开发期同源，无需处理跨域。

### 4. 容器化

**本机 / 自有服务器**（多容器，Ollama 跑在宿主机以便直接用 GPU）：

```bash
docker compose up -d --build
# 前端 http://localhost:8080 ｜ 后端 http://localhost:8000/docs
```

**部署到云端**（单容器：Ollama + FastAPI + 前端打包在一起，只暴露一个端口）：

```bash
docker build -t skin-diagnosis-agent .
docker run -p 7860:7860 -v skin-data:/mnt/workspace skin-diagnosis-agent
# 打开 http://localhost:7860 即为完整应用
```

根目录的 `Dockerfile` + `ms_deploy.json` 是**为免费云平台准备的**——
适配魔搭创空间这类只转发单一入口的平台：前后端合并成一个容器、
模型与数据落到持久化卷、单端口 `7860` 对外。

- 平台选择、免费额度对比与排错 → [部署指南](docs/部署指南.md)
- 魔搭创空间的逐步操作 → [deploy/README.md](deploy/README.md)

> ⚠️ 部署前请读一遍部署指南第六节：公网部署会让皮肤照片离开本机，
> 与本项目「数据不出内网」的原始定位冲突；当前也尚未做鉴权与限流。

## 使用方式

### 界面

- **文字问答** — 直接提问，如「皮肤溃疡超过多久不愈合需要就医？」
- **图片识别** — 拖拽或粘贴皮肤照片，可补充描述，得到结构化诊断卡片
- **知识库管理** — 右上角抽屉：上传文档、查看列表、检索测试

### 命令行

```bash
cd backend

python -m scripts.ingest_cli health                       # 检查模型就绪情况
python -m scripts.ingest_cli ingest ./data/docs -c 皮肤疾病 # 批量导入
python -m scripts.ingest_cli search "面部红斑伴鳞屑如何鉴别" # 检索调试（不调 LLM）
python -m scripts.ingest_cli list                         # 已入库文档
python -m scripts.ingest_cli reindex                      # 更换 embedding 模型后必须执行
```

### 用 Makefile

```bash
make help          # 查看所有命令
make venv          # 建环境装依赖
make models        # 拉取模型
make ingest        # 导入示例语料
make dev           # 启动后端
make fe-dev        # 启动前端
make test          # 跑测试
```

## 重要提示

1. **`bge-m3` 必须先拉取**。否则知识库无法检索，`/system/health` 会给出 `degraded` 与修复命令。
   想零配置先跑通，可临时把 `.env` 里 `SKIN_EMBEDDING_BACKEND` 改为 `chroma_default`（中文效果一般）。
2. **更换 embedding 模型后必须 `reindex`**。Chroma 集合的向量维度在首次创建时固定，
   不重建会直接抛维度不匹配错误。
3. **视觉分析需要 GPU 才实用**。7B 模型在 CPU 上单张图可能要 1~3 分钟；
   显存不足时切到 `qwen2.5vl:3b`。
4. **首次请求会较慢**。模型加载需要几十秒，服务启动时已做预热（Chroma 建集合 + 图编译），
   并设置了 `keep_alive=30m` 让模型常驻显存。

## 项目结构

```
skin-diagnosis-agent/
├── backend/            # FastAPI + LangGraph + Chroma
│   ├── app/            #   core / schemas / agent / services / repositories / api
│   ├── scripts/        #   知识库运维 CLI
│   ├── tests/          #   pytest（62+ 用例，不依赖 Ollama）
│   └── data/           #   语料 / 向量库 / 上传件 / SQLite
├── frontend/           # Vue 3 + Vite + Element Plus
├── docs/               # 系统设计文档 + 接口文档
├── docker-compose.yml
└── Makefile
```

## 测试与代码质量

```bash
cd backend

pytest -q                    # 单元测试（不依赖 Ollama，秒级完成）
pytest -q -v                 # 查看每个用例名
ruff check app scripts tests # 静态检查
ruff format app scripts tests
```

测试覆盖**解析容错**（模型吐脏 JSON）、**安全不变量**（风险等级单调性、
免责声明幂等）、**文本处理**（CJK 空格修复、分块元数据、jieba 领域词典）、
**RRF 数学性质**、**意图路由优先级**与**接口契约**。

模型效果评估应另建带标注集的流程，不混入单测——模型推理慢且不确定，
塞进单测只会让 CI 又慢又脆。

## 技术选型

| 场景 | 选择 | 备选 |
|------|------|------|
| 视觉 + 文本模型 | Ollama / `qwen2.5vl:7b` | `qwen2.5vl:3b`（更快）、`qwen3-vl:2b` |
| 中文向量模型 | Ollama / `bge-m3` | `BAAI/bge-small-zh-v1.5`、Chroma 内置 MiniLM |
| 编排 | LangGraph | 手写流程、LangChain AgentExecutor |
| 向量库 | Chroma（PersistentClient） | Milvus、Qdrant |
| 元数据 | SQLite（标准库） | PostgreSQL、SQLAlchemy |
| 后端 | FastAPI | Django REST Framework |
| 前端 | Vue 3 + Vite + Element Plus | React + Tailwind、Gradio |

## 免责声明

本系统由 AI 模型基于公开医学资料生成内容，**仅用于健康科普与初步参考**，
不构成医疗诊断，不能替代执业医师的面诊、检查与诊断。
请勿据此自行用药。若症状持续、加重或出现危险信号，请及时前往正规医疗机构就诊。

系统在识别到危险信号（如色素痣形态改变、皮损破溃不愈、迅速扩大、伴发热、
累及黏膜、呼吸困难等）时，会强制在回答中插入就医提示——**但这不能成为延迟就医的理由**。
