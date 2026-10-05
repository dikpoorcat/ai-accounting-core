<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { DashboardApiError, dashboardErrorMessage } from "../api/client";
import { LocalApiError } from "../api/localKernel";
import {
  fetchDeferredQuarterlyReport,
  fetchQuarterlyWorkbook,
  fetchReportExportStatus,
  requestQuarterlyExport,
  type DeferredQuarterlyReport,
  type ReportStatement,
  type ReportStatementRow,
} from "../api/reports";
import DashboardModuleHeader from "../components/DashboardModuleHeader.vue";
import DashboardSectionNav from "../components/DashboardSectionNav.vue";
import { useDashboardContext } from "../composables/useDashboardContext";
import { useDashboardSections } from "../composables/useDashboardSections";
import { formatFen } from "../utils/money";

interface SummaryCard {
  source: string;
  label: string;
  value: string;
  note: string;
}

interface StatementTemplateMeta {
  title: string;
  formCode: string;
}

type BalanceTemplateCell =
  | { kind: "section"; label: string }
  | { kind: "row"; row: ReportStatementRow }
  | { kind: "blank" };

type PeriodReference = {
  readonly key: string;
  readonly year: number;
  readonly month: number;
};

type QuarterReference = {
  readonly key: string;
  readonly year: number;
  readonly quarter: number;
};

const statementTemplateMeta: Record<string, StatementTemplateMeta> = {
  balance_sheet: {
    title: "资产负债表（适用执行小企业会计准则的企业）",
    formCode: "会小企01表",
  },
  profit_statement: {
    title: "利润表_月季报（适用执行小企业会计准则的企业）",
    formCode: "会小企02表",
  },
  cash_flow_statement: {
    title: "现金流量表_月季报（适用执行小企业会计准则的企业）",
    formCode: "会小企03表",
  },
};

const templateNumberFormatter = new Intl.NumberFormat("zh-CN", {
  maximumFractionDigits: 0,
});

const route = useRoute();
const router = useRouter();
const { context, load: loadContext, refresh: refreshContext } = useDashboardContext();
const selectedQuarter = ref("");
const report = ref<DeferredQuarterlyReport | null>(null);
const loading = ref(false);
const exporting = ref(false);
const needsRegeneration = ref(false);
let exportAttempt: { digest: string; requestId: string; jobId?: string } | null = null;
const errorMessage = ref("");
const exportNotice = ref("");
const exportNoticeKind = ref<"success" | "attention" | "error">("success");
const statementsExpanded = ref(false);
const taxTemplateMode = ref(false);
const activeStatementKey = ref("");
let mounted = false;
let previewController: AbortController | null = null;
let exportController: AbortController | null = null;
let requestGeneration = 0;

function selectionKey() { return JSON.stringify([route.query.company_id, route.query.period, route.query.quarter]); }
function isCurrent(generation: number, selection: string) { return mounted && generation === requestGeneration && selectionKey() === selection; }
function invalidateRequests(keepContent = false) {
  requestGeneration += 1;
  previewController?.abort(); exportController?.abort();
  previewController = null; exportController = null;
  if (!keepContent) report.value = null;
  loading.value = keepContent; exporting.value = false;
  exportNotice.value = ""; errorMessage.value = "";
}

const quarterOptions = computed(() =>
  (context.value?.quarters ?? []).map((quarter) => ({
    key: quarter.key,
    label: quarter.label,
    status: quarter.key === selectedQuarter.value && report.value
      ? report.value.close_state : undefined,
  })),
);
const sectionLinks = computed(() => {
  if (!report.value) return [];
  return [
    { id: "report-overview", label: "概览" },
    ...(report.value.statements.length ? [{ id: "report-statements", label: "财务报表" }] : []),
  ];
});
const { activeSection, focusSection } = useDashboardSections(sectionLinks, "report-overview");
const reportHeadline = computed(() => {
  if (needsRegeneration.value) return "报表需要重新生成";
  if (report.value?.export.available) return "已就绪";
  if (report.value?.readiness_state === "blocked") return "AI 会计核对中";
  if (report.value?.close_state === "open") return "相关月份结账后可下载报表";
  return "本季度报表暂时无法下载";
});
const reportNextStep = computed(() => {
  if (needsRegeneration.value) return "请根据提示重新生成。";
  if (report.value?.export.available) return "可下载 Excel 报表，使用前请复核。";
  if (report.value?.readiness_state === "blocked") return "AI 会计正在核对资料，如需您补充资料会另列待办。";
  if (report.value?.close_state === "open") return "当前为试算金额，相关月份结账后刷新。";
  return report.value?.message ?? "";
});
const checkedAt = computed(() => {
  if (!report.value) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(report.value.checked_at));
});
const summaryCards = computed<SummaryCard[]>(() => {
  const summary = report.value?.summary;
  if (!summary) return [];
  return [
    {
      source: "资产负债表",
      label: "资产合计",
      value: formatFen(summary.assets_total_fen),
      note: `负债合计 ${formatFen(summary.liabilities_total_fen)}`,
    },
    {
      source: "利润表",
      label: "本季度净利润",
      value: formatFen(summary.current_net_profit_fen),
      note: `本年累计 ${formatFen(summary.year_to_date_net_profit_fen)}`,
    },
    {
      source: "现金流量表",
      label: "本季度现金净增加额",
      value: formatFen(summary.current_cash_change_fen),
      note: `期末现金 ${formatFen(summary.ending_cash_fen)}`,
    },
  ];
});
const activeStatement = computed<ReportStatement | null>(() => {
  if (!report.value) return null;
  return report.value.statements.find((item) => item.key === activeStatementKey.value) ?? null;
});
const visibleStatementRows = computed(() => {
  if (!activeStatement.value) return [];
  return activeStatement.value.rows.filter(
    (row) => taxTemplateMode.value || row.has_amount || row.is_total
      || Object.values(row.values).some((value) => value === null),
  );
});
const activeTemplateMeta = computed<StatementTemplateMeta | null>(() => {
  if (!activeStatement.value) return null;
  return statementTemplateMeta[activeStatement.value.key] ?? null;
});
const templateQuarterStart = computed(() => {
  const period = report.value?.period;
  if (!period) return "";
  const firstMonth = String((period.quarter - 1) * 3 + 1).padStart(2, "0");
  return period.quarter_start ?? `${period.year}-${firstMonth}-01`;
});
const balanceTemplateRows = computed(() => {
  const statement = activeStatement.value;
  if (statement?.key !== "balance_sheet") return [];
  const byLine = new Map(statement.rows.map((row) => [row.line, row]));
  const rowCell = (line: number): BalanceTemplateCell => {
    const row = byLine.get(line);
    return row ? { kind: "row", row } : { kind: "blank" };
  };
  const section = (label: string): BalanceTemplateCell => ({ kind: "section", label });
  const blank = (): BalanceTemplateCell => ({ kind: "blank" });
  const left: BalanceTemplateCell[] = [
    section("流动资产："),
    ...Array.from({ length: 15 }, (_, index) => rowCell(index + 1)),
    section("非流动资产："),
    ...Array.from({ length: 15 }, (_, index) => rowCell(index + 16)),
  ];
  const right: BalanceTemplateCell[] = [
    section("流动负债："),
    ...Array.from({ length: 11 }, (_, index) => rowCell(index + 31)),
    section("非流动负债："),
    ...Array.from({ length: 6 }, (_, index) => rowCell(index + 42)),
    ...Array.from({ length: 6 }, blank),
    section("所有者权益（或股东权益）："),
    ...Array.from({ length: 6 }, (_, index) => rowCell(index + 48)),
  ];
  return left.map((leftCell, index) => ({ left: leftCell, right: right[index] }));
});
function routeQuarter(): string | null {
  const value = route.query.quarter;
  if (typeof value === "string") return value;
  const legacyValue = route.query.period;
  return typeof legacyValue === "string" && /^\d{4}-Q[1-4]$/.test(legacyValue)
    ? legacyValue
    : null;
}

