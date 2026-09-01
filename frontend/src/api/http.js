import { accountState } from '@/store/auth'

// 构造通用请求头：自动携带登录令牌；adminToken 非空时优先使用（后台页单独存令牌）
export function authHeader(adminToken = null) {
  const headers = { 'Content-Type': 'application/json' }
  const acc = accountState.account
  const tk = adminToken || (acc && acc.token ? acc.token : null)
  if (tk) headers['Authorization'] = 'Bearer ' + tk
  return headers
}

// 通用请求：自动携带登录令牌；adminToken 非空时优先使用（后台页单独存令牌）
export async function request(path, options = {}, adminToken = null) {
  const headers = Object.assign(authHeader(adminToken), options.headers || {})
  const res = await fetch(path, Object.assign({}, options, { headers }))
  let data = null
  try {
    data = await res.json()
  } catch (e) {
    data = {}
  }
  if (!res.ok) {
    // FastAPI 校验错误(422)的 detail 是数组，需转成可读中文，避免直接 new Error 显示成 [object Object]
    let msg = data.detail
    if (Array.isArray(msg)) {
      msg = msg
        .map((d) => {
          const field = Array.isArray(d.loc) ? d.loc.slice(1).join('.') : ''
          return (field ? field + '：' : '') + (d.msg || '')
        })
        .join('；')
    }
    const err = new Error(msg || ('请求失败 ' + res.status))
    err.status = res.status
    throw err
  }
  return data
}
