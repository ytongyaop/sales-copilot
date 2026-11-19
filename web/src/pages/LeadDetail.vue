<script setup>
import { computed, nextTick, onMounted, ref } from 'vue'
import {
  advanceLead,
  confirmDraft,
  createDraft,
  createFollowup,
  createRaw,
  discardDraft,
  extractRaw,
  getGradingLatest,
  getLeadCard,
  listFacts,
  listFollowups,
  listGradingHistory,
  listLeads,
  listTemplates,
  patchFact,
  patchLeadFields,
  proposeGrading,
  decideGrading,
} from '../api'
import {
  FACT_FIELDS,
  OBJECTIONS,
  factFieldLabel,
  factSourceLabel,
  fieldLabel,
  formatPercent,
  formatTime,
  gradeBadge,
  gradeClass,
  objectionLabel,
  sectionLabel,
} from '../labels'

// 客户详情：一个客户的完整工作面（头部 / 分级 / 信息卡 / 跟进 / 话术）。
// 所有数字与判定都来自后端：档位与四维来自 /grading/latest，覆盖度与缺项来自 /card，
// 改判要不要理由、推进缺什么字段都由后端判——这里只翻译中文、收集输入。

const props = defineProps({ leadId: { type: String, required: true } })
const emit = defineEmits(['back'])

const lead = ref(null)
const card = ref(null)
const latest = ref(null) // {current, record, result}
const history = ref([])
const followups = ref([])
const facts = ref([])
const templates = ref([])

const error = ref('')

// 分级区
const busy = ref('')
const decideForm = ref({ value: '', reason: '' })

// 跟进时间线
const followupForm = ref({
  conclusion: '',
  objection: 'price',
  next_followup_at: '',
  value_provided: '',
})
const followupError = ref('')

// 话术草稿
const draft = ref(null)
const draftIssues = ref([])
const confirmForm = ref({ objection: 'price', next_followup_at: '', value_provided: '' })

// 原文与事实（v1 能力保留：粘贴原文 → 提炼 → 事实表可改）
const rawText = ref('')
const savedRaw = ref(null)
const rawHint = ref('')
const extractInfo = ref(null)
const rawError = ref('')
const factsError = ref('')
const editingField = ref('')
const editValue = ref('')
const newFact = ref({ field: 'need', value: '' })

// 信息卡每个字段的原文片段默认不显示（销售看结论就够）：悬停看提示，点「原文」才在行内展开。
// 键是 `分区.字段`，展开状态各区互不干扰。
const openSpans = ref({})

function spanOpen(sectionKey, fieldKey) {
  return openSpans.value[`${sectionKey}.${fieldKey}`] === true
}

function toggleSpan(sectionKey, fieldKey) {
  const key = `${sectionKey}.${fieldKey}`
  openSpans.value = { ...openSpans.value, [key]: !openSpans.value[key] }
}

// 阶段推进与补字段
const fieldFormOpen = ref(false)
const fieldFormValues = ref({})
const fieldError = ref('')

const coverText = computed(() => (card.value ? formatPercent(card.value.coverage) : '—'))

const cardTotals = computed(() => {
  const sections = (card.value && card.value.sections) || []
  return {
    filled: sections.reduce((sum, section) => sum + section.filled, 0),
    total: sections.reduce((sum, section) => sum + section.total, 0),
  }
})

// 卡片顶部那一行汇总：共缺几项（数字来自后端 `missing`，前端不自己数）。
const missingCount = computed(() => (card.value && card.value.missing.length) || 0)

// ---------------------------------------------------------------- 信息卡填写态
// 一次只开一块：点标题或任一缺项就把这一块切成表单，其余分区保持只读。
// 保存走的后端同一套 PATCH（逐字段），前端只负责「谁变了、变了几个」。
const editingSection = ref('')
const sectionForm = ref({})
const sectionSaving = ref('')
const sectionNotice = ref('')
const sectionError = ref('')

function sectionMissing(section) {
  return Math.max(section.total - section.filled, 0)
}

function isComplete(section) {
  return section.total > 0 && section.filled >= section.total
}

function sectionEditing(key) {
  return editingSection.value === key
}

function startSection(section) {
  if (sectionEditing(section.key)) return
  const values = {}
  for (const field of section.fields) values[field.key] = field.value || ''
  sectionForm.value = values
  sectionNotice.value = ''
  sectionError.value = ''
  editingSection.value = section.key
}

// 「完成」与 Esc 都走这里：退出填写态、恢复只读（没保存的输入跟着丢掉）。
function cancelSection() {
  editingSection.value = ''
  sectionForm.value = {}
  sectionNotice.value = ''
  sectionError.value = ''
}

async function saveSection(section) {
  const pending = []
  for (const field of section.fields) {
    const next = String(sectionForm.value[field.key] || '').trim()
    // 空着 = 这次不填（后端也不接受空值）；没改就不发，省一次往返。
    if (!next) continue
    if (next === String(field.value || '').trim()) continue
    pending.push([field.key, next])
  }
  if (pending.length === 0) {
    sectionNotice.value = ''
    sectionError.value = '这一块没有新的内容可保存'
    return
  }

  sectionSaving.value = section.key
  sectionError.value = ''
  let saved = 0
  try {
    for (const [field, value] of pending) {
      await patchFact(props.leadId, field, value)
      saved += 1
    }
    await refreshCard()
    sectionNotice.value = `已保存 ${saved} 项`
  } catch (err) {
    sectionError.value = saved > 0 ? `已保存 ${saved} 项，之后失败：${err.message}` : err.message
  } finally {
    sectionSaving.value = ''
  }
}

