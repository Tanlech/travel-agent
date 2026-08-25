<template>
  <div class="login-wrap">
    <div class="card">
      <div class="brand"><span class="logo">🌍</span>旅行规划助手</div>
      <div class="sub">登录后长期记忆绑定账号、可跨设备同步</div>
      <div class="tabs">
        <button class="tab" :class="{ on: mode === 'login' }" @click="switchMode('login')">登录</button>
        <button class="tab" :class="{ on: mode === 'register' }" @click="switchMode('register')">注册</button>
      </div>
      <div class="field">
        <label>用户名</label>
        <input v-model.trim="username" autocomplete="username" placeholder="至少 2 个字符" @keydown.enter="onEnter">
      </div>
      <div v-if="mode === 'register'" class="field">
        <label>昵称（可选）</label>
        <input v-model.trim="nick" placeholder="昵称">
      </div>
      <div class="field">
        <label>密码</label>
        <input v-model="password" type="password" autocomplete="current-password" placeholder="至少 6 位" @keydown.enter="onEnter">
      </div>
      <div class="err">{{ err }}</div>
      <button class="btn" :disabled="loading" @click="submit">{{ mode === 'register' ? '注册' : '登录' }}</button>
      <div class="tip"><router-link to="/">← 返回聊天</router-link></div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api'
import { setAccount, accountState } from '@/store/auth'

const router = useRouter()
const mode = ref('login')
const username = ref('')
const password = ref('')
const nick = ref('')
const err = ref('')
const loading = ref(false)

function switchMode(m) {
  mode.value = m
  err.value = ''
}
function onEnter() {
  submit()
}

async function submit() {
  if (!username.value || !password.value) { err.value = '请输入用户名和密码'; return }
  loading.value = true
  err.value = ''
  try {
    if (mode.value === 'register') {
      const rd = await api.register({ username: username.value, password: password.value, display_name: nick.value || null })
      if (rd.status !== 'ok') throw new Error('注册失败')
    }
    const data = await api.login({ username: username.value, password: password.value })
    setAccount({ token: data.token, user: data.user })
    router.replace('/?logged=1')
  } catch (e) {
    err.value = e.message || '操作失败'
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  if (accountState.account) router.replace('/')
})
</script>

<style scoped>
.login-wrap {
  min-height: 100vh; background: linear-gradient(135deg,#5d79f8 0%,#4668ec 45%,#3a56cf 100%);
  display: flex; align-items: center; justify-content: center; padding: 20px;
}
.card {
  width: min(400px, 92vw); background: #fff; border-radius: 16px; padding: 34px 32px 28px;
  box-shadow: 0 20px 60px rgba(20,30,90,.35);
}
.brand { display: flex; align-items: center; gap: 10px; font-size: 17px; font-weight: 700; margin-bottom: 4px; }
.brand .logo { width: 34px; height: 34px; border-radius: 9px; display: flex; align-items: center; justify-content: center; background: linear-gradient(135deg,#4f6ef7,#7c5cf0); font-size: 18px; flex-shrink: 0; }
.sub { font-size: 12px; color: var(--muted); margin-bottom: 22px; }
.tabs { display: flex; gap: 8px; margin-bottom: 8px; }
.tabs .tab { flex: 1; padding: 9px; border: 1px solid var(--border); background: #fff; border-radius: 9px; font-size: 13px; cursor: pointer; color: var(--muted); }
.tabs .tab.on { background: var(--primary); border-color: var(--primary); color: #fff; font-weight: 600; }
.field { margin-top: 13px; }
.field label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 5px; }
.field input { width: 100%; padding: 10px 11px; border: 1px solid var(--border); border-radius: 8px; font-size: 13px; outline: none; transition: border-color .15s; }
.field input:focus { border-color: var(--primary); }
.err { color: #c62828; font-size: 12px; min-height: 16px; margin-top: 10px; }
.btn { width: 100%; margin-top: 10px; padding: 11px; border: none; border-radius: 9px; cursor: pointer; background: var(--primary); color: #fff; font-size: 14px; font-weight: 600; transition: background .2s; }
.btn:hover { background: var(--primary-dark); }
.btn:disabled { opacity: .6; cursor: not-allowed; }
.tip { font-size: 12px; color: var(--muted); margin-top: 14px; line-height: 1.6; text-align: center; }
.tip a { color: var(--primary); text-decoration: none; }
</style>
