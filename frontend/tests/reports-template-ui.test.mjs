import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createMemoryHistory, createRouter } from "vue-router";

function summaryCards(html) {
  const summary = /<section\b[^>]*class="summary-grid"[^>]*>([\s\S]*?)<\/section>/.exec(html)?.[1];
  assert.ok(summary, "quarterly summary is rendered");
  return [...summary.matchAll(/<article\b[^>]*>([\s\S]*?)<\/article>/g)].map(([, card]) => ({
    label: /<span\b[^>]*>([\s\S]*?)<\/span>/.exec(card)?.[1],
    value: /<strong\b[^>]*>([\s\S]*?)<\/strong>/.exec(card)?.[1],
    tone: /<strong\b[^>]*\bclass="([^"]*)"/.exec(card)?.[1] ?? "",
    note: /<small\b[^>]*>([\s\S]*?)<\/small>/.exec(card)?.[1],
  }));
}

test("report templates and quarterly summary preserve their formal statement values", async (t) => {
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
    for (const source of ["provisional", "mixed", "actual", "unestablished"]) {
      globalThis.reportTemplateUi.income_tax_source = source;
      const withTaxSource = await render();
      const provisionalNotice = /所得税含暂计金额，收到实际税额后由 AI 会计调整。/;
      if (["provisional", "mixed"].includes(source)) assert.match(withTaxSource, provisionalNotice);
      else assert.doesNotMatch(withTaxSource, provisionalNotice);
    }

    const operatingResponse = structuredClone(globalThis.reportTemplateUi);
    operatingResponse.statements.reverse();
    const profit = operatingResponse.statements.find(item => item.key === "profit_statement");
    Object.assign(profit.rows.find(row => row.line === 1), {
      name: "更名后的收入项目", values: { current_fen: "12345678", year_to_date_fen: "56789012" },
    });
    Object.assign(profit.rows.find(row => row.line === 2), {
      name: "更名后的成本项目", values: { current_fen: "4321098", year_to_date_fen: "13579024" },
    });
    Object.assign(profit.rows.find(row => row.line === 32), {
      name: "营业收入", values: { current_fen: "99999999", year_to_date_fen: "88888888" },
    });
    profit.rows.reverse();

    await t.test("four summary columns read quarter revenue and cost by statement and row identity", async () => {
      let expected;
      for (const [key, mode] of [["balance_sheet", false], ["profit_statement", true], ["cash_flow_statement", false]]) {
        statement = key;
        templateMode = mode;
        globalThis.reportTemplateUi = structuredClone(operatingResponse);
        server.moduleGraph.invalidateAll();
        const cards = summaryCards(await render());
        assert.deepEqual(cards.map(card => card.label), [
          "资产负债表 · 资产合计", "利润表 · 本季度营业收入", "利润表 · 本季度净利润", "现金流量表 · 本季度现金净增加额",
        ]);
        assert.equal(cards[1].value, "¥123,456.78");
        assert.equal(cards[1].note, "营业成本 ¥43,210.98");
        if (expected) assert.deepEqual(cards, expected, "statement selection and template mode do not change the summary");
        expected = cards;
      }
    });

    await t.test("replacing report responses preserves zero, unknown, negative and exact large amounts", async () => {
      for (const [revenue, cost, expectedRevenue, expectedCost] of [
        ["0", "0", "¥0.00", "¥0.00"],
        [null, "0", "暂无法确定", "¥0.00"],
        ["0", null, "¥0.00", "暂无法确定"],
        ["-12345", "-6789", "−¥123.45", "−¥67.89"],
        ["900719925474099301", "900719925474099299", "¥9,007,199,254,740,993.01", "¥9,007,199,254,740,992.99"],
      ]) {
        const response = structuredClone(operatingResponse);
        const rows = response.statements.find(item => item.key === "profit_statement").rows;
        rows.find(row => row.line === 1).values.current_fen = revenue;
        rows.find(row => row.line === 2).values.current_fen = cost;
        globalThis.reportTemplateUi = response;
        const card = summaryCards(await render())[1];
        assert.equal(card.value, expectedRevenue);
        assert.equal(card.note, `营业成本 ${expectedCost}`);
        assert.equal(card.tone, "", "operating revenue remains neutral for every amount and unknown state");
      }
    });

    await t.test("net profit color distinguishes profit and loss while zero and unknown stay neutral", async () => {
      for (const [amount, expectedValue, expectedTone] of [
        ["12345", "¥123.45", "gain"],
        ["-12345", "−¥123.45", "loss"],
        ["0", "¥0.00", ""],
        [null, "暂无法确定", ""],
      ]) {
        const response = structuredClone(operatingResponse);
        response.summary.current_net_profit_fen = amount;
        globalThis.reportTemplateUi = response;
        const card = summaryCards(await render())[2];
        assert.equal(card.value, expectedValue);
        assert.equal(card.tone, expectedTone, `net profit ${amount}`);
      }
    });

    await t.test("missing statement rows and current columns remain unknown instead of becoming zero", async () => {
      for (const [missing, expectedRevenue, expectedCost] of [
        ["revenue-row", "暂无法确定", "¥43,210.98"],
        ["cost-row", "¥123,456.78", "暂无法确定"],
        ["revenue-column", "暂无法确定", "¥43,210.98"],
        ["cost-column", "¥123,456.78", "暂无法确定"],
        ["statement", "暂无法确定", "暂无法确定"],
      ]) {
        const response = structuredClone(operatingResponse);
        const profitStatement = response.statements.find(item => item.key === "profit_statement");
        if (missing === "statement") response.statements = response.statements.filter(item => item !== profitStatement);
        else {
          const line = missing.startsWith("revenue") ? 1 : 2;
          if (missing.endsWith("row")) profitStatement.rows = profitStatement.rows.filter(row => row.line !== line);
          else delete profitStatement.rows.find(row => row.line === line).values.current_fen;
        }
        globalThis.reportTemplateUi = response;
        const card = summaryCards(await render())[1];
        assert.equal(card.value, expectedRevenue, missing);
        assert.equal(card.note, `营业成本 ${expectedCost}`, missing);
      }
    });
  } finally {
    await server.close();
    delete globalThis.reportTemplateUi;
  }
});
