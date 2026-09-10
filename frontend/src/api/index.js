import axios from 'axios'

/**
 * API 基址。
 * - 默认走相对路径 `/api/v1`：开发期由 Vite 代理转发，生产期由后端同源托管，
 *   两种情况都无需配置。
 * - 前后端分离部署时，用 `VITE_API_BASE=https://xxx/api/v1` 覆盖即可。
 */
const API_BASE = import.meta.env.VITE_API_BASE || '/api/v1'

/** 普通请求走 axios，统一 baseURL；流式请求单独用 fetch（见文件底部）。 */
const http = axios.create({
  baseURL: API_BASE,
  timeout: 60000,
})

/** 从 axios 错误里提取尽量可读的中文提示 */
function toReadableError(error) {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data
    let text = ''
    if (typeof detail === 'string') text = detail
    else if (detail && typeof detail === 'object') text = detail.detail || detail.message || ''
    if (text) return new Error(text)
    if (error.code === 'ECONNABORTED') return new Error('请求超时，请稍后重试')
    if (!error.response) return new Error('无法连接后端服务，请确认 FastAPI 已启动')
    return new Error(`请求失败（HTTP ${error.response.status}）`)
  }
  return error instanceof Error ? error : new Error(String(error))
}

async function request(promise) {
  try {
    const { data } = await promise
    return data
  } catch (error) {
    throw toReadableError(error)
  }
}

/* ------------------------------ 系统 ------------------------------ */

/** 健康检查：GET /api/v1/system/health */
export function getHealth() {
  return request(http.get('/system/health'))
}

/** 可用模型列表：GET /api/v1/system/models */
export function getModels() {
  return request(http.get('/system/models'))
}

/* ------------------------------ 知识库 ------------------------------ */

/** 文档列表：GET /api/v1/kb/documents */
export function listDocuments() {
  return request(http.get('/kb/documents'))
}

/**
 * 上传文档：POST /api/v1/kb/documents
 * multipart 字段名 file；分类通过查询参数 category 传递。
 */
export function uploadDocument(file, category) {
  const form = new FormData()
  form.append('file', file)
  return request(
    http.post('/kb/documents', form, {
      params: { category },
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 120000,
    })
  )
}

/** 删除文档：DELETE /api/v1/kb/documents/{docId} */
export function deleteDocument(docId) {
  return request(http.delete(`/kb/documents/${encodeURIComponent(docId)}`))
}

/** 检索：POST /api/v1/kb/search */
export function searchKnowledge(query, topK = 5) {
  return request(http.post('/kb/search', { query, top_k: topK }))
}

/* ------------------------------ 智能体流式对话 ------------------------------ */

function safeCall(fn, arg) {
  if (typeof fn === 'function') {
    try {
      fn(arg)
    } catch (err) {
      // 回调里抛错不应中断整条流的解析
    }
  }
}

function isAbortError(err) {
  return !!err && (err.name === 'AbortError' || err.code === 'ABORT_ERR')
}

function normalizeStreamError(err) {
  if (err instanceof Error) return err
  return new Error(String(err ?? '未知错误'))
}

/**
 * 流式对话：POST /api/v1/agent/chat
 *
 * @param {object} payload { session_id, message, image_base64, image_mime, use_knowledge }
 * @param {object} handlers { onMeta, onNode, onVision, onSources, onToken, onDone, onError }
 * @param {AbortSignal} [signal]
 */
