import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createMemoryHistory, createRouter } from "vue-router";

const samples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));

test("business detail renders owner amounts without payroll confirmation evidence", async () => {
  const data = structuredClone(samples.business_status.response.data);
  data.current_business_result = { posting_period: "2026-02", amount_label: "工资金额", amount_fen: "12500" };
  data.frozen_adoption = { close_period: "2026-01", amount_label: "已确认工资", amount_fen: "12345" };
  globalThis.payrollConfirmationBusinessDetail = data;
  const server = await createServer({
    root: fileURLToPath(new URL("..", import.meta.url)),
    configFile: false,
    optimizeDeps: { noDiscovery: true },
    plugins: [{
      name: "seed-payroll-confirmation-business-detail",
      enforce: "pre",
      transform(code, id) {
        if (id.replaceAll("\\", "/").endsWith("/src/components/BusinessStatusDetails.vue")) {
          return code.replace(
            "ref<BusinessStatusData | null>(null)",
            "ref(globalThis.payrollConfirmationBusinessDetail)",
          );
        }
      },
    }, vue()],
    server: { middlewareMode: true, hmr: false, ws: false },
    appType: "custom",
  });
  try {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: "/", component: {} }],
    });
    await router.push("/?company_id=co&period=2026-01");
    const { default: component } = await server.ssrLoadModule(
      "/src/components/BusinessStatusDetails.vue",
    );
    const app = createSSRApp(component, {
      subjectId: data.identity.subject_id,
      period: "2026-01",
      snapshotVersion: "fixture",
    });
    app.use(router);
    const html = await renderToString(app);

    assert.match(html, /已确认工资/);
    assert.match(html, /关账时已确认工资/);
    assert.match(html, /关账月份/);
    assert.match(html, /123\.45/);
    const frozenAmount = html.match(/<dl class="business-amounts"[\s\S]*?<\/dl>/)?.[0];
    assert(frozenAmount);
    assert.doesNotMatch(frozenAmount, /125\.00/);
    assert.doesNotMatch(html, /工资确认依据|负责人确认本月工资方案|fact-plan-exact|plan-evidence-digest|calculation_id|selection_proof|<pre/);

    data.frozen_adoption = null;
    const currentApp = createSSRApp(component, {
      subjectId: data.identity.subject_id, period: "2026-02", snapshotVersion: "fixture",
    });
    currentApp.use(router);
    const currentHtml = await renderToString(currentApp);
    assert.match(currentHtml, /当前工资金额/);
    assert.match(currentHtml, /125\.00/);
    assert.match(currentHtml, /该金额入账月/);
    assert.match(currentHtml, /以上是当前业务结果/);
    const currentAmount = currentHtml.match(/<dl class="business-amounts"[\s\S]*?<\/dl>/)?.[0];
    assert(currentAmount);
    assert.doesNotMatch(currentAmount, /关账时已确认工资|123\.45/);

  } finally {
    await server.close();
    delete globalThis.payrollConfirmationBusinessDetail;
  }
});