function quarterKeyForPeriod(period: PeriodReference) {
  return `${period.year}-Q${Math.ceil(period.month / 3)}`;
}

function latestPeriodForQuarter(
  currentContext: {
    readonly periods: readonly PeriodReference[];
    readonly quarters: readonly QuarterReference[];
  },
  quarterKey: string,
) {
  const quarter = currentContext.quarters.find((item) => item.key === quarterKey);
  if (!quarter) return null;
  return currentContext.periods.reduce<PeriodReference | null>((latest, period) => {
    if (
      period.year !== quarter.year ||
      Math.ceil(period.month / 3) !== quarter.quarter ||
      (latest !== null && period.month <= latest.month)
    ) {
      return latest;
    }
    return period;
  }, null);
}

async function synchronizeQuarter(force = false) {
  const generation = requestGeneration, selection = selectionKey();
  try {
    const currentContext = await loadContext();
    if (!isCurrent(generation, selection)) return;
    if (!currentContext.quarters.length) {
          previewController?.abort();
      exportController?.abort();
      selectedQuarter.value = "";
      report.value = null;
      loading.value = false;
      errorMessage.value = "";
      return;
    }
    const selectedPeriod = currentContext.periods.find((item) => item.key === route.query.period);
    const periodQuarter = selectedPeriod ? quarterKeyForPeriod(selectedPeriod) : null;
    const requestedQuarter = routeQuarter();
    const target = periodQuarter && currentContext.quarters.some((item) => item.key === periodQuarter)
      ? periodQuarter
      : currentContext.quarters.some((item) => item.key === requestedQuarter)
        ? (requestedQuarter as string)
        : (currentContext.default_quarter ?? currentContext.quarters.at(-1)?.key ?? "");
    const targetPeriod =
      selectedPeriod?.key ??
      latestPeriodForQuarter(currentContext, target)?.key ??
      currentContext.default_period ??
      undefined;
    if (route.query.quarter !== target || route.query.period !== targetPeriod) {
      await router.replace({
        query: {
          ...route.query,
          period: targetPeriod,
          quarter: target,
        },
        hash: route.hash,
      });
      return;
    }
    if (force || selectedQuarter.value !== target || report.value === null) {
      await preview(target);
    }
  } catch (error: unknown) {
    if (!isCurrent(generation, selection)) return;
    const message = dashboardErrorMessage(error);
    if (message) errorMessage.value = message;
  }
}

async function preview(quarterKey: string, contextGate?: Promise<void>) {
  const generation = ++requestGeneration, selection = selectionKey();
  const companyId = route.query.company_id;
  previewController?.abort();
  exportController?.abort();
  exportController = null; exporting.value = false;
  const controller = new AbortController();
  previewController = controller;
  const match = /^(\d{4})-Q([1-4])$/.exec(quarterKey);
  if (!match) {
    report.value = null; loading.value = false;
    controller.abort(); previewController = null;
    errorMessage.value = "请选择已有会计期间对应的季度。";
    return;
  }
  selectedQuarter.value = quarterKey;
  if (!contextGate) report.value = null;
  errorMessage.value = "";
  exportNotice.value = "";
  needsRegeneration.value = false;
  loading.value = true;
  try {
    if (typeof companyId !== "string") throw new Error("No selected company");
    const request = fetchDeferredQuarterlyReport(
      companyId,
      Number(match[1]),
      Number(match[2]),
      controller.signal,
    );
    const result = contextGate ? (await Promise.all([request, contextGate]))[0] : await request;
    if (!isCurrent(generation, selection) || previewController !== controller) return;
    report.value = result;
    loading.value = false;
    if (!result.statements.some((item) => item.key === activeStatementKey.value)) {
      activeStatementKey.value = "";
      statementsExpanded.value = false;
    }
    await nextTick();
  } catch (error: unknown) {
    if (isCurrent(generation, selection) && previewController === controller) {
      report.value = null;
      const message = dashboardErrorMessage(error);
      if (message) errorMessage.value = message;
    }
  } finally {
    if (isCurrent(generation, selection) && previewController === controller) {
      loading.value = false;
      previewController = null;
    }
  }
}

