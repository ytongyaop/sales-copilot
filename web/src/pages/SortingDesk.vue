<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import {
  claimBatch,
  confirmBatch,
  getBatch,
  listBatches,
  listLeads,
  listTodo,
  retryOnce,
  reviewBatch,
  sortContent,
} from '../api'
import { gradeBadge, gradeClass, scorerLabel } from '../labels'

// 分拣台只做四件事：看今日待办、投料、看这张单、处置。四块判断（归属 / 要素 / 分级 / 出库）
// 全在后端 domain/sorting.py；这里只翻译中文、显示后端给的原因，不复制任何规则。

// 状态 → 中文徽标：取值就是后端 SORTING_STATUSES 那四个，前端只负责翻译与配色。
const STATUS_LABELS = {
  pending_review: '待复核',
  pending_claim: '待认领',
  accepted: '已接受',
  rejected: '已拒绝',
}

// 四要素块的中文名（后端 CARD_SECTIONS 的显示翻译）。
const SECTION_LABELS = {
  basic: '基础信息',
  needs: '需求与痛点',
  decision: '决策流程与预算',
  followup: '跟进记录',
}

// 字段中文名：只影响显示；不认识的字段原样显示字段名，不猜它想表达什么。
const FIELD_LABELS = {
  company: '公司',
  name: '姓名',
  contact: '联系方式',
  channel: '沟通渠道',
  need: '核心需求',
  pain_point: '痛点',
  budget: '预算',
  amount: '金额',
  decision_maker: '决策人',
  approval_flow: '审批流程',
  objection: '异议',
  appointment_at: '约定时间',
  next_followup_at: '下次跟进时间',
}

const EMPTY_TEXT = '还没有分拣单，先把一段客户信息丢进上面的框里'

const leads = ref([])

// 今日待办：顺序与「为什么现在跟」「今天做什么」「给什么」全来自后端 /todo，前端只负责显示。
const todo = ref([])
const todoError = ref('')
const todoLoading = ref(false)
// 是否已经拿到过第一次结果：没有它，首帧会把「还没加载」画成「今天没有必须跟的客户」。
const todoLoaded = ref(false)

// 投料区
const content = ref('')
const targetLeadId = ref('')
const busy = ref('')
const error = ref('')
const notice = ref('')

// 当前这张分拣单（投料后出现；处置后原地更新，不整页刷新）
const card = ref(null)
const cardResult = ref(null)
const fixPanel = ref(false)
const fixLeadId = ref('')
const rejectPanel = ref(false)
const rejectReason = ref('')

// 待认领池与最近分拣单：每次处置完只重新拉这两个列表。
const pool = ref([])
const recent = ref([])
// 同理：列表没拿到之前不能报「池子空了 / 还没有分拣单」。
const listsLoaded = ref(false)
const panel = ref({ id: '', mode: '' })
const reviewInfo = ref({})
const claimPick = ref({})
const createPick = ref({})

const suggestion = computed(() => (card.value ? card.value.suggestion : null))
// 四行结论只读这几份派生值：要素最多露 3 条、分级只取档位与一句话理由，
// 四维分解 / 分数区间 / 引用原文一律留在「查看判断依据」里展开（信息不删，只降噪）。
const grading = computed(() => (suggestion.value ? suggestion.value.grading : null))
const elements = computed(() => (suggestion.value && suggestion.value.elements) || [])
const elementCount = computed(() => elements.value.length)
const topElements = computed(() => elements.value.slice(0, 3))
const candidates = computed(() => (card.value && card.value.lead_candidates) || [])
// 归属那一行的小字：分数 + 打分来源（如「100 · 精确命中」）。分数不换算，只把 scorer 翻成中文。
const matchText = computed(() => {
  if (!card.value) return ''
  const score = Number(card.value.confidence)
  const text = Number.isFinite(score) ? String(Number(score.toFixed(1))) : '—'
  return `${text} · ${scorerLabel(card.value.scorer)}`
})
const skippedText = computed(() => {
  const skipped = (cardResult.value && cardResult.value.skipped) || []
  return skipped.map((item) => `${fieldLabel(item.field)}：${item.reason}`).join('；')
})

function statusLabel(status) {
  return STATUS_LABELS[status] || status
}

