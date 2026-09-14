<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import { useRoute } from "vue-router";

import type { BriefActivityGroup, BriefActivityRow, BriefAssetReference, BriefVoucher, BriefVoucherLine } from "../../api/brief";
import { fen, formatFen } from "../../utils/money";
import VoucherTrace from "./VoucherTrace.vue";
import BusinessDetails from "./BusinessDetails.vue";

const props = defineProps<{
  groups: BriefActivityGroup[];
  vouchers: BriefVoucher[];
  voucherCount: number;
  focusedVoucher?: BriefVoucher | null;
}>();
const route = useRoute();

const mode = ref<"business" | "voucher">("business");
const selectedBusinessKey = ref("");
const selectedVoucherNumber = ref("");
const evidenceVoucherNumber = ref("");
const BUSINESS_PAGE_SIZE = 10;
const businessPage = ref(1);
const VOUCHER_PAGE_SIZE = 15;
const voucherDisplayMode = ref<"paged" | "all">("paged");
const voucherPage = ref(1);
const availableVouchers = computed(() => {
  const focused = props.focusedVoucher;
  return focused && !props.vouchers.some((item) => item.voucher_version_id === focused.voucher_version_id)
    ? [focused, ...props.vouchers]
    : props.vouchers;
});
const voucherPageCount = computed(() => Math.max(1, Math.ceil(availableVouchers.value.length / VOUCHER_PAGE_SIZE)));
const visibleVouchers = computed(() => {
  if (voucherDisplayMode.value === "all") return availableVouchers.value;
  const start = (voucherPage.value - 1) * VOUCHER_PAGE_SIZE;
  return availableVouchers.value.slice(start, start + VOUCHER_PAGE_SIZE);
});
const vouchersByNumber = computed(() => {
  const result = new Map<string, BriefVoucher>();
  for (const voucher of availableVouchers.value) {
    if (!result.has(voucher.number)) result.set(voucher.number, voucher);
  }
  return result;
});
const selectedBusiness = computed(
  () => props.groups.find((item) => item.key === selectedBusinessKey.value) || null,
);
const businessPageCount = computed(() => Math.max(1, Math.ceil((selectedBusiness.value?.rows.length || 0) / BUSINESS_PAGE_SIZE)));
const visibleBusinessRows = computed(() => {
  const start = (businessPage.value - 1) * BUSINESS_PAGE_SIZE;
  return selectedBusiness.value?.rows.slice(start, start + BUSINESS_PAGE_SIZE) || [];
});
const visibleBusinessStart = computed(() => selectedBusiness.value?.rows.length ? (businessPage.value - 1) * BUSINESS_PAGE_SIZE + 1 : 0);
const visibleBusinessEnd = computed(() => Math.min(businessPage.value * BUSINESS_PAGE_SIZE, selectedBusiness.value?.rows.length || 0));

function keepAvailableSelection() {
  if (!props.groups.some((item) => item.key === selectedBusinessKey.value)) {
    selectedBusinessKey.value = props.groups[0]?.key || "";
  }
  if (!props.vouchers.some((item) => item.number === selectedVoucherNumber.value) && props.focusedVoucher?.number !== selectedVoucherNumber.value) {
    selectedVoucherNumber.value = "";
  }
}

function selectMode(value: "business" | "voucher") {
  if (value === "voucher" && mode.value !== value) {
    selectedVoucherNumber.value = "";
    voucherPage.value = 1;
  }
  mode.value = value;
  if (value === "business" && !selectedBusiness.value) {
    selectedBusinessKey.value = props.groups[0]?.key || "";
  }
}

function selectBusiness(key: string) {
  selectedBusinessKey.value = key;
}

function changeBusinessPage(page: number) {
  businessPage.value = Math.min(Math.max(page, 1), businessPageCount.value);
}

