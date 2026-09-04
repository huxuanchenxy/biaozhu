/**
 * 本地翻译引擎（基于 @huggingface/transformers + NLLB-200，完全离线、零第三方 API）。
 *
 * 与上一版的「浏览器内置 Translator API」不同：
 *   - 不需要用户手势触发下载（Chrome 的硬限制已不存在）
 *   - 模型自托管在 public/models，随页面作为静态资源加载（进度条透明，无『下载包』动作）
 *   - 覆盖 200 种语言，任意现代浏览器可用
 *   - 推理在 Web Worker 后台进行，1M 文档也不卡 UI
 *
 * 真实代价：模型约 600MB（首次加载一次性，之后浏览器缓存）；WASM 推理比浏览器原生 API 慢，
 * 故优先 WebGPU，失败降级 WASM。
 */

/** 浏览器内置语言检测（Chrome/Edge 有，Firefox/Safari 没有 → 仅做 best-effort） */
interface LanguageDetectionResult {
  language: string
  confidence: number
}
interface BuiltinLanguageDetector {
  detect(text: string): Promise<LanguageDetectionResult[]>
}
interface BuiltinLanguageDetectorStatic {
  availability(): Promise<string>
  create(): Promise<BuiltinLanguageDetector>
}

/** BCP-47（LanguageDetector 返回，如 'fr'）→ NLLB-200 的 FLORES 码（如 'fra_Latn'） */
const BCP47_TO_NLLB: Record<string, string> = {
  fr: 'fra_Latn',
  en: 'eng_Latn',
  de: 'deu_Latn',
  ja: 'jpn_Jpan',
  ko: 'kor_Hang',
  ru: 'rus_Cyrl',
  es: 'spa_Latn',
  it: 'ita_Latn',
  pt: 'por_Latn',
  zh: 'zho_Hans',
  'zh-CN': 'zho_Hans',
  'zh-TW': 'zho_Hant',
}

/** 下拉框候选项（label 给用户看，value 是 NLLB 码） */
export const SOURCE_LANG_OPTIONS = [
  { value: 'auto', label: '自动检测' },
  { value: 'fra_Latn', label: '法语' },
  { value: 'eng_Latn', label: '英语' },
  { value: 'deu_Latn', label: '德语' },
  { value: 'jpn_Jpan', label: '日语' },
  { value: 'kor_Hang', label: '韩语' },
  { value: 'rus_Cyrl', label: '俄语' },
  { value: 'spa_Latn', label: '西班牙语' },
]

/** 中文目标码（简体） */
export const TARGET_NLLB = 'zho_Hans'

export function bcp47ToNllb(code: string): string | null {
  return BCP47_TO_NLLB[code] ?? null
}

/** 自动识别源语言（取文档前 2000 字采样）。不支持/出错返回 null，调用方回退手动选择 */
export async function detectLanguage(text: string): Promise<string | null> {
  const ns = (self as unknown as { LanguageDetector?: BuiltinLanguageDetectorStatic }).LanguageDetector
  if (!ns) return null
  try {
    const availability = await ns.availability()
    if (availability === 'unavailable') return null
    const detector = await ns.create()
    const results = await detector.detect(text.slice(0, 2000))
    const top = results?.[0]?.language
    return top ? bcp47ToNllb(top) : null
  } catch (e) {
    console.warn('[translator] 语言检测失败：', (e as Error)?.message)
    return null
  }
}

/** 翻译进度事件 */
export interface TranslationProgress {
  phase: 'model' | 'translate'
  loaded?: number
  total?: number
  file?: string
  status?: string
}

type WorkerMsg =
  | { type: 'progress'; phase: 'model'; data: any }
  | { type: 'info'; message: string }
  | { type: 'ready' }
  | { type: 'result'; id: number; text: string }
  | { type: 'error'; id?: number; message: string }

/**
 * 翻译引擎：包一层 Web Worker，对外暴露「准备 → 逐段翻译」的Promise 接口。
 * 同时把模型加载进度、降级信息、错误转成回调，便于 UI 展示。
 */
export class TranslationEngine {
  private worker: Worker | null = null
  private seq = 0
  private pending = new Map<number, { resolve: (v: string) => void; reject: (e: Error) => void }>()
  private readyResolvers: Array<() => void> = []
  private readyRejected: ((e: Error) => void) | null = null
  private isReady = false

  onProgress: ((p: TranslationProgress) => void) | null = null
  onInfo: ((msg: string) => void) | null = null
  onError: ((msg: string) => void) | null = null

  private ensureWorker() {
    if (this.worker) return
    this.worker = new Worker(new URL('../workers/translation.worker.ts', import.meta.url), {
      type: 'module',
    })
    this.worker.onmessage = (e: MessageEvent) => this.handle(e.data)
    this.worker.onerror = (e) => {
      const msg = e.message || '翻译 Worker 异常'
      this.onError?.(msg)
      this.readyRejected?.(new Error(msg))
    }
  }

  private handle(msg: WorkerMsg) {
    switch (msg.type) {
      case 'progress':
        this.onProgress?.({ phase: 'model', ...(msg.data ?? {}) })
        break
      case 'info':
        this.onInfo?.(msg.message)
        break
      case 'ready':
        this.isReady = true
        this.readyResolvers.forEach((r) => r())
        this.readyResolvers = []
        break
      case 'result': {
        const p = this.pending.get(msg.id)
        if (p) {
          this.pending.delete(msg.id)
          p.resolve(msg.text)
        }
        break
      }
      case 'error': {
        const err = new Error(msg.message)
        if (msg.id != null) {
          const p = this.pending.get(msg.id)
          if (p) {
            this.pending.delete(msg.id)
            p.reject(err)
          }
        } else {
          this.onError?.(msg.message)
          this.readyRejected?.(err)
        }
        break
      }
    }
  }

  /** 预热模型（后台加载）。返回 Promise，模型就绪后 resolve */
  ready(): Promise<void> {
    this.ensureWorker()
    if (this.isReady) return Promise.resolve()
    return new Promise<void>((resolve, reject) => {
      this.readyResolvers.push(resolve)
      this.readyRejected = reject
      this.worker!.postMessage({ type: 'warmup' })
    })
  }

  /** 翻译单段文本。srcLang/tgtLang 均为 NLLB FLORES 码 */
  translate(text: string, srcLang: string, tgtLang: string): Promise<string> {
    this.ensureWorker()
    const id = ++this.seq
    return new Promise<string>((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      this.worker!.postMessage({ type: 'translate', id, text, srcLang, tgtLang })
    })
  }

  destroy() {
    this.worker?.terminate()
    this.worker = null
    this.isReady = false
    this.pending.clear()
  }
}
