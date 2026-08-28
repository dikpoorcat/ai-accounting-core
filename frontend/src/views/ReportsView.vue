<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { DashboardApiError, dashboardErrorMessage } from "../api/client";
import {
  fetchQuarterlyReport,
  fetchQuarterlyWorkbook,
  type QuarterlyReport,
  type ReportStatement,
} from "../api/reports";
import DashboardModuleHeader from "../components/DashboardModuleHeader.vue";
import { useDashboardContext } from "../composables/useDashboardContext";
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

const route = useRoute();
const router = useRouter();
const { context, load: loadContext } = useDashboardContext();
const selectedQuarter = ref("");
const report = ref<QuarterlyReport | null>(null);
const loading = ref(false);
const exporting = ref(false);
const errorMessage = ref("");
const exportNotice = ref("");
const exportNoticeKind = ref<"success" | "attention" | "error">("success");
const statementsExpanded = ref(false);
const showAllRows = ref(false);
const activeStatementKey = ref("");
let mounted = false;
let previewController: AbortController | null = null;
let exportController: AbortController | null = null;

const quarterOptions = computed(() =>
  (context.value?.quarters ?? []).map((quarter) => ({
    key: quarter.key,
    label: quarter.label,
    status: quarter.complete ? "closed" : "open",
  })),
);
const reportStateClass = computed(() => report.value?.status.replace("_", "-") ?? "");
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
    .filter((item) => item.primary !== null)
    .map((item) => ({
      source: item.source,
      label: item.label,
      value: formatFen(item.primary),
      note: `${item.noteLabel} ${item.secondary === null ? "—" : formatFen(item.secondary)}`,
    }));
});
const activeStatement = computed<ReportStatement | null>(() => {
  if (!report.value) return null;
  return (
    report.value.statements.find((item) => item.key === activeStatementKey.value) ??
    report.value.statements[0] ??
    null
  );
});
const visibleStatementRows = computed(() => {
  if (!activeStatement.value) return [];
  return activeStatement.value.rows.filter(
    (row) => showAllRows.value || row.has_amount || row.is_total,
  );
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
    { label: "报表分类", value: `${technical.classification_count} 项` },
    { label: "所得税确认", value: `${technical.income_tax_confirmation_count} 项` },
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

async function synchronizeQuarter(force = false) {
  try {
    const currentContext = await loadContext();
    if (!currentContext.quarters.length) {
      previewController?.abort();
      exportController?.abort();
      selectedQuarter.value = "";
      report.value = null;
      loading.value = false;
      errorMessage.value = "";
      return;
    }
    const requested = routeQuarter();
    const target = currentContext.quarters.some((item) => item.key === requested)
      ? (requested as string)
      : (currentContext.default_quarter ?? currentContext.quarters.at(-1)?.key ?? "");
    const legacyQuarter = !route.query.quarter && requested === route.query.period;
    if (requested !== target || legacyQuarter) {
      await router.replace({
        query: {
          ...route.query,
          period: legacyQuarter ? (currentContext.default_period ?? undefined) : route.query.period,
          quarter: target,
        },
      });
      return;
    }
    if (force || selectedQuarter.value !== target || report.value === null) {
      await preview(target);
    }
  } catch (error: unknown) {
    const message = dashboardErrorMessage(error);
    if (message) errorMessage.value = message;
  }
}

async function preview(quarterKey: string) {
  previewController?.abort();
  exportController?.abort();
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
  loading.value = true;
  try {
    const result = await fetchQuarterlyReport(
      Number(match[1]),
      Number(match[2]),
      controller.signal,
    );
    if (previewController !== controller) return;
    report.value = result;
    activeStatementKey.value = result.statements[0]?.key ?? "";
    statementsExpanded.value = false;
    showAllRows.value = false;
  } catch (error: unknown) {
    if (previewController === controller) {
      const message = dashboardErrorMessage(error);
      if (message) errorMessage.value = message;
    }
  } finally {
    if (previewController === controller) {
      loading.value = false;
      previewController = null;
    }
  }
}

function changeQuarter(value: string) {
  if (!value || value === routeQuarter()) return;
  void router.push({ query: { ...route.query, quarter: value } });
}

async function refresh() {
  try {
    await loadContext(true);
    await synchronizeQuarter(true);
  } catch (error: unknown) {
    const message = dashboardErrorMessage(error);
    if (message) errorMessage.value = message;
  }
}

async function exportReport() {
  const current = report.value;
  if (!current?.export.available || !current.export.calculation_hash) return;
  exportController?.abort();
  const controller = new AbortController();
  exportController = controller;
  exporting.value = true;
  exportNotice.value = "";
  try {
    const blob = await fetchQuarterlyWorkbook(current, controller.signal);
    if (exportController !== controller) return;
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
      "已导出已填充的 Excel 导入文件；请在电子税务局手工导入并逐项复核。";
  } catch (error: unknown) {
    if (exportController !== controller) return;
    if (error instanceof DashboardApiError && error.code === "REPORT_PREVIEW_STALE") {
      await preview(selectedQuarter.value);
      exportNoticeKind.value = "attention";
      exportNotice.value = `${error.message} 报表已重新核对，请再次确认后导出。`;
    } else {
      const message = dashboardErrorMessage(error);
      if (message) {
        exportNoticeKind.value = "error";
        exportNotice.value = message;
      }
    }
  } finally {
    if (exportController === controller) {
      exporting.value = false;
      exportController = null;
    }
  }
}

function statementValue(value: string | null | undefined) {
  return value === null || value === undefined ? "—" : formatFen(value);
}

function selectStatement(key: string) {
  activeStatementKey.value = key;
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
  void nextTick(() => document.getElementById(`report-tab-${nextIndex}`)?.focus());
}

onMounted(() => {
  mounted = true;
  void synchronizeQuarter();
});

watch(
  () => route.query.quarter,
  () => {
    if (mounted) void synchronizeQuarter();
  },
);

onBeforeUnmount(() => {
  mounted = false;
  previewController?.abort();
  exportController?.abort();
});
</script>

<template>
  <section class="reports-page">
    <div class="reports-content">
      <DashboardModuleHeader
        eyebrow="财务报表"
        title="季度财务报表"
        description="核对已结账季度的三张财务报表，并导出已填充的电子税务局 Excel 导入文件。"
        :options="quarterOptions"
        :selected="selectedQuarter"
        :loading="loading"
        select-label="季度报表期间"
        @change="changeQuarter"
        @refresh="refresh"
      />

      <section v-if="!quarterOptions.length && !loading" class="state-panel">
        <strong>还没有可核对的季度</strong>
        <span>请先生成会计期间；季度报表不会创建或修改账务数据。</span>
      </section>

      <section v-else-if="loading && !report" class="state-panel" aria-live="polite">
        <strong>正在准备季度报表</strong>
        <span>正在核对关账快照、报表分类、所得税确认和三表勾稽关系…</span>
      </section>

      <section v-else-if="errorMessage && !report" class="state-panel error" role="alert">
        <strong>季度报表读取失败</strong>
        <span>{{ errorMessage }}</span>
        <button type="button" @click="refresh">重新核对</button>
      </section>

      <section v-else-if="report" class="report-dashboard">
        <section class="report-hero" aria-labelledby="report-readiness-title">
          <div class="report-heading">
            <div>
            <p class="eyebrow">季度申报准备</p>
              <h2 id="report-readiness-title">申报准备状态</h2>
              <p>根据已结账账务生成已填充的 Excel 文件；仍需负责人手工导入电子税务局并复核。</p>
            </div>
            <div class="report-actions">
              <button class="secondary" type="button" :disabled="loading" @click="refresh">
                {{ loading ? "正在核对…" : "重新核对" }}
              </button>
              <button
                type="button"
                :disabled="!report.export.available || exporting || loading"
                @click="exportReport"
              >
                {{ exporting ? "正在导出…" : "导出" }}
              </button>
            </div>
          </div>

          <div class="status-meta">
            <span class="status-badge" :class="reportStateClass">{{ report.status_label }}</span>
            <span>核对于 {{ checkedAt }}</span>
          </div>

          <div class="report-state" :class="reportStateClass" role="status" aria-live="polite">
            <strong>{{ report.headline }}</strong>
            <p>{{ report.message }}</p>
          </div>

          <div v-if="exportNotice" class="export-notice" :class="exportNoticeKind" role="status">
            {{ exportNotice }}
          </div>
        </section>

        <section v-if="report.readiness.length" class="readiness" aria-label="季度报表准备度">
          <article
            v-for="item in report.readiness"
            :key="item.key"
            class="readiness-item"
            :class="[item.state, { detailed: item.details.length }]"
          >
            <div class="readiness-head">
              <span>{{ item.state === "pass" ? "✓" : item.state === "pending" ? "…" : "!" }}</span>
              <strong>{{ item.label }}</strong>
            </div>
            <p>{{ item.summary }}</p>
            <ul v-if="item.details.length">
              <li v-for="detail in item.details" :key="`${detail.primary}-${detail.secondary}`">
                <div><strong>{{ detail.primary }}</strong><span>{{ detail.secondary }}</span></div>
                <strong v-if="detail.amount_fen != null">{{ formatFen(detail.amount_fen) }}</strong>
              </li>
            </ul>
          </article>
        </section>

        <section v-if="summaryCards.length" class="summary-grid" aria-label="季度报表摘要">
          <article v-for="item in summaryCards" :key="item.source">
            <span>{{ item.source }} · {{ item.label }}</span>
            <strong>{{ item.value }}</strong>
            <span>{{ item.note }}</span>
          </article>
        </section>

        <p v-if="report.draft" class="draft-note">
          以下金额为当前试算，不可用于申报或导出。
        </p>

        <section v-if="report.statements.length" class="report-review">
          <div class="review-heading">
            <button
              class="expand-button"
              type="button"
              :aria-expanded="statementsExpanded"
              aria-controls="report-full"
              @click="statementsExpanded = !statementsExpanded"
            >
              {{ statementsExpanded ? "收起完整三表 ↑" : "查看完整三表 ↓" }}
            </button>
            <span>
              {{ report.checks.total ? `${report.checks.passed} 项勾稽已通过` : "暂无勾稽结果" }}
            </span>
          </div>

          <div v-if="statementsExpanded" id="report-full" class="report-full">
            <div class="tabs" role="tablist" aria-label="季度财务报表">
              <button
                v-for="(statement, index) in report.statements"
                :id="`report-tab-${index}`"
                :key="statement.key"
                type="button"
                role="tab"
                :aria-selected="activeStatement?.key === statement.key"
                :tabindex="activeStatement?.key === statement.key ? 0 : -1"
                @click="selectStatement(statement.key)"
                @keydown="handleTabKey($event, index)"
              >
                {{ statement.label }}
              </button>
            </div>

            <template v-if="activeStatement">
              <div class="table-toolbar">
                <strong>{{ activeStatement.label }}{{ report.draft ? " · 当前试算" : "" }}</strong>
                <label><input v-model="showAllRows" type="checkbox" /> 显示全部行（含零值）</label>
              </div>
              <div class="table-wrap">
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
                <strong>勾稽核对</strong>
                <span :class="{ failed: report.checks.passed !== report.checks.total }">
                  {{ report.checks.passed }} / {{ report.checks.total }} 项通过
                </span>
              </summary>
              <div class="check-list">
                <div v-for="item in report.checks.items" :key="item.code" :class="{ failed: !item.passed }">
                  <span>{{ item.passed ? "✓" : "×" }}</span><strong>{{ item.label }}</strong>
                </div>
              </div>
            </details>

            <details class="disclosure technical">
              <summary><strong>技术信息</strong></summary>
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
.reports-content { width: min(calc(100% - 48px), 1320px); margin: 0 auto; padding: 25px 0 46px; }
.state-panel { display: grid; gap: 7px; padding: 28px; border: 1px solid var(--line); border-radius: 18px; background: var(--surface); box-shadow: var(--shadow-soft); }
.state-panel span { color: var(--muted); }
.state-panel.error { border-color: var(--danger); }
.state-panel button { width: fit-content; min-height: 40px; margin-top: 8px; padding: 0 14px; border: 0; border-radius: 10px; background: var(--accent); color: var(--surface); cursor: pointer; }
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
.export-notice { margin-top: 8px; padding: 10px 13px; border-radius: 10px; background: var(--accent-soft); color: var(--accent); font-size: 12px; }
.export-notice.attention { background: var(--warning-soft); color: var(--warning); }
.export-notice.error { background: var(--danger-soft); color: var(--danger); }
.readiness { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; }
.readiness-item { position: relative; overflow: hidden; padding: 15px 16px; border: 1px solid var(--line); border-radius: 16px; background: var(--surface); box-shadow: var(--shadow-soft); }
.readiness-item::before { position: absolute; top: 0; right: 0; left: 0; height: 3px; background: var(--accent); content: ""; }
.readiness-item.pending::before { background: var(--info); }
.readiness-item.attention::before { background: var(--warning); }
.readiness-item.detailed { grid-column: span 2; }
.readiness-head { display: flex; align-items: center; gap: 8px; }
.readiness-head > span { display: grid; width: 23px; height: 23px; flex: 0 0 auto; place-items: center; border-radius: 50%; background: var(--accent-soft); color: var(--accent); font-weight: 850; }
.readiness-item.pending .readiness-head > span { background: var(--info-soft); color: var(--info); }
.readiness-item.attention .readiness-head > span { background: var(--warning-soft); color: var(--warning); }
.readiness-item p { margin: 7px 0 0; color: var(--muted); font-size: 11px; }
.readiness-item ul { display: grid; gap: 7px; margin: 10px 0 0; padding: 10px 0 0; border-top: 1px solid var(--line); list-style: none; }
.readiness-item li { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 3px 10px; }
.readiness-item li div { display: grid; gap: 2px; min-width: 0; }
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
.expand-button { min-height: 36px; padding: 0; border: 0; background: transparent; color: var(--accent); cursor: pointer; font-weight: 750; }
.report-full { min-width: 0; margin-top: 12px; }
.tabs { display: flex; flex-wrap: wrap; gap: 6px; }
.tabs button { min-height: 36px; padding: 7px 13px; border: 1px solid var(--line); border-radius: 10px; background: var(--surface); color: var(--muted); cursor: pointer; }
.tabs button[aria-selected="true"] { border-color: var(--accent); background: var(--accent-soft); color: var(--accent); font-weight: 750; }
.table-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin: 12px 0 8px; }
.table-toolbar label { display: flex; align-items: center; gap: 7px; color: var(--muted); font-size: 12px; cursor: pointer; }
.table-wrap { min-width: 0; max-width: 100%; overflow-x: auto; border: 1px solid var(--line); border-radius: 10px; }
table { width: 100%; min-width: 650px; border-collapse: collapse; background: var(--surface); font-size: 12px; }
th, td { padding: 10px 11px; border-bottom: 1px solid var(--line); text-align: left; }
th { position: sticky; top: 0; background: var(--surface-soft); color: var(--muted); font-size: 11px; }
th:first-child { width: 54%; }
.line { width: 58px; color: var(--muted); text-align: center; }
.number { text-align: right; font-variant-numeric: tabular-nums; }
tr.total td { background: var(--accent-soft); font-weight: 750; }
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
@media (max-width: 960px) { .readiness { grid-template-columns: repeat(2, minmax(0, 1fr)); } .summary-grid { grid-template-columns: 1fr; } }
@media (max-width: 760px) { .reports-content { width: min(calc(100% - 24px), 1320px); padding: 16px 0 24px; } .report-hero { padding: 19px; border-radius: 17px; } .report-heading { flex-direction: column; gap: 13px; } .report-actions { width: 100%; } .report-actions button { min-height: 44px; flex: 1; } .readiness, .check-list { grid-template-columns: 1fr; } .readiness-item.detailed { grid-column: auto; } .review-heading, .table-toolbar { align-items: flex-start; flex-direction: column; } .tabs button, .expand-button, .disclosure summary { min-height: 44px; } .technical dl { grid-template-columns: 1fr; } }
</style>
