/**
 * Dify Workflow 翻译引擎。
 *
 * 取代原来的本地 NLLB-200 模型方案（@huggingface/transformers + Web Worker）：
 *   - 不再在浏览器下载/推理 600MB 模型，改为调用内网 Dify 上已搭好的翻译 workflow
 *   - 整篇 markdown 一次性提交（inputs[input] = 原文），blocking 模式等待 workflow 跑完
 *   - 从响应 data.outputs.text 取整篇译文
 *
 * 前端直接调用 Dify 服务（VITE_DIFY_API_BASE，默认 http://10.89.33.93），不走代理；
 * 需 Dify 侧允许跨域（CORS）。
 */

/** Dify workflow blocking 模式返回结构（只声明用到的字段） */
export interface DifyWorkflowOutputs {
  /** 翻译结果文本 */
  text?: string
  [key: string]: unknown
}

export interface DifyWorkflowData {
  id: string
  workflow_id: string
  /** succeeded / failed / running 等 */
  status: string
  outputs: DifyWorkflowOutputs | null
  error: string | null
  elapsed_time: number
  total_tokens: number
  total_steps: number
  created_at: number
  finished_at: number
}

export interface DifyWorkflowResponse {
  workflow_run_id: string
  task_id: string
  data: DifyWorkflowData
}

/** Dify 配置（来自 .env，均以 VITE_ 开头才会打包进前端） */
const DIFY_API_BASE = (import.meta.env.VITE_DIFY_API_BASE || 'http://10.89.33.93').replace(/\/$/, '')
const DIFY_API_KEY = import.meta.env.VITE_DIFY_API_KEY || ''
const DIFY_USER = import.meta.env.VITE_DIFY_USER || 'huyz'
const DIFY_INPUT_KEY = import.meta.env.VITE_DIFY_INPUT_KEY || 'input'

/**
 * 整篇翻译耗时可能较长（大文档 + LLM），单独设一个较大的超时兜底，避免请求永久挂起。
 * 文档很大时可适当调大；设为 0 则不超时。
 */
const DIFY_TIMEOUT = 5 * 60 * 1000

/**
 * 翻译引擎：对外暴露「整篇翻译」的 Promise 接口，UI 层只与该接口交互。
 * 内部用 AbortController 支持取消（文档切换 / 组件卸载时中止进行中的请求）。
 */
export class TranslationEngine {
  private controller: AbortController | null = null

  /**
   * 整篇翻译：把 markdown 原文提交给 Dify workflow，blocking 等待并返回整篇译文。
   * @param text markdown 原文
   */
  async translate(text: string): Promise<string> {
    if (!DIFY_API_KEY) {
      throw new Error('未配置 Dify API Key（请在 .env 设置 VITE_DIFY_API_KEY）')
    }

    // 取消上一个进行中的请求，避免文档切换后旧结果覆盖新结果
    this.controller?.abort()
    const controller = new AbortController()
    this.controller = controller

    let timedOut = false
    const timer =
      DIFY_TIMEOUT > 0
        ? setTimeout(() => {
            timedOut = true
            controller.abort()
          }, DIFY_TIMEOUT)
        : null

    try {
      const res = await fetch(`${DIFY_API_BASE}/v1/workflows/run`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${DIFY_API_KEY}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          inputs: { [DIFY_INPUT_KEY]: text },
          response_mode: 'blocking',
          user: DIFY_USER,
        }),
        signal: controller.signal,
      })

      if (!res.ok) {
        const detail = await res.text().catch(() => '')
        throw new Error(
          `Dify 请求失败（HTTP ${res.status}）${detail ? `：${detail.slice(0, 200)}` : ''}`,
        )
      }

      const json = (await res.json()) as DifyWorkflowResponse
      const data = json?.data
      if (!data) throw new Error('Dify 返回结构异常：缺少 data 字段')
      if (data.status !== 'succeeded') {
        throw new Error(`Dify workflow 执行未成功：${data.error || data.status || '未知状态'}`)
      }

      const output = data.outputs?.text
      if (output == null) throw new Error('Dify workflow 未返回 outputs.text')
      return String(output)
    } catch (e) {
      // 超时兜底：转成可读错误（主动取消则由调用方按 AbortError 忽略）
      if (timedOut) throw new Error('Dify 翻译超时（文档过大或服务繁忙），请稍后重试')
      throw e
    } finally {
      if (timer) clearTimeout(timer)
      if (this.controller === controller) this.controller = null
    }
  }

  /** 取消进行中的翻译请求（组件卸载 / 文档切换时调用） */
  destroy() {
    this.controller?.abort()
    this.controller = null
  }
}
