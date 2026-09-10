# 皮肤诊断智能体 · 前端

基于 Vue 3 + Vite + Element Plus 的浅色系单页界面，对接本地 FastAPI 后端（`http://127.0.0.1:8000`），支持多模态皮肤图像分析、SSE 流式回答、结构化诊断卡片与知识库管理。

## 启动

```bash
npm install
npm run dev
```

启动后打开 <http://127.0.0.1:5173>。开发服务器已配置 `/api` 代理到 `http://127.0.0.1:8000`，请确保后端已先行启动。

## 构建与预览

```bash
npm run build
npm run preview
```

## Docker 部署

```bash
docker build -t skin-diagnosis-frontend .
docker run -p 8080:80 skin-diagnosis-frontend
```

镜像为多阶段构建（node:22-alpine 构建 → nginx:alpine 托管），`nginx.conf` 中已把 `/api` 反向代理到 `http://backend:8000`，并针对 SSE 关闭了代理缓冲。若后端容器名不是 `backend`，请修改 `nginx.conf` 中的 `proxy_pass`。

## 目录结构

```
src/
  api/index.js              # axios 普通请求 + fetch SSE 流式解析
  components/
    ChatPanel.vue           # 消息流、输入区、流式渲染、停止生成
    ImageUploader.vue       # 点击 / 拖拽 / 粘贴上传，转 base64
    DiagnosisCard.vue       # 结构化诊断结果卡片
    SourceList.vue          # 参考来源折叠列表
    KnowledgePanel.vue      # 知识库上传 / 列表 / 检索测试抽屉
  composables/useSessions.js # 会话状态（模块级 ref + localStorage）
  styles/theme.css          # 全局 CSS 变量、滚动条、Markdown 样式
  utils/markdown.js         # markdown-it 封装
```
