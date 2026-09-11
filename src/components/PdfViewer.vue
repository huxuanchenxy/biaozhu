<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import type { ComponentPublicInstance } from 'vue'
import * as pdfjsLib from 'pdfjs-dist'
// Vite 把 pdf.js worker 打包成独立模块（?worker），避免用 CDN 路径
import PdfWorker from 'pdfjs-dist/build/pdf.worker.min.mjs?worker'
import type { PDFDocumentProxy, PDFPageProxy } from 'pdfjs-dist'

pdfjsLib.GlobalWorkerOptions.workerPort = new PdfWorker()

/**
 * 自研 PDF 查看器：用 pdf.js 把每一页渲染到 <canvas>，页面纵向排在普通
 * div 滚动容器里。之所以不用浏览器原生 <iframe> PDF 阅读器，是因为原生
 * 阅读器（尤其 Chrome）的滚动发生在插件内部，父页面既监听不到也无法
 * 程序化设置滚动位置，无法与右侧 MD 原文做滚动联动。
 */
const props = defineProps<{
  /** PDF 地址（blob URL） */
  src: string
}>()

const emit = defineEmits<{
  /** 滚动位置变化：top=scrollTop，view=clientHeight，full=scrollHeight */
  (e: 'scroll', payload: { top: number; view: number; full: number }): void
}>()

interface PageInfo {
  /** 1 基页码 */
  num: number
  /** scale=1 时的原始尺寸（磅） */
  baseWidth: number
  baseHeight: number
  /** 当前按容器宽度算出的缩放比 */
  scale: number
  rendered: boolean
  rendering: boolean
}

type LoadStatus = 'loading' | 'ready' | 'error'

const scrollRef = ref<HTMLElement | null>(null)
const pageWrappers = ref<HTMLElement[]>([])
const pages = ref<PageInfo[]>([])
const status = ref<LoadStatus>('loading')
const errorMsg = ref('')

/** 页与容器两侧留白（页间距 PAGE_GAP 直接写在 <style> 里） */
const SIDE_PAD = 10

let doc: PDFDocumentProxy | null = null
/** 已加载的页对象缓存，避免重复 getPage */
const pageCache = new Map<number, PDFPageProxy>()
/** 当前布局使用的容器内容宽度；宽度变化超过阈值才重新布局 */
let layoutWidth = 0
/** 每次换文档 / 重布局自增，使旧的异步渲染结果失效 */
let renderToken = 0
let observer: IntersectionObserver | null = null
let resizeObserver: ResizeObserver | null = null
let layoutTimer: number | undefined

function setWrapRef(el: Element | ComponentPublicInstance | null, idx: number) {
  if (el instanceof HTMLElement) pageWrappers.value[idx] = el
}

/** 释放旧文档与渲染任务，清空页面（换文档或卸载时调用） */
function reset() {
  renderToken++
  window.clearTimeout(layoutTimer)
  observer?.disconnect()
  observer = null
  pageCache.clear()
  pages.value = []
  pageWrappers.value = []
  if (doc) {
    doc.destroy().catch(() => undefined)
    doc = null
  }
}

/** 加载 PDF，逐页取原始尺寸用于布局，真正画 canvas 交给 IntersectionObserver 懒渲染 */
async function load() {
  reset()
  if (!props.src) return
  status.value = 'loading'
  errorMsg.value = ''
  const token = renderToken
  try {
    const task = pdfjsLib.getDocument({ url: props.src })
    const d = await task.promise
    if (token !== renderToken) {
      d.destroy().catch(() => undefined)
      return
    }
    doc = d
    const infos: PageInfo[] = []
    for (let i = 1; i <= d.numPages; i++) {
      const page = await d.getPage(i)
      const vp = page.getViewport({ scale: 1 })
      pageCache.set(i, page)
      infos.push({
        num: i,
        baseWidth: vp.width,
        baseHeight: vp.height,
        scale: 1,
        rendered: false,
        rendering: false,
      })
    }
    if (token !== renderToken) return
    pages.value = infos
    // 必须先切到 ready：模板里 .pdf-pages 与各页 wrapper 位于 v-else（status==='ready'）分支，
    // 只有 ready 后它们才会挂载、setWrapRef 才会填好 pageWrappers。否则 applyLayout 面对的是
    // 空 wrapper 列表——高度设不上、IntersectionObserver 也观察不到任何页，首屏一片空白，
    // 直到下一次 resize（如切分栏）触发重排才渲染出来。
    status.value = 'ready'
    await nextTick()
    applyLayout(token)
  } catch (e: any) {
    if (token === renderToken) {
      status.value = 'error'
      errorMsg.value = `PDF 渲染失败：${e?.message ?? '未知错误'}`
    }
  }
}

/**
 * 按容器当前宽度计算每页缩放比与占位高度，重置已渲染内容并重新观察。
 * 宽度为 0（容器 display:none，例如「原文」模式）时跳过，等显示出来
 * ResizeObserver 会再触发。
 */
function applyLayout(token: number) {
  const host = scrollRef.value
  if (!host || !pages.value.length) return
  const width = host.clientWidth - SIDE_PAD * 2
  if (width <= 0) return
  // 重排前记下滚动比例：重排后页面总高会变，保持「停留在同一相对位置」
  const prevMax = host.scrollHeight - host.clientHeight
  const prevRatio = prevMax > 0 ? host.scrollTop / prevMax : 0
  layoutWidth = width
  pages.value.forEach((info, idx) => {
    info.scale = width / info.baseWidth
    const wrap = pageWrappers.value[idx]
    if (wrap) wrap.style.height = `${Math.round(info.baseHeight * info.scale)}px`
    info.rendered = false
    info.rendering = false
  })
  pageWrappers.value.forEach((wrap) => wrap.replaceChildren())
  // 恢复相对位置（此时新高度已生效）；会派发 scroll，父组件据此把另一栏对齐
  const newMax = host.scrollHeight - host.clientHeight
  host.scrollTop = prevRatio * newMax
  observePages(token)
}

