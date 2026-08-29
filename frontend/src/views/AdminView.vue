<template>
  <div>
    <!-- 管理员登录 -->
    <div v-if="!authed" class="login-wrap">
      <div class="login-card">
        <h1>智能旅游管理后台</h1>
        <div class="sub">用户管理 · 需管理员账号登录</div>
        <label>用户名</label>
        <input v-model.trim="loginName" autocomplete="username" placeholder="请输入用户名" @keydown.enter="loginPassEl && loginPassEl.focus()">
        <label>密码</label>
        <input ref="loginPassEl" v-model="loginPass" type="password" autocomplete="current-password" placeholder="请输入密码" @keydown.enter="doLogin">
        <button class="btn" :disabled="loginLoading" @click="doLogin">登 录</button>
        <div class="err">{{ loginErr }}</div>
      </div>
    </div>

    <!-- 主面板 -->
    <div v-else class="main-view">
      <header>
        <h1>智能旅游管理后台</h1>
        <div class="spacer"></div>
        <span class="who">{{ whoami }}</span>
        <router-link to="/" class="btn ghost mini link-btn">← 返回聊天</router-link>
        <button class="btn ghost mini" @click="doLogout">退出登录</button>
      </header>

      <div class="body">
        <!-- 左侧功能菜单 -->
        <aside class="sidebar">
          <div class="nav-title">功能菜单</div>
          <button
            v-for="m in menus"
            :key="m.key"
            class="nav-item"
            :class="{ on: m.key === activeMenu }"
            @click="switchMenu(m.key)"
          >
            <span class="nav-ico">{{ m.icon }}</span>{{ m.label }}
          </button>
        </aside>

        <!-- 右侧内容区 -->
        <div class="panel">
          <div id="app-toast-host"></div>
          <div class="panel-head">
            <h2>{{ activeMenuTitle }}</h2>
          </div>
          <template v-if="activeMenu === 'users'">
          <div class="card">
            <div class="toolbar">
              <input v-model.trim="q" type="text" placeholder="搜索用户名 / 昵称" @keydown.enter="loadUsers(true)">
              <select v-model="fStatus" @change="loadUsers(true)">
                <option value="">全部状态</option>
                <option value="active">启用</option>
                <option value="disabled">禁用</option>
              </select>
              <select v-model="fSort" @change="loadUsers(true)">
                <option value="created_at">按注册时间</option>
                <option value="last_active_at">按最近活跃</option>
                <option value="username">按用户名</option>
              </select>
              <button class="btn mini" @click="loadUsers(true)">查询</button>
              <button class="btn ghost mini" @click="resetFilter">重置</button>
              <div class="grow"></div>
              <span class="count">{{ totalCount }}</span>
            </div>
            <table>
              <thead>
                <tr>
                  <th>用户名</th><th>昵称</th><th>角色</th><th>状态</th>
                  <th>行程数</th><th>注册时间</th><th>最近活跃</th><th>操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-if="!users.length">
                  <td colspan="8"><div class="empty" style="text-align:center;padding:24px">暂无用户</div></td>
                </tr>
                <tr v-for="u in users" :key="u.user_id">
                  <td>
                    <div class="uname">{{ u.username }}</div>
                    <div class="meta">{{ (u.user_id || '').slice(0, 12) }}</div>
                  </td>
                  <td>{{ u.display_name || '-' }}</td>
                  <td><span class="tag" :class="u.role === 'admin' ? 'admin' : 'user'">{{ u.role === 'admin' ? '管理员' : '用户' }}</span></td>
                  <td><span class="tag" :class="u.status === 'disabled' ? 'disabled' : 'active'">{{ u.status === 'disabled' ? '已禁用' : '正常' }}</span></td>
                  <td>{{ u.trips_count }}</td>
                  <td>{{ fmtDT(u.created_at) }}</td>
                  <td>{{ fmtDT(u.last_active_at) }}</td>
                  <td style="white-space:nowrap">
                    <button class="btn ghost mini" @click="showDetail(u.user_id)">详情</button>
                    <button v-if="u.status === 'disabled'" class="btn mini" @click="setStatus(u.user_id, 'active')">启用</button>
                    <button v-else class="btn ghost mini" @click="setStatus(u.user_id, 'disabled')">禁用</button>
                    <button class="btn danger mini" @click="delUser(u.user_id, u.username)">删除</button>
                  </td>
                </tr>
              </tbody>
            </table>
            <div class="pager">
              <button class="btn ghost mini" :disabled="page <= 1" @click="goPage(-1)">上一页</button>
              <span class="info">{{ pageInfo }}</span>
              <button class="btn ghost mini" :disabled="page >= pages" @click="goPage(1)">下一页</button>
            </div>
          </div>
          </template>
          <KnowledgeAdmin v-else-if="activeMenu === 'kb'" />
          <SchedulerAdmin v-else-if="activeMenu === 'scheduler'" />
        </div>
      </div>
    </div>

    <!-- 用户详情弹窗 -->
    <div v-if="detailVisible" class="modal-mask" @click.self="detailVisible = false">
      <div class="modal">
        <div class="m-head">
          <h2>👤 用户详情</h2>
          <button class="m-close" @click="detailVisible = false">✕</button>
        </div>
        <div class="m-body" v-html="detailHtml"></div>
      </div>
    </div>

    <!-- 删除确认弹框 -->
    <div v-if="confirmBox" class="modal-mask" @click.self="confirmBox = null">
      <div class="modal modal-sm">
        <div class="m-head"><h2>操作确认</h2><button class="m-close" @click="confirmBox = null">✕</button></div>
        <div class="m-body">{{ confirmBox.text }}</div>
        <div class="m-foot">
          <button class="btn ghost" @click="confirmBox = null">取消</button>
          <button class="btn danger" @click="confirmOk">确认删除</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, nextTick } from 'vue'
