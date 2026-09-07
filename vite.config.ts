import { fileURLToPath, URL } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  // 读取当前模式下的环境变量（.env / .env.[mode]）
  const env = loadEnv(mode, process.cwd(), 'VITE_')

  return {
    plugins: [vue()],

    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },

    server: {
      host: '0.0.0.0',
      port: Number(env.VITE_PORT) || 3000,
      open: false,
      // 开发环境接口代理：前端 /api -> 后端真实地址
      proxy: {
        [env.VITE_APP_BASE_API || '/api']: {
          target: env.VITE_PROXY_TARGET,
          changeOrigin: true,
          // 如果后端接口本身不带 /api 前缀，放开下面这行去掉前缀
          // rewrite: (path) => path.replace(new RegExp(`^${env.VITE_APP_BASE_API || '/api'}`), ''),
        },
        // MinIO 反向代理：把 /<bucket>/... 转发到真实 MinIO，让浏览器视为同源以绕过 CORS。
        // 关键：changeOrigin 必须为 false 且不重写路径，否则 Host/路径变化会导致 S3 的 SigV4 签名校验失败。
        ...(env.VITE_MINIO_BUCKET && env.VITE_MINIO_ENDPOINT
          ? {
              [`/${env.VITE_MINIO_BUCKET}`]: {
                target: env.VITE_MINIO_ENDPOINT,
                changeOrigin: false,
                secure: false,
              },
            }
          : {}),
      },
    },

    build: {
      outDir: env.VITE_OUT_DIR || 'dist',
      // 关闭后不生成 .map，包更小；需要线上排错可改为 true
      sourcemap: false,
      chunkSizeWarningLimit: 1500,
      rollupOptions: {
        output: {
          // 简单分包，避免单文件过大
          manualChunks: {
            vue: ['vue', 'vue-router'],
            elementPlus: ['element-plus', '@element-plus/icons-vue'],
          },
        },
      },
    },
  }
})
