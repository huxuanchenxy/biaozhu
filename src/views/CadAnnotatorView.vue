<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  baseName,
  boxDisplayName,
  boxTypeAndValue,
  cadObjectUrl,
  fetchCadAnnotations,
  imageIndexOfBox,
  listCadObjects,
  parseCadPath,
  polygonCorners,
  rectCorners,
  splitedImageName,
  type CadAnnotations,
  type CadBox,
  type CadPath,
} from '@/utils/cad'

/**
 * CAD 图纸标注页：/cad/<bucket>/<任务目录>/annotations/<任务编号>/annotations.json
 * 左栏列出该任务 images/<任务编号>/ 下的全部 png 切图，并把属于它的标注框画在图上；
 * 右栏罗列当前选中图片的标注信息（类型 / 数值）。字段与还原规则见根目录说明文档。
 */

const route = useRoute()

/** 当前路由携带的桶内对象 key（:key(.*) 一般为字符串，兼容数组情况） */
const rawKey = computed(() => {
  const p = route.params.key
  const key = Array.isArray(p) ? p.join('/') : String(p ?? '')
  return key.trim()
})

/** 解析出的目录结构；不合法为 null */
const cadPath = computed<CadPath | null>(() => parseCadPath(rawKey.value))

const loading = ref(false)
const loadError = ref('')
const ann = ref<CadAnnotations | null>(null)

/** 一张切图：对象 key、展示路径、归属它的标注框 */
interface CadImage {
  key: string
  /** 去掉桶名的相对展示路径 */
  displayPath: string
  name: string
  boxes: CadBox[]
}

const images = ref<CadImage[]>([])
const selectedIndex = ref(0)

/** 每张图的真实像素尺寸（@load 后写入），用于 SVG viewBox 换算 */
const dims = reactive<Record<string, { w: number; h: number }>>({})

/**
 * 悬停坐标：记录当前悬停图片、光标在视口(.img-stage)内的显示像素偏移，以及换算后的图片真实像素坐标。
 * left/top 用于定位跟随光标的标签；natX/natY 为标注 JSON 同系的像素坐标（十字线也用它）。
 */
const hover = reactive({ key: '', show: false, left: 0, top: 0, natX: 0, natY: 0 })

/**
 * 每张图独立的变换与交互态：
 *  - transforms[key] = { scale, x, y }：作用于 .img-wrap 的 translate+scale（transform-origin 0 0）。
 *  - panMode[key]：是否开启「抓手/拖动」模式。开启后拖动平移、滚轮失效；关闭时滚轮缩放、不响应拖动。
 */
interface CadTransform {
  scale: number
  x: number
  y: number
}
const transforms = reactive<Record<string, CadTransform>>({})
const panMode = reactive<Record<string, boolean>>({})
const MIN_SCALE = 0.1
const MAX_SCALE = 8

/** 每张图的默认适配态（contain：整张图完整可见并居中），加载完成/双击复位时回到这个态 */
const fits = reactive<Record<string, CadTransform>>({})

function tf(key: string): CadTransform {
  if (!transforms[key]) transforms[key] = { scale: 1, x: 0, y: 0 }
  return transforms[key]
}

/**
 * 计算并应用 contain 适配：图片按宽度铺满时若高度超出视口，则整体缩小到刚好全显并居中；
 * 未超出则保持 1×，仅垂直居中。wrap 布局原点在视口左上角（transform-origin 0 0）。
 */
function applyFit(key: string, stage: HTMLElement) {
  const d = dims[key]
  if (!d || !d.w || !d.h) return
  const sw = stage.clientWidth
  const sh = stage.clientHeight
  if (!sw || !sh) return
  const dispH = (d.h / d.w) * sw
  const s = Math.min(1, sh / dispH)
  const fit = { scale: s, x: (sw - sw * s) / 2, y: (sh - dispH * s) / 2 }
  fits[key] = fit
  transforms[key] = { ...fit }
}
function wrapStyle(key: string) {
  const t = transforms[key]
  return t ? { transform: `translate(${t.x}px, ${t.y}px) scale(${t.scale})` } : {}
}

/** 拖动中的临时状态（非响应式，仅记录起点） */
let drag: { key: string; sx: number; sy: number; ox: number; oy: number } | null = null
const dragging = ref(false)

function togglePan(key: string) {
  panMode[key] = !panMode[key]
  // 退出抓手模式时清掉可能残留的拖动
  if (!panMode[key]) {
    drag = null
    dragging.value = false
  }
}

