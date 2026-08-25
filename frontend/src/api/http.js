import { accountState } from '@/store/auth'

// 通用请求：自动携带登录令牌；adminToken 非空时优先使用（后台页单独存令牌）
export async function request(path, options = {}, adminToken = null) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, options.headers || {})
  const acc = accountState.account
  const tk = adminToken || (acc && acc.token ? acc.token : null)
  if (tk) headers['Authorization'] = 'Bearer ' + tk
  const res = await fetch(path, Object.assign({}, options, { headers }))
  let data = null
  try {
    data = await res.json()
  } catch (e) {
    data = {}
  }
  if (!res.ok) {
    const err = new Error(data.detail || ('请求失败 ' + res.status))
    err.status = res.status
    throw err
  }
  return data
}
