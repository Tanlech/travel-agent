import { request, authHeader } from './http'

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
  // SSE 流式对话：逐个解包 event/data 帧并回调 onPing/onStage/onToken/onDone/onError，
  // resolve 返回最后的 done 载荷（全量 ChatResponse）。用 fetch+ReadableStream（POST 请求，
  // EventSource 不支持），signal 用于中止。
  chatStream(payload, signal = null, handlers = {}) {
    const { onPing, onStage, onToken, onDone, onError } = handlers
    return fetch('/chat/stream', {
      method: 'POST',
      headers: authHeader(),
      body: JSON.stringify(payload),
      signal,
    }).then(async (res) => {
      if (!res.ok) {
        let msg = '请求失败 ' + res.status
        try { const d = await res.json(); msg = d.detail || msg } catch (e) { /* ignore */ }
        const err = new Error(msg)
        err.status = res.status
        if (onError) onError(err)
        throw err
      }
      if (!res.body) throw new Error('当前浏览器不支持流式响应')
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      let doneEvent = null
      const handleFrame = (frame) => {
        let event = 'message'
        let data = ''
        for (const line of frame.split('\n')) {
          if (line.startsWith('event:')) event = line.slice(6).trim()
          else if (line.startsWith('data:')) data = line.slice(5).trim()
        }
        if (!data) return
        let obj
        try { obj = JSON.parse(data) } catch (e) { return }
        if (event === 'ping') { if (onPing) onPing(obj) }
        else if (event === 'stage') { if (onStage) onStage(obj) }
        else if (event === 'token') { if (onToken) onToken(obj) }
        else if (event === 'done') { doneEvent = obj; if (onDone) onDone(obj) }
      }
      try {
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          buf += decoder.decode(value, { stream: true })
          let idx
          while ((idx = buf.indexOf('\n\n')) !== -1) {
            const frame = buf.slice(0, idx)
            buf = buf.slice(idx + 2)
            handleFrame(frame)
          }
        }
      } finally {
        reader.releaseLock()
      }
      return doneEvent
    })
  },
  sessions() {
    return request('/sessions')
  },
  getSession(sid) {
    return request('/session/' + encodeURIComponent(sid))
  },
  sessionEvents(sid) {
    return request('/session/' + encodeURIComponent(sid) + '/events')
  },
  undoSession(sid) {
    return request('/session/' + encodeURIComponent(sid) + '/undo', { method: 'POST' })
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