// 「给什么」由模板规则决定；模型写的那份只在**与规则值不同**时作灰字附注，不参与任何判断。
function modelOfferNote(item) {
  const next = (item && item.next_action) || {}
  const model = (next.model_value_offered || '').trim()
  if (!model) return false
  return model !== (next.value_offered || '').trim()
}

// 待办行点一下 → 抛给外层打开客户详情。
function openTodo(item) {
  emit('open-lead', item.lead_id)
}

function sectionLabel(section) {
  return SECTION_LABELS[section] || section
}

function fieldLabel(field) {
  return FIELD_LABELS[field] || field
}

function leadName(leadId) {
  const hit = leads.value.find((item) => item.id === leadId)
  return hit ? hit.name : leadId
}

function formatTime(iso) {
  if (!iso) return '-'
  return iso.replace('T', ' ').slice(0, 16)
}

// 待认领池的摘要：列表接口已经带 `content_preview`（原文前 60 字）；
// 老接口/脏数据没给就如实说明不可用，不拿别的字段凑一段假的「原文」。
function preview(item) {
  if (item.content_preview) return item.content_preview
  return '原文摘要暂不可用'
}

// -------------------------------------------------------------- URL 回放

// 刷新后回放靠 `?batch=<id>`：只读写查询串，不引路由（前端没有路由，页面切换靠 props）。
function batchIdFromUrl() {
  return new URLSearchParams(window.location.search).get('batch')
}

function setUrlBatch(batchId) {
  const url = new URL(window.location.href)
  if (batchId) url.searchParams.set('batch', batchId)
  else url.searchParams.delete('batch')
  window.history.replaceState({}, '', url)
}

async function loadLeads() {
  try {
    leads.value = await retryOnce(listLeads)
  } catch (err) {
    error.value = err.message
  }
}

// 待办取不到只影响这一块（不把投料与分拣单一起拖成白屏）。
// 失败会自动重试一次（间隔 500ms，见 `api.js` 的 `retryOnce`）：首屏那一次请求最容易
// 撞上后端还没就绪，本轮重试后仍失败才把错误条显示出来。
async function loadTodo() {
  todoLoading.value = true
  try {
    todo.value = await retryOnce(listTodo)
    todoError.value = ''
  } catch (err) {
    todo.value = []
    todoError.value = err.message
  } finally {
    todoLoading.value = false
    todoLoaded.value = true
  }
}

async function loadLists() {
  try {
    const [poolItems, allItems] = await retryOnce(() =>
      Promise.all([listBatches('pending_claim'), listBatches()]),
    )
    pool.value = poolItems
    recent.value = allItems.slice(0, 10)
  } catch (err) {
    error.value = err.message
  } finally {
    listsLoaded.value = true
  }
}

// -------------------------------------------------------------- 投料

async function submit() {
  busy.value = 'submit'
  error.value = ''
  notice.value = ''
  cardResult.value = null
  try {
    const created = await sortContent({
      content: content.value,
      source: 'paste',
      leadId: targetLeadId.value || null,
    })
    // 投料响应不带客户名：再取一次单条视图，卡片与列表用同一份口径。
    card.value = await getBatch(created.batch_id)
    cardResult.value = null
    fixPanel.value = false
    rejectPanel.value = false
    // 把这次的单写进 URL：刷新后还能把这张单原样捧回来。
    setUrlBatch(created.batch_id)
    content.value = ''
    await loadLists()
    // 新投的料可能改了归属或分级：待办顺序要跟着变。
    await loadTodo()
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = ''
  }
}

// -------------------------------------------------------------- 处置当前单

async function accept() {
  if (!card.value) return
  busy.value = 'accept'
  error.value = ''
  notice.value = ''
  try {
    const body = await confirmBatch(card.value.id, { action: 'accept' })
    card.value = body.batch
    cardResult.value = { applied: body.applied, skipped: body.skipped || [] }
    await loadLists()
    // 事实写进去了 → 缺项少了 → 待办里的「给什么」与排序都会变。
    await loadTodo()
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = ''
  }
}

function toggleFix() {
  fixPanel.value = !fixPanel.value
  rejectPanel.value = false
  fixLeadId.value = ''
}

