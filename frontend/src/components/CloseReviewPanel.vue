<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";

import { dashboardErrorMessage, isDashboardSnapshotChanged } from "../api/client";
import { fetchCloseReview, mergeCloseReviewSection, type CloseReviewCollection, type CloseReviewCollections, type CloseReviewSection } from "../api/closeReview";
import type { DashboardCloseReviewResponse } from "../api/generated/dashboardResponses";
import { businessStateLabel } from "../api/dashboardContracts";
import { formatFen } from "../utils/money";
import DashboardPagination from "./DashboardPagination.vue";

const props = defineProps<{ companyId: string; period: string; refreshKey: number }>();
type ReviewCollection = CloseReviewCollection;

const response = ref<DashboardCloseReviewResponse | null>(null);
const pinnedDigest = ref<string | null>(null);
const loading = ref(false);
const error = ref("");
const collections = ref<CloseReviewCollections>({});
const sectionLoading = ref<Partial<Record<CloseReviewSection, boolean>>>({});
const sectionErrors = ref<Partial<Record<CloseReviewSection, string>>>({});
const sectionRequests = new Map<CloseReviewSection, AbortController>();
let controller: AbortController | null = null;
let generation = 0;
let mounted = true;

const review = computed(() => response.value?.owner_review ?? null);
const statusCopy = computed(() => ({
  prepared: ["关账预览已准备，可按同一版本核对", "以下内容来自当前活动预览；展开明细时仍读取这一固定版本。"],
  closed: ["本月已关账", "以下是关账时冻结的负责人核对内容。"],
  covered: ["本月已被后续关账覆盖", response.value?.covered_by ? `本月没有独立关账，也没有本月的批准内容；由 ${response.value.covered_by.period} 月关账覆盖。` : "本月没有独立关账，也没有本月的批准内容。"],
  unprepared: ["尚未生成关账预览", "请先让 AI 会计生成本月关账预览，再回到这里核对。"],
  stale: ["这份核对内容已失效", "活动预览已被替代。本页不会自动切换版本，请刷新经营简报后读取新预览。"],
}[response.value?.state ?? "unprepared"]));

function cancel() {
  controller?.abort(); controller = null;
  for (const request of sectionRequests.values()) request.abort();
  sectionRequests.clear(); sectionLoading.value = {}; sectionErrors.value = {};
}

async function loadSummary() {
  const current = ++generation;
  cancel(); response.value = null; pinnedDigest.value = null; collections.value = {};
  if (!props.companyId || !props.period) return;
  const request = new AbortController(); controller = request; loading.value = true; error.value = "";
  try {
    const result = await fetchCloseReview(props.companyId, props.period, request.signal);
    if (!mounted || generation !== current || controller !== request) return;
    response.value = result;
    if ((result.state === "prepared" || result.state === "closed") && result.preview_digest) pinnedDigest.value = result.preview_digest;
  } catch (caught) {
    if (!mounted || generation !== current || controller !== request || (caught instanceof DOMException && caught.name === "AbortError")) return;
    error.value = dashboardErrorMessage(caught);
  } finally {
    if (mounted && generation === current && controller === request) { controller = null; loading.value = false; }
  }
}

async function loadSection(section: CloseReviewSection, more = false) {
  const digest = pinnedDigest.value;
  const existing = collections.value[section];
  if (!digest || sectionLoading.value[section] || (more && !existing?.page.next_cursor)) return;
  const current = generation;
  sectionRequests.get(section)?.abort();
  const request = new AbortController(); sectionRequests.set(section, request);
  sectionLoading.value[section] = true; sectionErrors.value[section] = "";
  let retryWithoutCursor = false;
  try {
    const result = await fetchCloseReview(props.companyId, props.period, request.signal, {
      previewDigest: digest, section, cursor: more ? existing?.page.next_cursor ?? undefined : undefined,
    });
    if (!mounted || generation !== current || sectionRequests.get(section) !== request) return;
    if (["stale", "unprepared", "covered"].includes(result.state)) { response.value = result; pinnedDigest.value = null; collections.value = {}; return; }
    if (result.preview_digest !== digest || !result.collection) return;
    const merged = mergeCloseReviewSection(response.value, collections.value, result, section, more);
    if (merged.bindingChanged) {
      for (const [otherSection, otherRequest] of sectionRequests) {
        if (otherSection !== section) { otherRequest.abort(); sectionRequests.delete(otherSection); }
      }
      sectionLoading.value = { [section]: true };
      sectionErrors.value = {};
    }
    response.value = result;
    collections.value = merged.collections;
  } catch (caught) {
    if (mounted && generation === current && sectionRequests.get(section) === request && more && isDashboardSnapshotChanged(caught)) {
      collections.value = { ...collections.value, [section]: undefined };
      retryWithoutCursor = true;
    } else if (mounted && generation === current && sectionRequests.get(section) === request && !(caught instanceof DOMException && caught.name === "AbortError")) {
      sectionErrors.value[section] = dashboardErrorMessage(caught);
    }
  } finally {
    if (mounted && generation === current && sectionRequests.get(section) === request) {
      sectionRequests.delete(section); sectionLoading.value[section] = false;
    }
  }
  if (retryWithoutCursor && mounted && generation === current) void loadSection(section);
}

