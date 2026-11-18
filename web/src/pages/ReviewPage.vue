<script setup>
import { onMounted, ref, watch } from 'vue'
import { getReview } from '../api'
import { formatTime } from '../labels'

// 周复盘（只读）：所有数字、明细与问题都由后端 `GET /review` 算好，前端只排版。
// 作答（写 preference_samples）留到 T009b，这里不提供写入入口，也就不会出现「半条作答」。
const props = defineProps({ active: { type: Boolean, default: false } })

const review = ref(null)
const error = ref('')
const loading = ref(false)

async function load() {
  loading.value = true
  error.value = ''
  try {
    review.value = await getReview()
  } catch (err) {
    review.value = null
    error.value = err.message
  } finally {
    loading.value = false
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
    <p v-if="error" class="error">{{ error }}</p>
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

      <section class="card">
        <h2>本周校准问题</h2>
        <p class="hint">作答入口在 T009b 接入（写进偏好样本），这里先只读显示问题本身。</p>
        <ol class="plain">
          <li v-for="item in review.questions" :key="item.key">{{ item.text }}</li>
        </ol>
      </section>
    </template>
  </div>
</template>

<style scoped>
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
</style>
