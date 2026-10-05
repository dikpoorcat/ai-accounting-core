<script setup lang="ts">
import { computed, onBeforeUnmount, ref, shallowRef, watch } from "vue";
import { fetchCloseReview } from "../api/closeReview";
import { dashboardErrorMessage } from "../api/client";
import { formatFen } from "../utils/money";
const props = defineProps<{ companyId: string; period: string; previewDigest: string }>();
const response = shallowRef<Awaited<ReturnType<typeof fetchCloseReview>> | null>(null);
const loading = ref(false), error = ref("");
let controller: AbortController | null = null, generation = 0, mounted = true;
const review = computed(() => response.value?.state === "prepared" ? response.value.owner_review : null);
const statusCopy = computed(() => ({
  prepared: ["请核对本月业务", "请核对以下金额和业务。如无问题，请告知 AI 会计，由它继续请求密码确认。"],
  closed: ["本次核对已结束", "本月已关账，请刷新经营简报。"],
  covered: ["本次核对已结束", "本月已由后续关账覆盖，请刷新经营简报。"],
  unprepared: ["本次核对不可用", "请刷新经营简报后重新查看。"],
  stale: ["这份核对内容已失效", "本月资料已变化，请刷新经营简报后重新查看。"],
}[response.value?.state ?? "unprepared"]));
async function loadSummary() {
  const current = ++generation; controller?.abort(); response.value = null;
  if (!props.companyId || !props.period || !props.previewDigest) { loading.value = false; return; }
  const request = new AbortController(); controller = request; loading.value = true; error.value = "";
  try {
    const result = await fetchCloseReview(props.companyId, props.period, request.signal, { previewDigest: props.previewDigest });
    if (!mounted || generation !== current || controller !== request) return;
    response.value = result;
  } catch (caught) {
    if (!mounted || generation !== current || controller !== request || (caught instanceof DOMException && caught.name === "AbortError")) return;
    error.value = dashboardErrorMessage(caught);
  } finally { if (mounted && generation === current && controller === request) { controller = null; loading.value = false; } }
}
watch(() => [props.companyId, props.period, props.previewDigest], loadSummary, { immediate: true, flush: "sync" });
onBeforeUnmount(() => { mounted = false; generation += 1; controller?.abort(); });
</script>

<template>
  <section class="close-review" aria-labelledby="close-review-title">
    <header><h2 id="close-review-title">本次核对内容</h2><span v-if="response" :class="['review-state', response.state]">{{ statusCopy[0] }}</span></header>
    <p v-if="loading" role="status">正在读取月度核对内容…</p>
    <p v-else-if="error" class="review-error" role="alert">{{ error }}</p>
    <template v-else-if="response">
      <p class="review-note">{{ statusCopy[1] }}</p>
      <template v-if="review">
        <dl>
          <div><dt>本月收入</dt><dd>{{ formatFen(review.amounts.month_revenue_fen) }}</dd></div>
          <div><dt>本月费用</dt><dd>{{ formatFen(review.amounts.month_expense_fen) }}</dd></div>
          <div><dt>本月账面盈亏</dt><dd>{{ formatFen(review.amounts.month_result_fen) }}</dd></div>
          <div><dt>月末账面资金</dt><dd>{{ formatFen(review.amounts.funds_total_fen) }}</dd></div>
          <div><dt>实际收款</dt><dd>{{ formatFen(review.amounts.actual_receipts_fen) }}</dd></div>
          <div><dt>实际付款</dt><dd>{{ formatFen(review.amounts.actual_payments_fen) }}</dd></div>
        </dl>
        <h3>本月业务</h3>
        <ul><li v-for="(item, index) in review.businesses" :key="index"><strong>{{ item.label }}{{ item.reversal ? '（更正原业务）' : '' }}</strong><span>{{ item.count }} 项<template v-if="item.business_amount_fen !== null"> · {{ item.amount_label }} {{ formatFen(item.business_amount_fen) }}</template><template v-else> · 金额暂不能确认</template></span></li></ul>
        <p v-if="!review.businesses.length">本月没有已入账业务。</p>
      </template>
    </template>
  </section>
</template>

<style scoped>
.close-review { display: grid; gap: 12px; padding: 18px; border: 1px solid var(--line); border-radius: var(--radius-panel); background: var(--surface); min-width: 0; }
header { display: flex; justify-content: space-between; align-items: baseline; gap: 16px; } h2, h3, p { margin: 0; } h2 { font-size: 22px; } h3 { font-size: 15px; }
.review-state { padding: 5px 9px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); font-size: 12px; font-weight: 750; } .review-state.stale, .review-error { color: var(--danger); } .review-state.unprepared { color: var(--warning); background: var(--warning-soft); } .review-note { color: var(--muted); font-size: 13px; line-height: 1.6; }
dl { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin: 0; } dl div { padding: 11px; border-radius: var(--radius-control); background: var(--surface-soft); } dt { color: var(--muted); font-size: 11px; } dd { margin: 4px 0 0; font-weight: 750; overflow-wrap: anywhere; }
ul { display: grid; gap: 7px; margin: 0; padding: 0; list-style: none; } li { display: flex; justify-content: space-between; gap: 12px; overflow-wrap: anywhere; } li span { color: var(--muted); }
@media (max-width: 760px) { header { flex-direction: column; } dl { grid-template-columns: 1fr; } li { flex-direction: column; gap: 3px; } }
</style>