import { api } from '@/api'
import { esc, fmtDT, toast } from '@/utils/format'
import KnowledgeAdmin from '@/components/KnowledgeAdmin.vue'
import SchedulerAdmin from '@/components/SchedulerAdmin.vue'

const TOKEN_KEY = 'ta_admin_token'

const token = () => localStorage.getItem(TOKEN_KEY)
const setToken = (t) => (t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY))

const authed = ref(false)
const whoami = ref('')
const loginName = ref('')
const loginPass = ref('')
const loginPassEl = ref(null)
const loginErr = ref('')
const loginLoading = ref(false)

const page = ref(1)
const pageSize = 20
const total = ref(0)
const users = ref([])
const q = ref('')
const fStatus = ref('')
const fSort = ref('created_at')

const detailVisible = ref(false)
const detailHtml = ref('')

// 应用内确认弹框（替代浏览器原生 confirm，避免出现 "localhost:8001 显示…" 标题）
const confirmBox = ref(null) // { text, onOk }
function askConfirm(text, onOk) {
  confirmBox.value = { text, onOk }
}
function confirmOk() {
  const box = confirmBox.value
  confirmBox.value = null
  if (box && box.onOk) box.onOk()
}

// 后台左侧功能菜单：新增模块只需往 menus 里追加一项
const menus = [
  { key: 'users', icon: '👥', label: '用户管理' },
  { key: 'kb', icon: '🗂️', label: '知识库管理' },
  { key: 'scheduler', icon: '⏰', label: '定时任务' },
]
const activeMenu = ref('users')
const activeMenuTitle = computed(() => {
  const m = menus.find((x) => x.key === activeMenu.value)
  return m ? m.label : ''
})
function switchMenu(key) {
  activeMenu.value = key
}

const pages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))
const totalCount = computed(() => '共 ' + total.value + ' 个用户')
const pageInfo = computed(() => '第 ' + page.value + ' / ' + pages.value + ' 页')

