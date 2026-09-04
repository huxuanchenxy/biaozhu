import MarkdownIt from 'markdown-it'

/** 全局共用一个实例：html 关闭，防止标注内容里的标签被渲染成真实 DOM */
const md = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
})

export function renderMarkdown(source: string): string {
  return md.render(source || '')
}
