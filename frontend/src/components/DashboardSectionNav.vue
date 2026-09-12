<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

const props = defineProps<{
  items: readonly { id: string; label: string }[];
  active: string;
  label: string;
  floating?: boolean;
}>();

const emit = defineEmits<{
  select: [id: string];
}>();
const element = ref<HTMLElement | null>(null);
const floatingActive = ref(false);
let observer: ResizeObserver | undefined;
let frame = 0;
let disposed = false;

function measure() {
  frame = 0;
  const nav = element.value, page = nav?.parentElement;
  const header = page?.querySelector<HTMLElement>(".module-header");
  if (!nav || !page || !header) return;
  // Measure in normal flow, independently of the current sticky scroll position.
  nav.style.position = "static";
  nav.style.marginTop = "0px";
  nav.style.marginBottom = window.innerWidth <= 720 ? "10px" : "12px";
  const rect = nav.getBoundingClientRect(), pageRect = page.getBoundingClientRect();
  const heading = header.querySelector(".module-heading")?.getBoundingClientRect();
  const toolbar = header.querySelector(".toolbar")?.getBoundingClientRect();
  const left = rect.left, right = rect.right;
  const fits = !!props.floating && window.innerWidth >= 1280 && !!heading && !!toolbar
    && left >= heading.right + 16 && right <= toolbar.left - 16;
  if (fits) {
    const lift = rect.top - pageRect.top - 8;
    nav.style.marginTop = `${-lift}px`;
    nav.style.marginBottom = `${lift - rect.height}px`;
  }
  floatingActive.value = fits;
  nav.style.position = "sticky";
}
function scheduleMeasure() {
  if (!frame && !disposed) frame = requestAnimationFrame(measure);
}
onMounted(() => {
  const nav = element.value, page = nav?.parentElement;
  observer = new ResizeObserver(scheduleMeasure);
  if (nav) observer.observe(nav);
  if (page) {
    observer.observe(page);
    page.querySelectorAll(".module-header, .module-heading, .toolbar").forEach(item => observer!.observe(item));
  }
  window.addEventListener("resize", scheduleMeasure);
  void document.fonts.ready.then(scheduleMeasure);
  scheduleMeasure();
});
watch(() => [props.items, props.floating], async () => { await nextTick(); scheduleMeasure(); });
onBeforeUnmount(() => {
  disposed = true;
  observer?.disconnect();
  cancelAnimationFrame(frame);
  window.removeEventListener("resize", scheduleMeasure);
});
</script>

<template>
  <nav ref="element" :class="['section-nav', { floating }]" :data-floating="floatingActive" :aria-label="label">
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
