/**
 * 运行时主题切换（dark / light）。
 *
 * 主题状态全部落在 <html> 上：`dark` 类（驱动 styles/index.css 变量与 Element Plus 深色变量）、
 * `data-theme` 属性、以及 `meta[name=color-scheme]`（控制浏览器 UA 控件，如内置 PDF 查看器外壳）。
 * 首屏由 index.html 的引导脚本（dev 由 Vite 插件、prod 由 server.mjs 注入）提前打标避免闪烁；
 * 该脚本与本模块共用同一 localStorage 键，用户切换后刷新仍保持所选主题，未切换过则回退到 APP_THEME 配置默认。
 */

export type ThemeMode = 'dark' | 'light'

/** 与引导脚本保持一致的持久化键 */
export const THEME_STORAGE_KEY = 'app-theme'

/** 把主题应用到 <html>（不写 localStorage）：dark 类 + data-theme + color-scheme */
export function applyTheme(mode: ThemeMode): void {
  const d = document.documentElement
  d.classList.toggle('dark', mode === 'dark')
  d.setAttribute('data-theme', mode)
  let m = document.querySelector('meta[name="color-scheme"]')
  if (!m) {
    m = document.createElement('meta')
    m.setAttribute('name', 'color-scheme')
    document.head.appendChild(m)
  }
  // light 用 'light only' 阻止系统深色把内置 PDF 查看器外壳渲染成黑；dark 用 'dark'
  m.setAttribute('content', mode === 'dark' ? 'dark' : 'light only')
}

/** 当前生效主题（以 <html> 上的 dark 类为准，由引导脚本/上次切换设定） */
export function currentTheme(): ThemeMode {
  return document.documentElement.classList.contains('dark') ? 'dark' : 'light'
}

/** 设置主题并持久化（persist=false 时仅应用、不写入 localStorage） */
export function setTheme(mode: ThemeMode, persist = true): void {
  if (persist) {
    try {
      localStorage.setItem(THEME_STORAGE_KEY, mode)
    } catch {
      /* 隐私模式等写入失败时忽略，仅本次会话生效 */
    }
  }
  applyTheme(mode)
}

/** 在 dark / light 间切换，返回切换后的主题 */
export function toggleTheme(): ThemeMode {
  const next: ThemeMode = currentTheme() === 'dark' ? 'light' : 'dark'
  setTheme(next)
  return next
}

/** 清除用户选择，交回 APP_THEME 配置默认（下次刷新由引导脚本按配置决定） */
export function clearThemeChoice(): void {
  try {
    localStorage.removeItem(THEME_STORAGE_KEY)
  } catch {
    /* 忽略 */
  }
}
