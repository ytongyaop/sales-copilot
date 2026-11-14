<script setup>
import { onMounted, ref } from 'vue'
import {
  advanceLead,
  createFollowup,
  createLead,
  listFollowups,
  listLeads,
  listTemplates,
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

// 每个线索的展开状态、跟进记录与错误各自独立，避免一行报错串到别的行。
const expandedId = ref('')
const followups = ref({})
const followupErrors = ref({})
const followupForm = ref({ conclusion: '', objection: 'price', next_followup_at: '' })
const busyId = ref('')
const savingFollowup = ref(false)

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
  await loadFollowups(lead.id)
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
.muted {
  color: #8a939d;
}
.hint {
  color: #57606a;
  margin: 0;
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
