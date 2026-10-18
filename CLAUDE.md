# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目指令：见 `AGENTS.md`

本仓库的完整项目指令集中在 **[`AGENTS.md`](AGENTS.md)**，Claude Code 每次会话会自动加载它——常用命令（pytest / ruff / ingest_cli / 前端构建）、架构要点（依赖方向、LangGraph 图拓扑、安全不变量、静态托管顺序）、测试与夹具约定、鉴权、知识库与 Embedding、三套部署镜像、明确禁止事项，全部以 `AGENTS.md` 为准。本文件不重复其内容，避免两处说明漂移；修改开发流程或架构约定时，只改 `AGENTS.md`。

## 动手前必读的文档

- `docs/代码精读指南.md` — 分层精读与文件地图，**动大块代码前先读**。
- `docs/系统设计文档.md` — 架构、数据流、检索链路、融合方案、ADR。
- `docs/接口文档.md` — 接口契约、SSE 协议、错误码。
