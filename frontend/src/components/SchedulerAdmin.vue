<template>
  <div class="sc-wrap">
    <div class="sc-card">
      <div class="sc-card-title">⏰ 知识库重建定时任务</div>
      <div class="cfg-row">
        <label class="switch">
          <input v-model="configDraft.enabled" type="checkbox">
          <span class="track"></span>
        </label>
        <span>启用定时重建</span>
      </div>
      <div class="cfg-row">
        <span>执行间隔（分钟）</span>
        <input v-model.number="configDraft.interval_minutes" class="num-input" type="number" min="1" placeholder="1440">
        <button class="btn mini" :disabled="saving" @click="saveConfig">{{ saving ? '保存中…' : '保存' }}</button>
      </div>
      <div class="next-run">下次自动执行：{{ nextRunText }}</div>
      <div class="hint-line">
        <p>· 开启后，服务会每隔「执行间隔」自动重建一次 RAG 景点知识库。</p>
        <p>· 重建以 data/attraction/*.json 为准，逐城市清旧点后整体重导。</p>
        <p>· 定时与手动重建互斥，不会同时运行。</p>
      </div>
    </div>

    <div class="sc-card">
      <div class="sc-card-title">📌 最近一次定时执行</div>
      <div v-if="lastScheduled" class="last-run">
        <div class="kv"><span>结果</span><b>{{ stateText(lastScheduled.state) }}</b></div>
        <div class="kv"><span>开始时间</span><b>{{ fmtEpoch(lastScheduled.started_at) }}</b></div>
        <div class="kv"><span>耗时</span><b>{{ fmtDuration(lastScheduled.duration_ms) }}</b></div>
        <div class="kv"><span>导入</span><b>{{ lastScheduled.total }} 条</b></div>
        <div v-if="lastScheduled.error" class="err-box">⚠ {{ lastScheduled.error }}</div>
      </div>
      <div v-else class="empty">暂无定时执行记录</div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '@/api'
import { fmtDuration, toast } from '@/utils/format'

const saving = ref(false)
const config = ref({ enabled: false, interval_minutes: 1440 })
const configDraft = ref({ enabled: false, interval_minutes: 1440 })
const status = ref({})
const history = ref([])

const lastScheduled = computed(() => history.value.find((h) => h.trigger === 'schedule') || null)

const nextRunText = computed(() => {
  if (!config.value.enabled) return '已关闭'
  if (status.value.state === 'running' && status.value.trigger === 'schedule') return '正在执行中…'
  const last = status.value.finished_at || status.value.started_at || 0
  if (!last) return '尚未运行过，启用后即触发'
  const next = last * 1000 + (config.value.interval_minutes || 0) * 60 * 1000
  return fmtEpoch(next / 1000) + ' 左右'
})

function stateText(s) {
  return { running: '运行中', success: '成功', failed: '失败', idle: '空闲' }[s] || '空闲'
}

function fmtEpoch(sec) {
  if (!sec) return '—'
  const d = new Date(sec * 1000)
  if (isNaN(d)) return '—'
  const p = (n) => String(n).padStart(2, '0')
  return d.getFullYear() + '/' + p(d.getMonth() + 1) + '/' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds())
}

async function load() {
  try {
    const data = await api.kbStatus()
    config.value = data.config || {}
    configDraft.value = {
      enabled: !!data.config.enabled,
      interval_minutes: data.config.interval_minutes || 1440,
    }
    status.value = data.status || {}
    history.value = data.history || []
  } catch (e) {
    toast(e.message || '获取定时任务状态失败')
  }
}

async function saveConfig() {
  saving.value = true
  try {
    const data = await api.kbConfig({
      enabled: !!configDraft.value.enabled,
      interval_minutes: configDraft.value.interval_minutes || 1440,
    })
    config.value = data.config || {}
    configDraft.value = {
      enabled: !!data.config.enabled,
      interval_minutes: data.config.interval_minutes || 1440,
    }
    toast('定时任务配置已保存')
  } catch (e) {
    toast(e.message || '保存失败')
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.sc-wrap { display: flex; flex-direction: column; gap: 14px; max-width: 640px; }
.sc-card { background: #fff; border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px; }
.sc-card-title { font-size: 14px; font-weight: 700; margin-bottom: 12px; }

.cfg-row { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; font-size: 13px; }
.num-input { width: 110px; padding: 6px 10px; border: 1px solid var(--border); border-radius: 8px; font-size: 13px; outline: none; }
.num-input:focus { border-color: var(--primary); }
.next-run { font-size: 13px; color: var(--primary); font-weight: 600; margin-bottom: 10px; }
.hint-line { font-size: 12px; color: var(--muted); line-height: 1.7; }
.hint-line p { margin: 0; }

.switch { position: relative; display: inline-block; width: 38px; height: 22px; flex-shrink: 0; }
.switch input { display: none; }
.switch .track { position: absolute; inset: 0; background: #d5d9e4; border-radius: 20px; transition: background .2s; cursor: pointer; }
.switch .track::after {
  content: ''; position: absolute; top: 2px; left: 2px; width: 18px; height: 18px;
  background: #fff; border-radius: 50%; transition: transform .2s; box-shadow: 0 1px 3px rgba(0,0,0,.2);
}
.switch input:checked + .track { background: var(--primary); }
.switch input:checked + .track::after { transform: translateX(16px); }

.kv { font-size: 13px; color: var(--muted); display: flex; justify-content: space-between; gap: 12px; border-bottom: 1px dashed var(--border); padding: 6px 0; }
.kv b { color: var(--text); font-weight: 600; }
.err-box { background: #fdecec; color: var(--danger); border-radius: 8px; padding: 8px 12px; font-size: 12px; margin-top: 4px; }
.empty { font-size: 13px; color: var(--muted); padding: 12px 0; }
</style>
