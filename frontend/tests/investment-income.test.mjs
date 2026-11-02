import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";

const collection = (items, total = items.length, filtered = total) => ({ items, page: {
  total_count: total, filtered_count: filtered, returned_count: items.length,
  has_more: filtered > items.length, next_cursor: filtered > items.length ? "synthetic-next-page" : null,
} });
const product = (changes = {}) => ({
  fund_id: "fund-a", investment_id: "holding-a", name: "合成稳健理财", product_type: "bank_wealth", category_label: "银行理财",
  closing_cost_fen: "1000000", actual_loss_fen: "0", pending_receipts_fen: "0", activity_only: false,
  quantity_label: "持有 10000 份", holding_label: "流动投资", recognized_income_fen: "23000", accrued_income_fen: "0",
  actual_payments_fen: "200000", actual_receipts_fen: "120000", ...changes,
});
const cooperation = (changes = {}) => ({
  fund_id: "project-a", investment_id: "contract-a", name: "合成合作项目", product_type: "cooperation", category_label: "合作项目",
  closing_cost_fen: null, actual_loss_fen: "0", cooperation_equity_fen: "700000", cooperation_held_fen: "450000",
  cooperation_operator_due_fen: "12300", cooperation_company_due_fen: "45600", ...changes,
});
function text(html) { return html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim(); }
function records(html) { return [...html.matchAll(/<article\b[^>]*>([\s\S]*?)<\/article>/g)].map(([, value]) => value); }
function record(html, name) {
  const value = records(html).find(item => item.includes(name));
  assert.ok(value, "investment " + name + " has a separate record");
  return text(value);
}
function heading(html) {
  const caption = html.match(/<p\b[^>]*class="list-caption"[^>]*>([\s\S]*?)<\/p>/);
  assert.ok(caption, "the title retains complete month amounts and loading counts");
  return text(caption[1]);
}