function changeQuarter(value: string) {
  if (!value) return;
  const latestPeriod = context.value ? latestPeriodForQuarter(context.value, value) : null;
  if (value === routeQuarter() && latestPeriod?.key === route.query.period) return;
  void router.push({
    query: { company_id: route.query.company_id, period: latestPeriod?.key ?? route.query.period, quarter: value }, hash: "",
  });
}

async function refresh() {
  invalidateRequests(true);
  const generation = requestGeneration, selection = selectionKey();
  try {
    const company = route.query.company_id, period = route.query.period, quarter = routeQuarter();
    if (typeof company === "string" && typeof period === "string" && quarter && route.query.quarter === quarter
      && context.value?.current_company?.company_id === company && context.value.periods.some((item) => item.key === period)) {
      const contextGate = refreshContext().then((fresh) => {
        const selected = fresh.periods.find((item) => item.key === period);
        if (fresh.current_company?.company_id !== company || !selected || quarterKeyForPeriod(selected) !== quarter
          || !fresh.quarters.some((item) => item.key === quarter))
          throw new Error("当前公司或期间已变化，请重新选择。");
      });
      await preview(quarter, contextGate);
    } else {
      await refreshContext();
      if (!isCurrent(generation, selection)) return;
      await synchronizeQuarter(true);
    }
  } catch (error: unknown) {
    if (!isCurrent(generation, selection)) return;
    report.value = null; loading.value = false;
    const message = dashboardErrorMessage(error);
    if (message) errorMessage.value = message;
  }
}

async function exportReport() {
  const generation = requestGeneration, selection = selectionKey();
  const current = report.value;
  const companyId = route.query.company_id;
  if (!current?.export.available || !current.export.preview_digest || typeof companyId !== "string") return;
  exportController?.abort();
  const controller = new AbortController();
  exportController = controller;
  exporting.value = true;
  exportNoticeKind.value = "attention";
  exportNotice.value = "正在准备生成报表…";
  try {
    const digest = `${companyId}:${current.export.preview_digest}`;
    if (exportAttempt?.digest !== digest) exportAttempt = { digest, requestId: crypto.randomUUID() };
    const attempt = exportAttempt;
    needsRegeneration.value = false;
    if (!attempt.jobId) attempt.jobId = (await requestQuarterlyExport(companyId, current, attempt.requestId, controller.signal)).job_id;
    const jobId = attempt.jobId;
    if (!jobId) throw new DashboardApiError(502, "REPORT_JOB_RESPONSE", "报表任务没有返回任务编号。");
    if (!isCurrent(generation, selection) || exportController !== controller) return;
    exportNotice.value = "报表正在生成，完成后将开始下载。";
    while (!controller.signal.aborted) {
      const job = await fetchReportExportStatus(companyId, jobId, controller.signal);
      if (!isCurrent(generation, selection) || exportController !== controller) return;
      if (job.status === "succeeded") break;
      if (job.status === "failed" && job.attempts >= 3) throw new DashboardApiError(409, "REPORT_JOB_FAILED", "原报表任务无法继续生成，请重新生成。");
      if (job.status === "failed") exportNotice.value = "本次生成未成功，正在等待自动重试。";
      await new Promise<void>((resolve, reject) => {
        const abort = () => { clearTimeout(timer); reject(new DOMException("Aborted", "AbortError")); };
        const timer = setTimeout(() => { controller.signal.removeEventListener("abort", abort); resolve(); }, 1200);
        controller.signal.addEventListener("abort", abort, { once: true });
      });
    }
    if (!isCurrent(generation, selection) || controller.signal.aborted || exportController !== controller) return;
    const blob = await fetchQuarterlyWorkbook(companyId, jobId, controller.signal);
    if (!isCurrent(generation, selection) || exportController !== controller) return;
    const href = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = href;
    link.download = current.export.file_name;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(href);
    exportNoticeKind.value = "success";
    exportNotice.value =
      "报表文件已开始下载，请在浏览器下载记录中查看，使用前请复核。";
  } catch (error: unknown) {
    if (!isCurrent(generation, selection) || exportController !== controller) return;
    if ((error instanceof DashboardApiError || error instanceof LocalApiError) && ["REPORT_PREVIEW_STALE", "preview_expired"].includes(error.code)) {
      exportAttempt = null;
      await preview(selectedQuarter.value);
      if (!mounted || selectionKey() !== selection || requestGeneration !== generation + 1) return;
      exportNoticeKind.value = "attention";
      exportNotice.value = "报表资料已有变化，已刷新为最新结果。请核对后重新生成并下载。";
    } else {
      if ((error instanceof DashboardApiError || error instanceof LocalApiError) && ["REPORT_JOB_FAILED", "report_download_invalid", "unknown_report_job"].includes(error.code)) {
        exportAttempt = null;
        needsRegeneration.value = true;
      }
      const message = dashboardErrorMessage(error);
      if (message) {
        exportNoticeKind.value = "error";
        exportNotice.value = message + (needsRegeneration.value ? " 请点击“重新生成”再试一次。" : "");
      }
    }
  } finally {
    if (isCurrent(generation, selection) && exportController === controller) {
      exporting.value = false;
      exportController = null;
    }
  }
}

