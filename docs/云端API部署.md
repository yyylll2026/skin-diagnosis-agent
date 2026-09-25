# 云端 VL API 部署（免本地 Ollama / 小内存免费档）

把模型调用从本机 Ollama 换成 **OpenAI 兼容云端 API**（默认阿里云百炼），
后端与前端可部署到 512MB 内存的免费容器（Render / Fly.io 等）。

> ⚠️ 图片与文本会上传到第三方推理服务，与「数据不出内网」的本地方案前提冲突。
> 真实患者数据请勿走本方案。系统免责声明与就医提示机制仍会生效。

## 架构变化

| 组件 | 本地 Ollama | 云端 VL API |
|------|-------------|-------------|
| 视觉 / 文本 / 轻量路由 | Ollama `qwen2.5vl` | 百炼 `qwen-vl-plus` / `qwen-plus` / `qwen-turbo` |
| Embedding | Ollama `bge-m3` | 百炼 `text-embedding-v3`（或 `chroma_default`） |
| 编排 / 检索 / 安全 | 不变 | 不变 |
| 内存需求 | ≥4～8GB | **≈512MB～1GB** |
| 镜像 | `Dockerfile`（含 Ollama） | **`Dockerfile.cloud`（无 Ollama）** |

开关在 `SKIN_LLM_BACKEND`：

- `ollama`（默认）：与原先一致
- `cloud`：走 `SKIN_CLOUD_API_BASE` + API Key

## 一、准备 API Key

1. 打开 [阿里云百炼](https://bailian.console.aliyun.com/) 开通并创建 API Key
2. 新账号通常有免费额度；用完需按量计费
3. 兼容其它 OpenAI 端点时，改 `SKIN_CLOUD_API_BASE` 即可（如讯飞、本地 vLLM 网关）

## 二、本地先跑通 cloud 模式

```powershell
cd backend
# .env 中设置：
#   SKIN_LLM_BACKEND=cloud
#   SKIN_CLOUD_API_KEY=sk-...
#   SKIN_EMBEDDING_BACKEND=cloud   # 或 chroma_default 先冒烟
#   SKIN_AUTH_ENABLED=false        # 本地调试

uvicorn app.main:app --reload --port 8000
```

健康检查应看到 `llm_backend=cloud`、`ollama_connected=true`（表示网关可达）。

知识库若此前用 Ollama/`bge-m3` 建过索引，**换 cloud embedding 后必须 reindex**：

```bash
python -m scripts.ingest_cli reindex
```

## 三、部署到 Render（推荐入门）

1. 把仓库推到 GitHub
2. Render → New + → Web Service → 连接仓库
3. Runtime 选 **Docker**，Dockerfile 填 `Dockerfile.cloud`
4. 环境变量（Secrets）：
   - `DASHSCOPE_API_KEY` = 你的 Key  
   - `SKIN_SECRET_KEY` = 随机长字符串  
   - （`render.yaml` 已带 `SKIN_AUTH_*` 默认值）
5. 免费实例约 512MB；首次部署后打开  
   `https://<your-service>.onrender.com/api/v1/system/health`

仓库根目录 `render.yaml` 可用于 Blueprint / 作为配置参考。

### Render 注意

- 免费实例**闲置会休眠**，冷启动约 30～60 秒
- 磁盘多为临时；重启后知识库若变空，需重新上传或 `SKIN_AUTO_SEED=1` 灌示例语料
- 已开 `SKIN_AUTH_COOKIE_SECURE=true`（HTTPS）

## 四、部署到 Fly.io

```bash
fly launch --copy-config --no-deploy   # 使用仓库内 fly.toml
fly secrets set DASHSCOPE_API_KEY=sk-... SKIN_SECRET_KEY=...
fly deploy
```

## 五、环境变量速查

| 变量 | 云端推荐值 | 说明 |
|------|------------|------|
| `SKIN_LLM_BACKEND` | `cloud` | 总开关 |
| `SKIN_CLOUD_API_BASE` | 百炼 compatible-mode | 任意 OpenAI 兼容基址 |
| `DASHSCOPE_API_KEY` / `SKIN_CLOUD_API_KEY` | Key | 二选一，前者更通用 |
| `SKIN_VISION_MODEL` | `qwen-vl-plus` | 视觉 |
| `SKIN_TEXT_MODEL` | `qwen-plus` | 生成 |
| `SKIN_FAST_MODEL` | `qwen-turbo` | 意图路由 / 重写 |
| `SKIN_EMBEDDING_BACKEND` | `cloud` | 或 `chroma_default` |
| `SKIN_CLOUD_EMBEDDING_MODEL` | `text-embedding-v3` | 云端向量 |
| `SKIN_AUTH_ENABLED` | `true` | 公网建议开 |
| `SKIN_AUTH_COOKIE_SECURE` | `true` | HTTPS 必须 |
| `SKIN_SECRET_KEY` | 随机串 | 会话相关 |
| `PORT` | 平台注入 | 默认 7860 |

本地模型名（如 `qwen2.5vl:7b`）在 `llm_backend=cloud` 时会**自动映射**成上表云端名，避免忘改配置发给百炼导致 400。

## 六、排错

| 现象 | 原因 | 处理 |
|------|------|------|
| `云端模型未配置 API Key` | 缺 Key | 设置 `DASHSCOPE_API_KEY` |
| `云端模型返回 401` | Key 无效/欠费 | 控制台检查额度与 Key |
| `云端模型返回 400` + response_format | 网关不认 json_schema | 已有三级降级；仍失败可查模型是否支持 JSON 模式 |
| 知识库检索变差 | embedding 换型未重建索引 | `python -m scripts.ingest_cli reindex` |
| 登录后立刻掉线 | HTTPS 却 `COOKIE_SECURE=false` | 设 `SKIN_AUTH_COOKIE_SECURE=true` |
| Render 休眠后首开很慢 | 免费实例冷启动 | 正常；或升付费/关自动休眠 |
| 用错镜像仍去连 Ollama | 用了根目录 `Dockerfile` | 云端请用 **`Dockerfile.cloud`** |

## 七、与魔搭方案怎么选

| | 魔搭 + `Dockerfile` | 云端 API + `Dockerfile.cloud` |
|--|---------------------|-------------------------------|
| 模型位置 | 容器内 Ollama | 百炼等第三方 |
| 数据出境 | 图片在平台容器内 | 图片到模型厂商 |
| 内存 | 8～16GB | 512MB～1GB |
| 推理速度 | CPU 慢 | 通常更快 |
| 费用 | 免费配额 + 本地模型 | 模型 API 可能产生费用 |
| 适用 | 演示、尽量本地化 | 快速公网 Demo、小内存 PaaS |
