<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { fetchLocalTrace, localBusinessName, localErrorMessage, type LocalTrace } from "../../api/localKernel";
const props = defineProps<{ calculationId: string }>();
const route = useRoute();
const trace = ref<LocalTrace | null>(null);
const loading = ref(false);
const opened = ref(false);
const error = ref("");
let controller: AbortController | undefined;
const evidence = computed(() => [...new Set(trace.value?.facts.flatMap(fact => fact.evidence) ?? [])]);
async function load(id = props.calculationId) {
  controller?.abort(); const active = new AbortController(); controller = active;
  opened.value = true; trace.value = null; error.value = ""; loading.value = true;
  try {
    const companyId = route.query.company_id;
    if (typeof companyId !== "string") return;
    const result = await fetchLocalTrace(companyId, id, active.signal);
    if (!active.signal.aborted) trace.value = result;
  } catch (caught) { if (!active.signal.aborted) error.value = localErrorMessage(caught); }
  finally { if (!active.signal.aborted) loading.value = false; }
}
function close() { controller?.abort(); opened.value = false; trace.value = null; }
watch(() => [props.calculationId, route.query.company_id], close);
onBeforeUnmount(() => controller?.abort());
</script>
<template>
  <section class="trace-details" :aria-busy="loading">
    <button type="button" class="trace-button" :aria-expanded="opened" @click="opened ? close() : load()">{{ opened ? "收起核算依据" : "查看核算依据" }}</button>
    <template v-if="opened">
      <p v-if="loading" role="status">正在读取核算依据…</p><p v-else-if="error" role="alert">{{ error }}</p>
      <template v-else-if="trace">
        <h3>{{ localBusinessName(trace.calculation.kind) }}的核算依据</h3>
        <p>采用 {{ trace.facts.length }} 项事实版本 · {{ evidence.length }} 份关联证据</p>
        <ul><li v-for="fact in trace.facts" :key="fact.id"><strong>{{ localBusinessName(fact.kind) }}</strong> · 第 {{ fact.revision }} 版<details><summary>查看采用的事实</summary><pre>{{ JSON.stringify(fact.data, null, 2) }}</pre></details></li></ul>
        <details v-if="evidence.length"><summary>查看来源证据</summary><ul><li v-for="item in evidence" :key="item">{{ item }}</li></ul></details>
        <div v-if="trace.upstream.length"><h4>上游计算依据</h4><button v-for="(id, index) in trace.upstream" :key="id" class="trace-button" @click="load(id)">查看上游依据 {{ index + 1 }}</button></div>
        <details><summary>技术信息与完整计算轨迹</summary><pre>{{ JSON.stringify(trace.calculation, null, 2) }}</pre></details>
      </template>
    </template>
  </section>
</template>
<style scoped>
.trace-details { border-top: 1px solid var(--line); padding: 18px 0 0; margin-top: 16px; } .trace-button { padding: 8px 12px; color: var(--accent); background: var(--surface); border: 1px solid var(--line); border-radius: 8px; cursor: pointer; margin: 0 8px 8px 0; } h3 { font-size: 16px; } p, li, summary { font-size: 13px; } li { padding: 6px 0; } summary { cursor: pointer; color: var(--muted); } pre { font-size: 12px; white-space: pre-wrap; overflow-wrap: anywhere; padding: 12px; background: var(--surface-soft); max-height: 420px; overflow: auto; }
</style>
