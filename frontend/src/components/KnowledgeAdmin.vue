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

    <!-- 景点库：地点列表在上、景点列表在下 -->
    <template v-if="activeBase === 'attraction'">
      <div class="kb-stack">
        <div class="kb-card">
          <div class="kb-card-title kb-title-row">
            📍 地点列表
            <span class="spot-total">{{ filteredCities.length + ' 个' }}</span>
            <span class="flex-spacer"></span>
            <button class="btn danger mini" @click="openDeleteCities">🗑 删除地点</button>
          </div>
          <div class="prov-filter">
            <select v-model="provinceFilter" @change="onProvinceFilter">
              <option v-for="p in provinceList" :key="p" :value="p">{{ p }}</option>
            </select>
          </div>
          <div v-if="filteredCities.length" class="city-list">
            <div
              v-for="c in filteredCities"
              :key="c.city"
              class="city-item"
              :class="{ on: activeCity === c.city }"
              @click="selectCity(c.city)"
            >
              <div class="ci-name">{{ c.city }}</div>
              <span class="ci-count">{{ c.count }}</span>
            </div>
          </div>
          <div v-else class="empty">暂无地点，可在下方新增景点时输入新地点自动创建</div>
        </div>

        <div class="kb-card">
          <div class="kb-card-title">
            🏞️ {{ activeCity ? activeCity + ' 的景点' : '景点列表' }}
            <span v-if="spots.length" class="spot-total">{{ spots.length }} 条</span>
          </div>
          <table v-if="spots.length" class="kb-table">
            <thead>
              <tr><th>景点</th><th>区域</th><th>时长</th><th>标签</th><th>操作</th></tr>
            </thead>
            <tbody>
              <tr v-for="s in pagedSpots" :key="s.name">
                <td>{{ s.name }}</td>
                <td>{{ s.area || '-' }}</td>
                <td>{{ s.duration ? s.duration + 'h' : '-' }}</td>
                <td>
                  <span v-for="t in s.tags" :key="t" class="tag-mini">{{ t }}</span>
                </td>
                <td class="spot-actions">
                  <button class="btn mini" @click="openEdit(s)">编辑</button>
                  <button class="btn danger mini" @click="deleteSpot(s.name)">删除</button>
                </td>
              </tr>
            </tbody>
          </table>
          <div v-else class="empty">{{ activeCity ? '该地点暂无景点' : '请先在上方选择地点' }}</div>
          <div v-if="spots.length && spotTotalPages > 1" class="pager">
            <button class="btn ghost mini" :disabled="spotPage <= 1" @click="spotPage--">‹ 上一页</button>
            <span class="pager-info">{{ spotPage }} / {{ spotTotalPages }}</span>
            <button class="btn ghost mini" :disabled="spotPage >= spotTotalPages" @click="spotPage++">下一页 ›</button>
          </div>

          <!-- 编辑景点 -->
          <div v-if="editing" class="edit-panel">
            <div class="kb-card-title">✏️ 编辑景点：{{ editForm.name }}</div>
            <div class="form-grid">
              <label>景点名 *
                <input v-model.trim="editForm.name" placeholder="修改名称将同步迁移向量">
              </label>
              <label>区域
                <input v-model.trim="editForm.area" placeholder="如：武昌区">
              </label>
              <label>省份
                <input v-model.trim="editForm.province" placeholder="如：湖北">
              </label>
              <label>建议时长（h）
                <input v-model.number="editForm.duration" type="number" min="0.5" step="0.5" placeholder="2">
              </label>
              <label>标签（逗号分隔）
                <input v-model.trim="editForm.tags" placeholder="自然,公园,拍照">
              </label>
              <label class="full">简介 / 推荐理由
                <textarea v-model.trim="editForm.reason" rows="2" placeholder="该景点的简介或推荐理由"></textarea>
              </label>
            </div>
            <div class="run-head">
              <span class="hint">保存后立即单点同步到向量库</span>
              <div class="spacer"></div>
              <button class="btn ghost mini" @click="cancelEdit">取消</button>
              <button class="btn mini" :disabled="savingEdit" @click="saveEditSpot">{{ savingEdit ? '保存中…' : '保存修改' }}</button>
            </div>
          </div>
        </div>
      </div>

      <!-- 标签清洗 + 景点清洗 左右并排 -->
      <div class="kb-cols">
        <div class="kb-card">
          <div class="kb-card-title kb-title-row">🏷️ 标签清洗
          <span class="flex-spacer"></span>
          <button class="btn mini ghost" :disabled="cleaningAllTags || cleaningTags" @click="cleanAllTagsGlobally">{{ cleaningAllTags ? '更新中' + cleanAllProg : '更新所有城市标签' }}</button>
          <button class="btn mini" @click="openAddTag">＋ 新增标签</button>
          <button class="btn mini" :disabled="upgradingTags" @click="upgradeTagLibrary">{{ upgradingTags ? '更新中…' : '更新标签' }}</button>
        </div>
        <!-- 省份 + 城市 + 更新城市标签 -->
        <div class="run-head">
          <select v-model="cleanProvince" class="tag-city-select" @change="cleanCity = ''">
            <option v-for="p in provinceList" :key="p" :value="p">{{ p }}</option>
          </select>
          <select v-model="cleanCity" class="tag-city-select">
            <option v-for="c in cleanCities" :key="c.city" :value="c.city">{{ c.city }}</option>
          </select>
          <button class="btn mini" :disabled="cleaningTags || !cleanCity || cleaningAllTags || qcAiAllLoading" @click="cleanAllTags">{{ cleaningTags ? '更新中…' : '更新城市标签' }}</button>
        </div>
        <div class="tag-groups">
          <div v-for="g in tagGroups" :key="g.name || '_rest'" class="tag-group">
            <div v-if="g.name" class="tag-group-name">{{ g.name }}</div>
            <div class="tag-lib-preview">
              <span v-for="t in g.tags" :key="t" class="tag-mini lib" :class="{ locked: tagLocked.has(t) }" :title="t" @click="openTagDetail(t)">{{ t }}</span>
            </div>
          </div>
        </div>

        <!-- 新增标签弹框 -->
        <div v-if="tagAddOpen" class="modal-mask" @click.self="tagAddOpen = false">
          <div class="modal modal-sm">
            <div class="m-head"><h2>新增标签</h2><button class="m-close" @click="tagAddOpen = false">✕</button></div>
            <div class="m-body">
              <label class="field">
                <span>标签名 *</span>
                <input v-model="newTagName" placeholder="如：夜景" />
              </label>
              <label class="field">
                <span>分类 *</span>
                <select v-model="newTagCategory">
                  <option v-for="c in tagCategories" :key="c" :value="c">{{ c }}</option>
                </select>
              </label>
              <label class="field">
                <span>别名（逗号分隔）</span>
                <input v-model="newTagAliases" placeholder="如：灯光秀,灯光夜景" />
              </label>
            </div>
            <div class="m-foot">
              <button class="btn ghost" @click="tagAddOpen = false">取消</button>
              <button class="btn" :disabled="addingTag" @click="submitAddTag">{{ addingTag ? '保存中…' : '保存' }}</button>
            </div>
          </div>
        </div>

        <!-- 标签详情弹框 -->
        <div v-if="tagDetailOpen" class="modal-mask" @click.self="tagDetailOpen = false">
          <div class="modal modal-sm">
            <div class="m-head">
              <h2>标签：{{ tagDetail && tagDetail.tag }}</h2>
              <label class="lock-toggle" :title="tagEdit.locked ? '已锁定：更新标签库时保持不变' : '未锁定'">
                <input type="checkbox" v-model="tagEdit.locked" />
                <span class="lock-icon">{{ tagEdit.locked ? '🔒' : '🔓' }}</span>
              </label>
              <button class="m-close" @click="tagDetailOpen = false">✕</button>
            </div>
            <div class="m-body">
              <label class="field">
                <span>分类</span>
                <input v-model="tagEdit.category" placeholder="如：自然风光" />
              </label>
              <label class="field">
                <span>别名（逗号分隔）</span>
                <input v-model="tagEdit.aliases" placeholder="如：灯光秀,灯光夜景" />
              </label>
            </div>
            <div class="m-foot">
              <button class="btn danger" :disabled="savingTag" @click="submitDeleteTag">删除</button>
              <span class="flex-spacer"></span>
              <button class="btn ghost" @click="tagDetailOpen = false">取消</button>
              <button class="btn" :disabled="savingTag" @click="submitTagDetail">{{ savingTag ? '保存中…' : '保存' }}</button>
            </div>
          </div>
        </div>
      </div>

      <!-- 景点清洗（原景点质量保证） -->
      <div class="kb-card qc-card">
        <div class="kb-card-title kb-title-row">🛡️ 景点清洗
          <span class="flex-spacer"></span>
          <button class="btn mini ghost" :disabled="qcAiAllLoading || qcAiLoading" @click="openProvincePick">{{ qcAiAllLoading ? '检测中' + qcAiAllProg : '检测省份景点' }}</button>
          <button class="btn mini ghost" :disabled="qcAiAllLoading || qcAiLoading" @click="qcRunAiAll">{{ qcAiAllLoading ? '检测中' + qcAiAllProg : '检测所有城市景点' }}</button>
        </div>
        <div class="run-head">
          <select v-model="qcProvince" class="tag-city-select" @change="qcCity = ''; qcReset()">
            <option v-for="p in provinceList" :key="p" :value="p">{{ p }}</option>
          </select>
          <select v-model="qcCity" class="tag-city-select" @change="qcReset">
            <option v-for="c in qcFilteredCities" :key="c.city" :value="c.city">{{ c.city }}</option>
          </select>
          <button class="btn mini" :disabled="qcAiLoading || !qcCity || cleaningAllTags || qcAiAllLoading" @click="qcRunAi">{{ qcAiLoading ? '检测中…' : '检测城市景点' }}</button>
        </div>

        <div v-if="qcGroups.length" class="qc-list">
          <div v-for="(g, i) in qcGroups" :key="i" class="qc-item">
            <div class="qc-reason">{{ g.reason }}</div>
            <div v-if="g.ai_judgment" class="qc-ai">
              <b>AI 判定：{{ g.ai_judgment }}</b>
              <span v-if="g.ai_reason" class="muted">（{{ g.ai_reason }}）</span>
            </div>
            <div class="qc-row"><span class="qc-role main">主｜{{ g.main }}</span><span class="qc-role sub">子｜{{ g.sub }}</span></div>
            <div class="qc-actions">
              <button class="btn mini" :class="{ decided: qcDecision[i] === 'merge' }" @click="qcSet(i, 'merge')">合并到主</button>
              <button class="btn danger mini" :class="{ decided: qcDecision[i] === 'delete' }" @click="qcSet(i, 'delete')">删除子</button>
              <button class="btn ghost mini" :class="{ decided: qcDecision[i] === 'keep' }" @click="qcSet(i, 'keep')">保留</button>
            </div>
          </div>
          <div class="run-head" style="margin-top:6px">
            <span class="hint">已选 {{ qcDecision.filter((d) => d).length }} 条（未选的不做处理）</span>
            <div class="spacer"></div>
            <button class="btn" :disabled="qcApplying || !qcDecision.some((d) => d)" @click="qcApply">{{ qcApplying ? '应用中…' : '应用处置' }}</button>
          </div>
        </div>
      </div>
    </div>

      <!-- 新增景点 -->
      <div class="kb-card">
        <div class="kb-card-title">➕ 新增景点
          <span class="flex-spacer"></span>
          <button v-if="aiMode === 'ai'" class="btn mini" :disabled="aiGenerating" @click="aiGenerateCity">
            {{ aiGenerating ? '生成中…' : '生成' }}
          </button>
          <button v-else class="btn mini" :disabled="creating" @click="createSpot">
            {{ creating ? '创建中…' : '创建景点' }}
          </button>
        </div>
        <div class="mode-tabs">
          <span class="mode-tab" :class="{ on: aiMode === 'ai' }" @click="aiMode = 'ai'">🤖 AI 生成</span>
          <span class="mode-tab" :class="{ on: aiMode === 'manual' }" @click="aiMode = 'manual'">✍️ 手动填写</span>
        </div>

        <!-- AI 生成：输入城市，AI 推荐一批高质量景点（自动清洗低质量项），前端勾选后批量导入 -->
        <template v-if="aiMode === 'ai'">
          <div class="form-grid">
            <label>城市 *
              <input v-model.trim="aiForm.city" placeholder="如：武汉">
            </label>
            <label>预估景点数量
              <input v-model.number="aiForm.count" type="number" min="1" :max="100" placeholder="默认 12~20">
            </label>
          </div>

          <div v-if="aiResult" class="ai-result">
            <div class="ai-result-head">
              <span>✨ {{ aiResult.city }} · 本次新增 {{ aiResult.spots.length }} 个景点{{ (aiResult.excluded_existing || 0) > 0 ? '，已自动去重已有 ' + aiResult.excluded_existing + ' 个' : '' }}</span>
              <span class="spacer"></span>
              <label class="check-all"><input type="checkbox" :checked="selectedCount === aiResult.spots.length" @change="toggleAll($event.target.checked)"> 全选</label>
            </div>
            <div class="ai-spot-list">
              <label v-for="(s, i) in aiResult.spots" :key="s.name" class="ai-spot" :class="{ on: aiSelected[i] }">
                <input v-model="aiSelected[i]" type="checkbox">
                <div class="ai-spot-body">
                  <div class="ai-spot-name">{{ s.name }} <span class="ai-spot-meta">{{ s.area }} · {{ s.duration }}h</span></div>
                  <div class="ai-spot-tags"><span v-for="t in s.tags" :key="t" class="tag-mini">{{ t }}</span></div>
                  <div class="ai-spot-reason">{{ s.reason }}</div>
                </div>
              </label>
            </div>
            <div class="ai-preview">
              <div class="ai-preview-head">
                <span>📄 生成 JSON（可复制）</span>
                <span class="spacer"></span>
                <button class="btn ghost mini" @click="copyJson">📋 复制</button>
              </div>
              <pre class="ai-json">{{ aiResult.json_text }}</pre>
            </div>
            <div class="ai-preview-foot">
              <span class="hint">{{ aiImported ? '已导入，上方 JSON 与右侧景点表均已同步展示' : '勾选需要导入的景点（默认全选，可取消低质量项）' }}</span>
              <span class="spacer"></span>
              <button class="btn" :disabled="creating || selectedCount === 0 || aiImported" @click="saveAiSpots">
                {{ creating ? '导入中…' : aiImported ? '✓ 已导入（可复制上方 JSON）' : '💾 导入选中景点（' + selectedCount + ' 个）' }}
              </button>
            </div>
          </div>
        </template>

        <!-- 手动填写 -->
        <template v-else>
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
        </template>
      </div>
    </template>

    <div v-else class="kb-card">
      <div class="kb-card-title">ℹ️ {{ activeBaseLabel }}</div>
      <div class="empty">该知识库暂不支持在线管理，请通过数据导入方式维护。</div>
    </div>

    <!-- 重建（直接重建整个知识库；各子库的独立重建 / 定时在「定时任务」页） -->
    <div class="kb-card">
      <div class="kb-card-title">🔄 知识库重建</div>
      <div class="run-head">
        <span class="state" :class="runStateClass">{{ runStateText }}</span>
        <div class="spacer"></div>
        <button class="btn ghost mini" :disabled="loading" @click="loadAll">刷新</button>
        <button class="btn mini" :disabled="isRunning || reindexing" @click="reindex()">
          {{ isRunning ? '重建中…' : '重建知识库' }}
        </button>
      </div>
      <div v-if="status.total > 0 || runError" class="run-detail">
        <div class="kv"><span>本次任务导入</span><b>{{ status.total }} 条</b></div>
        <div class="kv"><span>总耗时</span><b>{{ fmtDuration(status.duration_ms) }}</b></div>
        <div v-if="runError" class="err-box">⚠ {{ runError }}</div>
      </div>
    </div>

    <!-- 删除地点弹框：勾选要删除的地点 -->
    <div v-if="delBoxOpen" class="modal-mask" @click.self="delBoxOpen = false">
      <div class="modal modal-sm">
        <div class="m-head"><h2>🗑 删除地点</h2><button class="m-close" @click="delBoxOpen = false">✕</button></div>
        <div class="m-body">
          <div class="del-city-hint">勾选要删除的地点（将删除其下全部景点，不可恢复）</div>
          <div class="prov-filter del-prov-filter">
            <select v-model="delProvince">
              <option v-for="p in provinceList" :key="p" :value="p">{{ p }}</option>
            </select>
          </div>
          <div v-if="delFilteredCities.length" class="del-city-list">
            <label v-for="c in delFilteredCities" :key="c.city" class="del-city">
              <input type="checkbox" :value="c.city" v-model="delCitySel">
              <span class="dc-name">{{ c.city }}</span>
              <b class="dc-count">{{ c.count }} 个景点</b>
            </label>
          </div>
          <div v-else class="empty">暂无地点</div>
        </div>
        <div class="m-foot">
          <button class="btn ghost" @click="delBoxOpen = false">取消</button>
          <button class="btn danger" :disabled="!delCitySel.length" @click="confirmDeleteCities">确认删除（{{ delCitySel.length }}）</button>
        </div>
      </div>
    </div>

    <!-- 删除确认弹框 -->
    <div v-if="confirmBox" class="modal-mask" @click.self="confirmBox = null">
      <div class="modal modal-sm">
        <div class="m-head"><h2>操作确认</h2><button class="m-close" @click="confirmBox = null">✕</button></div>
        <div class="m-body">{{ confirmBox.text }}</div>
        <div class="m-foot">
          <button class="btn ghost" @click="confirmBox = null">取消</button>
          <button class="btn danger" @click="confirmOk">确认删除</button>
        </div>
      </div>
    </div>
  <!-- 选择省份 → 检测该省全部城市景点 -->
    <div v-if="provincePickOpen" class="modal-mask" @click.self="provincePickOpen = false">
      <div class="modal modal-sm">
        <div class="m-head"><h2>检测省份景点</h2><button class="m-close" @click="provincePickOpen = false">✕</button></div>
        <div class="m-body">
          <label>选择省份 *
            <select v-model="provincePickVal" class="tag-city-select" style="width: 100%">
              <option v-for="p in provinceList" :key="p" :value="p">{{ p }}</option>
            </select>
          </label>
        </div>
        <div class="m-foot">
          <button class="btn ghost" @click="provincePickOpen = false">取消</button>
          <button class="btn" :disabled="!provincePickVal" @click="confirmProvincePick">确认检测</button>
        </div>
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

