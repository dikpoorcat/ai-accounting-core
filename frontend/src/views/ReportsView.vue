<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";

import { DashboardApiError, dashboardErrorMessage } from "../api/client";
import { businessStateLabel } from "../api/dashboardContracts";
import { fetchLocalJob, LocalApiError } from "../api/localKernel";
import {
  fetchQuarterlyReport,
  fetchQuarterlyWorkbook,
  requestQuarterlyExport,
  type QuarterlyReport,
  type ReportStatement,
  type ReportStatementRow,
} from "../api/reports";
import DashboardModuleHeader from "../components/DashboardModuleHeader.vue";
import DashboardSectionNav from "../components/DashboardSectionNav.vue";
import PeriodPreparation from "../components/PeriodPreparation.vue";
import { useDashboardContext } from "../composables/useDashboardContext";
import { useDashboardSections } from "../composables/useDashboardSections";
import { formatFen } from "../utils/money";

interface SummaryCard {
  source: string;
  label: string;
  value: string;
  note: string;
}

interface TechnicalRow {
  label: string;
  value: string | string[];
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
const report = ref<QuarterlyReport | null>(null);
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

function selectionKey() { return JSON.stringify([route.query.company_id, route.query.period, route.query.quarter, route.query.carry_forward_fact_id]); }
function isCurrent(generation: number, selection: string) { return mounted && generation === requestGeneration && selectionKey() === selection; }
function invalidateRequests() {
  requestGeneration += 1;
  previewController?.abort(); exportController?.abort();
  previewController = null; exportController = null;
  report.value = null; loading.value = false; exporting.value = false;
  exportNotice.value = ""; errorMessage.value = "";
}

const quarterOptions = computed(() =>
  (context.value?.quarters ?? []).map((quarter) => ({
    key: quarter.key,
    label: quarter.label,
    status: quarter.key === selectedQuarter.value && report.value
      ? report.value.close_state : quarter.complete ? "closed" : "open",
  })),
);
const reportStateClass = computed(() => report.value?.status.replace("_", "-") ?? "");
const pendingReadiness = computed(() => report.value?.readiness.filter((item) => item.state !== "pass") ?? []);
const readinessGroups = computed(() => {
  const completed = report.value?.readiness.filter((item) => item.state === "pass") ?? [];
  return [
    { key: "pending", label: `需要核对的事项（${pendingReadiness.value.length}）`, expanded: true, items: pendingReadiness.value },
    { key: "completed", label: `已完成 ${completed.length} 项检查`, expanded: false, items: completed },
  ].filter((group) => group.items.length);
});
const monthlyPreparations = computed(() => (report.value?.period_preparations ?? []).map(preparation => {
  const current = preparation.current_followups;
  const issueGroups = [
    { label: "月末核算条件", issues: preparation.readiness?.issues ?? [] },
    { label: "资料", issues: current.materials.issues },
    { label: "核算", issues: current.accounting.issues },
    { label: "业务条件", issues: current.close_requirements.issues },
    { label: "款项来源", issues: current.settlements.issues ?? [] },
    { label: "外部办理", issues: current.external.fact_issues ?? [] },
  ].filter(group => group.issues.length);
  const notices: string[] = [];
  if (preparation.readiness?.order_failure) notices.push("月末核算顺序尚需核对");
  if (current.accounting.unpublished_count) notices.push(`${current.accounting.unpublished_count} 项业务尚未发布`);
  if (current.settlements.complete === false || [current.settlements.source_amount_fen, current.settlements.paid_fen, current.settlements.other_settled_fen, current.settlements.remaining_fen].some(value => value === null)) notices.push("相关款项金额尚不能完整确定");
  if (current.settlements.unestablished_state_selection_count) notices.push(`${current.settlements.unestablished_state_selection_count} 组来源尚不能证明封存采用`);
  if (current.file_jobs.issue_count) notices.push(`${current.file_jobs.issue_count} 项文件任务来源待核对`);
  if (current.file_jobs.status_counts.failed) notices.push(`${current.file_jobs.status_counts.failed} 项文件任务失败`);
  const closure = preparation.closure.state === "exact_close" ? "已关账"
    : preparation.closure.state === "sealed_by_later_close" ? `由 ${preparation.closure.sealing_boundary} 后续关账封存` : "尚未关账";
  return { preparation, closure, issueGroups, notices, statuses: [
    `资料：${businessStateLabel(current.materials.status)}`, `核算：${businessStateLabel(current.accounting.status)}`,
    `业务条件：${businessStateLabel(current.close_requirements.status)}`, `款项：${businessStateLabel(current.settlements.status)}`,
    `外部办理：${businessStateLabel(current.external.status)}`,
  ].join(" · ") };
}));
const sectionLinks = computed(() => {
  if (!report.value) return [];
  return [
    { id: "report-overview", label: "概览" },
    ...(report.value.carry_forward.options.length || readinessGroups.value.length ? [{ id: "report-checks", label: "核对事项" }] : []),
    ...(monthlyPreparations.value.length ? [{ id: "report-months", label: "各月跟进" }] : []),
    ...(report.value.statements.length ? [{ id: "report-statements", label: "财务报表" }] : []),
  ];
});
const { activeSection, focusSection } = useDashboardSections(sectionLinks, "report-overview");
const reportHeadline = computed(() => {
  if (needsRegeneration.value) return "报表需要重新生成";
  if (report.value?.export.available) return "本季度报表已准备好";
  if (pendingReadiness.value.length) return `还有 ${pendingReadiness.value.length} 项需要核对`;
  if (report.value?.close_state === "open") return "相关月份结账后可下载报表";
  return "本季度报表暂时无法下载";
});
const reportNextStep = computed(() => {
  if (needsRegeneration.value) return "请根据提示重新生成。";
  if (report.value?.export.available) return "可下载 Excel 报表，使用前请复核。";
  if (pendingReadiness.value.length) return "核对下方事项后刷新报表。";
  if (report.value?.close_state === "open") return "当前为试算金额，相关月份结账后刷新。";
  return report.value?.message ?? "";
});
const needsCarryForward = computed(() => report.value?.technical.requirement_codes.includes("report_carry_forward") ?? false);
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
  const cards = [
    {
      source: "资产负债表",
      label: "资产合计",
      primary: summary.assets_total_fen,
      noteLabel: "负债和所有者权益",
      secondary: summary.liabilities_equity_total_fen,
    },
    {
      source: "利润表",
      label: "本季度净利润",
      primary: summary.current_net_profit_fen,
      noteLabel: "本年累计",
      secondary: summary.year_to_date_net_profit_fen,
    },
    {
      source: "现金流量表",
      label: "本季度现金净增加额",
      primary: summary.current_cash_change_fen,
      noteLabel: "期末现金",
      secondary: summary.ending_cash_fen,
    },
  ];
  return cards
    .map((item) => ({
      source: item.source,
      label: item.label,
      value: formatFen(item.primary),
      note: `${item.noteLabel} ${formatFen(item.secondary)}`,
    }));
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
const technicalRows = computed<TechnicalRow[]>(() => {
  const technical = report.value?.technical;
  if (!technical) return [];
  const rows: Array<TechnicalRow | null> = [
    technical.calculation_hash
      ? { label: "计算哈希", value: technical.calculation_hash }
      : null,
    technical.template.file_name
      ? { label: "Excel 文件", value: technical.template.file_name }
      : null,
    technical.template.profile
      ? { label: "模板版本", value: technical.template.profile }
      : null,
    technical.template.sha256
      ? { label: "模板 SHA-256", value: technical.template.sha256 }
      : null,
    technical.rule.version ? { label: "计算规则", value: technical.rule.version } : null,
    { label: "结账快照", value: `${technical.source_close_hashes.length} 份` },
    { label: "报表分类", value: technical.classification_count === null ? "未提供" : `${technical.classification_count} 项` },
    { label: "所得税确认", value: technical.income_tax_confirmation_count === null ? "未提供" : `${technical.income_tax_confirmation_count} 项` },
    technical.requirement_codes.length
      ? { label: "待办代码", value: technical.requirement_codes }
      : null,
    technical.errors.length ? { label: "错误代码", value: technical.errors } : null,
  ];
  return rows.filter((item): item is TechnicalRow => item !== null);
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
    if (route.query.carry_forward_fact_id && selectedQuarter.value && target !== selectedQuarter.value) {
      await router.replace({ query: { ...route.query, carry_forward_fact_id: undefined }, hash: route.hash });
      return;
    }
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

async function preview(quarterKey: string) {
  const generation = ++requestGeneration, selection = selectionKey();
  previewController?.abort();
  exportController?.abort();
  exportController = null; exporting.value = false;
  const controller = new AbortController();
  previewController = controller;
  const match = /^(\d{4})-Q([1-4])$/.exec(quarterKey);
  if (!match) {
    errorMessage.value = "请选择已有会计期间对应的季度。";
    return;
  }
  selectedQuarter.value = quarterKey;
  report.value = null;
  errorMessage.value = "";
  exportNotice.value = "";
  needsRegeneration.value = false;
  loading.value = true;
  try {
    const result = await fetchQuarterlyReport(
      Number(match[1]),
      Number(match[2]),
      controller.signal,
      typeof route.query.carry_forward_fact_id === "string" ? route.query.carry_forward_fact_id : undefined,
    );
    if (!isCurrent(generation, selection) || previewController !== controller) return;
    report.value = result;
    if (!result.statements.some((item) => item.key === activeStatementKey.value)) {
      activeStatementKey.value = "";
      statementsExpanded.value = false;
    }
  } catch (error: unknown) {
    if (isCurrent(generation, selection) && previewController === controller) {
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

function changeCarryForward(value: string) {
  void router.replace({ query: { ...route.query, carry_forward_fact_id: value || undefined } });
}

async function refresh() {
  invalidateRequests();
  const generation = requestGeneration, selection = selectionKey();
  try {
    await refreshContext();
    if (!isCurrent(generation, selection)) return;
    await synchronizeQuarter(true);
  } catch (error: unknown) {
    if (!isCurrent(generation, selection)) return;
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
    if (!isCurrent(generation, selection) || exportController !== controller) return;
    exportNotice.value = "报表正在生成，完成后将开始下载。离开页面后，仍可在“文件与处理进度”中查看结果。";
    while (!controller.signal.aborted) {
      const [job] = await fetchLocalJob(companyId, attempt.jobId, controller.signal);
      if (!isCurrent(generation, selection) || exportController !== controller) return;
      if (job?.status === "succeeded") break;
      if (!job || (job.status === "failed" && job.attempts >= 3)) throw new DashboardApiError(409, "REPORT_JOB_FAILED", "原报表任务无法继续生成，请重新生成。");
      if (job.status === "failed") exportNotice.value = "本次生成未成功，正在等待自动重试。";
      await new Promise<void>((resolve, reject) => {
        const abort = () => { clearTimeout(timer); reject(new DOMException("Aborted", "AbortError")); };
        const timer = setTimeout(() => { controller.signal.removeEventListener("abort", abort); resolve(); }, 1200);
        controller.signal.addEventListener("abort", abort, { once: true });
      });
    }
    if (!isCurrent(generation, selection) || controller.signal.aborted || exportController !== controller) return;
    const blob = await fetchQuarterlyWorkbook(companyId, attempt.jobId, controller.signal);
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
  () => [route.query.company_id, route.query.period, route.query.quarter, route.query.carry_forward_fact_id],
  (value, previous) => {
    if (value.every((item, index) => item === previous[index])) return;
    invalidateRequests();
  },
  { flush: "sync" },
);
watch(
  () => [context.value?.current_company?.company_id, route.query.period, route.query.quarter, route.query.carry_forward_fact_id] as const,
  ([orgId, , , source], [previousOrgId, , , previousSource]) => {
    if (mounted && orgId) void synchronizeQuarter(orgId !== previousOrgId || source !== previousSource);
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
      />
      <DashboardSectionNav v-if="sectionLinks.length" :items="sectionLinks" :active="activeSection" label="报表内容导航" floating @select="focusSection" />

      <section v-if="!quarterOptions.length && !loading" class="state-panel">
        <strong>还没有可查看的报表</strong>
        <span>公司开始记账后，可在这里查看对应季度的财务报表。</span>
      </section>

      <section v-else-if="loading && !report" class="state-panel" aria-live="polite">
        <strong>正在整理本季度报表</strong>
        <span>正在读取账务和核对资料，请稍候…</span>
      </section>

      <section v-else-if="errorMessage && !report" class="state-panel error" role="alert">
        <strong>季度报表读取失败</strong>
        <span>{{ errorMessage }}</span>
        <button type="button" @click="refresh">刷新报表</button>
        <button v-if="route.query.carry_forward_fact_id" type="button" @click="changeCarryForward('')">采用默认来源重新核对</button>
      </section>

      <section v-else-if="report" class="report-dashboard">
        <section id="report-overview" class="report-hero" tabindex="-1" aria-labelledby="report-readiness-title">
          <div class="report-heading">
            <div>
              <p class="eyebrow">{{ report.period.label }}</p>
              <h2 id="report-readiness-title" aria-live="polite">{{ reportHeadline }}</h2>
              <p>{{ reportNextStep }}</p>
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

          <div class="status-meta">
            <span class="status-badge" :class="reportStateClass">{{ report.export.available ? "可生成下载" : "暂不可下载" }}</span>
            <span>更新于 {{ checkedAt }}</span>
          </div>

          <details class="report-state" :class="reportStateClass">
            <summary>查看报表准备详情</summary>
            <p>{{ report.close_state === "closed" ? "相关月份已结账" : "相关月份尚未全部结账" }} · {{ report.readiness_state === "ready" ? "报表资料已核对" : "报表资料待核对" }}</p>
            <p>{{ report.message }}</p>
          </details>

          <div v-if="exportNotice" class="export-notice" :class="exportNoticeKind" role="status">
            {{ exportNotice }}
          </div>
        </section>

        <section v-if="summaryCards.length" class="summary-grid" aria-label="季度报表摘要">
          <article v-for="item in summaryCards" :key="item.source">
            <span>{{ item.source }} · {{ item.label }}</span>
            <strong>{{ item.value }}</strong>
            <span>{{ item.note }}</span>
          </article>
        </section>

        <section v-if="report.carry_forward.options.length || readinessGroups.length" id="report-checks" class="report-checks" tabindex="-1" aria-label="报表核对事项">
          <details v-if="report.carry_forward.options.length" class="panel report-source" :open="needsCarryForward">
            <summary>报表来源{{ report.carry_forward.selected_fact_id ? ' · 已指定接账前资料' : '' }}</summary>
            <label>接账前累计资料
              <select :value="report.carry_forward.selected_fact_id || ''" :disabled="loading || exporting" @change="changeCarryForward(($event.target as HTMLSelectElement).value)">
                <option value="">采用现有报表资料</option>
                <option v-for="source in report.carry_forward.options" :key="source.fact_id" :value="source.fact_id">{{ source.label }} · {{ source.evidence_count }} 份附件</option>
              </select>
            </label>
            <p>页面与下载文件采用同一份资料。</p>
            <details><summary>供核对的资料版本</summary><ul><li v-for="source in report.carry_forward.options" :key="source.fact_id">{{ source.label }}{{ source.used ? ' · 本次已采用' : '' }}：{{ source.fact_id }}</li></ul></details>
          </details>

          <details v-for="group in readinessGroups" :key="group.key" class="readiness-group" :open="group.expanded">
            <summary>{{ group.label }}</summary>
            <div class="readiness">
            <article
              v-for="item in group.items"
              :key="item.key"
              class="readiness-item"
              :class="item.state"
            >
              <div class="readiness-head">
                <span>{{ item.state === "pass" ? "✓" : item.state === "pending" ? "…" : "!" }}</span>
                <strong>{{ item.label }}</strong>
              </div>
              <p>{{ item.summary }}</p>
              <ul v-if="item.details.length">
                <li v-for="detail in item.details" :key="`${detail.primary}-${detail.secondary}`">
                  <div><strong>{{ detail.primary }}</strong><span>{{ detail.secondary }}</span></div>
                  <RouterLink v-if="detail.location?.voucher_number !== undefined && detail.location?.period" :to="{ path: '/', query: { company_id: route.query.company_id, period: detail.location.period, voucher: String(detail.location.voucher_number) } }">查看相关凭证</RouterLink>
                  <details v-if="detail.location"><summary>供核对的详细信息</summary><pre>{{ JSON.stringify(detail.location, null, 2) }}</pre></details>
                  <strong v-if="detail.amount_fen != null">{{ formatFen(detail.amount_fen) }}</strong>
                </li>
              </ul>
            </article>
            </div>
          </details>
        </section>

        <p v-if="report.draft" class="draft-note">
          试算金额可能随资料变化，暂不能下载。
        </p>

        <section v-if="monthlyPreparations.length" id="report-months" class="monthly-preparations" tabindex="-1" aria-labelledby="report-months-title">
          <h3 id="report-months-title">各月核算与跟进</h3>
          <p class="monthly-scope">全公司当月事项；问题、业务和文件任务分别计数。</p>
          <details v-for="month in monthlyPreparations" :key="month.preparation.period" class="monthly-preparation">
            <summary>
              <span class="monthly-heading"><strong>{{ month.preparation.period }} · {{ month.closure }}</strong><span>展开本月依据</span></span>
              <span class="monthly-statuses">{{ month.statuses }}</span>
              <span v-if="month.notices.length" class="monthly-alert">{{ month.notices.join('；') }}</span>
              <span v-for="group in month.issueGroups" :key="group.label" class="monthly-issue"><strong>{{ group.label }} · {{ group.issues.length }} 条问题：</strong>{{ group.issues[0].message || '相关来源需要核对，展开查看完整依据。' }}</span>
            </summary>
            <PeriodPreparation :preparation="month.preparation" @changed="refresh" />
          </details>
        </section>

        <section v-if="report.statements.length" id="report-statements" class="report-review" tabindex="-1" aria-label="完整财务报表">
          <div class="review-heading">
            <div>
              <strong>完整财务报表</strong>
              <span class="table-scroll-hint">表格可左右滑动</span>
            </div>
            <span class="check-summary">
              {{ report.checks.total ? `${report.checks.passed} / ${report.checks.total} 项数字核对通过` : "暂无数字核对结果" }}
            </span>
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
                    <strong>{{ report.organization?.name || context?.company || "—" }}</strong>
                    <span class="template-meta-label">所属期起</span>
                    <strong>{{ templateQuarterStart }}</strong>
                    <span class="template-meta-label">所属期止</span>
                    <strong>{{ report.period.quarter_end }}</strong>
                  </div>

                  <table
                    v-if="activeStatement.key === 'balance_sheet'"
                    class="tax-template-table balance-template-table"
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

                  <table v-else class="tax-template-table">
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
                </div>
              </div>

              <div v-else class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>项目</th><th class="line">行次</th>
                      <th v-for="column in activeStatement.columns" :key="column.key" class="number">
                        {{ column.label }}
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="row in visibleStatementRows" :key="row.line" :class="{ total: row.is_total }">
                      <td>{{ row.name }}</td><td class="line">{{ row.line }}</td>
                      <td v-for="column in activeStatement.columns" :key="column.key" class="number">
                        {{ statementValue(row.values[column.key]) }}
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </template>

            <details class="disclosure">
              <summary>
                <strong>报表数字核对</strong>
                <span :class="{ failed: report.checks.passed !== report.checks.total }">
                  {{ report.checks.passed }} / {{ report.checks.total }} 项通过
                </span>
              </summary>
              <div class="check-list">
                <div v-for="item in report.checks.items" :key="item.code" :class="{ failed: item.passed === false }">
                  <span>{{ item.passed === null ? "—" : item.passed ? "✓" : "×" }}</span><strong>{{ item.label }}{{ item.passed === null ? '（依据不完整）' : '' }}</strong>
                </div>
              </div>
            </details>

            <details class="disclosure technical">
              <summary><strong>供核对的技术信息</strong></summary>
              <dl>
                <template v-for="item in technicalRows" :key="item.label">
                  <dt>{{ item.label }}</dt>
                  <dd v-if="Array.isArray(item.value)"><ul><li v-for="value in item.value" :key="value">{{ value }}</li></ul></dd>
                  <dd v-else>{{ item.value }}</dd>
                </template>
              </dl>
            </details>
          </div>
        </section>
      </section>
    </div>
  </section>
</template>

<style scoped>
.reports-page { min-height: 100%; }
.report-source { padding: 18px; border: 1px solid var(--line); border-radius: 16px; background: var(--surface); }
.report-checks { display: grid; min-width: 0; gap: 12px; }
[id][tabindex="-1"] { scroll-margin-top: 76px; }
.table-scroll-hint { display: none; }
.report-source > summary, .readiness-group > summary { color: var(--text); font-size: 13px; font-weight: 700; cursor: pointer; }
.report-source label { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-top: 14px; font-weight: 700; }
.report-source select { max-width: 100%; min-height: 38px; padding: 6px 10px; border: 1px solid var(--line); border-radius: 8px; color: var(--text); background: var(--surface); }
.report-source p, .report-source details { font-size: 13px; color: var(--muted); overflow-wrap: anywhere; }
.readiness pre { white-space: pre-wrap; overflow-wrap: anywhere; max-width: 100%; }
.readiness-group > summary { padding: 8px 0; }
.reports-content { width: min(calc(100% - 48px), 1320px); margin: 0 auto; padding: 25px 0 46px; }
.state-panel { display: grid; gap: 7px; padding: 28px; border: 1px solid var(--line); border-radius: 18px; background: var(--surface); box-shadow: var(--shadow-soft); }
.state-panel span { color: var(--muted); }
.state-panel.error { border-color: var(--danger); }
.state-panel button { width: fit-content; min-height: 40px; margin-top: 8px; padding: 0 14px; border: 0; border-radius: 10px; background: var(--accent); color: var(--surface); cursor: pointer; }
.monthly-preparations > h3 { font-size: 16px; margin: 8px 0; }
.monthly-scope, .monthly-statuses { color: var(--muted); font-size: 12px; line-height: 1.6; }
.monthly-scope { margin: 6px 0 10px; }
.monthly-preparation { margin-top: 8px; border: 1px solid var(--line); border-radius: 12px; background: var(--surface); }
.monthly-preparation > summary { padding: 12px 14px; cursor: pointer; overflow-wrap: anywhere; }
.monthly-preparation > summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.monthly-heading { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 5px 14px; min-height: 28px; font-size: 14px; }
.monthly-heading > span { color: var(--accent); font-size: 12px; }
.monthly-statuses, .monthly-alert, .monthly-issue { display: block; margin-top: 5px; }
.monthly-alert, .monthly-issue { color: var(--warning); font-size: 12px; line-height: 1.6; }
.monthly-preparation > :deep(.period-preparation) { margin: 0; border: 0; border-top: 1px solid var(--line); border-radius: 0 0 12px 12px; }
.report-dashboard { display: grid; min-width: 0; gap: 12px; }
.report-hero { padding: 23px 25px; border: 1px solid color-mix(in srgb, var(--accent) 20%, var(--line)); border-radius: 20px; background: radial-gradient(circle at 7% 12%, color-mix(in srgb, var(--accent) 11%, transparent), transparent 32%), linear-gradient(125deg, var(--surface), color-mix(in srgb, var(--accent-soft) 66%, var(--surface))); box-shadow: var(--shadow-soft); }
.report-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; }
.eyebrow { margin: 0 0 5px; color: var(--accent); font-size: 11px; font-weight: 850; letter-spacing: .08em; }
.report-heading h2 { margin: 0; font-size: clamp(25px, 2.8vw, 34px); letter-spacing: -.04em; }
.report-heading p { max-width: 710px; margin: 5px 0 0; color: var(--muted); font-size: 12px; }
.report-actions { display: flex; flex: 0 0 auto; gap: 8px; }
.report-actions button { min-height: 38px; padding: 0 13px; border: 1px solid var(--accent); border-radius: 10px; background: var(--accent); color: var(--surface); cursor: pointer; font-weight: 750; }
.report-actions button.secondary { background: var(--surface); color: var(--accent); }
.report-actions button:disabled { cursor: default; opacity: .5; }
.status-meta { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 13px; padding-top: 11px; border-top: 1px solid color-mix(in srgb, var(--accent) 18%, var(--line)); color: var(--muted); font-size: 11px; }
.status-badge { padding: 2px 8px; border-radius: 999px; background: var(--surface-soft); color: var(--muted); font-weight: 800; }
.status-badge.ready { background: var(--accent-soft); color: var(--accent); }
.status-badge.blocked, .status-badge.error { background: var(--danger-soft); color: var(--danger); }
.report-state { margin-top: 8px; padding: 11px 13px; border-radius: 11px; background: var(--surface-soft); }
.report-state.ready { background: var(--accent-soft); color: var(--accent); }
.report-state.in-progress { background: var(--info-soft); color: var(--info); }
.report-state.blocked { background: var(--warning-soft); color: var(--warning); }
.report-state.error { background: var(--danger-soft); color: var(--danger); }
.report-state p { margin: 3px 0 0; font-size: 12px; }
.report-state summary { font-size: 12px; cursor: pointer; }
.export-notice { margin-top: 8px; padding: 10px 13px; border-radius: 10px; background: var(--accent-soft); color: var(--accent); font-size: 12px; }
.export-notice.attention { background: var(--warning-soft); color: var(--warning); }
.export-notice.error { background: var(--danger-soft); color: var(--danger); }
.readiness { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr)); gap: 10px; }
.readiness-item { position: relative; overflow: hidden; padding: 15px 16px; border: 1px solid var(--line); border-radius: 16px; background: var(--surface); box-shadow: var(--shadow-soft); }
.readiness-item::before { position: absolute; top: 0; right: 0; left: 0; height: 3px; background: var(--accent); content: ""; }
.readiness-item.pending::before { background: var(--info); }
.readiness-item.attention::before { background: var(--warning); }
.readiness-item { min-width: 0; overflow-wrap: anywhere; }
.readiness-head { display: flex; align-items: center; gap: 8px; }
.readiness-head > span { display: grid; width: 23px; height: 23px; flex: 0 0 auto; place-items: center; border-radius: 50%; background: var(--accent-soft); color: var(--accent); font-weight: 850; }
.readiness-item.pending .readiness-head > span { background: var(--info-soft); color: var(--info); }
.readiness-item.attention .readiness-head > span { background: var(--warning-soft); color: var(--warning); }
.readiness-item p { margin: 7px 0 0; color: var(--muted); font-size: 11px; }
.readiness-item ul { display: grid; gap: 7px; margin: 10px 0 0; padding: 10px 0 0; border-top: 1px solid var(--line); list-style: none; }
.readiness-item li { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 3px 10px; }
.readiness-item li div { display: grid; gap: 2px; min-width: 0; }
.readiness-item li > a, .readiness-item li > details { grid-column: 1 / -1; min-width: 0; }
.readiness-item li span { color: var(--muted); font-size: 11px; }
.readiness-item li > strong { grid-row: 1 / 3; grid-column: 2; align-self: center; }
.summary-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.summary-grid article { position: relative; display: grid; min-height: 116px; align-content: space-between; gap: 4px; overflow: hidden; padding: 15px 16px; border: 1px solid var(--line); border-radius: 16px; background: var(--surface); box-shadow: var(--shadow-soft); }
.summary-grid article::before { position: absolute; top: 0; right: 0; left: 0; height: 3px; background: var(--info); content: ""; }
.summary-grid article:nth-child(2)::before { background: var(--accent); }
.summary-grid article:nth-child(3)::before { background: var(--gold); }
.summary-grid span { color: var(--muted); font-size: 11px; }
.summary-grid strong { color: var(--info); font-size: clamp(20px, 2vw, 27px); }
.summary-grid article:nth-child(2) strong { color: var(--accent); }
.summary-grid article:nth-child(3) strong { color: var(--gold); }
.draft-note { margin: 0; padding: 10px 13px; border-radius: 10px; background: var(--warning-soft); color: var(--warning); font-size: 12px; }
.report-review { min-width: 0; padding: 18px; border: 1px solid var(--line); border-radius: 16px; background: var(--surface); box-shadow: var(--shadow-soft); }
.review-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; color: var(--muted); font-size: 12px; }
.review-heading > div { display: grid; gap: 3px; }
.review-heading > div > strong { color: var(--text); font-size: 16px; }
.check-summary { padding: 5px 9px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); font-weight: 750; }
.statement-buttons { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 14px; }
.statement-buttons button { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; min-height: 88px; align-items: center; gap: 12px; padding: 14px 15px; border: 1px solid var(--line); border-radius: 14px; background: var(--surface-soft); color: var(--text); cursor: pointer; text-align: left; transition: border-color .16s ease, background .16s ease, transform .16s ease; }
.statement-buttons button:hover { border-color: color-mix(in srgb, var(--accent) 45%, var(--line)); transform: translateY(-1px); }
.statement-buttons button:focus-visible { outline: 3px solid color-mix(in srgb, var(--accent) 28%, transparent); outline-offset: 2px; }
.statement-buttons button.active { border-color: var(--accent); background: var(--accent-soft); color: var(--accent); }
.statement-number { display: grid; width: 38px; height: 38px; place-items: center; border-radius: 11px; background: var(--surface); color: var(--muted); font-size: 11px; font-weight: 850; }
.statement-buttons button.active .statement-number { background: var(--accent); color: var(--surface); }
.statement-button-copy { display: grid; gap: 4px; min-width: 0; }
.statement-button-copy strong { font-size: 16px; }
.statement-button-copy small { color: var(--muted); font-size: 11px; }
.statement-arrow { color: var(--muted); font-size: 18px; }
.statement-buttons button.active .statement-arrow { color: var(--accent); }
.report-full { min-width: 0; margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--line); }
.table-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin: 0 0 10px; }
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
.table-wrap { min-width: 0; max-width: 100%; overflow-x: auto; border: 1px solid var(--line); border-radius: 10px; }
table { width: 100%; min-width: 650px; border-collapse: collapse; background: var(--surface); font-size: 12px; }
th, td { padding: 10px 11px; border-bottom: 1px solid var(--line); text-align: left; }
th { position: sticky; top: 0; background: var(--surface-soft); color: var(--muted); font-size: 11px; }
th:first-child { width: 54%; }
.line { width: 58px; color: var(--muted); text-align: center; }
.number { text-align: right; font-variant-numeric: tabular-nums; }
tr.total td { background: var(--accent-soft); font-weight: 750; }
.template-wrap { min-width: 0; max-width: 100%; overflow-x: auto; padding: 14px; border: 1px solid #cbd5e1; border-radius: 10px; background: #e9edf1; }
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
.disclosure { margin-top: 14px; }
.disclosure summary { display: flex; min-height: 36px; align-items: center; gap: 9px; color: var(--accent); cursor: pointer; }
.disclosure summary span { padding: 4px 8px; border-radius: 999px; background: var(--accent-soft); font-size: 11px; }
.disclosure summary span.failed { background: var(--danger-soft); color: var(--danger); }
.check-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-top: 10px; }
.check-list div { display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: center; gap: 9px; padding: 10px 12px; border-radius: 9px; background: var(--surface-soft); }
.check-list div > span { display: grid; width: 22px; height: 22px; place-items: center; border-radius: 50%; background: var(--accent-soft); color: var(--accent); }
.check-list div.failed > span { background: var(--danger-soft); color: var(--danger); }
.technical dl { display: grid; grid-template-columns: minmax(150px, .45fr) minmax(0, 1.55fr); gap: 7px 16px; margin: 12px 0 0; }
.technical dt { color: var(--muted); }
.technical dd { margin: 0; overflow-wrap: anywhere; }
.technical ul { margin: 0; padding-left: 20px; }
@media (max-width: 960px) { .summary-grid, .statement-buttons { grid-template-columns: 1fr; } .table-scroll-hint { display: block; } }
@media (max-width: 760px) { .reports-content { width: min(calc(100% - 24px), 1320px); padding: 16px 0 24px; } .report-hero { padding: 19px; border-radius: 17px; } .report-heading { flex-direction: column; gap: 13px; } .report-actions { width: 100%; } .report-actions button { min-height: 44px; flex: 1; } .readiness, .check-list { grid-template-columns: 1fr; } .report-review { padding: 14px; } .review-heading, .table-toolbar { align-items: flex-start; flex-direction: column; } .switch-copy { justify-items: start; } .statement-buttons button, .disclosure summary { min-height: 64px; } .technical dl { grid-template-columns: 1fr; } }
</style>