async function adminApi(path, options = {}) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, options.headers || {})
  const tk = token()
  if (tk) headers['Authorization'] = 'Bearer ' + tk
  const res = await fetch(path, Object.assign({}, options, { headers }))
  if (res.status === 401) {
    showLogin('登录已过期，请重新登录')
    throw new Error('unauthorized')
  }
  let data = null
  try { data = await res.json() } catch (e) { data = {} }
  if (!res.ok) throw new Error(data.detail || ('请求失败 ' + res.status))
  return data
}

async function doLogin() {
  if (!loginName.value || !loginPass.value) { loginErr.value = '请输入用户名和密码'; return }
  loginLoading.value = true
  loginErr.value = ''
  try {
    const data = await adminApi('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username: loginName.value, password: loginPass.value }),
    })
    if (data.user.role !== 'admin') throw new Error('该账号不是管理员，无法进入后台')
    setToken(data.token)
    whoami.value = (data.user.display_name || data.user.username) + '（管理员）'
    authed.value = true
    loadUsers(true)
  } catch (e) {
    loginErr.value = e.message
  } finally {
    loginLoading.value = false
  }
}

function doLogout() {
  adminApi('/auth/logout', { method: 'POST' }).catch(() => {})
  setToken('')
  showLogin('')
}

function showLogin(msg) {
  authed.value = false
  loginErr.value = msg
}

async function loadUsers(reset = false) {
  if (reset) page.value = 1
  const params = new URLSearchParams({ page: page.value, page_size: pageSize, sort: fSort.value })
  if (q.value) params.set('search', q.value)
  if (fStatus.value) params.set('status', fStatus.value)
  try {
    const data = await adminApi('/admin/users?' + params.toString())
    total.value = data.total
    users.value = data.users || []
  } catch (e) {
    if (e.message !== 'unauthorized') toast(e.message, 'error')
  }
}

function resetFilter() {
  q.value = ''
  fStatus.value = ''
  fSort.value = 'created_at'
  loadUsers(true)
}

function goPage(delta) {
  const next = page.value + delta
  if (next < 1 || next > pages.value) return
  page.value = next
  loadUsers()
}

async function setStatus(userId, status) {
  try {
    await adminApi('/admin/users/' + encodeURIComponent(userId) + '/status', {
      method: 'PATCH',
      body: JSON.stringify({ status }),
    })
    toast(status === 'disabled' ? '已禁用该用户' : '已启用该用户')
    loadUsers()
  } catch (e) {
    if (e.message !== 'unauthorized') toast(e.message, 'error')
  }
}

function delUser(userId, username) {
  askConfirm(`确定删除用户「${username}」？\n将同时删除其登录令牌、偏好记忆、历史行程和全部会话，且不可恢复。`, async () => {
    try {
      const data = await adminApi('/admin/users/' + encodeURIComponent(userId), { method: 'DELETE' })
      toast('已删除用户，清理会话 ' + data.deleted_sessions + ' 个')
      loadUsers()
    } catch (e) {
      if (e.message !== 'unauthorized') toast(e.message, 'error')
    }
  })
}

async function showDetail(userId) {
  detailVisible.value = true
  detailHtml.value = '<div class="empty">加载中...</div>'
  try {
    const data = await adminApi('/admin/users/' + encodeURIComponent(userId))
    renderDetail(data)
  } catch (e) {
    detailHtml.value = '<div class="empty">加载失败：' + esc(e.message) + '</div>'
  }
}

