/** 与后端约定过的接口数据类型，放这里统一管理 */

/** 会话列表项（后端 /api/session/list 返回） */
export interface SessionItem {
  sessionId: string
  /** 列表展示用的是 name 字段，不是 content */
  name: string
  chatTime?: string
  [key: string]: any
}

/** 技能列表项（后端 /api/skill/list 返回） */
export interface SkillItem {
  id?: string | number
  name?: string
  description?: string
  [key: string]: any
}
