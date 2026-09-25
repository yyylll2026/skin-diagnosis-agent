<script setup>
import { computed } from 'vue'

/**
 * 追问澄清卡片：把「信息完备性判断」的结果结构化呈现。
 *
 * 后端已经把追问问题写成 markdown 正文（用户能直接读），
 * 这张卡片的作用是**把「已知什么、还缺什么、问到第几轮」变成一眼可见的结构**，
 * 而不是让用户在一段话里自己找。
 */
const props = defineProps({
  clarify: {
    type: Object,
    default: () => ({}),
  },
})

const collected = computed(() => Object.entries(props.clarify?.collected || {}))

const missing = computed(() => props.clarify?.missing || [])

const round = computed(() => Number(props.clarify?.round) || 0)
const maxRounds = computed(() => Number(props.clarify?.max_rounds) || 0)
const showRound = computed(() => round.value > 0 && maxRounds.value > 0)

/** 兜底作答的原因，用于给用户一句可读的解释 */
const fallbackHint = computed(() => {
  if (!props.clarify?.fallback) return ''
  const reason = props.clarify?.bypassed_reason
  if (reason === 'user_skip') return '已按你的要求直接作答，未再追问。'
  if (reason === 'round_limit') return '追问已达上限，现按已收集到的信息作答，结论会受信息不足影响。'
  return '信息不完整，已按现有信息作答。'
})
</script>

<template>
  <section class="clarify-card">
    <header class="head">
      <span class="dot"></span>
      <span class="title">
        {{ clarify?.needs_clarification ? '需要补充几项信息' : '信息完备性判断' }}
      </span>
      <span v-if="showRound" class="round-badge">第 {{ round }}/{{ maxRounds }} 轮追问</span>
    </header>

    <div v-if="collected.length" class="block">
      <p class="label">已了解到</p>
      <div class="chips">
        <span v-for="[key, value] in collected" :key="key" class="chip is-known">
          {{ key }}：{{ value }}
        </span>
      </div>
    </div>

    <div v-if="missing.length" class="block">
      <p class="label">待补充</p>
      <div class="chips">
        <span v-for="item in missing" :key="item" class="chip is-missing">{{ item }}</span>
      </div>
    </div>

    <p v-if="fallbackHint" class="fallback">{{ fallbackHint }}</p>

    <p v-else class="tip">补齐后我会给出更有针对性的分析；说不清也可以直接回复「直接回答」。</p>
  </section>
</template>

<style scoped>
.clarify-card {
  margin-top: 12px;
  padding: 12px 14px;
  background: var(--brand-weak);
  border: 1px solid var(--brand-border);
  border-radius: var(--radius);
}

.head {
  display: flex;
  align-items: center;
  gap: 7px;
  flex-wrap: wrap;
}

.dot {
  width: 7px;
  height: 7px;
  flex: 0 0 7px;
  border-radius: 50%;
  background: var(--brand);
}

.title {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--brand);
}

.round-badge {
  font-size: 11.5px;
  color: var(--brand);
  background: var(--card-bg);
  border: 1px solid var(--brand-border);
  border-radius: 999px;
  padding: 1px 8px;
}

.block {
  margin-top: 10px;
}

.label {
  font-size: 11.5px;
  color: var(--text-secondary);
  margin-bottom: 5px;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.chip {
  font-size: 12.5px;
  line-height: 1.5;
  padding: 2px 9px;
  border-radius: 999px;
  border: 1px solid transparent;
}

.chip.is-known {
  background: var(--card-bg);
  border-color: var(--brand-border);
  color: var(--text);
}

.chip.is-missing {
  background: var(--risk-medium-bg);
  border-color: #fde3b3;
  color: var(--risk-medium);
}

.tip,
.fallback {
  margin-top: 10px;
  font-size: 12.5px;
  line-height: 1.6;
}

.tip {
  color: var(--text-secondary);
}

.fallback {
  color: var(--risk-medium);
}
</style>
