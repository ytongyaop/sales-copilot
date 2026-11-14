<script setup>
import { onMounted, ref } from 'vue'
import {
  advanceLead,
  confirmDraft,
  createDraft,
  createFollowup,
  createLead,
  createRaw,
  discardDraft,
  extractRaw,
  listFacts,
  listFollowups,
  listLeads,
  listTemplates,
  patchFact,
} from './api'

// 来源下拉：与后端 ALLOWED_SOURCES 一一对应，这里只是显示文案，取值以字符串为准。
const SOURCES = [
  { value: 'referral', label: '转介绍' },
  { value: 'inbound', label: '主动咨询' },
  { value: 'outbound', label: '主动触达' },
  { value: 'event', label: '活动' },
  { value: 'other', label: '其他' },
]

// 异议下拉：与后端 ALLOWED_OBJECTIONS 一一对应，后端才是唯一权威。
const OBJECTIONS = [
  { value: 'price', label: '价格' },
  { value: 'time', label: '时间' },
  { value: 'effect', label: '效果' },
  { value: 'trust', label: '信任' },
  { value: 'other', label: '其它' },
]

const templates = ref([])
const templateKey = ref('edu_training')
const form = ref({ name: '', contact: '', source: 'referral', owner: '' })
const leads = ref([])
const loading = ref(false)
const submitting = ref(false)
const error = ref('')

// 事实字段的中文名：只影响显示，字段名本身是机器契约（与后端 ALLOWED_FACT_FIELDS 对齐）。
const FACT_FIELDS = [
  { value: 'decision_maker', label: '决策人' },
  { value: 'need', label: '核心需求' },
  { value: 'objection', label: '异议' },
  { value: 'amount', label: '金额' },
  { value: 'appointment_at', label: '约定时间' },
]

// 来源徽标：后端只产这三种来源，前端只负责翻译。
const SOURCE_LABELS = { llm: 'AI', manual: '人工', rule: '规则' }

// 每个线索的展开状态、跟进记录与错误各自独立，避免一行报错串到别的行。
const expandedId = ref('')
const followups = ref({})
const followupErrors = ref({})
const followupForm = ref({ conclusion: '', objection: 'price', next_followup_at: '' })
const busyId = ref('')
const savingFollowup = ref(false)

// 每个线索的原文与事实也各自独立：一个线索报错不影响别的线索。
const rawText = ref({})
const savedRaw = ref({})
const rawHint = ref({})
const rawError = ref({})
const extractInfo = ref({})
const facts = ref({})
const factsLoading = ref({})
const factsError = ref({})
const busyFacts = ref({})
const editingField = ref({})
const editValue = ref({})
const newFact = ref({})

// 话术草稿：每个线索一份独立的草稿、问题列表与确认表单，互不影响。
// `model` 必须显示出来（AI·fake / AI·deepseek），免得把假模型的输出当成真结论。
const draft = ref({})
const draftIssues = ref({})
const draftError = ref({})
const draftBusy = ref({})
const confirmForm = ref({})

function sourceLabel(value) {
  const hit = SOURCES.find((item) => item.value === value)
  return hit ? hit.label : value
}

function objectionLabel(value) {
  const hit = OBJECTIONS.find((item) => item.value === value)
  return hit ? hit.label : value
}

function formatTime(iso) {
  if (!iso) return '-'
  return iso.replace('T', ' ').slice(0, 16)
}

// 事实相关的中文名：字段名、来源徽标、异议枚举都只做显示翻译，不复制后端规则。
function factFieldLabel(field) {
  const hit = FACT_FIELDS.find((item) => item.value === field)
  return hit ? hit.label : field
}

function factValueLabel(fact) {
  return fact.field === 'objection' ? objectionLabel(fact.value) : fact.value
}

function factSourceLabel(fact) {
  // 模型产出的事实带上模型名（AI·fake / AI·deepseek），人工/规则仍是中文徽标。
  if (fact.source === 'llm') {
    return fact.model ? `AI·${fact.model}` : 'AI'
  }
  return SOURCE_LABELS[fact.source] || fact.source
}

// 「下一阶段」由后端给的模板 flow 计算，不在前端复制流程规则。
function templateOf(lead) {
  return templates.value.find((item) => item.key === lead.template)
}

