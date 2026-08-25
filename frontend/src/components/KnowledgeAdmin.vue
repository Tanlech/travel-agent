<template>
  <div class="kb-wrap">
    <!-- 知识库类型 -->
    <div class="kb-card">
      <div class="kb-card-title">🗃️ 知识库类型</div>
      <div class="base-tabs">
        <div
          v-for="b in bases"
          :key="b.collection"
          class="base-tab"
          :class="{ on: activeBase === b.collection }"
          @click="selectBase(b.collection)"
        >
          <div class="bt-label">{{ b.label }}</div>
          <div class="bt-count">{{ b.count }} 条</div>
        </div>
      </div>
    </div>

    <!-- 景点库：地点 / 景点管理 -->
    <template v-if="activeBase === 'attraction'">
      <div class="kb-grid-2">
        <div class="kb-card">
          <div class="kb-card-title">📍 地点列表（{{ cities.length }}）</div>
          <div v-if="cities.length" class="city-list">
            <div
              v-for="c in cities"
              :key="c.city"
              class="city-item"
              :class="{ on: activeCity === c.city }"
              @click="selectCity(c.city)"
            >
              <div class="ci-name">
                {{ c.city }}
                <span v-if="c.province" class="ci-prov">{{ c.province }}</span>
              </div>
              <span class="ci-count">{{ c.count }}</span>
              <button class="icon-btn danger" title="删除整个地点" @click.stop="deleteCity(c.city)">✕</button>
            </div>
          </div>
          <div v-else class="empty">暂无地点，可在下方新增景点时输入新地点自动创建</div>
        </div>

        <div class="kb-card">
          <div class="kb-card-title">🏞️ {{ activeCity ? activeCity + ' 的景点' : '景点列表' }}</div>
          <table v-if="spots.length" class="kb-table">
            <thead>
              <tr><th>景点</th><th>区域</th><th>时长</th><th>标签</th><th>操作</th></tr>
            </thead>
            <tbody>
              <tr v-for="s in spots" :key="s.name">
                <td>{{ s.name }}</td>
                <td>{{ s.area || '-' }}</td>
                <td>{{ s.duration ? s.duration + 'h' : '-' }}</td>
                <td>
                  <span v-for="t in s.tags" :key="t" class="tag-mini">{{ t }}</span>
                </td>
                <td>
                  <button class="btn danger mini" @click="deleteSpot(s.name)">删除</button>
                </td>
              </tr>
            </tbody>
          </table>
          <div v-else class="empty">{{ activeCity ? '该地点暂无景点' : '请先在左侧选择地点' }}</div>
        </div>
      </div>

      <!-- 新增景点 -->
      <div class="kb-card">
        <div class="kb-card-title">➕ 新增景点</div>
        <div class="form-grid">
          <label>地点 *
            <input v-model.trim="form.city" placeholder="城市，如：武汉（不存在则自动创建）">
          </label>
          <label>景点名 *
            <input v-model.trim="form.name" placeholder="如：东湖绿道">
          </label>
          <label>区域
            <input v-model.trim="form.area" placeholder="如：武昌区">
          </label>
          <label>省份
            <input v-model.trim="form.province" placeholder="如：湖北">
          </label>
          <label>建议时长（h）
            <input v-model.number="form.duration" type="number" min="0.5" step="0.5" placeholder="2">
          </label>
          <label>标签（逗号分隔）
            <input v-model.trim="form.tags" placeholder="自然,公园,拍照">
          </label>
          <label class="full">简介 / 推荐理由
            <textarea v-model.trim="form.reason" rows="2" placeholder="该景点的简介或推荐理由"></textarea>
          </label>
        </div>
        <button class="btn" :disabled="creating" @click="createSpot">{{ creating ? '创建中…' : '创建景点' }}</button>
      </div>
    </template>

    <div v-else class="kb-card">
      <div class="kb-card-title">ℹ️ {{ activeBaseLabel }}</div>
      <div class="empty">该知识库暂不支持在线管理，请通过数据导入方式维护。</div>
    </div>

    <!-- 重建与运行记录 -->
    <div class="kb-card">
      <div class="kb-card-title">🔄 知识库重建</div>
      <div class="run-head">
        <span class="state" :class="runStateClass">{{ runStateText }}</span>
        <span class="run-meta">最近触发：{{ triggerText }}</span>
        <div class="spacer"></div>
        <button class="btn ghost mini" :disabled="loading" @click="loadAll">刷新</button>
        <button class="btn mini" :disabled="isRunning || reindexing" @click="reindex">
          {{ isRunning ? '重建中…' : '立即重建' }}
        </button>
      </div>
      <div v-if="status.total > 0 || runError" class="run-detail">
        <div class="kv"><span>开始时间</span><b>{{ fmtEpoch(status.started_at) }}</b></div>
        <div class="kv"><span>结束时间</span><b>{{ fmtEpoch(status.finished_at) }}</b></div>
        <div class="kv"><span>总耗时</span><b>{{ fmtDuration(status.duration_ms) }}</b></div>
        <div class="kv"><span>导入数量</span><b>{{ status.total }} 条</b></div>
        <div v-if="runError" class="err-box">⚠ {{ runError }}</div>
      </div>
      <div v-if="history.length" class="his-row">
        <span v-for="(h, i) in history" :key="i" class="his-chip">
          {{ h.trigger === 'schedule' ? '定时' : '手动' }}
          <b :class="'c-' + h.state">{{ stateText(h.state) }}</b>
          {{ fmtEpoch(h.started_at) }} · {{ fmtDuration(h.duration_ms) }} · {{ h.total }}条
        </span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { api } from '@/api'
