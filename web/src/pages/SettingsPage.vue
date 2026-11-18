<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { getSettings, importLeads, retryOnce } from '../api'

// 设置页：只读展示 + 两件能做的事（导入 CSV、看分级模式说明）。
// 阈值 / 权重 / 档位下限全部原样来自后端 `GET /settings`（源头是模板 YAML），
// 这里只翻译成人话：前端不判断「多少分算 A」，也不知道「哪些字段必填」。
const props = defineProps({
  active: { type: Boolean, default: false },
  health: { type: Object, default: null },
})

// 展示用词表：认不出的键原样显示键名，不猜它想表达什么。
const THRESHOLD_META = {
  first_response_minutes: { label: '首响时限', unit: ' 分钟' },
  feedback_window_hours: { label: '反馈窗口', unit: ' 小时' },
  stale_followup_days: { label: '久未跟进', unit: ' 天' },
  zombie_ratio_max: { label: '僵尸线索占比上限', unit: '' },
}

const DIM_LABELS = {
  amount: '金额',
  urgency: '紧迫度',
  clarity: '决策清晰度',
  fit: '需求匹配度',
}

const BAND_LABELS = {
  a_min: 'A 类下限',
  b_min: 'B 类下限',
}

const MODE_LABELS = {
  propose: '先给建议，等人确认后才生效',
  auto: '直接采纳规则结论，不再逐条确认',
}

const settings = ref(null)
const error = ref('')
const loading = ref(false)

// CSV 导入（T009b 从货架页搬过来）：原文交给后端解析与去重，前端不做第二套规则。
const importing = ref(false)
const importResult = ref(null)
const importError = ref('')

const mode = computed(() => {
  if (settings.value) return settings.value.mode
  return props.health ? props.health.mode : ''
})

const models = computed(() => {
  if (settings.value) return settings.value.models
  return props.health ? props.health.models : null
})

const templates = computed(() => (settings.value ? settings.value.templates : []))

async function load() {
  loading.value = true
  error.value = ''
  try {
    settings.value = await retryOnce(getSettings)
  } catch (err) {
    settings.value = null
    error.value = err.message
  } finally {
    loading.value = false
  }
}

function thresholdEntries(template) {
  const thresholds = template.thresholds || {}
  return Object.entries(thresholds).map(([key, value]) => {
    const meta = THRESHOLD_META[key] || { label: key, unit: '' }
    return { key, label: meta.label, text: `${value}${meta.unit}` }
  })
}

function weightEntries(template) {
  const weights = (template.grading && template.grading.weights) || {}
  return Object.entries(weights).map(([key, value]) => ({
    key,
    label: DIM_LABELS[key] || key,
    text: String(value),
  }))
}

function bandEntries(template) {
  const bands = (template.grading && template.grading.bands) || {}
  return Object.entries(bands).map(([key, value]) => ({
    key,
    label: BAND_LABELS[key] || key,
    text: String(value),
  }))
}

function defaultMode(template) {
  const raw = (template.grading && template.grading.default_mode) || ''
  if (!raw) return '未配置'
  return `${raw}（${MODE_LABELS[raw] || '未登记的模式'}）`
}

function readFileText(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(new Error('读取文件失败，请确认这是一个文本 CSV 文件'))
    reader.readAsText(file, 'utf-8')
  })
}

async function onImportFile(event) {
  const input = event.target
  const file = input.files && input.files[0]
  if (!file) return

  importError.value = ''
  importResult.value = null
  importing.value = true
  try {
    const text = await readFileText(file)
    importResult.value = await importLeads(text, null)
  } catch (err) {
    importError.value = err.message || '导入失败，请重试'
  } finally {
    importing.value = false
    input.value = ''
  }
}

watch(
  () => props.active,
  async (on) => {
    if (!on) return
    await load()
  },
)

onMounted(load)
</script>

