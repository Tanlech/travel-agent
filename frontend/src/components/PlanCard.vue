<template>
  <div class="plan-card">
    <h3>🗺️ {{ plan.destination || '行程' }} 方案</h3>
    <div v-if="plan.summary" class="plan-summary">{{ plan.summary }}</div>

    <template v-if="days.length">
      <div class="day-tabs">
        <span
          v-for="(day, i) in days"
          :key="i"
          class="day-tab"
          :class="{ active: activeDay === i }"
          @click="activeDay = i"
        >Day {{ dayIndex(day, i) }}</span>
      </div>

      <div v-for="(day, i) in days" :key="i" class="day-content" :class="{ active: activeDay === i }">
        <div class="day-item" style="margin-bottom:0">
          <div>
            <span class="day-label">Day {{ dayIndex(day, i) }}</span>
            <span v-if="dayArea(day)" style="color:#555"> · {{ dayArea(day) }}</span>
          </div>
          <div v-if="dayWeather(i)" class="day-weather">🌤️ {{ dayWeather(i) }}</div>

          <template v-if="dayBlocks(day).length">
            <template v-for="type in PLAN_SEC_ORDER" :key="type">
              <div v-if="grouped[type] && grouped[type].length">
                <div class="plan-sec">{{ secLabel(type) }}<span class="sec-line"></span></div>
                <div class="blocks">
                  <div v-for="(b, bi) in grouped[type]" :key="bi" class="block">
                    <span class="b-time">{{ blockTime(b) }}</span>
                    <span>{{ secIcon(b.item_type) }}</span>
                    <span class="b-title">{{ b.title || '' }}</span>
                    <a
                      v-if="b.item_type === 'attraction'"
                      class="day-map-link"
                      target="_blank"
                      rel="noopener"
                      :href="'https://uri.amap.com/search?keyword=' + encodeURIComponent(b.title || '')"
                    >地图</a>
                    <div v-if="b.detail" class="b-detail">{{ b.detail }}</div>
                  </div>
                </div>
              </div>
            </template>
          </template>

          <div v-if="(day.notes || []).length" style="margin-top:8px;font-size:11px;color:#9094a2;line-height:1.6">
            <div v-for="(n, ni) in day.notes" :key="ni">· {{ n }}</div>
          </div>
        </div>
      </div>
    </template>

    <div class="meta-row">
      <span v-if="stayText" class="meta-chip">🏨 {{ stayText }}</span>
      <span v-if="plan.transport_plan && plan.transport_plan.length" class="meta-chip">🚗 交通已规划</span>
      <span v-if="plan.weather_notes && plan.weather_notes.length" class="meta-chip">🌤️ {{ weatherChip }}</span>
    </div>

    <div class="plan-actions">
      <button class="plan-act-btn" @click="$emit('show-map', plan)">🗺️ 行程地图</button>
      <button class="plan-act-btn" @click="showRaw = !showRaw">{{ showRaw ? '📄 收起原始数据' : '📄 查看原始数据' }}</button>
    </div>
    <pre v-show="showRaw" class="plan-raw">{{ planJson }}</pre>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'

const props = defineProps({ plan: { type: Object, required: true } })
defineEmits(['show-map'])

const PLAN_SEC_META = {
  transport: { label: '🚗 交通' },
  attraction: { label: '🏛️ 景点' },
  meal: { label: '🍽️ 餐饮' },
  flex: { label: '✨ 弹性活动' },
  return: { label: '🏠 返程' },
}
const PLAN_SEC_ORDER = ['transport', 'attraction', 'meal', 'flex', 'return']

const activeDay = ref(0)
const showRaw = ref(false)
const planJson = computed(() => JSON.stringify(props.plan, null, 2))

const days = computed(() => props.plan.daily_plan || props.plan.day_plans || [])

