<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { fetchBrief, type BriefData } from "../api/brief";
import { dashboardErrorMessage } from "../api/client";
import DashboardModuleHeader from "../components/DashboardModuleHeader.vue";
import DashboardSectionNav from "../components/DashboardSectionNav.vue";
import BriefActivityWorkbench from "../components/brief/BriefActivityWorkbench.vue";
import BriefFinancialOverview from "../components/brief/BriefFinancialOverview.vue";
import BriefOpenItems from "../components/brief/BriefOpenItems.vue";
import BriefWorkforceSection from "../components/brief/BriefWorkforceSection.vue";
import { useDashboardContext } from "../composables/useDashboardContext";
import { fen, formatFen, formatPositiveFen } from "../utils/money";

type PriorityAction = "bank-details";
type PriorityItem = {
  title: string;
  note: string;
  state: "error" | "attention" | "neutral";
  action?: PriorityAction;
};

const route = useRoute();
const router = useRouter();
const { context, load: loadContext } = useDashboardContext();
const response = ref<Awaited<ReturnType<typeof fetchBrief>> | null>(null);
const loading = ref(false);
const error = ref("");
const activeSection = ref("overview");
let controller: AbortController | null = null;
let sectionSyncLocked = false;
let initialized = false;

const selectedPeriod = computed(() => response.value?.selected_period?.key || "");
const periodOptions = computed(() => context.value?.periods || []);
const data = computed<BriefData | null>(() => response.value?.data || null);
const isClosed = computed(() => response.value?.selected_period?.status === "closed");
const currentOutstanding = computed(
  () => data.value?.open_items.current_outstanding || data.value?.open_items,
);
const sectionLinks = computed(() => {
  const links = [
    { id: "overview", label: "概览" },
    { id: "activity", label: "业务凭证" },
  ];
  if (data.value?.workforce_cost.has_activity) links.push({ id: "workforce", label: "用工" });
  links.push(
    { id: "finance", label: "财务位置" },
    { id: "open-items", label: "往来" },
    { id: "validation", label: "校验" },
  );
  return links;
});
const priorities = computed(() => {
  if (!data.value || !response.value?.selected_period) return [];
  const items: PriorityItem[] = [];
  if (!data.value.validation.integrity_valid) {
    items.push({ title: "账务一致性异常", note: "请优先查看失败的校验项", state: "error" });
  }
  if (data.value.cash.unmatched_count) {
    items.push({
      title: `${data.value.cash.unmatched_count} 笔流水待识别`,
      note: "尚不能当作已确认业务",
      state: "attention",
      action: "bank-details",
    });
  }
  if (data.value.cash.pending_late_count) {
    items.push({
      title: `${data.value.cash.pending_late_count} 笔迟到流水待处理`,
      note: "需按迟到证据流程处理",
      state: "attention",
    });
  }
  if (response.value.selected_period.status !== "closed") {
    items.push({
      title: `${response.value.selected_period.short_label}尚未关账`,
      note: "当前期间仍可补录或更正",
      state: "attention",
    });
  }
  if (response.value.selected_period.status !== "closed" && currentOutstanding.value?.total_count) {
    items.push({
      title: "目前仍有待收待付",
      note: `待收 ${formatFen(currentOutstanding.value.receivable_fen)} · 待付 ${formatFen(currentOutstanding.value.payable_fen)}`,
      state: "neutral",
    });
  }
  return items;
});
const takeaway = computed(() => {
  if (!data.value) return "";
  if (data.value.management_commentary) return data.value.management_commentary;
  return isClosed.value ? "本月暂无经营结论。" : "本月尚未关账，经营结论将在关账时形成。";
});

function queryPeriod() {
  return typeof route.query.period === "string" ? route.query.period : null;
}

async function loadData(period: string | null) {
  controller?.abort();
  const request = new AbortController();
  controller = request;
  loading.value = true;
  error.value = "";
  try {
    response.value = await fetchBrief(period, request.signal);
  } catch (caught: unknown) {
    if (caught instanceof DOMException && caught.name === "AbortError") return;
    error.value = dashboardErrorMessage(caught);
  } finally {
    if (controller === request) loading.value = false;
  }
}

