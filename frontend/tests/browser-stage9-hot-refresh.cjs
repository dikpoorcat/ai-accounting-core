// Hot, whole-page refresh timing against an isolated synthetic service and release build.
// Input is JSON on stdin: origin, ticket_url, playwright_module, channel,
// companies [{id, period, state}], optional warmups and repeats. Output is JSON.
// The strengthened endpoint below covers hot refresh of one fixed synthetic
// company/period/snapshot only. Navigation/company-switch endpoints retain
// their existing checks and have not been validated by this correction.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

// Load this harness's own generated contracts once, before browser timing starts.
const responseSchemas = JSON.parse(fs.readFileSync(
  path.join(__dirname, "../src/api/generated/dashboardResponseSchemas.json"), "utf8"));
const responseVersions = Object.freeze(Object.fromEntries([
  "dashboard_context", "dashboard_brief", "dashboard_funds", "dashboard_employees",
  "dashboard_assets", "dashboard_business_status", "dashboard_quarterly_report",
  "dashboard_period_preparation", "dashboard_close_review", "report_export_status",
].map(name => {
  const schema = responseSchemas?.[name];
  const version = schema?.properties?.schema_version;
  const expected = version?.const ?? (Array.isArray(version?.enum) && version.enum.length === 1
    ? version.enum[0] : undefined);
  assert(schema?.type === "object" && Array.isArray(schema.required)
    && schema.required.includes("schema_version")
    && version?.type === "integer" && Number.isSafeInteger(expected) && expected > 0
    && (!Object.hasOwn(version, "const") || version.const === expected)
    && (!Object.hasOwn(version, "enum")
      || (Array.isArray(version.enum) && version.enum.length === 1 && version.enum[0] === expected)),
  `${name}: missing or invalid generated schema version`);
  return [name, expected];
})));
const mainContractNames = {
  brief: "dashboard_brief", funds: "dashboard_funds", employees: "dashboard_employees",
  assets: "dashboard_assets", reports: "dashboard_quarterly_report",
};

const modules = [
  { key: "brief", path: "/", action: "brief", heading: /经营简报$/, visible: "#overview, #activity, #open-items, #owner-tasks" },
  { key: "funds", path: "/funds", action: "funds", heading: "资金总览", visible: "#funds-overview, #bank-details" },
  { key: "employees", path: "/employees", action: "employees", heading: "员工与薪酬概览", visible: "#employees-overview, #employee-list-title" },
  { key: "assets", path: "/assets", action: "assets", heading: "长期资产概览", visible: "#assets-overview, #asset-list-title" },
  { key: "reports", path: "/reports", action: "quarterly-report", heading: "季度财务报表", visible: "#report-overview, .summary-grid, #report-statements" },
];
const defaultCollections = {
  brief: ["activity", "vouchers", "open_items"],
  funds: ["accounts", "movements", "statements", "investment_products", "investment_events"],
  employees: ["employees", "labor_sources"],
  assets: ["assets", "projects"],
};
// Only the default business result, not global jobs or on-demand details.
const refreshRoots = {
  brief: ".brief-content", funds: ".funds-result", employees: ".employee-result",
  assets: ".asset-result", reports: ".report-dashboard",
};
function readRefreshProjection(document, rootSelector) {
  const root = document.querySelector(rootSelector);
  if (!root) return null;
  // Each visible child contributes its complete innerText: financial cards and
  // the conditional workforce section are included in the existing two-frame
  // stable-projection endpoint without adding assertions to the timed path.
  const visibleText = Array.from(root.children)
    .filter(node => node.getClientRects().length
      && document.defaultView.getComputedStyle(node).visibility !== "hidden")
    .map(node => node.innerText);
  // Reports' observation time changes on refresh; the period and all business
  // values remain required. Remove only this known non-business clock value.
  const clock = root.querySelector(".dashboard-hero-eyebrow");
  if (clock && clock.innerText.includes(" · 更新于 ")) {
    const [period] = clock.innerText.split(" · 更新于 ");
    for (let i = 0; i < visibleText.length; i++) {
      visibleText[i] = visibleText[i].replace(clock.innerText, period);
    }
  }
  return JSON.stringify({
    company: document.querySelector(".company-switcher-name")?.innerText,
    period: document.querySelector(".module-header select")?.value,
    status: document.querySelector(".module-header .period-status")?.innerText,
    content: visibleText,
    controls: Array.from(root.querySelectorAll("select, [aria-current], [aria-pressed], [aria-selected], details"))
      .map(node => [node.tagName, node.value ?? null, node.open ?? null,
        node.getAttribute("aria-current"), node.getAttribute("aria-pressed"), node.getAttribute("aria-selected")]),
  });
}

const observeHotRefresh = new Function("button", "selection", `
  const readProjection = ${readRefreshProjection.toString()};

  const briefReady = () => {
    const content = document.querySelector(".brief-content");
    const state = content?.getAttribute("data-month-state");
    const required = content?.getAttribute("data-owner-review-required");
    const prompt = document.querySelector("#owner-tasks .owner-review-request");
    return ["open", "closed", "covered"].includes(state)
      && ["true", "false"].includes(required)
      && (required === "true" ? state === "open" && prompt?.getClientRects().length
      && prompt.querySelector("button")?.getClientRects().length : !prompt)
      && !document.querySelector(".close-review, #monthly-review, #close-review-title");
  };
  button.addEventListener("click", () => {
    const started = performance.now();
    // ResourceTiming otherwise stops recording after the browser's default
    // 150 entries, and previous iterations must never satisfy this click.
    performance.setResourceTimingBufferSize(1000);
    performance.clearResourceTimings();
    window.__stage9RefreshStart = started;
    window.__stage9RenderedAt = null;
    let completeFrames = 0;
    const header = document.querySelector(".module-header");
    let sawBusy = header?.getAttribute("aria-busy") === "true";
    const busyObserver = new MutationObserver(records => {
      sawBusy ||= header?.getAttribute("aria-busy") === "true"
        || records.some(record => record.oldValue === "true");
    });
    if (header) busyObserver.observe(header, {
      attributes: true, attributeFilter: ["aria-busy"], attributeOldValue: true,
    });
    const actions = ["context", selection.action];
    const observed = () => {
      const resources = performance.getEntriesByType("resource").filter(entry => entry.startTime >= started);
      const networkComplete = actions.every(action => resources.some(entry => {
        const url = new URL(entry.name);
        if (url.pathname !== \`/api/dashboard/\${action}\`
          || url.searchParams.get("company_id") !== selection.companyId
          || entry.responseEnd <= started) return false;
        if (action === "context") return true;
        if (action === "quarterly-report") {
          return url.searchParams.get("year") === selection.quarter.slice(0, 4)
            && url.searchParams.get("quarter") === selection.quarter.slice(-1);
        }
        return url.searchParams.get("period") === selection.period;
      }));
      const mainReady = document.querySelector(".module-header")?.getAttribute("aria-busy") === "false"
        && selection.visible.every(selector => document.querySelector(selector)?.getClientRects().length);
      const annexReady = selection.key !== "brief" || briefReady();
      const projectionReady = !selection.projection || (networkComplete && mainReady && annexReady && sawBusy
        && readProjection(document, selection.root) === selection.projection);
      completeFrames = networkComplete && mainReady && annexReady && projectionReady ? completeFrames + 1 : 0;
      if (completeFrames >= 2) {
        // Timestamp in the page, before any Playwright assertions/RPC delay.
        window.__stage9RenderedAt = performance.now();
        busyObserver.disconnect();
      } else if (performance.now() - started < 30000) requestAnimationFrame(observed);
      else busyObserver.disconnect();
    };
    requestAnimationFrame(observed);
  }, { once: true, capture: true });

`);

const captureRefreshProjection = new Function("selection",
  `return (${readRefreshProjection.toString()})(document, selection.root);`);

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

