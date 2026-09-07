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
  /** Dify 翻译 workflow 的 API Key */
  readonly VITE_DIFY_API_KEY: string
  /** Dify 服务地址（前端直接调用，不走代理） */
  readonly VITE_DIFY_API_BASE: string
  /** 调用 Dify 时携带的用户标识 */
  readonly VITE_DIFY_USER: string
  /** Dify workflow 开始节点的输入变量名（md 原文放入 inputs[该 key]） */
  readonly VITE_DIFY_INPUT_KEY: string
  /** MinIO 存储桶名称（前端唯一需要的 MinIO 配置，用于拼同源路径 /<bucket>/<key>） */
  readonly VITE_MINIO_BUCKET: string
  /** 开发环境 MinIO 签名服务地址（本地 server.mjs），Vite 把 /<bucket>/... 代理到它 */
  readonly VITE_MINIO_SIGNER: string
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
