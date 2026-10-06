<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { useRoute } from "vue-router";
import type { BriefActivityGroup, BriefActivityRow, BriefVoucher } from "../../api/brief";
import { fen, formatFen } from "../../utils/money";
import BusinessStatusDetails from "../BusinessStatusDetails.vue";

const props = withDefaults(defineProps<{
  groups: BriefActivityGroup[]; items: BriefActivityRow[]; activityCount: number;
  focusedActivity?: BriefActivityRow | null; period: string; snapshotVersion?: string | null;
  vouchers: BriefVoucher[]; voucherCount: number; focusedVoucher?: BriefVoucher | null;
  voucherPreviewIndex?: ReadonlyMap<string, BriefVoucher>; focusedVoucherSelection?: number;
  vouchersLoading?: boolean; vouchersError?: string; vouchersHasMore?: boolean; vouchersReady?: boolean; active?: boolean;
}>(), { vouchersReady: true, active: true });
const emit = defineEmits<{ changed: []; moreVouchers: []; allVouchers: []; initializeVouchers: []; pauseVouchers: []; requestVoucher: [voucherVersionId: string] }>();
const route = useRoute();
const mode = ref<"business" | "voucher">("business");
const voucherDisplayMode = ref<"paged" | "all">("paged");
const voucherPage = ref(1), pendingVoucherPage = ref<number | null>(null);
const selectedVoucher = ref("");
const manualVoucherView = ref(false);
const expandedBusinessKey = ref(""), previewBusinessKey = ref(""), previewVoucherId = ref("");
const previewPosition = ref({ offset: 0, arrowTop: "50%" });
let previewPositionGeneration = 0;
const vouchersByVersion = computed(() => props.voucherPreviewIndex ?? new Map([
  ...props.vouchers.map(voucher => [voucher.voucher_version_id, voucher] as const),
  ...(props.focusedVoucher ? [[props.focusedVoucher.voucher_version_id, props.focusedVoucher] as const] : []),
]));
const previewVoucher = computed(() => vouchersByVersion.value.get(previewVoucherId.value));
function clearPreview(key?: string) {
  if (key && previewBusinessKey.value !== key) return;
  previewPositionGeneration += 1; previewPosition.value = { offset: 0, arrowTop: "50%" };
  previewBusinessKey.value = ""; previewVoucherId.value = "";
}
async function showPreview(item: BriefActivityRow) {
  if (typeof window !== "undefined" && window.matchMedia("(max-width: 760px)").matches) return;
  const generation = ++previewPositionGeneration;
  const selection = JSON.stringify([route.query.company_id, props.period, props.snapshotVersion, mode.value, activeGroup.value]);
  const index = props.voucherPreviewIndex;
  previewPosition.value = { offset: 0, arrowTop: "50%" };
  previewBusinessKey.value = item.key; previewVoucherId.value = item.voucher_version_id ?? "";
  await nextTick();
  if (generation !== previewPositionGeneration || previewBusinessKey.value !== item.key || index !== props.voucherPreviewIndex
    || selection !== JSON.stringify([route.query.company_id, props.period, props.snapshotVersion, mode.value, activeGroup.value])
    || typeof document === "undefined" || typeof window === "undefined") return;
  const tooltip = document.getElementById("activity-voucher-preview"), button = tooltip?.previousElementSibling;
  if (!tooltip || !button) return;
  const bounds = tooltip.getBoundingClientRect(), anchor = button.getBoundingClientRect();
  const top = Math.max(12, Math.min(bounds.top, window.innerHeight - 12 - bounds.height));
  previewPosition.value = { offset: top - bounds.top, arrowTop: `${anchor.top + anchor.height / 2 - top}px` };
}
onBeforeUnmount(() => clearPreview());
function toggleBusiness(item: BriefActivityRow, event: Event) {
  if (!item.subject_id) return;
  const target = event.target;
  if (typeof Element !== "undefined" && target instanceof Element && target.closest("button, a, details, .business-status-details")) return;
  expandedBusinessKey.value = expandedBusinessKey.value === item.key ? "" : item.key;
}
function businessKeydown(item: BriefActivityRow, event: KeyboardEvent) {
  if (event.target !== event.currentTarget || !["Enter", " "].includes(event.key)) return;
  event.preventDefault(); toggleBusiness(item, event);
}
function openBusinessVoucher(item: BriefActivityRow) {
  clearPreview();
  if (item.voucher_version_id) emit("requestVoucher", item.voucher_version_id);
}
const VOUCHER_PAGE_SIZE = 20;
const voucherPageCount = computed(() => Math.max(1, Math.ceil(props.voucherCount / VOUCHER_PAGE_SIZE)));
const visibleVouchers = computed(() => {
  const start = (voucherPage.value - 1) * VOUCHER_PAGE_SIZE;
  const items = !props.vouchersReady ? [] : voucherDisplayMode.value === "all" ? props.vouchers : props.vouchers.slice(start, start + VOUCHER_PAGE_SIZE);
  const focused = props.focusedVoucher;
  return focused && selectedVoucher.value === focused.voucher_version_id
    && !items.some(item => item.voucher_version_id === focused.voucher_version_id) ? [focused, ...items] : items;
});
function selectVoucherMode() {
  mode.value = "voucher"; manualVoucherView.value = true;
  if (!props.vouchersReady) {
    voucherPage.value = 1; pendingVoucherPage.value = null; selectedVoucher.value = "";
    emit("initializeVouchers");
  } else if (voucherDisplayMode.value === "all" && props.vouchersHasMore) emit("allVouchers");
}
function changeVoucherPage(value: number) {
  const target = Math.max(1, Math.min(value, voucherPageCount.value));
  if (!props.vouchersReady) {
    manualVoucherView.value = true; pendingVoucherPage.value = target;
    emit("initializeVouchers"); return;
  }
  const end = Math.min(target * VOUCHER_PAGE_SIZE, props.voucherCount);
  if (props.vouchers.length < end && props.vouchersHasMore) {
    pendingVoucherPage.value = target;
    emit("moreVouchers");
    return;
  }
  voucherPage.value = target; pendingVoucherPage.value = null; selectedVoucher.value = "";
}
function toggleVoucherDisplayMode() {
  voucherDisplayMode.value = voucherDisplayMode.value === "paged" ? "all" : "paged";
  pendingVoucherPage.value = null;
  manualVoucherView.value = true;
  if (!props.vouchersReady) { emit("initializeVouchers"); return; }
  if (voucherDisplayMode.value === "all" && props.vouchersHasMore) emit("allVouchers");
  if (voucherDisplayMode.value === "paged") {
    const index = props.vouchers.findIndex(item => item.voucher_version_id === selectedVoucher.value);
    voucherPage.value = index < 0 ? 1 : Math.floor(index / VOUCHER_PAGE_SIZE) + 1;
  }
}
function retryVouchers() {
  if (!props.vouchersReady) emit("initializeVouchers");
  else if (voucherDisplayMode.value === "all") emit("allVouchers");
  else emit("moreVouchers");
}
async function selectVoucher(id: string) {
  selectedVoucher.value = selectedVoucher.value === id ? "" : id;
  await nextTick();
  if (typeof document !== "undefined") document.querySelector<HTMLElement>(".activity-section .voucher-card.is-open")?.scrollIntoView({ block: "nearest" });
}
function voucherTotal(voucher: BriefVoucher, side: "debit_fen" | "credit_fen") {
  return voucher.lines.reduce((total, line) => total + fen(line[side]), 0n);
}
function voucherAssets(voucher: BriefVoucher) {
  const assets = new Map<string, NonNullable<BriefVoucher["asset"]>>();
  if (voucher.asset) assets.set(voucher.asset.asset_id, voucher.asset);
  for (const asset of voucher.asset_members) assets.set(asset.asset_id, asset);
  return [...assets.values()];
}
function assetLabel(asset: NonNullable<BriefVoucher["asset"]>) {
  return asset.name || asset.code || "资产卡片";
}
function assetTarget(assetId: string) {
  return { name: "assets", query: { company_id: route.query.company_id, period: props.period, asset_id: assetId }, hash: "#asset-card-target" };
}
function voucherDate(voucher: BriefVoucher) {
  return voucher.date || voucher.recognition.label;
}
function activityDate(item: BriefActivityRow) {
  if (!item.date) return item.recognition.precision === "month"
    ? item.recognition.period : "日期未提供";
  const parts = item.date.split("-");
  return parts.length === 3 ? `${Number(parts[1])} 月 ${Number(parts[2])} 日` : item.date;
}
watch(() => props.vouchersReady, ready => {
  if (!ready || mode.value !== "voucher" || !manualVoucherView.value) return;
  if (voucherDisplayMode.value === "all" && props.vouchersHasMore) emit("allVouchers");
  else if (pendingVoucherPage.value !== null) changeVoucherPage(pendingVoucherPage.value);
});
watch(() => props.active, active => {
  if (!active || mode.value !== "voucher" || !manualVoucherView.value) return;
  if (!props.vouchersReady) emit("initializeVouchers");
  else if (voucherDisplayMode.value === "all" && props.vouchersHasMore) emit("allVouchers");
});
watch(() => props.vouchers.length, () => {
  const target = pendingVoucherPage.value;
  if (target !== null && props.vouchers.length >= Math.min(target * VOUCHER_PAGE_SIZE, props.voucherCount)) {
    voucherPage.value = target; pendingVoucherPage.value = null; selectedVoucher.value = "";
  }
});
watch(() => [route.query.company_id, props.period, props.snapshotVersion], () => {
  mode.value = "business"; voucherDisplayMode.value = "paged";
  voucherPage.value = 1; pendingVoucherPage.value = null; selectedVoucher.value = "";
  manualVoucherView.value = false;
  expandedBusinessKey.value = ""; clearPreview();
});
watch(() => props.voucherPreviewIndex, () => clearPreview());
watch(mode, (value, previous) => {
  clearPreview();
  if (previous === "voucher" && value === "business") emit("pauseVouchers");
});
watch(() => [props.focusedVoucher, props.focusedVoucherSelection] as const, async ([voucher]) => {
  if (!voucher) return;
  manualVoucherView.value = false;
  mode.value = "voucher"; selectedVoucher.value = voucher.voucher_version_id;
  const index = props.vouchers.findIndex(item => item.voucher_version_id === voucher.voucher_version_id);
  if (index >= 0) voucherPage.value = Math.floor(index / VOUCHER_PAGE_SIZE) + 1;
  await nextTick();
  if (typeof document !== "undefined") document.getElementById("selected-voucher")?.scrollIntoView({ block: "center" });
}, { immediate: true });
const selectedGroup = ref("");
const activeGroup = computed(() => props.groups.some(group => group.key === selectedGroup.value)
  ? selectedGroup.value : props.groups[0]?.key || "");