function renderDetail(data) {
  const u = data.user
  const stTag = u.status === 'disabled' ? '已禁用' : '正常'
  let html = '<div class="m-sec">账号信息</div>' +
    '<div class="kv">' +
    '<span class="k">用户名</span><span>' + esc(u.username) + '</span>' +
    '<span class="k">昵称</span><span>' + esc(u.display_name || '-') + '</span>' +
    '<span class="k">角色</span><span>' + (u.role === 'admin' ? '管理员' : '用户') + '</span>' +
    '<span class="k">状态</span><span>' + stTag + '</span>' +
    '<span class="k">注册时间</span><span>' + fmtDT(u.created_at) + '</span>' +
    '<span class="k">最近活跃</span><span>' + fmtDT(u.last_active_at) + '</span>' +
    '<span class="k">用户ID</span><span style="word-break:break-all">' + esc(u.user_id) + '</span>' +
    '</div>'

  html += '<div class="m-sec">偏好记忆</div>'
  const p = data.preferences
  if (!p) {
    html += '<div class="empty">暂无偏好记录</div>'
  } else {
    const rows = [
      ['正向偏好', p.preferred_styles && p.preferred_styles.length ? p.preferred_styles.join('、') : null],
      ['负面偏好', p.disliked_styles && p.disliked_styles.length ? p.disliked_styles.join('、') : null],
      ['节奏偏好', p.pace_preference ? ({ relaxed: '轻松', dense: '紧凑' }[p.pace_preference] || p.pace_preference) : null],
      ['主题乐园', p.accept_theme_park == null ? null : (p.accept_theme_park ? '接受' : '不接受')],
      ['夜生活/演艺', p.accept_nightlife == null ? null : (p.accept_nightlife ? '接受' : '不接受')],
      ['亲子', p.family_friendly == null ? null : (p.family_friendly ? '适合带娃' : '不适合')],
      ['长辈', p.senior_friendly == null ? null : (p.senior_friendly ? '适合老人' : '不适合')],
    ].filter(([, v]) => v != null)
    html += '<div class="kv">' + (rows.length ? rows.map(([k, v]) => '<span class="k">' + k + '</span><span>' + esc(v) + '</span>').join('') : '<span class="k">-</span><span>暂无已确认偏好</span>') + '</div>'
  }

  html += '<div class="m-sec">历史行程（' + (data.trip_memories || []).length + '）</div>'
  const trips = data.trip_memories || []
  if (!trips.length) {
    html += '<div class="empty">暂无行程记忆</div>'
  } else {
    html += trips.map((t) => {
      const spots = (t.accepted_spots || []).join('、')
      const rej = (t.rejected_spots || []).join('、')
      return '<div class="m-row">' +
        '<b>' + esc(t.destination || '未定') + '</b>' + (t.days ? ' · ' + t.days + '天' : '') +
        (t.budget ? ' · 预算 ¥' + esc(String(t.budget)) : '') +
        (t.created_at ? ' · ' + fmtDT(t.created_at) : '') +
        (spots ? '<div><span class="k">景点：</span>' + esc(spots) + '</div>' : '') +
        (rej ? '<div><span class="k">排除：</span>' + esc(rej) + '</div>' : '') +
        (t.summary ? '<div style="color:#555">' + esc(t.summary) + '</div>' : '') +
        '</div>'
    }).join('')
  }

  html += '<div class="m-sec">会话记录（' + (data.sessions || []).length + '）</div>'
  const sessions = data.sessions || []
  if (!sessions.length) {
    html += '<div class="empty">暂无会话</div>'
  } else {
    html += sessions.map((s) => {
      const stageMap = { ready_to_plan: '待规划', clarify: '待补充', planning: '规划中', revise_collecting: '改稿中', revise_ready: '待改稿', qa: '对话', completed: '已完成' }
      return '<div class="m-row">' +
        '<div><b>' + esc(s.title || '（无标题）') + '</b> · ' + (stageMap[s.stage] || s.stage) +
        ' · 消息 ' + s.message_count + ' · ' + (s.has_plan ? '有行程' : '无行程') +
        ' · ' + fmtDT(s.updated_at) + '</div>' +
        '<div class="meta" style="color:var(--muted);font-size:12px">' + esc(s.session_id) + '</div>' +
        '</div>'
    }).join('')
  }
  detailHtml.value = html
}

onMounted(async () => {
  if (!token()) { showLogin(''); return }
  try {
    const me = await adminApi('/auth/me')
    if (me.user.role !== 'admin') throw new Error('no admin')
    whoami.value = (me.user.display_name || me.user.username) + '（管理员）'
    authed.value = true
    loadUsers(true)
  } catch (e) {
    showLogin(e.message === 'no admin' ? '该账号不是管理员' : '')
  }
})
</script>

