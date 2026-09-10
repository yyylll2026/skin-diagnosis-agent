# 魔搭创空间部署指南

把本项目部署到[魔搭社区（ModelScope）创空间](https://modelscope.cn/)，Docker 类型，
免费资源配置 `platform/2v-cpu-16g-mem`（2 核 CPU / 16 GB 内存）。

容器内同时运行 **Ollama**（本地模型推理）与 **FastAPI**（API + 前端静态资源），
只对外暴露 `7860` 一个端口。

> 完整的方案对比与其他平台的做法，见 [`../docs/部署指南.md`](../docs/部署指南.md)。

---

## 一、部署前必须知道的三件事

**1. 需要实名认证。**
魔搭的 Docker 类型创空间只对完成实名认证的用户开放。需要先绑定阿里云账号并完成
云账号实名认证。这一步无法跳过，也无法由他人代办。

**2. 免费配额**有时间限制，且长时间无访问会被平台回收资源。适合演示与自用，
不适合当生产服务。具体额度以创空间控制台显示为准。

**3. 2 核 CPU 跑视觉模型是慢的。**
本镜像已默认降级为 `qwen2.5vl:3b`、图片长边压到 1024px，单张图推理预计
**30 秒 ~ 2 分钟**。这是免费 CPU 方案的固有代价，不是配置错误。
要更快只能上 GPU（xGPU 资源需要单独申请加入「xGPU 乐园」组织）。

---

## 二、部署步骤

### 步骤 1 · 确认三个配置文件都在仓库根目录

```
皮肤诊断-agent/
├── Dockerfile          ← 单容器镜像（Ollama + FastAPI + 前端）
├── ms_deploy.json      ← 创空间部署配置（sdk_type=docker, port=7860）
├── deploy/
│   └── entrypoint.sh   ← 启动编排脚本
├── backend/
└── frontend/
```

`ms_deploy.json` 内容：

```json
{
  "$schema": "https://modelscope.cn/api/v1/studios/deploy_schema.json",
  "sdk_type": "docker",
  "resource_configuration": "platform/2v-cpu-16g-mem",
  "port": 7860
}
```

> ⚠️ 端口必须是 `7860`。平台在容器内已占用 `8080`，改端口会导致服务无法被路由。

### 步骤 2 · 创建创空间

1. 打开 <https://modelscope.cn/studios>
2. 点击「创建创空间」→ 选择 **编程式创空间**
3. 用「快速部署并创建」模式：填写名称与描述，**上传整个项目文件夹**
4. SDK 类型确认是 **Docker**，资源选 `2v-cpu-16g-mem`
5. 点击「确认创建并部署」

若走 Git 推送：在创空间页面的命令行推送指引里拿到仓库地址，然后

```bash
git remote add modelscope <创空间页面上显示的仓库地址>
git add .
git commit -m "deploy: 皮肤诊断智能体"
git push modelscope master:master
```

> 默认分支是 `master`，且平台禁止强推。首次推送前请确认 `.gitignore` 生效，
> 不要把 `backend/.venv`、`frontend/node_modules`、`backend/data/chroma` 推上去
> （仓库里已配好忽略规则，正常不会有问题）。

### 步骤 3 · 观察构建与运行日志

首次部署的 Docker 构建约 **3~8 分钟**（要装 Ollama 与 Python 依赖、构建前端）。

构建完成后进入运行时，容器内部会按这个顺序启动：

| 阶段 | 日志位置 | 预期 |
|------|---------|------|
| 1. 启动 ollama serve | `/mnt/workspace/ollama.log` | 数秒内就绪 |
| 2. 拉起 FastAPI | 平台 Runtime Logs | 立即监听 7860，**前端此时已可访问** |
| 3. 后台下载模型 | `/mnt/workspace/prepare.log` | 首次约 **4.4 GB**，视带宽 5~30 分钟 |
| 4. 自动灌入示例语料 | 同上 | 首次约 1 分钟，入库 4 篇文档 |

**为什么要把「起服务」放在「下模型」前面？**
否则首次部署时端口要等 4GB 模型下完才可用，用户看到的是空白页。
现在前端立刻能打开，健康面板会如实显示「向量模型未就绪」，
等模型到位后自动转为正常——这正是把 `/system/health` 设计成诊断面板
而不是存活探针的用意。

### 步骤 4 · 验证

部署完成后访问创空间地址，页面应显示：

- 左侧栏底部状态灯为正常
- 健康面板 `status = ok`、`warnings = []`
- 知识库显示 4 份文档 / 58 个向量块（自动灌入的示例语料）

可以直接测：
- **文字问答**：「皮肤溃疡超过多久不愈合需要就医？」
- **图片识别**：上传一张皮肤照片，等 30 秒 ~ 2 分钟后出结构化诊断卡片

---

## 三、持久化：为什么模型不会每次重下

容器默认文件系统在重启时会被清空。因此镜像做了两处重定向：

| 内容 | 位置 | 说明 |
|------|------|------|
| Ollama 模型 | `/mnt/workspace/ollama-models` | 4.4 GB，重启后复用，不再重下 |
| 向量库 / 会话历史 / 上传文件 | `/mnt/workspace/data` | 通过 `SKIN_DATA_ROOT` 整体重定向 |

对应环境变量（已写死在镜像里，一般不需要改）：

```dockerfile
ENV OLLAMA_MODELS=/mnt/workspace/ollama-models
ENV SKIN_DATA_ROOT=/mnt/workspace/data
```

> 注意：平台在**转移或重命名创空间**时，`/mnt/workspace` 也可能丢失。
> 重要数据请另行导出。

---

## 四、可调环境变量

在创空间的「环境变量」设置里覆盖即可，无需重新构建镜像：

| 变量 | 默认 | 说明 |
|------|------|------|
| `SKIN_VISION_MODEL` | `qwen2.5vl:3b` | 换成 `qwen2.5vl:7b` 精度更高但 CPU 上会慢 3~5 倍 |
| `SKIN_TEXT_MODEL` | `qwen2.5vl:3b` | 纯文本生成模型 |
| `SKIN_EMBEDDING_MODEL` | `bge-m3` | 向量模型。**换模型后必须在界面或 CLI 执行 reindex** |
| `SKIN_AUTO_SEED` | `1` | 设为 `0` 关闭首次自动灌入示例语料 |
| `SKIN_IMAGE_MAX_SIDE` | `1024` | 图片长边上限，调小可提速 |
| `SKIN_RERANK_ENABLED` | `false` | 打开会明显增加响应时间，CPU 环境不建议 |
| `SKIN_NUM_CTX` | `8192` | 上下文长度，调小可省内存但可能截断检索资料 |

---

## 五、排错

| 现象 | 原因 | 处理 |
|------|------|------|
| 构建失败：`failed to fetch anonymous token ... dial tcp ... registry.docker.io` | 国内网络无法访问 Docker Hub | 换用可达的镜像源。已实测**魔搭官方 registry 可用**：`modelscope-registry.cn-beijing.cr.aliyuncs.com/modelscope-repo/python:3.10`（`docker.1panel.live`、`docker.m.daocloud.io`、`hub-mirror.c.163.com` 实测均不可用或返回 403）。注意该基础镜像是 Python 3.10，本项目要求 ≥3.11，需相应调整或自备 3.11 镜像 |
| 构建失败：`pip install` 报错 | 依赖源或网络问题 | 看 Build Logs 具体行；必要时在 Dockerfile 里指定国内 PyPI 镜像 |
| 构建失败：找不到 `README.md` | `.dockerignore` 误排除了 `*.md` | 确认没有排除 `backend/README.md` |
| 运行时报 `bad interpreter: ...bash\r` | `entrypoint.sh` 被检出成 CRLF | 仓库已配 `.gitattributes` 强制 LF；或确认 Dockerfile 里的 `sed -i 's/\r$//'` 未被删除 |
| 页面打开但状态灯异常、`warnings` 提示模型未就绪 | 模型仍在下载 | 等 `prepare.log` 出现「准备阶段结束」；或到 Runtime Logs 确认 |
| 图片诊断很慢甚至超时 | 2 核 CPU 跑视觉模型的固有代价 | 换 `qwen2.5vl:3b`、降低 `SKIN_IMAGE_MAX_SIDE`、或申请 xGPU 资源 |
| 问答答不出内容、提示资料未涵盖 | 知识库为空 | 界面右上角知识库面板上传文档，或用内置语料重新 `reindex` |
| 重启后知识库变空 | 数据没落在持久化目录 | 确认 `SKIN_DATA_ROOT=/mnt/workspace/data` 生效 |
| 端口不通 | 服务没监听 `7860` | 平台固定转发 7860，且 8080 被平台占用，必须用 7860 |

---

## 六、这套部署方案的已知边界

- **不是生产方案。** 免费配额有时限、无 SLA、CPU 推理慢。定位是「把本机跑的
  Demo 搬到公网给人看」。
- **图片会上传到云端**，与本项目「数据不出内网」的原始设计前提相冲突。
  涉及真实患者数据时请勿使用此方案。
- **2 核 CPU 是硬瓶颈。** 并发请求会互相抢占，多人同时用体验会明显下降。
- **Ollama 在容器冷启动时需要重新加载模型到内存**（数秒到数十秒），
  这段时间的请求会变慢。镜像里已设置 `keep_alive` 减少反复加载。
