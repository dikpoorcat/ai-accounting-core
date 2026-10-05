<script setup lang="ts">
import { useRoute } from "vue-router";
import type { BriefData } from "../../api/brief";
import { formatFen } from "../../utils/money";
defineProps<{ funds: BriefData["funds_overview"] }>();
const route = useRoute();
</script>
<template>
  <section class="financial-section" aria-labelledby="financial-title">
    <header class="section-heading"><h2 id="financial-title">本月资金变化</h2><RouterLink :to="{ name: 'funds', query: route.query }">查看资金记录</RouterLink></header>
    <div class="overview-card">
      <dl class="flow">
        <div><dt>对外收款</dt><dd>{{ formatFen(funds.inflow_fen) }}</dd></div>
        <div><dt>对外付款</dt><dd>{{ formatFen(funds.outflow_fen) }}</dd></div>
      </dl>
      <dl class="summary-rows"><div><dt>资金净变动</dt><dd>{{ formatFen(funds.net_change_fen) }}</dd></div></dl>
      <p>对外收付款已扣除公司账户之间的转款。</p>
    </div>
  </section>
</template>
<style scoped>
.financial-section { min-width: 0; }
.section-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 18px; margin-bottom: 14px; padding: 0 2px; }
h2 { margin: 0; font-size: 22px; letter-spacing: -0.025em; }
a { color: var(--accent); font-size: 11px; font-weight: 700; text-decoration: none; }
a:hover { text-decoration: underline; }
.overview-card { min-width: 0; padding: 15px; border: 1px solid var(--line); border-radius: var(--radius-panel, 14px); background: var(--surface); }
.flow { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin: 0; }
.flow > div { min-width: 0; padding: 12px; border-radius: 10px; background: var(--surface-soft); }
dt { color: var(--muted); font-size: 11px; }
dd { margin: 4px 0 0; font-size: 19px; font-weight: 750; overflow-wrap: anywhere; }
.flow > div:first-child dd { color: var(--accent); }
.summary-rows { margin: 12px 0 0; }
.summary-rows > div { display: flex; align-items: baseline; justify-content: space-between; gap: 14px; padding-top: 12px; border-top: 1px solid var(--line); }
.summary-rows dd { min-width: 0; text-align: right; }
p { margin: 12px 0 0; color: var(--muted); font-size: 11px; line-height: 1.6; }
@media (max-width: 720px) {
  .section-heading { flex-direction: column; align-items: flex-start; gap: 7px; }
}
@media (max-width: 430px) {
  .flow { grid-template-columns: minmax(0, 1fr); }
  .summary-rows > div { flex-wrap: wrap; }
}
</style>