async function toggleVoucherDisplayMode() {
  voucherDisplayMode.value = voucherDisplayMode.value === "paged" ? "all" : "paged";
  if (voucherDisplayMode.value === "paged" && selectedVoucherNumber.value) {
    const index = availableVouchers.value.findIndex((item) => item.number === selectedVoucherNumber.value);
    if (index >= 0) voucherPage.value = Math.floor(index / VOUCHER_PAGE_SIZE) + 1;
  }
  await nextTick();
  document.getElementById("activity")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function changeVoucherPage(page: number) {
  voucherPage.value = Math.min(Math.max(page, 1), voucherPageCount.value);
  selectedVoucherNumber.value = "";
  await nextTick();
  document.getElementById("activity")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function activityName(item: BriefActivityRow) {
  if (item.asset) return assetReferenceName(item.asset);
  const party = item.party.trim();
  return party && party !== "—" && !party.includes("未提供") ? party : item.title;
}

function activityMeta(item: BriefActivityRow) {
  if (item.asset) {
    return [
      item.title,
      item.asset.name?.trim() && item.asset.code?.trim() ? item.asset.code.trim() : "",
      item.evidence.length ? `${item.evidence.length} 份凭据` : "",
    ].filter(Boolean).join(" · ");
  }
  const primary = activityName(item);
  const context = primary === item.title
    ? (item.subject !== item.title ? item.subject : "")
    : item.title;
  return [context, item.evidence.length ? `${item.evidence.length} 份凭据` : ""].filter(Boolean).join(" · ");
}

function assetReferenceName(asset: BriefAssetReference) {
  return asset.name?.trim() || (asset.code?.trim() ? `资产卡片 ${asset.code.trim()}` : "未命名资产卡片");
}

function assetReferenceLabel(asset: BriefAssetReference) {
  const name = asset.name?.trim();
  const code = asset.code?.trim();
  return name && code ? `${name}（${code}）` : name || (code ? `资产卡片 ${code}` : "未命名资产卡片");
}

function assetCardTarget(asset: BriefAssetReference) {
  return {
    name: "assets",
    query: {
      company_id: typeof route.query.company_id === "string" ? route.query.company_id : undefined,
      period: typeof route.query.period === "string" ? route.query.period : undefined,
      asset_id: asset.asset_id,
    },
    hash: "#asset-card-target",
  };
}

function voucherContext(voucher: BriefVoucher) {
  const title = voucher.list_summary.trim();
  let detail = (voucher.display_summary || voucher.summary).trim();
  if (!detail || detail === title) return "";
  if (title && detail.startsWith(title)) {
    detail = detail.slice(title.length).trim();
    detail = detail.replace(/^[（(][^）)]*[）)]\s*/, "");
    detail = detail.replace(/^[·•；;：:\-—\s]+/, "");
  }
  return detail === title ? "" : detail;
}

function voucherForReference(reference: string) {
  return vouchersByNumber.value.get(reference) || null;
}

function voucherPreviewTitle(item: BriefActivityRow) {
  const voucher = voucherForReference(item.reference);
  return voucher?.asset
    ? `${voucher.list_summary} · ${assetReferenceLabel(voucher.asset)}`
    : voucher?.list_summary || item.title;
}

function activityVoucherDate(item: BriefActivityRow) {
  const voucher = voucherForReference(item.reference);
  if (voucher) return compactVoucherDate(voucher);
  if (item.date && /^\d{4}-\d{2}-\d{2}/.test(item.date)) return item.date.slice(0, 10);
  return item.recognition?.period || item.date || "日期未提供";
}

function voucherLineAmount(line: BriefVoucherLine) {
  return fen(line.debit_fen)
    ? `借 ${formatFen(line.debit_fen)}`
    : `贷 ${formatFen(line.credit_fen)}`;
}

function compactVoucherDate(voucher: BriefVoucher) {
  if (voucher.date && /^\d{4}-\d{2}-\d{2}/.test(voucher.date)) return voucher.date.slice(0, 10);
  return voucher.recognition?.period || voucher.date || "日期未提供";
}

async function openVoucher(number: string) {
  mode.value = "voucher";
  if (voucherDisplayMode.value === "paged") {
    const index = availableVouchers.value.findIndex((item) => item.number === number);
    if (index >= 0) voucherPage.value = Math.floor(index / VOUCHER_PAGE_SIZE) + 1;
  }
  selectedVoucherNumber.value = number;
  await nextTick();
  document.querySelector<HTMLElement>(".activity-section .voucher-card.is-open")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function selectVoucher(number: string) {
  const opening = selectedVoucherNumber.value !== number;
  selectedVoucherNumber.value = opening ? number : "";
  if (!opening) return;
  await nextTick();
  const block = window.matchMedia("(max-width: 760px)").matches ? "start" : "nearest";
  document.querySelector<HTMLElement>(".activity-section .voucher-card.is-open")?.scrollIntoView({ behavior: "smooth", block });
}

function toggleVoucherEvidence(number: string) {
  evidenceVoucherNumber.value = evidenceVoucherNumber.value === number ? "" : number;
}

function formatDate(value: string | null, recognition?: { label: string }) {
  if (recognition?.label) return recognition.label;
  if (!value) return "日期未提供";
  if (/^\d{4}-\d{2}$/.test(value)) return `${value} · 按月确认`;
  const [year, month, day] = value.slice(0, 10).split("-");
  return `${year} 年 ${Number(month)} 月 ${Number(day)} 日`;
}

watch(() => [props.groups, props.vouchers], keepAvailableSelection, { immediate: true });
watch(selectedBusinessKey, () => { businessPage.value = 1; });
watch(selectedVoucherNumber, (number) => {
  if (evidenceVoucherNumber.value !== number) evidenceVoucherNumber.value = "";
});
watch(businessPageCount, (count) => { businessPage.value = Math.min(businessPage.value, count); });
watch(voucherPageCount, (count) => { voucherPage.value = Math.min(voucherPage.value, count); });
watch(() => props.focusedVoucher, () => {
  if (props.focusedVoucher) {
    mode.value = "voucher";
    selectedVoucherNumber.value = props.focusedVoucher.number;
    if (voucherDisplayMode.value === "paged") {
      const index = availableVouchers.value.findIndex((item) => item.voucher_version_id === props.focusedVoucher?.voucher_version_id);
      if (index >= 0) voucherPage.value = Math.floor(index / VOUCHER_PAGE_SIZE) + 1;
    }
  }
}, { immediate: true });
</script>

<template>
  <section class="brief-section activity-section" aria-labelledby="activity-title">
    <div class="section-heading">
      <div>
        <h2 id="activity-title">本月发生了什么</h2>
        <p>{{ voucherCount }} 张凭证 · {{ groups.length }} 类业务</p>
      </div>
      <div class="heading-controls">
        <div v-if="mode === 'voucher'" class="voucher-display-toggle">
          <span :class="{ active: voucherDisplayMode === 'paged' }">分页</span>
          <button
            type="button"
            role="switch"
            :aria-checked="voucherDisplayMode === 'all'"
            :aria-label="voucherDisplayMode === 'paged' ? '改为全部显示凭证' : '改为分页显示凭证'"
            @click="toggleVoucherDisplayMode"
          >
            <span aria-hidden="true"></span>
          </button>
          <span :class="{ active: voucherDisplayMode === 'all' }">全部</span>
        </div>
        <div class="view-switch" role="group" aria-label="本月业务查看方式">
          <button
            type="button"
            :aria-pressed="mode === 'business'"
            @click="selectMode('business')"
          >
            按业务
          </button>
          <button
            type="button"
            :aria-pressed="mode === 'voucher'"
            @click="selectMode('voucher')"
          >
            按凭证
          </button>
        </div>
      </div>
    </div>

    <div v-if="mode === 'business' && groups.length" class="workbench">
      <nav class="index" aria-label="业务分类">
        <button
          v-for="group in groups"
          :key="group.key"
          type="button"
          :aria-current="selectedBusinessKey === group.key ? 'true' : undefined"
          @click="selectBusiness(group.key)"
        >
          <span>
            <strong>{{ group.label }}</strong>
            <small>{{ group.type_counts.map((item) => `${item.label} ${item.count}`).join(" · ") }}</small>
          </span>
          <b>{{ group.event_count }}</b>
        </button>
      </nav>

      <div v-if="selectedBusiness" class="detail" aria-live="polite">
        <header class="detail-heading">
          <h3>{{ selectedBusiness.label }}</h3>
          <strong>
            本月 {{ selectedBusiness.event_count }} 项
            <template v-if="selectedBusiness.rows.length < selectedBusiness.event_count">· 已加载 {{ selectedBusiness.rows.length }} 项</template>
          </strong>
        </header>
        <ul class="event-list" aria-label="本月业务明细">
          <li v-for="item in visibleBusinessRows" :key="`${item.reference}-${item.title}`" class="event-row">
            <span class="event-reference">{{ formatDate(item.date, item.recognition) }}</span>
            <span class="event-copy">
              <strong>{{ activityName(item) }}</strong>
              <small v-if="activityMeta(item)">{{ activityMeta(item) }}</small>
            </span>
            <span :class="['state', { correction: item.state.includes('冲正') }]">{{ item.state }}</span>
            <span class="event-money">
              <small>{{ item.amount_label }}</small>
              <b>{{ item.amount_fen === null ? "见凭证" : formatFen(item.amount_fen) }}</b>
            </span>
            <span class="event-voucher-link">
              <button
                type="button"
                class="event-voucher-button"
                :aria-label="`打开凭证 ${item.reference}：${activityName(item)}`"
                :aria-describedby="`voucher-preview-${item.voucher_version_id}`"
                @click="openVoucher(item.reference)"
              >
                凭证 {{ item.reference }}
              </button>
              <span :id="`voucher-preview-${item.voucher_version_id}`" class="event-voucher-preview" role="tooltip">
                <span class="voucher-preview-heading">
                  <span>
                    <small>凭证 {{ item.reference }} · {{ activityVoucherDate(item) }}</small>
                    <strong>{{ voucherPreviewTitle(item) }}</strong>
                  </span>
                  <b>{{ formatFen(voucherForReference(item.reference)?.amount_fen || item.journal_total_fen) }}</b>
                </span>
                <span v-if="voucherForReference(item.reference)?.lines.length" class="voucher-preview-lines">
                  <span
                    v-for="line in voucherForReference(item.reference)?.lines"
                    :key="line.line_number"
                  >
                    <span>{{ line.account }}</span>
                    <strong>{{ voucherLineAmount(line) }}</strong>
                  </span>
                </span>
                <span v-else class="voucher-preview-empty">完整分录将在打开凭证后显示</span>
                <span class="voucher-preview-footer">
                  <span :class="['state', { correction: item.state.includes('冲正') }]">{{ item.state }}</span>
                  <small>点击打开凭证详情</small>
                </span>
              </span>
            </span>
          </li>
        </ul>
        <footer v-if="selectedBusiness.rows.length > BUSINESS_PAGE_SIZE" class="business-pagination" aria-label="已加载业务分页">
          <span>第 {{ visibleBusinessStart }}–{{ visibleBusinessEnd }} 项 · 已加载 {{ selectedBusiness.rows.length }} 项</span>
          <div>
            <button type="button" :disabled="businessPage === 1" @click="changeBusinessPage(businessPage - 1)">上一页</button>
            <strong>{{ businessPage }} / {{ businessPageCount }}</strong>
            <button type="button" :disabled="businessPage === businessPageCount" @click="changeBusinessPage(businessPage + 1)">下一页</button>
          </div>
        </footer>
      </div>
    </div>

    <div v-else-if="mode === 'voucher' && availableVouchers.length" class="voucher-view">
      <div class="voucher-list" aria-label="凭证清单">
        <article
          v-for="voucher in visibleVouchers"
          :key="voucher.voucher_version_id"
          :class="['voucher-card', 'selectable-card', { 'is-open': selectedVoucherNumber === voucher.number }]"
          tabindex="-1"
        >
          <button
            type="button"
            class="voucher-row"
            :aria-expanded="selectedVoucherNumber === voucher.number"
            @click="selectVoucher(voucher.number)"
          >
            <span class="voucher-reference">
              <strong>凭证 {{ voucher.number }}</strong>
              <small>{{ compactVoucherDate(voucher) }}</small>
            </span>
            <span class="voucher-copy">
              <strong>{{ voucher.list_summary }}</strong>
              <small v-if="voucherContext(voucher)">{{ voucherContext(voucher) }}</small>
            </span>
            <span :class="['state', { correction: voucher.state.includes('冲正') }]">{{ voucher.state }}</span>
            <strong class="voucher-row-amount">{{ formatFen(voucher.amount_fen) }}</strong>
            <span class="voucher-chevron" aria-hidden="true"></span>
          </button>

          <section
            v-if="selectedVoucherNumber === voucher.number"
            class="voucher-inline-detail"
            :aria-label="`${voucher.number} 凭证明细`"
          >
            <p v-if="voucher.reverses_version_id" class="voucher-correction">本凭证用于冲销原记录。</p>
            <RouterLink
              v-if="voucher.asset"
              class="voucher-asset-link"
              :to="assetCardTarget(voucher.asset)"
              :aria-label="`查看资产卡片：${assetReferenceLabel(voucher.asset)}`"
            >
              <span>对应资产</span>
              <strong>{{ assetReferenceLabel(voucher.asset) }}</strong>
              <small>查看资产卡片 <span aria-hidden="true">→</span></small>
            </RouterLink>
            <BusinessDetails plain :components="voucher.components" :funds="voucher.funds" :settlements="voucher.settlements" />

            <div class="table-wrap">
              <table>
                <colgroup>
                  <col class="voucher-account-column" />
                  <col class="voucher-party-column" />
                  <col class="voucher-amount-column" />
                  <col class="voucher-amount-column" />
                </colgroup>
                <thead>
                  <tr>
                    <th>科目</th>
                    <th>往来对象</th>
                    <th class="number">借方</th>
                    <th class="number">贷方</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="line in voucher.lines" :key="line.line_number">
                    <td data-label="科目">
                      <small>{{ line.code }}</small>
                      <strong>{{ line.account }}</strong>
                      <RouterLink v-if="line.asset" :to="assetCardTarget(line.asset)" :aria-label="`查看资产卡片：${assetReferenceLabel(line.asset)}`">{{ assetReferenceLabel(line.asset) }}</RouterLink>
                    </td>
                    <td data-label="往来对象">
                      <template v-if="line.parties?.length > 1">
                        <span v-for="(party, partyIndex) in line.parties" :key="`${party.id}-${partyIndex}`" class="line-party">{{ party.name }} · {{ formatFen(party.amount_fen) }}</span>
                      </template>
                      <span v-else :class="{ party: line.party }">{{ line.party || (line.party_state === 'unresolved' ? '见凭证业务说明' : '—') }}</span>
                      <small v-if="line.source_label">业务来源：{{ line.source_label }}</small>
                    </td>
                    <td class="number" data-label="借方">{{ fen(line.debit_fen) ? formatFen(line.debit_fen) : "—" }}</td>
                    <td class="number" data-label="贷方">{{ fen(line.credit_fen) ? formatFen(line.credit_fen) : "—" }}</td>
                  </tr>
                </tbody>
              </table>
            </div>

            <div v-if="voucher.evidence_details?.length || voucher.calculation_id" class="voucher-actions">
              <button
                v-if="voucher.evidence_details?.length"
                type="button"
                class="voucher-action"
                :aria-expanded="evidenceVoucherNumber === voucher.number"
                @click="toggleVoucherEvidence(voucher.number)"
              >
                关联凭据 <span>{{ voucher.evidence_details.length }} 份</span>
              </button>
              <VoucherTrace
                v-if="voucher.calculation_id"
                compact
                :calculation-id="voucher.calculation_id"
                :voucher-version-id="voucher.voucher_version_id"
              />
              <section v-if="evidenceVoucherNumber === voucher.number" class="voucher-evidence" aria-label="关联凭据">
                <ul>
                  <li
                    v-for="item in voucher.evidence_details"
                    :key="item.digest"
                    :title="item.name.trim() || '原文件名未保存'"
                  >
                    {{ item.name.trim() || "原文件名未保存" }}
                  </li>
                </ul>
              </section>
            </div>
          </section>
        </article>
      </div>
      <footer v-if="voucherDisplayMode === 'paged' && voucherPageCount > 1" class="business-pagination voucher-pagination" aria-label="凭证分页">
        <div>
          <button type="button" :disabled="voucherPage === 1" @click="changeVoucherPage(voucherPage - 1)">上一页</button>
          <strong>{{ voucherPage }} / {{ voucherPageCount }}</strong>
          <button type="button" :disabled="voucherPage === voucherPageCount" @click="changeVoucherPage(voucherPage + 1)">下一页</button>
        </div>
      </footer>
    </div>

    <p v-else class="empty">
      {{ mode === "business" ? "本月没有正式凭证业务。" : "本月没有凭证。" }}
    </p>

    <div class="activity-pagination">
      <slot name="pagination" />
    </div>
  </section>
</template>

<style scoped>
.event-money { display: grid; min-width: 0; gap: 2px; text-align: right; }
.event-money small { color: var(--brief-muted); font-weight: 400; }
.event-money b { overflow: hidden; font-size: 14px; text-overflow: ellipsis; white-space: nowrap; }
.line-party { display: block; margin-bottom: 4px; }
.brief-section {
  padding: 20px;
  border: 1px solid var(--brief-line);
  border-radius: 20px;
  background: var(--brief-surface);
  box-shadow: var(--brief-shadow);
}

.section-heading,
.detail-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
}

.section-heading {
  align-items: flex-end;
  margin-bottom: 14px;
}

h2,
h3,
p {
  margin-top: 0;
}

h2 {
  margin-bottom: 3px;
  font-size: 23px;
  letter-spacing: -0.025em;
}

h3 {
  margin-bottom: 0;
}

.section-heading p:last-child {
  margin-bottom: 0;
  color: var(--brief-muted);
  font-size: 13px;
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
  color: var(--brief-muted);
  font-size: 12px;
  white-space: nowrap;
}

.voucher-display-toggle > span {
  transition: color 140ms ease;
}

.voucher-display-toggle > span.active {
  color: var(--brief-text);
  font-weight: 750;
}

.voucher-display-toggle button {
  position: relative;
  width: 38px;
  height: 22px;
  flex: none;
  padding: 0;
  border: 1px solid var(--brief-line-strong);
  border-radius: 999px;
  background: var(--brief-soft);
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
  background: var(--brief-surface);
  box-shadow: 0 1px 4px rgb(18 45 31 / 20%);
  transition: transform 140ms ease;
}

.voucher-display-toggle button[aria-checked="true"] {
  border-color: var(--brief-green);
  background: var(--brief-green);
}

.voucher-display-toggle button[aria-checked="true"] > span {
  transform: translateX(16px);
}

.voucher-display-toggle button:focus-visible {
  outline: 2px solid var(--brief-green);
  outline-offset: 2px;
}

.view-switch {
  display: grid;
  flex: none;
  grid-template-columns: repeat(2, 1fr);
  gap: 3px;
  padding: 3px;
  border: 1px solid var(--brief-line);
  border-radius: 11px;
  background: var(--brief-soft);
}

.view-switch button {
  min-height: 34px;
  padding: 0 13px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--brief-muted);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}

