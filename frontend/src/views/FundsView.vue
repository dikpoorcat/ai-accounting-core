<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { dashboardErrorMessage } from "../api/client";
import {
  fetchFundsDashboard,
  type BankStatementState,
  type FundsData,
} from "../api/funds";
import DashboardModuleHeader from "../components/DashboardModuleHeader.vue";
import DashboardSectionNav from "../components/DashboardSectionNav.vue";
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
const selectedBankAccount = ref("");
const selectedDetailView = ref<"book" | "bank">("book");
const funds = ref<FundsData | null>(null);
const selectedPeriodLabel = ref("");
const loading = ref(false);
const initializing = ref(true);
const requestError = ref("");
const activeSection = ref("funds-overview");
let activeRequest: AbortController | null = null;
let sectionSyncLocked = false;

const periods = computed(() => context.value?.periods ?? []);
const pageError = computed(() => requestError.value || contextError.value);
const detailViews = ["book", "bank"] as const;
const visibleMovements = computed(() => {
  if (!funds.value) return [];
  if (!selectedAccount.value) return funds.value.movements;
  return funds.value.movements.filter(
    (item) => item.account_code === selectedAccount.value,
  );
});
const bankAccounts = computed(
  () => funds.value?.accounts.filter((account) => account.type === "bank") ?? [],
);
const visibleBankRows = computed(() => {
  const rows = funds.value?.bank_statement.rows ?? [];
  if (!selectedBankAccount.value) return rows;
  return rows.filter((item) => item.account_code === selectedBankAccount.value);
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
const sectionLinks = computed(() => {
  const links = [
    { id: "funds-overview", label: "概览" },
    { id: "fund-accounts", label: "账户" },
  ];
  if (attentionItems.value.length) links.push({ id: "funds-attention", label: "关注" });
  links.push({ id: "bank-details", label: "资金明细" });
  return links;
});

function routePeriod(): string | null {
  return typeof route.query.period === "string" ? route.query.period : null;
}

function lockSectionSync() {
  sectionSyncLocked = true;
}

function enableSectionSyncForUserScroll() {
  sectionSyncLocked = false;
}

function handleSectionScrollKey(event: KeyboardEvent) {
  if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(event.key)) {
    enableSectionSyncForUserScroll();
  }
}

function handleScrollbarPointer(event: PointerEvent) {
  if (event.clientX >= document.documentElement.clientWidth) {
    enableSectionSyncForUserScroll();
  }
}

function positionSection(section: HTMLElement) {
  const root = document.documentElement;
  const previousBehavior = root.style.scrollBehavior;
  root.style.scrollBehavior = "auto";
  window.scrollTo({
    top: Math.max(0, window.scrollY + section.getBoundingClientRect().top - 66),
    behavior: "auto",
  });
  root.style.scrollBehavior = previousBehavior;
}

async function revealBankDetails() {
  if (route.hash !== "#bank-details" || !funds.value) return;
  lockSectionSync();
  selectedDetailView.value = "bank";
  activeSection.value = "bank-details";
  await nextTick();
  const section = document.getElementById("bank-details");
  if (section) positionSection(section);
  document.getElementById("fund-detail-tab-bank")?.focus({ preventScroll: true });
}

function focusSection(id: string) {
  const section = document.getElementById(id);
  if (!section) return;
  lockSectionSync();
  activeSection.value = id;
  positionSection(section);
  section.focus({ preventScroll: true });
}

function updateSectionFromScroll() {
  if (sectionSyncLocked) return;
  const links = sectionLinks.value;
  if (!links.length) return;
  const probeTop = 80;
  let candidate = links[0].id;
  for (const link of links) {
    const section = document.getElementById(link.id);
    if (!section || section.getBoundingClientRect().top > probeTop) break;
    candidate = link.id;
  }
  if (candidate === activeSection.value) return;
  const currentIndex = links.findIndex((link) => link.id === activeSection.value);
  const candidateIndex = links.findIndex((link) => link.id === candidate);
  if (candidateIndex < currentIndex) {
    const currentSection = document.getElementById(activeSection.value);
    if (currentSection && currentSection.getBoundingClientRect().top <= probeTop + 24) return;
  }
  activeSection.value = candidate;
}

async function loadFunds(periodKey: string) {
  activeRequest?.abort();
  const controller = new AbortController();
  lockSectionSync();
  const sectionToRestore = activeSection.value;
  const shouldRestoreSection = funds.value !== null;
  activeRequest = controller;
  loading.value = true;
  requestError.value = "";
  try {
    const response = await fetchFundsDashboard(periodKey, controller.signal);
    if (response.schema_version !== 1) {
      throw new Error("FUNDS_SCHEMA_MISMATCH");
    }
    const data = response.data;
    funds.value = data;
    selectedPeriodLabel.value = response.selected_period?.label ?? "";
    if (!data?.accounts.some((account) => account.code === selectedAccount.value)) {
      selectedAccount.value = "";
    }
    if (
      !data?.accounts.some(
        (account) => account.type === "bank" && account.code === selectedBankAccount.value,
      )
    ) {
      selectedBankAccount.value = "";
    }
    await nextTick();
    const targetSection = sectionLinks.value.some((link) => link.id === sectionToRestore)
      ? sectionToRestore
      : sectionToRestore === "funds-attention"
        ? "bank-details"
        : "funds-overview";
    activeSection.value = targetSection;
    if (route.hash === "#bank-details") {
      await revealBankDetails();
    } else if (shouldRestoreSection && targetSection !== "funds-overview") {
      const section = document.getElementById(targetSection);
      if (section) positionSection(section);
    }
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

function selectDetailView(view: "book" | "bank") {
  selectedDetailView.value = view;
}

function handleDetailTabKey(event: KeyboardEvent, index: number) {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  let nextIndex = index;
  if (event.key === "Home") nextIndex = 0;
  if (event.key === "End") nextIndex = detailViews.length - 1;
  if (event.key === "ArrowLeft") {
    nextIndex = (index - 1 + detailViews.length) % detailViews.length;
  }
  if (event.key === "ArrowRight") nextIndex = (index + 1) % detailViews.length;
  const nextView = detailViews[nextIndex];
  selectedDetailView.value = nextView;
  void nextTick(() => document.getElementById(`fund-detail-tab-${nextView}`)?.focus());
}

function reconciliationAttention(state: string): boolean {
  return ["attention", "pending", "not_configured"].includes(state);
}

watch(
  () => route.query.org_id,
  (value, previous) => {
    if (value === previous) return;
    activeRequest?.abort();
    funds.value = null;
    selectedPeriod.value = "";
    selectedPeriodLabel.value = "";
    selectedAccount.value = "";
    selectedBankAccount.value = "";
    activeSection.value = "funds-overview";
    loading.value = false;
  },
);

watch(
  () => route.hash,
  () => void revealBankDetails(),
  { immediate: true },
);

watch(
  () => [context.value, route.query.period] as const,
  ([dashboardContext], previous) => {
    if (!dashboardContext) return;
    const previousContext = previous?.[0];
    const companyChanged =
      dashboardContext.current_company.org_id !== previousContext?.current_company.org_id;
    const requested = routePeriod();
    const requestedExists = dashboardContext.periods.some(
      (item) => item.key === requested,
    );
    const target = requestedExists ? requested : dashboardContext.default_period;
    if (!target) {
      activeRequest?.abort();
      selectedPeriod.value = "";
      selectedPeriodLabel.value = "";
      selectedAccount.value = "";
      selectedBankAccount.value = "";
      activeSection.value = "funds-overview";
      funds.value = null;
      return;
    }
    if (requested !== target) {
      void router.replace({ query: { ...route.query, period: target } });
      return;
    }
    if (companyChanged || selectedPeriod.value !== target) {
      selectedPeriod.value = target;
      void loadFunds(target);
    }
  },
  { immediate: true },
);

onMounted(async () => {
  window.addEventListener("scroll", updateSectionFromScroll, { passive: true });
  window.addEventListener("wheel", enableSectionSyncForUserScroll, { passive: true });
  window.addEventListener("touchmove", enableSectionSyncForUserScroll, { passive: true });
  window.addEventListener("keydown", handleSectionScrollKey);
  window.addEventListener("pointerdown", handleScrollbarPointer);
  try {
    await loadContext();
  } catch {
    // The shared context exposes the user-safe error message.
  } finally {
    initializing.value = false;
  }
});

onBeforeUnmount(() => {
  activeRequest?.abort();
  window.removeEventListener("scroll", updateSectionFromScroll);
  window.removeEventListener("wheel", enableSectionSyncForUserScroll);
  window.removeEventListener("touchmove", enableSectionSyncForUserScroll);
  window.removeEventListener("keydown", handleSectionScrollKey);
  window.removeEventListener("pointerdown", handleScrollbarPointer);
});
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

      <template v-else-if="funds">
        <DashboardSectionNav
          :items="sectionLinks"
          :active="activeSection"
          label="资金页面区段"
          floating
          @select="focusSection"
        />

        <div class="funds-dashboard" :aria-busy="loading">
        <section
          id="funds-overview"
          class="funds-hero section-anchor"
          aria-labelledby="funds-total-label"
          tabindex="-1"
        >
          <div>
            <p class="eyebrow">{{ selectedPeriodLabel }}期末 · 正式账簿口径</p>
            <span id="funds-total-label">账面资金合计</span>
            <strong class="funds-total">{{ formatFen(funds.total_fen) }}</strong>
            <p class="muted">
              <template v-if="funds.account_count">
                银行 {{ formatFen(funds.bank_fen) }} · 支付平台
                {{ formatFen(funds.payment_platform_fen) }} · 现金
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

        <section
          id="fund-accounts"
          class="panel section-panel section-anchor"
          aria-labelledby="fund-accounts-title"
          tabindex="-1"
        >
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
                  <span>{{ account.type === "bank" ? "银行账户" : account.type === "payment_platform" ? "支付平台" : "现金账户" }} · {{ account.code }}</span>
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
          id="funds-attention"
          class="panel section-panel attention-panel section-anchor"
          aria-labelledby="funds-attention-title"
          tabindex="-1"
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

        <div class="final-section-space">
        <section
          id="bank-details"
          class="panel section-panel section-anchor"
          aria-labelledby="fund-details-title"
          tabindex="-1"
        >
          <div class="section-heading detail-heading">
            <div>
              <p class="eyebrow">账面与银行数据 · 分口径查看</p>
              <h2 id="fund-details-title">资金明细</h2>
            </div>
            <div class="detail-switch" role="tablist" aria-label="选择资金明细口径">
              <button
                id="fund-detail-tab-book"
                type="button"
                role="tab"
                :aria-selected="selectedDetailView === 'book'"
                aria-controls="fund-detail-panel-book"
                :tabindex="selectedDetailView === 'book' ? 0 : -1"
                @click="selectDetailView('book')"
                @keydown="handleDetailTabKey($event, 0)"
              >
                账面明细
              </button>
              <button
                id="fund-detail-tab-bank"
                type="button"
                role="tab"
                :aria-selected="selectedDetailView === 'bank'"
                aria-controls="fund-detail-panel-bank"
                :aria-label="bankAttentionCount ? `银行流水，${bankAttentionCount} 笔待处理` : '银行流水'"
                :tabindex="selectedDetailView === 'bank' ? 0 : -1"
                @click="selectDetailView('bank')"
                @keydown="handleDetailTabKey($event, 1)"
              >
                银行流水
                <span v-if="bankAttentionCount" class="detail-tab-alert" aria-hidden="true">
                  {{ bankAttentionCount }}
                </span>
              </button>
            </div>
          </div>
          <div
            v-if="selectedDetailView === 'book'"
            id="fund-detail-panel-book"
            role="tabpanel"
            aria-labelledby="fund-detail-tab-book"
            tabindex="0"
          >
            <div class="detail-toolbar">
              <div>
                <strong>{{ funds.movement_count }} 条账户分录</strong>
                <p class="muted">
                  来自正式凭证 · 已确认入账。账户互转会分别出现在转出与转入账户，但不计入上方对外流入、流出。
                </p>
              </div>
              <select v-model="selectedAccount" class="control" aria-label="筛选账面资金账户">
                <option value="">全部账户</option>
                <option v-for="account in funds.accounts" :key="account.code" :value="account.code">
                  {{ account.name }}（{{ account.code }}）
                </option>
              </select>
            </div>
            <div v-if="visibleMovements.length" class="table-wrap">
              <table class="book-detail-table">
                <thead>
                  <tr>
                    <th class="date-column">日期</th>
                    <th class="account-column">账户</th>
                    <th>业务与摘要</th>
                    <th class="party-column">往来对象</th>
                    <th class="direction-column">方向</th>
                    <th class="number amount-column">金额</th>
                    <th class="reference-column">凭证</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(item, index) in visibleMovements" :key="`${index}-${item.reference}-${item.account_code}`">
                    <td class="date-column">{{ formatDate(item.date) }}</td>
                    <td class="account-column">{{ item.account_name }}（{{ item.account_code }}）</td>
                    <td>{{ item.type }} · {{ item.summary }}<template v-if="item.internal_transfer"> · 账户互转</template></td>
                    <td class="party-column">{{ item.party }}</td>
                    <td class="direction-column"><span class="direction" :class="item.direction">{{ item.direction === "inflow" ? "流入" : "流出" }}</span></td>
                    <td class="number amount-column">{{ movementAmount(item.direction, item.amount_fen) }}</td>
                    <td class="reference-column">{{ item.reference }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p v-else class="empty">{{ selectedAccount ? "本月该账户没有已入账资金变动。" : "本月没有已入账资金变动。" }}</p>
          </div>

          <div
            v-else
            id="fund-detail-panel-bank"
            role="tabpanel"
            aria-labelledby="fund-detail-tab-bank"
            tabindex="0"
          >
            <div class="detail-toolbar">
              <div>
                <span class="status-chip" :class="{ attention: bankAttentionCount }">
                  <template v-if="!funds.bank_statement.transaction_count">本月无银行流水</template>
                  <template v-else-if="bankAttentionCount">{{ bankAttentionCount }} 笔待处理</template>
                  <template v-else>流水均已处理</template>
                </span>
                <p class="muted">
                  <template v-if="funds.bank_statement.transaction_count">
                    来自已导入银行流水 · 共 {{ funds.bank_statement.transaction_count }} 笔 · 流入
                    {{ formatFen(funds.bank_statement.inflow_fen) }} · 流出
                    {{ formatFen(funds.bank_statement.outflow_fen) }} · 普通流水匹配
                    {{ funds.bank_statement.matched_count }}/{{ funds.bank_statement.ordinary_count }} 笔。未匹配流水不代表已确认业务。
                  </template>
                  <template v-else>本月没有已导入的银行流水；现金变动仍以账面明细为准。</template>
                </p>
              </div>
              <select v-model="selectedBankAccount" class="control" aria-label="筛选银行流水账户">
                <option value="">全部银行账户</option>
                <option v-for="account in bankAccounts" :key="account.code" :value="account.code">
                  {{ account.name }}（{{ account.code }}）
                </option>
              </select>
            </div>
            <div v-if="visibleBankRows.length" class="table-wrap">
              <table class="bank-detail-table">
                <thead>
                  <tr>
                    <th class="date-column">日期</th>
                    <th class="account-column">银行账户</th>
                    <th>对方与摘要</th>
                    <th class="direction-column">方向</th>
                    <th class="number amount-column">金额</th>
                    <th class="state-column">处理状态</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="(item, index) in visibleBankRows"
                    :key="`${index}-${item.date}-${item.account_code}`"
                    :class="{ 'attention-row': ['unmatched', 'invalid_match', 'pending_late'].includes(item.state) }"
                  >
                    <td class="date-column">{{ formatDate(item.date) }}</td>
                    <td class="account-column">{{ item.account_name }}（{{ item.account_code }}）</td>
                    <td>{{ item.party }} · {{ item.memo }}</td>
                    <td class="direction-column"><span class="direction" :class="item.direction">{{ item.direction === "inflow" ? "流入" : "流出" }}</span></td>
                    <td class="number amount-column">{{ movementAmount(item.direction, item.amount_fen) }}</td>
                    <td class="state-column">{{ bankStateLabel(item.state) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p v-else class="empty">{{ selectedBankAccount ? "本月该账户没有可展示的银行流水。" : "本月没有可展示的银行流水。" }}</p>
          </div>
        </section>
        </div>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.funds-page {
  min-height: 100%;
}

.page-content {
  width: min(calc(100% - 48px), 1320px);
  margin: 0 auto;
  padding: 25px 0 46px;
}

.funds-dashboard {
  display: grid;
  min-width: 0;
  gap: 12px;
  opacity: 1;
  transition: opacity 160ms ease;
}

.funds-dashboard[aria-busy="true"] {
  opacity: 0.72;
}

.section-anchor {
  scroll-margin-top: 66px;
}

.final-section-space {
  min-height: calc(100vh - 80px);
}

.panel,
.state-panel,
.kpi-grid article {
  min-width: 0;
  border: 1px solid var(--line);
  background: var(--surface);
  box-shadow: var(--shadow-soft);
}

.panel,
.kpi-grid article {
  border-radius: 16px;
}

.state-panel {
  display: grid;
  gap: 7px;
  padding: 28px;
  border-radius: 18px;
}

.state-panel p,
.state-panel span {
  margin: 0;
  color: var(--muted);
}

.state-panel button {
  width: max-content;
  min-height: 40px;
  margin-top: 8px;
  padding: 0 13px;
  border: 0;
  border-radius: 10px;
  background: var(--accent);
  color: var(--surface);
  cursor: pointer;
}

.error-state {
  border-color: var(--danger);
}

.funds-hero {
  display: grid;
  grid-template-columns: minmax(0, 1.55fr) minmax(300px, 0.75fr);
  gap: 25px;
  min-height: 198px;
  padding: 23px 25px;
  border: 1px solid color-mix(in srgb, var(--accent) 20%, var(--line));
  border-radius: 20px;
  background:
    radial-gradient(circle at 7% 12%, color-mix(in srgb, var(--accent) 11%, transparent), transparent 32%),
    linear-gradient(125deg, var(--surface), color-mix(in srgb, var(--accent-soft) 66%, var(--surface)));
  box-shadow: var(--shadow-soft);
}

.eyebrow {
  margin: 0 0 5px;
  color: var(--accent);
  font-size: 11px;
  font-weight: 850;
  letter-spacing: 0.08em;
}

.funds-hero > div:first-child > span {
  color: var(--muted);
  font-size: 12px;
}

.funds-total {
  display: block;
  margin: 7px 0 3px;
  color: var(--info);
  font-size: clamp(31px, 4vw, 42px);
  line-height: 1.1;
  letter-spacing: -0.035em;
}

.muted {
  color: var(--muted);
}

.funds-hero .muted {
  margin: 8px 0 0;
  font-size: 12px;
}

.funds-change {
  display: grid;
  align-content: center;
  align-self: stretch;
  padding: 14px;
  border: 1px solid color-mix(in srgb, var(--line) 82%, transparent);
  border-radius: 15px;
  background: color-mix(in srgb, var(--surface) 83%, transparent);
}

.funds-change span,
.funds-change small {
  display: block;
  color: var(--muted);
  font-size: 11px;
}

.funds-change strong {
  display: block;
  margin: 8px 0;
  color: var(--accent);
  font-size: 27px;
}

.loss,
.funds-change strong.loss {
  color: var(--danger);
}

.kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}

.kpi-grid article {
  position: relative;
  display: grid;
  min-width: 0;
  min-height: 126px;
  align-content: space-between;
  gap: 4px;
  overflow: hidden;
  padding: 15px 16px;
}

.kpi-grid article::before {
  position: absolute;
  top: 0;
  right: 0;
  left: 0;
  height: 3px;
  background: var(--accent);
  content: "";
}

.kpi-grid article:nth-child(1)::before {
  background: var(--info);
}

.kpi-grid article:nth-child(1) strong {
  color: var(--info);
}

.kpi-grid article:nth-child(2)::before {
  background: var(--gold);
}

.kpi-grid article:nth-child(2) strong {
  color: var(--gold);
}

.kpi-grid article:nth-child(4)::before {
  background: var(--danger);
}

.kpi-grid article:nth-child(4) strong {
  color: var(--danger);
}

.kpi-grid span,
.kpi-grid small {
  color: var(--muted);
  font-size: 11px;
}

.kpi-grid span {
  font-size: 12px;
  font-weight: 750;
}

.kpi-grid strong {
  font-size: clamp(20px, 2vw, 27px);
  line-height: 1.15;
  letter-spacing: -0.025em;
}

.section-panel {
  padding: 18px;
}

.section-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 14px;
}

.section-heading h2 {
  margin: 0;
  font-size: 20px;
}

.section-heading > strong {
  color: var(--muted);
  font-size: 12px;
}

.detail-heading {
  align-items: center;
  margin-bottom: 16px;
}

.detail-switch {
  display: inline-grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 4px;
  padding: 5px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--surface-soft);
}

.detail-switch button {
  display: inline-flex;
  min-width: 104px;
  min-height: 40px;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 7px 13px;
  border: 0;
  border-radius: 10px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  font: inherit;
  font-size: 13px;
  white-space: nowrap;
}

.detail-switch button[aria-selected="true"] {
  background: var(--surface);
  box-shadow: var(--shadow-soft);
  color: var(--text);
  font-weight: 800;
}

.detail-switch button:focus-visible,
[role="tabpanel"]:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.detail-tab-alert {
  display: inline-grid;
  min-width: 19px;
  min-height: 19px;
  place-items: center;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--warning-soft);
  color: var(--warning);
  font-size: 10px;
  font-weight: 850;
  line-height: 1;
}

.account-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.account-card {
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface-soft);
}

.account-card.attention {
  border-color: var(--warning);
}

.account-head {
  display: flex;
  justify-content: space-between;
  gap: 16px;
}

.account-head span,
.account-balance span {
  color: var(--muted);
  font-size: 11px;
}

.account-head h3 {
  margin: 4px 0 0;
  font-size: 16px;
}

.account-balance {
  text-align: right;
}

.account-balance strong {
  display: block;
  margin-top: 4px;
  font-size: 19px;
}

.account-metrics {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 1px;
  overflow: hidden;
  margin: 14px 0;
  padding: 1px;
  border-radius: 9px;
  background: var(--line);
}

.account-metrics div {
  padding: 9px;
  background: var(--surface);
}

.account-metrics dt {
  color: var(--muted);
  font-size: 11px;
}

.account-metrics dd {
  margin: 4px 0 0;
  overflow-wrap: anywhere;
  font-size: 13px;
  font-weight: 700;
}

.account-status {
  margin: 0;
  padding-top: 11px;
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: 12px;
}

.account-status.attention,
.account-status.pending,
.account-status.not_configured {
  color: var(--warning);
}

.attention-panel {
  border-color: color-mix(in srgb, var(--warning) 52%, var(--line));
}

.status-chip {
  display: inline-flex;
  min-height: 25px;
  align-items: center;
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 11px;
  font-weight: 800;
}

.status-chip.attention {
  background: var(--warning-soft);
  color: var(--warning);
}

.attention-list {
  display: grid;
  gap: 8px;
  margin: 0;
  padding-left: 21px;
}

.detail-toolbar {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 12px;
}

.detail-toolbar > div {
  min-width: 0;
}

.detail-toolbar strong {
  font-size: 12px;
}

.detail-toolbar p {
  margin: 5px 0 0;
  font-size: 12px;
}

.detail-toolbar .control {
  flex: 0 0 auto;
  max-width: min(100%, 320px);
}

.control {
  min-height: 38px;
  padding: 0 11px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--surface);
  color: var(--text);
}