<template>
  <div class="settings">
    <h2 class="page-title">设置</h2>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="loading && !settings" class="hint">加载中…</p>

    <section class="card">
      <h2>运行模式</h2>
      <p>
        <span v-if="mode" class="mode" :class="mode === 'live' ? 'live' : 'fake'">
          {{ mode === 'live' ? '模式：真模型' : '模式：假模型（演示数据不可信）' }}
        </span>
        <span v-else class="muted">模式未知（后端未连接）</span>
      </p>
      <p v-if="models" class="muted">
        对话模型：{{ models.chat }}　向量模型：{{ models.embed }}
      </p>
      <p class="hint">
        切换模式要改后端环境变量 `SALESCOPILOT_LIVE`（1 = 真模型，缺 key 会启动失败）并重启服务：
        模式是进程级配置，前端不提供热开关，免得同一份数据一半真一半假。
      </p>
    </section>

    <section class="card">
      <h2>分级模式</h2>
      <p class="hint">
        两档模式都是「规则算分 + 人决定」，区别只在<strong>要不要逐条确认</strong>，模型都不参与打分：
      </p>
      <ul class="plain">
        <li>
          <strong>propose（先提议）</strong>：系统按模板权重算出一个档位并留一行痕，等人确认或改判；
          默认用这个，改判本身就是要复盘的东西。
        </li>
        <li>
          <strong>auto（直接采纳）</strong>：规则结论当场生效，不再逐条确认；
          只在规则已经稳定、团队认可阈值时用，否则错了没人拦。
        </li>
      </ul>
      <p class="hint">每份模板的默认模式见下方「模板」里的 `default_mode`；这里只说明差异，不做开关。</p>
    </section>

    <section class="card">
      <h2>模板 <span class="muted">（{{ templates.length }} 份）</span></h2>
      <p class="hint">阈值、权重与档位下限都来自后端模板 YAML，前端只显示、不判定。</p>

      <div v-for="item in templates" :key="item.key" class="template">
        <h3>{{ item.name }} <span class="muted">{{ item.key }}</span></h3>

        <div class="template-grid">
          <div>
            <h4>阈值（thresholds）</h4>
            <ul class="plain">
              <li v-for="entry in thresholdEntries(item)" :key="entry.key">
                {{ entry.label }}：<strong>{{ entry.text }}</strong>
                <span class="muted">（{{ entry.key }}）</span>
              </li>
              <li v-if="thresholdEntries(item).length === 0" class="muted">这份模板没有阈值段。</li>
            </ul>
          </div>

          <div>
            <h4>分级权重（grading.weights）</h4>
            <ul class="plain">
              <li v-for="entry in weightEntries(item)" :key="entry.key">
                {{ entry.label }}：<strong>{{ entry.text }}</strong>
              </li>
              <li v-if="weightEntries(item).length === 0" class="muted">没有配置权重。</li>
            </ul>
          </div>

          <div>
            <h4>分档下限（grading.bands）</h4>
            <ul class="plain">
              <li v-for="entry in bandEntries(item)" :key="entry.key">
                {{ entry.label }}：<strong>{{ entry.text }}</strong>
              </li>
              <li v-if="bandEntries(item).length === 0" class="muted">没有配置分档下限。</li>
            </ul>
          </div>

          <div>
            <h4>卡面</h4>
            <ul class="plain">
              <li>字段数：<strong>{{ item.card_fields }}</strong> 项</li>
              <li>默认分级模式：{{ defaultMode(item) }}</li>
            </ul>
          </div>
        </div>
      </div>

      <p v-if="templates.length === 0" class="hint">模板清单为空（后端未连接或模板目录为空）。</p>
    </section>

    <section class="card">
      <h2>导入 CSV</h2>
      <p class="hint">
        支持带表头（姓名 / 联系方式 / 来源 / 归属），也支持无表头（列序：姓名,联系方式,来源,归属）；
        同一联系方式只会导入一次（去重与字段校验都在后端）。
      </p>
      <div class="row-actions">
        <input type="file" accept=".csv,text/csv" :disabled="importing" @change="onImportFile" />
        <span v-if="importing" class="muted">导入中…</span>
      </div>
      <p v-if="importError" class="error">{{ importError }}</p>
      <div v-if="importResult" class="import-result">
        <p>导入 {{ importResult.imported }} 条，跳过 {{ importResult.skipped.length }} 条。</p>
        <details v-if="importResult.skipped.length">
          <summary class="muted">查看跳过原因</summary>
          <ul class="skip-list">
            <li v-for="(item, index) in importResult.skipped" :key="index" class="muted">
              第 {{ item.row }} 行：{{ item.reason }}
            </li>
          </ul>
        </details>
      </div>
    </section>
  </div>
</template>

<style scoped>
.page-title {
  font-size: 20px;
  margin: 0 0 12px;
}

.template {
  padding: 12px 0;
  border-bottom: 1px solid #eef1f4;
}

.template:last-child {
  border-bottom: 0;
}

h3 {
  margin: 0 0 8px;
  font-size: 15px;
}

h4 {
  margin: 0 0 4px;
  font-size: 13px;
  color: #57606a;
}

/* 四块并排：窄屏自动折成两列/一列，读起来不用左右横扫。 */
.template-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px;
}

.plain {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.8;
}

.import-result {
  margin-top: 10px;
  padding: 10px 12px;
  border: 1px solid #e1e4e8;
  border-radius: 6px;
  background: #fafbfc;
  font-size: 14px;
}

.skip-list {
  margin: 6px 0 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.7;
}
</style>
