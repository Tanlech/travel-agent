import { request } from './http'

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
  chat(payload) {
    return request('/chat', { method: 'POST', body: JSON.stringify(payload) })
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
  kbReindex() {
    return request('/admin/kb/reindex', { method: 'POST' })
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
    return request('/admin/kb/attraction/spots?city=' + encodeURIComponent(city))
  },
  kbCreateSpot(payload) {
    return request('/admin/kb/attraction/spots', { method: 'POST', body: JSON.stringify(payload) })
  },
  kbDeleteSpot(city, name) {
    return request('/admin/kb/attraction/spots/' + encodeURIComponent(city) + '/' + encodeURIComponent(name), { method: 'DELETE' })
  },
  kbDeleteCity(city) {
    return request('/admin/kb/attraction/cities/' + encodeURIComponent(city), { method: 'DELETE' })
  },
}