async function initialize() {
  try {
    const loadedContext = await loadContext();
    initialized = true;
    const requested = queryPeriod();
    const target = requested || loadedContext.default_period;
    if (!requested && target) {
      await router.replace({ query: { ...route.query, period: target } });
      return;
    }
    await loadData(target);
  } catch (caught: unknown) {
    error.value = dashboardErrorMessage(caught);
    loading.value = false;
  }
}

function changePeriod(value: string) {
  void router.push({ query: { ...route.query, period: value } });
}

function runPriorityAction(action: PriorityAction) {
  if (action !== "bank-details") return;
  void router.push({
    name: "funds",
    query: { ...route.query, period: selectedPeriod.value || undefined },
    hash: "#bank-details",
  });
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
    top: Math.max(0, window.scrollY + section.getBoundingClientRect().top - 78),
    behavior: "auto",
  });
  root.style.scrollBehavior = previousBehavior;
}

function focusSection(id: string) {
  const section = document.getElementById(id);
  if (!section) return;
  lockSectionSync();
  activeSection.value = id;
  positionSection(section);
  const focusTarget = section.matches("[tabindex]") ? section : section.querySelector<HTMLElement>("[tabindex]");
  focusTarget?.focus({ preventScroll: true });
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

function statusLabel(status: string) {
  return status === "closed" ? "已关账" : "未关账";
}

function generatedText() {
  if (!data.value) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(data.value.generated_at));
}

function heroNote() {
  const period = response.value?.selected_period;
  if (!period) return "";
  if (period.status !== "closed") {
    return "当前期间仍可补录或更正；已入账业务、待识别流水和期末待结事项分开展示。";
  }
  const closedAt = period.closed_at ? `${new Date(period.closed_at).toLocaleString("zh-CN")} ` : "";
  return `${closedAt}完成关账；原凭证不可修改，后续更正以冲正方式保留。`;
}

function bankContext() {
  if (!data.value) return "";
  const net = fen(data.value.cash.net_fen);
  const movement =
    net > 0n
      ? `流水净流入 ${formatFen(net)}`
      : net < 0n
        ? `流水净流出 ${formatPositiveFen(net)}`
        : "流水无净变动";
  return `${movement} · ${data.value.cash.matched_count}/${data.value.cash.ordinary_count} 已匹配`;
}

watch(
  () => route.query.org_id,
  (value, previous) => {
    if (!initialized || value === previous) return;
    controller?.abort();
    response.value = null;
    loading.value = false;
  },
);
watch(
  () => [context.value?.current_company.org_id, route.query.period] as const,
  ([orgId, period], [previousOrgId, previousPeriod]) => {
    if (!initialized || !orgId) return;
    if (orgId !== previousOrgId || period !== previousPeriod) {
      void loadData(typeof period === "string" ? period : null);
    }
  },
);
watch(data, async (value, previous) => {
  const rememberedSection = activeSection.value;
  lockSectionSync();
  activeSection.value = sectionLinks.value.some((link) => link.id === rememberedSection)
    ? rememberedSection
    : "overview";
  await nextTick();
  if (value && previous && activeSection.value !== "overview") {
    const section = document.getElementById(activeSection.value);
    if (section) positionSection(section);
  }
});
onMounted(() => {
  window.addEventListener("scroll", updateSectionFromScroll, { passive: true });
  window.addEventListener("wheel", enableSectionSyncForUserScroll, { passive: true });
  window.addEventListener("touchmove", enableSectionSyncForUserScroll, { passive: true });
  window.addEventListener("keydown", handleSectionScrollKey);
  window.addEventListener("pointerdown", handleScrollbarPointer);
  void initialize();
});
onBeforeUnmount(() => {
  controller?.abort();
  window.removeEventListener("scroll", updateSectionFromScroll);
  window.removeEventListener("wheel", enableSectionSyncForUserScroll);
  window.removeEventListener("touchmove", enableSectionSyncForUserScroll);
  window.removeEventListener("keydown", handleSectionScrollKey);
  window.removeEventListener("pointerdown", handleScrollbarPointer);
});
</script>

