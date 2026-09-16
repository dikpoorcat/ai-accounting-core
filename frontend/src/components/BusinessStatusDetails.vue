<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { fetchBusinessStatus, type BusinessStatusData } from "../api/businessStatus";
import { dashboardErrorMessage, isDashboardSnapshotChanged } from "../api/client";
import { businessStateLabel } from "../api/dashboardContracts";
import DashboardPagination from "./DashboardPagination.vue";
import DashboardBusinessRecords from "./DashboardBusinessRecords.vue";
import { fen, formatFen } from "../utils/money";

interface BriefStatusContext {
  direction: "receivable" | "payable";
  party: string;
  description?: string;
  sourcePeriod?: string | null;
  sourceAmountFen?: string | null;
  paidFen?: string | null;
  otherSettledFen?: string | null;
  outstandingFen?: string | null;
  currentStatus?: string | null;
  currentOutstandingFen?: string | null;
  selectedPeriodClosed?: boolean;
}

const props = withDefaults(defineProps<{
  subjectId: string;
  period: string;
  snapshotVersion?: string | null;
  settlementView?: "historical" | "current";
  summaryLabel?: string;
  presentation?: "default" | "brief";
  briefContext?: BriefStatusContext;
}>(), { presentation: "default" });
const emit = defineEmits<{ changed: [] }>();
const route = useRoute();
const data = ref<BusinessStatusData | null>(null), error = ref("");
const loading = ref(false);
const compactOpen = ref(false);
const responseVersion = ref("");
const notice = ref("");
const collectionStates = ref<Record<string, { loading: boolean; error: string; notice: string; restart: boolean }>>({});
const collectionControllers = new Map<string, AbortController>();
let controller: AbortController | null = null, generation = 0, mounted = true;
const currentFollowupSettlements = computed(() => data.value?.current_followups?.settlements || null);
const showCurrentFollowups = computed(() => {
  const selected = data.value?.settlements;
  const current = currentFollowupSettlements.value;
  if (!selected || !current) return false;
  const currentCutoff = current.current_cutoff_period || current.cutoff_period;
  return currentCutoff !== selected.cutoff_period
    || current.status !== selected.status
    || current.complete === false
    || current.issues.length > 0
    || current.obligations.length !== selected.obligations.length;
});
const compactWarnings = computed(() => {
  if (!data.value) return [];
  const warnings: string[] = [];
  const accounting = data.value.selected_accounting.through_period;
  if (accounting.unestablished_state_selections.length) {
    warnings.push(`${accounting.unestablished_state_selections.length} 组核算依据尚待确认`);
  }
  const selected = data.value.settlements;
  warnings.push(...selected.issues.map((item) => item.message || "所选月末款项来源待核对"));
  if ((selected.complete === false || selected.status === "partially_established") && !selected.issues.length) {
    warnings.push("所选月末款项关系尚未完整确认");
  }
  const current = currentFollowupSettlements.value;
  if (showCurrentFollowups.value && current) {
    warnings.push(...current.issues.map((item) => item.message || "当前后续款项来源待核对"));
    if ((current.complete === false || current.status === "partially_established") && !current.issues.length) {
      warnings.push("当前后续款项关系尚未完整确认");
    }
  }
  return [...new Set(warnings)];
});
const compactCollections = computed(() => {
  if (!data.value) return [];
  const labels: Record<string, string> = {
    events: "核算记录",
    settlement_events: "清偿记录",
    source_history: "来源变更",
    file_jobs: "文件记录",
  };
  return Object.entries(data.value.collections)
    .filter(([, collection]) => collection.page.total_count > 0)
    .map(([key, collection]) => ({ key, label: labels[key] || "相关记录", collection }));
});
const compactCollectionTotal = computed(
  () => compactCollections.value.reduce((total, item) => total + item.collection.page.total_count, 0),
);
const briefContext = computed(() => props.briefContext ?? null);
const briefOutstandingWord = computed(() => briefContext.value?.direction === "payable" ? "待付" : "待收");
const briefSettledWord = computed(() => briefContext.value?.direction === "payable" ? "已付" : "已收");
const briefCurrentHeadline = computed(() => {
  const context = briefContext.value;
  if (!context) return "";
  if (context.currentStatus === "settled") {
    return context.direction === "payable" ? "当前已付清" : "当前已收回";
  }
  if (context.currentStatus === "partial") {
    return context.direction === "payable" ? "当前部分支付" : "当前部分收回";
  }
  if (context.currentStatus === "open") return `当前${briefOutstandingWord.value}`;
  return `${context.selectedPeriodClosed ? "关账时" : "月末"}${briefOutstandingWord.value}`;
});
const briefReason = computed(() => {
  const context = briefContext.value;
  if (!context) return "";
  const description = context.description || "相关业务";
  const source = context.sourcePeriod ? `形成于 ${periodLabel(context.sourcePeriod)}` : "来源期间未标注";
  return `${description}${source}；截至所选月末仍有余额，因此列在这里。`;
});
const briefActionMessage = computed(() => {
  const context = briefContext.value;
  if (!context) return "";
  if (compactWarnings.value.length) {
    return "存在需要确认的来源或收付关系，请让 AI 会计先核对下列问题。";
  }
  if (context.currentStatus === "settled") {
    return "这笔款项现在已经结清，无需继续跟进。";
  }
  if (context.currentStatus === "open" || context.currentStatus === "partial") {
    return context.direction === "payable"
      ? "目前仍需安排或确认付款；完成后让 AI 会计补充付款记录即可。"
      : "目前仍需跟进到账；收到后让 AI 会计补充收款记录即可。";
  }
  return "这里展示所选月末状态；当前收付进展尚未完整建立，可让 AI 会计继续核对。";
});
const briefSelectedBalanceLabel = computed(
  () => `${briefContext.value?.selectedPeriodClosed ? "关账时" : "月末"}${briefOutstandingWord.value}`,
);
const briefHasOtherSettlement = computed(() => {
  const value = briefContext.value?.otherSettledFen;
  return value !== null && value !== undefined && fen(value) !== 0n;
});
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
function toggleCompact() {
  compactOpen.value = !compactOpen.value;
  if (compactOpen.value && !data.value && !loading.value) void load();
}
function periodLabel(value: string | null | undefined) {
  if (!value) return "期间未提供";
  const matched = /^(\d{4})-(\d{2})$/.exec(value);
  return matched ? `${matched[1]} 年 ${Number(matched[2])} 月` : value;
}
function label(section: string) { return ({ events: "核算历史", settlement_events: props.settlementView === "historical" ? "相关历史清偿（含关联来源，截至所选月末）" : "当前后续清偿事件", source_history: "来源历史", file_jobs: "文件任务" } as Record<string, string>)[section] ?? "业务详情"; }
watch(selection, invalidate, { flush: "sync" });
onBeforeUnmount(() => { mounted = false; invalidate(); });
</script>

