<script setup>
import { onMounted, ref } from 'vue'
import { getHealth } from './api'
import LeadDetail from './pages/LeadDetail.vue'
import LeadsShelf from './pages/LeadsShelf.vue'
import ReviewPage from './pages/ReviewPage.vue'
import SettingsPage from './pages/SettingsPage.vue'
import SortingDesk from './pages/SortingDesk.vue'

// 四个页签 + 一个详情视图，不引路由：
// - 页签用 `v-show` 保持挂载，切走再回来不丢已展开的行、也不丢刚投的料；切回时各页自己重取数据；
// - 「客户详情」不是页签：从「客户」或「分拣台待办」点进来，点「返回」回到原来那一页。
const TABS = [
  { key: 'sorting', label: '分拣台' },
  { key: 'leads', label: '客户' },
  { key: 'review', label: '复盘' },
  { key: 'settings', label: '设置' },
]

const tab = ref('sorting')
const detailLeadId = ref('')
const health = ref(null)

// 模式徽标只在最外层取一次：四个页签看到的必须是同一个「真/假模型」，
// 否则同一块屏幕上会同时出现两个互相矛盾的模式说法。
// 首屏门禁：**先确认后端活着，再挂载各页**。
// 各页都在 `onMounted` 里自己取数，如果和 `/health` 同时发出去，后端（尤其是带 --reload 的
// uvicorn）还没就绪的那 1–3 秒里第一批请求就会失败，页面停在「0 家 / 空列表」且不再自愈，
// 必须切页签才会重取。先问一次 `/health` 把这批请求挡在后面，竞态就没有了。
const ready = ref(false)
const bootError = ref('')

// 后端刚起来的那几秒 `/health` 也会连不上：最多试 6 次（间隔 500ms），
// 仍失败才把「服务未连接」显示出来并留一个「重试」按钮。
const BOOT_ATTEMPTS = 6
const BOOT_INTERVAL_MS = 500

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

async function boot() {
  bootError.value = ''
  ready.value = false
  for (let attempt = 1; attempt <= BOOT_ATTEMPTS; attempt += 1) {
    try {
      health.value = await getHealth()
      ready.value = true
      return
    } catch (err) {
      health.value = null
      if (attempt === BOOT_ATTEMPTS) bootError.value = err.message
      else await wait(BOOT_INTERVAL_MS)
    }
  }
}

function switchTab(key) {
  detailLeadId.value = ''
  tab.value = key
}

function openLead(leadId) {
  if (leadId) detailLeadId.value = leadId
}

function closeDetail() {
  detailLeadId.value = ''
}

// `active` = 这一页此刻正在被看到（既没开详情、又是当前页签）：各页据它决定要不要重取数据。
function isActive(key) {
  return detailLeadId.value === '' && tab.value === key
}

onMounted(boot)
</script>

<template>
  <main class="page">
    <header class="masthead">
      <div class="brand">
        <h1>销售副驾</h1>
        <span
          v-if="health"
          class="mode"
          :class="health.mode === 'live' ? 'live' : 'fake'"
          :title="health.models ? `对话模型：${health.models.chat}；向量模型：${health.models.embed}` : ''"
        >
          {{ health.mode === 'live' ? '模式：真模型' : '模式：假模型（演示数据不可信）' }}
        </span>
      </div>

      <nav class="tabs">
        <button
          v-for="item in TABS"
          :key="item.key"
          type="button"
          class="tab"
          :class="{ active: isActive(item.key) }"
          :disabled="!ready"
          @click="switchTab(item.key)"
        >
          {{ item.label }}
        </button>
      </nav>
    </header>

    <p v-if="bootError" class="error boot">
      {{ bootError }}
      <button type="button" class="link-button" @click="boot">重试</button>
    </p>
    <p v-else-if="!ready" class="hint boot">正在连接后端…</p>

    <!-- 后端确认可用之后才挂载各页：它们的首屏请求不会再跑在还没就绪的服务前面。 -->
    <template v-if="ready">
      <SortingDesk
        v-show="isActive('sorting')"
        :active="isActive('sorting')"
        @open-lead="openLead"
      />
      <LeadsShelf v-show="isActive('leads')" :active="isActive('leads')" @open-lead="openLead" />
      <ReviewPage v-show="isActive('review')" :active="isActive('review')" />
      <SettingsPage
        v-show="isActive('settings')"
        :active="isActive('settings')"
        :health="health"
      />

      <LeadDetail
        v-if="detailLeadId"
        :key="detailLeadId"
        :lead-id="detailLeadId"
        @back="closeDetail"
      />
    </template>
  </main>
</template>

<style scoped>
.masthead {
  display: flex;
  align-items: baseline;
  gap: 16px;
  flex-wrap: wrap;
  border-bottom: 1px solid #e1e4e8;
  margin-bottom: 16px;
  padding-bottom: 4px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.brand h1 {
  margin: 0;
  font-size: 22px;
  letter-spacing: 0.5px;
}

.tabs {
  display: flex;
  gap: 4px;
  margin-left: auto;
}

/* 页签是「文字 + 下划线」，不用全局按钮的蓝底白字外观。 */
.tab {
  padding: 8px 12px;
  border: 0;
  border-bottom: 2px solid transparent;
  border-radius: 0;
  background: none;
  color: #57606a;
  font-size: 15px;
}

.tab.active {
  border-bottom-color: #1f6feb;
  color: #1f6feb;
  font-weight: 600;
}

/* 连接后端那一条：不摆成错误，只在还没就绪时占一行，免得页面看起来是空的。 */
.boot {
  margin: 8px 0 16px;
}
</style>
