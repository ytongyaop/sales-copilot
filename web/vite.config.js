import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 前端只通过 HTTP 调后端：所有请求走 /api 前缀，由 dev server 代理到 FastAPI。
// rewrite 把 /api/leads 变成后端的 /leads（后端路由不带 /api 前缀）。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