const SPOT_PAGE_SIZE = 15
const spotPage = ref(1)

// 新增景点：AI 生成 / 手动填写
const aiMode = ref('ai')
const aiGenerating = ref(false)
const aiResult = ref(null) // { city, spots, total, dropped, json_text }
const aiSelected = ref([]) // 与 aiResult.spots 对齐的勾选状态
const aiImported = ref(false) // 本次 AI 清单是否已导入（导入后保留 JSON 预览，按钮置灰）
const aiForm = ref({ city: '', hint: '', count: null })

const status = ref({})

const form = ref({ city: '', name: '', area: '', province: '', duration: 2, tags: '', reason: '' })

// 编辑景点
const editing = ref(false)
const savingEdit = ref(false)
const editBaseName = ref('') // 编辑前景点名（用于定位，改名时作为请求路径）
const editForm = ref({ name: '', area: '', province: '', duration: 2, tags: '', reason: '' })

// 删除地点弹框：勾选要删除的地点多选删除
const delBoxOpen = ref(false)
const delCitySel = ref([])

// 标签清洗
const standardTags = ref([])
const tagGroups = ref([]) // [{ name, tags: [] }]，按分类分组
// 已有分类（供新增标签时下拉选择）
const tagCategories = computed(() => tagGroups.value.map((g) => g.name).filter(Boolean))
const cleaningTags = ref(false)
const cleaningAllTags = ref(false)
const upgradingTags = ref(false)
const cleanCity = ref('') // 重刷目的地（在标签清洗区独立选择）
const cleanProvince = ref('') // 重刷省份（先选省份再选城市）
const tagAddOpen = ref(false)
const newTagName = ref('')
const newTagCategory = ref('')
// 标签详情：编辑 / 删除 / 锁定
const tagLocked = ref(new Set())
const tagDetailOpen = ref(false)
const tagDetail = ref(null) // { tag }
const tagEdit = ref({ aliases: '', category: '', locked: false })
const savingTag = ref(false)
const tagAliasMap = ref({}) // 标准标签 → 别名列表
const newTagAliases = ref('')
const addingTag = ref(false)

