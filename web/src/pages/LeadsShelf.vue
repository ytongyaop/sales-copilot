<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import {
  claimLead,
  createLead,
  getDashboard,
  importLeads,
  listLeads,
  listPool,
  listShelf,
  listTemplates,
  releaseLead,
} from '../api'
import {
  SOURCES,
  formatPercent,
  formatRate,
  formatTime,
  gradeBadge,
  gradeClass,
  sourceLabel,
} from '../labels'

// 客户货架：分组、搜索、公海筛选都只在这里做**分箱与过滤**，
// 档位与覆盖度全部来自 `GET /leads/shelf`（后端只读接口），浏览器里不重算任何业务口径。

// 四个分组：按「最近一次分级」分箱；没有分级（unknown / needs_info）一律进「待分级」。
const GROUPS = [
  { key: 'A', label: 'A 类' },
  { key: 'B', label: 'B 类' },
  { key: 'C', label: 'C 类' },
  { key: 'pending', label: '待分级' },
]

const shelf = ref([])
const leads = ref([])
const pool = ref([])
const dashboard = ref(null)
const templates = ref([])

const keyword = ref('')
const scope = ref('all') // all | pool
const loading = ref(false)
const error = ref('')

// 线索管理（新增 / 导入 / 认领 / 释放）：都是 v1 就有的入口，折叠到页面下方。
const form = ref({ name: '', contact: '', source: 'referral', owner: '', template: 'edu_training' })
const submitting = ref(false)
const importing = ref(false)
const importResult = ref(null)
const importError = ref('')
const busyId = ref('')

const emit = defineEmits(['open-lead'])

const poolIds = computed(() => new Set(pool.value.map((item) => item.id)))

const visibleCards = computed(() => {
  let items = shelf.value
  if (scope.value === 'pool') items = items.filter((card) => poolIds.value.has(card.lead_id))
  const query = keyword.value.trim().toLowerCase()
  if (query) {
    items = items.filter(
      (card) =>
        card.name.toLowerCase().includes(query) ||
        (card.owner || '').toLowerCase().includes(query),
    )
  }
  return items
})

// 分组顺序固定 A → B → C → 待分级；空组也保留标题（人一眼看出「A 类确实一家都没有」）。
const grouped = computed(() =>
  GROUPS.map((group) => ({
    ...group,
    items: visibleCards.value.filter((card) => groupOf(card.grade) === group.key),
  })),
)

function groupOf(grade) {
  return grade === 'A' || grade === 'B' || grade === 'C' ? grade : 'pending'
}

function openCard(card) {
  emit('open-lead', card.lead_id)
}

function templateOf(lead) {
  return templates.value.find((item) => item.key === lead.template)
}

// 阶段中文名优先用模板给的 labels（行业叫法只在模板里），没有就退回块 id。
function stageLabel(lead) {
  const template = templateOf(lead)
  return (template && template.labels && template.labels[lead.stage]) || lead.stage
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [cards, leadItems, poolItems, dashboardBody] = await Promise.all([
      listShelf(),
      listLeads(),
      listPool(),
      getDashboard(),
    ])
    shelf.value = cards
    leads.value = leadItems
    pool.value = poolItems
    dashboard.value = dashboardBody
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

async function loadTemplates() {
  try {
    templates.value = await listTemplates()
  } catch (err) {
    error.value = err.message
  }
}

// -------------------------------------------------------------- 线索管理（v1 入口）

async function submit() {
  submitting.value = true
  error.value = ''
  try {
    await createLead({
      name: form.value.name,
      contact: form.value.contact,
      source: form.value.source,
      owner: form.value.owner.trim() === '' ? null : form.value.owner,
      template: form.value.template,
    })
    form.value = { ...form.value, name: '', contact: '', owner: '' }
    await load()
  } catch (err) {
    // 校验规则在后端：422 的中文提示原样显示，前端不再判一次。
    error.value = err.message
  } finally {
    submitting.value = false
  }
}

function readFileText(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(new Error('读取文件失败，请确认这是一个文本 CSV 文件'))
    reader.readAsText(file, 'utf-8')
  })
}