.table-wrap {
  min-width: 0;
  max-width: 100%;
  overflow-x: auto;
  border: 1px solid var(--line);
  border-radius: 10px;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.book-detail-table {
  min-width: 1100px;
}

.bank-detail-table {
  min-width: 980px;
}

.date-column {
  width: 90px;
  white-space: nowrap;
}

.account-column {
  width: 210px;
}

.party-column {
  width: 150px;
}

.direction-column {
  width: 64px;
  white-space: nowrap;
}

.amount-column {
  width: 112px;
}

.reference-column {
  width: 100px;
}

.state-column {
  width: 86px;
  white-space: nowrap;
}

th,
td {
  padding: 10px 11px;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
}

th {
  background: var(--surface-soft);
  color: var(--muted);
  font-size: 11px;
  white-space: nowrap;
}

tbody tr:last-child td {
  border-bottom: 0;
}

.number {
  text-align: right;
  white-space: nowrap;
}

.direction {
  display: inline-flex;
  padding: 2px 7px;
  border-radius: 999px;
  font-size: 11px;
  white-space: nowrap;
}

.direction.inflow {
  background: var(--accent-soft);
  color: var(--accent);
}

.direction.outflow {
  background: var(--danger-soft);
  color: var(--danger);
}

.attention-row {
  background: color-mix(in srgb, var(--warning-soft) 46%, var(--surface));
}

.empty {
  margin: 0;
  padding: 20px;
  border-radius: 10px;
  background: var(--surface-soft);
  color: var(--muted);
  text-align: center;
}

@media (max-width: 980px) {
  .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .account-grid { grid-template-columns: 1fr; }
}

@media (max-width: 760px) {
  .page-content {
    width: min(calc(100% - 24px), 1320px);
    padding: 16px 0 24px;
  }

  .funds-hero,
  .kpi-grid {
    grid-template-columns: 1fr;
  }

  .funds-hero {
    gap: 13px;
    padding: 19px;
    border-radius: 17px;
  }

  .section-heading,
  .detail-toolbar,
  .account-head {
    align-items: stretch;
    flex-direction: column;
  }

  .detail-switch {
    width: 100%;
  }

  .detail-switch button {
    min-width: 0;
  }

  .account-balance {
    text-align: left;
  }

  .account-metrics {
    grid-template-columns: 1fr;
  }

  .detail-toolbar .control {
    width: 100%;
    max-width: none;
    min-height: 44px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .funds-dashboard { transition: none; }
}
</style>
