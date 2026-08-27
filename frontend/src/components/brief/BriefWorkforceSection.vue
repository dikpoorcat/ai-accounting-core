<script setup lang="ts">
import type { BriefWorkforceCost, WorkforcePeriod } from "../../api/brief";
import { fen, formatFen } from "../../utils/money";

const props = defineProps<{
  workforce: BriefWorkforceCost;
  periodKey: string;
  periodLabel: string;
}>();

function costPeriodLabel(
  period: WorkforcePeriod,
  key: "payroll_period" | "remuneration_period",
) {
  const sourcePeriod = period[key];
  if (!sourcePeriod) return "来源月份未展示";
  const sourceMonth = Number(sourcePeriod.slice(5));
  const label =
    sourcePeriod === props.periodKey
      ? `${sourceMonth} 月本月计提`
      : `${sourceMonth} 月${period.has_reversal ? "补记/调整" : "补记"}`;
  return `${label}${period.has_reversal ? "（含更正）" : ""}`;
}

function employeeCostNote() {
  const employee = props.workforce.employee;
  if (!employee.has_activity) return "本月没有入账的正式员工职工薪酬。";
  if (!employee.breakdown_available) {
    return employee.reason || "现有数据缺少可靠拆分依据，仅展示职工薪酬小计。";
  }
  const parts: string[] = [];
  if (fen(employee.employee_social_insurance_fen)) {
    parts.push(`个人承担社保医保 ${formatFen(employee.employee_social_insurance_fen)}`);
  }
  if (fen(employee.employee_housing_fund_fen)) {
    parts.push(`个人承担住房公积金 ${formatFen(employee.employee_housing_fund_fen)}`);
  }
  if (parts.length) parts.push("已包含在工资总额中，仅由工资代扣，不会重复计入公司成本");
  if (fen(employee.settlement_adjustment_fen)) {
    parts.push("工资结算调整已单列反映，不属于本月工资计提");
  }
  return parts.length
    ? `${parts.join("；")}。`
    : "本月没有从工资代扣的个人社保医保或住房公积金。";
}

function laborCostNote() {
  const labor = props.workforce.personal_labor;
  if (!labor.has_activity) return "本月没有入账的非员工个人劳务报酬。";
  if (!labor.breakdown_available) {
    return labor.reason || "现有数据缺少可靠拆分依据，仅展示劳务报酬小计。";
  }
  if (labor.withholding_status === "correction") {
    return "本月个人劳务成本含冲正或更正，未将理论税额认定为新增实际代扣。";
  }
  if (labor.withholding_status === "partially_settled") {
    const actual = fen(labor.actual_withholding_tax_fen);
    return `劳务尚未全部付款；已付款部分${actual ? `实际代扣个人所得税 ${formatFen(actual)}` : "实际未代扣个人所得税"}，未付款部分仅有理论测算。`;
  }
  if (labor.withholding_status === "pending_payment") {
    return "劳务尚未付款，个人所得税仅完成理论测算，未认定为实际代扣。";
  }
  if (fen(labor.actual_withholding_tax_fen)) {
    return `实际从劳务毛额代扣个人所得税 ${formatFen(labor.actual_withholding_tax_fen)}，不增加公司用工成本。`;
  }
  return labor.withholding_status === "not_withheld"
    ? "劳务报酬已按毛额支付，实际未代扣个人所得税，不影响用工成本。"
    : "本月劳务报酬没有需要说明的实际代扣个人所得税。";
}
</script>