// 阶段中文名：优先用模板给的 labels（行业叫法只在后端模板里），没有再退回块 id。
function stageLabel(lead) {
  const template = templateOf(lead)
  return (template && template.labels && template.labels[lead.stage]) || lead.stage
}

function nextStage(lead) {
  const template = templateOf(lead)
  if (!template) return ''
  const index = template.flow.indexOf(lead.stage)
  if (index < 0 || index === template.flow.length - 1) return ''
  return template.flow[index + 1]
}

function nextStageLabel(lead) {
  const next = nextStage(lead)
  if (!next) return ''
  const template = templateOf(lead)
  return (template && template.labels && template.labels[next]) || next
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    leads.value = await listLeads()
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

async function submit() {
  submitting.value = true
  error.value = ''
  try {
    await createLead({
      name: form.value.name,
      contact: form.value.contact,
      source: form.value.source,
      owner: form.value.owner.trim() === '' ? null : form.value.owner,
      template: templateKey.value,
    })
    form.value = { name: '', contact: '', source: 'referral', owner: '' }
    await load()
  } catch (err) {
    // 校验失败的后端提示（422）与「服务未连接」都走这里，界面只显示业务语言。
    error.value = err.message
  } finally {
    submitting.value = false
  }
}

async function advance(lead) {
  const target = nextStage(lead)
  if (!target) return

  busyId.value = lead.id
  error.value = ''
  try {
    await advanceLead(lead.id, target)
    await load()
  } catch (err) {
    error.value = err.message
  } finally {
    busyId.value = ''
  }
}

async function loadFollowups(leadId) {
  try {
    const items = await listFollowups(leadId)
    followups.value = { ...followups.value, [leadId]: items }
  } catch (err) {
    followupErrors.value = { ...followupErrors.value, [leadId]: err.message }
  }
}

async function toggleFollowups(lead) {
  if (expandedId.value === lead.id) {
    expandedId.value = ''
    return
  }

  expandedId.value = lead.id
  followupForm.value = { conclusion: '', objection: 'price', next_followup_at: '' }
  followupErrors.value = { ...followupErrors.value, [lead.id]: '' }
  newFact.value = { ...newFact.value, [lead.id]: { field: 'need', value: '' } }
  clearDraft(lead.id)
  await loadFollowups(lead.id)
  await loadFacts(lead.id)
}

async function saveFollowup(lead) {
  savingFollowup.value = true
  followupErrors.value = { ...followupErrors.value, [lead.id]: '' }
  try {
    await createFollowup(lead.id, followupForm.value)
    followupForm.value = { conclusion: '', objection: 'price', next_followup_at: '' }
    await loadFollowups(lead.id)
  } catch (err) {
    followupErrors.value = { ...followupErrors.value, [lead.id]: err.message }
  } finally {
    savingFollowup.value = false
  }
}

// -------------------------------------------------------------- 原文 → 事实

async function loadFacts(leadId) {
  factsLoading.value = { ...factsLoading.value, [leadId]: true }
  factsError.value = { ...factsError.value, [leadId]: '' }
  try {
    facts.value = { ...facts.value, [leadId]: await listFacts(leadId) }
  } catch (err) {
    factsError.value = { ...factsError.value, [leadId]: err.message }
  } finally {
    factsLoading.value = { ...factsLoading.value, [leadId]: false }
  }
}

async function saveRaw(lead) {
  const text = (rawText.value[lead.id] || '').trim()
  busyFacts.value = { ...busyFacts.value, [lead.id]: 'save' }
  rawError.value = { ...rawError.value, [lead.id]: '' }
  rawHint.value = { ...rawHint.value, [lead.id]: '' }
  try {
    const item = await createRaw(lead.id, { content: text, source: '手动粘贴' })
    savedRaw.value = { ...savedRaw.value, [lead.id]: item }
    rawText.value = { ...rawText.value, [lead.id]: '' }
    // 换了原文，旧的提炼结果就不作数了（免得对着新原文看旧数字）。
    extractInfo.value = { ...extractInfo.value, [lead.id]: null }
    rawHint.value = {
      ...rawHint.value,
      [lead.id]: item.duplicated ? '这段原文之前已经录过，可以直接提炼。' : '原文已保存，可以提炼了。',
    }
  } catch (err) {
    rawError.value = { ...rawError.value, [lead.id]: err.message }
  } finally {
    busyFacts.value = { ...busyFacts.value, [lead.id]: '' }
  }
}

async function runExtract(lead) {
  const raw = savedRaw.value[lead.id]
  if (!raw) return

  busyFacts.value = { ...busyFacts.value, [lead.id]: 'extract' }
  rawError.value = { ...rawError.value, [lead.id]: '' }
  rawHint.value = { ...rawHint.value, [lead.id]: '' }
  try {
    const result = await extractRaw(raw.id)
    extractInfo.value = {
      ...extractInfo.value,
      [lead.id]: { accepted: result.accepted.length, skipped: result.skipped },
    }
    await loadFacts(lead.id)
  } catch (err) {
    rawError.value = { ...rawError.value, [lead.id]: err.message }
  } finally {
    busyFacts.value = { ...busyFacts.value, [lead.id]: '' }
  }
}

// 人工写一个字段（编辑已有或补录）：后端负责 upsert 与锁定，前端只发值和刷新。
async function writeFact(lead, field, value) {
  busyFacts.value = { ...busyFacts.value, [lead.id]: 'patch' }
  factsError.value = { ...factsError.value, [lead.id]: '' }
  try {
    await patchFact(lead.id, field, value)
    await loadFacts(lead.id)
    return true
  } catch (err) {
    factsError.value = { ...factsError.value, [lead.id]: err.message }
    return false
  } finally {
    busyFacts.value = { ...busyFacts.value, [lead.id]: '' }
  }
}

function startEdit(lead, fact) {
  editingField.value = { ...editingField.value, [lead.id]: fact.field }
  editValue.value = { ...editValue.value, [lead.id]: fact.value }
}

function cancelEdit(lead) {
  editingField.value = { ...editingField.value, [lead.id]: '' }
}

async function saveEdit(lead, field) {
  const ok = await writeFact(lead, field, editValue.value[lead.id] || '')
  if (ok) editingField.value = { ...editingField.value, [lead.id]: '' }
}

async function addFact(lead) {
  const draftFact = newFact.value[lead.id]
  if (!draftFact) return
  const ok = await writeFact(lead, draftFact.field, draftFact.value || '')
  if (ok) newFact.value = { ...newFact.value, [lead.id]: { field: draftFact.field, value: '' } }
}

// -------------------------------------------------------------- 话术草稿

async function generateDraft(lead) {
  draftBusy.value = { ...draftBusy.value, [lead.id]: 'generate' }
  draftIssues.value = { ...draftIssues.value, [lead.id]: [] }
  try {
    const result = await createDraft(lead.id)
    draft.value = { ...draft.value, [lead.id]: result }
    confirmForm.value = {
      ...confirmForm.value,
      [lead.id]: { objection: 'price', next_followup_at: '' },
    }
  } catch (err) {
    // 被门槛拦下时后端给的是 issues（中文原因），逐条显示；网络错只有 message。
    draftIssues.value = { ...draftIssues.value, [lead.id]: draftIssueList(err) }
  } finally {
    draftBusy.value = { ...draftBusy.value, [lead.id]: '' }
  }
}

async function submitDraft(lead) {
  const current = draft.value[lead.id]
  if (!current) return

  const form = confirmForm.value[lead.id] || { objection: 'price', next_followup_at: '' }
  draftBusy.value = { ...draftBusy.value, [lead.id]: 'confirm' }
  draftIssues.value = { ...draftIssues.value, [lead.id]: [] }
  try {
    await confirmDraft(lead.id, current.draft_id, form)
    clearDraft(lead.id)
    await loadFollowups(lead.id)
  } catch (err) {
    draftIssues.value = { ...draftIssues.value, [lead.id]: draftIssueList(err) }
  } finally {
    draftBusy.value = { ...draftBusy.value, [lead.id]: '' }
  }
}

async function dropDraft(lead) {
  const current = draft.value[lead.id]
  if (!current) return

  draftBusy.value = { ...draftBusy.value, [lead.id]: 'discard' }
  try {
    await discardDraft(lead.id, current.draft_id)
    clearDraft(lead.id)
  } catch (err) {
    draftIssues.value = { ...draftIssues.value, [lead.id]: draftIssueList(err) }
  } finally {
    draftBusy.value = { ...draftBusy.value, [lead.id]: '' }
  }
}

function draftIssueList(err) {
  return err.issues && err.issues.length > 0 ? err.issues : [{ message: err.message }]
}

function clearDraft(leadId) {
  draft.value = { ...draft.value, [leadId]: null }
  draftIssues.value = { ...draftIssues.value, [leadId]: [] }
}

onMounted(async () => {
  await loadTemplates()
  await load()
})
</script>

<template>
  <main class="page">
    <h1>线索</h1>

    <section class="card">
      <h2>新增线索</h2>
      <form class="form" @submit.prevent="submit">
        <label>
          模板
          <select v-model="templateKey">
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

      <p v-if="error" class="error">{{ error }}</p>
    </section>

    <section class="card">
      <h2>线索列表</h2>
      <p v-if="loading" class="hint">加载中…</p>
      <p v-else-if="leads.length === 0" class="hint">还没有线索，先在上方新增一条。</p>
      <table v-else>
        <thead>
          <tr>
            <th>名称</th>
            <th>来源</th>
            <th>归属</th>
            <th>阶段</th>
            <th>创建时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="lead in leads" :key="lead.id">
            <tr>
              <td>{{ lead.name }}</td>
              <td>{{ sourceLabel(lead.source) }}</td>
              <td>{{ lead.owner || '未分配' }}</td>
              <td>{{ stageLabel(lead) }}</td>
              <td>{{ formatTime(lead.created_at) }}</td>
              <td class="actions">
                <button
                  type="button"
                  :disabled="busyId === lead.id || !nextStage(lead)"
                  :title="nextStage(lead) ? `推进到 ${nextStageLabel(lead)}` : '已是最后一步'"
                  @click="advance(lead)"
                >
                  {{ nextStage(lead) ? `推进到 ${nextStageLabel(lead)}` : '推进' }}
                </button>
                <button type="button" class="ghost" @click="toggleFollowups(lead)">
                  {{ expandedId === lead.id ? '收起' : '记录跟进' }}
                </button>
              </td>
            </tr>

            <tr v-if="expandedId === lead.id" class="expand">
              <td colspan="6">
                <div class="followup-form">
                  <label>
                    结论
                    <input
                      v-model="followupForm.conclusion"
                      type="text"
                      placeholder="这次谈了什么、有什么结论"
                    />
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
                  <button type="button" :disabled="savingFollowup" @click="saveFollowup(lead)">
                    {{ savingFollowup ? '保存中…' : '保存跟进' }}
                  </button>
                </div>

                <p v-if="followupErrors[lead.id]" class="error">{{ followupErrors[lead.id] }}</p>

                <p v-if="(followups[lead.id] || []).length === 0" class="hint">
                  还没有跟进记录。
                </p>
                <ul v-else class="followup-list">
                  <li v-for="item in followups[lead.id]" :key="item.id">
                    <span class="badge">{{ objectionLabel(item.objection) }}</span>
                    {{ item.conclusion }}
                    <span class="muted">下次：{{ item.next_followup_at }} · 记录于 {{ formatTime(item.at) }}</span>
                  </li>
                </ul>

                <details class="panel">
                  <summary>原文提炼与事实</summary>

                  <div class="raw-box">
                    <textarea
                      v-model="rawText[lead.id]"
                      rows="3"
                      placeholder="粘贴客户原话或聊天记录（至少 10 个字）"
                    ></textarea>
                    <div class="row-actions">
                      <button
                        type="button"
                        :disabled="busyFacts[lead.id] === 'save'"
                        @click="saveRaw(lead)"
                      >
                        {{ busyFacts[lead.id] === 'save' ? '保存中…' : '保存原文' }}
                      </button>
                      <button
                        type="button"
                        class="ghost"
                        :disabled="!savedRaw[lead.id] || busyFacts[lead.id] === 'extract'"
                        @click="runExtract(lead)"
                      >
                        {{ busyFacts[lead.id] === 'extract' ? '提炼中…' : '提炼' }}
                      </button>
                    </div>

                    <p v-if="rawError[lead.id]" class="error">{{ rawError[lead.id] }}</p>
                    <p v-if="rawHint[lead.id]" class="hint">{{ rawHint[lead.id] }}</p>

                    <div v-if="extractInfo[lead.id]" class="extract-result">
                      <p class="hint">本次采纳 {{ extractInfo[lead.id].accepted }} 条</p>
                      <details v-if="extractInfo[lead.id].skipped.length" class="inline">
                        <summary class="muted">
                          跳过 {{ extractInfo[lead.id].skipped.length }} 条（点击看原因）
                        </summary>
                        <ul class="skip-list">
                          <li
                            v-for="(item, index) in extractInfo[lead.id].skipped"
                            :key="index"
                            class="muted"
                          >
                            {{ item.location }}：{{ item.message }}
                          </li>
                        </ul>
                      </details>
                    </div>
                  </div>

                  <p v-if="factsLoading[lead.id]" class="hint">事实加载中…</p>
                  <p v-else-if="!facts[lead.id] || facts[lead.id].length === 0" class="hint">
                    还没有事实：先粘贴原文提炼，或在下方手工补一条。
                  </p>
                  <table v-else class="fact-table">
                    <thead>
                      <tr>
                        <th>字段</th>
                        <th>值（点一下可改）</th>
                        <th>来源</th>
                        <th>置信度</th>
                        <th>原文片段</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="fact in facts[lead.id]" :key="fact.id">
                        <td>{{ factFieldLabel(fact.field) }}</td>
                        <td>
                          <div v-if="editingField[lead.id] === fact.field" class="row-actions">
                            <select v-if="fact.field === 'objection'" v-model="editValue[lead.id]">
                              <option v-for="item in OBJECTIONS" :key="item.value" :value="item.value">
                                {{ item.label }}
                              </option>
                            </select>
                            <input v-else v-model="editValue[lead.id]" type="text" />
                            <button
                              type="button"
                              :disabled="busyFacts[lead.id] === 'patch'"
                              @click="saveEdit(lead, fact.field)"
                            >
                              保存
                            </button>
                            <button type="button" class="ghost" @click="cancelEdit(lead)">取消</button>
                          </div>
                          <button v-else type="button" class="value" @click="startEdit(lead, fact)">
                            {{ factValueLabel(fact) }}
                          </button>
                        </td>
                        <td>
                          <span class="badge">{{ factSourceLabel(fact) }}</span>
                          <span v-if="fact.locked" class="badge lock">已锁定</span>
                        </td>
                        <td>{{ (fact.confidence * 100).toFixed(0) }}%</td>
                        <td><span class="muted">「{{ fact.source_span }}」</span></td>
                      </tr>
                    </tbody>
                  </table>

                  <p v-if="factsError[lead.id]" class="error">{{ factsError[lead.id] }}</p>

                  <div v-if="newFact[lead.id]" class="add-fact">
                    <span class="muted">手工补一条</span>
                    <select v-model="newFact[lead.id].field">
                      <option v-for="item in FACT_FIELDS" :key="item.value" :value="item.value">
                        {{ item.label }}
                      </option>
                    </select>
                    <select v-if="newFact[lead.id].field === 'objection'" v-model="newFact[lead.id].value">
                      <option v-for="item in OBJECTIONS" :key="item.value" :value="item.value">
                        {{ item.label }}
                      </option>
                    </select>
                    <input v-else v-model="newFact[lead.id].value" type="text" placeholder="填写取值" />
                    <button type="button" :disabled="busyFacts[lead.id] === 'patch'" @click="addFact(lead)">
                      保存
                    </button>
                  </div>
                </details>

                <div class="draft-box">
                  <div class="row-actions">
                    <button
                      type="button"
                      :disabled="!facts[lead.id] || facts[lead.id].length === 0 || draftBusy[lead.id] === 'generate'"
                      @click="generateDraft(lead)"
                    >
                      {{ draftBusy[lead.id] === 'generate' ? '生成中…' : '生成话术草稿' }}
                    </button>
                    <span v-if="draft[lead.id]" class="badge">AI·{{ draft[lead.id].model }}</span>
                    <span v-if="!facts[lead.id] || facts[lead.id].length === 0" class="muted">
                      先提炼或补录事实，才能生成话术
                    </span>
                  </div>

                  <ul v-if="(draftIssues[lead.id] || []).length" class="draft-issues">
                    <li v-for="(issue, index) in draftIssues[lead.id]" :key="index">
                      {{ issue.message }}
                    </li>
                  </ul>

                  <div v-if="draft[lead.id]" class="draft-result">
                    <p class="draft-text">{{ draft[lead.id].text }}</p>
                    <ol class="citation-list">
                      <li v-for="(cite, index) in draft[lead.id].citations" :key="index">
                        [{{ index + 1 }}] 来源：{{ cite.source }}　片段：{{ cite.text }}
                      </li>
                    </ol>

                    <div class="row-actions">
                      <select v-model="confirmForm[lead.id].objection">
                        <option v-for="item in OBJECTIONS" :key="item.value" :value="item.value">
                          {{ item.label }}
                        </option>
                      </select>
                      <input v-model="confirmForm[lead.id].next_followup_at" type="date" />
                      <button
                        type="button"
                        :disabled="!confirmForm[lead.id].next_followup_at || draftBusy[lead.id] === 'confirm'"
                        @click="submitDraft(lead)"
                      >
                        {{ draftBusy[lead.id] === 'confirm' ? '落库中…' : '确认落库' }}
                      </button>
                      <button
                        type="button"
                        class="ghost"
                        :disabled="draftBusy[lead.id] === 'discard'"
                        @click="dropDraft(lead)"
                      >
                        丢弃
                      </button>
                    </div>
                  </div>
                </div>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </section>
  </main>
</template>

<style>
:root {
  color: #1f2328;
  background: #f6f7f9;
  font-family: system-ui, -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif;
}
.page {
  max-width: 880px;
  margin: 0 auto;
  padding: 24px 16px 48px;
}
h1 {
  font-size: 24px;
  margin: 0 0 16px;
}
h2 {
  font-size: 16px;
  margin: 0 0 12px;
}
.card {
  background: #fff;
  border: 1px solid #e1e4e8;
  border-radius: 8px;
  padding: 16px;
  margin-bottom: 16px;
}
.form {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  align-items: end;
}
.form label,
.followup-form label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  color: #57606a;
}
.form input,
.form select,
.followup-form input,
.followup-form select {
  padding: 8px 10px;
  border: 1px solid #d0d7de;
  border-radius: 6px;
  font-size: 14px;
  color: #1f2328;
  background: #fff;
}
button {
  padding: 9px 16px;
  border: 0;
  border-radius: 6px;
  background: #1f6feb;
  color: #fff;
  font-size: 14px;
  cursor: pointer;
}
button:disabled {
  background: #9db7e0;
  cursor: not-allowed;
}
button.ghost {
  background: #fff;
  color: #1f6feb;
  border: 1px solid #c8d8f5;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
}
th,
td {
  text-align: left;
  padding: 8px 10px;
  border-bottom: 1px solid #eaeef2;
  vertical-align: top;
}
th {
  color: #57606a;
  font-weight: 600;
  font-size: 13px;
}
.actions {
  display: flex;
  gap: 8px;
}
.actions button {
  padding: 6px 10px;
  font-size: 13px;
}
.followup-form {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  align-items: end;
  padding: 12px 0;
}
.followup-list {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.9;
}
.badge {
  display: inline-block;
  padding: 1px 8px;
  margin-right: 6px;
  border-radius: 10px;
  background: #eef2f7;
  color: #3d4653;
  font-size: 12px;
}
.badge.lock {
  background: #fff4d6;
  color: #7a5b00;
}
.panel {
  margin-top: 12px;
  border-top: 1px dashed #e1e4e8;
  padding-top: 10px;
  font-size: 13px;
}
.panel > summary,
details.inline > summary {
  cursor: pointer;
  color: #57606a;
  font-size: 13px;
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
  color: #1f2328;
  resize: vertical;
}
.row-actions {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.row-actions input,
.row-actions select,
.add-fact input,
.add-fact select {
  padding: 6px 8px;
  border: 1px solid #d0d7de;
  border-radius: 6px;
  font-size: 13px;
  color: #1f2328;
  background: #fff;
}
.extract-result {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.skip-list {
  margin: 6px 0 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.7;
}
.fact-table {
  margin: 8px 0;
  font-size: 13px;
}
.fact-table button.value {
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
  padding-top: 10px;
  border-top: 1px dashed #e1e4e8;
}
.muted {
  color: #8a939d;
}
.hint {
  color: #57606a;
  margin: 0;
}
.draft-box {
  margin-top: 12px;
  border-top: 1px dashed #e1e4e8;
  padding-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.draft-issues {
  margin: 0;
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
.error {
  margin: 12px 0 0;
  padding: 10px 12px;
  border: 1px solid #ffc1c0;
  border-radius: 6px;
  background: #fff0ef;
  color: #a40e26;
  font-size: 14px;
}
</style>