// 景点质量保证
const qcProvince = ref('')
const qcCity = ref('')
// 景点清洗：先选省份，再筛选城市
const qcFilteredCities = computed(() => filterByProvince(sortedCities.value, qcProvince.value))
const qcGroups = ref([])
const qcDecision = ref([])
const qcLoading = ref(false)
const qcApplying = ref(false)
const qcRan = ref(false)
const qcAiLoading = ref(false)
const qcAiAllLoading = ref(false)
const provincePickOpen = ref(false)
const provincePickVal = ref('')
const cleanAllProg = ref('')   // 更新所有城市标签进度提示
const qcAiAllProg = ref('')    // 检测所有城市景点进度提示
let taskPollTimer = null

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

// 地点列表：按省份筛选（默认选中拼音首字母排序的第一个省份），不做「全部」展示
// 省份列表（去重、有值优先，按拼音首字母排序）
const provinceList = computed(() => {
  const set = new Set()
  cities.value.forEach((c) => { if (c.province) set.add(c.province) })
  return Array.from(set).sort((a, b) => a.localeCompare(b, 'zh'))
})
const provinceFilter = ref('')
// 全部地点：按首字母（拼音）排序
const sortedCities = computed(() => {
  return [...cities.value].sort((a, b) => String(a.city || '').localeCompare(String(b.city || ''), 'zh'))
})
// 通用：按省份过滤地点（province 为空则返回全部）
function filterByProvince(list, province) {
  if (!province) return list
  return list.filter((c) => c.province === province)
}
// 通用：把逗号/中文逗号分隔的字符串解析为去空白的非空列表
function parseList(str) {
  return String(str || '').split(/[,，]/).map((s) => s.trim()).filter(Boolean)
}
// 按省份筛选后的地点
const filteredCities = computed(() => filterByProvince(sortedCities.value, provinceFilter.value))
// 标签清洗：先选省份，再筛选城市
const cleanCities = computed(() => filterByProvince(sortedCities.value, cleanProvince.value))
// 删除地点弹框：同样按省份筛选（默认第一个省份）
const delProvince = ref('')
const delFilteredCities = computed(() => filterByProvince(sortedCities.value, delProvince.value))

