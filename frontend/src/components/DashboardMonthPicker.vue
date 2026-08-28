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
const viewedYear = ref<number | null>(null);
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
const selectedPeriodInfo = computed(() =>
  props.periods.find((period) => period.key === props.selectedPeriod),
);
const selectedPeriodLabel = computed(() => {
  const period = selectedPeriodInfo.value;
  if (!period) return "尚未选择月份";
  return `${period.year}年${String(period.month).padStart(2, "0")}月`;
});

const monthSlots = computed(() =>
  monthOrder.map((month) => ({
    month,
    period: props.periods.find(
      (period) => period.year === viewedYear.value && period.month === month,
    ),
  })),
);

const canShowEarlier = computed(() => pageIndex.value > 0);
const canShowLater = computed(() => pageIndex.value < yearPages.value.length - 1);

watch(
  [() => props.periods, () => props.selectedPeriod],
  () => {
    const selectedYear = selectedPeriodInfo.value?.year ?? years.value.at(-1) ?? null;
    viewedYear.value = selectedYear;
    const selectedPage = yearPages.value.findIndex((page) => page.includes(selectedYear ?? -1));
    pageIndex.value = selectedPage >= 0 ? selectedPage : Math.max(yearPages.value.length - 1, 0);
  },
  { immediate: true },
);

function showEarlierYears() {
  if (!canShowEarlier.value) return;
  pageIndex.value -= 1;
  viewedYear.value = visibleYears.value.at(-1) ?? null;
}

function showLaterYears() {
  if (!canShowLater.value) return;
  pageIndex.value += 1;
  viewedYear.value = visibleYears.value.at(0) ?? null;
}

function monthLabel(month: number, period?: DashboardPeriod) {
  const label = `${viewedYear.value ?? ""}年${month}月`;
  if (!period) return `${label}，暂无可查看数据`;
  return period.key === props.selectedPeriod ? `${label}，当前选择` : `查看${label}`;
}
</script>

<template>
  <section v-if="periods.length" class="month-picker" aria-labelledby="month-picker-title">
    <div class="month-picker-heading">
      <strong id="month-picker-title">快速选月</strong>
      <span>当前 {{ selectedPeriodLabel }}</span>
    </div>

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

      <div
        class="year-options"
        role="group"
        aria-label="选择要浏览的年份"
        :style="{ gridTemplateColumns: `repeat(${visibleYears.length}, minmax(0, 1fr))` }"
      >
        <button
          v-for="year in visibleYears"
          :key="year"
          type="button"
          :class="[
            'year-option',
            {
              'is-viewed': year === viewedYear,
              'has-selected-period': year === selectedPeriodInfo?.year,
            },
          ]"
          :aria-pressed="year === viewedYear"
          :aria-label="`${year}年${year === selectedPeriodInfo?.year ? '，当前月份所在年份' : ''}`"
          @click="viewedYear = year"
        >
          {{ year }}
        </button>
      </div>

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

    <div class="quarter-labels" aria-hidden="true">
      <span>Q1</span><span>Q2</span><span>Q3</span><span>Q4</span>
    </div>

    <div class="month-grid" :aria-label="`${viewedYear}年月份`">
      <button
        v-for="slot in monthSlots"
        :key="slot.month"
        type="button"
        :class="['month-button', { 'is-current': slot.period?.key === selectedPeriod }]"
        :disabled="!slot.period"
        :aria-label="monthLabel(slot.month, slot.period)"
        :aria-current="slot.period?.key === selectedPeriod ? 'date' : undefined"
        @click="slot.period && emit('select', slot.period.key)"
      >
        {{ slot.month }}月
      </button>
    </div>
  </section>
</template>

<style scoped>
.month-picker {
  flex: 0 0 auto;
  margin-top: 16px;
  padding-top: 15px;
  border-top: 1px solid var(--line);
}

.month-picker-heading {
  display: grid;
  gap: 1px;
  padding: 0 3px;
}

.month-picker-heading strong {
  font-size: 13px;
  letter-spacing: 0.02em;
}

.month-picker-heading span {
  color: var(--muted);
  font-size: 11px;
}

.year-pager {
  display: grid;
  grid-template-columns: 26px minmax(0, 1fr) 26px;
  gap: 4px;
  align-items: center;
  margin-top: 10px;
}

.year-options {
  display: grid;
  gap: 3px;
}

.year-arrow,
.year-option,
.month-button {
  border: 1px solid transparent;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
}

.year-arrow {
  display: grid;
  width: 26px;
  height: 28px;
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
.year-option:hover,
.year-option:focus-visible {
  border-color: var(--accent);
  color: var(--accent);
}

.year-arrow:disabled {
  opacity: 0.38;
  cursor: not-allowed;
}

.year-option {
  position: relative;
  min-width: 0;
  height: 28px;
  padding: 0 2px;
  border-radius: 8px;
  font-size: 11px;
}

.year-option.is-viewed {
  border-color: var(--line-strong);
  background: var(--surface-soft);
  color: var(--text);
  font-weight: 750;
}

.year-option.has-selected-period::after {
  position: absolute;
  right: 4px;
  bottom: 3px;
  width: 3px;
  height: 3px;
  border-radius: 50%;
  background: var(--accent);
  content: "";
}

.quarter-labels,
.month-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 4px;
}

.quarter-labels {
  margin: 10px 0 4px;
  color: var(--muted);
  font-size: 10px;
  font-weight: 700;
  line-height: 1;
  text-align: center;
}

.month-button {
  min-width: 0;
  height: 31px;
  padding: 0;
  border-color: var(--line);
  border-radius: 8px;
  background: var(--surface);
  font-size: 11px;
}

.month-button:hover:not(:disabled),
.month-button:focus-visible {
  border-color: var(--accent);
  color: var(--accent);
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
