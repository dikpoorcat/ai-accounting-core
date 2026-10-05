import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createMemoryHistory, createRouter } from "vue-router";

test("report template renders the switch, organization, all rows and three original forms", async () => {
  const samples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));
  globalThis.reportTemplateUi = structuredClone(samples.quarterly_report.response);
  Object.assign(globalThis.reportTemplateUi.organization, { name: "合成报表测试公司", taxpayer_identification_number: "合成识别号" });
  globalThis.reportTemplateUi.statements = ["balance_sheet", "profit_statement", "cash_flow_statement"].map(key => ({
    key, label: key, columns: [{ key: "current_fen", label: "本期金额" }, { key: "previous_fen", label: "上期金额" }],
    rows: Array.from({ length: key === "balance_sheet" ? 53 : 32 }, (_, index) => ({
      line: index + 1, name: `合成项目${index + 1}`, has_amount: false, is_total: false,
      values: { current_fen: index === 0 ? "900719925474099301" : "0", previous_fen: index === 0 ? null : "0" },
    })),
  }));
  let statement = "balance_sheet", templateMode = true;
  const server = await createServer({
    configFile: false,
    optimizeDeps: { noDiscovery: true, include: [] },
    root: fileURLToPath(new URL("..", import.meta.url)),
    plugins: [{ name: "synthetic-report-template", enforce: "pre", transform(code, id) {
      if (!id.endsWith("/ReportsView.vue")) return;
      return code.replace("ref<DeferredQuarterlyReport | null>(null)", "ref<DeferredQuarterlyReport | null>(globalThis.reportTemplateUi)")
        .replace("const statementsExpanded = ref(false)", "const statementsExpanded = ref(true)")
        .replace("const taxTemplateMode = ref(false)", `const taxTemplateMode = ref(${templateMode})`)
        .replace('const activeStatementKey = ref("")', `const activeStatementKey = ref("${statement}")`);
    } }, vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom",
  });
  async function render() {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/", name: "reports", component: {} }] });
    await router.push("/?company_id=synthetic&quarter=2026-Q1");
    const { default: component } = await server.ssrLoadModule("/src/views/ReportsView.vue");
    return renderToString(createSSRApp(component).use(router));
  }
  try {
    for (const [key, code] of [["balance_sheet", "会小企01表"], ["profit_statement", "会小企02表"], ["cash_flow_statement", "会小企03表"]]) {
      statement = key;
      server.moduleGraph.invalidateAll();
      const html = await render();
      assert.match(html, /税务局模板格式/);
      assert.match(html, /<input(?=[^>]*role="switch")(?=[^>]*checked)[^>]*>/);
      assert.match(html, new RegExp(code));
      assert.match(html, /合成报表测试公司/);
      assert.match(html, /纳税人识别号/);
      assert.match(html, /9,007,199,254,740,993\.01/);
      assert.match(html, /mobile-template-table/);
      assert.match(html, /合成项目32/);
      if (key === "balance_sheet") assert.match(html, /合成项目53/);
    }
    templateMode = false;
    server.moduleGraph.invalidateAll();
    const html = await render();
    assert.doesNotMatch(html, /class="tax-template-sheet/);
    assert.match(html, /税务局模板格式/);
    assert.doesNotMatch(html, /合成项目32/);
  } finally {
    await server.close();
    delete globalThis.reportTemplateUi;
  }
});
