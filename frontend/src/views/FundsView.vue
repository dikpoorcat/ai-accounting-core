<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { dashboardErrorMessage } from "../api/client";
import {
  fetchFundsDashboard,
  type BankStatementState,
  type FundsData,
} from "../api/funds";
import DashboardModuleHeader from "../components/DashboardModuleHeader.vue";
import { useDashboardContext } from "../composables/useDashboardContext";
import { fen, formatFen, formatPositiveFen } from "../utils/money";

const route = useRoute();
const router = useRouter();
const {
  context,
  loading: contextLoading,
  error: contextError,
  load: loadContext,
} = useDashboardContext();

const selectedPeriod = ref("");
const selectedAccount = ref("");
const funds = ref<FundsData | null>(null);
const selectedPeriodLabel = ref("");
const loading = ref(false);
const initializing = ref(true);
const requestError = ref("");
let activeRequest: AbortController | null = null;

const periods = computed(() => context.value?.periods ?? []);
const pageError = computed(() => requestError.value || contextError.value);
const visibleMovements = computed(() => {
  if (!funds.value) return [];
  if (!selectedAccount.value) return funds.value.movements;
  return funds.value.movements.filter(
    (item) => item.account_code === selectedAccount.value,
  );
});
const attentionItems = computed(() => {
  if (!funds.value) return [];
  const items: string[] = [];
  for (const account of funds.value.accounts) {
    if (account.negative_balance) {
      items.push(
        `${account.name}期末账面余额为负数 ${formatFen(account.closing_fen)}。`,
      );
    }
    if (
      ["attention", "pending", "not_configured"].includes(
        account.reconciliation.state,
      )
    ) {
      items.push(`${account.name}：${account.reconciliation.label}。`);
    }
  }
  const statement = funds.value.bank_statement;
  if (statement.unmatched_count) {
    items.push(
      `${statement.unmatched_count} 笔普通银行流水尚未完成有效匹配，不能当作已确认业务。`,
    );
  }
  if (statement.pending_late_count) {
    items.push(`${statement.pending_late_count} 笔迟到银行流水仍待处理。`);
  }
  return items;
});
const bankAttentionCount = computed(() => {
  const statement = funds.value?.bank_statement;
  return statement
    ? statement.unmatched_count + statement.pending_late_count
    : 0;
});

function routePeriod(): string | null {
  return typeof route.query.period === "string" ? route.query.period : null;
}

async function loadFunds(periodKey: string) {
  activeRequest?.abort();
  const controller = new AbortController();
  activeRequest = controller;
  loading.value = true;
  requestError.value = "";
  try {
    const response = await fetchFundsDashboard(periodKey, controller.signal);
    if (response.schema_version !== 1) {
      throw new Error("FUNDS_SCHEMA_MISMATCH");
    }
    funds.value = response.data;
    selectedPeriodLabel.value = response.selected_period?.label ?? "";
    selectedAccount.value = "";
  } catch (error: unknown) {
    if (controller.signal.aborted) return;
    requestError.value = dashboardErrorMessage(error);
  } finally {
    if (activeRequest === controller) {
      activeRequest = null;
      loading.value = false;
    }
  }
}

function changePeriod(value: string) {
  void router.push({ query: { ...route.query, period: value || undefined } });
}

function refresh() {
  if (selectedPeriod.value) void loadFunds(selectedPeriod.value);
}

async function retry() {
  if (context.value) {
    refresh();
    return;
  }
  initializing.value = true;
  try {
    await loadContext(true);
  } catch {
    // The shared context exposes the user-safe error message.
  } finally {
    initializing.value = false;
  }
}

function formatDate(value: string): string {
  const parts = value.split("-");
  if (parts.length !== 3) return value;
  return `${Number(parts[1])} 月 ${Number(parts[2])} 日`;
}

function formatSigned(value: string): string {
  const amount = fen(value);
  if (amount > 0n) return `+${formatFen(amount)}`;
  if (amount < 0n) return `−${formatPositiveFen(amount)}`;
  return formatFen(0);
}

function movementAmount(direction: "inflow" | "outflow", value: string) {
  return `${direction === "inflow" ? "+" : "−"}${formatPositiveFen(value)}`;
}

function bankStateLabel(state: BankStatementState): string {
  return {
    matched: "已匹配",
    unmatched: "待匹配",
    invalid_match: "匹配已失效",
    pending_late: "迟到流水待处理",
    handled_late: "迟到流水已处理",
  }[state];
}