<template>
  <section class="brief-page">
    <DashboardModuleHeader
      eyebrow="经营简报"
      title="月度经营与财务概览"
      description="聚焦经营结果、资金动向、往来事项与账务可信度。"
      :options="periodOptions"
      :selected="selectedPeriod"
      :loading="loading"
      select-label="查看月份"
      @change="changePeriod"
      @refresh="loadData(queryPeriod())"
    />

    <div v-if="error" class="state-panel error" role="alert">
      <h2>经营简报加载失败</h2>
      <p>{{ error }}</p>
      <button type="button" @click="loadData(queryPeriod())">重新加载</button>
    </div>
    <div v-else-if="loading && !data" class="state-panel" role="status">
      正在读取所选月份的经营简报…
    </div>
    <div v-else-if="!data" class="state-panel">
      <h2>还没有可查看的月份</h2>
      <p>生成首个会计期间后，这里会出现只读经营简报。</p>
    </div>

    <template v-else>
      <DashboardSectionNav
        :items="sectionLinks"
        :active="activeSection"
        label="经营简报区段"
        floating
        @select="focusSection"
      />

      <section id="overview" class="cockpit section-anchor" tabindex="-1">
        <div class="cockpit-copy">
          <div class="cockpit-meta">
            <span>{{ response?.selected_period?.short_label }}</span>
            <span>数据生成于 {{ generatedText() }}</span>
          </div>
          <h2>{{ response?.selected_period?.short_label }}经营简报</h2>
          <p class="hero-note">{{ heroNote() }}</p>
          <div class="status-rail" aria-label="本月状态">
            <span :class="['status-chip', { error: !data.validation.integrity_valid }]">
              {{ data.validation.integrity_valid ? "账务一致" : "账务异常" }}
            </span>
            <span
              :class="['status-chip', { attention: data.cash.unmatched_count + data.cash.pending_late_count }]"
            >
              {{
                data.cash.unmatched_count + data.cash.pending_late_count
                  ? `${data.cash.unmatched_count + data.cash.pending_late_count} 笔流水待处理`
                  : data.cash.transaction_count
                    ? "银行流水已处理"
                    : "本月无银行流水"
              }}
            </span>
            <span :class="['status-chip', { attention: response?.selected_period?.status !== 'closed' }]">
              {{ response?.selected_period?.status === "closed" ? "期间已锁定" : "期间尚未关账" }}
            </span>
          </div>
          <div class="takeaway">
            <span>经营结论</span>
            <strong>{{ takeaway }}</strong>
          </div>
        </div>

        <aside class="action-queue" aria-label="需要处理">
          <header>
            <span class="queue-title">需要处理</span>
            <span :class="['queue-count', { healthy: !priorities.length }]">
              {{ priorities.length ? `${priorities.length}项` : "✓" }}
            </span>
          </header>
          <div v-if="priorities.length" class="priority-list">
            <component
              :is="item.action ? 'button' : 'article'"
              v-for="item in priorities"
              :key="item.title"
              :class="[item.state, { 'priority-action': item.action }]"
              :type="item.action ? 'button' : undefined"
              @click="item.action && runPriorityAction(item.action)"
            >
              <span aria-hidden="true" />
              <div>
                <strong>{{ item.title }}</strong>
                <small>{{ item.note }}</small>
              </div>
            </component>
          </div>
          <div v-else class="healthy-summary">
            <strong>{{ data.validation.title }}</strong>
            <span>{{ data.validation.summary }}。</span>
          </div>
        </aside>
      </section>

      <section class="kpi-grid" aria-label="本月核心指标">
        <article class="kpi bank">
          <span class="kpi-label">账面银行余额</span>
          <strong>{{ formatFen(data.position.bank_fen) }}</strong>
          <small>{{ bankContext() }}</small>
        </article>
        <article :class="['kpi', 'result', { loss: fen(data.position.month_result_fen) < 0n }]">
          <span class="kpi-label">本月账面损益</span>
          <strong>{{ formatFen(data.position.month_result_fen) }}</strong>
          <small>
            收入 {{ formatFen(data.position.month_revenue_fen) }} · 费用
            {{ formatFen(data.position.month_expense_fen) }} · 按业务归属月
          </small>
        </article>
        <button class="kpi asset" type="button" @click="focusSection('finance')">
          <span class="kpi-label">长期资产净值</span>
          <strong>{{ formatFen(data.long_term_assets.net_fen) }}</strong>
          <small>
            固定 {{ data.long_term_assets.fixed_active_count }} 项 · 无形
            {{ data.long_term_assets.intangible_active_count }} 项 <b>查看构成 ›</b>
          </small>
        </button>
        <button class="kpi open" type="button" @click="focusSection('open-items')">
          <span class="kpi-label">{{ isClosed ? "关账时点应收 / 应付" : "期末待收 / 待付" }}</span>
          <strong>
            {{ formatFen(data.open_items.receivable_fen) }} /
            {{ formatFen(data.open_items.payable_fen) }}
          </strong>
          <small>
            {{ isClosed ? "应收" : "待收" }} {{ data.open_items.receivable_count }} 项 ·
            {{ isClosed ? "应付" : "待付" }} {{ data.open_items.payable_count }} 项
            <b>{{ isClosed ? "查看历史快照" : "查看往来" }} ›</b>
          </small>
        </button>
      </section>

      <div id="activity" class="section-anchor" tabindex="-1">
        <BriefActivityWorkbench
          :groups="data.activity_groups"
          :vouchers="data.vouchers"
          :voucher-count="data.voucher_count"
          :line-count="data.line_count"
        />
      </div>

      <div v-if="data.workforce_cost.has_activity" id="workforce" class="section-anchor" tabindex="-1">
        <BriefWorkforceSection
          :workforce="data.workforce_cost"
          :period-key="selectedPeriod"
          :period-label="response?.selected_period?.short_label || ''"
        />
      </div>

      <div id="finance" class="section-anchor" tabindex="-1">
        <BriefFinancialOverview
          :cash="data.cash"
          :position="data.position"
          :unmatched="data.unmatched_bank_activity"
        />
      </div>

      <div id="open-items" class="section-anchor" tabindex="-1">
        <BriefOpenItems
          :open-items="data.open_items"
          :period-label="response?.selected_period?.short_label || ''"
          :period-status="response?.selected_period?.status || ''"
        />
      </div>

      <div class="final-section-space">
      <footer
        id="validation"
        :class="['trust-footer', 'section-anchor', data.validation.state]"
        tabindex="-1"
      >
        <div class="trust-heading">
          <div>
            <p>可信度校验</p>
            <h2>{{ data.validation.title }}</h2>
            <span>{{ data.validation.summary }}。</span>
          </div>
          <span :class="['trust-state', data.validation.state]">
            {{ data.validation.integrity_valid ? "账务一致" : "需要复核" }}
          </span>
        </div>
        <div class="checks">
          <article v-for="item in data.validation.items" :key="item.key" :class="item.state">
            <span class="check-mark">
              {{ item.state === "pass" ? "✓" : item.state === "error" ? "×" : item.state === "pending" ? "!" : "–" }}
            </span>
            <div>
              <strong>{{ item.label }}</strong>
              <small>{{ item.text }}</small>
            </div>
          </article>
        </div>
        <details class="trust-proof">
          <summary>查看本月校验依据</summary>
          <dl>
            <div><dt>正式凭证 / 分录</dt><dd>{{ data.voucher_count }} 张 / {{ data.line_count }} 行</dd></div>
            <div><dt>借方合计 / 贷方合计</dt><dd>{{ formatFen(data.total_debit_fen) }} / {{ formatFen(data.total_credit_fen) }}</dd></div>
            <div><dt>银行当前有效匹配</dt><dd>{{ data.cash.matched_count }} / {{ data.cash.ordinary_count }}</dd></div>
            <div><dt>期间状态</dt><dd>{{ statusLabel(response?.selected_period?.status || "") }}</dd></div>
            <div><dt>页面数据生成时间</dt><dd>{{ generatedText() }}</dd></div>
          </dl>
        </details>
      </footer>
      </div>
    </template>
  </section>