// 景点分页
const spotTotalPages = computed(() => Math.max(1, Math.ceil(spots.value.length / SPOT_PAGE_SIZE)))
const pagedSpots = computed(() => {
  const start = (spotPage.value - 1) * SPOT_PAGE_SIZE
  return spots.value.slice(start, start + SPOT_PAGE_SIZE)
})

// AI 生成结果中已勾选的景点数
const selectedCount = computed(() => (aiResult.value ? aiSelected.value.filter(Boolean).length : 0))

let timer = null
let lastLoad = 0

const isRunning = computed(() => status.value.state === 'running')
const runStateClass = computed(() => 's-' + (status.value.state || 'idle'))
const runStateText = computed(() => stateText(status.value.state))
const runError = computed(() => status.value.error || '')

function stateText(s) {
  return { running: '运行中', success: '成功', failed: '失败', idle: '空闲' }[s] || '空闲'
}

async function loadBases() {
  try {
    const data = await api.kbBases()
    bases.value = data.bases || []
  } catch (e) {
    toast(e.message || '获取知识库列表失败', 'error')
  }
}

async function loadCities() {
  try {
    const data = await api.kbCities()
    cities.value = data.cities || []
    // 默认选中第一个省份（按拼音首字母排序），避免铺开全部
    if (!provinceList.value.includes(provinceFilter.value)) {
      provinceFilter.value = provinceList.value[0] || ''
    }
    if (activeCity.value && !filteredCities.value.some((c) => c.city === activeCity.value)) {
      activeCity.value = ''
      spots.value = []
    }
    if (!activeCity.value && filteredCities.value.length) {
      selectCity(filteredCities.value[0].city)
    }
  } catch (e) {
    toast(e.message || '获取地点列表失败', 'error')
  }
}

function onProvinceFilter() {
  // 切换省份后，若当前选中地点不在新筛选范围内则清空选中
  if (activeCity.value && !filteredCities.value.some((c) => c.city === activeCity.value)) {
    activeCity.value = ''
    spots.value = []
  }
  if (!activeCity.value && filteredCities.value.length) {
    selectCity(filteredCities.value[0].city)
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
    if (spotPage.value > spotTotalPages.value) spotPage.value = spotTotalPages.value
  } catch (e) {
    toast(e.message || '获取景点列表失败', 'error')
  }
}

async function loadStatus() {
  try {
    const data = await api.kbStatus()
    status.value = data.status || {}
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
  spotPage.value = 1
  loadSpots()
}

async function createSpot() {
  const city = form.value.city.trim()
  const name = form.value.name.trim()
  if (!city || !name) {
    toast('请填写地点和景点名', 'error')
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
      tags: parseList(form.value.tags),
    }
    const data = await api.kbCreateSpot(payload)
    toast(data.message || '创建完成')
    if (data.status === 'ok') {
      form.value = { city, name: '', area: '', province: '', duration: 2, tags: '', reason: '' }
      activeCity.value = city
      await Promise.all([loadCities(), loadSpots()])
    }
  } catch (e) {
    toast(e.message || '创建失败', 'error')
  } finally {
    creating.value = false
  }
}

function openEdit(s) {
  const info = cities.value.find((c) => c.city === activeCity.value)
  editForm.value = {
    name: s.name,
    area: s.area || '',
    province: (info && info.province) || '',
    duration: s.duration || 2,
    tags: (s.tags || []).join(','),
    reason: s.reason || '',
  }
  editBaseName.value = s.name
  editing.value = true
}

function cancelEdit() {
  editing.value = false
  editBaseName.value = ''
}