export async function streamAgentChat(payload = {}, handlers = {}, signal) {
  const h = handlers || {}
  const fail = (err) => safeCall(h.onError, normalizeStreamError(err))

  let response
  try {
    response = await fetch(`${API_BASE}/agent/chat`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
      },
      body: JSON.stringify({
        session_id: payload.session_id ?? null,
        message: payload.message ?? '',
        // 无图片时不要发 image_base64 / image_mime 的 null 占位：
        // 虽然后端已做 null 容忍，但「不带图片就不传图片字段」语义更清晰，
        // 也让抓包时一眼能看出这个请求是纯文本还是带图
        ...(payload.image_base64 && payload.image_mime
          ? { image_base64: payload.image_base64, image_mime: payload.image_mime }
          : {}),
        use_knowledge: payload.use_knowledge ?? true,
        stream: true,
      }),
      signal,
    })
  } catch (err) {
    // 用户主动停止：不算错误，按正常结束处理
    if (isAbortError(err)) {
      safeCall(h.onDone, { aborted: true })
      return
    }
    fail(err)
    return
  }

  // 非 200：读取 body 作为错误详情，避免只显示状态码
  if (!response.ok) {
    let detail = ''
    try {
      detail = await response.text()
    } catch (err) {
      detail = ''
    }
    const statusText = response.statusText ? ` ${response.statusText}` : ''
    const suffix = detail ? `：${detail.slice(0, 300)}` : ''
    fail(new Error(`请求失败（HTTP ${response.status}${statusText}）${suffix}`))
    return
  }

  if (!response.body || typeof response.body.getReader !== 'function') {
    fail(new Error('当前浏览器不支持流式响应'))
    return
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  let finished = false

  const finish = (data) => {
    if (finished) return
    finished = true
    safeCall(h.onDone, data)
  }

  const dispatch = (type, data) => {
    switch (type) {
      case 'meta':
        safeCall(h.onMeta, data)
        break
      case 'node':
        safeCall(h.onNode, data)
        break
      case 'vision':
      case 'diagnosis':
        safeCall(h.onVision, data)
        break
      case 'sources':
      case 'reference':
        safeCall(h.onSources, data)
        break
      case 'token':
      case 'message': {
        const text = extractToken(data)
        if (text) safeCall(h.onToken, text)
        break
      }
      case 'done':
      case 'end':
      case 'finish':
        finish(data)
        break
      case 'error':
        fail(
          new Error(
            typeof data === 'string' ? data : data?.message || data?.detail || '服务端返回错误'
          )
        )
        finish({ error: true })
        break
      default:
        // 未声明事件名时：字符串直接当 token；对象里若带 type 已在上面归一化
        if (typeof data === 'string' && data) safeCall(h.onToken, data)
        break
    }
  }

  /** 解析一个完整 SSE 事件块（已按 \n\n 切分） */
  const parseBlock = (block) => {
    const lines = block.split('\n')
    let eventName = ''
    const dataLines = []

    for (const line of lines) {
      if (!line || line.startsWith(':')) continue // 注释行 / 心跳
      const colon = line.indexOf(':')
      const field = colon === -1 ? line : line.slice(0, colon)
      let value = colon === -1 ? '' : line.slice(colon + 1)
      if (value.startsWith(' ')) value = value.slice(1)
      if (field === 'event') eventName = value.trim()
      else if (field === 'data') dataLines.push(value)
    }

    if (!dataLines.length) return
    const raw = dataLines.join('\n')
    if (raw.trim() === '[DONE]') {
      finish({ done: true })
      return
    }

    let data
    try {
      data = JSON.parse(raw)
    } catch (err) {
      data = raw
    }

    let type = eventName
    // 兼容只发 data、把类型塞进 JSON 的服务端实现
    if (!type && data && typeof data === 'object') {
      type = data.type || data.event || ''
    }
    if (!type && data && typeof data === 'object' && (data.token || data.delta || data.content)) {
      type = 'token'
    }

    dispatch(type, data)
  }

  try {
    while (!finished) {
      const { value, done } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      // 统一换行符：跨 chunk 的 \r\n 会被拼成 \n\n，处理时不丢事件
      buffer = buffer.replace(/\r\n/g, '\n').replace(/\r/g, '\n')

      let sep = buffer.indexOf('\n\n')
      while (sep !== -1) {
        const block = buffer.slice(0, sep)
        buffer = buffer.slice(sep + 2)
        if (block.trim()) parseBlock(block)
        if (finished) break
        sep = buffer.indexOf('\n\n')
      }
    }

    // 流结束：冲刷解码器，并处理没有以空行结尾的最后一个事件块
    if (!finished) {
      buffer += decoder.decode()
      buffer = buffer.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
      if (buffer.trim()) {
        parseBlock(buffer)
        buffer = ''
      }
    }

    finish({ done: true })
  } catch (err) {
    if (isAbortError(err)) {
      finish({ aborted: true })
      return
    }
    fail(err)
    finish({ error: true })
  } finally {
    try {
      reader.releaseLock()
    } catch (err) {
      /* 忽略：部分环境不支持 releaseLock */
    }
  }
}

function extractToken(data) {
  if (data == null) return ''
  if (typeof data === 'string') return data
  if (typeof data === 'object') {
    const value = data.token ?? data.text ?? data.content ?? data.delta ?? ''
    return typeof value === 'string' ? value : ''
  }
  return ''
}

export default {
  getHealth,
  getModels,
  listDocuments,
  uploadDocument,
  deleteDocument,
  searchKnowledge,
  streamAgentChat,
}