</template>

<style scoped>
.brief-page {
  --brief-page: var(--background);
  --brief-surface: var(--surface);
  --brief-soft: var(--surface-soft);
  --brief-text: var(--text);
  --brief-muted: var(--muted);
  --brief-line: var(--line);
  --brief-line-strong: var(--line-strong);
  --brief-green: var(--accent);
  --brief-green-soft: var(--accent-soft);
  --brief-blue: var(--info);
  --brief-blue-soft: var(--info-soft);
  --brief-gold: var(--gold);
  --brief-gold-soft: var(--gold-soft);
  --brief-amber: var(--warning);
  --brief-amber-soft: var(--warning-soft);
  --brief-red: var(--danger);
  --brief-red-soft: var(--danger-soft);
  --brief-shadow: var(--shadow-soft);
  width: min(calc(100% - 48px), 1320px);
  margin: 0 auto;
  padding: 25px 0 46px;
  color: var(--brief-text);
}

.state-panel {
  padding: 28px;
  border: 1px solid var(--brief-line);
  border-radius: 18px;
  background: var(--brief-surface);
  box-shadow: var(--brief-shadow);
}

.state-panel h2,
.state-panel p {
  margin-top: 0;
}

.state-panel.error {
  border-color: var(--brief-red);
}

.state-panel button {
  min-height: 40px;
  padding: 0 13px;
  border: 0;
  border-radius: 10px;
  background: var(--brief-green);
  color: var(--brief-surface);
  cursor: pointer;
}

