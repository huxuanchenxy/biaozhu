/**
 * 翻译 Web Worker：在独立线程加载 NLLB-200 模型并逐段翻译，
 * 避免 1M 文档的长推理阻塞主线程 UI。
 *
 * 关键配置（完全离线、零运行时网络）：
 *   env.allowRemoteModels = false      → 不访问 HuggingFace CDN
 *   env.localModelPath = '/models/'     → 模型从本服务器 public/models 下发
 *   onnxruntime 的 wasm 由 Vite 构建时自动 emit 进 dist/assets，随页面从本服务器下发，
 *   无需设置 wasmPaths，也不访问任何 CDN。
 *
 * 设备：优先 WebGPU（快），失败自动降级到 WASM（兼容任意浏览器）。
 */
import { pipeline, env } from '@huggingface/transformers'

// transformers.js v4 把『本地/远程』拆成两个独立开关（v3 时只需要 allowRemoteModels）
// 浏览器/Worker 下两个默认都是不允许本地、不允许远程，所以要同时打开本地 + 关掉远程。
env.allowRemoteModels = false
env.allowLocalModels = true
env.localModelPath = '/models/'
// onnxruntime-web 的 wasm 由 Vite 打包进 dist/assets（构建时已自动 emit），
// 随页面从本服务器下发，无需设置 wasmPaths，亦不访问任何 CDN。

const MODEL_ID = 'Xenova/nllb-200-distilled-600M'

interface TranslationFn {
  (text: string, opts: { src_lang: string; tgt_lang: string }): Promise<Array<{ translation_text: string }>>
}

let translator: TranslationFn | null = null
let loadPromise: Promise<TranslationFn> | null = null

function post(msg: unknown) {
  ;(self as unknown as { postMessage: (m: unknown) => void }).postMessage(msg)
}

async function getTranslator(): Promise<TranslationFn> {
  if (translator) return translator
  if (loadPromise) return loadPromise

  loadPromise = (async () => {
    const progress = (p: { status: string; file?: string; progress?: number; loaded?: number; total?: number }) => {
      post({ type: 'progress', phase: 'model', data: p })
    }
    let pipe: TranslationFn
    try {
      pipe = (await pipeline('translation', MODEL_ID, {
        device: 'webgpu',
        dtype: 'q8',
        progress_callback: progress,
      })) as unknown as TranslationFn
      post({ type: 'info', message: '使用 WebGPU 加速' })
    } catch {
      post({ type: 'info', message: 'WebGPU 不可用，降级到 WASM CPU（速度较慢）' })
      pipe = (await pipeline('translation', MODEL_ID, {
        device: 'wasm',
        dtype: 'q8',
        progress_callback: progress,
      })) as unknown as TranslationFn
    }
    translator = pipe
    return pipe
  })()

  return loadPromise
}

self.onmessage = async (e: MessageEvent) => {
  const msg = e.data
  if (msg.type === 'warmup') {
    try {
      await getTranslator()
      post({ type: 'ready' })
    } catch (err) {
      post({ type: 'error', message: (err as Error)?.message ?? String(err) })
    }
    return
  }

  if (msg.type === 'translate') {
    try {
      const t = await getTranslator()
      const out = await t(msg.text, { src_lang: msg.srcLang, tgt_lang: msg.tgtLang })
      post({ type: 'result', id: msg.id, text: out[0]?.translation_text ?? '' })
    } catch (err) {
      post({ type: 'error', id: msg.id, message: (err as Error)?.message ?? String(err) })
    }
  }
}
