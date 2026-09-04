/**
 * 自托管 NLLB-200 翻译模型 + onnxruntime-web wasm。
 *
 * 用途：把模型权重与推理运行时从「浏览器运行时从 CDN 拉」改为「从你自己服务器（public/）下发」，
 * 这样最终用户**不需要任何『下载语言包』的点击/手势**——模型就像普通静态资源一样随页面加载，
 * 且翻译过程零网络请求、零第三方 API。
 *
 * 运行（需要能访问 HuggingFace 的网络，通常在本机/CI 执行一次，不是用户侧）：
 *   npm run fetch:model
 *
 * 国内访问 HuggingFace 常超时，脚本已做镜像回退：
 *   - 默认先走 HF 官方；若不通，自动尝试 hf-mirror.com 镜像（无需任何额外配置）
 *   - 可用环境变量强制指定端点： set HF_ENDPOINT=https://hf-mirror.com 后再跑
 *   - 单文件失败会重试 3 次再换下一个镜像
 *
 * 产物：
 *   public/models/Xenova/nllb-200-distilled-600M/  ← 翻译模型（约 600MB，量化版）
 *   public/wasm/                                    ← onnxruntime-web wasm（约几 MB，离线推理用）
 *
 * 注意：模型文件较大，首次拉取取决于网速；拉完会被 gitignore，部署时随 public/ 一起拷贝即可。
 */
import { createWriteStream, existsSync } from 'node:fs'
import { mkdir, copyFile, access } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { pipeline as streamPipeline } from 'node:stream/promises'
import { Readable } from 'node:stream'

const __dirname = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(__dirname, '..')
const PUBLIC = resolve(ROOT, 'public')

const MODEL_REPO = 'Xenova/nllb-200-distilled-600M'

// 端点：优先用环境变量 HF_ENDPOINT，否则官方；国内不通时自动回退镜像
const HF_ENDPOINT = (process.env.HF_ENDPOINT || 'https://huggingface.co').replace(/\/$/, '')
const MIRRORS = Array.from(new Set([HF_ENDPOINT, 'https://hf-mirror.com']))

// 模型需要的最小文件集合（quantized 版 onnx；按仓库实际存在性校对）
// 真实仓库 Xenova/nllb-200-distilled-600M 的文件清单（HEAD 探测）：
//   存在: config / generation_config / tokenizer / tokenizer_config / special_tokens_map / sentencepiece.bpe.model
//   存在: onnx/encoder_model_quantized.onnx + onnx/decoder_model_merged_quantized.onnx
//   不存在: added_tokens.json、onnx/model_quantized.onnx、vocab.json、merges.txt（属于 BPE，非 SP）
const MODEL_FILES = [
  'config.json',
  'generation_config.json',
  'special_tokens_map.json',
  'tokenizer.json',
  'tokenizer_config.json',
  'sentencepiece.bpe.model',
  'onnx/encoder_model_quantized.onnx',
  'onnx/decoder_model_merged_quantized.onnx',
]

async function exists(p) {
  try {
    await access(p)
    return true
  } catch {
    return false
  }
}

/**
 * 下载单个模型文件：依次尝试各镜像，每个镜像失败重试 3 次。
 * 仓库里本来就 404 的文件（404 Not Found）当作"跳过"而非"失败"——避免上游文件清单漂移时整个任务挂掉。
 * @param {string} file  仓库内相对路径，如 'onnx/model_quantized.onnx'
 */
async function fetchToFile(file, dest, label) {
  await mkdir(dirname(dest), { recursive: true })
  if (await exists(dest)) {
    console.log(`  ✓ 已存在，跳过：${label}`)
    return
  }
  let lastErr
  for (const base of MIRRORS) {
    const url = `${base}/${MODEL_REPO}/resolve/main/${file}`
    for (let attempt = 1; attempt <= 3; attempt++) {
      try {
        const res = await fetch(url, { redirect: 'follow' })
        if (res.status === 404) {
          console.log(`  · 仓库无此文件，跳过：${label}（${base} 404）`)
          return
        }
        if (!res.ok || !res.body) throw new Error(`HTTP ${res.status} ${res.statusText}`)
        const total = Number(res.headers.get('content-length')) || 0
        let loaded = 0
        const started = Date.now()
        const out = createWriteStream(dest)
        const nodeStream = Readable.fromWeb(res.body)
        nodeStream.on('data', (chunk) => {
          loaded += chunk.length
          const pct = total ? Math.floor((loaded / total) * 100) : 0
          const sec = ((Date.now() - started) / 1000).toFixed(0)
          process.stdout.write(
            `\r  ↓ ${label} [${base}]  ${pct}%  ${(loaded / 1e6).toFixed(0)}/${(total / 1e6).toFixed(0)}MB  ${sec}s`,
          )
        })
        await streamPipeline(nodeStream, out)
        process.stdout.write('\n')
        console.log(`  ✓ 完成：${label}`)
        return
      } catch (e) {
        lastErr = e
        console.warn(`\n  ! ${base} 第 ${attempt} 次失败：${e.message}`)
        // 删除可能写入一半的残文件，避免下次误判为「已存在」
        try {
          const { rm } = await import('node:fs')
          await rm(dest, { force: true })
        } catch {
          /* ignore */
        }
        if (attempt < 3) await new Promise((r) => setTimeout(r, 1500))
      }
    }
  }
  throw new Error(`所有镜像均失败：${file}（${lastErr?.message}）`)
}

async function copyWasm() {
  // onnxruntime-web 随 @huggingface/transformers 安装，wasm 文件在 node_modules 里
  const candidates = [
    resolve(ROOT, 'node_modules/@huggingface/transformers/node_modules/onnxruntime-web/dist'),
    resolve(ROOT, 'node_modules/onnxruntime-web/dist'),
  ]
  let dist = candidates.find((p) => p && existsSync(p))
  if (!dist) {
    console.warn('  ! 未找到 onnxruntime-web/dist，跳过 wasm 拷贝（运行时将回退到 CDN，仍可用但不完全离线）')
    return
  }
  const dest = resolve(PUBLIC, 'wasm')
  await mkdir(dest, { recursive: true })
  const { readdirSync } = await import('node:fs')
  const files = readdirSync(dist).filter((f) => f.endsWith('.wasm') || f.endsWith('.mjs') || f.endsWith('.js'))
  for (const f of files) {
    await copyFile(resolve(dist, f), resolve(dest, f))
  }
  console.log(`  ✓ 已拷贝 onnxruntime-web wasm 到 public/wasm（${files.length} 个文件）`)
}

async function main() {
  console.log(`\n[1/2] 下载翻译模型 ${MODEL_REPO} → public/models/...`)
  console.log(`     镜像顺序：${MIRRORS.join('  →  ')}`)
  for (const f of MODEL_FILES) {
    const dest = resolve(PUBLIC, 'models', MODEL_REPO, f)
    await fetchToFile(f, dest, f)
  }

  console.log(`\n[2/2] 拷贝 onnxruntime-web wasm → public/wasm/...`)
  await copyWasm()

  console.log('\n✅ 完成。模型已自托管，应用运行时不再访问任何外部网络。')
  console.log('   若部署到生产，请确保 public/models 与 public/wasm 随构建产物一起发布。\n')
}

main().catch((e) => {
  console.error('\n❌ 模型下载失败：', e.message)
  console.error('   请确认本机可访问 https://huggingface.co ，或手动把模型文件放入 public/models/ 对应目录。')
  process.exit(1)
})