const availableItems = computed(() => props.focusedActivity && !props.items.some(item => item.key === props.focusedActivity?.key)
  ? [props.focusedActivity, ...props.items] : props.items);
const visibleItems = computed(() => availableItems.value.filter(item => item.group === activeGroup.value));
watch(activeGroup, () => { expandedBusinessKey.value = ""; clearPreview(); });
watch(() => props.focusedActivity, async item => {
  if (!item) return;
  selectedGroup.value = item.group;
  await nextTick();
  if (typeof document !== "undefined") document.getElementById("selected-business")?.scrollIntoView({ block: "center" });
}, { immediate: true });
</script>

<template>
  <section class="activity-section" aria-labelledby="activity-title">
    <header class="section-heading">
      <div><h2 id="activity-title">本月发生</h2><p>本月共 {{ activityCount }} 项 · {{ voucherCount }} 张凭证</p></div>
      <div class="heading-controls">
        <div v-if="mode === 'voucher'" class="voucher-display-toggle">
          <span :class="{ active: voucherDisplayMode === 'paged' }">分页</span>
          <button type="button" role="switch" :aria-checked="voucherDisplayMode === 'all'"
            :aria-label="voucherDisplayMode === 'paged' ? '改为全部显示凭证' : '改为分页显示凭证'" @click="toggleVoucherDisplayMode"><span aria-hidden="true"></span></button>
          <span :class="{ active: voucherDisplayMode === 'all' }">全部</span>
        </div>
        <div class="view-switch" role="group" aria-label="本月业务查看方式">
          <button type="button" :aria-pressed="mode === 'business'" @click="mode = 'business'">按业务</button>
          <button type="button" :aria-pressed="mode === 'voucher'" @click="selectVoucherMode">按凭证</button>
        </div>
      </div>
    </header>
    <p v-if="vouchersLoading" role="status" class="voucher-load-status">正在读取凭证…</p>
    <p v-if="vouchersError && mode === 'business'" role="alert" class="voucher-load-status">{{ vouchersError }} · 请再次选择对应凭证。</p>
    <div v-if="mode === 'business'" class="workbench" data-section-focus tabindex="-1">
      <nav class="index" aria-label="业务分类">
        <span class="category-heading">业务分类</span>
        <button v-for="group in groups" :key="group.key" type="button" :aria-pressed="activeGroup === group.key" @click="selectedGroup = group.key"><strong>{{ group.label }}</strong><b>{{ group.event_count }} 项</b></button>
      </nav>
      <div class="detail">
        <div class="list-columns business-list-columns" aria-hidden="true"><span>业务时间</span><span>对象</span><span>事项</span><span>状态</span><span class="column-money">业务金额</span><span class="column-action">凭证</span><span></span></div>
        <ul class="event-list">
          <li v-for="item in visibleItems" :id="item.key === focusedActivity?.key ? 'selected-business' : undefined" :key="item.key" :class="['event-row', 'business-list-row', { highlighted: item.key === focusedActivity?.key, expandable: !!item.subject_id }]" :role="item.subject_id ? 'button' : undefined" :tabindex="item.subject_id ? 0 : undefined" :aria-expanded="item.subject_id ? expandedBusinessKey === item.key : undefined" :aria-label="item.subject_id ? `${item.party || item.title}，${expandedBusinessKey === item.key ? '收起' : '展开'}业务详情` : undefined" @click="toggleBusiness(item, $event)" @keydown="businessKeydown(item, $event)">
            <small class="event-date business-list-date">{{ activityDate(item) }}<template v-if="!item.date && item.recognition.precision === 'month'"><br />按月确认</template></small>
            <div class="event-copy business-list-object"><strong>{{ item.party || "无需往来对象" }}</strong></div>
            <span class="event-matter business-list-matter">{{ item.title }}</span>
            <span :class="['state', 'business-list-state', { correction: item.state === '更正原业务' || item.state.includes('冲正') || item.state.includes('撤回') }]">{{ item.state }}</span>
            <span class="event-money business-list-money"><small>{{ item.amount_label }}</small><b>{{ item.amount_fen == null ? "待核对" : formatFen(item.amount_fen) }}</b></span>
            <span v-if="item.voucher_version_id" class="event-voucher-link business-list-voucher" @mouseenter="showPreview(item)" @mouseleave="clearPreview(item.key)">
              <button type="button" class="event-voucher-button" :disabled="!vouchersByVersion.has(item.voucher_version_id)" :aria-describedby="previewBusinessKey === item.key && previewVoucher ? 'activity-voucher-preview' : undefined" @focus="showPreview(item)" @blur="clearPreview(item.key)" @click.stop="openBusinessVoucher(item)">{{ vouchersByVersion.has(item.voucher_version_id) ? '凭证 ' + vouchersByVersion.get(item.voucher_version_id)?.number : '凭证未加载' }}</button>
              <span v-if="previewBusinessKey === item.key && previewVoucher" id="activity-voucher-preview" class="event-voucher-preview" :class="{ correction: !!previewVoucher.reverses_version_id }" :style="{ '--preview-offset': `${previewPosition.offset}px`, '--preview-arrow-top': previewPosition.arrowTop }" role="tooltip">
                <span class="voucher-preview-heading"><span><small>凭证 {{ previewVoucher.number }} · {{ voucherDate(previewVoucher) }}</small><strong>{{ previewVoucher.list_summary }}</strong></span><span class="voucher-preview-amount"><small>{{ previewVoucher.business_amount_label }}</small><b>{{ formatFen(previewVoucher.business_amount_fen) }}</b></span></span>
                <span class="voucher-preview-lines"><span v-for="line in previewVoucher.lines" :key="line.line_number"><span>{{ line.account }}</span><strong>{{ fen(line.debit_fen) ? '借 ' + formatFen(line.debit_fen) : '贷 ' + formatFen(line.credit_fen) }}</strong></span></span>
                <span class="voucher-preview-footer"><span :class="['state', { correction: !!previewVoucher.reverses_version_id }]">{{ previewVoucher.state }}</span><small>点击打开凭证详情</small></span>
              </span>
            </span>
            <span v-else class="event-voucher-link business-list-voucher">—</span>
            <svg v-if="item.subject_id" class="row-chevron business-list-arrow" :class="{ expanded: expandedBusinessKey === item.key }" viewBox="0 0 16 16" aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg><span v-else class="business-list-arrow-space" aria-hidden="true"></span>
            <BusinessStatusDetails v-if="item.subject_id" :subject-id="item.subject_id" :period="period" :snapshot-version="snapshotVersion" :activity-context="item" :expanded="expandedBusinessKey === item.key" hide-summary presentation="brief" @click.stop @keydown.stop @changed="$emit('changed')" />
          </li>
        </ul>
        <p v-if="!visibleItems.length" class="empty">当前已加载记录中没有此类业务。</p>
        <slot name="pagination" />
      </div>
    </div>
    <div v-else class="voucher-view" :aria-busy="vouchersLoading || false">
      <p class="voucher-load-status"><template v-if="vouchersReady">已加载 {{ vouchers.length }} / 本月 {{ voucherCount }} 张凭证<template v-if="voucherDisplayMode === 'all' && vouchersHasMore"> · 全部凭证尚未读取完</template><template v-if="pendingVoucherPage !== null"> · 正在读取第 {{ pendingVoucherPage }} 页</template></template><template v-else-if="selectedVoucher && focusedVoucher">当前显示选中凭证 · 本月 {{ voucherCount }} 张凭证</template><template v-else>本月 {{ voucherCount }} 张凭证 · 按凭证号读取</template></p>
      <div class="voucher-list" aria-label="凭证清单" data-section-focus tabindex="-1">
        <article v-for="voucher in visibleVouchers" :key="voucher.voucher_version_id" :id="voucher.voucher_version_id === focusedVoucher?.voucher_version_id ? 'selected-voucher' : undefined"
          :class="['voucher-card', { 'is-open': selectedVoucher === voucher.voucher_version_id }]" tabindex="-1">
          <button type="button" class="voucher-row" :aria-expanded="selectedVoucher === voucher.voucher_version_id" @click="selectVoucher(voucher.voucher_version_id)">
            <span class="voucher-reference"><strong>凭证 {{ voucher.number }}</strong><small>{{ voucherDate(voucher) }}</small></span>
            <span class="voucher-copy"><strong>{{ voucher.list_summary }}</strong><small>{{ voucher.type }}</small></span>
            <span :class="['state', { correction: voucher.state.includes('冲正') }]">{{ voucher.state }}</span>
            <span class="voucher-row-amount"><small>{{ voucher.business_amount_label }}</small><strong>{{ formatFen(voucher.business_amount_fen) }}</strong></span>
            <span class="voucher-chevron" aria-hidden="true"></span>
          </button>
          <section v-if="selectedVoucher === voucher.voucher_version_id" class="voucher-inline-detail" :aria-label="`${voucher.number} 凭证明细`">
            <p v-if="voucher.reverses_version_id" class="voucher-correction">本凭证用于冲销原记录。</p>
            <p class="voucher-summary">{{ voucher.summary }}</p>
            <div v-if="voucherAssets(voucher).length" class="voucher-asset-references"><span>对应资产</span><div class="voucher-asset-links"><RouterLink v-for="asset in voucherAssets(voucher)" :key="asset.asset_id" class="voucher-asset-link" :to="assetTarget(asset.asset_id)">{{ assetLabel(asset) }}</RouterLink></div></div>
            <BusinessStatusDetails :subject-id="voucher.subject_id" :period="period" :snapshot-version="snapshotVersion" summary-label="业务详情" @changed="$emit('changed')" />
            <div class="table-wrap"><table>
              <colgroup><col class="voucher-account-column" /><col class="voucher-party-column" /><col class="voucher-amount-column" /><col class="voucher-amount-column" /></colgroup>
              <thead><tr><th>科目</th><th>往来对象</th><th class="number">借方</th><th class="number">贷方</th></tr></thead>
              <tbody><tr v-for="line in voucher.lines" :key="line.line_number">
                <td data-label="科目"><small>{{ line.code }}</small><strong>{{ line.account }}</strong><RouterLink v-if="line.asset" :to="assetTarget(line.asset.asset_id)">{{ assetLabel(line.asset) }}</RouterLink></td>
                <td data-label="往来对象"><template v-if="line.parties.length > 1"><span v-for="(party, index) in line.parties" :key="index" class="line-party">{{ party.name }} · {{ formatFen(party.amount_fen) }}</span></template><span v-else :class="{ party: line.party }">{{ line.party || (line.party_state === 'unresolved' ? '见凭证业务说明' : '—') }}</span><small v-if="line.source_label">业务来源：{{ line.source_label }}</small></td>
                <td class="number" data-label="借方">{{ fen(line.debit_fen) ? formatFen(line.debit_fen) : '—' }}</td>
                <td class="number" data-label="贷方">{{ fen(line.credit_fen) ? formatFen(line.credit_fen) : '—' }}</td>
              </tr></tbody>
              <tfoot><tr><th colspan="2">借贷合计</th><td class="number" data-label="借方合计">{{ formatFen(voucherTotal(voucher, 'debit_fen')) }}</td><td class="number" data-label="贷方合计">{{ formatFen(voucherTotal(voucher, 'credit_fen')) }}</td></tr></tfoot>
            </table></div>
          </section>
        </article>
      </div>
      <p v-if="!visibleVouchers.length && !vouchersLoading" class="empty">{{ voucherCount ? '凭证尚未读取。' : '本月没有凭证。' }}</p>
      <div v-if="vouchersError" class="voucher-load-status" role="alert">{{ vouchersError }} <button type="button" :disabled="vouchersLoading" @click="retryVouchers">重新读取</button></div>
      <footer v-if="vouchersReady && voucherDisplayMode === 'paged' && voucherPageCount > 1" class="business-pagination voucher-pagination" aria-label="凭证分页">
        <div><button type="button" :disabled="voucherPage === 1 || vouchersLoading" @click="changeVoucherPage(voucherPage - 1)">上一页</button><strong>{{ voucherPage }} / {{ voucherPageCount }}</strong><button type="button" :disabled="voucherPage === voucherPageCount || vouchersLoading" @click="changeVoucherPage(voucherPage + 1)">下一页</button></div>
      </footer>
      <button v-if="vouchersHasMore && !vouchersLoading && !vouchersError && voucherDisplayMode === 'all'" class="voucher-retry" type="button" @click="retryVouchers">继续读取全部凭证</button>
    </div>
  </section>
