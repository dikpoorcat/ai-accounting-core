// T5: actual candidate assets and loopback API; no response fixtures or business writes.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");

async function run(config) {
  const { chromium } = require(config.playwright_module);
  const browser = await chromium.launch({ channel: config.channel, headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    const errors = [], apiFailures = [], scripts = new Set();
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    page.on("response", response => {
      const url = new URL(response.url());
      if (url.pathname.endsWith(".js")) scripts.add(url.pathname.slice(1));
      if (url.pathname.startsWith("/api/dashboard/") && response.status() >= 400) {
        apiFailures.push({ path: url.pathname, status: response.status() });
      }
    });
    const [first, second] = config.companies;
    const url = (path, company = first.id, period = "2026-09", extra = "") =>
      `${config.origin}${path}?company_id=${company}&period=${period}${extra}`;
    async function dashboard(path, action, heading, company = first.id, period = "2026-09", extra = "") {
      const response = page.waitForResponse(response => {
        const target = new URL(response.url());
        return target.pathname === `/api/dashboard/${action}` && response.status() === 200;
      });
      await page.goto(url(path, company, period, extra));
      const data = await (await response).json();
      await page.getByRole("heading", { name: heading, exact: true }).waitFor();
      await page.getByRole("heading", { name: /所选月末核算/ }).first().waitFor();
      await page.getByText("当前后续事项", { exact: true }).first().waitFor();
      assert.equal(data.schema_version, action === "quarterly-report" ? 1 : 2);
      assert.equal(await page.getByLabel("切换公司").inputValue(), company);
      return data;
    }
    const ticket = new URL(config.ticket_url);
    ticket.search = `?company_id=${first.id}&period=2026-09`;
    await page.goto(ticket.href);
    await page.getByRole("heading", { name: "月度经营与财务概览", exact: true }).waitFor();
    assert.equal(new URL(page.url()).hash, "", "ticket must be consumed before routing");
    assert((await context.cookies()).some(cookie => cookie.name === "finance_session" && cookie.httpOnly));
    const brief = await dashboard("/index.html", "brief", "月度经营与财务概览");
    assert.equal(brief.data.vouchers.length, 100);
    assert.equal(brief.data.voucher_count, first.voucher_count);
    assert(brief.data.collections.vouchers.page.has_more);
    const continued = page.waitForResponse(response => {
      const target = new URL(response.url());
      return target.pathname === "/api/dashboard/brief" && target.searchParams.has("cursor");
    });
    await page.locator("#activity").getByRole("button", { name: "加载更多", exact: true }).click();
    const continuation = await (await continued).json();
    assert.equal(continuation.snapshot_version, brief.snapshot_version);
    assert.equal(continuation.data.collections.vouchers.page.has_more, false);
    await page.getByText(`已加载 ${first.voucher_count} / ${first.voucher_count} 张凭证；本页汇总按全月计算。`, { exact: true }).waitFor();
    const focused = await dashboard("/", "brief", "月度经营与财务概览", first.id, "2026-09", `&voucher=${first.target_number}`);
    assert.equal(focused.data.focused_voucher.voucher_version_id, first.target_version_id);
    assert(!focused.data.vouchers.some(item => item.voucher_version_id === first.target_version_id));
    await page.locator("#activity").getByText("精确定位的凭证", { exact: true }).waitFor();
    await dashboard("/funds", "funds", "资金总览");
    const employees = await dashboard("/employees", "employees", "员工与薪酬概览");
    assert(employees.data.employees.items.length > 0);
    await page.getByText(first.employee_name, { exact: true }).first().waitFor();
    await page.getByLabel("员工查看月份", { exact: true }).selectOption("2026-10");
    await page.waitForURL(target => target.searchParams.get("period") === "2026-10");
    await page.getByLabel("员工查看月份", { exact: true }).selectOption("2026-09");
    await page.waitForURL(target => target.searchParams.get("period") === "2026-09");
    await page.getByText(first.employee_name, { exact: true }).first().waitFor();
    const changedCompany = page.waitForResponse(response => {
      const target = new URL(response.url());
      return target.pathname === "/api/dashboard/employees" && target.searchParams.get("company_id") === second.id;
    });
    await page.getByLabel("切换公司").selectOption(second.id);
    assert.equal((await (await changedCompany).json()).schema_version, 2);
    await page.getByText(second.employee_name, { exact: true }).first().waitFor();
    assert.equal(await page.getByText(first.employee_name, { exact: true }).count(), 0);
    const assets = await dashboard("/assets", "assets", "长期资产概览");
    assert(assets.data.collections.assets.items.length > 0);
    await page.getByText(first.asset_name, { exact: true }).first().waitFor();
    await dashboard("/reports", "quarterly-report", "季度财务报表", first.id, "2026-09", "&quarter=2026-Q3");
    await dashboard("/local.html", "brief", "月度经营与财务概览");
    const refreshed = page.waitForResponse(response => new URL(response.url()).pathname === "/api/dashboard/brief");
    await page.getByRole("button", { name: "刷新数据", exact: true }).click();
    assert.equal((await (await refreshed).json()).schema_version, 2);
    await page.screenshot({ path: join(config.output, "desktop.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: join(config.output, "mobile.png"), fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false);
    assert.equal(errors.length, 0, errors.join("\n"));
    assert.deepEqual(apiFailures, []);
    assert(scripts.size > 0);
    for (const script of scripts) assert(config.assets[script], `unverified script ${script}`);
    return {
      status: "passed", actual_same_origin_api: true, actual_candidate_assets: true,
      five_pages: true, both_html_entries: true, refresh: true, companies: 2, periods: 2,
      nonempty_employee_and_asset: true, voucher_count: first.voucher_count,
      page_continuation: true, numeric_voucher_outside_first_page: true,
      company_switch_clears_old_data: true, employee_month_a_b_a: true,
      page_errors: errors, api_failures: apiFailures, loaded_scripts: [...scripts].sort(),
      screenshots: ["desktop.png", "mobile.png"], mobile_width: 390,
      limitation: "Thin browser seam; detailed stale-response, unknown-state and invalid-cursor cases reuse recorded unit/HTTP evidence.",
    };
  } finally { await browser.close(); }
}

if (require.main === module) {
  const config = JSON.parse(readFileSync(0, "utf8"));
  run(config).then(result => process.stdout.write(JSON.stringify(result)), error => {
    const message = String(error.message).replace(/https?:\/\/[^\s"']+/g, "[browser URL]");
    process.stdout.write(JSON.stringify({ status: "failed", message }));
    process.exitCode = 1;
  });
}
module.exports = run;
