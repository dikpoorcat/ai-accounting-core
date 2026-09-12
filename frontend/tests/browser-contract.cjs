// Historical v1 response fixtures: not a current T4/T5 browser acceptance entrypoint.
async page => {
  // Dedicated Vite test server; every API request uses synthetic fixture data.
  const base = "http://127.0.0.1:5178";
  const f = await (await page.request.get(`${base}/tests/dashboard-fixtures.json`)).json();
  await page.addInitScript(() => { if (window.location.protocol !== "http:") return; localStorage.removeItem("finance-dashboard-company-id"); localStorage.removeItem("finance-dashboard-theme"); });
  const failures = [];
  const calls = [];
  let expired = false;
  let authenticated = false, includeCompany = false, ticketExchanges = 0, jobPolls = 0, exportPosts = 0;
  page.on("pageerror", error => failures.push(error.message));
  const period = { key: "2026-09", year: 2026, month: 9, label: "2026年9月", short_label: "9月", status: "open", start_date: "2026-09-01", end_date: "2026-09-30", closed_at: null };
  const companies = [{ company_id: "company-a", name: "演示甲公司", status: "active" }, { company_id: "company-b", name: "演示乙公司", status: "active" }];
  const pageInfo = { has_more: false, next_cursor: null, total_count: 0 };
  const workforce = { has_activity: false, total_fen: "0", employee: { has_activity: false, periods: [] }, personal_labor: { has_activity: false, periods: [] } };
  const asset = (asset_type) => ({ asset_type, code: asset_type, name: asset_type === "fixed" ? "演示固定资产" : "演示无形资产", category: asset_type, category_label: "演示类别", status: "active", status_label: "在用", acquisition_date: null, posting_date: null, supplier: "未提供", settlement_method: "unknown", settlement_label: "收付款独立记录", payment_date: null, due_date: null, purchase_price_fen: null, noncreditable_tax_fen: null, other_direct_cost_fen: null, cost_fen: "0", accumulated_charge_fen: "0", month_charge_fen: "0", book_value_fen: "0", latest_charge_period: null, benefit_area_label: null, useful_life_months: null, acquisition_reference: "暂无凭证", in_service_date: null, reimbursing_employee: "", residual_value_fen: null, depreciation_method_label: null, depreciation_group_code: null, disposal: null, available_for_use_date: null, life_basis_label: "未提供", life_basis_explanation: "", rights_description: "未提供", retirement: null });
  const voucher = (n, name) => ({ number: String(n), calculation_id: `calculation-${n}`, date: null, recognition: { precision: "month", period: "2026-09", date: null, label: "2026年9月 · 按月确认" }, type: "费用", state: "已发布", summary: `${name}月度费用${n}`, display_summary: "", list_summary: `${name}月度费用${n}`, amount_fen: "9007199254740993", evidence: [], components: [], funds: [], lines: [{ line_number: 1, code: "5602", account: "管理费用", debit_fen: "9007199254740993", credit_fen: "0", party: "", component_id: null }, { line_number: 2, code: "1002", account: "银行存款", debit_fen: "0", credit_fen: "9007199254740993", party: "", component_id: null }] });
  await page.goto("about:blank");
  await page.unroute("**/api/**");
  await page.unroute(`${base}/api/**`);
  await page.route(`${base}/api/**`, async route => {
    const request = route.request();
    const path = "/" + request.url().split("/").slice(3).join("/").split("?")[0];
    const query = Object.fromEntries((request.url().split("?")[1] || "").split("&").filter(Boolean).map(item => item.split("=").map(decodeURIComponent)));
    const reply = (json, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(json) });
    calls.push({ path, company: query.company_id });
    if (request.headers().authorization) throw new Error("owner credentials sent by browser");
    if (path === "/api/browser-session") { ticketExchanges++; return reply({ status: "ready", authenticated: false }); }
    if (path === "/api/security-request") {
      const { operation } = request.postDataJSON();
      if (operation === "session_status") return reply({ provisioned: true, authenticated });
      if (operation === "request") return reply({ request_id: "native-fixture", status: "waiting_for_user" });
      if (operation === "status") { authenticated = true; return reply({ request_id: "native-fixture", status: "succeeded", browser_authenticated: true }); }
    }
    if (expired) return reply({ code: "OWNER_SESSION_EXPIRED", status: "rejected" }, 401);
    if (!authenticated) throw new Error(`business API loaded before authentication: ${path}`);
    if (path === "/api/dashboard/context") {
      const current = companies.find(item => item.company_id === query.company_id) || companies[0];
      return reply({ schema_version: 2, company: current.name, companies: includeCompany ? companies : [], current_company: includeCompany ? current : null, generated_at: "2026-09-12T00:00:00Z", periods: [period], default_period: period.key, quarters: [{ key: "2026-Q3", year: 2026, quarter: 3, label: "2026年第3季度", complete: false }], default_quarter: "2026-Q3", disclaimer: "演示数据" });
    }
    const current = companies.find(item => item.company_id === query.company_id) || companies[0];
    if (path === "/api/dashboard/brief") {
      const n = query.after_number === "1" ? 2 : 1;
      return reply({ schema_version: 1, selected_period: period, data: { ...f.brief, generated_at: "2026-09-12T00:00:00Z", voucher_count: 2, line_count: 4, vouchers: [voucher(n, current.name)], voucher_page: { has_more: n === 1, next_after_number: n, total_count: 2 }, position: { ...f.position, assets_fen: "9007199254740993", bank_fen: "9007199254740993", equation_valid: true }, cash: f.cash, unmatched_bank_activity: { count: 0, ordinary_count: 0, pending_late_count: 0, inflow_fen: "0", outflow_fen: "0", rows: [] }, open_items: { receivable_count: 0, receivable_fen: "0", payable_count: 0, payable_fen: "0", total_count: 0, categories: [] }, workforce_cost: workforce, long_term_assets: { net_fen: "0", fixed_net_fen: "0", intangible_net_fen: "0", fixed_active_count: 0, intangible_active_count: 0 }, validation: { state: "attention", title: "待核对", summary: "演示账务", integrity_valid: true, attention_count: 0, items: [] }, material_completeness: { closed: false, satisfied: false, issues: [] } } });
    }
    if (path === "/api/local/trace") return reply({ calculation: { id: query.calculation_id, subject_id: "subject-1", kind: "expense", period: 202609, digest: "fixture", program_version: "test", outcome: { lines: [], values: {}, explanation: [], balances: [] } }, facts: [{ id: "fact-1", subject_id: "subject-1", revision: 2, kind: "expense", data: { recognition_period: "2026-09" }, evidence: ["fixture-evidence"] }], upstream: [] });
    if (path === "/api/dashboard/funds") {
      const second = !!query.after_movement;
      return reply({ schema_version: 1, selected_period: period, data: { ...f.funds, movement_count: 2, movements: [{ id: second ? "m2" : "m1", date: "2026-09", account_code: "1002", account_name: "演示账户", account_type: "bank", direction: "outflow", amount_fen: "100", signed_amount_fen: "-100", reference: second ? "资金第二页" : "资金第一页", type: "费用", summary: "演示付款", display_summary: "演示付款", party: "", internal_transfer: false, component_kinds: [] }], movement_page: { has_more: !second, next_cursor: second ? null : "m1", total_count: 2 }, bank_statement: { ...f.bank, page: pageInfo } } });
    }
    if (path === "/api/dashboard/employees") return reply({ schema_version: 1, selected_period: period, data: { employees: { ...f.employees, registered_count: 1, unknown_period_count: 1, items: [{ ...f.employee, code: "employee-1", name: "姓名未提供", profile_available: true, record_status: "unknown", period_state: "unknown", period_state_label: "核算范围未提供", tax_reported_salary_fen: null, net_salary_fen: null, wage_tax_scope: "none", wage_tax_scope_label: "未提供" }] }, workforce_cost: workforce } });
    if (path === "/api/dashboard/assets") return reply({ schema_version: 1, selected_period: period, data: { ...f.assets, fixed: { ...f.fixed, items: [asset("fixed")] }, intangible: { ...f.intangible, items: [asset("intangible")] }, reconciled: true, reconciliation_label: "相符", differences: { cost_fen: "0", accumulated_fen: "0", net_fen: "0" } } });
    if (path === "/api/dashboard/quarterly-report") return reply({ schema_version: 1, status: "ready", status_label: "可导出", headline: "季度报表已准备", message: "演示报表", checked_at: "2026-09-12T00:00:00Z", period: { year: 2026, quarter: 3, label: "2026年第3季度", quarter_end: "2026-09-30" }, readiness: [], summary: { assets_total_fen: "0", liabilities_equity_total_fen: "0", current_net_profit_fen: "0", year_to_date_net_profit_fen: "0", current_cash_change_fen: "0", ending_cash_fen: "0" }, statements: [{ key: "profit_statement", label: "利润表", columns: [{ key: "current_fen", label: "本季金额" }], rows: [{ line: 32, name: "净利润", values: { current_fen: "0" }, is_total: true, has_amount: true }] }], checks: { passed: 0, total: 0, items: [] }, draft: false, export: { available: true, file_name: "fixture.xlsx", calculation_hash: "fixture-hash", preview_digest: "fixture-preview", epochs: { accounting: 1, material: 2, management: 3 } }, technical: { calculation_hash: "fixture-hash", template: {}, rule: {}, source_close_hashes: [], classification_count: null, income_tax_confirmation_count: null, requirement_codes: [], errors: [] } });
    if (path === "/api/local/report-export") {
      const data = request.postDataJSON();
      if (data.company_id !== "company-b" || data.preview_digest !== "fixture-preview" || !data.request_id || data.epochs.accounting !== 1) throw new Error("wrong report export body");
      exportPosts++; return reply({ status: "queued", job_id: "old-report-job" });
    }
    if (path === "/api/local/jobs") {
      if (query.job_id) { jobPolls++; return reply([{ id: query.job_id, kind: "report_export", status: jobPolls > 1 ? "succeeded" : "running", attempts: 1, last_error: null, result: {} }]); }
      return reply([{ id: "pending-job", kind: "portable_backup", status: "pending", attempts: 0, last_error: null, result: null }]);
    }
    if (path === "/api/local/report-export/old-report-job/download") {
      if (jobPolls < 2) throw new Error("download before successful job");
      return route.fulfill({ status: 200, contentType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", body: "synthetic fixture" });
    }
    throw new Error(`Unexpected API ${path}`);
  });
  await page.goto(`${base}/?org_id=old-company&period=2026-09#ticket=fixture-ticket`);
  await page.getByRole("button", { name: "负责人登录", exact: true }).click();
  await page.getByText("目录中还没有公司。登记公司后，可在这里查看已发布账务。", { exact: true }).waitFor();
  if (await page.locator('input[type="password"]').count()) throw new Error("password input in browser");
  if (page.url().includes("#") || page.url().includes("org_id")) throw new Error("legacy ID or ticket retained");
  includeCompany = true;
  await page.reload();
  await page.getByRole("heading", { name: "月度经营与财务概览" }).waitFor();
  await page.getByRole("button", { name: "按凭证看", exact: true }).click();
  await page.getByRole("button", { name: /演示甲公司月度费用1/ }).click();
  await page.getByRole("button", { name: "查看核算依据", exact: true }).click();
  await page.getByRole("heading", { name: "费用的核算依据" }).waitFor();
  if (!(await page.locator("body").innerText()).includes("¥90,071,992,547,409.93")) throw new Error("large amount rounded");
  if ((await page.locator("body").innerText()).includes("NaN")) throw new Error("month precision produced fake day");
  await page.getByRole("button", { name: "加载更多业务与凭证" }).click();
  await page.getByRole("button", { name: /演示甲公司月度费用2/ }).waitFor();
  await page.getByLabel("切换公司").selectOption("company-b");
  await page.getByRole("button", { name: "按凭证看", exact: true }).click();
  await page.getByRole("button", { name: /演示乙公司月度费用1/ }).waitFor();
  if ((await page.locator("body").innerText()).includes("演示甲公司月度费用")) throw new Error("previous company data retained");
  await page.getByRole("button", { name: "后台任务", exact: true }).click();
  await page.getByRole("heading", { name: "最近后台任务" }).waitFor();
  await page.getByText("等待处理", { exact: true }).waitFor();
  await page.getByRole("button", { name: "后台任务", exact: true }).click();
  await page.getByRole("link", { name: "资金", exact: true }).click();
  await page.getByRole("button", { name: "加载更多账面明细" }).click();
  await page.getByText("资金第二页", { exact: true }).waitFor();
  await page.getByRole("link", { name: "员工", exact: true }).click();
  await page.getByText("姓名未提供", { exact: true }).first().waitFor();
  if (!(await page.locator("body").innerText()).includes("人数未提供")) throw new Error("unknown workforce count hidden");
  await page.getByText("姓名未提供", { exact: true }).first().click();
  const employmentStatus = page.locator(".employee-profile-grid div").filter({ has: page.getByText("员工档案状态", { exact: true }) });
  if (!(await employmentStatus.innerText()).includes("未提供")) throw new Error("unknown employment status presented as inactive");
  await page.getByRole("link", { name: "资产", exact: true }).click();
  await page.getByRole("heading", { name: "资产概览" }).waitFor();
  await page.getByText("启用日期未提供", { exact: true }).waitFor();
  await page.getByRole("heading", { name: "演示无形资产", exact: true }).click();
  const amortizationLife = page.locator(".asset-card").filter({ has: page.getByRole("heading", { name: "演示无形资产", exact: true }) }).locator(".asset-detail div").filter({ has: page.getByText("摊销期限", { exact: true }) });
  if (!(await amortizationLife.innerText()).includes("未提供")) throw new Error("missing useful life is not explicit");
  if (!(await page.locator("body").innerText()).includes("可供使用日期未提供")) throw new Error("missing asset date hidden");
  if ((await page.locator("body").innerText()).includes("null 个月")) throw new Error("null useful life rendered as duration");
  await page.getByRole("link", { name: "财务报表", exact: true }).click();
  await page.getByRole("tab", { name: /利润表/ }).click();
  await page.locator("details.technical > summary").click();
  const counts = await page.locator("details.technical").innerText();
  if (counts.includes("null 项") || !counts.includes("未提供")) throw new Error("null report counts rendered as items");
  await page.getByRole("button", { name: "导出", exact: true }).click();
  await page.getByText("已下载经过校验的季度报表文件，请负责人逐项复核后使用。", { exact: true }).waitFor();
  await page.getByRole("link", { name: "经营简报", exact: true }).click();
  await page.getByRole("heading", { name: "月度经营与财务概览" }).waitFor();
  await page.setViewportSize({ width: 1440, height: 1080 });
  await page.screenshot({ path: ".playwright-cli/restored-dashboard-desktop.png", fullPage: true });
  await page.getByRole("button", { name: "切换深色外观" }).click();
  await page.screenshot({ path: ".playwright-cli/restored-dashboard-dark.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: ".playwright-cli/restored-dashboard-mobile.png", fullPage: true });
  if (await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)) throw new Error("mobile overflow");
  expired = true;
  await page.getByRole("button", { name: "刷新数据", exact: true }).click();
  await page.getByRole("button", { name: "负责人登录", exact: true }).waitFor();
  if (await page.getByRole("heading", { name: "月度经营与财务概览" }).count()) throw new Error("expired session retained business view");
  if (ticketExchanges !== 1 || exportPosts !== 1 || jobPolls !== 2) throw new Error("ticket reuse or wrong export lifecycle");
  if (failures.length) throw new Error(failures.join("\n"));
  console.log(JSON.stringify({ passed: true, views: 5, ticketExchanges, exportPosts, jobPolls, requests: calls.length, mobileWidth: 390 }));
}
