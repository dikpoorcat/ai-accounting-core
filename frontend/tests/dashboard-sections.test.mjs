import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import * as Vue from "vue";
import ts from "typescript";

let sequence = 0;
async function harness({ hash = "", links = [] } = {}) {
  const route = Vue.reactive({ hash });
  const items = Vue.ref(links);
  const elements = new Map(), listeners = new Map(), mounted = [], unmounted = [], scrolls = [];
  const window = {
    scrollY: 0,
    innerHeight: 1000,
    addEventListener(name, callback) {
      if (!listeners.has(name)) listeners.set(name, new Set());
      listeners.get(name).add(callback);
    },
    removeEventListener(name, callback) { listeners.get(name)?.delete(callback); },
    scrollTo(options) { scrolls.push(options); this.scrollY = options.top; },
  };
  const document = {
    activeElement: null,
    documentElement: { style: { scrollBehavior: "smooth" }, clientWidth: 1440, scrollHeight: 5000 },
    getElementById: id => elements.get(id) ?? null,
    querySelector: selector => selector === ".section-nav" ? nav : null,
  };
  class Element {
    constructor(id, top, { height = 200, input = false, tag = "div", section = true, parent = null, panel = false } = {}) {
      this.id = id; this.top = top; this.height = height; this.input = input;
      this.tag = input ? "input" : tag; this.section = section; this.parent = parent; this.panel = panel;
      this.attributes = new Map(); this.focusCalls = [];
    }
    getBoundingClientRect() { return { top: this.top - window.scrollY, bottom: this.top - window.scrollY + this.height, height: this.height }; }
    getClientRects() { return [this.getBoundingClientRect()]; }
    get parentElement() { return this.parent; }
    hasAttribute(name) { return this.attributes.has(name); }
    setAttribute(name, value) { this.attributes.set(name, value); }
    matches() { return this.hasAttribute("tabindex"); }
    querySelector() { return null; }
    closest(selector) {
      for (let element = this; element; element = element.parent) {
        if (selector.split(",").some(part => {
          const match = part.trim();
          return match === ".section-anchor" ? element.section
            : match === "section" ? element.tag === "section"
            : match === "[data-section-focus]" ? element.panel
            : ["button", "a", "summary", "input", "select", "textarea"].includes(match) && element.tag === match;
        })) return element;
      }
      return null;
    }
    focus(options) {
      this.focusCalls.push(options); document.activeElement = this;
      listeners.get("focusin")?.forEach(callback => callback({ target: this }));
    }
  }
  const nav = new Element("nav", 8, { height: 44, section: false });
  const key = `dashboardSections${++sequence}`;
  globalThis[key] = { Vue, route, mounted, unmounted, window, document, Element, nav };
  const source = readFileSync(new URL("../src/composables/useDashboardSections.ts", import.meta.url), "utf8")
    .replace(/import[^;]+;/g, "");
  const { outputText } = ts.transpileModule(`
    const environment = globalThis.${key};
    const { nextTick, ref, toValue, watch } = environment.Vue;
    const { window, document, Element } = environment;
    const getComputedStyle = () => ({ top: String(environment.nav.top) });
    const useRoute = () => environment.route;
    const onMounted = callback => environment.mounted.push(callback);
    const onBeforeUnmount = callback => environment.unmounted.push(callback);
    ${source}
  `, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
  const { useDashboardSections } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
  delete globalThis[key];
  const scope = Vue.effectScope();
  const result = {
    route, items, nav, window, document, listeners, scrolls,
    add(id, top, options) { const element = new Element(id, top, options); elements.set(id, element); return element; },
    mount() {
      Object.assign(result, scope.run(() => useDashboardSections(items, "overview")));
      mounted.forEach(callback => callback());
    },
    dispatch(name, event = {}) { listeners.get(name)?.forEach(callback => callback(event)); },
    close() { unmounted.forEach(callback => callback()); scope.stop(); },
  };
  return result;
}
async function flush() { for (let index = 0; index < 3; index++) await Vue.nextTick(); }
const links = [{ id: "overview", label: "概览" }, { id: "employees", label: "员工明细" }];

test("section navigation resolves a deep link when its deferred section appears", async () => {
  const h = await harness({ hash: "#employees", links: links.slice(0, 1) });
  try {
    h.add("overview", 80);
    h.mount(); await flush();
    assert.equal(h.scrolls.length, 0);
    const employeeSection = h.add("employees", 1000);
    h.items.value = links;
    await flush();
    assert.equal(h.activeSection.value, "employees");
    assert.equal(h.window.scrollY, 940, "navigation clears the measured 44px nav, its 8px inset and 8px gap");
    assert.equal(h.document.activeElement, employeeSection);
    assert.deepEqual(employeeSection.focusCalls, [{ preventScroll: true }]);
    assert.equal(h.document.documentElement.style.scrollBehavior, "smooth");
  } finally { h.close(); }
});

test("replacing paginated page data with the same section IDs preserves the reading position", async () => {
  const h = await harness({ hash: "#employees", links });
  try {
    h.add("overview", 80); h.add("employees", 1000);
    h.mount(); await flush();
    const count = h.scrolls.length;
    assert.equal(count, 1);
    h.window.scrollY = 1400;
    h.items.value = links.map(item => ({ ...item }));
    await flush();
    assert.equal(h.scrolls.length, count, "appending a page must not follow the unchanged URL hash again");
    assert.equal(h.window.scrollY, 1400);
  } finally { h.close(); }
});

test("navigation remeasures its height, follows user scrolling, and releases listeners on unmount", async () => {
  const h = await harness({ links });
  try {
    h.add("overview", 80); const employeeSection = h.add("employees", 1000);
    h.mount(); await flush();
    h.nav.height = 70; h.nav.top = 4;
    h.focusSection("employees");
    assert.equal(h.window.scrollY, 918);
    h.window.scrollY = 0;
    h.dispatch("scroll");
    assert.equal(h.activeSection.value, "employees", "programmatic navigation stays selected until user movement");
    h.dispatch("wheel"); h.dispatch("scroll");
    assert.equal(h.activeSection.value, "overview");
    h.window.scrollY = 950;
    h.dispatch("scroll");
    assert.equal(h.activeSection.value, "employees");
    assert.equal(h.document.activeElement, employeeSection, "scroll highlighting does not steal focus");
    assert([...h.listeners.values()].some(callbacks => callbacks.size > 0));
    h.close();
    assert([...h.listeners.values()].every(callbacks => callbacks.size === 0));
    h.window.scrollY = 0; h.dispatch("wheel"); h.dispatch("scroll");
    assert.equal(h.activeSection.value, "employees");
  } finally { h.close(); }
});

test("bottom scrolling selects the section at the viewport midpoint rather than the preceding or final section", async () => {
  const h = await harness({ links: [
    { id: "overview", label: "概览" }, { id: "workforce", label: "用工成本" },
    { id: "open-items", label: "待收待付" }, { id: "owner-tasks", label: "老板待办" },
  ] });
  try {
    h.add("overview", 80); h.add("workforce", 1280, { height: 258 });
    h.add("open-items", 1570, { height: 629 }); h.add("owner-tasks", 2231, { height: 103 });
    h.document.documentElement.scrollHeight = 2390;
    h.mount(); await flush();
    h.window.scrollY = 1390;
    h.dispatch("wheel"); h.dispatch("scroll");
    assert.equal(h.activeSection.value, "open-items", "at the bottom, the visible payment section must start its continuation");
    assert.equal(h.scrolls.length, 0, "scroll highlighting must not reposition the page");
    h.window.scrollY = 1300;
    h.dispatch("scroll");
    assert.equal(h.activeSection.value, "workforce", "away from the bottom, the header probe still determines the active section");
  } finally { h.close(); }
});

test("clicking controls and rows activates their nearest known section without requiring an anchor class or scrolling", async () => {
  const h = await harness({ links });
  try {
    const overview = h.add("overview", 80), employees = h.add("employees", 1000, { section: false });
    const unknown = h.add("unregistered", 1500);
    const button = h.add("filter", 1050, { tag: "button", section: false, parent: employees });
    const label = h.add("filter-label", 1050, { section: false, parent: button });
    const row = h.add("row", 1100, { section: false, parent: employees, panel: true });
    h.mount(); await flush();
    for (const target of [label, row]) {
      h.focusSection("overview");
      const beforeScrolls = h.scrolls.length, beforeY = h.window.scrollY;
      h.focusSelectedPanel({ target });
      assert.equal(h.activeSection.value, "employees");
      assert.equal(h.scrolls.length, beforeScrolls);
      assert.equal(h.window.scrollY, beforeY);
      if (target === label) assert.equal(h.document.activeElement, overview, "control click must retain the existing focus behavior");
      else assert.equal(h.document.activeElement, row);
    }
    h.focusSelectedPanel({ target: unknown });
    assert.equal(h.activeSection.value, "employees", "unknown section anchors are ignored");
    h.focusSelectedPanel({ target: {} });
    assert.equal(h.activeSection.value, "employees");
  } finally { h.close(); }
});

test("keyboard focus activates its section and the focus listener is removed on unmount", async () => {
  const h = await harness({ links });
  try {
    h.add("overview", 80); const employees = h.add("employees", 1000);
    const input = h.add("employee-filter", 1050, { input: true, section: false, parent: employees });
    const unknown = h.add("unknown", 1600);
    h.mount(); await flush();
    h.focusSection("overview");
    const beforeScrolls = h.scrolls.length, beforeY = h.window.scrollY;
    input.focus({ preventScroll: true });
    assert.equal(h.activeSection.value, "employees");
    assert.equal(h.document.activeElement, input);
    assert.equal(h.scrolls.length, beforeScrolls);
    assert.equal(h.window.scrollY, beforeY);
    h.dispatch("focusin", { target: unknown });
    h.dispatch("focusin", { target: {} });
    assert.equal(h.activeSection.value, "employees");
    assert.equal(h.listeners.get("focusin")?.size, 1);
    h.close();
    assert.equal(h.listeners.get("focusin")?.size, 0);
    h.dispatch("focusin", { target: h.document.getElementById("overview") });
    assert.equal(h.activeSection.value, "employees");
  } finally { h.close(); }
});

test("heading anchors activate the containing section when its record receives keyboard focus", async () => {
  const h = await harness({ links });
  try {
    h.add("overview", 80);
    const section = h.add("employee-panel", 1000, { tag: "section", section: false });
    h.add("employees", 1020, { tag: "h2", section: false, parent: section });
    const row = h.add("employee-row", 1100, { section: false, parent: section });
    h.mount(); await flush();
    h.focusSection("overview");
    const beforeScrolls = h.scrolls.length;
    row.focus({ preventScroll: true });
    assert.equal(h.activeSection.value, "employees");
    assert.equal(h.scrolls.length, beforeScrolls);
  } finally { h.close(); }
});

test("pointer focus waits for the control's click before activating its collection", async () => {
  const h = await harness({ links });
  try {
    h.add("overview", 80); const employees = h.add("employees", 1000);
    const control = h.add("display-tab", 1050, { tag: "button", section: false, parent: employees });
    h.mount(); await flush();
    h.dispatch("pointerdown", { clientX: 500 });
    control.focus({ preventScroll: true });
    assert.equal(h.activeSection.value, "overview", "pointer focus must not start reading the control's old display mode");
    h.dispatch("pointerup");
    h.focusSelectedPanel({ target: control });
    assert.equal(h.activeSection.value, "employees");
    h.focusSection("overview");
    h.dispatch("pointerdown", { clientX: 500 }); h.dispatch("pointercancel");
    control.focus({ preventScroll: true });
    assert.equal(h.activeSection.value, "employees");
  } finally { h.close(); }
});
