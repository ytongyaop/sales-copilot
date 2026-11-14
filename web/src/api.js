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

export { OFFLINE_MESSAGE }
