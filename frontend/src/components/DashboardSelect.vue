<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch, type CSSProperties } from "vue";

interface Option {
  value: string;
  label: string;
  description?: string;
}
interface OptionGroup {
  label: string;
  options: readonly Option[];
}

const props = withDefaults(defineProps<{
  id: string;
  label: string;
  modelValue: string;
  options?: readonly Option[];
  groups?: readonly OptionGroup[];
  scope?: string;
  disabled?: boolean;
  icon?: "filter" | "calendar" | "bank";
  menuWidth?: number;
  align?: "start" | "end";
}>(), { scope: "", icon: "filter", menuWidth: 280, align: "end" });
const emit = defineEmits<{ change: [value: string] }>();
const optionGroups = computed(() => props.groups ?? [{ label: "", options: props.options ?? [] }]);
const options = computed(() => optionGroups.value.flatMap(group => group.options));
const selectedLabel = computed(() => options.value.find(option => option.value === props.modelValue)?.label ?? props.label);
const disabled = computed(() => Boolean(props.disabled) || options.value.length === 0);
const open = ref(false);
const root = ref<HTMLElement | null>(null);
const trigger = ref<HTMLButtonElement | null>(null);
const menu = ref<HTMLElement | null>(null);
const position = ref<{ left: number; top: number; width: number; maxHeight: number } | null>(null);
const menuStyle = computed<CSSProperties>(() => ({
  left: `${position.value?.left ?? 0}px`,
  top: `${position.value?.top ?? 0}px`,
  width: `${position.value?.width ?? props.menuWidth}px`,
  maxHeight: `${position.value?.maxHeight ?? 540}px`,
  visibility: position.value ? "visible" : "hidden",
}));
let openGeneration = 0;
let searchText = "", searchTime = 0;

function contains(target: EventTarget | null) {
  return target instanceof Node && Boolean(root.value?.contains(target) || menu.value?.contains(target));
}

function closeMenu(restoreFocus = false) {
  openGeneration += 1;
  open.value = false;
  position.value = null;
  searchText = "";
  if (restoreFocus) trigger.value?.focus({ preventScroll: true });
}

function positionMenu() {
  if (!trigger.value || !menu.value) return;
  const rect = trigger.value.getBoundingClientRect();
  const viewportWidth = document.documentElement.clientWidth, viewportHeight = window.innerHeight;
  const margin = 12, gap = 8;
  if (rect.bottom <= margin || rect.top >= viewportHeight - margin) { closeMenu(); return; }
  const width = Math.min(Math.max(props.menuWidth, rect.width), viewportWidth - margin * 2);
  // Measure wrapped labels at the final width before deciding which side fits.
  menu.value.style.width = `${width}px`;
  const desiredHeight = Math.min(menu.value.scrollHeight + 2, 540);
  const below = Math.max(0, viewportHeight - rect.bottom - gap - margin);
  const above = Math.max(0, rect.top - gap - margin);
  const opensAbove = below < desiredHeight && above > below;
  const maxHeight = Math.min(540, opensAbove ? above : below);
  const left = props.align === "start" ? rect.left : rect.right - width;
  position.value = {
    left: Math.max(margin, Math.min(left, viewportWidth - width - margin)),
    top: opensAbove ? rect.top - gap - Math.min(desiredHeight, maxHeight) : rect.bottom + gap,
    width, maxHeight,
  };
}

function optionButtons() {
  return Array.from(menu.value?.querySelectorAll<HTMLButtonElement>('button[role="option"]') ?? []);
}

function focusOption(index: number) {
  const button = optionButtons()[index];
  button?.focus({ preventScroll: true });
  button?.scrollIntoView({ block: "nearest" });
}

async function openMenu(edge?: "first" | "last") {
  if (disabled.value) return;
  if (open.value && !edge) { closeMenu(); return; }
  const generation = ++openGeneration;
  open.value = true;
  position.value = null;
  await nextTick();
  if (!open.value || generation !== openGeneration) return;
  positionMenu();
  await nextTick();
  if (!open.value || generation !== openGeneration) return;
  const index = edge === "first" ? 0 : edge === "last" ? options.value.length - 1
    : Math.max(0, options.value.findIndex(option => option.value === props.modelValue));
  focusOption(index);
}

