<template>
  <div class="chat-body">
    <!-- 左侧多对话栏 -->
    <aside id="sidebar" :style="{ width: sidebarWidth + 'px', minWidth: sidebarWidth + 'px' }">
      <div class="brand"><span class="logo">✈️</span><span>Travel Agent</span></div>
      <button id="newConv" @click="newConv">＋ 新建对话</button>
      <div id="convList">
        <div
          v-for="c in sorted"
          :key="c.id"
          class="conv"
          :class="{ active: c.id === activeId }"
          @click="switchConv(c.id)"
        >
          <div class="meta">
            <div class="title">{{ c.title || '新对话' }}</div>
            <div class="stamp">{{ c.messages.length ? (c.messages[c.messages.length - 1].time || '') : '' }}</div>
          </div>
          <button class="del" title="删除对话" @click.stop="delConv(c.id)">×</button>
        </div>
      </div>
      <div v-if="!sorted.length" id="convEmpty">还没有对话<br>点击"新建对话"开始</div>
    </aside>
    <div id="resizer" @mousedown="startResize"></div>

    <!-- 主区 -->
    <section id="main">
      <div id="topbar">
        <h1>🌍 旅行规划助手</h1>
        <div class="sub">{{ activeTitle }}</div>
        <div class="spacer"></div>
        <div class="acct">
          <button class="tb-btn" @click="acctClick">{{ acctLabel }}</button>
          <div v-if="acctOpen" class="acct-dd">
            <div class="name">{{ acctName }}</div>
            <button class="opt" @click="doLogout">退出登录</button>
          </div>
        </div>
        <button v-if="isAdmin()" class="tb-btn" @click="goAdmin">后台管理</button>
        <button class="tb-btn" @click="showHist">对话历史</button>
        <button class="tb-btn" @click="showMem">对话长期记忆</button>
        <button class="tb-btn" @click="copyConv">复制对话记录</button>
      </div>

      <div ref="messagesEl" id="messages">
        <div class="inner">
          <template v-if="current">
            <div v-if="!current.messages.length" class="msg ai">
              <div class="bubble">你好！我是旅行规划助手。告诉我你想去哪玩、什么时候出发，我来帮你规划行程。</div>
            </div>
            <div v-for="m in current.messages" :key="m.uid" class="msg" :class="m.role === 'user' ? 'user' : 'ai'">
              <div class="head">
                <span v-if="m.role === 'user'" class="avatar user">我</span>
                <span v-if="m.role !== 'user'" class="avatar ai">AI</span>
                <span style="font-size:11px;color:#8a8fa0">{{ msgMeta(m) }}</span>
              </div>
              <div class="bubble">
                <div v-if="m.role === 'user' || m.mode === 'error'" class="plain">{{ m.text }}</div>
                <div v-else class="md" v-html="msgMarkdown(m)"></div>
              </div>
              <PlanCard v-if="m.plan" :plan="m.plan" @show-map="planForMap = $event" />
            </div>
            <div v-if="sending" class="msg ai">
              <div class="loading show">
                <span class="dot"></span><span class="dot"></span><span class="dot"></span>
                <span>正在思考...</span>
              </div>
            </div>
          </template>
          <template v-else>
            <div class="msg ai">
              <div class="bubble">你好！我是旅行规划助手。告诉我你想去哪玩、什么时候出发，我来帮你规划行程。</div>
            </div>
          </template>
        </div>
      </div>

      <div id="composer">
        <div class="box">
          <div class="row">
            <textarea
              ref="inputEl"
              v-model="input"
              rows="1"
              placeholder="例如：我想去北京玩，8月10号到12号，两个人…"
              @keydown="onKeydown"
              @input="autoResize"
            ></textarea>
            <button id="send" :disabled="sending" @click="send">发送</button>
          </div>
        </div>
        <div class="hint">提示：规划可能需要 1-2 分钟（调用高德地图 + 大模型）。多轮对话会自动累计出行需求，每条回复会显示耗时。</div>
      </div>
    </section>

    <!-- 对话历史弹窗 -->
    <div v-if="histVisible" class="modal-mask" @click.self="histVisible = false">
      <div class="modal">
        <div class="m-head"><h2>📋 对话历史</h2><button class="m-close" @click="histVisible = false">✕</button></div>
        <div class="m-body" v-html="histHtml"></div>
      </div>
    </div>

    <!-- 对话长期记忆弹窗 -->
    <div v-if="memVisible" class="modal-mask" @click.self="memVisible = false">
      <div class="modal">
        <div class="m-head"><h2>🧠 对话长期记忆</h2><button class="m-close" @click="memVisible = false">✕</button></div>
        <div class="m-body" v-html="memHtml"></div>
      </div>
    </div>

    <!-- 行程地图弹窗 -->
    <PlanMapModal v-if="planForMap" :plan="planForMap" @close="planForMap = null" />
  </div>
