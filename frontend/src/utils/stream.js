/**
 * SSE 事件字段容错解析。
 *
 * 后端在迭代过程中事件字段名发生过变化，这里集中做归一，
 * 视图层只面对稳定的解析结果，不写防御代码。
 */

/** 从 node 事件 payload 里取节点 key */
export function resolveNode(data) {
  if (!data) return ''
  if (typeof data === 'string') return data
  return data.node || data.name || data.step || data.current || ''
}

/** 从 vision / diagnosis 事件 payload 里取诊断对象 */
export function resolveDiagnosis(data) {
  if (!data || typeof data !== 'object') return null
  if (data.diagnosis && typeof data.diagnosis === 'object') return data.diagnosis
  if (data.result && typeof data.result === 'object') return data.result
  if ('risk_level' in data || 'hypotheses' in data || 'summary' in data) return data
  if (data.data && typeof data.data === 'object') return data.data
  return null
}

/** 从 sources 事件 payload 里取来源数组 */
export function resolveSources(data) {
  if (Array.isArray(data)) return data
  if (Array.isArray(data?.sources)) return data.sources
  if (Array.isArray(data?.data)) return data.data
  if (Array.isArray(data?.items)) return data.items
  return []
}

/** 从 clarify / done 事件 payload 里取澄清对象 */
export function resolveClarify(data) {
  if (!data || typeof data !== 'object') return null
  if (Array.isArray(data.missing) || Array.isArray(data.missing_keys)) return data
  if (data.data && typeof data.data === 'object') return data.data
  // 兜底：后端把判定结果放在 done 事件的 clarify 字段里
  if (data.clarify && typeof data.clarify === 'object') return data.clarify
  return null
}
