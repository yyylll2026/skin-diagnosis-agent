<script setup>
/**
 * ECharts 容器组件：只负责「把 option 画出来」。
 *
 * 三件事必须处理对，否则图表在抽屉/标签页里会以 0×0 渲染：
 * 1. **ResizeObserver**：容器尺寸变化时 resize。抽屉动画、标签页切换、
 *    侧栏折叠都会改变宽度，只监听 window.resize 会漏掉这些情况。
 * 2. **dispose**：卸载时销毁实例，否则反复开关抽屉会持续泄漏 canvas。
 * 3. **setOption(option, true)**：用 notMerge 覆盖式更新，避免上一次的
 *    series 残留（比如部位数量变少时旧的柱子还挂在图上）。
 */
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import echarts from '../../utils/echarts'

const props = defineProps({
  option: { type: Object, required: true },
  height: { type: String, default: '260px' },
})

const el = ref(null)
const chart = shallowRef(null)
let observer = null

function resize() {
  chart.value?.resize()
}

onMounted(() => {
  chart.value = echarts.init(el.value)
  chart.value.setOption(props.option, true)
  window.addEventListener('resize', resize)
  if (typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(resize)
    observer.observe(el.value)
  }
})

watch(
  () => props.option,
  (next) => {
    if (!chart.value || !next) return
    chart.value.setOption(next, true)
  },
  { deep: true }
)

onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  observer?.disconnect()
  observer = null
  chart.value?.dispose()
  chart.value = null
})
</script>

<template>
  <div ref="el" class="echart" :style="{ height }" />
</template>

<style scoped>
.echart {
  width: 100%;
}
</style>