</template>

<script setup>
import { ref, computed, onMounted, nextTick, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api'
import { accountState, setAccount, getUserId, memoryUserId, isAdmin, accountDisplayName } from '@/store/auth'
import { chatState, saveStore, currentConversation, newConversation as storeNewConv, deleteConversation as storeDelConv, sortedConversations } from '@/store/chat'
import { esc, nowTime, fmtDuration, genId, renderMarkdown, toast } from '@/utils/format'
import PlanCard from '@/components/PlanCard.vue'
import PlanMapModal from '@/components/PlanMapModal.vue'

const router = useRouter()

const input = ref('')
const sending = ref(false)
const typing = ref(null) // { id, text }
const sidebarWidth = ref(272)
const acctOpen = ref(false)
const inputEl = ref(null)
const messagesEl = ref(null)

const histVisible = ref(false)
const histHtml = ref('')
const memVisible = ref(false)
const memHtml = ref('')
const planForMap = ref(null)

const activeId = computed(() => chatState.activeId)
const current = computed(() => currentConversation())
const sorted = computed(() => sortedConversations())
const activeTitle = computed(() => {
  const c = currentConversation()
  return c ? c.title || '新对话' : '选择或新建一个对话'
})
const acctLabel = computed(() => {
  const name = accountDisplayName()
  return name ? '👤 ' + name : '登录 / 注册'
})
const acctName = computed(() => {
  const acc = accountState.account
  if (!acc || !acc.user) return ''
  return (acc.user.display_name || acc.user.username) + '（' + (acc.user.role === 'admin' ? '管理员' : '用户') + '）'
})

// ===================== 会话管理 =====================
function newConv() {
  const c = storeNewConv()
  chatState.activeId = c.id
  inputEl.value && inputEl.value.focus()
  nextTick(scrollToBottom)
}

function switchConv(id) {
  chatState.activeId = id
  saveStore()
  const c = currentConversation()
  if (c && c.sid && !c.messages.length) {
    loadServerMessages(c).then(() => { nextTick(scrollToBottom) })
  }
  inputEl.value && inputEl.value.focus()
  nextTick(scrollToBottom)
}

async function loadServerMessages(c) {
  try {
    const data = await api.getSession(c.sid)
    if (data.status !== 'ok' || !Array.isArray(data.recent_messages)) return
    c.messages = data.recent_messages.map((m) => ({
      uid: genId(),
      role: m.role === 'user' ? 'user' : 'assistant',
      text: typeof m.content === 'string' ? m.content : '',
      time: '',
    }))
    // 服务端返回的完整行程产物挂到最后一条 AI 消息上，恢复历史会话时也能显示行程卡
    if (data.plan) {
      for (let i = c.messages.length - 1; i >= 0; i--) {
        if (c.messages[i].role !== 'user') {
          c.messages[i].plan = data.plan
          break
        }
      }
    }
    if (data.has_plan && !c.title) c.title = '行程会话'
    saveStore()
  } catch (e) { /* 网络异常忽略 */ }
}

async function delConv(id) {
  if (!confirm('删除这个对话？')) return
  const target = storeDelConv(id)
  if (target && target.sid) {
    api.deleteSession(target.sid).catch(() => {})
  }
  nextTick(scrollToBottom)
}

// ===================== 消息渲染 =====================
function msgMeta(m) {
  const time = m.time || ''
  const dur = m.role !== 'user' && m.duration ? ' · ' + fmtDuration(m.duration) : ''
  return time + dur
}
function msgMarkdown(m) {
  const text = typing.value && typing.value.id === m.uid ? typing.value.text : m.text
  return renderMarkdown(text) + (typing.value && typing.value.id === m.uid ? '<span class="caret"></span>' : '')
}
function typeWriterFor(msg, fullText, onDone) {
  typing.value = { id: msg.uid, text: '' }
  let i = 0
  const timer = setInterval(() => {
    i += 2
    typing.value = { id: msg.uid, text: fullText.slice(0, Math.min(i, fullText.length)) }
    scrollToBottom()
    if (i >= fullText.length) {
      clearInterval(timer)
      typing.value = null
      if (onDone) onDone()
    }
  }, 16)
}

function scrollToBottom() {
  nextTick(() => {
    if (messagesEl.value) messagesEl.value.scrollTop = messagesEl.value.scrollHeight
  })
}

watch(() => chatState.conversations.map((c) => c.messages.length).join(','), () => {
  if (!typing.value) scrollToBottom()
})

// ===================== 发送 =====================
function onKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    send()
  }
}

