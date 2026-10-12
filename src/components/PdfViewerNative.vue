<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'

/**
 * 原生 PDF 查看器：把 blob URL 塞进 <iframe>，交给浏览器内置的 PDF 阅读器渲染。
 *
 * 之所以需要它：部分老文档由国内转换器生成，中文字体未内嵌（无 FontFile）、
 * ToUnicode 映射损坏，pdf.js 渲染时会整段丢失中文；而 Chrome/Edge 内置阅读器
 * （PDFium）能按字体名回退到系统中文字体，渲染结果与直接打开文件一致。
 *
 * 代价：原生阅读器的滚动发生在插件内部，父页面监听不到也无法程序化设置滚动位置，
 * 因此不支持与译文的滚动联动——需要联动时请切回 pdf.js 的「联动模式」。
 */
const props = defineProps<{
  /** PDF 的 blob 对象 URL（application/pdf） */
  src: string
}>()

/** iframe 加载失败（如浏览器禁用了内置查看器）时的提示 */
const failed = ref(false)
let failTimer = window.setTimeout(() => (failed.value = true), 10000)

function onFrameLoad() {
  window.clearTimeout(failTimer)
  failed.value = false
}

watch(
  () => props.src,
  () => {
    failed.value = false
    window.clearTimeout(failTimer)
    failTimer = window.setTimeout(() => (failed.value = true), 10000)
  },
)

onBeforeUnmount(() => window.clearTimeout(failTimer))
</script>

<template>
  <div class="pdf-native">
    <div v-if="!src" class="pdf-native-state">暂无 PDF</div>
    <div v-else-if="failed" class="pdf-native-state pdf-native-state--error">
      当前浏览器无法内嵌显示 PDF，请使用 Chrome / Edge 打开，或切回「联动模式」
    </div>
    <!-- #toolbar=1 让内置阅读器显示工具栏（含下载/打印），zoom=page-width 按页宽适配；
         background 指定查看器页面背景色（PDFium view parameter），避免深色主题下发黑 -->
    <iframe v-else class="pdf-native-frame" :src="`${src}#toolbar=1&zoom=page-width&background=%23ffffff`" @load="onFrameLoad" />
  </div>
</template>

<style scoped>
.pdf-native {
  position: relative;
  flex: 1;
  min-height: 0;
  background: #e9eaee;
}

.pdf-native-frame {
  display: block;
  width: 100%;
  height: 100%;
  border: 0;
}

.pdf-native-state {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  padding: 16px;
  font-size: 13px;
  color: #606266;
  text-align: center;
}

.pdf-native-state--error {
  color: #f56c6c;
}
</style>