<template>
  <div v-if="presentation === 'brief'" class="compact-status-details">
    <button
      type="button"
      class="compact-status-trigger"
      :aria-expanded="compactOpen"
      @click="toggleCompact"
    >
      <span>{{ summaryLabel || "查看详情" }}</span>
      <i aria-hidden="true"></i>
    </button>

    <section v-if="compactOpen" class="compact-status-panel" :aria-busy="loading" aria-live="polite">
      <header v-if="briefContext" class="compact-owner-heading">
        <span>
          <small>{{ briefContext.direction === "payable" ? "待付详情" : "待收详情" }}</small>
          <strong>{{ briefContext.party }}</strong>
          <em v-if="briefContext.description && briefContext.description !== briefContext.party">{{ briefContext.description }}</em>
        </span>
        <b :class="{ settled: briefContext.currentStatus === 'settled', attention: briefContext.currentStatus !== 'settled' }">{{ briefCurrentHeadline }}</b>
      </header>

      <section v-if="briefContext" :class="['compact-owner-overview', briefContext.direction]">
        <p>{{ briefReason }}</p>
        <dl class="compact-money-grid">
          <div v-if="briefContext.sourceAmountFen !== undefined">
            <dt>原金额</dt>
            <dd>{{ formatFen(briefContext.sourceAmountFen) }}</dd>
          </div>
          <div v-if="briefContext.paidFen !== undefined">
            <dt>{{ briefSettledWord }}</dt>
            <dd>{{ formatFen(briefContext.paidFen) }}</dd>
          </div>
          <div v-if="briefHasOtherSettlement">
            <dt>抵销等</dt>
            <dd>{{ formatFen(briefContext.otherSettledFen) }}</dd>
          </div>
          <div class="remaining">
            <dt>{{ briefSelectedBalanceLabel }}</dt>
            <dd>{{ formatFen(briefContext.outstandingFen) }}</dd>
          </div>
        </dl>
      </section>

      <p v-if="notice" class="compact-notice" role="status">{{ notice }}</p>
      <div v-if="loading" class="compact-placeholder" role="status">正在读取款项详情…</div>
      <div v-else-if="error" class="compact-placeholder error" role="alert">
        <span>{{ error }}</span>
        <button type="button" @click="load()">重新读取</button>
      </div>
      <template v-else-if="data">
        <header v-if="!briefContext" class="compact-status-heading">
          <span>
            <small>核对结果</small>
            <strong>{{ businessStateLabel(data.review.status) }}</strong>
          </span>
          <b v-if="compactWarnings.length" class="attention">{{ compactWarnings.length }} 项需要核对</b>
          <b v-else>未发现问题</b>
        </header>

        <section :class="['compact-decision', { attention: compactWarnings.length }]">
          <strong>{{ compactWarnings.length ? `${compactWarnings.length} 项需要核对` : "账务与收付关系已核对" }}</strong>
          <span v-if="briefContext">{{ briefActionMessage }}</span>
          <span v-else>{{ compactWarnings.length ? "请查看下列问题。" : "未发现来源或收付关系异常。" }}</span>
        </section>

        <ul v-if="compactWarnings.length" class="compact-warnings">
          <li v-for="warning in compactWarnings" :key="warning">{{ warning }}</li>
        </ul>

        <details class="compact-accounting">
          <summary>
            <span>查看账务确认过程</span>
            <small>{{ businessStateLabel(data.review.status) }}</small>
          </summary>
          <dl class="compact-status-grid">
            <div>
              <dt>业务是否已入账</dt>
              <dd>{{ businessStateLabel(data.selected_accounting.through_period.status) }}</dd>
              <span>
                截至 {{ periodLabel(data.selected_accounting.cutoff_period) }}
                <template v-if="data.selected_accounting.through_period.voucher_event_count">
                  · {{ data.selected_accounting.through_period.voucher_event_count }} 张凭证
                </template>
              </span>
            </div>
            <div>
              <dt>所选月末收付状态</dt>
              <dd>{{ businessStateLabel(data.settlements.status) }}</dd>
              <span>截至 {{ periodLabel(data.settlements.cutoff_period) }} · {{ data.settlements.obligations.length }} 项款项</span>
            </div>
            <div v-if="showCurrentFollowups && currentFollowupSettlements">
              <dt>现在的收付状态</dt>
              <dd>{{ businessStateLabel(currentFollowupSettlements.status) }}</dd>
              <span>
                截至 {{ periodLabel(currentFollowupSettlements.current_cutoff_period || currentFollowupSettlements.cutoff_period) }}
                · {{ currentFollowupSettlements.obligations.length }} 项款项
              </span>
            </div>
          </dl>
        </details>

        <details v-if="compactCollections.length" class="compact-history">
          <summary>
            <span>查看相关记录</span>
            <small>{{ compactCollectionTotal }} 项</small>
          </summary>
          <section v-for="item in compactCollections" :key="item.key">
            <header>
              <h4>{{ item.label }}</h4>
              <span>{{ item.collection.page.total_count }} 项</span>
            </header>
            <DashboardBusinessRecords :items="item.collection.items" :period="period" :show-business="false" />
            <DashboardPagination
              compact
              :page="item.collection.page"
              :loaded="item.collection.items.length"
              :loading="collectionStates[item.key]?.loading"
              :error="collectionStates[item.key]?.error"
              @more="load(item.key)"
              @retry="load(item.key)"
            />
          </section>
        </details>
      </template>
    </section>
  </div>
  <details v-else class="business-status-default" @toggle="opened">
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
.business-status-default,
.business-status-default p {
  font-size: 13px;
  line-height: 1.7;
}

