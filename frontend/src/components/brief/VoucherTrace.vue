<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { fetchLocalTrace, fetchVoucherTrace, localBusinessName, localErrorMessage, type LocalTrace } from "../../api/localKernel";
import { formatFen } from "../../utils/money";
import EvidenceList from "./EvidenceList.vue";
const props = defineProps<{ calculationId: string; voucherVersionId?: string }>();
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
  <section class="trace-details" :aria-busy="loading">
    <button type="button" class="trace-button" :aria-expanded="opened" @click="opened ? close() : load()">{{ opened ? "收起核算依据" : "查看核算依据" }}</button>
    <template v-if="opened">
      <p v-if="loading" role="status">正在读取核算依据…</p><p v-else-if="error" role="alert">{{ error }} <button type="button" class="trace-button" @click="retry">重新读取这项依据</button></p>
      <button v-if="history.length" type="button" class="trace-button" @click="back">返回上一层依据</button>
      <template v-if="!loading && !error && trace">
        <h3>{{ localBusinessName(trace.calculation.kind) }}的核算依据</h3>
        <p v-if="trace.voucher">凭证 {{ trace.voucher.number }} · {{ trace.voucher.period }} · 借贷各 {{ formatFen(trace.voucher.total) }}</p>
        <p v-if="trace.voucher?.reverses_id">此处展示被冲销的原计算；本次冲正将原借贷方向反转。</p>
        <div v-if="trace.related_vouchers?.length"><h4>更正关系</h4><button v-for="item in trace.related_vouchers" :key="item.id" class="trace-button" @click="load({ voucherVersionId: item.id })">{{ item.label || `${({ original: '原凭证', reversal: '冲正凭证', replacement: '替换凭证' })[item.role]} ${item.number} · ${item.period}` }}</button></div>
        <p>依据以下业务资料及 {{ evidence.length }} 份文件核算。</p>
        <ul><li v-for="fact in trace.facts" :key="fact.id"><strong>{{ localBusinessName(fact.kind) }}</strong><span v-if="typeof fact.data.period === 'string'"> · {{ fact.data.period }}</span></li></ul>
        <details v-if="evidence.length"><summary>查看来源证据</summary><EvidenceList :items="evidence" /></details>
        <div v-if="trace.upstream.length"><h4>相关业务来源</h4><button v-for="(id, index) in trace.upstream" :key="id" class="trace-button" @click="load({ calculationId: id })">{{ trace.upstream_details?.find(item => item.id === id)?.label || `查看业务来源 ${index + 1}` }}</button></div>
        <details><summary>技术详情</summary><h4>采用的事实与版本</h4><pre>{{ JSON.stringify(trace.facts, null, 2) }}</pre><h4>完整计算记录</h4><pre>{{ JSON.stringify(trace.calculation, null, 2) }}</pre></details>
      </template>
    </template>
  </section>
</template>
<style scoped>
.trace-details { border-top: 1px solid var(--line); padding: 18px 0 0; margin-top: 16px; } .trace-button { padding: 8px 12px; color: var(--accent); background: var(--surface); border: 1px solid var(--line); border-radius: 8px; cursor: pointer; margin: 0 8px 8px 0; } h3 { font-size: 16px; } p, li, summary { font-size: 13px; } li { padding: 6px 0; } summary { cursor: pointer; color: var(--muted); } pre { font-size: 12px; white-space: pre-wrap; overflow-wrap: anywhere; padding: 12px; background: var(--surface-soft); max-height: 420px; overflow: auto; }
</style>