.view-switch button[aria-pressed="true"] {
  background: var(--brief-surface);
  color: var(--brief-text);
  box-shadow: 0 2px 8px rgb(18 45 31 / 8%);
}

.workbench {
  display: grid;
  grid-template-columns: 270px minmax(0, 1fr);
  overflow: visible;
  border: 1px solid var(--brief-line);
  border-radius: 16px;
  background: var(--brief-soft);
}

.index {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px;
  border-right: 1px solid var(--brief-line);
  border-radius: 15px 0 0 15px;
}

.index button {
  position: relative;
  display: grid;
  min-height: 66px;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px;
  align-items: center;
  padding: 10px 11px 10px 13px;
  border: 1px solid transparent;
  border-radius: 11px;
  background: transparent;
  color: var(--brief-text);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.index button::before {
  position: absolute;
  top: 12px;
  bottom: 12px;
  left: 0;
  width: 3px;
  border-radius: 999px;
  background: transparent;
  content: "";
}

.index button:hover {
  border-color: var(--brief-line-strong);
}

.index button[aria-current="true"] {
  border-color: color-mix(in srgb, var(--brief-green) 20%, var(--brief-line));
  background: var(--brief-green-soft);
}

.index button[aria-current="true"]::before {
  background: var(--brief-green);
}

.index button span,
.index button small {
  display: block;
  min-width: 0;
}

.index button small {
  overflow: hidden;
  margin-top: 4px;
  color: var(--brief-muted);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.index button b {
  color: var(--brief-green);
  font-size: 13px;
  white-space: nowrap;
}

.detail {
  min-width: 0;
  padding: 15px;
  border-radius: 0 15px 15px 0;
  background: var(--brief-surface);
}

.detail-heading {
  padding-bottom: 13px;
  border-bottom: 1px solid var(--brief-line);
}

.detail-heading > strong {
  color: var(--brief-muted);
  font-size: 13px;
}

.event-list {
  overflow: visible;
  margin: 10px 0 0;
  padding: 0;
  border: 1px solid var(--brief-line);
  border-radius: 12px;
  list-style: none;
}

.event-list > li + li {
  border-top: 1px solid var(--brief-line);
}

.event-row {
  position: relative;
  display: grid;
  min-height: 54px;
  grid-template-columns: minmax(90px, 0.55fr) minmax(160px, 1.5fr) auto minmax(104px, 0.6fr) auto;
  gap: 12px;
  align-items: center;
  padding: 8px 10px;
  background: var(--brief-surface);
  transition: background 140ms ease;
}

.event-row:first-child {
  border-radius: 11px 11px 0 0;
}

.event-row:last-child {
  border-radius: 0 0 11px 11px;
}

.event-row:only-child {
  border-radius: 11px;
}

.event-row:hover,
.event-row:focus-within {
  z-index: 4;
  background: color-mix(in srgb, var(--brief-green-soft) 38%, var(--brief-surface));
}

.event-reference {
  min-width: 0;
  overflow: hidden;
  color: var(--brief-muted);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.event-copy {
  display: grid;
  min-width: 0;
  gap: 2px;
}

.event-copy strong,
.event-copy small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.event-copy strong {
  font-size: 13px;
}

.event-copy small {
  color: var(--brief-muted);
  font-size: 11px;
}

.event-voucher-button {
  min-height: 32px;
  padding: 0 9px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--brief-green);
  font: inherit;
  font-size: 11px;
  font-weight: 750;
  white-space: nowrap;
  cursor: pointer;
}

.event-voucher-button:hover,
.event-voucher-button:focus-visible {
  background: var(--brief-green-soft);
}

.event-voucher-link {
  position: relative;
  justify-self: end;
}

.event-voucher-preview {
  position: absolute;
  top: 50%;
  right: calc(100% + 10px);
  z-index: 30;
  display: grid;
  width: min(380px, calc(100vw - 48px));
  gap: 10px;
  padding: 13px;
  border: 1px solid color-mix(in srgb, var(--brief-green) 20%, var(--brief-line));
  border-radius: 12px;
  background: var(--brief-surface);
  box-shadow: 0 18px 42px rgb(18 45 31 / 16%);
  opacity: 0;
  color: var(--brief-text);
  pointer-events: none;
  text-align: left;
  transform: translate(8px, -50%);
  transition: opacity 140ms ease, transform 140ms ease, visibility 140ms ease;
  visibility: hidden;
}

.event-voucher-preview::after {
  position: absolute;
  top: calc(50% - 5px);
  right: -6px;
  width: 10px;
  height: 10px;
  border-top: 1px solid color-mix(in srgb, var(--brief-green) 20%, var(--brief-line));
  border-right: 1px solid color-mix(in srgb, var(--brief-green) 20%, var(--brief-line));
  background: var(--brief-surface);
  content: "";
  transform: rotate(45deg);
}

.event-voucher-link:hover .event-voucher-preview,
.event-voucher-link:focus-within .event-voucher-preview {
  opacity: 1;
  transform: translate(0, -50%);
  visibility: visible;
}

.voucher-preview-heading,
.voucher-preview-footer,
.voucher-preview-lines > span {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.voucher-preview-heading > span {
  display: grid;
  min-width: 0;
  gap: 3px;
}

.voucher-preview-heading small,
.voucher-preview-footer small,
.voucher-preview-empty {
  color: var(--brief-muted);
  font-size: 10px;
}

.voucher-preview-heading strong {
  font-size: 13px;
  overflow-wrap: anywhere;
}

.voucher-preview-heading b {
  flex: none;
  font-size: 14px;
  white-space: nowrap;
}

.voucher-preview-lines {
  display: grid;
  gap: 5px;
  padding: 8px 9px;
  border-radius: 8px;
  background: var(--brief-soft);
}

.voucher-preview-lines > span {
  align-items: flex-start;
  min-width: 0;
  color: var(--brief-muted);
  font-size: 11px;
}

.voucher-preview-lines > span > span {
  min-width: 0;
  overflow-wrap: anywhere;
}

.voucher-preview-lines strong {
  flex: none;
  color: var(--brief-text);
  font-size: 11px;
  white-space: nowrap;
}

.voucher-preview-footer > .state {
  min-height: 20px;
  padding: 1px 6px;
  font-size: 10px;
}

.business-pagination {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 2px 0;
  color: var(--brief-muted);
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
  border: 1px solid var(--brief-line);
  border-radius: 8px;
  background: var(--brief-surface);
  color: var(--brief-green);
  font: inherit;
  cursor: pointer;
}

.business-pagination button:hover:not(:disabled) {
  border-color: var(--brief-green);
  background: var(--brief-green-soft);
}

.business-pagination button:disabled {
  color: var(--brief-muted);
  cursor: default;
  opacity: 0.5;
}

.business-pagination strong {
  min-width: 42px;
  color: var(--brief-text);
  text-align: center;
}

.state,
.party {
  display: inline-flex;
  min-height: 23px;
  align-items: center;
  padding: 2px 7px;
  border-radius: 999px;
  background: var(--brief-green-soft);
  color: var(--brief-green);
  font-weight: 750;
}

.event-row > .state {
  min-height: 21px;
  padding: 1px 7px;
  font-size: 11px;
  white-space: nowrap;
}

.state.correction {
  background: var(--brief-amber-soft);
  color: var(--brief-amber);
}

.voucher-view {
  min-width: 0;
}

.voucher-list {
  display: grid;
  gap: 7px;
}

.voucher-pagination {
  justify-content: flex-end;
}

.voucher-card {
  overflow: hidden;
  border: 1px solid var(--brief-line);
  border-radius: 12px;
  background: var(--brief-surface);
  transition: border-color 140ms ease, box-shadow 140ms ease;
}

.voucher-card:hover {
  border-color: var(--brief-line-strong);
}

.voucher-card.is-open {
  border-color: color-mix(in srgb, var(--brief-green) 42%, var(--brief-line));
  box-shadow: 0 7px 18px rgb(18 45 31 / 6%);
}

.voucher-row {
  display: grid;
  width: 100%;
  min-height: 62px;
  grid-template-columns: 132px minmax(180px, 1fr) auto 132px 16px;
  grid-template-areas: "reference copy state amount chevron";
  gap: 14px;
  align-items: center;
  padding: 9px 13px;
  border: 0;
  background: transparent;
  color: var(--brief-text);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.voucher-row:hover {
  background: color-mix(in srgb, var(--brief-green-soft) 48%, var(--brief-surface));
}

.voucher-card.is-open > .voucher-row {
  background: color-mix(in srgb, var(--brief-green-soft) 34%, var(--brief-surface));
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
  color: var(--brief-green);
  font-size: 12px;
}

.voucher-reference small,
.voucher-copy small {
  overflow: hidden;
  color: var(--brief-muted);
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
  border-right: 1.5px solid var(--brief-muted);
  border-bottom: 1.5px solid var(--brief-muted);
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
  margin: 0 13px;
  padding: 2px 0 16px;
  border-top: 1px solid var(--brief-line);
}

.voucher-correction {
  margin: 12px 0 0;
  color: var(--brief-amber);
  font-size: 12px;
}

.voucher-asset-link {
  display: flex;
  align-items: baseline;
  gap: 9px;
  min-width: 0;
  margin: 10px 0 2px;
  padding: 7px 9px;
  border-radius: 8px;
  background: var(--brief-soft);
  color: var(--brief-text);
  text-decoration: none;
}

.voucher-asset-link > span {
  flex: none;
  color: var(--brief-muted);
  font-size: 11px;
}

.voucher-asset-link > strong {
  min-width: 0;
  overflow-wrap: anywhere;
  font-size: 12px;
}

.voucher-asset-link > small {
  flex: none;
  margin-left: auto;
  color: var(--brief-green);
  font-size: 11px;
  font-weight: 750;
}

.voucher-asset-link:hover {
  background: var(--brief-green-soft);
}

.voucher-asset-link:focus-visible {
  outline: 2px solid var(--brief-green);
  outline-offset: 2px;
}

.voucher-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0 8px;
  margin-top: 10px;
  padding: 4px 6px;
  border-radius: 9px;
  background: var(--brief-soft);
}

.voucher-action,
.voucher-actions :deep(.trace-details.compact > .trace-button) {
  flex: 0 0 auto;
  order: 1;
  min-height: 32px;
  margin: 0;
  padding: 0 8px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--brief-green);
  font: inherit;
  font-size: 12px;
  font-weight: 750;
  cursor: pointer;
}

.voucher-action:hover,
.voucher-actions :deep(.trace-details.compact > .trace-button:hover) {
  background: var(--brief-green-soft);
}

.voucher-action span {
  margin-left: 3px;
  color: var(--brief-muted);
  font-weight: 500;
}

.voucher-actions :deep(.trace-details.compact) {
  display: contents;
}

.voucher-actions :deep(.trace-details.compact > .trace-content) {
  flex: 1 0 100%;
  order: 3;
  min-width: 0;
  padding-top: 8px;
}

.voucher-evidence {
  flex: 1 0 100%;
  order: 2;
  min-width: 0;
  padding: 6px 8px 2px;
}

.voucher-evidence ul {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 5px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.voucher-evidence li {
  overflow: hidden;
  color: var(--brief-muted);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
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
  background: var(--brief-soft);
  color: var(--brief-muted);
  font-size: 11px;
}

th:first-child {
  border-radius: 8px 0 0 8px;
}

th:last-child {
  border-radius: 0 8px 8px 0;
}

tbody tr:nth-child(even) {
  background: color-mix(in srgb, var(--brief-soft) 55%, transparent);
}

td:first-child small,
td:first-child strong {
  display: block;
}

td:first-child small {
  margin-bottom: 2px;
  color: var(--brief-muted);
}

td:nth-child(2) > small {
  display: block;
  margin-top: 4px;
  color: var(--brief-muted);
}

.number {
  text-align: right;
  white-space: nowrap;
}

.empty {
  margin: 0;
  padding: 30px;
  border: 1px dashed var(--brief-line-strong);
  border-radius: 14px;
  color: var(--brief-muted);
  text-align: center;
}

.activity-pagination:empty {
  display: none;
}

.activity-pagination {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--brief-line);
}

.activity-pagination :deep(.dashboard-pagination) {
  padding: 0;
  color: var(--brief-muted);
}

@media (max-width: 760px) {
  .brief-section {
    padding: 17px;
    border-radius: 17px;
  }

  .section-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 12px;
  }

  .view-switch {
    width: 100%;
  }

  .heading-controls {
    width: 100%;
    flex-wrap: wrap;
  }

  .heading-controls .view-switch {
    flex: 1 1 220px;
    width: auto;
  }

  .view-switch button {
    min-height: 44px;
  }

  .workbench {
    display: block;
  }

  .index {
    max-height: none;
    flex-direction: row;
    overflow-x: auto;
    border-right: 0;
    border-bottom: 1px solid var(--brief-line);
    border-radius: 15px 15px 0 0;
    scroll-snap-type: x proximity;
  }

  .index button {
    min-width: 210px;
    min-height: 64px;
    scroll-snap-align: start;
  }

  .detail {
    padding: 12px;
    border-radius: 0 0 15px 15px;
  }

  .detail-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 4px;
  }

  .event-row {
    min-height: 78px;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 5px 10px;
    padding: 9px 10px;
  }

  .event-reference {
    grid-row: 1;
    grid-column: 1;
  }

  .event-copy {
    grid-row: 2;
    grid-column: 1 / 3;
  }

  .event-row > .state {
    grid-row: 1;
    grid-column: 2;
  }

  .event-money {
    grid-row: 3;
    grid-column: 1;
    text-align: left;
  }

  .event-voucher-link {
    grid-row: 3;
    grid-column: 2;
    justify-self: end;
  }

  .event-voucher-preview {
    display: none;
  }

  .business-pagination {
    align-items: flex-start;
    flex-direction: column;
  }

  .voucher-row {
    min-height: 74px;
    grid-template-columns: minmax(0, 1fr) auto 14px;
    grid-template-areas:
      "reference amount chevron"
      "copy state chevron";
    gap: 6px 10px;
    padding: 10px 11px;
  }

  .voucher-reference {
    display: flex;
    align-items: baseline;
    gap: 8px;
  }

  .voucher-row > .state {
    justify-self: end;
  }

  .voucher-row-amount {
    font-size: 13px;
  }

  .voucher-inline-detail {
    margin: 0 11px;
    padding-bottom: 13px;
  }

  .voucher-evidence ul {
    grid-template-columns: minmax(0, 1fr);
  }

  table,
  tbody,
  tr,
  td {
    display: block;
    width: 100%;
  }

  thead {
    display: none;
  }

  tbody {
    display: grid;
    gap: 6px;
  }

  tr {
    padding: 8px 0;
    border: 0;
    border-radius: 8px;
    background: color-mix(in srgb, var(--brief-soft) 62%, transparent);
  }

  td,
  td:first-child {
    display: grid;
    grid-template-columns: 86px minmax(0, 1fr);
    gap: 8px;
    padding: 5px 0;
    border: 0;
    text-align: left;
    white-space: normal;
  }

  td::before {
    color: var(--brief-muted);
    font-size: 11px;
    font-weight: 750;
    content: attr(data-label);
  }

  td:first-child small,
  td:first-child strong {
    grid-column: 2;
  }
}
</style>
