<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { fetchLocalTrace, fetchVoucherTrace, localBusinessName, localErrorMessage, type LocalTrace } from "../../api/localKernel";
import { formatFen } from "../../utils/money";
import EvidenceList from "./EvidenceList.vue";
const props = withDefaults(defineProps<{ calculationId: string; voucherVersionId?: string; compact?: boolean }>(), { compact: false });
const route = useRoute();
const trace = ref<LocalTrace | null>(null);
const loading = ref(false);
const opened = ref(false);
const error = ref("");
const history = ref<{ calculationId?: string; voucherVersionId?: string }[]>([]);
let current: { calculationId?: string; voucherVersionId?: string } | undefined;
let controller: AbortController | undefined;
let generation = 0, mounted = true;
function selection() { return JSON.stringify([route.query.company_id, props.calculationId, props.voucherVersionId]); }
const evidence = computed(() => trace.value?.evidence_details ?? []);
async function load(target: { calculationId?: string; voucherVersionId?: string } = { calculationId: props.calculationId, voucherVersionId: props.voucherVersionId }, remember = true) {
  if (remember && current) history.value.push(current);
  current = target;
  controller?.abort(); const active = new AbortController(); controller = active;
  const version = ++generation, key = selection();
  const valid = () => mounted && generation === version && selection() === key && controller === active;
  opened.value = true; trace.value = null; error.value = ""; loading.value = true;
  try {
    const companyId = route.query.company_id;
    if (typeof companyId !== "string") return;
    const result = target.voucherVersionId
      ? await fetchVoucherTrace(companyId, target.voucherVersionId, target.calculationId, active.signal)
      : await fetchLocalTrace(companyId, target.calculationId!, active.signal);
    if (valid()) trace.value = result;
  } catch (caught) { if (valid()) error.value = localErrorMessage(caught); }
  finally { if (valid()) loading.value = false; }
}
function close() { generation += 1; controller?.abort(); controller = undefined; opened.value = false; loading.value = false; error.value = ""; trace.value = null; history.value = []; current = undefined; }
function back() { const previous = history.value.pop(); if (previous) void load(previous, false); }
function retry() { if (current && !loading.value) void load(current, false); }
watch(() => [props.calculationId, props.voucherVersionId, route.query.company_id], close, { flush: "sync" });
onBeforeUnmount(() => { mounted = false; close(); });
</script>
<template>
  <section :class="['trace-details', { compact }]" :aria-busy="loading">
    <button type="button" class="trace-button" :aria-expanded="opened" @click="opened ? close() : load()">{{ compact ? (opened ? "收起核算追溯" : "核算追溯") : (opened ? "收起核算依据" : "查看核算依据") }}</button>
    <div v-if="opened" class="trace-content">
      <p v-if="loading" role="status">正在读取核算依据…</p><p v-else-if="error" role="alert">{{ error }} <button type="button" class="trace-button" @click="retry">重新读取这项依据</button></p>
      <button v-if="history.length" type="button" class="trace-button" @click="back">返回上一层依据</button>
      <template v-if="!loading && !error && trace">
        <h3>{{ localBusinessName(trace.calculation.kind) }}的核算依据</h3>
        <p v-if="trace.voucher && !compact">凭证 {{ trace.voucher.number }} · {{ trace.voucher.period }} · 借贷各 {{ formatFen(trace.voucher.total) }}</p>
        <p v-if="trace.voucher?.reverses_id">此处展示被冲销的原计算；本次冲正将原借贷方向反转。</p>
        <div v-if="trace.related_vouchers?.length"><h4>更正关系</h4><button v-for="item in trace.related_vouchers" :key="item.id" class="trace-button" @click="load({ voucherVersionId: item.id })">{{ item.label || `${({ original: '原凭证', reversal: '冲正凭证', replacement: '替换凭证' })[item.role]} ${item.number} · ${item.period}` }}</button></div>
        <p>{{ compact ? "采用的业务资料：" : `依据以下业务资料及 ${evidence.length} 份文件核算。` }}</p>
        <ul><li v-for="fact in trace.facts" :key="fact.id"><strong>{{ localBusinessName(fact.kind) }}</strong><span v-if="typeof fact.data.period === 'string'"> · {{ fact.data.period }}</span></li></ul>
        <details v-if="evidence.length && !compact"><summary>查看来源证据</summary><EvidenceList :items="evidence" /></details>
        <div v-if="trace.upstream.length"><h4>相关业务来源</h4><button v-for="(id, index) in trace.upstream" :key="id" class="trace-button" @click="load({ calculationId: id })">{{ trace.upstream_details?.find(item => item.id === id)?.label || `查看业务来源 ${index + 1}` }}</button></div>
        <details v-if="!compact"><summary>技术详情</summary><h4>采用的事实与版本</h4><pre>{{ JSON.stringify(trace.facts, null, 2) }}</pre><h4>完整计算记录</h4><pre>{{ JSON.stringify(trace.calculation, null, 2) }}</pre></details>
      </template>
    </div>
  </section>
</template>
<style scoped>
.trace-details {
  margin-top: 16px;
  padding: 18px 0 0;
  border-top: 1px solid var(--line);
}

.trace-button {
  margin: 0 8px 8px 0;
  padding: 8px 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  color: var(--accent);
  cursor: pointer;
}

.compact > .trace-button {
  padding: 7px 0;
  border: 0;
  background: transparent;
  font-weight: 700;
}

.trace-content {
  min-width: 0;
}

h3 {
  font-size: 16px;
}

p,
li,
summary {
  font-size: 13px;
}

li {
  padding: 6px 0;
}

summary {
  color: var(--muted);
  cursor: pointer;
}

pre {
  overflow: auto;
  max-height: 420px;
  padding: 12px;
  background: var(--surface-soft);
  font-size: 12px;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.compact .trace-content {
  color: var(--muted);
}

.compact h3 {
  margin: 4px 0 7px;
  color: var(--text);
  font-size: 14px;
  line-height: 1.35;
}

.compact h4 {
  margin: 10px 0 5px;
  color: var(--text);
  font-size: 12px;
}

.compact p,
.compact li,
.compact summary {
  font-size: 12px;
  line-height: 1.5;
}

.compact p {
  margin: 5px 0;
}

.compact .trace-content > ul {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  margin: 4px 0 6px;
  padding: 0;
  list-style: none;
}

.compact .trace-content > ul li {
  padding: 0;
}

.compact .trace-content .trace-button {
  padding: 5px 8px;
  border-radius: 7px;
  font-size: 12px;
}
</style>