async function send() {
  const text = input.value.trim()
  if (!text) return
  if (!chatState.activeId) newConv()
  const c = currentConversation()
  if (!c) return

  input.value = ''
  autoResize()
  sending.value = true

  const t0 = performance.now()
  const userMsg = { uid: genId(), role: 'user', text, time: nowTime() }
  c.messages.push(userMsg)
  c.updatedAt = Date.now()
  if (c.title === '新对话') c.title = text.slice(0, 18)
  nextTick(scrollToBottom)

  try {
    const res = await api.chat({ session_id: c.sid, user_id: memoryUserId(), message: text })
    if (res.status === 401) {
      setAccount(null)
      toast('登录已过期，请重新登录')
      router.push('/login')
      return
    }
    if (res.session_id) c.sid = res.session_id
    const duration = performance.now() - t0
    // 必须优先用后端真实回复，不能用用户输入文本当回复
    const replyText = res.summary || res.follow_up_question || '(无回复)'
    const aiMsg = {
      uid: genId(),
      role: 'assistant',
      text: replyText,
      mode: res.mode,
      time: nowTime(),
      duration,
      plan: res.plan || null,
    }
    c.messages.push(aiMsg)
    c.updatedAt = Date.now()
    saveStore()
    if (aiMsg.mode === 'error') {
      nextTick(scrollToBottom)
    } else {
      typeWriterFor(aiMsg, replyText)
    }
  } catch (e) {
    const dur = performance.now() - t0
    c.messages.push({ uid: genId(), role: 'assistant', text: '请求失败：' + e.message, mode: 'error', time: nowTime(), duration: dur })
    nextTick(scrollToBottom)
  } finally {
    sending.value = false
    saveStore()
    inputEl.value && inputEl.value.focus()
  }
}

// ===================== 账号 / 后台 =====================
function acctClick() {
  if (accountState.account && accountState.account.user) {
    acctOpen.value = !acctOpen.value
  } else {
    router.push('/login')
  }
}
function goAdmin() {
  const acc = accountState.account
  if (!acc || !acc.token) { router.push('/login'); return }
  localStorage.setItem('ta_admin_token', acc.token)
  router.push('/admin')
}
function doLogout() {
  const acc = accountState.account
  if (acc && acc.token) api.logout().catch(() => {})
  setAccount(null)
  acctOpen.value = false
  toast('已退出登录')
}

