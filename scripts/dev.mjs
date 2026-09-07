/**
 * 开发启动器：同时拉起「MinIO 签名服务 server.mjs」和「Vite 开发服务器」。
 *
 * 开发链路：浏览器 -> Vite(同源) -> server.mjs(签名) -> MinIO。
 * Vite 把 /<bucket>/... 代理到 server.mjs（见 vite.config.ts 的 VITE_MINIO_SIGNER），
 * 由 server.mjs 用服务端时钟完成 SigV4 后转发 MinIO，因此开发也必须跑 server.mjs。
 * 本脚本让 `npm run dev` 一条命令同时起两者，任一退出则全部退出。
 */
import { spawn } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const SIGNER_PORT = process.env.SIGNER_PORT || '3345'

const signer = spawn(process.execPath, [path.join(root, 'server.mjs')], {
  env: { ...process.env, PORT: SIGNER_PORT },
  stdio: 'inherit',
  cwd: root,
})
const vite = spawn(process.execPath, [path.join(root, 'node_modules', 'vite', 'bin', 'vite.js')], {
  stdio: 'inherit',
  cwd: root,
})

let closing = false
function close(code) {
  if (closing) return
  closing = true
  signer.kill()
  vite.kill()
  process.exit(code)
}
signer.on('exit', (code) => close(code ?? 0))
vite.on('exit', (code) => close(code ?? 0))
process.on('SIGINT', () => close(0))
process.on('SIGTERM', () => close(0))
