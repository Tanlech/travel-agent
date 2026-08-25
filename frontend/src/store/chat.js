import { reactive } from 'vue'
import { genId } from '@/utils/format'

const STORE_KEY = 'ta_conversations'

function loadStore() {
  try {
    return JSON.parse(localStorage.getItem(STORE_KEY) || '[]')
  } catch (e) {
    return []
  }
}

export const chatState = reactive({
  conversations: loadStore(),
  activeId: null,
})

export function saveStore() {
  localStorage.setItem(STORE_KEY, JSON.stringify(chatState.conversations))
}

export function currentConversation() {
  return chatState.conversations.find((c) => c.id === chatState.activeId)
}

export function newConversation() {
  const c = { id: genId(), sid: null, title: '新对话', updatedAt: Date.now(), messages: [] }
  chatState.conversations.push(c)
  chatState.activeId = c.id
  saveStore()
  return c
}

export function deleteConversation(id) {
  const target = chatState.conversations.find((c) => c.id === id)
  chatState.conversations = chatState.conversations.filter((c) => c.id !== id)
  if (chatState.activeId === id) {
    chatState.activeId = chatState.conversations.length ? chatState.conversations[0].id : null
  }
  saveStore()
  return target
}

export function sortedConversations() {
  return [...chatState.conversations].sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0))
}