.business-status-default,
.business-status-default details {
  min-width: 0;
  overflow-wrap: anywhere;
}

.business-status-default summary {
  padding: 6px 0;
  cursor: pointer;
}

.business-status-default summary:focus-visible,
.compact-status-trigger:focus-visible,
.compact-accounting > summary:focus-visible,
.compact-history > summary:focus-visible {
  outline: 2px solid var(--focus, var(--brief-green));
  outline-offset: 2px;
}

.business-status-default button,
.compact-placeholder button {
  padding: 7px 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  color: var(--text);
  cursor: pointer;
}

.business-status-default pre {
  max-height: 360px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.incomplete-status {
  padding: 10px 12px;
  border-left: 3px solid var(--warning);
  background: var(--warning-soft);
  font-weight: 650;
}

.compact-status-details {
  display: contents;
}

.compact-status-trigger {
  display: inline-flex;
  min-height: 32px;
  align-items: center;
  justify-content: center;
  gap: 7px;
  padding: 0 9px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--brief-green, var(--accent));
  font: inherit;
  font-size: 11px;
  font-weight: 750;
  white-space: nowrap;
  cursor: pointer;
}

.compact-status-trigger:hover,
.compact-status-trigger[aria-expanded="true"] {
  background: var(--brief-green-soft, var(--surface-soft));
}

