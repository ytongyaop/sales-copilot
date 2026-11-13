<script setup>
import { onMounted, ref } from 'vue'
import { createLead, listLeads } from './api'

// 来源下拉：与后端 ALLOWED_SOURCES 一一对应，这里只是显示文案，取值以字符串为准。
const SOURCES = [
  { value: 'referral', label: '转介绍' },
  { value: 'inbound', label: '主动咨询' },
  { value: 'outbound', label: '主动触达' },
  { value: 'event', label: '活动' },
  { value: 'other', label: '其他' },
]

const form = ref({ name: '', contact: '', source: 'referral', owner: '' })
const leads = ref([])
const loading = ref(false)
const submitting = ref(false)
const error = ref('')

function sourceLabel(value) {
  const hit = SOURCES.find((item) => item.value === value)
  return hit ? hit.label : value
}

function formatTime(iso) {
  if (!iso) return '-'
  return iso.replace('T', ' ').slice(0, 16)
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

async function submit() {
  submitting.value = true
  error.value = ''
  try {
    await createLead({
      name: form.value.name,
      contact: form.value.contact,
      source: form.value.source,
      owner: form.value.owner.trim() === '' ? null : form.value.owner,
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

onMounted(load)
</script>

<template>
  <main class="page">
    <h1>线索</h1>

    <section class="card">
      <h2>新增线索</h2>
      <form class="form" @submit.prevent="submit">
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
            <th>创建时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="lead in leads" :key="lead.id">
            <td>{{ lead.name }}</td>
            <td>{{ sourceLabel(lead.source) }}</td>
            <td>{{ lead.owner || '未分配' }}</td>
            <td>{{ formatTime(lead.created_at) }}</td>
          </tr>
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
}
th {
  color: #57606a;
  font-weight: 600;
  font-size: 13px;
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
