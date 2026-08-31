<script setup lang="ts">
defineProps<{
  items: readonly { id: string; label: string }[];
  active: string;
  label: string;
  floating?: boolean;
}>();

const emit = defineEmits<{
  select: [id: string];
}>();
</script>

<template>
  <nav :class="['section-nav', { floating }]" :aria-label="label">
    <button
      v-for="item in items"
      :key="item.id"
      type="button"
      :aria-current="active === item.id ? 'location' : undefined"
      @click="emit('select', item.id)"
    >
      {{ item.label }}
    </button>
  </nav>
</template>

<style scoped>
.section-nav {
  position: sticky;
  z-index: 25;
  top: 8px;
  display: flex;
  width: max-content;
  max-width: 100%;
  align-self: start;
  gap: 3px;
  margin: 0 auto 12px;
  padding: 4px;
  overflow-x: auto;
  border: 1px solid color-mix(in srgb, var(--line) 85%, transparent);
  border-radius: 13px;
  background: color-mix(in srgb, var(--surface) 91%, transparent);
  box-shadow: 0 7px 24px rgb(25 55 37 / 8%);
  backdrop-filter: blur(16px);
  scrollbar-width: none;
}

.section-nav::-webkit-scrollbar {
  display: none;
}

.section-nav button {
  min-height: 34px;
  padding: 0 12px;
  border: 0;
  border-radius: 9px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  font: inherit;
  font-size: 12px;
  white-space: nowrap;
}

.section-nav button:hover,
.section-nav button:focus-visible,
.section-nav button[aria-current="location"] {
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 800;
}

.section-nav button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

@media (min-width: 1280px) {
  .section-nav.floating {
    margin-top: -136px;
    margin-bottom: 92px;
  }
}

@media (max-width: 720px) {
  .section-nav {
    top: 4px;
    width: 100%;
    justify-content: flex-start;
    margin-bottom: 10px;
  }

  .section-nav button {
    min-height: 44px;
  }
}
</style>
