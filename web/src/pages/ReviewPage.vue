<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { getReview, retryOnce, saveReviewAnswers } from '../api'
import { formatTime } from '../labels'

// 周复盘：所有数字、明细与问题都由后端 `GET /review` 算好，前端只排版。
// 作答写进 `preference_samples`（`POST /review/answers`）：问题编号合法性与「至少 2 个字」
// 的判定都在后端（`domain/review.py`），前端只是把明显填不了的输入先挡住，省一次白跑的请求。
const props = defineProps({ active: { type: Boolean, default: false } })

// 与后端 `MIN_ANSWER_LEN` 对齐，只用于「还不够长」这条前端提示；判定权仍在后端。
const MIN_ANSWER_LEN = 2

const review = ref(null)
const error = ref('')
const loading = ref(false)

// 三个问题的草稿：**键从后端给的 questions 里取**，前端不写死问题编号。
const draft = ref({})
const submitting = ref(false)
const notice = ref('')

const questions = computed(() => (review.value ? review.value.questions : []))

const filled = computed(() =>
  questions.value.filter((item) => (draft.value[item.key] || '').trim() !== ''),
)

// 填了但不到两个字的那几条：只提示、不提交（后端同样会 422）。
const tooShort = computed(() =>
  filled.value.filter((item) => (draft.value[item.key] || '').trim().length < MIN_ANSWER_LEN),
)

const canSubmit = computed(() => filled.value.length > 0 && tooShort.value.length === 0)

function resetDraft() {
  draft.value = Object.fromEntries(questions.value.map((item) => [item.key, '']))
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    // 首屏那一次请求最容易撞上后端还没就绪：失败自动重试一次（见 `api.js`）。
    review.value = await retryOnce(getReview)
    if (!review.value.answered) resetDraft()
  } catch (err) {
    review.value = null
    error.value = err.message
  } finally {
    loading.value = false
  }
}