// Pure association/value extraction, also serialized into the final browser
// evaluate. Resource timing is browser network work, not a source CPU profile.
function captureRefreshResources(entries, selection, clickStart, renderAt, timeOrigin) {
  const actions = ["context", selection.action];
  const resources = Object.fromEntries(actions.map(action => [action, []]));
  for (const entry of entries) {
    if (!(entry.startTime >= clickStart)) continue;
    let url;
    try { url = new URL(entry.name); } catch { continue; }
    const action = actions.find(item => url.pathname === `/api/dashboard/${item}`);
    if (!action || url.searchParams.get("company_id") !== selection.companyId) continue;
    if (action !== "context") {
      if (action === "quarterly-report") {
        if (url.searchParams.get("year") !== selection.quarter.slice(0, 4)
          || url.searchParams.get("quarter") !== selection.quarter.slice(-1)) continue;
      } else if (url.searchParams.get("period") !== selection.period) continue;
    }
    const query = { company_id: url.searchParams.get("company_id") };
    for (const key of action === "quarterly-report" ? ["year", "quarter"]
      : action === "context" ? [] : ["period"]) query[key] = url.searchParams.get(key);
    const value = { action, path: url.pathname, query };
    for (const key of ["startTime", "fetchStart", "domainLookupStart", "domainLookupEnd",
      "connectStart", "secureConnectionStart", "connectEnd", "requestStart", "responseStart",
      "responseEnd", "duration", "transferSize", "encodedBodySize", "decodedBodySize",
      "responseStatus"]) value[key] = Number.isFinite(entry[key]) ? entry[key] : null;
    for (const key of ["initiatorType", "nextHopProtocol"])
      value[key] = typeof entry[key] === "string" ? entry[key] : null;
    resources[action].push(value);
  }
  const diagnostics = [];
  if (!Number.isFinite(clickStart) || !Number.isFinite(renderAt) || renderAt < clickStart
    || !Number.isFinite(timeOrigin)) diagnostics.push("invalid_measurement_timestamps");
  for (const action of actions) {
    if (resources[action].length === 0) diagnostics.push(`${action}:missing_resource`);
    else if (resources[action].length > 1) diagnostics.push(`${action}:duplicate_resources`);
    for (const entry of resources[action]) {
      if (entry.responseEnd === null || entry.responseEnd <= clickStart
        || entry.responseEnd < entry.startTime || entry.responseEnd > renderAt
        || entry.duration === null || entry.duration < 0)
        diagnostics.push(`${action}:invalid_resource_timestamps`);
    }
  }
  const responseEnd = diagnostics.length === 0
    ? Math.max(...actions.map(action => resources[action][0].responseEnd)) : null;
  return {
    timeOrigin, clickStart, renderAt, resources, diagnostics,
    render_tail_ms: responseEnd === null ? null : renderAt - responseEnd,
    attribution: "Browser ResourceTiming timestamps are relative to timeOrigin; render tail includes response processing, JSON, reactive DOM work and the existing two RAF completion check, not source CPU attribution or isolated paint time.",
  };
}

// Constructed in Node, before measurements; browser execution uses no eval and
// captures diagnostics only after the original completion timestamp is set.
const captureRefreshMeasurement = new Function("selection", `
  const capture = ${captureRefreshResources.toString()};
  const clickStart = window.__stage9RefreshStart;
  const renderAt = window.__stage9RenderedAt;
  const measured = renderAt - clickStart;
  const resources = capture(performance.getEntriesByType("resource").map(entry => entry.toJSON()),
    selection, clickStart, renderAt, performance.timeOrigin);
  delete window.__stage9RefreshStart;
  delete window.__stage9RenderedAt;
  return { measured, resources };
`);

function verifyBriefState(data, selected = {}) {
  assert(["open", "closed", "covered"].includes(data.month_state), "brief: invalid typed month state");
  const locator = data.owner_review_request;
  assert(locator === null || (locator && typeof locator === "object"
    && typeof locator.preview_digest === "string" && /^[a-f0-9]{64}$/.test(locator.preview_digest)),
    "brief: invalid typed owner review locator");
  assert(data.month_state === "open" || locator === null, "brief: closed/covered month requests owner review");
  if (selected.state === "prepared") {
    assert.equal(data.month_state, "open", "brief: prepared source lost its open month state");
    assert(locator, "brief: prepared source lost its owner review prompt");
  } else if (["closed", "covered"].includes(selected.state)) {
    assert.equal(data.month_state, selected.state, "brief: source month state changed");
  }
  if (selected.preview_digest && selected.state === "prepared") {
    assert.equal(locator.preview_digest, selected.preview_digest, "brief: owner review preview was replaced");
  }
}

