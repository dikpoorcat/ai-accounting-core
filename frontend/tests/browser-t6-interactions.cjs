// Current ordinary build + actual loopback API; synthetic data, no response fixtures.
const assert = require("node:assert/strict");
const { readFileSync, writeFileSync } = require("node:fs");
const { createHash } = require("node:crypto");
const { join } = require("node:path");

const sanitize = value => String(value).replace(/https?:\/\/[^\s"']+/g, "[browser URL]");
const money = value => {
  if (value === null) return "暂无法确定";
  const amount = BigInt(value), absolute = amount < 0n ? -amount : amount;
  return `${amount < 0n ? "−" : ""}¥${new Intl.NumberFormat("zh-CN").format(absolute / 100n)}.${String(absolute % 100n).padStart(2, "0")}`;
};

async function run(config) {
  const { chromium } = require(config.playwright_module);
  const browser = await chromium.launch({ channel: config.channel, headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  const errors = [], apiFailures = [], scripts = new Map(), hashChecks = [], screenshots = [], checks = [];
  let deliberateFailure = false;
  const expectedErrors = [];
  page.on("pageerror", error => errors.push(sanitize(error.message)));
  page.on("console", message => {
    if (message.type() === "error") (deliberateFailure ? expectedErrors : errors).push(sanitize(message.text()));
  });
  page.on("response", response => {
    const target = new URL(response.url());
    if (target.pathname.endsWith(".js")) {
      hashChecks.push((async () => {
        assert.equal(target.origin, config.origin);
        const name = target.pathname.slice(1);
        const actual = createHash("sha256").update(await response.body()).digest("hex");
        assert.equal(actual, config.assets[name], `Actual browser script differs: ${name}`);
        scripts.set(name, actual);
      })().catch(error => ({ error: sanitize(error.message) })));
    }
    if (target.pathname.startsWith("/api/dashboard/") && response.status() >= 400) {
      apiFailures.push({ path: target.pathname, status: response.status(), deliberate: deliberateFailure });
    }
  });
  const [first, second] = config.companies;
  const pages = [
    { key: "brief", path: "/", action: "brief", heading: "月度经营与财务概览" },
    { key: "funds", path: "/funds", action: "funds", heading: "资金总览" },
    { key: "employees", path: "/employees", action: "employees", heading: "员工与薪酬概览" },
    { key: "assets", path: "/assets", action: "assets", heading: "长期资产概览" },
    { key: "reports", path: "/reports", action: "quarterly-report", heading: "季度财务报表" },
  ];
  const url = (path, company = first.id, period = "2026-09", extra = "") => `${config.origin}${path}?company_id=${company}&period=${period}${extra}`;
  const api = (action, predicate = () => true) => {
    const pending = page.waitForResponse(response => {
      const target = new URL(response.url());
      return target.pathname === `/api/dashboard/${action}` && response.status() === 200 && predicate(target);
    });
    // A UI assertion can fail before its paired response is awaited; keep that failure reportable.
    void pending.catch(() => {});
    return pending;
  };
  async function ready(item) {
    await page.getByRole("heading", { name: item.heading, exact: true }).waitFor();
    await page.getByRole("button", { name: "刷新数据", exact: true }).waitFor({ state: "visible" });
    await page.waitForFunction(() => !document.querySelector('.module-header[aria-busy="true"]'));
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  }

  async function navigationLayout(width) {
    assert.equal(await page.locator(".module-header .eyebrow, .module-header .description").count(), 0);
    const geometry = await page.locator(".section-nav").evaluate(nav => {
      const box = element => { const r = element.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width }; };
      const header = document.querySelector(".module-header");
      return { nav: box(nav), parent: box(nav.parentElement), heading: box(header.querySelector(".module-heading")), toolbar: box(header.querySelector(".toolbar")), header: box(header), floating: nav.dataset.floating === "true" };
    });
    assert(Math.abs((geometry.nav.left + geometry.nav.right - geometry.parent.left - geometry.parent.right) / 2) < 2, "Navigation remains centered in the page");
    if (width < 1280 || !geometry.floating) assert(geometry.nav.top >= geometry.header.bottom, "Flow navigation follows the complete header");
    else {
      assert(geometry.nav.right <= geometry.toolbar.left - 15 && geometry.nav.left >= geometry.heading.right + 15, "Floating navigation cannot cover heading or toolbar");
      assert(geometry.nav.top <= 12, "Floating navigation starts at the page top");
    }
    if (width === 1440) assert(geometry.floating, "The five standard desktop headers fit the floating navigation");
  }
  async function dashboard(item, company = first.id, period = "2026-09", extra = "") {
    const pending = api(item.action, target => target.searchParams.get("company_id") === company);
    await page.goto(url(item.path, company, period, extra));
    const response = await (await pending).json();
    await ready(item);
    assert.equal(await page.getByLabel("切换公司", { exact: true }).inputValue(), company);
    assert.equal(response.schema_version, item.key === "reports" ? 1 : 2);
    return response;
  }
  async function screenshot(name, top = true) {
    if (top) await page.evaluate(() => window.scrollTo(0, 0));
    const overflow = await page.evaluate(() => ({
      width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
      offenders: [...document.querySelectorAll("main *")].filter(element => {
        const rect = element.getBoundingClientRect();
        return rect.width > 0 && rect.right > innerWidth + 1 && !element.closest('.table-scroll, .table-wrap, .table-container, .report-table-wrap');
      }).slice(0, 8).map(element => ({ tag: element.tagName, class: element.className })),
    }));
    await page.screenshot({ path: join(config.output, name) });
    screenshots.push(name);
    assert(overflow.scrollWidth <= overflow.width + 1, `${name}: document overflow ${JSON.stringify(overflow)}`);
  }
  async function expand(item) {
    if (item.key === "employees") await page.locator("summary.employee-card-summary").first().click();
    else if (item.key === "assets") await page.locator("summary.asset-card-summary").first().click();
    else if (item.key === "reports") {
      await page.getByRole("tablist", { name: "季度财务报表", exact: true }).getByRole("tab").first().click();
      await page.locator("#report-full").evaluate(element => element.scrollIntoView({ block: "start" }));
    }
    else if (item.key === "funds") {
      const detail = page.locator("#fund-detail-panel-book summary").first();
      if (await detail.count()) await detail.click();
      else await page.locator("#funds-attention summary").first().click();
    } else {
      const detail = page.locator("details.brief-section").first();
      const pending = await detail.locator(".dashboard-pagination").count() ? null : api("brief", target => target.searchParams.get("section") === "businesses");
      await detail.locator(":scope > summary").click();
      if (pending) await pending;
      await page.waitForFunction(() => !document.querySelector('details.brief-section[open] .dashboard-pagination[aria-busy="true"]'));
    }
  }
  try {
    const ticket = new URL(config.ticket_url);
    const reportCompanyDefault = config.scenario === "report-company-default";
    const initialReport = reportCompanyDefault ? api("quarterly-report", target => target.searchParams.get("company_id") === first.id) : null;
    if (reportCompanyDefault) ticket.pathname = "/reports";
    ticket.search = reportCompanyDefault
      ? `?company_id=${first.id}&period=2026-01&quarter=2026-Q1`
      : `?company_id=${first.id}&period=2026-09`;
    await page.goto(ticket.href);
    await ready(reportCompanyDefault ? pages[4] : pages[0]);
    assert.equal(new URL(page.url()).hash, "");
    assert((await context.cookies()).some(cookie => cookie.name === "finance_session" && cookie.httpOnly));
    checks.push("one-use ticket consumed before router; HttpOnly session");

    if (reportCompanyDefault) {
      const firstReport = await (await initialReport).json();
      assert.equal(firstReport.period.year, 2026);
      assert.equal(firstReport.period.quarter, 1);
      assert.equal(await page.getByLabel("切换公司", { exact: true }).inputValue(), first.id);
      assert.equal(await page.getByLabel("季度报表期间", { exact: true }).inputValue(), "2026-Q1");
      assert.equal(new URL(page.url()).searchParams.get("period"), "2026-01");
      await screenshot("report-company-a-q1.png");

      const targetContext = api("context", target => target.searchParams.get("company_id") === second.id);
      const targetReport = api("quarterly-report", target => target.searchParams.get("company_id") === second.id && target.searchParams.get("quarter") === "2");
      await page.getByLabel("切换公司", { exact: true }).selectOption(second.id);
      const [companyContext, companyReport] = await Promise.all([
        targetContext.then(response => response.json()),
        targetReport.then(response => response.json()),
      ]);
      await ready(pages[4]);
      assert.deepEqual(companyContext.periods.map(item => item.key), ["2026-06", "2026-03"]);
      assert.equal(companyContext.default_period, "2026-06");
      assert.equal(companyContext.default_quarter, "2026-Q2");
      const selectedUrl = new URL(page.url());
      assert.equal(selectedUrl.pathname, "/reports");
      assert.equal(selectedUrl.searchParams.get("company_id"), companyContext.current_company.company_id);
      assert.equal(selectedUrl.searchParams.get("period"), companyContext.default_period);
      assert.equal(selectedUrl.searchParams.get("quarter"), companyContext.default_quarter);
      assert.equal(companyContext.current_company.name, second.name);
      assert.equal(await page.getByLabel("切换公司", { exact: true }).inputValue(), second.id);
      assert.equal((await page.getByLabel("切换公司", { exact: true }).locator("option:checked").textContent()).trim(), companyContext.current_company.name);
      assert.equal(await page.getByLabel("季度报表期间", { exact: true }).inputValue(), "2026-Q2");
      assert.equal(companyReport.period.year, 2026);
      assert.equal(companyReport.period.quarter, 2);
      assert.equal(companyReport.period.quarter_end, "2026-06-30");
      assert.equal(companyReport.organization.taxpayer_identification_number, second.taxpayer_id);
      const notice = `所选月份已不可查看，已切换至 ${companyContext.default_period}。`;
      await page.getByText(notice, { exact: true }).waitFor();
      await screenshot("report-company-b-default-q2.png");
      const assetResults = await Promise.all(hashChecks);
      assert(scripts.size > 0);
      assert(!assetResults.some(result => result?.error), JSON.stringify(assetResults.filter(Boolean)));
      assert.deepEqual(errors, []);
      assert.deepEqual(apiFailures, []);
      return {
        status: "passed", scenario: "report-company-default", actual_same_origin_api: true,
        checks: [...checks, "Three typed synthetic expense facts only", "A January/Q1 report switches to B June/Q2 default despite B also having March/Q1", "Company and quarter controls, fallback notice, URL and real context/report API agree", "All actually requested JS response bodies match the final ordinary build"],
        observations: {
          from: { company: first.name, period: "2026-01", quarter: "2026-Q1" },
          to: { company: companyContext.current_company.name, periods: companyContext.periods.map(item => item.key), default_period: companyContext.default_period, selected_period: selectedUrl.searchParams.get("period"), selected_quarter: selectedUrl.searchParams.get("quarter"), api_quarter: companyReport.period.quarter, notice },
        },
        screenshots, loaded_scripts_sha256: Object.fromEntries(scripts),
        page_errors: errors, api_failures: apiFailures,
        limitation: "Bounded report-company regression; no five-page matrix or Funds unknown-state browser claim.",
      };
    }

    if (config.scenario === "navigation") {
      await dashboard(pages[0]);
      await page.getByRole("navigation", { name: "经营简报区段", exact: true }).getByRole("button", { name: "业务凭证", exact: true }).click();
      const pagination = page.locator("#activity .dashboard-pagination");
      await pagination.scrollIntoViewIfNeeded();
      await screenshot("navigation-pagination-before.png", false);
      const before = await page.evaluate(() => ({ scroll: scrollY, targetTop: document.querySelector('#activity .dashboard-pagination').getBoundingClientRect().top }));
      const continuation = api("brief", target => target.searchParams.has("cursor"));
      await pagination.getByRole("button", { name: "加载更多", exact: true }).click();
      await (await continuation).json();
      await page.waitForFunction(() => !document.querySelector('#activity .dashboard-pagination[aria-busy="true"]'));
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      const after = await page.evaluate(() => ({ scroll: scrollY, targetTop: document.querySelector('#activity .dashboard-pagination').getBoundingClientRect().top }));
      await screenshot("navigation-pagination-after.png", false);
      await page.getByText("查看相关款项、外部办理与文件任务", { exact: true }).click();
      const fileJobs = api("brief", target => target.searchParams.get("section") === "file_jobs");
      await page.getByRole("link", { name: "查看任务明细", exact: true }).click();
      await (await fileJobs).json();
      await page.waitForFunction(() => !document.querySelector('#brief-file_jobs .dashboard-pagination[aria-busy="true"]'));
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      const fileTarget = await page.evaluate(() => ({
        scroll: scrollY, targetTop: document.querySelector('#brief-file_jobs').getBoundingClientRect().top,
        targetBottom: document.querySelector('#brief-file_jobs').getBoundingClientRect().bottom,
        height: innerHeight, open: document.querySelector('#brief-file_jobs').open,
        hash: location.hash, activeElement: document.activeElement?.tagName,
      }));
      await screenshot("navigation-file-target.png", false);
      const observations = { pagination: { before, after, jumpedBack: after.scroll < before.scroll - 200 }, fileTarget };
      const filesVisible = fileTarget.open && fileTarget.targetTop < fileTarget.height && fileTarget.targetBottom > 0;
      for (const width of [1440, 390]) {
        await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
        await dashboard(pages[0]);
        await screenshot(`brief-${width}-default.png`);
        if (width === 390) assert.equal(await page.locator(".kpi-grid > .kpi").evaluateAll(elements => new Set(elements.map(element => Math.round(element.getBoundingClientRect().top))).size), 4, "Mobile Brief should show its four cards in one column");
        await expand(pages[0]);
        await screenshot(`brief-${width}-expanded.png`, false);
        await dashboard(pages[1]);
        await screenshot(`funds-${width}-default.png`);
        await expand(pages[1]);
        await screenshot(`funds-${width}-expanded.png`, false);
        await dashboard(pages[4]);
        await screenshot(`reports-${width}-default.png`);
        await page.locator(".monthly-preparations").evaluate(element => element.scrollIntoView({ block: "start" }));
        const months = page.locator(".monthly-preparations > details.monthly-preparation");
        assert.equal(await months.count(), 3);
        assert.equal(await page.locator(".monthly-preparations > details[open]").count(), 0);
        await screenshot(`reports-${width}-monthly-preparations.png`, false);
        await months.first().locator(":scope > summary").click();
        assert(await months.first().locator(".period-preparation").isVisible());
        await months.first().locator(":scope > summary").click();
        await expand(pages[4]);
        assert(await page.locator("#report-full table").first().isVisible());
        await screenshot(`reports-${width}-table-expanded.png`, false);
      }
      await page.setViewportSize({ width: 320, height: 800 });
      await dashboard(pages[0]);
      await screenshot("brief-320-default.png");
      assert.equal(await page.locator(".kpi-grid > .kpi").evaluateAll(elements => new Set(elements.map(element => Math.round(element.getBoundingClientRect().top))).size), 4);
      await page.setViewportSize({ width: 1440, height: 1000 });
      for (const item of [pages[2], pages[3]]) await dashboard(item);
      const assetResults = await Promise.all(hashChecks);
      assert(!assetResults.some(result => result?.error), JSON.stringify(assetResults.filter(Boolean)));
      assert.deepEqual(errors, []);
      assert.deepEqual(apiFailures, []);
      return {
        status: observations.pagination.jumpedBack || !filesVisible ? "failed" : "passed",
        message: observations.pagination.jumpedBack || !filesVisible ? "Brief data merge moved scroll away from the active pagination or requested file section" : "Navigation retained intended target",
        checks: [...checks, "Brief pagination retains scroll and file deep link remains visible", "Brief four-card single-column layout at 390 and 320", "Brief/Funds/Reports default and expanded affected views at 1440 and 390", "Three report months collapsed with real preparation reachable; full report table visible", "Employees and Assets actual API/DOM smoke binds final loaded JS; prior visual evidence reused", "All fetched JS response bodies match current ordinary dist"],
        observations, screenshots, page_errors: errors, api_failures: apiFailures,
        loaded_scripts_sha256: Object.fromEntries(scripts),
      };
    }

    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      for (const item of pages) {
        const response = await dashboard(item);
        await navigationLayout(width);
        if (item.key === "brief") {
          const cards = page.getByRole("region", { name: "本月核心指标", exact: true });
          const cardText = await cards.innerText();
          for (const value of [response.data.position.month_result_fen, response.data.funds_overview.total_fen, response.data.open_items.receivable_fen, response.data.open_items.payable_fen]) assert(cardText.includes(money(value)), `Core card must match full API amount ${value}`);
          assert(await page.evaluate(() => {
            const overview = document.querySelector("#overview");
            const metrics = document.querySelector(".kpi-grid");
            return !!overview && !!metrics && !!(overview.compareDocumentPosition(metrics) & Node.DOCUMENT_POSITION_FOLLOWING);
          }), "Operating overview must precede the four metric cards");
          assert.equal(await page.locator('.module-header [aria-label="切换公司"]').count(), 0);
          assert.equal(await page.locator('.company-switcher [aria-label="切换公司"]').count(), 1);
          assert.equal(await page.getByText("财务看板", { exact: true }).count(), 0);
          assert.equal(await page.getByText("了解本月赚亏、资金去向和待收待付。", { exact: true }).count(), 0);
          assert.equal(await page.getByText("以上为所选月末完整汇总。待收包含预付款待冲抵等事项，不代表预计或到期现金收付。", { exact: true }).count(), 0);
          if (width === 1440) {
            assert(await page.locator(".month-picker").isVisible());
            const navBox = await page.locator(".section-nav.floating").boundingBox();
            assert(navBox && navBox.y <= 12, "Wide Brief navigation should float in the header space");
          } else {
            assert(await page.locator(".company-switcher").isVisible());
            assert(await page.locator(".month-picker").isHidden());
          }
        }
        if (item.key === "employees") assert(response.data.employees.items.length > 0);
        if (item.key === "assets") assert(response.data.collections.assets.items.length > 0);
        await screenshot(`${item.key}-${width}-default.png`);
        await expand(item);
        await screenshot(`${item.key}-${width}-expanded.png`, false);
        const lastLink = page.locator(".section-nav button").last();
        await lastLink.focus();
        await page.keyboard.press("Enter");
        const anchor = await page.evaluate(() => {
          const target = document.activeElement?.getBoundingClientRect();
          const nav = document.querySelector(".section-nav").getBoundingClientRect();
          return { targetTop: target?.top, targetBottom: target?.bottom, navBottom: nav.bottom, selected: document.querySelector('.section-nav button[aria-current="location"]')?.textContent.trim() };
        });
        assert.equal(anchor.selected, (await lastLink.innerText()).trim());
        assert(anchor.targetTop >= anchor.navBottom - 1 && anchor.targetTop < (width === 390 ? 780 : 1000), "Keyboard target stays visible below the sticky navigation");
        await page.mouse.wheel(0, -1);
        await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
        await page.waitForFunction(() => document.querySelector('.section-nav button')?.getAttribute("aria-current") === "location");
      }
    }
    checks.push("five pages: default and expanded DOM/screenshots at 1440 and 390");
    await dashboard(pages[0]);
    await page.locator("#overview").scrollIntoViewIfNeeded();
    await screenshot("brief-390-operating-conclusion.png", false);

    await page.setViewportSize({ width: 1440, height: 1000 });
    for (const item of pages) {
      await dashboard(item);
      const changed = api(item.action, target => target.searchParams.get("company_id") === second.id);
      await page.getByLabel("切换公司", { exact: true }).selectOption(second.id);
      await changed; await ready(item);
      assert.equal(new URL(page.url()).searchParams.get("company_id"), second.id);
      if (item.key === "employees") assert.equal(await page.getByText(first.employee_name, { exact: true }).count(), 0);
      if (item.key === "assets") assert.equal(await page.getByText(first.asset_name, { exact: true }).count(), 0);
      const returned = api(item.action, target => target.searchParams.get("company_id") === first.id);
      await page.getByLabel("切换公司", { exact: true }).selectOption(first.id);
      await returned; await ready(item);
      const periodControl = page.locator(".module-header select").filter({ has: page.locator('option[value="2026-10"], option[value="2026-Q4"]') });
      const next = item.key === "reports" ? "2026-Q4" : "2026-10";
      const previous = item.key === "reports" ? "2026-Q3" : "2026-09";
      const nextRead = api(item.action);
      await periodControl.selectOption(next); await nextRead; await ready(item);
      assert.equal(new URL(page.url()).searchParams.get("period"), "2026-10");
      const previousRead = api(item.action);
      await periodControl.selectOption(previous); await previousRead; await ready(item);
      assert.equal(new URL(page.url()).searchParams.get("period"), "2026-09");
    }
    checks.push("five pages company A-B-A and September-October-September (report quarter backfill)");

    const brief = await dashboard(pages[0]);
    await page.getByRole("button", { name: "收起侧边栏", exact: true }).click();
    assert(await page.locator(".company-switcher").isHidden());
    assert(await page.locator(".month-picker").isHidden());
    await page.getByRole("button", { name: "展开侧边栏", exact: true }).click();
    assert(await page.locator(".company-switcher").isVisible());
    assert(await page.locator(".month-picker").isVisible());
    checks.push("restored sidebar company and month controls hide and return with desktop collapse");
    assert.equal(brief.data.vouchers.length, 100);
    assert.equal(brief.data.voucher_count, first.voucher_count);
    let continuation = brief;
    while (continuation.data.collections.vouchers.page.has_more) {
      const next = api("brief", target => target.searchParams.has("cursor"));
      await page.locator("#activity").getByRole("button", { name: "加载更多", exact: true }).click();
      continuation = await (await next).json();
      assert.equal(continuation.snapshot_version, brief.snapshot_version);
    }
    await page.getByText(`已加载 ${first.voucher_count} / ${first.voucher_count} 张凭证；本页汇总按全月计算。`, { exact: true }).waitFor();
    const focused = await dashboard(pages[0], first.id, "2026-09", `&voucher=${first.target_number}`);
    assert.equal(focused.data.focused_voucher.voucher_version_id, first.target_version_id);
    assert(!focused.data.vouchers.some(item => item.voucher_version_id === first.target_version_id));
    await page.locator("#activity").getByText("精确定位的凭证", { exact: true }).waitFor();
    checks.push(`actual ${first.voucher_count} vouchers: continuation snapshot and off-page numeric deep link`);

    await dashboard(pages[1]);
    const accounts = api("funds", target => target.searchParams.get("section") === "accounts");
    await page.getByRole("button", { name: "继续加载账户选项", exact: true }).first().click();
    await accounts;
    const filtered = api("funds", target => target.searchParams.get("movement_account_id") === first.last_account_id);
    await page.getByLabel("筛选账面资金账户", { exact: true }).selectOption(`bank:${first.last_account_id}`);
    const filteredResponse = await (await filtered).json();
    assert(filteredResponse.data.movements.every(item => item.account_id === first.last_account_id));
    await page.waitForFunction(name => document.querySelector('[aria-label="筛选账面资金账户"]')?.selectedOptions[0]?.textContent.includes(name), first.last_account_name);
    const firstSummary = page.locator("#fund-detail-panel-book summary").first();
    if (await firstSummary.count()) await firstSummary.click();
    const voucherLink = page.locator('#fund-detail-panel-book a[href*="voucher="]').first();
    const destination = new URL(await voucherLink.getAttribute("href"), config.origin);
    assert.equal(destination.searchParams.get("period"), filteredResponse.selected_period.key);
    await voucherLink.click(); await ready(pages[0]);
    const back = api("funds", target => target.searchParams.get("movement_account_id") === first.last_account_id);
    await page.goBack(); await back; await ready(pages[1]);
    assert.equal(await page.getByLabel("筛选账面资金账户", { exact: true }).inputValue(), `bank:${first.last_account_id}`);
    checks.push("off-page account name survives filter; voucher uses posting period; browser back restores filter with fresh API");

    await dashboard(pages[3]);
    await expand(pages[3]);
    deliberateFailure = true;
    const sourceRoute = "**/api/dashboard/assets?**";
    const failSource = async route => {
      const target = new URL(route.request().url());
      if (target.searchParams.get("section") === "source_history") await route.abort("failed");
      else await route.continue();
    };
    await page.route(sourceRoute, failSource);
    await page.getByText("查看本项资产的来源历史", { exact: true }).click();
    const retry = page.getByRole("button", { name: "重新读取", exact: true }).first();
    await retry.waitFor();
    assert(await page.getByText(first.asset_name, { exact: true }).first().isVisible());
    const independent = api("assets", target => target.searchParams.get("section") === "settlement_events");
    await page.getByText("当前后续事项 · 关联清偿事件", { exact: true }).click();
    await independent;
    assert(await retry.isVisible(), "Independent settlement detail must not replace the source failure");
    await screenshot("assets-local-failure.png", false);
    await page.unroute(sourceRoute, failSource);
    const recovered = api("assets", target => target.searchParams.get("section") === "source_history");
    await retry.click(); await recovered;
    await retry.waitFor({ state: "hidden" });
    deliberateFailure = false;
    checks.push("deliberate source failure retains asset; independent settlement detail loads; local retry recovers via real API");

    await page.setViewportSize({ width: 320, height: 800 });
    await page.locator("summary.asset-card-summary").first().scrollIntoViewIfNeeded();
    await screenshot("assets-320-long-expanded.png", false);
    for (const width of [1280, 1279, 1080, 900]) {
      await page.setViewportSize({ width, height: 900 });
      for (const item of pages) {
        await dashboard(item);
        await navigationLayout(width);
        assert.equal(await page.locator(".month-picker").isVisible(), width > 900);
        assert(await page.locator(".company-switcher").isVisible());
        await screenshot(`${item.key}-${width}-default.png`);
      }
    }
    for (const item of pages) {
      await page.setViewportSize({ width: 1440, height: 1000 });
      await dashboard(item);
      await page.getByRole("button", { name: "切换深色外观", exact: true }).click();
      await screenshot(`${item.key}-1440-dark.png`);
      await page.getByRole("button", { name: "切换浅色外观", exact: true }).click();
    }
    for (const [item, hash] of [[pages[1], "fund-accounts"], [pages[2], "employee-list-title"], [pages[3], "asset-list-title"], [pages[4], "report-statements"]]) {
      await dashboard(item, first.id, "2026-09", `#${hash}`);
      await page.waitForFunction(id => document.activeElement?.id === id, hash);
      const position = await page.locator(`#${hash}`).boundingBox();
      const navigation = await page.locator(".section-nav").boundingBox();
      assert(position.y >= navigation.y + navigation.height - 1 && position.y < 900, `Asynchronous #${hash} remains visible after floating layout`);
    }
    await page.setViewportSize({ width: 900, height: 900 });
    await dashboard(pages[0]);
    assert(await page.locator(".company-switcher").isVisible());
    assert(await page.locator(".month-picker").isHidden());
    await screenshot("brief-900-default.png");
    await page.getByRole("button", { name: "切换深色外观", exact: true }).click();
    await screenshot("brief-900-dark.png");
    await page.getByRole("button", { name: "切换浅色外观", exact: true }).click();
    const navigation = page.locator(".section-nav button").last();
    await navigation.focus(); await page.keyboard.press("Enter");
    await page.waitForFunction(() => document.activeElement?.matches("h2, h3, [tabindex='-1']"));
    checks.push("representative 1279 document-flow navigation, 900 shell, 320 long content, dark theme, keyboard section focus");

    for (const path of ["/index.html", "/local.html"]) await dashboard({ ...pages[0], path });
    const refreshed = api("brief");
    await page.getByRole("button", { name: "刷新数据", exact: true }).click();
    await refreshed; await ready(pages[0]);
    const assetResults = await Promise.all(hashChecks);
    assert(!assetResults.some(result => result?.error), JSON.stringify(assetResults.filter(Boolean)));
    assert(scripts.size > 0);
    assert.deepEqual(errors, []);
    assert.deepEqual(apiFailures, []);
    checks.push("both HTML entries, explicit refresh, actual fetched JS SHA256 matches ordinary dist");
    return {
      status: "passed", actual_same_origin_api: true, actual_source_build_assets: true,
      checks, screenshots, loaded_scripts_sha256: Object.fromEntries(scripts),
      page_errors: errors, api_failures: apiFailures, deliberate_network_failure_console: expectedErrors,
      limitation: "Synthetic browser coverage; unknown/frozen/preview-export version semantics also rely on targeted frontend contracts. P3 server disconnect stderr remains separately preserved.",
    };
  } catch (error) {
    await page.screenshot({ path: join(config.output, "failure.png"), fullPage: true }).catch(() => {});
    writeFileSync(join(config.output, "failure-dom.txt"), sanitize(await page.locator("body").innerText().catch(() => "DOM unavailable")));
    return { status: "failed", message: sanitize(error.message), checks, screenshots, page_errors: errors, api_failures: apiFailures, loaded_scripts_sha256: Object.fromEntries(scripts) };
  } finally { await context.close(); await browser.close(); }
}

if (require.main === module) {
  const config = JSON.parse(readFileSync(0, "utf8"));
  run(config).then(result => { process.stdout.write(JSON.stringify(result)); if (result.status !== "passed") process.exitCode = 1; }, error => {
    process.stdout.write(JSON.stringify({ status: "failed", message: sanitize(error.message) })); process.exitCode = 1;
  });
}
module.exports = run;
