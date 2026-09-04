<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import {
  TranslationEngine,
  detectLanguage,
  SOURCE_LANG_OPTIONS,
  TARGET_NLLB,
} from '@/utils/translator'

const props = withDefaults(
  defineProps<{
    /** markdown 原文 */
    content: string
    /** 目标语言，固定简体中文 */
    target?: string
  }>(),
  { target: TARGET_NLLB },
)

type ChunkState = 'pending' | 'translating' | 'done'
interface Chunk {
  id: number
  original: string
  translated: string | null
  state: ChunkState
}

const hostRef = ref<HTMLElement | null>(null)
const chunks = ref<Chunk[]>([])
const status = ref<'idle' | 'detecting' | 'loading-model' | 'translating' | 'done' | 'unsupported' | 'no-model'>(
  'idle',
)
const modelPct = ref(0)
const progress = ref({ done: 0, total: 0 })
const reason = ref('')
const info = ref('')

/** 源语言：'auto' 走自动检测，否则为 NLLB FLORES 码 */
const sourceMode = ref<string>('auto')
const detectedSource = ref<string>('')
const resolvedSource = computed(() => (sourceMode.value === 'auto' ? detectedSource.value || 'fra_Latn' : sourceMode.value))

let cancelledRef = false
let engineInstance: TranslationEngine | null = null

const emit = defineEmits<{
  scroll: [ratio: number]
}>()

function splitParagraphs(md: string): Chunk[] {
  return md
    .split(/\n\s*\n/)
    .map((s) => s.trim())
    .filter(Boolean)
    .map((original, id) => ({ id, original, translated: null, state: 'pending' as ChunkState }))
}

function getEngine(): TranslationEngine {
  if (!engineInstance) {
    const e = new TranslationEngine()
    e.onProgress = (p) => {
      if (p.phase === 'model' && p.total) modelPct.value = Math.round(((p.loaded ?? 0) / p.total) * 100)
    }
    e.onInfo = (m) => (info.value = m)
    e.onError = (m) => {
      // 模型文件缺失是典型错误：提示运行 fetch:model
      if (/model|not found|ENOENT|404|failed to load/i.test(m)) {
        status.value = 'no-model'
        reason.value = '未在 public/models 找到翻译模型，请先运行 `npm run fetch:model`（需能访问 HuggingFace 的网络，本机执行一次）'
      } else {
        status.value = 'unsupported'
        reason.value = m
      }
      console.warn('[translator] 错误：', m)
    }
    engineInstance = e
  }
  return engineInstance
}

async function prepare() {
  cancelledRef = false
  status.value = 'detecting'

  if (sourceMode.value === 'auto') {
    detectedSource.value = (await detectLanguage(props.content)) ?? ''
  }

  const engine = getEngine()
  status.value = 'loading-model'
  try {
    await engine.ready()
  } catch {
    // onError 已处理状态；这里仅中止流程
    return
  }
  if (cancelledRef) return
  await runTranslate()
}

async function runTranslate() {
  const engine = getEngine()
  status.value = 'translating'
  progress.value = { done: 0, total: chunks.value.length }

  for (let i = 0; i < chunks.value.length; i++) {
    if (cancelledRef) return
    const c = chunks.value[i]
    c.state = 'translating'
    try {
      c.translated = await engine.translate(c.original, resolvedSource.value, props.target)
      c.state = 'done'
    } catch (e: any) {
      c.state = 'pending'
      status.value = 'unsupported'
      reason.value = e?.message ?? '翻译失败'
      console.warn('[translator] 单段翻译失败：', e?.message)
      return
    }
    progress.value.done = i + 1
  }
  status.value = 'done'
}

/** 文档变化 → 重置并重翻 */
watch(
  () => props.content,
  (val) => {
    cancelledRef = true
    chunks.value = splitParagraphs(val)
    progress.value = { done: 0, total: chunks.value.length }
    modelPct.value = 0
    detectedSource.value = ''
    status.value = 'idle'
    if (chunks.value.length) prepare()
  },
  { immediate: true },
)

/** 手动切换源语言：无需重载模型（NLLB 多语言），直接重翻 */
function onSourceModeChange() {
  cancelledRef = true
  detectedSource.value = ''
  chunks.value.forEach((c) => {
    c.translated = null
    c.state = 'pending'
  })
  progress.value = { done: 0, total: chunks.value.length }
  if (chunks.value.length) prepare()
}

onBeforeUnmount(() => {
  cancelledRef = true
  engineInstance?.destroy()
  engineInstance = null
})

