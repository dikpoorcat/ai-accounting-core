<script setup lang="ts">
import { computed } from "vue";

import type { ReportStatement, ReportStatementRow } from "../api/reports";
import { cashFlowClass, formatFen } from "../utils/money";

interface StatementLayout {
  roots: readonly number[];
  children: Readonly<Record<number, readonly number[]>>;
  results: readonly number[];
}

interface DisplayRow {
  row: ReportStatementRow;
  depth: number;
  hasChildren: boolean;
  expanded: boolean;
  result: boolean;
}

const props = withDefaults(defineProps<{
  statement: ReportStatement;
  expandedRows?: readonly number[];
}>(), { expandedRows: () => [] });

const emit = defineEmits<{
  "update:expandedRows": [rows: number[]];
}>();

const statementLayouts: Readonly<Record<string, StatementLayout>> = {
  balance_sheet: {
    roots: [15, 29, 30, 41, 46, 47, 52, 53],
    children: {
      15: [1, 2, 3, 4, 5, 6, 7, 8, 9, 14],
      9: [10, 11, 12, 13],
      29: [16, 17, 20, 21, 22, 23, 24, 25, 26, 27, 28],
      20: [18, 19],
      41: [31, 32, 33, 34, 35, 36, 37, 38, 39, 40],
      46: [42, 43, 44, 45],
      52: [48, 49, 50, 51],
    },
    results: [30, 47, 53],
  },
  profit_statement: {
    roots: [1, 2, 3, 11, 14, 18, 20, 21, 22, 24, 30, 31, 32],
    children: {
      3: [4, 5, 6, 7, 8, 9, 10],
      11: [12, 13],
      14: [15, 16, 17],
      18: [19],
      22: [23],
      24: [25, 26, 27, 28, 29],
    },
    results: [21, 30, 32],
  },
  cash_flow_statement: {
    roots: [7, 13, 19, 20, 21, 22],
    children: {
      7: [1, 2, 3, 4, 5, 6],
      13: [8, 9, 10, 11, 12],
      19: [14, 15, 16, 17, 18],
    },
    results: [20, 22],
  },
};

const hierarchy = computed(() => {
  const layout = statementLayouts[props.statement.key];
  const rows = new Map(props.statement.rows.map(row => [row.line, row]));
  const children = new Map<number, number[]>();
  const parents = new Set<number>();
  for (const [parentLine, childLines] of Object.entries(layout?.children ?? {})) {
    const parent = Number(parentLine);
    if (!rows.has(parent)) continue;
    const available = childLines.filter(line => rows.has(line));
    if (!available.length) continue;
    children.set(parent, available);
    for (const line of available) parents.add(line);
  }
  // Missing parents and unfamiliar lines remain visible as independent roots.
  const rootCandidates = [...(layout?.roots ?? []), ...rows.keys()];
  const roots = [...new Set(rootCandidates)].filter(line => rows.has(line) && !parents.has(line));
  return { rows, children, roots, results: new Set(layout?.results ?? []) };
});

const expanded = computed(() => new Set(props.expandedRows));
const expandableLines = computed(() => [...hierarchy.value.children.keys()]);
const allExpanded = computed(() => expandableLines.value.every(line => expanded.value.has(line)));
const anyExpanded = computed(() => expandableLines.value.some(line => expanded.value.has(line)));
const visibleRows = computed<DisplayRow[]>(() => {
  const structure = hierarchy.value;
  const result: DisplayRow[] = [];
  const visited = new Set<number>();
  function visit(line: number, depth: number) {
    const row = structure.rows.get(line);
    if (!row || visited.has(line)) return;
    visited.add(line);
    const childLines = structure.children.get(line) ?? [];
    const open = expanded.value.has(line);
    result.push({ row, depth, hasChildren: childLines.length > 0, expanded: open, result: structure.results.has(line) });
    if (open) for (const child of childLines) visit(child, depth + 1);
  }
  for (const root of structure.roots) visit(root, 0);
  return result;
});

function toggleRow(line: number) {
  const next = new Set(props.expandedRows);
  if (next.has(line)) next.delete(line);
  else next.add(line);
  emit("update:expandedRows", [...next]);
}

function displayAmount(value: string | null | undefined) {
  return value === null || value === undefined ? "暂无法确定" : formatFen(value);
}

function amountTone(line: number, value: string | null | undefined) {
  if (value === null || value === undefined) return "";
  if (props.statement.key === "profit_statement" && statementLayouts.profit_statement.results.includes(line)) {
    const amount = BigInt(value);
    return amount === 0n ? "" : amount > 0n ? "profit-gain" : "profit-loss";
  }
  if (props.statement.key === "cash_flow_statement" && [7, 13, 19, 20].includes(line)) {
    return cashFlowClass(value, "inflow");
  }
  return "";
}
</script>

