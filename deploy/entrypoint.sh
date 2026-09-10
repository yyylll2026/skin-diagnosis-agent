#!/usr/bin/env bash
# =============================================================================
# 单容器启动编排
#
# 云平台只给一个容器、一个公开端口，所以这里要在同一个容器里拉起两个进程：
#   ollama serve  —— 后台常驻，只监听 127.0.0.1（不对外暴露）
#   uvicorn       —— 前台常驻，监听 0.0.0.0:7860（平台唯一入口）
#
# 启动顺序是刻意设计的：**先起 API，再在后台拉模型**。
# 若反过来，首次部署时端口要等 4GB 模型下完才可用，用户看到的是空白页；
# 而先起 API 的话，前端立刻能打开，健康面板会如实显示「向量模型未就绪」，
# 等模型到位后自动转为正常——这正是当初把 /system/health 设计成诊断面板
# 而不是存活探针的意义。
# =============================================================================
set -euo pipefail

PERSIST_DIR="${SKIN_PERSIST_DIR:-/mnt/workspace}"
export OLLAMA_MODELS="${OLLAMA_MODELS:-${PERSIST_DIR}/ollama-models}"
export OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"
export PATH="/usr/local/bin:${PATH}"

PORT="${SKIN_PORT:-7860}"
VISION_MODEL="${SKIN_VISION_MODEL:-qwen2.5vl:3b}"
EMBED_MODEL="${SKIN_EMBEDDING_MODEL:-bge-m3}"

mkdir -p "${OLLAMA_MODELS}" "${SKIN_DATA_ROOT:-${PERSIST_DIR}/data}"

log() { echo "[entrypoint] $*"; }

log "=========================================================="
log "持久化目录 : ${PERSIST_DIR}"
log "模型目录   : ${OLLAMA_MODELS}"
log "数据目录   : ${SKIN_DATA_ROOT:-${PERSIST_DIR}/data}"
log "视觉模型   : ${VISION_MODEL}"
log "向量模型   : ${EMBED_MODEL}"
log "服务端口   : ${PORT}"
log "=========================================================="

# ---------------------------------------------------------------------------
# 1. 启动 Ollama
# ---------------------------------------------------------------------------
log "启动 ollama serve（日志：${PERSIST_DIR}/ollama.log）"
ollama serve > "${PERSIST_DIR}/ollama.log" 2>&1 &

OLLAMA_READY=0
for i in $(seq 1 90); do
    if curl -sf "http://127.0.0.1:11434/api/tags" > /dev/null 2>&1; then
        log "Ollama 就绪（耗时 ${i}s）"
        OLLAMA_READY=1
        break
    fi
    sleep 1
done
if [ "${OLLAMA_READY}" -ne 1 ]; then
    # 不直接退出：API 仍要起来，让健康面板把问题暴露给用户，而不是整个服务不可用
    log "警告：Ollama 90s 内未就绪，将继续启动 API（健康面板会显示异常）"
fi

# ---------------------------------------------------------------------------
# 2. 后台拉模型 + 首次自动灌入示例语料
# ---------------------------------------------------------------------------
prepare_models() {
    local all_ok=1
    for model in "${VISION_MODEL}" "${EMBED_MODEL}"; do
        # 已存在则立刻返回，不会重复下载（模型目录是持久化的）
        if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "${model}"; then
            log "模型已存在，跳过下载：${model}"
            continue
        fi
        log "开始下载模型：${model}（首次较慢，日志见 pull.log）"
        if ollama pull "${model}"; then
            log "模型下载完成：${model}"
        else
            log "模型下载失败：${model}"
            all_ok=0
        fi
    done

    if [ "${all_ok}" -ne 1 ]; then
        log "存在下载失败的模型，跳过自动灌库"
        return
    fi

    # 首次部署时知识库为空 → 自动把内置示例语料入库，让演示开箱可用。
    # 判断条件是「文档表为空」而不是「向量块为 0」，避免重复灌入。
    if [ "${SKIN_AUTO_SEED:-0}" = "1" ]; then
        local count
        count=$(python -c "
from app.config import get_settings
from app.repositories.db import MetadataStore
print(len(MetadataStore(get_settings().resolved_sqlite_path).list_documents()))
" 2>/dev/null || echo "-1")

        if [ "${count}" = "0" ]; then
            log "知识库为空，开始灌入内置示例语料"
            python -m scripts.ingest_cli ingest ./seed_docs --category 皮肤疾病 \
                || log "示例语料灌入失败（可在界面里手动上传文档）"
        else
            log "知识库已有 ${count} 份文档，跳过自动灌库"
        fi
    fi

    log "准备阶段结束，模型与语料就绪"
}
prepare_models > "${PERSIST_DIR}/prepare.log" 2>&1 &

# ---------------------------------------------------------------------------
# 3. 前台启动 API（唯一公开端口）
# ---------------------------------------------------------------------------
cleanup() {
    log "收到退出信号，正在停止子进程"
    kill "$(jobs -p)" 2>/dev/null || true
    exit 0
}
trap cleanup TERM INT

log "启动 FastAPI：0.0.0.0:${PORT}"
# 单 worker：本地模型推理本身就是串行的瓶颈，多 worker 只会争抢 CPU 与内存
uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" --workers 1 &
UVICORN_PID=$!

wait "${UVICORN_PID}"
