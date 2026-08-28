<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type { DashboardPeriod } from "../api/context";

const props = defineProps<{
  periods: readonly DashboardPeriod[];
  selectedPeriod: string;
}>();

const emit = defineEmits<{
  select: [periodKey: string];
}>();

const monthOrder = [1, 4, 7, 10, 2, 5, 8, 11, 3, 6, 9, 12] as const;
const pageIndex = ref(0);

const years = computed(() =>
  [...new Set(props.periods.map((period) => period.year))].sort((left, right) => left - right),
);

const yearPages = computed(() => {
  const pages: number[][] = [];
  for (let end = years.value.length; end > 0; end -= 3) {
    pages.unshift(years.value.slice(Math.max(0, end - 3), end));
  }
  return pages;
});

const visibleYears = computed(() => yearPages.value[pageIndex.value] ?? []);
const visibleYearRange = computed(() => {
  const first = visibleYears.value.at(0);
  const last = visibleYears.value.at(-1);
  if (first === undefined || last === undefined) return "";
  if (first === last) return String(first);
  const lastLabel =
    Math.floor(first / 100) === Math.floor(last / 100) ? String(last).slice(-2) : last;
  return `${first}–${lastLabel}`;
});
const selectedPeriodInfo = computed(() =>
  props.periods.find((period) => period.key === props.selectedPeriod),
);

const visibleYearMatrices = computed(() =>
  visibleYears.value.map((year) => ({
    year,
    months: monthOrder.map((month) => ({
      month,
      period: props.periods.find((period) => period.year === year && period.month === month),
    })),
  })),
);

const canShowEarlier = computed(() => pageIndex.value > 0);
const canShowLater = computed(() => pageIndex.value < yearPages.value.length - 1);

watch(
  [() => props.periods, () => props.selectedPeriod],
  () => {
    const selectedYear = selectedPeriodInfo.value?.year ?? years.value.at(-1) ?? null;
    const selectedPage = yearPages.value.findIndex((page) => page.includes(selectedYear ?? -1));
    pageIndex.value = selectedPage >= 0 ? selectedPage : Math.max(yearPages.value.length - 1, 0);
  },
  { immediate: true },
);

function showEarlierYears() {
  if (!canShowEarlier.value) return;
  pageIndex.value -= 1;
}

function showLaterYears() {
  if (!canShowLater.value) return;
  pageIndex.value += 1;
}

function monthLabel(year: number, month: number, period?: DashboardPeriod) {
  const label = `${year}年${month}月`;
  if (!period) return `${label}，暂无可查看数据`;
  return period.key === props.selectedPeriod ? `${label}，当前选择` : `查看${label}`;
}
</script>

<template>
  <section v-if="periods.length" class="month-picker" aria-label="快速选月">
    <div class="year-pager">
      <button
        class="year-arrow"
        type="button"
        :disabled="!canShowEarlier"
        aria-label="查看更早的三个年份"
        title="更早年份"
        @click="showEarlierYears"
      >
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m14 6-6 6 6 6" /></svg>
      </button>

      <strong
        class="year-page-label"
        aria-live="polite"
        :aria-label="`当前显示${visibleYears.join('、')}年`"
      >
        {{ visibleYearRange }}
      </strong>

      <button
        class="year-arrow"
        type="button"
        :disabled="!canShowLater"
        aria-label="查看更新的三个年份"
        title="更新年份"
        @click="showLaterYears"
      >
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m10 6 6 6-6 6" /></svg>
      </button>
    </div>

    <div class="year-matrices">
      <div
        v-for="matrix in visibleYearMatrices"
        :key="matrix.year"
        class="year-matrix"
        role="group"
        :aria-label="`${matrix.year}年月份`"
      >
        <div class="year-matrix-heading">
          <strong>{{ matrix.year }}年</strong>
        </div>

        <div class="quarter-labels" aria-hidden="true">
          <span>Q1</span><span>Q2</span><span>Q3</span><span>Q4</span>
        </div>

        <div class="month-grid">
          <button
            v-for="slot in matrix.months"
            :key="slot.month"
            type="button"
            :class="['month-button', { 'is-current': slot.period?.key === selectedPeriod }]"
            :disabled="!slot.period"
            :aria-label="monthLabel(matrix.year, slot.month, slot.period)"
            :aria-current="slot.period?.key === selectedPeriod ? 'date' : undefined"
            @click="slot.period && emit('select', slot.period.key)"
          >
            {{ slot.month }}月
          </button>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.month-picker {
  flex: 0 0 auto;
  margin-top: 14px;
}

.year-pager {
  display: grid;
  grid-template-columns: 24px minmax(0, 1fr) 24px;
  gap: 3px;
  align-items: center;
}

.year-arrow,
.month-button {
  border: 1px solid transparent;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
}

.year-arrow {
  display: grid;
  width: 24px;
  height: 26px;
  padding: 0;
  place-items: center;
  border-color: var(--line);
  border-radius: 8px;
  background: var(--surface);
}

.year-arrow svg {
  width: 15px;
  height: 15px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.year-arrow:hover:not(:disabled),
.year-arrow:focus-visible,
.month-button:hover:not(:disabled),
.month-button:focus-visible {
  border-color: var(--accent);
  color: var(--accent);
}

.year-arrow:disabled {
  opacity: 0.38;
  cursor: not-allowed;
}

.year-page-label {
  overflow: hidden;
  color: var(--text);
  font-size: 11px;
  line-height: 1;
  text-align: center;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.year-matrices {
  display: grid;
  gap: 13px;
  margin-top: 8px;
}

.year-matrix {
  min-width: 0;
}

.year-matrix-heading {
  min-height: 18px;
  padding: 0 2px;
}

.year-matrix-heading strong {
  font-size: 12px;
}

.quarter-labels,
.month-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 4px;
}

.quarter-labels {
  margin: 5px 0 4px;
  color: var(--muted);
  font-size: 10px;
  font-weight: 700;
  line-height: 1;
  text-align: center;
}

.month-button {
  min-width: 0;
  height: 30px;
  padding: 0;
  border-color: var(--line);
  border-radius: 8px;
  background: var(--surface);
  font-size: 11px;
}

.month-button.is-current {
  border-color: color-mix(in srgb, var(--accent) 54%, var(--line));
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 800;
}

.month-button:disabled {
  border-color: color-mix(in srgb, var(--line) 68%, transparent);
  background: color-mix(in srgb, var(--surface-soft) 52%, transparent);
  color: color-mix(in srgb, var(--muted) 46%, transparent);
  cursor: not-allowed;
}

@media (max-width: 900px) {
  .month-picker {
    display: none;
  }
}
</style>