function reconciliationAttention(state: string): boolean {
  return ["attention", "pending", "not_configured"].includes(state);
}

watch(
  () => [context.value, route.query.period] as const,
  ([dashboardContext]) => {
    if (!dashboardContext) return;
    const requested = routePeriod();
    const requestedExists = dashboardContext.periods.some(
      (item) => item.key === requested,
    );
    const target = requestedExists ? requested : dashboardContext.default_period;
    if (!target) {
      activeRequest?.abort();
      selectedPeriod.value = "";
      selectedPeriodLabel.value = "";
      funds.value = null;
      return;
    }
    if (requested !== target) {
      void router.replace({ query: { ...route.query, period: target } });
      return;
    }
    if (selectedPeriod.value !== target) {
      selectedPeriod.value = target;
      void loadFunds(target);
    }
  },
  { immediate: true },
);

onMounted(async () => {
  try {
    await loadContext();
  } catch {
    // The shared context exposes the user-safe error message.
  } finally {
    initializing.value = false;
  }
});

onBeforeUnmount(() => activeRequest?.abort());
</script>

<template>
  <div class="funds-page">
    <div class="page-content">
      <DashboardModuleHeader
        eyebrow="资金"
        title="资金总览"
        description="查看银行与现金账户的账面余额、当月收支、逐账户状态和资金明细。页面只读，不提供账户管理或付款操作。"
        :options="periods"
        :selected="selectedPeriod"
        :loading="loading || contextLoading"
        select-label="资金查看月份"
        @change="changePeriod"
        @refresh="refresh"
      />

      <section v-if="pageError" class="state-panel error-state" role="alert">
        <strong>资金数据暂时无法读取</strong>
        <p>{{ pageError }}</p>
        <button type="button" @click="retry">重新加载</button>
      </section>

      <section
        v-if="(initializing || loading) && !funds"
        class="state-panel loading-state"
        aria-live="polite"
      >
        <strong>正在读取资金数据…</strong>
        <span>正在核对所选月份的正式凭证与银行流水。</span>
      </section>

      <section v-else-if="!periods.length" class="state-panel">
        <strong>还没有可查看的资金月份</strong>
        <span>生成首个会计期间并完成资金入账后，这里会出现只读资金信息。</span>
      </section>

      <section v-else-if="!funds && !pageError" class="state-panel">
        <strong>所选月份暂无资金数据</strong>
        <span>可以选择其他月份或重新加载。</span>
      </section>

      <div v-else-if="funds" class="funds-dashboard" :aria-busy="loading">
        <section class="funds-hero" aria-labelledby="funds-total-label">
          <div>
            <p class="eyebrow">{{ selectedPeriodLabel }}期末 · 正式账簿口径</p>
            <span id="funds-total-label">账面资金合计</span>
            <strong class="funds-total">{{ formatFen(funds.total_fen) }}</strong>
            <p class="muted">
              <template v-if="funds.account_count">
                银行 {{ formatFen(funds.bank_fen) }} · 现金
                {{ formatFen(funds.cash_fen) }} · 共 {{ funds.account_count }} 个资金账户
              </template>
              <template v-else>本月尚无银行或现金账户余额及资金活动</template>
            </p>
          </div>
          <div class="funds-change">
            <span>本月账面资金净变动</span>
            <strong :class="{ loss: fen(funds.net_change_fen) < 0n }">
              {{ formatSigned(funds.net_change_fen) }}
            </strong>
            <small>
              月初 {{ formatFen(funds.opening_fen) }} ·
              <template v-if="fen(funds.internal_transfer_fen)">
                账户互转 {{ formatFen(funds.internal_transfer_fen) }}
              </template>
              <template v-else>本月无账户互转</template>
            </small>
          </div>
        </section>

        <section class="kpi-grid" aria-label="资金核心指标">
          <article>
            <span>银行账户余额</span>
            <strong>{{ formatFen(funds.bank_fen) }}</strong>
            <small>{{ funds.bank_account_count ? `${funds.bank_account_count} 个银行账户` : "本月无银行账户资金" }}</small>
          </article>
          <article>
            <span>现金账户余额</span>
            <strong>{{ formatFen(funds.cash_fen) }}</strong>
            <small>{{ funds.cash_account_count ? `${funds.cash_account_count} 个现金账户` : "本月无现金账户资金" }}</small>
          </article>
          <article>
            <span>本月对外流入</span>
            <strong>{{ formatFen(funds.inflow_fen) }}</strong>
            <small>按已入账资金分录，账户互转已剔除</small>
          </article>
          <article>
            <span>本月对外流出</span>
            <strong>{{ formatFen(funds.outflow_fen) }}</strong>
            <small>按已入账资金分录，账户互转已剔除</small>
          </article>
        </section>

        <section class="panel section-panel" aria-labelledby="fund-accounts-title">
          <div class="section-heading">
            <div>
              <p class="eyebrow">逐账户查看</p>
              <h2 id="fund-accounts-title">银行与现金账户</h2>
            </div>
            <strong>{{ funds.account_count }} 个账户 · 期末 {{ formatFen(funds.total_fen) }}</strong>
          </div>
          <div v-if="funds.accounts.length" class="account-grid">
            <article
              v-for="account in funds.accounts"
              :key="account.code"
              class="account-card"
              :class="{
                attention:
                  account.negative_balance ||
                  reconciliationAttention(account.reconciliation.state),
              }"
            >
              <div class="account-head">
                <div>
                  <span>{{ account.type === "bank" ? "银行账户" : "现金账户" }} · {{ account.code }}</span>
                  <h3>{{ account.name }}</h3>
                </div>
                <div class="account-balance">
                  <span>期末账面余额</span>
                  <strong :class="{ loss: account.negative_balance }">
                    {{ formatFen(account.closing_fen) }}
                  </strong>
                </div>
              </div>
              <dl class="account-metrics">
                <div><dt>月初</dt><dd>{{ formatFen(account.opening_fen) }}</dd></div>
                <div><dt>本月流入</dt><dd>{{ formatFen(account.inflow_fen) }}</dd></div>
                <div><dt>本月流出</dt><dd>{{ formatFen(account.outflow_fen) }}</dd></div>
              </dl>
              <p class="account-status" :class="account.reconciliation.state">
                <template v-if="account.negative_balance">余额为负 · </template>
                {{ account.reconciliation.label }}
                <template v-if="account.type === 'bank' && account.statement.transaction_count">
                  · 流水 {{ account.statement.transaction_count }} 笔
                </template>
              </p>
            </article>
          </div>
          <p v-else class="empty">本月没有已启用或有资金事实的银行、现金账户。</p>
        </section>

        <section
          v-if="attentionItems.length"
          class="panel section-panel attention-panel"
          aria-labelledby="funds-attention-title"
        >
          <div class="section-heading">
            <div>
              <p class="eyebrow">不影响只读查看，但需要后续核对</p>
              <h2 id="funds-attention-title">资金关注事项</h2>
            </div>
            <span class="status-chip attention">{{ attentionItems.length }} 项</span>
          </div>
          <ul class="attention-list">
            <li v-for="(item, index) in attentionItems" :key="`${index}-${item}`">{{ item }}</li>
          </ul>
        </section>

        <section class="panel section-panel" aria-labelledby="fund-movements-title">
          <div class="section-heading">
            <div>
              <p class="eyebrow">来自正式凭证 · 已确认入账</p>
              <h2 id="fund-movements-title">本月资金明细</h2>
            </div>
            <strong>{{ funds.movement_count }} 条账户分录</strong>
          </div>
          <div class="table-toolbar">
            <p class="muted">账户互转会分别出现在转出与转入账户，但不计入上方对外流入、流出。</p>
            <select v-model="selectedAccount" class="control" aria-label="筛选资金账户">
              <option value="">全部账户</option>
              <option v-for="account in funds.accounts" :key="account.code" :value="account.code">
                {{ account.name }}（{{ account.code }}）
              </option>
            </select>
          </div>
          <div v-if="visibleMovements.length" class="table-wrap">
            <table>
              <thead>
                <tr><th>日期</th><th>账户</th><th>业务与摘要</th><th>往来对象</th><th>方向</th><th class="number">金额</th><th>凭证</th></tr>
              </thead>
              <tbody>
                <tr v-for="(item, index) in visibleMovements" :key="`${index}-${item.reference}-${item.account_code}`">
                  <td>{{ formatDate(item.date) }}</td>
                  <td>{{ item.account_name }}（{{ item.account_code }}）</td>
                  <td>{{ item.type }} · {{ item.summary }}<template v-if="item.internal_transfer"> · 账户互转</template></td>
                  <td>{{ item.party }}</td>
                  <td><span class="direction" :class="item.direction">{{ item.direction === "inflow" ? "流入" : "流出" }}</span></td>
                  <td class="number">{{ movementAmount(item.direction, item.amount_fen) }}</td>
                  <td>{{ item.reference }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-else class="empty">{{ selectedAccount ? "本月该账户没有已入账资金变动。" : "本月没有已入账资金变动。" }}</p>
        </section>

        <section class="panel section-panel" aria-labelledby="bank-statement-title">
          <div class="section-heading">
            <div>
              <p class="eyebrow">来自已导入银行流水 · 与账面分开展示</p>
              <h2 id="bank-statement-title">银行流水明细</h2>
            </div>
            <span class="status-chip" :class="{ attention: bankAttentionCount }">
              <template v-if="!funds.bank_statement.transaction_count">本月无银行流水</template>
              <template v-else-if="bankAttentionCount">{{ bankAttentionCount }} 笔待处理</template>
              <template v-else>流水均已处理</template>
            </span>
          </div>
          <p class="muted">
            <template v-if="funds.bank_statement.transaction_count">
              共 {{ funds.bank_statement.transaction_count }} 笔 · 流入
              {{ formatFen(funds.bank_statement.inflow_fen) }} · 流出
              {{ formatFen(funds.bank_statement.outflow_fen) }} · 普通流水匹配
              {{ funds.bank_statement.matched_count }}/{{ funds.bank_statement.ordinary_count }} 笔。
            </template>
            <template v-else>本月没有已导入的银行流水；现金变动仍以正式凭证资金明细为准。</template>
          </p>
          <div v-if="funds.bank_statement.rows.length" class="table-wrap">
            <table>
              <thead>
                <tr><th>日期</th><th>银行账户</th><th>对方与摘要</th><th>方向</th><th class="number">金额</th><th>处理状态</th></tr>
              </thead>
              <tbody>
                <tr
                  v-for="(item, index) in funds.bank_statement.rows"
                  :key="`${index}-${item.date}-${item.account_code}`"
                  :class="{ 'attention-row': ['unmatched', 'invalid_match', 'pending_late'].includes(item.state) }"
                >
                  <td>{{ formatDate(item.date) }}</td>
                  <td>{{ item.account_name }}（{{ item.account_code }}）</td>
                  <td>{{ item.party }} · {{ item.memo }}</td>
                  <td><span class="direction" :class="item.direction">{{ item.direction === "inflow" ? "流入" : "流出" }}</span></td>
                  <td class="number">{{ movementAmount(item.direction, item.amount_fen) }}</td>
                  <td>{{ bankStateLabel(item.state) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-else class="empty">本月没有可展示的银行流水。</p>
        </section>
      </div>
    </div>
  </div>
</template>

<style scoped>
.funds-page { height: 100%; overflow-y: auto; }
.page-content { width: min(calc(100% - 48px), 1240px); margin: 0 auto; padding: 42px 0 64px; }
.funds-dashboard { display: grid; gap: 22px; opacity: 1; transition: opacity 160ms ease; }
.funds-dashboard[aria-busy="true"] { opacity: 0.72; }
.panel, .state-panel, .kpi-grid article { border: 1px solid var(--line); border-radius: 15px; background: var(--surface); box-shadow: var(--shadow-soft); }
.state-panel { display: grid; gap: 7px; padding: 24px; }
.state-panel p, .state-panel span { margin: 0; color: var(--muted); }
.state-panel button { width: max-content; margin-top: 8px; padding: 8px 13px; cursor: pointer; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); color: var(--text); }
.error-state { margin-bottom: 18px; border-color: color-mix(in srgb, #b04a3a 42%, var(--line)); }
.funds-hero { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(260px, 0.8fr); gap: 30px; padding: 30px; border-radius: 18px; background: linear-gradient(135deg, var(--accent), color-mix(in srgb, var(--accent) 76%, #173c29)); color: #fff; box-shadow: var(--shadow-soft); }
.eyebrow { margin: 0 0 6px; color: var(--accent); font-size: 12px; font-weight: 750; letter-spacing: 0.08em; }
.funds-hero .eyebrow, .funds-hero .muted { color: rgb(255 255 255 / 76%); }
.funds-total { display: block; margin-top: 7px; font-size: clamp(32px, 5vw, 52px); line-height: 1.1; }
.muted { color: var(--muted); }
.funds-change { align-self: center; padding: 18px 20px; border: 1px solid rgb(255 255 255 / 22%); border-radius: 13px; background: rgb(255 255 255 / 10%); }
.funds-change span, .funds-change small { display: block; color: rgb(255 255 255 / 76%); }
.funds-change strong { display: block; margin: 8px 0; font-size: 28px; }
.loss { color: #d75543; }
.funds-hero .loss { color: #ffe09a; }
.kpi-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 13px; }
.kpi-grid article { display: grid; gap: 8px; min-width: 0; padding: 18px; }
.kpi-grid span, .kpi-grid small { color: var(--muted); }
.kpi-grid strong { font-size: 22px; }
.section-panel { padding: 24px; }
.section-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; margin-bottom: 18px; }
.section-heading h2 { margin: 0; font-size: 21px; }
.section-heading > strong { color: var(--muted); font-size: 14px; }
.account-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.account-card { padding: 18px; border: 1px solid var(--line); border-radius: 12px; background: color-mix(in srgb, var(--surface) 94%, var(--background)); }
.account-card.attention { border-color: color-mix(in srgb, #bc8130 45%, var(--line)); }
.account-head { display: flex; justify-content: space-between; gap: 16px; }
.account-head span, .account-balance span { color: var(--muted); font-size: 12px; }
.account-head h3 { margin: 4px 0 0; font-size: 17px; }
.account-balance { text-align: right; }
.account-balance strong { display: block; margin-top: 4px; font-size: 19px; }
.account-metrics { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 16px 0; }
.account-metrics div { padding: 9px; border-radius: 8px; background: var(--background); }
.account-metrics dt { color: var(--muted); font-size: 11px; }
.account-metrics dd { margin: 4px 0 0; font-size: 13px; font-weight: 700; }
.account-status { margin: 0; padding-top: 12px; border-top: 1px solid var(--line); color: var(--muted); font-size: 13px; }
.account-status.attention, .account-status.pending, .account-status.not_configured { color: #9b681e; }
.attention-panel { border-color: color-mix(in srgb, #bc8130 42%, var(--line)); background: color-mix(in srgb, #fff5dc 28%, var(--surface)); }
.status-chip { display: inline-flex; min-height: 30px; align-items: center; padding: 0 10px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); font-size: 12px; font-weight: 700; }
.status-chip.attention { background: color-mix(in srgb, #efb95c 23%, var(--surface)); color: #8d5d14; }
.attention-list { display: grid; gap: 8px; margin: 0; padding-left: 21px; }
.table-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 18px; margin-bottom: 14px; }
.table-toolbar p { margin: 0; }
.control { min-height: 38px; padding: 0 11px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); color: var(--text); }
.table-wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 10px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 11px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
th { background: var(--background); color: var(--muted); font-size: 12px; white-space: nowrap; }
tbody tr:last-child td { border-bottom: 0; }
.number { text-align: right; white-space: nowrap; }
.direction { display: inline-flex; padding: 2px 7px; border-radius: 999px; font-size: 12px; white-space: nowrap; }
.direction.inflow { background: var(--accent-soft); color: var(--accent); }
.direction.outflow { background: color-mix(in srgb, #c96b55 14%, var(--surface)); color: #a24e3a; }
.attention-row { background: color-mix(in srgb, #efb95c 9%, var(--surface)); }
.empty { margin: 0; padding: 20px; border-radius: 9px; background: var(--background); color: var(--muted); text-align: center; }

@media (max-width: 980px) {
  .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .account-grid { grid-template-columns: 1fr; }
}

@media (max-width: 700px) {
  .page-content { width: min(calc(100% - 28px), 1240px); padding-top: 28px; }
  .funds-hero, .kpi-grid { grid-template-columns: 1fr; }
  .funds-hero { padding: 22px; }
  .section-panel { padding: 18px; }
  .section-heading, .table-toolbar, .account-head { align-items: stretch; flex-direction: column; }
  .account-balance { text-align: left; }
  .account-metrics { grid-template-columns: 1fr; }
  .table-toolbar .control { width: 100%; }
}

@media (prefers-reduced-motion: reduce) {
  .funds-dashboard { transition: none; }
}
</style>
