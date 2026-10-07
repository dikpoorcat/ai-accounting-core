<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, shallowRef, watch } from "vue";
import { useRoute } from "vue-router";
import type { BriefActivityGroup, BriefActivityRow, BriefVoucher } from "../../api/brief";
import type { BusinessStatusCacheEntry } from "../../api/businessStatus";
import { fen, formatFen } from "../../utils/money";
import BusinessStatusDetails from "../BusinessStatusDetails.vue";

const props = withDefaults(defineProps<{
  groups: BriefActivityGroup[]; items: BriefActivityRow[]; activityCount: number;
  focusedActivity?: BriefActivityRow | null; period: string; snapshotVersion?: string | null;
  vouchers: BriefVoucher[]; voucherCount: number; focusedVoucher?: BriefVoucher | null;
  voucherPreviewIndex?: ReadonlyMap<string, BriefVoucher>; focusedVoucherSelection?: number;
  vouchersLoading?: boolean; vouchersError?: string; vouchersHasMore?: boolean; vouchersReady?: boolean; active?: boolean; refreshing?: boolean;
}>(), { vouchersReady: true, active: true, refreshing: false });
const emit = defineEmits<{ changed: []; moreVouchers: []; allVouchers: []; initializeVouchers: []; pauseVouchers: []; requestVoucher: [voucherVersionId: string] }>();
const route = useRoute();
const mode = ref<"business" | "voucher">("business");
const voucherDisplayMode = ref<"paged" | "all">("paged");
const voucherPage = ref(1), pendingVoucherPage = ref<number | null>(null);
const selectedVoucher = ref("");
const voucherProgressCache = new Map<string, BusinessStatusCacheEntry>();
const manualVoucherView = ref(false);
const expandedBusinessKey = ref(""), previewBusinessKey = ref(""), previewVoucherId = ref("");
const previewPosition = ref({ offset: 0, arrowTop: "50%" });
let previewPositionGeneration = 0;
const progressVoucher = shallowRef<BriefVoucher | null>(null);
const progressPanel = ref<HTMLElement | null>(null);
const progressIsMobile = ref(false), progressPosition = ref({ left: 12, top: 12, side: "left", arrow: 14, ready: false });
let progressAnchor: HTMLButtonElement | null = null;
let progressCloseTimer: ReturnType<typeof setTimeout> | undefined;
let progressObserver: ResizeObserver | undefined;
let progressGeneration = 0, progressAnchorHovered = false, progressPanelHovered = false, suppressProgressFocus = false;
let mobileLock: { overflow: string; app: HTMLElement | null; inert: boolean } | null = null;
function cancelProgressClose() { clearTimeout(progressCloseTimer); progressCloseTimer = undefined; }
function lockProgressBackground() {
  if (progressIsMobile.value && !mobileLock) {
    const app = document.getElementById("app");
    mobileLock = { overflow: document.body.style.overflow, app, inert: app?.inert ?? false };
    document.body.style.overflow = "hidden";
    if (app) app.inert = true;
  } else if (!progressIsMobile.value && mobileLock) unlockProgressBackground();
}
function unlockProgressBackground() {
  if (!mobileLock) return;
  document.body.style.overflow = mobileLock.overflow;
  if (mobileLock.app) mobileLock.app.inert = mobileLock.inert;
  mobileLock = null;
}
function closeProgress(returnFocus = false) {
  if (!progressVoucher.value && !progressAnchor) return;
  cancelProgressClose(); progressGeneration += 1;
  progressObserver?.disconnect(); progressObserver = undefined;
  if (typeof document !== "undefined") {
    document.removeEventListener("pointerdown", progressOutside, true);
    document.removeEventListener("keydown", progressKeyboard, true);
    document.removeEventListener("focusin", progressFocusChanged);
    window.removeEventListener("resize", positionProgress);
    window.removeEventListener("scroll", positionProgress, true);
    unlockProgressBackground();
  }
  const anchor = progressAnchor;
  progressVoucher.value = null; progressAnchor = null;
  progressAnchorHovered = false; progressPanelHovered = false;
  if (returnFocus && anchor?.isConnected) {
    suppressProgressFocus = true; anchor.focus({ preventScroll: true });
    void nextTick(() => { suppressProgressFocus = false; });
  }
}
function positionProgress() {
  if (!progressVoucher.value || !progressPanel.value || !progressAnchor) return;
  const mobile = window.matchMedia("(max-width: 760px)").matches;
  const changed = mobile !== progressIsMobile.value;
  progressIsMobile.value = mobile; lockProgressBackground();
  if (mobile) {
    progressPosition.value.ready = true;
    if (changed) progressPanel.value.querySelector<HTMLButtonElement>(".voucher-progress-close")?.focus();
    return;
  }
  const anchor = progressAnchor.getBoundingClientRect();
  const leftWidth = anchor.left - 22;
  // Keep the preferred left placement at narrower desktop widths as well.
  progressPanel.value.style.setProperty('--progress-width', `${Math.min(680, window.innerWidth - 24, leftWidth >= 420 ? leftWidth : 680)}px`);
  const panel = progressPanel.value.getBoundingClientRect();
  if (anchor.bottom < 0 || anchor.top > window.innerHeight) { closeProgress(); return; }
  const clamp = (value: number, maximum: number) => Math.max(12, Math.min(value, maximum));
  let side = "left", left = anchor.left - 10 - panel.width;
  let top = clamp(anchor.top + anchor.height / 2 - panel.height / 2, window.innerHeight - 12 - panel.height);
  if (left < 12) {
    side = "right"; left = anchor.right + 10;
    if (left + panel.width > window.innerWidth - 12) {
      side = "below"; left = clamp(anchor.right - panel.width, window.innerWidth - 12 - panel.width);
      top = anchor.bottom + 10;
      if (top + panel.height > window.innerHeight - 12 && anchor.top - 10 - panel.height >= 12) {
        side = "above"; top = anchor.top - 10 - panel.height;
      }
      top = clamp(top, window.innerHeight - 12 - panel.height);
    }
  }
  const vertical = side === "left" || side === "right";
  const arrow = Math.max(14, Math.min(vertical ? anchor.top + anchor.height / 2 - top : anchor.left + anchor.width / 2 - left,
    (vertical ? panel.height : panel.width) - 14));
  progressPosition.value = { left, top, side, arrow, ready: true };
}
function scheduleProgressClose() {
  if (!progressVoucher.value || progressIsMobile.value) return;
  cancelProgressClose();
  progressCloseTimer = setTimeout(() => {
    const focused = document.activeElement;
    if (!progressAnchorHovered && !progressPanelHovered
      && !progressAnchor?.contains(focused) && !progressPanel.value?.contains(focused)) closeProgress();
  }, 150);
}
function progressAnchorLeave() { progressAnchorHovered = false; scheduleProgressClose(); }
function progressPanelEnter() { progressPanelHovered = true; cancelProgressClose(); }
function progressPanelLeave() { progressPanelHovered = false; scheduleProgressClose(); }
function progressFocusChanged() {
  if (progressPanel.value?.contains(document.activeElement) || progressAnchor?.contains(document.activeElement)) cancelProgressClose();
  else scheduleProgressClose();
}
function progressOutside(event: PointerEvent) {
  if (event.target instanceof Node && !progressAnchor?.contains(event.target) && !progressPanel.value?.contains(event.target)) {
    if (progressIsMobile.value) event.preventDefault();
    closeProgress(progressIsMobile.value);
  }
}
function progressKeyboard(event: KeyboardEvent) {
  if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeProgress(true); return; }
  if (event.key !== "Tab" || !progressPanel.value) return;
  const controls = [...progressPanel.value.querySelectorAll<HTMLElement>("button:not(:disabled), a[href], [tabindex]:not([tabindex='-1'])")]
    .filter(element => element.getClientRects().length > 0);
  const first = controls[0], last = controls.at(-1);
  if (progressIsMobile.value) {
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  } else if (!event.shiftKey && document.activeElement === progressAnchor) {
    event.preventDefault(); first?.focus();
  } else if (event.shiftKey && document.activeElement === first) {
    event.preventDefault(); progressAnchor?.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    const pageControls = [...document.querySelectorAll<HTMLElement>("button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex='-1'])")]
      .filter(element => element.getClientRects().length > 0 && !progressPanel.value?.contains(element));
    const next = pageControls[pageControls.indexOf(progressAnchor!) + 1];
    if (next) { event.preventDefault(); closeProgress(); next.focus(); }
  }
}
async function openProgress(voucher: BriefVoucher, event: Event, source: "hover" | "focus" | "click") {
  if (typeof window === "undefined" || !voucher.subject_id || !voucher.has_business_progress || props.refreshing) return;
  const mobile = window.matchMedia("(max-width: 760px)").matches;
  if ((mobile && source !== "click") || (source === "focus" && suppressProgressFocus)) return;
  cancelProgressClose();
  if (progressVoucher.value?.voucher_version_id === voucher.voucher_version_id) {
    if (source === "hover") progressAnchorHovered = true;
    return;
  }
  closeProgress();
  progressAnchor = event.currentTarget instanceof HTMLButtonElement ? event.currentTarget : null;
  if (!progressAnchor) return;
  const generation = ++progressGeneration;
  progressIsMobile.value = mobile; progressAnchorHovered = source === "hover";
  progressPosition.value.ready = false;
  progressVoucher.value = voucher;
  document.addEventListener("pointerdown", progressOutside, true);
  document.addEventListener("keydown", progressKeyboard, true);
  document.addEventListener("focusin", progressFocusChanged);
  window.addEventListener("resize", positionProgress);
  window.addEventListener("scroll", positionProgress, true);
  await nextTick();
  if (generation !== progressGeneration || !progressPanel.value) return;
  positionProgress();
  if (!progressPanel.value) return;
  if (mobile) progressPanel.value.querySelector<HTMLButtonElement>(".voucher-progress-close")?.focus();
  if (typeof ResizeObserver !== "undefined") {
    progressObserver = new ResizeObserver(positionProgress); progressObserver.observe(progressPanel.value);
  }
}
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
onBeforeUnmount(() => { clearPreview(); closeProgress(); });
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
  closeProgress();
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
  const lineAssets = new Set(voucher.lines.flatMap(line => line.asset ? [line.asset.asset_id] : []));
  return [...assets.values()].filter(asset => !lineAssets.has(asset.asset_id));
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
  if (ready) resumeVouchers();
}, { flush: "post" });
function resumeVouchers() {
  if (props.active === false || props.refreshing || mode.value !== "voucher" || !manualVoucherView.value) return;
  if (!props.vouchersReady) emit("initializeVouchers");
  else if (voucherDisplayMode.value === "all" && props.vouchersHasMore) emit("allVouchers");
  else if (pendingVoucherPage.value !== null) changeVoucherPage(pendingVoucherPage.value);
}
watch(() => [props.active, props.refreshing], resumeVouchers, { flush: "post" });
watch(() => props.vouchers.length, () => {
  const target = pendingVoucherPage.value;
  if (target !== null && props.vouchers.length >= Math.min(target * VOUCHER_PAGE_SIZE, props.voucherCount)) {
    voucherPage.value = target; pendingVoucherPage.value = null; selectedVoucher.value = "";
  }
});
watch(() => [route.query.company_id, props.period, props.snapshotVersion], () => {
  voucherProgressCache.clear();
  closeProgress();
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
watch(() => props.active, active => { if (active === false) closeProgress(); });
watch(() => props.refreshing, refreshing => { if (refreshing) closeProgress(); });
watch(() => [mode.value, voucherPage.value, voucherDisplayMode.value], () => closeProgress());
watch(visibleVouchers, vouchers => {
  if (progressVoucher.value && !vouchers.some(voucher => voucher.voucher_version_id === progressVoucher.value?.voucher_version_id)) closeProgress();
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
              <span v-if="previewBusinessKey === item.key && previewVoucher" id="activity-voucher-preview" class="event-voucher-preview dashboard-hover-preview" data-side="left" :class="{ correction: !!previewVoucher.reverses_version_id }" :style="{ '--preview-offset': `${previewPosition.offset}px`, '--preview-arrow': previewPosition.arrowTop }" role="tooltip">
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
          <div class="voucher-row-shell">
            <button type="button" class="voucher-row" :aria-expanded="selectedVoucher === voucher.voucher_version_id" @click="selectVoucher(voucher.voucher_version_id)">
              <span class="voucher-reference"><strong>凭证 {{ voucher.number }}</strong><small>{{ voucherDate(voucher) }}</small></span>
              <span class="voucher-copy"><strong>{{ voucher.summary }}</strong><span v-if="voucher.reverses_version_id" class="voucher-correction">本凭证用于冲销原记录。</span></span>
              <small class="voucher-type">{{ voucher.type }}</small>
              <span :class="['state', { correction: voucher.state.includes('冲正') }]">{{ voucher.state }}</span>
              <span class="voucher-row-amount"><small>{{ voucher.business_amount_label }}</small><strong>{{ formatFen(voucher.business_amount_fen) }}</strong></span>
              <svg class="voucher-chevron business-list-arrow" :class="{ expanded: selectedVoucher === voucher.voucher_version_id }" viewBox="0 0 16 16" aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg>
            </button>
            <button v-if="voucher.subject_id && voucher.has_business_progress" type="button" class="voucher-progress-button" aria-haspopup="dialog"
              :aria-label="`凭证 ${voucher.number} 的业务进展`" :aria-expanded="progressVoucher?.voucher_version_id === voucher.voucher_version_id"
              :aria-controls="progressVoucher?.voucher_version_id === voucher.voucher_version_id ? 'voucher-progress-popover' : undefined"
              @mouseenter="openProgress(voucher, $event, 'hover')" @mouseleave="progressAnchorLeave"
              @focus="openProgress(voucher, $event, 'focus')" @blur="scheduleProgressClose" @click="openProgress(voucher, $event, 'click')">业务进展</button>
            <span v-else class="voucher-progress-empty" aria-label="暂无业务进展">—</span>
          </div>
          <section v-if="selectedVoucher === voucher.voucher_version_id" class="voucher-inline-detail" :aria-label="`${voucher.number} 凭证明细`">
            <div class="table-wrap"><table aria-label="凭证分录">
              <colgroup><col class="voucher-account-column" /><col class="voucher-party-column" /><col class="voucher-source-column" /><col class="voucher-amount-column" /><col class="voucher-amount-column" /></colgroup>
              <thead><tr><th>科目</th><th>往来对象</th><th>业务来源</th><th class="number">借方</th><th class="number">贷方</th></tr></thead>
              <tbody><tr v-for="line in voucher.lines" :key="line.line_number">
                <td data-label="科目"><span class="voucher-line-account"><small>{{ line.code }}</small><strong :title="line.account">{{ line.account }}</strong></span></td>
                <td data-label="往来对象"><template v-if="line.parties.length > 1"><span v-for="(party, index) in line.parties" :key="index" class="line-party">{{ party.name }} · {{ formatFen(party.amount_fen) }}</span></template><span v-else :class="{ party: line.party }">{{ line.party || (line.party_state === 'unresolved' ? '见凭证业务说明' : '—') }}</span><small v-if="line.source_label || line.asset" class="voucher-mobile-source">业务来源：<RouterLink v-if="line.asset" class="voucher-asset-link" :to="assetTarget(line.asset.asset_id)">{{ line.source_label || assetLabel(line.asset) }}</RouterLink><template v-else>{{ line.source_label }}</template></small></td>
                <td class="voucher-line-source" data-label="业务来源"><RouterLink v-if="line.asset" class="voucher-asset-link" :to="assetTarget(line.asset.asset_id)">{{ line.source_label || assetLabel(line.asset) }}</RouterLink><template v-else>{{ line.source_label || '—' }}</template></td>
                <td class="number" data-label="借方">{{ fen(line.debit_fen) ? formatFen(line.debit_fen) : '—' }}</td>
                <td class="number" data-label="贷方">{{ fen(line.credit_fen) ? formatFen(line.credit_fen) : '—' }}</td>
              </tr></tbody>
              <tfoot><tr><th colspan="3">借贷合计</th><td class="number" data-label="借方合计">{{ formatFen(voucherTotal(voucher, 'debit_fen')) }}</td><td class="number" data-label="贷方合计">{{ formatFen(voucherTotal(voucher, 'credit_fen')) }}</td></tr></tfoot>
            </table></div>
            <div v-if="voucherAssets(voucher).length" class="voucher-asset-references"><span class="voucher-detail-label">对应资产</span><div class="voucher-asset-links"><RouterLink v-for="asset in voucherAssets(voucher)" :key="asset.asset_id" class="voucher-asset-link" :to="assetTarget(asset.asset_id)">{{ assetLabel(asset) }}</RouterLink></div></div>
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
  <template v-if="progressVoucher">
  <Teleport to="body">
    <div class="voucher-progress-layer" :class="{ mobile: progressIsMobile }">
      <section id="voucher-progress-popover" ref="progressPanel" class="voucher-progress-popover dashboard-hover-preview" role="dialog"
        :data-side="progressIsMobile ? 'sheet' : progressPosition.side"
        aria-labelledby="voucher-progress-title" :aria-modal="progressIsMobile ? true : undefined"
        :style="{ ...(progressIsMobile ? {} : { left: `${progressPosition.left}px`, top: `${progressPosition.top}px`, '--preview-arrow': `${progressPosition.arrow}px` }), visibility: progressPosition.ready ? 'visible' : 'hidden' }"
        @mouseenter="progressPanelEnter" @mouseleave="progressPanelLeave">
        <header class="voucher-progress-heading"><h3 id="voucher-progress-title">业务进展 · 凭证 {{ progressVoucher.number }}</h3><button type="button" class="voucher-progress-close" aria-label="关闭业务进展" @click="closeProgress(true)"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4 4 8 8M12 4l-8 8" /></svg></button></header>
        <div class="voucher-progress-content">
          <BusinessStatusDetails :key="progressVoucher.voucher_version_id" :subject-id="progressVoucher.subject_id!" :period="period" :snapshot-version="snapshotVersion"
            :voucher-context="progressVoucher" :detail-cache="voucherProgressCache" presentation="voucher" :expanded="true" hide-summary @changed="$emit('changed')" />
        </div>
      </section>
    </div>
  </Teleport>
  </template>
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
.detail { min-width: 0; padding: 12px var(--dashboard-list-gutter); }
.column-money, .column-action { text-align: right; }
.event-list { margin: 0; padding: 0; list-style: none; }
.state { padding: 2px 7px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); font-size: 11px; overflow-wrap: anywhere; }
.event-row :deep(.compact-status-details) { display: contents; }
.event-row :deep(.compact-status-panel) { grid-column: 1 / -1; }
.event-voucher-link { position: relative; min-width: 0; justify-self: end; font-size: 11px; color: var(--muted); }
.event-voucher-button { min-height: 32px; padding: 0 9px; border-radius: 8px; font-weight: 750; white-space: nowrap; }
.event-voucher-button:hover, .event-voucher-button:focus-visible { background: var(--accent-soft); }
.event-voucher-button:disabled { color: var(--muted); cursor: default; }
.event-voucher-preview { position: absolute; top: 50%; right: calc(100% + 10px); z-index: 30; display: grid; width: min(380px, calc(100vw - 48px)); gap: 10px; pointer-events: none; transform: translateY(calc(-50% + var(--preview-offset))); }
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
}
@media (max-width: 760px) {
  .section-heading { flex-direction: column; align-items: flex-start; gap: 7px; }
  .workbench { grid-template-columns: minmax(0, 1fr); }
  .index { padding: 10px; border-right: 0; border-bottom: 1px solid var(--line); border-radius: 14px 14px 0 0; }
  .category-heading { width: 100%; }
  .index button { min-height: 44px; padding: 9px 12px; }
  .detail { padding: 4px var(--dashboard-list-gutter); }
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
  padding-inline: var(--dashboard-list-gutter);
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--surface);
}

.voucher-pagination {
  justify-content: flex-end;
}

.voucher-card {
  overflow: hidden;
  scroll-margin-top: var(--brief-anchor-offset, 78px);
  background: var(--surface);
}

.voucher-card + .voucher-card {
  border-top: 1px solid var(--line);
}

.voucher-row-shell {
  display: grid;
  min-height: 62px;
  grid-template-columns: 132px minmax(0, 1fr) auto 132px 70px 16px;
  grid-template-rows: auto auto;
  gap: 3px 14px;
  align-items: center;
  padding: 10px 4px;
}

.voucher-row {
  display: grid;
  grid-column: 1 / -1;
  grid-row: 1 / -1;
  grid-template-columns: subgrid;
  grid-template-rows: subgrid;
  grid-template-areas: "reference copy state amount . chevron" "reference type state amount . chevron";
  align-items: center;
  width: 100%;
  min-width: 0;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--text);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.voucher-row-shell:hover {
  background: var(--surface-soft);
}

.voucher-card.is-open > .voucher-row-shell {
  background: color-mix(in srgb, var(--accent-soft) 28%, var(--surface));
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
.voucher-type {
  color: var(--muted);
  font-size: 11px;
  overflow-wrap: anywhere;
}

.voucher-type { grid-area: type; }

.voucher-copy {
  grid-area: copy;
}

.voucher-copy strong {
  min-width: 0;
  font-size: 13px;
  font-weight: 650;
  line-height: 1.65;
  white-space: normal;
  overflow-wrap: anywhere;
}

.voucher-progress-button, .voucher-progress-empty { grid-column: 5; grid-row: 1 / -1; z-index: 1; }
.voucher-progress-button { min-height: 32px; padding: 5px 4px; border: 0; border-radius: 6px; background: transparent; color: var(--accent); font: inherit; font-size: 11px; font-weight: 650; cursor: pointer; }
.voucher-progress-empty { color: var(--muted); font-size: 12px; text-align: center; }
.voucher-progress-button:hover, .voucher-progress-button[aria-expanded="true"] { background: var(--accent-soft); }
.voucher-row:focus-visible, .voucher-progress-button:focus-visible, .voucher-progress-close:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }

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
  grid-area: chevron;
}

