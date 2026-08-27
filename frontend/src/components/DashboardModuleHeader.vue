<script setup lang="ts">
import { computed } from "vue";

interface Option {
  key: string;
  label: string;
  status?: string;
}

const props = defineProps<{
  eyebrow: string;
  title: string;
  description: string;
  options: readonly Option[];
  selected: string;
  loading?: boolean;
  selectLabel: string;
}>();

const selectedStatus = computed(
  () => props.options.find((item) => item.key === props.selected)?.status || "",
);

const emit = defineEmits<{
  change: [value: string];
  refresh: [];
}>();

function handleChange(event: Event) {
  emit("change", (event.target as HTMLSelectElement).value);
}
</script>

<template>
  <header class="module-header" :aria-busy="loading">
    <div>
      <p class="eyebrow">{{ eyebrow }}</p>
      <h1>{{ title }}</h1>
      <p class="description">{{ description }}</p>
    </div>
    <div class="toolbar">
      <select
        class="control"
        :value="selected"
        :aria-label="selectLabel"
        :disabled="loading || options.length === 0"
        @change="handleChange"
      >
        <option v-for="option in options" :key="option.key" :value="option.key">
          {{ option.label }}
        </option>
      </select>
      <span
        v-if="selected"
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
  gap: 28px;
  margin-bottom: 22px;
  padding-bottom: 4px;
}

.eyebrow {
  margin: 0 0 5px;
  color: var(--accent);
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}

h1 {
  margin: 0;
  font-size: clamp(27px, 3vw, 36px);
  line-height: 1.12;
  letter-spacing: -0.035em;
}

.description {
  max-width: 720px;
  margin: 7px 0 0;
  color: var(--muted);
  font-size: 14px;
}

.toolbar {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 8px;
}

.control,
.period-status {
  min-height: 38px;
  border: 1px solid var(--line);
  border-radius: 10px;
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

  .toolbar select {
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

  .toolbar select {
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
</style>