async function onImportFile(event) {
  const input = event.target
  const file = input.files && input.files[0]
  if (!file) return

  importError.value = ''
  importResult.value = null
  importing.value = true
  try {
    const text = await readFileText(file)
    importResult.value = await importLeads(text, null)
    await load()
  } catch (err) {
    importError.value = err.message || '导入失败，请重试'
  } finally {
    importing.value = false
    input.value = ''
  }
}

async function claim(lead) {
  const owner = window.prompt(`填写「${lead.name}」的负责人`, '')
  if (owner === null) return
  if (owner.trim() === '') {
    error.value = '归属人不能为空'
    return
  }
  busyId.value = lead.id
  error.value = ''
  try {
    await claimLead(lead.id, owner.trim())
    await load()
  } catch (err) {
    error.value = err.message
  } finally {
    busyId.value = ''
  }
}

async function release(lead) {
  busyId.value = lead.id
  error.value = ''
  try {
    await releaseLead(lead.id)
    await load()
  } catch (err) {
    error.value = err.message
  } finally {
    busyId.value = ''
  }
}

// 页签切回「客户」时重取：分拣台接受、详情页改判都会改这里的档位与覆盖度。
const props = defineProps({ active: { type: Boolean, default: false } })

watch(
  () => props.active,
  async (on) => {
    if (!on) return
    await load()
  },
)

onMounted(async () => {
  await loadTemplates()
  await load()
})
</script>