// ===================== 对话历史 / 长期记忆 =====================
async function showHist() {
  const c = currentConversation()
  if (!c) { toast('请先选择或新建对话'); return }
  histVisible.value = true
  histHtml.value = '<div style="color:var(--muted)">加载中...</div>'

  let msgs = c.messages
  if (!msgs.length && c.sid) {
    try {
      const data = await api.getSession(c.sid)
      if (data.status === 'ok' && Array.isArray(data.recent_messages)) {
        msgs = data.recent_messages.map((m) => ({
          role: m.role === 'user' ? 'user' : 'assistant',
          text: m.content || '',
          time: '',
          duration: null,
          mode: '',
          plan: null,
        }))
        if (data.plan) {
          for (let i = msgs.length - 1; i >= 0; i--) {
            if (msgs[i].role !== 'user') { msgs[i].plan = data.plan; break }
          }
        }
      }
    } catch (e) { /* 拉取失败回落本地 */ }
  }

  let html = '<div style="margin-bottom:12px;color:var(--muted)">会话：' + esc(c.title || '新对话') +
    (c.sid ? '（ID：' + esc(c.sid) + '）' : '') + ' · 共 ' + msgs.length + ' 条消息</div>'
  if (!msgs.length) {
    html += '<div style="color:var(--muted)">暂无消息</div>'
  } else {
    html += msgs.map((m) => {
      const who = m.role === 'user' ? '我' : 'AI'
      const meta = [m.time, !m.duration ? '' : fmtDuration(m.duration), m.mode && m.mode !== 'qa' ? m.mode : '']
        .filter(Boolean).join(' · ')
      const head = meta ? '[' + who + ' | ' + meta + ']' : '[' + who + ']'
      const planBrief = m.plan && m.plan.destination
        ? '<div style="color:var(--primary);font-weight:600;margin-top:4px">🗺️ ' + esc(m.plan.destination) + ' 行程方案</div>'
        : ''
      return '<div class="m-row">' +
        '<div class="m-meta">' + esc(head) + '</div>' +
        '<div class="m-text">' + esc(m.text || '') + '</div>' + planBrief + '</div>'
    }).join('')
  }
  histHtml.value = html
}

async function showMem() {
  memVisible.value = true
  memHtml.value = '<div style="color:var(--muted)">加载中...</div>'
  try {
    const data = await api.memory(memoryUserId())
    if (data.status !== 'ok') throw new Error('接口异常')
    let html = '<div style="margin-bottom:12px;color:var(--muted)">用户：' + esc(data.user_id || '匿名') +
      ' · 偏好与历史行程会跨对话持续累积</div>'

    html += '<div class="m-sec">📌 用户偏好</div>'
    const um = data.user_memory
    if (!um) {
      html += '<div style="color:var(--muted)">暂无偏好记录（对话中表达偏好后才会累积）</div>'
    } else {
      const items = [
        ['正向偏好', um.preferred_styles && um.preferred_styles.length ? um.preferred_styles.join('、') : null],
        ['负面偏好', um.disliked_styles && um.disliked_styles.length ? um.disliked_styles.join('、') : null],
        ['节奏偏好', um.pace_preference ? ({ relaxed: '轻松', dense: '紧凑' }[um.pace_preference] || um.pace_preference) : null],
        ['主题乐园', um.accept_theme_park == null ? null : (um.accept_theme_park ? '接受' : '不接受')],
        ['夜生活/演艺', um.accept_nightlife == null ? null : (um.accept_nightlife ? '接受' : '不接受')],
        ['亲子', um.family_friendly == null ? null : (um.family_friendly ? '适合带娃' : '不适合带娃')],
        ['长辈', um.senior_friendly == null ? null : (um.senior_friendly ? '适合老人' : '不适合老人')],
      ].filter(([, v]) => v != null && v !== '')
      if (!items.length) {
        html += '<div style="color:var(--muted)">暂无已确认偏好</div>'
      } else {
        html += items.map(([k, v]) => '<div><span style="color:var(--muted)">' + k + '：</span>' + esc(v) + '</div>').join('')
      }
    }

    const trips = data.trip_memories || []
    html += '<div class="m-sec">🗺️ 历史行程（' + trips.length + '）</div>'
    if (!trips.length) {
      html += '<div style="color:var(--muted)">暂无行程记忆（规划完成后会记录）</div>'
    } else {
      html += trips.map((t) => {
        const spots = (t.accepted_spots || []).join('、')
        const rej = (t.rejected_spots || []).join('、')
        const when = t.created_at ? ' · ' + esc(String(t.created_at).slice(0, 10)) : ''
        return '<div class="m-row">' +
          '<div><b>' + esc(t.destination || '未定') + '</b>' + (t.days ? ' · ' + t.days + '天' : '') +
          (t.budget ? ' · 预算 ¥' + esc(String(t.budget)) : '') + when + '</div>' +
          (spots ? '<div><span style="color:var(--muted)">景点：</span>' + esc(spots) + '</div>' : '') +
          (rej ? '<div><span style="color:var(--muted)">排除：</span>' + esc(rej) + '</div>' : '') +
          (t.summary ? '<div style="color:#555">' + esc(t.summary) + '</div>' : '') +
          '</div>'
      }).join('')
    }
    memHtml.value = html
  } catch (e) {
    memHtml.value = '<div style="color:#c62828">加载失败：' + esc(e.message) + '</div>'
  }
}