</template>

<style scoped>
.activity-section { min-width: 0; }
.section-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; margin-bottom: 14px; padding: 0 2px; }
h2 { margin: 0; font-size: 22px; letter-spacing: -0.025em; }
.section-heading > span { color: var(--muted); font-size: 13px; }
.workbench { --business-list-columns: 98px minmax(0, 1fr) minmax(0, .9fr) 68px 130px 64px 12px; display: grid; min-width: 0; grid-template-columns: 280px minmax(0, 1fr); border: 1px solid var(--line); border-radius: 14px; background: var(--surface); }
.index { display: grid; min-width: 0; align-content: start; gap: 3px; padding: 12px 10px; border-right: 1px solid var(--line); border-radius: 14px 0 0 14px; }
.category-heading { display: flex; min-height: 30px; align-items: center; padding: 0 12px 7px; color: var(--muted); font-size: 11px; letter-spacing: 0.04em; }
.index button { position: relative; display: grid; width: 100%; min-width: 0; min-height: 46px; grid-template-columns: minmax(0, 1fr) auto; gap: 5px 10px; align-items: center; padding: 11px 12px; border: 1px solid transparent; border-radius: 9px; background: transparent; color: var(--text); font: inherit; text-align: left; cursor: pointer; transition: background 140ms ease, border-color 140ms ease; }
.index button::before { position: absolute; top: 12px; bottom: 12px; left: -1px; width: 2px; border-radius: 999px; background: transparent; content: ""; }
.index button:hover { background: var(--surface-soft); }
.index button[aria-pressed="true"] { border-color: color-mix(in srgb, var(--accent) 16%, transparent); background: color-mix(in srgb, var(--accent-soft) 54%, var(--surface)); }
.index button[aria-pressed="true"]::before { background: var(--accent); }
.index button strong { min-width: 0; font-size: 14px; font-weight: 600; overflow-wrap: anywhere; }
.index button[aria-pressed="true"] strong { font-weight: 750; }
.index button b { color: var(--muted); font-size: 11px; font-weight: 600; white-space: nowrap; }
.index button[aria-pressed="true"] b { color: var(--accent); }
.detail { min-width: 0; padding: 12px 20px; }
.column-money, .column-action { text-align: right; }
.event-list { margin: 0; padding: 0; list-style: none; }
.state { padding: 2px 7px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); font-size: 11px; overflow-wrap: anywhere; }
.event-row :deep(.compact-status-details) { display: contents; }
.event-row :deep(.compact-status-panel) { grid-column: 1 / -1; }
.event-voucher-link { position: relative; min-width: 0; justify-self: end; font-size: 11px; color: var(--muted); }
.event-voucher-button { min-height: 32px; padding: 0 9px; border-radius: 8px; font-weight: 750; white-space: nowrap; }
.event-voucher-button:hover, .event-voucher-button:focus-visible { background: var(--accent-soft); }
.event-voucher-button:disabled { color: var(--muted); cursor: default; }
.event-voucher-preview { --preview-accent: var(--accent); position: absolute; top: 50%; right: calc(100% + 10px); z-index: 30; display: grid; width: min(380px, calc(100vw - 48px)); gap: 10px; padding: 13px; border: 1px solid color-mix(in srgb, var(--preview-accent) 20%, var(--line)); border-radius: 12px; background: var(--surface); box-shadow: var(--shadow-overlay); color: var(--text); pointer-events: none; text-align: left; transform: translateY(calc(-50% + var(--preview-offset))); }
.event-voucher-preview.correction { --preview-accent: var(--warning); }
.event-voucher-preview::after { position: absolute; top: calc(var(--preview-arrow-top) - 5px); right: -6px; width: 10px; height: 10px; border-top: 1px solid color-mix(in srgb, var(--preview-accent) 20%, var(--line)); border-right: 1px solid color-mix(in srgb, var(--preview-accent) 20%, var(--line)); background: var(--surface); content: ""; transform: rotate(45deg); }
.voucher-preview-heading, .voucher-preview-footer, .voucher-preview-lines > span { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.voucher-preview-heading > span { display: grid; min-width: 0; gap: 3px; }
.voucher-preview-heading small, .voucher-preview-footer small { color: var(--muted); font-size: 10px; }
.voucher-preview-heading strong { font-size: 13px; overflow-wrap: anywhere; }
.voucher-preview-amount { flex: none; text-align: right; }
.voucher-preview-amount b { font-size: 14px; white-space: nowrap; }
.voucher-preview-lines { display: grid; gap: 5px; padding: 8px 9px; border-radius: 8px; background: var(--surface-soft); }
.voucher-preview-lines > span { align-items: flex-start; min-width: 0; color: var(--muted); font-size: 11px; }
.voucher-preview-lines > span > span { min-width: 0; overflow-wrap: anywhere; }
.voucher-preview-lines strong { flex: none; color: var(--text); font-size: 11px; white-space: nowrap; }
.state.correction { background: var(--surface-soft); color: var(--muted); }
.highlighted, .event-row.highlighted:hover { background: var(--accent-soft); box-shadow: inset 0 0 0 2px var(--accent); border-radius: 8px; }
.empty { margin: 0; padding: 22px 4px; color: var(--muted); font-size: 13px; }
@media (max-width: 1199px) {
  .workbench { grid-template-columns: 264px minmax(0, 1fr); }
  .detail { padding: 12px; }
}
@media (max-width: 760px) {
  .section-heading { flex-direction: column; align-items: flex-start; gap: 7px; }
  .workbench { grid-template-columns: minmax(0, 1fr); }
  .index { padding: 10px; border-right: 0; border-bottom: 1px solid var(--line); border-radius: 14px 14px 0 0; }
  .category-heading { width: 100%; }
  .index button { min-height: 44px; padding: 9px 12px; }
  .detail { padding: 4px 12px; }
  .event-row :deep(.compact-status-panel) { grid-column: 1 / -1; }
}
.heading-controls {
  display: flex;
  flex: none;
  align-items: center;
  gap: 12px;
}

.voucher-display-toggle {
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--muted);
  font-size: 12px;
  white-space: nowrap;
}