function statementValue(value: string | null | undefined) {
  return value === null || value === undefined ? "—" : formatFen(value);
}

function templateStatementValue(value: string | null | undefined) {
  if (value === null || value === undefined) return "—";
  const amount = BigInt(value);
  const negative = amount < 0n;
  const absolute = negative ? -amount : amount;
  const yuan = absolute / 100n;
  const cents = String(absolute % 100n).padStart(2, "0");
  return `${negative ? "-" : ""}${templateNumberFormatter.format(yuan)}.${cents}`;
}

function templateSectionLabel(statementKey: string, line: number) {
  if (statementKey !== "cash_flow_statement") return "";
  return (
    {
      1: "一、经营活动产生的现金流量：",
      8: "二、投资活动产生的现金流量：",
      14: "三、筹资活动产生的现金流量：",
    } as Record<number, string>
  )[line] ?? "";
}

function templateRowName(statementKey: string, row: ReportStatementRow) {
  const prefixes =
    statementKey === "profit_statement"
      ? ({ 1: "一、", 2: "减：", 21: "二、", 22: "加：", 24: "减：", 30: "三、", 31: "减：", 32: "四、" } as Record<number, string>)
      : statementKey === "cash_flow_statement"
        ? ({ 20: "四、", 21: "加：", 22: "五、" } as Record<number, string>)
        : {};
  return `${prefixes[row.line] ?? ""}${row.name}`;
}

function isNegative(value: string | null | undefined) {
  return value !== null && value !== undefined && BigInt(value) < 0n;
}

function balanceTemplateCellValue(cell: BalanceTemplateCell, columnIndex: number) {
  if (cell.kind !== "row") return "";
  const column = activeStatement.value?.columns[columnIndex];
  return column ? templateStatementValue(cell.row.values[column.key]) : "";
}

function balanceTemplateCellIsNegative(cell: BalanceTemplateCell, columnIndex: number) {
  if (cell.kind !== "row") return false;
  const column = activeStatement.value?.columns[columnIndex];
  return column ? isNegative(cell.row.values[column.key]) : false;
}

function selectStatement(key: string) {
  if (statementsExpanded.value && activeStatementKey.value === key) {
    statementsExpanded.value = false;
    activeStatementKey.value = "";
    return;
  }
  activeStatementKey.value = key;
  statementsExpanded.value = true;
}

function handleTabKey(event: KeyboardEvent, index: number) {
  const statements = report.value?.statements ?? [];
  if (!statements.length || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
    return;
  }
  event.preventDefault();
  let nextIndex = index;
  if (event.key === "Home") nextIndex = 0;
  if (event.key === "End") nextIndex = statements.length - 1;
  if (event.key === "ArrowLeft") nextIndex = (index - 1 + statements.length) % statements.length;
  if (event.key === "ArrowRight") nextIndex = (index + 1) % statements.length;
  activeStatementKey.value = statements[nextIndex].key;
  statementsExpanded.value = true;
  const generation = requestGeneration, selection = selectionKey();
  void nextTick(() => { if (isCurrent(generation, selection)) document.getElementById(`report-statement-button-${nextIndex}`)?.focus(); });
}

onMounted(() => {
  mounted = true;
  void synchronizeQuarter();
});

watch(
  () => [route.query.company_id, route.query.period, route.query.quarter],
  (value, previous) => {
    if (value.every((item, index) => item === previous[index])) return;
    invalidateRequests();
  },
  { flush: "sync" },
);
watch(
  () => [context.value?.current_company?.company_id, route.query.period, route.query.quarter] as const,
  ([orgId, period, quarter], previous) => {
    if (previous && orgId === previous[0] && period === previous[1]
      && quarter === previous[2]) return;
    const previousOrgId = previous?.[0];
    if (mounted && orgId) void synchronizeQuarter(orgId !== previousOrgId);
  },
);

onBeforeUnmount(() => {
  mounted = false;
  invalidateRequests();
});
</script>

