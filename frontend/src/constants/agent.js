/**
 * LangGraph 节点 key → 界面文案。
 * label 用于结构化展示，active 是流式过程中的进行中文案。
 */
export const AGENT_NODES = {
  preprocess: { label: '预处理输入', active: '正在预处理输入…' },
  route: { label: '意图识别', active: '正在识别意图…' },
  vision_analyze: { label: '图像分析', active: '正在分析图像…' },
  check_completeness: { label: '信息完备性判断', active: '正在判断信息是否充分…' },
  clarify: { label: '生成追问', active: '正在整理需要补充的信息…' },
  build_query: { label: '构建检索意图', active: '正在构建检索意图…' },
  retrieve: { label: '检索知识库', active: '正在检索知识库…' },
  generate: { label: '生成回答', active: '正在生成回答…' },
  safety_review: { label: '安全审查', active: '正在进行安全审查…' },
}

/** 取节点文案，未知节点兜底为原始 key */
export function nodeMeta(key) {
  return AGENT_NODES[key] || { label: key, active: `正在${key}…` }
}
