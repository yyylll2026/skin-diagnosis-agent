<script setup>
import { computed } from 'vue'

const props = defineProps({
  diagnosis: {
    type: Object,
    default: () => ({}),
  },
})

const RISK_META = {
  low: { label: '低风险', color: 'var(--risk-low)', bg: 'var(--risk-low-bg)' },
  medium: { label: '中等风险', color: 'var(--risk-medium)', bg: 'var(--risk-medium-bg)' },
  high: { label: '高风险', color: 'var(--risk-high)', bg: 'var(--risk-high-bg)' },
  urgent: { label: '紧急', color: 'var(--risk-urgent)', bg: 'var(--risk-urgent-bg)' },
}

const d = computed(() => props.diagnosis || {})

const risk = computed(() => {
  const key = String(d.value.risk_level || '').toLowerCase()
  return RISK_META[key] || { label: '未评估', color: 'var(--text-secondary)', bg: 'var(--surface-hover)' }
})

const hypotheses = computed(() => {
  const list = Array.isArray(d.value.hypotheses) ? d.value.hypotheses : []
  return list.map((item) => {
    const raw = typeof item?.confidence === 'number' ? item.confidence : 0
    const percent = Math.max(0, Math.min(100, Math.round(raw <= 1 ? raw * 100 : raw)))
    return {
      name: item?.name || '未命名方向',
      rationale: item?.rationale || '',
      percent,
    }
  })
})

const observations = computed(() => {
  const obs = d.value.observations || {}
  const rows = [
    { label: '部位', value: obs.site },
    { label: '形态', value: obs.morphology },
    { label: '颜色', value: obs.color },
    { label: '边界', value: obs.border },
    { label: '大小', value: obs.size_estimate },
    { label: '分布', value: obs.distribution },
  ]
  return rows
    .map((row) => {
      const value = row.value
      const list = Array.isArray(value) ? value.filter(Boolean) : []
      return {
        label: row.label,
        tags: list,
        text: typeof value === 'string' && value.trim() ? value.trim() : '',
        visible: list.length > 0 || (typeof value === 'string' && !!value.trim()),
      }
    })
    .filter((row) => row.visible)
})

const redFlags = computed(() =>
  Array.isArray(d.value.red_flags) ? d.value.red_flags.filter((item) => !!item) : []
)

function barColor(percent) {
  if (percent >= 70) return 'var(--brand)'
  if (percent >= 40) return 'var(--brand-aux)'
  return '#7dd3e0'
}
</script>

<template>
  <div class="diagnosis">
    <div class="notice">
      <span class="notice-icon">i</span>
      <span>AI 辅助参考，不能替代医生诊断</span>
    </div>

    <div class="head">
      <span class="risk-tag" :style="{ color: risk.color, background: risk.bg, borderColor: risk.color }">
        <span class="risk-dot" :style="{ background: risk.color }"></span>
        {{ risk.label }}
      </span>
      <span class="head-title">疑似方向</span>
    </div>

    <p v-if="d.summary" class="summary">{{ d.summary }}</p>

    <p v-if="d.image_quality" class="quality">图像质量：{{ d.image_quality }}</p>

    <div v-if="hypotheses.length" class="hypotheses">
      <div v-for="(item, index) in hypotheses" :key="`${item.name}-${index}`" class="hypothesis">
        <div class="hypothesis-head">
          <span class="hypothesis-name">{{ item.name }}</span>
          <span class="hypothesis-percent">{{ item.percent }}%</span>
        </div>
        <div class="bar">
          <div
            class="bar-inner"
            :style="{ width: `${item.percent}%`, background: barColor(item.percent) }"
          ></div>
        </div>
        <p v-if="item.rationale" class="hypothesis-rationale">{{ item.rationale }}</p>
      </div>
    </div>
    <p v-else class="empty-line">本次未给出明确的疑似方向。</p>

    <div v-if="observations.length" class="obs">
      <div v-for="row in observations" :key="row.label" class="obs-row">
        <span class="obs-label">{{ row.label }}</span>
        <span class="obs-value">
          <template v-if="row.tags.length">
            <span v-for="(tag, i) in row.tags" :key="i" class="obs-chip">{{ tag }}</span>
          </template>
          <template v-else>{{ row.text }}</template>
        </span>
      </div>
    </div>

    <div v-if="redFlags.length" class="flags">
      <p class="flags-title">危险信号 · 建议尽快就医</p>
      <ul class="flags-list">
        <li v-for="(flag, index) in redFlags" :key="index">{{ flag }}</li>
      </ul>
    </div>

    <div v-if="d.recommended_department || d.urgency" class="advice">
      <div v-if="d.recommended_department" class="advice-item">
        <span class="advice-label">建议科室</span>
        <span class="advice-value">{{ d.recommended_department }}</span>
      </div>
      <div v-if="d.urgency" class="advice-item">
        <span class="advice-label">就医紧迫度</span>
        <span class="advice-value">{{ d.urgency }}</span>
      </div>
    </div>

    <p v-if="d.disclaimer" class="disclaimer">{{ d.disclaimer }}</p>
  </div>
