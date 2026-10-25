import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";

const statements = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"))
  .quarterly_report.response.statements;
const defaults = {
  balance_sheet: [15, 29, 30, 41, 46, 47, 52, 53],
  profit_statement: [1, 2, 3, 11, 14, 18, 20, 21, 22, 24, 30, 31, 32],
  cash_flow_statement: [7, 13, 19, 20, 21, 22],
};
const copyStatement = key => structuredClone(statements.find(statement => statement.key === key));
const plainText = html => html.replace(/<[^>]*>/g, "").trim();

function renderedRows(html) {
  assert.match(html, /class="[^"]*\bstatement-table-wrap\b/);
  return [...html.matchAll(/<tr\b([^>]*)>([\s\S]*?)<\/tr>/g)]
    .filter(([, attributes]) => /\bdata-line="\d+"/.test(attributes))
    .map(([, attributes, content]) => ({
      line: Number(/\bdata-line="(\d+)"/.exec(attributes)[1]),
      depth: Number(/\bdata-depth="(\d+)"/.exec(attributes)?.[1]),
      attributes,
      content,
      cells: [...content.matchAll(/<td\b[^>]*>([\s\S]*?)<\/td>/g)].map(([, cell]) => plainText(cell)),
    }));
}

function rowAt(rows, line) {
  const row = rows.find(item => item.line === line);
  assert.ok(row, `formal row ${line} is rendered`);
  return row;
}

function expectLines(rows, expected) {
  assert.deepEqual(rows.map(row => row.line), expected);
  assert.equal(new Set(rows.map(row => row.line)).size, rows.length, "formal rows occur once");
}

function bulkButton(html, label) {
  return [...html.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)]
    .find(([, , content]) => plainText(content) === label);
}

