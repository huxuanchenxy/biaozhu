<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { MdPreview } from 'md-editor-v3'
import 'md-editor-v3/lib/preview.css'
import { getDocJson, getMinioDocJson, saveDocJson } from '@/api'
import type { DocJsonRecord } from '@/api/types'
import { deriveAnnotationKeys, derivePdfKey, getObjectBlobUrl, getObjectText } from '@/utils/minio'
import { TranslationEngine } from '@/utils/translator'

/**
 * 待标注文档地址。
 * 优先从 URL 路由参数取 MinIO 对象路径（/markdown/<key>）；
 * 没有带 key 时回退到 .env 里 VITE_APP_DOC_URL 指向的本地文件。
 */
const DOC_URL = import.meta.env.VITE_APP_DOC_URL || '/doc/BE1020801A3.md'

const route = useRoute()

/** 当前路由携带的 MinIO 对象 key（:key(.*) 一般为字符串，兼容数组情况） */
const docKey = computed(() => {
  const raw = route.params.key
  const key = Array.isArray(raw) ? raw.join('/') : String(raw ?? '')
  return key.trim()
})

/**
 * 由 md 的 key 推导三个标注 json 的 MinIO key；无 docKey（回退本地）时为 null。
 * 规则见 utils/minio 的 deriveAnnotationKeys：QA/SFT/CoT 与 MD 同级，文件名同 md 主名加后缀。
 */
const annotationKeys = computed(() => (docKey.value ? deriveAnnotationKeys(docKey.value) : null))

/**
 * 由 md 的 key 推导原始 PDF 的 key（PDF 与装 MD 的目录同级、文档名 + .pdf）；无 docKey 时为空串。
 * 规则见 utils/minio 的 derivePdfKey，可用 .env 的 VITE_DOC_PDF_PATTERN 覆盖。
 */
const pdfKey = computed(() => (docKey.value ? derivePdfKey(docKey.value) : ''))

/**
 * 左栏展示模式：
 *   preview 预览——显示由 md 路径推导出的原始 PDF（浏览器原生阅读器）；
 *   split   分栏——PDF 与 MD 源码并排，各自独立滚动（PDF 在 iframe 内，无法与源码同步）；
 *   source  原文——只读查看 MD 源码。
 * 翻译为「选中一段再译」（见下方选段翻译）；PDF 内划选不触发翻译，MD 源码与右侧标注可。
 */
type ViewMode = 'preview' | 'split' | 'source'
const viewMode = ref<ViewMode>('preview')

/* ------------------------------------------------------------------
 * 已注释：可编辑的文档内容（当前只需要预览 + 只读看原文）
 * ------------------------------------------------------------------ */
// const DEFAULT_DOC = `# 标注说明
// ...
// `

const content = ref('')
const loading = ref(false)
const loadError = ref('')

/** PDF 预览：blob 对象 URL（供 <iframe> 直接渲染）、加载态、错误信息 */
const pdfUrl = ref('')
const pdfLoading = ref(false)
const pdfError = ref('')

/** 拉取文档：URL 带 key 走 MinIO，否则读本地 VITE_APP_DOC_URL；页面其它部分不用动 */
async function loadDoc() {
  loading.value = true
  loadError.value = ''
  content.value = ''
  let text = ''
  try {
    if (docKey.value) {
      text = await getObjectText(docKey.value)
    } else {
      const res = await fetch(DOC_URL)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      text = await res.text()
    }
  } catch (e: any) {
    loadError.value = `文档加载失败：${e?.message ?? '未知错误'}`
    loading.value = false
    return
  }
  // 先让「加载中」转圈真正绘制到屏幕上（双 rAF 保证完成一次 paint），
  // 再塞入大文档触发渲染；否则大文件渲染会阻塞主线程，转圈来不及显示就白屏。
  await nextTick()
  await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))
  content.value = text
  // 等预览渲染完成后再收转圈，避免渲染期间白屏无反馈
  await nextTick()
  loading.value = false
}

