// 显示用词表：只做「机器取值 → 中文」的翻译。
// 判定（能不能推进、要不要理由、档位怎么算）一律在后端，这里不存任何规则。

// 来源下拉：与后端 ALLOWED_SOURCES 一一对应。
export const SOURCES = [
  { value: 'referral', label: '转介绍' },
  { value: 'inbound', label: '主动咨询' },
  { value: 'outbound', label: '主动触达' },
  { value: 'event', label: '活动' },
  { value: 'other', label: '其他' },
]

// 异议下拉：与后端 ALLOWED_OBJECTIONS 一一对应。
export const OBJECTIONS = [
  { value: 'price', label: '价格' },
  { value: 'time', label: '时间' },
  { value: 'effect', label: '效果' },
  { value: 'trust', label: '信任' },
  { value: 'other', label: '其它' },
]

// 事实字段的中文名：字段名本身是机器契约（与后端 ALLOWED_FACT_FIELDS 对齐）。
export const FACT_FIELDS = [
  { value: 'decision_maker', label: '决策人' },
  { value: 'need', label: '核心需求' },
  { value: 'objection', label: '异议' },
  { value: 'amount', label: '金额' },
  { value: 'appointment_at', label: '约定时间' },
]

// 信息卡分区（后端 CARD_SECTIONS）的显示名；模板自己给了 label 时以后端返回的为准。
export const SECTION_LABELS = {
  basic: '基础信息',
  needs: '需求与痛点',
  decision: '决策流程与预算',
  followup: '跟进记录',
}

// 常见字段中文名；不认识的字段原样显示字段名，不猜它想表达什么。
export const FIELD_LABELS = {
  company: '公司',
  industry: '所属行业',
  size: '规模',
  name: '姓名',
  contact: '联系方式',
  channel: '沟通渠道',
  need: '核心需求',
  pain_point: '痛点',
  pain_strength: '痛点强度',
  budget: '预算',
  budget_range: '预算区间',
  amount: '金额',
  decision_maker: '决策人',
  approval_flow: '审批流程',
  our_fit: '我方匹配点',
  objection: '异议',
  appointment_at: '约定时间',
  next_followup_at: '下次跟进时间',
  next_step: '下一步动作',
  customer_promise: '客户承诺',
  value_provided: '本次提供的价值',
  conclusion: '结论',
  source: '来源',
}

// 来源徽标：后端只产这三种来源，前端只负责翻译。
export const SOURCE_LABELS = { llm: 'AI', manual: '人工', rule: '规则' }

// 归属打分来源（`domain/matching.py` 的 scorer）：只翻译，不重算任何分数。
export const SCORER_LABELS = {
  exact: '精确命中',
  fuzzy: '字符串相近',
  vector: '向量相近',
  none: '未命中',
}

export function scorerLabel(value) {
  return SCORER_LABELS[value] || value || '未命中'
}

// 待办 / 货架 / 详情页共用的档位文案：`needs_info` 是「待补全」，不是第三个正档。
export const GRADE_LABELS = {
  A: 'A 类',
  B: 'B 类',
  C: 'C 类',
  needs_info: '待补全',
  unknown: '未分级',
}

export function sourceLabel(value) {
  const hit = SOURCES.find((item) => item.value === value)
  return hit ? hit.label : value
}

export function objectionLabel(value) {
  const hit = OBJECTIONS.find((item) => item.value === value)
  return hit ? hit.label : value
}

export function sectionLabel(key, fallback) {
  return fallback || SECTION_LABELS[key] || key
}

export function fieldLabel(key) {
  return FIELD_LABELS[key] || key
}

export function factFieldLabel(field) {
  const hit = FACT_FIELDS.find((item) => item.value === field)
  return hit ? hit.label : fieldLabel(field)
}

// 模型产出的事实带上模型名（AI·fake / AI·deepseek），免得把假模型的输出当成真结论。
export function factSourceLabel(fact) {
  if (fact.source === 'llm') return fact.model ? `AI·${fact.model}` : 'AI'
  return SOURCE_LABELS[fact.source] || fact.source || '未填'
}

export function gradeLabel(value) {
  return GRADE_LABELS[value] || value
}

// 徽标上的短文案：正档直接给字母，`needs_info` / `unknown` 给中文，人不至于看到一串英文键。
export function gradeBadge(value) {
  if (value === 'A' || value === 'B' || value === 'C') return value
  return GRADE_LABELS[value] || value || '—'
}

export function gradeClass(value) {
  return value === 'A' || value === 'B' || value === 'C' ? value.toLowerCase() : 'pending'
}

export function formatTime(iso) {
  if (!iso) return '-'
  return iso.replace('T', ' ').slice(0, 16)
}

export function formatRate(value) {
  if (typeof value !== 'number' || Number.isNaN(value)) return '-'
  return `${(value * 100).toFixed(1)}%`
}

export function formatPercent(value) {
  if (typeof value !== 'number' || Number.isNaN(value)) return '-'
  return `${Math.round(value * 100)}%`
}
