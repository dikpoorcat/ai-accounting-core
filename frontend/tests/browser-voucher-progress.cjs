// Browser-only synthetic responses; no accounting writes or service changes.
const assert = require("node:assert/strict");
const { selectDashboardOption } = require("./helpers/dashboard-select.cjs");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

async function run(config) {
  const { chromium } = require(config.playwright_module);
  const fixtures = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures/dashboard-contracts.json"), "utf8"));
  const validators = {};
  for (const name of ["Brief", "BusinessStatus"]) validators[name] = (await import(pathToFileURL(path.join(__dirname, `../src/api/generated/dashboard${name}.js`))))[`validateDashboard${name}Response`];
  const clone = value => structuredClone(value);
  const browser = await chromium.launch({ channel: config.channel || "msedge", headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
  page.setDefaultTimeout(12000);
  const requests = [], errors = [], fixtureErrors = [], layouts = [];
  const company = config.company_id || "voucher-progress-company", period = config.period || "2026-01";
  const later = period.endsWith("-12") ? `${Number(period.slice(0, 4)) + 1}-01` : `${period.slice(0, 4)}-${String(Number(period.slice(5)) + 1).padStart(2, "0")}`;
  let phase = "synthetic setup", snapshot = "voucher-progress-v1", failInitial = true, failMore = true, holdNext = false, releaseHeld, notifyHeld, cardIndex = 0, layoutMode = false, largeProgress = false;
  const pageInfo = (items, total = items.length, cursor = null) => ({ total_count: total, filtered_count: total, returned_count: items.length, has_more: cursor !== null, next_cursor: cursor });
  const periodInfo = value => ({ ...fixtures.company_with_period.response.periods[0], key: value, year: Number(value.slice(0, 4)), month: Number(value.slice(5)), label: `${value.slice(0, 4)} 年 ${Number(value.slice(5))} 月`, short_label: `${Number(value.slice(5))} 月`, start_date: `${value}-01`, end_date: `${value}-${new Date(Number(value.slice(0, 4)), Number(value.slice(5)), 0).getDate()}` });
  const scope = (response, url) => {
    response.read_context.company_id = url.searchParams.get("company_id") || company;
    response.selected_period = periodInfo(url.searchParams.get("period") || period);
    response.snapshot_version = snapshot;
    return response;
  };
  const recognition = value => ({ precision: "month", period: value, date: null, label: value });
  const title = "演示工资事项与明确业务用途", party = "演示员工张某（长名称换行检查）";
  function brief(url) {
    const response = scope(clone(fixtures.brief.response), url), data = response.data, current = response.selected_period.key;
    const base = data.collections.vouchers.items[0], activityBase = data.collections.activity.items[0];
    const vouchers = Array.from({ length: 21 }, (_, index) => ({ ...clone(base), voucher_version_id: `progress-voucher-${index}`, subject_id: `progress-business-${index}`, has_business_progress: index !== 19, number: String(index + 1), recognition: recognition(current), date: null, list_summary: "演示工资短摘要", summary: title, business_amount_fen: "800000", business_amount_label: "税前工资", amount_fen: "800000", type: "工资", kind: "payroll", reverses_version_id: null, lines: base.lines.map(line => ({ ...clone(line), debit_fen: BigInt(line.debit_fen) ? "800000" : "0", credit_fen: BigInt(line.credit_fen) ? "800000" : "0", source_label: line.source_label ? `${current} · 工资` : "", party, parties: [] })) }));
    const activities = vouchers.slice(0, 2).map((voucher, index) => ({ ...clone(activityBase), key: `progress-activity-${index}`, subject_id: voucher.subject_id, voucher_version_id: voucher.voucher_version_id, recognition: recognition(current), date: null, party, title: "工资", description: title, amount_fen: "800000", amount_label: "税前工资", state: "已入账" }));
    if (layoutMode) for (const voucher of vouchers) {
      voucher.summary = `${title}，包含本月员工工资、个人所得税与后续清偿的完整业务说明，长文本应自然换行并完整展示，不依赖截断提示识别事项。`;
      voucher.business_amount_fen = "9007199254740993123";
      voucher.amount_fen = "9007199254740993123";
      for (const line of voucher.lines) {
        line.account = "固定资产及长期经营设备累计核算科目长名称展示检查";
        line.source_label = "演示长期业务来源与资产用途说明自然换行展示检查";
        line.debit_fen = BigInt(line.debit_fen) ? "9007199254740993123" : "0";
        line.credit_fen = BigInt(line.credit_fen) ? "9007199254740993123" : "0";
        line.parties = [{ id: "layout-person-a", name: "演示往来对象甲方长名称", amount_fen: "9007199254740980778" }, { id: "layout-person-b", name: "演示往来对象乙方长名称", amount_fen: "12345" }];
      }
    }
    data.activity_count = activities.length; data.voucher_count = vouchers.length;
    data.focused_activity = null; data.focused_voucher = null;
    data.activity_groups = [{ ...data.activity_groups.find(group => group.key === activityBase.group), event_count: activities.length }];
    data.collections.activity = { items: activities, page: pageInfo(activities) };
    data.collections.vouchers = { items: vouchers.slice(0, 2), page: pageInfo(vouchers.slice(0, 2)) };
    const focus = url.searchParams.get("voucher_version_id"), number = url.searchParams.get("voucher_number");
    if (focus || number) data.focused_voucher = vouchers.find(item => focus ? item.voucher_version_id === focus : item.number === number) || null;
    const section = url.searchParams.get("section");
    if (section === "vouchers") {
      const items = url.searchParams.has("cursor") ? vouchers.slice(20) : vouchers.slice(0, 20);
      data.collections = { vouchers: { items, page: pageInfo(items, 21, items.length === 20 ? "progress-vouchers-next" : null) } };
    } else if (section) data.collections = section === "activity" ? { activity: data.collections.activity, vouchers: data.collections.vouchers } : { [section]: data.collections[section] };
    return response;
  }
  function detail(url) {
    const response = scope(clone(fixtures.business_status.response), url), data = response.data, current = response.selected_period.key;
    const subject = url.searchParams.get("subject_id");
    data.identity.company_id = response.read_context.company_id; data.identity.subject_id = subject; data.identity.kind = "payroll";
    data.period = current; data.latest_source.period = current; data.latest_source.deleted = false;
    data.current_business_result = { amount_fen: "800000", amount_label: "税前工资", posting_period: current };
    const source = { key: `${subject}-net`, name: "net", direction: "payable", category_key: "payroll_payables", source_period: current, source_amount_fen: "800000", paid_fen: "600000", other_settled_fen: "0", remaining_fen: "200000", settlement_status: "partial" };
    if (largeProgress) Object.assign(source, { source_amount_fen: "9007199254740993123", paid_fen: "6007199254740993123", remaining_fen: "3000000000000000000" });
    const tax = { ...source, key: `${subject}-tax`, name: "tax", source_amount_fen: "12345", paid_fen: "0", remaining_fen: "12345", settlement_status: "open" };
    const housing = { ...tax, key: `${subject}-housing`, name: "employee_housing", source_amount_fen: "10000", remaining_fen: "10000" };
    data.settlements = { cutoff_period: current, status: "established", checking: false, obligations: [source, tax, housing] };
    data.current_followups.settlements = { ...clone(data.settlements), cutoff_period: later, obligations: [{ ...source, paid_fen: source.source_amount_fen, remaining_fen: "0", settlement_status: "settled" }, tax, housing] };
    const profile = (entity_id, name, purpose = null, note = null) => ({ entity_id, values: Object.fromEntries(["display_name", "display_number", "purpose", "note", "employment_start", "employment_end", "employment_status", "active", "category_label", "rights_description"].map(key => [key, key === "display_name" ? name : key === "purpose" ? purpose : key === "note" ? note : null])) });
    data.display_profiles = { business: profile(subject, title, title, "演示独有备注，请自然换行展示"), employees: [profile("progress-person", party), profile("progress-person", party)], counterparties: [profile("progress-person", party)], fund_accounts: [profile("progress-account", "演示工资付款账户")] };
    const event = index => ({ id: `${subject}-event-${index}`, subject_id: `progress-payment-${index}`, source_subject_id: subject, posting_period: current, direction: index === 4 ? -1 : 1, signed_amount_fen: index === 4 ? "-10000" : "10000", relation_state: "resolved", kind: "payment", name: index % 2 ? "tax" : "net", mode: ["payment", "offset", "advance", "accepted"][index % 4] });
    const more = url.searchParams.has("cursor"), items = more ? [event(20)] : Array.from({ length: 20 }, (_, index) => event(index));
    data.collections.settlement_events = { ...data.collections.settlement_events, items, page: pageInfo(items, 21, more ? null : "progress-events-next"), scope_period: current, current_cutoff_period: later };
    data.settlement_view = url.searchParams.get("settlement_view") || "current";
    return response;
  }
  const safe = handler => async route => {
    try { await handler(route); } catch (error) { fixtureErrors.push(error.message); await route.fulfill({ status: 500, json: { message: "合成响应构造失败" } }).catch(() => {}); }
  };
  page.on("request", request => { const url = new URL(request.url()); if (url.pathname.startsWith("/api/dashboard/")) requests.push(url); });
  page.on("pageerror", error => errors.push(error.message));
  const detailRequests = () => requests.filter(url => url.pathname.endsWith("business-status"));
  const briefRequests = () => requests.filter(url => url.pathname.endsWith("brief"));
  const frames = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const idle = async () => { await page.waitForFunction(() => document.querySelector(".module-header")?.getAttribute("aria-busy") === "false"); await frames(); };
  const refresh = async () => { await page.getByRole("button", { name: "刷新数据", exact: true }).click(); await idle(); };
  const card = () => page.locator(".voucher-card").nth(cardIndex);
  const trigger = () => card().locator(".voucher-progress-button");
  const popover = () => page.locator("#voucher-progress-popover");
  const closeProgress = async () => { if (!await popover().count()) return; await popover().getByRole("button", { name: "关闭业务进展", exact: true }).click(); await popover().waitFor({ state: "detached" }); };
  const panel = () => popover().locator(".business-detail-panel");
  const verifyScrollbar = async () => assert(await popover().locator('.voucher-progress-content').evaluate(element => {
    if (CSS.supports('selector(::-webkit-scrollbar)')) {
      const scrollbar = getComputedStyle(element, '::-webkit-scrollbar');
      return getComputedStyle(element).scrollbarWidth === 'auto' && scrollbar.width === '3px' && scrollbar.height === '3px';
    }
    return getComputedStyle(element).scrollbarWidth === 'thin';
  }), 'popup scrollbar must use 3px WebKit styling or thin fallback');
  async function openCard() { const row = card().locator(".voucher-row"); if (await row.getAttribute("aria-expanded") !== "true") { await row.focus(); await row.press("Enter"); } await card().getByRole("table", { name: "凭证分录", exact: true }).waitFor(); await frames(); }
  async function waitHeld() {
    await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => { notifyHeld = null; reject(new Error("delayed detail request did not start")); }, 12000);
      notifyHeld = () => { clearTimeout(timeout); notifyHeld = null; resolve(); };
    });
  }
  try {
    await page.route(url => url.pathname.startsWith("/api/"), route => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === "/api/browser-ticket") return route.fulfill({ json: { url: `${config.origin || new URL(config.ticket_url).origin}/#ticket=synthetic-progress-ticket` } });
      if (pathname === "/api/browser-session") return route.fulfill({ json: { status: "ok" } });
      if (pathname === "/api/security-request") return route.fulfill({ json: { schema_version: 1, catalog_instance_id: "progress-synthetic-catalog", provisioned: true, login_name: "演示负责人", active: true, authenticated: true } });
      fixtureErrors.push(`unhandled isolated API: ${pathname}`); return route.fulfill({ status: 503, json: { message: "隔离测试未配置此接口" } });
    });
    await page.route("**/api/dashboard/context?*", safe(route => {
      const response = clone(fixtures.company_with_period.response);
      response.companies = [{ ...response.companies[0], company_id: company, name: "演示公司", taxpayer_id: null }];
      response.current_company = response.companies[0]; response.company = "演示公司"; response.periods = [period, later].map(periodInfo); response.default_period = period;
      return route.fulfill({ json: response });
    }));
    await page.route("**/api/dashboard/brief?*", safe(route => {
      const url = new URL(route.request().url()), response = brief(url);
      assert(validators.Brief(response), JSON.stringify(validators.Brief.errors));
      return route.fulfill({ json: response });
    }));
    await page.route("**/api/dashboard/business-status?*", safe(async route => {
      const url = new URL(route.request().url());
      assert.equal(url.searchParams.get("limit"), "20"); assert.equal(url.searchParams.get("expected_version"), snapshot);
      assert(url.searchParams.get("subject_id").startsWith("progress-business-"));
      const response = detail(url); assert(validators.BusinessStatus(response), JSON.stringify(validators.BusinessStatus.errors));
      if (!url.searchParams.has("cursor") && failInitial) { failInitial = false; return route.fulfill({ status: 503, json: { message: "合成首次读取失败" } }); }
      if (url.searchParams.has("cursor") && failMore) { failMore = false; return route.fulfill({ status: 503, json: { message: "合成续页读取失败" } }); }
      if (holdNext) { holdNext = false; await new Promise(resolve => { releaseHeld = resolve; notifyHeld?.(); }); }
      await route.fulfill({ json: response }).catch(() => {});
    }));
    phase = "default and precise cached voucher preview";
    await page.goto(`${config.origin || new URL(config.ticket_url).origin}/?company_id=${encodeURIComponent(company)}&period=${period}`); await idle();
    await page.locator(".section-nav").getByRole("button", { name: "本月发生", exact: true }).click(); await frames();
    assert.equal(detailRequests().length, 0);
    const previewButton = page.locator(".event-voucher-button").first(), beforePreview = briefRequests().length;
    await previewButton.hover(); await page.locator("#activity-voucher-preview").waitFor(); await previewButton.focus(); await frames();
    assert.equal(briefRequests().length, beforePreview); assert.equal(detailRequests().length, 0);
    await previewButton.click(); await page.locator("#selected-voucher").waitFor(); await frames();
    assert.equal(briefRequests().length, beforePreview); assert.equal(detailRequests().length, 0);
    assert.equal(await page.locator(".voucher-card").count(), 1);
    await openCard(); await trigger().waitFor();
    assert.equal(await card().locator('.voucher-inline-detail .voucher-summary').count(), 0);
    assert.equal(await card().getByRole('heading', { name: '会计分录', exact: true }).count(), 0);
    assert((await card().locator('.voucher-copy').textContent()).includes(title));
    assert.equal(await trigger().getAttribute('aria-expanded'), 'false');

    phase = "initial retry and automatic continuation retry";
    await trigger().focus(); await trigger().press("Enter");
    assert.equal(await trigger().getAttribute('aria-controls'), 'voucher-progress-popover');
    await popover().getByRole("button", { name: "重新读取", exact: true }).click();
    await panel().getByRole("button", { name: "重试读取", exact: true }).waitFor();
    assert.equal(detailRequests().length, 3); assert.equal(await panel().locator("ul > li").count(), 20);
    assert.equal(await card().locator(".voucher-row").getAttribute("aria-expanded"), "true");
    await panel().getByRole("button", { name: "重试读取", exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll("#voucher-progress-popover .business-detail-panel ul > li").length === 21);
    assert.equal(detailRequests().length, 4);
    assert.equal(await panel().getByRole("button", { name: "加载更多", exact: true }).count(), 0);
    assert.equal(await panel().locator(".owner-item-latest").count(), 1, "unchanged tax must not repeat in latest progress");
    assert.equal(await panel().locator(".business-amounts").count(), 0, "voucher amount and posting month must not repeat");
    const text = await panel().textContent();
    assert(!text.includes(title), "voucher summary repeated in progress");
    assert(text.includes("演示独有备注")); assert(text.includes("员工：") && text.includes("往来方："), "same name with distinct roles must remain clear");

    phase = "hover boundary, keyboard and internal actions";
    const cached = detailRequests().length;
    await closeProgress();
    await trigger().evaluate(element => element.blur());
    await page.mouse.move(1, 1);
    await trigger().hover(); await panel().waitFor();
    assert.equal(await popover().count(), 1);
    assert(await popover().evaluate((element, anchorSelector) => {
      const anchor = document.querySelector(anchorSelector), box = element.getBoundingClientRect(), button = anchor.getBoundingClientRect();
      const arrow = getComputedStyle(element, '::after');
      const arrowCenter = box.top + parseFloat(arrow.top) + parseFloat(arrow.height) / 2;
      return element.classList.contains('dashboard-hover-preview') && element.dataset.side === 'left'
        && box.right <= button.left - 9.5 && Math.abs(arrowCenter - (button.top + button.height / 2)) < 2
        && parseFloat(arrow.right) < 0 && arrow.content !== 'none';
    }, '.voucher-card .voucher-progress-button'), 'desktop progress must use shared frame on entrance left with right arrow aligned to entrance');
    await popover().hover(); await page.waitForTimeout(200); assert.equal(await popover().count(), 1);
    await page.mouse.move(1, 1); await page.waitForTimeout(60); assert.equal(await popover().count(), 1, 'hover leave closed before grace interval');
    await popover().waitFor({ state: 'detached' });
    await trigger().focus(); await panel().waitFor(); await frames(); assert.equal(detailRequests().length, cached);
    await page.keyboard.press('Tab');
    assert(await popover().getByRole('button', { name: '关闭业务进展', exact: true }).evaluate(element => document.activeElement === element), 'desktop Tab must enter teleported popup');
    await page.keyboard.press('Shift+Tab'); assert(await trigger().evaluate(element => document.activeElement === element));
    await trigger().focus(); await panel().waitFor();
    await trigger().press('Enter'); assert.equal(await popover().count(), 1, 'click after focus must keep popup open');
    await panel().click({ position: { x: 8, y: 8 } });
    assert.equal(await card().locator('.voucher-row').getAttribute('aria-expanded'), 'true');
    await page.keyboard.press('Escape'); await popover().waitFor({ state: 'detached' });
    assert(await trigger().evaluate(element => document.activeElement === element));
    await trigger().click(); await panel().waitFor();
    await page.mouse.click(1, 1); await popover().waitFor({ state: 'detached' });
    await trigger().click(); await panel().waitFor();
    phase = "long summary, account, multiple objects and exact large amounts";
    layoutMode = true; await refresh(); await page.getByRole("button", { name: "按凭证", exact: true }).click(); await openCard(); await trigger().click(); await panel().waitFor();
    assert((await card().locator('.voucher-copy').textContent()).includes('不依赖截断提示识别事项'));
    for (const theme of ["light", "dark"]) {
      await page.evaluate(value => document.documentElement.dataset.theme = value, theme);
      for (const width of [1440, 1024, 761, 760, 390, 320]) {
        await page.setViewportSize({ width, height: 1000 }); await frames();
        if (!await popover().count()) { await trigger().click(); await panel().waitFor(); }
        assert.equal(await popover().count(), 1);
        assert(await popover().evaluate(element => { const box = element.getBoundingClientRect(); return box.left >= 11.5 && box.right <= innerWidth - 11.5 && box.top >= 11.5 && box.bottom <= innerHeight - 11.5; }), `popover outside 12px viewport margin ${theme}/${width}`);
        if (width <= 760) {
          assert.equal(await popover().getAttribute("aria-modal"), "true");
          assert.equal(await panel().locator('.voucher-progress-table').first().isVisible(), false, 'mobile must retain existing payment cards');
          assert.equal(await panel().locator('.owner-activity-obligation:visible').count(), 3);
          await closeProgress(); assert(await trigger().evaluate(element => document.activeElement === element));
          await trigger().focus(); await frames(); assert.equal(await popover().count(), 0, "mobile focus must not open progress");
          await trigger().click(); await panel().waitFor();
          await page.mouse.click(1, 1); await popover().waitFor({ state: 'detached' });
          assert(await trigger().evaluate(element => document.activeElement === element), 'mobile backdrop close must restore trigger focus');
          await trigger().click(); await panel().waitFor();
        }
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `document overflow ${theme}/${width}`);
        assert(await panel().evaluate(element => element.scrollWidth <= element.clientWidth + 1), `progress overflow ${theme}/${width}`);
        assert(await card().locator('.voucher-chevron').evaluate(arrow => {
          const box = arrow.getBoundingClientRect(), style = getComputedStyle(arrow), button = arrow.closest('.voucher-card').querySelector('.voucher-row').getBoundingClientRect();
          const transform = new DOMMatrix(style.transform);
          return arrow.tagName.toLowerCase() === 'svg' && arrow.classList.contains('business-list-arrow') && arrow.classList.contains('expanded')
            && Math.abs(box.width - 16) < 1 && Math.abs(box.height - 12) < 1 && parseFloat(style.strokeWidth) === 1.8
            && Math.abs(transform.a) < .001 && Math.abs(transform.b - 1) < .001 && Math.abs(transform.c + 1) < .001 && Math.abs(transform.d) < .001
            && box.right <= button.right + 3 && box.left >= button.right - 40;
        }), `expanded shared arrow must rotate down and remain at row end ${theme}/${width}`);
        const unselectedCard = page.locator('.voucher-card').nth(1);
        assert(await unselectedCard.locator('.voucher-chevron').evaluate(arrow => {
          const style = getComputedStyle(arrow), box = arrow.getBoundingClientRect(), button = arrow.closest('.voucher-card').querySelector('.voucher-row').getBoundingClientRect();
          return arrow.classList.contains('business-list-arrow') && !arrow.classList.contains('expanded') && style.transform === 'none'
            && Math.abs(box.width - 12) < 1 && Math.abs(box.height - 16) < 1 && parseFloat(style.strokeWidth) === 1.8
            && box.right <= button.right + 3 && box.left >= button.right - 40;
        }), `collapsed shared arrow must face right and remain at row end ${theme}/${width}`);
        await verifyScrollbar();
        if (width > 760) {
          const popoverWidth = await popover().evaluate(element => element.getBoundingClientRect().width);
          assert(popoverWidth <= 681, `desktop popup exceeds 680px maximum ${theme}/${width}`);
          if (width === 1440) assert(popoverWidth >= 679, `wide desktop popup should use 680px ${theme}/${width}`);
          const progressTable = panel().locator('.voucher-progress-table').first();
          assert.equal(await progressTable.locator('thead').count(), 1);
          assert.equal(await progressTable.locator('tbody tr').count(), 3);
          assert.equal(await progressTable.locator('thead').getByText('原应付', { exact: true }).count(), 1);
          assert.equal(await progressTable.locator('tbody').getByText('个人公积金', { exact: true }).count(), 1);
          assert(await progressTable.evaluate(table => {
            const headers = [...table.querySelectorAll('thead th')].map(item => item.getBoundingClientRect());
            return [...table.querySelectorAll('tbody tr')].every(row => [...row.children].every((cell, index) => {
              const box = cell.getBoundingClientRect(); return Math.abs(box.left - headers[index].left) < 2 && Math.abs(box.right - headers[index].right) < 2;
            }));
          }), `shared headers must align with each payment row ${theme}/${width}`);
          assert(await popover().locator('.voucher-progress-content').evaluate(content => {
            const box = content.getBoundingClientRect();
            const scrollbar = CSS.supports('selector(::-webkit-scrollbar)') ? parseFloat(getComputedStyle(content, '::-webkit-scrollbar').width) : content.offsetWidth - content.clientWidth;
            return [...content.querySelectorAll('.voucher-progress-table .business-state')].filter(item => item.getClientRects().length)
              .every(state => box.right - scrollbar - state.getBoundingClientRect().right >= 11.5);
          }), `desktop status needs 12px clearance from scrollbar ${theme}/${width}`);
          assert(await card().getByRole("table", { name: "凭证分录", exact: true }).evaluate(table => {
            const width = (element, side) => element ? parseFloat(getComputedStyle(element)[`border${side}Width`]) : 0;
            const top = Math.max(width(table, 'Top'), width(table.querySelector('thead'), 'Top'), width(table.querySelector('thead tr'), 'Top'), width(table.querySelector('thead th'), 'Top'));
            const header = Math.max(width(table.querySelector('thead'), 'Bottom'), width(table.querySelector('thead tr'), 'Bottom'), width(table.querySelector('thead th'), 'Bottom'));
            const bottom = Math.max(width(table, 'Bottom'), width(table.querySelector('tfoot'), 'Bottom'), width(table.querySelector('tfoot tr'), 'Bottom'), width(table.querySelector('tfoot td'), 'Bottom'));
            return top > 0 && header > 0 && bottom > 0 && [...table.querySelectorAll('tbody tr, tbody td')].every(element => width(element, 'Top') === 0 && width(element, 'Bottom') === 0);
          }), `three-line table ${theme}/${width}`);
        }
        const directory = config.screenshot_directory || config.screenshots_directory;
        if (directory) { fs.mkdirSync(directory, { recursive: true }); await page.screenshot({ path: path.join(directory, `voucher-progress-${theme}-${width}.png`) }); }
        if (directory && [1440, 761, 760, 320].includes(width)) {
          await closeProgress(); await card().evaluate(element => element.scrollIntoView({ block: 'start' })); await frames();
          await card().screenshot({ path: path.join(directory, `voucher-entries-${theme}-${width}.png`), style: '.module-nav { visibility: hidden !important; }' });
          await trigger().click(); await panel().waitFor();
        }
        layouts.push({ theme, width });
      }
    }

    phase = "manual voucher mode restores cached progress";
    layoutMode = false;
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.getByRole("button", { name: "按业务", exact: true }).click();
    await page.getByRole("button", { name: "按凭证", exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll(".voucher-card").length === 20);
    phase = "explicit unavailable progress remains passive";
    const emptyCard = page.locator('.voucher-card').nth(19), beforeEmpty = detailRequests().length;
    await closeProgress(); await page.mouse.move(1, 1);
    assert.equal(await emptyCard.locator('.voucher-progress-button').count(), 0);
    assert.equal(await emptyCard.locator('.voucher-progress-empty').textContent(), '—');
    await emptyCard.locator('.voucher-progress-empty').hover(); await emptyCard.focus(); await frames();
    assert.equal(await popover().count(), 0); assert.equal(detailRequests().length, beforeEmpty);
    await page.mouse.move(1, 1);
    await openCard(); await trigger().click(); await panel().waitFor(); await frames(); assert.equal(detailRequests().length, cached);
    await popover().evaluate(element => {
      const controls = [...element.querySelectorAll('button:not(:disabled), a[href], [tabindex]:not([tabindex="-1"])')].filter(item => item.getClientRects().length);
      controls.at(-1)?.focus();
    });
    await page.keyboard.press('Tab'); await popover().waitFor({ state: 'detached' });
    assert(await page.evaluate(() => document.activeElement?.matches('button, a, input, select, textarea, [tabindex]') && !document.activeElement.matches('.voucher-progress-button')), 'desktop last Tab must continue to the next page control');
    phase = "card collapse leaves progress independent";
    await closeProgress(); await page.mouse.move(1, 1);
    await card().locator(".voucher-row").focus(); await card().locator(".voucher-row").press("Space"); await openCard();
    await trigger().click(); await panel().waitFor(); await frames(); assert.equal(detailRequests().length, cached, "reopened voucher reread progress");
    phase = "voucher page two then return restores cached progress";
    await page.locator(".voucher-pagination").getByRole("button", { name: "下一页", exact: true }).click();
    await page.mouse.move(1, 1);
    await page.waitForFunction(() => document.querySelector(".voucher-pagination strong")?.textContent.trim() === "2 / 2");
    await page.locator(".voucher-pagination").getByRole("button", { name: "上一页", exact: true }).click();
    await page.mouse.move(1, 1);
    await openCard(); await trigger().click(); await panel().waitFor(); await frames(); assert.equal(detailRequests().length, cached, "page return reread progress");

    phase = "same snapshot refresh retains cache";
    await refresh(); await page.getByRole("button", { name: "按凭证", exact: true }).click(); await openCard();
    await trigger().click();
    await panel().waitFor(); await frames(); assert.equal(detailRequests().length, cached, "same snapshot refresh reread progress");
    phase = "late response cannot restore old progress";
    await closeProgress(); await page.mouse.move(1, 1);
    cardIndex = 1; await openCard();
    holdNext = true; const cancelled = waitHeld(); await trigger().click(); await cancelled;
    await closeProgress();
    const afterCancel = detailRequests().length;
    releaseHeld(); releaseHeld = null; await frames();
    assert.equal(await popover().count(), 0, "cancelled response restored popup");
    assert.equal(detailRequests().length, afterCancel, "cancelled first response started continuation");
    phase = "held response across snapshot change";
    holdNext = true; const held = waitHeld(); await trigger().click(); await held;
    snapshot = "voucher-progress-v2"; await refresh(); const beforeLate = detailRequests().length;
    releaseHeld(); releaseHeld = null; cardIndex = 0; await frames();
    assert.equal(await page.locator("#voucher-progress-popover .business-detail-panel").count(), 0); assert.equal(detailRequests().length, beforeLate);
    await page.getByRole("button", { name: "按凭证", exact: true }).click(); await openCard(); await trigger().click();
    await page.waitForFunction(() => document.querySelectorAll("#voucher-progress-popover .business-detail-panel ul > li").length === 21);
    assert.equal(detailRequests().length, beforeLate + 2, "changed scope incorrectly reused old detail cache");
    phase = "month change starts a fresh progress scope";
    const beforeMonth = detailRequests().length;
    await selectDashboardOption(page, "查看月份", later); await idle();
    assert.equal(await page.locator("#voucher-progress-popover .business-detail-panel").count(), 0);
    await page.locator(".section-nav").getByRole("button", { name: "本月发生", exact: true }).click();
    await page.getByRole("button", { name: "按凭证", exact: true }).click(); await openCard(); await trigger().click();
    await page.waitForFunction(() => document.querySelectorAll("#voucher-progress-popover .business-detail-panel ul > li").length === 21);
    assert.equal(detailRequests().length, beforeMonth + 2, "month change reused old progress cache");
    assert(detailRequests().slice(beforeMonth).every(url => url.searchParams.get("period") === later));
    phase = "wide compact progress preserves exact large amounts and internal scrolling";
    largeProgress = true; snapshot = "voucher-progress-large-layout";
    await refresh(); await page.getByRole("button", { name: "按凭证", exact: true }).click(); await openCard(); await trigger().click();
    await page.waitForFunction(() => document.querySelectorAll('#voucher-progress-popover .business-detail-panel ul > li').length === 21);
    for (const width of [1440, 1024, 761]) {
      await page.setViewportSize({ width, height: 1000 }); await frames();
      if (!await popover().count()) { await trigger().click(); await panel().waitFor(); }
      assert(await panel().evaluate(element => element.scrollWidth <= element.clientWidth + 1), `large progress horizontal overflow ${width}`);
      assert(await panel().locator('.voucher-progress-table').first().evaluate(table => {
        return [...table.querySelectorAll('tbody tr')].every(row => [...row.children].every(cell => cell.scrollWidth <= cell.clientWidth + 1));
      }), `large progress cells overflow ${width}`);
      const directory = config.screenshot_directory || config.screenshots_directory;
      if (directory) await page.screenshot({ path: path.join(directory, `voucher-progress-large-${width}.png`) });
      assert(await popover().locator('.voucher-progress-content').evaluate(element => { element.scrollTop = element.scrollHeight; return element.scrollTop > 0; }), 'long progress must scroll within popup');
      await verifyScrollbar();
      assert(await panel().locator('ul > li').last().isVisible());
      await popover().locator('.voucher-progress-content').evaluate(element => element.scrollTop = 0);
    }
    assert.equal(fixtureErrors.length, 0, fixtureErrors.join("; ")); assert.equal(errors.length, 0, errors.join("; "));
    return { status: "passed", synthetic_layouts: layouts, default_progress_requests: 0, paired_preview_requests: 0, local_exact_focus_requests: 0, first_progress_requests: 1, first_retry_requests: 1, automatic_page_limit: 20, continuation_retry: true, related_events: 21, changed_followups_only: true, unique_notes_and_roles: true, internal_actions_isolated: true, keyboard: true, hover_grace: true, single_popover: true, mobile_close_focus: true, outside_and_escape: true, card_and_page_return_cache: true, same_snapshot_refresh_cache: true, changed_scope_invalidates_cache: true, late_response_rejected: true, browser_errors: 0 };
  } catch (error) {
    const directory = config.screenshot_directory || config.screenshots_directory;
    if (directory) { fs.mkdirSync(directory, { recursive: true }); await page.screenshot({ path: path.join(directory, 'failed-progress.png') }).catch(() => {}); }
    const state = await page.evaluate(() => ({ arrow: (() => { const arrow = document.querySelector(".voucher-chevron"); if (!arrow) return null; const style = getComputedStyle(arrow); return { html: arrow.outerHTML, box: arrow.getBoundingClientRect().toJSON(), row: arrow.closest(".voucher-card").querySelector(".voucher-row").getBoundingClientRect().toJSON(), transform: style.transform, stroke: style.strokeWidth }; })(), active: document.activeElement?.outerHTML.slice(0, 300), popup: document.querySelector('#voucher-progress-popover')?.outerHTML.slice(0, 300), buttons: [...document.querySelectorAll('.voucher-progress-button')].slice(0, 2).map(element => element.outerHTML), row: document.querySelector('.voucher-row')?.outerHTML.slice(0, 600) })).catch(() => null);
    throw new Error(`${phase}: ${fixtureErrors.length ? fixtureErrors.join("; ") : String(error.message).replace(/https?:\/\/[^\s"']+/g, "[URL]")}\nDOM state: ${JSON.stringify(state)}`);
  }
  finally { releaseHeld?.(); await browser.close(); }
}
if (require.main === module) {
  let input = ""; process.stdin.setEncoding("utf8"); process.stdin.on("data", chunk => input += chunk);
  process.stdin.on("end", async () => { try { process.stdout.write(JSON.stringify(await run(JSON.parse(input))) + "\n"); } catch (error) { process.stdout.write(JSON.stringify({ status: "failed", message: error.message }) + "\n"); process.exitCode = 1; } });
}
module.exports = { run };