/** 滚轮缩放（拖动中也可用）：以光标位置为锚点缩放，保持光标下的图像点不动 */
function onWheel(img: CadImage, e: WheelEvent) {
  // 先拦截：无论是否已到缩放上下限，都不让滚轮冒泡影响外层滚动条
  e.preventDefault()
  const stage = e.currentTarget as HTMLElement
  const rect = stage.getBoundingClientRect()
  const cx = e.clientX - rect.left
  const cy = e.clientY - rect.top
  const t = tf(img.key)
  const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12
  const next = Math.min(MAX_SCALE, Math.max(MIN_SCALE, t.scale * factor))
  if (next === t.scale) return
  const k = next / t.scale
  t.x = cx - k * (cx - t.x)
  t.y = cy - k * (cy - t.y)
  t.scale = next
}

/** 抓手模式：按下开始平移 */
function onStageMouseDown(img: CadImage, e: MouseEvent) {
  if (!panMode[img.key]) return
  const t = tf(img.key)
  drag = { key: img.key, sx: e.clientX, sy: e.clientY, ox: t.x, oy: t.y }
  dragging.value = true
  e.preventDefault()
}
function onWindowMouseMove(e: MouseEvent) {
  if (!drag) return
  const t = tf(drag.key)
  t.x = drag.ox + (e.clientX - drag.sx)
  t.y = drag.oy + (e.clientY - drag.sy)
}
function onWindowMouseUp() {
  drag = null
  dragging.value = false
}

/** 双击复位到默认适配态（整张图全显） */
function resetTransform(img: CadImage) {
  const f = fits[img.key]
  transforms[img.key] = f ? { ...f } : { scale: 1, x: 0, y: 0 }
}

/** 每张图的光标样式：拖动中→grabbing；抓手模式→grab；否则→默认（不显放大镜） */
function cursorFor(key: string): string {
  if (!panMode[key]) return 'default'
  return dragging.value && drag && drag.key === key ? 'grabbing' : 'grab'
}

/** 当前选中图片 */
const currentImage = computed<CadImage | null>(() => images.value[selectedIndex.value] ?? null)

/** 框配色：与截图一致的绿色系为主，多类型时循环取色 */
const PALETTE = ['#22c55e', '#3b82f6', '#f59e0b', '#a855f7', '#ef4444', '#14b8a6', '#eab308']
function boxColor(i: number): string {
  return PALETTE[i % PALETTE.length]
}

/**
 * 右栏：平铺显示全部标注（跨图片）。
 * 每行记录归属图片的下标与框在该图内的序号，配色/命名与左栏叠加层保持一致；
 * 点击某行 → 选中并滚动到对应图片、高亮对应框。
 */
const allRows = computed(() => {
  const rows: Array<{
    box: CadBox
    imageIndex: number
    index: number
    color: string
    name: string
    type: string
    value: string
    imageName: string
  }> = []
  images.value.forEach((img, imageIndex) => {
    img.boxes.forEach((box, i) => {
      const { type, value } = boxTypeAndValue(box)
      const isPolygon = Array.isArray(box.points) && (box.points?.length ?? 0) >= 6
      rows.push({
        box,
        imageIndex,
        index: i,
        color: boxColor(i),
        name: boxDisplayName(box, isPolygon ? '多边形' : '矩形', i + 1),
        type,
        value,
        imageName: img.name,
      })
    })
  })
  return rows
})

