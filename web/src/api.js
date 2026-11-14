// 前端与后端的唯一通道：只发 HTTP，不碰数据库、不复制校验规则。
// 出错时抛出带业务语言 message 的 Error；调用方只负责把 message 显示出来。

const BASE = '/api'

const OFFLINE_MESSAGE =
  '服务未连接，请确认后端已启动（uvicorn salescopilot.entrypoints.api.app:app --reload --port 8000）'

async function request(path, options) {
  let response
  try {
    response = await fetch(`${BASE}${path}`, options)
  } catch {
    // fetch 本身失败 = 网络层没通（后端没起、端口不对）
    throw new Error(OFFLINE_MESSAGE)
  }

  // 后端返回的一定是 JSON；解析不出来说明是代理层报错（后端进程没起时代理给 500 + 空文本）
  const data = await response.json().catch(() => null)

  if (!response.ok) {
    const issues = data && Array.isArray(data.issues) ? data.issues : []
    if (issues.length === 0 && data === null) {
      throw new Error(OFFLINE_MESSAGE)
    }
    const error = new Error(
      issues.length > 0 ? issues.map((issue) => issue.message).join('；') : `请求失败（HTTP ${response.status}）`,
    )
    error.issues = issues
    throw error
  }

  return data
}

function jsonOptions(method, payload) {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }
}

export function listLeads() {
  return request('/leads')
}

// 运行模式：演示前先确认后端是真模型还是假模型（假模型的数据不能当真实结论）。
export function getHealth() {
  return request('/health')
}

export function createLead(payload) {
  return request('/leads', jsonOptions('POST', payload))
}

export function listTemplates() {
  return request('/templates')
}

export function advanceLead(leadId, targetStage) {
  return request(`/leads/${leadId}/advance`, jsonOptions('POST', { target_stage: targetStage }))
}

export function listFollowups(leadId) {
  return request(`/leads/${leadId}/followups`)
}

export function createFollowup(leadId, payload) {
  return request(`/leads/${leadId}/followups`, jsonOptions('POST', payload))
}

// ---------------------------------------------------------------- 原文 / 事实

export function createRaw(leadId, payload) {
  return request(`/leads/${leadId}/raws`, jsonOptions('POST', payload))
}

export function extractRaw(rawId) {
  return request(`/raws/${rawId}/extract`, { method: 'POST' })
}

export function listFacts(leadId) {
  return request(`/leads/${leadId}/facts`)
}

// 人工写字段：后端是 upsert（没有就建、有就改并锁定），前端不复制这套规则。
export function patchFact(leadId, field, value) {
  return request(`/leads/${leadId}/facts/${encodeURIComponent(field)}`, jsonOptions('PATCH', { value }))
}

// ---------------------------------------------------------------- 原文列表 / 话术草稿

export function listRaws(leadId) {
  return request(`/leads/${leadId}/raws`)
}

// 生成草稿：后端会先过「有事实 / 有资料依据 / 模型引用 / 无禁用词」四道门槛，
// 不通过时返回 422 + issues，前端只负责把 message 逐条显示出来。
export function createDraft(leadId) {
  return request(`/leads/${leadId}/draft`, { method: 'POST' })
}

export function confirmDraft(leadId, draftId, payload) {
  return request(`/leads/${leadId}/drafts/${draftId}/confirm`, jsonOptions('POST', payload))
}

export function discardDraft(leadId, draftId) {
  return request(`/leads/${leadId}/drafts/${draftId}`, { method: 'DELETE' })
}

export { OFFLINE_MESSAGE }