test("ordinary financial statements preserve all formal rows within accounting disclosure groups", async t => {
  const server = await createServer({
    configFile: false, root: fileURLToPath(new URL("..", import.meta.url)),
    optimizeDeps: { noDiscovery: true, include: [] }, plugins: [vue()],
    server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom",
  });
  try {
    const { default: component } = await server.ssrLoadModule("/src/components/ReportStatementTable.vue");
    const render = (statement, expandedRows) => renderToString(createSSRApp(component, {
      statement, ...(expandedRows === undefined ? {} : { expandedRows }),
    }));

    await t.test("default summaries retain every zero and unknown main row", async () => {
      for (const [key, expected] of Object.entries(defaults)) {
        const statement = copyStatement(key);
        for (const row of statement.rows) {
          row.has_amount = false;
          row.is_total = false;
          row.values[statement.columns[0].key] = "0";
          row.values[statement.columns[1].key] = null;
        }
        const rows = renderedRows(await render(statement));
        expectLines(rows, expected);
        for (const row of rows) {
          assert.equal(row.depth, 0);
          assert.deepEqual(row.cells.slice(-2), ["¥0.00", "暂无法确定"]);
        }
      }
    });

    await t.test("expanding complete real fixtures reveals 53, 32 and 22 unique formal rows", async () => {
      for (const [key, count] of [["balance_sheet", 53], ["profit_statement", 32], ["cash_flow_statement", 22]]) {
        const statement = copyStatement(key);
        const rows = renderedRows(await render(statement, statement.rows.map(row => row.line)));
        assert.equal(rows.length, count);
        assert.deepEqual(rows.map(row => row.line).sort((a, b) => a - b), Array.from({ length: count }, (_, index) => index + 1));
        assert.equal(new Set(rows.map(row => row.line)).size, count);
        for (const source of statement.rows) assert.ok(rowAt(rows, source.line).content.includes(source.name));
      }
    });

    await t.test("nested inventory and fixed-asset details require their visible ancestors to expand", async () => {
      const statement = copyStatement("balance_sheet");
      expectLines(renderedRows(await render(statement, [9, 20])), defaults.balance_sheet);
      const firstLevel = renderedRows(await render(statement, [15]));
      assert.equal(rowAt(firstLevel, 9).depth, 1);
      assert.ok(!firstLevel.some(row => [10, 11, 12, 13, 18, 19].includes(row.line)));
      const nested = renderedRows(await render(statement, [15, 9, 29, 20]));
      for (const line of [10, 11, 12, 13, 18, 19]) assert.equal(rowAt(nested, line).depth, 2);
      for (const line of [9, 16, 17, 20, 21, 22, 23, 24, 25, 26, 27, 28]) assert.equal(rowAt(nested, line).depth, 1);
      assert.match(rowAt(nested, 9).content, /<button\b[^>]*aria-expanded="true"/);
      assert.ok(rowAt(nested, 9).content.includes(`aria-label="收起${statement.rows.find(row => row.line === 9).name}明细"`));
      assert.ok(rowAt(firstLevel, 9).content.includes(`aria-label="展开${statement.rows.find(row => row.line === 9).name}明细"`));
    });

    await t.test("partial disclosure never replaces its parent value or reverses depreciation signs", async () => {
      const statement = copyStatement("balance_sheet");
      const values = { 9: "10000", 10: "1000", 11: "2000", 12: "0", 13: null, 18: "10000", 19: "1234", 20: "8766" };
      for (const [line, amount] of Object.entries(values)) statement.rows.find(row => row.line === Number(line)).values.ending_fen = amount;
      const rows = renderedRows(await render(statement, [15, 9, 29, 20]));
      assert.equal(rowAt(rows, 9).cells.at(-2), "¥100.00");
      assert.equal(rowAt(rows, 13).cells.at(-2), "暂无法确定");
      assert.equal(rowAt(rows, 19).cells.at(-2), "¥12.34");
      assert.ok(rowAt(rows, 19).content.includes("减：累计折旧"));
      assert.equal(rowAt(rows, 20).cells.at(-2), "¥87.66");
    });

    await t.test("only disclosure children collapse, regardless of names, input order or display flags", async () => {
      const statement = copyStatement("profit_statement");
      statement.rows.reverse();
      for (const row of statement.rows) {
        row.name = `全新报表名称${row.line}`;
        row.has_amount = false;
        row.is_total = row.line !== 21 && row.line !== 30 && row.line !== 32;
      }
      expectLines(renderedRows(await render(statement)), defaults.profit_statement);
      for (const [parent, expectedChildren] of [
        [3, [4, 5, 6, 7, 8, 9, 10]], [11, [12, 13]], [14, [15, 16, 17]],
        [18, [19]], [22, [23]], [24, [25, 26, 27, 28, 29]],
      ]) {
        const partial = renderedRows(await render(statement, [parent]));
        assert.deepEqual(partial.filter(row => row.depth === 1).map(row => row.line), expectedChildren);
      }
      const rows = renderedRows(await render(statement, [3, 11, 14, 18, 22, 24]));
      for (const line of [4, 5, 6, 7, 8, 9, 10, 12, 13, 15, 16, 17, 19, 23, 25, 26, 27, 28, 29]) {
        assert.equal(rowAt(rows, line).depth, 1);
      }
      for (const line of defaults.profit_statement) assert.equal(rowAt(rows, line).depth, 0);
      const interest = statement.rows.find(row => row.line === 19);
      interest.values.current_fen = "-12345";
      const interestRow = rowAt(renderedRows(await render(statement, [18])), 19);
      assert.equal(interestRow.cells.at(-2), "−¥123.45");
      assert.doesNotMatch(interestRow.attributes + interestRow.content, /\b(?:gain|loss|cash-inflow|cash-outflow)\b/, "negative interest is an expense detail, not a profit or cash-flow signal");
    });

    await t.test("each cash-flow activity expands its own receipts and payments", async () => {
      const statement = copyStatement("cash_flow_statement");
      for (const [parent, expectedChildren] of [[7, [1, 2, 3, 4, 5, 6]], [13, [8, 9, 10, 11, 12]], [19, [14, 15, 16, 17, 18]]]) {
        const rows = renderedRows(await render(statement, [parent]));
        assert.deepEqual(rows.filter(row => row.depth === 0).map(row => row.line), defaults.cash_flow_statement);
        assert.deepEqual(rows.filter(row => row.depth === 1).map(row => row.line), expectedChildren);
        assert.equal(rows.length, defaults.cash_flow_statement.length + expectedChildren.length);
      }
    });

    await t.test("each original amount column preserves large integers, negatives, zero and unknown", async () => {
      for (const [key, line] of [["balance_sheet", 30], ["profit_statement", 1], ["cash_flow_statement", 20]]) {
        const statement = copyStatement(key);
        const source = statement.rows.find(row => row.line === line);
        source.values[statement.columns[0].key] = "900719925474099301";
        source.values[statement.columns[1].key] = "-900719925474099299";
        let row = rowAt(renderedRows(await render(statement)), line);
        assert.deepEqual(row.cells.slice(-2), ["¥9,007,199,254,740,993.01", "−¥9,007,199,254,740,992.99"]);
        source.values[statement.columns[0].key] = null;
        source.values[statement.columns[1].key] = "0";
        row = rowAt(renderedRows(await render(statement)), line);
        assert.deepEqual(row.cells.slice(-2), ["暂无法确定", "¥0.00"]);
      }
    });

    await t.test("unknown statements and rows remain visible, and absent parents cannot discard children", async () => {
      const unknown = copyStatement("profit_statement");
      unknown.key = "synthetic_future_statement";
      unknown.rows = unknown.rows.slice(0, 3).reverse();
      const unknownHtml = await render(unknown);
      assert.deepEqual(renderedRows(unknownHtml).map(row => row.line).sort((a, b) => a - b), [1, 2, 3]);
      assert.equal(bulkButton(unknownHtml, "展开全部"), undefined);
      assert.equal(bulkButton(unknownHtml, "收起全部"), undefined);
      const extra = copyStatement("balance_sheet");
      extra.rows.push({ line: 999, name: "新增正式项目", values: { ending_fen: "0", beginning_fen: null }, is_total: false, has_amount: false });
      assert.deepEqual(rowAt(renderedRows(await render(extra)), 999).cells.slice(-2), ["¥0.00", "暂无法确定"]);
      for (const [key, missing] of [["balance_sheet", [15, 20]], ["profit_statement", [3]], ["cash_flow_statement", [7]]]) {
        const statement = copyStatement(key);
        statement.rows = statement.rows.filter(row => !missing.includes(row.line));
        const rows = renderedRows(await render(statement, statement.rows.map(row => row.line)));
        assert.deepEqual(rows.map(row => row.line).sort((a, b) => a - b), statement.rows.map(row => row.line).sort((a, b) => a - b));
        assert.equal(new Set(rows.map(row => row.line)).size, statement.rows.length);
      }
    });

    await t.test("bulk controls exist only for available detail groups", async () => {
      const statement = copyStatement("profit_statement");
      let html = await render(statement);
      for (const label of ["展开全部", "收起全部"]) assert.ok(bulkButton(html, label), `${label} is available`);
      html = await render(statement, statement.rows.map(row => row.line));
      for (const label of ["展开全部", "收起全部"]) assert.ok(bulkButton(html, label), `${label} remains available`);
      statement.rows = statement.rows.filter(row => row.line === 1);
      html = await render(statement);
      assert.equal(bulkButton(html, "展开全部"), undefined);
      assert.equal(bulkButton(html, "收起全部"), undefined);
    });
  } finally {
    await server.close();
  }
});