function wireFen(value, label, nullable = false) {
  if (nullable && value === null) return null;
  assert(typeof value === "string" && /^-?(0|[1-9][0-9]*)$/.test(value), `${label}: invalid integer fen`);
  return BigInt(value);
}
function formatFen(value) {
  if (value === null) return "暂无法确定";
  const amount = wireFen(value, "display amount"), absolute = amount < 0n ? -amount : amount;
  return `${amount < 0n ? "−" : ""}¥${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(absolute / 100n)}.${String(absolute % 100n).padStart(2, "0")}`;
}
function verifyCollection(collection, label, { cursor = false } = {}) {
  assert(collection && Array.isArray(collection.items) && collection.page, `${label}: collection missing`);
  const { page, items } = collection;
  for (const key of ["total_count", "filtered_count", "returned_count"]) {
    assert(Number.isSafeInteger(page[key]) && page[key] >= 0, `${label}: invalid ${key}`);
  }
  assert(page.filtered_count <= page.total_count, `${label}: filtered count exceeds full count`);
  assert.equal(items.length, page.returned_count, `${label}: incomplete collection`);
  assert(items.length <= 20, `${label}: page exceeds default 20`);
  if (!cursor) assert.equal(items.length, Math.min(20, page.filtered_count), `${label}: first page was truncated`);
  assert.equal(typeof page.has_more, "boolean", `${label}: missing continuation state`);
  assert(page.has_more ? typeof page.next_cursor === "string" && page.next_cursor.length > 0
    : page.next_cursor === null, `${label}: invalid continuation cursor`);
  if (!cursor) assert.equal(page.has_more, page.filtered_count > items.length, `${label}: continuation differs from full count`);
}
function verifyVoucher(voucher) {
  assert(typeof voucher.number === "string" && /^[1-9][0-9]*$/.test(voucher.number), "voucher: invalid public number");
  for (const field of ["voucher_version_id", "subject_id", "type", "kind", "summary", "list_summary", "business_amount_label"]) {
    assert(typeof voucher[field] === "string" && voucher[field], `voucher: missing ${field}`);
  }
  assert(["已入账", "冲正"].includes(voucher.state), "voucher: invalid state");
  assert(voucher.recognition && ["day", "month"].includes(voucher.recognition.precision), "voucher: missing date precision");
  assert(/^\d{4}-\d{2}$/.test(voucher.recognition.period), "voucher: invalid recognition month");
  assert.equal(typeof voucher.recognition.label, "string", "voucher: missing date label");
  assert(voucher.date === null || /^\d{4}-\d{2}-\d{2}$/.test(voucher.date), "voucher: invalid actual date");
  assert.equal(voucher.date, voucher.recognition.date, "voucher: recognition date differs");
  assert(voucher.recognition.precision !== "month" || voucher.date === null, "voucher: month precision invented an actual date");
  wireFen(voucher.business_amount_fen, "voucher business amount", true);
  const amount = wireFen(voucher.amount_fen, "voucher amount");
  assert(Array.isArray(voucher.lines) && voucher.lines.length >= 2, "voucher: missing complete entry lines");
  let debit = 0n, credit = 0n;
  for (const [index, line] of voucher.lines.entries()) {
    assert.equal(line.line_number, index + 1, "voucher: missing or reordered line");
    for (const field of ["code", "account", "party", "source_label"]) assert.equal(typeof line[field], "string", `voucher line: missing ${field}`);
    assert(line.code && line.account, "voucher line: missing account");
    const dr = wireFen(line.debit_fen, "voucher debit"), cr = wireFen(line.credit_fen, "voucher credit");
    assert(dr >= 0n && cr >= 0n && !(dr && cr), "voucher line: invalid debit/credit");
    debit += dr; credit += cr;
    assert(Array.isArray(line.parties), "voucher line: missing business objects");
    assert(["known", "multiple", "name_missing", "not_applicable", "unresolved"].includes(line.party_state), "voucher line: missing object state");
    for (const party of line.parties) {
      assert.equal(typeof party.name, "string", "voucher party: missing name");
      wireFen(party.amount_fen, "voucher party amount", true);
    }
  }
  assert.equal(debit, credit, "voucher: entries do not balance");
  assert.equal(debit, amount, "voucher: entry total differs from voucher amount");
  assert(Array.isArray(voucher.asset_members), "voucher: missing asset members");
  assert(voucher.asset === null || (voucher.asset && typeof voucher.asset.asset_id === "string"), "voucher: missing asset reference state");
  for (const asset of voucher.asset_members) {
    assert(typeof asset.asset_id === "string" && ["fixed", "intangible"].includes(asset.asset_type), "voucher: incomplete asset member");
    wireFen(asset.amount_fen, "voucher asset amount", true);
  }
}
function verifyMainPayload(payload, key, selected, target, { requireVouchers = true } = {}) {
  assert(Object.hasOwn(mainContractNames, key), `${key}: unknown main response contract`);
  assert.equal(payload.schema_version, responseVersions[mainContractNames[key]], `${key}: outdated response contract`);
  assert.equal(payload.read_context?.company_id, selected.id, `${key}: response used another company`);
  assert(/^\d{4}-\d{2}-\d{2}$/.test(payload.read_context?.as_of), `${key}: missing current-knowledge date`);
  assert(typeof payload.read_context?.read_version === "string" && payload.read_context.read_version, `${key}: missing read version`);
  if (key === "reports") {
    assert.deepEqual(payload.statements?.map(item => item.key), ["balance_sheet", "profit_statement", "cash_flow_statement"], "reports: default statements missing");
    for (const statement of payload.statements) {
      assert(statement.columns?.length && statement.rows?.length, "reports: empty full statement");
      for (const row of statement.rows) for (const column of statement.columns) wireFen(row.values[column.key], "report statement amount", true);
    }
    return;
  }
  assert.equal(payload.selected_period?.key, selected.period, `${key}: response used another month`);
  assert(typeof payload.snapshot_version === "string" && payload.snapshot_version, `${key}: missing snapshot`);
  assert.equal(payload.read_semantics?.knowledge, "current_knowledge", `${key}: wrong knowledge semantics`);
  assert.equal(payload.read_semantics?.accounting, "as_posted", `${key}: wrong accounting semantics`);
  assert.equal(payload.read_semantics?.system_time_replay, false, `${key}: unexpected historical replay`);
  assert(payload.data, `${key}: missing main data`);
  if (target.searchParams.has("expected_version")) assert.equal(payload.snapshot_version, target.searchParams.get("expected_version"), `${key}: local read crossed snapshots`);
  const section = target.searchParams.get("section"), cursor = target.searchParams.has("cursor");
  for (const name of section ? [section] : defaultCollections[key]) verifyCollection(payload.data.collections?.[name], `${key}/${name}`, { cursor });
  if (key === "brief") {
    verifyBriefState(payload.data, selected);
    const { vouchers, activity } = payload.data.collections;
    if (!section) {
      assert(payload.data.financial_position && payload.data.workforce_cost, "brief: main financial/workforce summaries missing");
      assert.equal(vouchers.page.total_count, payload.data.voucher_count, "brief: voucher total lost");
      assert.equal(activity.page.total_count, payload.data.activity_count, "brief: activity total lost");
      if (requireVouchers) assert(vouchers.page.total_count > 0 && activity.page.total_count > 0, "brief: synthetic book returned no vouchers/business");
    }
    for (const voucher of vouchers?.items ?? []) verifyVoucher(voucher);
    if (payload.data.focused_voucher) verifyVoucher(payload.data.focused_voucher);
  }
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
  const refreshProjections = new Map();
  page.setDefaultTimeout(30000);
  await page.addInitScript(({ selectedCompany, selectedPeriod, selectedQuarter }) => {
    performance.setResourceTimingBufferSize(1000);
    const specs = {
      "/": { action: "brief", visible: ["#overview", "#activity", "#open-items", "#owner-tasks"] },
      "/funds": { action: "funds", visible: ["#funds-overview", "#bank-details"] },
      "/employees": { action: "employees", visible: ["#employees-overview", "#employee-list-title"] },
      "/assets": { action: "assets", visible: ["#assets-overview", "#asset-list-title"] },
      "/reports": { action: "quarterly-report", visible: ["#report-overview", ".summary-grid", "#report-statements"] },
    };
    const spec = specs[location.pathname];
    if (!spec) return;
    const briefReady = () => {
      const content = document.querySelector(".brief-content");
      const state = content?.getAttribute("data-month-state");
      const required = content?.getAttribute("data-owner-review-required");
      const prompt = document.querySelector("#owner-tasks .owner-review-request");
      return ["open", "closed", "covered"].includes(state)
        && ["true", "false"].includes(required)
        && (required === "true" ? state === "open" && prompt?.getClientRects().length
          && prompt.querySelector("button")?.getClientRects().length : !prompt)
        && !document.querySelector(".close-review, #monthly-review, #close-review-title");
    };
    let frames = 0;
    const observe = () => {
      const required = ["context", spec.action];
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
      }));
      const ready = document.querySelector(".module-header")?.getAttribute("aria-busy") === "false"
        && spec.visible.every(selector => document.querySelector(selector)?.getClientRects().length)
        && (spec.action !== "brief" || briefReady());
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
  });
  const pageErrors = [], apiFailures = [], closeReviewRequests = [], legacyJobsRequests = [];
  const allowedCloseReviewRequests = new Set();
  page.on("request", request => {
    if (new URL(request.url()).pathname === "/api/dashboard/close-review") closeReviewRequests.push(request.url());
    if (new URL(request.url()).pathname === "/api/local/jobs") legacyJobsRequests.push("/api/local/jobs");
  });
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
    assert.deepEqual(legacyJobsRequests, [], "default page requested the retired task list");
    assert.deepEqual(closeReviewRequests.filter(url => !allowedCloseReviewRequests.has(url)), [], "default page requested close-review");
    assert.deepEqual(pageErrors, [], "browser error");
    assert.deepEqual(apiFailures.filter(failure => !allowedNavigationAborts.has(failure)), [], "API error");
  }
  async function verifyReply(reply, module, selected = company, { requireVouchers = true } = {}) {
    const target = new URL(reply.url());
    assert.equal(reply.status(), 200, `${module.key}: ${target.pathname} failed`);
    const payload = await reply.json();
    const auxiliaryContract = {
      "/api/dashboard/context": "dashboard_context",
      "/api/dashboard/close-review": "dashboard_close_review",
    }[target.pathname];
    if (auxiliaryContract) assert.equal(payload.schema_version, responseVersions[auxiliaryContract],
      `${auxiliaryContract}: outdated response contract`);
    assert.equal(payload.read_context?.company_id ?? payload.current_company?.company_id ?? payload.company_id, selected.id);
    if (target.pathname === "/api/dashboard/quarterly-report") {
      const selectedQuarter = selected.id === company.id
        ? quarter : `${selected.period.slice(0, 4)}-Q${Math.ceil(Number(selected.period.slice(5)) / 3)}`;
      assert.equal(target.searchParams.get("year"), selectedQuarter.slice(0, 4));
      assert.equal(target.searchParams.get("quarter"), selectedQuarter.slice(-1));
    } else if (target.pathname !== "/api/dashboard/context") {
      assert.equal(target.searchParams.get("period"), selected.period);
    }
    if (target.pathname === `/api/dashboard/${module.action}`) {
      const requestedLimit = target.searchParams.get("limit");
      assert(requestedLimit === null || requestedLimit === "20", `${module.key}: default page limit changed`);
      verifyMainPayload(payload, module.key, selected, target, { requireVouchers });
      if (!target.searchParams.has("section") && !target.searchParams.has("cursor")) {
        await rendered(module, selected);
        await verifyMainVisible(payload, module);
        if (selected.id === company.id && selected.period === company.period) {
          const snapshot = module.key === "reports" ? payload.read_context.read_version : payload.snapshot_version;
          const established = refreshProjections.get(module.key);
          if (established) assert.equal(snapshot, established.snapshot,
            `${module.key}: hot refresh crossed the validated snapshot`);
          else {
            const dom = await page.evaluate(
              captureRefreshProjection,
              { root: refreshRoots[module.key] });
            assert(dom && JSON.parse(dom).content.length, `${module.key}: empty refresh projection`);
            refreshProjections.set(module.key, { dom, snapshot });
          }
        }
      }
    }
    return payload;
  }
  async function verifyMainVisible(payload, module) {
    const data = module.key === "reports" ? payload : payload.data;
    const amounts = module.key === "brief" ? [
      ["#overview .hero strong", data.position.month_result_fen],
      ["#overview .kpi.funds strong", data.funds_overview.total_fen],
      ["#overview .kpi.receivable strong", data.open_items.receivable_fen],
      ["#overview .kpi.payable strong", data.open_items.payable_fen],
    ] : module.key === "funds" ? [[".funds-total", data.total_fen]]
      : module.key === "employees" ? [[".people-kpi-grid article:nth-child(1) strong", data.employees.ledger_cost_fen],
        [".people-kpi-grid article:nth-child(2) strong", data.employees.direct_net_payments_fen],
        [".people-kpi-grid article:nth-child(3) strong", data.employees.outstanding_net_fen]]
        : module.key === "assets" ? [[".assets-total", data.ledger_net_fen]] : [];
    for (const [selector, amount] of amounts) {
      assert.equal((await page.locator(selector).textContent())?.trim(), formatFen(amount), `${module.key}: full-company amount differs`);
    }
    if (module.key === "brief") {
      await verifyBriefVisible(data);
      assert.equal(await page.locator(".view-switch button[aria-pressed=true]").textContent(), "按业务", "brief: default view changed");
      const selectedLabel = (await page.locator("#activity nav[aria-label='业务分类'] button[aria-pressed=true] strong").textContent())?.trim();
      const selectedGroup = selectedLabel === "全部" ? null
        : data.activity_groups.find(group => group.label === selectedLabel);
      assert(selectedLabel === "全部" || selectedGroup, "brief: selected business group missing");
      const expectedItems = data.collections.activity.items.filter(item => !selectedGroup || item.group === selectedGroup.key);
      assert.equal(await page.locator("#activity .event-row").count(), expectedItems.length, "brief: selected business rows incomplete");
      assert.equal(await page.locator("#owner-tasks > article:not(.owner-review-request)").count(), data.owner_tasks.length, "brief: owner tasks incomplete");
    } else if (module.key === "funds") {
      assert.equal(await page.locator(".account-grid .account-card").count(), data.collections.accounts.items.length, "funds: default accounts incomplete");
      assert.equal(await page.locator(".investment-summary-table tbody tr").count(), data.collections.investment_products.items.length, "funds: default investments incomplete");
      const selected = data.selected_movement_account;
      const current = page.locator(".fund-account-index button[aria-current=true]");
      assert.equal(await current.count(), selected === null ? 0 : 1, "funds: automatic account selection missing");
      assert.equal(await page.locator(".book-activity-row").count(), data.collections.movements.items.length, "funds: selected account rows incomplete");
      if (selected) for (const item of data.collections.movements.items) {
        assert.equal(item.account_id, selected.account_id, "funds: another account leaked into movements");
        assert.equal(item.account_type, selected.type, "funds: another account type leaked into movements");
      }
    } else if (module.key === "employees") {
      assert.equal(await page.locator(".employee-grid").first().locator(".employee-card").count(), data.collections.employees.items.length, "employees: default employee rows incomplete");
      assert.equal(await page.locator("section:has(> #labor-title) .employee-card").count(), data.collections.labor_sources.items.length, "employees: default labor rows incomplete");
    } else if (module.key === "assets") {
      assert.equal(await page.locator(".asset-grid .asset-card").count(), data.collections.assets.items.length, "assets: default asset rows incomplete");
      assert.equal(await page.locator(".project-card").count(), data.collections.projects.items.length, "assets: default project rows incomplete");
    } else if (module.key === "reports") {
      const summary = data.summary;
      assert.equal(await page.locator(".summary-grid article").count(), summary === null ? 0 : 3, "reports: default summary incomplete");
      if (summary) {
        for (const [index, value] of [summary.assets_total_fen, summary.current_net_profit_fen, summary.current_cash_change_fen].entries()) {
          assert.equal((await page.locator(".summary-grid article strong").nth(index).textContent())?.trim(), formatFen(value), "reports: summary amount differs");
        }
      }
    }
  }
  async function verifyBriefSummaries(data) {
    const financial = data.financial_position, workforce = data.workforce_cost;
    assert(financial && workforce, "brief: main summaries missing");
    const overview = page.locator(".financial-section");
    await overview.waitFor({ state: "visible" });
    assert.equal(await overview.locator(".overview-card").count(), 2, "brief: financial cards missing");
    const amount = async (selector, value, label) => {
      const node = page.locator(selector);
      await node.waitFor({ state: "visible" });
      assert.equal((await node.textContent())?.trim(), formatFen(value), `brief: ${label} differs from main response`);
    };
    await amount(".cash-card .flow > div:nth-child(1) strong", data.funds_overview.inflow_fen, "external receipts");
    await amount(".cash-card .flow > div:nth-child(2) strong", data.funds_overview.outflow_fen, "external payments");
    await amount(".cash-card .summary-rows > div:first-child dd", data.funds_overview.total_fen, "closing funds");
    const assetsLabel = financial.assets_fen === null ? "资产 无法完整建立" : `资产 ${formatFen(financial.assets_fen)}`;
    assert.equal((await page.locator(".balance-trigger").textContent())?.trim(), assetsLabel, "brief: assets total differs");
    for (const [id, field] of [["bank-asset-tooltip", "bank_fen"], ["fixed-asset-tooltip", "fixed_asset_net_fen"], ["intangible-asset-tooltip", "intangible_asset_net_fen"], ["other-assets-tooltip", "other_assets_fen"], ["liability-tooltip", "liabilities_fen"]]) {
      await amount(`[aria-describedby="${id}"]`, financial[field], field);
    }
    // Read the explanation DOM by its field group; visual opening is checked
    // separately in layout_only so it cannot influence refresh timestamps.
    const tooltipAmounts = async (id, values) => {
      const text = await page.locator(`#${id}`).textContent();
      for (const value of values) assert(text.includes(formatFen(value)), `brief: ${id} calculation amount missing`);
    };
    if (financial.equation_valid !== null) {
      await tooltipAmounts("balance-tooltip", [financial.assets_fen, financial.liabilities_fen, financial.equity_fen, financial.capital_fen]);
      const profit = financial.cumulative_result_fen;
      await tooltipAmounts("balance-tooltip", [profit === null ? null : (BigInt(profit) < 0n ? -BigInt(profit) : BigInt(profit)).toString()]);
    }
    const bank = financial.bank_calculation;
    if (financial.bank_fen !== null && [bank.opening_fen, bank.inflow_fen, bank.outflow_fen].every(value => value !== null)
      && BigInt(bank.opening_fen) + BigInt(bank.inflow_fen) - BigInt(bank.outflow_fen) === BigInt(financial.bank_fen)) {
      await tooltipAmounts("bank-asset-tooltip", [bank.opening_fen, bank.inflow_fen, bank.outflow_fen, financial.bank_fen]);
    }
    for (const [id, fields] of [["fixed-asset-tooltip", ["fixed_asset_cost_fen", "accumulated_depreciation_fen", "fixed_asset_net_fen"]], ["intangible-asset-tooltip", ["intangible_asset_cost_fen", "accumulated_amortization_fen", "intangible_asset_net_fen"]]]) {
      if (fields.every(field => financial[field] !== null)) await tooltipAmounts(id, fields.map(field => financial[field]));
    }
    const liabilities = financial.liability_calculation;
    if (financial.liabilities_fen !== null && liabilities.current_fen !== null && liabilities.non_current_fen !== null
      && BigInt(liabilities.current_fen) + BigInt(liabilities.non_current_fen) === BigInt(financial.liabilities_fen)) {
      await tooltipAmounts("liability-tooltip", [liabilities.current_fen, liabilities.non_current_fen, financial.liabilities_fen]);
    }
    if (financial.other_assets_fen !== null && data.funds_overview.cash_fen !== null && data.funds_overview.payment_platform_fen !== null) {
      await tooltipAmounts("other-assets-tooltip", [data.funds_overview.cash_fen, data.funds_overview.payment_platform_fen,
        (BigInt(financial.other_assets_fen) - BigInt(data.funds_overview.cash_fen) - BigInt(data.funds_overview.payment_platform_fen)).toString(), financial.other_assets_fen]);
    }
    assert.equal(await page.locator("#workforce").count(), workforce.has_activity ? 1 : 0, "brief: workforce visibility differs from activity");
    if (workforce.has_activity) {
      await page.locator("#workforce").waitFor({ state: "visible" });
      assert.equal(await page.locator("#workforce .workforce-card").count(), 2, "brief: workforce cards missing");
      await page.locator("#employee-title").waitFor({ state: "visible" });
      await page.locator("#labor-title").waitFor({ state: "visible" });
      await amount("#workforce .total-help-trigger", workforce.total_fen, "total workforce cost");
      await amount("#workforce .workforce-card:nth-child(1) .subtotal strong", workforce.employee.total_fen, "employee cost");
      await amount("#workforce .workforce-card:nth-child(2) .subtotal strong", workforce.personal_labor.total_fen, "personal labor cost");
    }
  }
  async function verifyBriefVisible(data) {
    await verifyBriefSummaries(data);
    await page.waitForFunction(({ state, required }) => {
      const content = document.querySelector(".brief-content");
      return document.querySelector(".module-header")?.getAttribute("aria-busy") === "false"
        && content?.getAttribute("data-month-state") === state
        && content?.getAttribute("data-owner-review-required") === String(required);
    }, { state: data.month_state, required: data.owner_review_request !== null });
    const prompt = page.locator("#owner-tasks .owner-review-request");
    assert.equal(await prompt.count(), data.owner_review_request === null ? 0 : 1,
      "brief: owner review prompt differs from the authoritative locator");
    if (data.owner_review_request !== null) {
      await prompt.waitFor({ state: "visible" });
      await prompt.getByRole("button", { name: "查看本次核对内容", exact: true }).waitFor({ state: "visible" });
    }
    const status = { open: "未关账", closed: "已关账", covered: "由后续关账覆盖" }[data.month_state];
    assert.equal((await page.locator(".module-header .period-status").textContent())?.trim(), status,
      "brief: month status differs from the authoritative brief");
    assert.equal(await page.locator(".close-review, #monthly-review, #close-review-title").count(), 0,
      "brief: monthly review remains in the default page");
  }
  async function rendered(module, selected = company) {
    await page.getByRole("heading", { name: module.heading }).first().waitFor();
    await page.waitForFunction(() => document.querySelector(".module-header")?.getAttribute("aria-busy") === "false");
    if (selected.name) {
      assert.equal((await page.locator(".company-switcher-name").textContent())?.trim(), selected.name);
    }
    for (const selector of module.visible.split(", ")) await page.locator(selector).waitFor();
    if (module.key === "brief") {
      assert.equal(await page.locator(".close-review, #monthly-review, #close-review-title").count(), 0,
        "brief: monthly review remains in the default page");
    }
    assert.equal(await page.locator("main [role=alert], .state-panel[role=alert], .state-panel.error, .error-state, .review-error, .preparation-state[role=alert], .report-error, .voucher-load-status[role=alert]").count(), 0, `${module.key}: technical error rendered`);
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
  async function measure(module, resourceSamples, requireProjection = Boolean(resourceSamples)) {
    if (requireProjection) assert(refreshProjections.has(module.key),
      `${module.key}: formal refresh requires a validated default DOM projection`);
    const contextReply = response("context");
    const mainReply = response(module.action);
    const refreshButton = page.locator(".module-header button.refresh");
    await refreshButton.evaluate(observeHotRefresh, { action: module.action, key: module.key, companyId: company.id,
      period: company.period, quarter,
      visible: module.visible.split(", "), root: refreshRoots[module.key],
      projection: refreshProjections.get(module.key)?.dom });
    await refreshButton.click();
    await page.waitForFunction(() => window.__stage9RenderedAt !== null);
    const measurement = await page.evaluate(captureRefreshMeasurement, {
      action: module.action, companyId: company.id, period: company.period, quarter,
    });
    if (resourceSamples) resourceSamples.push(measurement.resources);
    for (const pending of [contextReply, mainReply]) {
      await verifyReply(await pending, module);
    }
    await rendered(module);
    assertNoBrowserFailures();
    return measurement.measured;
  }
  async function measureCompanySwitch(target) {
    await page.goto(address(modules[0]));
    await rendered(modules[0]);
    const actions = ["context", "brief"];
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
      const briefReady = () => {
        const content = document.querySelector(".brief-content");
        const state = content?.getAttribute("data-month-state");
        const required = content?.getAttribute("data-owner-review-required");
        const prompt = document.querySelector("#owner-tasks .owner-review-request");
        return ["open", "closed", "covered"].includes(state)
          && ["true", "false"].includes(required)
          && (required === "true" ? state === "open" && prompt?.getClientRects().length
          && prompt.querySelector("button")?.getClientRects().length : !prompt)
          && !document.querySelector(".close-review, #monthly-review, #close-review-title");
      };
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
          const complete = period && ["context", "brief"].every(action =>
            resources.some(entry => {
              const url = new URL(entry.name);
              return url.pathname === `/api/dashboard/${action}`
                && url.searchParams.get("company_id") === selection.companyId
                && (action === "context" || url.searchParams.get("period") === period);
            }));
          const ready = document.querySelector(".module-header")?.getAttribute("aria-busy") === "false"
            && document.querySelector(".company-switcher-name")?.textContent?.trim() === selection.companyName
            && document.querySelector("#overview")?.getClientRects().length
            && document.querySelector("#activity")?.getClientRects().length
            && document.querySelector("#open-items")?.getClientRects().length
            && document.querySelector("#owner-tasks")?.getClientRects().length
            && briefReady();
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
    const briefReply = finalReply("brief");
    assert(briefReply, "switch: final month request did not complete");
    const selected = { ...target, period: expectedPeriod };
    if (expectedPeriod !== target.period) {
      delete selected.state;
      delete selected.preview_digest;
    }
    const brief = await verifyReply(briefReply, modules[0], selected,
      { requireVouchers: expectedPeriod === target.period });
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
      selected_period: expectedPeriod, month_state: brief.data.month_state, elapsed_ms: elapsed };
    } finally {
      page.off("response", capture);
    }
  }
  try {
    const ticket = new URL(config.ticket_url);
    ticket.searchParams.set("company_id", company.id);
    ticket.searchParams.set("period", company.period);
    const firstReplies = [response("context"), response("brief")];
    await page.goto(ticket.href);
    const firstNavigation = await navigationRendered(modules[0]);
    let firstBriefPayload;
    for (const reply of await Promise.all(firstReplies)) {
      const payload = await verifyReply(reply, modules[0]);
      if (new URL(reply.url()).pathname === "/api/dashboard/brief") firstBriefPayload = payload;
    }
    assertNoBrowserFailures();
    if (config.no_jobs_panel) {
      assert.equal(await page.getByRole("button", { name: "文件与处理进度", exact: true }).count(), 0);
      assert.equal(await page.locator(".jobs-panel").count(), 0);
    }
    const firstLoad = firstNavigation.elapsed_ms;
    await page.getByLabel("切换公司", { exact: true }).waitFor();
    const navigationBase = {
      first_load_ms: firstLoad,
      first_render_epoch_ms: firstNavigation.epoch_ms,
    };
    Object.assign(partialNavigation, navigationBase);
    if (config.activity_interactions) {
      const requests = [];
      page.on("request", request => {
        const url = new URL(request.url());
        if (url.pathname.startsWith("/api/dashboard/")) requests.push(url);
      });
      const businessRows = page.locator("#activity .event-row");
      assert(await businessRows.count(), "activity: no business row to inspect");
      const row = businessRows.first();
      const button = row.locator(".event-voucher-button");
      const number = (await button.textContent()).trim().match(/^凭证 ([1-9][0-9]*)$/)?.[1];
      assert(number, "activity: public voucher number missing");
      const voucher = firstBriefPayload.data.collections.vouchers.items.find(item => item.number === number);
      assert(voucher, "activity: displayed voucher is outside the authenticated page");
      const tooltip = page.locator("#activity [role=tooltip]");
      assert.equal(await tooltip.count(), 0, "activity: hidden voucher details eagerly mounted");
      assert.equal(await row.locator(".business-detail-trigger:visible").count(), 0, "activity: duplicate business detail entry");
      await button.hover();
      await tooltip.waitFor({ state: "visible" });
      assert.equal(await tooltip.count(), 1, "activity: multiple voucher popovers mounted");
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      const previewBounds = await tooltip.boundingBox();
      assert(previewBounds && previewBounds.y >= 11 && previewBounds.y + previewBounds.height <= page.viewportSize().height - 11,
        "activity: voucher popover is clipped by the viewport");
      assert.equal((await tooltip.locator(".voucher-preview-heading strong").textContent()).trim(), voucher.list_summary);
      assert.equal((await tooltip.locator(".voucher-preview-amount b").textContent()).trim(), formatFen(voucher.business_amount_fen));
      const lines = tooltip.locator(".voucher-preview-lines > span");
      assert.equal(await lines.count(), voucher.lines.length);
      for (let index = 0; index < voucher.lines.length; index++) {
        const saved = voucher.lines[index];
        assert.equal((await lines.nth(index).locator("span").textContent()).trim(), saved.account);
        assert.equal((await lines.nth(index).locator("strong").textContent()).trim(),
          BigInt(saved.debit_fen) ? `借 ${formatFen(saved.debit_fen)}` : `贷 ${formatFen(saved.credit_fen)}`);
      }
      if (config.screenshot_directory) {
        fs.mkdirSync(config.screenshot_directory, { recursive: true });
        await page.screenshot({ path: path.join(config.screenshot_directory, "activity-voucher-desktop.png") });
      }
      await page.mouse.move(0, 0);
      await page.waitForFunction(() => !document.querySelector("#activity [role=tooltip]"));
      await button.focus();
      await tooltip.waitFor({ state: "visible" });
      assert.deepEqual(requests, [], "activity: hover or focus fetched data");
      await button.click();
      await page.waitForFunction(() => document.querySelector(".view-switch button[aria-pressed=true]")?.textContent.trim() === "按凭证");
      assert.equal(await page.locator("#activity .voucher-card.is-open .voucher-inline-detail").count(), 1);
      assert.equal((await page.locator("#activity .voucher-card.is-open .voucher-reference strong").textContent()).trim(), `凭证 ${number}`);
      assert.deepEqual(requests, [], "activity: loaded voucher click fetched data");
      await page.getByRole("button", { name: "按业务", exact: true }).click();
      const businessReply = page.waitForResponse(reply => new URL(reply.url()).pathname === "/api/dashboard/business-status");
      await businessRows.first().locator(".event-copy").click();
      const reply = await businessReply;
      assert.equal(reply.status(), 200);
      const payload = await reply.json();
      const target = new URL(reply.url());
      assert.equal(target.searchParams.get("company_id"), company.id);
      assert.equal(target.searchParams.get("period"), company.period);
      assert.equal(target.searchParams.get("expected_version"), firstBriefPayload.snapshot_version);
      assert.equal(payload.schema_version, responseVersions.dashboard_business_status);
      const panel = businessRows.first().locator(".business-detail-panel");
      await panel.waitFor({ state: "visible" });
      assert.equal(await businessRows.first().getAttribute("aria-expanded"), "true");
      await panel.click({ position: { x: 12, y: 12 } });
      assert.equal(await businessRows.first().getAttribute("aria-expanded"), "true", "activity: detail click collapsed its row");
      await businessRows.first().locator(".event-copy").click();
      assert.equal(await businessRows.first().getAttribute("aria-expanded"), "false");
      await businessRows.first().focus();
      await businessRows.first().press("Enter");
      await panel.waitFor({ state: "visible" });
      assert.equal(await businessRows.first().getAttribute("aria-expanded"), "true");
      await businessRows.first().press("Space");
      assert.equal(await businessRows.first().getAttribute("aria-expanded"), "false");
      assert(requests.length && requests.every(url => url.pathname === "/api/dashboard/business-status"),
        "activity: row expansion reloaded unrelated page data");
      assert.equal(await page.locator(".module-header").getAttribute("aria-busy"), "false");
      await page.setViewportSize({ width: 375, height: 812 });
      await businessRows.first().locator(".event-voucher-button").click();
      await page.waitForFunction(() => document.querySelector(".view-switch button[aria-pressed=true]")?.textContent.trim() === "按凭证");
      assert.equal(await page.locator("#activity [role=tooltip]:visible").count(), 0);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "activity: mobile horizontal overflow");
      if (config.screenshot_directory) await page.screenshot({ path: path.join(config.screenshot_directory, "activity-voucher-mobile.png") });
      assertNoBrowserFailures();
      return { status: "passed", activity_interactions: { hover: true, focus: true, local_voucher: true,
        row_toggle: true, keyboard: true, mobile: true, requests: requests.length }, navigation: navigationBase };
    }
    if (config.layout_only) {
      const layouts = [];
      // These interaction checks are outside every default/cold/refresh timing.
      async function localChange(module, action, section = null) {
        const requests = [], replies = [];
        const requestListener = request => {
          const url = new URL(request.url());
          if (url.pathname.startsWith("/api/dashboard/")) requests.push(url);
        };
        const replyListener = reply => {
          if (new URL(reply.url()).pathname === `/api/dashboard/${module.action}`) replies.push(reply);
        };
        await page.evaluate(() => {
          const header = document.querySelector(".module-header");
          window.__stage9LocalRoot = header;
          window.__stage9LocalFailures = [];
          window.__stage9LocalObserver = new MutationObserver(() => {
            if (!header.isConnected || header.getAttribute("aria-busy") !== "false") window.__stage9LocalFailures.push("whole-page loading");
          });
          window.__stage9LocalObserver.observe(document.body, { subtree: true, attributes: true, childList: true });
        });
        page.on("request", requestListener); page.on("response", replyListener);
        try {
          const pending = section ? waitForLater(page.waitForResponse(reply => {
            const url = new URL(reply.url());
            return matchesAction(url, module.action) && url.searchParams.get("section") === section;
          })) : null;
          await action();
          if (pending) await pending;
          await page.waitForFunction(() => !Array.from(document.querySelectorAll("[role=status], .empty"))
            .some(node => node.getClientRects().length && /正在读取所选|正在读取资金明细|正在读取银行流水|正在读取凭证/.test(node.textContent ?? "")));
          await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
          for (const url of requests) {
            assert.equal(url.pathname, `/api/dashboard/${module.action}`, `${module.key}: display change read unrelated data`);
            assert(section && url.searchParams.get("section") === section, `${module.key}: display change reloaded whole page`);
            assert.equal(url.searchParams.get("company_id"), company.id, "local filter: company changed");
            assert.equal(url.searchParams.get("period"), company.period, "local filter: month changed");
            assert(url.searchParams.get("expected_version"), "local filter: snapshot boundary missing");
          }
          for (const reply of replies) await verifyReply(reply, module);
          assert.deepEqual(await page.evaluate(() => window.__stage9LocalFailures), [], `${module.key}: local change flashed whole page`);
          assert.equal(await page.locator(".module-header").getAttribute("aria-busy"), "false");
          assert.equal(await page.locator("[role=alert]").count(), 0, `${module.key}: local read failed`);
          assertNoBrowserFailures();
        } finally {
          page.off("request", requestListener); page.off("response", replyListener);
          await page.evaluate(() => window.__stage9LocalObserver.disconnect());
        }
      }
      async function checkOwnerLayout(module, scenario) {
        const layout = await page.evaluate(() => {
          const visible = element => element.getClientRects().length
            && getComputedStyle(element).visibility !== "hidden";
          const textBounds = element => {
            const range = document.createRange();
            range.selectNodeContents(element);
            return Array.from(range.getClientRects());
          };
          const currencyOverflow = Array.from(document.querySelectorAll("strong, dd, td, .component-value-trigger, .balance-trigger, .total-help-trigger"))
            .filter(element => visible(element) && /[¥￥]/.test(element.textContent ?? ""))
            .flatMap(element => textBounds(element)
              .filter(rect => rect.left < -1 || rect.right > innerWidth + 1)
              .map(rect => ({ label: element.textContent, left: rect.left, right: rect.right })));
          const heroOverlap = [];
          const hero = document.querySelector(".funds-hero");
          if (hero) {
            const panes = Array.from(hero.children).filter(element => element.tagName === "DIV");
            for (const [index, pane] of panes.entries()) {
              const rects = Array.from(pane.querySelectorAll("strong"))
                .filter(visible).flatMap(textBounds);
              for (const other of panes.slice(index + 1)) {
                const box = other.getBoundingClientRect();
                for (const rect of rects) {
                  if (rect.right > box.left + 1 && rect.left < box.right - 1
                    && rect.bottom > box.top + 1 && rect.top < box.bottom - 1) {
                    heroOverlap.push({ text: pane.textContent, other: other.textContent });
                  }
                }
              }
            }
          }
          const workbench = document.querySelector(".fund-business-workbench");
          const lastAccount = document.querySelector(".fund-account-index button:last-of-type");
          const firstMovement = document.querySelector(".book-activity-row");
          const detailEnd = document.querySelector(".fund-business-detail > :last-child");
          const mobileFundsGaps = innerWidth <= 760 && workbench && lastAccount && firstMovement
            ? {
              before_rows: firstMovement.getBoundingClientRect().top - lastAccount.getBoundingClientRect().bottom,
              after_rows: detailEnd
                ? workbench.getBoundingClientRect().bottom - detailEnd.getBoundingClientRect().bottom : 0,
            } : null;
          return {
            viewport: innerWidth, width: document.documentElement.scrollWidth,
            technical: Array.from(document.querySelectorAll("summary, h2, h3, dt"))
              .filter(visible).map(element => element.textContent || "")
              .filter(text => /技术详情|原始 JSON|来源证明|核算依据|试算平衡/.test(text)),
            currency_overflow: currencyOverflow, hero_overlap: heroOverlap,
            mobile_funds_gaps: mobileFundsGaps,
          };
        });
        layouts.push({ page: module.key, scenario, ...layout });
        assert(layout.width <= layout.viewport + 1,
          `${module.key}/${scenario}: horizontal overflow at ${layout.viewport}px (${layout.width}px)`);
        assert.deepEqual(layout.technical, [], `${module.key}/${scenario}: technical content remains`);
        assert.deepEqual(layout.currency_overflow, [], `${module.key}/${scenario}: amount overflows the screen`);
        assert.deepEqual(layout.hero_overlap, [], `${module.key}/${scenario}: hero amount overlaps another section`);
        if (layout.mobile_funds_gaps) {
          assert(layout.mobile_funds_gaps.before_rows <= 100,
            `${module.key}/${scenario}: excessive blank space before the movements`);
          assert(layout.mobile_funds_gaps.after_rows <= 60,
            `${module.key}/${scenario}: excessive blank space after the movements`);
        }
      }
      for (const width of [320, 375, 768, 1440]) {
        await page.setViewportSize({ width, height: 900 });
        for (const module of modules) {
          const expected = [response("context"), response(module.action)];
          await page.goto(address(module));
          let mainPayload;
          for (const reply of await Promise.all(expected)) {
            const payload = await verifyReply(reply, module);
            if (new URL(reply.url()).pathname === `/api/dashboard/${module.action}`) mainPayload = payload;
          }
          await rendered(module);
          await checkOwnerLayout(module, "default");
          if (width === 375 && config.screenshot_directory) {
            await page.screenshot({ path: `${config.screenshot_directory}/${module.key}.png`, fullPage: true });
          }
          if (module.key === "reports") {
            for (let index = 0; index < 3; index++) {
              await localChange(module, () => page.locator(`#report-statement-button-${index}`).click());
              await page.locator("#report-full tbody tr").first().waitFor();
              await checkOwnerLayout(module, `statement_${index + 1}`);
              await localChange(module, () => page.locator(".template-switch input").check());
              await page.locator(".tax-template-sheet").waitFor();
              await checkOwnerLayout(module, `tax_template_${index + 1}`);
              await localChange(module, () => page.locator(".template-switch input").uncheck());
              if (width === 375 && config.screenshot_directory) {
                await page.screenshot({ path: `${config.screenshot_directory}/report-statement-${index + 1}.png`, fullPage: true });
              }
            }
          }
          if (module.key === "employees") {
            const employee = page.locator("details.employee-card").first();
            if (await employee.count() && !(await employee.evaluate(node => node.open))) {
              await localChange(module, () => employee.locator(":scope > summary").click());
              await employee.locator(".employee-detail").waitFor();
              const item = mainPayload.data.collections.employees.items.find(item => item.selection_status !== "unestablished");
              const detailText = await employee.locator(".employee-detail").textContent();
              for (const field of ["gross_salary_fen", "employee_social_insurance_fen", "employee_housing_fund_fen", "individual_income_tax_fen", "other_net_settlements_fen"]) {
                assert(detailText.includes(formatFen(item[field])), `employees: current payroll ${field} missing`);
              }
              await checkOwnerLayout(module, "current_payroll");
            }
            await localChange(module, () => page.getByLabel("筛选员工", { exact: true }).selectOption("payroll"), "employees");
            await localChange(module, () => page.getByLabel("筛选员工", { exact: true }).selectOption("all"), "employees");
          }
          if (module.key === "assets") {
            await localChange(module, () => page.getByLabel("筛选资产", { exact: true }).selectOption("fixed"), "assets");
            await localChange(module, () => page.getByLabel("筛选资产", { exact: true }).selectOption("all"), "assets");
          }
          if (module.key === "funds") {
            await localChange(module, () => page.locator("#fund-detail-tab-bank").click());
            await localChange(module, () => page.locator("#fund-detail-tab-book").click());
            const accounts = page.locator(".fund-account-index > button");
            if (await accounts.count() > 1) {
              await localChange(module, () => accounts.nth(1).click(), "movements");
              await localChange(module, () => accounts.first().click(), "movements");
            }
          }
          if (module.key === "brief") {
            if (width === 375) {
              for (const selector of [".financial-section .overview-card", "#workforce .workforce-card"]) {
                const bounds = await page.locator(selector).evaluateAll(nodes => nodes.map(node => {
                  const rect = node.getBoundingClientRect();
                  return { left: rect.left, right: rect.right, viewport: innerWidth };
                }));
                for (const rect of bounds) assert(rect.left >= -1 && rect.right <= rect.viewport + 1, `brief: ${selector} outside mobile viewport`);
              }
              const triggers = page.locator(".financial-section [aria-describedby], #workforce .total-help-trigger");
              for (let index = 0; index < await triggers.count(); index++) {
                const trigger = triggers.nth(index), id = await trigger.getAttribute("aria-describedby");
                await localChange(module, async () => {
                  await trigger.focus();
                  await page.locator(`#${id}`).waitFor({ state: "visible" });
                  await page.waitForFunction(id => {
                    const style = getComputedStyle(document.getElementById(id));
                    return style.visibility === "visible" && Number(style.opacity) === 1;
                  }, id);
                });
                await checkOwnerLayout(module, `summary_tooltip_${id}`);
                const bounds = await page.locator(`#${id}`).boundingBox();
                assert(bounds && bounds.x >= -1 && bounds.x + bounds.width <= width + 1, `brief: ${id} outside mobile viewport`);
                await trigger.evaluate(node => node.blur());
              }
            }
            const categories = page.locator("#activity .index button");
            if (await categories.count() > 1) {
              await localChange(module, () => categories.nth(1).click());
              await localChange(module, () => categories.first().click());
            }
            await localChange(module, () => page.getByRole("button", { name: "按凭证", exact: true }).click());
            const vouchers = mainPayload.data.collections.vouchers.items;
            assert.equal(await page.locator(".voucher-card").count(), vouchers.length, "brief: first 20 vouchers incomplete");
            if (vouchers.length) {
              await localChange(module, () => page.locator(".voucher-row").first().click());
              const detail = page.locator(".voucher-inline-detail");
              await detail.waitFor();
              assert.equal(await detail.locator("tbody tr").count(), vouchers[0].lines.length, "voucher: visible entry lines missing");
              const totals = await detail.locator("tfoot").textContent();
              assert(totals.includes(formatFen(vouchers[0].amount_fen)), "voucher: visible debit/credit totals missing");
              await checkOwnerLayout(module, "voucher_detail");
            }
            if (width === 375 && mainPayload.data.collections.vouchers.page.has_more) {
              await localChange(module, async () => {
                await page.getByRole("button", { name: "下一页", exact: true }).click();
                await page.waitForFunction(() => document.querySelector(".voucher-pagination strong")?.textContent?.trim().startsWith("2 /"));
              }, "vouchers");
              assert.equal(await page.locator(".voucher-card").count(), Math.min(20, mainPayload.data.voucher_count - 20), "voucher: second page incomplete");
              await localChange(module, async () => {
                await page.getByRole("button", { name: "改为全部显示凭证", exact: true }).click();
                await page.waitForFunction(total => document.querySelectorAll(".voucher-card").length === total
                  && document.querySelector(".voucher-view")?.getAttribute("aria-busy") === "false", mainPayload.data.voucher_count);
              }, mainPayload.data.voucher_count > 40 ? "vouchers" : null);
              const numbers = await page.locator(".voucher-reference strong").allTextContents();
              assert.equal(new Set(numbers).size, mainPayload.data.voucher_count, "voucher: all mode duplicated or lost vouchers");
              await localChange(module, () => page.getByRole("button", { name: "改为分页显示凭证", exact: true }).click());
            }
            await localChange(module, () => page.getByRole("button", { name: "按业务", exact: true }).click());
          }
          const detail = page.locator(".business-status-details").first();
          if (await detail.count()) {
            const ancestors = detail.locator("xpath=ancestor::details[not(@open)]");
            const count = await ancestors.count();
            for (let index = 0; index < count; index++) {
              // Requery after each real click: opened ancestors leave this set.
              await detail.locator("xpath=ancestor::details[not(@open)]").first()
                .locator(":scope > summary").click();
            }
            const reply = response("business-status");
            if (module.key === "brief") await detail.locator("xpath=ancestor::li[contains(@class,'event-row')]").locator(".event-copy").click();
            else await detail.locator("summary").click();
            await verifyReply(await reply, module);
            await detail.locator(".business-detail-panel").waitFor();
            assert.equal(await detail.locator("[role=alert]").count(), 0, `${module.key}: business detail failed`);
            await checkOwnerLayout(module, "business_detail");
            if (width === 375 && config.screenshot_directory) {
              await page.screenshot({ path: `${config.screenshot_directory}/${module.key}-business-detail.png`, fullPage: true });
            }
          }
          if (module.key === "brief" && width === 375 && mainPayload.data.owner_review_request) {
            const digest = mainPayload.data.owner_review_request.preview_digest;
            const pending = response("close-review");
            const markAllowed = request => {
              const url = new URL(request.url());
              if (matchesAction(url, "close-review") && url.searchParams.get("preview_digest") === digest) allowedCloseReviewRequests.add(request.url());
            };
            page.on("request", markAllowed);
            try {
              await page.getByRole("button", { name: "查看本次核对内容", exact: true }).click();
              const reply = await pending, review = await verifyReply(reply, module);
              assert.equal(new URL(reply.url()).searchParams.get("preview_digest"), digest, "review: requested another preview");
              assert.equal(review.preview_digest, digest, "review: displayed another preview");
              await page.locator(".close-review").waitFor();
              await checkOwnerLayout(module, "on_demand_review");
              assertNoBrowserFailures();
              await page.getByRole("button", { name: "收起本次核对", exact: true }).click();
              await verifyBriefVisible(mainPayload.data);
            } finally { page.off("request", markAllowed); }
          }
        }
      }
      assertNoBrowserFailures();
      return { status: "passed", company_id: company.id, period: company.period,
        pages: {}, layouts, navigation: navigationBase };
    }
    if (config.foreground_stream) {
      assert(typeof config.stop_file === "string" && config.stop_file.length > 0);
      process.stdout.write(`${JSON.stringify({ kind: "ready", company_id: company.id,
        period: company.period, first_render_ms: firstLoad })}\n`);
      while (!fs.existsSync(config.stop_file)) {
        const elapsed = await measure(modules[0], undefined, true);
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
          selected_period: lastSwitch.selected_period, month_state: lastSwitch.month_state,
          samples: timings, median_ms: percentile(sorted, 0.5),
          p95_ms: percentile(sorted, 0.95), max_ms: sorted.at(-1),
        } },
      };
    }
    const coldOpen = {};
    for (const module of modules) {
      const expected = [response("context"), response(module.action)];
      result[module.key] = { samples: [] };
      await page.goto(address(module));
      coldOpen[module.key] = (await navigationRendered(module)).elapsed_ms;
      partialNavigation.cold_open_ms = coldOpen;
      for (const reply of await Promise.all(expected)) await verifyReply(reply, module);
      if (config.refresh_pages && !config.refresh_pages.includes(module.key)) {
        delete result[module.key];
        continue;
      }
      for (let i = 0; i < warmups; i++) await measure(module);
      const timings = [];
      const resourceSamples = [];
      result[module.key] = { samples: timings, resource_samples: resourceSamples };
      for (let i = 0; i < samples; i++) timings.push(await measure(module, resourceSamples));
      const sorted = [...timings].sort((a, b) => a - b);
      result[module.key] = {
        samples: timings,
        resource_samples: resourceSamples,
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
      status: Object.keys(result).length > 0 && Object.values(result).every(page => page.samples.length === samples && page.over_500_ms === 0) ? "passed" : "over_target",
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

module.exports = { waitForLater, verifyBriefState, verifyCollection, verifyVoucher, verifyMainPayload, formatFen, captureRefreshResources, readRefreshProjection, observeHotRefresh };
