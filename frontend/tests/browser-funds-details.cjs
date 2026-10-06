// Existing company reads only; all scenario responses and screenshots are browser fixtures.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

async function run(config) {
  const { chromium } = require(config.playwright_module);
  const { validateDashboardFundsResponse: validateFunds } = await import(pathToFileURL(path.join(__dirname, "../src/api/generated/dashboardFunds.js")));
  const { validateDashboardBusinessStatusResponse: validateStatus } = await import(pathToFileURL(path.join(__dirname, "../src/api/generated/dashboardBusinessStatus.js")));
  const fixtures = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures/dashboard-contracts.json"), "utf8"));
  const clone = value => structuredClone(value);
  const browser = await chromium.launch({ channel: config.channel || "msedge", headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
  page.setDefaultTimeout(12000);
  const requests = [], errors = [], held = [], fixtureErrors = [];
  let notifyHeld = null;
  const takeHeld = async () => {
    if (!held.length) await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => { notifyHeld = null; reject(new Error("synthetic delayed route was not reached")); }, 12000);
      notifyHeld = () => { clearTimeout(timeout); notifyHeld = null; resolve(); };
    });
    return held.shift();
  };
  const safeRoute = handler => async route => {
    try { await handler(route); }
    catch (error) {
      fixtureErrors.push(String(error.message));
      await route.fulfill({ status: 500, json: { message: "合成回归响应构造失败" } }).catch(() => {});
    }
  };
  page.on("request", request => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/dashboard/")) requests.push(url);
  });
  page.on("pageerror", () => errors.push("browser script error"));
  const detailCount = () => requests.filter(url => url.pathname.endsWith("business-status")).length;
  const frames = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const idle = async () => {
    await page.waitForFunction(() => document.querySelector(".module-header")?.getAttribute("aria-busy") === "false");
    await frames();
  };
  const reply = action => page.waitForResponse(response => new URL(response.url()).pathname === `/api/dashboard/${action}`);
  const rows = () => page.locator(".book-activity-row");
  const nav = name => page.locator(".section-nav").getByRole("button", { name, exact: true });
  const refresh = async () => {
    const pending = reply("funds");
    await page.getByRole("button", { name: "刷新数据", exact: true }).click();
    await pending; await idle();
  };
  const money = value => {
    if (value == null) return "待核对";
    const n = BigInt(value), abs = n < 0n ? -n : n;
    return `${n < 0n ? "−" : ""}¥${new Intl.NumberFormat("zh-CN").format(abs / 100n)}.${String(abs % 100n).padStart(2, "0")}`;
  };
  const amount = item => item.correction ? money(item.signed_amount_fen) : `${item.direction === "inflow" ? "+" : "−"}${money(item.amount_fen)}`;
  const color = (locator, token) => locator.evaluate((element, token) => {
    const probe = document.createElement("span"); probe.style.color = `var(${token})`; element.append(probe);
    const ok = getComputedStyle(element).color === getComputedStyle(probe).color; probe.remove(); return ok;
  }, token);
  let phase = "readonly authentication";
  try {
    const ticket = new URL(config.ticket_url);
    ticket.searchParams.set("company_id", config.company_id); ticket.searchParams.set("period", config.period);
    const contextReply = reply("context");
    await page.goto(ticket.href); const realContext = await (await contextReply).json(); await idle();
    const fundsReply = reply("funds");
    await page.goto(`${config.origin}/funds?company_id=${encodeURIComponent(config.company_id)}&period=${encodeURIComponent(config.period)}`);
    const realFunds = await (await fundsReply).json();
    assert(validateFunds(realFunds), "real funds failed generated contract"); await idle();
    await nav("资金明细").click(); await rows().first().waitFor();
    assert.equal(detailCount(), 0, "default page requested business details");
    const firstMovement = realFunds.data.collections.movements.items.find(item => item.subject_id);
    assert(firstMovement, "existing month has no expandable first-page movement");
    const realIndex = realFunds.data.collections.movements.items.findIndex(item => item.id === firstMovement.id);
    const realRow = rows().nth(realIndex), realPanel = realRow.locator(".business-detail-panel");
    assert.equal(await realRow.getAttribute("role"), "button");
    assert.equal(await realRow.getAttribute("tabindex"), "0");
    const realReply = reply("business-status"); await realRow.focus(); await realRow.press("Enter");
    const realStatus = await (await realReply).json(); assert(validateStatus(realStatus), "real status failed generated contract");
    await realPanel.waitFor();
    assert((await realRow.locator(".book-movement-amount").textContent()).trim() === amount(firstMovement), "real movement amount changed on expansion");
    // Wait until the automatic continuation has either completed or offered retry before checking cache.
    await realRow.locator(".dashboard-pagination[aria-busy='true']").waitFor({ state: "hidden" });
    await realRow.press("Space"); await realPanel.waitFor({ state: "hidden" });
    const realReadCount = detailCount(); await realRow.press("Enter"); await realPanel.waitFor(); await frames();
    assert.equal(detailCount(), realReadCount, "same-scope real expansion reread details");
    await realRow.press("Space");

    phase = "synthetic fixture setup";
    const nextPeriod = config.period.endsWith("-12") ? `${Number(config.period.slice(0, 4)) + 1}-01` : `${config.period.slice(0, 4)}-${String(Number(config.period.slice(5)) + 1).padStart(2, "0")}`;
    const otherCompany = "synthetic-funds-company";
    let snapshot = "synthetic-funds-v1", failInitial = true, failMore = true, holdMode = null;
    const baseFunds = clone(fixtures.first_frozen_account_funds.response);
    const baseDetail = clone(fixtures.business_status.response);
    const pageInfo = (count, total = count, more = false) => ({ total_count: total, filtered_count: total, returned_count: count, has_more: more, next_cursor: more ? "synthetic-next" : null });
    const profile = (id, name, purpose = null, note = null) => ({ entity_id: id, values: {
      display_name: name, display_number: null, purpose, note, employment_start: null, employment_end: null,
      employment_status: null, active: null, category_label: null, rights_description: null,
    } });
    const accounts = baseFunds.data.collections.accounts.items.map((item, i) => ({ ...item,
      account_id: `synthetic-account-${i}`, name: "演示同名账户", code: `演示账户${i + 1}`,
      statement: { ...item.statement, account_code: `演示账户${i + 1}`, account_name: "演示同名账户" },
    }));
    const seed = baseFunds.data.collections.movements.items[0];
    const movement = (id, overrides = {}) => ({ ...seed, id: `synthetic-${id}`, subject_id: `synthetic-${id}`,
      date: `${config.period}-01`, account_id: accounts[0].account_id, account_code: accounts[0].code,
      account_name: accounts[0].name, account_type: "bank", direction: "outflow", correction: false,
      amount_fen: "600000", signed_amount_fen: "-600000", type: "工资付款", party: "演示员工张某",
      list_summary: "工资付款", display_summary: "演示工资部分支付及后续款项处理，完整摘要保留用途和月份", internal_transfer: false, ...overrides,
    });
    const movements = [
      movement("partial"),
      movement("second-payment", { subject_id: "synthetic-partial", amount_fen: "100000", signed_amount_fen: "-100000", list_summary: "第二次支付" }),
      movement("transfer-out", { subject_id: "synthetic-transfer", type: "账户互转", party: "", internal_transfer: true, list_summary: "账户互转", display_summary: "账户互转" }),
      movement("transfer-in", { subject_id: "synthetic-transfer", account_id: accounts[1].account_id, account_code: accounts[1].code, direction: "inflow", signed_amount_fen: "600000", type: "账户互转", party: "", internal_transfer: true, list_summary: "账户互转", display_summary: "账户互转" }),
      movement("refund", { type: "预付款退回", direction: "inflow", amount_fen: "10000", signed_amount_fen: "10000", list_summary: "预付款退回", display_summary: "演示供应商退回部分预付款" }),
      movement("correction", { correction: true, direction: "inflow", amount_fen: "12345", signed_amount_fen: "-12345", type: "业务更正", list_summary: "工资业务更正", display_summary: "演示原业务更正" }),
      movement("unknown", { amount_fen: "9007199254740993123", signed_amount_fen: "-9007199254740993123", type: "待核对事项", list_summary: "待核对事项", display_summary: "演示未知分类的大额资金记录" }),
    ];
    const obligation = { key: "synthetic-net", name: "net", direction: "payable", category_key: "payroll_payables", source_period: config.period,
      source_amount_fen: "800000", paid_fen: "600000", other_settled_fen: "0", remaining_fen: "200000", settlement_status: "partial" };
    const tax = { ...obligation, key: "synthetic-tax", name: "tax", source_amount_fen: "12345", paid_fen: "0", remaining_fen: "12345", settlement_status: "open" };
    const event = (subject, n) => ({ id: `${subject}-event-${n}`, subject_id: `synthetic-payment-${n}`, source_subject_id: subject,
      posting_period: config.period, direction: n === 4 ? -1 : 1, signed_amount_fen: n === 4 ? "-10000" : "10000",
      relation_state: n === 5 ? "unresolved" : "resolved", kind: "payment", name: n % 2 ? "tax" : "net", mode: ["payment", "offset", "advance", "accepted"][n % 4] });
    function scope(response, url) {
      response.snapshot_version = snapshot; response.read_context.company_id = url.searchParams.get("company_id");
      const period = url.searchParams.get("period") || config.period;
      response.selected_period = { ...response.selected_period, key: period, year: Number(period.slice(0, 4)), month: Number(period.slice(5)), label: `${period.slice(0, 4)} 年 ${Number(period.slice(5))} 月`, short_label: `${Number(period.slice(5))} 月`, start_date: `${period}-01`, end_date: `${period}-28` };
      return response;
    }
    function statusFixture(url, continuation = false) {
      const response = scope(clone(baseDetail), url), subject = url.searchParams.get("subject_id"), data = response.data;
      data.identity.subject_id = subject; data.identity.company_id = response.read_context.company_id; data.identity.kind = "payroll";
      data.period = response.selected_period.key; data.latest_source.period = data.period;
      data.current_business_result = { amount_fen: "800000", amount_label: "业务确认金额", posting_period: data.period };
      data.display_profiles = { business: profile(subject, "演示业务", "演示用途：专门用于移动端长文本自然换行核验", "演示备注：分次支付与本期进度"),
        employees: [profile("synthetic-employee", "演示员工张某")], counterparties: [profile("synthetic-party", "演示供应商")],
        assets: [profile("synthetic-asset", "演示资产")], fund_accounts: accounts.map(item => profile(item.account_id, item.name)) };
      data.settlements = { cutoff_period: data.period, status: "established", checking: false, obligations: [clone(obligation), clone(tax)] };
      data.current_followups.settlements = { ...clone(data.settlements), cutoff_period: nextPeriod,
        obligations: [{ ...obligation, paid_fen: "800000", remaining_fen: "0", settlement_status: "settled" }, clone(tax)] };
      data.settlement_view = "historical";
      if (subject === "synthetic-transfer") {
        data.settlements.obligations = []; data.current_followups.settlements.obligations = [];
        data.display_profiles = { business: profile(subject, "账户互转", "账户互转", "账户互转"), fund_accounts: accounts.map(item => profile(item.account_id, item.name)) };
      }
      if (subject === "synthetic-refund") {
        const refund = { ...obligation, name: "primary", direction: "receivable", category_key: "supplier_advances", source_amount_fen: "50000", paid_fen: "10000", other_settled_fen: "20000", remaining_fen: "20000" };
        data.settlements.obligations = [refund]; data.current_followups.settlements = clone(data.settlements);
      }
      if (subject === "synthetic-unknown") {
        data.settlements.obligations = [{ ...obligation, direction: "unknown", category_key: "unknown", source_amount_fen: null, paid_fen: null, other_settled_fen: null, remaining_fen: null, settlement_status: "checking" }];
        data.settlements.checking = true; data.current_followups.settlements = clone(data.settlements);
      }
      const paged = subject === "synthetic-partial";
      data.collections.settlement_events = { items: paged ? continuation ? [event(subject, 20)] : Array.from({ length: 20 }, (_, n) => event(subject, n)) : [],
        page: paged ? pageInfo(continuation ? 1 : 20, 21, !continuation) : pageInfo(0),
        scope_period: data.period, current_cutoff_period: nextPeriod, cutoff_semantics: "current_published_relations_independent_of_as_of" };
      assert(validateStatus(response), `synthetic status failed generated contract: ${JSON.stringify(validateStatus.errors)}`); return response;
    }
    const syntheticContext = clone(realContext);
    const contextCompany = syntheticContext.companies.find(item => item.company_id === config.company_id);
    assert(contextCompany, "selected company missing from context");
    syntheticContext.companies = [{ ...contextCompany, name: "演示公司", taxpayer_id: null }, { ...contextCompany, company_id: otherCompany, name: "演示第二家公司", taxpayer_id: null }];
    syntheticContext.periods = [config.period, nextPeriod].map(key => ({ ...realContext.periods[0], key, year: Number(key.slice(0, 4)), month: Number(key.slice(5)), label: `${key.slice(0, 4)} 年 ${Number(key.slice(5))} 月`, short_label: `${Number(key.slice(5))} 月` }));
    await page.route("**/api/dashboard/context?*", safeRoute(route => {
      const response = clone(syntheticContext), companyId = new URL(route.request().url()).searchParams.get("company_id") || config.company_id;
      response.current_company = response.companies.find(item => item.company_id === companyId); response.company = response.current_company.name;
      return route.fulfill({ json: response });
    }));
    await page.route("**/api/dashboard/funds?*", safeRoute(route => {
      const url = new URL(route.request().url()), response = scope(clone(baseFunds), url), accountId = url.searchParams.get("movement_account_id") || accounts[0].account_id;
      response.data.selected_movement_account = { type: "bank", account_id: accountId };
      const selected = movements.filter(item => item.account_id === accountId);
      response.data.collections.accounts = { items: clone(accounts), page: pageInfo(accounts.length) };
      response.data.collections.movements = { items: clone(selected), page: pageInfo(selected.length) };
      response.data.movement_count = movements.length;
      const section = url.searchParams.get("section");
      if (section) response.data.collections = { [section]: response.data.collections[section] };
      assert(validateFunds(response), `synthetic funds failed generated contract: ${JSON.stringify(validateFunds.errors)}`); return route.fulfill({ json: response });
    }));
    let initialAttempts = 0, continuationAttempts = 0;
    await page.route("**/api/dashboard/business-status?*", safeRoute(async route => {
      const url = new URL(route.request().url()), continuation = url.searchParams.has("cursor");
      assert.equal(url.searchParams.get("limit"), "20"); assert.equal(url.searchParams.get("settlement_view"), "historical");
      assert.equal(url.searchParams.get("expected_version"), snapshot);
      if (continuation) { continuationAttempts++; assert.equal(url.searchParams.get("cursor"), "synthetic-next"); }
      else initialAttempts++;
      if (!continuation && failInitial) { failInitial = false; return route.fulfill({ status: 503, json: { message: "合成首次读取失败" } }); }
      if (continuation && failMore) { failMore = false; return route.fulfill({ status: 503, json: { message: "合成续页读取失败" } }); }
      const response = statusFixture(url, continuation);
      if (holdMode === (continuation ? "continuation" : "initial")) {
        holdMode = null; await new Promise(resolve => { held.push({ release: resolve }); notifyHeld?.(); });
      }
      await route.fulfill({ json: response }).catch(() => {});
    }));

    phase = "synthetic initial retry and automatic continuation retry";
    await page.goto(`${config.origin}/funds?company_id=${encodeURIComponent(config.company_id)}&period=${encodeURIComponent(config.period)}&movement_account_type=bank&movement_account_id=${accounts[0].account_id}`);
    await idle(); await nav("资金明细").click(); await frames();
    const first = rows().first(), panel = first.locator(".business-detail-panel");
    const before = detailCount(); await first.focus(); await first.press("Enter");
    await first.getByRole("button", { name: "重新读取", exact: true }).click();
    await panel.getByRole("button", { name: "重试读取", exact: true }).waitFor();
    assert.equal(detailCount(), before + 3); assert.equal(await first.getAttribute("aria-expanded"), "true");
    assert.equal(await panel.locator("ul > li").count(), 20);
    assert.equal(await first.locator(".business-detail-trigger:visible").count(), 0);
    assert.equal(await panel.locator(".owner-activity-obligation").count(), 2);
    assert.equal((await panel.locator(".owner-item-balance strong").first().textContent()).trim(), "¥2,000.00");
    assert.equal(await panel.locator(".owner-item-latest").count(), 1, "unchanged tax duplicated in followups");
    assert((await panel.textContent()).includes("相关款项处理"));
    assert((await panel.textContent()).includes("员工：") === false, "row party repeated as employee");
    assert((await panel.textContent()).includes("往来方：演示供应商"));
    assert((await panel.textContent()).includes("相关资产：演示资产"));
    assert((await panel.textContent()).includes("相关账户：演示同名账户"));
    const layouts = [];
    for (const theme of ["light", "dark"]) {
      await page.evaluate(theme => document.documentElement.dataset.theme = theme, theme);
      for (const width of [1440, 768, 375, 320]) {
        await page.setViewportSize({ width, height: 1000 }); await frames();
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `document overflow ${theme}/${width}`);
        assert(await panel.evaluate(element => element.scrollWidth <= element.clientWidth + 1), `panel overflow ${theme}/${width}`);
        assert(await first.evaluate(element => {
          const detail = element.querySelector(".business-detail-panel"), copy = element.querySelector(".book-movement-copy");
          const row = element.getBoundingClientRect(), box = detail.getBoundingClientRect(), style = getComputedStyle(element);
          return box.width >= row.width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight) - 3 && box.top >= copy.getBoundingClientRect().bottom;
        }), `detail did not span row ${theme}/${width}`);
        assert(await color(panel.locator(".business-state.pending").first(), "--warning"));
        assert(await color(panel.locator(".business-state.settled").first(), "--accent"));
        if (config.screenshot_directory) {
          fs.mkdirSync(config.screenshot_directory, { recursive: true });
          await first.screenshot({ path: path.join(config.screenshot_directory, `funds-${theme}-${width}.png`), style: "body * { visibility: hidden !important; } .book-activity-row, .book-activity-row * { visibility: visible !important; }" });
        }
        layouts.push({ theme, width });
      }
    }
    await panel.getByRole("button", { name: "重试读取", exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll(".book-activity-row.expanded .business-detail-panel ul > li").length === 21);
    assert.equal(await panel.locator(".dashboard-pagination").count(), 0);
    assert.equal(await panel.getByRole("button", { name: "加载更多", exact: true }).count(), 0);
    assert.equal(await panel.locator("ul > li").last().evaluate(element => getComputedStyle(element).borderBottomWidth), "0px");
    assert(!(await panel.textContent()).includes("本次筛选已全部加载"));
    const cachedCount = detailCount(); await first.focus(); await first.press("Space"); await first.press("Enter"); await panel.waitFor(); await frames();
    assert.equal(detailCount(), cachedCount);
    phase = "same snapshot refresh cache"; await refresh(); await panel.waitFor(); assert.equal(detailCount(), cachedCount);

    phase = "multiple payments and transfer sides";
    async function open(index) { const row = rows().nth(index); await row.focus(); await row.press("Enter"); await row.locator(".business-detail-panel").waitFor(); return row; }
    const second = await open(1); assert.equal(await first.getAttribute("aria-expanded"), "false");
    assert.equal(await rows().locator(".business-detail-panel:visible").count(), 1);
    assert.equal((await second.locator(".book-movement-amount").textContent()).trim(), "−¥1,000.00");
    assert.equal((await second.locator(".owner-item-balance strong").first().textContent()).trim(), "¥2,000.00");
    for (const side of ["out", "in"]) {
      if (side === "in") {
        await page.locator(".fund-account-index > button").nth(1).click();
        await page.waitForFunction(() => document.querySelectorAll(".book-activity-row").length === 1);
      }
      const row = await open(side === "out" ? 2 : 0), text = await row.locator(".business-detail-panel").textContent();
      assert(text.includes("相关账户：演示同名账户"), "other account with identical name was excluded");
      assert.equal(text.split("演示同名账户").length - 1, 1, "current account was repeated");
      assert(!text.includes("事项说明："), "identical full summary repeated");
      assert(!text.includes("用途／备注："), "duplicate purpose and note repeated");
      assert.equal((await row.locator(".direction").textContent()).trim(), side === "out" ? "转出" : "转入");
    }
    await page.locator(".fund-account-index > button").first().click();
    await page.waitForFunction(() => document.querySelectorAll(".book-activity-row").length === 6);
    phase = "refund correction and unknown exact large amount";
    const refund = await open(3); assert((await refund.locator(".business-detail-panel").textContent()).includes("已退回"));
    assert((await refund.locator(".business-detail-panel").textContent()).includes("已冲抵"));
    const correction = await open(4); assert.equal((await correction.locator(".book-movement-amount").textContent()).trim(), "−¥123.45");
    assert.equal((await correction.locator(".direction").textContent()).trim(), "更正原业务");
    for (const theme of ["light", "dark"]) {
      await page.evaluate(theme => document.documentElement.dataset.theme = theme, theme);
      assert(await color(correction.locator(".direction"), "--muted")); assert(await color(correction.locator(".book-movement-amount"), "--muted"));
    }
    const unknown = await open(5); assert.equal((await unknown.locator(".book-movement-amount").textContent()).trim(), amount(movements[6]));
    assert((await unknown.locator(".business-detail-panel").textContent()).includes("待核对"));
    assert((await unknown.locator(".business-detail-panel").textContent()).includes("AI 会计核对中"));
    for (const width of [1440, 768, 375, 320]) {
      await page.setViewportSize({ width, height: 1000 }); await frames();
      assert(await unknown.evaluate(element => element.scrollWidth <= element.clientWidth + 1), `large amount overflow ${width}`);
    }

    phase = "book bank account month company collapse";
    await page.getByRole("tab", { name: "按流水", exact: true }).click(); await frames();
    assert.equal(await unknown.getAttribute("aria-expanded"), "false");
    await page.getByRole("tab", { name: "按业务", exact: true }).click(); await open(2);
    await page.locator(".fund-account-index > button").nth(1).click();
    await page.waitForFunction(() => document.querySelectorAll(".book-activity-row").length === 1); await frames();
    assert.equal(await rows().first().getAttribute("aria-expanded"), "false");
    await open(0); await page.getByRole("combobox", { name: "资金查看月份", exact: true }).selectOption(nextPeriod); await idle();
    assert.equal(await rows().locator(".business-detail-panel:visible").count(), 0);
    await nav("资金明细").click(); await open(0);
    await page.getByRole("combobox", { name: "切换公司", exact: true }).selectOption(otherCompany); await idle();
    assert.equal(await rows().locator(".business-detail-panel:visible").count(), 0);
    await page.getByRole("combobox", { name: "切换公司", exact: true }).selectOption(config.company_id); await idle();
    await page.getByRole("combobox", { name: "资金查看月份", exact: true }).selectOption(config.period); await idle();
    await page.locator(".fund-account-index > button").first().click(); await frames();
    await nav("资金明细").click();

    // A changed snapshot clears each component cache without changing movement identities.
    phase = "module switch permits expanded continuation";
    snapshot = "synthetic-funds-v2"; await refresh(); holdMode = "continuation";
    await open(0); await page.waitForFunction(() => document.querySelector(".book-activity-row.expanded .dashboard-pagination")?.getAttribute("aria-busy") === "true");
    const moduleContinuation = await takeHeld();
    await nav("概览").click(); moduleContinuation.release();
    await page.waitForFunction(() => document.querySelectorAll(".book-activity-row.expanded .business-detail-panel ul > li").length === 21);
    const moduleReads = detailCount(); await nav("资金明细").click(); await frames(); assert.equal(detailCount(), moduleReads);

    phase = "collapse cancels continuation and ignores late response";
    snapshot = "synthetic-funds-v3"; await refresh(); holdMode = "continuation";
    await open(0); await page.waitForFunction(() => document.querySelector(".book-activity-row.expanded .dashboard-pagination")?.getAttribute("aria-busy") === "true");
    const cancelled = await takeHeld();
    await rows().first().focus(); await rows().first().press("Space"); cancelled.release(); await frames();
    assert.equal(await rows().first().getAttribute("aria-expanded"), "false");
    assert.equal(await rows().first().locator(".business-detail-panel ul > li").count(), 20);
    const reopened = await open(0);
    await page.waitForFunction(() => document.querySelectorAll(".book-activity-row.expanded .business-detail-panel ul > li").length === 21);
    assert.equal(await reopened.getAttribute("aria-expanded"), "true");

    phase = "new snapshot rejects delayed initial response";
    snapshot = "synthetic-funds-v4"; await refresh(); holdMode = "initial";
    await rows().first().focus(); await rows().first().press("Enter");
    const late = await takeHeld(); snapshot = "synthetic-funds-v5"; await refresh();
    const afterCancel = detailCount(); late.release(); await frames();
    assert.equal(await rows().first().getAttribute("aria-expanded"), "false");
    assert.equal(await rows().first().locator(".business-detail-panel").count(), 0); assert.equal(detailCount(), afterCancel);
    assert.equal(fixtureErrors.length, 0, "synthetic route failed"); assert.equal(errors.length, 0);
    return { status: "passed", readonly_real_expansion: true, default_detail_requests: 0, cached_expand_requests: 0,
      synthetic_scenarios: movements.length, synthetic_layouts: layouts, initial_attempts: initialAttempts, continuation_attempts: continuationAttempts,
      initial_retry: true, continuation_retry: true, related_records: 21, one_expanded_row: true, exact_large_amount: true,
      scope_collapse: true, same_snapshot_refresh_rereads: 0, module_switch_continuation: true, collapse_cancellation: true, late_response_rejected: true, browser_errors: 0 };
  } catch (error) {
    // Assertion text never includes real response fields, identifiers, amounts or ticket URLs.
    if (phase.startsWith("readonly")) throw new Error(`${phase}: read-only browser assertion or interaction failed`);
    if (fixtureErrors.length) throw new Error(`${phase}: ${fixtureErrors.join("; ")}`);
    throw new Error(`${phase}: ${String(error.message).replace(/https?:\/\/[^\s"']+/g, "[URL]").split(config.company_id).join("[company]")}`);
  } finally { for (const pending of held) pending.release(); await browser.close(); }
}
if (require.main === module) {
  let input = ""; process.stdin.setEncoding("utf8"); process.stdin.on("data", chunk => input += chunk);
  process.stdin.on("end", async () => {
    try { process.stdout.write(JSON.stringify(await run(JSON.parse(input))) + "\n"); }
    catch (error) { process.stdout.write(JSON.stringify({ status: "failed", message: error.message }) + "\n"); process.exitCode = 1; }
  });
}
module.exports = { run };
