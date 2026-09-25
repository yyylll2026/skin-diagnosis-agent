/**
 * 通用小工具：不依赖任何业务语义，可在任意模块复用。
 */

/** 生成短随机 id（会话 / 消息 / 列表 key 通用） */
export function uid() {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}

/**
 * 从接口返回里容错地取数组。
 * 后端不同接口的列表字段名不一致（documents / items / data / results），
 * 也可能是裸数组，统一在这里归一。
 */
export function pickList(data, keys = ['documents', 'items', 'data', 'results']) {
  if (Array.isArray(data)) return data
  if (!data || typeof data !== 'object') return []
  for (const key of keys) {
    if (Array.isArray(data[key])) return data[key]
  }
  return []
}

/**
 * 归一化一条「参考来源 / 检索命中」条目。
 * SourceList（对话内参考来源）与 KnowledgePanel（检索测试）共用，
 * 字段兜底顺序保持一致，避免两处各写一套。
 */
export function normalizeSource(raw, index = 0, fallbackPrefix = '参考片段') {
  const item = raw || {}
  return {
    key: item.doc_id ?? item.id ?? `source-${index}`,
    title: item.title || item.source || item.filename || item.name || `${fallbackPrefix} ${index + 1}`,
    source: item.source || '',
    category: item.category || '',
    snippet: item.snippet || item.content || item.text || item.chunk || '',
    score: typeof item.score === 'number' ? item.score : null,
  }
}

/** 相关度分数 → 百分比文本（0~1 与 0~100 两种量纲都兼容） */
export function scorePercent(score) {
  if (typeof score !== 'number' || Number.isNaN(score)) return '—'
  return `${Math.round(score <= 1 ? score * 100 : score)}%`
}

/** 字节数 → 可读文本 */
export function formatBytes(bytes) {
  const size = Number(bytes) || 0
  if (!size) return ''
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / (1024 * 1024)).toFixed(2)} MB`
}
