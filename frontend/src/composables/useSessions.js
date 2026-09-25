import { ref, computed } from 'vue'
import { uid } from '../utils/common'

/**
 * 会话状态：模块级 ref（不引入 pinia），App 与 ChatPanel 共享同一份状态。
 * 全部会话（含消息）持久化到 localStorage。
 *
 * 注意：这里刻意不使用深度 watch 自动持久化——流式输出时每个 token 都会触发，
 * 会导致频繁 JSON.stringify。改为在「一轮对话结束」等关键节点显式调用 persist()。
 */
const STORAGE_KEY = 'skin-diagnosis:sessions:v1'

const sessions = ref([])
const currentId = ref(null)
let initialized = false

function safeParse(raw) {
  try {
    const data = JSON.parse(raw)
    return Array.isArray(data) ? data : []
  } catch (err) {
    return []
  }
}

function persist() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(sessions.value))
  } catch (err) {
    // 超出配额或隐私模式：静默降级，不影响本次会话使用
  }
}

function normalizeSession(raw) {
  return {
    id: raw?.id || uid(),
    title: typeof raw?.title === 'string' && raw.title ? raw.title : '新会话',
    createdAt: Number(raw?.createdAt) || Date.now(),
    updatedAt: Number(raw?.updatedAt) || Number(raw?.createdAt) || Date.now(),
    messages: Array.isArray(raw?.messages) ? raw.messages : [],
  }
}

function sortByUpdated() {
  sessions.value = [...sessions.value].sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0))
}

function createSession() {
  const now = Date.now()
  const session = { id: uid(), title: '新会话', createdAt: now, updatedAt: now, messages: [] }
  sessions.value = [session, ...sessions.value]
  currentId.value = session.id
  persist()
  return session
}

function selectSession(id) {
  if (sessions.value.some((s) => s.id === id)) {
    currentId.value = id
  }
}

function removeSession(id) {
  const index = sessions.value.findIndex((s) => s.id === id)
  if (index === -1) return
  const wasCurrent = currentId.value === id
  sessions.value = sessions.value.filter((s) => s.id !== id)
  if (wasCurrent) {
    if (sessions.value.length) {
      currentId.value = sessions.value[0].id
    } else {
      createSession()
      return
    }
  }
  persist()
}

/** 用首条用户消息自动命名会话 */
function maybeRenameFromMessage(session, text) {
  if (!session) return
  if (session.title && session.title !== '新会话') return
  const clean = String(text || '').replace(/\s+/g, ' ').trim()
  if (!clean) return
  session.title = clean.length > 18 ? `${clean.slice(0, 18)}…` : clean
}

/** 标记会话有更新并落盘 */
function touchSession(session) {
  if (!session) return
  session.updatedAt = Date.now()
  sortByUpdated()
  persist()
}

function init() {
  if (initialized) return
  initialized = true

  if (typeof localStorage !== 'undefined') {
    const list = safeParse(localStorage.getItem(STORAGE_KEY)).map(normalizeSession)
    if (list.length) {
      sessions.value = list.sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0))
      currentId.value = sessions.value[0].id
    }
  }

  if (!sessions.value.length) {
    createSession()
  }
}

const currentSession = computed(() => sessions.value.find((s) => s.id === currentId.value) || null)

const currentMessages = computed(() => currentSession.value?.messages || [])

export function useSessions() {
  return {
    sessions,
    currentId,
    currentSession,
    currentMessages,
    init,
    createSession,
    selectSession,
    removeSession,
    maybeRenameFromMessage,
    touchSession,
    persist,
  }
}