function opened(event: Event, section: CloseReviewSection) {
  if ((event.target as HTMLDetailsElement).open && !collections.value[section]) void loadSection(section);
}

function sectionLabel(section: CloseReviewSection) {
  return { vouchers: "正式凭证", adopted_bases: "实际采用依据", policies: "政策依据", payroll_confirmations: "工资确认", evidence: "原始凭据" }[section];
}

function itemStatusLabel(status: string) {
  return ({ posted: "已记账", reversal: "冲正凭证", adopted: "本月实际采用", confirmed: "负责人已确认", retained: "原件已保全" } as Record<string, string>)[status]
    ?? businessStateLabel(status);
}

function businessActionLabel(action: "business" | "correction" | "opening" | "state") {
  return ({ business: "本月业务", correction: "更正入账", opening: "期初", state: "状态采用" } as const)[action];
}

function materialCategoryLabel(category: string) {
  return ({ transactions: "业务资料", payroll: "工资资料", bank: "银行与资金资料", tax: "税务资料", assets: "资产资料", financing: "融资资料" } as Record<string, string>)[category] ?? category;
}

function collectionPage(collection: ReviewCollection) {
  return { ...collection.page, filtered_count: collection.page.total_count };
}

function referenceLabel(reference: ReviewCollection["items"][number]["references"][number]) {
  if (reference.name) return reference.name;
  return { voucher: "凭证记录", calculation: "核算结果", fact: "业务事实", evidence: "原始凭据", inventory: "资料清单" }[reference.source_type];
}

watch(() => [props.companyId, props.period, props.refreshKey], loadSummary, { immediate: true, flush: "post" });
onBeforeUnmount(() => { mounted = false; generation += 1; cancel(); });
</script>