<template>
  <section class="brief-section workforce" aria-labelledby="workforce-title">
    <div class="section-heading">
      <div>
        <p class="section-kicker">费用拆分</p>
        <h2 id="workforce-title">本月用工成本</h2>
      </div>
      <div class="total">
        <span>公司本月用工成本</span>
        <strong>{{ formatFen(workforce.total_fen) }}</strong>
      </div>
    </div>

    <div class="workforce-grid">
      <article class="workforce-card" aria-labelledby="employee-title">
        <header>
          <div>
            <h3 id="employee-title">正式员工</h3>
            <p>工资及公司承担的社保医保、公积金</p>
          </div>
          <div class="subtotal">
            <span>员工成本小计</span>
            <strong>{{ formatFen(workforce.employee.total_fen) }}</strong>
          </div>
        </header>
        <div v-if="workforce.employee.breakdown_available" class="cost-grid">
          <div class="cost salary">
            <span>工资总额</span>
            <strong>{{ formatFen(workforce.employee.gross_salary_fen) }}</strong>
          </div>
          <div class="cost social">
            <span>公司承担社保医保</span>
            <strong>{{ formatFen(workforce.employee.employer_social_insurance_fen) }}</strong>
          </div>
          <div v-if="fen(workforce.employee.employer_housing_fund_fen)" class="cost fund">
            <span>公司承担住房公积金</span>
            <strong>{{ formatFen(workforce.employee.employer_housing_fund_fen) }}</strong>
          </div>
        </div>
        <dl v-if="fen(workforce.employee.settlement_adjustment_fen)" class="reconciliation">
          <div>
            <dt>{{ periodLabel }}工资及公司社保</dt>
            <dd>{{ formatFen(workforce.employee.controlled_total_fen) }}</dd>
          </div>
          <span>+</span>
          <div>
            <dt>
              {{
                fen(workforce.employee.settlement_adjustment_fen) ===
                fen(workforce.employee.prior_period_settlement_adjustment_fen)
                  ? "以前月份工资结算调整"
                  : "工资结算调整"
              }}
            </dt>
            <dd>{{ formatFen(workforce.employee.settlement_adjustment_fen) }}</dd>
          </div>
          <span>=</span>
          <div>
            <dt>职工薪酬科目净额</dt>
            <dd>{{ formatFen(workforce.employee.total_fen) }}</dd>
          </div>
        </dl>
        <p :class="['note', { attention: !workforce.employee.breakdown_available }]">
          {{ employeeCostNote() }}
        </p>
        <div v-if="workforce.employee.periods.length" class="periods">
          <span
            v-for="period in workforce.employee.periods"
            :key="`${period.payroll_period}-${period.total_fen}`"
          >
            {{ costPeriodLabel(period, "payroll_period") }}
            <strong>{{ formatFen(period.total_fen) }}</strong>
          </span>
        </div>
      </article>

      <article class="workforce-card" aria-labelledby="labor-title">
        <header>
          <div>
            <h3 id="labor-title">非员工个人劳务</h3>
            <p>个人劳务报酬及佣金，不作为员工工资</p>
          </div>
          <div class="subtotal">
            <span>劳务成本小计</span>
            <strong>{{ formatFen(workforce.personal_labor.total_fen) }}</strong>
          </div>
        </header>
        <div v-if="workforce.personal_labor.breakdown_available" class="cost-grid single">
          <div class="cost labor">
            <span>个人劳务报酬 / 佣金毛额</span>
            <strong>{{ formatFen(workforce.personal_labor.gross_remuneration_fen) }}</strong>
          </div>
        </div>
        <p :class="['note', { attention: !workforce.personal_labor.breakdown_available }]">
          {{ laborCostNote() }}
        </p>
        <div v-if="workforce.personal_labor.periods.length" class="periods">
          <span
            v-for="period in workforce.personal_labor.periods"
            :key="`${period.remuneration_period}-${period.total_fen}`"
          >
            {{ costPeriodLabel(period, "remuneration_period") }}
            <strong>{{ formatFen(period.total_fen) }}</strong>
          </span>
        </div>
      </article>
    </div>
    <p class="payment-note">
      工资、社保医保及个人劳务的实际付款只清偿已经确认的应付款，不会在付款时再次计入用工成本。
    </p>
  </section>
