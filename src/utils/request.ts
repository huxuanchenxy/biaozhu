/**
 * 全局 HTTP 请求封装（axios）
 *
 * 用法示例：
 *   import http from '@/utils/request'
 *   const list = await http.get<User[]>('/user/list')
 *   const res  = await http.post('/user/add', { name: '张三' })
 *
 * 说明：
 *  - baseURL 取自 .env 中的 VITE_APP_BASE_API（默认 /api）
 *  - 拦截器中只做「拆外壳 + 统一报错」，拿到的数据就是后端 data 部分
 *  - 若后端直接返回数组/裸对象（无 code 字段），会自动跳过业务码校验，原样返回
 */
import axios, {
  type AxiosInstance,
  type AxiosRequestConfig,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from 'axios'
import { ElMessage } from 'element-plus'

/** 后端统一响应结构（字段按你后端实际情况在 SUCCESS_CODES / 字段名处调整） */
export interface ApiResult<T = any> {
  code?: number
  data?: T
  message?: string
  msg?: string
  [key: string]: any
}

/** 约定为成功的业务码 */
const SUCCESS_CODES: number[] = [0, 200]

const service: AxiosInstance = axios.create({
  baseURL: import.meta.env.VITE_APP_BASE_API || '/api',
  timeout: 15000,
  headers: { 'Content-Type': 'application/json;charset=utf-8' },
})

/* ----------------------------- 请求拦截器 ----------------------------- */
service.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    // TODO: 接入登录后在这里注入 token
    // const token = localStorage.getItem('token')
    // if (token) config.headers.Authorization = `Bearer ${token}`
    return config
  },
  (error) => Promise.reject(error),
)

/* ----------------------------- 响应拦截器 ----------------------------- */
service.interceptors.response.use(
  (response: AxiosResponse<ApiResult>) => {
    const res = response.data

    // 后端返回的是数组 / 字符串 / null 等裸数据 → 原样透传
    if (!res || typeof res !== 'object' || res.code === undefined) {
      return res as any
    }

    if (!SUCCESS_CODES.includes(res.code)) {
      const msg = res.message || res.msg || '请求失败'
      ElMessage.error(msg)
      return Promise.reject(new Error(msg))
    }

    return res as any
  },
  (error: any) => {
    const status: number | undefined = error?.response?.status
    const serverMsg: string | undefined = error?.response?.data?.message

    if (error?.code === 'ECONNABORTED' || error?.message?.includes('timeout')) {
      ElMessage.error('请求超时，请稍后重试')
    } else if (!error?.response) {
      ElMessage.error('网络连接异常，请检查网络或服务是否启动')
    } else {
      const map: Record<number, string> = {
        400: '请求参数错误',
        401: '未授权，请先登录',
        403: '没有权限访问该资源',
        404: '请求的资源不存在',
        405: '请求方法不被允许',
        408: '请求超时',
        500: '服务器内部错误',
        502: '网关错误',
        503: '服务不可用',
        504: '网关超时',
      }
      ElMessage.error(serverMsg || map[status as number] || `请求失败（${status}）`)
    }

    return Promise.reject(error)
  },
)

/* --------------------------- 对外暴露的方法 --------------------------- */
const http = {
  get<T = any>(url: string, config?: AxiosRequestConfig): Promise<T> {
    // 响应拦截器已把 AxiosResponse 拆成后端 data，这里断言成 Promise<T>
    return service.get<any, any>(url, config) as Promise<T>
  },

  post<T = any>(url: string, data?: any, config?: AxiosRequestConfig): Promise<T> {
    return service.post<any, any>(url, data, config) as Promise<T>
  },

  put<T = any>(url: string, data?: any, config?: AxiosRequestConfig): Promise<T> {
    return service.put<any, any>(url, data, config) as Promise<T>
  },

  delete<T = any>(url: string, config?: AxiosRequestConfig): Promise<T> {
    return service.delete<any, any>(url, config) as Promise<T>
  },

  /** 文件上传：默认字段名 file，可按后端要求调整 */
  upload<T = any>(
    url: string,
    file: File,
    extra?: Record<string, any>,
    field = 'file',
  ): Promise<T> {
    const formData = new FormData()
    formData.append(field, file)
    if (extra) {
      Object.entries(extra).forEach(([key, value]) => formData.append(key, value))
    }
    return service.post<any, any>(url, formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    }) as Promise<T>
  },

  /** 下载文件（后端返回二进制流时使用） */
  download(url: string, data?: any, fileName?: string, config?: AxiosRequestConfig) {
    return service
      .post(url, data, { ...config, responseType: 'blob' })
      .then((res: any) => {
        const blob = new Blob([res.data ?? res])
        const link = document.createElement('a')
        link.href = URL.createObjectURL(blob)
        link.download = fileName || 'download'
        link.click()
        URL.revokeObjectURL(link.href)
      })
  },
}

export { service }
export default http
