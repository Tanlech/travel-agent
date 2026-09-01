<template>
  <div class="modal-mask" @click.self="$emit('close')">
    <div class="modal" style="width:min(880px,94vw)">
      <div class="m-head">
        <h2>🗺️ {{ plan.destination || '行程' }} 地图</h2>
        <button class="m-close" @click="$emit('close')">✕</button>
      </div>
      <div class="m-body" style="padding:0">
        <div id="mapLegend">
          <span v-for="lg in legend" :key="lg.label + lg.color" class="lg-item">
            <span v-if="lg.type === 'line'" class="lg-line" :style="{ background: lg.color }"></span>
            <span v-else class="lg-dot" :style="{ background: lg.color }"></span>
            {{ lg.label }}
          </span>
        </div>
        <div class="map-tip">● 圆点=景点（数字为天数）、紫方=餐饮、蓝点=交通、红=住宿；加粗实线=真实乘车/步行路线（按高德规划），细灰虚线=无精确路线时的兜底连线；点击路线或标记查看转乘详情。</div>
        <div ref="mapEl" id="mapWrap"></div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, nextTick } from 'vue'
import L from 'leaflet'
import { esc } from '@/utils/format'

const props = defineProps({ plan: { type: Object, required: true } })
defineEmits(['close'])

const DAY_COLORS = ['#4f6ef7', '#f59e0b', '#10b981', '#ec4899', '#8b5cf6', '#14b8a6', '#f43f5e', '#6366f1']
const mapEl = ref(null)
const legend = ref([])

