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

    // transformers.js 体积大、文件多，预打包会生成数百文件的临时目录，
    // 本沙箱的 safe-delete 守卫会拦截其清理导致 dev 崩溃；排除后由浏览器按原生 ESM 加载
    // （生产构建走 Rollup 完整打包，不受影响）。
    optimizeDeps: {
      exclude: ['@huggingface/transformers', 'onnxruntime-web'],
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
