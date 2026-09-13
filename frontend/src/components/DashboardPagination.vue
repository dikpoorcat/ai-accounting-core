<script setup lang="ts">
import type { DashboardPage } from "../api/dashboardContracts";
withDefaults(defineProps<{ page?: DashboardPage; loaded: number; loading?: boolean; error?: string; compact?: boolean; itemLabel?: string }>(), {
  compact: false,
  itemLabel: "项",
});
defineEmits<{ more: []; retry: [] }>();
</script>

<template>
  <div v-if="error || (page && (!compact || page.has_more))" class="dashboard-pagination" :aria-busy="loading">
    <span v-if="page && (!compact || page.has_more)">
      <template v-if="compact">已加载 {{ loaded }} / {{ page.filtered_count }} {{ itemLabel }}</template>
      <template v-else>完整总计 {{ page.total_count }} 项 · 筛选总计 {{ page.filtered_count }} 项 · 已加载 {{ loaded }} 项</template>
    </span>
    <p v-if="error" role="alert">{{ error }} <button type="button" :disabled="loading" @click="$emit('retry')">重试读取</button></p>
    <button v-else-if="page?.has_more" type="button" :disabled="loading" @click="$emit('more')">{{ loading ? "加载中…" : "加载更多" }}</button>
    <span v-else-if="page && loaded > 0">本次筛选已全部加载</span>
  </div>
</template>

<style scoped>
.dashboard-pagination { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 0; color: var(--muted, #68766c); font-size: 13px; }
button { padding: 7px 12px; border: 1px solid currentColor; border-radius: 8px; background: transparent; color: inherit; cursor: pointer; }
button:disabled { cursor: wait; opacity: .6; }
button:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
@media (max-width: 720px) { button { min-height: 44px; } }
</style>
