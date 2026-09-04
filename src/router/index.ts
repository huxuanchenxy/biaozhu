import {
  createRouter,
  createWebHashHistory,
  createWebHistory,
  type RouteRecordRaw,
} from 'vue-router'

/** 路由表：新增页面在这里注册即可 */
const routes: RouteRecordRaw[] = [
  {
    path: '/',
    redirect: '/home',
  },
  {
    path: '/home',
    name: 'Home',
    component: () => import('@/views/HomeView.vue'),
    meta: { title: '首页' },
  },
  {
    path: '/about',
    name: 'About',
    component: () => import('@/views/AboutView.vue'),
    meta: { title: '关于' },
  },
  {
    path: '/markdown',
    name: 'MarkdownAnnotator',
    component: () => import('@/views/MarkdownAnnotatorView.vue'),
    /** hideNav: 保留平台抬头，但隐藏首页/关于等导航 tab */
    meta: { title: 'Markdown 标注', hideNav: true },
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'NotFound',
    component: () => import('@/views/NotFoundView.vue'),
    meta: { title: '页面不存在' },
  },
]

/**
 * 路由模式，由 .env 的 VITE_ROUTER_MODE 控制：
 *  - hash    （默认）URL 形如 http://localhost:3000/#/home，刷新/直开子路径都不会 404，部署无需服务端配置
 *  - history URL 形如 http://localhost:3000/home，干净好看，但刷新子路径需要服务端把所有路径回退到 index.html
 */
const useHashMode = import.meta.env.VITE_ROUTER_MODE !== 'history'

const router = createRouter({
  history: useHashMode
    ? createWebHashHistory(import.meta.env.BASE_URL)
    : createWebHistory(import.meta.env.BASE_URL),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

/** 切换页面时更新浏览器标签标题 */
router.afterEach((to) => {
  const title = (to.meta.title as string) || ''
  const appTitle = import.meta.env.VITE_APP_TITLE || ''
  document.title = title ? `${title} - ${appTitle}` : appTitle
})

export default router
