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
  // 会话在 httpOnly cookie 里，前端拿不到也不需要它。
  // withCredentials 让浏览器在跨源场景下也带上 cookie（同源时本来就会带）。
  withCredentials: true,
})

/**
 * 401 回调。
 *
 * 由 useAuth 注册：任何请求遇到 401（会话过期/未登录）都能统一弹登录框，
 * 而不是在每个调用点各写一遍判断。放在这里而不是 axios 拦截器里直接弹窗，
 * 是为了不让 api 层依赖 UI——api 层只负责「通知」，怎么展示由 UI 决定。
 */
let onUnauthorized = null

export function setUnauthorizedHandler(handler) {
  onUnauthorized = handler
}

http.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error?.response?.status
    // 401 = 没登录/会话失效；403 = 登录了但没权限。
    // 只有 401 才该弹登录框——403 弹登录框会让用户反复输入密码却依然无权限。
    if (status === 401 && typeof onUnauthorized === 'function') {
      onUnauthorized()
    }
    return Promise.reject(error)
  }
)

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

/* ------------------------------ 鉴权 ------------------------------ */

/**
 * 当前登录用户：GET /api/v1/auth/me
 * 401 表示未登录（由拦截器统一处理），200 返回身份与权限点。
 */
export function getMe() {
  return request(http.get('/auth/me'))
}

/** 登录：POST /api/v1/auth/login（成功后 cookie 由服务端下发，前端不接触凭据） */
export function login(username, password) {
  return request(http.post('/auth/login', { username, password }))
}

/** 注册：POST /api/v1/auth/register（角色固定为客户，注册成功即登录） */
export function register(username, password, displayName = '') {
  return request(
    http.post('/auth/register', {
      username,
      password,
      display_name: displayName,
    })
  )
}

/** 登出：POST /api/v1/auth/logout */
export function logout() {
  return request(http.post('/auth/logout'))
}

/** 修改自己的密码：POST /api/v1/auth/password（成功后所有设备需重新登录） */
export function changePassword(oldPassword, newPassword) {
  return request(
    http.post('/auth/password', { old_password: oldPassword, new_password: newPassword })
  )
}

/* ------------------------------ 用户管理（管理员） ------------------------------ */

/** 用户列表：GET /api/v1/users */
export function listUsers(keyword = '') {
  return request(http.get('/users', { params: keyword ? { keyword } : {} }))
}

/** 创建用户：POST /api/v1/users（可指定角色） */
export function createUser(payload) {
  return request(http.post('/users', payload))
}

/** 修改用户：PATCH /api/v1/users/{id}（显示名 / 角色 / 状态） */
export function updateUser(userId, payload) {
  return request(http.patch(`/users/${encodeURIComponent(userId)}`, payload))
}

/** 删除用户：DELETE /api/v1/users/{id} */
export function deleteUser(userId) {
  return request(http.delete(`/users/${encodeURIComponent(userId)}`))
}

/** 重置他人密码：POST /api/v1/users/{id}/password（对方所有会话会失效） */
export function resetUserPassword(userId, newPassword) {
  return request(
    http.post(`/users/${encodeURIComponent(userId)}/password`, { new_password: newPassword })
  )
}

/** 强制下线：POST /api/v1/users/{id}/force-logout */
export function forceLogoutUser(userId) {
  return request(http.post(`/users/${encodeURIComponent(userId)}/force-logout`))
}

/** 审计日志：GET /api/v1/audit/logs */
export function listAuditLogs(limit = 100, action = '') {
  return request(http.get('/audit/logs', { params: { limit, ...(action ? { action } : {}) } }))
}

/* ------------------------------ 个人健康记录 ------------------------------ */

/** 获取当前账号的健康记录时间线 */
export function listHealthRecords(limit = 100, offset = 0) {
  return request(http.get('/health-records', { params: { limit, offset } }))
}

/** 新增健康记录 */
export function createHealthRecord(payload) {
  return request(http.post('/health-records', payload))
}

/** 更新本人健康记录 */
export function updateHealthRecord(recordId, payload) {
  return request(http.patch(`/health-records/${encodeURIComponent(recordId)}`, payload))
}

/** 删除本人健康记录 */
export function deleteHealthRecord(recordId) {
  return request(http.delete(`/health-records/${encodeURIComponent(recordId)}`))
}

/** 导出本人全部记录为 JSON 文件 */
export function exportHealthRecords() {
  return request(http.get('/health-records/export', { responseType: 'blob' }))
}

/**
 * 病程趋势聚合：GET /api/v1/health-records/trends
 *
 * 聚合在后端完成（按天序列 / 部位分布 / 程度分布），前端只负责画图——
 * 把「算」和「画」分开，避免在浏览器里重复实现一遍窗口与分档逻辑。
 */
