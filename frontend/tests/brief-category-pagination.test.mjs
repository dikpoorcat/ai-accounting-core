import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createRenderer, h, nextTick, reactive, ssrContextKey } from "vue";
import { createMemoryHistory, createRouter } from "vue-router";

const flush = async () => { await nextTick(); await nextTick(); };
test("business root continuation follows only the incomplete selected category", async t => {
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true }, plugins: [vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  try {
    const { default: pagination } = await server.ssrLoadModule("/src/components/DashboardPagination.vue");
    for (const section of ["activity", "open_items"]) await t.test(section, async () => {
      const { default: component } = await server.ssrLoadModule(section === "activity" ? "/src/components/brief/BriefActivityWorkbench.vue" : "/src/components/brief/BriefOpenItems.vue");
      const item = (key, category) => ({ group_key: key, group: category, category_key: category });
      const categories = [{ key: "a", label: "甲分类", group_count: 20, count: 20, direction: "payable", type_counts: [] }, { key: "b", label: "乙分类", group_count: 2, count: 2, direction: "payable", type_counts: [] }];
      const props = reactive({ items: Array.from({ length: 20 }, (_, n) => item(`a-${n}`, "a")), groups: categories, openItems: { categories }, vouchers: [], voucherCount: 0, activityCount: 22, period: "2026-09", periodLabel: "9月", periodStatus: "open", snapshotVersion: "v1" });
      const paging = reactive({ page: { total_count: 22, filtered_count: 22, returned_count: 20, has_more: true, next_cursor: "20" }, loaded: 20, loading: false, active: true });
      const events = [];
      let state;
      const host = createRenderer({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} });
      const wrapped = { ...component, ssrRender: undefined, setup(_props, context) { state = component.setup(props, context); return state; }, render() { return h(pagination, { ...paging, automatic: true, scope: props.snapshotVersion, active: paging.active && state.categoryNeedsMore.value, onMore() { events.push("more"); paging.loading = true; }, onPause(scope) { events.push(["pause", scope]); paging.loading = false; } }); } };
      const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/", component: {} }] }); await router.push("/?company_id=co");
      const app = host.createApp(wrapped, props); app.use(router); app.provide(ssrContextKey, { modules: new Set() }); app.mount({});
      const select = key => { if (section === "activity") state.selectedGroup.value = key; else state.selectCategory(key); };
      try {
        await flush(); assert.deepEqual(events, [], "complete selected category does not read another category");
        select("b"); await flush(); assert.deepEqual(events, ["more"]);
        select("a"); await flush(); assert.deepEqual(events.at(-1), ["pause", "v1"], "switching to a complete category cancels continuation");
        select("b"); await flush(); assert.equal(events.at(-1), "more", "unconsumed global cursor is reusable after cancellation");
        props.items.push(item("b-0", "b")); paging.loaded = 21; paging.page = { ...paging.page, next_cursor: "21" }; paging.loading = false;
        await flush(); assert.equal(events.filter(event => event === "more").length, 3, "partially loaded category continues");
        props.items.push(item("b-1", "b")); paging.loaded = 22; paging.loading = false;
        await flush(); assert.equal(state.categoryNeedsMore.value, false); assert.equal(events.filter(event => event === "more").length, 3);
        if (section === "activity") {
          props.items = props.items.filter(item => item.group_key !== "b-1"); props.focusedActivityGroup = item("b-1", "b");
          await flush(); assert.equal(state.categoryNeedsMore.value, false, "precisely inserted group counts once");
          props.items.push(item("b-1", "b")); await flush(); assert.equal(state.visibleItems.value.length, 2);
        }
        props.items = props.items.filter(item => item.group_key.startsWith("a-")); props.focusedActivityGroup = null; props.snapshotVersion = "v2";
        await flush(); assert.equal(events.at(-1), "more", "new snapshot reads the selected missing category again");
        paging.active = false; await flush(); assert.deepEqual(events.at(-1), ["pause", "v2"]);
      } finally { app.unmount(); }
    });
  } finally { await server.close(); }
});
