// Browser-only synthetic continuations; the authenticated accounting service is read-only.
const assert = require("node:assert/strict");
const { selectDashboardOption } = require("./helpers/dashboard-select.cjs");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

async function run(config) {
  const { chromium } = require(config.playwright_module);
  const fixtures = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures/dashboard-contracts.json"), "utf8"));
  const validators = {};
  for (const name of ["Brief", "Funds", "Employees", "Assets"]) {
    validators[name.toLowerCase()] = (await import(pathToFileURL(path.join(__dirname, `../src/api/generated/dashboard${name}.js`))))[`validateDashboard${name}Response`];
  }
  const clone = value => structuredClone(value);
  const expand = (item, identity) => Array.from({ length: 45 }, (_, i) => ({ ...clone(item), [identity]: `auto-${identity}-${i}` }));
  const bases = {
    brief: clone(fixtures.brief.response),
    funds: clone(fixtures.first_frozen_account_funds.response),
    employees: clone(fixtures.employees_month_dates.response),
    assets: clone(fixtures.asset_payment_summary.response),
  };
  const b = bases.brief.data;
  const activity = b.collections.activity.items[0], voucher = b.collections.vouchers.items[0];
  const activities = expand(activity, "key").map((item, i) => ({ ...item, voucher_version_id: `auto-voucher-${i}` }));
  const vouchers = expand(voucher, "voucher_version_id").map((item, i) => ({ ...item, voucher_version_id: `auto-voucher-${i}`, number: String(i + 1) }));
  b.activity_count = 45; b.voucher_count = 45;
  b.activity_groups = b.activity_groups.filter(group => group.key === activity.group).map(group => ({ ...group, event_count: 45 }));
  b.open_items = { ...b.open_items, receivable_count: 0, receivable_fen: "0", payable_count: 45, payable_fen: "9000000", total_count: 45, complete: true, cutoff_period: config.period, current_cutoff_period: config.period,
    categories: [{ key: "payroll_payables", label: "待付工资、社保与个税", direction: "payable", unit: "笔", count: 45, loaded_count: 20, outstanding_fen: "9000000" }] };
  const openItems = expand({ id: "", category_key: "payroll_payables", party: "演示员工", description: "实发工资", status: "partial", source_amount_fen: "800000", paid_fen: "600000", other_settled_fen: "0", outstanding_fen: "200000", current_status: "partial", current_outstanding_fen: "200000", subject_id: "auto-payroll", contribution_group_key: null, contribution_component: null, payroll_period: null }, "id");
  const f = bases.funds.data, account = f.collections.accounts.items[0];
  const employee = bases.employees.data.collections.employees.items[0];
  bases.employees.data.employee_filter = "all"; bases.employees.data.employee_id = null;
  const datasets = {
    brief: { activity: activities, vouchers, open_items: openItems },
    funds: {
      accounts: [clone(account), ...expand(account, "account_id").slice(1)],
      movements: expand(f.collections.movements.items[0], "id"),
      statements: expand(f.collections.statements.items[0], "id"),
      investment_products: expand({ fund_id: "", opening_cost_fen: "0", subscription_cost_fen: "10000", redemption_cost_fen: "0", closing_cost_fen: "10000", investment_income_fen: "0", name: "演示基金" }, "fund_id"),
      investment_events: expand({ id: "", subject_id: "auto-investment", date: null, period: config.period, fund_id: "auto-fund_id-0", name: "演示基金", type: "申购", cost_fen: "10000", net_proceeds_fen: null, investment_income_fen: null, settlement_fen: "10000" }, "id"),
    },
    employees: { employees: expand(employee, "employee_id"), labor_sources: expand(fixtures.employees_labor_sources.response.data.collections.labor_sources.items[0], "source_id") },
    assets: {
      assets: expand(bases.assets.data.collections.assets.items[0], "asset_id"),
      projects: expand({ source_id: "", project_id: "auto-project", period: config.period, kind: "construction", label: "演示项目", party: "施工单位", cost_fen: "10000", remaining_fen: "10000" }, "source_id"),
    },
  };
  const browser = await chromium.launch({ channel: config.channel || "msedge", headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
  const page = await context.newPage(), requests = [], errors = [];
  page.setDefaultTimeout(10000);
  const sanitize = value => {
    let message = String(value).replace(/https?:\/\/[^\s"']+/g, "[URL]").replace(/\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi, "[ID]");
    for (const sensitive of [config.company_id, config.ticket_url]) if (sensitive) message = message.split(sensitive).join("[REDACTED]");
    return message;
  };
  page.on("pageerror", error => errors.push({ phase, message: sanitize(error.message) }));
  const frames = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const idle = async () => { await page.waitForFunction(() => document.querySelector(".module-header")?.getAttribute("aria-busy") === "false"); await frames(); };
  let fail = false, holdTarget = null, release, phase = "login";
  try {
    const ticket = new URL(config.ticket_url);
    ticket.searchParams.set("company_id", config.company_id); ticket.searchParams.set("period", config.period);
    await page.goto(ticket.href); await idle();
    await page.route(/\/api\/dashboard\/(brief|funds|employees|assets)(\?|$)/, async route => {
      const url = new URL(route.request().url()), name = url.pathname.split("/").at(-1);
      const section = url.searchParams.get("section"), offset = Number(url.searchParams.get("cursor") || 0);
      requests.push({ name, section, offset, limit: url.searchParams.get("limit"), version: url.searchParams.get("expected_version") });
      if (name === "brief" && section === "activity" && offset === 20 && fail) {
        fail = false;
        await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "模拟后续页读取失败" }) }); return;
      }
      if (name === holdTarget?.name && section === holdTarget.section && offset === 20) {
        holdTarget = null; await new Promise(resolve => { release = resolve; });
      }
      const response = clone(bases[name]);
      response.read_context.company_id = config.company_id;
      response.selected_period.key = config.period;
      response.snapshot_version = "auto-pages-v1";
      const source = datasets[name];
      const sections = section ? (name === "brief" && section === "activity" ? ["activity", "vouchers"] : [section]) : Object.keys(source);
      response.data.collections = Object.fromEntries(sections.map(key => {
        const items = clone(source[key].slice(offset, offset + 20));
        if (name === "funds" && key === "statements" && url.searchParams.has("statement_account_id")) for (const item of items) item.account_id = url.searchParams.get("statement_account_id");
        return [key, { items, page: { total_count: 45, filtered_count: 45, returned_count: items.length, has_more: offset + 20 < 45, next_cursor: offset + 20 < 45 ? String(offset + 20) : null } }];
      }));
      if (name === "funds") {
        response.data.selected_movement_account = url.searchParams.get("movement_account_selection") === "first"
          ? { type: account.type, account_id: account.account_id }
          : url.searchParams.has("movement_account_id") ? { type: url.searchParams.get("movement_account_type"), account_id: url.searchParams.get("movement_account_id") } : null;
        for (const item of response.data.collections.movements?.items || []) {
          item.account_type = response.data.selected_movement_account?.type || account.type;
          item.account_id = response.data.selected_movement_account?.account_id || account.account_id;
        }
      }
      if (name === "employees") response.data.employee_filter = url.searchParams.get("employee_filter") || "all";
      if (name === "assets") response.data.asset_filter = url.searchParams.get("asset_filter") || "all";
      assert(validators[name](response), `${name} synthetic response failed generated contract: ${JSON.stringify(validators[name].errors)}`);
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(response) }).catch(() => {});
    });
    const nav = label => page.locator(".section-nav").getByRole("button", { name: label, exact: true });
    async function navigate(name) {
      phase = `${name} first page`; requests.length = 0;
      await page.goto(`${config.origin}${name === "brief" ? "/" : "/" + name}?company_id=${config.company_id}&period=${config.period}`);
      await idle(); await frames();
      assert.equal(requests.filter(r => r.section).length, 0, `${name} overview auto-read unselected modules`);
      assert.equal(await page.getByRole("button", { name: "加载更多", exact: true }).count(), 0);
    }
    const cases = [
      ["brief", "本月发生", ["activity"], "#activity .event-row"],
      ["brief", "待收待付", ["open_items"], "#open-items .open-event-row"],
      ["funds", "账户", ["accounts"], ".account-grid .account-card"],
      ["funds", "货币基金", ["investment_products", "investment_events"], ".investment-summary-table tbody tr"],
      ["funds", "资金明细", ["accounts", "movements"], ".book-activity-row"],
      ["employees", "员工明细", ["employees"], ".panel:has(#employee-list-title) .employee-card"],
      ["employees", "个人劳务", ["labor_sources"], ".panel:has(#labor-title) .employee-card"],
      ["assets", "资产卡片", ["assets"], ".asset-grid .asset-card"],
      ["assets", "项目投入", ["projects"], ".project-card"],
    ];
    const results = [];
    for (const [name, label, sections, selector] of cases) {
      await navigate(name); phase = `${name}/${label}`;
      assert.equal(await page.locator(selector).count(), 20, `${phase} first page not 20`);
      await nav(label).click();
      await page.waitForFunction(selector => document.querySelectorAll(selector).length === 45, selector);
      await frames();
      for (const section of sections) assert.deepEqual(requests.filter(r => r.section === section).map(r => r.offset), [20, 40], `${phase} continuation count`);
      assert.equal(requests.filter(r => r.section && !sections.includes(r.section)).length, 0, `${phase} read unrelated module`);
      assert(requests.filter(r => r.section).every(r => [null, "20"].includes(r.limit) && r.version === "auto-pages-v1"));
      const count = requests.length;
      await nav("概览").click(); await nav(label).click(); await frames();
      assert.equal(requests.length, count, `${phase} complete collection reread`);
      results.push({ page: name, module: label, loaded: 45, continuation_requests: sections.length * 2 });
    }
    await navigate("funds"); phase = "bank statements";
    await page.getByRole("tab", { name: "按流水", exact: true }).click();
    await nav("资金明细").click();
    await page.waitForFunction(() => document.querySelectorAll(".bank-activity-item").length === 45);
    assert.deepEqual(requests.filter(r => r.section === "statements").map(r => r.offset), [20, 40]);
    assert.deepEqual(requests.filter(r => r.section === "accounts").map(r => r.offset), [20, 40]);
    assert.equal(requests.filter(r => r.section === "movements").length, 0);
    results.push({ page: "funds", module: "银行流水", loaded: 45, continuation_requests: 4 });

    await navigate("brief"); phase = "continuation failure"; fail = true;
    await nav("本月发生").click();
    await page.locator("#activity").getByRole("button", { name: "重试读取" }).waitFor();
    assert.equal(await page.locator("#activity .event-row").count(), 20);
    assert.equal(requests.filter(r => r.section === "activity").length, 1);
    await page.locator("#activity").getByRole("button", { name: "重试读取" }).click();
    await page.waitForFunction(() => document.querySelectorAll("#activity .event-row").length === 45);
    assert.deepEqual(requests.filter(r => r.section === "activity").map(r => r.offset), [20, 20, 40]);

    for (const [name, label, section, control, value, selector] of [
      ["employees", "员工明细", "employees", "筛选员工", "in_period", ".panel:has(#employee-list-title) .employee-card"],
      ["assets", "资产卡片", "assets", "筛选资产", "fixed", ".asset-grid .asset-card"],
    ]) {
      await navigate(name); phase = `${name} filter continuation`;
      await nav(label).click();
      await page.waitForFunction(selector => document.querySelectorAll(selector).length === 45, selector);
      requests.length = 0;
      const firstFiltered = page.waitForResponse(response => {
        const url = new URL(response.url());
        return url.pathname === `/api/dashboard/${name}` && url.searchParams.get("section") === section && !url.searchParams.has("cursor");
      });
      await selectDashboardOption(page, control, value);
      await firstFiltered;
      await page.waitForFunction(selector => document.querySelectorAll(selector).length === 45, selector);
      assert.deepEqual(requests.filter(r => r.section === section).map(r => r.offset), [0, 20, 40], "new filter failed to start its own automatic cursor sequence");
      assert.equal(await page.locator(".module-header").getAttribute("aria-busy"), "false", "filter entered whole-page wait");
    }
    for (const [name, label, section, selector] of [
      ["brief", "本月发生", "activity", "#activity .event-row"],
      ["funds", "账户", "accounts", ".account-grid .account-card"],
      ["employees", "员工明细", "employees", ".panel:has(#employee-list-title) .employee-card"],
      ["assets", "资产卡片", "assets", ".asset-grid .asset-card"],
    ]) {
      await navigate(name); phase = `${name} deselect cancellation`; holdTarget = { name, section }; release = null;
      const pending = page.waitForRequest(request => new URL(request.url()).searchParams.get("cursor") === "20");
      await nav(label).click(); await pending; await frames();
      assert(release, "continuation was not held");
      await nav("概览").click(); release(); await frames();
      assert.equal(await page.locator(selector).count(), 20, "late unselected continuation changed rows");
      await nav(label).click();
      await page.waitForFunction(selector => document.querySelectorAll(selector).length === 45, selector);
      assert.deepEqual(requests.filter(r => r.section === section).map(r => r.offset), [20, 20, 40]);
    }
    assert.equal(errors.length, 0);
    return { status: "passed", modules: results, failed_page_retry: true, filter_continuation: true, deselection_cancellation: true, cancellation_pages: 4, late_response_rejected: true, repeated_selection_requests: 0, browser_errors: 0 };
  } catch (error) { return { status: "failed", phase, message: sanitize(error.message), browser_errors: errors.length, errors, requests }; }
  finally { release?.(); await browser.close(); }
}
let input = "";
process.stdin.setEncoding("utf8").on("data", chunk => { input += chunk; });
process.stdin.on("end", async () => {
  try { const result = await run(JSON.parse(input)); process.stdout.write(JSON.stringify(result)); process.exitCode = result.status === "passed" ? 0 : 1; }
  catch (error) { process.stdout.write(JSON.stringify({ status: "failed", message: error.message })); process.exitCode = 1; }
});