// ===================== 复制对话记录 =====================
function copyConv() {
  const c = currentConversation()
  if (!c || !c.messages.length) { toast('当前对话暂无消息'); return }
  const lines = c.messages.map((m) => {
    const who = m.role === 'user' ? '我' : 'AI'
    const meta = [m.time, m.role !== 'user' && m.duration ? fmtDuration(m.duration) : '', m.mode !== 'qa' && m.mode ? m.mode : '']
      .filter(Boolean).join(' · ')
    const head = meta ? '[' + who + ' | ' + meta + '] ' : '[' + who + '] '
    return head + m.text
  })
  const text = (c.title ? '【对话】' + c.title + '\n' : '') + lines.join('\n')
  navigator.clipboard.writeText(text)
    .then(() => toast('对话记录已复制到剪贴板'))
    .catch(() => {
      const ta = document.createElement('textarea')
      ta.value = text; document.body.appendChild(ta); ta.select()
      document.execCommand('copy'); ta.remove()
      toast('对话记录已复制')
    })
}

// ===================== 输入框 / 侧栏拖拽 =====================
function autoResize() {
  const el = inputEl.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 140) + 'px'
}

function startResize(e) {
  e.preventDefault()
  const startX = e.clientX
  const startW = sidebarWidth.value
  document.body.classList.add('resizing')
  const onMove = (ev) => {
    const w = Math.max(200, Math.min(440, startW + (ev.clientX - startX)))
    sidebarWidth.value = w
  }
  const onUp = () => {
    document.body.classList.remove('resizing')
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
  }
  document.addEventListener('mousemove', onMove)
  document.addEventListener('mouseup', onUp)
}

// ===================== 初始化 =====================
async function syncServerConversations() {
  try {
    const data = await api.sessions()
    if (data.status !== 'ok' || !Array.isArray(data.sessions)) return
    const sers = data.sessions.filter((s) => s.session_id)
    sers.forEach((s, idx) => {
      if (chatState.conversations.some((c) => c.sid === s.session_id)) return
      chatState.conversations.push({
        id: genId(),
        sid: s.session_id,
        title: (s.title || '历史对话').slice(0, 18) || '历史对话',
        updatedAt: Date.now() + (idx * 1000),
        messages: [],
      })
    })
    if (chatState.conversations.length) saveStore()
  } catch (e) { /* 服务端不可用时仅本地 */ }
}

function restoreSessions() {
  if (chatState.conversations.length) {
    chatState.activeId = chatState.conversations[0].id
    const c = currentConversation()
    if (c && c.sid) loadServerMessages(c).then(() => nextTick(scrollToBottom))
  } else {
    storeNewConv()
  }
  nextTick(scrollToBottom)
}

onMounted(() => {
  if (chatState.conversations.length) {
    chatState.activeId = chatState.conversations[0].id
    nextTick(scrollToBottom)
  } else {
    syncServerConversations().then(restoreSessions)
  }
})
</script>

