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

// ---------------------------------------------------------------- 公海 / 归属

export function listPool() {
  return request('/leads/pool')
}

// 认领：后端判定「能不能认领」，前端只发负责人名字；已被别人占了会回 409 + 中文提示。
export function claimLead(leadId, owner) {
  return request(`/leads/${leadId}/claim`, jsonOptions('POST', { owner }))
}

export function releaseLead(leadId) {
  return request(`/leads/${leadId}/release`, { method: 'POST' })
}

// 提醒：阈值判定全在后端（模板 thresholds），前端只按 level 上色。
export function listReminders() {
  return request('/reminders')
}

// 仪表盘：后端算好的指标（口径写在 domain/dashboard.py），前端只负责显示，不复制计算规则。
export function getDashboard() {
  return request('/dashboard')
}

// CSV 导入：原文交给后端解析与去重（同一联系方式幂等跳过），前端不做第二套规则。
export function importLeads(csvText, defaultOwner) {
  return request(
    '/leads/import',
    jsonOptions('POST', { csv_text: csvText, default_owner: defaultOwner || null }),
  )
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

// 补线索字段：只发「要补的那几项」，键是否合法、值是否为空全由后端判定。
export function patchLeadFields(leadId, fields) {
  return request(`/leads/${leadId}/fields`, jsonOptions('PATCH', { fields }))
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

// ---------------------------------------------------------------- 分拣单

// 投料：归属判不判得出来全由后端漏斗决定（精确键 / 灰区 / 新客户），
// 前端只负责把原文、来源和「已指定的客户」如实发过去。
export function sortContent({ content, source, leadId }) {
  return request('/sorting', jsonOptions('POST', { content, source, lead_id: leadId || null }))
}

// 列表：`status` 留空就是全部；取值合不合法由后端判（不认识的状态回 422 + 中文原因）。
export function listBatches(status) {
  const query = status ? `?status=${encodeURIComponent(status)}` : ''
  return request(`/sorting/batches${query}`)
}

export function getBatch(id) {
  return request(`/sorting/batches/${id}`)
}

// 处置：accept / fix / reject 的合法性（状态、理由、目标客户）全在后端判；
// 成功回 `{batch, applied, skipped}`，前端只显示写了几条、跳过什么。
export function confirmBatch(id, { action, lead_id, reason }) {
  return request(
    `/sorting/${id}/confirm`,
    jsonOptions('POST', { action, lead_id: lead_id || null, reason: reason || null }),
  )
}

// 认领：`{lead_id}` 挂已有客户，`{create_name}` 新建；两个都给或都不给由后端挡。
export function claimBatch(id, payload) {
  return request(`/sorting/${id}/claim`, jsonOptions('POST', payload))
}

// 复核只是「建议」：结果不落库，人点了采用才走 claim。
export function reviewBatch(id) {
  return request(`/sorting/${id}/review`)
}

// ---------------------------------------------------------------- 今日待办

// 待办是后端排好的顺序（分级 → 到期 → 缺项 → 最久未联系），前端只负责显示与跳转。
export function listTodo() {
  return request('/todo')
}

// ---------------------------------------------------------------- 客户货架 / 详情

// 货架：每家一张小卡（档位 / 覆盖度 / 缺项数 / 下次约定）一次取完，不在浏览器里重算。
export function listShelf() {
  return request('/leads/shelf')
}

export function getLeadCard(leadId) {
  return request(`/leads/${leadId}/card`)
}

export function getGradingLatest(leadId) {
  return request(`/leads/${leadId}/grading/latest`)
}

export function listGradingHistory(leadId) {
  return request(`/leads/${leadId}/grading/history`)
}

// 重新评估：后端按规则出一次建议并写一行留痕（与详情页只读的 latest 不同）。
export function proposeGrading(leadId) {
  return request(`/leads/${leadId}/grading/propose`, { method: 'POST' })
}

// 改判：能不能改、要不要理由全由后端判（422 时把 issues 逐条显示）。
export function decideGrading(leadId, payload) {
  return request(`/leads/${leadId}/grading/decide`, jsonOptions('POST', payload))
}

// ---------------------------------------------------------------- 周复盘

export function getReview() {
  return request('/review')
}

export function saveReviewAnswers(answers) {
  return request('/review/answers', jsonOptions('POST', { answers }))
}

export { OFFLINE_MESSAGE }
