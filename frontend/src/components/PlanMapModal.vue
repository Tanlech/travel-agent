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
        <div class="map-tip">● 圆点为景点（数字为第几天），线段为该日串联路线，不同颜色代表不同天；点击标记可查看详情。</div>
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
    const items = (day.items || []).filter((it) => it.item_type === 'attraction' && it.lng != null && it.lat != null)
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
  if (stay) legend.value.push({ type: 'dot', color: '#e0245e', label: '住宿' })

  dayLayers.forEach(({ color, items, idx }) => {
    if (items.length >= 2) {
      L.polyline(items.map((it) => [it.lat, it.lng]), { color, weight: 3, opacity: 0.85 }).addTo(map)
        .bindPopup('<b>Day ' + idx + ' 当日路线</b>')
    }
    items.forEach((it) => {
      const icon = L.divIcon({
        className: '',
        html: '<div style="width:20px;height:20px;border-radius:50%;background:' + color +
          ';border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;color:#fff;font-size:10px;font-weight:700">' + idx + '</div>',
        iconSize: [20, 20], iconAnchor: [10, 10],
      })
      const time = (it.start_time || '') + (it.end_time ? '-' + it.end_time : '')
      L.marker([it.lat, it.lng], { icon }).addTo(map).bindPopup(
        '<b>' + esc(it.title || '') + '</b><br>' + esc(time) + (it.area ? ' · ' + esc(it.area) : '')
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
