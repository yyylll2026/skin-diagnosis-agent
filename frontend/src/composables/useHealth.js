import { ref } from 'vue'
import { getHealth } from '../api'

/**
 * Ollama 连接状态轮询（模块级单例，App 与侧栏共享一份）。
 *
 * 后端健康接口的字段在迭代中变过几次，解析逻辑集中在这里：
 * 布尔字段、嵌套 ollama 对象、status 字符串三种形态都兼容。
 */
const health = ref({ online: false, model: '', checked: false })
const healthLoading = ref(false)
let timer = null

function firstBoolean(source, keys) {
  if (!source || typeof source !== 'object') return undefined
  for (const key of keys) {
    if (typeof source[key] === 'boolean') return source[key]
  }
  return undefined
}

function pickModel(source) {
  if (!source || typeof source !== 'object') return ''
  const keys = ['model', 'current_model', 'llm_model', 'ollama_model', 'vision_model', 'name']
  for (const key of keys) {
    if (typeof source[key] === 'string' && source[key]) return source[key]
  }
  return ''
}

async function refreshHealth() {
  if (healthLoading.value) return
  healthLoading.value = true
  try {
    const data = await getHealth()
    const onlineFlag =
      firstBoolean(data, ['ollama', 'ollama_connected', 'ollama_available', 'healthy', 'ok']) ??
      ['ok', 'healthy', 'up'].includes(String(data?.status || '').toLowerCase())
    // 后端也可能把 ollama 状态放在嵌套字段里
    const nested = data?.ollama && typeof data.ollama === 'object' ? data.ollama : null
    const nestedFlag = firstBoolean(nested, ['connected', 'available', 'ok', 'running'])
    health.value = {
      online: nestedFlag ?? onlineFlag,
      model: pickModel(data) || pickModel(nested),
      checked: true,
    }
  } catch (err) {
    health.value = { online: false, model: '', checked: true }
  } finally {
    healthLoading.value = false
  }
}

function startPolling(interval = 30000) {
  stopPolling()
  refreshHealth()
  timer = window.setInterval(refreshHealth, interval)
}

function stopPolling() {
  if (timer) {
    window.clearInterval(timer)
    timer = null
  }
}

export function useHealth() {
  return { health, healthLoading, refreshHealth, startPolling, stopPolling }
}