import { fmtDuration, toast } from '@/utils/format'

const loading = ref(false)
const reindexing = ref(false)
const creating = ref(false)

const bases = ref([])
const activeBase = ref('attraction')
const activeBaseLabel = computed(() => {
  const b = bases.value.find((x) => x.collection === activeBase.value)
  return b ? b.label : activeBase.value
})

const cities = ref([])
const activeCity = ref('')
const spots = ref([])

const status = ref({})
const history = ref([])

const form = ref({ city: '', name: '', area: '', province: '', duration: 2, tags: '', reason: '' })

let timer = null
let lastLoad = 0

const isRunning = computed(() => status.value.state === 'running')
const runStateClass = computed(() => 's-' + (status.value.state || 'idle'))
const runStateText = computed(() => stateText(status.value.state))
const triggerText = computed(() =>
  status.value.trigger === 'schedule' ? '定时任务' : status.value.trigger === 'manual' ? '手动触发' : '—'
)
const runError = computed(() => status.value.error || '')

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

async function loadBases() {
  try {
    const data = await api.kbBases()
    bases.value = data.bases || []
  } catch (e) {
    toast(e.message || '获取知识库列表失败')
  }
}

async function loadCities() {
  try {
    const data = await api.kbCities()
    cities.value = data.cities || []
    if (activeCity.value && !cities.value.some((c) => c.city === activeCity.value)) {
      activeCity.value = ''
      spots.value = []
    }
    if (!activeCity.value && cities.value.length) selectCity(cities.value[0].city)
  } catch (e) {
    toast(e.message || '获取地点列表失败')
  }
}

async function loadSpots() {
  if (!activeCity.value) {
    spots.value = []
    return
  }
  try {
    const data = await api.kbSpots(activeCity.value)
    spots.value = data.spots || []
  } catch (e) {
    toast(e.message || '获取景点列表失败')
  }
}

async function loadStatus() {
  try {
    const data = await api.kbStatus()
    status.value = data.status || {}
    history.value = data.history || []
  } catch (e) {
    /* 状态非关键，静默 */
  }
}

async function loadAll() {
  loading.value = true
  try {
    await Promise.all([loadBases(), loadCities(), loadStatus()])
  } finally {
    loading.value = false
    lastLoad = Date.now()
  }
}

function selectBase(collection) {
  activeBase.value = collection
  if (collection === 'attraction') loadCities()
}

function selectCity(city) {
  activeCity.value = city
  loadSpots()
}

async function createSpot() {
  const city = form.value.city.trim()
  const name = form.value.name.trim()
  if (!city || !name) {
    toast('请填写地点和景点名')
    return
  }
  creating.value = true
  try {
    const payload = {
      city,
      name,
      area: form.value.area,
      province: form.value.province,
      duration: form.value.duration || 2,
      reason: form.value.reason,
      tags: form.value.tags.split(/[,，]/).map((t) => t.trim()).filter(Boolean),
    }
    const data = await api.kbCreateSpot(payload)
    toast(data.message || '创建完成')
    if (data.status === 'ok') {
      form.value = { city, name: '', area: '', province: '', duration: 2, tags: '', reason: '' }
      activeCity.value = city
      await Promise.all([loadCities(), loadSpots()])
    }
  } catch (e) {
    toast(e.message || '创建失败')
  } finally {
    creating.value = false
  }
}

async function deleteSpot(name) {
  if (!activeCity.value) return
  if (!window.confirm(`确定删除景点「${name}」（${activeCity.value}）吗？`)) return
  try {
    const data = await api.kbDeleteSpot(activeCity.value, name)
    toast(data.message || '删除完成')
    await Promise.all([loadSpots(), loadCities()])
  } catch (e) {
    toast(e.message || '删除失败')
  }
}

async function deleteCity(city) {
  if (!window.confirm(`确定删除整个地点「${city}」及其全部 ${cities.value.find((c) => c.city === city)?.count || ''} 个景点吗？此操作不可恢复！`)) return
  try {
    const data = await api.kbDeleteCity(city)
    toast(data.message || '删除完成')
    if (activeCity.value === city) {
      activeCity.value = ''
      spots.value = []
    }
    await loadCities()
  } catch (e) {
    toast(e.message || '删除失败')
  }
}