test("investment cards and lists share owner amounts while keeping confirmations and cash distinct", async (t) => {
  const previousFetch = globalThis.fetch, calls = [];
  globalThis.fetch = async url => { calls.push(String(url)); throw new Error("investment display must not fetch business data"); };
  const server = await createServer({
    root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true },
    plugins: [{ name: "investment-display-fixture", enforce: "pre", transform(code, id) {
      if (id.replaceAll("\\", "/").endsWith("/src/components/FundInvestments.vue")) return code
        .replace('const displayMode = ref<"cards" | "list">("cards")', 'const displayMode = ref(globalThis.investmentDisplayMode ?? "cards")');
    } }, vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom",
  });
  try {
    const { default: component } = await server.ssrLoadModule("/src/components/FundInvestments.vue");
    async function render(items = [product()], { mode, total = items.length, filtered = total, closing = "1000000", ...overrides } = {}) {
      globalThis.investmentDisplayMode = mode;
      const count = calls.length;
      const app = createSSRApp(component, {
        summary: { closing_cost_fen: closing }, collection: collection(items, total, filtered), filter: "all",
        loading: false, pageState: { loading: false, error: "" }, active: false, scope: "synthetic-company-month-version",
        ...overrides,
      });
      const html = await renderToString(app);
      assert.equal(calls.length, count, "viewing investment records does not request events or business progress");
      assert.doesNotMatch(html, /undefined|NaN|5111|<details\b|查看业务事项|本月投资事项/);
      return html;
    }
    await t.test("cards are the default with complete month amount and the three financial meanings", async () => {
      const html = await render(), value = record(html, "合成稳健理财");
      assert.match(html, /<button\b[^>]*aria-pressed="true"[^>]*>卡片<\/button>/);
      assert.match(heading(html), /金融投资账面金额\s*¥10,000\.00[\s\S]*共\s*1\s*项[\s\S]*已加载\s*1\s*项/);
      assert.match(value, /银行理财[\s\S]*流动投资[\s\S]*合成稳健理财[\s\S]*持有 10000 份/);
      assert.match(value, /月末账面金额\s*¥10,000\.00/);
      assert.match(value, /本月确认收益\s*¥230\.00\s*未扣投资增值税/);
      assert.match(value, /本月实际到账\s*¥1,200\.00\s*含退出本金/);
      assert.match(value, /月末待收款\s*¥0\.00/);
      assert.match(value, /本月实际付款\s*¥2,000\.00/);
      assert.match(text(html), /不代表市值[\s\S]*未计入[\s\S]*账户资金/);
      assert.doesNotMatch(html, /aria-label="筛选投资"/);
      assert.doesNotMatch(heading(html), /本月确认收益|实际到账|实际付款/);
    });
    for (const mode of ["cards", "list"]) {
      await t.test(mode + ": booked amounts, dated estimate, receivable breakdown and loss stay separate", async () => {
        const html = await render([product({ pending_receipts_fen: "15000", accrued_income_fen: "3500", actual_loss_fen: "8000",
          market_value_fen: "1200000", market_as_of: "2026-01-08" })], { mode });
        const value = record(html, "合成稳健理财");
        assert.match(value, /月末账面金额\s*¥10,000\.00|¥10,000\.00/);
        assert.match(value, /月末待收款\s*¥150\.00\s*其中待收收益\s*¥35\.00/);
        assert.match(value, /本月实际投资损失\s*¥80\.00/);
        assert.match(value, /参考估值\s*¥12,000\.00\s*·\s*2026-01-08/);
        assert.doesNotMatch(value, /¥185\.00|¥70\.00|可用资金|当前市值|净赚|净亏/);
        if (mode === "list") {
          assert.match(html, /<button\b[^>]*aria-pressed="true"[^>]*>列表<\/button>/);
          assert.match(text(html), /产品／项目[\s\S]*收益与资金情况/);
          assert.match(html, /data-label="月末账面金额"/);
          const descriptions = [...records(html)[0].matchAll(/<dd\b[^>]*>([\s\S]*?)<\/dd>/g)];
          for (const note of ["未扣投资增值税", "含退出本金等实际收款", "其中待收收益"]) {
            const description = descriptions.find(([, content]) => text(content).includes(note));
            assert.ok(description, `${note} belongs to its amount description`);
            assert.match(description[1], /<small\b[^>]*>[\s\S]*?<\/small>/);
          }
        }
      });
      await t.test(mode + ": integer fen above Number precision stays exact in holdings, profit and receipts", async () => {
        const html = await render([product({ closing_cost_fen: "9007199254740993", recognized_income_fen: "-9007199254740991",
          actual_receipts_fen: "9007199254740989" })], { mode, closing: "9007199254740993" });
        assert.match(heading(html), /¥90,071,992,547,409\.93/);
        const value = record(html, "合成稳健理财");
        assert.match(value, /¥90,071,992,547,409\.93/);
        assert.match(value, /本月确认收益\s*−¥90,071,992,547,409\.91/);
        assert.match(value, /本月实际到账\s*¥90,071,992,547,409\.89/);
        assert.doesNotMatch(value, /¥90,071,992,547,409\.92/);
      });
      await t.test(mode + ": cooperation shows its own capital and obligations without fabricated financial zeroes", async () => {
        const html = await render([product(), cooperation()], { mode }), value = record(html, "合成合作项目");
        assert.equal(records(html).length, 2);
        assert.match(heading(html), /金融投资账面金额\s*¥10,000\.00/);
        assert.doesNotMatch(heading(html), /¥7,000\.00|¥4,500\.00|¥17,000\.00|¥21,500\.00/);
        assert.match(value, /合作权益[\s\S]*¥7,000\.00|¥7,000\.00/);
        assert.match(value, /代管资金\s*¥4,500\.00/);
        assert.match(value, /经营者待补\s*¥123\.00/);
        assert.match(value, /公司待补\s*¥456\.00/);
        assert.doesNotMatch(value, /确认收益|实际到账|实际付款|月末待收款|未提供|¥0\.00|¥579\.00|¥333\.00/);
      });
      await t.test(mode + ": unknown holdings, zero-cost units and distinct terms survive unchanged", async () => {
        const html = await render([
          product({ investment_id: "income-only", name: "合成历史收益", activity_only: true, closing_cost_fen: null, quantity_label: undefined, holding_label: undefined }),
          product({ investment_id: "zero-cost", name: "合成零成本份额", closing_cost_fen: "0", quantity_label: "持有 1 份" }),
          product({ investment_id: "another-term", name: "合成零成本份额", closing_cost_fen: "10000", quantity_label: "持有 2 份" }),
        ], { mode, closing: "10000" });
        assert.equal(records(html).length, 3);
        const unknown = record(html, "合成历史收益");
        assert.match(unknown, /—/);
        assert.match(unknown, /收益及收款记录，未列持仓金额/);
        assert.doesNotMatch(unknown, /月末账面金额\s*¥0\.00|已清仓|无持仓/);
        const sameName = records(html).filter(value => value.includes("合成零成本份额")).map(text);
        assert.match(sameName[0], /持有 1 份[\s\S]*¥0\.00/);
        assert.match(sameName[1], /持有 2 份[\s\S]*¥100\.00/);
      });
    }
    await t.test("only a dated estimate and nonzero supplementary payments or loss add notes", async () => {
      const html = await render([product({ actual_payments_fen: "0", market_value_fen: "12345" })]);
      const value = record(html, "合成稳健理财");
      assert.doesNotMatch(value, /本月实际付款|实际投资损失|参考估值|估值日期未提供/);
    });
    for (const total of [3, 1, 0]) await t.test(`empty server filter keeps controls with ${total} total records`, async () => {
      const html = await render([], { filter: "cooperation", total, filtered: 0, closing: "2700000" });
      assert.match(html, /aria-label="筛选投资"/);
      assert.match(html, /data-value="cooperation"/);
      assert.match(heading(html), new RegExp(`金融投资账面金额\\s*¥27,000\\.00[\\s\\S]*共\\s*${total}\\s*项[\\s\\S]*合作项目\\s*0\\s*项[\\s\\S]*已加载\\s*0\\s*项`));
      assert.match(text(html), /本月没有合作项目记录，可切换筛选查看/);
      assert.equal(records(html).length, 0);
    });
    await t.test("loading and error states retain retry and never fabricate an empty successful result", async () => {
      const loading = await render([], { filter: "cooperation", total: 3, filtered: 2, pageState: { loading: true, error: "" } });
      assert.match(text(loading), /正在读取合作项目/);
      assert.doesNotMatch(text(loading), /本月没有可展示/);
      assert.doesNotMatch(text(loading), /合作项目\s*2\s*项|已加载\s*0\s*\/\s*2/);
      const failed = await render([], { filter: "cooperation", total: 3, filtered: 2, pageState: { loading: false, error: "读取投资失败" } });
      assert.match(failed, /role="alert"/);
      assert.match(text(failed), /读取投资失败[\s\S]*重试读取/);
      assert.doesNotMatch(text(failed), /本月没有可展示/);
      assert.doesNotMatch(text(failed), /合作项目\s*2\s*项|已加载\s*0\s*\/\s*2/);
    });
    await t.test("pagination shows the complete server count and follows active/loading gates", async () => {
      let more = 0;
      const html = await render([product({ closing_cost_fen: "10000" })], { total: 27, closing: "2700000", active: true, onMore: () => { more += 1; } });
      assert.match(heading(html), /金融投资账面金额\s*¥27,000\.00/);
      assert.match(text(html), /已加载\s*1\s*\/\s*27/);
      assert.match(record(html, "合成稳健理财"), /¥100\.00/);
      assert.equal(more, 1);
      more = 0;
      await render([product()], { total: 27, active: false, onMore: () => { more += 1; } });
      await render([product()], { total: 27, active: true, loading: true, onMore: () => { more += 1; } });
      assert.equal(more, 0);
    });
  } finally {
    await server.close();
    globalThis.fetch = previousFetch;
    delete globalThis.investmentDisplayMode;
  }
});
