/// <reference types="vite/client" />

/** 自定义环境变量类型提示（新增变量时同步补这里，写代码会有提示） */
interface ImportMetaEnv {
  /** 当前环境标识：development / production */
  readonly VITE_APP_ENV: string
  /** 应用标题 */
  readonly VITE_APP_TITLE: string
  /** 接口基础路径，如 /api */
  readonly VITE_APP_BASE_API: string
  /** 路由模式：hash | history */
  readonly VITE_ROUTER_MODE: string
  /** 待标注文档地址（本地文件或后端接口） */
  readonly VITE_APP_DOC_URL: string
  /** 是否默认自动加载本地翻译模型：true 自动预热 / false 进入翻译页手动加载（省内存） */
  readonly VITE_APP_TRANSLATION_AUTOLOAD: string
  /** 开发代理目标地址 */
  readonly VITE_PROXY_TARGET: string
  /** 开发服务器端口 */
  readonly VITE_PORT: string
  /** 打包输出目录 */
  readonly VITE_OUT_DIR: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

/** 让 TS 认识 .vue 单文件组件 */
declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}