/** 拉取原始 PDF：由 md 的 key 推导 pdf key，取二进制生成 blob URL 供 <iframe> 预览 */
async function loadPdf() {
  // 切换文档前先释放上一次的 blob URL，避免内存泄漏
  if (pdfUrl.value) {
    URL.revokeObjectURL(pdfUrl.value)
    pdfUrl.value = ''
  }
  pdfError.value = ''
  if (!pdfKey.value) {
    // 本地回退模式（URL 没带对象 key）没有对应 PDF，给出可操作提示
    pdfError.value = '本地模式无对应 PDF：请在 URL 带上 MinIO 对象路径（/markdown/<对象key>）'
    return
  }
  pdfLoading.value = true
  try {
    pdfUrl.value = await getObjectBlobUrl(pdfKey.value)
  } catch (e: any) {
    pdfError.value = `PDF 加载失败：${e?.message ?? '未知错误'}`
  } finally {
    pdfLoading.value = false
  }
}

loadDoc()
loadPdf()

/** ---------- 标注数据（Q&A 标签页） ---------- */

/**
 * 三个标签页的字段映射：每个 json 只取两个字段按一问一答展示，
 * 顺序为（问槽位, 答槽位），其余字段隐藏。
 */
interface QaTab {
  key: string
  label: string
  /** public/doc 下的本地回退 json 文件名（无 docKey 时用） */
  file: string
  /** 对应 annotationKeys 里的字段：MinIO 动态读取时用它取推导出的 key */
  minioField: 'qa' | 'alpaca' | 'cot'
  /** 问槽位展示的字段 */
  qField: string
  /** 答槽位展示的字段 */
  aField: string
  status: 'loading' | 'ok' | 'error'
  error: string
  records: DocJsonRecord[]
}

const tabs = ref<QaTab[]>([
  {
    key: 'alpaca',
    label: 'SFT',
    file: 'BE1020801A3_alpaca.json',
    minioField: 'alpaca',
    qField: 'instruction',
    aField: 'output',
    status: 'loading',
    error: '',
    records: [],
  },
  {
    key: 'cot',
    label: 'COT',
    file: 'BE1020801A3_cot.json',
    minioField: 'cot',
    qField: 'question',
    aField: 'scenario',
    status: 'loading',
    error: '',
    records: [],
  },
  {
    key: 'qa',
    label: 'QA',
    file: 'BE1020801A3_qa.json',
    minioField: 'qa',
    qField: 'question',
    aField: 'answer',
    status: 'loading',
    error: '',
    records: [],
  },
])

const activeTabKey = ref(tabs.value[0].key)
const activeTab = computed(
  () => tabs.value.find((t) => t.key === activeTabKey.value) ?? tabs.value[0],
)

/** 加载单个标签页：URL 带 key 走 MinIO（按 md 名推导 json key），否则读本地 json */
function loadTab(tab: QaTab) {
  tab.status = 'loading'
  tab.error = ''
  const keys = annotationKeys.value
  const source = keys ? keys[tab.minioField] : tab.file
  const task = keys ? getMinioDocJson(keys[tab.minioField]) : getDocJson(tab.file)
  task
    .then((records) => {
      tab.records = records
      tab.status = 'ok'
    })
    .catch((e: any) => {
      tab.records = []
      tab.error = `${source} 加载失败：${e?.message ?? '未知错误'}`
      tab.status = 'error'
    })
}

tabs.value.forEach(loadTab)

/** ---------- 前端分页 ---------- */
const currentPage = ref(1)
const pageSize = ref(5)

/** 当前页的全局 0 基起始下标：用于卡片编号（标签名_n）与定位编辑项 */
const pageStart = computed(() => (currentPage.value - 1) * pageSize.value)

const pagedRecords = computed(() =>
  activeTab.value.records.slice(pageStart.value, pageStart.value + pageSize.value),
)

/** ---------- Q&A 编辑 ---------- */
/** 正在编辑的条目全局下标（0 基），-1 表示没在编辑 */
const editingIndex = ref(-1)
const editQ = ref('')
const editA = ref('')
const saving = ref(false)

/** 切标签页回到第 1 页；切标签页/翻页都放弃进行中的编辑 */
watch(activeTabKey, () => {
  currentPage.value = 1
  editingIndex.value = -1
})
watch([currentPage, pageSize], () => {
  editingIndex.value = -1
})