// 顶部「共缺 N 项」：跳到第一个缺失分区并展开填写。
async function jumpToFirstMissing() {
  const first = (card.value && card.value.missing[0]) || null
  if (!first) return
  const section = (card.value.sections || []).find((item) => item.key === first.section)
  if (!section) return
  startSection(section)
  await nextTick()
  const el = document.getElementById(`card-section-${first.section}`)
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function currentGrade() {
  return latest.value ? latest.value.current : 'unknown'
}

// 抬头那一句：用后端给的**短理由**（`summary`，不带区间数字）。
// 人工改判写下的原话留在历史表里，抬头只回答「按现在的事实是个什么结论」。
function gradeSummary() {
  if (!latest.value) return ''
  const result = latest.value.result
  if (result && result.summary) return result.summary
  return result ? result.reason : ''
}

// 只刷新「信息卡 / 分级 / 事实」三份：补完一块后覆盖率、圆点与档位都要立刻变，
// 但不该把跟进时间线、分级历史这些无关的东西重取一遍。
async function refreshCard() {
  const [cardBody, latestBody, factRows] = await Promise.all([
    getLeadCard(props.leadId),
    getGradingLatest(props.leadId),
    listFacts(props.leadId),
  ])
  card.value = cardBody
  latest.value = latestBody
  facts.value = factRows
}

const timeline = computed(() => [...followups.value].reverse())

// 阶段中文名与下一阶段：模板是唯一来源（行业叫法只在模板里），前端只负责显示。
const template = computed(() => {
  const key = lead.value ? lead.value.template : ''
  return templates.value.find((item) => item.key === key) || null
})

const nextStage = computed(() => {
  if (!template.value || !lead.value) return ''
  const flow = template.value.flow
  const index = flow.indexOf(lead.value.stage)
  if (index < 0 || index === flow.length - 1) return ''
  return flow[index + 1]
})

const nextStageLabel = computed(() => {
  if (!nextStage.value) return ''
  const labels = (template.value && template.value.labels) || {}
  return labels[nextStage.value] || nextStage.value
})

function stageLabel() {
  if (!lead.value) return '-'
  const labels = (template.value && template.value.labels) || {}
  return labels[lead.value.stage] || lead.value.stage
}

// 线索字段的口径与后端一致：`contact` / `source` 是线索自身属性，其余在 `lead.fields` 里。
function leadFieldValue(field) {
  if (!lead.value) return ''
  const fields = lead.value.fields || {}
  if (Object.prototype.hasOwnProperty.call(fields, field)) return fields[field]
  return lead.value[field] || ''
}

// 推进还缺哪些必填项：块契约来自后端 `GET /templates` 的 `requires`，前端不硬编码。
const missingFields = computed(() => {
  if (!template.value || !nextStage.value) return []
  const requires = (template.value.requires && template.value.requires[nextStage.value]) || []
  return requires.filter((field) => String(leadFieldValue(field)).trim() === '')
})

async function load() {
  busy.value = 'load'
  error.value = ''
  try {
    const [leadList, cardBody, latestBody, historyRows, followupRows, factRows] = await Promise.all([
      listLeads(),
      getLeadCard(props.leadId),
      getGradingLatest(props.leadId),
      listGradingHistory(props.leadId),
      listFollowups(props.leadId),
      listFacts(props.leadId),
    ])
    lead.value = leadList.find((item) => item.id === props.leadId) || null
    card.value = cardBody
    latest.value = latestBody
    history.value = historyRows
    followups.value = followupRows
    facts.value = factRows
    if (['A', 'B', 'C'].includes(latestBody.current)) decideForm.value.value = latestBody.current
    if (!lead.value) error.value = '这条客户不在列表里了（可能已被删改），返回后刷新再看。'
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = ''
  }
}

async function loadTemplates() {
  try {
    templates.value = await listTemplates()
  } catch (err) {
    error.value = err.message
  }
}

async function reloadFollowups() {
  try {
    followups.value = await listFollowups(props.leadId)
    followupError.value = ''
  } catch (err) {
    followupError.value = err.message
  }
}

// -------------------------------------------------------------- 分级

async function repropose() {
  busy.value = 'propose'
  error.value = ''
  try {
    await proposeGrading(props.leadId)
    // 重新评估会写一行留痕：档位、四维与历史都要重取（四维来自只读 latest，不另行计算）。
    await load()
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = ''
  }
}

async function submitDecide() {
  if (!decideForm.value.value) return
  busy.value = 'decide'
  error.value = ''
  try {
    await decideGrading(props.leadId, {
      value: decideForm.value.value,
      reason: decideForm.value.reason.trim() === '' ? null : decideForm.value.reason.trim(),
    })
    decideForm.value = { value: decideForm.value.value, reason: '' }
    await load()
  } catch (err) {
    // 改档没写理由由后端拦（422），这里原样显示它的中文原因。
    error.value = err.message
  } finally {
    busy.value = ''
  }
}

// -------------------------------------------------------------- 跟进

async function saveFollowup() {
  busy.value = 'followup'
  followupError.value = ''
  try {
    await createFollowup(props.leadId, {
      conclusion: followupForm.value.conclusion,
      objection: followupForm.value.objection,
      next_followup_at: followupForm.value.next_followup_at,
      // 空串 = 明确标记「这次没给客户新东西」，后端据此统计无价值跟进。
      value_provided: followupForm.value.value_provided.trim(),
    })
    followupForm.value = {
      conclusion: '',
      objection: 'price',
      next_followup_at: '',
      value_provided: '',
    }
    await reloadFollowups()
  } catch (err) {
    followupError.value = err.message
  } finally {
    busy.value = ''
  }
}

// -------------------------------------------------------------- 话术草稿

async function generateDraft() {
  busy.value = 'draft'
  draftIssues.value = []
  try {
    draft.value = await createDraft(props.leadId)
    confirmForm.value = {
      objection: 'price',
      next_followup_at: '',
      value_provided: '',
    }
  } catch (err) {
    // 被门槛拦下时后端给 issues（中文原因），逐条显示；网络错只有 message。
    draftIssues.value = err.issues && err.issues.length > 0 ? err.issues : [{ message: err.message }]
  } finally {
    busy.value = ''
  }
}

async function submitDraft() {
  if (!draft.value) return
  busy.value = 'draft-confirm'
  draftIssues.value = []
  try {
    await confirmDraft(props.leadId, draft.value.draft_id, {
      objection: confirmForm.value.objection,
      next_followup_at: confirmForm.value.next_followup_at,
      value_provided: confirmForm.value.value_provided.trim(),
    })
    draft.value = null
    await load()
  } catch (err) {
    draftIssues.value = err.issues && err.issues.length > 0 ? err.issues : [{ message: err.message }]
  } finally {
    busy.value = ''
  }
}

async function dropDraft() {
  if (!draft.value) return
  busy.value = 'draft-discard'
  try {
    await discardDraft(props.leadId, draft.value.draft_id)
    draft.value = null
    draftIssues.value = []
  } catch (err) {
    draftIssues.value = err.issues && err.issues.length > 0 ? err.issues : [{ message: err.message }]
  } finally {
    busy.value = ''
  }
}

// -------------------------------------------------------------- 原文与事实

async function saveRaw() {
  busy.value = 'raw'
  rawError.value = ''
  rawHint.value = ''
  try {
    const item = await createRaw(props.leadId, { content: rawText.value.trim(), source: '手动粘贴' })
    savedRaw.value = item
    rawText.value = ''
    extractInfo.value = null
    rawHint.value = item.duplicated ? '这段原文之前已经录过，可以直接提炼。' : '原文已保存，可以提炼了。'
  } catch (err) {
    rawError.value = err.message
  } finally {
    busy.value = ''
  }
}

async function runExtract() {
  if (!savedRaw.value) return
  busy.value = 'extract'
  rawError.value = ''
  try {
    const result = await extractRaw(savedRaw.value.id)
    extractInfo.value = {
      accepted: result.accepted.length,
      skipped: result.skipped,
      fields: result.field_updates || {},
    }
    await load()
  } catch (err) {
    rawError.value = err.message
  } finally {
    busy.value = ''
  }
}

async function writeFact(field, value) {
  busy.value = 'fact'
  factsError.value = ''
  try {
    await patchFact(props.leadId, field, value)
    await load()
    return true
  } catch (err) {
    factsError.value = err.message
    return false
  } finally {
    busy.value = ''
  }
}

function startEdit(fact) {
  editingField.value = fact.field
  editValue.value = fact.value
}

async function saveEdit(field) {
  if (await writeFact(field, editValue.value || '')) editingField.value = ''
}

async function addFact() {
  if (await writeFact(newFact.value.field, newFact.value.value || '')) {
    newFact.value = { field: newFact.value.field, value: '' }
  }
}

// -------------------------------------------------------------- 阶段推进 / 补字段

async function advance() {
  if (!nextStage.value) return
  busy.value = 'advance'
  error.value = ''
  try {
    await advanceLead(props.leadId, nextStage.value)
    await load()
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = ''
  }
}

function toggleFieldForm() {
  if (fieldFormOpen.value) {
    fieldFormOpen.value = false
    return
  }
  const values = {}
  for (const field of missingFields.value) values[field] = ''
  fieldFormValues.value = values
  fieldError.value = ''
  fieldFormOpen.value = true
}

async function saveLeadFields() {
  const patch = {}
  for (const [field, value] of Object.entries(fieldFormValues.value)) {
    if (String(value || '').trim() !== '') patch[field] = String(value).trim()
  }
  if (Object.keys(patch).length === 0) {
    fieldError.value = '请至少填写一项再保存'
    return
  }
  busy.value = 'fields'
  fieldError.value = ''
  try {
    await patchLeadFields(props.leadId, patch)
    fieldFormOpen.value = false
    await load()
    // 补完自动重试推进一次：过不了由后端把「还缺什么」再说一遍。
    if (nextStage.value) await advance()
  } catch (err) {
    fieldError.value = err.message
  } finally {
    busy.value = ''
  }
}

onMounted(async () => {
  await loadTemplates()
  await load()
})
</script>

<template>
  <div class="detail">
    <div class="detail-nav">
      <button type="button" class="ghost" @click="emit('back')">← 返回</button>
      <span class="muted">客户详情</span>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="busy === 'load'" class="hint">加载中…</p>

    <template v-if="lead">
      <section class="card head">
        <div class="head-main">
          <h2 class="name">{{ lead.name }}</h2>
          <span class="grade-badge" :class="gradeClass(currentGrade())">
            {{ gradeBadge(currentGrade()) }}
          </span>
          <span class="muted">{{ lead.owner || '未分配（公海）' }}</span>
          <span class="stage">{{ stageLabel() }}</span>
        </div>
        <p class="reason">{{ gradeSummary() }}</p>
        <div class="head-bar">
          <div class="bar" :title="`信息卡覆盖 ${cardTotals.filled}/${cardTotals.total}`">
            <span class="bar-fill" :style="{ width: coverText }"></span>
          </div>
          <span class="muted">
            信息卡覆盖 {{ coverText }}（{{ cardTotals.filled }}/{{ cardTotals.total }}）· 缺
            {{ (card && card.missing.length) || 0 }} 项
          </span>
        </div>
        <div class="row-actions head-actions">
          <button
            type="button"
            :disabled="!nextStage || busy !== ''"
            :title="nextStage ? `推进到 ${nextStageLabel}` : '已是最后一步'"
            @click="advance"
          >
            {{ nextStage ? `推进到 ${nextStageLabel}` : '推进' }}
          </button>
          <button
            v-if="missingFields.length"
            type="button"
            class="ghost"
            :title="`还缺：${missingFields.map(fieldLabel).join('、')}`"
            @click="toggleFieldForm"
          >
            {{ fieldFormOpen ? '收起补字段' : '补字段' }}
          </button>
        </div>

        <div v-if="fieldFormOpen" class="panel-form">
          <label v-for="field in missingFields" :key="field">
            {{ fieldLabel(field) }}
            <input
              v-if="field === 'appointment_at'"
              v-model="fieldFormValues[field]"
              type="date"
            />
            <input v-else v-model="fieldFormValues[field]" type="text" placeholder="填写取值" />
          </label>
          <div class="row-actions">
            <button type="button" :disabled="busy === 'fields'" @click="saveLeadFields">
              {{ busy === 'fields' ? '保存中…' : '保存并重试推进' }}
            </button>
            <span class="muted">保存后自动重试推进一次</span>
          </div>
          <p v-if="fieldError" class="error">{{ fieldError }}</p>
        </div>
      </section>

      <section class="card">
        <h2>分级</h2>
        <p v-if="!latest" class="hint">分级加载中…</p>
        <template v-else>
          <!-- 默认只给「档位 + 一句话理由」；四维分解、分数区间与引用原文收进「查看计算过程」。 -->
          <p class="grade-line">
            <span class="grade-badge" :class="gradeClass(currentGrade())">
              {{ gradeBadge(currentGrade()) }}
            </span>
            <span class="grade-reason">{{ gradeSummary() }}</span>
          </p>

          <details class="calc">
            <summary>查看计算过程</summary>

            <div class="dims">
              <div v-for="dim in latest.result.dims" :key="dim.key" class="dim">
                <span class="dim-label">{{ dim.label }}</span>
                <span class="tag" :class="dim.known ? 'known' : 'unknown'">
                  {{ dim.known ? '已知' : '未知' }}
                </span>
                <span class="dim-score">{{ dim.known ? dim.score.toFixed(1) : '—' }}</span>
                <span class="muted dim-weight">权重 {{ formatPercent(dim.weight) }}</span>
              </div>
            </div>

            <p class="band">
              分数区间 <strong>{{ latest.result.low.toFixed(2) }} – {{ latest.result.high.toFixed(2) }}</strong>
              <span class="muted">（按规则现算，跨档时需要补全维度）</span>
            </p>
            <p class="reason">{{ latest.result.reason }}</p>
            <ul v-if="latest.result.basis.length" class="basis">
              <li v-for="(span, index) in latest.result.basis" :key="index" class="quote">
                「{{ span }}」
              </li>
            </ul>
          </details>

          <div class="row-actions">
            <button type="button" :disabled="busy !== ''" @click="repropose">
              {{ busy === 'propose' ? '评估中…' : '重新评估' }}
            </button>
            <span class="muted">重新评估会写一行留痕（规则出的档位）</span>
          </div>

          <div class="decide">
            <strong>改判</strong>
            <select v-model="decideForm.value">
              <option value="">选择档位</option>
              <option value="A">A</option>
              <option value="B">B</option>
              <option value="C">C</option>
            </select>
            <input
              v-model="decideForm.reason"
              type="text"
              placeholder="改档理由（改档时后端要求必填，≥2 字）"
            />
            <button
              type="button"
              :disabled="!decideForm.value || busy !== ''"
              @click="submitDecide"
            >
              {{ busy === 'decide' ? '提交中…' : '确认改判' }}
            </button>
          </div>

          <h3>历史（最新在前）</h3>
          <p v-if="history.length === 0" class="hint">还没有分级记录：点「重新评估」出第一档。</p>
          <table v-else>
            <thead>
              <tr>
                <th>时间</th>
                <th>AI 值 → 人工值</th>
                <th>理由</th>
                <th>模式</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in history" :key="row.id">
                <td>{{ formatTime(row.at) }}</td>
                <td>
                  {{ row.ai_value || '—' }} → {{ row.human_value || '（未定档）' }}
                </td>
                <td>{{ row.reason || '—' }}</td>
                <td>
                  <span class="badge">{{ row.mode }}</span>
                  <span v-if="row.actor" class="muted">{{ row.actor }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </template>
      </section>

      <section class="card">
        <h2>
          信息卡
          <span class="muted">（{{ coverText }}，{{ cardTotals.filled }}/{{ cardTotals.total }}）</span>
        </h2>
        <p v-if="!card" class="hint">信息卡加载中…</p>
        <template v-else>
          <!-- 顶部一行汇总：共缺几项；点一下 = 跳到第一个缺失分区并展开填写。 -->
          <button
            v-if="missingCount"
            type="button"
            class="missing-summary"
            @click="jumpToFirstMissing"
          >
            <span class="dot"></span>
            共缺 {{ missingCount }} 项，从这里开始补
          </button>
          <p v-else class="hint">这一张信息卡齐了 ✓</p>

          <div
            v-for="section in card.sections"
            :key="section.key"
            :id="`card-section-${section.key}`"
            class="section"
            :class="{ editing: sectionEditing(section.key) }"
          >
            <!-- 标题行：分区名 + 覆盖数 + 缺几项（有缺失才带橙点）。点它就是进入填写态。 -->
            <button
              type="button"
              class="section-head"
              :disabled="sectionSaving === section.key"
              @click="startSection(section)"
            >
              <span class="section-title">{{ sectionLabel(section.key, section.label) }}</span>
              <span class="section-count">{{ section.filled }}/{{ section.total }}</span>
              <template v-if="sectionMissing(section)">
                <span class="dot"></span>
                <span class="missing-note">缺 {{ sectionMissing(section) }} 项</span>
              </template>
              <span v-else class="done">✓</span>
              <span class="spacer"></span>
              <span class="muted section-hint">
                {{ sectionEditing(section.key) ? '填写中' : sectionMissing(section) ? '点击填写' : '已齐' }}
              </span>
            </button>

            <!-- 只读态：已填 `标签：值`（带来源小徽标），缺失是「橙点 + 灰标签 + 点击补全」。 -->
            <ul v-if="!sectionEditing(section.key)" class="fields">
              <li v-for="field in section.fields" :key="field.key" class="field-row">
                <template v-if="field.value">
                  <!-- 原文片段默认不显示：悬停整行有提示，点「原文」才在行内展开。 -->
                  <div class="field-line" :title="field.span ? `原文：「${field.span}」` : null">
                    <span class="field-label">{{ field.label }}：</span>
                    <span class="field-value">{{ field.value }}</span>
                    <span v-if="field.source" class="badge">{{ factSourceLabel(field) }}</span>
                    <span v-if="field.locked" class="badge lock">已锁定</span>
                    <button
                      v-if="field.span"
                      type="button"
                      class="link-button span-toggle"
                      :aria-expanded="spanOpen(section.key, field.key)"
                      @click="toggleSpan(section.key, field.key)"
                    >
                      {{ spanOpen(section.key, field.key) ? '收起原文' : '原文' }}
                    </button>
                  </div>
                  <p v-if="field.span && spanOpen(section.key, field.key)" class="quote span-text">
                    「{{ field.span }}」
                  </p>
                </template>
                <button
                  v-else
                  type="button"
                  class="field-line field-missing"
                  :title="field.question"
                  @click="startSection(section)"
                >
                  <span class="dot"></span>
                  <span class="field-label missing-label">{{ field.label }}</span>
                  <span class="spacer"></span>
                  <span class="click-hint">点击补全</span>
                </button>
              </li>
            </ul>

            <!-- 填写态：列出该区**所有**字段，已填的预填当前值，缺失的空着（占位是模板里的问题句）。
                 输入框在保存时不禁用：禁用会让它失焦，Enter 连打第二项就得重新点一次。 -->
            <div v-else class="section-form">
              <label v-for="field in section.fields" :key="field.key" class="form-row">
                <span class="form-label" :class="{ filled: field.value }">{{ field.label }}</span>
                <input
                  v-model="sectionForm[field.key]"
                  type="text"
                  :placeholder="field.question || '填写取值'"
                  @keydown.enter.prevent="saveSection(section)"
                  @keydown.esc.prevent="cancelSection"
                />
              </label>
              <div class="row-actions form-actions">
                <button
                  type="button"
                  :disabled="sectionSaving === section.key"
                  @click="saveSection(section)"
                >
                  {{ sectionSaving === section.key ? '保存中…' : '保存' }}
                </button>
                <button type="button" class="ghost" @click="cancelSection">完成</button>
                <span v-if="sectionNotice" class="saved-note">{{ sectionNotice }}</span>
                <span v-if="isComplete(section)" class="done">这一块齐了 ✓</span>
                <span class="muted">Enter 保存 · Esc 取消</span>
              </div>
              <p v-if="sectionError" class="error">{{ sectionError }}</p>
            </div>
          </div>
        </template>
      </section>

      <section class="card">
        <h2>跟进时间线</h2>
        <p v-if="timeline.length === 0" class="hint">还没有跟进记录。</p>
        <ul v-else class="timeline">
          <li v-for="item in timeline" :key="item.id">
            <div class="timeline-head">
              <span class="badge">{{ objectionLabel(item.objection) }}</span>
              <span class="muted">{{ formatTime(item.at) }}</span>
              <span v-if="item.next_followup_at" class="muted">下次：{{ item.next_followup_at }}</span>
            </div>
            <p class="conclusion">{{ item.conclusion }}</p>
            <p v-if="item.value_provided" class="value">本次提供的价值：{{ item.value_provided }}</p>
            <p v-else><span class="badge novalue">无价值跟进</span>这次没给客户新东西</p>
          </li>
        </ul>

        <div class="followup-form">
          <label>
            结论
            <input v-model="followupForm.conclusion" type="text" placeholder="这次谈了什么、有什么结论" />
          </label>
          <label>
            异议
            <select v-model="followupForm.objection">
              <option v-for="item in OBJECTIONS" :key="item.value" :value="item.value">
                {{ item.label }}
              </option>
            </select>
          </label>
          <label>
            下次跟进时间
            <input v-model="followupForm.next_followup_at" type="date" />
          </label>
          <label>
            本次提供的价值
            <input
              v-model="followupForm.value_provided"
              type="text"
              placeholder="留空 = 明确标记为无价值跟进"
            />
          </label>
          <button type="button" :disabled="busy !== ''" @click="saveFollowup">
            {{ busy === 'followup' ? '保存中…' : '记录跟进' }}
          </button>
        </div>
        <p v-if="followupError" class="error">{{ followupError }}</p>
      </section>

      <section class="card">
        <h2>话术草稿</h2>
        <div class="row-actions">
          <button
            type="button"
            :disabled="facts.length === 0 || busy !== ''"
            @click="generateDraft"
          >
            {{ busy === 'draft' ? '生成中…' : '生成话术草稿' }}
          </button>
          <span v-if="draft" class="badge">AI·{{ draft.model }}</span>
          <span v-if="facts.length === 0" class="muted">先提炼或补录事实，才能生成话术</span>
        </div>

        <ul v-if="draftIssues.length" class="draft-issues">
          <li v-for="(issue, index) in draftIssues" :key="index">{{ issue.message }}</li>
        </ul>

        <div v-if="draft" class="draft-result">
          <p class="draft-text">{{ draft.text }}</p>
          <ol class="citation-list">
            <li v-for="(cite, index) in draft.citations" :key="index">
              [{{ index + 1 }}] 来源：{{ cite.source }}　片段：{{ cite.text }}
            </li>
          </ol>
          <div class="row-actions">
            <select v-model="confirmForm.objection">
              <option v-for="item in OBJECTIONS" :key="item.value" :value="item.value">
                {{ item.label }}
              </option>
            </select>
            <input v-model="confirmForm.next_followup_at" type="date" />
            <input
              v-model="confirmForm.value_provided"
              type="text"
              placeholder="本次提供的价值（可留空）"
            />
            <button
              type="button"
              :disabled="!confirmForm.next_followup_at || busy !== ''"
              @click="submitDraft"
            >
              {{ busy === 'draft-confirm' ? '落库中…' : '确认落库' }}
            </button>
            <button type="button" class="ghost" :disabled="busy !== ''" @click="dropDraft">
              丢弃
            </button>
          </div>
        </div>
      </section>

      <details class="card">
        <summary><h2 class="inline-title">原文与事实（粘贴原文 → 提炼 → 手工修订）</h2></summary>

        <div class="raw-box">
          <textarea
            v-model="rawText"
            rows="3"
            placeholder="粘贴客户原话或聊天记录（至少 10 个字）"
          ></textarea>
          <div class="row-actions">
            <button type="button" :disabled="busy === 'raw'" @click="saveRaw">
              {{ busy === 'raw' ? '保存中…' : '保存原文' }}
            </button>
            <button
              type="button"
              class="ghost"
              :disabled="!savedRaw || busy === 'extract'"
              @click="runExtract"
            >
              {{ busy === 'extract' ? '提炼中…' : '提炼' }}
            </button>
          </div>
          <p v-if="rawError" class="error">{{ rawError }}</p>
          <p v-if="rawHint" class="hint">{{ rawHint }}</p>
          <div v-if="extractInfo">
            <p class="hint">本次采纳 {{ extractInfo.accepted }} 条</p>
            <details v-if="extractInfo.skipped.length" class="inline">
              <summary class="muted">跳过 {{ extractInfo.skipped.length }} 条（点击看原因）</summary>
              <ul class="skip-list">
                <li v-for="(item, index) in extractInfo.skipped" :key="index" class="muted">
                  {{ item.location }}：{{ item.message }}
                </li>
              </ul>
            </details>
          </div>
        </div>

        <p v-if="facts.length === 0" class="hint">
          还没有事实：先粘贴原文提炼，或在下方手工补一条。
        </p>
        <table v-else>
          <thead>
            <tr>
              <th>字段</th>
              <th>值（点一下可改）</th>
              <th>来源</th>
              <th>原文片段</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="fact in facts" :key="fact.id">
              <td>{{ factFieldLabel(fact.field) }}</td>
              <td>
                <div v-if="editingField === fact.field" class="row-actions">
                  <input v-model="editValue" type="text" />
                  <button type="button" :disabled="busy === 'fact'" @click="saveEdit(fact.field)">
                    保存
                  </button>
                  <button type="button" class="ghost" @click="editingField = ''">取消</button>
                </div>
                <button v-else type="button" class="value" @click="startEdit(fact)">
                  {{ fact.value }}
                </button>
              </td>
              <td>
                <span class="badge">{{ factSourceLabel(fact) }}</span>
                <span v-if="fact.locked" class="badge lock">已锁定</span>
              </td>
              <td><span class="muted">「{{ fact.source_span }}」</span></td>
            </tr>
          </tbody>
        </table>
        <p v-if="factsError" class="error">{{ factsError }}</p>

        <div class="add-fact">
          <span class="muted">手工补一条</span>
          <select v-model="newFact.field">
            <option v-for="item in FACT_FIELDS" :key="item.value" :value="item.value">
              {{ item.label }}
            </option>
          </select>
          <input v-model="newFact.value" type="text" placeholder="填写取值" />
          <button type="button" :disabled="busy === 'fact'" @click="addFact">保存</button>
        </div>
      </details>
    </template>
  </div>
</template>

<style scoped>
.detail-nav {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}

.detail-nav button {
  padding: 6px 12px;
  font-size: 13px;
}

.head-main {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.name {
  margin: 0;
  font-size: 20px;
}

.stage {
  padding: 1px 8px;
  border-radius: 10px;
  background: #eef2f7;
  color: #3d4653;
  font-size: 12px;
}

.reason {
  margin: 8px 0;
  font-size: 14px;
  color: #3d4653;
}

.head-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
}

.bar {
  flex: 0 0 160px;
  height: 6px;
  border-radius: 3px;
  background: #eef1f4;
  overflow: hidden;
}

.bar-fill {
  display: block;
  height: 100%;
  background: #1f6feb;
}

.head-actions {
  margin-top: 12px;
}

.panel-form {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed #e1e4e8;
  display: flex;
  gap: 12px;
  align-items: flex-end;
  flex-wrap: wrap;
  font-size: 13px;
}

.panel-form label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  color: #57606a;
}

.panel-form input {
  padding: 6px 8px;
  border: 1px solid #d0d7de;
  border-radius: 6px;
  font-size: 13px;
}

/* 分级默认那一行：档位徽标 + 一句话理由；徽标与文字之间的间距由 `.grade-badge` 的 margin 保证。 */
.grade-line {
  display: flex;
  gap: 0;
  align-items: baseline;
  flex-wrap: wrap;
  margin: 0 0 6px;
  font-size: 14px;
}

/* 头部一行里徽标后面还有归属人 / 阶段，间距交给 `.head-main` 的 gap，不叠一层 margin。 */
.head-main > .grade-badge {
  margin-right: 0;
}

.grade-reason {
  flex: 1;
  min-width: 0;
  color: #3d4653;
}

.calc {
  margin: 4px 0 8px;
}

.calc > summary {
  cursor: pointer;
  color: #1f6feb;
  font-size: 13px;
}

/* 四维分解：一行一维，已知/未知用色块区分，未知的分数显示「—」。 */
.dims {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 8px;
  margin-bottom: 10px;
}

.dim {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid #e1e4e8;
  border-radius: 6px;
  background: #fafbfc;
  font-size: 13px;
}

.dim-label {
  font-weight: 600;
}

.dim-score {
  font-size: 16px;
  font-weight: 600;
}

.dim-weight {
  margin-left: auto;
  font-size: 12px;
}

.tag {
  padding: 0 6px;
  border-radius: 8px;
  font-size: 11px;
}

.tag.known {
  background: #e7f6ec;
  color: #17663a;
}

.tag.unknown {
  background: #f6f8fa;
  color: #8a939d;
}

.band {
  margin: 8px 0;
}

.basis {
  margin: 0;
  padding-left: 0;
  list-style: none;
}

.decide {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px dashed #e1e4e8;
  font-size: 13px;
}

.decide select,
.decide input {
  padding: 6px 8px;
  border: 1px solid #d0d7de;
  border-radius: 6px;
  font-size: 13px;
}

.decide input {
  flex: 1;
  min-width: 220px;
}

h3 {
  margin: 14px 0 8px;
  font-size: 13px;
  color: #57606a;
  font-weight: 600;
}

.section {
  padding: 8px 0;
  border-bottom: 1px solid #eef1f4;
}

.section:last-child {
  border-bottom: 0;
}

.fields {
  margin: 0;
  padding: 0;
  list-style: none;
}

.field-row {
  padding: 6px 0;
  font-size: 14px;
}

.field-line {
  display: flex;
  gap: 8px;
  align-items: baseline;
  flex-wrap: wrap;
}

.field-label {
  color: #57606a;
  font-size: 13px;
}

/* 未填的字段整行变浅并挂一个橙点——缺什么，一眼能看到；点它直接进填写态。 */
.missing-summary {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 8px;
  padding: 6px 10px;
  border: 1px solid #f0d896;
  border-radius: 6px;
  background: #fff8e6;
  color: #7a5b00;
  font-size: 13px;
  cursor: pointer;
}

.missing-summary:hover {
  background: #fff1cc;
}

.dot {
  display: inline-block;
  flex: 0 0 8px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #f0a020;
}

.section-head {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 6px 0;
  border: 0;
  background: transparent;
  text-align: left;
  font-size: 14px;
  cursor: pointer;
}

.section-head:hover .section-title {
  color: #1f6feb;
}

.section-title {
  font-weight: 600;
  color: #1f2328;
}

.section-count {
  font-size: 12px;
  color: #8a939d;
}

.missing-note {
  font-size: 12px;
  color: #b07000;
}

.section-hint {
  font-size: 12px;
}

.spacer {
  flex: 1;
}

.done {
  font-size: 12px;
  color: #17663a;
}

.field-missing {
  width: 100%;
  padding: 4px 0;
  border: 0;
  background: transparent;
  text-align: left;
  cursor: pointer;
}

.field-missing:hover .click-hint {
  color: #1f6feb;
  text-decoration: underline;
}

.missing-label {
  color: #8a939d;
}

.click-hint {
  font-size: 12px;
  color: #9aa2ab;
}

.section.editing {
  background: #fffdf6;
}

.section-form {
  margin: 6px 0 10px;
  padding: 10px 12px;
  border: 1px dashed #d0d7de;
  border-radius: 6px;
  background: #fafbfc;
}

.form-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

.form-label {
  flex: 0 0 110px;
  font-size: 13px;
  color: #8a939d;
}

.form-label.filled {
  color: #57606a;
}

.form-row input {
  flex: 1;
  padding: 6px 8px;
  border: 1px solid #d0d7de;
  border-radius: 6px;
  font-size: 13px;
}

.saved-note {
  font-size: 12px;
  color: #17663a;
}

.quote {
  color: #8a939d;
  font-size: 12px;
}

/* 「原文」小开关：用文字链接的样子，不跟字段值抢位置。 */
.span-toggle {
  color: #8a939d;
  font-size: 12px;
  text-decoration: underline;
}

.span-text {
  margin: 4px 0 0 12px;
  padding-left: 8px;
  border-left: 2px solid #e1e4e8;
}

.timeline {
  margin: 0 0 12px;
  padding: 0;
  list-style: none;
}

.timeline li {
  padding: 10px 0;
  border-bottom: 1px solid #eef1f4;
}

.timeline-head {
  display: flex;
  gap: 10px;
  align-items: baseline;
  flex-wrap: wrap;
  font-size: 13px;
}

.conclusion {
  margin: 4px 0;
}

.value {
  margin: 0;
  color: #17663a;
  font-size: 13px;
}

.followup-form {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
  align-items: end;
  padding-top: 10px;
  border-top: 1px dashed #e1e4e8;
}

.followup-form label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  color: #57606a;
}

