/**
 * 生产/开发一体化服务器：静态文件 + MinIO 签名反向代理（零依赖，Node 内置模块）。
 *
 * 架构：浏览器 --(无签名, 同源)--> 本服务 --(SigV4 签名, 服务端时钟)--> MinIO
 *
 * 相比「浏览器签名 + 透明代理」的三大好处：
 *   1. 签名在服务端完成，浏览器时钟偏差不会再触发 RequestTimeTooSkewed；
 *      若服务端与 MinIO 仍有时差，读取 MinIO 响应 Date 头自动校正后重试一次。
 *   2. 签名请求由本服务直连 MinIO（Host=MinIO），不再依赖中间代理保留 Host/路径。
 *   3. 密钥只存在于服务端配置（.env，不带 VITE_ 前缀），不进入前端包。
 *
 * 用法（在 dist 目录下，即含 index.html/assets 的目录）：
 *   node server.mjs
 * 配置（读运行目录 .env / .env.production，或启动环境变量，环境变量优先）：
 *   SERVER_PORT / MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY / MINIO_BUCKET(或 VITE_MINIO_BUCKET) / DIST
 */
import http from 'node:http'
import crypto from 'node:crypto'
import { createReadStream, existsSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'

// 默认服务当前工作目录（即你运行 node server.mjs 时所在的 dist 目录）
const DIST = path.resolve(process.env.DIST || process.cwd())

/** 极简 .env 解析（KEY = VALUE，忽略注释/空行/包裹引号），不挑 VITE_ 前缀 */
function loadEnvFile(file) {
  const out = {}
  let text = ''
  try {
    text = readFileSync(file, 'utf8')
  } catch {
    return out
  }
  for (const line of text.split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/)
    if (!m) continue
    let v = m[2].trim()
    if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) v = v.slice(1, -1)
    out[m[1]] = v
  }
  return out
}
const fileEnv = {
  ...loadEnvFile(path.resolve(DIST, '.env')),
  ...loadEnvFile(path.resolve(DIST, '.env.production')),
}

// 监听端口：环境变量 PORT > .env 文件 SERVER_PORT > 默认 3344
const PORT = Number(process.env.PORT || fileEnv.SERVER_PORT || 3344)
// 监听地址：环境变量 HOST > .env 文件 SERVER_HOST > 默认 0.0.0.0（生产全接口）；开发由 scripts/dev.mjs 按 --network 参数传入收窄
const HOST = process.env.HOST || fileEnv.SERVER_HOST || '0.0.0.0'
// MinIO 配置：环境变量 > .env 文件（MINIO_* 优先，兼容旧的 VITE_MINIO_*）> 硬编码默认
const MINIO_ENDPOINT =
  process.env.MINIO_ENDPOINT || fileEnv.MINIO_ENDPOINT || fileEnv.VITE_MINIO_ENDPOINT || 'http://10.89.14.234:12001'
const MINIO_ACCESS_KEY =
  process.env.MINIO_ACCESS_KEY || fileEnv.MINIO_ACCESS_KEY || fileEnv.VITE_MINIO_ACCESS_KEY || ''
const MINIO_SECRET_KEY =
  process.env.MINIO_SECRET_KEY || fileEnv.MINIO_SECRET_KEY || fileEnv.VITE_MINIO_SECRET_KEY || ''
const BUCKET =
  process.env.MINIO_BUCKET || fileEnv.MINIO_BUCKET || fileEnv.VITE_MINIO_BUCKET || 'drivdernet_abc'
// 后端服务地址（生产把 /api/... 反代到它，保存 json 用）：环境变量 > .env > 默认
const API_TARGET =
  process.env.API_TARGET || fileEnv.API_TARGET || fileEnv.VITE_PROXY_TARGET || 'http://10.89.34.77:8080'
