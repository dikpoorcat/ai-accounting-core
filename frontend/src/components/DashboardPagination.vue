<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue";
import type { DashboardPage } from "../api/dashboardContracts";
const props = withDefaults(defineProps<{ page?: DashboardPage; loaded: number; loading?: boolean; error?: string; compact?: boolean; itemLabel?: string; automatic?: boolean; active?: boolean; scope?: string }>(), {
  compact: false, itemLabel: "项", automatic: false, active: true, scope: "",
});
const emit = defineEmits<{ more: []; retry: []; pause: [scope: string] }>();
const progressError = ref("");
const attempted = new Set<string>();
let waiting = false;
function pause(scope: string) {
  if (waiting) emit("pause", scope);
  waiting = false; attempted.clear(); progressError.value = "";
}
watch(() => [props.active, props.scope] as const, (_value, previous) => {
  pause(previous?.[1] ?? props.scope);
}, { flush: "sync" });
watch(() => [props.automatic, props.active, props.scope, props.page?.next_cursor, props.page?.has_more, props.loading, props.error], () => {
  if (!props.automatic || !props.active || props.loading || props.error || progressError.value) return;
  if (!props.page?.has_more) { waiting = false; return; }
  const cursor = props.page.next_cursor;
  if (!cursor || attempted.has(cursor)) {
    waiting = false; progressError.value = "后续记录暂时无法继续读取，请重试。"; return;
  }
  attempted.add(cursor); waiting = true; emit("more");
}, { immediate: true, flush: "post" });
function retry() { waiting = props.automatic && props.active; attempted.clear(); progressError.value = ""; emit("retry"); }
onBeforeUnmount(() => pause(props.scope));
</script>

<template>
  <div v-if="error || progressError || (page && (!compact || page.has_more))" class="dashboard-pagination" :aria-busy="loading">
    <span v-if="page && (!compact || page.has_more)">
      <template v-if="compact">已加载 {{ loaded }} / {{ page.filtered_count }} {{ itemLabel }}</template>
      <template v-else>完整总计 {{ page.total_count }} 项 · 筛选总计 {{ page.filtered_count }} 项 · 已加载 {{ loaded }} 项</template>
    </span>
    <p v-if="error || progressError" role="alert">{{ error || progressError }} <button type="button" :disabled="loading" @click="retry">重试读取</button></p>
    <button v-else-if="page?.has_more && !automatic" type="button" :disabled="loading" @click="$emit('more')">{{ loading ? "加载中…" : "加载更多" }}</button>
    <span v-else-if="page?.has_more && automatic && loading" role="status">正在读取剩余记录…</span>
    <span v-else-if="page && !page.has_more && loaded > 0">本次筛选已全部加载</span>
  </div>
</template>

<style scoped>
.dashboard-pagination { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 0; color: var(--muted, #68766c); font-size: 13px; }
button { padding: 7px 12px; border: 1px solid currentColor; border-radius: 8px; background: transparent; color: inherit; cursor: pointer; }
button:disabled { cursor: wait; opacity: .6; }
button:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
@media (max-width: 720px) { button { min-height: 44px; } }
</style>