/**
 * URL 上的对象路径变化时：md 与三个标注 json 一起重新拉取，
 * 并回到第 1 页、放弃进行中的编辑（同一组件复用不重新挂载，故手动重载）。
 */
watch(docKey, () => {
  currentPage.value = 1
  editingIndex.value = -1
  loadDoc()
  loadPdf()
  tabs.value.forEach(loadTab)
})

function fieldText(rec: DocJsonRecord, field: string) {
  const v = rec?.[field]
  return v == null ? '' : String(v)
}

function startEdit(index: number) {
  const rec = activeTab.value.records[index]
  if (!rec) return
  editingIndex.value = index
  editQ.value = fieldText(rec, activeTab.value.qField)
  editA.value = fieldText(rec, activeTab.value.aField)
}

function cancelEdit() {
  editingIndex.value = -1
}

/** 保存单条 Q&A：写回当前标签页数据后，MinIO 模式调 uploadOverwrite 上传整个标签页 json */
async function saveEdit() {
  const tab = activeTab.value
  const rec = tab.records[editingIndex.value]
  if (!rec) {
    editingIndex.value = -1
    return
  }
  rec[tab.qField] = editQ.value
  rec[tab.aField] = editA.value
  saving.value = true
  try {
    const keys = annotationKeys.value
    await saveDocJson(tab.file, tab.records, keys ? keys[tab.minioField] : undefined)
    ElMessage.success(keys ? '已保存' : '已保存（本地模式，未上传）')
    editingIndex.value = -1
  } catch (e: any) {
    ElMessage.error(`保存失败：${e?.message ?? '未知错误'}`)
  } finally {
    saving.value = false
  }
}

/* ------------------------------------------------------------------
 * 选段翻译：在 MD 源码或右侧标注卡片里划选一段 → 选区上方浮现「翻译」按钮 → 弹窗展示译文。
 * 取代原来的整篇翻译（太慢），只把选中片段提交给 Dify。
 * 注：PDF 预览在 <iframe> 内，选区不被父页捕获，故 PDF 上划选不触发翻译。
 * ------------------------------------------------------------------ */
const engine = new TranslationEngine()
/** 当前划选的文本 */
const selText = ref('')
/** 浮动「翻译」按钮位置（fixed 视口坐标）与显隐 */
const selBtn = ref({ show: false, x: 0, y: 0 })
/** 译文弹窗 */
const transDialog = ref(false)
const transState = ref<'loading' | 'done' | 'error'>('loading')
const transResult = ref('')
const transError = ref('')

/**
 * 触发翻译按钮的选区容器：
 *   - 左：文档面板（预览 / 原文）
 *   - 右：标注面板（SFT / COT / QA 三个 json 的问答卡片）
 * 只有落在这些面板内部的选区才会浮现「翻译」按钮。
 */
const leftPaneRef = ref<HTMLElement | null>(null)
const rightPaneRef = ref<HTMLElement | null>(null)

/** 隐藏浮动按钮（滚动 / 选区清空 / 开始翻译时） */
function hideSelBtn() {
  selBtn.value.show = false
}

/** 点击浮动按钮 / 重试：把选中片段提交 Dify 翻译 */
async function translateSelection() {
  const text = selText.value
  if (!text) return
  hideSelBtn()
  // 清除 DOM 选区：否则弹窗内点击（如「复制译文」）触发的全局 mouseup
  // 会因选区仍在而重新弹出「翻译」按钮。selText 已存下，不影响翻译。
  window.getSelection()?.removeAllRanges()
  transDialog.value = true
  transState.value = 'loading'
  transResult.value = ''
  transError.value = ''
  try {
    transResult.value = await engine.translate(text)
    transState.value = 'done'
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    transState.value = 'error'
    transError.value = e?.message ?? '翻译失败'
  }
}

async function copyTranslation() {
  const text = transResult.value
  if (!text) return
  try {
    // 优先用异步 Clipboard API（仅 https / localhost 等安全上下文可用）；
    // 内网 http 部署时 navigator.clipboard 为 undefined，回退到 execCommand('copy')。
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
    } else {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.top = '-9999px'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.focus()
      ta.select()
      const ok = document.execCommand('copy')
      document.body.removeChild(ta)
      if (!ok) throw new Error('execCommand copy failed')
    }
    ElMessage.success('译文已复制')
  } catch {
    ElMessage.error('复制失败，请手动选中译文复制')
  }
}

