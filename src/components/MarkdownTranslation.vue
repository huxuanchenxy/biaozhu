<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { MdPreview } from 'md-editor-v3'
import 'md-editor-v3/lib/preview.css'
import { TranslationEngine } from '@/utils/translator'

const props = defineProps<{
  /** markdown 原文 */
  content: string
}>()

const emit = defineEmits<{
  /** 滚动位置变化，参数是 0~1 的比例（与左侧原文预览双向同步） */
  scroll: [ratio: number]
}>()

const hostRef = ref<HTMLElement | null>(null)
const engine = new TranslationEngine()

/** 翻译状态：idle 等待文档 / translating 翻译中 / done 完成 / error 失败 */
const status = ref<'idle' | 'translating' | 'done' | 'error'>('idle')
/** 整篇译文（markdown） */
const translated = ref('')
const errorMsg = ref('')
/** 当前正在翻译的原文，用于丢弃过期结果（文档切换后旧请求返回不覆盖新结果） */
let currentSource = ''

async function runTranslate(text: string) {
  currentSource = text
  status.value = 'translating'
  errorMsg.value = ''
  try {
    const result = await engine.translate(text)
    // 文档已变化，丢弃过期结果
    if (currentSource !== text) return
    translated.value = result
    status.value = 'done'
  } catch (e: any) {
    // 主动取消（文档切换 / 组件卸载）不当作错误
    if (e?.name === 'AbortError' || currentSource !== text) return
    status.value = 'error'
    errorMsg.value = e?.message ?? '翻译失败'
    console.warn('[translation] Dify 翻译失败：', e?.message)
  }
}

/** 翻译失败后重试 */
function retry() {
  const text = props.content
  if (text && text.trim()) runTranslate(text)
}

/**
 * 文档加载 / 变化后自动整篇翻译：
 * 组件在翻译页用 v-show 常驻挂载，页面读取 md 后 content 就绪即触发，
 * 无需等用户切到「翻译」页，也无需手动点按钮。
 */
watch(
  () => props.content,
  (val) => {
    if (!val || !val.trim()) {
      // 文档尚未就绪：中止可能存在的请求并复位
      engine.destroy()
      currentSource = ''
      translated.value = ''
      errorMsg.value = ''
      status.value = 'idle'
      return
    }
    runTranslate(val)
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  currentSource = ''
  engine.destroy()
})

/** ---------- 滚动同步（与左侧原文预览双向） ---------- */
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

defineExpose({ setScrollRatio, getRatio, status })
</script>

<template>
  <div ref="hostRef" class="md-translation" @scroll.passive="onScroll">
    <div class="status-bar" :class="{ 'status-bar--warn': status === 'error' }">
      <template v-if="status === 'idle'">等待文档加载…</template>
      <template v-else-if="status === 'translating'">
        <el-icon class="is-loading bar-icon"><Loading /></el-icon>
        正在翻译，请稍候…
      </template>
      <template v-else-if="status === 'done'">翻译完成</template>
      <template v-else-if="status === 'error'">
        {{ errorMsg || '翻译失败' }}
        <el-button size="small" class="retry-btn" @click="retry">重试</el-button>
      </template>
    </div>

    <!-- 翻译中：等待提示（用户此时切到「翻译」页会看到） -->
    <div v-if="status === 'translating'" class="placeholder">
      <el-icon class="is-loading placeholder-icon"><Loading /></el-icon>
      <p class="placeholder-title">正在翻译，请稍候…</p>
      <p class="placeholder-sub">整篇文档已提交至 Dify 翻译，完成后会自动展示</p>
    </div>

    <!-- 完成：渲染整篇译文 markdown -->
    <MdPreview
      v-else-if="status === 'done'"
      :model-value="translated"
      preview-theme="github"
      theme="light"
      language="zh-CN"
      :no-katex="true"
      :no-mermaid="true"
      :no-highlight="true"
    />

    <!-- 失败：错误提示 + 重试 -->
    <div v-else-if="status === 'error'" class="placeholder">
      <p class="placeholder-error">{{ errorMsg || '翻译失败' }}</p>
      <el-button size="small" type="primary" @click="retry">重试</el-button>
    </div>

    <!-- 等待文档加载 -->
    <div v-else class="placeholder">
      <p class="placeholder-sub">等待文档加载…</p>
    </div>
  </div>
</template>

<style scoped>
.md-translation {
  flex: 1;
  min-width: 0;
  overflow: auto;
  background: #fff;
}

.status-bar {
  position: sticky;
  top: 0;
  display: flex;
  align-items: center;
  gap: 6px;
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

.bar-icon {
  font-size: 13px;
}

.retry-btn {
  margin-left: 8px;
}

/* md-editor-v3 自带内边距，这里只铺满容器 */
.md-translation :deep(.md-editor-preview-wrapper) {
  padding: 16px 20px;
}

.placeholder {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 60px 20px;
  text-align: center;
}

.placeholder-icon {
  font-size: 28px;
  color: #409eff;
}

.placeholder-title {
  margin: 0;
  font-size: 14px;
  color: #606266;
}

.placeholder-sub {
  margin: 0;
  font-size: 12px;
  color: #a8abb2;
}

.placeholder-error {
  margin: 0;
  font-size: 13px;
  color: #f56c6c;
  word-break: break-word;
}
</style>