.voucher-display-toggle > span {
  transition: color 140ms ease;
}

.voucher-display-toggle > span.active {
  color: var(--text);
  font-weight: 750;
}

.voucher-display-toggle button {
  position: relative;
  width: 38px;
  height: 22px;
  flex: none;
  padding: 0;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--surface-soft);
  cursor: pointer;
  transition: border-color 140ms ease, background 140ms ease;
}

.voucher-display-toggle button > span {
  position: absolute;
  top: 3px;
  left: 3px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--surface);
  box-shadow: 0 1px 4px rgb(18 45 31 / 20%);
  transition: transform 140ms ease;
}

.voucher-display-toggle button[aria-checked="true"] {
  border-color: var(--accent);
  background: var(--accent);
}

.voucher-display-toggle button[aria-checked="true"] > span {
  transform: translateX(16px);
}

.voucher-display-toggle button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.view-switch {
  display: grid;
  flex: none;
  grid-template-columns: repeat(2, 1fr);
  gap: 3px;
  padding: 3px;
  border: 1px solid var(--line);
  border-radius: 11px;
  background: var(--surface-soft);
}

.view-switch button {
  min-height: 34px;
  padding: 0 13px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--muted);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}

.view-switch button[aria-pressed="true"] {
  background: var(--surface);
  color: var(--text);
}