<style scoped>
.login-wrap { height: 100vh; display: flex; align-items: center; justify-content: center; background: linear-gradient(135deg,#5d79f8 0%,#4668ec 55%,#3a56cf 100%); }
.login-card { width: 360px; background: #fff; border-radius: 16px; padding: 34px 32px; box-shadow: 0 18px 50px rgba(30,40,90,.28); }
.login-card h1 { font-size: 20px; font-weight: 700; margin-bottom: 6px; }
.login-card .sub { font-size: 12px; color: var(--muted); margin-bottom: 24px; }
.login-card label { display: block; font-size: 12px; color: var(--muted); margin: 14px 0 6px; }
.login-card input { width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: 9px; font-size: 14px; outline: none; transition: border-color .2s; }
.login-card input:focus { border-color: var(--primary); }
.login-card .err { color: var(--danger); font-size: 12px; margin-top: 10px; min-height: 16px; }
.main-view { height: 100vh; flex-direction: column; display: flex; }
header { display: flex; align-items: center; gap: 12px; padding: 0 24px; height: 58px; background: #fff; border-bottom: 1px solid var(--border); flex-shrink: 0; }
header h1 { font-size: 16px; font-weight: 700; }
header .spacer { flex: 1; }
header .who { font-size: 13px; color: var(--muted); }
.link-btn { text-decoration: none; }
.body { flex: 1; display: flex; overflow: hidden; }
.sidebar { width: 216px; flex-shrink: 0; background: #fff; border-right: 1px solid var(--border); padding: 16px 12px; overflow-y: auto; }
.nav-title { font-size: 12px; color: var(--muted); padding: 2px 10px 10px; }
.nav-item { display: flex; align-items: center; gap: 9px; width: 100%; padding: 10px 12px; border: none; background: transparent; border-radius: 9px; font-size: 13px; cursor: pointer; color: var(--text); text-align: left; margin-bottom: 4px; transition: background .15s, color .15s; }
.nav-item:hover { background: var(--primary-light); color: var(--primary); }
.nav-item.on { background: var(--primary); color: #fff; font-weight: 600; }
.nav-ico { width: 18px; text-align: center; font-size: 14px; }
.panel { flex: 1; overflow-y: auto; padding: 20px 24px; }
.panel-head { margin-bottom: 14px; }
.panel-head h2 { font-size: 16px; font-weight: 700; }
.card { background: #fff; border-radius: 12px; border: 1px solid var(--border); }
.toolbar { display: flex; align-items: center; gap: 10px; padding: 14px 16px; flex-wrap: wrap; }
.toolbar input[type=text], .toolbar select { padding: 8px 12px; border: 1px solid var(--border); border-radius: 8px; font-size: 13px; outline: none; }
.toolbar input:focus, .toolbar select:focus { border-color: var(--primary); }
.toolbar .grow { flex: 1; }
.toolbar .count { font-size: 12px; color: var(--muted); }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 12px 16px; font-size: 13px; border-bottom: 1px solid var(--border); }
th { color: var(--muted); font-weight: 600; background: #fafbff; white-space: nowrap; }
tr:hover td { background: #fafbff; }
td .uname { font-weight: 600; }
td .meta { font-size: 12px; color: var(--muted); margin-top: 2px; }
.tag { display: inline-block; padding: 2px 9px; border-radius: 20px; font-size: 12px; }
.tag.active { background: #e6f7ef; color: var(--success); }
.tag.disabled { background: #fdecec; color: var(--danger); }
.tag.admin { background: #eef1ff; color: var(--primary); }
.tag.user { background: #f1f2f5; color: var(--muted); }
.pager { display: flex; align-items: center; gap: 12px; padding: 14px 16px; justify-content: flex-end; }
.pager .info { font-size: 12px; color: var(--muted); }
.kv { display: grid; grid-template-columns: 90px 1fr; row-gap: 6px; font-size: 13px; }
.kv .k { color: var(--muted); }
</style>