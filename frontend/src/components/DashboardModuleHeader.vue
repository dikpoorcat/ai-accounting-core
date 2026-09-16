<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import { useDashboardContext } from "../composables/useDashboardContext";

interface Option {
  key: string;
  label: string;
  status?: string;
}

const props = defineProps<{
  title: string;
  options: readonly Option[];
  selected: string;
  loading?: boolean;
  selectLabel: string;
}>();

const selectedStatus = computed(
  () => props.options.find((item) => item.key === displayedSelection.value)?.status || "",
);

const emit = defineEmits<{
  change: [value: string];
  refresh: [];
}>();
const route = useRoute();
const displayedSelection = computed(() => props.selected || (props.options.find(item => item.key === (route.name === "reports" ? route.query.quarter : route.query.period))?.key ?? ""));
const { setSelectionNotice } = useDashboardContext();

function handleChange(event: Event) {
  setSelectionNotice("");
  emit("change", (event.target as HTMLSelectElement).value);
}
</script>

<template>
  <header class="module-header" :class="{ 'with-navigation': $slots.navigation }" data-section-header :aria-busy="loading">
    <div class="module-heading">
      <h1>{{ title }}</h1>
    </div>
    <div v-if="$slots.navigation" class="module-navigation">
      <slot name="navigation" />
    </div>
    <div class="toolbar">
      <select
        class="control"
        :value="displayedSelection"
        :aria-label="selectLabel"
        :disabled="options.length === 0"
        @change="handleChange"
      >
        <option v-for="option in options" :key="option.key" :value="option.key">
          {{ option.label }}
        </option>
      </select>
      <span
        v-if="displayedSelection"
        :class="['period-status', selectedStatus === 'closed' ? 'closed' : 'open']"
        role="status"
      >
        {{ selectedStatus === "closed" ? "已关账" : "未关账" }}
      </span>
      <button class="control refresh" type="button" :disabled="loading" @click="emit('refresh')">
        {{ loading ? "加载中…" : "刷新数据" }}
      </button>
    </div>
    <span v-if="loading" class="header-progress" aria-hidden="true" />
  </header>
</template>

<style scoped>
.module-header {
  position: relative;
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px 24px;
  margin-bottom: 12px;
  padding: 0 2px 4px;
}
.module-heading { min-width: 0; }

h1 {
  margin: 0;
  font-size: clamp(24px, 2.4vw, 30px);
  line-height: 1.12;
  letter-spacing: -0.035em;
}

.toolbar {
  display: flex;
  flex: 0 1 auto;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.control,
.period-status {
  min-height: 38px;
  border: 1px solid var(--line);
  border-radius: var(--radius-control, 9px);
  background: var(--surface);
  color: var(--text);
}

.control {
  padding: 0 11px;
  font: inherit;
  font-size: 13px;
}

select.control {
  cursor: pointer;
}

.refresh {
  min-width: 76px;
  cursor: pointer;
}

.refresh:hover:not(:disabled),
.refresh:focus-visible {
  border-color: var(--accent);
  background: var(--accent-soft);
  color: var(--accent);
}

.control:disabled {
  cursor: default;
  opacity: 0.65;
}

.period-status {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  min-height: 28px;
  padding: 3px 9px;
  border: 0;
  border-radius: 999px;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 13px;
  font-weight: 750;
}

.period-status.open {
  background: var(--warning-soft);
  color: var(--warning);
}

.header-progress {
  position: absolute;
  right: 0;
  bottom: -9px;
  left: 0;
  height: 2px;
  overflow: hidden;
  border-radius: 999px;
  background: color-mix(in srgb, var(--accent) 14%, transparent);
}

.header-progress::after {
  display: block;
  width: 34%;
  height: 100%;
  border-radius: inherit;
  background: var(--accent);
  content: "";
  animation: header-loading 1.05s ease-in-out infinite;
}

@keyframes header-loading {
  from {
    transform: translateX(-105%);
  }

  to {
    transform: translateX(395%);
  }
}

@media (max-width: 980px) {
  .module-header {
    align-items: flex-start;
    flex-direction: column;
    gap: 14px;
  }

  .toolbar {
    width: 100%;
    flex-wrap: wrap;
  }

  .toolbar select {
    flex: 1;
  }
}

@media (max-width: 720px) {
  .module-header {
    margin-bottom: 18px;
  }

  .toolbar {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto auto;
  }

  .toolbar > select {
    min-width: 0;
  }

  .control {
    min-height: 44px;
  }
}

@media (max-width: 430px) {
  .toolbar {
    grid-template-columns: minmax(0, 1fr) auto;
  }

  .toolbar > select {
    grid-column: 1 / -1;
  }

  .period-status {
    justify-self: start;
  }

  .refresh {
    justify-self: end;
  }
}

@media (prefers-reduced-motion: reduce) {
  .header-progress::after {
    animation-duration: 2.4s;
  }
}

.module-header.with-navigation {
  position: sticky;
  top: 0;
  z-index: 25;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 14px 24px;
  margin: 0 0 14px;
  padding: 10px 2px;
  background: color-mix(in srgb, var(--background) 96%, transparent);
  backdrop-filter: blur(12px);
}
.with-navigation h1 { font-size: 26px; white-space: nowrap; }
.with-navigation .toolbar { grid-row: 1; grid-column: 3; width: auto; flex-wrap: nowrap; }
/* The navigation is rendered by each page and disappears while its data is loading;
   keeping the row height here stops the header and the toolbar from moving. */
.with-navigation .module-navigation { grid-row: 1; grid-column: 2; min-width: 0; min-height: 42px; }
.with-navigation :deep(.section-nav) {
  position: static;
  min-width: 0;
  width: auto;
  gap: 16px;
  margin: 0;
  padding: 0;
  border: 0;
  background: transparent;
  backdrop-filter: none;
}
.with-navigation :deep(.section-nav button) { min-height: 42px; }
.with-navigation :deep(.section-nav button[aria-current="location"]::after) { bottom: 0; }
.with-navigation .header-progress { bottom: 0; }
@media (max-width: 1199px) {
  .module-header.with-navigation { grid-template-columns: minmax(0, 1fr) auto; gap: 6px 16px; }
  .with-navigation .toolbar { grid-column: 2; }
  .with-navigation .module-navigation { grid-row: 2; grid-column: 1 / -1; }
}
@media (max-width: 720px) {
  .module-header.with-navigation { grid-template-columns: minmax(0, 1fr); gap: 8px; padding: 8px 2px; }
  .with-navigation h1 { font-size: 24px; }
  .with-navigation .toolbar { display: flex; grid-row: 2; grid-column: 1; gap: 6px; }
  .with-navigation .toolbar select { min-width: 0; width: 0; flex: 1; }
  .with-navigation .module-navigation { grid-row: 3; grid-column: 1; }
  .with-navigation :deep(.section-nav) { gap: 14px; }
  .with-navigation .period-status { flex: none; padding: 3px 7px; font-size: 11px; }
}
</style>
