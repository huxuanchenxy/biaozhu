<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
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
 * 悬停坐标：记录当前悬停图片、光标在图内的显示像素偏移，以及换算后的图片真实像素坐标。
 * left/top 用于定位跟随光标的标签；natX/natY 为标注 JSON 同系的像素坐标（十字线也用它）。
 */
const hover = reactive({ key: '', show: false, left: 0, top: 0, natX: 0, natY: 0 })

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

/** 图片加载完成：记录真实像素尺寸 */
function onImgLoad(key: string, e: Event) {
  const img = e.target as HTMLImageElement
  dims[key] = { w: img.naturalWidth, h: img.naturalHeight }
}

/** 鼠标在图上移动：换算出光标处的图片真实像素坐标（按显示尺寸↔自然尺寸比例） */
function onImgMove(img: CadImage, e: MouseEvent) {
  const el = e.currentTarget as HTMLImageElement
  const rect = el.getBoundingClientRect()
  const offX = e.clientX - rect.left
  const offY = e.clientY - rect.top
  const d = dims[img.key]
  hover.key = img.key
  hover.left = offX
  hover.top = offY
  hover.natX = d && rect.width ? Math.round((offX / rect.width) * d.w) : 0
  hover.natY = d && rect.height ? Math.round((offY / rect.height) * d.h) : 0
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

onMounted(load)
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
            <el-button size="small" round>✋ 抓手</el-button>
          </div>
          <div class="img-stage">
            <div class="img-wrap">
              <img
                :src="cadObjectUrl(img.key)"
                :alt="img.name"
                class="img-el"
                @load="onImgLoad(img.key, $event)"
                @mousemove="onImgMove(img, $event)"
                @mouseleave="onImgLeave"
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
                <!-- 十字辅助线（悬停时）：用自然像素坐标，non-scaling-stroke 保证线宽不受拉伸影响 -->
                <g v-if="hover.show && hover.key === img.key && dims[img.key]">
                  <line
                    :x1="hover.natX"
                    y1="0"
                    :x2="hover.natX"
                    :y2="dims[img.key].h"
                    stroke="#9ca3af"
                    stroke-dasharray="6 6"
                    vector-effect="non-scaling-stroke"
                  />
                  <line
                    x1="0"
                    :y1="hover.natY"
                    :x2="dims[img.key].w"
                    :y2="hover.natY"
                    stroke="#9ca3af"
                    stroke-dasharray="6 6"
                    vector-effect="non-scaling-stroke"
                  />
                </g>
              </svg>
              <!-- 跟随光标的坐标标签（HTML，不受 SVG 拉伸影响） -->
              <div
                v-if="hover.show && hover.key === img.key"
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

.img-stage {
  position: relative;
  padding: 12px;
  background: #fafafa;
}

/* 包裹 img 与其叠加层/标签，尺寸与 img 完全一致，便于绝对定位对齐 */
.img-wrap {
  position: relative;
}

.img-el {
  display: block;
  width: 100%;
  height: auto;
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
