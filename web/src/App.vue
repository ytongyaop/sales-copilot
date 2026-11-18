<script setup>
import { ref } from 'vue'
import LeadsPage from './pages/LeadsPage.vue'
import SortingDesk from './pages/SortingDesk.vue'

// 页签只是显示切换：两个页面都保持挂载（v-show），来回切不丢已展开的行、也不丢刚投的料。
// 不引路由：一个页签两个组件就够，路由等真有第二个入口再说。
const tab = ref('sorting')
</script>

<template>
  <main class="page">
    <nav class="tabs">
      <button type="button" class="tab" :class="{ active: tab === 'sorting' }" @click="tab = 'sorting'">
        分拣台
      </button>
      <button type="button" class="tab" :class="{ active: tab === 'leads' }" @click="tab = 'leads'">
        线索
      </button>
    </nav>

    <SortingDesk v-show="tab === 'sorting'" :active="tab === 'sorting'" />
    <LeadsPage v-show="tab === 'leads'" :active="tab === 'leads'" />
  </main>
</template>

<style scoped>
.tabs {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid #e1e4e8;
  margin-bottom: 16px;
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
</style>
