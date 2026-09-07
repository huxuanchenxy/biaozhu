<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import MarkdownPreview from '@/components/MarkdownPreview.vue'
import MarkdownTranslation from '@/components/MarkdownTranslation.vue'
import { getDocJson, saveDocJson } from '@/api'
import type { DocJsonRecord } from '@/api/types'
import { getObjectText } from '@/utils/minio'

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
 * 左栏展示模式：默认预览；分栏时预览与原文并排且滚动同步；
 * 翻译模式下预览与译文并排且滚动同步。
 * 需要恢复编辑能力时，把 'edit' 加回来并接上下面的编辑区即可。
 */
type ViewMode = 'preview' | 'split' | 'source' | 'translate'
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

/** 拉取文档：URL 带 key 走 MinIO，否则读本地 VITE_APP_DOC_URL；页面其它部分不用动 */
async function loadDoc() {
  loading.value = true
  loadError.value = ''
  try {
    if (docKey.value) {
      content.value = await getObjectText(docKey.value)
    } else {
      const res = await fetch(DOC_URL)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      content.value = await res.text()
    }
  } catch (e: any) {
    loadError.value = `文档加载失败：${e?.message ?? '未知错误'}`
  } finally {
    loading.value = false
  }
}

loadDoc()

/** URL 上的对象路径变化时重新拉取（同一组件复用，不重新挂载） */
watch(docKey, () => loadDoc())

/** ---------- 标注数据（Q&A 标签页） ---------- */

/**
 * 三个标签页的字段映射：每个 json 只取两个字段按一问一答展示，
 * 顺序为（问槽位, 答槽位），其余字段隐藏。
 */
interface QaTab {
  key: string
  label: string
  /** public/doc 下的 json 文件名 */
  file: string
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
    label: 'Alpaca',
    file: 'BE1020801A3_alpaca.json',
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

/** 进入页面并行加载三个标签页：目前读本地 json，后续换接口只改 api 层 */
function loadTab(tab: QaTab) {
  tab.status = 'loading'
  tab.error = ''
  getDocJson(tab.file)
    .then((records) => {
      tab.records = records
      tab.status = 'ok'
    })
    .catch((e: any) => {
      tab.records = []
      tab.error = `${tab.file} 加载失败：${e?.message ?? '未知错误'}`
      tab.status = 'error'
    })
}

tabs.value.forEach(loadTab)

/** ---------- 前端分页 ---------- */
const currentPage = ref(1)
const pageSize = ref(5)

/** 当前页的全局 0 基起始下标：用于 Q&A_n 编号与定位编辑项 */
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

/** 保存单条 Q&A：写回当前标签页数据后上传整个标签页的 json（接口未接，api 层先模拟） */
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
    await saveDocJson(tab.file, tab.records)
    ElMessage.success('已保存（上传接口未接，暂为本地修改）')
    editingIndex.value = -1
  } catch (e: any) {
    ElMessage.error(`保存失败：${e?.message ?? '未知错误'}`)
  } finally {
    saving.value = false
  }
}

const previewRef = ref<InstanceType<typeof MarkdownPreview> | null>(null)

/** ---------- 预览 / 原文 / 翻译 滚动同步 ---------- */
const sourceRef = ref<HTMLElement | null>(null)
const translationRef = ref<InstanceType<typeof MarkdownTranslation> | null>(null)
/** 最近一次滚动比例，切换模式时用来把位置带过去 */
const lastRatio = ref(0)

/**
 * 谁在主动滚。被动那一方设置 scrollTop 也会触发 scroll 事件，
 * 不锁住的话两边会互相拉扯，出现抖动。
 */
let syncOwner: 'preview' | 'source' | 'translation' | null = null
let syncTimer: ReturnType<typeof setTimeout> | undefined

function setScrollRatio(el: HTMLElement | null, ratio: number) {
  if (!el) return
  const max = el.scrollHeight - el.clientHeight
  el.scrollTop = Math.max(0, Math.min(max, ratio * max))
}

/**
 * 各模式下只允许两端互相同步：
 *   split      ↔ preview ↔ source
 *   translate  ↔ preview ↔ translation
 * 单栏模式只记录 lastRatio，不同步。
 */