.followup-form input,
.followup-form select {
  padding: 8px 10px;
  border: 1px solid #d0d7de;
  border-radius: 6px;
  font-size: 14px;
}

.draft-issues {
  margin: 10px 0 0;
  padding: 8px 10px 8px 28px;
  border: 1px solid #ffc1c0;
  border-radius: 6px;
  background: #fff0ef;
  color: #a40e26;
  font-size: 13px;
  line-height: 1.7;
}

.draft-result {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 10px;
  padding: 10px 12px;
  border: 1px solid #e1e4e8;
  border-radius: 6px;
  background: #fafbfc;
}

.draft-text {
  margin: 0;
  font-size: 14px;
  line-height: 1.8;
}

.citation-list {
  margin: 0;
  padding-left: 22px;
  font-size: 12px;
  line-height: 1.7;
  color: #57606a;
}

.raw-box {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 0;
}

.raw-box textarea {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid #d0d7de;
  border-radius: 6px;
  font-size: 14px;
  font-family: inherit;
  resize: vertical;
}

table button.value {
  padding: 2px 6px;
  background: #f6f8fa;
  color: #1f2328;
  border: 1px solid #d0d7de;
  font-size: 13px;
  text-align: left;
}

.add-fact {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed #e1e4e8;
}

.add-fact input,
.add-fact select {
  padding: 6px 8px;
  border: 1px solid #d0d7de;
  border-radius: 6px;
  font-size: 13px;
}

.skip-list {
  margin: 6px 0 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.7;
}

details > summary {
  cursor: pointer;
}

.inline-title {
  display: inline;
  font-size: 16px;
}
</style>