.business-pagination {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 2px 0;
  color: var(--muted);
  font-size: 12px;
}

.business-pagination > div {
  display: flex;
  align-items: center;
  gap: 8px;
}

.business-pagination button {
  min-height: 32px;
  padding: 0 10px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  color: var(--accent);
  font: inherit;
  cursor: pointer;
}

.business-pagination button:hover:not(:disabled) {
  border-color: var(--accent);
  background: var(--accent-soft);
}

.business-pagination button:disabled {
  color: var(--muted);
  cursor: default;
  opacity: 0.5;
}

.business-pagination strong {
  min-width: 42px;
  color: var(--text);
  text-align: center;
}

.voucher-view {
  min-width: 0;
}

.voucher-list {
  display: grid;
  gap: 0;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--surface);
}

.voucher-pagination {
  justify-content: flex-end;
}

.voucher-card {
  overflow: hidden;
  background: var(--surface);
}

.voucher-card + .voucher-card {
  border-top: 1px solid var(--line);
}

.voucher-card.is-open {
  background: color-mix(in srgb, var(--surface-soft) 42%, var(--surface));
}

.voucher-row {
  display: grid;
  width: 100%;
  min-height: 62px;
  grid-template-columns: 132px minmax(180px, 1fr) auto 132px 16px;
  grid-template-areas: "reference copy state amount chevron";
  gap: 14px;
  align-items: center;
  padding: 10px 16px;
  border: 0;
  background: transparent;
  color: var(--text);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.voucher-row:hover {
  background: var(--surface-soft);
}

.voucher-card.is-open > .voucher-row {
  background: var(--surface-soft);
}

.voucher-reference,
.voucher-copy {
  display: grid;
  min-width: 0;
  gap: 3px;
}

.voucher-reference {
  grid-area: reference;
}

.voucher-reference strong {
  color: var(--accent);
  font-size: 12px;
}

.voucher-reference small,
.voucher-copy small {
  overflow: hidden;
  color: var(--muted);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.voucher-copy {
  grid-area: copy;
}

.voucher-copy strong {
  min-width: 0;
  overflow: hidden;
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.voucher-row > .state {
  grid-area: state;
  min-height: 21px;
  padding: 1px 7px;
  font-size: 11px;
  white-space: nowrap;
}

.voucher-row-amount {
  grid-area: amount;
  font-size: 14px;
  text-align: right;
  white-space: nowrap;
}

.voucher-chevron {
  width: 7px;
  height: 7px;
  grid-area: chevron;
  border-right: 1.5px solid var(--muted);
  border-bottom: 1.5px solid var(--muted);
  transform: rotate(45deg) translateY(-2px);
  transition: transform 140ms ease;
}

.voucher-card.is-open .voucher-chevron {
  transform: rotate(225deg) translate(-1px, -1px);
}

.voucher-inline-detail {
  --voucher-account-width: 35%;
  --voucher-party-width: 33%;
  --voucher-amount-width: 16%;
  min-width: 0;
  margin: 0;
  padding: 2px 16px 16px;
  border-top: 1px solid var(--line);
  background: var(--surface-soft);
}

.voucher-account-column {
  width: var(--voucher-account-width);
}

.voucher-party-column {
  width: var(--voucher-party-width);
}

.voucher-amount-column {
  width: var(--voucher-amount-width);
}

.table-wrap {
  overflow-x: auto;
  margin-top: 14px;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

th,
td {
  padding: 10px 9px;
  text-align: left;
  vertical-align: top;
}

th {
  background: var(--surface-soft);
  color: var(--muted);
  font-size: 11px;
}

th:first-child {
  border-radius: 8px 0 0 8px;
}

th:last-child {
  border-radius: 0 8px 8px 0;
}

tbody tr:nth-child(even) {
  background: color-mix(in srgb, var(--surface-soft) 55%, transparent);
}

td:first-child small,
td:first-child strong {
  display: block;
}

td:first-child small {
  margin-bottom: 2px;
  color: var(--muted);
}

td:nth-child(2) > small {
  display: block;
  margin-top: 4px;
  color: var(--muted);
}

.number {
  text-align: right;
  white-space: nowrap;
}


.section-heading p { margin: 5px 0 0; color: var(--muted); font-size: 13px; }
.event-voucher-button { border: 0; background: transparent; color: var(--accent); font: inherit; font-size: 11px; font-weight: 750; cursor: pointer; }
.voucher-retry { justify-self: start; padding: 4px 0; border: 0; background: transparent; color: var(--accent); font: inherit; font-size: 12px; cursor: pointer; }
.voucher-row-amount { display: grid; min-width: 0; gap: 3px; white-space: normal; overflow-wrap: anywhere; }
.voucher-row-amount small, .voucher-load-status { color: var(--muted); font-size: 12px; }
.voucher-copy strong, .voucher-copy small, .voucher-reference small { white-space: normal; overflow-wrap: anywhere; }
.voucher-summary { margin: 12px 0; font-size: 13px; overflow-wrap: anywhere; }
.voucher-correction { margin: 12px 0; color: var(--amber); font-size: 12px; }
.voucher-asset-references { margin: 12px 0; font-size: 12px; color: var(--muted); }
.voucher-asset-links { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; }
.voucher-asset-link { color: var(--accent); overflow-wrap: anywhere; }
.line-party { display: block; margin-bottom: 4px; overflow-wrap: anywhere; }
.table-wrap { overflow-x: visible; }
table { table-layout: fixed; }
td { overflow-wrap: anywhere; }
tfoot th, tfoot td { border-top: 1px solid var(--line); }
@media (max-width: 1024px) {
  .heading-controls { flex-wrap: wrap; max-width: 100%; }
  .voucher-row { grid-template-columns: minmax(0, 1fr) auto 14px; grid-template-areas: "reference amount chevron" "copy state chevron"; gap: 6px 10px; padding: 10px 11px; }
  .voucher-row > .state { justify-self: end; }
  .voucher-row-amount { max-width: 130px; font-size: 13px; }
}
@media (max-width: 760px) {
  .event-voucher-preview { display: none; }
  .event-voucher-button { min-height: 44px; }
  .heading-controls { width: 100%; }
  .view-switch button { min-height: 44px; }
  .voucher-inline-detail { padding: 2px 11px 13px; }
  table, tbody, tfoot, tr, td { display: block; width: 100%; }
  thead, tfoot th { display: none; }
  tbody { display: grid; gap: 6px; }
  tr { padding: 8px 0; border-radius: 8px; background: var(--surface-soft); }
  td, td:first-child { display: grid; grid-template-columns: 86px minmax(0, 1fr); gap: 8px; padding: 5px 0; text-align: left; white-space: normal; }
  td::before { color: var(--muted); font-size: 11px; font-weight: 750; content: attr(data-label); }
  td > *, td:first-child small, td:first-child strong { grid-column: 2; }
}
</style>
