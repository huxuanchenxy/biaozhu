# 项目长期记忆

## 项目概况

G:\project\biaozhu —— 标注平台前端。Vue 3 + TypeScript + Vite 脚手架，2026-09-03 从零搭建。

## 约定

- 包管理器：npm（用户明确选 npm，虽本机也装了 pnpm）
- 路径别名：`@` -> `src`
- 环境变量命名：`VITE_APP_*` 业务前缀 + `VITE_PROXY_TARGET` / `VITE_PORT` / `VITE_OUT_DIR`；新增变量要同步更新根目录 `env.d.ts`
- 后端服务：http://10.89.34.77:8080（dev 通过 Vite 代理 /api 转发，生产走 Nginx 反代）
- UI 库：Element Plus（全量引入 + zh-cn 语言包 + 图标全量注册）
- 路由模式：默认 hash，由 `.env` 的 `VITE_ROUTER_MODE`（hash | history）控制；改 env 后必须重启 dev server
- 开发端口：`VITE_PORT` 默认 3000，本机该端口常被其他 node 进程占用，Vite 会顺延到 3001/3002，启动日志会写明实际端口

## HTTP 模块约定

- 统一走 `src/utils/request.ts`，接口声明集中在 `src/api/index.ts`，类型放 `src/api/types.ts`
- 成功码 `[0, 200]`；后端可能直接返回数组（如 /api/session/list），拦截器会自动跳过 code 校验透传，页面用 `unwrap()` 兼容两种结构
- axios 1.20 泛型坑：`service.get<any, any>(...) as Promise<T>`，不要写成 `service.get<any, T>()`

## 用户偏好（延续自其他项目）

- 改动增量式推进，一次只提一项
- 调试信息优先输出到 console，不占用 UI
- 有实时接口时不保留静态兜底数据
- 空数据直接隐藏对应标题/标签，不展示空 heading
