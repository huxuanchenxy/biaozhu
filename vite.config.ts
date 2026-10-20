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

/**
 * 生成主题引导内联脚本：在首屏渲染前给 <html> 打标（dark 类 + data-theme）并设定 color-scheme，避免闪烁。
 * 与 server.mjs 中的实现保持一致：dev 由本文件 transformIndexHtml 注入，prod 由 server.mjs 注入。
 * light 保留原有 'light only'（阻止系统深色把内置 PDF 查看器外壳渲染成黑），dark 用 'dark'。
 */
function themeBootstrap(theme: string) {
  const t = theme === 'dark' ? 'dark' : 'light'
  return `<script>(function(){var t=${JSON.stringify(t)};var d=document.documentElement;if(t==='dark')d.classList.add('dark');d.setAttribute('data-theme',t);var m=document.querySelector('meta[name="color-scheme"]');if(!m){m=document.createElement('meta');m.setAttribute('name','color-scheme');document.head.appendChild(m)}m.content=t==='dark'?'dark':'light only';})();</script>`
}

/**
 * 开发期主题注入：把 index.html 里的 <!--APP_THEME_BOOTSTRAP--> 占位符替换为引导脚本。
 * 仅 apply:'serve'（不作用于 build），让 dist/index.html 保留占位符，交由 server.mjs 运行时注入，
 * 从而实现「改 APP_THEME + 重启即切换，无需 build」。
 */
function injectTheme(theme: string) {
  return {
    name: 'inject-app-theme',
    apply: 'serve' as const,
    transformIndexHtml(html: string) {
      return html.replace('<!--APP_THEME_BOOTSTRAP-->', themeBootstrap(theme))
    },
  }
}

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  // 读取当前模式下的环境变量（.env / .env.[mode]）
  const env = loadEnv(mode, process.cwd(), 'VITE_')
  // 主题变量 APP_THEME 不带 VITE_ 前缀（不进前端包），这里用空前缀再读一次拿到它
  const appTheme = loadEnv(mode, process.cwd(), '').APP_THEME || 'light'

  return {
    plugins: [injectTheme(appTheme), vue(), copyRuntimeFiles(env.VITE_OUT_DIR || 'dist')],

    // mupdf 用了 top-level await 且含被条件保护的 node:fs 动态导入，排除出预构建，
    // 交由 Vite 直接服务/打包（wasm 经 new URL(..., import.meta.url) 作为资源产出）。
    optimizeDeps: { exclude: ['mupdf'] },

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
        // CAD 标注页列目录接口：开发把 /cad-api 转发到本地签名服务 server.mjs（同 MinIO 那一跳）
        '/cad-api': {
          target: env.VITE_MINIO_SIGNER || 'http://127.0.0.1:3345',
          changeOrigin: true,
          secure: false,
        },
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
      // mupdf 的 top-level await 需要 esnext 目标（默认 es2020 不支持 TLA）
      target: 'esnext',
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
