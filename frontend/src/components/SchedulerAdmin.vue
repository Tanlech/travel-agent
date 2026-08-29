<template>
  <div class="sc-wrap">
    <div class="sc-card">
      <div class="sc-card-title">⏰ 知识库重建定时任务</div>
      <div class="sc-sub">每个知识库独立定时、独立重建：到点只会重建它自己，互不影响。定时与手动重建互斥，不会同时运行。</div>

      <div class="run-head">
        <span class="state" :class="runStateClass">{{ runStateText }}</span>
        <div class="spacer"></div>
        <button class="btn ghost mini" :disabled="loading" @click="loadAll">刷新</button>
        <button class="btn mini" :disabled="isRunning || reindexing" @click="reindex()">
          {{ isRunning ? '重建中…' : '全部重建' }}
        </button>
      </div>
      <div v-if="status.total > 0 || runError" class="run-detail">
        <div class="kv"><span>本次导入</span><b>{{ status.total }} 条</b></div>
        <div class="kv"><span>总耗时</span><b>{{ fmtDuration(status.duration_ms) }}</b></div>
        <div v-if="runError" class="err-box">⚠ {{ runError }}</div>
      </div>

      <div v-if="bases.length" class="sc-list">
        <div v-for="b in bases" :key="b.collection" class="sc-row">
          <div class="base-main">
            <div class="base-title">{{ b.label }} <span class="base-count">{{ b.count }} 条</span></div>
            <div class="base-desc">{{ b.desc }}</div>
            <div v-if="b.last && b.last.finished_at" class="base-last">{{ baseLastText(b) }}</div>
          </div>
          <div class="base-timer">
            <label class="timer-switch">
              <input type="checkbox" :checked="!!b.enabled" @change="toggleSchedule(b, $event)" />
              <span>定时</span>
            </label>
            <input
              v-if="b.enabled"
              type="number" class="timer-input" min="1"
              :value="b.interval_minutes"
              @change="setIntervalMin(b, $event)"
            />
            <span v-if="b.enabled" class="hint">分钟</span>
          </div>
          <button
            class="btn mini" :disabled="isRunning || reindexing || isBaseRunning(b)"
            @click="reindex(b.collection)"
          >
            {{ isBaseRunning(b) ? '重建中…' : '重建' }}
          </button>
        </div>
      </div>
      <div v-else class="empty">暂无子知识库</div>
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

    <!-- 重建确认弹框（应用内弹框，不显示端口） -->
    <div v-if="confirmBox" class="modal-mask" @click.self="confirmBox = null">
      <div class="modal modal-sm">
        <div class="m-head"><h2>操作确认</h2><button class="m-close" @click="confirmBox = null">✕</button></div>
        <div class="m-body">{{ confirmBox.text }}</div>
        <div class="m-foot">
          <button class="btn ghost" @click="confirmBox = null">取消</button>
          <button class="btn danger" @click="confirmOk">确认重建</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '@/api'
import { fmtDuration, toast } from '@/utils/format'

const loading = ref(false)
const reindexing = ref(false)
const bases = ref([])
const status = ref({})
const history = ref([])

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

const lastScheduled = computed(() => history.value.find((h) => h.trigger === 'schedule') || null)

const isRunning = computed(() => status.value.state === 'running')
const runStateClass = computed(() => 's-' + (status.value.state || 'idle'))
const runStateText = computed(() => stateText(status.value.state))
const runError = computed(() => status.value.error || '')
const isBaseRunning = (b) => {
  const st = status.value.bases && status.value.bases[b.collection]
  return (st && st.state === 'running') || false
}

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

function baseLastText(b) {
  const l = b.last || {}
  if (!l.finished_at) return '从未重建'
  const s = ({ success: '成功', failed: '失败', running: '运行中' })[l.state] || l.state || '—'
  const err = l.error ? `（${l.error}）` : ''
  return `${s}${l.total != null ? ' ' + l.total + ' 条' : ''} · ${fmtEpoch(l.finished_at)}${err}`
}

async function loadBases() {
  const data = await api.kbBases()
  bases.value = data.bases || []
}

async function loadAll() {
  loading.value = true
  try {
    const data = await api.kbStatus()
    status.value = data.status || {}
    history.value = data.history || []
    await loadBases()
  } catch (e) {
    toast(e.message || '获取定时任务状态失败', 'error')
  } finally {
    loading.value = false
  }
}