function select(value: string) {
  emit("change", value);
  closeMenu(true);
}

function menuKeydown(event: KeyboardEvent) {
  if (event.key === "Tab") { closeMenu(true); return; }
  const buttons = optionButtons();
  if (!buttons.length) return;
  const current = buttons.findIndex(button => button === document.activeElement);
  if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
    event.preventDefault();
    const index = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
      : (current + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
    focusOption(index);
  } else if (event.key.length === 1 && event.key !== " " && !event.isComposing && !event.ctrlKey && !event.altKey && !event.metaKey) {
    event.preventDefault();
    const now = Date.now();
    searchText = now - searchTime > 700 ? event.key : searchText + event.key;
    searchTime = now;
    const repeated = [...searchText].every(character => character === searchText[0]);
    const query = (repeated ? searchText[0] : searchText).toLocaleLowerCase();
    const start = repeated || searchText.length === 1 ? current + 1 : Math.max(0, current);
    for (let offset = 0; offset < options.value.length; offset += 1) {
      const index = (start + offset) % options.value.length;
      if (options.value[index]?.label.toLocaleLowerCase().startsWith(query)) { focusOption(index); break; }
    }
  }
}

function focusOut(event: FocusEvent) {
  if (!contains(event.relatedTarget)) closeMenu();
}
function dismiss(event: PointerEvent) {
  if (!contains(event.target)) closeMenu();
}
function viewportChanged(event: Event) {
  if (event.target instanceof Node && menu.value?.contains(event.target)) return;
  closeMenu();
}
function removeListeners() {
  document.removeEventListener("pointerdown", dismiss);
  window.removeEventListener("scroll", viewportChanged, true);
  window.removeEventListener("resize", viewportChanged);
  window.removeEventListener("blur", viewportChanged);
}

watch(open, value => {
  removeListeners();
  if (!value) return;
  document.addEventListener("pointerdown", dismiss);
  window.addEventListener("scroll", viewportChanged, true);
  window.addEventListener("resize", viewportChanged);
  window.addEventListener("blur", viewportChanged);
}, { flush: "sync" });
watch(() => [props.modelValue, props.scope, disabled.value], () => closeMenu());
watch(optionGroups, async () => {
  if (!open.value) return;
  const hadMenuFocus = menu.value?.contains(document.activeElement);
  await nextTick();
  if (!open.value) return;
  positionMenu();
  if (open.value && hadMenuFocus && !contains(document.activeElement)) {
    focusOption(Math.max(0, options.value.findIndex(option => option.value === props.modelValue)));
  }
});
onBeforeUnmount(() => { closeMenu(); removeListeners(); });
</script>