async function saveEditSpot() {
  const city = activeCity.value
  const name = editBaseName.value
  const newName = editForm.value.name.trim()
  if (!city || !name) {
    toast('请先选择地点', 'error')
    return
  }
  if (!newName) {
    toast('景点名不能为空', 'error')
    return
  }
  savingEdit.value = true
  try {
    const payload = {
      name: newName,
      area: editForm.value.area,
      province: editForm.value.province,
      duration: editForm.value.duration || 2,
      reason: editForm.value.reason,
      tags: parseList(editForm.value.tags),
    }
    const data = await api.kbUpdateSpot(city, name, payload)
    toast(data.message || '保存完成')
    if (data.status === 'ok') {
      editing.value = false
      editBaseName.value = ''
      await Promise.all([loadSpots()])
    }
  } catch (e) {
    toast(e.message || '保存失败', 'error')
  } finally {
    savingEdit.value = false
  }
}

async function aiGenerateCity() {
  const city = aiForm.value.city.trim()
  if (!city) {
    toast('请填写城市', 'error')
    return
  }
  aiResult.value = null
  aiSelected.value = []
  aiImported.value = false
  startBgTask(() => {
    const count = aiForm.value.count ? Math.max(1, Math.min(100, Math.round(aiForm.value.count))) : null
    return api.kbAiGenerateSpot({ city, hint: aiForm.value.hint.trim(), count })
  }, aiGenerating, 'AI 生成失败')
}

function toggleAll(checked) {
  if (!aiResult.value) return
  aiSelected.value = aiResult.value.spots.map(() => checked)
}

async function saveAiSpots() {
  const city = aiForm.value.city.trim()
  const spots = (aiResult.value?.spots || []).filter((_, i) => aiSelected.value[i])
  if (!city || !spots.length) {
    toast('请先选择要导入的景点', 'error')
    return
  }
  creating.value = true
  try {
    const data = await api.kbBatchCreateSpots({ city, spots })
    toast(data.message || `导入完成：成功 ${data.created} / 已存在 ${data.exists} / 失败 ${data.failed}`)
    if (data.status === 'ok' && (data.created > 0 || data.exists > 0)) {
      // 保留 aiResult 以便上方 JSON 预览仍然可见，并刷新景点表展示入库内容
      aiImported.value = true
      aiSelected.value = []
      activeCity.value = city
      await Promise.all([loadCities(), loadSpots()])
    }
  } catch (e) {
    toast(e.message || '批量导入失败', 'error')
  } finally {
    creating.value = false
  }
}

async function copyJson() {
  if (!aiResult.value) return
  try {
    await navigator.clipboard.writeText(aiResult.value.json_text || '')
    toast('已复制 JSON')
  } catch (e) {
    toast('复制失败，请手动选择复制', 'error')
  }
}

async function deleteSpot(name) {
  if (!activeCity.value) return
  askConfirm(`确定删除景点「${name}」（${activeCity.value}）吗？`, async () => {
    try {
      const data = await api.kbDeleteSpot(activeCity.value, name)
      toast(data.message || '删除完成')
      await Promise.all([loadSpots(), loadCities()])
    } catch (e) {
      toast(e.message || '删除失败', 'error')
    }
  })
}

function openDeleteCities() {
  delCitySel.value = []
  delProvince.value = provinceList.value[0] || '' // 默认第一个省份
  delBoxOpen.value = true
}

async function confirmDeleteCities() {
  const list = delCitySel.value.slice()
  delBoxOpen.value = false
  for (const city of list) {
    try {
      const data = await api.kbDeleteCity(city)
      toast(data.message || '已删除 ' + city)
      if (activeCity.value === city) {
        activeCity.value = ''
        spots.value = []
      }
    } catch (e) {
      toast(`${city}：${e.message || '删除失败'}`, 'error')
    }
  }
  await loadCities()
}

// 标签清洗：加载标准标签库 + 一键重刷
async function loadTagLibrary() {
  try {
    const data = await api.kbTagLibrary()
    standardTags.value = data.standard || []
    tagAliasMap.value = data.tags || {}
    tagLocked.value = new Set(data.locked || [])
    const cats = data.categories || {}
    const groups = Object.entries(cats).map(([name, arr]) => ({ name, tags: (arr || []).filter((t) => standardTags.value.includes(t)) }))
    // 未归入任何分类的标准标签：展示但无分组标题（不显示"其他"），保证标签不丢失
    const used = new Set()
    groups.filter((g) => g.name).forEach((g) => g.tags.forEach((t) => used.add(t)))
    const rest = standardTags.value.filter((t) => !used.has(t))
    if (rest.length) groups.push({ name: '', tags: rest })
    tagGroups.value = groups
  } catch (e) {
    toast(e.message || '标签库加载失败', 'error')
  }
}
function openTagDetail(t) {
  const tag = String(t)
  const category = tagGroups.value.find((g) => g.tags.includes(tag))?.name || ''
  tagDetail.value = { tag }
  tagEdit.value = {
    aliases: (tagAliasMap.value[tag] || []).join(', '),
    category,
    locked: tagLocked.value.has(tag),
  }
  tagDetailOpen.value = true
}
async function submitTagDetail() {
  if (savingTag.value) return
  savingTag.value = true
  try {
    const aliases = parseList(tagEdit.value.aliases)
    const data = await api.kbUpdateTag({
      tag: tagDetail.value.tag,
      aliases,
      category: tagEdit.value.category.trim(),
      locked: !!tagEdit.value.locked,
    })
    toast(data.message || '已保存')
    tagDetailOpen.value = false
    await loadTagLibrary()
  } catch (e) {
    toast(e.message || '保存失败', 'error')
  } finally {
    savingTag.value = false
  }
}
async function submitDeleteTag() {
  if (savingTag.value) return
  if (!confirm(`确定删除标准标签「${tagDetail.value.tag}」？\n已入库景点中的该标签下次清洗时会将其丢弃。`)) return
  savingTag.value = true
  try {
    const data = await api.kbDeleteTag(tagDetail.value.tag)
    toast(data.message || '已删除')
    tagDetailOpen.value = false
    await loadTagLibrary()
  } catch (e) {
    toast(e.message || '删除失败', 'error')
  } finally {
    savingTag.value = false
  }
}
function openAddTag() {
  newTagName.value = ''
  newTagAliases.value = ''
  newTagCategory.value = tagCategories.value[0] || ''
  tagAddOpen.value = true
}
async function submitAddTag() {
  if (addingTag.value) return
  const tag = newTagName.value.trim()
  if (!tag) {
    toast('请填写标签名', 'error')
    return
  }
  const category = newTagCategory.value.trim()
  if (!category) {
    toast('请选择标签分类', 'error')
    return
  }
  addingTag.value = true
  try {
    const aliases = parseList(newTagAliases.value)
    const data = await api.kbAddTag({ tag, aliases, category })
    if (data.status === 'error') {
      toast(data.message || '新增失败', 'error')
    } else {
      toast(data.message || '已新增')
      tagAddOpen.value = false
      newTagName.value = ''
      newTagAliases.value = ''
      newTagCategory.value = ''
      await loadTagLibrary()
    }
  } catch (e) {
    toast(e.message || '新增失败', 'error')
  } finally {
    addingTag.value = false
  }
}
// 通用：启动一个后台任务（LLM 长任务）。自动 guard loading、失败 toast，成功则开始轮询
async function startBgTask(call, loadingRef, errText) {
  if (loadingRef.value) return
  loadingRef.value = true
  try {
    const data = await call()
    if (!data.running || data.status === 'error') {
      toast(data.message || errText, 'error')
      loadingRef.value = false
      return
    }
    pollTasks()
  } catch (e) {
    toast(e.message || errText, 'error')
    loadingRef.value = false
  }
}