.voucher-inline-detail {
  --voucher-account-width: 28%;
  --voucher-party-width: 22%;
  --voucher-source-width: 22%;
  --voucher-amount-width: 14%;
  min-width: 0;
  margin: 10px 0 18px 146px;
  padding: 4px 0;
}

.voucher-account-column {
  width: var(--voucher-account-width);
}

.voucher-party-column {
  width: var(--voucher-party-width);
}

.voucher-source-column {
  width: var(--voucher-source-width);
}

.voucher-amount-column {
  width: var(--voucher-amount-width);
}

.table-wrap {
  min-width: 0;
}

table {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
  font-size: 13px;
}

th,
td {
  padding: 11px 8px;
  text-align: left;
  vertical-align: top;
  overflow-wrap: anywhere;
}

th {
  color: var(--muted);
  font-size: 11px;
}

td:first-child small,
td:first-child strong {
  display: block;
}

td:first-child small {
  margin-bottom: 2px;
  color: var(--muted);
}

.voucher-line-account { display: contents; }
.voucher-mobile-source { display: none; }

.number {
  text-align: right;
  white-space: normal;
}


.section-heading p { margin: 5px 0 0; color: var(--muted); font-size: 13px; }
.event-voucher-button { border: 0; background: transparent; color: var(--accent); font: inherit; font-size: 11px; font-weight: 750; cursor: pointer; }
.voucher-retry { justify-self: start; padding: 4px 0; border: 0; background: transparent; color: var(--accent); font: inherit; font-size: 12px; cursor: pointer; }
.voucher-row-amount { display: grid; min-width: 0; gap: 3px; white-space: normal; overflow-wrap: anywhere; }
.voucher-row-amount small, .voucher-load-status { color: var(--muted); font-size: 12px; }
.voucher-reference small { white-space: normal; overflow-wrap: anywhere; }
.voucher-detail-label { margin: 0; color: var(--muted); font-size: 11px; font-weight: 600; }
.voucher-correction { color: var(--muted); font-size: 12px; line-height: 1.6; }
.voucher-asset-references { display: flex; align-items: baseline; flex-wrap: wrap; gap: 6px 14px; margin-top: 12px; padding-inline: 4px; font-size: 12px; }
.voucher-asset-links { display: flex; flex-wrap: wrap; gap: 6px 12px; min-width: 0; }
.voucher-asset-link { color: var(--accent); overflow-wrap: anywhere; }
.line-party { display: block; margin-bottom: 4px; overflow-wrap: anywhere; }
tfoot th, tfoot td { font-size: 12px; font-weight: 650; }
.voucher-progress-layer { position: fixed; inset: 0; z-index: 110; pointer-events: none; }
.voucher-progress-popover { position: fixed; display: flex; flex-direction: column; width: min(var(--progress-width, 680px), calc(100vw - 24px)); max-height: min(600px, calc(100dvh - 24px)); pointer-events: auto; }
.voucher-progress-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding-bottom: 12px; }
.voucher-progress-heading h3 { margin: 0; font-size: 13px; font-weight: 650; }
.voucher-progress-close { display: grid; flex: none; place-items: center; width: 32px; height: 32px; padding: 6px; border: 0; border-radius: 6px; background: transparent; color: var(--muted); cursor: pointer; }
.voucher-progress-close:hover { background: var(--surface-soft); }
.voucher-progress-close svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 1.5; stroke-linecap: round; }
.voucher-progress-content { min-height: 0; overflow: auto; overscroll-behavior: contain; scrollbar-width: thin; scrollbar-color: color-mix(in srgb, var(--muted) 38%, transparent) transparent; }
.voucher-progress-content::-webkit-scrollbar { width: 3px; height: 3px; }
.voucher-progress-content::-webkit-scrollbar-track { background: transparent; }
.voucher-progress-content::-webkit-scrollbar-thumb { border-radius: 3px; background: color-mix(in srgb, var(--muted) 38%, transparent); }
.voucher-progress-content::-webkit-scrollbar-thumb:hover { background: color-mix(in srgb, var(--muted) 60%, transparent); }
@supports selector(::-webkit-scrollbar) {
  .voucher-progress-content { scrollbar-width: auto; scrollbar-color: auto; }
}
.voucher-progress-content :deep(.business-detail-panel), .voucher-progress-content :deep(.business-detail-state) { margin: 0; padding: 0; border: 0; border-radius: 0; background: transparent; }
@media (max-width: 1024px) {
  .heading-controls { flex-wrap: wrap; max-width: 100%; }
  .voucher-row-shell { grid-template-columns: minmax(0, 1fr) auto 72px 16px; grid-template-rows: auto auto auto; gap: 6px 10px; }
  .voucher-row { grid-template-areas: "reference amount amount chevron" "copy copy copy copy" "type state . ."; }
  .voucher-progress-button, .voucher-progress-empty { grid-column: 3 / -1; grid-row: 3; }
  .voucher-inline-detail { margin-left: 0; }
  .voucher-row > .state { justify-self: end; }
  .voucher-row-amount { max-width: 130px; font-size: 13px; }
}
@media (min-width: 761px) {
  .voucher-progress-content { padding-right: 12px; scrollbar-gutter: stable; }
  table { border-top: 1.5px solid var(--line-strong, var(--line)); border-bottom: 1.5px solid var(--line-strong, var(--line)); }
  thead { border-bottom: 1px solid var(--line-strong, var(--line)); }
  th:first-child, td:first-child { padding-left: 4px; }
  th:last-child, td:last-child { padding-right: 4px; }
  .voucher-line-account { display: flex; align-items: baseline; gap: 7px; min-width: 0; }
  .voucher-line-account small { flex: none; margin-bottom: 0; }
  .voucher-line-account strong { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
}
@media (min-width: 761px) and (max-width: 1024px) {
  .voucher-row { grid-template-areas: "reference amount . chevron" "copy copy copy copy" "type state . ."; }
  .voucher-progress-button, .voucher-progress-empty { grid-row: 1; }
}
@media (max-width: 760px) {
  .event-voucher-preview { display: none; }
  .event-voucher-button { min-height: 44px; }
  .heading-controls { width: 100%; }
  .view-switch button { min-height: 44px; }
  .voucher-inline-detail { margin: 8px 0 14px 11px; padding: 4px 0 4px 12px; border-left: 1px solid var(--line); }
  .voucher-progress-button, .voucher-progress-close { min-height: 44px; }
  .voucher-progress-close { width: 44px; }
  .voucher-progress-layer.mobile { pointer-events: auto; background: rgb(0 0 0 / 25%); }
  .voucher-progress-popover { left: 12px; right: 12px; bottom: 12px; width: auto; max-height: calc(100dvh - 24px); padding: 12px; }
  .voucher-progress-popover::after { display: none; }
  table, tbody, tfoot, tr, td { display: block; width: 100%; }
  thead, tfoot th { display: none; }
  tbody { display: grid; }
  tr { padding: 10px 0; }
  tbody tr + tr { border-top: 1px solid var(--line); }
  td, td:first-child { display: grid; grid-template-columns: 62px minmax(0, 1fr); gap: 3px 8px; padding: 4px; text-align: left; white-space: normal; }
  td::before { color: var(--muted); font-size: 11px; font-weight: 500; content: attr(data-label); }
  td > *, td:first-child small, td:first-child strong { grid-column: 2; }
  td.voucher-line-source { display: none; }
  .voucher-mobile-source { display: block; margin-top: 4px; color: var(--muted); }
  tfoot tr { border-top: 1px solid var(--line); }
  tfoot td { border: 0; }
}
</style>