<template>
  <section class="reports-page">
    <div class="reports-content">
      <DashboardModuleHeader
        title="季度财务报表"
        :options="quarterOptions"
        :selected="selectedQuarter"
        :loading="loading"
        select-label="季度报表期间"
        @change="changeQuarter"
        @refresh="refresh"
      >
        <template #navigation>
          <DashboardSectionNav v-if="sectionLinks.length" v-show="!loading" :items="sectionLinks" :active="activeSection" label="报表内容导航" @select="focusSection" />
        </template>
      </DashboardModuleHeader>


      <section v-if="!quarterOptions.length && !loading" class="state-panel">
        <strong>还没有可查看的报表</strong>
        <span>公司开始记账后，可在这里查看对应季度的财务报表。</span>
      </section>

      <section v-else-if="loading" class="state-panel" aria-live="polite">
        <strong>正在整理本季度报表</strong>
        <span>正在读取账务和核对资料，请稍候…</span>
      </section>

      <section v-else-if="errorMessage && !report" class="state-panel error" role="alert">
        <strong>季度报表读取失败</strong>
        <span>{{ errorMessage }}</span>
        <button type="button" @click="refresh">刷新报表</button>
      </section>

      <section v-if="report" v-show="!loading && !errorMessage" class="report-dashboard">
        <section id="report-overview" class="report-hero" tabindex="-1" aria-labelledby="report-readiness-title">
          <div class="report-heading">
            <div>
              <p class="dashboard-hero-eyebrow">{{ report.period.label }} · 更新于 {{ checkedAt }}</p>
              <h2
                id="report-readiness-title"
                :class="['dashboard-hero-title', 'report-readiness-title', { ready: report.export.available }]"
                aria-live="polite"
              >
                <span class="report-readiness-help">
                  <button type="button" class="report-readiness-trigger" aria-describedby="report-readiness-tooltip">
                    {{ reportHeadline }}
                  </button>
                  <span id="report-readiness-tooltip" class="report-readiness-tooltip" role="tooltip">
                    <strong>
                      {{ report.close_state === "closed" ? "相关月份已结账" : "相关月份尚未全部结账" }} ·
                      {{ report.readiness_state === "ready" ? "报表资料已核对" : "报表资料待核对" }}
                    </strong>
                    <span>{{ report.message }}</span>
                  </span>
                </span>
              </h2>
              <p class="dashboard-hero-note">{{ reportNextStep }}</p>
            </div>
            <div class="report-actions">
              <button class="secondary" type="button" :disabled="loading" @click="refresh">
                {{ loading ? "正在刷新…" : "刷新报表" }}
              </button>
              <button
                type="button"
                :disabled="!report.export.available || exporting || loading"
                @click="exportReport"
              >
                {{ exporting ? "正在生成…" : needsRegeneration ? "重新生成" : "生成并下载" }}
              </button>
            </div>
          </div>

          <div v-if="exportNotice" class="export-notice" :class="exportNoticeKind" role="status">
            {{ exportNotice }}
          </div>
        <section v-if="summaryCards.length" class="summary-grid" aria-label="季度报表摘要">
          <article v-for="item in summaryCards" :key="item.source">
            <span>{{ item.source }} · {{ item.label }}</span>
            <strong>{{ item.value }}</strong>
            <small>{{ item.note }}</small>
          </article>
        </section>
          </section>



        <section v-if="report.statements.length" id="report-statements" class="report-review" tabindex="-1" aria-label="完整财务报表">
          <div class="review-heading">
            <div>
              <strong>完整财务报表</strong>

            </div>

          </div>

          <div class="statement-buttons" role="tablist" aria-label="季度财务报表">
            <button
              v-for="(statement, index) in report.statements"
              :id="`report-statement-button-${index}`"
              :key="statement.key"
              type="button"
              role="tab"
              :class="{ active: activeStatement?.key === statement.key }"
              :aria-selected="activeStatement?.key === statement.key"
              :aria-expanded="activeStatement?.key === statement.key && statementsExpanded"
              :aria-controls="activeStatement?.key === statement.key ? 'report-full' : undefined"
              :tabindex="activeStatement?.key === statement.key || !activeStatement ? 0 : -1"
              @click="selectStatement(statement.key)"
              @keydown="handleTabKey($event, index)"
            >
              <span class="statement-number">{{ String(index + 1).padStart(2, "0") }}</span>
              <span class="statement-button-copy">
                <strong>{{ statement.label }}</strong>
                <small>{{ activeStatement?.key === statement.key ? "收起报表" : "查看完整报表" }}</small>
              </span>
              <span class="statement-arrow" aria-hidden="true">
                {{ activeStatement?.key === statement.key ? "↑" : "↓" }}
              </span>
            </button>
          </div>

          <div v-if="statementsExpanded" id="report-full" class="report-full">
            <template v-if="activeStatement">
              <div class="table-toolbar">
                <strong>{{ activeStatement.label }}{{ report.draft ? " · 当前试算" : "" }}</strong>
                <label class="template-switch">
                  <span class="switch-copy">
                    <strong>税务局模板格式</strong>
                    <small>按导入 Excel 版式显示全部项目</small>
                  </span>
                  <input v-model="taxTemplateMode" type="checkbox" role="switch" />
                  <span class="switch-track" aria-hidden="true"><span></span></span>
                </label>
              </div>

              <div v-if="taxTemplateMode && activeTemplateMeta" class="template-wrap">
                <div
                  class="tax-template-sheet"
                  :class="{ 'balance-sheet': activeStatement.key === 'balance_sheet' }"
                >
                  <div class="template-title-row">
                    <h3>{{ activeTemplateMeta.title }}</h3>
                    <span>{{ activeTemplateMeta.formCode }}　单位：元</span>
                  </div>
                  <div class="template-meta-grid">
                    <span class="template-meta-label">纳税人识别号</span>
                    <strong>{{ report.organization?.taxpayer_identification_number || "—" }}</strong>
                    <span class="template-meta-label">纳税人名称</span>
                    <strong>{{ report.organization?.name || "—" }}</strong>
                    <span class="template-meta-label">所属期起</span>
                    <strong>{{ templateQuarterStart }}</strong>
                    <span class="template-meta-label">所属期止</span>
                    <strong>{{ report.period.quarter_end }}</strong>
                  </div>

                  <table
                    v-if="activeStatement.key === 'balance_sheet'"
                    class="tax-template-table balance-template-table desktop-template-table"
                  >
                    <thead>
                      <tr>
                        <th>资产</th><th class="line">行次</th>
                        <th v-for="column in activeStatement.columns" :key="`asset-${column.key}`" class="number">
                          {{ column.label }}
                        </th>
                        <th>负债和所有者权益</th><th class="line">行次</th>
                        <th v-for="column in activeStatement.columns" :key="`liability-${column.key}`" class="number">
                          {{ column.label }}
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="(pair, rowIndex) in balanceTemplateRows" :key="rowIndex">
                        <template v-for="(cell, sideIndex) in [pair.left, pair.right]" :key="sideIndex">
                          <td
                            class="template-item"
                            :class="{
                              section: cell.kind === 'section',
                              blank: cell.kind === 'blank',
                              total: cell.kind === 'row' && cell.row.is_total,
                            }"
                          >
                            {{ cell.kind === "section" ? cell.label : cell.kind === "row" ? cell.row.name : "" }}
                          </td>
                          <td
                            class="line"
                            :class="{
                              section: cell.kind === 'section',
                              blank: cell.kind === 'blank',
                              total: cell.kind === 'row' && cell.row.is_total,
                            }"
                          >
                            {{ cell.kind === "row" ? cell.row.line : "" }}
                          </td>
                          <td
                            v-for="columnIndex in 2"
                            :key="columnIndex"
                            class="number"
                            :class="{
                              negative: balanceTemplateCellIsNegative(cell, columnIndex - 1),
                              section: cell.kind === 'section',
                              blank: cell.kind === 'blank',
                              total: cell.kind === 'row' && cell.row.is_total,
                            }"
                          >
                            {{ balanceTemplateCellValue(cell, columnIndex - 1) }}
                          </td>
                        </template>
                      </tr>
                    </tbody>
                  </table>

                  <table v-else class="tax-template-table desktop-template-table">
                    <thead>
                      <tr>
                        <th>项目</th><th class="line">行次</th>
                        <th v-for="column in activeStatement.columns" :key="column.key" class="number">
                          {{ column.key === "current_fen" ? "本期金额" : column.label }}
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      <template v-for="row in activeStatement.rows" :key="row.line">
                        <tr v-if="templateSectionLabel(activeStatement.key, row.line)" class="template-section-row">
                          <td :colspan="activeStatement.columns.length + 2">
                            {{ templateSectionLabel(activeStatement.key, row.line) }}
                          </td>
                        </tr>
                        <tr :class="{ total: row.is_total }">
                          <td>{{ templateRowName(activeStatement.key, row) }}</td>
                          <td class="line">{{ row.line }}</td>
                          <td
                            v-for="column in activeStatement.columns"
                            :key="column.key"
                            class="number"
                            :class="{ negative: isNegative(row.values[column.key]) }"
                          >
                            {{ templateStatementValue(row.values[column.key]) }}
                          </td>
                        </tr>
                      </template>
                    </tbody>
                  </table>
                  <table class="tax-template-table mobile-template-table">
                    <tbody>
                      <template v-for="row in activeStatement.rows" :key="row.line">
                        <tr v-if="templateSectionLabel(activeStatement.key, row.line)" class="template-section-row"><td>{{ templateSectionLabel(activeStatement.key, row.line) }}</td></tr>
                        <tr :class="{ total: row.is_total }">
                          <td>{{ row.line }} · {{ templateRowName(activeStatement.key, row) }}</td>
                          <td v-for="column in activeStatement.columns" :key="column.key" class="number" :data-label="column.key === 'current_fen' ? '本期金额' : column.label" :class="{ negative: isNegative(row.values[column.key]) }">{{ templateStatementValue(row.values[column.key]) }}</td>
                        </tr>
                      </template>
                    </tbody>
                  </table>
                </div>
              </div>

              <div v-else class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>项目</th>
                      <th v-for="column in activeStatement.columns" :key="column.key" class="number">
                        {{ column.label }}
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="row in visibleStatementRows" :key="row.line" :class="{ total: row.is_total }">
                      <td>{{ row.name }}</td>
                      <td v-for="column in activeStatement.columns" :key="column.key" class="number" :data-label="column.label">
                        {{ statementValue(row.values[column.key]) }}
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </template>

          </div>
        </section>
      </section>
    </div>
  </section>