/** 高亮某条标注（右栏点击 → 选中所属图片 + 高亮对应框 + 滚动定位） */
const activeBox = ref<CadBox | null>(null)
function focusRow(row: (typeof allRows.value)[number]) {
  const same = activeBox.value === row.box
  selectedIndex.value = row.imageIndex
  activeBox.value = same ? null : row.box
  if (!same) {
    nextTick(() => {
      document.getElementById(`cad-img-${row.imageIndex}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    })
  }
}

/** 一张图上某个框的多角点（像素坐标） */
function cornersOf(box: CadBox) {
  return Array.isArray(box.points) && (box.points?.length ?? 0) >= 6 ? polygonCorners(box) : rectCorners(box)
}
function pointsAttr(box: CadBox): string {
  return cornersOf(box)
    .map((c) => `${c.x},${c.y}`)
    .join(' ')
}

/** 图片加载完成：记录真实像素尺寸，并应用整张可见的默认适配 */
function onImgLoad(key: string, e: Event) {
  const img = e.target as HTMLImageElement
  dims[key] = { w: img.naturalWidth, h: img.naturalHeight }
  const stage = img.closest('.img-stage') as HTMLElement | null
  if (stage) applyFit(key, stage)
}

/**
 * 光标在视口内移动（含图片以外的灰色区域）：换算出光标处的图片真实像素坐标。
 * 超出图片时 offX/offY 为负或大于图像尺寸，natX/natY 相应为负值/越界值。
 */
function onStageMove(img: CadImage, e: MouseEvent) {
  const stage = e.currentTarget as HTMLElement
  const stageRect = stage.getBoundingClientRect()
  const imgEl = stage.querySelector('.img-el') as HTMLImageElement | null
  const d = dims[img.key]
  const cx = e.clientX - stageRect.left
  const cy = e.clientY - stageRect.top
  hover.key = img.key
  hover.left = cx
  hover.top = cy
  if (imgEl && d) {
    const r = imgEl.getBoundingClientRect()
    const offX = e.clientX - r.left
    const offY = e.clientY - r.top
    hover.natX = r.width ? Math.round((offX / r.width) * d.w) : 0
    hover.natY = r.height ? Math.round((offY / r.height) * d.h) : 0
  } else {
    hover.natX = 0
    hover.natY = 0
  }
  hover.show = true
}

function onImgLeave() {
  hover.show = false
}

function selectImage(i: number) {
  selectedIndex.value = i
  activeBox.value = null
}

/** 选中的框在右栏滚动定位用（简单高亮即可，暂不做跨栏联动滚动） */
const imageListRef = ref<HTMLElement | null>(null)

/**
 * 载入：解析路径 → 读 annotations.json → 列 images/<taskId> 目录 →
 * 按 displayKey 的切图序号把每个框归到对应图片。
 */
async function load() {
  activeBox.value = null
  images.value = []
  ann.value = null
  selectedIndex.value = 0
  loadError.value = ''
  // 换图时清空上一份的缩放/平移/抓手态与尺寸缓存
  for (const k of Object.keys(transforms)) delete transforms[k]
  for (const k of Object.keys(fits)) delete fits[k]
  for (const k of Object.keys(panMode)) delete panMode[k]
  for (const k of Object.keys(dims)) delete dims[k]
  drag = null
  dragging.value = false
  hover.show = false
  const path = cadPath.value
  if (!path) {
    loadError.value = '路径不合法，应为 /cad/<桶>/<任务目录>/annotations/<任务编号>/annotations.json'
    return
  }
  loading.value = true
  try {
    const [data, objectKeys] = await Promise.all([
      fetchCadAnnotations(path.annotationsKey),
      listCadObjects(path.imagesPrefix),
    ])
    ann.value = data

    // 只保留 png，按文件名自然排序
    const pngKeys = objectKeys
      .filter((k) => /\.png$/i.test(k))
      .sort((a, b) => baseName(a).localeCompare(baseName(b), 'zh-CN', { numeric: true }))

    // 文件名 → 对象 key，供按 splited_image 归属匹配
    const byName = new Map<string, string>()
    for (const k of pngKeys) byName.set(baseName(k), k)

    // 收集所有待还原框（精剪矩形 + 多边形）
    const allBoxes: CadBox[] = [
      ...(data.splitedAnnotation?.content ?? []),
      ...(data.splitedAnnotationPolygon?.content ?? []),
    ]

    // 归组：key = 图片对象 key
    const grouped = new Map<string, CadBox[]>()
    const unassigned: CadBox[] = []
    for (const box of allBoxes) {
      const n = imageIndexOfBox(box)
      let imgKey: string | undefined
      if (n >= 0) {
        const name = splitedImageName(data, n)
        imgKey = byName.get(name) ?? pngKeys[n]
      }
      if (!imgKey && pngKeys.length === 1) imgKey = pngKeys[0]
      if (!imgKey) {
        unassigned.push(box)
        continue
      }
      if (!grouped.has(imgKey)) grouped.set(imgKey, [])
      grouped.get(imgKey)!.push(box)
    }

    const bucketPrefix = `${path.bucket}/`
    images.value = pngKeys.map((k) => ({
      key: k,
      displayPath: k.startsWith(bucketPrefix) ? k.slice(bucketPrefix.length) : k,
      name: baseName(k),
      boxes: grouped.get(k) ?? [],
    }))

    // 有框但没匹配到图的，挂到第一张图上，避免丢框
    if (unassigned.length && images.value.length) {
      images.value[0].boxes.push(...unassigned)
    }

    if (!images.value.length) {
      ElMessage.warning('未在 images 目录找到 png 切图')
    }
  } catch (e: any) {
    loadError.value = `加载失败：${e?.message ?? '未知错误'}`
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  load()
  window.addEventListener('mousemove', onWindowMouseMove)
  window.addEventListener('mouseup', onWindowMouseUp)
})
onBeforeUnmount(() => {
  window.removeEventListener('mousemove', onWindowMouseMove)
  window.removeEventListener('mouseup', onWindowMouseUp)
})
watch(rawKey, load)
</script>

<template>
  <div class="cad-page">
    <!-- 左：切图列表 + 标注框叠加 -->
    <section class="pane pane-left">
      <header class="pane-head">
        <span class="pane-title">小图片切割</span>
        <span v-if="loading" class="hint">加载中…</span>
        <span v-else class="hint">{{ images.length }} 张切图</span>
      </header>
      <div class="cad-path">{{ currentImage?.displayPath || (cadPath?.imagesPrefix ?? '') }}</div>

      <div ref="imageListRef" v-loading="loading" class="pane-body img-list">
        <el-alert v-if="loadError" :title="loadError" type="error" show-icon :closable="false" />
        <div v-else-if="!loading && !images.length" class="empty">暂无图片</div>

        <article
          v-for="(img, i) in images"
          :id="`cad-img-${i}`"
          :key="img.key"
          :class="['img-card', { 'img-card--active': i === selectedIndex }]"
          @click="selectImage(i)"
        >
          <div class="img-card-head">
            <span class="img-path">{{ img.displayPath }}</span>
            <el-button
              size="small"
              round
              :type="panMode[img.key] ? 'primary' : 'default'"
              @click.stop="togglePan(img.key)"
            >
              {{ panMode[img.key] ? '👉 拖动中' : '✋ 抓手' }}
            </el-button>
          </div>
          <div class="img-card-body">
            <div
              class="img-stage"
              :style="{ cursor: cursorFor(img.key) }"
              @wheel="onWheel(img, $event)"
              @mousedown="onStageMouseDown(img, $event)"
              @dblclick="resetTransform(img)"
              @mousemove="onStageMove(img, $event)"
              @mouseleave="onImgLeave"
            >
              <div class="img-wrap" :style="wrapStyle(img.key)">
                <img
                  :src="cadObjectUrl(img.key)"
                  :alt="img.name"
                  class="img-el"
                  draggable="false"
                  @load="onImgLoad(img.key, $event)"
                />
                <svg
                  v-if="dims[img.key]"
                  class="img-overlay"
                  :viewBox="`0 0 ${dims[img.key].w} ${dims[img.key].h}`"
                  preserveAspectRatio="none"
                >
                  <g v-for="(box, bi) in img.boxes" :key="bi">
                    <polygon
                      :points="pointsAttr(box)"
                      fill="none"
                      :stroke="boxColor(bi)"
                      :stroke-width="activeBox === box ? 3 : 1.5"
                      :opacity="activeBox && activeBox !== box ? 0.45 : 1"
                    />
                  </g>
                </svg>
              </div>
              <!-- 十字辅助线：铺满整个视口（含图片外），随光标定位 -->
              <div
                v-if="hover.show && hover.key === img.key && !dragging"
                class="crosshair"
              >
                <div class="crosshair-v" :style="{ left: hover.left + 'px' }"></div>
                <div class="crosshair-h" :style="{ top: hover.top + 'px' }"></div>
              </div>
              <!-- 跟随光标的坐标标签：置于未变换的视口内，不随图片缩放/平移漂移 -->
              <div
                v-if="hover.show && hover.key === img.key && !dragging"
                class="coord-label"
                :style="{ left: hover.left + 'px', top: hover.top + 'px' }"
              >
                X: {{ hover.natX }}, Y: {{ hover.natY }}
              </div>
            </div>
          </div>
        </article>
      </div>
    </section>

    <!-- 右：标注信息 -->
    <section class="pane pane-right">
      <header class="pane-head">
        <span class="pane-title">矩形</span>
        <span class="hint">{{ allRows.length }} 项</span>
      </header>

      <div class="pane-body ann-list">
        <div v-if="!loading && !allRows.length" class="empty">暂无标注</div>

        <ul v-else class="ann-ul">
          <template v-for="(row, idx) in allRows" :key="`${row.imageIndex}-${row.index}`">
            <li v-if="idx === 0 || allRows[idx - 1].imageIndex !== row.imageIndex" class="ann-group">
              {{ row.imageName }}
            </li>
            <li
              :class="['ann-item', { 'ann-item--active': activeBox === row.box }]"
              @click="focusRow(row)"
            >
              <span class="ann-no">{{ row.index + 1 }}</span>
              <span class="ann-chip">
                <span class="ann-dot" :style="{ background: row.color }"></span>
                {{ row.name }}
              </span>
              <span class="ann-field">类型: {{ row.type || '—' }}</span>
              <span class="ann-field ann-value">数值: {{ row.value || '—' }}</span>
            </li>
          </template>
        </ul>
      </div>
    </section>
  </div>
</template>

<style scoped>
.cad-page {
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
  flex: 3;
}

.pane-right {
  flex: 1;
  min-width: 260px;
  max-width: 360px;
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

.cad-path {
  flex: none;
  padding: 8px 16px;
  font-size: 13px;
  color: #606266;
  border-bottom: 1px solid #f0f0f0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.pane-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

/* 左：切图卡片 */
.img-list {
  padding: 12px 16px;
}

.img-card {
  margin-bottom: 16px;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  cursor: pointer;
}

.img-card--active {
  border-color: #409eff;
  box-shadow: 0 0 0 1px #409eff inset;
}

.img-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 12px;
  border-bottom: 1px solid #f0f0f0;
}

.img-path {
  font-size: 13px;
  color: #303133;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.img-card-body {
  padding: 12px;
  background: #e8eaed;
}

/* 视口：缩放/平移在此框内裁剪，固定高度使“拖动”有意义；背景置灰与白底图纸区分 */
.img-stage {
  position: relative;
  overflow: hidden;
  height: 62vh;
  min-height: 320px;
  background: #e8eaed;
}

/* 包裹 img 与叠加层，受 translate+scale 变换；以左上角为缩放/平移基准 */
.img-wrap {
  position: relative;
  width: 100%;
  transform-origin: 0 0;
  will-change: transform;
}

.img-el {
  display: block;
  width: 100%;
  height: auto;
  user-select: none;
}

.img-overlay {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
}

/* 跟随光标的坐标标签（红色，与截图一致） */
.coord-label {
  position: absolute;
  z-index: 2;
  transform: translate(10px, -50%);
  padding: 0 2px;
  font-size: 13px;
  color: #ef4444;
  white-space: nowrap;
  pointer-events: none;
  text-shadow: 0 0 2px #fff, 0 0 2px #fff;
}

/* 十字辅助线：铺满整个视口，虚线，不拦截鼠标 */
.crosshair {
  position: absolute;
  inset: 0;
  z-index: 1;
  overflow: hidden;
  pointer-events: none;
}

.crosshair-v {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 0;
  border-left: 1px dashed #b0b3b8;
}

.crosshair-h {
  position: absolute;
  left: 0;
  right: 0;
  height: 0;
  border-top: 1px dashed #b0b3b8;
}

/* 右：标注列表 */
.ann-list {
  padding: 8px 0;
}

.ann-ul {
  list-style: none;
  margin: 0;
  padding: 0;
}

.ann-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 14px;
  font-size: 13px;
  cursor: pointer;
}

.ann-item:hover {
  background: #f5f7fa;
}

.ann-item--active {
  background: #ecf5ff;
}

.ann-group {
  position: sticky;
  top: 0;
  z-index: 1;
  padding: 6px 14px;
  font-size: 12px;
  font-weight: 600;
  color: #606266;
  background: #f5f7fa;
  border-top: 1px solid #ebeef5;
  border-bottom: 1px solid #ebeef5;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ann-no {
  width: 20px;
  color: #909399;
  text-align: right;
}

.ann-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 88px;
  padding: 2px 8px;
  background: #f5f7fa;
  border: 1px solid #ebeef5;
  border-radius: 4px;
}

.ann-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
}

.ann-field {
  color: #909399;
}

.ann-value {
  color: #409eff;
}

.empty {
  padding: 24px;
  text-align: center;
  color: #a8abb2;
}
</style>
