<script setup lang="ts">
import { computed } from "vue";
import type { BriefComponent, BriefFundMovement, BriefSettlement } from "../../api/brief";
import { formatFen } from "../../utils/money";
const props = withDefaults(defineProps<{
  components: BriefComponent[];
  funds: BriefFundMovement[];
  settlements: BriefSettlement[];
  plain?: boolean;
}>(), { plain: false });
const kind = computed(() => props.components[0]?.kind || "");
const payment = computed(() => ["payment", "cash_payment", "platform_payment", "payroll_reserve_payment"].includes(kind.value));
const show = computed(() => ["employee_advance", "reimbursement_acceptance", "settlement", "reimbursed_asset_batch", "payroll_reserve_payment"].includes(kind.value)
  || (payment.value && (props.settlements.length > 1 || props.settlements.some(row => row.source_period !== props.components[0]?.recognition?.period))));
const heading = computed(() => kind.value === "reimbursed_asset_batch" ? "整批应付明细" : payment.value ? "付款对应事项" : "代付与抵销说明");
</script>
<template>
  <component :is="plain ? 'section' : 'details'" v-if="show" :class="['business-details', { plain }]">
    <h4 v-if="plain">{{ heading }}</h4>
    <summary v-else>{{ heading }}</summary>
    <p v-if="kind === 'reimbursed_asset_batch'">以下为整批资产的应付对象，未分摊到单张资产卡片。</p>
    <p v-else-if="kind === 'settlement'">本项通过款项抵销结清，不发生公司账户收付款。</p>
    <p v-else-if="['employee_advance', 'reimbursement_acceptance'].includes(kind)">原款项由个人代付，公司相应改为应付代付人。</p>
    <ul><li v-for="item in settlements" :key="item.id"><span>{{ item.source_label || `${item.source_period}相关款项` }}<template v-if="item.party"> · {{ item.party }}</template></span><strong>{{ formatFen(item.amount_fen) }}</strong></li></ul>
    <p v-if="kind === 'payroll_reserve_payment'">整笔付款明确拆分为应付净薪和备用金支出；备用金部分不作为工资清偿。</p>
  </component>
</template>
<style scoped>
.business-details { margin-top: 12px; color: var(--muted); font-size: 13px; }
.business-details.plain { margin-top: 14px; }
h4 { margin: 0 0 8px; color: var(--text); font-size: 13px; }
summary { cursor: pointer; }
p { margin: 10px 0; }
ul { padding-left: 18px; }
li { padding: 6px 0; }
strong { margin-left: 12px; color: var(--text); white-space: nowrap; }
</style>