</template>

<style scoped>
.brief-section {
  padding: 20px;
  border: 1px solid var(--brief-line);
  border-radius: 20px;
  background: var(--brief-surface);
  box-shadow: var(--brief-shadow);
}

.section-heading,
.workforce-card header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
}

.section-heading {
  align-items: flex-end;
  margin-bottom: 16px;
}

.section-kicker {
  margin: 0 0 4px;
  color: var(--brief-green);
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 0.08em;
}

h2,
h3,
p {
  margin-top: 0;
}

h2 {
  margin-bottom: 0;
  font-size: 23px;
  letter-spacing: -0.025em;
}

h3 {
  margin-bottom: 4px;
}

.total,
.subtotal {
  display: grid;
  justify-items: end;
  white-space: nowrap;
}

.total span,
.subtotal span,
.workforce-card header p,
.cost span {
  color: var(--brief-muted);
  font-size: 12px;
}

.total strong {
  font-size: 23px;
}

.subtotal strong {
  font-size: 18px;
}

.workforce-card header p {
  margin-bottom: 0;
}

.workforce-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  align-items: start;
}

.workforce-card {
  padding: 15px;
  border: 1px solid var(--brief-line);
  border-radius: 16px;
  background: var(--brief-soft);
}

.cost-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin-top: 11px;
}

.cost-grid.single {
  grid-template-columns: 1fr;
}

.cost {
  display: grid;
  gap: 3px;
  padding: 10px;
  border-radius: 12px;
  background: var(--brief-surface);
}

.cost strong {
  font-size: 18px;
}

.cost.salary {
  background: var(--brief-blue-soft);
  color: var(--brief-blue);
}

.cost.social {
  background: var(--brief-green-soft);
  color: var(--brief-green);
}

.cost.fund {
  background: var(--brief-gold-soft);
  color: var(--brief-gold);
}

.cost.labor {
  background: color-mix(in srgb, var(--brief-blue-soft) 58%, var(--brief-green-soft));
}

.reconciliation {
  display: grid;
  grid-template-columns: 1fr auto 1fr auto 1fr;
  gap: 8px;
  align-items: center;
  margin: 9px 0 0;
  padding: 8px 10px;
  border: 1px solid var(--brief-line);
  border-radius: 11px;
  background: var(--brief-surface);
}

.reconciliation div {
  display: grid;
  gap: 2px;
}

.reconciliation dt {
  color: var(--brief-muted);
  font-size: 10px;
}

.reconciliation dd {
  margin: 0;
  font-size: 13px;
  font-weight: 800;
}

.reconciliation > span {
  color: var(--brief-muted);
}

.note,
.payment-note {
  margin: 9px 0 0;
  color: var(--brief-muted);
  font-size: 12px;
  line-height: 1.65;
}

.note {
  padding: 8px 10px;
  border-radius: 10px;
  background: var(--brief-surface);
}

.note.attention {
  background: var(--brief-amber-soft);
  color: var(--brief-amber);
}

.periods {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 9px;
}

.periods > span {
  display: inline-flex;
  min-height: 27px;
  align-items: center;
  gap: 6px;
  padding: 3px 8px;
  border: 1px solid var(--brief-line);
  border-radius: 999px;
  background: var(--brief-surface);
  font-size: 11px;
}

.payment-note {
  margin-bottom: 0;
  padding-left: 2px;
}

@media (max-width: 900px) {
  .workforce-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 560px) {
  .brief-section {
    padding: 17px;
    border-radius: 17px;
  }

  .section-heading,
  .workforce-card header {
    align-items: flex-start;
    flex-direction: column;
  }

  .total,
  .subtotal {
    justify-items: start;
  }

  .cost-grid {
    grid-template-columns: 1fr;
  }

  .reconciliation {
    grid-template-columns: 1fr;
  }

  .reconciliation > span {
    display: none;
  }
}
</style>
