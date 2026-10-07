// Dashboard scenarios use fixtures; isolated runs also stub local authentication.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

async function run(config) {
  const { chromium } = require(config.playwright_module);
  const fixtures = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures/dashboard-contracts.json"), "utf8"));
  const validators = {};
  for (const name of ["Brief", "Funds", "BusinessStatus"]) validators[name] = (await import(pathToFileURL(path.join(__dirname, `../src/api/generated/dashboard${name}.js`))))[`validateDashboard${name}Response`];
  const clone = value => structuredClone(value);
  const browser = await chromium.launch({ channel: config.channel || "msedge", headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
  page.setDefaultTimeout(12000);
  const requests = [], errors = [], fixtureErrors = [], layouts = [];
  let phase = "synthetic authentication", snapshot = "unified-v1", failVouchers = true, holdVouchers = false, releaseHeld, heldStarted;
  const periods = [config.period, config.period.endsWith("-12") ? `${Number(config.period.slice(0, 4)) + 1}-01` : `${config.period.slice(0, 4)}-${String(Number(config.period.slice(5)) + 1).padStart(2, "0")}`];
  const companyIds = [config.company_id, "unified-second-company"];
  const pageInfo = (items, total = items.length, more = false) => ({ total_count: total, filtered_count: total, returned_count: items.length, has_more: more, next_cursor: more ? "unified-next" : null });
  const recognition = period => ({ precision: "month", period, date: null, label: period });
  const periodInfo = period => ({ ...fixtures.company_with_period.response.periods[0], key: period, year: Number(period.slice(0, 4)), month: Number(period.slice(5)), label: `${period.slice(0, 4)} 年 ${Number(period.slice(5))} 月`, short_label: `${Number(period.slice(5))} 月`, start_date: `${period}-01`, end_date: `${period}-${new Date(Number(period.slice(0, 4)), Number(period.slice(5)), 0).getDate()}` });
  const scope = (response, url) => {
    response.read_context.company_id = url.searchParams.get("company_id") || config.company_id;
    response.selected_period = periodInfo(url.searchParams.get("period") || config.period);
    response.snapshot_version = snapshot; return response;
  };
  function brief(url) {
    const response = scope(clone(fixtures.brief.response), url), data = response.data, period = response.selected_period.key;
    const base = data.collections.activity.items[0], baseVoucher = data.collections.vouchers.items[0];
    const activities = [0, 1].map(index => ({ ...clone(base), key: `unified-activity-${index}`, subject_id: `unified-business-${index}`, voucher_version_id: `unified-voucher-${index}`, date: `${period}-${index ? "02" : "28"}`, recognition: { precision: "day", period, date: `${period}-${index ? "02" : "28"}`, label: `${period}-${index ? "02" : "28"}` }, party: "演示业务对象名称较长用于自然换行检查", description: `演示事项${index + 1}与明确业务用途`, amount_fen: "9007199254740993", state: index ? "更正原业务" : "已入账" }));
    const vouchers = Array.from({ length: 30 }, (_, index) => ({ ...clone(baseVoucher), voucher_version_id: `unified-voucher-${index}`, subject_id: `unified-business-${index}`, number: String(index + 1), recognition: recognition(period), date: null, list_summary: `独立凭证事项${index + 1}`, summary: `独立凭证完整说明${index + 1}` }));
    data.activity_count = 2; data.voucher_count = 30; data.focused_activity = null; data.focused_voucher = null;
    const voucherNumber = url.searchParams.get("voucher_number");
    const voucherVersion = url.searchParams.get("voucher_version_id");
    if (voucherNumber || voucherVersion) data.focused_voucher = vouchers.find(voucher => voucherNumber ? voucher.number === voucherNumber : voucher.voucher_version_id === voucherVersion) || null;
    data.activity_groups = [{ ...data.activity_groups.find(group => group.key === base.group), event_count: 2 }];
    data.collections.activity = { items: activities, page: pageInfo(activities) };
    // The default paired slice cannot stand in for the independent ordered voucher page.
    data.collections.vouchers = { items: vouchers.slice(0, 2), page: pageInfo(vouchers.slice(0, 2)) };
    const open = ["employee_social", "employer_social", "employee_housing", "employer_housing"].map((component, index) => ({ id: `unified-obligation-${index}`, category_key: "payroll_payables", party: "演示员工姓名较长用于自然换行检查", description: "社保", status: "partial", source_amount_fen: "10000", paid_fen: "2000", other_settled_fen: "1000", outstanding_fen: "7000", current_status: "partial", current_outstanding_fen: "7000", subject_id: `unified-payroll-${index}`, contribution_group_key: "unified-employee", contribution_component: component, payroll_period: period }));
    data.open_items = { ...data.open_items, receivable_count: 0, receivable_fen: "0", payable_count: 4, payable_fen: "28000", total_count: 4, complete: true, cutoff_period: period, current_cutoff_period: period, categories: [{ key: "payroll_payables", label: "待付工资、社保与个税", direction: "payable", unit: "笔", count: 4, loaded_count: 4, outstanding_fen: "28000" }] };
    data.collections.open_items = { items: open, page: pageInfo(open) };
    const section = url.searchParams.get("section");
    if (section === "vouchers") {
      const items = url.searchParams.has("cursor") ? vouchers.slice(20) : vouchers.slice(0, 20);
      data.collections = { vouchers: { items, page: pageInfo(items, 30, !url.searchParams.has("cursor")) } };
    } else if (section) data.collections = section === "activity" ? { activity: data.collections.activity, vouchers: data.collections.vouchers } : { [section]: data.collections[section] };
    return response;
  }
  function funds(url) {
    const response = scope(clone(fixtures.first_account_funds.response), url), data = response.data, period = response.selected_period.key;
    const account = data.collections.accounts.items[0]; account.name = "演示现金账户";
    const movement = data.collections.movements.items[0];
    const items = [0, 1].map(index => ({ ...clone(movement), id: `unified-movement-${index}`, subject_id: `unified-business-${index}`, date: `${period}-${index ? "02" : "28"}`, list_summary: "演示资金用途较长用于自然换行检查", display_summary: "演示资金完整业务用途", party: "演示往来对象", direction: "outflow", amount_fen: "9007199254740993", signed_amount_fen: "-9007199254740993", correction: false }));
    data.selected_movement_account = { type: account.type, account_id: account.account_id };
    data.collections.movements = { items, page: pageInfo(items) }; data.movement_count = 2;
    const section = url.searchParams.get("section"); if (section) data.collections = { [section]: data.collections[section] };
    return response;
  }
  function detail(url) {
    const response = scope(clone(fixtures.business_status.response), url), data = response.data, period = response.selected_period.key;
    data.identity.company_id = response.read_context.company_id; data.identity.subject_id = url.searchParams.get("subject_id");
    data.period = period; data.latest_source.period = period; data.current_business_result.posting_period = period;
    data.settlements.cutoff_period = period; data.current_followups.settlements.cutoff_period = period;
    data.collections.settlement_events = { ...data.collections.settlement_events, items: [], page: pageInfo([]), scope_period: period, current_cutoff_period: period };
    data.settlement_view = url.searchParams.get("settlement_view") || "current";
    return response;
  }
  const safe = handler => async route => {
    try { await handler(route); } catch (error) { fixtureErrors.push(error.message); await route.fulfill({ status: 500, json: { message: "合成响应构造失败" } }).catch(() => {}); }
  };
  page.on("request", request => { const url = new URL(request.url()); if (url.pathname.startsWith("/api/dashboard/")) requests.push(url); });
  page.on("pageerror", () => errors.push("browser script error"));
  const frames = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const idle = async () => { await page.waitForFunction(() => document.querySelector(".module-header")?.getAttribute("aria-busy") === "false"); await frames(); };
  const nav = name => page.locator(".section-nav").getByRole("button", { name, exact: true });
  const voucherRequests = () => requests.filter(url => url.pathname.endsWith("brief") && url.searchParams.get("section") === "vouchers");
  const allBrief = () => requests.filter(url => url.pathname.endsWith("brief")).length;
  const screenshotDir = config.screenshots_directory || config.screenshot_directory;
  async function keyboard(row, panel, arrow) {
    await row.focus(); await row.press("Enter"); await panel.waitFor();
    assert.equal(await row.getAttribute("aria-expanded"), "true");
    assert.equal(await arrow.count(), 1, "one expansion arrow required");
    assert(await arrow.evaluate(element => element.classList.contains("expanded")), "expanded arrow did not rotate");
    await panel.click({ position: { x: 8, y: 8 } }); assert.equal(await row.getAttribute("aria-expanded"), "true", "internal detail click collapsed row");
    await panel.evaluate(element => { element.tabIndex = 0; }); await panel.focus(); await panel.press("Enter"); await panel.press("Space");
    assert.equal(await row.getAttribute("aria-expanded"), "true", "internal keyboard collapsed row");
    await row.focus(); await row.press("Space"); assert.equal(await row.getAttribute("aria-expanded"), "false");
    assert(!await arrow.evaluate(element => element.classList.contains("expanded")), "collapsed arrow stayed expanded");
    await row.press("Enter"); await panel.waitFor();
  }
  async function layout(name, row, selectors) {
    for (const theme of ["light", "dark"]) {
      await page.evaluate(theme => document.documentElement.dataset.theme = theme, theme);
      for (const width of [1440, 768, 375, 320]) {
        await page.setViewportSize({ width, height: 1000 }); await frames();
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `${name} document overflow ${theme}/${width}`);
        assert(await row.evaluate(element => element.scrollWidth <= element.clientWidth + 1), `${name} row overflow ${theme}/${width}`);
        assert(await row.evaluate(element => {
          const object = element.querySelector(".business-list-object strong"), matter = element.querySelector(".business-list-matter");
          return Number(getComputedStyle(object).fontWeight) > Number(getComputedStyle(matter).fontWeight);
        }), `${name} object must be stronger than matter`);
        const boxes = await row.evaluate((element, selectors) => selectors.map(selector => { const box = element.querySelector(selector)?.getBoundingClientRect(); return box ? { x: box.x, y: box.y, right: box.right, bottom: box.bottom } : null; }), selectors);
        assert(boxes.every(Boolean), `${name} required columns missing`);
        if (width === 1440) {
          for (let index = 1; index < boxes.length; index++) assert(boxes[index].x >= boxes[index - 1].right - 2, `${name} desktop columns overlap/order mismatch`);
        } else {
          const mobile = await row.evaluate(element => {
            const box = selector => { const rect = element.querySelector(selector)?.getBoundingClientRect(); return rect ? { x: rect.x, y: rect.y, right: rect.right, bottom: rect.bottom } : null; };
            return { object: box(".business-list-object"), matter: box(".business-list-matter"), date: box(".business-list-date"), state: box(".business-list-state"), money: box(".business-list-money"), voucher: box(".business-list-voucher"), arrow: box(".business-list-arrow") };
          });
          assert(mobile.matter.y >= mobile.object.bottom - 2, `${name} matter must follow object vertically`);
          assert(mobile.state.y >= mobile.matter.bottom - 2, `${name} state must follow matter vertically`);
          assert(mobile.money.y >= mobile.state.bottom - 2, `${name} money must follow status vertically`);
          if (mobile.date) { assert(mobile.date.y >= mobile.matter.bottom - 2); assert(mobile.date.right <= mobile.state.x + 2, `${name} date/status overlap`); }
          if (mobile.voucher) assert(mobile.money.right <= mobile.voucher.x + 2, `${name} money/voucher overlap`);
          assert(mobile.arrow.y < mobile.matter.y, `${name} arrow must stay at upper right`);
        }
        if (screenshotDir) { fs.mkdirSync(screenshotDir, { recursive: true }); await row.screenshot({ path: path.join(screenshotDir, `unified-${name}-${theme}-${width}.png`), style: "body * { visibility: hidden !important; } .business-list-row, .business-list-row * { visibility: visible !important; }" }); }
        layouts.push({ name, theme, width });
      }
    }
    await page.setViewportSize({ width: 1440, height: 1000 }); await frames();
  }
  try {
    if (config.synthetic_authentication) await page.route("**/api/**", route => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === "/api/browser-session") return route.fulfill({ json: { status: "ok" } });
      if (pathname === "/api/security-request") return route.fulfill({ json: {
        schema_version: 1, catalog_instance_id: "unified-synthetic-catalog",
        provisioned: true, login_name: "演示负责人", active: true, authenticated: true,
      } });
      fixtureErrors.push(`unhandled isolated API: ${pathname}`);
      return route.fulfill({ status: 503, json: { message: "隔离测试未配置此接口" } });
    });
    await page.route("**/api/dashboard/context?*", safe(route => {
      const url = new URL(route.request().url()), response = clone(fixtures.company_with_period.response);
      response.companies = companyIds.map((company_id, index) => ({ ...response.companies[0], company_id, name: `演示公司${index + 1}`, taxpayer_id: null }));
      response.current_company = response.companies.find(company => company.company_id === url.searchParams.get("company_id")) || response.companies[0]; response.company = response.current_company.name;
      response.periods = periods.map(periodInfo); response.default_period = config.period;
      return route.fulfill({ json: response });
    }));
    await page.route("**/api/dashboard/brief?*", safe(async route => {
      const url = new URL(route.request().url()), response = brief(url);
      if (url.searchParams.get("section") === "vouchers") {
        assert.equal(url.searchParams.get("limit"), "20"); assert.equal(url.searchParams.get("expected_version"), snapshot);
        if (failVouchers) { failVouchers = false; return route.fulfill({ status: 503, json: { message: "合成独立凭证首次读取失败" } }); }
        if (holdVouchers) { holdVouchers = false; await new Promise(resolve => { releaseHeld = resolve; heldStarted?.(); }); }
      }
      assert(validators.Brief(response), `brief fixture: ${JSON.stringify(validators.Brief.errors)}`); await route.fulfill({ json: response }).catch(() => {});
    }));
    await page.route("**/api/dashboard/funds?*", safe(route => { const response = funds(new URL(route.request().url())); assert(validators.Funds(response), `funds fixture: ${JSON.stringify(validators.Funds.errors)}`); return route.fulfill({ json: response }); }));
    await page.route("**/api/dashboard/business-status?*", safe(route => { const response = detail(new URL(route.request().url())); assert(validators.BusinessStatus(response), `detail fixture: ${JSON.stringify(validators.BusinessStatus.errors)}`); return route.fulfill({ json: response }); }));
    const ticket = new URL(config.ticket_url); ticket.searchParams.set("company_id", config.company_id); ticket.searchParams.set("period", config.period);
    await page.goto(ticket.href); await idle(); await nav("本月发生").click();
    phase = "activity layout and keyboard";
    const activity = page.locator(".activity-section .event-row").first();
    await keyboard(activity, activity.locator(".business-detail-panel:not(.owner-activity-summary)"), activity.locator(".business-list-arrow"));
    await layout("activity", activity, [".business-list-date", ".business-list-object", ".business-list-matter", ".business-list-state", ".business-list-money", ".business-list-voucher", ".business-list-arrow"]);
    phase = "paired voucher preview and local exact selection";
    await activity.focus(); await activity.press("Space");
    const beforePreview = allBrief(); await activity.locator(".event-voucher-button").focus(); await page.getByRole("tooltip").waitFor(); await frames();
    assert.equal(allBrief(), beforePreview, "paired preview requested brief");
    await activity.locator(".event-voucher-button").click(); await page.locator("#selected-voucher").waitFor(); await frames();
    assert.equal(allBrief(), beforePreview, "exact paired selection requested brief"); assert.equal(voucherRequests().length, 0, "exact selection initialized ordered list");
    assert.equal(await page.locator(".voucher-card").count(), 1, "uninitialized exact selection should show only focused card");
    phase = "same-snapshot refresh preserves local precise focus";
    await page.getByRole("button", { name: "刷新数据", exact: true }).click(); await idle();
    assert.equal(await page.locator("#selected-voucher").count(), 1);
    assert.equal(await page.locator(".voucher-card").count(), 1, "precise refresh lost the selected card");
    assert.equal(voucherRequests().length, 0, "precise refresh initialized the monthly collection");
    phase = "manual independent voucher first page and retry cache";
    await page.getByRole("button", { name: "按业务", exact: true }).click(); await page.getByRole("button", { name: "按凭证", exact: true }).click();
    await page.locator(".voucher-view").getByRole("button", { name: "重新读取", exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll(".voucher-card").length === 20); await frames();
    assert.equal(voucherRequests().length, 2); for (const url of voucherRequests()) assert.equal(url.searchParams.has("cursor"), false, "first ordered page must not reuse paired cursor");
    const cached = voucherRequests().length; await page.getByRole("button", { name: "按业务", exact: true }).click(); await page.getByRole("button", { name: "按凭证", exact: true }).click(); await frames(); assert.equal(voucherRequests().length, cached);
    phase = "cancelled pending voucher page resumes on module return";
    holdVouchers = true;
    const pendingStarted = new Promise(resolve => { heldStarted = resolve; });
    const beforePending = voucherRequests().length;
    await page.locator(".voucher-pagination").getByRole("button", { name: "下一页", exact: true }).click();
    await Promise.race([pendingStarted, new Promise((_, reject) => setTimeout(() => reject(new Error("pending page was not held")), 12000))]);
    await nav("概览").click(); await nav("本月发生").click();
    await page.waitForFunction(() => document.querySelector(".voucher-pagination strong")?.textContent.trim() === "2 / 2");
    assert.equal(voucherRequests().length, beforePending + 2, "return must resume the cancelled page once");
    releaseHeld(); releaseHeld = null; await frames();
    assert.equal(await page.locator(".voucher-card").count(), 10, "late cancelled page changed the current page");
    phase = "same-snapshot refresh retains paged and complete vouchers";
    const refreshes = [];
    for (const width of [1440, 375]) {
      await page.setViewportSize({ width, height: 1000 }); await frames();
      const before = voucherRequests().length;
      await page.getByRole("button", { name: "刷新数据", exact: true }).click(); await idle();
      assert.equal(await page.locator(".voucher-pagination strong").textContent(), "2 / 2");
      assert.equal(await page.locator(".voucher-card").count(), 10);
      assert.equal(voucherRequests().length, before, "same-snapshot refresh reread an independent collection");
      await page.getByRole("switch", { name: "改为全部显示凭证", exact: true }).click();
      await page.waitForFunction(() => document.querySelectorAll(".voucher-card").length === 30);
      await page.getByRole("button", { name: "刷新数据", exact: true }).click(); await idle();
      assert.equal(await page.getByRole("switch", { name: "改为分页显示凭证", exact: true }).getAttribute("aria-checked"), "true");
      assert.equal(await page.locator(".voucher-card").count(), 30);
      assert.equal(voucherRequests().length, before, "complete list was reread during refresh");
      refreshes.push({ width, page_retained: 2, all_retained: 30, voucher_requests: 0 });
      await page.getByRole("switch", { name: "改为分页显示凭证", exact: true }).click();
      await page.locator(".voucher-pagination").getByRole("button", { name: "下一页", exact: true }).click();
    }
    await page.setViewportSize({ width: 1440, height: 1000 }); await frames();
    phase = "changed snapshot discards independent collection";
    const beforeChanged = voucherRequests().length; snapshot = "unified-v2";
    await page.getByRole("button", { name: "刷新数据", exact: true }).click(); await idle();
    assert.equal(await page.locator(".voucher-view").count(), 0, "changed snapshot retained old voucher mode");
    assert.equal(voucherRequests().length, beforeChanged, "changed snapshot initialized vouchers without selection");
    phase = "refresh during initialization rejects old response and restores manual mode";
    holdVouchers = true;
    const initializingStarted = new Promise(resolve => { heldStarted = resolve; });
    await page.getByRole("button", { name: "按凭证", exact: true }).click();
    await Promise.race([initializingStarted, new Promise((_, reject) => setTimeout(() => reject(new Error("initialization was not held")), 12000))]);
    const beforeInitializingRefresh = voucherRequests().length;
    await page.getByRole("button", { name: "刷新数据", exact: true }).click(); await idle();
    await page.waitForFunction(() => document.querySelectorAll(".voucher-card").length === 20);
    assert.equal(voucherRequests().length, beforeInitializingRefresh + 1, "manual initialization must resume after refresh");
    assert.equal(voucherRequests().at(-1).searchParams.has("cursor"), false);
    releaseHeld(); releaseHeld = null; await frames();
    assert.equal(await page.locator(".voucher-card").count(), 20);
    phase = "direct voucher target retains manual paging and complete mode on refresh";
    await page.goto(`${config.origin}/?company_id=${encodeURIComponent(config.company_id)}&period=${config.period}&voucher=1`); await idle();
    await page.locator("#selected-voucher").waitFor();
    assert.equal(await page.locator(".voucher-card").count(), 1);
    await page.getByRole("button", { name: "按业务", exact: true }).click();
    await page.getByRole("button", { name: "按凭证", exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll(".voucher-card").length === 20);
    await page.locator(".voucher-pagination").getByRole("button", { name: "下一页", exact: true }).click();
    await page.waitForFunction(() => document.querySelector(".voucher-pagination strong")?.textContent === "2 / 2");
    const beforeDirectRefresh = voucherRequests().length;
    await page.getByRole("button", { name: "刷新数据", exact: true }).click(); await idle();
    assert.equal(await page.locator(".voucher-pagination strong").textContent(), "2 / 2");
    assert.equal(await page.locator(".voucher-card").count(), 10);
    assert.equal(voucherRequests().length, beforeDirectRefresh, "direct target reset manual pagination during refresh");
    await page.getByRole("switch", { name: "改为全部显示凭证", exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll(".voucher-card").length === 30);
    await page.getByRole("button", { name: "刷新数据", exact: true }).click(); await idle();
    assert.equal(await page.getByRole("switch", { name: "改为分页显示凭证", exact: true }).getAttribute("aria-checked"), "true");
    assert.equal(await page.locator(".voucher-card").count(), 30);
    assert.equal(voucherRequests().length, beforeDirectRefresh, "direct target reread a complete collection during refresh");
    phase = "open items layout and keyboard";
    await nav("待收待付").click(); const open = page.locator(".open-event-row").first();
    const detailsBefore = requests.filter(url => url.pathname.endsWith("business-status")).length;
    await keyboard(open, open.locator(".contribution-detail"), open.locator(".business-list-arrow"));
    assert.equal(requests.filter(url => url.pathname.endsWith("business-status")).length, detailsBefore, "group expansion requested business status");
    await layout("open-items", open, [".business-list-object", ".business-list-matter", ".business-list-state", ".business-list-money", ".business-list-arrow"]);
    phase = "funds layout and keyboard";
    await page.goto(`${config.origin}/funds?company_id=${encodeURIComponent(config.company_id)}&period=${config.period}`); await idle(); await nav("资金明细").click();
    const movement = page.locator(".book-activity-row").first();
    await keyboard(movement, movement.locator(".business-detail-panel"), movement.locator(".business-list-arrow"));
    await layout("funds", movement, [".business-list-date", ".business-list-object", ".business-list-matter", ".business-list-state", ".business-list-money", ".business-list-arrow"]);
    phase = "late first ordered voucher page after scope change";
    await page.goto(`${config.origin}/?company_id=${encodeURIComponent(config.company_id)}&period=${config.period}`); await idle(); await nav("本月发生").click();
    holdVouchers = true; const started = new Promise(resolve => { heldStarted = resolve; }); await page.getByRole("button", { name: "按凭证", exact: true }).click();
    await page.waitForFunction(() => document.querySelector(".voucher-view")?.getAttribute("aria-busy") === "true");
    await Promise.race([started, new Promise((_, reject) => setTimeout(() => reject(new Error("late fixture request was not held")), 12000))]);
    await page.getByRole("combobox", { name: "切换公司", exact: true }).selectOption(companyIds[1]); await idle(); releaseHeld(); releaseHeld = null; await frames();
    assert.equal(await page.locator(".voucher-view").count(), 0, "stale ordered page switched new scope into voucher mode");
    await nav("本月发生").click(); const beforeNewScope = voucherRequests().length; await page.getByRole("button", { name: "按凭证", exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll(".voucher-card").length === 20); assert.equal(voucherRequests().length, beforeNewScope + 1, "new company incorrectly reused stale ordered page");
    phase = "month switch resets independent voucher initialization";
    await page.getByRole("combobox", { name: "查看月份", exact: true }).selectOption(periods[1]); await idle();
    assert.equal(await page.locator(".voucher-view").count(), 0, "month switch kept stale voucher mode");
    await nav("本月发生").click(); const beforeMonth = voucherRequests().length; await page.getByRole("button", { name: "按凭证", exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll(".voucher-card").length === 20); assert.equal(voucherRequests().length, beforeMonth + 1, "month switch reused old ordered page");
    assert.equal(fixtureErrors.length, 0, fixtureErrors.join("; ")); assert.equal(errors.length, 0);
    return { status: "passed", synthetic_layouts: layouts, keyboard_and_arrows: true, internal_detail_isolation: true, paired_preview_requests: 0, local_exact_selection_requests: 0, precise_refresh_retained: true, ordered_first_page_limit: 20, ordered_first_page_cursor: null, ordered_initial_retry: true, ordered_cached_switch_requests: 0, pending_page_return_resumes: true, same_snapshot_refresh: refreshes, direct_target_manual_mode_retained: true, changed_snapshot_discards: true, initialization_refresh_resumes: true, scope_late_response_rejected: true, month_initialization_reset: true, browser_errors: 0 };
  } catch (error) { throw new Error(`${phase}: ${fixtureErrors.length ? fixtureErrors.join("; ") : String(error.message).replace(/https?:\/\/[^\s"']+/g, "[URL]").split(config.company_id).join("[company]")}`); }
  finally { releaseHeld?.(); await browser.close(); }
}
if (require.main === module) {
  let input = ""; process.stdin.setEncoding("utf8"); process.stdin.on("data", chunk => input += chunk);
  process.stdin.on("end", async () => { try { process.stdout.write(JSON.stringify(await run(JSON.parse(input))) + "\n"); } catch (error) { process.stdout.write(JSON.stringify({ status: "failed", message: error.message }) + "\n"); process.exitCode = 1; } });
}
module.exports = { run };