async function toggleSchedule(b, ev) {
  b.enabled = !!ev.target.checked
  try {
    await api.kbConfig({ bases: { [b.collection]: { enabled: b.enabled } } })
    toast(b.enabled ? `已开启「${b.label}」定时重建` : `已关闭「${b.label}」定时重建`)
    await loadBases()
  } catch (e) {
    toast(e.message || '保存定时配置失败', 'error')
  }
}

async function setIntervalMin(b, ev) {
  const val = parseInt(ev.target.value || '', 10)
  if (!val || val < 1) {
    await loadBases()
    return
  }
  b.interval_minutes = val
  try {
    await api.kbConfig({ bases: { [b.collection]: { interval_minutes: val } } })
    toast(`「${b.label}」重建间隔已设为 ${val} 分钟`)
    await loadBases()
  } catch (e) {
    toast(e.message || '保存定时配置失败', 'error')
  }
}

async function reindex(target) {
  if (isRunning.value) return
  const label = target ? (bases.value.find((b) => b.collection === target)?.label || target) : '全部子知识库'
  askConfirm(`确定要立即重建「${label}」吗？\n每个子库只会重建自己，不影响其它库。`, async () => {
    reindexing.value = true
    try {
      const data = await api.kbReindex(target ?? null)
      toast(data.message || '已触发重建')
      setTimeout(loadAll, 800)
    } catch (e) {
      toast(e.message || '触发失败', 'error')
    } finally {
      reindexing.value = false
    }
  })
}

onMounted(loadAll)
</script>

<style scoped>
.sc-wrap { display: flex; flex-direction: column; gap: 14px; max-width: 780px; }
.sc-card { background: #fff; border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px; }
.sc-card-title { font-size: 14px; font-weight: 700; margin-bottom: 8px; }
.sc-sub { font-size: 12px; color: var(--muted); line-height: 1.7; margin-bottom: 10px; }

.run-head { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
.run-head .spacer { flex: 1; }
.state { font-size: 12px; padding: 3px 10px; border-radius: 20px; font-weight: 600; }
.s-running { background: #fff7e6; color: #d97706; }
.s-success { background: #e8f7ef; color: var(--success); }
.s-failed { background: #fdecec; color: var(--danger); }
.s-idle { background: #f5f6fa; color: var(--muted); }
.run-detail { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 8px 20px; margin-bottom: 10px; }
.run-detail .kv { display: flex; justify-content: space-between; font-size: 13px; color: var(--muted); border-bottom: 1px dashed var(--border); padding: 5px 0; }
.run-detail .kv b { color: var(--text); font-weight: 600; }
.err-box { background: #fdecec; color: var(--danger); border-radius: 8px; padding: 8px 12px; font-size: 12px; margin-top: 4px; }
.empty { font-size: 13px; color: var(--muted); padding: 12px 0; }

.sc-list { display: flex; flex-direction: column; gap: 8px; margin-top: 6px; }
.sc-row { display: flex; align-items: center; gap: 12px; padding: 10px 12px; border: 1px solid var(--border); border-radius: 10px; background: #fafbff; flex-wrap: wrap; }
.base-main { flex: 1; min-width: 180px; }
.base-title { font-size: 13px; font-weight: 700; display: flex; align-items: center; gap: 8px; }
.base-count { font-size: 11px; color: var(--primary); background: var(--primary-light); padding: 1px 6px; border-radius: 10px; font-weight: 600; }
.base-desc { font-size: 12px; color: var(--muted); margin-top: 2px; }
.base-last { font-size: 12px; color: var(--muted); margin-top: 2px; }
.base-timer { display: flex; align-items: center; gap: 8px; }
.timer-switch { display: inline-flex; align-items: center; gap: 4px; cursor: pointer; font-size: 12px; color: var(--text); }
.timer-switch input { accent-color: var(--primary); }
.timer-input { width: 72px; font-size: 13px; padding: 5px 8px; border: 1px solid var(--border); border-radius: 7px; outline: none; }
.timer-input:focus { border-color: var(--primary); }
.hint { font-size: 12px; color: var(--muted); }

.last-run { display: flex; flex-direction: column; gap: 2px; }
.kv { font-size: 13px; color: var(--muted); display: flex; justify-content: space-between; gap: 12px; border-bottom: 1px dashed var(--border); padding: 6px 0; }
.kv b { color: var(--text); font-weight: 600; }
</style>