async function cleanAllTags() {
  if (!cleanCity.value) {
    toast('请先选择要重刷的地点', 'error')
    return
  }
  startBgTask(() => api.kbCleanTags(cleanCity.value), cleaningTags, '清洗失败')
}
async function cleanAllTagsGlobally() {
  if (cleaningAllTags.value) return
  askConfirm('将用 AI 更新知识库中所有城市景点的标签，是否继续？', () => doCleanAllTagsGlobally())
}
async function doCleanAllTagsGlobally() {
  cleanAllProg.value = ''
  startBgTask(() => api.kbCleanAllTags(), cleaningAllTags, '清洗失败')
}
async function upgradeTagLibrary() {
  if (upgradingTags.value) return
  upgradingTags.value = true
  try {
    const data = await api.kbUpgradeTags()
    if (data.status === 'error') {
      toast(data.message || '标签库升级失败', 'error')
    } else {
      const scrub = data.scrub || {}
      toast(`标签库已升级：${data.old_count || 0} → ${(data.new_tags || []).length} 个标准标签；已重刷 ${scrub.changed_spots || 0} 个景点`)
      await loadTagLibrary()
      await loadCities()
      if (activeCity.value) await loadSpots(activeCity.value)
    }
  } catch (e) {
    toast(e.message || '标签库升级失败', 'error')
  } finally {
    upgradingTags.value = false
  }
}

