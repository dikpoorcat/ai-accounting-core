// Independent real-runtime functional evidence. No business response is mocked.
const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");
const { readFile } = require("node:fs/promises");
const { join } = require("node:path");

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve: () => resolve() };
}

async function run(config) {
  const { chromium } = require(config.playwright_module);
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const checks = [], errors = [], requests = [];
  let diagnosticPage;
  const path = (extra = {}) => "/review/materials?" + new URLSearchParams({ company_id: config.company_id, business_id: config.current_business_id, ...extra });
  const ticketPath = (ticket, extra = {}) => config.origin + path(extra) + new URL(ticket).hash;
  const ready = page => page.locator(".materials-summary").waitFor();
  async function navigate(page, target) {
    await page.evaluate(target => { history.pushState({}, "", target); window.dispatchEvent(new PopStateEvent("popstate")); }, target);
  }
  try {
    const unauthenticated = await browser.newContext();
    const locked = await unauthenticated.newPage();
    const lockedReads = [];
    locked.on("request", request => { if (new URL(request.url()).pathname.startsWith("/api/review/")) lockedReads.push(request.url()); });
    await locked.goto(ticketPath(config.unauthenticated_ticket_url));
    await locked.getByRole("heading", { name: "登录后查阅业务来源" }).waitFor();
    assert.equal(await locked.locator("input[type=password]").count(), 0);
    assert.equal(lockedReads.length, 0);
    assert.equal(new URL(locked.url()).pathname, "/review/materials");
    checks.push("unauthenticated_deep_link_has_no_source_read_or_browser_password");
    await unauthenticated.close();

    const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1200, height: 900 } });
    const page = await context.newPage();
    diagnosticPage = page;
    page.setDefaultTimeout(15000);
    page.on("pageerror", error => errors.push(error.message));
    page.on("response", response => {
      if (["script", "stylesheet"].includes(response.request().resourceType()) && response.status() >= 400) errors.push(`Resource failed: ${new URL(response.url()).pathname} ${response.status()}`);
    });
    page.on("requestfailed", request => {
      if (["script", "stylesheet"].includes(request.resourceType()) && !/ERR_ABORTED/.test(request.failure()?.errorText ?? "")) errors.push(`Resource unavailable: ${new URL(request.url()).pathname}`);
    });
    page.on("request", request => {
      const target = new URL(request.url());
      if (target.pathname.startsWith("/api/")) requests.push({ path: target.pathname, query: target.search });
    });
    await page.goto(ticketPath(config.ticket_url));
    await ready(page);
    assert.equal(new URL(page.url()).hash, "");
    assert.equal(new URL(page.url()).searchParams.has("period"), false);
    assert.equal(requests.filter(request => request.path.startsWith("/api/dashboard/")).length, 0);
    assert.equal(await page.locator(".material-row").count(), 20);
    assert.match(await page.locator(".materials-summary").textContent(), /共 25 份来源，已显示 20 份/);
    assert.equal(requests.filter(request => request.path.endsWith("/content")).length, 0);
    await page.getByRole("button", { name: "加载更多来源", exact: true }).click();
    await page.getByText("共 25 份来源，已显示 25 份。", { exact: true }).waitFor();
    const pages = requests.filter(request => request.path === "/api/review/materials");
    assert.equal(new URLSearchParams(pages[0].query).get("limit"), "20");
    assert.equal(new URLSearchParams(pages[1].query).get("limit"), "500");
    assert(new URLSearchParams(pages[1].query).get("expected_version"));
    checks.push("ticket_first_deep_link_preserves_scope_and_complete_20_plus_5");

    await page.locator(".material-row").nth(1).getByRole("button", { name: "查看说明", exact: true }).click();
    await page.locator(".material-text").waitFor();
    assert.equal(await page.locator(".material-text").textContent(), config.statement);
    assert.equal(await page.evaluate(() => window.__reviewSourceExecuted), undefined);
    assert.equal(await page.locator(".materials-page iframe,.materials-page object,.materials-page embed,.material-text script").count(), 0);
    const download = page.waitForEvent("download");
    const downloadResponse = page.waitForResponse(response => new URL(response.url()).pathname.endsWith(`/sources/${config.source_ids[0]}/content`));
    await page.getByRole("button", { name: "下载原件", exact: true }).click();
    const file = await download;
    const downloaded = await downloadResponse;
    assert.equal(downloaded.status(), 200);
    const headers = downloaded.headers();
    assert.equal(headers["content-type"], "application/octet-stream");
    assert.match(headers["content-disposition"], /^attachment;/);
    assert.equal(headers["x-content-type-options"], "nosniff");
    assert.equal(headers["x-accounting-company-id"], config.company_id);
    assert.equal(headers["x-accounting-source-id"], config.source_ids[0]);
    assert.equal(headers["x-content-sha256"], config.file_sha256);
    assert.equal(Number(headers["content-length"]), config.file_size);
    assert.equal(file.suggestedFilename(), config.file_name);
    const destination = join(config.output_directory, "downloaded-original.html");
    await file.saveAs(destination);
    const bytes = await readFile(destination);
    assert.equal(bytes.length, config.file_size);
    assert.equal(createHash("sha256").update(bytes).digest("hex"), config.file_sha256);
    assert.equal(await page.evaluate(() => window.__reviewSourceExecuted), undefined);
    checks.push("statement_is_literal_text_and_original_is_exact_download_only");

    await navigate(page, path(config.frozen));
    await page.getByText("已关账时采用的资料 · 2026-01", { exact: true }).waitFor();
    assert.equal(await page.locator(".material-row").count(), 1);
    await page.getByRole("button", { name: "查看说明", exact: true }).click();
    await page.locator(".material-text").waitFor();
    assert.equal(await page.locator(".material-text").textContent(), config.statement);
    await navigate(page, path({ business_id: config.other_business_id }));
    await ready(page);
    await page.getByRole("button", { name: "查看说明", exact: true }).click();
    await page.getByText("第3份合成老板说明", { exact: true }).waitFor();
    const beforeOld = requests.length;
    await navigate(page, path(config.old_open));
    await page.getByRole("alert").filter({ hasText: "指定的旧入账结果不会自动换成当前结果" }).waitFor();
    assert.equal(await page.locator(".materials-summary").count(), 0);
    assert(requests.slice(beforeOld).filter(request => request.path === "/api/review/materials").every(request => new URLSearchParams(request.query).has("result_id")));
    checks.push("frozen_and_current_sources_separate_and_old_open_409_never_falls_back");

    // Hold two actual server responses while the SPA goes A→B→A. Their bytes
    // are not replaced; the latest A is fully paged before old replies settle.
    const held = [], arrivals = [deferred(), deferred()];
    const targets = [config.current_business_id, config.other_business_id];
    await page.route("**/api/review/materials?**", async route => {
      const query = new URL(route.request().url()).searchParams;
      if (query.get("business_id") === targets[0] && !query.has("cursor")) {
        targets.shift();
        const response = await route.fetch();
        assert.equal(response.status(), 200);
        await new Promise(resolve => {
          const index = held.length;
          held.push(async () => { try { await route.fulfill({ response }); } catch {} resolve(); });
          arrivals[index].resolve();
        });
      } else await route.continue();
    });
    await navigate(page, path());
    await page.waitForFunction(() => document.querySelector(".materials-page[aria-busy=true]"));
    await arrivals[0].promise;
    await navigate(page, path({ business_id: config.other_business_id }));
    await arrivals[1].promise;
    await navigate(page, path()); await ready(page);
    await page.getByRole("button", { name: "加载更多来源", exact: true }).click();
    await page.getByText("共 25 份来源，已显示 25 份。", { exact: true }).waitFor();
    await Promise.all(held.map(release => release()));
    await page.unroute("**/api/review/materials?**");
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert.equal(await page.locator(".material-row").count(), 25);
    assert.equal(new URL(page.url()).searchParams.get("business_id"), config.current_business_id);
    checks.push("spa_A_B_A_aborts_old_reads_and_late_real_responses_do_not_replace_latest_pages");

    const lateDownloads = [];
    page.on("download", download => lateDownloads.push(download));
    for (const [index, button] of [[1, "查看说明"], [0, "下载原件"]]) {
      if (new URL(page.url()).searchParams.get("business_id") !== config.current_business_id) {
        await navigate(page, path()); await ready(page);
      }
      let release;
      const arrived = deferred();
      await page.route("**/api/review/sources/*/content?**", async route => {
        const response = await route.fetch();
        assert.equal(response.status(), 200);
        await new Promise(done => { release = async () => { try { await route.fulfill({ response }); } catch {} done(); }; arrived.resolve(); });
      });
      await page.locator(".material-row").nth(index).getByRole("button", { name: button, exact: true }).click();
      await arrived.promise;
      const aborted = page.waitForEvent("requestfailed", { predicate: request => new URL(request.url()).pathname.startsWith("/api/review/sources/") });
      void aborted.catch(() => {});
      await navigate(page, path({ business_id: config.other_business_id })); await ready(page);
      await aborted;
      await release(); await page.unroute("**/api/review/sources/*/content?**");
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.equal(await page.locator(".material-text").count(), 0);
      assert.equal(lateDownloads.length, 0);
    }
    checks.push("route_switch_cancels_statement_and_download_without_late_text_or_file");

    let releaseContext, contextArrived;
    const arrived = new Promise(resolve => { contextArrived = resolve; });
    await page.route("**/api/dashboard/context?**", async route => {
      const response = await route.fetch();
      assert.equal(response.status(), 200);
      await new Promise(resolve => { releaseContext = async () => { try { await route.fulfill({ response }); } catch {} resolve(); }; contextArrived(); });
    });
    await page.getByRole("link", { name: "返回看板", exact: true }).click();
    await arrived;
    await page.goBack(); await ready(page);
    await releaseContext(); await page.unroute("**/api/dashboard/context?**");
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert.equal(new URL(page.url()).pathname, "/review/materials");
    assert.equal(new URL(page.url()).searchParams.has("period"), false);
    await page.getByRole("link", { name: "返回看板", exact: true }).click();
    await page.locator(".module-header h1").filter({ hasText: /经营简报$/ }).waitFor();
    assert.equal(await page.locator(".module-nav a").count(), 5);
    checks.push("late_dashboard_context_cannot_rewrite_deep_link_and_return_keeps_five_pages");

    await page.goto(config.origin + path()); await ready(page);
    await page.locator(".material-row").nth(1).getByRole("button", { name: "查看说明", exact: true }).click();
    await page.locator(".material-text").waitFor();
    await page.setViewportSize({ width: 390, height: 844 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: join(config.output_directory, "materials-mobile.png"), fullPage: true });
    await page.setViewportSize({ width: 1200, height: 900 });
    await page.screenshot({ path: join(config.output_directory, "materials-desktop.png"), fullPage: true });
    assert.deepEqual(errors, []);
    await context.close();
    return { status: "passed", checks, page_errors: errors, evidence: { source_count: 25, file_sha256: config.file_sha256 } };
  } catch (error) {
    if (diagnosticPage && !diagnosticPage.isClosed()) await diagnosticPage.screenshot({ path: join(config.output_directory, "failure.png"), fullPage: true }).catch(() => {});
    return { status: "failed", checks, page_errors: errors, message: String(error.message).replace(/https?:\/\/[^\s"']+/g, "[browser URL]"),
      visible_text: diagnosticPage && !diagnosticPage.isClosed() ? (await diagnosticPage.locator("body").innerText()).slice(0, 8000) : "",
      stack: String(error.stack).replace(/https?:\/\/[^\s"']+/g, "[browser URL]") };
  } finally { await browser.close(); }
}

if (require.main === module) {
  let raw = "";
  process.stdin.setEncoding("utf8"); process.stdin.on("data", chunk => { raw += chunk; });
  process.stdin.on("end", async () => {
    const timer = setTimeout(() => { console.log(JSON.stringify({ status: "failed", phase: "functional_timeout" })); process.exit(1); }, 180000);
    try { console.log(JSON.stringify(await run(JSON.parse(raw)))); }
    catch (error) { console.log(JSON.stringify({ status: "failed", message: String(error.message).replace(/https?:\/\/[^\s"']+/g, "[browser URL]"), stack: String(error.stack).replace(/https?:\/\/[^\s"']+/g, "[browser URL]") })); process.exitCode = 1; }
    finally { clearTimeout(timer); }
  });
}
