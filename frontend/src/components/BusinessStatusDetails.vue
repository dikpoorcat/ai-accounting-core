<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { fetchBusinessStatus, type BusinessStatusData } from "../api/businessStatus";
import { dashboardErrorMessage, isDashboardSnapshotChanged } from "../api/client";
import { businessStateLabel } from "../api/dashboardContracts";
import DashboardPagination from "./DashboardPagination.vue";
import DashboardBusinessRecords from "./DashboardBusinessRecords.vue";
import VoucherTrace from "./brief/VoucherTrace.vue";
const props = defineProps<{ subjectId: string; period: string; snapshotVersion?: string | null; settlementView?: "historical" | "current"; summaryLabel?: string }>();
const emit = defineEmits<{ changed: [] }>();
const route = useRoute();
const data = ref<BusinessStatusData | null>(null), error = ref("");
const loading = ref(false);
const responseVersion = ref("");
const notice = ref("");
const collectionStates = ref<Record<string, { loading: boolean; error: string; notice: string; restart: boolean }>>({});
const collectionControllers = new Map<string, AbortController>();
let controller: AbortController | null = null, generation = 0, mounted = true;
function selection() { return JSON.stringify([route.query.company_id, props.subjectId, props.period, props.snapshotVersion, props.settlementView]); }
function invalidate() {
  generation += 1; controller?.abort(); controller = null;
  for (const request of collectionControllers.values()) request.abort();
  collectionControllers.clear(); collectionStates.value = {};
  data.value = null; loading.value = false; error.value = ""; responseVersion.value = ""; notice.value = "";
}
function snapshotChanged() { invalidate(); notice.value = "业务资料已更新，正在重新读取。"; emit("changed"); }
async function load(section?: string) {
  if (section) { await loadCollection(section); return; }
  if (loading.value) return;
  const version = ++generation, key = selection(), request = new AbortController();
  controller = request; loading.value = true; error.value = "";
  const valid = () => mounted && generation === version && selection() === key && controller === request;
  try {
    const result = await fetchBusinessStatus(props.period, props.subjectId, request.signal, { expected_version: props.snapshotVersion, settlement_view: props.settlementView ?? "current" });
    if (!valid()) return;
    responseVersion.value = result.snapshot_version;
    data.value = result.data; notice.value = "";
  } catch (caught) { if (valid()) { if (isDashboardSnapshotChanged(caught)) snapshotChanged(); else error.value = dashboardErrorMessage(caught); } }
  finally { if (valid()) loading.value = false; }
}
async function loadCollection(section: string) {
  const current = data.value;
  if (!current || loading.value) return;
  if (!collectionStates.value[section]) collectionStates.value[section] = { loading: false, error: "", notice: "", restart: false };
  const state = collectionStates.value[section];
  const page = current.collections[section]?.page;
  if (state.loading || (!state.restart && (!page?.has_more || !page.next_cursor))) return;
  const version = generation, key = selection(), expectedVersion = responseVersion.value;
  const request = new AbortController(); collectionControllers.set(section, request);
  const valid = () => mounted && generation === version && selection() === key && collectionControllers.get(section) === request && responseVersion.value === expectedVersion && data.value !== null;
  state.loading = true; state.error = "";
  let replace = state.restart;
  const read = (cursor?: string) => fetchBusinessStatus(props.period, props.subjectId, request.signal, { section, cursor, expected_version: expectedVersion, settlement_view: props.settlementView ?? "current" });
  try {
    let result;
    try { result = await read(replace ? undefined : page?.next_cursor ?? undefined); }
    catch (caught) {
      if (!valid()) return;
      if (section !== "file_jobs" || replace || !isDashboardSnapshotChanged(caught)) throw caught;
      // A file-only update invalidates its cursor, while the original business snapshot may remain valid.
      replace = true; state.restart = true; state.notice = "文件任务已更新，正在重新读取该集合。";
      result = await read();
    }
    if (!valid() || !data.value) return;
    const next = result.data.collections[section], latest = data.value;
    data.value = { ...latest, collections: { ...latest.collections, [section]: { ...next, items: replace ? next.items : [...latest.collections[section].items, ...next.items] } } };
    state.restart = false;
    if (replace) state.notice = "文件任务已更新，已重新读取；其他业务资料保持原核算版本。";
  } catch (caught) {
    if (valid()) {
      if (isDashboardSnapshotChanged(caught)) snapshotChanged();
      else { state.error = dashboardErrorMessage(caught); if (state.restart) state.notice = "文件任务已变化，旧分页已停止使用。请重新读取该集合。"; }
    }
  } finally { if (valid()) { state.loading = false; collectionControllers.delete(section); } }
}
function opened(event: Event) { if ((event.target as HTMLDetailsElement).open && !data.value && !loading.value) void load(); }
function label(section: string) { return ({ events: "核算历史", settlement_events: props.settlementView === "historical" ? "相关历史清偿（含关联来源，截至所选月末）" : "当前后续清偿事件", source_history: "来源历史", file_jobs: "文件任务" } as Record<string, string>)[section] ?? "业务详情"; }
watch(selection, invalidate, { flush: "sync" });
onBeforeUnmount(() => { mounted = false; invalidate(); });
</script>