// 界面主题（背景配色）：环境变量 > .env 文件（dist/.env.production）> 默认 light。
// 不带 VITE_ 前缀故不编译进包，运行时注入 index.html；改完重启 node server.mjs 即切换，无需重新 build。
const APP_THEME = (process.env.APP_THEME || fileEnv.APP_THEME || 'light').trim() === 'dark' ? 'dark' : 'light'

/** 与 vite.config.ts 的 themeBootstrap 保持一致：首屏前给 <html> 打标并设定 color-scheme；localStorage 选择优先于 APP_THEME 默认 */
function themeBootstrap(theme) {
  const t = theme === 'dark' ? 'dark' : 'light'
  return (
    '<script>(function(){var K=\'app-theme\',cfg=' +
    JSON.stringify(t) +
    ';var t;try{t=localStorage.getItem(K)}catch(e){}if(t!==\'dark\'&&t!==\'light\')t=cfg;' +
    'var d=document.documentElement;d.classList.toggle(\'dark\',t===\'dark\');d.setAttribute(\'data-theme\',t);' +
    'var m=document.querySelector(\'meta[name="color-scheme"]\');if(!m){m=document.createElement(\'meta\');' +
    'm.setAttribute(\'name\',\'color-scheme\');document.head.appendChild(m)}' +
    'm.setAttribute(\'content\',t===\'dark\'?\'dark\':\'light only\');})();</' +
    'script>'
  )
}

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.md': 'text/markdown; charset=utf-8',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.map': 'application/json',
}

/* ---------------- SigV4 签名（GET Object，空 query、空 body） ---------------- */
const REGION = 'us-east-1'
const SERVICE = 's3'
const EMPTY_SHA256 = crypto.createHash('sha256').update('').digest('hex')

function sha256hex(s) {
  return crypto.createHash('sha256').update(s).digest('hex')
}
function hmac(key, s) {
  return crypto.createHmac('sha256', key).update(s).digest()
}
/** 生成 ISO 基本格式时间戳：YYYYMMDDTHHMMSSZ */
function amzDateOf(date) {
  return date.toISOString().replace(/[-:]/g, '').replace(/\.\d{3}Z$/, 'Z')
}
/**
 * 把路径规范化为「严格编码」形式：先 decode 再按段 encodeURIComponent 并补上它不编码的 !'()*。
 * MinIO 计算 canonical URI 时会对路径做规范化（如把字面 '(' 变成 %28），
 * 若我们签名用的路径与它规范化后的不一致就会 SignatureDoesNotMatch。
 * 统一成严格编码后，「线上路径 == 签名 canonical == MinIO canonical」，三者一致。
 */