.compact-status-trigger i {
  width: 6px;
  height: 6px;
  border-right: 1.5px solid currentColor;
  border-bottom: 1.5px solid currentColor;
  transform: rotate(45deg) translateY(-1px);
  transition: transform 140ms ease;
}

.compact-status-trigger[aria-expanded="true"] i {
  transform: rotate(225deg) translate(-1px, -1px);
}

.compact-status-panel {
  display: grid;
  min-width: 0;
  width: 100%;
  gap: 12px;
  margin-top: 8px;
  padding: 14px 16px;
  border-radius: var(--brief-control-radius, var(--radius-control, 9px));
  background: var(--brief-soft, var(--surface-soft));
  color: var(--brief-text, var(--text));
}

.compact-notice,
.compact-placeholder {
  margin: 0;
  color: var(--brief-muted, var(--muted));
  font-size: 12px;
}

.compact-placeholder {
  display: flex;
  min-height: 60px;
  align-items: center;
  justify-content: center;
  gap: 10px;
}

.compact-placeholder.error {
  color: var(--brief-amber, var(--warning));
}

.compact-owner-heading,
.compact-status-heading,
.compact-accounting > summary,
.compact-history > summary,
.compact-history > section > header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.compact-owner-heading {
  align-items: flex-start;
}

.compact-owner-heading > span,
.compact-status-heading > span {
  display: grid;
  min-width: 0;
  gap: 2px;
}

.compact-owner-heading small,
.compact-status-heading small,
.compact-accounting small,
.compact-history small,
.compact-history > section > header span {
  color: var(--brief-muted, var(--muted));
  font-size: 11px;
}

.compact-owner-heading strong,
.compact-status-heading strong {
  font-size: 15px;
}

.compact-owner-heading em {
  overflow: hidden;
  color: var(--brief-muted, var(--muted));
  font-size: 11px;
  font-style: normal;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.compact-owner-heading > b,
.compact-status-heading > b {
  flex: none;
  padding: 4px 9px;
  border-radius: 999px;
  background: var(--brief-green-soft, var(--surface-soft));
  color: var(--brief-green, var(--accent));
  font-size: 11px;
  white-space: nowrap;
}

.compact-owner-heading > b.attention,
.compact-status-heading > b.attention {
  background: var(--brief-amber-soft, var(--warning-soft));
  color: var(--brief-amber, var(--warning));
}

.compact-owner-heading > b.settled {
  background: var(--brief-green-soft, var(--surface-soft));
  color: var(--brief-green, var(--accent));
}

.compact-owner-overview {
  display: grid;
  gap: 10px;
}

.compact-owner-overview > p {
  margin: 0;
  color: var(--brief-muted, var(--muted));
  font-size: 11px;
  line-height: 1.55;
}

.compact-money-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(105px, 1fr));
  gap: 8px;
  margin: 0;
}

