<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { renderMarkdown } from '@/utils/markdown'

/** ---------- 文档 ---------- */
const DEFAULT_DOC = `# 标注说明

选中右侧预览区里的任意一段文字，即可在右边的标注面板中给它打标签。

## 操作提示

1. 在预览区用鼠标**划选**一段文本
2. 在右侧选择或新建一个标签
3. 点击「添加标注」，文本会被高亮并记录到标注列表

> 标注结果可以一键导出为 JSON，方便后续入库或二次加工。
`

const source = ref(DEFAULT_DOC)
const viewMode = ref<'edit' | 'preview' | 'split'>('split')

/** ---------- 标签 ---------- */
interface Label {
  name: string
  color: string
}

const labels = ref<Label[]>([
  { name: '实体', color: '#409eff' },
  { name: '关系', color: '#67c23a' },
  { name: '事件', color: '#e6a23c' },
  { name: '观点', color: '#9254de' },
])
const activeLabel = ref('实体')
const newLabel = ref('')
const LABEL_COLORS = ['#409eff', '#67c23a', '#e6a23c', '#9254de', '#f56c6c', '#909399']

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

/** 标签配色：淡底 + 同色边框文字，避免 Element Plus color 属性表现不一致 */
function tagStyle(color: string) {
  return {
    backgroundColor: `${color}1a`,
    borderColor: color,
    color,
  }
}

function removeLabel(name: string) {
  labels.value = labels.value.filter((l) => l.name !== name)
  if (activeLabel.value === name) activeLabel.value = labels.value[0]?.name ?? ''
}

/** ---------- 标注 ---------- */
interface Annotation {
  id: number
  text: string
  label: string
  color: string
  note: string
}

let seq = 0
const annotations = ref<Annotation[]>([])
const selectedText = ref('')
const note = ref('')
const activeId = ref<number | null>(null)

const previewRef = ref<HTMLElement | null>(null)
const html = computed(() => renderMarkdown(source.value))

/** 预览区鼠标划选后同步到右侧面板 */
function onSelect() {
  const text = window.getSelection()?.toString().trim() ?? ''
  if (text) {
    selectedText.value = text
    note.value = ''
  }
}

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
  const el = previewRef.value?.querySelector<HTMLElement>(`mark.ann[data-id="${id}"]`)
  if (el) {
    el.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }
}

function exportJSON() {
  const payload = JSON.stringify(
    {
      source: source.value,
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

/** ---------- 高亮渲染 ---------- */
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
  for (const ann of annotations.value) {
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
  const root = previewRef.value
  if (!root) return
  unwrapMarks(root)
  applyHighlight(root)
}

watch([html, annotations], () => nextTick(refreshHighlight), { deep: true })
onMounted(() => nextTick(refreshHighlight))

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
        <el-radio-group v-model="viewMode" size="small">
          <el-radio-button value="edit">编辑</el-radio-button>
          <el-radio-button value="split">分栏</el-radio-button>
          <el-radio-button value="preview">预览</el-radio-button>
        </el-radio-group>
      </header>

      <div class="pane-body doc-body" :class="`mode-${viewMode}`">
        <textarea
          v-show="viewMode !== 'preview'"
          v-model="source"
          class="md-editor"
          spellcheck="false"
          placeholder="在此输入 Markdown 内容"
        />
        <div
          v-show="viewMode !== 'edit'"
          ref="previewRef"
          class="md-preview markdown-body"
          @mouseup="onSelect"
          v-html="html"
        />
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

.pane-body {
  flex: 1;
  min-height: 0;
}

/* 左侧编辑 / 预览 */
.doc-body {
  display: flex;
  min-height: 0;
}

.doc-body > * {
  flex: 1;
  min-width: 0;
  overflow: auto;
}

.mode-split > * {
  flex: 1 1 50%;
}

.mode-split .md-editor {
  border-right: 1px solid #ebeef5;
}

.md-editor {
  padding: 16px;
  border: none;
  outline: none;
  resize: none;
  font-family: Consolas, Monaco, 'Courier New', monospace;
  font-size: 13px;
  line-height: 1.7;
  color: #303133;
  background: #fff;
}

.md-preview {
  padding: 16px 20px;
  line-height: 1.75;
  color: #303133;
}

.md-preview :deep(h1),
.md-preview :deep(h2),
.md-preview :deep(h3) {
  margin: 1em 0 0.6em;
  font-weight: 600;
  line-height: 1.35;
}

.md-preview :deep(h1) {
  font-size: 22px;
  border-bottom: 1px solid #ebeef5;
  padding-bottom: 8px;
}

.md-preview :deep(h2) {
  font-size: 18px;
}

.md-preview :deep(h3) {
  font-size: 15px;
}

.md-preview :deep(p) {
  margin: 0 0 0.9em;
}

.md-preview :deep(ul),
.md-preview :deep(ol) {
  margin: 0 0 0.9em;
  padding-left: 22px;
}

.md-preview :deep(blockquote) {
  margin: 0 0 0.9em;
  padding: 8px 12px;
  color: #606266;
  background: #f5f7fa;
  border-left: 3px solid #dcdfe6;
}

.md-preview :deep(code) {
  padding: 2px 5px;
  font-size: 12px;
  background: #f5f7fa;
  border-radius: 3px;
}

.md-preview :deep(pre) {
  padding: 12px;
  overflow: auto;
  background: #f5f7fa;
  border-radius: 4px;
}

.md-preview :deep(pre code) {
  padding: 0;
  background: none;
}

.md-preview :deep(table) {
  width: 100%;
  margin-bottom: 0.9em;
  border-collapse: collapse;
}

.md-preview :deep(th),
.md-preview :deep(td) {
  padding: 6px 10px;
  border: 1px solid #ebeef5;
}

.md-preview :deep(mark.ann) {
  padding: 1px 0;
  color: inherit;
  border-radius: 2px;
  cursor: pointer;
}

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