<template>
  <div ref="root" class="dashboard-select" @click.stop @focusout="focusOut" @keydown.esc.stop.prevent="closeMenu(true)">
    <button ref="trigger" class="select-trigger" type="button" aria-haspopup="listbox" :disabled="disabled"
      :data-value="modelValue" :aria-expanded="open" :aria-controls="open ? id : undefined" :aria-label="label" :aria-describedby="`${id}-selected`"
      @click="openMenu()" @keydown.down.prevent="openMenu('first')" @keydown.up.prevent="openMenu('last')">
      <svg class="select-icon" viewBox="0 0 24 24" aria-hidden="true">
        <path v-if="icon === 'filter'" d="M4 5h16l-6 7v6l-4 2v-8Z" />
        <template v-else-if="icon === 'calendar'"><rect x="3" y="5" width="18" height="16" rx="2" /><path d="M7 3v4m10-4v4M3 11h18" /></template>
        <template v-else><path d="m3 9 9-6 9 6H3Zm2 3v6m7-6v6m7-6v6M3 21h18" /></template>
      </svg>
      <span :id="`${id}-selected`" class="select-label">{{ selectedLabel }}</span>
      <svg class="select-chevron" :class="{ 'is-open': open }" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
    </button>
    <Teleport to="body">
      <div v-if="open" :id="id" ref="menu" class="select-menu" :style="menuStyle" role="listbox" :aria-label="label"
        @click.stop @focusout="focusOut" @keydown="menuKeydown" @keydown.esc.stop.prevent="closeMenu(true)">
        <div v-for="group in optionGroups" :key="group.label" class="select-group" role="group" :aria-label="group.label || undefined">
          <p v-if="group.label" class="select-group-label" aria-hidden="true">{{ group.label }}</p>
          <button v-for="option in group.options" :key="option.value" type="button" role="option" tabindex="-1"
            :data-value="option.value" :aria-selected="modelValue === option.value" :class="['select-option', { 'is-selected': modelValue === option.value }]"
            @click="select(option.value)">
            <span class="select-option-copy"><span>{{ option.label }}</span><small v-if="option.description">{{ option.description }}</small></span>
            <svg v-if="modelValue === option.value" class="select-check" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6" /></svg>
          </button>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.dashboard-select { position: relative; flex: 0 0 auto; width: var(--select-width, 202px); max-width: 100%; min-width: 0; }
.select-trigger { display: flex; align-items: center; gap: 9px; width: 100%; min-height: 44px; padding: 10px 12px; border: 1px solid var(--line); border-radius: var(--radius-control); background: var(--surface); color: var(--text); font-size: 13px; font-weight: 600; text-align: left; cursor: pointer; transition: border-color 150ms ease, background 150ms ease; }
.select-trigger:hover:not(:disabled), .select-trigger[aria-expanded="true"] { border-color: color-mix(in srgb, var(--accent) 45%, var(--line)); background: color-mix(in srgb, var(--accent-soft) 40%, var(--surface)); }
.select-trigger:disabled { opacity: .65; cursor: default; }
.select-label { display: -webkit-box; min-width: 0; overflow: hidden; line-height: 1.5; overflow-wrap: anywhere; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.dashboard-select svg, .select-menu svg { flex: 0 0 auto; width: 17px; height: 17px; fill: none; stroke: currentColor; stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }
.select-icon { color: var(--muted); }
.select-chevron { margin-left: auto; color: var(--muted); transition: transform 150ms ease; }
.select-chevron.is-open { transform: rotate(180deg); }
.select-menu { position: fixed; z-index: 80; overflow-y: auto; overscroll-behavior: contain; padding: 6px; border: 1px solid var(--line); border-radius: 13px; background: var(--surface); color: var(--text); box-shadow: var(--shadow-overlay); }
.select-group + .select-group { margin-top: 5px; padding-top: 5px; border-top: 1px solid var(--line); }
.select-group-label { margin: 4px 10px 5px; color: var(--muted); font-size: 11px; font-weight: 600; letter-spacing: .06em; }
.select-option { display: flex; align-items: center; justify-content: space-between; gap: 12px; width: 100%; min-height: 40px; padding: 9px 10px; border: 0; border-radius: 8px; background: transparent; color: var(--text); text-align: left; cursor: pointer; }
.select-option:hover, .select-option:focus-visible { background: var(--surface-soft); }
.select-option:focus-visible { outline: 2px solid var(--focus); outline-offset: -2px; }
.select-option.is-selected { background: var(--accent-soft); color: var(--accent); }
.select-option-copy { display: grid; gap: 2px; min-width: 0; font-size: 13px; line-height: 1.5; overflow-wrap: anywhere; }
.select-option-copy > span { font-weight: 600; }
.select-option-copy small { color: var(--muted); font-size: 11px; line-height: 1.5; }
.select-check { color: var(--accent); }
@media (max-width: 720px) { .dashboard-select { width: 100%; } .select-option { min-height: 44px; } }
@media (prefers-reduced-motion: reduce) { .select-trigger, .select-chevron { transition: none; } }
</style>