<template>
  <div class="statement-table-wrap" data-section-focus tabindex="-1">
    <div v-if="expandableLines.length" class="statement-controls" role="group" :aria-label="`${statement.label}明细展开方式`">
      <button type="button" :disabled="allExpanded" @click="emit('update:expandedRows', [...expandableLines])">展开全部</button>
      <button type="button" :disabled="!anyExpanded" @click="emit('update:expandedRows', [])">收起全部</button>
    </div>
    <table class="statement-table" :aria-label="`${statement.label}分组明细`">
      <thead>
        <tr>
          <th scope="col">项目</th>
          <th v-for="column in statement.columns" :key="column.key" scope="col" class="statement-number">{{ column.label }}</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="item in visibleRows"
          :key="item.row.line"
          :data-line="item.row.line"
          :data-depth="item.depth"
          :class="{ 'summary-row': item.hasChildren, 'result-row': item.result, 'child-row': item.depth > 0 }"
        >
          <th scope="row" class="statement-name">
            <div class="statement-name-content" :style="{ '--statement-depth': item.depth }">
              <button
                v-if="item.hasChildren"
                class="statement-disclosure"
                type="button"
                :aria-label="`${item.expanded ? '收起' : '展开'}${item.row.name}明细`"
                :aria-expanded="item.expanded"
                @click="toggleRow(item.row.line)"
              >
                <svg class="business-list-arrow" :class="{ expanded: item.expanded }" viewBox="0 0 16 16" aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg>
                <span class="statement-label">{{ item.row.name }}</span>
              </button>
              <template v-else>
                <span class="business-list-arrow-space" aria-hidden="true"></span>
                <span class="statement-label">{{ item.row.name }}</span>
              </template>
            </div>
          </th>
          <td
            v-for="column in statement.columns"
            :key="column.key"
            class="statement-number"
            :class="amountTone(item.row.line, item.row.values[column.key])"
            :data-label="column.label"
          >{{ displayAmount(item.row.values[column.key]) }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.statement-table-wrap { min-width: 0; max-width: 100%; overflow: hidden; padding-inline: var(--dashboard-list-gutter); border: 1px solid var(--line); border-radius: var(--radius-control); background: var(--surface); }
.statement-controls { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 6px; padding: 9px 4px; border-bottom: 1px solid var(--line); }
.statement-controls button { min-height: 30px; padding: 3px 10px; border: 1px solid var(--line); border-radius: 6px; background: var(--surface); color: var(--muted); font-size: 12px; cursor: pointer; }
.statement-controls button:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }
.statement-controls button:disabled { opacity: .45; cursor: default; }
.statement-controls button:focus-visible, .statement-disclosure:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.statement-table { width: 100%; min-width: 0; border-collapse: collapse; table-layout: fixed; font-size: 13px; }
.statement-table th, .statement-table td { padding: 10px 16px; border-bottom: 1px solid var(--line); line-height: 1.55; }
.statement-table th:first-child, .statement-table td:first-child { padding-inline-start: 4px; }
.statement-table th:last-child, .statement-table td:last-child { padding-inline-end: 4px; }
.statement-table thead th { background: var(--surface-soft); color: var(--muted); font-size: 11px; font-weight: 650; text-align: left; }
.statement-table thead th:first-child { width: 54%; }
.statement-table tbody th { color: var(--text); font-weight: 500; text-align: left; }
.statement-table tbody tr:last-child > * { border-bottom: 0; }
.statement-table .statement-number { text-align: right; color: var(--text); font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.statement-table thead .statement-number { color: var(--muted); }
.statement-name-content { display: flex; align-items: flex-start; gap: 8px; min-width: 0; padding-inline-start: calc(var(--statement-depth) * 20px); }
.statement-label { min-width: 0; overflow-wrap: anywhere; }
.statement-disclosure { display: flex; width: 100%; min-width: 0; min-height: 24px; align-items: flex-start; gap: 8px; padding: 0; border: 0; border-radius: 4px; background: transparent; color: inherit; font: inherit; font-weight: inherit; line-height: inherit; text-align: left; cursor: pointer; }
.business-list-arrow, .business-list-arrow-space { flex-shrink: 0; margin-top: 2px; }
.statement-table .summary-row th, .statement-table .summary-row td { font-weight: 650; }
.statement-table .child-row .statement-label { color: var(--muted); }
.statement-table .result-row > * { border-top: 1px solid var(--line-strong); background: var(--surface-soft); font-weight: 750; }
.statement-table .profit-gain { color: var(--accent); }
.statement-table .profit-loss, .statement-table .cash-outflow { color: var(--danger); }
.statement-table .cash-inflow { color: var(--cash-in); }

@media (max-width: 760px) {
  .statement-table-wrap { padding-inline: 12px; }
  .statement-controls { padding: 8px 4px; }
  .statement-controls button { min-height: 40px; padding-inline: 12px; }
  .statement-table, .statement-table tbody, .statement-table tr, .statement-table th, .statement-table td { display: block; min-width: 0; width: auto; }
  .statement-table thead { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
  .statement-table tbody tr { padding: 8px 4px 11px; border-bottom: 1px solid var(--line); }
  .statement-table tbody tr:last-child { border-bottom: 0; }
  .statement-table th, .statement-table td { padding: 4px 0; border: 0; }
  .statement-table th:first-child, .statement-table td:first-child, .statement-table th:last-child, .statement-table td:last-child { padding-inline: 0; }
  .statement-name-content { align-items: center; padding-inline-start: calc(var(--statement-depth) * 16px); }
  .statement-disclosure { min-height: 40px; align-items: center; }
  .business-list-arrow, .business-list-arrow-space { margin-top: 0; }
  .statement-table td[data-label] { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.6fr); align-items: baseline; gap: 12px; }
  .statement-table td[data-label]::before { content: attr(data-label); color: var(--muted); text-align: left; font-size: 11px; font-weight: 400; }
  .statement-table .result-row { border-top: 1px solid var(--line-strong); background: var(--surface-soft); }
  .statement-table .result-row > * { border-top: 0; background: transparent; }
}
</style>
