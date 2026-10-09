// Read-only checks on an isolated service, plus browser-only validated synthetic responses.
// JSON stdin: origin, ticket_url, playwright_module, channel, company_id, period.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

async function run(config) {
  const { chromium } = require(config.playwright_module);
  const { validateDashboardBriefResponse: validateBrief } = await import(pathToFileURL(path.join(__dirname, "../src/api/generated/dashboardBrief.js")));
  const { validateDashboardBriefGroupResponse: validateGroup } = await import(pathToFileURL(path.join(__dirname, "../src/api/generated/dashboardBriefGroup.js")));
  const fixtures = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures/dashboard-contracts.json"), "utf8"));
  const company = config.company_id ?? config.companies?.[0]?.id;
  const period = config.period ?? config.companies?.[0]?.period;
  const browser = await chromium.launch({ channel: config.channel ?? "msedge", headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
  const requests = [], errors = [], routeErrors = [], checks = [], growth = [], screenshots = [];
  let phase = "real", base, synthetic = null, memberTotal = 24, groupTotal = 1;
  let holdFirst = false, holdContinuation = false, holdRootContinuation = false;
  const held = [], heldRoots = [];
  page.on("request", request => { const url = new URL(request.url()); if (url.pathname.startsWith("/api/dashboard/")) requests.push(url); });
  page.on("pageerror", error => errors.push(error.message));
  const count = action => requests.filter(url => url.pathname === `/api/dashboard/${action}`).length;
  const frames = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const reply = action => page.waitForResponse(response => new URL(response.url()).pathname === `/api/dashboard/${action}`);
  const refresh = async () => { const pending = reply("brief"); await page.getByRole("button", { name: "刷新数据", exact: true }).click(); await pending; await page.waitForFunction(() => document.querySelector(".module-header")?.getAttribute("aria-busy") === "false"); await frames(); };
  const finishMembers = async row => { await row.locator(".member-row").first().waitFor(); await row.locator(".group-members .dashboard-pagination").waitFor({ state: "hidden", timeout: 60000 }); await frames(); };
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
    const address = new URL(config.ticket_url); address.searchParams.set("company_id", company); address.searchParams.set("period", period);
    const first = reply("brief"); await page.goto(address.href); base = await (await first).json(); assert(validateBrief(base), JSON.stringify(validateBrief.errors));
    await page.locator(".activity-section").waitFor(); await frames();
    assert.equal(count("brief-group"), 0, "default overview does not read group members");
    assert.equal(count("business-status"), 0, "default overview does not read business progress");
    assert.equal(base.data.collections.vouchers, undefined, "default brief does not hydrate voucher pages");
    const realGroup = base.data.collections.activity.items[0];
    if (realGroup) {
      const category = base.data.activity_groups.findIndex(item => item.key === realGroup.group);
      await page.locator(".activity-section .index button").nth(category).click();
      const row = page.locator(".activity-section .event-row").first();
      assert.equal((await row.locator(".event-money b").textContent()).trim(), money(realGroup.amount_fen));
      const detail = reply("brief-group"); await row.focus(); await row.press("Enter");
      const memberReply = await (await detail).json(); assert(validateGroup(memberReply), JSON.stringify(validateGroup.errors));
      await finishMembers(row); assert.equal(count("business-status"), 0);
      await screenshot("brief-groups-desktop-expanded");
      const voucherMember = memberReply.data.collections.members.items.find(item => item.voucher_version_id);
      const buttons = row.locator(".event-voucher-button:not(:disabled)");
      if (voucherMember && await buttons.count()) {
        const before = requests.length; await buttons.first().hover(); await row.locator('[role="tooltip"]').waitFor(); await frames();
        assert.equal(requests.length, before, "loaded voucher hover sends no requests");
        assert((await row.locator('[role="tooltip"]').innerText()).includes(`凭证 ${voucherMember.voucher_number}`));
        await buttons.first().click(); await page.locator("#selected-voucher").waitFor();
        assert((await page.locator("#selected-voucher .voucher-reference").innerText()).includes(`凭证 ${voucherMember.voucher_number}`));
        assert.equal(count("business-status"), 0); checks.push("real keyboard expansion, local hover, precise voucher selection");
        await page.getByRole("button", { name: "按业务", exact: true }).click();
      } else checks.push("real keyboard expansion; this group has no voucher member");
    } else checks.push("real activity empty; interactive coverage uses validated synthetic groups");
    const openCandidates = [base.data.collections.open_items.items[0], base.data.collections.open_items.items.find(item => item.description.includes("社保"))].filter((item, index, all) => item && all.findIndex(other => other?.group_key === item.group_key) === index);
    for (const realOpen of openCandidates) {
      const category = base.data.open_items.categories.filter(item => item.count).findIndex(item => item.key === realOpen.category_key);
      await page.locator(".open-items .open-index button").nth(category).click();
      const rowIndex = base.data.collections.open_items.items.filter(item => item.category_key === realOpen.category_key).findIndex(item => item.group_key === realOpen.group_key);
      const row = page.locator(".open-items .open-event-row").nth(rowIndex);
      assert.equal((await row.locator(".open-event-money b").textContent()).trim(), money(realOpen.outstanding_fen));
      const detail = reply("brief-group"); await row.focus(); await row.press("Enter");
      const payload = await (await detail).json(); assert(validateGroup(payload), JSON.stringify(validateGroup.errors));
      await finishMembers(row); assert.equal(count("business-status"), 0);
      const purpose = payload.data.collections.members.items.find(item => item.purpose)?.purpose;
      if (purpose) assert((await row.locator(".group-members").innerText()).includes(purpose));
      if (realOpen.description.includes("社保")) {
        await screenshot("brief-pending-social-desktop-expanded");
        assert((await row.locator(".group-members").innerText()).includes("社保与公积金"));
        checks.push("real pending social detail keeps monthly four-component breakdowns");
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
      return { ...base.data.collections.activity.items[0] ?? fixtures.brief.response.data.collections.activity.items[0], key: `synthetic-group-${index}`, group_key: `synthetic-group-${index}`, group: "funds", kind: "collection", party: `同一对象 ${index}`, title: "代收代付", member_count: memberTotal, voucher_count: memberTotal, amount_fen: String(BigInt(memberTotal) * 100n), amount_label: "代收金额", state: "已入账", date_from: null, date_to: null, has_month_recognition: true };
    }
    function rootPage(offset = 0) {
      const result = structuredClone(synthetic), items = Array.from({ length: Math.min(20, groupTotal - offset) }, (_, n) => group(offset + n));
      result.data.collections.activity = { items, page: { total_count: groupTotal, filtered_count: groupTotal, returned_count: items.length, has_more: offset + items.length < groupTotal, next_cursor: offset + items.length < groupTotal ? `groups:${offset + items.length}` : null } };
      return result;
    }
    function members(key, offset) {
      const result = structuredClone(memberSeed);
      result.read_context = structuredClone(base.read_context); result.selected_period = structuredClone(base.selected_period); result.snapshot_version = synthetic.snapshot_version;
      result.data.group_key = key;
      const length = Math.min(20, memberTotal - offset);
      result.data.collections.members.items = Array.from({ length }, (_, n) => {
        const index = offset + n;
        return { ...activitySeed, key: `member-${key}-${index}`, group_key: key, subject_id: `subject-${key}-${index}`, voucher_version_id: `voucher-${key}-${index}`, voucher_number: index + 1, date: null, recognition: { ...activitySeed.recognition, period, date: null, precision: "month", label: `${period} · 按月确认` }, party: "同一对象", title: "代收代付", description: `每笔不同的真实用途 ${index + 1}`, state: "已入账", amount_fen: "100", amount_label: "代收金额", group: "funds" };
      });
      result.data.collections.members.page = { total_count: memberTotal, filtered_count: memberTotal, returned_count: length, has_more: offset + length < memberTotal, next_cursor: offset + length < memberTotal ? `members:${offset + length}` : null };
      result.data.collections.vouchers.items = result.data.collections.members.items.map((item, index) => ({ ...voucherSeed, voucher_version_id: item.voucher_version_id, subject_id: item.subject_id, number: String(offset + index + 1), date: null, recognition: item.recognition, business_amount_fen: "100", business_amount_label: "代收金额", amount_fen: "100", list_summary: item.description, summary: item.description, has_business_progress: false,
        lines: voucherSeed.lines.slice(0, 2).map((line, lineIndex) => ({ ...line, line_number: lineIndex + 1, debit_fen: lineIndex ? "0" : "100", credit_fen: lineIndex ? "100" : "0", party: "", parties: [], party_state: "not_applicable" })) }));
      result.data.collections.vouchers.page = { total_count: length, filtered_count: length, returned_count: length, has_more: false, next_cursor: null };
      return result;
    }
    function prepare(groups, records) {
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
      if (url.searchParams.get("section") === "open_items") return fulfill(route, synthetic, validateBrief);
      const cursor = url.searchParams.get("cursor"), offset = cursor?.startsWith("groups:") ? Number(cursor.slice(7)) : 0;
      if (offset && holdRootContinuation) { heldRoots.push(() => fulfill(route, rootPage(offset), validateBrief)); return; }
      return fulfill(route, rootPage(offset), validateBrief);
    });
    await page.route("**/api/dashboard/brief-group?*", async route => {
      if (!synthetic) return route.continue();
      const url = new URL(route.request().url()), cursor = url.searchParams.get("cursor"), offset = cursor ? Number(cursor.slice(8)) : 0;
      const payload = members(url.searchParams.get("group_key"), offset);
      if ((!offset && holdFirst) || (offset && holdContinuation)) { held.push(() => fulfill(route, payload, validateGroup)); return; }
      return fulfill(route, payload, validateGroup);
    });

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

    prepare(24, 40); holdRootContinuation = true; await refresh();
    const rows = page.locator(".activity-section .event-row");
    assert.equal(await rows.count(), 20, "default root collection is bounded at twenty groups");
    holdContinuation = true; const row = rows.first(); await row.focus(); await row.press("Enter");
    await row.locator(".member-row").first().waitFor(); await page.waitForFunction(() => document.querySelectorAll(".group-members .member-row").length === 20);
    await page.evaluate(() => { window.__firstBriefMember = document.querySelector(".group-members li"); });
    assert.equal(await row.locator(".member-row").count(), 20); assert.equal(count("business-status"), 0);
    await waitHeld(); holdContinuation = false; await held.shift()(); await finishMembers(row);
    assert.equal(await row.locator(".member-row").count(), 40);
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
