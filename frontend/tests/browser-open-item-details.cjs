// Read-only real-book checks, followed by explicitly mocked browser-only scenarios.
// Input on stdin: origin, ticket_url, company_id, period, playwright_module,
// optional channel and screenshot_directory. Never persist the login ticket.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

async function run(config) {
  const { chromium } = require(config.playwright_module);
  const { validateDashboardBusinessStatusResponse } = await import(pathToFileURL(path.join(__dirname, "../src/api/generated/dashboardBusinessStatus.js")));
  const { validateDashboardBriefResponse } = await import(pathToFileURL(path.join(__dirname, "../src/api/generated/dashboardBrief.js")));
  const browser = await chromium.launch({ channel: config.channel || "msedge", headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
  const page = await context.newPage();
  const requests = [], failures = [];
  page.on("pageerror", () => failures.push("browser script error"));
  page.on("request", request => { const url = new URL(request.url()); if (url.pathname.startsWith("/api/dashboard/")) requests.push(url); });
  const frames = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const reply = action => { const pending = page.waitForResponse(response => new URL(response.url()).pathname === `/api/dashboard/${action}`); void pending.catch(() => {}); return pending; };
  const format = value => {
    if (value == null) return "待核对";
    const amount = BigInt(value), abs = amount < 0n ? -amount : amount;
    return `${amount < 0n ? "−" : ""}¥${new Intl.NumberFormat("zh-CN").format(abs / 100n)}.${String(abs % 100n).padStart(2, "0")}`;
  };
  const address = new URL(config.ticket_url);
  address.searchParams.set("company_id", config.company_id); address.searchParams.set("period", config.period);
  let phase = "real page";
  try {
    const first = reply("brief"); await page.goto(address.href);
    const brief = await (await first).json();
    assert(validateDashboardBriefResponse(brief), "real brief contract failed");
    await page.locator("#open-items").waitFor();
    await page.waitForFunction(() => document.querySelector(".module-header")?.getAttribute("aria-busy") === "false");
    assert.equal(requests.filter(url => url.pathname.endsWith("business-status")).length, 0, "default page requested details");
    const isSingle = item => item.subject_id && !(item.contribution_group_key && item.contribution_component && item.payroll_period);
    const category = brief.data.open_items.categories.find(category => brief.data.collections.open_items.items.some(item => item.category_key === category.key && isSingle(item)));
    assert(category, "real book has no loaded business category");
    await page.locator("#open-items .open-index button").nth(brief.data.open_items.categories.filter(item => item.count).indexOf(category)).click();
    await page.locator("#open-items .open-event-row").first().waitFor();
    const selected = brief.data.collections.open_items.items.find(item => item.category_key === category.key && isSingle(item));
    assert(selected, "real book has no selectable business row");
    const row = page.locator("#open-items .open-event-row").filter({ hasText: selected.party }).filter({ hasText: selected.description }).first();
    const trigger = row, panel = row.locator(".business-detail-panel");
    const detailReply = reply("business-status"); await trigger.focus(); await trigger.press("Enter");
    phase = "real detail";
    const response = await detailReply, realDetail = await response.json();
    assert.equal(response.status(), 200); assert(validateDashboardBusinessStatusResponse(realDetail), "real detail contract failed");
    await panel.waitFor();
    const exact = realDetail.data.settlements.obligations.find(item => item.key === selected.id);
    assert(exact, "real detail cannot locate selected obligation");
    assert.equal((await row.locator(".open-event-money b").textContent()).trim(), format(selected.outstanding_fen));
    const requestCount = requests.filter(url => url.pathname.endsWith("business-status")).length;
    await trigger.press("Space"); await panel.waitFor({ state: "hidden" });
    await trigger.press("Enter"); await panel.waitFor(); await frames();
    assert.equal(requests.filter(url => url.pathname.endsWith("business-status")).length, requestCount, "same snapshot re-expansion requested data");
    const realLayouts = [];
    for (const width of [320, 375, 768, 1440]) {
      await page.setViewportSize({ width, height: 1000 }); await frames();
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `real page overflows at ${width}`);
      assert(await panel.evaluate(element => element.scrollWidth <= element.clientWidth + 1), `real detail overflows at ${width}`);
      realLayouts.push(width);
    }
    await trigger.click({ position: { x: 12, y: 12 } }); await panel.waitFor({ state: "hidden" });
    const countBeforeRefresh = requests.filter(url => url.pathname.endsWith("business-status")).length;
    phase = "real refresh";
    const refreshed = reply("brief"); await page.getByRole("button", { name: "刷新数据", exact: true }).click(); await refreshed;
    await page.waitForFunction(() => document.querySelector(".module-header")?.getAttribute("aria-busy") === "false"); await frames();
    assert.equal(requests.filter(url => url.pathname.endsWith("business-status")).length, countBeforeRefresh, "refresh fetched collapsed details");

    // Mock the read responses only; no accounting writes or database replacements.
    const synthetic = structuredClone(brief), detail = structuredClone(realDetail);
    const key = "synthetic-net", subject = "synthetic-payroll";
    const later = config.period === "2026-12" ? "2027-01" : `${config.period.slice(0, 4)}-${String(Number(config.period.slice(5)) + 1).padStart(2, "0")}`;
    const source = { key, name: "net", direction: "payable", category_key: "payroll_payables", source_period: config.period, source_amount_fen: "800000", paid_fen: "600000", other_settled_fen: "0", remaining_fen: "200000", settlement_status: "partial" };
    synthetic.data.open_items = { ...synthetic.data.open_items, receivable_count: 0, receivable_fen: "0", payable_count: 1, payable_fen: "200000", total_count: 1, complete: true, cutoff_period: config.period, current_cutoff_period: later, categories: [{ key: "payroll_payables", label: "待付工资、社保与个税", direction: "payable", unit: "笔", count: 1, loaded_count: 1, outstanding_fen: "200000" }] };
    synthetic.data.collections.open_items = { items: [{ id: key, category_key: "payroll_payables", party: "演示员工张某（长名称换行测试）", description: "本月工资及已记录的业务用途", status: "partial", source_amount_fen: "800000", paid_fen: "600000", other_settled_fen: "0", outstanding_fen: "200000", current_status: "settled", current_outstanding_fen: "0", subject_id: subject, contribution_group_key: null, contribution_component: null, payroll_period: null }], page: { total_count: 1, filtered_count: 1, returned_count: 1, has_more: false, next_cursor: null } };
    detail.data.identity.subject_id = subject; detail.data.identity.kind = "payroll"; detail.data.latest_source.deleted = false;
    detail.data.settlements = { cutoff_period: config.period, status: "established", checking: false, obligations: [source, { ...source, key: "synthetic-tax", name: "tax", remaining_fen: "12345" }] };
    detail.data.current_followups.settlements = { ...detail.data.settlements, cutoff_period: later, obligations: [{ ...source, paid_fen: "800000", remaining_fen: "0", settlement_status: "settled" }] };
    detail.data.display_profiles = { business: { entity_id: subject, values: { display_name: "演示工资", display_number: null, purpose: "已确认的本月工资，包含个人实发款与税费；本详情单独说明选中的个人实发款。", note: null, employment_start: null, employment_end: null, employment_status: null, active: null, category_label: null, rights_description: null } } };
    const event = n => ({ id: `synthetic-${n}`, subject_id: `synthetic-payment-${n}`, source_subject_id: subject, posting_period: config.period, direction: n === 4 ? -1 : 1, signed_amount_fen: n === 4 ? "-10000" : "10000", relation_state: "resolved", kind: "payment", name: n % 2 ? "tax" : "net", purpose_label: n % 2 ? "个人所得税" : "实发工资", party: "演示员工张某", mode: ["payment", "offset", "advance", "accepted"][n % 4] });
    detail.data.collections.settlement_events = { items: Array.from({ length: 20 }, (_, n) => event(n)), page: { total_count: 21, filtered_count: 21, returned_count: 20, has_more: true, next_cursor: "synthetic-next" }, scope_period: config.period, current_cutoff_period: later, cutoff_semantics: "current_published_relations_independent_of_as_of" };
    assert(validateDashboardBriefResponse(synthetic), "synthetic brief contract failed"); assert(validateDashboardBusinessStatusResponse(detail), "synthetic detail contract failed");
    let initialAttempts = 0, continuationAttempts = 0, delayed = false, pendingRoute, releasePending, pendingRead;
    phase = "synthetic refresh";
    await page.route("**/api/dashboard/brief?*", route => route.fulfill({ json: synthetic }));
    await page.route("**/api/dashboard/business-status?*", async route => {
      const url = new URL(route.request().url());
      assert.equal(url.searchParams.get("subject_id"), subject); assert.equal(url.searchParams.get("limit"), "20");
      assert.equal(url.searchParams.get("expected_version"), detail.snapshot_version);
      if (url.searchParams.has("cursor")) {
        assert.equal(url.searchParams.get("cursor"), "synthetic-next"); continuationAttempts += 1;
        if (continuationAttempts === 1) return route.fulfill({ status: 500, json: { code: "synthetic_retry", message: "合成续页失败，请重试" } });
        const next = structuredClone(detail); next.data.collections.settlement_events.items = [event(20)];
        next.data.collections.settlement_events.page = { total_count: 21, filtered_count: 21, returned_count: 1, has_more: false, next_cursor: null };
        return route.fulfill({ json: next });
      }
      initialAttempts++;
      if (initialAttempts === 1) return route.fulfill({ status: 500, json: { code: "synthetic_initial", message: "合成首次读取失败，请重试" } });
      if (delayed) { pendingRoute = route; pendingRead(); const stale = structuredClone(detail); await new Promise(resolve => releasePending = resolve); try { await route.fulfill({ json: stale }); } catch { /* Canceled read. */ } return; }
      return route.fulfill({ json: detail });
    });
    const mockRefresh = reply("brief"); await page.getByRole("button", { name: "刷新数据", exact: true }).click(); await mockRefresh;
    await page.locator("#open-items .open-event-copy strong").filter({ hasText: "演示员工" }).waitFor();
    const mockRow = page.locator("#open-items .open-event-row").first(), mockPanel = mockRow.locator(".business-detail-panel");
    phase = "synthetic detail";
    const beforeExpand = requests.filter(url => url.pathname.endsWith("business-status")).length;
    await mockRow.click({ position: { x: 12, y: 12 } });
    await mockRow.getByRole("button", { name: "重新读取", exact: true }).waitFor();
    assert.equal((await mockRow.locator(".open-event-money b").textContent()).trim(), "¥2,000.00");
    await mockRow.getByRole("button", { name: "重新读取", exact: true }).click(); await mockPanel.waitFor();
    await mockPanel.getByRole("button", { name: "重试读取", exact: true }).waitFor();
    assert.equal(requests.filter(url => url.pathname.endsWith("business-status")).length, beforeExpand + 3);
    assert.equal(await mockRow.getAttribute("aria-expanded"), "true");
    assert.equal((await mockRow.locator(".open-event-money b").textContent()).trim(), "¥2,000.00");
    assert.equal((await mockPanel.locator(".owner-item-latest strong").textContent()).trim(), "¥0.00");
    assert.equal(await mockPanel.locator(".owner-item-progress").count(), 0);
    assert.equal((await mockPanel.locator(".owner-item-amounts dd").first().textContent()).trim(), "¥8,000.00");
    assert(!(await mockPanel.textContent()).includes("演示员工"));
    assert.equal(await mockRow.locator("summary:visible").count(), 0);
    const syntheticLayouts = [];
    phase = "synthetic layouts";
    for (const theme of ["light", "dark"]) {
      await page.evaluate(theme => document.documentElement.dataset.theme = theme, theme);
    for (const width of [320, 375, 768, 1440]) {
      await page.setViewportSize({ width, height: 1000 }); await frames();
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `synthetic page overflows at ${width}`);
      assert(await mockPanel.evaluate(element => element.scrollWidth <= element.clientWidth + 1), `synthetic detail overflows at ${width}`);
      if (config.screenshot_directory) { fs.mkdirSync(config.screenshot_directory, { recursive: true }); await mockPanel.screenshot({ path: path.join(config.screenshot_directory, `detail-${theme}-${width}.png`), style: "body * { visibility: hidden !important; } .business-detail-panel, .business-detail-panel * { visibility: visible !important; }" }); }
      syntheticLayouts.push({ theme, width });
      assert(await mockRow.locator(".status").evaluate(element => {
        const probe = document.createElement("span"); probe.style.color = "var(--brief-green)"; element.append(probe);
        const ok = getComputedStyle(element).color === getComputedStyle(probe).color; probe.remove(); return ok;
      }));
    }
    }
    assert.equal(await mockPanel.getByRole("button", { name: "加载更多", exact: true }).count(), 0);
    phase = "synthetic continuation";
    await mockPanel.getByRole("button", { name: "重试读取", exact: true }).waitFor();
    assert.equal(await mockPanel.locator("ul > li").count(), 20);
    await mockPanel.getByRole("button", { name: "重试读取", exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll("#open-items .business-detail-panel ul > li").length === 21);
    assert.equal(continuationAttempts, 2);
    assert.equal(await mockRow.getAttribute("aria-expanded"), "true");
    assert(!(await mockPanel.textContent()).includes("本次筛选已全部加载"));
    assert.equal(await mockPanel.locator("ul > li").last().evaluate(element => getComputedStyle(element).borderBottomWidth), "0px");
    const cached = requests.length;
    await mockRow.focus(); await mockRow.press("Space"); await mockPanel.waitFor({ state: "hidden" });
    await mockRow.press("Enter"); await mockPanel.waitFor(); await frames(); assert.equal(requests.length, cached);
    phase = "status colors";
    for (const [status, token] of [["partial", "--brief-amber"], ["open", "--brief-amber"], ["settled", "--brief-green"], [null, "--brief-muted"]]) {
      synthetic.data.collections.open_items.items[0].current_status = status;
      const pending = reply("brief"); await page.getByRole("button", { name: "刷新数据", exact: true }).click(); await pending; await page.waitForFunction(() => document.querySelector(".module-header")?.getAttribute("aria-busy") === "false"); await frames();
      for (const theme of ["light", "dark"]) {
        await page.evaluate(theme => document.documentElement.dataset.theme = theme, theme);
        assert(await mockRow.locator(".status").evaluate((element, token) => {
          const probe = document.createElement("span"); probe.style.color = `var(${token})`; element.append(probe);
          const ok = getComputedStyle(element).color === getComputedStyle(probe).color; probe.remove(); return ok;
        }, token), `status ${status}/${theme}`);
      }
    }
    phase = "late open-item response";
    synthetic.data.collections.open_items.items[0].id = "synthetic-next-selection";
    const changedRow = reply("brief"); await page.getByRole("button", { name: "刷新数据", exact: true }).click(); await changedRow; await page.waitForFunction(() => document.querySelector(".module-header")?.getAttribute("aria-busy") === "false"); await frames();
    const readStarted = new Promise(resolve => pendingRead = resolve);
    delayed = true; await mockRow.focus(); await mockRow.press("Enter");
    await mockRow.locator(".business-detail-state").filter({ hasText: "正在读取" }).waitFor();
    await readStarted; assert(pendingRoute);
    synthetic.snapshot_version = "synthetic-changed-snapshot";
    const changedSnapshot = reply("brief"); await page.getByRole("button", { name: "刷新数据", exact: true }).click(); await changedSnapshot; await page.waitForFunction(() => document.querySelector(".module-header")?.getAttribute("aria-busy") === "false"); await frames();
    const canceledCount = requests.length; releasePending(); await frames();
    assert.equal(await mockRow.getAttribute("aria-expanded"), "false"); assert.equal(await mockPanel.count(), 0); assert.equal(requests.length, canceledCount);
    assert.equal(failures.length, 0);
    return { status: "passed", real_layouts: realLayouts, synthetic_layouts: syntheticLayouts, exact_obligation: true, keyboard: true, same_snapshot_reuse: true, refresh_without_details: true, continuation_retry: true, initial_retry: true, late_response: true, status_colors: true, related_records: 21, browser_errors: failures.length };
  } catch (error) { throw new Error(`${phase}: ${error.message}`); }
  finally { await browser.close(); }
}
if (require.main === module) {
  let input = ""; process.stdin.setEncoding("utf8"); process.stdin.on("data", chunk => input += chunk);
  process.stdin.on("end", async () => { try { process.stdout.write(JSON.stringify(await run(JSON.parse(input))) + "\n"); } catch (error) { process.stdout.write(JSON.stringify({ status: "failed", message: String(error.message).replace(/https?:\/\/[^\s"']+/g, "[URL]") }) + "\n"); process.exitCode = 1; } });
}
module.exports = { run };