async function submitAnswers() {
  if (!canSubmit.value || submitting.value) return
  submitting.value = true
  error.value = ''
  notice.value = ''
  try {
    const body = await saveReviewAnswers(
      filled.value.map((item) => ({ key: item.key, answer: draft.value[item.key].trim() })),
    )
    notice.value = `已保存 ${body.saved} 条作答`
    // 重新取一次复盘：`answered` 与 `answers` 都由后端算，页面不自己拼「本周已作答」。
    await load()
  } catch (err) {
    error.value = err.message
  } finally {
    submitting.value = false
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
  <div class="review-page">
    <h2 class="page-title">复盘</h2>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="notice" class="notice">{{ notice }}</p>
    <p v-if="loading" class="hint">加载中…</p>

    <template v-if="review">
      <section class="card">
        <h2>
          本周（{{ formatTime(review.window_start) }} → {{ formatTime(review.window_end) }}）
          <span class="muted">{{ review.answered ? '· 本周已校准' : '· 本周还没答校准问题' }}</span>
        </h2>
        <div class="stats">
          <div class="stat">
            <span class="stat-value">{{ review.new_leads }}</span>
            <span class="stat-label">新增客户</span>
          </div>
          <div class="stat">
            <span class="stat-value">{{ review.advanced }}</span>
            <span class="stat-label">推进次数</span>
          </div>
          <div class="stat">
            <span class="stat-value">{{ review.closed }}</span>
            <span class="stat-label">关闭客户</span>
          </div>
          <div class="stat">
            <span class="stat-value">{{ review.stuck.length }}</span>
            <span class="stat-label">卡住的客户</span>
          </div>
          <div class="stat">
            <span class="stat-value">{{ review.needs_info_leads }}</span>
            <span class="stat-label">待补全档位</span>
          </div>
          <div class="stat">
            <span class="stat-value">{{ review.no_value_followups.length }}</span>
            <span class="stat-label">无价值跟进</span>
          </div>
        </div>
      </section>

      <section class="card">
        <h2>卡点</h2>
        <p v-if="review.stuck.length === 0" class="hint">本周没有被卡住的客户。</p>
        <ul v-else class="plain">
          <li v-for="item in review.stuck" :key="item.lead_id">
            <span class="risk-dot" :class="item.level"></span>
            <strong>{{ item.lead_name }}</strong> {{ item.message }}
          </li>
        </ul>
      </section>

      <section class="card">
        <h2>缺项前三</h2>
        <p v-if="review.missing_top.length === 0" class="hint">没有集中缺项。</p>
        <ul v-else class="plain">
          <li v-for="item in review.missing_top" :key="item.section">
            {{ item.section_label }} <span class="muted">{{ item.count }} 家缺</span>
          </li>
        </ul>
      </section>

      <section class="card">
        <h2>本周改档</h2>
        <p v-if="review.grading_changes.length === 0" class="hint">本周没有人工改档。</p>
        <table v-else>
          <thead>
            <tr>
              <th>客户</th>
              <th>改档</th>
              <th>理由</th>
              <th>时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(item, index) in review.grading_changes" :key="index">
              <td>{{ item.lead_name }}</td>
              <td>{{ item.from || '—' }} → {{ item.to }}</td>
              <td>{{ item.reason || '—' }}</td>
              <td>{{ formatTime(item.at) }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="card">
        <h2>无价值跟进</h2>
        <p v-if="review.no_value_followups.length === 0" class="hint">本周每次跟进都给了客户新东西。</p>
        <ul v-else class="plain">
          <li v-for="(item, index) in review.no_value_followups" :key="index">
            <strong>{{ item.lead_name }}</strong> · {{ item.conclusion }}
            <span class="muted">{{ formatTime(item.at) }}</span>
          </li>
        </ul>
      </section>

      <!-- 已作答：只读回放这周写了什么；想改答案要等下一个窗口（样本表只追加）。 -->
      <section v-if="review.answered" class="card">
        <h2>本周已作答</h2>
        <p v-if="review.answers.length === 0" class="hint">
          本周已经答过，但没有读到窗口内的作答记录。
        </p>
        <ul v-else class="answers">
          <li v-for="item in review.answers" :key="`${item.key}-${item.at}`">
            <p class="question-text">{{ item.question }}</p>
            <p class="answer-text">{{ item.answer }}</p>
            <p class="muted when">答于 {{ formatTime(item.at) }}</p>
          </li>
        </ul>
      </section>

      <section v-else class="card">
        <h2>本周校准问题</h2>
        <p class="hint">
          至少答一条，每条 ≥ {{ MIN_ANSWER_LEN }} 个字；作答会写进偏好样本，下周复盘直接用上。
        </p>
        <form class="answers-form" @submit.prevent="submitAnswers">
          <label v-for="item in questions" :key="item.key" class="question">
            <span class="question-text">{{ item.text }}</span>
            <textarea
              v-model="draft[item.key]"
              rows="2"
              placeholder="写一句你自己的判断：哪一家、为什么"
            ></textarea>
          </label>
          <p v-if="tooShort.length > 0" class="hint warn">
            还有 {{ tooShort.length }} 条不到 {{ MIN_ANSWER_LEN }} 个字，补全后才能提交。
          </p>
          <div class="row-actions">
            <button type="submit" :disabled="!canSubmit || submitting">
              {{ submitting ? '提交中…' : '提交作答' }}
            </button>
            <span class="muted">已填 {{ filled.length }} / {{ questions.length }} 条</span>
          </div>
        </form>
      </section>
    </template>
  </div>
</template>

<style scoped>
.page-title {
  font-size: 20px;
  margin: 0 0 12px;
}

.stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 10px;
}

.stat {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px 12px;
  border: 1px solid #e1e4e8;
  border-radius: 6px;
  background: #fafbfc;
}

.stat-value {
  font-size: 20px;
  font-weight: 600;
}

.stat-label {
  font-size: 12px;
  color: #57606a;
}

.plain {
  margin: 0;
  padding-left: 18px;
  font-size: 14px;
  line-height: 1.9;
}

.risk-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 6px;
}

.risk-dot.red {
  background: #cf222e;
}

.risk-dot.yellow {
  background: #d4a72c;
}

.risk-dot.gray {
  background: #8a939d;
}

.answers-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 12px;
}

.question {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.question-text {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}

.question textarea {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid #d0d7de;
  border-radius: 6px;
  font-family: inherit;
  font-size: 14px;
  color: #1f2328;
  resize: vertical;
}

/* 已作答：一问一答上下排，答案比问题重一点，回看时先看到自己写了什么。 */
.answers {
  margin: 0;
  padding: 0;
  list-style: none;
}

.answers > li {
  padding: 10px 0;
  border-bottom: 1px solid #eef1f4;
}

.answers > li:last-child {
  border-bottom: 0;
}

.answer-text {
  margin: 4px 0 0;
  font-size: 14px;
  white-space: pre-wrap;
}

.when {
  margin: 4px 0 0;
  font-size: 12px;
}

.warn {
  color: #b35900;
}

.notice {
  margin: 12px 0 0;
  padding: 10px 12px;
  border: 1px solid #b7e2c4;
  border-radius: 6px;
  background: #e7f6ec;
  color: #17663a;
  font-size: 14px;
}
</style>
