<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import type { ComponentPublicInstance } from 'vue'

/**
 * 基于 MuPDF(WASM) 的自研 PDF 查看器：把每一页渲染成 PNG 画到 <img>，页面纵向排在
 * 普通 div 滚动容器里，背景由我们完全控制（纯白）。
 *
 * 为什么不用浏览器内置查看器（PdfViewerNative）：内置查看器的深浅色跟随系统主题，
 * 网页代码改不动，深色系统下工具栏与四周画布发黑。
 * 为什么不用 pdf.js（PdfViewer）：这类老标准 PDF 的中文字体未内嵌、字体名损坏，
 * pdf.js 即便配了 cMap 也只能解码文字、画不出中文字形（缺字/乱码）。
 * MuPDF 自带 CJK 字体回退，能正确渲染中文，且渲染结果是我们自己的图片，主题可控，
 * 滚动也在我们自己的容器里 → 可与译文联动。
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
  /** 页宽（磅，scale=1） */
  baseWidth: number
  baseHeight: number
  /** 当前按容器宽度算出的缩放比（css 像素 / 磅） */
  scale: number
  rendered: boolean
  rendering: boolean
  /** 该页已生成的 PNG 对象 URL（重排/卸载时释放） */
  url: string
}

const scrollRef = ref<HTMLElement | null>(null)
const pageWrappers = ref<HTMLElement[]>([])
const pages = ref<PageInfo[]>([])
const status = ref<'loading' | 'ready' | 'error'>('loading')
const errorMsg = ref('')

/** 页与容器两侧留白（与 <style> 里的 padding / margin 保持一致） */
const SIDE_PAD = 10

// mupdf 通过动态 import 惰性加载（含 ~10MB wasm），避免拖慢首屏；类型宽松即可
let mupdf: any = null
let doc: any = null
const pageCache = new Map<number, any>()
let layoutWidth = 0
let renderToken = 0
let observer: IntersectionObserver | null = null
let resizeObserver: ResizeObserver | null = null
let layoutTimer: number | undefined

function setWrapRef(el: Element | ComponentPublicInstance | null, idx: number) {
  if (el instanceof HTMLElement) pageWrappers.value[idx] = el
}

/** 释放旧文档、图片对象 URL 与渲染任务，清空页面 */
function reset() {
  renderToken++
  window.clearTimeout(layoutTimer)
  observer?.disconnect()
  observer = null
  pageCache.forEach((p) => {
    try {
      p.destroy?.()
    } catch {
      /* ignore */
    }
  })
  pageCache.clear()
  pages.value.forEach((p) => p.url && URL.revokeObjectURL(p.url))
  pages.value = []
  pageWrappers.value = []
  if (doc) {
    try {
      doc.destroy?.()
    } catch {
      /* ignore */
    }
    doc = null
  }
}

/** 加载 PDF：取字节 → openDocument → 逐页取尺寸用于布局，真正画 PNG 交给 IntersectionObserver */
async function load() {
  reset()
  if (!props.src) return
  status.value = 'loading'
  errorMsg.value = ''
  const token = renderToken
  try {
    if (!mupdf) mupdf = await import('mupdf')
    const buf = await (await fetch(props.src)).arrayBuffer()
    if (token !== renderToken) return
    doc = mupdf.Document.openDocument(new Uint8Array(buf), 'application/pdf')
    const n = doc.countPages()
    const infos: PageInfo[] = []
    for (let i = 0; i < n; i++) {
      const page = doc.loadPage(i)
      const [x0, y0, x1, y1] = page.getBounds()
      infos.push({
        num: i + 1,
        baseWidth: x1 - x0,
        baseHeight: y1 - y0,
        scale: 1,
        rendered: false,
        rendering: false,
        url: '',
      })
      page.destroy?.()
    }
    if (token !== renderToken) return
    pages.value = infos
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
 * 按容器当前宽度算每页缩放与占位高度，清掉旧图并重新观察。
 * 宽度为 0（容器隐藏）时跳过，等显示出来 ResizeObserver 再触发。
 */
function applyLayout(token: number) {
  const host = scrollRef.value
  if (!host || !pages.value.length) return
  const width = host.clientWidth - SIDE_PAD * 2
  if (width <= 0) return
  const prevMax = host.scrollHeight - host.clientHeight
  const prevRatio = prevMax > 0 ? host.scrollTop / prevMax : 0
  layoutWidth = width
  pages.value.forEach((info, idx) => {
    info.scale = width / info.baseWidth
    const wrap = pageWrappers.value[idx]
    if (wrap) wrap.style.height = `${Math.round(info.baseHeight * info.scale)}px`
    info.rendered = false
    info.rendering = false
    if (info.url) {
      URL.revokeObjectURL(info.url)
      info.url = ''
    }
    if (wrap) wrap.replaceChildren()
  })
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
    if (width <= 0 || Math.abs(width - layoutWidth) < 2) return
    applyLayout(++renderToken)
  }, 200)
}

/** 懒渲染：只画视口附近（上下各 800px）的页，滚到再画，避免大文档一次卡死 */
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
    // 让转圈/滚动先绘制一帧，再做同步的 WASM 渲染（toPixmap 会短暂阻塞主线程）
    await new Promise((r) => setTimeout(r, 0))
    if (token !== renderToken) return
    let page = pageCache.get(idx)
    if (!page) {
      page = doc.loadPage(idx)
      pageCache.set(idx, page)
    }
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    const s = info.scale * dpr
    const matrix = mupdf.Matrix.scale(s, s)
    const pix = page.toPixmap(matrix, mupdf.ColorSpace.DeviceRGB, false)
    const png = pix.asPNG()
    pix.destroy?.()
    if (token !== renderToken) return
    const url = URL.createObjectURL(new Blob([png], { type: 'image/png' }))
    const img = document.createElement('img')
    img.src = url
    img.style.cssText = 'width:100%;display:block;margin:0 auto'
    img.onload = () => {
      if (info.url && info.url !== url) URL.revokeObjectURL(info.url)
      info.url = url
    }
    img.onerror = () => URL.revokeObjectURL(url)
    wrap.replaceChildren(img)
    info.rendered = true
  } catch (e: any) {
    console.error('[PdfViewerMupdf] 渲染失败', e)
    if (token === renderToken) {
      wrap.textContent = `第 ${info.num} 页渲染失败：${e?.message ?? '未知错误'}`
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
  /* 视口外围随主题变色（深色时为深），MuPDF 渲染的页面本体仍为白底 */
  background: var(--c-bg-stage);
}

.pdf-pages {
  padding: 10px;
}

.pdf-page {
  width: 100%;
  margin-bottom: 12px;
  background: #fff;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.12);
  overflow: hidden;
}

.pdf-page:last-child {
  margin-bottom: 10px;
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
  color: var(--c-text-regular);
}

.pdf-state--error {
  padding: 16px;
  color: #f56c6c;
}

.pdf-state-icon {
  font-size: 16px;
}
</style>
