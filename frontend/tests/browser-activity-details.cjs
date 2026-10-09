// Read-only existing-book checks and browser-only synthetic responses.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

async function run(config) {
  const { chromium } = require(config.playwright_module);
  const { validateDashboardBusinessStatusResponse } = await import(pathToFileURL(path.join(__dirname, "../src/api/generated/dashboardBusinessStatus.js")));
  const { validateDashboardBriefResponse } = await import(pathToFileURL(path.join(__dirname, "../src/api/generated/dashboardBrief.js")));
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
  const requests = [], errors = [];
  page.on("request", request => { const url = new URL(request.url()); if (url.pathname.startsWith("/api/dashboard/")) requests.push(url); });
  page.on("pageerror", () => errors.push("browser script error"));
  const count = () => requests.filter(url => url.pathname.endsWith("business-status")).length;
  const frames = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const reply = action => page.waitForResponse(response => new URL(response.url()).pathname === `/api/dashboard/${action}`);
  const refresh = async () => { const pending = reply("brief"); await page.getByRole("button", { name: "刷新数据", exact: true }).click(); await pending; await page.waitForFunction(() => document.querySelector(".module-header")?.getAttribute("aria-busy") === "false"); await frames(); };
  const money = value => {
    if (value == null) return "待核对";
    const n = BigInt(value), abs = n < 0n ? -n : n;
    return `${n < 0n ? "−" : ""}¥${new Intl.NumberFormat("zh-CN").format(abs / 100n)}.${String(abs % 100n).padStart(2, "0")}`;
  };
  let phase = "real page";
  try {
    const address = new URL(config.ticket_url); address.searchParams.set("company_id", config.company_id); address.searchParams.set("period", config.period);
    const first = reply("brief"); await page.goto(address.href); const brief = await (await first).json();
    assert(validateDashboardBriefResponse(brief)); await page.locator(".activity-section").waitFor(); await frames(); assert.equal(count(), 0);
    const selected = brief.data.collections.activity.items[0]; assert(selected);
    const index = brief.data.activity_groups.findIndex(group => group.key === selected.group);
    await page.locator(".activity-section .index button").nth(index).click();
    const row = page.locator(".activity-section .event-row").first();
    const summary = row.locator(".business-detail-panel");
    const response = reply("business-status"); await row.focus(); await row.press("Enter");
    const realDetail = await (await response).json(); assert(validateDashboardBusinessStatusResponse(realDetail));
    await row.locator(".business-detail-panel:not(.owner-activity-summary)").waitFor();
    assert.equal((await row.locator(".event-money b").textContent()).trim(), money(selected.amount_fen));
    await row.press("Space"); await summary.waitFor({ state: "hidden" }); const readCount = count();
    await row.press("Enter"); await summary.waitFor(); await frames(); assert.equal(count(), readCount);
    const realLayouts = [];
    for (const width of [320, 375, 768, 1440]) {
      await page.setViewportSize({ width, height: 1000 }); await frames();
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `real overflow ${width}`);
      realLayouts.push(width);
    }
    await row.click({ position: { x: 12, y: 12 } }); await summary.waitFor({ state: "hidden" });
    await refresh(); assert.equal(count(), readCount, "refresh requested details");

    phase = "synthetic scenarios";
    const synthetic = structuredClone(brief), detail = structuredClone(realDetail);
    const subject = "synthetic-salary", later = config.period === "2026-12" ? "2027-01" : `${config.period.slice(0, 4)}-${String(Number(config.period.slice(5)) + 1).padStart(2, "0")}`;
    const makeRow = (key, amount, state) => ({ ...selected, key, voucher_version_id: key, subject_id: subject, party: "演示员工张某（长名称换行检查）", title: "工资", description: "已记录的工资事项与用途", state, amount_fen: amount, amount_label: "税前工资" });
    synthetic.data.collections.activity.items = [makeRow("synthetic-wage", "800000", "已入账"), makeRow("synthetic-correction", "-800000", "更正原业务")];
    synthetic.data.collections.activity.page = { total_count: 2, filtered_count: 2, returned_count: 2, has_more: false, next_cursor: null };
    synthetic.data.activity_count = 2;
    const voucher = brief.data.collections.vouchers.items.find(item => item.voucher_version_id === selected.voucher_version_id);
    assert(voucher);
    synthetic.data.collections.vouchers = { items: synthetic.data.collections.activity.items.map((item, n) => ({ ...voucher, voucher_version_id: item.voucher_version_id, subject_id: subject, number: String(n + 1), business_amount_fen: item.amount_fen, state: n ? "冲正" : "已入账", reverses_version_id: n ? "synthetic-wage" : null })), page: { ...synthetic.data.collections.activity.page } };
    synthetic.data.voucher_count = 2;
    synthetic.data.activity_groups = [{ ...brief.data.activity_groups[index], event_count: 2 }];
    const source = { key: "synthetic-net", name: "net", direction: "payable", category_key: "payroll_payables", source_period: config.period, source_amount_fen: "800000", paid_fen: "600000", other_settled_fen: "0", remaining_fen: "200000", settlement_status: "partial" };
    detail.data.identity.subject_id = subject; detail.data.identity.kind = "payroll"; detail.data.latest_source.deleted = false;
    const tax = { ...source, key: "synthetic-tax", name: "tax", source_amount_fen: "12345", paid_fen: "0", remaining_fen: "12345", settlement_status: "open" };
    detail.data.settlements = { cutoff_period: config.period, status: "established", checking: false, obligations: [source, tax] };
    detail.data.current_followups.settlements = { ...detail.data.settlements, cutoff_period: later, obligations: [{ ...source, remaining_fen: "0", paid_fen: "800000", settlement_status: "settled" }, tax] };
    const profile = (entity_id, display_name) => ({ entity_id, values: Object.fromEntries(["display_name", "display_number", "purpose", "note", "employment_start", "employment_end", "employment_status", "active", "category_label", "rights_description"].map(name => [name, name === "display_name" ? display_name : null])) });
    detail.data.display_profiles = { employees: [profile("synthetic-employee", "演示员工张某")], fund_accounts: [profile("synthetic-account", "演示工资支付账户")] };
    const event = n => ({ id: `synthetic-event-${n}`, subject_id: `payment-${n}`, source_subject_id: subject, posting_period: config.period, direction: n === 4 ? -1 : 1, signed_amount_fen: n === 4 ? "-10000" : "10000", relation_state: "resolved", kind: "payment", name: n % 2 ? "tax" : "net", purpose_label: n % 2 ? "个人所得税" : "实发工资", party: "演示员工张某", mode: ["payment", "offset", "advance", "accepted"][n % 4] });
    detail.data.collections.settlement_events = { items: Array.from({ length: 20 }, (_, n) => event(n)), page: { total_count: 21, filtered_count: 21, returned_count: 20, has_more: true, next_cursor: "synthetic-next" }, scope_period: config.period, current_cutoff_period: later, cutoff_semantics: "current_published_relations_independent_of_as_of" };
    assert(validateDashboardBriefResponse(synthetic)); assert(validateDashboardBusinessStatusResponse(detail));
    let initialAttempts = 0, continuationAttempts = 0, delayed = false, pendingRoute, releasePending;
    await page.route("**/api/dashboard/brief?*", route => route.fulfill({ json: synthetic }));
    await page.route("**/api/dashboard/business-status?*", async route => {
      const url = new URL(route.request().url()); assert.equal(url.searchParams.get("subject_id"), subject); assert.equal(url.searchParams.get("limit"), "20"); assert.equal(url.searchParams.get("expected_version"), detail.snapshot_version);
      if (url.searchParams.has("cursor")) {
        continuationAttempts++; assert.equal(url.searchParams.get("cursor"), "synthetic-next");
        if (continuationAttempts === 1) return route.fulfill({ status: 500, json: { code: "synthetic_retry", message: "合成续页失败，请重试" } });
        const next = structuredClone(detail); next.data.collections.settlement_events.items = [event(20)]; next.data.collections.settlement_events.page = { total_count: 21, filtered_count: 21, returned_count: 1, has_more: false, next_cursor: null };
        return route.fulfill({ json: next });
      }
      initialAttempts++;
      if (initialAttempts === 1) return route.fulfill({ status: 500, json: { code: "synthetic_initial", message: "合成首次读取失败，请重试" } });
      if (delayed) { pendingRoute = route; const stale = structuredClone(detail); await new Promise(resolve => releasePending = resolve); try { await route.fulfill({ json: stale }); } catch { /* An aborted read is expected. */ } return; }
      return route.fulfill({ json: detail });
    });
    phase = "synthetic refresh"; await refresh(); const mockRow = page.locator(".activity-section .event-row").first(), mockSummary = mockRow.locator(".event-money b"), panel = mockRow.locator(".business-detail-panel:not(.owner-activity-summary)");
    phase = "synthetic initial expansion"; const initialCount = count(); await mockRow.focus(); await mockRow.press("Enter");
    await mockRow.getByRole("button", { name: "重新读取", exact: true }).waitFor();
    assert.equal((await mockSummary.textContent()).trim(), "¥8,000.00");
    await mockRow.getByRole("button", { name: "重新读取", exact: true }).click(); await panel.waitFor();
    await panel.getByRole("button", { name: "重试读取", exact: true }).waitFor(); assert.equal(count(), initialCount + 3); assert.equal(await mockRow.getAttribute("aria-expanded"), "true");
    assert.equal((await panel.locator(".owner-activity-obligation .owner-item-balance strong").first().textContent()).trim(), "¥2,000.00");
    assert.equal(await panel.locator(".owner-activity-summary").count(), 0);
    const layouts = [];
    for (const theme of ["light", "dark"]) {
      await page.evaluate(theme => document.documentElement.dataset.theme = theme, theme);
    for (const width of [320, 375, 768, 1440]) {
      await page.setViewportSize({ width, height: 1000 }); await frames();
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `mock overflow ${width}`);
      assert(await panel.evaluate(element => element.scrollWidth <= element.clientWidth + 1));
      if (config.screenshot_directory) { fs.mkdirSync(config.screenshot_directory, { recursive: true }); await mockRow.screenshot({ path: path.join(config.screenshot_directory, `activity-${theme}-${width}.png`), style: "body * { visibility: hidden !important; } .event-row, .event-row * { visibility: visible !important; }" }); }
      layouts.push({ theme, width });
      assert(await panel.locator(".business-state.pending").first().evaluate(element => {
        const probe = document.createElement("span"); probe.style.color = "var(--warning)"; element.append(probe);
        const ok = getComputedStyle(element).color === getComputedStyle(probe).color; probe.remove(); return ok;
      }), `pending color ${theme}/${width}`);
      assert(await panel.locator(".owner-item-latest").evaluate(element => {
        const probe = document.createElement("span"); probe.style.background = "var(--surface)"; element.append(probe);
        const ok = getComputedStyle(element).backgroundColor === getComputedStyle(probe).backgroundColor; probe.remove(); return ok;
      }), `latest background ${theme}/${width}`);
    }
    }
    assert.equal(await panel.locator(".owner-item-latest").count(), 1, "unchanged tax repeated in followups");
    assert.equal(await panel.locator(".owner-item-latest .business-state.settled").count(), 1);
    assert(await panel.locator(".business-state.settled").evaluate(element => {
      const probe = document.createElement("span"); probe.style.color = "var(--accent)"; element.append(probe);
      const ok = getComputedStyle(element).color === getComputedStyle(probe).color; probe.remove(); return ok;
    }));
    assert.equal(await panel.getByRole("button", { name: "加载更多", exact: true }).count(), 0); await panel.getByRole("button", { name: "重试读取", exact: true }).waitFor(); assert.equal(await panel.locator("ul > li").count(), 20);
    await panel.getByRole("button", { name: "重试读取", exact: true }).click(); await page.waitForFunction(() => document.querySelectorAll(".activity-section .business-detail-panel ul > li").length === 21);
    assert.equal(await mockRow.getAttribute("aria-expanded"), "true"); assert.equal(continuationAttempts, 2);
    assert(!(await panel.textContent()).includes("本次筛选已全部加载"));
    assert.equal(await panel.locator(".dashboard-pagination").count(), 0);
    assert.equal(await panel.locator("ul > li").last().evaluate(element => getComputedStyle(element).borderBottomWidth), "0px");
    phase = "synthetic cached expansion"; await mockRow.focus(); await mockRow.press("Space"); const cachedCount = count(); await mockRow.press("Enter"); await panel.waitFor(); await frames(); assert.equal(count(), cachedCount);
    phase = "synthetic correction"; const other = page.locator(".activity-section .event-row").nth(1); await other.focus(); await other.press("Enter"); await other.locator(".business-detail-panel:not(.owner-activity-summary)").waitFor();
    assert.equal((await other.locator(".event-money b").textContent()).trim(), "−¥8,000.00"); assert((await other.locator(".state").first().textContent()).includes("更正原业务"));
    for (const theme of ["light", "dark"]) {
      await page.evaluate(theme => document.documentElement.dataset.theme = theme, theme);
      assert(await other.locator(".state.correction").first().evaluate(element => {
        const probe = document.createElement("span"); probe.style.color = "var(--muted)"; element.append(probe);
        const ok = getComputedStyle(element).color === getComputedStyle(probe).color; probe.remove(); return ok;
      }));
    }
    phase = "late response protection";
    synthetic.data.collections.activity.items[0].key = "synthetic-new-occurrence";
    await refresh(); delayed = true; await mockRow.focus(); await mockRow.press("Enter");
    await page.waitForFunction(() => document.querySelector(".activity-section .business-detail-state")?.textContent.includes("正在读取"));
    synthetic.snapshot_version = "synthetic-changed-snapshot";
    await refresh(); const afterCancel = count(); assert(pendingRoute); releasePending(); await frames();
    assert.equal(await mockRow.getAttribute("aria-expanded"), "false"); assert.equal(await mockRow.locator(".business-detail-panel").count(), 0); assert.equal(count(), afterCancel);
    assert.equal(errors.length, 0);
    return { status: "passed", real_layouts: realLayouts, synthetic_layouts: layouts, default_details: 0, first_expand_requests: 1, initial_retry_requests: 1, cached_expand_requests: 0, paging_failure_requests: 1, paging_retry_requests: 1, refresh_detail_requests: 0, late_response: true, exact_occurrence: true, keyboard: true, related_records: 21, browser_errors: 0 };
  } catch (error) { throw new Error(`${phase}: ${error.message}`); }
  finally { await browser.close(); }
}
if (require.main === module) {
  let input = ""; process.stdin.setEncoding("utf8"); process.stdin.on("data", chunk => input += chunk);
  process.stdin.on("end", async () => { try { process.stdout.write(JSON.stringify(await run(JSON.parse(input))) + "\n"); } catch (error) { process.stdout.write(JSON.stringify({ status: "failed", message: String(error.message).replace(/https?:\/\/[^\s"']+/g, "[URL]") }) + "\n"); process.exitCode = 1; } });
}
module.exports = { run };