/** 外部按比例滚动本容器（双向同步用） */
function setScrollRatio(ratio: number) {
  const el = hostRef.value
  if (!el) return
  const max = el.scrollHeight - el.clientHeight
  el.scrollTop = Math.max(0, Math.min(max, ratio * max))
}

function getRatio() {
  const el = hostRef.value
  if (!el) return 0
  const max = el.scrollHeight - el.clientHeight
  return max > 0 ? el.scrollTop / max : 0
}

function onScroll() {
  emit('scroll', getRatio())
}

function langLabel(code: string) {
  return SOURCE_LANG_OPTIONS.find((o) => o.value === code)?.label ?? code
}

defineExpose({ setScrollRatio, getRatio, status })
</script>

<template>
  <div ref="hostRef" class="md-translation" @scroll.passive="onScroll">
    <div
      class="status-bar"
      :class="{ 'status-bar--warn': status === 'unsupported' || status === 'no-model' }"
    >
      <template v-if="status === 'idle'">待启动…</template>
      <template v-else-if="status === 'detecting'">
        正在识别源语言…
        <span v-if="detectedSource" class="lang-tag">检测到：{{ langLabel(detectedSource) }}</span>
      </template>
      <template v-else-if="status === 'loading-model'">
        加载翻译模型 {{ modelPct }}%（来自本服务器，首次一次性，之后浏览器缓存）
      </template>
      <template v-else-if="status === 'translating'">
        翻译中 {{ progress.done }} / {{ progress.total }}
        <span class="lang-tag">{{ langLabel(resolvedSource) }} → 中文</span>
        <span v-if="info" class="lang-tag">{{ info }}</span>
      </template>
      <template v-else-if="status === 'done'">
        翻译完成 · 共 {{ chunks.length }} 段
        <span class="lang-tag">{{ langLabel(resolvedSource) }} → 中文</span>
        <span v-if="info" class="lang-tag">{{ info }}</span>
      </template>
      <template v-else-if="status === 'no-model'">{{ reason }}</template>
      <template v-else-if="status === 'unsupported'">
        {{ reason || '翻译失败' }}
      </template>

      <el-select
        v-if="status !== 'unsupported' && status !== 'no-model'"
        v-model="sourceMode"
        size="small"
        class="lang-select"
        @change="onSourceModeChange"
      >
        <el-option v-for="o in SOURCE_LANG_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
      </el-select>
    </div>

    <div v-if="chunks.length" class="chunk-list">
      <div v-for="c in chunks" :key="c.id" :class="['chunk', `chunk--${c.state}`]">
        <div class="chunk-original">{{ c.original }}</div>
        <div class="chunk-translated">
          <template v-if="c.state === 'done'">{{ c.translated }}</template>
          <span v-else-if="c.state === 'translating'" class="loading">翻译中…</span>
          <span v-else class="pending">…</span>
        </div>
      </div>
    </div>
    <div v-else class="empty">暂无内容</div>
  </div>
</template>

<style scoped>
.md-translation {
  flex: 1;
  min-width: 0;
  overflow: auto;
  background: #fff;
  padding: 12px 20px 20px;
}

.status-bar {
  position: sticky;
  top: 0;
  margin: -12px -20px 12px;
  padding: 8px 20px;
  font-size: 12px;
  color: #606266;
  background: #fafafa;
  border-bottom: 1px solid #ebeef5;
  z-index: 1;
}

.status-bar--warn {
  color: #e6a23c;
  background: #fdf6ec;
  border-bottom-color: #faecd8;
}

.status-bar :deep(.el-select) {
  margin-left: 10px;
  width: 110px;
  vertical-align: middle;
}

.lang-tag {
  margin-left: 8px;
  padding: 1px 6px;
  font-size: 11px;
  color: #909399;
  background: #f4f4f5;
  border-radius: 3px;
}

.lang-select {
  margin-left: 10px;
  width: 110px;
  vertical-align: middle;
}

.empty {
  padding: 20px;
  font-size: 13px;
  color: #a8abb2;
  text-align: center;
}

.chunk-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.chunk {
  padding: 8px 12px;
  background: #fafbfc;
  border-left: 2px solid #dcdfe6;
  border-radius: 2px;
  transition: background-color 0.15s;
}

.chunk--done {
  background: #fff;
  border-left-color: #67c23a;
}

.chunk--translating {
  background: #ecf5ff;
  border-left-color: #409eff;
}

.chunk-original {
  margin-bottom: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: #909399;
  word-break: break-word;
}

.chunk-translated {
  font-size: 13px;
  line-height: 1.7;
  color: #303133;
  word-break: break-word;
  white-space: pre-wrap;
}

.loading {
  color: #409eff;
}

.pending {
  color: #c0c4cc;
}
</style>