<template>
  <section class="close-review" aria-labelledby="close-review-title">
    <header>
      <div><p>负责人只读核对</p><h3 id="close-review-title">关账前核对内容</h3></div>
      <span v-if="response" :class="['review-state', response.state]">{{ statusCopy[0] }}</span>
    </header>
    <p v-if="loading" role="status">正在读取关账核对内容…</p>
    <p v-else-if="error" class="review-error" role="alert">{{ error }}</p>
    <template v-else-if="response">
      <p class="review-note">{{ statusCopy[1] }}</p>
      <template v-if="review && response.state !== 'stale'">
        <dl class="accounting-summary">
          <div><dt>本月凭证</dt><dd>{{ review.accounting_summary.voucher_count }} 张 / {{ review.accounting_summary.line_count }} 行</dd></div>
          <div><dt>借方 / 贷方</dt><dd>{{ formatFen(review.accounting_summary.total_debit_fen) }} / {{ formatFen(review.accounting_summary.total_credit_fen) }}</dd></div>
          <div><dt>本月收入</dt><dd>{{ formatFen(review.accounting_summary.month_revenue_fen) }}</dd></div>
          <div><dt>本月费用</dt><dd>{{ formatFen(review.accounting_summary.month_expense_fen) }}</dd></div>
          <div><dt>本月结果</dt><dd>{{ formatFen(review.accounting_summary.month_result_fen) }}</dd></div>
          <div><dt>期末资产 / 负债 / 权益</dt><dd>{{ formatFen(review.accounting_summary.ending_assets_fen) }} / {{ formatFen(review.accounting_summary.ending_liabilities_fen) }} / {{ formatFen(review.accounting_summary.ending_equity_fen) }}</dd></div>
          <div><dt>资金合计</dt><dd>{{ formatFen(review.accounting_summary.funds_total_fen) }}</dd></div>
          <div><dt>银行 / 现金 / 支付平台</dt><dd>{{ formatFen(review.accounting_summary.bank_fen) }} / {{ formatFen(review.accounting_summary.cash_fen) }} / {{ formatFen(review.accounting_summary.payment_platform_fen) }}</dd></div>
          <div><dt>实际收款 / 付款 / 内部转账</dt><dd>{{ formatFen(review.accounting_summary.actual_receipts_fen) }} / {{ formatFen(review.accounting_summary.actual_payments_fen) }} / {{ formatFen(review.accounting_summary.internal_transfer_fen) }}</dd></div>
          <div><dt>凭证借贷</dt><dd>{{ review.accounting_summary.voucher_balanced ? '平衡' : '需要核对' }}</dd></div>
          <div><dt>资产负债试算</dt><dd>{{ !review.accounting_summary.financial_position_complete ? '资料尚不能完整确认' : review.accounting_summary.financial_position_balanced ? '平衡' : '需要核对' }}</dd></div>
        </dl>
        <details open><summary>全月业务</summary><ul><li v-for="item in review.business_summary" :key="`${item.action}-${item.kind}-${item.reversal}`"><strong>{{ businessActionLabel(item.action) }} · {{ item.label }}{{ item.reversal ? '（冲正）' : '' }}</strong><span>{{ item.count }} 项<template v-if="item.business_amount_fen != null"> · {{ item.amount_label }} {{ formatFen(item.business_amount_fen) }}</template> · 凭证金额 {{ formatFen(item.journal_total_fen) }}</span></li></ul></details>
        <details><summary>资料覆盖与负责人确认</summary><ul><li v-for="item in review.material_summary" :key="`${item.category}-${item.inventory_id}`"><strong>{{ materialCategoryLabel(item.category) }}</strong><span>{{ item.received }} / {{ item.expected }}{{ item.no_business ? ' · 已确认无业务' : '' }} · {{ referenceLabel(item.confirmation) }}</span><details><summary>内部校验信息</summary><code>{{ item.confirmation.id }}</code><code v-if="item.confirmation.digest">{{ item.confirmation.digest }}</code></details></li></ul><p>月度负责人确认：{{ referenceLabel(review.owner_confirmation) }}</p><details><summary>内部校验信息</summary><code>{{ review.owner_confirmation.id }}</code><code v-if="review.owner_confirmation.digest">{{ review.owner_confirmation.digest }}</code></details></details>
        <details><summary>实际采用依据概况</summary><p>{{ review.adopted_basis_summary.summary }}</p></details>
        <details><summary>关账后仍需跟进</summary><p>共 {{ review.followup_summary.followup_count }} 项 · 关账核对问题 {{ review.followup_summary.close_issue_count }} 项 · 往来清偿问题 {{ review.followup_summary.settlement_issue_count }} 项 · 外部办理问题 {{ review.followup_summary.external_issue_count }} 项 · 文件任务问题 {{ review.followup_summary.file_issue_count }} 项</p></details>
        <div class="review-directories">
          <details v-for="directory in review.collections" :key="directory.section" @toggle="opened($event, directory.section)">
            <summary>{{ directory.label || sectionLabel(directory.section) }} <span>{{ directory.total_count }} 项</span></summary>
            <p v-if="sectionLoading[directory.section] && !collections[directory.section]">正在读取…</p>
            <p v-if="sectionErrors[directory.section] && !collections[directory.section]" class="review-error">{{ sectionErrors[directory.section] }}</p>
            <template v-if="collections[directory.section]">
              <article v-for="item in collections[directory.section]!.items" :key="item.key" class="review-item">
                <div><strong>{{ item.title }}</strong><span>{{ item.subtitle }}</span></div>
                <p>{{ itemStatusLabel(item.status) }}<template v-if="item.count != null"> · {{ item.count }} 项</template><template v-if="item.amount_fen != null"> · {{ formatFen(item.amount_fen) }}</template></p>
                <details v-if="item.references.length"><summary>查看精确来源</summary><ul><li v-for="reference in item.references" :key="`${reference.source_type}-${reference.id}`"><span>{{ referenceLabel(reference) }}<template v-if="reference.revision != null"> · 第 {{ reference.revision }} 版</template></span><details><summary>内部校验信息</summary><code>{{ reference.id }}</code><code v-if="reference.digest">{{ reference.digest }}</code><span v-if="reference.media_type">{{ reference.media_type }}</span></details></li></ul></details>
              </article>
              <DashboardPagination :page="collectionPage(collections[directory.section]!)" :loaded="collections[directory.section]!.items.length" :loading="sectionLoading[directory.section]" :error="sectionErrors[directory.section]" @more="loadSection(directory.section, true)" @retry="loadSection(directory.section, true)" />
            </template>
          </details>
        </div>
      </template>
    </template>
  </section>
</template>

<style scoped>
.close-review { grid-column: 1 / -1; display: grid; gap: 12px; padding: 18px; border: 1px solid var(--line); border-radius: var(--radius-panel); background: var(--surface); }
header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
header p, header h3, .review-note { margin: 0; } header p { color: var(--muted); font-size: 12px; } header h3 { margin-top: 4px; }
.review-state { padding: 5px 9px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); font-size: 12px; font-weight: 750; }
.review-state.stale, .review-error { color: var(--danger); } .review-state.unprepared { color: var(--warning); background: var(--warning-soft); }
.review-note { color: var(--muted); font-size: 13px; }
.accounting-summary { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin: 0; }
.accounting-summary div { padding: 11px; border-radius: var(--radius-control); background: var(--surface-soft); } dt { color: var(--muted); font-size: 11px; } dd { margin: 4px 0 0; font-weight: 750; }
details > summary { padding: 6px 0; color: var(--accent); cursor: pointer; } summary span { color: var(--muted); font-size: 11px; }
ul { display: grid; gap: 7px; margin: 8px 0 0; padding: 0; list-style: none; } li { display: flex; justify-content: space-between; gap: 12px; } li span { color: var(--muted); }
.review-directories { display: grid; gap: 8px; padding-top: 5px; border-top: 1px solid var(--line); }
.review-item { padding: 10px 0; border-bottom: 1px solid var(--line); } .review-item > div { display: grid; gap: 3px; } .review-item p { margin: 5px 0; }
.review-item code { display: block; overflow-wrap: anywhere; color: var(--muted); }
@media (max-width: 760px) { header { flex-direction: column; } .accounting-summary { grid-template-columns: 1fr; } li { flex-direction: column; gap: 2px; } }
</style>
