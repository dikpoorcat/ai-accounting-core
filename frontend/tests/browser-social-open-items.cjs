// Authenticated service reads only; all invented obligations live in intercepted responses.
// stdin: origin, ticket_url, company_id, period, playwright_module, optional channel,
// screenshots_directory (also accepts the older screenshot_directory spelling).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

async function run(config) {
  const deadline = Date.now() + 120000;
  const { chromium } = require(config.playwright_module);
  const { validateDashboardBriefResponse: validate } = await import(pathToFileURL(path.join(__dirname, "../src/api/generated/dashboardBrief.js")));
  const browser = await chromium.launch({ channel: config.channel || "msedge", headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
  const page = await context.newPage(), requests = [], errors = [];
  page.setDefaultTimeout(10000);
  page.setDefaultNavigationTimeout(15000);
  const bounded = (promise, label, milliseconds = 10000) => {
    let timer;
    return Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`${label} timed out after ${milliseconds} ms`)), milliseconds); })]).finally(() => clearTimeout(timer));
  };
  page.on("pageerror", () => errors.push("browser script error"));
  page.on("request", request => { const url = new URL(request.url()); if (url.pathname.startsWith("/api/dashboard/")) requests.push(url); });
  const frames = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const idle = async () => { await page.waitForFunction(() => document.querySelector(".module-header")?.getAttribute("aria-busy") === "false"); await frames(); };
  const details = () => requests.filter(url => url.pathname.endsWith("business-status")).length;
  const refresh = async () => { const response = page.waitForResponse(response => { const url = new URL(response.url()); return url.pathname === "/api/dashboard/brief" && !url.searchParams.has("section"); }); await page.getByRole("button", { name: "刷新数据", exact: true }).click(); await response; await idle(); };
  const address = new URL(config.ticket_url); address.searchParams.set("company_id", config.company_id); address.searchParams.set("period", config.period);
  let phase = "real read-only page", releasePage, continuationStarted;
  const setPhase = label => { phase = label; process.stderr.write(JSON.stringify({ phase }) + "\n"); };
  setPhase(phase);
  const overallTimer = setTimeout(() => { process.stderr.write(JSON.stringify({ phase, error: "overall timeout after 120000 ms" }) + "\n"); void browser.close(); }, Math.max(1, deadline - Date.now()));
  try {
    const first = page.waitForResponse(response => new URL(response.url()).pathname === "/api/dashboard/brief");
    await page.goto(address.href); const real = await (await first).json();
    assert(validate(real), "real brief violates generated contract"); await idle();
    assert.equal(details(), 0, "default page requests business details");
    assert(real.data, "selected real company has no brief");
    const realContribution = real.data.collections.open_items.items.find(item => item.contribution_group_key && item.contribution_component && item.payroll_period);
    let realGroupDetail = false;
    if (realContribution) {
      await page.locator(".section-nav").getByRole("button", { name: "待收待付", exact: true }).click();
      const categories = real.data.open_items.categories.filter(category => category.count);
      await page.locator("#open-items .open-index button").nth(categories.findIndex(category => category.key === "payroll_payables")).click();
      const realGroup = page.locator("#open-items .open-event-row").filter({ hasText: realContribution.party }).filter({ has: page.locator(".open-event-matter", { hasText: "社保与公积金" }) }).first();
      await realGroup.focus(); await realGroup.press("Enter"); await realGroup.locator(".contribution-detail").waitFor(); await frames();
      assert.equal(details(), 0, "real grouped detail requested business status");
      assert.equal(await realGroup.getAttribute("aria-expanded"), "true");
      await realGroup.press("Space"); assert.equal(await realGroup.getAttribute("aria-expanded"), "false"); realGroupDetail = true;
      // Restore the main module before installing the deliberately delayed synthetic continuation.
      await page.locator(".section-nav").getByRole("button", { name: "概览", exact: true }).click();
    }
    const synthetic = structuredClone(real), sample = structuredClone(real.data.collections.open_items.items[0] || JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures/dashboard-contracts.json"), "utf8")).brief.response.data.collections.open_items.items[0]);
    const components = ["employee_social", "employee_housing", "employer_social", "employer_housing"];
    const item = (id, changes = {}) => ({ ...sample, id, category_key: "payroll_payables", party: "合成同名员工", description: "合成待付款", subject_id: `subject-${id}`, status: "open", current_status: "open", source_amount_fen: "10000", paid_fen: "0", other_settled_fen: "0", outstanding_fen: "10000", current_outstanding_fen: "10000", contribution_group_key: null, contribution_component: null, payroll_period: null, ...changes });
    // One group crosses the original 20-obligation page boundary. Salary and tax stay separate.
    const source = [item("first-social", { contribution_group_key: "employee-a", contribution_component: components[0], payroll_period: config.period }), item("salary", { description: "实发工资" }), item("tax", { description: "个人所得税" }), ...Array.from({ length: 17 }, (_, index) => item(`ordinary-${index}`, { party: `普通员工${index}` })), ...components.slice(1).map((component, index) => item(`last-${index}`, { contribution_group_key: "employee-a", contribution_component: component, payroll_period: config.period })), item("different-person", { contribution_group_key: "employee-b", contribution_component: components[0], payroll_period: config.period }), item("different-month", { contribution_group_key: "employee-a", contribution_component: components[0], payroll_period: "2025-01" })];
    synthetic.snapshot_version = "social-browser-snapshot";
    synthetic.data.open_items = { ...synthetic.data.open_items, cutoff_period: config.period, current_cutoff_period: config.period, receivable_count: 0, receivable_fen: "0", payable_count: source.length, payable_fen: String(source.length * 10000), total_count: source.length, categories: [{ key: "payroll_payables", label: "待付工资、社保与个税", direction: "payable", unit: "笔", count: source.length, loaded_count: 20, outstanding_fen: String(source.length * 10000) }] };
    let hold = true, failContinuation = true, continuationRequests = 0;
    const started = new Promise(resolve => { continuationStarted = resolve; });
    await page.route("**/api/dashboard/brief?*", async route => {
      const url = new URL(route.request().url()), section = url.searchParams.get("section"), offset = Number(url.searchParams.get("cursor") || 0);
      assert.equal(url.searchParams.get("limit"), "20");
      // Capture the response before a hold so a released old request keeps its original snapshot.
      const response = structuredClone(synthetic), items = structuredClone(source.slice(offset, offset + 20));
      if (offset) {
        assert.equal(section, "open_items"); assert.equal(url.searchParams.get("expected_version"), synthetic.snapshot_version);
        continuationRequests++; continuationStarted(); if (hold) await new Promise(resolve => { releasePage = resolve; });
        if (failContinuation) { failContinuation = false; return route.fulfill({ status: 503, json: { code: "synthetic_social_retry", message: "合成后续页读取失败，请重试" } }).catch(() => {}); }
      }
      const collection = { items, page: { total_count: source.length, filtered_count: source.length, returned_count: items.length, has_more: offset + 20 < source.length, next_cursor: offset + 20 < source.length ? String(offset + 20) : null } };
      if (section) response.data.collections = { open_items: collection }; else response.data.collections.open_items = collection;
      assert(validate(response), `synthetic contract: ${JSON.stringify(validate.errors)}`);
      await route.fulfill({ json: response }).catch(() => {});
    });
    setPhase("incomplete collection");
    await refresh();
    const rows = page.locator("#open-items .open-event-row"), group = rows.first();
    assert.match(await group.locator(".open-event-money").textContent(), /等待读取/);
    const beforeExpand = details(); await group.focus();
    await bounded(started, "keyboard focus continuation start without navigation");
    assert.equal(await page.locator('.section-nav [aria-current="location"]').textContent().then(text => text.trim()), "待收待付");
    assert.match(await group.textContent(), /正在汇总|尚未读全/);
    assert.doesNotMatch(await group.locator(".open-event-money").textContent(), /¥/);
    await group.press("Enter");
    assert.equal(await group.getAttribute("aria-expanded"), "true"); await frames(); assert.equal(details(), beforeExpand);
    setPhase("failed continuation");
    hold = false; releasePage();
    await page.locator("#open-items .dashboard-pagination").getByRole("button", { name: "重试读取", exact: true }).waitFor(); await frames();
    assert.match(await group.textContent(), /尚未读全/); assert.doesNotMatch(await group.locator(".open-event-money").textContent(), /¥/);
    assert.doesNotMatch(await group.locator(".contribution-detail").textContent(), /¥/);
    assert.equal(details(), beforeExpand);
    setPhase("completed group after retry");
    await page.locator("#open-items .dashboard-pagination").getByRole("button", { name: "重试读取", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("#open-items .open-event-row .open-event-money")?.textContent.includes("¥400.00")); await frames();
    assert.equal(continuationRequests, 2); assert.equal(await rows.count(), 22);
    assert.equal(await group.locator("table tbody tr").count(), 4);
    for (const label of ["个人社保", "个人公积金", "公司社保", "公司公积金", "原应付", "实际已付", "抵销／代付", "月末待付"]) assert((await group.textContent()).includes(label), label);
    assert.equal(details(), beforeExpand, "group table requested business status");
    assert.equal(await rows.filter({ hasText: "合成同名员工" }).count(), 5, "name or period was used to merge separate identities");
    await group.locator("table").click(); assert.equal(await group.getAttribute("aria-expanded"), "true", "detail click toggled row");
    const beforeReopen = requests.length;
    await group.focus(); await group.press("Space"); assert.equal(await group.getAttribute("aria-expanded"), "false");
    await group.press("Enter"); assert.equal(await group.getAttribute("aria-expanded"), "true");
    await frames(); assert.equal(requests.length, beforeReopen, "same-snapshot local reopening issued a request");
    const secondGroup = rows.filter({ hasText: "合成同名员工" }).nth(3); await secondGroup.focus(); await secondGroup.press("Enter");
    assert.equal(await group.getAttribute("aria-expanded"), "false"); assert.equal(await secondGroup.getAttribute("aria-expanded"), "true");
    assert.match(await secondGroup.textContent(), /该月末未列待付款项/);
    setPhase("responsive layout");
    const layouts = [], directory = config.screenshots_directory || config.screenshot_directory;
    for (const theme of ["light", "dark"]) for (const width of [320, 375, 768, 1440]) {
      await page.evaluate(theme => document.documentElement.dataset.theme = theme, theme); await page.setViewportSize({ width, height: 1000 }); await frames();
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `${theme}/${width} page overflow`);
      assert(await secondGroup.evaluate(element => element.scrollWidth <= element.clientWidth + 1), `${theme}/${width} row overflow`);
      if (directory) { fs.mkdirSync(directory, { recursive: true }); await secondGroup.screenshot({ path: path.join(directory, `social-${theme}-${width}.png`) }); }
      layouts.push({ theme, width });
    }
    setPhase("refresh scope collapse");
    synthetic.snapshot_version = "social-browser-next-snapshot";
    await refresh();
    assert.equal(await page.locator('#open-items .open-event-row[aria-expanded="true"]').count(), 0);
    setPhase("latest changed components and semantic colors");
    Object.assign(source[0], { current_status: "partial", current_outstanding_fen: "5000" });
    Object.assign(source[20], { current_status: "settled", current_outstanding_fen: "0" });
    Object.assign(source[21], { current_status: "reversed", current_outstanding_fen: "0" });
    Object.assign(source[23], { current_status: "reversed", current_outstanding_fen: "0" });
    Object.assign(source[24], { current_status: "settled", current_outstanding_fen: "0" });
    synthetic.snapshot_version = "social-browser-colors";
    await refresh();
    await page.locator(".section-nav").getByRole("button", { name: "待收待付", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("#open-items .open-event-row .open-event-money")?.textContent.includes("¥400.00"));
    await group.focus(); await group.press("Enter"); await group.locator(".contribution-latest").waitFor();
    const latest = group.locator(".contribution-current"); assert.equal(await latest.count(), 3);
    for (const label of ["个人社保", "个人公积金", "公司社保"]) assert((await group.locator(".contribution-latest").textContent()).includes(label), label);
    assert(!(await group.locator(".contribution-latest").textContent()).includes("公司公积金"), "unchanged component appears in latest progress");
    const color = async (locator, token) => assert(await locator.evaluate((element, token) => {
      const probe = document.createElement("span"); probe.style.color = `var(${token})`; element.append(probe);
      const same = getComputedStyle(element).color === getComputedStyle(probe).color; probe.remove(); return same;
    }, token), `incorrect semantic status color: ${token}`);
    for (const theme of ["light", "dark"]) {
      await page.evaluate(theme => document.documentElement.dataset.theme = theme, theme);
      await color(group.locator(":scope > .status"), "--brief-amber");
      await color(rows.filter({ hasText: "合成同名员工" }).nth(3).locator(":scope > .status"), "--brief-muted");
      await color(rows.filter({ hasText: "合成同名员工" }).nth(4).locator(":scope > .status"), "--brief-green");
      await color(latest.filter({ hasText: "个人社保" }).locator(".status"), "--brief-amber");
      await color(latest.filter({ hasText: "个人公积金" }).locator(".status"), "--brief-green");
      await color(latest.filter({ hasText: "公司社保" }).locator(".status"), "--brief-muted");
    }
    assert.equal(details(), beforeExpand);
    setPhase("late continuation rejected after snapshot changes");
    hold = true; synthetic.snapshot_version = "social-browser-held-snapshot";
    const lateStarted = new Promise(resolve => { continuationStarted = resolve; });
    await refresh();
    await page.locator(".section-nav").getByRole("button", { name: "待收待付", exact: true }).click();
    await bounded(lateStarted, "held old-snapshot continuation start"); const releaseOldPage = releasePage;
    hold = false; synthetic.snapshot_version = "social-browser-replacement-snapshot";
    await refresh();
    await page.locator(".section-nav").getByRole("button", { name: "待收待付", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("#open-items .open-event-row .open-event-money")?.textContent.includes("¥400.00")); await frames();
    const beforeLate = requests.length; releaseOldPage(); await frames();
    assert.equal(await rows.count(), 22); assert.equal(requests.length, beforeLate, "late old page triggered an extra refresh");
    assert.match(await group.locator(".open-event-money").textContent(), /¥400\.00/);
    assert.equal(errors.length, 0);
    return { status: "passed", real_group_detail: realGroupDetail, original_obligations: source.length, grouped_rows: 22, incomplete_amount_hidden: true, continuation_retry: true, local_group_detail: true, cached_reopen: true, changed_components_only: true, status_colors: true, late_snapshot_rejected: true, page_limit: 20, keyboard: true, keyboard_activation_without_navigation: true, independent_identity_and_period: true, layouts, browser_errors: errors.length };
  } catch (error) { throw new Error(`${phase}: ${error.message}`); }
  finally { clearTimeout(overallTimer); releasePage?.(); await browser.close(); }
}
if (require.main === module) {
  let input = ""; process.stdin.setEncoding("utf8"); process.stdin.on("data", chunk => { input += chunk; });
  process.stdin.on("end", async () => { try { process.stdout.write(JSON.stringify(await run(JSON.parse(input))) + "\n"); } catch (error) { process.stdout.write(JSON.stringify({ status: "failed", message: String(error.message).replace(/https?:\/\/[^\s"']+/g, "[URL]") }) + "\n"); process.exitCode = 1; } });
}
module.exports = { run };