.section-anchor {
  scroll-margin-top: 78px;
}

.cockpit {
  display: grid;
  grid-template-columns: minmax(0, 1.55fr) minmax(300px, 0.75fr);
  gap: 25px;
  min-height: 198px;
  padding: 23px 25px;
  border: 1px solid color-mix(in srgb, var(--brief-green) 20%, var(--brief-line));
  border-radius: 20px;
  background:
    radial-gradient(circle at 7% 12%, color-mix(in srgb, var(--brief-green) 11%, transparent), transparent 32%),
    linear-gradient(125deg, var(--brief-surface), color-mix(in srgb, var(--brief-green-soft) 66%, var(--brief-surface)));
  box-shadow: var(--brief-shadow);
}

.cockpit-meta,
.status-rail {
  display: flex;
  flex-wrap: wrap;
  gap: 7px 13px;
  color: var(--brief-muted);
  font-size: 11px;
}

.cockpit-meta span:first-child {
  color: var(--brief-green);
  font-weight: 850;
  letter-spacing: 0.06em;
}

.cockpit h2 {
  margin: 7px 0 3px;
  font-size: clamp(25px, 2.8vw, 34px);
  letter-spacing: -0.04em;
}

.hero-note {
  margin: 0;
  color: var(--brief-muted);
  font-size: 12px;
}

.status-rail {
  margin-top: 11px;
}

.status-chip,
.queue-count,
.trust-state {
  display: inline-flex;
  min-height: 25px;
  align-items: center;
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--brief-green-soft);
  color: var(--brief-green);
  font-size: 11px;
  font-weight: 800;
}

.status-chip.attention,
.trust-state.attention {
  background: var(--brief-amber-soft);
  color: var(--brief-amber);
}

.status-chip.error,
.trust-state.error {
  background: var(--brief-red-soft);
  color: var(--brief-red);
}

.takeaway {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 10px;
  align-items: start;
  margin-top: 13px;
  padding-top: 11px;
  border-top: 1px solid color-mix(in srgb, var(--brief-green) 18%, var(--brief-line));
}

