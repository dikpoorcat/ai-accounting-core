<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import { useRoute } from "vue-router";
import type { BriefActivityGroup, BriefActivityRow, BriefVoucher } from "../../api/brief";
import { fen, formatFen } from "../../utils/money";
import BusinessStatusDetails from "../BusinessStatusDetails.vue";

const props = defineProps<{
  groups: BriefActivityGroup[]; items: BriefActivityRow[]; activityCount: number;
  focusedActivity?: BriefActivityRow | null; period: string; snapshotVersion?: string | null;
  vouchers: BriefVoucher[]; voucherCount: number; focusedVoucher?: BriefVoucher | null;
  vouchersLoading?: boolean; vouchersError?: string; vouchersHasMore?: boolean;
}>();
const emit = defineEmits<{ changed: []; moreVouchers: []; allVouchers: []; requestVoucher: [voucherVersionId: string] }>();
const route = useRoute();
const mode = ref<"business" | "voucher">("business");
const voucherDisplayMode = ref<"paged" | "all">("paged");
const voucherPage = ref(1), pendingVoucherPage = ref<number | null>(null);
const selectedVoucher = ref("");
const VOUCHER_PAGE_SIZE = 20;
const voucherPageCount = computed(() => Math.max(1, Math.ceil(props.voucherCount / VOUCHER_PAGE_SIZE)));
const visibleVouchers = computed(() => {
  const start = (voucherPage.value - 1) * VOUCHER_PAGE_SIZE;
  const items = voucherDisplayMode.value === "all" ? props.vouchers : props.vouchers.slice(start, start + VOUCHER_PAGE_SIZE);
  const focused = props.focusedVoucher;
  return focused && selectedVoucher.value === focused.voucher_version_id
    && !items.some(item => item.voucher_version_id === focused.voucher_version_id) ? [focused, ...items] : items;
});
function changeVoucherPage(value: number) {
  const target = Math.max(1, Math.min(value, voucherPageCount.value));
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
  if (voucherDisplayMode.value === "all" && props.vouchersHasMore) emit("allVouchers");
  if (voucherDisplayMode.value === "paged") {
    const index = props.vouchers.findIndex(item => item.voucher_version_id === selectedVoucher.value);
    voucherPage.value = index < 0 ? 1 : Math.floor(index / VOUCHER_PAGE_SIZE) + 1;
  }
}
function retryVouchers() {
  if (voucherDisplayMode.value === "all") emit("allVouchers");
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
watch(() => props.vouchers.length, () => {
  const target = pendingVoucherPage.value;
  if (target !== null && props.vouchers.length >= Math.min(target * VOUCHER_PAGE_SIZE, props.voucherCount)) {
    voucherPage.value = target; pendingVoucherPage.value = null; selectedVoucher.value = "";
  }
});
watch(() => [props.period, props.snapshotVersion], () => {
  mode.value = "business"; voucherDisplayMode.value = "paged";
  voucherPage.value = 1; pendingVoucherPage.value = null; selectedVoucher.value = "";
});
watch(() => props.focusedVoucher, async voucher => {
  if (!voucher) return;
  mode.value = "voucher"; selectedVoucher.value = voucher.voucher_version_id;
  const index = props.vouchers.findIndex(item => item.voucher_version_id === voucher.voucher_version_id);
  if (index >= 0) voucherPage.value = Math.floor(index / VOUCHER_PAGE_SIZE) + 1;
  await nextTick();
  if (typeof document !== "undefined") document.getElementById("selected-voucher")?.scrollIntoView({ block: "center" });
}, { immediate: true });
const selectedGroup = ref("");
const availableItems = computed(() => props.focusedActivity && !props.items.some(item => item.key === props.focusedActivity?.key)
  ? [props.focusedActivity, ...props.items] : props.items);
const visibleItems = computed(() => availableItems.value.filter(item => !selectedGroup.value || item.group === selectedGroup.value));
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
      <div><h2 id="activity-title">已入账变化</h2><p>本月共 {{ activityCount }} 项 · {{ voucherCount }} 张凭证</p></div>
      <div class="heading-controls">
        <div v-if="mode === 'voucher'" class="voucher-display-toggle">
          <span :class="{ active: voucherDisplayMode === 'paged' }">分页</span>
          <button type="button" role="switch" :aria-checked="voucherDisplayMode === 'all'"
            :aria-label="voucherDisplayMode === 'paged' ? '改为全部显示凭证' : '改为分页显示凭证'" @click="toggleVoucherDisplayMode"><span aria-hidden="true"></span></button>
          <span :class="{ active: voucherDisplayMode === 'all' }">全部</span>
        </div>
        <div class="view-switch" role="group" aria-label="本月业务查看方式">
          <button type="button" :aria-pressed="mode === 'business'" @click="mode = 'business'">按业务</button>
          <button type="button" :aria-pressed="mode === 'voucher'" @click="mode = 'voucher'">按凭证</button>
        </div>
      </div>
    </header>
    <p v-if="vouchersLoading" role="status" class="voucher-load-status">正在读取凭证…</p>
    <p v-if="vouchersError && mode === 'business'" role="alert" class="voucher-load-status">{{ vouchersError }} · 请再次选择对应凭证。</p>
    <div v-if="mode === 'business'" class="workbench">
      <nav class="index" aria-label="业务分类">
        <span class="category-heading">业务分类</span>
        <button type="button" :aria-pressed="!selectedGroup" @click="selectedGroup = ''"><strong>全部</strong></button>
        <button v-for="group in groups" :key="group.key" type="button" :aria-pressed="selectedGroup === group.key" @click="selectedGroup = group.key"><strong>{{ group.label }}</strong><b>{{ group.event_count }} 项</b></button>
      </nav>
      <div class="detail">
        <div class="list-columns" aria-hidden="true"><span>对象与事项</span><span>业务时间</span><span>状态</span><span class="column-money">业务金额</span><span class="column-action">详情</span></div>
        <ul class="event-list">
          <li v-for="item in visibleItems" :id="item.key === focusedActivity?.key ? 'selected-business' : undefined" :key="item.key" :class="['event-row', { highlighted: item.key === focusedActivity?.key }]">
            <div class="event-copy"><strong>{{ item.party || item.title }}</strong><p>{{ item.description }}</p><button v-if="item.voucher_version_id" type="button" class="event-voucher-button" @click="$emit('requestVoucher', item.voucher_version_id)">查看对应凭证</button></div>
            <small class="event-date">{{ item.date || item.recognition.period + ' · 按月确认' }}</small>
            <span class="state">{{ item.state }}</span>
            <span class="event-money"><small>{{ item.amount_label }}</small><b>{{ formatFen(item.amount_fen) }}</b></span>
            <BusinessStatusDetails :subject-id="item.subject_id" :period="period" :snapshot-version="snapshotVersion" summary-label="业务详情" presentation="brief" @changed="$emit('changed')" />
          </li>
        </ul>
        <p v-if="!visibleItems.length" class="empty">当前已加载记录中没有此类业务。</p>
        <slot name="pagination" />
      </div>
    </div>
    <div v-else class="voucher-view" :aria-busy="vouchersLoading || false">
      <p class="voucher-load-status">已加载 {{ vouchers.length }} / 本月 {{ voucherCount }} 张凭证<template v-if="voucherDisplayMode === 'all' && vouchersHasMore"> · 全部凭证尚未读取完</template><template v-if="pendingVoucherPage !== null"> · 正在读取第 {{ pendingVoucherPage }} 页</template></p>
      <div class="voucher-list" aria-label="凭证清单">
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
      <footer v-if="voucherDisplayMode === 'paged' && voucherPageCount > 1" class="business-pagination voucher-pagination" aria-label="凭证分页">
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
.workbench { --list-columns: minmax(110px, 1fr) 100px 88px 144px 80px; display: grid; min-width: 0; grid-template-columns: 280px minmax(0, 1fr); border: 1px solid var(--line); border-radius: 14px; background: var(--surface); }
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
.list-columns { display: grid; grid-template-columns: var(--list-columns); gap: 12px; align-items: center; min-height: 30px; padding: 0 4px 8px; border-bottom: 1px solid var(--line); color: var(--muted); font-size: 11px; }
.column-money, .column-action { text-align: right; }
.event-list { margin: 0; padding: 0; list-style: none; }
.event-row { position: relative; display: grid; min-width: 0; min-height: 76px; grid-template-columns: var(--list-columns); gap: 12px; align-items: center; padding: 14px 4px; transition: background 140ms ease; }
.event-row + .event-row { border-top: 1px solid var(--line); }
.event-row:hover, .event-row:focus-within { background: var(--surface-soft); }
.event-copy { display: grid; min-width: 0; gap: 2px; overflow-wrap: anywhere; }
.event-copy strong { font-size: 14px; }
.event-copy p { margin: 0; color: var(--muted); font-size: 11px; line-height: 1.55; }
.event-date { color: var(--muted); font-size: 11px; overflow-wrap: anywhere; }
.state { justify-self: start; padding: 2px 7px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); font-size: 11px; overflow-wrap: anywhere; }
.event-money { display: grid; min-width: 0; gap: 2px; text-align: right; }
.event-money small { color: var(--muted); font-size: 11px; }
.event-money b { font-size: 14px; overflow-wrap: anywhere; }
.event-row :deep(.compact-status-details) { display: contents; }
.event-row :deep(.compact-status-trigger) { grid-column: 5; justify-self: end; }
.event-row :deep(.compact-status-panel) { grid-column: 1 / -1; }
.highlighted, .event-row.highlighted:hover { background: var(--accent-soft); box-shadow: inset 0 0 0 2px var(--accent); border-radius: 8px; }
.empty { margin: 0; padding: 22px 4px; color: var(--muted); font-size: 13px; }
@media (max-width: 1199px) {
  .workbench { --list-columns: minmax(100px, 1fr) 86px 76px 110px 72px; grid-template-columns: 220px minmax(0, 1fr); }
  .detail { padding: 12px; }
  .list-columns, .event-row { gap: 8px; }
}
@media (max-width: 1024px) {
  .section-heading { flex-direction: column; align-items: flex-start; gap: 7px; }
  .workbench { grid-template-columns: minmax(0, 1fr); }
  .index { display: flex; flex-wrap: wrap; padding: 10px; border-right: 0; border-bottom: 1px solid var(--line); border-radius: 14px 14px 0 0; }
  .category-heading { width: 100%; }
  .index button { width: auto; max-width: 100%; min-height: 44px; padding: 9px 12px; }
  .list-columns { display: none; }
  .event-row { grid-template-columns: minmax(0, 1fr); gap: 6px; padding: 14px 4px; }
  .event-money { text-align: left; }
  .event-row :deep(.compact-status-trigger) { grid-column: 1; justify-self: start; }
  .event-row :deep(.compact-status-panel) { grid-column: 1; }
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
.event-voucher-button, .voucher-retry { justify-self: start; padding: 4px 0; border: 0; background: transparent; color: var(--accent); font: inherit; font-size: 12px; cursor: pointer; }
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