</template>

<style scoped>
.reports-page { min-height: 100%; }
[id][tabindex="-1"] { scroll-margin-top: 76px; }
.reports-content { width: min(calc(100% - 48px), 1320px); margin: 0 auto; padding: 25px 0 46px; }
.state-panel { display: grid; gap: 7px; padding: 28px; border: 1px solid var(--line); border-radius: var(--radius-panel); background: var(--surface);  }
.state-panel span { color: var(--muted); }
.state-panel.error { border-color: var(--danger); }
.state-panel button { width: fit-content; min-height: 40px; margin-top: 8px; padding: 0 14px; border: 0; border-radius: var(--radius-control); background: var(--accent); color: var(--surface); cursor: pointer; }
.report-dashboard { display: grid; min-width: 0; gap: 12px; }
.report-hero { padding: 23px 25px; border: 1px solid color-mix(in srgb, var(--accent) 20%, var(--line)); border-radius: 20px; background: radial-gradient(circle at 7% 12%, color-mix(in srgb, var(--accent) 11%, transparent), transparent 32%), linear-gradient(125deg, var(--surface), color-mix(in srgb, var(--accent-soft) 66%, var(--surface)));  }
.report-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; }
.report-heading .dashboard-hero-note { max-width: 710px; }
.report-readiness-title { font-size: clamp(28px, 3.3vw, 36px); }
.report-readiness-title.ready { color: color-mix(in srgb, var(--accent) 82%, var(--text)); }
.report-actions { display: flex; flex: 0 0 auto; gap: 8px; }
.report-actions button { min-height: 38px; padding: 0 13px; border: 1px solid var(--accent); border-radius: var(--radius-control); background: var(--accent); color: var(--surface); cursor: pointer; font-weight: 750; }
.report-actions button.secondary { background: var(--surface); color: var(--accent); }
.report-actions button:disabled { cursor: default; opacity: .5; }
.report-readiness-help { position: relative; display: inline-block; }
.report-readiness-trigger { padding: 0; border: 0; background: transparent; color: inherit; cursor: help; font: inherit; letter-spacing: inherit; text-align: left; }
.report-readiness-trigger:focus-visible { border-radius: 4px; outline: 2px solid var(--accent); outline-offset: 3px; }
.report-readiness-tooltip {
  position: absolute;
  top: calc(100% + 9px);
  left: 0;
  z-index: 20;
  display: grid;
  width: min(420px, calc(100vw - 48px));
  gap: 5px;
  padding: 11px 13px;
  border: 1px solid color-mix(in srgb, var(--accent) 22%, var(--line));
  border-radius: var(--radius-control);
  background: var(--surface);
  box-shadow: var(--shadow-overlay);
  opacity: 0;
  color: var(--text);
  font-size: 12px;
  font-weight: 400;
  letter-spacing: normal;
  line-height: 1.55;
  pointer-events: none;
  text-align: left;
  transform: translateY(-4px);
  transition: opacity 140ms ease, transform 140ms ease, visibility 140ms ease;
  visibility: hidden;
}
.report-readiness-tooltip > span { color: var(--muted); }
.report-readiness-help:hover .report-readiness-tooltip,
.report-readiness-help:focus-within .report-readiness-tooltip { opacity: 1; transform: translateY(0); visibility: visible; }
.export-notice { margin-top: 8px; padding: 10px 13px; border-radius: var(--radius-control); background: var(--accent-soft); color: var(--accent); font-size: 12px; }
.export-notice.attention { background: var(--warning-soft); color: var(--warning); }
.export-notice.error { background: var(--danger-soft); color: var(--danger); }
.summary-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 20px 28px;
  overflow: visible;

  border: 0;

  border-radius: 0;

  background: transparent;

  grid-column: 1 / -1;

  margin-top: 8px;
}
.summary-grid article { position: relative; display: grid; min-height: 0; align-content: start; gap: 6px; overflow: hidden; padding: 0; border: 0; border-radius: 0; background: transparent;
  min-width: 0;

  border-left: 0;

  grid-template-rows: auto auto 1fr;
}
.summary-grid span, .summary-grid small { color: var(--muted); }
.summary-grid span { display: block; font-size: 12px; font-weight: 750; }
.summary-grid small { font-size: 11px; }
.summary-grid strong { display: block; margin: 4px 0; color: var(--text); font-size: clamp(20px, 2vw, 26px); line-height: 1.15; letter-spacing: -.025em;
  font-variant-numeric: tabular-nums;

  overflow-wrap: anywhere;
}
.draft-note { margin: 0; padding: 10px 13px; border-radius: var(--radius-control); background: var(--warning-soft); color: var(--warning); font-size: 12px; }
.report-review { min-width: 0; padding: 0; border: 0; border-radius: var(--radius-panel); background: transparent;
  margin-top: 28px;
}
.review-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; color: var(--muted); font-size: 12px; }
.review-heading > div { display: grid; gap: 3px; }
.review-heading > div > strong { color: var(--text); font-size: 16px; }
.statement-buttons { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 14px; }
.statement-buttons button { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; min-height: 88px; align-items: center; gap: 12px; padding: 14px 15px; border: 1px solid var(--line); border-radius: var(--radius-control); background: var(--surface-soft); color: var(--text); cursor: pointer; text-align: left; transition: border-color .16s ease, background .16s ease, transform .16s ease; }
.statement-buttons button:hover { border-color: color-mix(in srgb, var(--accent) 45%, var(--line)); transform: none; }
.statement-buttons button:focus-visible { outline: 3px solid color-mix(in srgb, var(--accent) 28%, transparent); outline-offset: 2px; }
.statement-buttons button.active { border-color: var(--accent); background: var(--accent-soft); color: var(--accent); }
.statement-number { display: grid; width: 38px; height: 38px; place-items: center; border-radius: var(--radius-control); background: var(--surface); color: var(--muted); font-size: 11px; font-weight: 850; }
.statement-buttons button.active .statement-number { background: var(--accent); color: var(--surface); }
.statement-button-copy { display: grid; gap: 4px; min-width: 0; }
.statement-button-copy strong { font-size: 16px; }
.statement-button-copy small { color: var(--muted); font-size: 11px; }
.statement-arrow { color: var(--muted); font-size: 18px; }
.statement-buttons button.active .statement-arrow { color: var(--accent); }
.report-full { min-width: 0; margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--line); }
.table-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin: 0 0 10px; }
.table-wrap { min-width: 0; max-width: 100%; overflow-x: auto; border: 1px solid var(--line); border-radius: var(--radius-control); }
table { width: 100%; min-width: 0; border-collapse: collapse; background: var(--surface); font-size: 12px; }
th, td { padding: 10px 11px; border-bottom: 1px solid var(--line); text-align: left; }
th { position: sticky; top: 0; background: var(--surface-soft); color: var(--muted); font-size: 11px; }
th:first-child { width: 54%; }
.number { text-align: right; font-variant-numeric: tabular-nums; }
tr.total td { background: var(--surface-soft); font-weight: 750; }
@media (max-width: 960px) { .statement-buttons { grid-template-columns: 1fr; } }
@media (max-width: 900px) { .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 760px) { .reports-content { width: min(calc(100% - 24px), 1320px); padding: 16px 0 24px; } .report-hero { padding: 19px; border-radius: 17px; } .report-heading { flex-direction: column; gap: 13px; } .report-actions { width: 100%; } .report-actions button { min-height: 44px; flex: 1; } .report-review { padding: 0; } .review-heading, .table-toolbar { align-items: flex-start; flex-direction: column; } .statement-buttons button { min-height: 64px; } }


