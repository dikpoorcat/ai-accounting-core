import { nextTick, onBeforeUnmount, onMounted, ref, toValue, watch, type MaybeRefOrGetter } from "vue";
import { useRoute } from "vue-router";

type SectionLink = { id: string; label: string };

// The five pages share anchor placement and scroll highlighting, including lazy sections.
export function useDashboardSections(items: MaybeRefOrGetter<readonly SectionLink[]>, initialId: string) {
  const route = useRoute();
  const activeSection = ref(initialId);
  let locked = false;
  let mounted = false;
  let pointerFocusing = false;
  function offset() {
    const nav = document.querySelector<HTMLElement>(".section-nav");
    const sticky = nav?.closest<HTMLElement>("[data-section-header]") ?? nav;
    return sticky ? sticky.getBoundingClientRect().height + (parseFloat(getComputedStyle(sticky).top) || 0) + 8 : 16;
  }
  function lockSectionSync() { locked = true; }
  function positionSection(section: HTMLElement) {
    const root = document.documentElement, previous = root.style.scrollBehavior;
    root.style.scrollBehavior = "auto";
    window.scrollTo({ top: Math.max(0, window.scrollY + section.getBoundingClientRect().top - offset()), behavior: "auto" });
    root.style.scrollBehavior = previous;
  }
  function focusSection(id: string) {
    const section = document.getElementById(id);
    if (!section) return;
    lockSectionSync();
    activeSection.value = id;
    positionSection(section);
    const content = section.querySelector<HTMLElement>("[data-section-focus]")
      ?? section.closest<HTMLElement>(".panel")?.querySelector<HTMLElement>("[data-section-focus]");
    const target = content ?? (section.matches("[tabindex]") ? section : section.querySelector<HTMLElement>("[tabindex], h2, h3"));
    const focusTarget = target ?? section;
    if (!focusTarget.hasAttribute("tabindex")) focusTarget.setAttribute("tabindex", "-1");
    focusTarget.focus({ preventScroll: true });
  }
  function focusSelectedPanel(event: MouseEvent) {
    activateSectionFromTarget(event);
    if (!(event.target instanceof Element) || event.target.closest("button, a, summary, input, select, textarea")) return;
    const panel = event.target.closest<HTMLElement>("[data-section-focus], .selectable-card")
      ?? event.target.closest<HTMLElement>(".selectable-section, .section-panel, .panel, .report-review")
        ?.querySelector<HTMLElement>("[data-section-focus]");
    panel?.focus({ preventScroll: true });
  }
  function activateSectionFromTarget(event: Event) {
    if (!(event.target instanceof Element)) return;
    const sections = toValue(items).map(item => {
      const anchor = document.getElementById(item.id);
      return { id: item.id, element: anchor?.closest("section") ?? anchor };
    });
    for (let element: Element | null = event.target; element; element = element.parentElement) {
      const section = sections.find(item => item.element === element);
      if (!section) continue;
      lockSectionSync();
      activeSection.value = section.id;
      return;
    }
  }
  function activateFocusedSection(event: FocusEvent) {
    // Pointer clicks activate after the control has applied its tab or filter change.
    if (!pointerFocusing) activateSectionFromTarget(event);
  }
  function updateSectionFromScroll() {
    if (locked) return;
    const sections = toValue(items).map(item => ({ id: item.id, element: document.getElementById(item.id) }))
      .filter(item => item.element && item.element.getClientRects().length)
      .sort((a, b) => a.element!.getBoundingClientRect().top - b.element!.getBoundingClientRect().top);
    if (!sections.length) return;
    // Near the document end, the next section cannot reach the sticky header.
    const atEnd = Math.ceil(window.scrollY + window.innerHeight) >= document.documentElement.scrollHeight;
    const probe = atEnd ? Math.max(offset(), window.innerHeight / 2) : offset();
    let candidate = sections[0].id;
    for (const section of sections) {
      if (section.element!.getBoundingClientRect().top > probe) break;
      candidate = section.id;
    }
    const current = document.getElementById(activeSection.value);
    if (current && candidate !== activeSection.value && current.getBoundingClientRect().top <= probe + 24
      && current.getBoundingClientRect().top > probe) return;
    activeSection.value = candidate;
  }
  function unlock() { locked = false; }
  function scrollKey(event: KeyboardEvent) {
    pointerFocusing = false;
    if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(event.key)
      && !(event.target instanceof Element && event.target.closest("input, select, textarea, [contenteditable=true]"))) unlock();
  }
  function scrollbarPointer(event: PointerEvent) {
    pointerFocusing = true;
    if (event.clientX >= document.documentElement.clientWidth) unlock();
  }
  function releasePointerFocus() { pointerFocusing = false; }
  watch([() => route.hash, () => toValue(items).map(item => item.id).join("|")], async ([hash]) => {
    const links = toValue(items);
    if (!links.some(item => item.id === activeSection.value)) activeSection.value = initialId;
    await nextTick();
    if (!mounted) return;
    const target = links.find(item => `#${item.id}` === hash);
    if (target) focusSection(target.id);
  }, { flush: "post", immediate: true });
  onMounted(() => {
    mounted = true;
    window.addEventListener("scroll", updateSectionFromScroll, { passive: true });
    window.addEventListener("wheel", unlock, { passive: true });
    window.addEventListener("touchmove", unlock, { passive: true });
    window.addEventListener("keydown", scrollKey);
    window.addEventListener("pointerdown", scrollbarPointer);
    window.addEventListener("pointerup", releasePointerFocus);
    window.addEventListener("pointercancel", releasePointerFocus);
    window.addEventListener("focusin", activateFocusedSection);
  });
  onBeforeUnmount(() => {
    mounted = false;
    window.removeEventListener("scroll", updateSectionFromScroll);
    window.removeEventListener("wheel", unlock);
    window.removeEventListener("touchmove", unlock);
    window.removeEventListener("keydown", scrollKey);
    window.removeEventListener("pointerdown", scrollbarPointer);
    window.removeEventListener("pointerup", releasePointerFocus);
    window.removeEventListener("pointercancel", releasePointerFocus);
    window.removeEventListener("focusin", activateFocusedSection);
  });
  return { activeSection, focusSection, focusSelectedPanel, positionSection, lockSectionSync };
}
