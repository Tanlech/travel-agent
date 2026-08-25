import { marked } from 'marked'

export function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;')
}

// 北京时间格式化 YYYY-MM-DD HH:mm:ss（不显示"北京时间"前缀，仅日期时间）
export function nowTime() {
  return new Date().toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai', hour12: false,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

export function fmtDuration(ms) {
  if (ms == null || isNaN(ms)) return ''
  if (ms < 1000) return ms + 'ms'
  const s = Math.round(ms / 1000)
  if (s < 60) return s + 's'
  const m = Math.floor(s / 60)
  return m + 'm ' + (s % 60) + 's'
}

export function fmtDT(iso) {
  if (!iso) return '-'
  const d = new Date(iso)
  if (isNaN(d)) return String(iso)
  const p = (n) => String(n).padStart(2, '0')
  return d.getFullYear() + '/' + p(d.getMonth() + 1) + '/' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes())
}

export function genId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 8)
}

// Markdown 渲染：先 esc 转义原始 HTML 再解析，防止 LLM 输出注入任意标签
export function renderMarkdown(src) {
  const safe = esc(src || '')
  try {
    return marked.parse(safe, { gfm: true, breaks: false })
  } catch (e) {
    return safe
  }
}

export function toast(msg) {
  const t = document.createElement('div')
  t.className = 'toast'
  t.textContent = msg
  document.body.appendChild(t)
  setTimeout(() => t.remove(), 1900)
}