.report-hero .summary-grid { margin-top: 32px; }
.report-hero .summary-grid > * { min-height: 0; padding: 0; border: 0; background: transparent; }
.report-hero .summary-grid strong { font-variant-numeric: tabular-nums; }
@media (max-width: 760px) {
  .summary-grid, .report-hero .summary-grid { grid-template-columns: minmax(0, 1fr); gap: 20px; }
  .table-wrap { overflow: hidden; }
  table, tbody, tr, td { display: block; width: auto; min-width: 0; }
  thead { display: none; }
  tr { padding: 12px; border-bottom: 1px solid var(--line); }
  td { border: 0; padding: 5px 0; overflow-wrap: anywhere; }
  td:first-child { font-weight: 750; }
  td[data-label] { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 12px; }
  td[data-label]::before { content: attr(data-label); color: var(--muted); text-align: left; }
}
.template-switch { position: relative; display: flex; align-items: center; gap: 10px; color: var(--muted); cursor: pointer; }
.switch-copy { display: grid; justify-items: end; gap: 1px; }
.switch-copy strong { color: var(--text); font-size: 12px; }
.switch-copy small { font-size: 10px; }
.template-switch input { position: absolute; width: 1px; height: 1px; opacity: 0; }
.switch-track { position: relative; width: 42px; height: 24px; flex: 0 0 auto; border: 1px solid var(--line); border-radius: 999px; background: var(--surface-soft); transition: border-color .16s ease, background .16s ease; }
.switch-track span { position: absolute; top: 3px; left: 3px; width: 16px; height: 16px; border-radius: 50%; background: var(--muted); box-shadow: 0 1px 3px rgb(0 0 0 / 18%); transition: transform .16s ease, background .16s ease; }
.template-switch input:checked + .switch-track { border-color: var(--accent); background: var(--accent); }
.template-switch input:checked + .switch-track span { background: var(--surface); transform: translateX(18px); }
.template-switch input:focus-visible + .switch-track { outline: 3px solid color-mix(in srgb, var(--accent) 28%, transparent); outline-offset: 2px; }
.template-wrap { min-width: 0; max-width: 100%; overflow-x: auto; padding: 14px; border: 1px solid #cbd5e1; border-radius: var(--radius-control); background: #e9edf1; }
.tax-template-sheet { width: 780px; box-sizing: border-box; padding: 26px 30px 32px; background: #fff; color: #171717; box-shadow: 0 2px 10px rgb(15 23 42 / 10%); font-family: SimSun, "Songti SC", serif; }
.tax-template-sheet.balance-sheet { width: 1120px; }
.template-title-row { position: relative; display: flex; min-height: 76px; align-items: center; justify-content: center; padding-bottom: 14px; }
.template-title-row h3 { margin: 0; font-family: inherit; font-size: 20px; font-weight: 700; letter-spacing: .02em; text-align: center; white-space: nowrap; }
.template-title-row span { position: absolute; right: 0; bottom: 7px; color: #4b5563; font-size: 12px; white-space: nowrap; }
.template-meta-grid { display: grid; grid-template-columns: 135px minmax(190px, 1fr) 135px minmax(190px, 1fr); border-top: 2px solid #444; border-left: 2px solid #444; font-size: 12px; }
.template-meta-grid > * { min-height: 38px; display: flex; align-items: center; justify-content: center; padding: 7px 10px; border-right: 1px solid #555; border-bottom: 1px solid #555; }
.template-meta-grid > :nth-child(4n) { border-right-width: 2px; }
.template-meta-grid > :nth-last-child(-n + 4) { border-bottom-width: 2px; }
.template-meta-label { background: #e0e0e0; font-weight: 400; }
.template-meta-grid strong { min-width: 0; justify-content: flex-start; overflow-wrap: anywhere; background: #f8f8f8; font-family: Arial, "Microsoft YaHei", sans-serif; font-weight: 500; }
.tax-template-table { width: 100%; min-width: 0; margin: 0; border-collapse: collapse; border-right: 2px solid #444; border-bottom: 2px solid #444; background: #fff; color: #171717; font-size: 12px; table-layout: fixed; }
.tax-template-table th, .tax-template-table td { position: static; height: 36px; padding: 6px 8px; border-right: 1px solid #555; border-bottom: 1px solid #555; background: #fff; color: #171717; font-weight: 400; line-height: 1.35; }
.tax-template-table th { background: #e0e0e0; font-weight: 700; text-align: center; }
.tax-template-table th:first-child { width: 46%; }
.balance-template-table th:first-child, .balance-template-table th:nth-child(5) { width: 22%; }
.balance-template-table th:nth-child(2), .balance-template-table th:nth-child(6) { width: 4.5%; }
.balance-template-table th:nth-child(3), .balance-template-table th:nth-child(4), .balance-template-table th:nth-child(7), .balance-template-table th:nth-child(8) { width: 11.75%; }
.tax-template-table .line { color: #171717; text-align: center; }
.tax-template-table .number { color: #374151; text-align: right; }
.tax-template-table .negative { color: #b42318; }
.tax-template-table td.section, .tax-template-table td.blank, .tax-template-table td.total, .tax-template-table tr.total td, .template-section-row td { background: #e0e0e0; color: #171717; }
.tax-template-table td.total, .tax-template-table tr.total td, .template-section-row td { font-weight: 700; }
.template-item { text-align: left; }

.mobile-template-table { display: none; }
@media (max-width: 760px) {
  .template-switch { width: 100%; justify-content: space-between; }
  .switch-copy { justify-items: start; }
  .template-wrap { overflow: hidden; padding: 8px; }
  .tax-template-sheet, .tax-template-sheet.balance-sheet { width: 100%; padding: 12px 8px; }
  .template-title-row { flex-direction: column; gap: 8px; min-height: 0; }
  .template-title-row h3 { font-size: 16px; white-space: normal; overflow-wrap: anywhere; }
  .template-title-row span { position: static; white-space: normal; }
  .template-meta-grid { grid-template-columns: 94px minmax(0, 1fr); }
  .desktop-template-table { display: none; }
  .mobile-template-table { display: block; }
  .mobile-template-table td { height: auto; border: 0; padding: 5px 0; }
  .mobile-template-table td[data-label] { grid-template-columns: minmax(0, 1fr) minmax(0, 1.4fr); }
  .mobile-template-table td[data-label]::before { color: #4b5563; }
}
</style>
