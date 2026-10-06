import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey } from "vue";
import { renderToString } from "@vue/server-renderer";

const page = (cursor = "20") => ({ total_count: 45, filtered_count: 45, returned_count: 20, has_more: !!cursor, next_cursor: cursor || null });
const flush = async () => { await nextTick(); await nextTick(); };

test("automatic paging only reads the selected scope and preserves explicit retries", async t => {
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true }, plugins: [vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  try {
    const { default: component } = await server.ssrLoadModule("/src/components/DashboardPagination.vue");
    function mount(overrides = {}) {
      const events = [];
      const props = reactive({ page: page(), loaded: 20, automatic: true, active: false, scope: "company/month/v1", loading: false, error: "", ...overrides,
        onMore() { events.push("more"); props.loading = true; },
        onRetry() { events.push("retry"); props.error = ""; props.loading = true; },
        onPause(scope) { events.push(["pause", scope]); props.loading = false; },
      });
      let state;
      const host = createRenderer({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} });
      const wrapped = { ...component, ssrRender: undefined, setup(_actual, context) { state = component.setup(props, context); return state; }, render: () => h("div") };
      const app = host.createApp(wrapped, props);
      app.provide(ssrContextKey, { modules: new Set() }); app.mount({});
      let closed = false;
      return { props, events, state, close() { if (!closed) { closed = true; app.unmount(); } } };
    }
    await t.test("initial inactive overview makes no request; selected module reads 20 + 20 + 5 sequentially", async () => {
      const m = mount();
      try {
        await flush(); assert.deepEqual(m.events, []);
        m.props.active = true; await flush(); assert.deepEqual(m.events, ["more"]);
        await flush(); assert.equal(m.events.length, 1);
        m.props.page = page("40"); m.props.loaded = 40; m.props.loading = false;
        await flush(); assert.deepEqual(m.events, ["more", "more"]);
        m.props.page = page(""); m.props.loaded = 45; m.props.loading = false;
        await flush(); assert.equal(m.events.length, 2);
        m.props.active = false; await flush(); assert.equal(m.events.length, 2);
        m.props.active = true; await flush(); assert.equal(m.events.length, 2);
      } finally { m.close(); }
    });
    await t.test("deselect cancels in-flight reading, reselect resumes and new snapshot allows its own cursor", async () => {
      const m = mount();
      try {
        m.props.active = true; await flush();
        m.props.active = false; await flush();
        assert.deepEqual(m.events, ["more", ["pause", "company/month/v1"]]);
        m.props.active = true; await flush(); assert.equal(m.events.at(-1), "more");
        m.props.scope = "company/month/v2"; await flush();
        assert.deepEqual(m.events.slice(-2), [["pause", "company/month/v1"], "more"]);
        m.close(); assert.deepEqual(m.events.at(-1), ["pause", "company/month/v2"]);
      } finally { m.close(); }
    });
    await t.test("failure stops reading, keeps count and cursor, retry permits continuation", async () => {
      const m = mount();
      try {
        m.props.active = true; await flush();
        m.props.error = "网络读取失败"; m.props.loading = false; await flush();
        assert.equal(m.events.length, 1); assert.equal(m.props.loaded, 20);
        await flush(); assert.equal(m.events.length, 1);
        m.state.retry(); await flush(); assert.equal(m.events.at(-1), "retry");
        m.props.page = page("40"); m.props.loaded = 40; m.props.loading = false; await flush();
        assert.equal(m.events.at(-1), "more"); assert.equal(m.events.length, 3);
      } finally { m.close(); }
    });
    await t.test("non-advancing cursor cannot start an unbounded request loop", async () => {
      const m = mount();
      try {
        m.props.active = true; await flush();
        m.props.loading = false; await flush();
        assert.equal(m.events.length, 1); assert.match(m.state.progressError.value, /请重试/);
        m.state.retry(); await flush(); assert.equal(m.events.at(-1), "retry");
      } finally { m.close(); }
    });
    await t.test("a manually retried continuation also cancels when its module is deselected", async () => {
      const m = mount();
      try {
        m.props.active = true; await flush();
        m.props.error = "读取失败"; m.props.loading = false; await flush();
        m.state.retry(); await flush();
        m.props.active = false; await flush();
        assert.deepEqual(m.events, ["more", "retry", ["pause", "company/month/v1"]]);
        m.props.active = true; await flush(); assert.equal(m.events.at(-1), "more");
      } finally { m.close(); }
    });
    await t.test("automatic UI removes load-more, retains progress and retry; manual mode still works", async () => {
      const auto = await renderToString(createSSRApp(component, { page: page(), loaded: 20, automatic: true, active: false }));
      assert.doesNotMatch(auto, /加载更多/);
      const failed = await renderToString(createSSRApp(component, { page: page(), loaded: 20, automatic: true, active: false, error: "读取失败" }));
      assert.match(failed, /重试读取/);
      const manual = await renderToString(createSSRApp(component, { page: page(), loaded: 20 }));
      assert.match(manual, /加载更多/);
      const complete = await renderToString(createSSRApp(component, { page: page(""), loaded: 45, compact: true, automatic: true }));
      assert.doesNotMatch(complete, /已全部加载|已加载|加载更多/);
    });
  } finally { await server.close(); }
});