async function reindex() {
  if (isRunning.value) return
  if (!window.confirm('确定要立即重建整个景点知识库吗？\n将按 data/attraction 下的文档逐城市重导，耗时约 1-2 分钟。')) return
  reindexing.value = true
  try {
    const data = await api.kbReindex()
    toast(data.message || '已触发重建')
    setTimeout(loadAll, 800)
  } catch (e) {
    toast(e.message || '触发失败')
  } finally {
    reindexing.value = false
  }
}

onMounted(() => {
  loadAll()
  timer = setInterval(() => {
    if (isRunning.value) {
      loadAll()
    } else if (!document.hidden && Date.now() - lastLoad > 30000) {
      loadAll()
    }
  }, 5000)
})
onBeforeUnmount(() => clearInterval(timer))
</script>

<style scoped>
.kb-wrap { display: flex; flex-direction: column; gap: 14px; }
.kb-card { background: #fff; border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px; }
.kb-card-title { font-size: 14px; font-weight: 700; margin-bottom: 12px; }
.kb-grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; }

/* 知识库类型 */
.base-tabs { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; }
.base-tab {
  border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; cursor: pointer;
  transition: all .15s; background: #fafbff;
}
.base-tab:hover { border-color: var(--primary); }
.base-tab.on { border-color: var(--primary); background: var(--primary-light); box-shadow: 0 0 0 1px var(--primary); }
.bt-label { font-size: 14px; font-weight: 700; }
.bt-count { font-size: 12px; color: var(--muted); margin-top: 4px; }

/* 地点列表 */
.city-list { max-height: 420px; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; }
.city-item {
  display: flex; align-items: center; gap: 10px; padding: 9px 12px; border: 1px solid var(--border);
  border-radius: 9px; cursor: pointer; transition: all .15s; font-size: 13px;
}
.city-item:hover { border-color: var(--primary); }
.city-item.on { border-color: var(--primary); background: var(--primary-light); }
.ci-name { font-weight: 600; flex: 1; }
.ci-prov { font-size: 11px; color: var(--muted); font-weight: 400; margin-left: 6px; }
.ci-count { font-size: 12px; color: var(--primary); font-weight: 700; background: var(--primary-light); padding: 2px 8px; border-radius: 10px; }
.icon-btn { border: none; background: transparent; cursor: pointer; font-size: 12px; color: var(--muted); border-radius: 6px; padding: 3px 6px; }
.icon-btn:hover { background: #fdecec; color: var(--danger); }

/* 景点表 */
.kb-table { width: 100%; border-collapse: collapse; }
.kb-table th, .kb-table td { text-align: left; padding: 9px 10px; font-size: 13px; border-bottom: 1px solid var(--border); }
.kb-table th { color: var(--muted); font-weight: 600; background: #fafbff; white-space: nowrap; }
.tag-mini { display: inline-block; font-size: 11px; background: var(--primary-light); color: var(--primary); padding: 1px 7px; border-radius: 8px; margin-right: 4px; }

/* 表单 */
.form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-bottom: 14px; }
.form-grid label { font-size: 12px; color: var(--muted); display: flex; flex-direction: column; gap: 5px; }
.form-grid label.full { grid-column: 1 / -1; }
.form-grid input, .form-grid textarea {
  padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; font-size: 13px; outline: none;
  font-family: inherit; resize: vertical;
}
.form-grid input:focus, .form-grid textarea:focus { border-color: var(--primary); }

/* 重建 */
.run-head { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
.run-head .spacer { flex: 1; }
.run-meta { font-size: 12px; color: var(--muted); }
.state { font-size: 12px; padding: 3px 10px; border-radius: 14px; font-weight: 600; }
.s-running { background: #fff7e6; color: #d97706; }
.s-success { background: #e8f7ef; color: var(--success); }
.s-failed { background: #fdecec; color: var(--danger); }
.s-idle { background: #f5f6fa; color: var(--muted); }
.run-detail { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 8px 20px; }
.kv { font-size: 13px; color: var(--muted); display: flex; justify-content: space-between; gap: 12px; border-bottom: 1px dashed var(--border); padding: 6px 0; }
.kv b { color: var(--text); font-weight: 600; }
.err-box { grid-column: 1 / -1; background: #fdecec; color: var(--danger); border-radius: 8px; padding: 8px 12px; font-size: 12px; margin-top: 4px; }
.his-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.his-chip { font-size: 11px; color: var(--muted); background: #f5f6fa; padding: 3px 10px; border-radius: 12px; }
.his-chip .c-success { color: var(--success); }
.his-chip .c-failed { color: var(--danger); }
.his-chip .c-running { color: #d97706; }
.empty { font-size: 13px; color: var(--muted); padding: 14px 0; text-align: center; }
</style>