<template>
  <div class="shelf">
    <section class="card">
      <h2>仪表盘</h2>
      <p v-if="!dashboard" class="hint">仪表盘加载中…</p>
      <div v-else class="stats">
        <div class="stat">
          <span class="stat-value">{{ dashboard.total }}</span>
          <span class="stat-label">客户总数</span>
        </div>
        <div class="stat">
          <span class="stat-value">{{ dashboard.pool }}</span>
          <span class="stat-label">公海</span>
        </div>
        <div class="stat">
          <span class="stat-value">{{ dashboard.new_this_week }}</span>
          <span class="stat-label">本周新增</span>
        </div>
        <div class="stat">
          <span class="stat-value">{{ dashboard.followups_this_week }}</span>
          <span class="stat-label">本周跟进</span>
        </div>
        <div class="stat small">
          <span class="stat-value">{{ formatRate(dashboard.first_response_24h_rate) }}</span>
          <span class="stat-label">24h 首联率</span>
        </div>
        <div class="stat small">
          <span class="stat-value">{{ formatRate(dashboard.stale_ratio) }}</span>
          <span class="stat-label">无下次跟进占比</span>
        </div>
      </div>
      <div v-if="dashboard" class="reminder-counts">
        <span class="risk"><span class="risk-dot red"></span>红 {{ dashboard.reminders_by_level.red }}</span>
        <span class="risk"><span class="risk-dot yellow"></span>黄 {{ dashboard.reminders_by_level.yellow }}</span>
        <span class="risk"><span class="risk-dot gray"></span>灰 {{ dashboard.reminders_by_level.gray }}</span>
        <span class="muted">久未新增跟进 {{ dashboard.silent_count }} 条</span>
      </div>
    </section>

    <p v-if="error" class="error">{{ error }}</p>

    <section class="card">
      <div class="shelf-head">
        <h2>客户货架 <span class="muted">（{{ visibleCards.length }} 家）</span></h2>
        <div class="filters">
          <button
            type="button"
            class="chip"
            :class="{ active: scope === 'all' }"
            @click="scope = 'all'"
          >
            全部
          </button>
          <button
            type="button"
            class="chip"
            :class="{ active: scope === 'pool' }"
            @click="scope = 'pool'"
          >
            公海 {{ pool.length }}
          </button>
          <input v-model="keyword" type="search" placeholder="按名称 / 归属人过滤" />
        </div>
      </div>

      <p v-if="loading" class="hint">加载中…</p>
      <p v-else-if="shelf.length === 0" class="hint">
        还没有客户：在页面下方「线索管理」里新增一条，或到分拣台投一段料。
      </p>
      <p v-else-if="visibleCards.length === 0" class="hint">当前筛选下没有客户。</p>

      <div v-for="group in grouped" :key="group.key" class="group">
        <h3 class="group-title">
          {{ group.label }} <span class="muted">{{ group.items.length }}</span>
        </h3>
        <p v-if="group.items.length === 0" class="hint">这一组还没有客户。</p>
        <div v-else class="lead-grid">
          <article
            v-for="card in group.items"
            :key="card.lead_id"
            class="lead-card"
            title="打开客户详情"
            @click="openCard(card)"
          >
            <div class="lead-card-head">
              <strong>{{ card.name }}</strong>
              <span class="grade-badge" :class="gradeClass(card.grade)">
                {{ gradeBadge(card.grade) }}
              </span>
            </div>
            <p class="muted owner">{{ card.owner || '未分配（公海）' }}</p>
            <div class="bar" :title="`信息卡覆盖 ${card.filled}/${card.total}`">
              <span class="bar-fill" :style="{ width: formatPercent(card.coverage) }"></span>
            </div>
            <p class="meta">
              覆盖 {{ formatPercent(card.coverage) }}（{{ card.filled }}/{{ card.total }}）· 缺
              {{ card.missing_count }} 项
            </p>
            <p v-if="card.next_appointment" class="meta">下次约定 {{ card.next_appointment }}</p>
            <p v-else class="meta muted">还没约定下次</p>
          </article>
        </div>
      </div>
    </section>

    <details class="card manage">
      <summary><h2 class="inline-title">线索管理（新增 / 导入 / 认领 / 释放）</h2></summary>

      <h3>新增线索</h3>
      <form class="form" @submit.prevent="submit">
        <label>
          模板
          <select v-model="form.template">
            <option v-for="item in templates" :key="item.key" :value="item.key">
              {{ item.name }}
            </option>
          </select>
        </label>
        <label>
          名称
          <input v-model="form.name" type="text" placeholder="填写客户名称" />
        </label>
        <label>
          联系方式
          <input v-model="form.contact" type="text" placeholder="手机号 / 微信号，至少 5 个字符" />
        </label>
        <label>
          来源
          <select v-model="form.source">
            <option v-for="item in SOURCES" :key="item.value" :value="item.value">
              {{ item.label }}
            </option>
          </select>
        </label>
        <label>
          归属人
          <input v-model="form.owner" type="text" placeholder="可留空" />
        </label>
        <button type="submit" :disabled="submitting">
          {{ submitting ? '提交中…' : '新增' }}
        </button>
      </form>

      <h3>导入 CSV</h3>
      <p class="hint">
        支持带表头（姓名 / 联系方式 / 来源 / 归属），也支持无表头（列序：姓名,联系方式,来源,归属）；
        同一联系方式只会导入一次（去重在后端）。
      </p>
      <div class="row-actions">
        <input type="file" accept=".csv,text/csv" :disabled="importing" @change="onImportFile" />
        <span v-if="importing" class="muted">导入中…</span>
      </div>
      <p v-if="importError" class="error">{{ importError }}</p>
      <div v-if="importResult" class="import-result">
        <p>导入 {{ importResult.imported }} 条，跳过 {{ importResult.skipped.length }} 条。</p>
        <details v-if="importResult.skipped.length">
          <summary class="muted">查看跳过原因</summary>
          <ul class="skip-list">
            <li v-for="(item, index) in importResult.skipped" :key="index" class="muted">
              第 {{ item.row }} 行：{{ item.reason }}
            </li>
          </ul>
        </details>
      </div>

      <h3>归属（认领 / 释放）</h3>
      <p v-if="leads.length === 0" class="hint">还没有客户。</p>
      <table v-else>
        <thead>
          <tr>
            <th>名称</th>
            <th>归属</th>
            <th>来源</th>
            <th>阶段</th>
            <th>创建时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="lead in leads" :key="lead.id">
            <td>{{ lead.name }}</td>
            <td>{{ lead.owner || '未分配' }}</td>
            <td>{{ sourceLabel(lead.source) }}</td>
            <td>{{ stageLabel(lead) }}</td>
            <td>{{ formatTime(lead.created_at) }}</td>
            <td>
              <button
                v-if="!lead.owner"
                type="button"
                :disabled="busyId === lead.id"
                @click="claim(lead)"
              >
                认领
              </button>
              <button
                v-else
                type="button"
                class="ghost"
                :disabled="busyId === lead.id"
                title="放回公海，其他人可以再认领"
                @click="release(lead)"
              >
                释放
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </details>
  </div>