async function confirmFix() {
  if (!card.value || !fixLeadId.value) return
  busy.value = 'fix'
  error.value = ''
  notice.value = ''
  try {
    const body = await confirmBatch(card.value.id, { action: 'fix', lead_id: fixLeadId.value })
    card.value = body.batch
    cardResult.value = { applied: body.applied, skipped: body.skipped || [] }
    fixPanel.value = false
    await loadLists()
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = ''
  }
}

function toggleReject() {
  rejectPanel.value = !rejectPanel.value
  fixPanel.value = false
  rejectReason.value = ''
}

async function reject() {
  if (!card.value || rejectReason.value.trim() === '') return
  busy.value = 'reject'
  error.value = ''
  notice.value = ''
  try {
    const body = await confirmBatch(card.value.id, {
      action: 'reject',
      reason: rejectReason.value.trim(),
    })
    card.value = body.batch
    cardResult.value = null
    rejectPanel.value = false
    await loadLists()
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = ''
  }
}

// -------------------------------------------------------------- 待认领池

function togglePanel(id, mode) {
  if (panel.value.id === id && panel.value.mode === mode) {
    panel.value = { id: '', mode: '' }
    return
  }
  panel.value = { id, mode }
}

// 「复核候选」点一下就去问后端：建议只进响应、不落库，人点「采用」才真正认领。
async function openReview(item) {
  panel.value = { id: item.id, mode: 'review' }
  reviewInfo.value = { ...reviewInfo.value, [item.id]: null }
  busy.value = `review:${item.id}`
  error.value = ''
  notice.value = ''
  try {
    const body = await reviewBatch(item.id)
    reviewInfo.value = { ...reviewInfo.value, [item.id]: body }
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = ''
  }
}

async function claimItem(item, payload, label) {
  busy.value = `claim:${item.id}`
  error.value = ''
  notice.value = ''
  try {
    const body = await claimBatch(item.id, payload)
    const warnings = (body.warnings || []).map((warning) => warning.message).join('；')
    notice.value = `${label}，状态改为待复核（还要接受一次才把要素写进事实）${
      warnings ? `｜提醒：${warnings}` : ''
    }`
    if (card.value && card.value.id === item.id) card.value = body.batch
    panel.value = { id: '', mode: '' }
    await loadLists()
    await loadLeads()
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = ''
  }
}

function adoptSuggestion(item) {
  const info = reviewInfo.value[item.id]
  if (!info || !info.suggested_lead_id) return
  return claimItem(item, { lead_id: info.suggested_lead_id }, `已认领到「${leadName(info.suggested_lead_id)}」`)
}

function claimExisting(item) {
  const leadId = claimPick.value[item.id]
  if (!leadId) return
  return claimItem(item, { lead_id: leadId }, `已认领到「${leadName(leadId)}」`)
}

function createLeadAndClaim(item) {
  const name = (createPick.value[item.id] || '').trim()
  if (!name) return
  return claimItem(item, { create_name: name }, `已按「${name}」新建客户并认领`)
}

// -------------------------------------------------------------- 从列表回放一张单

// 最近分拣单的行点一下：取单条视图（带完整原文、归属候选、丢弃诊断）渲染进上方卡片。
async function openBatch(batchId) {
  if (!batchId) return
  busy.value = `open:${batchId}`
  error.value = ''
  notice.value = ''
  try {
    card.value = await getBatch(batchId)
    cardResult.value = null
    fixPanel.value = false
    rejectPanel.value = false
    setUrlBatch(batchId)
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = ''
  }
}

onMounted(async () => {
  await loadLeads()
  await loadLists()
  await loadTodo()
  // URL 带 `?batch=<id>` 就把那张单捧回来；没有就保持空态。
  const initial = batchIdFromUrl()
  if (initial) await openBatch(initial)
})

// 切回分拣台时重取客户与列表：客户可能刚在「客户」页新建/改过，
// 不重取就会出现「下拉里没有它、复核建议只能显示 id」。
const props = defineProps({ active: { type: Boolean, default: false } })
// 待办行点一下 → 让外层切到客户详情（分拣台不认识详情页，只负责把 id 抛上去）。
const emit = defineEmits(['open-lead'])

watch(
  () => props.active,
  async (on) => {
    if (!on) return
    await loadLeads()
    await loadLists()
    await loadTodo()
  },
)
</script>

