<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import {
  claimBatch,
  confirmBatch,
  getBatch,
  listBatches,
  listLeads,
  listTodo,
  reviewBatch,
  sortContent,
} from '../api'
import { gradeBadge, gradeClass } from '../labels'

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
const panel = ref({ id: '', mode: '' })
const reviewInfo = ref({})
const claimPick = ref({})
const createPick = ref({})

const suggestion = computed(() => (card.value ? card.value.suggestion : null))
const confidenceText = computed(() =>
  card.value ? `${Number(card.value.confidence).toFixed(1)} · ${card.value.scorer}` : '',
)
const skippedText = computed(() => {
  const skipped = (cardResult.value && cardResult.value.skipped) || []
  return skipped.map((item) => `${fieldLabel(item.field)}：${item.reason}`).join('；')
})

function statusLabel(status) {
  return STATUS_LABELS[status] || status
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
    leads.value = await listLeads()
  } catch (err) {
    error.value = err.message
  }
}

// 待办取不到只影响这一块（不把投料与分拣单一起拖成白屏）。
async function loadTodo() {
  try {
    todo.value = await listTodo()
    todoError.value = ''
  } catch (err) {
    todo.value = []
    todoError.value = err.message
  }
}

async function loadLists() {
  try {
    const [poolItems, allItems] = await Promise.all([listBatches('pending_claim'), listBatches()])
    pool.value = poolItems
    recent.value = allItems.slice(0, 10)
  } catch (err) {
    error.value = err.message
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
      <h2>今日待办 <span class="muted">（{{ todo.length }} 家）</span></h2>
      <p v-if="todoError" class="error">{{ todoError }}</p>
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
        <strong>{{ card.lead_name || '未认出' }}</strong>
        <span class="muted">置信度 {{ confidenceText }}</span>
      </div>

      <div v-if="card.content" class="raw">
        <h3>投料原文</h3>
        <p class="raw-text">{{ card.content }}</p>
      </div>

      <div class="judge">
        <section class="block">
          <h3>归属</h3>
          <p v-if="card.lead_name">命中客户：{{ card.lead_name }}</p>
          <p v-else class="muted">没有确定归属：系统没有把握，先到下面「待认领池」认领</p>
          <template v-if="card.lead_candidates && card.lead_candidates.length">
            <p class="muted">归属候选：</p>
            <ul class="candidates">
              <li v-for="item in card.lead_candidates" :key="item.lead_id">
                {{ item.name }}
                <span class="muted">{{ Number(item.score).toFixed(1) }} · {{ item.scorer }}</span>
              </li>
            </ul>
          </template>
          <p v-else class="muted">没有候选</p>
        </section>

        <section class="block">
          <h3>要素</h3>
          <ul v-if="suggestion && suggestion.elements.length" class="elements">
            <li v-for="(element, index) in suggestion.elements" :key="index">
              <div>
                {{ sectionLabel(element.card_section) }} · {{ fieldLabel(element.field) }}：
                <strong>{{ element.value }}</strong>
              </div>
              <span class="quote">「{{ element.source_span }}」</span>
            </li>
          </ul>
          <p v-else class="muted">没有一个要素能对上原文</p>
        </section>

        <section class="block">
          <h3>分级</h3>
          <div v-if="suggestion" class="grading">
            <span class="grade" :class="`grade-${(suggestion.grading.value || '').toLowerCase()}`">
              {{ suggestion.grading.value || '—' }}
            </span>
            <div>
              <p>{{ suggestion.grading.reason }}</p>
              <ul class="basis">
                <li v-for="(item, index) in suggestion.grading.basis" :key="index" class="quote">
                  「{{ item }}」
                </li>
              </ul>
            </div>
          </div>
          <p v-else class="muted">分级读不出来</p>
        </section>

        <section class="block">
          <h3>下一步</h3>
          <p v-if="suggestion"><strong>{{ suggestion.next_action.action }}</strong></p>
          <p v-if="suggestion && suggestion.next_action.value_offered" class="muted">
            提供的价值：{{ suggestion.next_action.value_offered }}
          </p>
        </section>
      </div>

      <details v-if="card.dropped && card.dropped.length" class="dropped">
        <summary>丢弃诊断（{{ card.dropped.length }} 条）</summary>
        <ul>
          <li v-for="(item, index) in card.dropped" :key="index" class="muted">
            第 {{ item.index === null || item.index === undefined ? '?' : item.index + 1 }} 条 ·
            {{ item.reason }}
          </li>
        </ul>
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
      <h2>待认领池 <span class="muted">（{{ pool.length }} 条）</span></h2>
      <p v-if="pool.length === 0" class="hint">待认领池是空的。</p>
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
      <table v-if="recent.length">
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

.judge {
  border-top: 1px solid #eef1f4;
}

/* 四个判断分块：用浅色横线隔开，读的时候一眼知道「这是另一块判断」。 */
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

.elements,
.basis {
  margin: 0;
  padding-left: 18px;
  font-size: 14px;
  line-height: 1.9;
}

.elements li {
  margin-bottom: 4px;
}

.basis {
  padding-left: 0;
  list-style: none;
}

.quote {
  color: #8a939d;
  font-size: 13px;
}

.grading {
  display: flex;
  gap: 12px;
  align-items: flex-start;
}

.grade {
  flex: none;
  width: 44px;
  height: 44px;
  line-height: 44px;
  border-radius: 8px;
  text-align: center;
  font-size: 22px;
  font-weight: 700;
  background: #eef1f4;
  color: #3d4653;
}

.grade-a {
  background: #e7f6ec;
  color: #17663a;
}

.grade-b {
  background: #fff4d6;
  color: #7a5b00;
}

.grade-c {
  background: #eef1f4;
  color: #57606a;
}

.dropped {
  margin-top: 10px;
  font-size: 13px;
}

.dropped summary {
  cursor: pointer;
  color: #57606a;
}

.dropped ul {
  margin: 6px 0 0;
  padding-left: 18px;
  line-height: 1.7;
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

/* 投料原文：保留换行，读的时候不把几段话糊成一行。 */
.raw {
  padding: 10px 0;
  border-bottom: 1px solid #eef1f4;
}

.raw h3 {
  margin: 0 0 6px;
  font-size: 13px;
  color: #57606a;
  font-weight: 600;
}

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
