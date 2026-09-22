// Actual Stage 7 build + loopback-only synthetic service.
const assert = require("node:assert/strict");
const { mkdirSync, writeFileSync } = require("node:fs");
const { join } = require("node:path");

const sanitize = value => String(value)
  .replace(/https?:\/\/[^\s"']+/g, "[browser URL]")
  .replace(/[A-Za-z]:\\[^\s"']+/g, "[local path]")
  .replace(/[?&](?:ticket|capability|token)=[^&\s"']+/gi, "$&".replace(/=.*/, "=[redacted]"));

async function run(config) {
  assert(config && typeof config === "object");
  assert(Array.isArray(config.companies) && config.companies.length >= 4);
  const { chromium } = require(config.playwright_module);
  mkdirSync(config.output, { recursive: true });
  const browser = await chromium.launch({ channel: config.channel, headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
  const page = await context.newPage();
  page.setDefaultTimeout(20000);

  const errors = [];
  const apiFailures = [];
  const screenshots = [];
  const checks = [];
  page.on("pageerror", error => errors.push(sanitize(error.message)));
  page.on("console", message => { if (message.type() === "error") errors.push(sanitize(message.text())); });
  page.on("response", response => {
    const target = new URL(response.url());
    if (target.pathname.startsWith("/api/") && response.status() >= 400) {
      apiFailures.push({ path: target.pathname, status: response.status() });
    }
  });

  const byState = state => {
    const company = config.companies.find(item => item.state === state);
    assert(company, `Missing ${state} synthetic company`);
    return company;
  };
  const prepared = byState("prepared");
  const closed = byState("closed");
  const unprepared = byState("unprepared");
  const empty = byState("empty");
  const modules = [
    { path: "/", action: "brief", heading: /经营简报$/ },
    { path: "/funds", action: "funds", heading: "资金总览" },
    { path: "/employees", action: "employees", heading: "员工与薪酬概览" },
    { path: "/assets", action: "assets", heading: "长期资产概览" },
    { path: "/reports", action: "quarterly-report", heading: "季度财务报表", extra: "&quarter=2026-Q1" },
  ];

  const dashboardResponse = (action, companyId, extra = () => true) => {
    const pending = page.waitForResponse(response => {
      const target = new URL(response.url());
      return target.pathname === `/api/dashboard/${action}`
        && target.searchParams.get("company_id") === companyId
        && response.status() === 200
        && extra(target);
    });
    void pending.catch(() => {});
    return pending;
  };
  const reviewResponse = (companyId, extra = () => true) => dashboardResponse("close-review", companyId, extra);
  const url = (path, company, extra = "") => {
    const query = new URLSearchParams({ company_id: company.id });
    if (company.period) query.set("period", company.period);
    return `${config.origin}${path}?${query}${extra}`;
  };
  async function ready(module) {
    await page.getByRole("heading", { name: module.heading }).first().waitFor();
    await page.waitForFunction(() => !document.querySelector('.module-header[aria-busy="true"]'));
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  }
  async function shot(name, locator = null) {
    const target = locator || page;
    await target.screenshot({ path: join(config.output, name), ...(locator ? {} : { fullPage: true }) });
    screenshots.push(name);
  }
  async function visit(module, company) {
    const pending = dashboardResponse(module.action, company.id);
    const review = module.path === "/" && company.period ? reviewResponse(company.id) : null;
    const responses = Promise.all(review ? [pending, review] : [pending]);
    await page.goto(url(module.path, company, module.extra || ""));
    const [http] = await responses;
    const payload = await http.json();
    await ready(module);
    assert.equal(await page.getByLabel("切换公司", { exact: true }).inputValue(), company.id);
    assert.equal(payload.read_context?.company_id ?? payload.company_id, company.id);
    assert.equal(await page.locator('input[type="password"]').count(), 0, "Dashboard must not collect the owner password");
    return payload;
  }

  try {
    const ticket = new URL(config.ticket_url);
    ticket.searchParams.set("company_id", prepared.id);
    ticket.searchParams.set("period", prepared.period);
    const initialBrief = dashboardResponse("brief", prepared.id);
    const initialReview = reviewResponse(prepared.id);
    const initialResponses = Promise.all([initialBrief, initialReview]);
    await page.goto(ticket.href);
    const [, initialReviewHttp] = await initialResponses;
    const preparedSummary = await initialReviewHttp.json();
    await ready(modules[0]);
    assert((await context.cookies()).some(cookie => cookie.name === "finance_session" && cookie.httpOnly));
    assert.equal(preparedSummary.state, "prepared");
    assert.equal(preparedSummary.preview_digest, prepared.preview_digest);
    if (prepared.expected_review_expense_fen !== undefined) {
      assert.equal(
        preparedSummary.owner_review.accounting_summary.month_expense_fen,
        prepared.expected_review_expense_fen,
      );
      assert.equal(preparedSummary.owner_review.accounting_summary.actual_payments_fen, "0");
      assert(preparedSummary.owner_review.business_summary.some(item =>
        item.action === "business"
          && item.business_amount_fen === prepared.expected_review_expense_fen
          && item.kind === "expense",
      ), "Prepared review must retain the actual monthly expense row");
    }
    await page.getByText("关账预览已准备，可按同一版本核对", { exact: true }).waitFor();
    await page.getByText("借方 / 贷方", { exact: true }).waitFor();
    if (prepared.expected_review_expense_fen !== undefined) {
      await page.getByText("本月费用", { exact: true }).waitFor();
      await page.getByText("本月业务 · 费用", { exact: true }).waitFor();
      await page.getByText("¥10.00", { exact: true }).first().waitFor();
    }
    assert.equal(await page.locator(".close-review button").count(), 0, "Close review is read-only");
    checks.push("One-use browser ticket becomes an HttpOnly session");
    checks.push("Prepared review renders accounting, business, materials and adopted-source summaries");
    await shot("stage7-prepared-summary.png", page.locator(".close-review"));

    const directories = page.locator(".review-directories > details");
    let nonEmptyDirectory = null;
    for (let index = 0; index < await directories.count(); index += 1) {
      const candidate = directories.nth(index);
      const label = await candidate.locator(":scope > summary").innerText();
      if ((Number(/(\d+)\s*项/.exec(label)?.[1]) || 0) > 0) { nonEmptyDirectory = candidate; break; }
    }
    assert(nonEmptyDirectory, "Prepared review should expose at least one non-empty detail directory");
    const sectionRequest = reviewResponse(prepared.id, target => target.searchParams.has("section"));
    await nonEmptyDirectory.locator(":scope > summary").click();
    const detailHttp = await sectionRequest;
    const detailUrl = new URL(detailHttp.url());
    const detailPayload = await detailHttp.json();
    assert.equal(detailUrl.searchParams.get("preview_digest"), prepared.preview_digest);
    assert.equal(detailPayload.preview_digest, prepared.preview_digest);
    await nonEmptyDirectory.locator(".review-item").first().waitFor();
    const source = nonEmptyDirectory.getByText("查看精确来源", { exact: true }).first();
    if (await source.count()) {
      await source.click();
      assert(await nonEmptyDirectory.getByText("内部校验信息", { exact: true }).first().isVisible());
    }
    await shot("stage7-prepared-detail.png", page.locator(".close-review"));
    checks.push("Detail requests keep the active preview digest and expose source names before internal identifiers");

    for (const module of modules) {
      await visit(module, prepared);
      await shot(`stage7-${module.action}.png`);
    }
    checks.push("All five dashboard pages render from actual Stage 7 HTTP responses");

    await visit(modules[0], closed);
    await page.getByText("本月已关账", { exact: true }).waitFor();
    await shot("stage7-closed-review.png", page.locator(".close-review"));

    const contextSwitch = dashboardResponse("context", unprepared.id);
    const briefSwitch = dashboardResponse("brief", unprepared.id);
    const reviewSwitch = reviewResponse(unprepared.id);
    await page.getByLabel("切换公司", { exact: true }).selectOption(unprepared.id);
    await Promise.all([contextSwitch, briefSwitch, reviewSwitch]);
    await page.getByText("尚未生成关账预览", { exact: true }).waitFor();
    assert.equal(new URL(page.url()).searchParams.get("company_id"), unprepared.id);
    assert.equal(await page.getByText("关账预览已准备，可按同一版本核对", { exact: true }).count(), 0);
    assert.equal(await page.locator(".close-review button").count(), 0);
    await shot("stage7-company-switch-unprepared.png");
    checks.push("Company switch cancels the prior review and displays the selected company's state");

    const preparedContext = dashboardResponse("context", prepared.id);
    const preparedBrief = dashboardResponse("brief", prepared.id);
    const preparedReview = reviewResponse(prepared.id);
    await page.getByLabel("切换公司", { exact: true }).selectOption(prepared.id);
    await Promise.all([preparedContext, preparedBrief, preparedReview]);
    await page.getByText("关账预览已准备，可按同一版本核对", { exact: true }).waitFor();
    assert.equal(new URL(page.url()).searchParams.get("period"), prepared.period);
    checks.push("Company and month URL selection remain correlated after switching back");

    const emptyContext = dashboardResponse("context", empty.id);
    await page.getByLabel("切换公司", { exact: true }).selectOption(empty.id);
    await emptyContext;
    await page.getByText("还没有可查看的月份", { exact: true }).waitFor();
    assert.equal(new URL(page.url()).searchParams.get("company_id"), empty.id);
    assert.equal(new URL(page.url()).searchParams.has("period"), false);
    await shot("stage7-empty-company.png");
    checks.push("A company without activity keeps an empty month selection");

    assert.deepEqual(errors, []);
    assert.deepEqual(apiFailures, []);
    return { status: "passed", checks, screenshots, page_errors: errors, api_failures: apiFailures };
  } catch (error) {
    try { await shot("stage7-failure.png"); } catch {}
    let pageSummary = "";
    try { pageSummary = sanitize((await page.locator("body").innerText()).slice(0, 1200)); } catch {}
    const diagnostic = { message: sanitize(error?.message || error), page_errors: errors, api_failures: apiFailures, page_summary: pageSummary, screenshots };
    throw new Error(JSON.stringify(diagnostic));
  } finally {
    await context.close();
    await browser.close();
  }
}

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", chunk => { input += chunk; });
process.stdin.on("end", async () => {
  try {
    const result = await run(JSON.parse(input));
    process.stdout.write(`${JSON.stringify(result)}\n`);
  } catch (error) {
    const report = { status: "failed", message: sanitize(error?.stack || error) };
    try {
      const config = JSON.parse(input);
      if (config?.output) writeFileSync(join(config.output, "browser-runner-error.json"), `${JSON.stringify(report, null, 2)}\n`, "utf8");
    } catch {}
    process.stdout.write(`${JSON.stringify(report)}\n`);
    process.exitCode = 1;
  }
});
