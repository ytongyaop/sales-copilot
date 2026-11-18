<script setup>
import { onMounted, ref, watch } from 'vue'
import { getHealth, listTemplates } from '../api'

// 设置（只读）：当前运行模式、模型名、模板清单（阶段顺序 + 每块必填项）。
// 真/假模型由后端的 SALESCOPILOT_LIVE 决定，前端没有开关——页面上不做一个点了没用的按钮，
// 只把「现在是什么模式、怎么切换」写清楚。
const props = defineProps({
  active: { type: Boolean, default: false },
  health: { type: Object, default: null },
})

const templates = ref([])
const health = ref(props.health)
const error = ref('')

async function load() {
  error.value = ''
  try {
    const [templateItems, healthBody] = await Promise.all([listTemplates(), getHealth()])
    templates.value = templateItems
    health.value = healthBody
  } catch (err) {
    error.value = err.message
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
    <p v-if="error" class="error">{{ error }}</p>

    <section class="card">
      <h2>运行模式</h2>
      <p>
        <span
          v-if="health"
          class="mode"
          :class="health.mode === 'live' ? 'live' : 'fake'"
        >
          {{ health.mode === 'live' ? '模式：真模型' : '模式：假模型（演示数据不可信）' }}
        </span>
        <span v-else class="muted">模式未知（后端未连接）</span>
      </p>
      <p v-if="health" class="muted">
        对话模型：{{ health.models.chat }}　向量模型：{{ health.models.embed }}
      </p>
      <p class="hint">
        切换模式要改后端环境变量 `SALESCOPILOT_LIVE`（1 = 真模型，缺 key 会启动失败）并重启服务：
        模式是进程级配置，前端不提供热开关，免得同一份数据一半真一半假。
      </p>
    </section>

    <section class="card">
      <h2>模板</h2>
      <p class="hint">阶段顺序与每块的必填项都来自后端 `GET /templates`，前端只显示、不判定。</p>
      <div v-for="item in templates" :key="item.key" class="template">
        <h3>{{ item.name }} <span class="muted">{{ item.key }}</span></h3>
        <ol class="flow">
          <li v-for="stage in item.flow" :key="stage">
            {{ (item.labels && item.labels[stage]) || stage }}
            <span class="muted">（{{ stage }}）</span>
            <span v-if="(item.requires[stage] || []).length" class="muted">
              必填：{{ item.requires[stage].join('、') }}
            </span>
          </li>
        </ol>
      </div>
      <p v-if="templates.length === 0" class="hint">模板清单为空（后端未连接或模板目录为空）。</p>
    </section>
  </div>
</template>

<style scoped>
.template {
  padding: 10px 0;
  border-bottom: 1px solid #eef1f4;
}

.template:last-child {
  border-bottom: 0;
}

h3 {
  margin: 0 0 6px;
  font-size: 14px;
}

.flow {
  margin: 0;
  padding-left: 20px;
  font-size: 13px;
  line-height: 1.9;
}
</style>
