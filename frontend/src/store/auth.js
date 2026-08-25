import { reactive } from 'vue'

const AUTH_KEY = 'ta_account'
const USER_ID_KEY = 'ta_user_id'

function loadAccount() {
  try {
    return JSON.parse(localStorage.getItem(AUTH_KEY) || 'null')
  } catch (e) {
    return null
  }
}

export const accountState = reactive({ account: loadAccount() })

export function setAccount(acc) {
  accountState.account = acc
  if (acc) localStorage.setItem(AUTH_KEY, JSON.stringify(acc))
  else localStorage.removeItem(AUTH_KEY)
}

// 浏览器持久化的匿名用户标识：长期记忆按 user 维度累积，前端固定一个 user_id 让记忆跨会话生效
export function getUserId() {
  let uid = localStorage.getItem(USER_ID_KEY)
  if (!uid) {
    uid = 'u_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8)
    localStorage.setItem(USER_ID_KEY, uid)
  }
  return uid
}

// 登录后长期记忆用账号 user_id，未登录用匿名 getUserId()
export function memoryUserId() {
  const acc = accountState.account
  return (acc && acc.user && acc.user.user_id) || getUserId()
}

export function isAdmin() {
  const acc = accountState.account
  return !!(acc && acc.user && acc.user.role === 'admin')
}

export function accountDisplayName() {
  const acc = accountState.account
  return acc && acc.user ? (acc.user.display_name || acc.user.username) : null
}
