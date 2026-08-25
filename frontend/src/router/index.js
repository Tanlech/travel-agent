import { createRouter, createWebHistory } from 'vue-router'
import { accountState } from '@/store/auth'
import ChatView from '@/views/ChatView.vue'
import LoginView from '@/views/LoginView.vue'
import AdminView from '@/views/AdminView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'chat', component: ChatView },
    { path: '/login', name: 'login', component: LoginView },
    { path: '/admin', name: 'admin', component: AdminView },
  ],
})

router.beforeEach((to) => {
  // 已登录访问 /login 直接回聊天页
  if (to.name === 'login' && accountState.account) return '/'
  return true
})

export default router