/**
 * 全局 mouseup：在左侧文档面板里用左键划选到文字，就在选区上方浮现「翻译」按钮；
 * 选区为空或不在文档面板内则收起。纯左键划选触发，不涉及右键（避免默认右键菜单）。
 */
function onDocMouseUp() {
  // 译文弹窗打开时不处理（弹窗内的点击不应触发浮动按钮）
  if (transDialog.value) return
  const sel = window.getSelection()
  const text = sel?.toString().trim() ?? ''
  if (!text || !sel || sel.rangeCount === 0) {
    hideSelBtn()
    return
  }
  const range = sel.getRangeAt(0)
  const startEl =
    range.startContainer instanceof Element ? range.startContainer : range.startContainer.parentElement
  // 左侧文档面板或右侧标注面板内的选区都触发翻译（复用同一个 Dify 引擎与弹窗）
  const inPane =
    !!startEl &&
    ((leftPaneRef.value?.contains(startEl) ?? false) ||
      (rightPaneRef.value?.contains(startEl) ?? false))
  if (!inPane) {
    hideSelBtn()
    return
  }
  selText.value = text
  const rect = range.getBoundingClientRect()
  const x = Math.min(Math.max(rect.left + rect.width / 2 - 28, 8), window.innerWidth - 72)
  const y = Math.max(rect.top - 36, 8)
  selBtn.value = { show: true, x, y }
}

onMounted(() => document.addEventListener('mouseup', onDocMouseUp))
onBeforeUnmount(() => {
  document.removeEventListener('mouseup', onDocMouseUp)
  engine.destroy()
  // 释放 PDF 的 blob 对象 URL
  if (pdfUrl.value) URL.revokeObjectURL(pdfUrl.value)
})
</script>

