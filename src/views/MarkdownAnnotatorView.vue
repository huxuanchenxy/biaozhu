<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import MarkdownPreview from '@/components/MarkdownPreview.vue'
import MarkdownTranslation from '@/components/MarkdownTranslation.vue'
import type { Annotation, Label } from '@/types/annotation'

/**
 * 待标注文档地址。
 * 现在指向 public/doc 下的本地文件，后续换成后端接口时只改 .env 里的 VITE_APP_DOC_URL 即可。
 */
const DOC_URL = import.meta.env.VITE_APP_DOC_URL || '/doc/BE1020801A3.md'

/**
 * 是否默认自动加载本地翻译模型（NLLB-200，约 600MB）。
 * 关闭时（默认）进入「翻译」页不会立即占用内存，需手动点按钮加载。
 * 通过 .env 里的 VITE_APP_TRANSLATION_AUTOLOAD 控制。
 */
const TRANSLATION_AUTOLOAD = ['true', '1', 'yes', 'on'].includes(
  String(import.meta.env.VITE_APP_TRANSLATION_AUTOLOAD ?? '').trim().toLowerCase(),
)

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

/** 拉取文档：以后换成接口也走这里，页面其它部分不用动 */
async function loadDoc() {
  loading.value = true
  loadError.value = ''
  try {
    const res = await fetch(DOC_URL)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    content.value = await res.text()
  } catch (e: any) {
    loadError.value = `文档加载失败：${e?.message ?? '未知错误'}`
  } finally {
    loading.value = false
  }
}

loadDoc()

/** ---------- 标签 ---------- */
const labels = ref<Label[]>([
  { name: '实体', color: '#409eff' },
  { name: '关系', color: '#67c23a' },
  { name: '事件', color: '#e6a23c' },
  { name: '观点', color: '#9254de' },
])
const activeLabel = ref('实体')
const newLabel = ref('')
const LABEL_COLORS = ['#409eff', '#67c23a', '#e6a23c', '#9254de', '#f56c6c', '#909399']

/** 标签配色：淡底 + 同色边框文字，避免 Element Plus color 属性表现不一致 */
function tagStyle(color: string) {
  return {
    backgroundColor: `${color}1a`,
    borderColor: color,
    color,
  }
}

function addLabel() {
  const name = newLabel.value.trim()
  if (!name) return
  if (labels.value.some((l) => l.name === name)) {
    ElMessage.warning('标签已存在')
    return
  }
  labels.value.push({ name, color: LABEL_COLORS[labels.value.length % LABEL_COLORS.length] })
  activeLabel.value = name
  newLabel.value = ''
}

function removeLabel(name: string) {
  labels.value = labels.value.filter((l) => l.name !== name)
  if (activeLabel.value === name) activeLabel.value = labels.value[0]?.name ?? ''
}

/** ---------- 标注 ---------- */
let seq = 0
const annotations = ref<Annotation[]>([])
const selectedText = ref('')
const note = ref('')
const activeId = ref<number | null>(null)
const previewRef = ref<InstanceType<typeof MarkdownPreview> | null>(null)

/** 预览组件抛出的划选文本 */
function onSelect(text: string) {
  selectedText.value = text
  note.value = ''
}

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

function addAnnotation() {
  const text = selectedText.value.trim()
  if (!text) {
    ElMessage.warning('请先在预览区选中一段文本')
    return
  }
  const label = labels.value.find((l) => l.name === activeLabel.value)
  annotations.value.push({
    id: ++seq,
    text,
    label: label?.name ?? '未分类',
    color: label?.color ?? '#909399',
    note: note.value.trim(),
  })
  selectedText.value = ''
  note.value = ''
  window.getSelection()?.removeAllRanges()
}

function removeAnnotation(id: number) {
  annotations.value = annotations.value.filter((a) => a.id !== id)
  if (activeId.value === id) activeId.value = null
}

function focusAnnotation(id: number) {
  activeId.value = id
  previewRef.value?.focus(id)
}

function exportJSON() {
  const payload = JSON.stringify(
    {
      source: DOC_URL,
      annotations: annotations.value.map(({ id, text, label, note }) => ({ id, text, label, note })),
    },
    null,
    2,
  )
  const url = URL.createObjectURL(new Blob([payload], { type: 'application/json' }))
  const a = document.createElement('a')
  a.href = url
  a.download = 'annotations.json'
  a.click()
  URL.revokeObjectURL(url)
}

