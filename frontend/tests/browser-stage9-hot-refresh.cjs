// Hot, whole-page refresh timing against an isolated synthetic service and release build.
// Input is JSON on stdin: origin, ticket_url, playwright_module, channel,
// companies [{id, period, state}], optional warmups and repeats. Output is JSON.
const assert = require("node:assert/strict");
const fs = require("node:fs");

const modules = [
  { key: "brief", path: "/", action: "brief", heading: /经营简报$/, visible: "#overview, #monthly-review-title" },
  { key: "funds", path: "/funds", action: "funds", heading: "资金总览", visible: "#funds-overview, #bank-details" },
  { key: "employees", path: "/employees", action: "employees", heading: "员工与薪酬概览", visible: "#employees-overview, #employee-list-title" },
  { key: "assets", path: "/assets", action: "assets", heading: "长期资产概览", visible: "#assets-overview, #asset-list-title" },
  { key: "reports", path: "/reports", action: "quarterly-report", heading: "季度财务报表", visible: "#report-overview, .summary-grid, #report-statements" },
];
const defaultCollections = {
  brief: ["vouchers", "open_items"],
  funds: ["accounts", "movements", "statements", "investment_products", "investment_events"],
  employees: ["employees", "labor_sources"],
  assets: ["assets", "projects"],
};
const percentile = (sorted, proportion) => sorted[Math.ceil(sorted.length * proportion) - 1];
const sanitize = value => String(value)
  .replace(/https?:\/\/[^\s"']+/g, "[browser URL]")
  .replace(/[A-Za-z]:\\[^\s"']+/g, "[local path]")
  .replace(/[?&](?:ticket|capability|token)=[^&\s"']+/gi, "[redacted]");

function waitForLater(promise) {
  // Navigation/render checks may outlast Playwright's response timeout. Mark
  // rejection handled immediately; awaiting the original promise still throws.
  void promise.catch(() => {});
  return promise;
}

async function run(config) {
  assert(config?.origin && config?.ticket_url && config?.playwright_module);
  assert(Array.isArray(config.companies) && config.companies.length >= 1);
  const company = config.companies.find(item => item.state === "prepared")
    ?? config.companies.find(item => item.state === "closed")
    ?? config.companies[0];
  assert(company.id && company.period && company.state !== "empty");
  const quarter = config.quarter ?? `${company.period.slice(0, 4)}-Q${Math.ceil(Number(company.period.slice(5)) / 3)}`;
  const warmups = config.warmups ?? 3, samples = config.repeats ?? 30;
  assert(Number.isInteger(warmups) && warmups >= 1);
  assert(Number.isInteger(samples) && samples >= 1);
  const { chromium } = require(config.playwright_module);
  const browser = await chromium.launch({ channel: config.channel, headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  await page.addInitScript(({ selectedCompany, selectedPeriod, selectedQuarter, selectedState }) => {
    performance.setResourceTimingBufferSize(1000);
    const specs = {
      "/": { action: "brief", visible: ["#overview", "#monthly-review-title"] },
      "/funds": { action: "funds", visible: ["#funds-overview", "#bank-details"] },
      "/employees": { action: "employees", visible: ["#employees-overview", "#employee-list-title"] },
      "/assets": { action: "assets", visible: ["#assets-overview", "#asset-list-title"] },
      "/reports": { action: "quarterly-report", visible: ["#report-overview", ".summary-grid", "#report-statements"] },
    };
    const spec = specs[location.pathname];
    if (!spec) return;
    let frames = 0;
    const observe = () => {
      const required = ["context", spec.action,
        ...(spec.action === "brief" ? ["close-review"] : [])];
      const resources = performance.getEntriesByType("resource");
      const networkComplete = required.every(action => resources.some(entry => {
        const url = new URL(entry.name);
        if (url.pathname !== `/api/dashboard/${action}`
          || url.searchParams.get("company_id") !== selectedCompany) return false;
        if (action === "context") return true;
        if (action === "quarterly-report") {
          return url.searchParams.get("year") === selectedQuarter.slice(0, 4)
            && url.searchParams.get("quarter") === selectedQuarter.slice(-1);
        }
        return url.searchParams.get("period") === selectedPeriod;
      })) && (spec.action !== "brief" || resources.some(entry => {
        const url = new URL(entry.name);
        return url.pathname === "/api/dashboard/brief" && url.searchParams.get("company_id") === selectedCompany
          && url.searchParams.get("period") === selectedPeriod
          && (url.searchParams.get("preparation") === "complete" || resources.some(other => {
            const followup = new URL(other.name);
            return followup.pathname === "/api/dashboard/period-preparation"
              && followup.searchParams.get("company_id") === selectedCompany
              && followup.searchParams.get("period") === selectedPeriod;
          }));
      }));
      const ready = document.querySelector(".module-header")?.getAttribute("aria-busy") === "false"
        && spec.visible.every(selector => document.querySelector(selector)?.getClientRects().length)
        && (spec.action !== "brief" || (
          document.querySelector(".period-preparation")?.getClientRects().length
          && document.querySelector(`.close-review .review-state.${selectedState}`)?.getClientRects().length
          && document.querySelector(".close-review .accounting-summary")?.getClientRects().length
        ));
      frames = networkComplete && ready ? frames + 1 : 0;
      if (frames >= 2) {
        window.__stage9NavigationRenderedAt = performance.now();
        window.__stage9NavigationRenderedEpochMs = Date.now();
      }
      else if (performance.now() < 30000) requestAnimationFrame(observe);
    };
    requestAnimationFrame(observe);
  }, {
    selectedCompany: company.id, selectedPeriod: company.period, selectedQuarter: quarter,
    selectedState: company.state === "closed" ? "closed" : "prepared",
  });
  const pageErrors = [], apiFailures = [];
  const allowedNavigationAborts = new Set();
  page.on("pageerror", error => pageErrors.push(sanitize(error.message)));
  page.on("response", response => {
    if (new URL(response.url()).pathname.startsWith("/api/") && response.status() >= 400) {
      apiFailures.push({ path: new URL(response.url()).pathname, status: response.status() });
    }
  });
  page.on("requestfailed", request => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/")) apiFailures.push({
      path: url.pathname, company_id: url.searchParams.get("company_id"),
      period: url.searchParams.get("period"),
      error: sanitize(request.failure()?.errorText ?? "network_failed"),
    });
  });
  const result = {};
  const partialNavigation = {};

  function address(module) {
    const url = new URL(module.path, config.origin);
    url.searchParams.set("company_id", company.id);
    url.searchParams.set("period", company.period);
    if (module.key === "reports") url.searchParams.set("quarter", quarter);
    return url.href;
  }
  function matchesAction(url, action) {
    if (url.pathname !== `/api/dashboard/${action}` || url.searchParams.get("company_id") !== company.id) return false;
    if (action === "context") return true;
    if (action === "quarterly-report") {
      return url.searchParams.get("year") === quarter.slice(0, 4)
        && url.searchParams.get("quarter") === quarter.slice(-1);
    }
    return url.searchParams.get("period") === company.period;
  }
  function response(action) {
    return waitForLater(page.waitForResponse(reply => {
      const url = new URL(reply.url());
      return matchesAction(url, action);
    }));
  }
  function assertNoBrowserFailures() {
    assert.deepEqual(pageErrors, [], "browser error");
    assert.deepEqual(apiFailures.filter(failure => !allowedNavigationAborts.has(failure)), [], "API error");
  }
  async function verifyReply(reply, module, selected = company, { requireVouchers = true } = {}) {
    const target = new URL(reply.url());
    assert.equal(reply.status(), 200, `${module.key}: ${target.pathname} failed`);
    const payload = await reply.json();
    assert.equal(payload.read_context?.company_id ?? payload.current_company?.company_id ?? payload.company_id, selected.id);
    if (target.pathname === "/api/dashboard/quarterly-report") {
      const selectedQuarter = selected.id === company.id
        ? quarter : `${selected.period.slice(0, 4)}-Q${Math.ceil(Number(selected.period.slice(5)) / 3)}`;
      assert.equal(target.searchParams.get("year"), selectedQuarter.slice(0, 4));
      assert.equal(target.searchParams.get("quarter"), selectedQuarter.slice(-1));
    } else if (target.pathname !== "/api/dashboard/context") {
      assert.equal(target.searchParams.get("period"), selected.period);
    }
    if (target.pathname === "/api/dashboard/close-review") {
      assert.equal(payload.state, selected.state, `${module.key}: close review changed state`);
      if (selected.preview_digest) {
        assert.equal(payload.preview_digest, selected.preview_digest, `${module.key}: close preview was replaced`);
      }
    }
    if (target.pathname === `/api/dashboard/${module.action}`) {
      const requestedLimit = target.searchParams.get("limit");
      assert(requestedLimit === null || requestedLimit === "100", `${module.key}: default page limit changed`);
      if (module.key === "reports") {
        assert.deepEqual(payload.statements?.map(item => item.key),
          ["balance_sheet", "profit_statement", "cash_flow_statement"],
          "reports: default statements missing");
      } else {
        assert(payload.data, `${module.key}: missing main data`);
        for (const section of defaultCollections[module.key]) {
          const collection = payload.data.collections?.[section];
          assert(collection, `${module.key}: default ${section} collection missing`);
          assert.equal(collection.items.length, collection.page.returned_count, `${module.key}: incomplete default ${section} collection`);
          assert.equal(collection.items.length, Math.min(100, collection.page.filtered_count), `${module.key}: default ${section} collection was truncated`);
        }
        if (module.key === "brief" && requireVouchers) assert(payload.data.voucher_count > 0, "brief: synthetic book returned empty vouchers");
      }
    }
    return payload;
  }
  async function rendered(module, selected = company) {
    await page.getByRole("heading", { name: module.heading }).first().waitFor();
    await page.waitForFunction(() => document.querySelector(".module-header")?.getAttribute("aria-busy") === "false");
    if (selected.name) {
      assert.equal((await page.locator(".company-switcher-name").textContent())?.trim(), selected.name);
    }
    for (const selector of module.visible.split(", ")) await page.locator(selector).waitFor();
    if (module.key === "brief") {
      await page.locator(".period-preparation").waitFor();
      await page.locator(".close-review").waitFor();
      const reviewState = selected.state === "closed" ? "closed" : selected.state ?? "prepared";
      await page.locator(`.close-review .review-state.${reviewState}`).waitFor();
      if (["prepared", "closed"].includes(reviewState)) await page.locator(".close-review .accounting-summary").waitFor();
    }
    assert.equal(await page.locator(".state-panel.error, .error-state, .review-error, .preparation-state[role=alert], .report-error").count(), 0, `${module.key}: technical error rendered`);
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  }
  async function navigationRendered(module) {
    await rendered(module);
    await page.waitForFunction(() => Number.isFinite(window.__stage9NavigationRenderedAt));
    return page.evaluate(() => ({
      elapsed_ms: window.__stage9NavigationRenderedAt,
      epoch_ms: window.__stage9NavigationRenderedEpochMs,
    }));
  }
  async function measure(module) {
    const contextReply = response("context");
    const mainReply = response(module.action);
    const reviewReply = module.key === "brief" ? response("close-review") : null;
    const refreshButton = page.locator(".module-header button.refresh");
    await refreshButton.evaluate((button, selection) => {
      button.addEventListener("click", () => {
        // ResourceTiming otherwise stops recording after the browser's default
        // 150 entries, and previous iterations must never satisfy this click.
        performance.setResourceTimingBufferSize(1000);
        performance.clearResourceTimings();
        const started = performance.now();
        window.__stage9RefreshStart = started;
        window.__stage9RenderedAt = null;
        let completeFrames = 0;
        const actions = ["context", selection.action,
          ...(selection.key === "brief" ? ["close-review"] : [])];
        const observed = () => {
          const resources = performance.getEntriesByType("resource").filter(entry => entry.startTime >= started);
          const networkComplete = actions.every(action => resources.some(entry => {
            const url = new URL(entry.name);
            if (url.pathname !== `/api/dashboard/${action}`
              || url.searchParams.get("company_id") !== selection.companyId
              || entry.responseEnd <= started) return false;
            if (action === "context") return true;
            if (action === "quarterly-report") {
              return url.searchParams.get("year") === selection.quarter.slice(0, 4)
                && url.searchParams.get("quarter") === selection.quarter.slice(-1);
            }
            return url.searchParams.get("period") === selection.period;
          })) && (selection.key !== "brief" || resources.some(entry => {
            const url = new URL(entry.name);
            return url.pathname === "/api/dashboard/brief" && url.searchParams.get("company_id") === selection.companyId
              && url.searchParams.get("period") === selection.period
              && (url.searchParams.get("preparation") === "complete" || resources.some(other => {
                const followup = new URL(other.name);
                return followup.pathname === "/api/dashboard/period-preparation"
                  && followup.searchParams.get("company_id") === selection.companyId
                  && followup.searchParams.get("period") === selection.period;
              }));
          }));
          const mainReady = document.querySelector(".module-header")?.getAttribute("aria-busy") === "false"
            && selection.visible.every(selector => document.querySelector(selector)?.getClientRects().length);
          const annexReady = selection.key !== "brief" || (
            document.querySelector(".period-preparation")?.getClientRects().length
            && document.querySelector(`.close-review .review-state.${selection.state}`)?.getClientRects().length
            && document.querySelector(".close-review .accounting-summary")?.getClientRects().length
          );
          completeFrames = networkComplete && mainReady && annexReady ? completeFrames + 1 : 0;
          if (completeFrames >= 2) {
            // Timestamp in the page, before any Playwright assertions/RPC delay.
            window.__stage9RenderedAt = performance.now();
          } else if (performance.now() - started < 30000) requestAnimationFrame(observed);
        };
        requestAnimationFrame(observed);
      }, { once: true, capture: true });
    }, { action: module.action, key: module.key, companyId: company.id,
      period: company.period, quarter,
      visible: module.visible.split(", "), state: company.state === "closed" ? "closed" : "prepared" });
    await refreshButton.click();
    await page.waitForFunction(() => window.__stage9RenderedAt !== null);
    const elapsed = await page.evaluate(() => {
      const measured = window.__stage9RenderedAt - window.__stage9RefreshStart;
      delete window.__stage9RefreshStart;
      delete window.__stage9RenderedAt;
      return measured;
    });
    for (const pending of [contextReply, mainReply, reviewReply].filter(Boolean)) {
      await verifyReply(await pending, module);
    }
    await rendered(module);
    assertNoBrowserFailures();
    return elapsed;
  }
  async function measureCompanySwitch(target) {
    await page.goto(address(modules[0]));
    await rendered(modules[0]);
    const actions = ["context", "brief", "close-review"];
    const replies = [];
    const capture = reply => {
      const url = new URL(reply.url());
      if (actions.some(action => url.pathname === `/api/dashboard/${action}`)
        && url.searchParams.get("company_id") === target.id) replies.push(reply);
    };
    const contextReply = waitForLater(page.waitForResponse(reply => {
      const url = new URL(reply.url());
      return url.pathname === "/api/dashboard/context" && url.searchParams.get("company_id") === target.id;
    }));
    const failureStart = apiFailures.length;
    page.on("response", capture);
    try {
    const selector = page.getByLabel("切换公司", { exact: true });
    await selector.evaluate((element, selection) => {
      element.addEventListener("change", () => {
        performance.setResourceTimingBufferSize(1000);
        performance.clearResourceTimings();
        const started = performance.now();
        window.__stage9SwitchStart = started;
        window.__stage9SwitchRenderedAt = null;
        let frames = 0;
        const observe = () => {
          const period = new URL(location.href).searchParams.get("period");
          const resources = performance.getEntriesByType("resource").filter(entry => entry.startTime >= started);
          const complete = period && ["context", "brief", "close-review"].every(action =>
            resources.some(entry => {
              const url = new URL(entry.name);
              return url.pathname === `/api/dashboard/${action}`
                && url.searchParams.get("company_id") === selection.companyId
                && (action === "context" || url.searchParams.get("period") === period);
            })) && resources.some(entry => {
              const url = new URL(entry.name);
              return url.pathname === "/api/dashboard/brief"
                && url.searchParams.get("company_id") === selection.companyId
                && url.searchParams.get("period") === period
                && (url.searchParams.get("preparation") === "complete" || resources.some(other => {
                  const followup = new URL(other.name);
                  return followup.pathname === "/api/dashboard/period-preparation"
                    && followup.searchParams.get("company_id") === selection.companyId
                    && followup.searchParams.get("period") === period;
                }));
            });
          const ready = document.querySelector(".module-header")?.getAttribute("aria-busy") === "false"
            && document.querySelector(".company-switcher-name")?.textContent?.trim() === selection.companyName
            && document.querySelector("#overview")?.getClientRects().length
            && document.querySelector("#monthly-review-title")?.getClientRects().length
            && document.querySelector(".period-preparation")?.getClientRects().length
            && document.querySelector(".close-review .review-state")?.getClientRects().length
            && document.querySelector(".close-review .review-note")?.getClientRects().length;
          frames = complete && ready ? frames + 1 : 0;
          if (frames >= 2) window.__stage9SwitchRenderedAt = performance.now();
          else if (performance.now() - started < 30000) requestAnimationFrame(observe);
        };
        requestAnimationFrame(observe);
      }, { once: true, capture: true });
    }, { companyId: target.id, companyName: target.name });
    await selector.selectOption(target.id);
    const selectedContext = await verifyReply(await contextReply, modules[0], target);
    const expectedPeriod = selectedContext.periods.some(item => item.key === company.period)
      ? company.period : selectedContext.default_period;
    assert(expectedPeriod && selectedContext.periods.some(item => item.key === expectedPeriod),
      "switch: context has no selectable default month");
    await page.waitForFunction(() => Number.isFinite(window.__stage9SwitchRenderedAt));
    const elapsed = await page.evaluate(() => window.__stage9SwitchRenderedAt - window.__stage9SwitchStart);
    const finalReply = action => replies.find(reply => {
      const url = new URL(reply.url());
      return url.pathname === `/api/dashboard/${action}` && url.searchParams.get("period") === expectedPeriod;
    });
    const briefReply = finalReply("brief"), reviewReply = finalReply("close-review");
    assert(briefReply && reviewReply, "switch: final month requests did not complete");
    const selected = { ...target, period: expectedPeriod, preview_digest: undefined };
    await verifyReply(briefReply, modules[0], selected, { requireVouchers: expectedPeriod === target.period });
    const review = await reviewReply.json();
    assert(["prepared", "closed", "covered", "unprepared", "stale"].includes(review.state),
      "switch: unsupported close-review state");
    assert.equal(review.period, expectedPeriod, "switch: close review used another month");
    selected.state = review.state;
    if (expectedPeriod === target.period) selected.preview_digest = target.preview_digest;
    await verifyReply(reviewReply, modules[0], selected);
    await rendered(modules[0], selected);
    assert.equal(new URL(page.url()).searchParams.get("company_id"), target.id);
    assert.equal(new URL(page.url()).searchParams.get("period"), expectedPeriod);
    for (const failure of apiFailures.slice(failureStart)) {
      if (failure.error === "net::ERR_ABORTED"
        && (failure.company_id !== target.id || failure.period !== expectedPeriod)) {
        allowedNavigationAborts.add(failure);
      }
    }
    assertNoBrowserFailures();
    return { status: "measured", from_company_id: company.id, to_company_id: target.id,
      selected_period: expectedPeriod, review_state: review.state, elapsed_ms: elapsed };
    } finally {
      page.off("response", capture);
    }
  }
  try {
    const ticket = new URL(config.ticket_url);
    ticket.searchParams.set("company_id", company.id);
    ticket.searchParams.set("period", company.period);
    const firstReplies = [response("context"), response("brief"), response("close-review")];
    await page.goto(ticket.href);
    const firstNavigation = await navigationRendered(modules[0]);
    for (const reply of await Promise.all(firstReplies)) await verifyReply(reply, modules[0]);
    assertNoBrowserFailures();
    const firstLoad = firstNavigation.elapsed_ms;
    await page.getByLabel("切换公司", { exact: true }).waitFor();
    const navigationBase = {
      first_load_ms: firstLoad,
      first_render_epoch_ms: firstNavigation.epoch_ms,
    };
    Object.assign(partialNavigation, navigationBase);
    if (config.foreground_stream) {
      assert(typeof config.stop_file === "string" && config.stop_file.length > 0);
      process.stdout.write(`${JSON.stringify({ kind: "ready", company_id: company.id,
        period: company.period, first_render_ms: firstLoad })}\n`);
      while (!fs.existsSync(config.stop_file)) {
        const elapsed = await measure(modules[0]);
        process.stdout.write(`${JSON.stringify({ kind: "sample", elapsed_ms: elapsed })}\n`);
      }
      assertNoBrowserFailures();
      return { status: "passed", company_id: company.id, period: company.period,
        foreground_samples_complete: true };
    }
    if (config.navigation_only === "cold") {
      assertNoBrowserFailures();
      return {
        status: "passed", company_id: company.id, period: company.period,
        pages: {}, navigation: navigationBase,
      };
    }
    if (config.navigation_only === "switch") {
      const target = config.companies.find(item => item.id !== company.id
        && (item.state === "prepared" || item.state === "closed") && item.period);
      assert(target, "Switch-only measurement requires a prepared second company");
      for (let i = 0; i < warmups; i++) await measureCompanySwitch(target);
      const timings = [];
      let lastSwitch;
      partialNavigation.company_switch = { status: "partial", from_company_id: company.id,
        to_company_id: target.id, samples: timings };
      for (let i = 0; i < samples; i++) {
        lastSwitch = await measureCompanySwitch(target);
        timings.push(lastSwitch.elapsed_ms);
      }
      const sorted = [...timings].sort((a, b) => a - b);
      assertNoBrowserFailures();
      return {
        status: "passed", company_id: company.id, period: company.period,
        warmups, sample_count: samples, pages: {},
        navigation: { ...navigationBase, company_switch: {
          status: "measured", from_company_id: company.id, to_company_id: target.id,
          selected_period: lastSwitch.selected_period, review_state: lastSwitch.review_state,
          samples: timings, median_ms: percentile(sorted, 0.5),
          p95_ms: percentile(sorted, 0.95), max_ms: sorted.at(-1),
        } },
      };
    }
    const coldOpen = {};
    for (const module of modules) {
      const expected = [response("context"), response(module.action),
        ...(module.key === "brief" ? [response("close-review")] : [])];
      result[module.key] = { samples: [] };
      await page.goto(address(module));
      coldOpen[module.key] = (await navigationRendered(module)).elapsed_ms;
      partialNavigation.cold_open_ms = coldOpen;
      for (const reply of await Promise.all(expected)) await verifyReply(reply, module);
      for (let i = 0; i < warmups; i++) await measure(module);
      const timings = [];
      result[module.key] = { samples: timings };
      for (let i = 0; i < samples; i++) timings.push(await measure(module));
      const sorted = [...timings].sort((a, b) => a - b);
      result[module.key] = {
        samples: timings,
        median_ms: percentile(sorted, 0.5),
        p95_ms: percentile(sorted, 0.95),
        max_ms: sorted.at(-1),
        over_500_ms: timings.filter(value => value >= 500).length,
      };
    }
    const switchTarget = config.companies.find(item => item.id !== company.id
      && (item.state === "prepared" || item.state === "closed") && item.period);
    partialNavigation.company_switch = switchTarget
      ? { status: "partial", from_company_id: company.id, to_company_id: switchTarget.id }
      : { status: "unavailable", reason: "single_eligible_company" };
    const companySwitch = switchTarget
      ? await measureCompanySwitch(switchTarget)
      : { status: "unavailable", reason: "single_eligible_company" };
    partialNavigation.company_switch = companySwitch;
    assertNoBrowserFailures();
    return {
      status: Object.values(result).every(page => page.over_500_ms === 0) ? "passed" : "over_target",
      company_id: company.id, period: company.period, warmups, sample_count: samples, pages: result,
      navigation: { ...navigationBase, cold_open_ms: coldOpen, company_switch: companySwitch },
    };
  } catch (error) {
    return {
      status: "failed", message: sanitize(error?.message || error),
      company_id: company.id, period: company.period, pages: result,
      navigation: partialNavigation, warmups, sample_count: samples,
      page_errors: pageErrors, api_failures: apiFailures,
    };
  } finally {
    await context.close();
    await browser.close();
  }
}

if (require.main === module) {
  let input = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", chunk => { input += chunk; });
  process.stdin.on("end", async () => {
    try {
      const result = await run(JSON.parse(input));
      process.stdout.write(`${JSON.stringify(result)}\n`);
      if (result.status !== "passed") process.exitCode = 1;
    } catch (error) {
      process.stdout.write(`${JSON.stringify({ status: "failed", message: sanitize(error?.message || error) })}\n`);
      process.exitCode = 1;
    }
  });
}

module.exports = { waitForLater };