<template>
  <div class="desk">

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="notice" class="notice">{{ notice }}</p>

    <section class="card">
      <div class="card-head-line">
        <h2>今日待办 <span class="muted">（{{ todoLoaded ? todo.length : '…' }} 家）</span></h2>
        <button
          type="button"
          class="ghost small"
          :disabled="todoLoading"
          title="重新拉一次今日待办"
          @click="loadTodo"
        >
          {{ todoLoading ? '刷新中…' : '刷新' }}
        </button>
      </div>
      <!-- 顺序固定：错误 → 没加载完 → 空 → 列表。少了中间那档，首帧就会撒谎说「今天没有必须跟的客户」。 -->
      <p v-if="todoError" class="error">{{ todoError }}</p>
      <p v-else-if="!todoLoaded" class="hint">加载中…</p>
      <p v-else-if="todo.length === 0" class="hint">今天没有必须跟的客户</p>
      <ul v-else class="todo">
        <li
          v-for="item in todo"
          :key="item.lead_id"
          class="todo-row"
          title="打开客户详情"
          @click="openTodo(item)"
        >
          <span class="grade-badge" :class="gradeClass(item.grade)">{{ gradeBadge(item.grade) }}</span>
          <div class="todo-body">
            <div class="todo-head">
              <strong>{{ item.lead_name }}</strong>
              <span class="muted">{{ item.reason }}</span>
            </div>
            <p class="todo-action">今天：{{ item.action }}</p>
            <p class="muted">给什么：{{ item.value_offered }}</p>
          </div>
          <span class="chevron">›</span>
        </li>
      </ul>
    </section>

    <section class="card">
      <h2>投料</h2>
      <div class="feed">
        <textarea
          v-model="content"
          rows="6"
          placeholder="粘贴通话纪要 / 聊天记录 / 邮件原文"
        ></textarea>
        <div class="feed-row">
          <label class="field">
            归属到已有客户
            <select v-model="targetLeadId">
              <option value="">让系统判断</option>
              <option v-for="lead in leads" :key="lead.id" :value="lead.id">{{ lead.name }}</option>
            </select>
          </label>
          <button type="button" :disabled="busy === 'submit'" @click="submit">
            {{ busy === 'submit' ? '处理中…' : '投进去' }}
          </button>
        </div>
      </div>
    </section>

    <section v-if="card" class="card">
      <div class="card-head">
        <span class="status" :class="card.status">{{ statusLabel(card.status) }}</span>
      </div>

      <!-- 结论优先：面向销售只看这四行就能处置；原文、引文、四维这类判断过程全收进下面的
           「查看判断依据」（默认收起、可展开，留着排查用，不是删除）。 -->
      <div class="conclusion">
        <div class="conclusion-row">
          <span class="row-label">归属</span>
          <div class="row-body">
            <p class="row-main">
              <strong v-if="card.lead_name">{{ card.lead_name }}</strong>
              <span v-else>未认出</span>
              <span class="row-note">{{ matchText }}</span>
            </p>
            <p v-if="!card.lead_name" class="muted row-hint">
              系统没有把握，先到下面「待认领池」认领
            </p>
          </div>
        </div>

        <div class="conclusion-row">
          <span class="row-label">要素</span>
          <div class="row-body">
            <p v-if="elementCount" class="row-main">已补 {{ elementCount }} 项</p>
            <ul v-if="elementCount" class="row-list">
              <li v-for="(element, index) in topElements" :key="index">
                {{ sectionLabel(element.card_section) }}·{{ fieldLabel(element.field) }}：
                <strong>{{ element.value }}</strong>
              </li>
            </ul>
            <p v-if="elementCount > topElements.length" class="muted row-hint">
              还有 {{ elementCount - topElements.length }} 项，展开「查看判断依据」看全部
            </p>
            <p v-if="!elementCount" class="muted">没有一个要素能对上原文</p>
          </div>
        </div>

        <div class="conclusion-row">
          <span class="row-label">分级</span>
          <div class="row-body">
            <p v-if="grading" class="row-main">
              <span class="grade-badge" :class="gradeClass(grading.value)">{{ gradeBadge(grading.value) }}</span>
              <span class="row-reason">{{ grading.reason }}</span>
            </p>
            <p v-else class="muted">分级读不出来</p>
          </div>
        </div>

        <div class="conclusion-row">
          <span class="row-label">下一步</span>
          <div class="row-body">
            <p v-if="suggestion" class="row-main">
              <strong>{{ suggestion.next_action.action }}</strong>
            </p>
            <p v-if="suggestion && suggestion.next_action.value_offered" class="row-note">
              提供的价值：{{ suggestion.next_action.value_offered }}
            </p>
            <p v-if="!suggestion" class="muted">没有出库建议</p>
          </div>
        </div>
      </div>

      <details class="evidence">
        <summary>查看判断依据</summary>

        <section v-if="card.content" class="block">
          <h3>投料原文</h3>
          <p class="raw-text">{{ card.content }}</p>
        </section>

        <section class="block">
          <h3>要素明细（{{ elementCount }} 条）</h3>
          <table v-if="elementCount">
            <thead>
              <tr>
                <th>字段</th>
                <th>值</th>
                <th>来源</th>
                <th>置信度</th>
                <th>原文片段</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(element, index) in elements" :key="index">
                <td>{{ sectionLabel(element.card_section) }}·{{ fieldLabel(element.field) }}</td>
                <td>{{ element.value }}</td>
                <td><span class="badge">模型抽取</span></td>
                <td>
                  {{ Number.isFinite(Number(element.confidence)) ? Number(element.confidence).toFixed(2) : '—' }}
                </td>
                <td class="quote">「{{ element.source_span }}」</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="muted">没有一个要素能对上原文</p>
        </section>

        <section class="block">
          <h3>分级依据</h3>
          <template v-if="grading">
            <p>{{ grading.reason }}</p>
            <ul v-if="grading.basis && grading.basis.length" class="basis">
              <li v-for="(item, index) in grading.basis" :key="index" class="quote">
                「{{ item }}」
              </li>
            </ul>
            <!-- 四维分解与分数区间：详情页 `GET /grading/latest` 有，分拣单接口目前不含这两项；
                 后端补上就自动显示，前端不自己算任何分数。 -->
            <div v-if="grading.dims && grading.dims.length" class="dims">
              <div v-for="dim in grading.dims" :key="dim.key" class="dim">
                <span>{{ dim.label }}</span>
                <span>{{ dim.known ? dim.score.toFixed(1) : '—' }}</span>
              </div>
            </div>
            <p v-if="grading.low !== undefined && grading.high !== undefined" class="muted">
              分数区间 {{ Number(grading.low).toFixed(2) }} – {{ Number(grading.high).toFixed(2) }}
            </p>
          </template>
          <p v-else class="muted">分级读不出来</p>
        </section>

        <section class="block">
          <h3>归属候选（{{ candidates.length }} 条）</h3>
          <ul v-if="candidates.length" class="candidates">
            <li v-for="item in candidates" :key="item.lead_id">
              {{ item.name }}
              <span class="muted">{{ Number(item.score).toFixed(1) }} · {{ scorerLabel(item.scorer) }}</span>
            </li>
          </ul>
          <p v-else class="muted">没有候选</p>
        </section>

        <section v-if="card.dropped && card.dropped.length" class="block">
          <h3>丢弃诊断（{{ card.dropped.length }} 条）</h3>
          <ul>
            <li v-for="(item, index) in card.dropped" :key="index" class="muted">
              第 {{ item.index === null || item.index === undefined ? '?' : item.index + 1 }} 条 ·
              {{ item.reason }}
            </li>
          </ul>
        </section>

        <p v-if="modelOfferNote(suggestion)" class="model-note">
          模型建议的价值：{{ suggestion.next_action.model_value_offered }}（规则值以模板为准）
        </p>
      </details>

      <div class="row-actions card-actions">
        <button
          type="button"
          :disabled="busy !== '' || card.status !== 'pending_review'"
          :title="card.status === 'pending_review' ? '把要素写成这个客户的事实' : '先认领客户，才能接受'"
          @click="accept"
        >
          {{ busy === 'accept' ? '处理中…' : '接受' }}
        </button>
        <button
          type="button"
          class="ghost"
          :disabled="busy !== '' || card.status !== 'pending_review'"
          @click="toggleFix"
        >
          改归属
        </button>
        <button
          type="button"
          class="ghost"
          :disabled="busy !== '' || card.status === 'accepted' || card.status === 'rejected'"
          @click="toggleReject"
        >
          拒绝
        </button>
      </div>

      <div v-if="fixPanel" class="panel-form row-actions">
        <select v-model="fixLeadId">
          <option value="">选择客户</option>
          <option v-for="lead in leads" :key="lead.id" :value="lead.id">{{ lead.name }}</option>
        </select>
        <button type="button" :disabled="!fixLeadId || busy === 'fix'" @click="confirmFix">
          {{ busy === 'fix' ? '处理中…' : '确认改归属并接受' }}
        </button>
      </div>

      <div v-if="rejectPanel" class="panel-form row-actions">
        <input v-model="rejectReason" type="text" placeholder="写一句拒绝理由（必填）" />
        <button
          type="button"
          :disabled="rejectReason.trim() === '' || busy === 'reject'"
          @click="reject"
        >
          {{ busy === 'reject' ? '处理中…' : '确认拒绝' }}
        </button>
      </div>

      <p v-if="cardResult" class="result">
        已写入 {{ cardResult.applied }} 条事实，跳过 {{ cardResult.skipped.length }} 条<template
          v-if="cardResult.skipped.length"
        >（{{ skippedText }}）</template>
      </p>
      <p v-else-if="card.status === 'rejected'" class="result muted">
        这张单已拒绝：要素不会写进任何客户。
      </p>
    </section>

    <section v-else class="card">
      <p class="hint">{{ EMPTY_TEXT }}</p>
    </section>

    <section class="card">
      <h2>待认领池 <span class="muted">（{{ listsLoaded ? pool.length : '…' }} 条）</span></h2>
      <p v-if="!listsLoaded" class="hint">加载中…</p>
      <p v-else-if="pool.length === 0" class="hint">待认领池是空的。</p>
      <ul v-else class="pool">
        <li v-for="item in pool" :key="item.id">
          <div class="pool-head">
            <span class="muted">{{ formatTime(item.created_at) }}</span>
            <span class="pool-text">{{ preview(item) }}</span>
            <span v-if="item.dropped_count" class="badge warn">有丢弃 {{ item.dropped_count }}</span>
          </div>

          <div class="row-actions">
            <button
              type="button"
              class="ghost"
              :disabled="busy !== ''"
              @click="openReview(item)"
            >
              {{ busy === `review:${item.id}` ? '处理中…' : '复核候选' }}
            </button>
            <button type="button" class="ghost" :disabled="busy !== ''" @click="togglePanel(item.id, 'claim')">
              认领到已有客户
            </button>
            <button type="button" class="ghost" :disabled="busy !== ''" @click="togglePanel(item.id, 'create')">
              新建客户
            </button>
          </div>

          <div v-if="panel.id === item.id && panel.mode === 'review'" class="panel-form">
            <p v-if="reviewInfo[item.id]">
              <template v-if="reviewInfo[item.id].suggested_lead_id">
                建议归给「{{ leadName(reviewInfo[item.id].suggested_lead_id) }}」：{{
                  reviewInfo[item.id].reason
                }}
              </template>
              <template v-else>模型没有挑出客户：{{ reviewInfo[item.id].reason }}</template>
            </p>
            <div class="row-actions">
              <button
                v-if="reviewInfo[item.id] && reviewInfo[item.id].suggested_lead_id"
                type="button"
                :disabled="busy === `claim:${item.id}`"
                @click="adoptSuggestion(item)"
              >
                {{ busy === `claim:${item.id}` ? '处理中…' : '采用这个建议' }}
              </button>
            </div>
          </div>

          <div v-if="panel.id === item.id && panel.mode === 'claim'" class="panel-form row-actions">
            <select v-model="claimPick[item.id]">
              <option value="">选择客户</option>
              <option v-for="lead in leads" :key="lead.id" :value="lead.id">{{ lead.name }}</option>
            </select>
            <button
              type="button"
              :disabled="!claimPick[item.id]"
              @click="claimExisting(item)"
            >
              {{ busy === `claim:${item.id}` ? '处理中…' : '认领' }}
            </button>
          </div>

          <div v-if="panel.id === item.id && panel.mode === 'create'" class="panel-form row-actions">
            <input v-model="createPick[item.id]" type="text" placeholder="新客户名称" />
            <button
              type="button"
              :disabled="(createPick[item.id] || '').trim() === ''"
              @click="createLeadAndClaim(item)"
            >
              {{ busy === `claim:${item.id}` ? '处理中…' : '新建并认领' }}
            </button>
          </div>
        </li>
      </ul>
    </section>

    <section class="card">
      <h2>最近分拣单</h2>
      <p v-if="!listsLoaded" class="hint">加载中…</p>
      <table v-else-if="recent.length">
        <thead>
          <tr>
            <th>时间</th>
            <th>状态</th>
            <th>归属</th>
            <th>置信度</th>
            <th>丢弃数</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="item in recent"
            :key="item.id"
            class="clickable"
            :class="{ active: card && card.id === item.id }"
            title="点击把这张单放回上面的卡片"
            @click="openBatch(item.id)"
          >
            <td>{{ formatTime(item.created_at) }}</td>
            <td><span class="status" :class="item.status">{{ statusLabel(item.status) }}</span></td>
            <td>{{ item.lead_name || '未认出' }}</td>
            <td>{{ Number(item.confidence).toFixed(1) }}</td>
            <td>{{ item.dropped_count }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="hint">{{ EMPTY_TEXT }}</p>
    </section>
  </div>
</template>

<style scoped>
.feed {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.feed textarea {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid #d0d7de;
  border-radius: 6px;
  font-family: inherit;
  font-size: 14px;
  color: #1f2328;
  resize: vertical;
}

.feed-row {
  display: flex;
  gap: 12px;
  align-items: flex-end;
  flex-wrap: wrap;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  color: #57606a;
}

.field select {
  padding: 8px 10px;
  border: 1px solid #d0d7de;
  border-radius: 6px;
  font-size: 14px;
  color: #1f2328;
  background: #fff;
}

/* 成功提示走绿色，与红色的错误条分开：两者都只是「刚发生了什么」，不是结论。 */
.notice {
  margin: 12px 0 0;
  padding: 10px 12px;
  border: 1px solid #b7e2c4;
  border-radius: 6px;
  background: #e7f6ec;
  color: #17663a;
  font-size: 14px;
}

.card-head {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
  padding-bottom: 10px;
}

/* 状态徽标配色：待复核=蓝 / 待认领=橙 / 已接受=绿 / 已拒绝=灰。 */
.status {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 10px;
  font-size: 12px;
  border: 1px solid transparent;
}

.status.pending_review {
  background: #e7f0ff;
  border-color: #c8d8f5;
  color: #1f6feb;
}

.status.pending_claim {
  background: #fff3e0;
  border-color: #f0d896;
  color: #b35900;
}

.status.accepted {
  background: #e7f6ec;
  border-color: #b7e2c4;
  color: #17663a;
}

.status.rejected {
  background: #eef1f4;
  border-color: #d6dce3;
  color: #57606a;
}

/* 四行结论：标签固定宽度在左，值在右；尾巴上的小字只做注解，不抢视线。 */
.conclusion {
  border-top: 1px solid #eef1f4;
}

.conclusion-row {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  padding: 10px 0;
  border-bottom: 1px solid #eef1f4;
}

.conclusion-row:last-child {
  border-bottom: 0;
}

.row-label {
  flex: none;
  width: 44px;
  color: #57606a;
  font-size: 13px;
  font-weight: 600;
  line-height: 1.8;
}

.row-body {
  flex: 1;
  min-width: 0;
  font-size: 14px;
}

.row-main {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  margin: 0;
  line-height: 1.8;
}

/* 容器已经有 8px 的 gap，徽标自带的 margin-right 在这里清零，间距仍是 8px 而不是 16px。 */
.row-main > .grade-badge,
.todo-row > .grade-badge {
  margin-right: 0;
}

.row-note {
  margin: 2px 0 0;
  color: #8a939d;
  font-size: 12px;
}

.row-reason {
  flex: 1;
  min-width: 0;
  font-size: 14px;
}

.row-hint {
  margin: 2px 0 0;
  font-size: 12px;
}

.row-list {
  margin: 2px 0 0;
  padding-left: 16px;
  font-size: 13px;
  color: #3d4653;
  line-height: 1.8;
}

/* 判断依据：默认收起；展开后是排查用的全量材料（原文 / 要素明细 / 分级依据 / 候选 / 丢弃）。 */
.evidence {
  margin-top: 4px;
  padding-top: 10px;
  border-top: 1px solid #eef1f4;
}

.evidence > summary {
  cursor: pointer;
  color: #1f6feb;
  font-size: 13px;
}

.dims {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 8px;
  margin: 6px 0;
}

.dim {
  display: flex;
  gap: 8px;
  align-items: baseline;
  padding: 6px 8px;
  border: 1px solid #e1e4e8;
  border-radius: 6px;
  background: #fafbfc;
  font-size: 13px;
}

.block {
  padding: 10px 0;
  border-bottom: 1px solid #eef1f4;
}

.block:last-child {
  border-bottom: 0;
}

.block h3 {
  margin: 0 0 6px;
  font-size: 13px;
  color: #57606a;
  font-weight: 600;
}

.block p {
  margin: 0 0 4px;
}

.basis {
  margin: 0;
  padding-left: 0;
  list-style: none;
  font-size: 14px;
  line-height: 1.9;
}

.quote {
  color: #8a939d;
  font-size: 13px;
}

.model-note {
  color: #a2aab3;
  font-size: 12px;
}

.card-actions {
  margin-top: 12px;
}

.panel-form {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed #e1e4e8;
  font-size: 13px;
}

.panel-form p {
  margin: 0;
}

.result {
  margin: 10px 0 0;
  font-size: 14px;
  color: #17663a;
}

.pool {
  margin: 0;
  padding: 0;
  list-style: none;
}

.pool > li {
  padding: 10px 0;
  border-bottom: 1px solid #eef1f4;
}

.pool > li:last-child {
  border-bottom: 0;
}

.pool-head {
  display: flex;
  gap: 10px;
  align-items: baseline;
  flex-wrap: wrap;
  margin-bottom: 8px;
  font-size: 14px;
}

.pool-text {
  flex: 1;
  min-width: 200px;
}

.badge.warn {
  background: #fff3e0;
  color: #b35900;
}

/* 投料原文：保留换行，读的时候不把几段话糊成一行（只在「查看判断依据」里出现）。 */
.raw-text {
  margin: 0;
  padding: 8px 10px;
  border: 1px solid #e1e4e8;
  border-radius: 6px;
  background: #f6f8fa;
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}

/* 归属候选：名称在前，分数与打分者是注解，不抢视线。 */
.candidates {
  margin: 4px 0 0;
  padding-left: 18px;
  font-size: 14px;
  line-height: 1.8;
}

/* 可回放的分拣单行：给个手型 + 悬停底色，点起来不像「点不动」。 */
tr.clickable {
  cursor: pointer;
}

tr.clickable:hover {
  background: #f6f8fa;
}

tr.clickable.active {
  background: #eef4ff;
}

/* 今日待办标题行：标题在左、刷新按钮贴右，不把按钮挤到标题下面。 */
.card-head-line {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.card-head-line h2 {
  margin: 0;
}

button.small {
  padding: 4px 10px;
  font-size: 13px;
}

/* 今日待办：一行一家，左边档位徽标、中间「为什么跟 / 做什么 / 给什么」、右边箭头。 */
.todo {
  margin: 0;
  padding: 0;
  list-style: none;
}

.todo-row {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  padding: 12px 8px;
  border-bottom: 1px solid #eef1f4;
  border-radius: 6px;
  cursor: pointer;
}

.todo-row:last-child {
  border-bottom: 0;
}

.todo-row:hover {
  background: #f6f8fa;
}

.todo-body {
  flex: 1;
  min-width: 0;
}

.todo-head {
  display: flex;
  gap: 10px;
  align-items: baseline;
  flex-wrap: wrap;
}

.todo-action {
  margin: 4px 0 2px;
  font-size: 14px;
}

.todo-body p {
  margin: 2px 0;
  font-size: 13px;
}

.chevron {
  color: #c4ccd4;
  font-size: 18px;
  line-height: 1;
}
</style>