export function getHealthTrends(days = 90) {
  return request(http.get('/health-records/trends', { params: { days } }))
}

/* ------------------------------ 医生工作台 ------------------------------ */

// 以下三个接口在服务端都由 `health_records:read_all` 权限点守卫（客户没有该点），
// 前端在侧栏按同一权限点决定是否显示入口，这里不做二次判断。

/** 患者列表（含每人最新记录摘要，按最近记录时间倒序） */
export function listPatients(limit = 200, offset = 0) {
  return request(http.get('/health-records/patients', { params: { limit, offset } }))
}

/** 查看某患者的全部健康记录（只读；编辑/删除仍仅限患者本人） */
export function listPatientRecords(patientId, limit = 200, offset = 0) {
  return request(
    http.get(`/health-records/patients/${encodeURIComponent(patientId)}/records`, {
      params: { limit, offset },
    })
  )
}

/** 查看某患者的病程趋势聚合 */
export function getPatientTrends(patientId, days = 90) {
  return request(
    http.get(`/health-records/patients/${encodeURIComponent(patientId)}/trends`, {
      params: { days },
    })
  )
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
 *
 * 分类必须作为 **multipart 表单字段**发送，不能放查询参数——
 * 后端的签名是 `category: Annotated[str, Form(...)]`，只从 body 取值，
 * 查询参数会被静默忽略、分类永远落到默认的「其他」。
 * （此处原先是 `params: { category }`，实测确认分类被丢弃，已修正。）
 */
export function uploadDocument(file, category) {
  const form = new FormData()
  form.append('file', file)
  if (category) form.append('category', category)
  return request(
    http.post('/kb/documents', form, {
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

/** 检索评测标注模板：GET /api/v1/kb/eval/starter */
export function getEvalStarter() {
  return request(http.get('/kb/eval/starter'))
}

/**
 * 运行检索评测：POST /api/v1/kb/eval/run
 *
 * 只走检索链路（不调生成模型），但样本多时仍要逐条跑混合检索，
 * 所以超时给得比普通请求宽。
 */
export function runRetrievalEval(payload) {
  return request(http.post('/kb/eval/run', payload, { timeout: 180000 }))
}

/* ------------------------------ 后台分析任务 ------------------------------ */

/** 提交后台图片分析，快速返回任务 ID */
export function createAnalysisTask(payload) {
  return request(http.post('/analysis-tasks', payload, { timeout: 120000 }))
}

/** 查询后台任务状态与最终结果 */
export function getAnalysisTask(taskId) {
  return request(http.get(`/analysis-tasks/${encodeURIComponent(taskId)}`))
}

/** 取消后台任务 */
export function cancelAnalysisTask(taskId) {
  return request(http.post(`/analysis-tasks/${encodeURIComponent(taskId)}/cancel`))
}

/** 在服务端仍短时保留原始输入时重试失败任务 */
export function retryAnalysisTask(taskId) {
  return request(http.post(`/analysis-tasks/${encodeURIComponent(taskId)}/retry`))
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
 * @param {object} handlers { onMeta, onNode, onVision, onClarify, onSources, onToken, onDone, onError }
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
      // fetch 默认不带 cookie（与 axios 不同），必须显式声明。
      // 漏掉这一行的表现是「REST 接口正常，但一对话就 401」，很难查。
      credentials: 'include',
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
    // 401 走和 axios 同一套处理：弹登录框，而不是把 JSON 原文丢给用户看
    if (response.status === 401 && typeof onUnauthorized === 'function') {
      onUnauthorized()
      fail(new Error('登录状态已失效，请重新登录'))
      return
    }
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
      case 'clarify':
        safeCall(h.onClarify, data)
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
  // 系统
  getHealth,
  getModels,
  // 鉴权
  getMe,
  login,
  register,
  logout,
  changePassword,
  // 用户管理
  listUsers,
  createUser,
  updateUser,
  deleteUser,
  resetUserPassword,
  forceLogoutUser,
  listAuditLogs,
  // 个人健康记录
  listHealthRecords,
  createHealthRecord,
  updateHealthRecord,
  deleteHealthRecord,
  exportHealthRecords,
  getHealthTrends,
  // 医生工作台
  listPatients,
  listPatientRecords,
  getPatientTrends,
  // 知识库
  listDocuments,
  uploadDocument,
  deleteDocument,
  searchKnowledge,
  getEvalStarter,
  runRetrievalEval,
  // 后台分析任务
  createAnalysisTask,
  getAnalysisTask,
  cancelAnalysisTask,
  retryAnalysisTask,
  // 对话
  streamAgentChat,
  setUnauthorizedHandler,
}
