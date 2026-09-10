import MarkdownIt from 'markdown-it'

// html: false —— 禁止渲染原始 HTML，避免模型输出里携带的标签造成 XSS。
// breaks: true —— 单个换行也换行，更贴近模型输出的自然段。
const md = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
  underline: false,
})

const defaultLinkOpen =
  md.renderer.rules.link_open ||
  function (tokens, idx, options, _env, self) {
    return self.renderToken(tokens, idx, options)
  }

// 外链统一新窗口打开并加 rel，避免覆盖当前页面
md.renderer.rules.link_open = function (tokens, idx, options, env, self) {
  const token = tokens[idx]
  const hrefIndex = token.attrIndex('href')
  if (hrefIndex >= 0) {
    token.attrSet('target', '_blank')
    token.attrSet('rel', 'noopener noreferrer')
  }
  return defaultLinkOpen(tokens, idx, options, env, self)
}

/**
 * 渲染 Markdown 文本为 HTML 字符串。
 * @param {string} text
 * @returns {string}
 */
export function renderMarkdown(text) {
  if (!text) return ''
  try {
    return md.render(String(text))
  } catch (err) {
    // 渲染失败时退化为纯文本，保证消息不丢失
    return escapeHtml(String(text))
  }
}

function escapeHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}
