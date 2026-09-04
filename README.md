# 标注平台（biaozhu）前端脚手架

Vue 3 + TypeScript + Vite + Vue Router + Axios + Element Plus。
无登录、无权限，只保留最基础的运行骨架。

## 快速开始

```bash
npm install     # 安装依赖
npm run dev     # 启动开发服务器，默认 http://localhost:3000
npm run build   # 类型检查 + 打包，产物在 dist/
npm run preview # 本地预览打包产物
```

## 技术栈

| 依赖 | 版本 | 说明 |
| --- | --- | --- |
| vue | ^3.5 | 框架 |
| vue-router | ^4.5 | 路由 |
| axios | ^1.20 | HTTP 请求 |
| element-plus | ^2.9 | UI 组件库（已全量引入 + 中文语言包） |
| vite | ^6 | 构建工具 |
| typescript | ~5.7 | 类型检查 |
| vue-tsc | ^2.2 | 打包前类型检查 |

## 目录结构

```
.
├── .env                    # 通用环境变量（所有环境生效）
├── .env.development        # 开发环境变量
├── .env.production         # 生产环境变量
├── env.d.ts                # 环境变量 + .vue 的类型声明
├── index.html
├── vite.config.ts          # 别名、代理、打包配置
├── tsconfig.json           # 引用下面两个配置
├── tsconfig.app.json       # 前端代码编译配置
├── tsconfig.node.json      # vite.config.ts 编译配置
└── src
    ├── main.ts             # 入口：注册 Element Plus、图标、路由
    ├── App.vue             # 布局：顶部菜单 + router-view
    ├── api
    │   ├── index.ts        # 接口统一管理
    │   └── types.ts        # 接口数据类型
    ├── router/index.ts     # 路由表
    ├── utils/request.ts    # axios 封装（核心）
    ├── styles/index.css    # 全局样式
    └── views               # 页面
        ├── HomeView.vue    # 接口调用示例
        ├── AboutView.vue   # 环境变量展示
        └── NotFoundView.vue
```

## 环境变量

只有 `VITE_` 开头的变量才会被打包进前端代码。新增变量后记得同步更新 `env.d.ts`，写代码时才有类型提示。

| 变量 | 说明 | 开发环境 | 生产环境 |
| --- | --- | --- | --- |
| `VITE_APP_TITLE` | 浏览器标签标题 | 标注平台 | 标注平台 |
| `VITE_APP_ENV` | 环境标识 | development | production |
| `VITE_PORT` | 开发服务器端口 | 3000 | - |
| `VITE_APP_BASE_API` | 接口请求前缀 | /api | /api |
| `VITE_ROUTER_MODE` | 路由模式 | hash | hash |
| `VITE_PROXY_TARGET` | dev 代理目标 | http://10.89.34.77:8080 | - |
| `VITE_OUT_DIR` | 打包输出目录 | - | dist |

加载优先级：`.env.[mode]` > `.env`。如果某个环境需要单独配置，直接建 `.env.staging` 等文件即可，脚本里加 `"build:staging": "vue-tsc -b && vite build --mode staging"`。

## 接口调用

`src/utils/request.ts` 已封装好 baseURL、超时、拦截器、统一报错，接口层只写路径和业务参数：

```ts
// src/api/index.ts
import http from '@/utils/request'

// GET，返回的就是后端 data
export const getSessionList = () => http.get<SessionItem[]>('/session/list')

// POST
export const createDemo = (data: Record<string, any>) => http.post('/demo/create', data)

// 上传
export const uploadFile = (file: File) => http.upload('/file/upload/batch', file)
```

页面里直接用：

```ts
const list = await getSessionList()
```

几个约定：

- 路径不用写 `/api` 前缀，`baseURL` 已经统一加了
- 后端返回 `{ code, data, message }` 时会自动校验 `code`（0 和 200 视为成功），失败自动弹错误提示并 reject
- 后端直接返回数组或裸对象时，会跳过 code 校验，原样返回
- 网络异常 / 4xx / 5xx 都有对应的中文提示

如果后端的成功码不是 0 和 200，改 `request.ts` 里的 `SUCCESS_CODES`。

## 开发代理

开发时浏览器请求 `/api/xxx`，由 Vite 转发到 `VITE_PROXY_TARGET`，不产生跨域。
如果后端接口本身不带 `/api` 前缀，把 `vite.config.ts` 里 `rewrite` 那行注释打开。

生产环境默认也是 `/api`，由 Nginx 反代；若前后端不同域且后端已开 CORS，把 `.env.production` 的 `VITE_APP_BASE_API` 改成完整地址即可。

## 路由模式

由 `.env` 里的 `VITE_ROUTER_MODE` 控制，改完要重启 dev server 才生效（env 是启动时加载的）。

| 模式 | URL 形态 | 特点 |
| --- | --- | --- |
| `hash`（当前默认） | `http://localhost:3000/#/home` | 带 `#`，刷新和直接打开子路径都不会 404，丢到任何静态服务器上直接能跑，不需要服务端配置 |
| `history` | `http://localhost:3000/home` | URL 干净，但刷新子路径会 404，需要服务端把未匹配路径全部回退到 `index.html`（Nginx 配 `try_files $uri $uri/ /index.html;`） |

## 端口

`VITE_PORT` 默认 3000。若该端口已被占用，Vite 会自动顺延到 3001、3002……启动日志里会写明实际端口。
想让端口固定（占用时直接报错而不是悄悄换），在 `vite.config.ts` 的 `server` 里加 `strictPort: true`。

## 新增页面

1. 在 `src/views` 新建 `.vue` 文件
2. 在 `src/router/index.ts` 的 `routes` 里加一条（建议用懒加载 `() => import('@/views/XxxView.vue')`）
3. 需要菜单入口的话，加到 `src/App.vue` 的 `menus` 数组

## 已知事项

- Element Plus 目前是全量引入，打包约 1MB（gzip 340KB）。项目变大后想优化，可以换 `unplugin-vue-components` + `ElementPlusResolver` 按需引入，组件用法不用改。
- 未引入 Pinia（状态管理）和 ESLint/Prettier，需要的时候说一声，我给你加上。