<template>
  <div class="md-page">
    <!-- 左：2/3 -->
    <section class="pane pane-left" ref="leftPaneRef">
      <header class="pane-head">
        <span class="pane-title">文档</span>
        <span v-if="loading" class="hint">加载中…</span>
        <el-radio-group v-model="viewMode" size="small">
          <el-radio-button value="preview">预览</el-radio-button>
          <el-radio-button value="split">分栏</el-radio-button>
          <el-radio-button value="source">原文</el-radio-button>
        </el-radio-group>
      </header>

      <div
        v-loading="loading || pdfLoading"
        element-loading-text="文档加载中…"
        class="pane-body doc-body"
      >
        <el-alert v-if="loadError" :title="loadError" type="error" show-icon :closable="false" />

        <!-- 已注释：编辑区（当前只需要预览） -->
        <!--
        <textarea
          v-show="viewMode !== 'preview'"
          v-model="content"
          class="md-editor"
          spellcheck="false"
          placeholder="在此输入 Markdown 内容"
        />
        -->

        <!-- 预览：显示由 md 路径推导出的原始 PDF（浏览器原生阅读器，用 blob URL 承载） -->
        <div
          v-show="viewMode !== 'source'"
          :class="['pdf-host', { 'pdf-host--split': viewMode === 'split' }]"
        >
          <el-alert
            v-if="pdfError"
            :title="pdfError"
            type="warning"
            show-icon
            :closable="false"
          />
          <iframe v-else-if="pdfUrl" :src="pdfUrl" class="pdf-frame" title="PDF 预览" />
          <div v-else class="empty">暂无 PDF</div>
        </div>

        <!-- 原文只读查看 MD 源码：分栏时与 PDF 并排，两者各自独立滚动 -->
        <pre
          v-show="viewMode === 'split' || viewMode === 'source'"
          :class="['md-source', { 'md-source--split': viewMode === 'split' }]"
        >{{ content }}</pre>
      </div>
    </section>

    <!-- 右：1/3 -->
    <section class="pane pane-right">
      <header class="pane-head">
        <span class="pane-title">标注</span>
        <span v-if="activeTab.status === 'ok'" class="hint">共 {{ activeTab.records.length }} 条</span>
      </header>

      <el-tabs v-model="activeTabKey" class="qa-tabs">
        <el-tab-pane
          v-for="t in tabs"
          :key="t.key"
          :name="t.key"
          :label="t.status === 'ok' ? `${t.label}（${t.records.length}）` : t.label"
        />
      </el-tabs>

      <div v-loading="activeTab.status === 'loading'" ref="rightPaneRef" class="pane-body qa-body">
        <el-alert
          v-if="activeTab.status === 'error'"
          :title="activeTab.error"
          type="error"
          show-icon
          :closable="false"
        />

        <div v-else class="qa-list">
          <article v-for="(rec, i) in pagedRecords" :key="pageStart + i" class="qa-card">
            <header class="qa-card-head">{{ activeTab.label }}_{{ pageStart + i + 1 }}</header>

            <!-- 问槽位 -->
            <div class="qa-question">
              <el-input
                v-if="editingIndex === pageStart + i"
                v-model="editQ"
                type="textarea"
                :autosize="{ minRows: 2, maxRows: 10 }"
              />
              <div v-else class="qa-text">{{ fieldText(rec, activeTab.qField) }}</div>
            </div>

            <!-- 答槽位 + 操作按钮 -->
            <div class="qa-answer">
              <el-input
                v-if="editingIndex === pageStart + i"
                v-model="editA"
                type="textarea"
                :autosize="{ minRows: 3, maxRows: 14 }"
              />
              <div v-else class="qa-text">{{ fieldText(rec, activeTab.aField) }}</div>

              <div class="qa-actions">
                <template v-if="editingIndex === pageStart + i">
                  <el-button size="small" :disabled="saving" @click="cancelEdit">取消</el-button>
                  <el-button size="small" type="primary" :loading="saving" @click="saveEdit">
                    保存
                  </el-button>
                </template>
                <el-button v-else size="small" type="primary" @click="startEdit(pageStart + i)">
                  编辑
                </el-button>
              </div>
            </div>
          </article>

          <div v-if="activeTab.status === 'ok' && !pagedRecords.length" class="empty">暂无数据</div>
        </div>
      </div>

      <footer class="qa-footer">
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :total="activeTab.status === 'ok' ? activeTab.records.length : 0"
          :page-sizes="[5, 10, 20, 50]"
          layout="total, sizes, prev, pager, next"
          size="small"
          background
        />
      </footer>
    </section>

    <!-- 选段翻译：划选后浮现的按钮（teleport 到 body，fixed 定位）+ 译文弹窗 -->
    <teleport to="body">
      <button
        v-show="selBtn.show"
        class="sel-translate-btn"
        :style="{ left: selBtn.x + 'px', top: selBtn.y + 'px' }"
        @mousedown.prevent
        @click="translateSelection"
      >
        翻译
      </button>
    </teleport>

    <el-dialog v-model="transDialog" title="选段翻译" width="720px" top="8vh">
      <div class="trans-label">原文（{{ selText.length }} 字）</div>
      <pre class="trans-src">{{ selText }}</pre>
      <div class="trans-label">译文</div>
      <div v-if="transState === 'loading'" class="trans-loading">
        <el-icon class="is-loading"><Loading /></el-icon> 正在翻译…
      </div>
      <div v-else-if="transState === 'error'" class="trans-error">
        <span>{{ transError }}</span>
        <el-button size="small" type="primary" @click="translateSelection">重试</el-button>
      </div>
      <MdPreview
        v-else
        class="trans-out"
        :model-value="transResult"
        preview-theme="github"
        theme="light"
        language="zh-CN"
        :no-katex="true"
        :no-mermaid="true"
        :no-highlight="true"
      />
      <template #footer>
        <el-button @click="transDialog = false">关闭</el-button>
        <el-button type="primary" :disabled="transState !== 'done'" @click="copyTranslation">复制译文</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.md-page {
  display: flex;
  gap: 16px;
  height: calc(100vh - var(--app-header-height));
  padding: 16px;
}

.pane {
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  overflow: hidden;
}

.pane-left {
  flex: 2;
}

.pane-right {
  flex: 1;
}

.pane-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex: none;
  height: 48px;
  padding: 0 16px;
  border-bottom: 1px solid #ebeef5;
}

.pane-title {
  font-size: 15px;
  font-weight: 600;
}

.hint {
  font-size: 12px;
  color: #a8abb2;
}

