import { copyFileSync, existsSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

/**
 * 构建后把 server.mjs 与 .env.production 拷进 dist：
 * 部署时整个 dist 带走即可，无需手动拷贝；
 * server.mjs 运行时会读 dist 里的 .env.production 取 MinIO 地址，改配置只需改 .env.production 再 build。
 */
function copyRuntimeFiles(outDir: string) {
  return {
    name: 'copy-runtime-files',
    closeBundle() {
      for (const file of ['server.mjs', '.env.production']) {
        const src = fileURLToPath(new URL(`./${file}`, import.meta.url))
        const dest = fileURLToPath(new URL(`./${outDir}/${file}`, import.meta.url))
        if (existsSync(src)) copyFileSync(src, dest)
      }
    },
  }
}

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  // 读取当前模式下的环境变量（.env / .env.[mode]）
  const env = loadEnv(mode, process.cwd(), 'VITE_')

  return {
    plugins: [vue(), copyRuntimeFiles(env.VITE_OUT_DIR || 'dist')],

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
        // MinIO 反向代理（开发）：把 /<bucket>/... 转发到本地签名服务 server.mjs（由 npm run dev 自动拉起）。
        // 这一跳浏览器->签名服务是「无签名」的，Host/路径怎么变都不影响签名；
        // 真正的 SigV4 由 server.mjs 用服务端时钟对 MinIO 重新签名，故此处无需保留 Host。
        ...(env.VITE_MINIO_BUCKET
          ? {
              [`/${env.VITE_MINIO_BUCKET}`]: {
                target: env.VITE_MINIO_SIGNER || 'http://127.0.0.1:3345',
                changeOrigin: true,
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
