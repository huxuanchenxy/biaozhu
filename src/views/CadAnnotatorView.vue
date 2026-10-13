<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  baseName,
  boxAtPoint,
  boxCorners,
  boxDisplayName,
  boxExtent,
  boxFormEntries,
  boxShapeLabel,
  boxVertexCount,
  cadObjectUrl,
  CAD_RECT_TYPE_OPTIONS,
  CAD_POLYGON_TYPE_OPTIONS,
  fetchCadAnnotations,
  imageIndexOfBox,
  isPolygonBox,
  listCadObjects,
  parseCadPath,
  saveCadAnnotations,
  splitedImageName,
  type CadAnnotations,
  type CadBox,
  type CadCorner,
  type CadFormMap,
  type CadPath,
} from '@/utils/cad'
import EditIcon from '@/icons/EditIcon.vue'
import DeleteIcon from '@/icons/DeleteIcon.vue'

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

/** 拖动中的临时状态（非响应式，仅记录起点）；moved 用于区分「拖动后松手」与「单击」 */
let drag: { key: string; sx: number; sy: number; ox: number; oy: number; moved: boolean } | null = null
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
  drag = { key: img.key, sx: e.clientX, sy: e.clientY, ox: t.x, oy: t.y, moved: false }
  dragging.value = true
  e.preventDefault()
}
function onWindowMouseMove(e: MouseEvent) {
  if (!drag) return
  const t = tf(drag.key)
  const dx = e.clientX - drag.sx
  const dy = e.clientY - drag.sy
  if (Math.abs(dx) > 2 || Math.abs(dy) > 2) drag.moved = true
  t.x = drag.ox + dx
  t.y = drag.oy + dy
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

/** 每张图的光标样式：拖动中→grabbing；抓手模式→grab；悬停到可点框→pointer；否则→默认（不显放大镜） */
function cursorFor(key: string): string {
  if (panMode[key]) return dragging.value && drag && drag.key === key ? 'grabbing' : 'grab'
  return hover.show && hover.key === key && hoverBox.value ? 'pointer' : 'default'
}

/** 当前选中图片 */
const currentImage = computed<CadImage | null>(() => images.value[selectedIndex.value] ?? null)

/** 叠加层尺寸基准：viewBox 为自然像素，按图宽归一（以 1000px 为 1 单）使线宽/字体不随图大小差异 */
function unit(key: string): number {
  return (dims[key]?.w || 1000) / 1000
}
function vertexRadius(key: string): number {
  return 5 * unit(key)
}

/** 框配色：与截图一致的绿色系为主，多类型时循环取色 */
const PALETTE = ['#22c55e', '#3b82f6', '#f59e0b', '#a855f7', '#ef4444', '#14b8a6', '#eab308']
function boxColor(i: number): string {
  return PALETTE[i % PALETTE.length]
}

/** 叠加层开关：显示标注 / 多边形半透明填充 / 高亮框顶点手柄 */
const showBoxes = ref(true)
const fillPolygons = ref(true)
const showVertices = ref(true)

/** 形状筛选：全部 / 仅矩形 / 仅多边形 */
type ShapeFilter = 'all' | 'rect' | 'polygon'
const shapeFilter = ref<ShapeFilter>('all')

/**
 * 右栏：平铺显示全部标注（跨图片）。
 * 每行记录归属图片的下标与框在该图内的序号，配色/命名与左栏叠加层保持一致；
 * 点击某行 → 选中并滚动到对应图片、高亮对应框；反之点图上的框也会定位到该行。
 */
const allRows = computed(() => {
  const rows: Array<{
    box: CadBox
    imageIndex: number
    index: number
    color: string
    name: string
    imageName: string
    isPolygon: boolean
    /** 属性表单里全部有值字段（含类型/数值），空值字段不展示；数值项带 isValue 供高亮 */
    fields: Array<{ label: string; value: string; isValue: boolean }>
    /** 顶点数 */
    vertices: number
    /** 区域外接尺寸文本（主要给多边形用） */
    extent: string
  }> = []
  images.value.forEach((img, imageIndex) => {
    img.boxes.forEach((box, i) => {
      const isPolygon = isPolygonBox(box)
      const entries = boxFormEntries(box)
      const ext = boxExtent(box)
      // 动态字段：接口 content 里有哪些键就展示哪些，值为空的键值对整体不显示；
      // 固定「类型」在前、「数值」其次，其余按 content 原始顺序
      const fields: Array<{ label: string; value: string; isValue: boolean }> = []
      const typeEntry = entries.find((e) => e.label === '类型')
      if (typeEntry) fields.push({ ...typeEntry, isValue: false })
      const valueEntry = entries.find((e) => e.label === '数值')
      if (valueEntry) fields.push({ ...valueEntry, isValue: true })
      for (const e of entries) {
        if (e.label !== '类型' && e.label !== '数值') fields.push({ ...e, isValue: false })
      }
      rows.push({
        box,
        imageIndex,
        index: i,
        color: boxColor(i),
        name: boxDisplayName(box, boxShapeLabel(box), i + 1),
        imageName: img.name,
        isPolygon,
        fields,
        vertices: boxVertexCount(box),
        extent: ext ? `${Math.round(ext.w)}×${Math.round(ext.h)} px` : '',
      })
    })
  })
  return rows
})

/** 按筛选条件后的行（按矩形/多边形分组排列），以及两种形状下的总数 */
const visibleRows = computed(() => {
  if (shapeFilter.value === 'rect') return allRows.value.filter((r) => !r.isPolygon)
  if (shapeFilter.value === 'polygon') return allRows.value.filter((r) => r.isPolygon)
  // 全部：先矩形后多边形，使右栏按形状分类展示
  const rows = allRows.value
  return [...rows.filter((r) => !r.isPolygon), ...rows.filter((r) => r.isPolygon)]
})
const rectCount = computed(() => allRows.value.filter((r) => !r.isPolygon).length)
const polyCount = computed(() => allRows.value.filter((r) => r.isPolygon).length)

/** 高亮某条标注（右栏点击 → 选中所属图片 + 高亮对应框 + 滚动定位） */
const activeBox = ref<CadBox | null>(null)
/** 当前悬停命中的标注（图上悬停预览 / 右栏行悬停反向高亮共用） */
const hoverBox = ref<CadBox | null>(null)
function activateRow(row: (typeof allRows.value)[number]) {
  selectedIndex.value = row.imageIndex
  activeBox.value = row.box
}
function focusRow(row: (typeof allRows.value)[number]) {
  const same = activeBox.value === row.box
  activateRow(row)
  activeBox.value = same ? null : row.box
  if (!same) {
    nextTick(() => {
      document.getElementById(`cad-img-${row.imageIndex}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    })
  }
}

/** 把高亮框在右栏对应行滚到可见 */
function scrollToRow(box: CadBox) {
  const row = allRows.value.find((r) => r.box === box)
  if (!row) return
  nextTick(() => {
    document
      .getElementById(`ann-row-${row.imageIndex}-${row.index}`)
      ?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  })
}

/** 切换高亮框（再次点同一个则取消） */
function toggleActiveBox(box: CadBox) {
  activeBox.value = activeBox.value === box ? null : box
  if (activeBox.value) scrollToRow(activeBox.value)
}

/* ------------------------------------------------------------------
 * 标注信息：修改 / 删除（写回 MinIO 上的 annotations.json）
 *  - 行右侧「编辑」→ 弹框：类型用单选框，其余文本字段可编辑；多边形通常只有类型。
 *  - 行右侧「删除」→ 二次确认后从源数据与各图 boxes 中移除。
 *  - box 对象与 ann.value 里的源数组共享引用，就地改 content 即同步反映到写回体。
 * ------------------------------------------------------------------ */
type AnnRow = (typeof allRows.value)[number]

const saving = ref(false)
const editDialog = ref(false)
const editBox = ref<CadBox | null>(null)
const editName = ref('')
const editType = ref('')
/** 当前编辑的框是否自带「类型」字段（无类型的框不展示类型单选） */
const editHasType = ref(false)
/** 当前编辑的框是否为多边形（决定类型单选项取哪套清单） */
const editIsPolygon = ref(false)
/** 除类型外的可编辑文本字段（由 box.content 动态得出） */
const editFields = ref<Array<{ key: string; label: string; value: string }>>([])

/** 类型选项：按形状选用矩形/多边形清单 + 当前值（若未收录则追加一项，避免选中项不显示） */
const typeOptions = computed(() => {
  const opts = [...(editIsPolygon.value ? CAD_POLYGON_TYPE_OPTIONS : CAD_RECT_TYPE_OPTIONS)]
  const cur = editType.value
  if (cur && !opts.some((o) => o.code === cur)) opts.unshift({ code: cur, label: `${cur}（未收录）` })
  return opts
})

function startEdit(row: AnnRow) {
  const box = row.box
  editBox.value = box
  editName.value = box.name ?? ''
  editType.value = ''
  editHasType.value = false
  editIsPolygon.value = isPolygonBox(box)
  editFields.value = []
  const content = box.content
  if (content && !Array.isArray(content)) {
    for (const [k, f] of Object.entries(content)) {
      const v = f?.value
      const text = v == null ? '' : typeof v === 'number' ? String(v) : typeof v === 'string' ? v : ''
      if (k === 'type' || f?.type === 'radio') {
        editType.value = text
        editHasType.value = true
      } else {
        editFields.value.push({ key: k, label: f?.name || k, value: text })
      }
    }
  }
  editDialog.value = true
}

async function submitEdit() {
  const box = editBox.value
  const content = box?.content
  if (box && content && !Array.isArray(content)) {
    const map = content as CadFormMap
    // 回写类型（radio）
    for (const [k, f] of Object.entries(map)) {
      if ((k === 'type' || f?.type === 'radio') && f) f.value = editType.value
    }
    // 回写其余文本字段
    for (const ef of editFields.value) {
      const f = map[ef.key]
      if (f) f.value = ef.value
    }
    // 名称
    const nm = editName.value.trim()
    if (nm) box.name = nm
    else if (box.name != null) box.name = ''
  }
  editDialog.value = false
  await persist('已保存修改')
}

async function deleteBox(row: AnnRow) {
  try {
    await ElMessageBox.confirm(
      `确定删除标注「${row.name}」？此操作会写回并覆盖原 annotations.json，不可恢复。`,
      '删除标注',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return
  }
  const src = ann.value
  if (!src) return
  const removeFrom = (arr?: CadBox[]): boolean => {
    if (!arr) return false
    let i = arr.indexOf(row.box)
    if (i < 0 && row.box.uuid) i = arr.findIndex((b) => b.uuid === row.box.uuid)
    if (i >= 0) {
      arr.splice(i, 1)
      return true
    }
    return false
  }
  const ok = removeFrom(src.splitedAnnotation?.content) || removeFrom(src.splitedAnnotationPolygon?.content)
  // 同步 UI：从各图 boxes 中移除
  for (const img of images.value) {
    const j = img.boxes.indexOf(row.box)
    if (j >= 0) img.boxes.splice(j, 1)
  }
  if (activeBox.value === row.box) activeBox.value = null
  if (hoverBox.value === row.box) hoverBox.value = null
  if (!ok) {
    ElMessage.warning('未在源数据中找到该标注，未写回')
    return
  }
  await persist('已删除标注')
}

/** 把当前内存里的整份 annotations 写回 MinIO */
async function persist(okMsg: string) {
  const p = cadPath.value
  if (!p || !ann.value) return
  saving.value = true
  try {
    await saveCadAnnotations(p.annotationsKey, ann.value)
    ElMessage.success(okMsg)
  } catch (e: any) {
    ElMessage.error(`保存失败：${e?.message ?? '未知错误'}`)
  } finally {
    saving.value = false
  }
}

/**
 * 舞台层单击：按光标的自然像素坐标做命中选取，而不是依赖 SVG 元素自身的可点区域——
 * 矩形框 fill=none 时 SVG 不会把内部当命中区，叠在上面的大剖面多边形也会抢走点击。
 * 重叠时取面积最小者，保证嵌在剖面区里的小尺寸框能优先点中；拖动后的松手不算点击。
 */
function onStageClick(img: CadImage, e: MouseEvent) {
  selectedIndex.value = images.value.indexOf(img)
  if (drag?.moved) return
  const pt = naturalPoint(img, e)
  if (!pt) {
    activeBox.value = null
    return
  }
  const hit = boxAtPoint(
    boxesForImage(img).map((b) => b.box),
    pt.x,
    pt.y,
  )
  if (!hit) {
    activeBox.value = null
    return
  }
  toggleActiveBox(hit)
}

/** 一张图上某个框的多角点（像素坐标） */
function cornersOf(box: CadBox) {
  return boxCorners(box)
}
function pointsAttr(box: CadBox): string {
  return cornersOf(box)
    .map((c) => `${c.x},${c.y}`)
    .join(' ')
}
/** 按筛选条件决定这张图上需要叠加绘制的框（保留原图内序号，使配色与右栏一致） */
function boxesForImage(img: CadImage): Array<{ box: CadBox; bi: number }> {
  if (!showBoxes.value) return []
  return img.boxes
    .map((box, bi) => ({ box, bi }))
    .filter(({ box }) => shapeFilter.value === 'all' || (shapeFilter.value === 'polygon') === isPolygonBox(box))
}

/** 是否处于强调态（选中或悬停命中） */
function isEmphasized(box: CadBox): boolean {
  return activeBox.value === box || hoverBox.value === box
}
function shapeFill(box: CadBox, bi: number): string {
  if (isEmphasized(box)) return boxColor(bi)
  return isPolygonBox(box) && fillPolygons.value ? boxColor(bi) : 'none'
}
function shapeFillOpacity(box: CadBox): number {
  if (activeBox.value === box) return 0.3
  if (hoverBox.value === box) return 0.22
  return isPolygonBox(box) && fillPolygons.value ? 0.14 : 0
}
function shapeStrokeWidth(box: CadBox): number {
  if (activeBox.value === box) return 5
  return hoverBox.value === box ? 4 : 3
}
function shapeOpacity(box: CadBox): number {
  return activeBox.value && activeBox.value !== box && hoverBox.value !== box ? 0.4 : 1
}

/** 图片加载完成：记录真实像素尺寸，并应用整张可见的默认适配 */
function onImgLoad(key: string, e: Event) {
  const img = e.target as HTMLImageElement
  dims[key] = { w: img.naturalWidth, h: img.naturalHeight }
  const stage = img.closest('.img-stage') as HTMLElement | null
  if (stage) applyFit(key, stage)
}

/**
 * 光标位置换算为图片自然像素坐标（与标注 JSON 同坐标系）；超出图片时为负值/越界值。
 * 以 .img-el 的实时矩形为基准，因此缩放/平移后依旧准确。
 */
function naturalPoint(img: CadImage, e: MouseEvent): CadCorner | null {
  const stage = e.currentTarget as HTMLElement
  const imgEl = stage.querySelector('.img-el') as HTMLImageElement | null
  const d = dims[img.key]
  if (!imgEl || !d) return null
  const r = imgEl.getBoundingClientRect()
  if (!r.width || !r.height) return null
  return {
    x: ((e.clientX - r.left) / r.width) * d.w,
    y: ((e.clientY - r.top) / r.height) * d.h,
  }
}

/**
 * 光标在视口内移动（含图片以外的灰色区域）：换算出图片自然像素坐标，
 * 并实时算出命中的标注（仅用于悬停预览，拖动中不干预）。
 */
function onStageMove(img: CadImage, e: MouseEvent) {
  const stage = e.currentTarget as HTMLElement
  const stageRect = stage.getBoundingClientRect()
  hover.key = img.key
  hover.left = e.clientX - stageRect.left
  hover.top = e.clientY - stageRect.top
  const pt = naturalPoint(img, e)
  if (pt) {
    hover.natX = Math.round(pt.x)
    hover.natY = Math.round(pt.y)
    hoverBox.value = dragging.value ? null : boxAtPoint(boxesForImage(img).map((b) => b.box), pt.x, pt.y)
  } else {
    hover.natX = 0
    hover.natY = 0
    hoverBox.value = null
  }
  hover.show = true
}

function onImgLeave() {
  hover.show = false
  hoverBox.value = null
}

function selectImage(i: number) {
  selectedIndex.value = i
  activeBox.value = null
}

/** 一张图上的多边形个数（卡片头信息用） */
function polyCountOf(img: CadImage): number {
  return img.boxes.filter(isPolygonBox).length
}

/** 选中的框在右栏滚动定位用（简单高亮即可，暂不做跨栏联动滚动） */
const imageListRef = ref<HTMLElement | null>(null)

/** 未能按 displayKey 匹配到切图的标注数量（会被暂挂在第一张图上） */
const unassignedCount = ref(0)

/**
 * 载入：解析路径 → 读 annotations.json → 列 images/<taskId> 目录 →
 * 按 displayKey 的切图序号把每个框归到对应图片。
 */
async function load() {
  activeBox.value = null
  hoverBox.value = null
  images.value = []
  ann.value = null
  selectedIndex.value = 0
  loadError.value = ''
  unassignedCount.value = 0
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

    // 只保留 png，保持接口（ListObjectsV2）返回的原始顺序，不再按文件名重排
    const pngKeys = objectKeys.filter((k) => /\.png$/i.test(k))

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
      unassignedCount.value = unassigned.length
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
      <!-- <div class="cad-path">当前图片：{{ currentImage?.displayPath || (cadPath?.imagesPrefix ?? '') }}</div> -->

      <!-- 叠加层工具条：形状筛选 + 绘制开关 -->
      <div class="cad-toolbar">
        <!-- <el-radio-group v-model="shapeFilter" size="small">
          <el-radio-button value="all">全部 {{ allRows.length }}</el-radio-button>
          <el-radio-button value="rect">矩形 {{ rectCount }}</el-radio-button>
          <el-radio-button value="polygon">多边形 {{ polyCount }}</el-radio-button>
        </el-radio-group>
        <el-checkbox v-model="showBoxes" size="small">标注叠加</el-checkbox>
        <el-checkbox v-model="fillPolygons" size="small">多边形填充</el-checkbox>
        <el-checkbox v-model="showVertices" size="small">顶点手柄</el-checkbox> -->
        <span class="toolbar-hint">实线=矩形・虚线=多边形・单击框可联动右栏</span>
      </div>

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
            <span class="img-meta">
              {{ img.boxes.length }} 个标注
              <em v-if="polyCountOf(img)">· 多边形 {{ polyCountOf(img) }}</em>
            </span>
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
              @click.stop="onStageClick(img, $event)"
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
                  <g
                    v-for="{ box, bi } in boxesForImage(img)"
                    :key="bi"
                    :opacity="shapeOpacity(box)"
                  >
                    <polygon
                      class="box-shape"
                      :class="{
                        'box-shape--poly': isPolygonBox(box),
                        'box-shape--active': activeBox === box,
                        'box-shape--hover': hoverBox === box && activeBox !== box,
                      }"
                      :points="pointsAttr(box)"
                      :fill="shapeFill(box, bi)"
                      :fill-opacity="shapeFillOpacity(box)"
                      :stroke="boxColor(bi)"
                      :stroke-dasharray="isPolygonBox(box) ? '8 5' : undefined"
                      :stroke-width="shapeStrokeWidth(box)"
                      vector-effect="non-scaling-stroke"
                      stroke-linejoin="round"
                    />
                    <!-- 选中框：仅顶点手柄（不再显示任何值标签） -->
                    <template v-if="activeBox === box">
                      <circle
                        v-if="showVertices"
                        v-for="(c, ci) in cornersOf(box)"
                        :key="`v-${ci}`"
                        :cx="c.x"
                        :cy="c.y"
                        :r="vertexRadius(img.key)"
                        :fill="boxColor(bi)"
                        stroke="#fff"
                        :stroke-width="1.5"
                        vector-effect="non-scaling-stroke"
                      />
                    </template>
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
        <span class="pane-title">标注信息</span>
        <span class="hint"> {{ allRows.length }} 项</span>
      </header>

      <div class="pane-body ann-list">
        <div v-if="!loading && !allRows.length" class="empty">暂无标注</div>
        <el-alert
          v-else-if="unassignedCount"
          class="ann-warn"
          :title="`${unassignedCount} 个标注未能按 displayKey 匹配到切图，已暂挂在第一张图`"
          type="warning"
          show-icon
          :closable="false"
        />

        <ul v-if="visibleRows.length" class="ann-ul">
          <template v-for="(row, idx) in visibleRows" :key="`${row.imageIndex}-${row.index}`">
            <li
              v-if="idx === 0 || visibleRows[idx - 1].isPolygon !== row.isPolygon"
              class="ann-group"
            >
              {{ row.isPolygon ? '多边形' : '矩形' }}
              <span class="hint">{{ row.isPolygon ? polyCount : rectCount }} 项</span>
            </li>
            <li
              :id="`ann-row-${row.imageIndex}-${row.index}`"
              :class="[
                'ann-item',
                { 'ann-item--active': activeBox === row.box, 'ann-item--poly': row.isPolygon },
              ]"
              @mouseenter="hoverBox = row.box"
              @mouseleave="hoverBox = null"
              @click="focusRow(row)"
            >
              <div class="ann-line">
                <span class="ann-no">{{ row.index + 1 }}</span>
                <span class="ann-chip">
                  <span class="ann-dot" :style="{ background: row.color }"></span>
                  {{ row.name }}
                </span>
                <span class="ann-img-hint">{{ row.imageName }}</span>
                <!-- <span :class="['ann-badge', row.isPolygon ? 'ann-badge--poly' : '']">
                  {{ row.isPolygon ? '多边形' : '矩形' }}
                </span> -->
                <span class="ann-actions">
                  <el-button link type="primary" size="small" :icon="EditIcon" @click.stop="startEdit(row)"></el-button>
                  <el-button link type="danger" size="small" :icon="DeleteIcon" @click.stop="deleteBox(row)"></el-button>
                </span>
              </div>
              <div v-if="row.fields.length" class="ann-line ann-line--sub">
                <span v-for="f in row.fields" :key="f.label" class="ann-field">
                  <span class="ann-key">{{ f.label }}</span
                  ><span class="ann-sep">: </span><span class="ann-val">{{ f.value }}</span>
                </span>
              </div>
            </li>
          </template>
        </ul>
      </div>
    </section>

    <!-- 标注编辑弹框：类型单选、其余文本可编辑；多边形通常只有类型 -->
    <el-dialog v-model="editDialog" title="编辑标注" width="560px" top="8vh" append-to-body>
      <el-form label-width="88px">
        <el-form-item label="名称">
          <el-input v-model="editName" placeholder="展示名称（可选）" clearable />
        </el-form-item>
        <el-form-item v-if="editHasType" label="类型">
          <div class="type-radios">
            <el-radio-group v-model="editType">
              <el-radio v-for="o in typeOptions" :key="o.code" :value="o.code">{{ o.label }}</el-radio>
            </el-radio-group>
          </div>
        </el-form-item>
        <el-form-item v-for="ef in editFields" :key="ef.key" :label="ef.label">
          <el-input v-model="ef.value" clearable />
        </el-form-item>
        <div v-if="!editHasType && !editFields.length" class="edit-empty">该标注无可编辑属性</div>
      </el-form>
      <template #footer>
        <el-button @click="editDialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitEdit">保存</el-button>
      </template>
    </el-dialog>
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
  min-width: 390px;
  max-width: 540px;
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

/* 叠加层工具条 */
.cad-toolbar {
  flex: none;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  padding: 8px 16px;
  border-bottom: 1px solid #f0f0f0;
  background: #fafbfc;
}

.toolbar-hint {
  margin-left: auto;
  font-size: 12px;
  color: #a8abb2;
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
  flex: 1;
  min-width: 0;
  font-size: 13px;
  color: #303133;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.img-meta {
  flex: none;
  font-size: 12px;
  color: #909399;
}

.img-meta em {
  font-style: normal;
  color: #0ea5e9;
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

/* 框本体不参与命中：命中统一由舞台层按自然坐标算（矩形 fill=none 也能点中） */
.box-shape {
  pointer-events: none;
}

/* 高亮框中心的类型/数值标签：白描边保证在图纸上可读 */
.box-label {
  pointer-events: none;
  font-weight: 600;
  paint-order: stroke;
  stroke: #fff;
  stroke-width: 4px;
  stroke-linejoin: round;
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
  padding: 0 0 8px;
}

.ann-warn {
  margin: 0 12px 8px;
}

.ann-ul {
  list-style: none;
  margin: 0;
  padding: 0;
}

.ann-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 14px;
  font-size: 13px;
  cursor: pointer;
  border-left: 3px solid transparent;
  border-bottom: 1px solid #eef0f3;
}

/* 多边形行用淡蓝底 + 虚线左边，与矩形区分 */
.ann-item--poly {
  background: #f6fbff;
  border-left-color: #bfdbfe;
}

.ann-line {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}

.ann-line--sub {
  padding-left: 30px;
  font-size: 12px;
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
  z-index: 10;
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

/* 按形状分组后，行内保留所属图片名以便回溯 */
.ann-img-hint {
  font-size: 12px;
  color: #a8abb2;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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

.ann-badge {
  flex: none;
  padding: 0 6px;
  font-size: 12px;
  line-height: 18px;
  color: #909399;
  border: 1px solid #dcdfe6;
  border-radius: 3px;
}

.ann-badge--poly {
  color: #0ea5e9;
  border-color: #7dd3fc;
  border-style: dashed;
}

.ann-field {
  color: #909399;
}

/* 键（字段名）统一蓝色，值统一深灰 */
.ann-key {
  color: #409eff;
}

.ann-sep {
  color: #909399;
}

.ann-val {
  color: #303133;
}

.ann-value {
  color: #409eff;
}

/* 行右侧操作区 */
.ann-actions {
  margin-left: auto;
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 2px;
}

/* 编辑弹框：类型选项较多，限高滚动 */
.type-radios {
  width: 100%;
  max-height: 260px;
  overflow-y: auto;
  padding-right: 8px;
}

.type-radios :deep(.el-radio) {
  margin-right: 16px;
}

.edit-empty {
  padding: 4px 0 0 88px;
  font-size: 13px;
  color: #909399;
}

.empty {
  padding: 24px;
  text-align: center;
  color: #a8abb2;
}
</style>