<style scoped>
.chat-body { display: flex; height: 100vh; overflow: hidden; }
#sidebar {
  width: 272px; min-width: 200px; max-width: 440px; background: var(--sidebar-grad);
  color: var(--sidebar-text); display: flex; flex-direction: column; height: 100vh; flex-shrink: 0;
  box-shadow: 2px 0 12px rgba(70,100,220,.18); z-index: 2;
}
#sidebar .brand { display: flex; align-items: center; gap: 10px; padding: 18px 16px; color: #fff; font-size: 15px; font-weight: 700; letter-spacing: .3px; }
#sidebar .brand .logo { width: 34px; height: 34px; border-radius: 9px; display: flex; align-items: center; justify-content: center; background: linear-gradient(135deg,#4f6ef7,#7c5cf0); font-size: 18px; flex-shrink: 0; }
#newConv { margin: 4px 12px 12px; display: flex; align-items: center; justify-content: center; gap: 8px; padding: 10px; border: 1px solid rgba(255,255,255,.14); border-radius: 9px; cursor: pointer; background: rgba(255,255,255,.06); color: #fff; font-size: 13px; font-weight: 600; transition: background .2s; }
#newConv:hover { background: rgba(255,255,255,.14); }
#convList { flex: 1; overflow-y: auto; padding: 0 8px 10px; }
.conv { display: flex; align-items: center; gap: 8px; padding: 10px; border-radius: 9px; cursor: pointer; transition: background .15s; position: relative; margin-bottom: 4px; }
.conv:hover { background: rgba(255,255,255,.08); }
.conv.active { background: rgba(255,255,255,.22); }
.conv .meta { flex: 1; min-width: 0; }
.conv .title { font-size: 13px; color: #eef0f6; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.conv .stamp { font-size: 11px; color: var(--sidebar-text); opacity: .7; margin-top: 2px; }
.conv .del { opacity: 0; width: 22px; height: 22px; border-radius: 5px; border: none; cursor: pointer; background: transparent; color: #aab; font-size: 14px; line-height: 1; flex-shrink: 0; }
.conv:hover .del { opacity: 1; }
.conv .del:hover { background: rgba(255,80,80,.35); color: #fff; }
#convEmpty { padding: 20px; text-align: center; font-size: 12px; color: var(--sidebar-text); opacity: .6; }

#resizer { width: 6px; cursor: col-resize; background: transparent; flex-shrink: 0; transition: background .15s; }
#resizer:hover, :global(body.resizing) #resizer { background: var(--primary-light); border-right: 1px solid var(--primary); }
:global(body.resizing) { cursor: col-resize; user-select: none; }

#main { flex: 1; display: flex; flex-direction: column; height: 100vh; min-width: 0; }
#topbar { display: flex; align-items: center; gap: 14px; padding: 0 24px; height: 58px; border-bottom: 1px solid var(--border); background: #fff; flex-shrink: 0; }
#topbar h1 { font-size: 16px; font-weight: 700; }
#topbar .sub { font-size: 12px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#topbar .spacer { flex: 1; }
.tb-btn { background: var(--primary-light); color: var(--primary); border: 1px solid var(--border); padding: 6px 12px; border-radius: 8px; cursor: pointer; font-size: 12px; font-weight: 600; transition: background .2s; }
.tb-btn:hover { background: #e2e8ff; }
.acct { position: relative; }
.acct-dd { position: absolute; right: 0; top: calc(100% + 8px); min-width: 180px; background: #fff; border: 1px solid var(--border); border-radius: 10px; box-shadow: 0 10px 30px rgba(0,0,0,.12); padding: 6px; z-index: 50; }
.acct-dd .name { font-size: 13px; font-weight: 600; padding: 8px 10px; }
.acct-dd .opt { display: block; width: 100%; text-align: left; border: none; background: none; cursor: pointer; padding: 8px 10px; font-size: 13px; color: var(--danger); border-radius: 7px; }
.acct-dd .opt:hover { background: #fdecec; }

#messages { flex: 1; overflow-y: auto; padding: 24px; scroll-behavior: smooth; }
#messages .inner { max-width: 880px; margin: 0 auto; display: flex; flex-direction: column; gap: 18px; }
.msg { display: flex; flex-direction: column; max-width: 86%; animation: fadeIn .25s ease; }
.msg.user { align-self: flex-end; align-items: flex-end; }
.msg.ai { align-self: flex-start; align-items: flex-start; }
.msg .head { display: flex; align-items: center; gap: 8px; margin-bottom: 5px; font-size: 11px; color: var(--muted); }
.msg.user .head { flex-direction: row-reverse; }
.avatar { width: 26px; height: 26px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 12px; color: #fff; flex-shrink: 0; }
.avatar.ai { background: linear-gradient(135deg,#4f6ef7,#7c5cf0); }
.avatar.user { background: #99a2ad; }
.bubble { padding: 10px 14px; border-radius: 14px; line-height: 1.7; font-size: 14px; word-break: break-word; }
.msg.ai .bubble { background: var(--bubble-ai); border: 1px solid var(--border); color: #2a2d37; border-bottom-left-radius: 4px; box-shadow: 0 1px 2px rgba(0,0,0,.04); }
.msg.user .bubble { background: var(--bubble-user); color: #fff; border-bottom-right-radius: 4px; box-shadow: 0 2px 8px rgba(79,110,247,.25); white-space: pre-wrap; }
.bubble .plain { white-space: pre-wrap; }
.bubble .md { line-height: 1.75; }
.bubble .md > :first-child { margin-top: 0; }
.bubble .md > :last-child { margin-bottom: 0; }
.bubble .md p { margin: 6px 0; }
.bubble .md h1, .bubble .md h2, .bubble .md h3 { margin: 10px 0 6px; font-weight: 700; color: #22252e; }
.bubble .md h1 { font-size: 16px; } .bubble .md h2 { font-size: 15px; } .bubble .md h3 { font-size: 14px; }
.bubble .md ul, .bubble .md ol { margin: 6px 0; padding-left: 20px; }
.bubble .md li { margin: 2px 0; }
.bubble .md code { background: #f2f3f7; padding: 1px 5px; border-radius: 4px; font-size: 12px; }
.bubble .md pre { background: #282c36; color: #e6e6e6; padding: 10px 12px; border-radius: 8px; overflow-x: auto; margin: 8px 0; }
.bubble .md pre code { background: transparent; padding: 0; color: inherit; font-size: 12px; }
.bubble .md blockquote { border-left: 3px solid var(--primary); margin: 6px 0; padding: 2px 12px; color: #667; background: var(--primary-light); border-radius: 0 6px 6px 0; }
.bubble .md a { color: var(--primary); }
.bubble .caret { display: inline-block; width: 2px; height: 15px; background: var(--primary); vertical-align: text-bottom; margin-left: 2px; animation: blink .8s infinite; }
@keyframes blink { 50% { opacity: 0; } }

.loading { display: flex; align-items: center; gap: 6px; color: var(--muted); font-size: 13px; }
.dot { width: 7px; height: 7px; background: var(--primary); border-radius: 50%; animation: bounce 1.4s infinite; }
.dot:nth-child(2) { animation-delay: .2s; } .dot:nth-child(3) { animation-delay: .4s; }
@keyframes bounce { 0%,60%,100% { transform: translateY(0); } 30% { transform: translateY(-6px); } }

#composer { flex-shrink: 0; padding: 14px 24px 18px; background: rgba(255,255,255,.7); border-top: 1px solid var(--border); }
#composer .box { max-width: 880px; margin: 0 auto; background: #fff; border: 1px solid var(--border); border-radius: 14px; padding: 8px 10px; box-shadow: 0 4px 16px rgba(0,0,0,.05); }
#composer .row { display: flex; align-items: flex-end; gap: 8px; }
#input, textarea { flex: 1; border: none; outline: none; padding: 8px 10px; font-size: 14px; resize: none; max-height: 140px; line-height: 1.6; background: transparent; font-family: inherit; }
#send { background: var(--primary); color: #fff; border: none; padding: 9px 20px; border-radius: 10px; cursor: pointer; font-size: 14px; font-weight: 600; transition: background .2s; flex-shrink: 0; }
#send:hover { background: var(--primary-dark); }
#send:disabled { background: #b9c0d6; cursor: not-allowed; }
#composer .hint { max-width: 880px; margin: 8px auto 0; font-size: 11px; color: var(--muted); }

@media (max-width: 720px) {
  #sidebar { display: none; }
  #resizer { display: none; }
}
</style>
