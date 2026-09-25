<script setup>
/**
 * 空会话欢迎页：纯展示组件，无状态。
 */
</script>

<template>
  <div class="welcome">
    <div class="welcome-logo">皮</div>
    <h3 class="welcome-title">皮肤诊断智能体</h3>
    <p class="welcome-text">
      上传一张皮肤图像，或直接描述症状（部位、形态、持续时间等），我会给出初步的视觉观察与就医建议。
    </p>
    <div class="welcome-tips">
      <span class="tip">支持点击 / 拖拽 / 粘贴图片</span>
      <span class="tip">可开启知识库检索获取参考资料</span>
      <span class="tip">结果仅供辅助参考</span>
    </div>
  </div>
</template>

<style scoped>
.welcome {
  margin: auto;
  max-width: 520px;
  padding: 32px 24px;
  text-align: center;
}

.welcome-logo {
  width: 56px;
  height: 56px;
  margin: 0 auto 16px;
  border-radius: 16px;
  background: var(--brand);
  color: var(--text-inverse);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;
  font-weight: 600;
}

.welcome-title {
  font-size: 19px;
  font-weight: 600;
  color: var(--text);
}

.welcome-text {
  margin-top: 8px;
  font-size: 13.5px;
  line-height: 1.8;
  color: var(--text-secondary);
}

.welcome-tips {
  margin-top: 18px;
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 8px;
}

.tip {
  padding: 4px 11px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--card-bg);
  font-size: 12px;
  color: var(--text-secondary);
}

@media (max-width: 640px) {
  .welcome {
    padding: 24px 8px;
  }
}
</style>
