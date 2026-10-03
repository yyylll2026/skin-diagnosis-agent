<script setup>
/**
 * 用户消息气泡：可选图片 + 文本。
 */
defineProps({
  message: {
    type: Object,
    required: true,
  },
})
</script>

<template>
  <div class="row row-user">
    <div class="user-stack">
      <img v-if="message.image" class="user-image" :src="message.image" alt="上传的皮肤图像" />
      <div v-else-if="message.hasImage" class="image-placeholder">已上传图片（为保护隐私，图片不会保存在浏览器存储中）</div>
      <div v-if="message.content" class="user-bubble">{{ message.content }}</div>
    </div>
  </div>
</template>

<style scoped>
.row {
  display: flex;
  width: 100%;
  justify-content: flex-end;
}

.user-stack {
  max-width: min(72%, 620px);
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 8px;
}

.user-image {
  max-width: 220px;
  max-height: 220px;
  border-radius: var(--radius);
  border: 1px solid var(--border);
  box-shadow: var(--card-shadow);
  object-fit: cover;
}

.image-placeholder {
  padding: 7px 11px;
  border: 1px dashed var(--brand-border);
  border-radius: var(--radius-sm);
  background: var(--brand-weak);
  color: var(--text-secondary);
  font-size: 12px;
}

.user-bubble {
  padding: 10px 14px;
  border-radius: 14px 14px 4px 14px;
  background: var(--brand);
  color: var(--text-inverse);
  font-size: 14px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  box-shadow: 0 2px 8px rgba(14, 116, 144, 0.18);
}

@media (max-width: 640px) {
  .user-stack {
    max-width: 88%;
  }

  .user-image {
    max-width: 180px;
    max-height: 180px;
  }
}
</style>
