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
    addEventListener(name, callback) {
      if (!listeners.has(name)) listeners.set(name, new Set());
      listeners.get(name).add(callback);
    },
    removeEventListener(name, callback) { listeners.get(name)?.delete(callback); },
    scrollTo(options) { scrolls.push(options); this.scrollY = options.top; },
  };
  const document = {
    activeElement: null,
    documentElement: { style: { scrollBehavior: "smooth" }, clientWidth: 1440 },
    getElementById: id => elements.get(id) ?? null,
    querySelector: selector => selector === ".section-nav" ? nav : null,
  };
  class Element {
    constructor(id, top, { height = 200, input = false } = {}) {
      this.id = id; this.top = top; this.height = height; this.input = input;
      this.attributes = new Map(); this.focusCalls = [];
    }
    getBoundingClientRect() { return { top: this.top - window.scrollY, height: this.height }; }
    getClientRects() { return [this.getBoundingClientRect()]; }
    hasAttribute(name) { return this.attributes.has(name); }
    setAttribute(name, value) { this.attributes.set(name, value); }
    matches() { return this.hasAttribute("tabindex"); }
    querySelector() { return null; }
    closest() { return this.input ? this : null; }
    focus(options) { this.focusCalls.push(options); document.activeElement = this; }
  }
  const nav = new Element("nav", 8, { height: 44 });
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
