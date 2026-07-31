import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    outDir: '../static',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('node_modules')) {
            if (id.includes('react')) return 'vendor-react'
            if (id.includes('antd') || id.includes('@ant-design')) return 'vendor-antd'
            return 'vendor-misc'
          }

          if (id.includes('/src/pages/Accounts')) return 'page-accounts'
          if (id.includes('/src/pages/RegisterTaskPage')) return 'page-register'
          if (id.includes('/src/pages/Settings')) return 'page-settings'
          if (id.includes('/src/pages/TaskHistory')) return 'page-history'
          if (id.includes('/src/pages/Proxies')) return 'page-proxies'
          return undefined
        },
      },
    },
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
})