</template>

<style scoped>
.diagnosis {
  margin-top: 12px;
  padding: 14px 15px 13px;
  border: 1px solid var(--brand-border);
  border-radius: var(--radius);
  background: #fbfeff;
}

.notice {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 6px 10px;
  margin-bottom: 12px;
  border-radius: var(--radius-sm);
  background: var(--warning-bg);
  border: 1px solid var(--warning-border);
  color: var(--warning-text);
  font-size: 12.5px;
  font-weight: 500;
}

.notice-icon {
  width: 15px;
  height: 15px;
  flex: 0 0 15px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: var(--warning-icon);
  color: #fff;
  font-size: 11px;
  font-weight: 700;
  font-family: Georgia, serif;
}

.head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}

.risk-tag {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 10px;
  border-radius: 999px;
  border: 1px solid;
  font-size: 12.5px;
  font-weight: 600;
}

.risk-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}

.head-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
}

.summary {
  font-size: 13.5px;
  color: var(--text);
  line-height: 1.7;
}

.quality {
  margin-top: 5px;
  font-size: 12.5px;
  color: var(--text-muted);
}

.hypotheses {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 11px;
}

.hypothesis-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
}

.hypothesis-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--text);
}

.hypothesis-percent {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--brand);
  font-variant-numeric: tabular-nums;
}

.bar {
  margin-top: 5px;
  height: 6px;
  border-radius: 999px;
  background: var(--surface-track);
  overflow: hidden;
}

.bar-inner {
  height: 100%;
  border-radius: 999px;
  background: var(--brand);
  transition: width 0.4s ease;
}

.hypothesis-rationale {
  margin-top: 5px;
  font-size: 12.5px;
  color: var(--text-secondary);
  line-height: 1.6;
}

.empty-line {
  margin-top: 10px;
  font-size: 12.5px;
  color: var(--text-muted);
}

.obs {
  margin-top: 13px;
  padding-top: 11px;
  border-top: 1px dashed var(--border);
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 7px 16px;
}

.obs-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 12.5px;
}

.obs-label {
  flex: 0 0 42px;
  color: var(--text-muted);
}

.obs-value {
  flex: 1;
  min-width: 0;
  color: var(--text);
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  line-height: 1.5;
}

.obs-chip {
  display: inline-block;
  padding: 0 7px;
  border-radius: 4px;
  background: var(--surface-tint);
  border: 1px solid var(--border);
  font-size: 12px;
  color: #334155;
}

.flags {
  margin-top: 13px;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  background: var(--danger-bg);
  border: 1px solid var(--danger-border);
}

.flags-title {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--danger);
  margin-bottom: 5px;
}

.flags-list {
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.flags-list li {
  position: relative;
  padding-left: 13px;
  font-size: 12.5px;
  color: var(--danger-text);
  line-height: 1.6;
}

.flags-list li::before {
  content: '';
  position: absolute;
  left: 0;
  top: 8px;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--danger);
}

.advice {
  margin-top: 13px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.advice-item {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 5px 11px;
  border-radius: var(--radius-sm);
  background: var(--brand-weak);
  border: 1px solid var(--brand-border);
  font-size: 12.5px;
}

.advice-label {
  color: var(--text-muted);
}

.advice-value {
  color: var(--brand);
  font-weight: 600;
}

.disclaimer {
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px solid var(--divider);
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text-muted);
}
</style>
