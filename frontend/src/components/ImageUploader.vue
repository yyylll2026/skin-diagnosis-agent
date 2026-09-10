<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'

const props = defineProps({
  modelValue: {
    type: Object,
    default: null,
  },
  disabled: {
    type: Boolean,
    default: false,
  },
})

const emit = defineEmits(['update:modelValue'])

const ACCEPT_MIME = ['image/png', 'image/jpeg', 'image/jpg', 'image/webp']
const ACCEPT_EXT = /\.(png|jpe?g|webp)$/i
const MAX_SIZE = 8 * 1024 * 1024

const dragging = ref(false)
const fileInput = ref(null)

const image = computed(() => props.modelValue || null)

const sizeText = computed(() => {
  const bytes = image.value?.size || 0
  if (!bytes) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
})

function guessMime(name) {
  const lower = String(name || '').toLowerCase()
  if (lower.endsWith('.png')) return 'image/png'
  if (lower.endsWith('.webp')) return 'image/webp'
  return 'image/jpeg'
}

function validate(file) {
  if (!file) return false
  const mimeOk = ACCEPT_MIME.includes(file.type)
  if (!mimeOk && !ACCEPT_EXT.test(file.name || '')) {
    ElMessage.error('仅支持 PNG / JPG / JPEG / WEBP 格式的图片')
    return false
  }
  if (file.size > MAX_SIZE) {
    ElMessage.error('图片大小不能超过 8MB')
    return false
  }
  return true
}

function readFile(file) {
  if (!validate(file)) return
  const reader = new FileReader()
  reader.onload = () => {
    const dataUrl = String(reader.result || '')
    const comma = dataUrl.indexOf(',')
    const base64 = comma === -1 ? dataUrl : dataUrl.slice(comma + 1)
    emit('update:modelValue', {
      name: file.name || '未命名图片',
      size: file.size || 0,
      mime: file.type || guessMime(file.name),
      dataUrl,
      base64,
    })
  }
  reader.onerror = () => {
    ElMessage.error('图片读取失败，请重新选择')
  }
  reader.readAsDataURL(file)
}

function openPicker() {
  if (props.disabled) return
  fileInput.value?.click()
}

function onInputChange(event) {
  const file = event.target?.files?.[0]
  if (file) readFile(file)
  if (event.target) event.target.value = ''
}

function onDrop(event) {
  dragging.value = false
  if (props.disabled) return
  const file = event.dataTransfer?.files?.[0]
  if (file) readFile(file)
}

function onDragOver() {
  if (!props.disabled) dragging.value = true
}

function onDragLeave() {
  dragging.value = false
}

function clear() {
  emit('update:modelValue', null)
}

/** 全局粘贴：剪贴板里带图片时直接读取 */
function onPaste(event) {
  if (props.disabled) return
  const items = event.clipboardData?.items
  if (!items) return
  for (const item of items) {
    if (item.kind === 'file' && item.type.startsWith('image/')) {
      const file = item.getAsFile()
      if (file) {
        event.preventDefault()
        readFile(file)
        ElMessage.success('已从剪贴板读取图片')
      }
      return
    }
  }
}

onMounted(() => {
  window.addEventListener('paste', onPaste)
})

onBeforeUnmount(() => {
  window.removeEventListener('paste', onPaste)
})
</script>

<template>
  <div class="uploader">
    <input
      ref="fileInput"
      class="hidden-input"
      type="file"
      accept="image/png,image/jpeg,image/webp"
      @change="onInputChange"
    />

    <div v-if="!image" class="dropzone" :class="{ 'is-dragging': dragging }" @click="openPicker"
      @dragover.prevent="onDragOver" @dragenter.prevent="onDragOver" @dragleave.prevent="onDragLeave"
      @drop.prevent="onDrop">
      <span class="dropzone-icon">＋</span>
      <span class="dropzone-text">
        点击 / 拖拽 / 粘贴图片
        <em>支持 PNG、JPG、WEBP，不超过 8MB</em>
      </span>
    </div>

    <div v-else class="preview">
      <img class="preview-img" :src="image.dataUrl" :alt="image.name" />
      <div class="preview-meta">
        <p class="preview-name" :title="image.name">{{ image.name }}</p>
        <p class="preview-size">{{ sizeText }} · {{ image.mime }}</p>
      </div>
      <button class="preview-remove" type="button" title="移除图片" @click="clear">×</button>
    </div>
  </div>
</template>

<style scoped>
.uploader {
  width: 100%;
}

.hidden-input {
  display: none;
}

.dropzone {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius-sm);
  background: #fbfdfe;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
  user-select: none;
}

.dropzone:hover,
.dropzone.is-dragging {
  border-color: var(--brand);
  background: var(--brand-weak);
}

.dropzone-icon {
  width: 26px;
  height: 26px;
  flex: 0 0 26px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 7px;
  background: var(--brand-weak);
  color: var(--brand);
  font-size: 15px;
  line-height: 1;
  border: 1px solid var(--brand-border);
}

.dropzone-text {
  display: flex;
  flex-direction: column;
  font-size: 13px;
  color: var(--text-secondary);
  line-height: 1.45;
}

.dropzone-text em {
  font-style: normal;
  font-size: 11.5px;
  color: var(--text-muted);
}

.preview {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border: 1px solid var(--brand-border);
  border-radius: var(--radius-sm);
  background: var(--brand-weak);
}

.preview-img {
  width: 52px;
  height: 52px;
  flex: 0 0 52px;
  object-fit: cover;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  background: #fff;
}

.preview-meta {
  flex: 1;
  min-width: 0;
}

.preview-name {
  font-size: 13px;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.preview-size {
  font-size: 11.5px;
  color: var(--text-muted);
}

.preview-remove {
  width: 22px;
  height: 22px;
  flex: 0 0 22px;
  border: none;
  border-radius: 5px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 16px;
  line-height: 1;
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}

.preview-remove:hover {
  background: var(--danger-bg);
  color: var(--danger);
}
</style>