.pane-body {
  flex: 1;
  min-height: 0;
}

/* 左侧：默认预览 PDF，可切到分栏（PDF + MD 源码）或只读 MD 原文 */
.doc-body {
  display: flex;
  min-height: 0;
  overflow: hidden;
}

/* PDF 预览宿主：预览模式占满，分栏时占左半；内部 iframe 撑满 */
.pdf-host {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: #fff;
}

.pdf-host--split {
  flex: 0 0 50%;
}

.pdf-frame {
  flex: 1;
  width: 100%;
  min-height: 0;
  border: 0;
}

/* 分栏时两个容器各占一半，中间加分隔线 */
.md-source--split {
  flex: 0 0 50%;
  border-left: 1px solid #ebeef5;
}

.md-source {
  flex: 1;
  min-width: 0;
  margin: 0;
  padding: 16px 20px;
  overflow: auto;
  font-family: Consolas, Monaco, 'Courier New', monospace;
  font-size: 13px;
  line-height: 1.7;
  color: #303133;
  /* 文档里有超长行，pre-wrap 换行避免横向滚动条 */
  white-space: pre-wrap;
  word-break: break-word;
  background: #fff;
}

/* 翻译视图：与预览并排时占 50% */
.md-translation-host {
  flex: 0 0 50%;
  min-width: 0;
  display: flex;
  border-left: 1px solid #ebeef5;
}

/* 已注释：编辑区样式（当前只需要预览）
.md-editor {
  padding: 16px;
  border: none;
  outline: none;
  resize: none;
  font-family: Consolas, Monaco, 'Courier New', monospace;
  font-size: 13px;
  line-height: 1.7;
}
*/

/* 右侧标注面板：标签页 + Q&A 卡片 + 分页 */
.qa-tabs {
  flex: none;
  padding: 0 16px;
}

.qa-tabs :deep(.el-tabs__header) {
  margin-bottom: 0;
}

.qa-body {
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding: 12px 16px;
  overflow: auto;
  background: #f0f2f5;
}

.qa-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.qa-card {
  overflow: hidden;
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 6px;
}

.qa-card-head {
  padding: 8px 12px;
  font-size: 13px;
  font-weight: 600;
  color: #303133;
  background: #f5f7fa;
  border-bottom: 1px solid #ebeef5;
}

/* 问槽位：白底带边框盒子 */
.qa-question {
  margin: 10px 12px 0;
  padding: 10px 12px;
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 4px;
}

/* 答槽位：灰底带边框盒子，右下角放操作按钮 */
.qa-answer {
  margin: 10px 12px 12px;
  padding: 10px 12px;
  background: #f5f7fa;
  border: 1px solid #e4e7ed;
  border-radius: 4px;
}

.qa-text {
  font-size: 13px;
  line-height: 1.7;
  color: #303133;
  /* 问答内容自带换行，保留原始排版 */
  white-space: pre-wrap;
  word-break: break-word;
}

.qa-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 8px;
}

.empty {
  padding: 10px 0;
  font-size: 12px;
  color: #a8abb2;
}

.qa-footer {
  display: flex;
  justify-content: flex-end;
  flex: none;
  padding: 8px 16px;
  border-top: 1px solid #ebeef5;
}
/* ---------- 选段翻译 ---------- */
.sel-translate-btn {
  position: fixed;
  z-index: 3000;
  padding: 2px 12px;
  font-size: 12px;
  line-height: 20px;
  color: #fff;
  background: #409eff;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.25);
}
.sel-translate-btn:hover {
  background: #66b1ff;
}
.trans-label {
  margin: 4px 0 6px;
  font-size: 12px;
  color: #909399;
}
.trans-src {
  max-height: 140px;
  overflow: auto;
  margin: 0 0 12px;
  padding: 8px 10px;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  background: #f5f7fa;
  border-radius: 4px;
}
.trans-loading {
  padding: 12px 0;
  font-size: 13px;
  color: #909399;
}
.trans-error {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 0;
  font-size: 13px;
  color: #f56c6c;
}
.trans-out {
  max-height: 50vh;
  overflow: auto;
  border: 1px solid #ebeef5;
  border-radius: 4px;
}
</style>