.takeaway span {
  color: var(--brief-green);
  font-size: 11px;
  font-weight: 850;
  white-space: nowrap;
}

.takeaway strong {
  font-size: 14px;
  line-height: 1.55;
}

.action-queue {
  min-width: 0;
  padding: 14px;
  border: 1px solid color-mix(in srgb, var(--brief-line) 82%, transparent);
  border-radius: 15px;
  background: color-mix(in srgb, var(--brief-surface) 83%, transparent);
}

.action-queue header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 13px;
}

.queue-title,
.healthy-summary span,
.priority-list small {
  color: var(--brief-muted);
  font-size: 11px;
}

.queue-count {
  min-width: 27px;
  justify-content: center;
  background: var(--brief-amber-soft);
  color: var(--brief-amber);
}

.queue-count.healthy {
  background: var(--brief-green-soft);
  color: var(--brief-green);
}

.priority-list {
  display: grid;
  gap: 6px;
  margin-top: 9px;
}

.priority-list article,
.priority-list .priority-action {
  display: grid;
  grid-template-columns: 6px minmax(0, 1fr);
  gap: 9px;
  align-items: center;
  width: 100%;
  padding: 7px 9px;
  border: 0;
  border-radius: 9px;
  background: var(--brief-soft);
  color: inherit;
  font: inherit;
  text-align: left;
}

.priority-list .priority-action {
  cursor: pointer;
}

.priority-list .priority-action:hover {
  background: color-mix(in srgb, var(--brief-soft) 72%, var(--brief-amber-soft));
}

.priority-list .priority-action:focus-visible {
  outline: 2px solid var(--brief-amber);
  outline-offset: 2px;
}

.priority-list article > span,
.priority-list .priority-action > span {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--brief-muted);
}

.priority-list article.attention > span,
.priority-list .priority-action.attention > span {
  background: var(--brief-amber);
}

.priority-list article.error > span,
.priority-list .priority-action.error > span {
  background: var(--brief-red);
}

.priority-list article > div,
.priority-list .priority-action > div,
.healthy-summary {
  display: grid;
}

.priority-list strong {
  font-size: 12px;
}

.healthy-summary {
  gap: 4px;
  margin-top: 17px;
  padding: 13px;
  border-radius: 11px;
  background: var(--brief-green-soft);
  color: var(--brief-green);
}

.kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-top: 10px;
}

.kpi {
  position: relative;
  display: grid;
  min-width: 0;
  min-height: 126px;
  align-content: space-between;
  gap: 4px;
  overflow: hidden;
  padding: 15px 16px;
  border: 1px solid var(--brief-line);
  border-radius: 16px;
  background: var(--brief-surface);
  color: var(--brief-text);
  box-shadow: var(--brief-shadow);
  font: inherit;
  text-align: left;
}

.kpi::before {
  position: absolute;
  top: 0;
  right: 0;
  left: 0;
  height: 3px;
  background: var(--brief-green);
  content: "";
}

.kpi.bank::before,
.kpi.open::before {
  background: var(--brief-blue);
}

.kpi.asset::before {
  background: var(--brief-gold);
}

.kpi.result.loss::before {
  background: var(--brief-red);
}

button.kpi {
  cursor: pointer;
}

button.kpi:hover,
button.kpi:focus-visible {
  border-color: var(--brief-green);
  transform: translateY(-1px);
}

.kpi-label {
  color: var(--brief-muted);
  font-size: 12px;
  font-weight: 750;
}

.kpi > strong {
  overflow-wrap: anywhere;
  font-size: clamp(20px, 2vw, 27px);
  line-height: 1.15;
  letter-spacing: -0.025em;
}

.kpi.result:not(.loss) > strong {
  color: var(--brief-green);
}

.kpi.result.loss > strong {
  color: var(--brief-red);
}

.kpi.asset > strong {
  color: var(--brief-gold);
}

.kpi.bank > strong,
.kpi.open > strong {
  color: var(--brief-blue);
}

