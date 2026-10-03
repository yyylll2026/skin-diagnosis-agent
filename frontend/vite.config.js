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
          // ECharts 单独分包。它只被「个人健康记录」抽屉用到，而那是个异步组件；
          // 若跟着 vendor 走，vendor 就成了入口的静态依赖，图表库会在首屏被下载。
          // 单独成块后它只挂在异步链上，不开抽屉就不会加载。
          if (id.includes('echarts') || id.includes('zrender')) return 'echarts'
          return 'vendor'
        },
      },
    },
  },
})
