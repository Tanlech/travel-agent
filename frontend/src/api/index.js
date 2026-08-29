import { request } from './http'

// 构造查询串：encodeURIComponent 各键值，跳过空值
function q(params) {
  const sp = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${k}=${encodeURIComponent(v)}`)
  return sp.length ? '?' + sp.join('&') : ''
}

export const api = {
  register(payload) {
    return request('/auth/register', { method: 'POST', body: JSON.stringify(payload) })
  },
  login(payload) {
    return request('/auth/login', { method: 'POST', body: JSON.stringify(payload) })
  },
  logout(adminToken = null) {
    return request('/auth/logout', { method: 'POST' }, adminToken)
  },
  me(adminToken = null) {
    return request('/auth/me', {}, adminToken)
  },
  chat(payload, signal = null) {
    return request('/chat', { method: 'POST', body: JSON.stringify(payload), signal })
  },
  sessions() {
    return request('/sessions')
  },
  getSession(sid) {
    return request('/session/' + encodeURIComponent(sid))
  },
  deleteSession(sid) {
    return request('/session/' + encodeURIComponent(sid), { method: 'DELETE' })
  },
  memory(uid) {
    return request('/memory/' + encodeURIComponent(uid))
  },
  adminUsers(params) {
    return request('/admin/users?' + params.toString())
  },
  adminUser(userId) {
    return request('/admin/users/' + encodeURIComponent(userId))
  },
  adminSetStatus(userId, status) {
    return request('/admin/users/' + encodeURIComponent(userId) + '/status', {
      method: 'PATCH',
      body: JSON.stringify({ status }),
    })
  },
  adminDelete(userId) {
    return request('/admin/users/' + encodeURIComponent(userId), { method: 'DELETE' })
  },
  kbStatus() {
    return request('/admin/kb/status')
  },
  kbReindex(target) {
    return request('/admin/kb/reindex', { method: 'POST', body: JSON.stringify({ target: target ?? null }) })
  },
  kbConfig(payload) {
    return request('/admin/kb/config', { method: 'PATCH', body: JSON.stringify(payload) })
  },
  kbBases() {
    return request('/admin/kb/bases')
  },
  kbCities() {
    return request('/admin/kb/attraction/cities')
  },
  kbSpots(city) {
    return request('/admin/kb/attraction/spots' + q({ city }))
  },
  kbCreateSpot(payload) {
    return request('/admin/kb/attraction/spots', { method: 'POST', body: JSON.stringify(payload) })
  },
  kbAiGenerateSpot(payload) {
    return request('/admin/kb/attraction/spots/ai-generate', { method: 'POST', body: JSON.stringify(payload) })
  },
  kbBatchCreateSpots(payload) {
    return request('/admin/kb/attraction/spots/batch', { method: 'POST', body: JSON.stringify(payload) })
  },
  kbDeleteSpot(city, name) {
    return request(`/admin/kb/attraction/spots/${encodeURIComponent(city)}/${encodeURIComponent(name)}`, { method: 'DELETE' })
  },
  kbUpdateSpot(city, name, payload) {
    return request(`/admin/kb/attraction/spots/${encodeURIComponent(city)}/${encodeURIComponent(name)}`, { method: 'PUT', body: JSON.stringify(payload) })
  },
  kbDeleteCity(city) {
    return request('/admin/kb/attraction/cities/' + encodeURIComponent(city), { method: 'DELETE' })
  },

  kbTagLibrary() {
    return request('/admin/kb/attraction/tags/library')
  },
  kbCleanTags(city) {
    return request('/admin/kb/attraction/tags/clean' + q({ city }), { method: 'POST' })
  },
  kbCleanAllTags() {
    return request('/admin/kb/attraction/tags/clean-all', { method: 'POST' })
  },
  kbAddTag(body) {
    return request('/admin/kb/attraction/tags/add', { method: 'POST', body: JSON.stringify(body) })
  },
  kbDeleteTag(tag) {
    return request('/admin/kb/attraction/tags/delete', { method: 'POST', body: JSON.stringify({ tag }) })
  },
  kbUpdateTag(body) {
    return request('/admin/kb/attraction/tags/update', { method: 'POST', body: JSON.stringify(body) })
  },
  kbUpgradeTags() {
    return request('/admin/kb/attraction/tags/upgrade', { method: 'POST' })
  },
  kbQualityCheck(city) {
    return request('/admin/kb/attraction/quality-check' + q({ city }))
  },
  kbQualityAi(city) {
    return request('/admin/kb/attraction/quality-ai' + q({ city }), { method: 'POST' })
  },
  kbQualityAiAll() {
    return request('/admin/kb/attraction/quality-ai-all', { method: 'POST' })
  },
  kbQualityAiProvince(province) {
    return request('/admin/kb/attraction/quality-ai-province' + q({ province }), { method: 'POST' })
  },
  kbTaskStatus() {
    return request('/admin/kb/attraction/task')
  },
  kbQualityApply(actions) {
    return request('/admin/kb/attraction/quality-apply', { method: 'POST', body: JSON.stringify({ actions }) })
  },
}