.kpi small {
  color: var(--brief-muted);
  font-size: 11px;
  line-height: 1.4;
}

.kpi small b {
  color: var(--brief-green);
}

.kpi-grid + .section-anchor,
.section-anchor + .section-anchor {
  margin-top: 12px;
}

.final-section-space {
  min-height: calc(100vh - 80px);
}

.trust-footer {
  margin-top: 12px;
  padding: 15px 18px;
  border: 1px solid var(--brief-line);
  border-left: 3px solid var(--brief-green);
  border-radius: 16px;
  background: var(--brief-surface);
  box-shadow: var(--brief-shadow);
}

.trust-footer.attention {
  border-left-color: var(--brief-amber);
}

.trust-footer.error {
  border-left-color: var(--brief-red);
}

.trust-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
}

.trust-heading p {
  margin: 0 0 3px;
  color: var(--brief-green);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.08em;
}

.trust-heading h2 {
  margin: 0;
  font-size: 20px;
}

.trust-heading > div > span {
  color: var(--brief-muted);
  font-size: 12px;
}

.checks {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 7px;
  margin-top: 9px;
}

.checks article {
  display: grid;
  min-width: 0;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 8px;
  padding: 7px 8px;
  border-radius: 10px;
  background: var(--brief-soft);
}

.check-mark {
  display: grid;
  width: 21px;
  height: 21px;
  place-items: center;
  border-radius: 50%;
  background: var(--brief-green-soft);
  color: var(--brief-green);
  font-size: 11px;
  font-weight: 850;
}

.checks article.pending .check-mark {
  background: var(--brief-amber-soft);
  color: var(--brief-amber);
}

.checks article.error .check-mark {
  background: var(--brief-red-soft);
  color: var(--brief-red);
}

.checks article.neutral .check-mark {
  background: var(--brief-line);
  color: var(--brief-muted);
}

.checks article > div {
  display: grid;
  min-width: 0;
}

.checks strong {
  font-size: 11px;
}

.checks small {
  overflow: hidden;
  color: var(--brief-muted);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.trust-proof {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--brief-line);
}

.trust-proof summary {
  color: var(--brief-green);
  font-size: 11px;
  font-weight: 750;
  cursor: pointer;
}

.trust-proof dl {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px 18px;
  margin: 10px 0 0;
}

.trust-proof dl > div {
  display: flex;
  justify-content: space-between;
  gap: 14px;
  padding-bottom: 5px;
  border-bottom: 1px solid var(--brief-line);
  font-size: 11px;
}

.trust-proof dt {
  color: var(--brief-muted);
}

.trust-proof dd {
  margin: 0;
  font-weight: 800;
  text-align: right;
}

@media (max-width: 1080px) {
  .cockpit {
    grid-template-columns: minmax(0, 1.3fr) minmax(275px, 0.8fr);
  }

  .kpi-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .checks {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 760px) {
  .brief-page {
    width: min(calc(100% - 24px), 1320px);
    padding: 16px 0 24px;
  }

  .cockpit {
    grid-template-columns: 1fr;
    gap: 13px;
    padding: 19px;
    border-radius: 17px;
  }

  .takeaway {
    grid-template-columns: 1fr;
    gap: 3px;
  }

  .action-queue {
    padding: 12px;
  }

  .kpi-grid {
    grid-template-columns: 1fr;
  }

  .kpi {
    min-height: 116px;
  }

  .section-anchor {
    scroll-margin-top: 68px;
  }

  .trust-footer {
    padding: 16px;
  }

  .trust-heading {
    flex-direction: column;
    gap: 8px;
  }

  .checks,
  .trust-proof dl {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .checks small {
    white-space: normal;
  }

  .trust-proof summary {
    display: flex;
    min-height: 44px;
    align-items: center;
  }
}

@media (max-width: 430px) {
  .cockpit h2 {
    font-size: 26px;
  }

  .kpi > strong {
    font-size: 23px;
  }
}
</style>