function scheduleLayout() {
  window.clearTimeout(layoutTimer)
  layoutTimer = window.setTimeout(() => {
    const host = scrollRef.value
    if (!host || !pages.value.length) return
    const width = host.clientWidth - SIDE_PAD * 2
    // 宽度没变（可能只是高度变化）或仍不可见，不重新布局
    if (width <= 0 || Math.abs(width - layoutWidth) < 2) return
    const token = ++renderToken
    applyLayout(token)
  }, 200)
}

/** 懒渲染：只画视口附近（上下各 800px）的页面，滚到再画，避免大文档一次性卡死 */
function observePages(token: number) {
  observer?.disconnect()
  if (!scrollRef.value) return
  observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue
        const idx = Number((entry.target as HTMLElement).dataset.idx)
        observer?.unobserve(entry.target)
        void renderPage(idx, token)
      }
    },
    { root: scrollRef.value, rootMargin: '800px 0px 800px 0px', threshold: 0 },
  )
  pageWrappers.value.forEach((wrap) => observer?.observe(wrap))
}

async function renderPage(idx: number, token: number) {
  const info = pages.value[idx]
  const wrap = pageWrappers.value[idx]
  if (!info || !wrap || info.rendered || info.rendering || info.scale <= 0 || !doc) return
  info.rendering = true
  try {
    const page = pageCache.get(info.num) ?? (await doc.getPage(info.num))
    if (token !== renderToken) return
    pageCache.set(info.num, page)
    // 高分屏放大画布像素（上限 2 倍，控制内存），CSS 尺寸保持逻辑像素
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    const viewport = page.getViewport({ scale: info.scale })
    const canvas = document.createElement('canvas')
    canvas.width = Math.round(viewport.width * dpr)
    canvas.height = Math.round(viewport.height * dpr)
    canvas.style.width = `${Math.round(viewport.width)}px`
    canvas.style.height = `${Math.round(viewport.height)}px`
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    const task = page.render({
      canvasContext: ctx,
      viewport,
      transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined,
    })
    await task.promise
    // 期间换了文档 / 重布局，丢弃这次结果
    if (token !== renderToken) return
    wrap.replaceChildren(canvas)
    info.rendered = true
  } catch (e: any) {
    // 重布局 / 销毁文档会取消在途渲染，属于正常情况
    if (e?.name !== 'RenderingCancelledException' && token === renderToken) {
      wrap.textContent = `第 ${info.num} 页渲染失败`
      wrap.classList.add('pdf-page--error')
    }
  } finally {
    if (info && token === renderToken) info.rendering = false
  }
}

function onScroll() {
  const el = scrollRef.value
  if (!el) return
  emit('scroll', { top: el.scrollTop, view: el.clientHeight, full: el.scrollHeight })
}

/** 供父组件做联动：按 0~1 的滚动比例定位 */
function scrollToRatio(ratio: number) {
  const el = scrollRef.value
  if (!el) return
  const max = el.scrollHeight - el.clientHeight
  el.scrollTop = Math.max(0, Math.min(max, max * ratio))
}

defineExpose({ scrollToRatio })

watch(
  () => props.src,
  () => void load(),
  { immediate: true },
)

// ResizeObserver：预览↔分栏切换或窗口缩放导致容器宽度变化时，防抖重布局
if (typeof ResizeObserver !== 'undefined') {
  resizeObserver = new ResizeObserver(scheduleLayout)
}
watch(scrollRef, (el, oldEl) => {
  if (oldEl) resizeObserver?.unobserve(oldEl)
  if (el) resizeObserver?.observe(el)
})

onBeforeUnmount(() => {
  reset()
  resizeObserver?.disconnect()
})
</script>

<template>
  <div ref="scrollRef" class="pdf-scroll" @scroll="onScroll">
    <div v-if="status === 'error'" class="pdf-state pdf-state--error">{{ errorMsg }}</div>
    <div v-else-if="status === 'loading'" class="pdf-state">
      <el-icon class="is-loading pdf-state-icon"><Loading /></el-icon>
      PDF 加载中…
    </div>
    <div v-else class="pdf-pages">
      <div
        v-for="(p, idx) in pages"
        :key="p.num"
        class="pdf-page"
        :data-idx="idx"
        :ref="(el) => setWrapRef(el, idx)"
      />
    </div>
  </div>
</template>

<style scoped>
.pdf-scroll {
  flex: 1;
  min-height: 0;
  overflow: auto;
  background: #e9eaee;
}

/* scoped 中无法引用 JS 常量，这里与 SIDE_PAD / PAGE_GAP 保持一致（10px / 12px） */
.pdf-pages {
  padding: 10px;
}

.pdf-page {
  width: 100%;
  margin-bottom: 12px;
  background: #fff;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.18);
  overflow: hidden;
}

.pdf-page:last-child {
  margin-bottom: 10px;
}

.pdf-page canvas {
  display: block;
  margin: 0 auto;
}

.pdf-page--error {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  color: #f56c6c;
}

.pdf-state {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  height: 100%;
  font-size: 13px;
  color: #606266;
}

.pdf-state--error {
  padding: 16px;
  color: #f56c6c;
}

.pdf-state-icon {
  font-size: 16px;
}
</style>
