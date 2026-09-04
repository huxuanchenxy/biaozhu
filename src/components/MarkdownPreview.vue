<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { MdPreview } from 'md-editor-v3'
import 'md-editor-v3/lib/preview.css'
import type { Annotation } from '@/types/annotation'

const props = defineProps<{
  /** markdown 原文 */
  content: string
  /** 需要在预览里高亮的标注 */
  annotations: Annotation[]
}>()

const emit = defineEmits<{
  /** 用户在预览区鼠标划选了一段文本 */
  select: [text: string]
  /** 滚动位置变化，参数是 0~1 的比例 */
  scroll: [ratio: number]
}>()

const hostRef = ref<HTMLElement | null>(null)

/** 划选文本：交给右侧面板去打标签 */
function onMouseUp() {
  const text = window.getSelection()?.toString().trim() ?? ''
  if (text) emit('select', text)
}

/** ---------- 滚动同步 ---------- */

/** 当前滚动比例，0 顶部、1 底部 */
function getScrollRatio(el: HTMLElement | null) {
  if (!el) return 0
  const max = el.scrollHeight - el.clientHeight
  return max > 0 ? el.scrollTop / max : 0
}

function onScroll() {
  emit('scroll', getScrollRatio(hostRef.value))
}

/** 供父组件调用：按比例定位，用于与原文双向同步 */
function setScrollRatio(ratio: number) {
  const el = hostRef.value
  if (!el) return
  const max = el.scrollHeight - el.clientHeight
  el.scrollTop = Math.max(0, Math.min(max, ratio * max))
}

/** ---------- 标注高亮 ---------- */

/** 还原上一次注入的 <mark>，保证高亮可以反复重算 */
function unwrapMarks(root: HTMLElement) {
  root.querySelectorAll('mark.ann').forEach((m) => {
    const parent = m.parentNode
    if (!parent) return
    while (m.firstChild) parent.insertBefore(m.firstChild, m)
    parent.removeChild(m)
    parent.normalize()
  })
}

/** 把标注文本包成 <mark>，每条只高亮第一次出现的位置 */
function applyHighlight(root: HTMLElement) {
  for (const ann of props.annotations) {
    if (!ann.text) continue

    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        if (!node.nodeValue || !node.nodeValue.includes(ann.text)) return NodeFilter.FILTER_REJECT
        let p = node.parentElement
        while (p && p !== root) {
          if (p.tagName === 'MARK') return NodeFilter.FILTER_REJECT
          p = p.parentElement
        }
        return NodeFilter.FILTER_ACCEPT
      },
    })

    const targets: Text[] = []
    let n = walker.nextNode()
    while (n) {
      targets.push(n as Text)
      n = walker.nextNode()
    }

    for (const node of targets) {
      const idx = node.nodeValue?.indexOf(ann.text) ?? -1
      if (idx < 0) continue
      const range = document.createRange()
      range.setStart(node, idx)
      range.setEnd(node, idx + ann.text.length)
      const mark = document.createElement('mark')
      mark.className = 'ann'
      mark.dataset.id = String(ann.id)
      mark.style.backgroundColor = `${ann.color}26`
      mark.style.boxShadow = `inset 0 -2px 0 ${ann.color}`
      range.surroundContents(mark)
      break
    }
  }
}

function refreshHighlight() {
  const root = hostRef.value
  if (!root) return
  unwrapMarks(root)
  applyHighlight(root)
}

/** md-editor-v3 渲染完成的回调：渲染是异步的，必须等它吐出 html 再注入高亮 */
function onHtmlChanged() {
  nextTick(refreshHighlight)
}

/** 标注增删后重算高亮 */
watch(
  () => props.annotations,
  () => nextTick(refreshHighlight),
  { deep: true },
)

/** ---------- 图片兜底 ---------- */
/** 文档里的图片是相对路径，资源缺失时替换成占位，避免满屏碎图 */
function onImageError(e: Event) {
  const target = e.target as HTMLElement | null
  if (!target || target.tagName !== 'IMG') return
  const img = target as HTMLImageElement
  const placeholder = document.createElement('span')
  placeholder.className = 'img-missing'
  placeholder.textContent = `[图片缺失：${img.alt || img.getAttribute('src') || '未知'}]`
  img.replaceWith(placeholder)
}

onMounted(() => {
  hostRef.value?.addEventListener('error', onImageError, true)
  nextTick(refreshHighlight)
})

onBeforeUnmount(() => {
  hostRef.value?.removeEventListener('error', onImageError, true)
})

/** 供父组件调用：滚动定位到某条标注的高亮位置 */
function focus(id: number) {
  const el = hostRef.value?.querySelector<HTMLElement>(`mark.ann[data-id="${id}"]`)
  el?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

defineExpose({ focus, setScrollRatio })
</script>

<template>
  <div ref="hostRef" class="md-preview-host" @mouseup="onMouseUp" @scroll.passive="onScroll">
    <MdPreview
      :model-value="content"
      preview-theme="github"
      theme="light"
      language="zh-CN"
      :no-katex="true"
      :no-mermaid="true"
      :no-highlight="true"
      @on-html-changed="onHtmlChanged"
    />
  </div>
</template>

<style scoped>
.md-preview-host {
  flex: 1;
  min-width: 0;
  overflow: auto;
  background: #fff;
}

/* md-editor-v3 自带内边距，这里只铺满容器 */
.md-preview-host :deep(.md-editor-preview-wrapper) {
  padding: 16px 20px;
}
</style>

<style>
/* 高亮与图片占位是运行时注入的节点，不能加 scoped */
.md-preview-host mark.ann {
  padding: 1px 0;
  color: inherit;
  border-radius: 2px;
  cursor: pointer;
}

.md-preview-host .img-missing {
  display: inline-block;
  padding: 2px 6px;
  font-size: 12px;
  color: #a8abb2;
  background: #f5f7fa;
  border: 1px dashed #dcdfe6;
  border-radius: 3px;
}
</style>