<template>
  <details @toggle="opened">
    <summary>{{ summaryLabel || (settlementView === 'historical' ? '查看更多历史清偿与精确来源' : '查看这项业务的完整状态与追溯') }}</summary>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="loading">正在读取…</p>
    <p v-if="error" role="alert">{{ error }}<button type="button" @click="load()">重新读取</button></p>
    <template v-if="data">
      <p>当前核算依据：{{ businessStateLabel(data.review.status) }}</p>
      <h4>所选月末核算</h4>
      <p>核算截至 {{ data.selected_accounting.cutoff_period }} · {{ businessStateLabel(data.selected_accounting.through_period.status) }}</p>
      <section v-for="(selection, index) in data.selected_accounting.through_period.unestablished_state_selections" :key="index">
        <strong>尚不能证明冻结采用</strong>
        <p>以下为精确候选，不能当作已采用结果或按零金额处理。</p>
        <div v-for="candidate in selection.candidates" :key="candidate.calculation_id">
          <VoucherTrace :calculation-id="candidate.calculation_id" />
          <details><summary>查看候选与未建立原因</summary><pre>{{ JSON.stringify({ reason: selection.reason, candidate }, null, 2) }}</pre></details>
        </div>
      </section>
      <DashboardBusinessRecords :items="data.selected_accounting.through_period.state_results" :period="period" :show-business="false" />
      <h4>所选月末款项</h4>
      <p>截至 {{ data.settlements.cutoff_period }} · {{ businessStateLabel(data.settlements.status) }}</p>
      <p v-if="data.settlements.complete === false || data.settlements.status === 'partially_established' || data.settlements.unestablished_state_selections?.length || data.selected_accounting.through_period.unestablished_state_selections.length" class="incomplete-status" role="status">历史月末款项尚不能完整确定；已有金额不能代表完整清偿结果，请核对下方来源和未建立候选。</p>
      <p v-for="(issue, index) in data.settlements.issues" :key="index">{{ issue.message || "款项来源尚待核对。" }}</p>
      <DashboardBusinessRecords :items="data.settlements.obligations" :period="period" :show-business="false" />
      <h4>本项历史业务相关的当前跟进</h4>
      <template v-if="data.current_followups">
        <p>相关后来清偿截至 {{ data.current_followups.settlements.current_cutoff_period || data.current_followups.settlements.cutoff_period }} · {{ businessStateLabel(data.current_followups.settlements.status) }}</p>
        <p v-if="data.current_followups.settlements.complete === false || data.current_followups.settlements.unestablished_state_selections?.length || data.current_followups.settlements.status === 'partially_established'" class="incomplete-status" role="status">相关当前款项尚不能完整确定；即使已有金额，也不能据此认定已结清。<span v-if="data.current_followups.settlements.unestablished_state_selections?.length">仍有 {{ data.current_followups.settlements.unestablished_state_selections.length }} 组采用依据尚未建立，候选保留在技术依据中供核对。</span></p>
        <p v-for="(issue, index) in data.current_followups.settlements.issues" :key="index">{{ issue.message || "当前款项来源尚待核对。" }}</p>
        <DashboardBusinessRecords :items="data.current_followups.settlements.obligations" :period="period" :show-business="false" />
      </template>
      <p v-else>当前跟进资料尚未提供。</p>
      <h4>外部办理</h4>
      <DashboardBusinessRecords :items="[data.external]" :period="period" :show-business="false" />
      <details v-for="(collection, section) in data.collections" :key="section">
        <summary>{{ label(section) }}</summary>
        <p v-if="collectionStates[section]?.notice" role="status">{{ collectionStates[section].notice }}</p>
        <template v-if="!collectionStates[section]?.restart">
          <DashboardBusinessRecords :items="collection.items" :period="period" :show-business="false" />
          <DashboardPagination :page="collection.page" :loaded="collection.items.length" :loading="collectionStates[section]?.loading" :error="collectionStates[section]?.error" @more="load(section)" @retry="load(section)" />
        </template>
        <p v-else-if="collectionStates[section]?.error" role="alert">{{ collectionStates[section].error }} <button type="button" :disabled="collectionStates[section].loading" @click="load(section)">重新读取文件任务</button></p>
      </details>
      <details><summary>技术依据与字段来源</summary><pre>{{ JSON.stringify(data, null, 2) }}</pre></details>
    </template>
  </details>
</template>

<style scoped>
details, p { font-size: 13px; line-height: 1.7; } pre { max-height: 360px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
.incomplete-status { padding: 10px 12px; border-left: 3px solid var(--warning); background: var(--warning-soft); font-weight: 650; }
</style>
