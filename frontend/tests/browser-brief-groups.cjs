// Read-only checks on an isolated service, plus browser-only validated synthetic responses.
// JSON stdin: origin, ticket_url, playwright_module, channel, company_id, period,
// optional synthetic_only (intercepts every API; requires no backend or ticket).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

async function run(config) {
  const { chromium } = require(config.playwright_module);
  const { validateDashboardBriefResponse: validateBrief } = await import(pathToFileURL(path.join(__dirname, "../src/api/generated/dashboardBrief.js")));
  const { validateDashboardBriefGroupResponse: validateGroup } = await import(pathToFileURL(path.join(__dirname, "../src/api/generated/dashboardBriefGroup.js")));
  const { validateDashboardBusinessStatusResponse: validateStatus } = await import(pathToFileURL(path.join(__dirname, "../src/api/generated/dashboardBusinessStatus.js")));
  const fixtures = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures/dashboard-contracts.json"), "utf8"));
  const company = config.company_id ?? config.companies?.[0]?.id;
  const period = config.period ?? config.companies?.[0]?.period;
  const browser = await chromium.launch({ channel: config.channel ?? "msedge", headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
  const requests = [], errors = [], routeErrors = [], checks = [], growth = [], screenshots = [];
  let phase = "real", base, synthetic = null, memberTotal = 24, groupTotal = 1, categorySplit = null, openCategoryMode = false, batchMode = false;
  let holdFirst = false, holdContinuation = false, holdRootContinuation = false;
  const held = [], heldRoots = [];
  page.on("request", request => { const url = new URL(request.url()); if (url.pathname.startsWith("/api/dashboard/")) requests.push(url); });
  page.on("pageerror", error => errors.push(error.message));
  const count = action => requests.filter(url => url.pathname === `/api/dashboard/${action}`).length;
  const frames = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const reply = action => page.waitForResponse(response => new URL(response.url()).pathname === `/api/dashboard/${action}`);
  const refresh = async () => { const pending = reply("brief"); await page.getByRole("button", { name: "刷新数据", exact: true }).click(); await pending; await page.waitForFunction(() => document.querySelector(".module-header")?.getAttribute("aria-busy") === "false"); await frames(); };
  const finishMembers = async row => { await row.locator(".member-row, .contribution-row").first().waitFor(); await row.locator(".group-members .dashboard-pagination").waitFor({ state: "hidden", timeout: 60000 }); await frames(); };
  const money = value => { if (value == null) return "待核对"; const number = BigInt(value), absolute = number < 0n ? -number : number; return `${number < 0n ? "−" : ""}¥${new Intl.NumberFormat("zh-CN").format(absolute / 100n)}.${String(absolute % 100n).padStart(2, "0")}`; };
  const fulfill = async (route, payload, validator) => {
    try {
      if (!validator(payload)) {
        routeErrors.push({ phase, url: route.request().url(), message: "synthetic response failed generated validation", validation_errors: structuredClone(validator.errors) });
        await route.abort("failed").catch(() => {});
        return;
      }
      await route.fulfill({ json: payload }).catch(() => {});
    } catch (error) {
      routeErrors.push({ phase, url: route.request().url(), message: error.message });
      await route.abort("failed").catch(() => {});
    }
  };
  const waitHeld = async () => { for (let attempt = 0; !held.length && attempt < 100; attempt++) await page.waitForTimeout(20); assert(held.length, "expected a held member request"); };
  const screenshot = async name => {
    if (!config.screenshot_directory) return;
    fs.mkdirSync(config.screenshot_directory, { recursive: true });
    const target = path.join(config.screenshot_directory, `${name}.png`);
    await page.screenshot({ path: target, fullPage: true }); screenshots.push(target);
  };
  try {
    if (config.synthetic_only) {
      // All API traffic is intercepted before navigation; this mode needs only
      // an isolated Vite server and never connects to an accounting service.
      await page.route(url => url.pathname.startsWith("/api/"), async route => {
        const url = new URL(route.request().url()), pathname = url.pathname;
        if (pathname === "/api/browser-ticket") return route.fulfill({ json: { url: `${config.origin ?? new URL(config.ticket_url).origin}/#ticket=synthetic-groups-ticket` } });
        if (pathname === "/api/browser-session") return route.fulfill({ json: { status: "ok" } });
        if (pathname === "/api/security-request") return route.fulfill({ json: { schema_version: 1, catalog_instance_id: "groups-synthetic-catalog", provisioned: true, login_name: "演示负责人", active: true, authenticated: true } });
        if (pathname === "/api/dashboard/context") {
          const payload = structuredClone(fixtures.company_with_period.response);
          payload.companies = [{ ...payload.companies[0], company_id: company, name: "演示公司", taxpayer_id: null }];
          payload.current_company = payload.companies[0]; payload.company = "演示公司";
          payload.periods = payload.periods.filter(item => item.key === period); payload.default_period = period;
          return route.fulfill({ json: payload });
        }
        if (pathname === "/api/dashboard/brief") {
          const payload = structuredClone(fixtures.brief.response);
          payload.read_context.company_id = company;
          for (const key of ["activity", "open_items"]) payload.data.collections[key] = { items: [], page: { total_count: 0, filtered_count: 0, returned_count: 0, has_more: false, next_cursor: null } };
          payload.data.activity_groups = []; payload.data.activity_count = 0; payload.data.group_count = 0;
          delete payload.data.collections.vouchers;
          return fulfill(route, payload, validateBrief);
        }
        routeErrors.push({ phase, message: `unhandled isolated API: ${pathname}` });
        return route.abort().catch(() => {});
      });
    }
    const address = new URL(config.ticket_url); address.searchParams.set("company_id", company); address.searchParams.set("period", period);
    const first = reply("brief"); await page.goto(address.href); base = await (await first).json(); assert(validateBrief(base), JSON.stringify(validateBrief.errors));
    await page.locator(".activity-section").waitFor(); await frames();
    assert.equal(count("brief-group"), 0, "default overview does not read group members");
    assert.equal(count("business-status"), 0, "default overview does not read business progress");
    assert.equal(base.data.collections.vouchers, undefined, "default brief does not hydrate voucher pages");
    const realGroup = base.data.collections.activity.items.find(item => !item.is_batch) ?? base.data.collections.activity.items[0];
    if (realGroup) {
      const category = base.data.activity_groups.findIndex(item => item.key === realGroup.group);
      await page.locator(".activity-section .index button").nth(category).click();
      const rowIndex = base.data.collections.activity.items.filter(item => item.group === realGroup.group).findIndex(item => item.group_key === realGroup.group_key);
      const row = page.locator(".activity-section .event-row").nth(rowIndex);
      assert.equal((await row.locator(".event-money b").textContent()).trim(), money(realGroup.amount_fen));
      const detail = reply("brief-group"); await row.focus(); await row.press("Enter");
      const memberReply = await (await detail).json(); assert(validateGroup(memberReply), JSON.stringify(validateGroup.errors));
      await finishMembers(row); const activityProgress = count("business-status"); assert.equal(activityProgress, 0, "expanding any business group never preloads progress");
      await screenshot("brief-groups-desktop-expanded");
      const voucherMember = memberReply.data.collections.members.items.find(item => item.voucher_version_id);
      const buttons = row.locator(".event-voucher-button:not(:disabled)");
      if (voucherMember && await buttons.count()) {
        const before = requests.length; await buttons.first().hover(); await row.locator('[role="tooltip"]').waitFor(); await frames();
        assert.equal(requests.length, before, "loaded voucher hover sends no requests");
        assert((await row.locator('[role="tooltip"]').innerText()).includes(`凭证 ${voucherMember.voucher_number}`));
        await buttons.first().click(); await page.locator("#selected-voucher").waitFor();
        assert((await page.locator("#selected-voucher .voucher-reference").innerText()).includes(`凭证 ${voucherMember.voucher_number}`));
        assert.equal(count("business-status"), activityProgress); checks.push("real keyboard expansion, local hover, precise voucher selection");
        await page.getByRole("button", { name: "按业务", exact: true }).click();
      } else checks.push("real keyboard expansion; this group has no voucher member");
    } else checks.push(config.synthetic_only ? "all APIs intercepted; real service checks skipped" : "real activity empty; interactive coverage uses validated synthetic groups");
    const realProgress = count("business-status");
    const openCandidates = [base.data.collections.open_items.items[0], base.data.collections.open_items.items.find(item => item.description.includes("社保"))].filter((item, index, all) => item && all.findIndex(other => other?.group_key === item.group_key) === index);
    for (const realOpen of openCandidates) {
      const category = base.data.open_items.categories.filter(item => item.count).findIndex(item => item.key === realOpen.category_key);
      await page.locator(".open-items .open-index button").nth(category).click();
      const rowIndex = base.data.collections.open_items.items.filter(item => item.category_key === realOpen.category_key).findIndex(item => item.group_key === realOpen.group_key);
      const row = page.locator(".open-items .open-event-row").nth(rowIndex);
      assert.equal((await row.locator(".open-event-money b").textContent()).trim(), money(realOpen.outstanding_fen));
      const detail = reply("brief-group"); await row.focus(); await row.press("Enter");
      const payload = await (await detail).json(); assert(validateGroup(payload), JSON.stringify(validateGroup.errors));
      await finishMembers(row); assert.equal(count("business-status"), realProgress);
      const purpose = payload.data.collections.members.items.find(item => item.purpose)?.purpose;
      if (purpose) assert((await row.locator(".group-members").innerText()).includes(purpose));
      if (realOpen.description.includes("社保")) {
        await screenshot("brief-pending-social-desktop-expanded");
        assert((await row.locator(".group-members").innerText()).includes(" · 社保"));
        checks.push("real pending social detail keeps monthly matter-specific two-component breakdowns");
      }
      await row.press("Space"); await row.locator(".group-members").waitFor({ state: "hidden" });
      checks.push("real pending group keyboard expansion preserves period-end balance and member purpose");
    }

    // Choosing a voucher and returning to business view preserves the group's
    // expansion. End real interactions explicitly before measuring collapsed
    // synthetic roots, so their first refresh has no prior expanded group.
    const realExpanded = page.locator('.activity-section .event-row[aria-expanded="true"]');
    while (await realExpanded.count()) {
      const expanded = realExpanded.first();
      await expanded.focus(); await expanded.press("Space");
      await expanded.locator(".group-members").waitFor({ state: "hidden" });
    }
    await frames();
    assert.equal(await page.locator(".activity-section .group-members").count(), 0);

    phase = "synthetic";
    const memberSeed = fixtures.brief_activity_members_0.response;
    const activitySeed = memberSeed.data.collections.members.items[0];
    const voucherSeed = memberSeed.data.collections.vouchers.items[0];
    assert(activitySeed && voucherSeed);
    function group(index) {
      return { ...base.data.collections.activity.items[0] ?? fixtures.brief.response.data.collections.activity.items[0], key: `synthetic-group-${index}`, group_key: `synthetic-group-${index}`, group: !openCategoryMode && categorySplit != null && index >= categorySplit ? "expenses" : "funds", kind: "collection", party: `同一对象 ${index}`, title: "代收代付", is_batch: batchMode, member_count: memberTotal, voucher_count: memberTotal, amount_fen: String(BigInt(memberTotal) * 100n), amount_label: "代收金额", state: "已入账", date_from: null, date_to: null, has_month_recognition: true };
    }
    function rootPage(offset = 0) {
      const result = structuredClone(synthetic), items = Array.from({ length: Math.min(20, groupTotal - offset) }, (_, n) => group(offset + n));
      result.data.collections.activity = { items, page: { total_count: groupTotal, filtered_count: groupTotal, returned_count: items.length, has_more: offset + items.length < groupTotal, next_cursor: offset + items.length < groupTotal ? `groups:${offset + items.length}` : null } };
      if (openCategoryMode) {
        const seed = Object.values(fixtures).flatMap(entry => entry.response?.data?.collections?.open_items?.items ?? [])[0];
        assert(seed, "actual open group seed is required");
        result.data.collections.open_items = { items: items.map((item, n) => ({ ...seed, id: item.group_key, group_key: item.group_key, category_key: offset + n < categorySplit ? "payroll_payables" : "tax_payables", party: item.party, description: "分类续读事项", member_count: 1, source_amount_fen: "100", paid_fen: "0", other_settled_fen: "0", outstanding_fen: "100", current_outstanding_fen: "100", status: "open", current_status: "open" })), page: { ...result.data.collections.activity.page } };
      }
      return result;
    }
    function members(key, offset) {
      const result = structuredClone(memberSeed);
      result.read_context = structuredClone(base.read_context); result.selected_period = structuredClone(base.selected_period); result.snapshot_version = synthetic.snapshot_version;
      result.data.group_key = key;
      const length = Math.min(20, memberTotal - offset);
      result.data.collections.members.items = Array.from({ length }, (_, n) => {
        const index = offset + n;
        return { ...activitySeed, key: `member-${key}-${index}`, group_key: key, subject_id: `subject-${key}-${index}`, voucher_version_id: `voucher-${key}-${index}`, voucher_number: index + 1, detail_scope_key: null, date: null, recognition: { ...activitySeed.recognition, period, date: null, precision: "month", label: `${period} · 按月确认` }, party: "同一对象", title: "代收代付", description: `每笔不同的真实用途 ${index + 1}`, state: "已入账", amount_fen: "100", amount_label: "代收金额", group: "funds" };
      });
      result.data.collections.members.page = { total_count: memberTotal, filtered_count: memberTotal, returned_count: length, has_more: offset + length < memberTotal, next_cursor: offset + length < memberTotal ? `members:${offset + length}` : null };
      result.data.collections.vouchers.items = result.data.collections.members.items.map((item, index) => ({ ...voucherSeed, voucher_version_id: item.voucher_version_id, subject_id: item.subject_id, number: String(offset + index + 1), date: null, recognition: item.recognition, business_amount_fen: "100", business_amount_label: "代收金额", amount_fen: "100", list_summary: item.description, summary: item.description, has_business_progress: false,
        lines: voucherSeed.lines.slice(0, 2).map((line, lineIndex) => ({ ...line, line_number: lineIndex + 1, debit_fen: lineIndex ? "0" : "100", credit_fen: lineIndex ? "100" : "0", party: "", parties: [], party_state: "not_applicable" })) }));
      result.data.collections.vouchers.page = { total_count: length, filtered_count: length, returned_count: length, has_more: false, next_cursor: null };
      return result;
    }
    function prepare(groups, records) {
      categorySplit = null; openCategoryMode = false;
      groupTotal = groups; memberTotal = records; synthetic = structuredClone(base);
      synthetic.snapshot_version = `synthetic-groups-${groups}-members-${records}`;
      synthetic.data.group_count = groups; synthetic.data.activity_count = groups * records; synthetic.data.voucher_count = groups * records;
      synthetic.data.activity_groups = [{ key: "funds", label: "收付款", event_count: groups * records, group_count: groups, type_counts: [{ label: "代收代付", count: groups * records }] }];
      synthetic.data.focused_activity = null; synthetic.data.focused_activity_group = null; synthetic.data.focused_voucher = null;
      synthetic.data.collections.activity = rootPage().data.collections.activity;
      delete synthetic.data.collections.vouchers;
      assert(validateBrief(synthetic), JSON.stringify(validateBrief.errors));
    }
    await page.route("**/api/dashboard/brief?*", async route => {
      if (!synthetic) return route.continue();
      const url = new URL(route.request().url());
      if (url.searchParams.get("section") === "open_items" && !openCategoryMode) return fulfill(route, synthetic, validateBrief);
      const cursor = url.searchParams.get("cursor"), offset = cursor?.startsWith("groups:") ? Number(cursor.slice(7)) : 0;
      if (offset && holdRootContinuation) { const payload = rootPage(offset); heldRoots.push(() => fulfill(route, payload, validateBrief)); return; }
      return fulfill(route, rootPage(offset), validateBrief);
    });
    await page.route("**/api/dashboard/brief-group?*", async route => {
      if (!synthetic) return route.continue();
      const url = new URL(route.request().url()), cursor = url.searchParams.get("cursor"), offset = cursor ? Number(cursor.slice(8)) : 0;
      const payload = members(url.searchParams.get("group_key"), offset);
      if ((!offset && holdFirst) || (offset && holdContinuation)) { held.push(() => fulfill(route, payload, validateGroup)); return; }
      return fulfill(route, payload, validateGroup);
    });

    await page.route("**/api/dashboard/business-status?*", route => {
      const url = new URL(route.request().url()), payload = structuredClone(fixtures.business_status.response);
      payload.read_context = structuredClone(base.read_context); payload.selected_period = structuredClone(base.selected_period); payload.snapshot_version = synthetic.snapshot_version;
      payload.data.identity.company_id = company; payload.data.identity.subject_id = url.searchParams.get("subject_id");
      payload.data.detail_scope = null; payload.data.settlement_view = "current";
      for (const collection of Object.values(payload.data.collections)) collection.page = { total_count: collection.items.length, filtered_count: collection.items.length, returned_count: collection.items.length, has_more: false, next_cursor: null };
      return fulfill(route, payload, validateStatus);
    });
    const assertClosedProgress = async row => {
      await frames();
      assert(await row.getByRole("button", { name: "业务进展", exact: true }).evaluateAll(buttons => buttons.length > 0 && buttons.every(button => button.getAttribute("aria-expanded") === "false")), "all member progress buttons start collapsed");
      assert.equal(await row.locator("details[open], .business-detail-panel").count(), 0, "no member progress details are open");
    };
    for (const [batch, records] of [[false, 24], [true, 1], [true, 24]]) {
      batchMode = batch; prepare(1, records); synthetic.snapshot_version += `-batch-${batch}`;
      const before = count("business-status"); await refresh();
      const row = page.locator(".activity-section .event-row").first();
      await row.focus(); await row.press("Enter"); await finishMembers(row); await assertClosedProgress(row);
      assert.equal(count("business-status"), before, "first group expansion never reads progress");
      const selectedIndex = records > 1 ? 1 : 0;
      const buttons = row.getByRole("button", { name: "业务进展", exact: true });
      await buttons.nth(selectedIndex).click(); await row.locator(".business-detail-panel").waitFor(); await frames();
      assert.equal(count("business-status"), before + 1, "manual progress click reads only the chosen member");
      assert.deepEqual(await buttons.evaluateAll(buttons => buttons.map(button => button.getAttribute("aria-expanded"))), Array.from({ length: records }, (_, index) => index === selectedIndex ? "true" : "false"));
      assert.equal(await row.locator("details[open]").count(), 1);
      await row.focus(); await row.press("Space"); await row.locator(".group-members").waitFor({ state: "hidden" });
      const memberRequests = count("brief-group"); await row.press("Enter"); await finishMembers(row); await assertClosedProgress(row);
      assert.equal(count("brief-group"), memberRequests, "reopening reuses loaded members");
      assert.equal(count("business-status"), before + 1, "cached reopening does not read progress");
      await refresh(); assert.equal(await page.locator(".group-members").count(), 0); assert.equal(count("business-status"), before + 1, "refresh does not reopen progress");
      await row.focus(); await row.press("Enter"); await finishMembers(row); await assertClosedProgress(row);
      assert.equal(count("business-status"), before + 1, "expanding after refresh keeps progress collapsed");
      await row.press("Space");
      checks.push(`${batch ? "batch" : "ordinary"} ${records} members: first expansion, cached reopening and refresh keep every progress detail collapsed; manual click opens one member`);
    }
    batchMode = false;

    for (const records of [24, 2400]) {
      prepare(1, records);
      const progressBefore = count("business-status"), groupBefore = count("brief-group"), start = Date.now();
      await refresh(); const rootMs = Date.now() - start;
      const rows = page.locator(".activity-section .event-row"); assert.equal(await rows.count(), 1);
      assert.equal((await rows.first().locator(".event-money b").textContent()).trim(), money(String(BigInt(records) * 100n)));
      assert.equal(count("brief-group"), groupBefore, "refreshing a collapsed group does not read its members");
      const expandedStart = Date.now(); await rows.first().focus(); await rows.first().press("Enter"); await finishMembers(rows.first());
      assert.equal(await rows.first().locator(".member-row").count(), records);
      assert.equal(count("business-status"), progressBefore, "listing all members never reads per-member progress");
      growth.push({ groups: 1, members: records, root_rows: 1, member_rows: records, root_refresh_ms: rootMs, member_full_render_ms: Date.now() - expandedStart, member_page_requests: count("brief-group") - groupBefore });
      await rows.first().press("Space"); await rows.first().locator(".group-members").waitFor({ state: "hidden" });
    }

    // A complete selected category must not read the unrelated twenty-first root
    // group. Selecting the missing category resumes the existing global cursor.
    for (const section of ["activity", "open_items"]) {
      prepare(21, 1); categorySplit = 20; openCategoryMode = section === "open_items";
      synthetic.snapshot_version += `-category-${section}`;
      if (openCategoryMode) {
        const category = (key, label, count) => ({ key, label, direction: "payable", unit: "笔", count, group_count: count, loaded_count: key === "payroll_payables" ? 20 : 0, outstanding_fen: String(count * 100) });
        synthetic.data.open_items = { ...synthetic.data.open_items, group_count: 21, total_count: 21, receivable_count: 0, receivable_fen: "0", payable_count: 21, payable_fen: "2100", complete: true, categories: [category("payroll_payables", "待付工资", 20), category("tax_payables", "待缴税费", 1)] };
      } else synthetic.data.activity_groups = [{ key: "funds", label: "甲分类", event_count: 20, group_count: 20, type_counts: [{ label: "事项", count: 20 }] }, { key: "expenses", label: "乙分类", event_count: 1, group_count: 1, type_counts: [{ label: "事项", count: 1 }] }];
      const selector = openCategoryMode ? ".open-items .open-index button" : ".activity-section .index button";
      const rowSelector = openCategoryMode ? ".open-items .open-event-row" : ".activity-section .event-row";
      const continuations = () => requests.filter(url => url.pathname === "/api/dashboard/brief" && url.searchParams.get("cursor") && url.searchParams.get("section") === section).length;
      const before = continuations(); await refresh(); await page.locator(selector).first().click(); await frames();
      assert.equal(await page.locator(rowSelector).count(), 20); assert.equal(continuations(), before, `${section}: complete category must not prefetch another category`);
      holdRootContinuation = true; await page.locator(selector).nth(1).click();
      for (let attempt = 0; !heldRoots.length && attempt < 100; attempt++) await page.waitForTimeout(20);
      assert.equal(heldRoots.length, 1); assert.equal(continuations(), before + 1);
      await page.locator(selector).first().click(); await frames(); await heldRoots.shift()(); await frames();
      assert.equal(await page.locator(rowSelector).count(), 20, "late canceled continuation cannot replace the selected category");
      await page.locator(selector).nth(1).click();
      for (let attempt = 0; !heldRoots.length && attempt < 100; attempt++) await page.waitForTimeout(20);
      assert.equal(heldRoots.length, 1, "switching back must retry the unconsumed cursor");
      synthetic.snapshot_version += "-refresh"; await page.locator(selector).first().click(); await refresh();
      await heldRoots.shift()(); await frames();
      holdRootContinuation = false; await page.locator(selector).nth(1).click();
      await page.waitForFunction(selector => document.querySelectorAll(selector).length === 1, rowSelector); await frames();
      assert((await page.locator(rowSelector).innerText()).includes("同一对象 20"));
      assert.equal(continuations(), before + 3, "refresh rejects the previous cursor result and the current snapshot resumes once");
      checks.push(`${section}: complete selected category skips unrelated root continuation; missing category resumes; rapid switch and refresh reject late pages`);
    }

    await page.locator(".section-nav button").first().click();
    prepare(24, 40); holdRootContinuation = true; await refresh();
    await page.locator(".section-nav").getByRole("button", { name: "本月发生", exact: true }).click();
    const rows = page.locator(".activity-section .event-row");
    assert.equal(await rows.count(), 20, "default root collection is bounded at twenty groups");
    holdContinuation = true; const row = rows.first(); await row.focus(); await row.press("Enter");
    await row.locator(".member-row").first().waitFor(); await page.waitForFunction(() => document.querySelectorAll(".group-members .member-row").length === 20);
    await page.evaluate(() => { window.__firstBriefMember = document.querySelector(".group-members li"); });
    assert.equal(await row.locator(".member-row").count(), 20); const beforeContinuationProgress = count("business-status"); await assertClosedProgress(row);
    await waitHeld(); holdContinuation = false; await held.shift()(); await finishMembers(row);
    assert.equal(await row.locator(".member-row").count(), 40);
    await assertClosedProgress(row); assert.equal(count("business-status"), beforeContinuationProgress, "member continuation never selects another progress detail");
    assert(await page.evaluate(() => window.__firstBriefMember === document.querySelector(".group-members li")), "continuation preserves earlier member DOM nodes");
    checks.push("more than twenty groups stay bounded; group continuation appends forty members incrementally");
    await row.press("Space");
    holdRootContinuation = false; for (const release of heldRoots.splice(0)) await release();

    // A fresh snapshot removes cached group members; collapse its held first read.
    synthetic.snapshot_version = `${synthetic.snapshot_version}-cancel`;
    holdFirst = true; await refresh(); await row.focus(); await row.press("Enter");
    await waitHeld(); await row.press("Space");
    holdFirst = false; await held.shift()(); await frames(); assert.equal(await row.locator(".member-row").count(), 0);
    await row.press("Enter"); await finishMembers(row); assert.equal(await row.locator(".member-row").count(), 40);
    checks.push("same-scope collapse rejects the late first response and reopening reads again");

    await page.setViewportSize({ width: 375, height: 900 }); await frames();
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "375px member layout overflows");
    await screenshot("brief-groups-mobile-375-expanded");
    const beforeMobile = requests.length;
    await row.locator(".event-voucher-button").first().click(); await page.locator("#selected-voucher").waitFor(); await frames();
    assert.equal(requests.length, beforeMobile, "mobile loaded voucher click is local");
    assert((await page.locator("#selected-voucher .voucher-reference").innerText()).includes("凭证 1"));
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "375px voucher layout overflows");
    await screenshot("brief-groups-mobile-375-precise-voucher");
    checks.push("mobile 375px has no horizontal overflow and opens the exact loaded voucher locally");

    // A direct voucher route carries its exact business group and member even when
    // neither belongs to the first twenty root groups or members.
    await page.setViewportSize({ width: 1440, height: 1000 });
    prepare(40, 40);
    const focusGroup = group(29), focused = members(focusGroup.group_key, 34);
    synthetic.data.focused_activity_group = focusGroup;
    synthetic.data.focused_activity = focused.data.collections.members.items[0];
    synthetic.data.focused_voucher = focused.data.collections.vouchers.items[0];
    assert(validateBrief(synthetic), JSON.stringify(validateBrief.errors));
    holdRootContinuation = true; holdContinuation = true;
    const targetUrl = new URL("/", config.origin ?? new URL(config.ticket_url).origin);
    targetUrl.searchParams.set("company_id", company); targetUrl.searchParams.set("period", period); targetUrl.searchParams.set("voucher", "35");
    await page.goto(targetUrl.href); await page.locator("#selected-voucher").waitFor();
    assert((await page.locator("#selected-voucher .voucher-reference").innerText()).includes("凭证 35"));
    await page.getByRole("button", { name: "按业务", exact: true }).click();
    const precise = page.locator("#selected-business"); await precise.waitFor();
    assert((await precise.locator(".event-copy").innerText()).includes("同一对象 29"));
    await precise.locator(".member-row").nth(1).waitFor(); await waitHeld();
    assert((await precise.locator(".member-row").first().innerText()).includes("每笔不同的真实用途 35"));
    assert.equal(await page.locator(".activity-section .event-row").count(), 21, "exact group is prepended to the bounded root page");
    const exactBefore = requests.length; await precise.locator(".event-voucher-button").first().click(); await page.locator("#selected-voucher").waitFor(); await frames();
    assert.equal(requests.length, exactBefore, "deeply focused loaded voucher selection remains local");
    checks.push("exact deep route locates group 30 and member 35 outside both first pages without searching all pages");
    await screenshot("brief-groups-desktop-deep-precise-voucher");
    // Clone actual scoped backend responses, then validate every synthetic page.
    // Distinct scopes of one category and voucher must remain separate reads.
    phase = "sibling member scopes";
    await page.unroute("**/api/dashboard/business-status?*");
    const scoped = Object.values(fixtures).filter(sample => sample.command === "dashboard_business_status" && sample.response.data.detail_scope && sample.response.data.collections.settlement_events?.items.length);
    const firstScope = scoped.find(sample => scoped.some(other => other !== sample
      && other.response.data.detail_scope.category === sample.response.data.detail_scope.category
      && other.response.data.detail_scope.voucher_version_id === sample.response.data.detail_scope.voucher_version_id
      && other.response.data.detail_scope.key !== sample.response.data.detail_scope.key));
    assert(firstScope, "backend fixtures must include two scopes of the same voucher and original category");
    const secondScope = scoped.find(sample => sample !== firstScope
      && sample.response.data.detail_scope.category === firstScope.response.data.detail_scope.category
      && sample.response.data.detail_scope.voucher_version_id === firstScope.response.data.detail_scope.voucher_version_id
      && sample.response.data.detail_scope.key !== firstScope.response.data.detail_scope.key);
    const scopeSources = [firstScope.response, secondScope.response];
    const scopeRows = scopeSources.map((source, index) => {
      const key = source.data.detail_scope.key;
      const actual = Object.values(fixtures).filter(sample => sample.command === "dashboard_brief_group" && sample.response.data.section === "activity")
        .flatMap(sample => sample.response.data.collections.members.items).find(item => item.key === key);
      assert(actual, "each scoped backend sample must have a matching real activity member");
      return { source, member: { ...structuredClone(actual), group_key: `scope-group-${index}`, amount_fen: String((index + 1) * 11111), party: `事项${index ? "B" : "A"}专属对象`, description: `事项${index ? "B" : "A"}原单用途` }, tag: index ? "B" : "A" };
    });
    const scopeRoot = structuredClone(base);
    scopeRoot.snapshot_version = "same-voucher-sibling-scope-browser";
    scopeRoot.data.focused_activity = null; scopeRoot.data.focused_activity_group = null; scopeRoot.data.focused_voucher = null;
    scopeRoot.data.group_count = 2; scopeRoot.data.activity_count = 2; scopeRoot.data.voucher_count = 1;
    scopeRoot.data.activity_groups = [{ key: scopeRows[0].member.group, label: "同类别不同事项", event_count: 2, group_count: 2, type_counts: [{ label: "不同事项", count: 2 }] }];
    const completePage = length => ({ total_count: length, filtered_count: length, returned_count: length, has_more: false, next_cursor: null });
    scopeRoot.data.collections.activity = { items: scopeRows.map(({ member, tag }) => ({ ...group(0), key: member.group_key, group_key: member.group_key, group: member.group, kind: scopeSources[0].data.identity.kind, title: `事项${tag}`, party: member.party, member_count: 1, voucher_count: 1, amount_fen: member.amount_fen, amount_label: member.amount_label, is_batch: false })), page: completePage(2) };
    delete scopeRoot.data.collections.vouchers;
    assert(validateBrief(scopeRoot), JSON.stringify(validateBrief.errors));
    const heldScope = [];
    let delayA = false, delayAPage = true;
    await page.unroute("**/api/dashboard/brief?*"); await page.unroute("**/api/dashboard/brief-group?*");
    await page.route("**/api/dashboard/brief?*", route => fulfill(route, scopeRoot, validateBrief));
    await page.route("**/api/dashboard/brief-group?*", async route => {
      const url = new URL(route.request().url()), selected = scopeRows.find(item => item.member.group_key === url.searchParams.get("group_key"));
      if (!selected) { routeErrors.push({ phase, message: "unexpected sibling group request" }); return route.abort().catch(() => {}); }
      const payload = structuredClone(memberSeed);
      payload.read_context = structuredClone(base.read_context); payload.selected_period = structuredClone(base.selected_period); payload.snapshot_version = scopeRoot.snapshot_version;
      payload.data.group_key = selected.member.group_key;
      payload.data.collections.members = { items: [selected.member], page: completePage(1) };
      payload.data.collections.vouchers = { items: [{ ...structuredClone(voucherSeed), voucher_version_id: selected.member.voucher_version_id, subject_id: selected.member.subject_id, number: String(selected.member.voucher_number) }], page: completePage(1) };
      await fulfill(route, payload, validateGroup);
    });
    await page.route("**/api/dashboard/business-status?*", async route => {
      const url = new URL(route.request().url()), selected = scopeRows.find(item => item.member.detail_scope_key === url.searchParams.get("detail_scope_key"));
      if (!selected || url.searchParams.get("company_id") !== company || url.searchParams.get("period") !== period || url.searchParams.get("voucher_version_id") !== selected.member.voucher_version_id || url.searchParams.get("subject_id") !== selected.member.subject_id || url.searchParams.get("expected_version") !== scopeRoot.snapshot_version || url.searchParams.get("limit") !== "20") {
        routeErrors.push({ phase, message: "sibling detail request lost exact member identity" }); return route.abort().catch(() => {});
      }
      const offset = url.searchParams.get("cursor") ? 20 : 0;
      if (offset && (url.searchParams.get("cursor") !== `scope:${selected.member.key}:20` || url.searchParams.get("section") !== "settlement_events")) {
        routeErrors.push({ phase, message: "sibling continuation lost its member cursor" }); return route.abort().catch(() => {});
      }
      const payload = structuredClone(selected.source);
      payload.read_context = structuredClone(base.read_context); payload.selected_period = structuredClone(base.selected_period); payload.snapshot_version = scopeRoot.snapshot_version;
      payload.data.settlement_view = "current";
      payload.data.detail_scope.amount_fen = typeof payload.data.detail_scope.amount_fen === "number" ? Number(selected.member.amount_fen) : selected.member.amount_fen;
      const collection = payload.data.collections.settlement_events, event = collection.items[0];
      if (!event) { routeErrors.push({ phase, message: "scoped fixture must include a related settlement event" }); return route.abort().catch(() => {}); }
      const total = selected.tag === "A" ? 22 : 1, length = Math.min(20, total - offset);
      collection.items = Array.from({ length }, (_, index) => ({ ...event, id: `scope-event-${selected.tag}-${offset + index}`, party: selected.member.party, purpose_label: `事项${selected.tag}专属收付${offset + index + 1}` }));
      collection.page = { total_count: total, filtered_count: total, returned_count: length, has_more: offset + length < total, next_cursor: offset + length < total ? `scope:${selected.member.key}:20` : null };
      const release = () => fulfill(route, payload, validateStatus);
      if (selected.tag === "A" && (delayA || offset && delayAPage)) heldScope.push(release); else await release();
    });
    const scopeAddress = new URL("/", config.origin ?? new URL(config.ticket_url).origin);
    scopeAddress.searchParams.set("company_id", company); scopeAddress.searchParams.set("period", period);
    await page.goto(scopeAddress.href); await page.locator(".activity-section .event-row").first().waitFor();
    const scopeRequestsBefore = count("business-status");
    const scopeRow = index => page.locator(".activity-section .event-row").nth(index);
    await scopeRow(0).focus(); await scopeRow(0).press("Enter"); await finishMembers(scopeRow(0));
    assert.equal(count("business-status"), scopeRequestsBefore, "sibling member listing does not preload progress");
    await scopeRow(0).getByRole("button", { name: "业务进展", exact: true }).click();
    const panelA = scopeRow(0).locator(".business-detail-panel"); await panelA.waitFor();
    await page.waitForFunction(() => [...document.querySelectorAll(".business-detail-panel")].some(node => node.innerText.includes("事项A专属收付20")));
    let text = await panelA.innerText(); assert(text.includes(money(scopeRows[0].member.amount_fen))); assert(text.includes("事项A专属对象")); assert(!text.includes("事项B"));
    for (let attempt = 0; !heldScope.length && attempt < 100; attempt++) await page.waitForTimeout(20);
    assert.equal(heldScope.length, 1, "A continuation is held after its first twenty records");
    delayAPage = false; await heldScope.shift()();
    await page.waitForFunction(() => [...document.querySelectorAll(".business-detail-panel")].some(node => node.innerText.includes("事项A专属收付22")));
    await scopeRow(1).focus(); await scopeRow(1).press("Enter"); await finishMembers(scopeRow(1));
    const beforeB = count("business-status"); await scopeRow(1).getByRole("button", { name: "业务进展", exact: true }).click();
    const panelB = scopeRow(1).locator(".business-detail-panel"); await panelB.waitFor(); await frames();
    assert.equal(count("business-status"), beforeB + 1, "B does not reuse A's accepted detail");
    text = await panelB.innerText(); assert(text.includes(money(scopeRows[1].member.amount_fen))); assert(text.includes("事项B专属对象")); assert(text.includes("事项B专属收付1")); assert(!text.includes("事项A"));
    // Group collapse unmounts its detail and cancels A's held first response.
    delayA = true; await scopeRow(0).focus(); await scopeRow(0).press("Enter"); await finishMembers(scopeRow(0));
    await scopeRow(0).getByRole("button", { name: "业务进展", exact: true }).click();
    for (let attempt = 0; !heldScope.length && attempt < 100; attempt++) await page.waitForTimeout(20);
    assert.equal(heldScope.length, 1, "A first read is held before switching to B");
    await scopeRow(1).focus(); await scopeRow(1).press("Enter"); await finishMembers(scopeRow(1));
    await scopeRow(1).getByRole("button", { name: "业务进展", exact: true }).click(); await panelB.waitFor(); await frames();
    delayA = false; await heldScope.shift()(); await frames();
    text = await panelB.innerText(); assert(text.includes("事项B专属收付1")); assert(!text.includes("事项A"));
    assert.equal(await scopeRow(0).locator(".business-detail-panel").count(), 0, "late A does not recreate a closed detail");
    checks.push("same voucher/category sibling scopes keep amounts, objects, related payments and cursors isolated; late A never overwrites B");
    await screenshot("brief-sibling-matter-scopes");
    assert.deepEqual(errors, []);
    assert.deepEqual(routeErrors, []);
    return { status: "passed", checks, growth, screenshots, page_errors: errors, route_errors: routeErrors };
  } catch (error) {
    return { status: "failed", phase, message: error.message, checks, growth, screenshots, page_errors: errors, route_errors: routeErrors };
  } finally { await page.close(); await browser.close(); }
}

if (require.main === module) {
  let input = ""; process.stdin.setEncoding("utf8"); process.stdin.on("data", chunk => input += chunk);
  process.stdin.on("end", async () => { try { const result = await run(JSON.parse(input)); process.stdout.write(`${JSON.stringify(result)}\n`); if (result.status !== "passed") process.exitCode = 1; } catch (error) { process.stdout.write(`${JSON.stringify({ status: "failed", message: error.message })}\n`); process.exitCode = 1; } });
}
module.exports = { run };