function dayIndex(day, i) {
  return day.day_index ?? day.day ?? day.day_number ?? (i + 1)
}
function dayArea(day) {
  return day.primary_area || day.area || day.theme || ''
}
function dayBlocks(day) {
  return day.time_blocks || day.blocks || day.items || []
}
function dayWeather(i) {
  const wn = Array.isArray(props.plan.weather_notes) ? props.plan.weather_notes[i] : ''
  return typeof wn === 'string' ? wn : ''
}
function blockTime(b) {
  return (b.start_time || '') + (b.end_time ? '-' + b.end_time : '')
}
function secLabel(type) {
  return (PLAN_SEC_META[type] && PLAN_SEC_META[type].label) || type || '其他'
}
function secIcon(type) {
  return { attraction: '🏛️', transport: '🚗', meal: '🍽️', return: '🏠', flex: '✨' }[type] || '•'
}
const stayText = computed(() => {
  const stay = props.plan.stay_recommendation && props.plan.stay_recommendation[0]
  if (!stay) return ''
  const st = typeof stay === 'string' ? stay : (stay.name || stay.hotel_name || '住宿已推荐')
  return String(st).slice(0, 30)
})
const weatherChip = computed(() => {
  const w = props.plan.weather_notes && props.plan.weather_notes[0]
  return typeof w === 'string' ? w.slice(0, 30) : '天气已评估'
})
</script>

<style scoped>
.plan-card {
  background: #fff; border: 1px solid var(--border); border-radius: 14px; padding: 16px; margin-top: 2px;
  box-shadow: 0 4px 16px rgba(0,0,0,.05); border-left: 4px solid var(--primary);
}
.plan-card h3 { font-size: 15px; color: #333; margin-bottom: 10px; display: flex; align-items: center; gap: 6px; }
.plan-summary { font-size: 13px; color: #555; line-height: 1.8; margin-bottom: 12px; background: var(--primary-light); border-radius: 8px; padding: 10px 12px; }
.day-item { background: #f7f8fb; border-radius: 8px; padding: 10px 12px; margin-bottom: 8px; font-size: 13px; }
.day-item .day-label { font-weight: 700; color: var(--primary); margin-right: 6px; }
.blocks { margin-top: 6px; }
.block { font-size: 12px; color: #444; padding: 3px 0; line-height: 1.5; display: flex; align-items: baseline; gap: 4px; }
.b-time { color: var(--primary); font-weight: 600; font-size: 11px; flex-shrink: 0; }
.b-title { color: #333; }
.b-detail { color: #9094a2; font-size: 11px; padding-left: 26px; margin-top: 1px; }
.meta-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
.meta-chip { font-size: 11px; background: #eef; color: #445; padding: 4px 10px; border-radius: 8px; }
.day-tabs { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 12px; }
.day-tab { padding: 5px 12px; border-radius: 16px; font-size: 12px; font-weight: 600; cursor: pointer; border: 1px solid var(--border); background: #f7f8fb; color: #666; }
.day-tab.active { background: var(--primary); color: #fff; border-color: var(--primary); }
.day-content { display: none; }
.day-content.active { display: block; animation: fadeIn .2s ease; }
.day-map-link { margin-left: 6px; text-decoration: none; font-size: 11px; color: var(--primary); background: var(--primary-light); padding: 1px 7px; border-radius: 4px; }
.plan-sec { font-size: 11px; font-weight: 700; color: var(--muted); margin: 10px 0 4px; letter-spacing: .5px; display: flex; align-items: center; gap: 4px; }
.plan-sec .sec-line { flex: 1; height: 1px; background: var(--border); }
.plan-actions { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
.plan-act-btn { border: 1px solid var(--border); background: #f7f8fb; color: var(--primary); padding: 6px 12px; border-radius: 7px; font-size: 12px; cursor: pointer; font-weight: 600; }
.plan-act-btn:hover { background: var(--primary-light); border-color: var(--primary); }
.day-weather { font-size: 12px; color: #555; background: var(--primary-light); border-radius: 7px; padding: 6px 10px; margin-bottom: 8px; }
.plan-raw { margin-top: 8px; padding: 10px 12px; background: #0f1526; color: #d7e0f7; border-radius: 8px; font-size: 11px; line-height: 1.5; overflow: auto; max-height: 52vh; font-family: "SFMono-Regular", Consolas, "Roboto Mono", monospace; white-space: pre-wrap; word-break: break-all; }
</style>