.compact-money-grid > div {
  display: grid;
  min-width: 0;
  gap: 2px;
  padding-left: 10px;
  border-left: 1px solid var(--brief-line, var(--line));
}

.compact-money-grid > div:first-child {
  padding-left: 0;
  border-left: 0;
}

.compact-money-grid dt {
  color: var(--brief-muted, var(--muted));
  font-size: 10px;
}

.compact-money-grid dd {
  margin: 0;
  font-size: 13px;
  font-weight: 760;
  white-space: nowrap;
}

.compact-owner-overview.receivable .remaining dd {
  color: var(--brief-blue, var(--accent));
}

.compact-owner-overview.payable .remaining dd {
  color: var(--brief-amber, var(--warning));
}

.compact-decision {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--brief-line, var(--line));
  font-size: 11px;
  line-height: 1.5;
}

.compact-decision strong {
  flex: none;
}

.compact-decision span {
  color: var(--brief-muted, var(--muted));
}

.compact-decision.attention strong {
  color: var(--brief-amber, var(--warning));
}

.compact-status-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 8px;
  margin: 0;
}

.compact-status-grid > div {
  display: grid;
  gap: 3px;
  padding: 4px 12px;
  border-left: 1px solid var(--brief-line, var(--line));
}

.compact-status-grid > div:first-child {
  padding-left: 0;
  border-left: 0;
}

.compact-status-grid dt {
  color: var(--brief-muted, var(--muted));
  font-size: 10px;
}

.compact-status-grid dd {
  margin: 0;
  font-size: 13px;
  font-weight: 750;
}

.compact-status-grid span {
  color: var(--brief-muted, var(--muted));
  font-size: 10px;
  line-height: 1.45;
}

.compact-warnings {
  display: grid;
  gap: 5px;
  margin: 0;
  padding: 9px 12px 9px 28px;
  border-left: 3px solid var(--brief-amber, var(--warning));
  border-radius: 7px;
  background: var(--brief-amber-soft, var(--warning-soft));
  color: var(--brief-text, var(--text));
  font-size: 11px;
  line-height: 1.5;
}

.compact-accounting,
.compact-history {
  min-width: 0;
  border-top: 1px solid var(--brief-line, var(--line));
}

.compact-accounting > summary,
.compact-history > summary {
  min-height: 34px;
  padding: 4px 2px 0;
  color: var(--brief-green, var(--accent));
  font-size: 11px;
  font-weight: 750;
  list-style: none;
  cursor: pointer;
}

.compact-accounting > summary::-webkit-details-marker,
.compact-history > summary::-webkit-details-marker {
  display: none;
}

.compact-accounting > .compact-status-grid {
  padding: 10px 0;
}

.compact-history > section {
  min-width: 0;
  padding: 10px 0;
  border-top: 1px solid var(--brief-line, var(--line));
}

.compact-history > section > header h4 {
  margin: 0;
  font-size: 12px;
}

.compact-history :deep(.business-records article) {
  padding: 9px 0;
}

@media (max-width: 720px) {
  .business-status-default summary,
  .business-status-default button,
  .compact-status-trigger {
    min-height: 44px;
  }

  .compact-status-panel {
    padding: 12px;
  }

  .compact-status-grid > div,
  .compact-status-grid > div:first-child {
    padding: 8px 0;
    border-top: 1px solid var(--brief-line, var(--line));
    border-left: 0;
  }

  .compact-status-grid > div:first-child {
    padding-top: 0;
    border-top: 0;
  }

  .compact-owner-heading,
  .compact-status-heading {
    align-items: flex-start;
    flex-direction: column;
  }

  .compact-money-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .compact-money-grid > div,
  .compact-money-grid > div:first-child {
    padding: 5px 0;
    border-top: 1px solid var(--brief-line, var(--line));
    border-left: 0;
  }

  .compact-decision {
    display: grid;
    gap: 3px;
  }

  .compact-status-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