const stats = computed(() => {
  const map = new Map<string, number>()
  annotations.value.forEach((a) => map.set(a.label, (map.get(a.label) ?? 0) + 1))
  return [...map.entries()]
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
          :annotations="annotations"
          @select="onSelect"
          @scroll="onPreviewScroll"
        />

        <!-- 原文只读查看：用 v-show 保住预览区 DOM，切回来时高亮和滚动位置都还在 -->
        <pre
          v-show="viewMode === 'split' || viewMode === 'source'"
          ref="sourceRef"
          :class="['md-source', { 'md-source--split': viewMode === 'split' }]"
          @scroll.passive="onSourceScroll"
        >{{ content }}</pre>

        <!-- 翻译视图：本地 Translator API，按段落分批 -->
        <div v-show="viewMode === 'translate'" class="md-translation-host">
          <MarkdownTranslation
            ref="translationRef"
            :content="content"
            :autoload="TRANSLATION_AUTOLOAD"
            @scroll="onTranslationScroll"
          />
        </div>
      </div>
    </section>

    <!-- 右：1/3 -->
    <section class="pane pane-right">
      <header class="pane-head">
        <span class="pane-title">标注</span>
        <el-button size="small" :disabled="!annotations.length" @click="exportJSON">
          导出 JSON
        </el-button>
      </header>

      <div class="pane-body side-body">
        <div class="block">
          <div class="block-title">待标注文本</div>
          <div v-if="selectedText" class="quote">{{ selectedText }}</div>
          <div v-else class="empty">在左侧预览区划选文本</div>
          <el-input
            v-model="note"
            class="note-input"
            size="small"
            placeholder="备注（可选）"
            maxlength="100"
            show-word-limit
          />
          <el-button type="primary" size="small" class="full" @click="addAnnotation">
            添加标注
          </el-button>
        </div>

        <div class="block">
          <div class="block-title">标签</div>
          <div class="label-wrap">
            <span
              v-for="l in labels"
              :key="l.name"
              class="label-chip"
              @click="activeLabel = l.name"
            >
              <el-tag
                :class="{ active: activeLabel === l.name }"
                :style="tagStyle(l.color)"
                closable
                @close="removeLabel(l.name)"
              >
                {{ l.name }}
              </el-tag>
            </span>
          </div>
          <div class="label-add">
            <el-input v-model="newLabel" size="small" placeholder="新标签名" @keyup.enter="addLabel" />
            <el-button size="small" @click="addLabel">新增</el-button>
          </div>
        </div>

        <div class="block" v-if="stats.length">
          <div class="block-title">统计</div>
          <div class="label-wrap">
            <el-tag
              v-for="[name, count] in stats"
              :key="name"
              size="small"
              :style="tagStyle(labels.find((l) => l.name === name)?.color ?? '#909399')"
            >
              {{ name }} · {{ count }}
            </el-tag>
          </div>
        </div>

        <div class="block block-list">
          <div class="block-title">
            标注列表
            <span class="count">{{ annotations.length }}</span>
          </div>
          <div v-if="!annotations.length" class="empty">暂无标注</div>
          <ul v-else class="ann-list">
            <li
              v-for="a in annotations"
              :key="a.id"
              :class="['ann-item', { active: activeId === a.id }]"
              @click="focusAnnotation(a.id)"
            >
              <div class="ann-top">
                <el-tag size="small" :style="tagStyle(a.color)">{{ a.label }}</el-tag>
                <el-button link type="danger" size="small" @click.stop="removeAnnotation(a.id)">
                  删除
                </el-button>
              </div>
              <div class="ann-text">{{ a.text }}</div>
              <div v-if="a.note" class="ann-note">备注：{{ a.note }}</div>
            </li>
          </ul>
        </div>
      </div>
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

/* 右侧标注面板 */
.side-body {
  display: flex;
  flex-direction: column;
  gap: 18px;
  padding: 16px;
  overflow: auto;
}

.block-title {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 10px;
  font-size: 13px;
  font-weight: 600;
  color: #303133;
}

.count {
  padding: 0 6px;
  font-weight: 500;
  color: #909399;
  background: #f5f7fa;
  border-radius: 8px;
}

.quote {
  padding: 8px 10px;
  font-size: 13px;
  color: #303133;
  background: #f5f7fa;
  border-left: 3px solid #409eff;
  border-radius: 2px;
  word-break: break-all;
}

.empty {
  padding: 10px 0;
  font-size: 12px;
  color: #a8abb2;
}

.note-input {
  margin-top: 10px;
}

.full {
  width: 100%;
  margin-top: 10px;
}

.label-wrap {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.label-chip {
  cursor: pointer;
}

.label-chip .active {
  outline: 2px solid #409eff;
  outline-offset: 1px;
}

.label-add {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}

.block-list {
  flex: 1;
  min-height: 0;
}

.ann-list {
  margin: 0;
  padding: 0;
  list-style: none;
}

.ann-item {
  padding: 10px;
  margin-bottom: 8px;
  background: #fafafa;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  cursor: pointer;
}

.ann-item:hover,
.ann-item.active {
  border-color: #409eff;
  background: #ecf5ff;
}

.ann-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}

.ann-text {
  font-size: 13px;
  line-height: 1.6;
  color: #303133;
  word-break: break-all;
}

.ann-note {
  margin-top: 4px;
  font-size: 12px;
  color: #909399;
}
</style>
