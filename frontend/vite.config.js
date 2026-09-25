import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发环境把 /api 反向代理到本地 FastAPI，避免跨域
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1500,
    rollupOptions: {
      output: {
        // 三方库单独分包：利用浏览器长缓存，业务代码改动不拖累 vendor
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined
          if (id.includes('element-plus') || id.includes('@element-plus')) return 'element-plus'
          if (id.includes('markdown-it') || id.includes('linkify-it') || id.includes('mdurl') || id.includes('uc.micro')) {
            return 'markdown'
          }
          return 'vendor'
        },
      },
    },
  },
})
