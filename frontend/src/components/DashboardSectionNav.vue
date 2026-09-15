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
  <nav class="section-nav" :aria-label="label">
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
  top: 0;
  display: flex;
  width: 100%;
  max-width: 100%;
  gap: 22px;
  margin: 0 0 30px;
  padding: 0 2px 8px;
  overflow-x: auto;
  border-bottom: 1px solid var(--line);
  background: color-mix(in srgb, var(--background) 94%, transparent);
  backdrop-filter: blur(12px);
  scrollbar-width: none;
}

.section-nav::-webkit-scrollbar {
  display: none;
}

.section-nav button {
  position: relative;
  min-height: 38px;
  flex: 0 0 auto;
  padding: 0 2px;
  border: 0;
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
  color: var(--accent);
}

.section-nav button[aria-current="location"] {
  font-weight: 800;
}

.section-nav button[aria-current="location"]::after {
  position: absolute;
  right: 0;
  bottom: -9px;
  left: 0;
  height: 2px;
  border-radius: 999px;
  background: var(--accent);
  content: "";
}

.section-nav button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
}

@media (max-width: 720px) {
  .section-nav {
    gap: 18px;
    margin-bottom: 24px;
  }

  .section-nav button {
    min-height: 44px;
  }
}
</style>