onMounted(async () => {
  await nextTick()
  const days = props.plan.daily_plan || []
  const stay = (props.plan.stay_recommendation && props.plan.stay_recommendation[0]) || null
  const dayLayers = []
  const allPoints = []
  days.forEach((day, i) => {
    const color = DAY_COLORS[i % DAY_COLORS.length]
    const items = (day.items || []).filter((it) => ['attraction', 'meal', 'transport'].includes(it.item_type) && it.lng != null && it.lat != null)
    items.forEach((it) => allPoints.push([it.lat, it.lng]))
    dayLayers.push({ color, items, idx: day.day_index ?? day.day ?? (i + 1) })
  })
  if (stay && stay.lng != null && stay.lat != null) allPoints.push([stay.lat, stay.lng])

  const map = L.map(mapEl.value, { zoomControl: true }).setView(allPoints.length ? allPoints[0] : [30.5, 114.3], 12)
  const amap = L.tileLayer('https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}', {
    subdomains: '1234', maxZoom: 18, attribution: '© 高德地图',
  }).addTo(map)
  amap.on('tileerror', () => {
    if (!map._osmFallback) {
      map._osmFallback = true
      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 18 }).addTo(map)
    }
  })

  legend.value = dayLayers.map(({ color, idx }) => ({ type: 'line', color, label: 'Day ' + idx + ' 路线' }))
  legend.value.push({ type: 'dot', color: '#7c3aed', label: '餐饮' })
  legend.value.push({ type: 'dot', color: '#2563eb', label: '交通' })
  if (stay) legend.value.push({ type: 'dot', color: '#e0245e', label: '住宿' })

  dayLayers.forEach(({ color, items, idx }) => {
    // ① 真实路线：转场/进店块携带高德返回的轨迹点(path)，直接按其走向绘制加粗实线
    items.forEach((it) => {
      const path = it.path && Array.isArray(it.path) && it.path.length >= 2 ? it.path : null
      if (!path) return
      const routeInfo = it.detail || ''
      L.polyline(path, { color, weight: 4, opacity: 0.9 }).addTo(map)
        .bindPopup('<b>' + esc(it.title || '') + ' 交通路线</b><br>' + esc(String(routeInfo).slice(0, 120)))
    })
    // ② 目的地顺序兜底：景点/餐饮之间若没有真实轨迹，才用细灰虚线简单串联，避免“每点连线”主导
    const dests = items.filter((it) => it.item_type === 'meal' || it.item_type === 'attraction')
    for (let n = 0; n < dests.length - 1; n++) {
      const a = dests[n]
      const b = dests[n + 1]
      if (a.lng == null || a.lat == null || b.lng == null || b.lat == null) continue
      if (Math.abs(a.lat - b.lat) > 1e-6 || Math.abs(a.lng - b.lng) > 1e-6) {
        L.polyline([[a.lat, a.lng], [b.lat, b.lng]], { color: '#cfd6e4', weight: 1.5, opacity: 0.9, dashArray: '5 6' }).addTo(map)
      }
    }
    const keyOf = (it) => Math.round(it.lat * 1000) + ',' + Math.round(it.lng * 1000)
    // 先汇总全天“目的地”坐标（景点/餐饮，无论前后顺序）。交通目的地若落在其上，则不单独打点（仍参与连线）
    const destinationKeys = new Set()
    items.forEach((it) => {
      if (it.item_type !== 'transport') destinationKeys.add(keyOf(it))
    })
    const usedCoords = new Set() // 通用去重（对称方向），避免交通与目的地叠点
    items.forEach((it) => {
      const key = keyOf(it)
      const isMeal = it.item_type === 'meal'
      const isTransport = it.item_type === 'transport'
      let html
      if (isMeal) {
        html = '<div style="width:20px;height:20px;border-radius:8px;background:#7c3aed;border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;color:#fff;font-size:11px;font-weight:700">食</div>'
      } else if (isTransport) {
        html = '<div style="width:20px;height:20px;border-radius:50%;background:#2563eb;border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;color:#fff;font-size:11px;font-weight:700">交</div>'
      } else {
        html = '<div style="width:20px;height:20px;border-radius:50%;background:' + color +
          ';border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;color:#fff;font-size:10px;font-weight:700">' + idx + '</div>'
      }
      // 交通目的地与全天任一目标点（景点/餐饮）重合，或与已打点重合（含方向对称的去重）时，不再重复打点
      if (isTransport && destinationKeys.has(key)) return
      if (usedCoords.has(key)) return
      usedCoords.add(key)
      const icon = L.divIcon({ className: '', html, iconSize: [20, 20], iconAnchor: [10, 10] })
      const time = (it.start_time || '') + (it.end_time ? '-' + it.end_time : '')
      L.marker([it.lat, it.lng], { icon }).addTo(map).bindPopup(
        '<b>' + esc(it.title || '') + '</b><br>' + esc(time) + (it.area ? ' · ' + esc(it.area) : '') + (it.address ? '<br>' + esc(it.address) : '')
      )
    })
  })

  if (stay && stay.lng != null && stay.lat != null) {
    const icon = L.divIcon({
      className: '',
      html: '<div style="width:22px;height:22px;border-radius:6px;background:#e0245e;border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;font-size:12px">🏨</div>',
      iconSize: [22, 22], iconAnchor: [11, 11],
    })
    L.marker([stay.lat, stay.lng], { icon }).addTo(map).bindPopup('<b>🏨 ' + esc(stay.name || '住宿') + '</b><br>' + esc(stay.area || ''))
  }

  if (allPoints.length) map.fitBounds(L.latLngBounds(allPoints).pad(0.15))
  setTimeout(() => map.invalidateSize(), 50)
})
</script>

<style scoped>
#mapWrap { width: 100%; height: 60vh; min-height: 380px; background: #e8ecf3; }
#mapLegend { display: flex; gap: 14px; flex-wrap: wrap; padding: 10px 14px; font-size: 12px; color: #333; background: #fff; border-bottom: 1px solid var(--border); }
.lg-item { display: flex; align-items: center; gap: 5px; }
.lg-dot { width: 11px; height: 11px; border-radius: 50%; display: inline-block; }
.lg-line { width: 18px; height: 3px; border-radius: 2px; display: inline-block; }
.map-tip { font-size: 11px; color: var(--muted); padding: 6px 14px 4px; }
</style>