function syncScroll(from: 'preview' | 'source' | 'translation', ratio: number) {
  lastRatio.value = ratio
  if (syncOwner && syncOwner !== from) return
  syncOwner = from
  if (syncTimer) clearTimeout(syncTimer)
  syncTimer = setTimeout(() => {
    syncOwner = null
    syncTimer = undefined
  }, 120)

  if (viewMode.value === 'split') {
    if (from === 'preview') setScrollRatio(sourceRef.value, ratio)
    else if (from === 'source') previewRef.value?.setScrollRatio(ratio)
  } else if (viewMode.value === 'translate') {
    if (from === 'preview') translationRef.value?.setScrollRatio(ratio)
    else if (from === 'translation') previewRef.value?.setScrollRatio(ratio)
  }
}

function onPreviewScroll(ratio: number) {
  syncScroll('preview', ratio)
}

function onSourceScroll() {
  syncScroll('source', getRatio(sourceRef.value))
}

function onTranslationScroll(ratio: number) {
  syncScroll('translation', ratio)
}

function getRatio(el: HTMLElement | null) {
  if (!el) return 0
  const max = el.scrollHeight - el.clientHeight
  return max > 0 ? el.scrollTop / max : 0
}

/** 切换模式后把滚动位置按比例搬过去，避免从头开始看 */
watch(viewMode, (mode) => {
  nextTick(() => {
    if (mode === 'split') {
      previewRef.value?.setScrollRatio(lastRatio.value)
      setScrollRatio(sourceRef.value, lastRatio.value)
    } else if (mode === 'translate') {
      previewRef.value?.setScrollRatio(lastRatio.value)
      translationRef.value?.setScrollRatio(lastRatio.value)
    } else if (mode === 'preview') {
      previewRef.value?.setScrollRatio(lastRatio.value)
    } else if (mode === 'source') {
      setScrollRatio(sourceRef.value, lastRatio.value)
    }
  })
})
</script>

<template>
  <div class="md-page">
    <!-- 左：2/3 -->
    <section class="pane pane-left">
      <header class="pane-head">
        <span class="pane-title">文档</span>
        <span v-if="loading" class="hint">加载中…</span>
        <el-radio-group v-model="viewMode" size="small">
          <el-radio-button value="preview">预览</el-radio-button>
          <el-radio-button value="split">分栏</el-radio-button>
          <el-radio-button value="translate">翻译</el-radio-button>
          <el-radio-button value="source">原文</el-radio-button>
        </el-radio-group>
      </header>

      <div class="pane-body doc-body">
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

        <MarkdownPreview
          v-show="viewMode !== 'source'"
          ref="previewRef"
          v-loading="loading"
          :content="content"
          @scroll="onPreviewScroll"
        />

        <!-- 原文只读查看：用 v-show 保住预览区 DOM，切回来时高亮和滚动位置都还在 -->
        <pre
          v-show="viewMode === 'split' || viewMode === 'source'"
          ref="sourceRef"
          :class="['md-source', { 'md-source--split': viewMode === 'split' }]"
          @scroll.passive="onSourceScroll"
        >{{ content }}</pre>

        <!-- 翻译视图：读取 md 后自动提交 Dify workflow 整篇翻译，翻译中显示等待提示 -->
        <div v-show="viewMode === 'translate'" class="md-translation-host">
          <MarkdownTranslation
            ref="translationRef"
            :content="content"
            @scroll="onTranslationScroll"
          />
        </div>
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

      <div v-loading="activeTab.status === 'loading'" class="pane-body qa-body">
        <el-alert
          v-if="activeTab.status === 'error'"
          :title="activeTab.error"
          type="error"
          show-icon
          :closable="false"
        />

        <div v-else class="qa-list">
          <article v-for="(rec, i) in pagedRecords" :key="pageStart + i" class="qa-card">
            <header class="qa-card-head">Q&A_{{ pageStart + i + 1 }}</header>

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

/* 左侧：默认预览，可切到分栏（滚动同步）或只读原文 */
.doc-body {
  display: flex;
  min-height: 0;
  overflow: hidden;
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
</style>
