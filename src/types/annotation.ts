/** 标注相关的公共类型，视图与预览组件共用 */

/** 标签 */
export interface Label {
  name: string
  color: string
}

/** 一条标注 */
export interface Annotation {
  id: number
  /** 被标注的原文，高亮时按文本匹配 */
  text: string
  /** 所属标签名 */
  label: string
  /** 标签色值，用于高亮底色 */
  color: string
  note: string
}