// 景点质量保证
function qcReset() {
  qcGroups.value = []
  qcDecision.value = []
  qcRan.value = false
}
function qcSet(i, v) {
  qcDecision.value[i] = qcDecision.value[i] === v ? '' : v
}
async function qcRun() {
  if (!qcCity.value || qcLoading.value) return
  qcLoading.value = true
  qcRan.value = true
  try {
    const data = await api.kbQualityCheck(qcCity.value)
    qcGroups.value = data.groups || []
    qcDecision.value = (data.groups || []).map(() => '')
  } catch (e) {
    toast(e.message || '质量检测失败', 'error')
    qcGroups.value = []
  } finally {
    qcLoading.value = false
  }
}
async function qcRunAi() {
  if (!qcCity.value) return
  qcRan.value = true
  qcGroups.value = []
  qcDecision.value = []
  startBgTask(() => api.kbQualityAi(qcCity.value), qcAiLoading, 'AI 判断失败')
}
async function qcRunAiAll() {
  qcAiAllProg.value = ''
  qcRan.value = true
  startBgTask(() => api.kbQualityAiAll(), qcAiAllLoading, 'AI 判断失败')
}
function openProvincePick() {
  if (qcAiAllLoading.value || qcAiLoading.value) return
  provincePickVal.value = qcProvince.value || ''
  provincePickOpen.value = true
}
function confirmProvincePick() {
  if (!provincePickVal.value) return
  provincePickOpen.value = false
  qcProvince.value = provincePickVal.value
  qcCity.value = ''
  qcReset()
  qcRunAiProvince()
}
async function qcRunAiProvince() {
  if (!qcProvince.value) {
    toast('请先选择省份', 'error')
    return
  }
  qcAiAllProg.value = ''
  qcRan.value = true
  startBgTask(() => api.kbQualityAiProvince(qcProvince.value), qcAiAllLoading, 'AI 判断失败')
}
// 后台长任务轮询：刷新后恢复运行态、渐进展示 quality_all 结果、局运行时禁用单城操作
const QC_NEED = ['child', 'duplicate', 'low_quality', 'sub']
function qcFilterGroups(groups) {
  return (groups || []).filter((g) => QC_NEED.includes((g.ai_judgment || '').toLowerCase()))
}
function qcSyncGroups(groups) {
  const gs = qcFilterGroups(groups)
  qcGroups.value = gs
  qcDecision.value = gs.map((g) => (g.ai_action && g.ai_action !== 'keep' ? g.ai_action : 'merge'))
}
async function pollTasks() {
  let tasks = {}
  try {
    const data = await api.kbTaskStatus()
    tasks = data.tasks || {}
  } catch (e) {
    return
  }
  const ca = tasks.clean_all || {}
  const qa = tasks.quality_all || {}
  const cc = tasks.clean_city || {}
  const qci = tasks.quality_city || {}
  const ag = tasks.ai_generate || {}

  if (ca.running) {
    cleaningAllTags.value = true
    cleanAllProg.value = ca.total_cities ? `（${ca.done_cities || 0}/${ca.total_cities}）` : ''
  } else if (cleaningAllTags.value) {
    cleaningAllTags.value = false
    cleanAllProg.value = ''
    if (ca.error) toast(ca.error, 'error')
    else toast(`全部城市 AI 重打完成：更新了 ${ca.changed_spots || 0} 个景点`)
    loadAll()
    loadTagLibrary()
  }
  if (qa.running) {
    qcAiAllLoading.value = true
    qcRan.value = true
    qcAiAllProg.value = qa.total_cities ? `（${qa.done_cities || 0}/${qa.total_cities}）` : ''
    qcSyncGroups(qa.groups)
  } else if (qcAiAllLoading.value) {
    qcAiAllLoading.value = false
    qcAiAllProg.value = ''
    qcSyncGroups(qa.groups)
    if (qa.error) toast(qa.error, 'error')
    else toast(`检测完成${qa.scope && qa.scope !== '全部' ? '（' + qa.scope + '）' : ''}：发现 ${qa.issues || 0} 处需处置`)
  }
  if (cc.running) {
    cleaningTags.value = true
  } else if (cleaningTags.value) {
    cleaningTags.value = false
    if (cc.error) toast(cc.error, 'error')
    else toast(`「${cc.scope || cleanCity.value}」AI 重打完成：更新了 ${cc.changed_spots || 0} 个景点`)
    loadTagLibrary()
    if (activeCity.value) loadSpots(activeCity.value)
    else if (cleanCity.value) loadSpots(cleanCity.value)
  }
  if (qci.running) {
    qcAiLoading.value = true
    qcRan.value = true
    qcSyncGroups(qci.groups)
  } else if (qcAiLoading.value) {
    qcAiLoading.value = false
    qcSyncGroups(qci.groups)
    if (qci.error) toast(qci.error, 'error')
    else toast(`检测完成（${qci.scope || qcCity.value}）：发现 ${qci.issues || 0} 处需处置`)
  }
  if (ag.running) {
    aiGenerating.value = true
  } else if (aiGenerating.value) {
    aiGenerating.value = false
    const res = ag.result || {}
    if (ag.error) {
      toast(ag.error, 'error')
    } else if (res.status === 'ok' && res.spots && res.spots.length) {
      if (!aiForm.value.city) aiForm.value.city = ag.scope || ''
      aiResult.value = res
      aiSelected.value = res.spots.map(() => true)
      toast(`AI 已生成 ${res.spots.length} 个景点（清洗剔除 ${res.dropped || 0} 个），请勾选后导入`)
    } else {
      toast(res.message || 'AI 未生成有效景点，请重试', 'error')
    }
  }
  if (cleaningAllTags.value || qcAiAllLoading.value || cleaningTags.value || qcAiLoading.value || aiGenerating.value) ensureTaskPoll()
  else stopTaskPoll()
}
function ensureTaskPoll() {
  if (taskPollTimer) return
  taskPollTimer = setInterval(pollTasks, 2500)
}
function stopTaskPoll() {
  if (taskPollTimer) {
    clearInterval(taskPollTimer)
    taskPollTimer = null
  }
}
async function qcApply() {
  const actions = qcGroups.value
    .map((g, i) => ({ city: g.city || qcCity.value, main: g.main, sub: g.sub, action: qcDecision.value[i] }))
    .filter((a) => a.action)
  if (!actions.length || qcApplying.value) return
  qcApplying.value = true
  try {
    const data = await api.kbQualityApply(actions)
    toast('已完成')
    await loadCities()
    if (activeCity.value) await loadSpots(activeCity.value)
    qcReset()
  } catch (e) {
    toast(e.message || '处置失败', 'error')
  } finally {
    qcApplying.value = false
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

onMounted(() => {
  loadAll()
  loadTagLibrary()
  pollTasks() // 刷新后恢复后台长任务的运行态
  timer = setInterval(() => {
    if (isRunning.value) {
      loadAll()
    } else if (!document.hidden && Date.now() - lastLoad > 30000) {
      loadAll()
    }
  }, 5000)
})
onBeforeUnmount(() => { clearInterval(timer); clearInterval(taskPollTimer) })
</script>

<style scoped>
.kb-wrap { display: flex; flex-direction: column; gap: 14px; }
/* 标签清洗 + 景点清洗 左右并排 */
.kb-cols { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; align-items: stretch; }
@media (max-width: 1100px) { .kb-cols { grid-template-columns: 1fr; } }
.kb-card { background: #fff; border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px; }
.kb-card-title { font-size: 14px; font-weight: 700; margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }
.kb-title-row { display: flex; align-items: center; gap: 10px; }
.flex-spacer { flex: 1; }
.edit-panel { border: 1px dashed var(--primary); border-radius: 10px; padding: 14px 16px; margin-top: 12px; background: #f8faff; }
/* 地点列表在上、景点列表在下：垂直排列 */
.kb-stack { display: flex; flex-direction: column; gap: 14px; }

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
.prov-filter { margin-bottom: 12px; }
.prov-filter select {
  width: 100%; max-width: 320px; padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px;
  font-size: 13px; outline: none; background: #fafbff; font-family: inherit;
}
.prov-filter select:focus { border-color: var(--primary); }
.del-prov-filter { margin-bottom: 10px; max-width: 320px; }
.city-list {
  max-height: 240px; overflow-y: auto;
  display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 6px;
  align-items: start;
}
.city-item {
  display: flex; align-items: center; gap: 6px; padding: 6px 9px; border: 1px solid var(--border);
  border-radius: 7px; cursor: pointer; transition: all .15s; font-size: 12px;
}
.city-item:hover { border-color: var(--primary); }
.city-item.on { border-color: var(--primary); background: var(--primary-light); }
.ci-name { font-weight: 600; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ci-count { font-size: 11px; color: var(--primary); font-weight: 700; background: var(--primary-light); padding: 1px 6px; border-radius: 10px; margin-left: auto; }

/* 景点表：固定布局，列宽不随内容变化，长景点名/过多标签不撑动位置 */
.kb-table { width: 100%; border-collapse: collapse; table-layout: fixed; }
.kb-table th, .kb-table td { text-align: left; padding: 8px 8px; font-size: 13px; border-bottom: 1px solid var(--border); word-break: break-all; }
.kb-table th { color: var(--muted); font-weight: 600; background: #fafbff; white-space: nowrap; }
.kb-table th:nth-child(1), .kb-table td:nth-child(1) { width: 24%; }
.kb-table th:nth-child(2), .kb-table td:nth-child(2) { width: 14%; }
.kb-table th:nth-child(3), .kb-table td:nth-child(3) { width: 9%; }
.kb-table th:nth-child(5), .kb-table td:nth-child(5) { width: 118px; }
.kb-table td:nth-child(1) { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.kb-table td:nth-child(2) { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); }
.tag-mini { display: inline-block; max-width: 110px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: top; font-size: 11px; background: var(--primary-light); color: var(--primary); padding: 1px 7px; border-radius: 8px; margin-right: 4px; margin-bottom: 2px; }
.spot-total { font-size: 11px; color: var(--muted); font-weight: 400; margin-left: 6px; }
.spot-actions { display: flex; gap: 6px; white-space: nowrap; }

/* 标签清洗 + 质量保证 */
.tag-groups { display: flex; flex-direction: column; gap: 10px; margin: 8px 0 4px; max-height: 60vh; overflow-y: auto; }
.tag-group { display: flex; flex-direction: column; gap: 4px; }
.tag-group-name { font-size: 12px; font-weight: 600; color: var(--muted); }
.tag-lib-preview { display: flex; flex-wrap: wrap; gap: 6px; }
.tag-mini.lib { background: #eef3ff; border: 1px solid #d8e2ff; font-weight: 600; padding: 2px 8px; cursor: pointer; }
.tag-mini.lib:hover { border-color: var(--primary); background: #e3ecff; }
.tag-mini.lib.locked { background: #fff7e6; border-color: #ffd591; color: #ad6800; }
.tag-lock { font-style: normal; margin-left: 4px; font-size: 10px; }
.check-line { flex-direction: row; align-items: center; gap: 8px; }
.field { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; }
.field span { font-size: 12px; color: var(--muted); }
.field input, .field select { font-size: 13px; padding: 7px 9px; border: 1px solid var(--border); border-radius: 7px; background: #fff; }
.qc-city-select { font-size: 12px; padding: 4px 6px; border: 1px solid var(--border); border-radius: 7px; background: #fff; }
.tag-city-select { font-size: 13px; padding: 5px 8px; min-width: 120px; border: 1px solid var(--border); border-radius: 7px; background: #fff; }
.lock-toggle { display: inline-flex; align-items: center; cursor: pointer; }
.lock-toggle input { display: none; }
.lock-icon { font-size: 17px; width: 30px; height: 30px; display: inline-flex; align-items: center; justify-content: center; border-radius: 8px; background: var(--primary-light); cursor: pointer; }
.qc-list { display: flex; flex-direction: column; gap: 8px; margin-top: 8px; flex: 1; min-height: 0; max-height: 60vh; overflow-y: auto; }
.qc-card { display: flex; flex-direction: column; }
.qc-item { border: 1px solid var(--border); border-radius: 9px; padding: 9px 11px; background: #fff; }
.qc-reason { font-size: 12px; color: var(--muted); margin-bottom: 6px; }
.qc-row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.qc-role { font-size: 12px; padding: 3px 8px; border-radius: 7px; }
.qc-role.main { background: var(--primary-light); color: var(--primary); font-weight: 600; }
.qc-role.sub { background: #fdecec; color: var(--danger); font-weight: 600; }
.qc-actions { display: flex; gap: 6px; }
.qc-actions .btn.decided { outline: 2px solid var(--primary); }
.qc-ai { font-size: 12px; color: var(--primary); background: #f2f6ff; border-radius: 6px; padding: 4px 8px; margin-bottom: 6px; }
.muted { color: var(--muted); font-weight: 400; }

/* 景点分页 */
.pager { display: flex; align-items: center; justify-content: center; gap: 12px; margin-top: 12px; }
.pager-info { font-size: 12px; color: var(--muted); }

/* 表单 */
.form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-bottom: 14px; }
.form-grid label { font-size: 12px; color: var(--muted); display: flex; flex-direction: column; gap: 5px; }
.form-grid label.full { grid-column: 1 / -1; }
.form-grid input, .form-grid textarea {
  padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; font-size: 13px; outline: none;
  font-family: inherit; resize: vertical;
}
.form-grid input:focus, .form-grid textarea:focus { border-color: var(--primary); }

/* 新增景点：AI / 手动 切换 */
.mode-tabs { display: inline-flex; border: 1px solid var(--border); border-radius: 9px; overflow: hidden; margin-bottom: 14px; }
.mode-tab { padding: 7px 18px; font-size: 13px; cursor: pointer; color: var(--muted); background: #fafbff; transition: all .15s; }
.mode-tab:hover { color: var(--primary); }
.mode-tab.on { background: var(--primary); color: #fff; font-weight: 600; }

/* AI 生成预览 */
.ai-preview { margin-top: 14px; border: 1px solid var(--primary); border-radius: 10px; padding: 12px 14px; background: var(--primary-light); }
.ai-preview-head, .ai-preview-foot { display: flex; align-items: center; gap: 10px; font-size: 13px; font-weight: 600; }
.ai-preview-head { margin-bottom: 8px; }
.ai-preview-foot { margin-top: 10px; }
.ai-preview .spacer { flex: 1; }
.ai-preview .hint { font-size: 12px; color: var(--muted); font-weight: 400; }
.ai-json {
  max-height: 320px; overflow: auto; background: #0f172a; color: #e2e8f0; border-radius: 8px;
  padding: 10px 12px; font-size: 12px; line-height: 1.6; font-family: Consolas, Menlo, monospace; white-space: pre-wrap; word-break: break-all;
}

/* AI 候选景点列表 */
.ai-result { margin-top: 14px; }
.ai-result-head { display: flex; align-items: center; gap: 10px; font-size: 13px; font-weight: 600; margin-bottom: 10px; }
.ai-result-head .spacer { flex: 1; }
.check-all { font-size: 12px; color: var(--muted); display: inline-flex; align-items: center; gap: 5px; cursor: pointer; }
.ai-spot-list { display: flex; flex-direction: column; gap: 8px; max-height: 380px; overflow-y: auto; }
.ai-spot {
  display: flex; gap: 10px; align-items: flex-start; padding: 10px 12px; border: 1px solid var(--border);
  border-radius: 10px; cursor: pointer; transition: border-color .15s, background .15s;
}
.ai-spot:hover { border-color: var(--primary); }
.ai-spot.on { border-color: var(--primary); background: var(--primary-light); }
.ai-spot input { margin-top: 3px; accent-color: var(--primary); }
.ai-spot-body { flex: 1; min-width: 0; }
.ai-spot-name { font-size: 13px; font-weight: 700; }
.ai-spot-meta { font-size: 11px; color: var(--muted); font-weight: 400; margin-left: 6px; }
.ai-spot-tags { margin-top: 4px; }
.ai-spot-reason { font-size: 12px; color: #555; line-height: 1.6; margin-top: 4px; word-break: break-all; }

/* 重建 */
.run-head { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
.run-head .spacer { flex: 1; }
.state { font-size: 12px; padding: 3px 10px; border-radius: 14px; font-weight: 600; }
.s-running { background: #fff7e6; color: #d97706; }
.s-success { background: #e8f7ef; color: var(--success); }
.s-failed { background: #fdecec; color: var(--danger); }
.s-idle { background: #f5f6fa; color: var(--muted); }
.run-detail { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 8px 20px; }
.kv { font-size: 13px; color: var(--muted); display: flex; justify-content: space-between; gap: 12px; border-bottom: 1px dashed var(--border); padding: 6px 0; }
.kv b { color: var(--text); font-weight: 600; }
.err-box { grid-column: 1 / -1; background: #fdecec; color: var(--danger); border-radius: 8px; padding: 8px 12px; font-size: 12px; margin-top: 4px; }

/* 删除地点弹框 */
.del-city-hint { font-size: 12px; color: var(--muted); margin-bottom: 10px; }
.del-city-list { display: flex; flex-direction: column; gap: 6px; max-height: 320px; overflow-y: auto; }
.del-city { display: flex; align-items: center; gap: 8px; padding: 7px 10px; border: 1px solid var(--border); border-radius: 8px; font-size: 13px; cursor: pointer; }
.del-city:hover { border-color: var(--danger); }
.del-city input { accent-color: var(--danger); }
.dc-name { font-weight: 600; flex: 1; }
.dc-count { font-size: 11px; color: var(--muted); font-weight: 400; margin-left: 8px; }

.empty { font-size: 13px; color: var(--muted); padding: 14px 0; text-align: center; }
</style>