function strictEncodePath(p) {
  return p
    .split('/')
    .map((seg) => {
      let dec = seg
      try {
        dec = decodeURIComponent(seg)
      } catch {
        /* 保持原样 */
      }
      return encodeURIComponent(dec).replace(/[!'()*]/g, (c) => '%' + c.charCodeAt(0).toString(16).toUpperCase())
    })
    .join('/')
}
/** 把 query 参数编成 SigV4 canonical query string（按字典序，RFC3986 严格编码） */
function canonicalQueryOf(params) {
  return [...params.entries()]
    .map(([k, v]) => [strictEncodeComponent(k), strictEncodeComponent(v)])
    .sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
    .map(([k, v]) => `${k}=${v}`)
    .join('&')
}
/** 计算 SigV4 Authorization 头 */
function buildAuth({ host, pathname, canonicalQuery, now }) {
  const amzDate = amzDateOf(now)
  const dateStamp = amzDate.slice(0, 8)
  const canonicalHeaders = `host:${host}\nx-amz-content-sha256:${EMPTY_SHA256}\nx-amz-date:${amzDate}\n`
  const signedHeaders = 'host;x-amz-content-sha256;x-amz-date'
  const canonicalRequest = ['GET', pathname, canonicalQuery, canonicalHeaders, signedHeaders, EMPTY_SHA256].join('\n')
  const scope = `${dateStamp}/${REGION}/${SERVICE}/aws4_request`
  const stringToSign = ['AWS4-HMAC-SHA256', amzDate, scope, sha256hex(canonicalRequest)].join('\n')
  const kDate = hmac(`AWS4${MINIO_SECRET_KEY}`, dateStamp)
  const kRegion = hmac(kDate, REGION)
  const kService = hmac(kRegion, SERVICE)
  const kSigning = hmac(kService, 'aws4_request')
  const signature = crypto.createHmac('sha256', kSigning).update(stringToSign).digest('hex')
  return {
    authorization: `AWS4-HMAC-SHA256 Credential=${MINIO_ACCESS_KEY}/${scope}, SignedHeaders=${signedHeaders}, Signature=${signature}`,
    amzDate,
  }
}
/** 以服务端时钟（+offsetMs 校正）签名并向 MinIO 发起 GET，返回上游响应 */
function signedGet(pathname, offsetMs, query = '') {
  return new Promise((resolve, reject) => {
    const target = new URL(MINIO_ENDPOINT)
    const host = target.host
    const { authorization, amzDate } = buildAuth({
      host,
      pathname,
      canonicalQuery: query.startsWith('?') ? query.slice(1) : query,
      now: new Date(Date.now() + offsetMs),
    })
    const req = http.request(
      {
        protocol: target.protocol,
        hostname: target.hostname,
        port: target.port || (target.protocol === 'https:' ? 443 : 80),
        method: 'GET',
        path: query ? `${pathname}${query}` : pathname,
        headers: {
          host,
          'x-amz-date': amzDate,
          'x-amz-content-sha256': EMPTY_SHA256,
          authorization,
        },
      },
      resolve,
    )
    req.on('error', reject)
    req.end()
  })
}
function readBody(res) {
  return new Promise((resolve) => {
    const chunks = []
    res.on('data', (c) => chunks.push(c))
    res.on('end', () => resolve(Buffer.concat(chunks)))
    res.on('error', () => resolve(Buffer.alloc(0)))
  })
}
function pipeUpstream(upstream, res) {
  res.writeHead(upstream.statusCode || 502, {
    ...upstream.headers,
    'Access-Control-Allow-Origin': '*',
  })
  upstream.pipe(res)
}

/** 把 /<bucket>/... 请求签名后转发 MinIO；遇时钟偏差自动校正重试一次 */
async function proxyToMinio(req, res) {
  const pathname = strictEncodePath((req.url || '/').split('?')[0])
  if (!MINIO_ACCESS_KEY || !MINIO_SECRET_KEY) {
    res.writeHead(500, { 'Content-Type': 'text/plain; charset=utf-8' })
    res.end('server.mjs 未配置 MinIO 密钥（MINIO_ACCESS_KEY / MINIO_SECRET_KEY）')
    return
  }
  let upstream
  try {
    upstream = await signedGet(pathname, 0)
  } catch (e) {
    res.writeHead(502, { 'Content-Type': 'text/plain; charset=utf-8' })
    res.end(`MinIO 连接失败：${e.message}`)
    return
  }
  // 时钟偏差：读 MinIO 的 Date 头算出偏移，用校正后的时间重签一次
  if (upstream.statusCode === 403) {
    const body = await readBody(upstream)
    if (/RequestTimeTooSkewed/.test(body.toString('utf8'))) {
      const serverTime = Date.parse(upstream.headers.date || '')
      const offset = Number.isNaN(serverTime) ? 0 : serverTime - Date.now()
      try {
        upstream = await signedGet(pathname, offset)
      } catch (e) {
        res.writeHead(502, { 'Content-Type': 'text/plain; charset=utf-8' })
        res.end(`MinIO 连接失败：${e.message}`)
        return
      }
      pipeUpstream(upstream, res)
      return
    }
    res.writeHead(403, { ...upstream.headers, 'Access-Control-Allow-Origin': '*' })
    res.end(body)
    return
  }
  pipeUpstream(upstream, res)
}

/* ---------------- CAD 标注：列目录接口 ---------------- */
/** 单段路径的严格编码（与 strictEncodePath 同规则，供签名与请求 URL 共用） */
function strictEncodeComponent(seg) {
  return encodeURIComponent(seg).replace(/[!'()*]/g, (c) => '%' + c.charCodeAt(0).toString(16).toUpperCase())
}
function sendJson(res, code, obj) {
  const body = JSON.stringify(obj)
  res.writeHead(code, {
    'Content-Type': 'application/json; charset=utf-8',
    'Access-Control-Allow-Origin': '*',
  })
  res.end(body)
}
/** 从 ListObjectsV2 的 XML 响应里抽 <Key>（encoding-type=url 时需 decode）、判截断、取续传 token */
function parseListXml(xml) {
  const keys = []
  for (const m of xml.matchAll(/<Key>([^<]*)<\/Key>/g)) {
    let k = m[1]
    try {
      k = decodeURIComponent(k)
    } catch {
      /* 保持原样 */
    }
    keys.push(k)
  }
  const truncated = /<IsTruncated>\s*true\s*<\/IsTruncated>/i.test(xml)
  const token = xml.match(/<NextContinuationToken>([^<]*)<\/NextContinuationToken>/)?.[1] || ''
  return { keys, truncated, token }
}
/** GET /cad-api/list?prefix=<桶内前缀>：签名调 ListObjectsV2 并自动翻页，返回 { objects } */
async function handleList(req, res, url) {
  let prefix = url.searchParams.get('prefix') || ''
  // ListObjectsV2 的 prefix 相对桶内根，需去掉前端传来的桶名前缀段（<bucket>/）
  if (prefix === BUCKET) prefix = ''
  else if (prefix.startsWith(`${BUCKET}/`)) prefix = prefix.slice(BUCKET.length + 1)
  if (!MINIO_ACCESS_KEY || !MINIO_SECRET_KEY) {
    sendJson(res, 500, { error: 'server.mjs 未配置 MinIO 密钥（MINIO_ACCESS_KEY / MINIO_SECRET_KEY）' })
    return
  }
  const pathname = strictEncodePath(`/${BUCKET}`)
  const objects = []
  let token = ''
  for (let page = 0; page < 20; page += 1) {
    const params = new URLSearchParams({
      'list-type': '2',
      'encoding-type': 'url',
      'max-keys': '1000',
      prefix,
    })
    if (token) params.set('continuation-token', token)
    const query = `?${canonicalQueryOf(params)}`
    let upstream
    try {
      upstream = await signedGet(pathname, 0, query)
    } catch (e) {
      sendJson(res, 502, { error: `MinIO 连接失败：${e.message}` })
      return
    }
    const body = (await readBody(upstream)).toString('utf8')
    if (upstream.statusCode !== 200) {
      sendJson(res, 502, { error: `列目录失败 HTTP ${upstream.statusCode}`, detail: body.slice(0, 800) })
      return
    }
    const parsed = parseListXml(body)
    objects.push(...parsed.keys)
    if (!parsed.truncated || !parsed.token) break
    token = parsed.token
  }
  sendJson(res, 200, { objects })
}

/** 普通反向代理：把 /api/... 透传到后端（保存 json 的 uploadOverwrite 走这里），不改路径 */
function proxyToApi(req, res) {
  const target = new URL(API_TARGET)
  const headers = { ...req.headers, host: target.host }
  const proxyReq = http.request(
    {
      protocol: target.protocol,
      hostname: target.hostname,
      port: target.port || (target.protocol === 'https:' ? 443 : 80),
      method: req.method,
      path: req.url,
      headers,
    },
    (up) => {
      res.writeHead(up.statusCode || 502, up.headers)
      up.pipe(res)
    },
  )
  proxyReq.on('error', (e) => {
    if (!res.headersSent) res.writeHead(502, { 'Content-Type': 'text/plain; charset=utf-8' })
    res.end(`后端代理错误：${e.message}`)
  })
  req.pipe(proxyReq)
}

/* ---------------- 静态文件服务 ---------------- */
/** 静态文件服务；找不到时回退 index.html（hash 路由 SPA） */
function sendStatic(res, urlPath) {
  const root = path.resolve(DIST)
  let filePath = path.join(root, decodeURIComponent(urlPath))
  // 防目录穿越
  if (!path.resolve(filePath).startsWith(root)) {
    res.writeHead(403).end('forbidden')
    return
  }
  let st = existsSync(filePath) ? statSync(filePath) : null
  if (st && st.isDirectory()) {
    filePath = path.join(filePath, 'index.html')
    st = existsSync(filePath) ? statSync(filePath) : null
  }
  if (!st) {
    filePath = path.join(root, 'index.html')
    st = existsSync(filePath) ? statSync(filePath) : null
  }
  if (!st) {
    res.writeHead(404).end('not found')
    return
  }
  const ext = path.extname(filePath).toLowerCase()
  // index.html：运行时把主题占位符替换为引导脚本（其余静态文件原样流式输出）
  if (ext === '.html' && path.basename(filePath).toLowerCase() === 'index.html') {
    let html
    try {
      html = readFileSync(filePath, 'utf8')
    } catch {
      res.writeHead(500).end('read index.html failed')
      return
    }
    if (html.includes('<!--APP_THEME_BOOTSTRAP-->')) {
      html = html.replace('<!--APP_THEME_BOOTSTRAP-->', themeBootstrap(APP_THEME))
    } else if (/<\/head>/i.test(html)) {
      // 兼容：若 build 过程删除了占位符注释，退而求其次插到 </head> 前（仍在首屏前执行）
      html = html.replace(/<\/head>/i, themeBootstrap(APP_THEME) + '</head>')
    }
    const buf = Buffer.from(html, 'utf8')
    res.writeHead(200, { 'Content-Type': MIME['.html'], 'Content-Length': buf.length })
    res.end(buf)
    return
  }
  res.writeHead(200, {
    'Content-Type': MIME[ext] || 'application/octet-stream',
    'Content-Length': st.size,
  })
  createReadStream(filePath).pipe(res)
}

const server = http.createServer((req, res) => {
  const urlPath = (req.url || '/').split('?')[0]
  // CAD 标注页的列目录接口（服务端签名，前端不接触密钥）
  if (urlPath === '/cad-api/list' && req.method === 'GET') {
    handleList(req, res, new URL(req.url, 'http://localhost')).catch((e) =>
      sendJson(res, 500, { error: `列目录失败：${e.message}` }),
    )
    return
  }
  // 后端接口（保存 json 等）：普通反代，不签名
  if (urlPath === '/api' || urlPath.startsWith('/api/')) {
    proxyToApi(req, res)
    return
  }
  const isMinioRoute = urlPath === `/${BUCKET}` || urlPath.startsWith(`/${BUCKET}/`)
  if (isMinioRoute && req.method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET,HEAD,OPTIONS',
      'Access-Control-Allow-Headers': '*',
    })
    res.end()
    return
  }
  if (isMinioRoute) {
    proxyToMinio(req, res)
  } else {
    sendStatic(res, urlPath)
  }
})

server.listen(PORT, HOST, () => {
  console.log(`[server] 静态目录: ${DIST}`)
  console.log(`[server] 监听端口: ${PORT}（绑定 ${HOST}）`)
  console.log(`[server] 界面主题 APP_THEME: ${APP_THEME}（改 dist/.env.production 后重启即切换，无需 build）`)
  console.log(`[server] 代理 /${BUCKET}/* -> ${MINIO_ENDPOINT}（服务端 SigV4 签名，时钟自动校正）`)
  console.log(`[server] 列目录 /cad-api/list?prefix=... -> ${MINIO_ENDPOINT}（ListObjectsV2）`)
  console.log(`[server] 代理 /api/* -> ${API_TARGET}（普通反代，保存 json）`)
})