</template>

<style scoped>
.shelf-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  flex-wrap: wrap;
  justify-content: space-between;
  margin-bottom: 8px;
}

.shelf-head h2 {
  margin: 0;
}

.filters {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}

.filters input {
  padding: 6px 10px;
  border: 1px solid #d0d7de;
  border-radius: 6px;
  font-size: 13px;
  min-width: 200px;
}

.group {
  margin-top: 14px;
}

.group-title {
  margin: 0 0 8px;
  font-size: 13px;
  color: #57606a;
  font-weight: 600;
  border-left: 3px solid #d0d7de;
  padding-left: 8px;
}

.lead-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 10px;
}

.lead-card {
  padding: 10px 12px;
  border: 1px solid #e1e4e8;
  border-radius: 8px;
  background: #fff;
  cursor: pointer;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

.lead-card:hover {
  border-color: #c8d8f5;
  box-shadow: 0 1px 4px rgba(31, 111, 235, 0.12);
}

.lead-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.owner {
  margin: 2px 0 8px;
  font-size: 12px;
}

/* 覆盖度比例条：底色浅、填充主色，缺得多就明显短一截。 */
.bar {
  height: 6px;
  border-radius: 3px;
  background: #eef1f4;
  overflow: hidden;
}

.bar-fill {
  display: block;
  height: 100%;
  background: #1f6feb;
  border-radius: 3px;
}

.meta {
  margin: 4px 0 0;
  font-size: 12px;
  color: #57606a;
}

.manage summary {
  cursor: pointer;
}

.manage summary h2 {
  display: inline;
}

.inline-title {
  font-size: 16px;
}

.manage h3 {
  margin: 16px 0 8px;
  font-size: 14px;
  color: #57606a;
}

.form {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
  align-items: end;
}

.form label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  color: #57606a;
}

.form input,
.form select {
  padding: 8px 10px;
  border: 1px solid #d0d7de;
  border-radius: 6px;
  font-size: 14px;
  color: #1f2328;
  background: #fff;
}

.stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
  gap: 10px;
}

.stat {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px 12px;
  border: 1px solid #e1e4e8;
  border-radius: 6px;
  background: #fafbfc;
}

.stat.small {
  background: #fff;
}

.stat-value {
  font-size: 20px;
  font-weight: 600;
}

.stat.small .stat-value {
  font-size: 16px;
}

.stat-label {
  font-size: 12px;
  color: #57606a;
}

.reminder-counts {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  align-items: center;
  margin-top: 12px;
  font-size: 13px;
}

.risk {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}

.risk-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.risk-dot.red {
  background: #cf222e;
}

.risk-dot.yellow {
  background: #d4a72c;
}

.risk-dot.gray {
  background: #8a939d;
}

.import-result {
  margin-top: 10px;
  padding: 10px 12px;
  border: 1px solid #e1e4e8;
  border-radius: 6px;
  background: #fafbfc;
  font-size: 14px;
}

.skip-list {
  margin: 6px 0 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.7;
}
</style>
