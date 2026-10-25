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
  "dashboard_assets", "dashboard_brief_group", "dashboard_business_status", "dashboard_quarterly_report",
  "dashboard_period_preparation", "dashboard_close_review", "report_export_status",
].map(name => {
  const schema = responseSchemas?.[name];
  const branches = schema?.oneOf?.map(branch => schema.$defs?.[branch.$ref?.split("/").at(-1)]) ?? [schema];
  const version = branches[0]?.properties?.schema_version;
  const expected = version?.const ?? (Array.isArray(version?.enum) && version.enum.length === 1
    ? version.enum[0] : undefined);
  assert(branches.length > 0 && branches.every(branch => branch?.type === "object"
    && Array.isArray(branch.required) && branch.required.includes("schema_version")
    && JSON.stringify(branch.properties?.schema_version) === JSON.stringify(version))
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
  { key: "employees", path: "/employees", action: "employees", heading: "员工与薪酬概览", visible: "#employees-overview, #employee-list-title, #employees-cost-value, #employees-count-value, #employees-gross-value, #employees-contributions-value, #employees-paid-value, #employees-outstanding-value" },
  { key: "assets", path: "/assets", action: "assets", heading: "长期资产概览", visible: "#assets-overview, #asset-list-title" },
  { key: "reports", path: "/reports", action: "quarterly-report", heading: "季度财务报表", visible: "#report-overview, .summary-grid, #report-statements" },
];
const defaultCollections = {
  brief: ["activity", "open_items"],
  funds: ["accounts", "movements", "statements", "investment_products", "investment_events"],
  employees: ["employees", "labor_workers"],
  assets: ["assets", "projects"],
};
// Only the default business result, not global jobs or on-demand details.
const refreshRoots = {
  brief: ".brief-content", funds: ".funds-result", employees: ".employee-result",
  assets: ".asset-result", reports: ".report-dashboard",
};

// A retry is the same typed page read carried in a real POST body. Normalize
// only its scope for existing contract checks; retain the actual path/method
// in diagnostics and never manufacture an extra GET to satisfy an observer.
function dashboardReadTarget(value, method = "GET", body = undefined) {
  const target = new URL(value);
  if (!/^\/api\/dashboard\/(brief|funds|employees|assets|quarterly-report)\/retry$/.test(target.pathname)) return target;
  if (method !== "POST" || target.search) throw new Error("retry observation requires its actual POST body");
  const query = typeof body === "string" ? JSON.parse(body) : body;
  if (!query || typeof query !== "object" || Array.isArray(query)
    || !Object.hasOwn(query, "company_id") || ["section", "cursor", "expected_version"].some(key => Object.hasOwn(query, key)))
    throw new Error("retry observation has no unambiguous main page scope");
  for (const [key, value] of Object.entries(query)) {
    if (!["string", "number", "boolean"].includes(typeof value)) throw new Error("retry scope must contain scalar values");
    target.searchParams.set(key, String(value));
  }
  target.pathname = target.pathname.slice(0, -6);
  return target;
}
function dashboardRequestTarget(request) {
  const method = typeof request.method === "function" ? request.method() : "GET";
  return dashboardReadTarget(request.url(), method, method === "POST" ? request.postDataJSON() : undefined);
}
function dashboardResponseTarget(reply) {
  return typeof reply.request === "function" ? dashboardRequestTarget(reply.request()) : new URL(reply.url());
}
function observedDashboardResource(entry, reads = []) {
  const matched = reads.filter(read => read.wire_url === entry.name && read.start <= entry.startTime).at(-1);
  if (matched) return { target: new URL(matched.url), state: matched.state, status: matched.status };
  const target = new URL(entry.name);
  return target.pathname.endsWith("/retry") ? null : { target, state: "native", status: null };
}
function createStaticResourceObserver(origin) {
  const failures = [], pending = new Set(), navigationAborts = new WeakSet();
  const required = request => new URL(request.url()).origin === origin
    && ["script", "stylesheet"].includes(request.resourceType());
  const record = (request, detail) => ({ path: new URL(request.url()).pathname,
    url: diagnosticUrl(request.url()), required: required(request), ...detail });
  return {
    failures,
    start(request) { if (required(request)) pending.add(request); },
    finished(request) { pending.delete(request); },
    // Only unfinished old-document resources can be superseded by our navigation.
    navigating() { for (const request of pending) navigationAborts.add(request); },
    response(response) {
      const request = response.request(), target = new URL(response.url());
      if (target.pathname.startsWith("/api/") && !required(request)) return;
      if (response.status() >= 400 || request.resourceType() === "stylesheet"
        && response.headers()["content-type"]?.split(";", 1)[0] !== "text/css")
        failures.push(record(request, { status: response.status(), content_type: response.headers()["content-type"] ?? null }));
    },
    failed(request) {
      if (new URL(request.url()).pathname.startsWith("/api/") && !required(request)) return;
      const error = request.failure()?.errorText ?? "network_failed";
      failures.push(record(request, { error: sanitize(error),
        navigation_cancelled: error === "net::ERR_ABORTED" && navigationAborts.has(request) }));
      pending.delete(request);
    },
    assertComplete() {
      assert.deepEqual(failures.filter(item => item.required && !item.navigation_cancelled), [],
        "required same-origin script or stylesheet failed");
    },
  };
}
const installDashboardReadObserver = new Function(`
  const target = ${dashboardReadTarget.toString()};
  const original = window.fetch;
  window.__stage9DashboardReads = [];
  window.fetch = function(input, options = {}) {
    const absolute = new URL(typeof input === 'string' ? input : input.url, location.href).href;
    let read;
    if (/^\\/api\\/dashboard\\/(brief|funds|employees|assets|quarterly-report)(\\/retry)?$/.test(new URL(absolute).pathname)) {
      try {
        const normalized = target(absolute, options.method ?? 'GET', options.body);
        read = { wire_url: absolute, url: normalized.href, start: performance.now(), state: 'pending', status: null };
        window.__stage9DashboardReads.push(read);
      } catch { /* Let the real fetch and generated contract report an invalid request. */ }
    }
    return original.call(this, input, options).then(response => {
      if (read) {
        read.status = response.status;
        const json = response.json;
        response.json = function(...args) {
          return json.apply(this, args).then(value => { read.state = value?.state ?? 'native'; return value; });
        };
      }
      return response;
    });
  };
`);
function readRefreshProjection(document, rootSelector) {
  const root = document.querySelector(rootSelector);
  if (!root) return null;
  // Each visible child contributes its complete innerText: financial cards and
  // the long-term asset card and conditional workforce section are included in the existing two-frame
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
    period: document.querySelector(".module-header .select-trigger")?.getAttribute("data-value"),
    status: document.querySelector(".module-header .period-status")?.innerText,
    content: visibleText,
    controls: Array.from(root.querySelectorAll(".select-trigger, [aria-current], [aria-pressed], [aria-selected], details"))
      .map(node => [node.tagName, node.getAttribute("data-value") ?? node.value ?? null, node.open ?? null,
        node.getAttribute("aria-current"), node.getAttribute("aria-pressed"), node.getAttribute("aria-selected")]),
  });
}

const observeHotRefresh = new Function("button", "selection", `
  const readProjection = ${readRefreshProjection.toString()};
  const observedResource = ${observedDashboardResource.toString()};

  const briefReady = () => {
    const content = document.querySelector(".brief-content");
    const state = content?.getAttribute("data-month-state");
    const required = content?.getAttribute("data-owner-review-required");
    const prompt = document.querySelector("#owner-tasks .owner-review-request");
    const asset = document.querySelector("#overview .kpi.asset");
    return ["open", "closed", "covered"].includes(state)
      && ["true", "false"].includes(required)
      && (required === "true" ? state === "open" && prompt?.getClientRects().length
      && prompt.querySelector("button")?.getClientRects().length : !prompt)
      && !document.querySelector(".close-review, #monthly-review, #close-review-title")
      && asset?.getClientRects().length && asset.querySelector("strong")?.innerText.trim()
      && /^固定 [0-9]+ 项 · 无形 [0-9]+ 项$/.test(asset.querySelector("small")?.innerText.trim());
  };
  button.addEventListener("click", () => {
    const started = performance.now();
    // ResourceTiming otherwise stops recording after the browser's default
    // 150 entries, and previous iterations must never satisfy this click.
    performance.setResourceTimingBufferSize(1000);
    performance.clearResourceTimings();
    window.__stage9DashboardReads = [];
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
        const actual = observedResource(entry, window.__stage9DashboardReads ?? []);
        if (!actual || !['ready', 'native'].includes(actual.state) || actual.status !== null && actual.status !== 200) return false;
        const url = actual.target;
        if (url.pathname !== \`/api/dashboard/\${action}\`
          || url.searchParams.get("company_id") !== selection.companyId
          || entry.responseEnd <= started) return false;
        if (action === "context") return true;
        if (selection.requireRoot && (url.searchParams.has("section") || url.searchParams.has("cursor"))) return false;
        if (action === "quarterly-report") {
          return url.searchParams.get("year") === selection.quarter.slice(0, 4)
            && url.searchParams.get("quarter") === selection.quarter.slice(-1);
        }
        return url.searchParams.get("period") === selection.period;
      }));
      const mainReady = document.querySelector(".module-header")?.getAttribute("aria-busy") === "false"
        && selection.visible.every(selector => document.querySelector(selector)?.getClientRects().length)
        && (selection.key !== "employees" || (selection.visible.slice(2)
          .every(selector => document.querySelector(selector)?.innerText.trim())
          && /^[0-9]+ *人$/.test(document.querySelector("#employees-count-value")?.innerText.trim())));
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

function resultHasVisibleContent(document, root) {
  if (!root) return false;
  const style = document.defaultView.getComputedStyle(root);
  if (style.display === "none" || style.visibility === "hidden") return false;
  // Result wrappers intentionally use display: contents and have no box.
  // Require an actually rendered child; the wrapper's existence is insufficient.
  if (style.display === "contents")
    return Array.from(root.children).some(child => resultHasVisibleContent(document, child));
  return Boolean(root.getClientRects().length);
}

function readInteractionPosition(document, position) {
  const target = document.querySelector(position.selector), view = document.defaultView;
  if (!target || !resultHasVisibleContent(document, target)
    || position.require_focus && target !== document.activeElement && !target.contains(document.activeElement)) return null;
  const rect = target.getBoundingClientRect();
  const values = [rect.left, rect.top, rect.right, rect.bottom, view.scrollX, view.scrollY, view.innerWidth, view.innerHeight];
  if (!values.every(Number.isFinite) || rect.right <= rect.left || rect.bottom <= rect.top
    || Math.min(rect.right, view.innerWidth) <= Math.max(rect.left, 0)
    || Math.min(rect.bottom, view.innerHeight) <= Math.max(rect.top, 0)) return null;
  // A tall card on a phone only needs a visible portion, rather than fitting
  // wholly inside the viewport. Its calibrated position must still be reached.
  return values;
}

function interactionPositionReady(position, current) {
  if (!position) return true;
  return Array.isArray(current) && Array.isArray(position.expected)
    && current.length === position.expected.length
    && current.every((value, index) => Number.isFinite(position.expected[index]) && Math.abs(value - position.expected[index]) <= 1);
}

const calibrateInteractionPosition = new Function("position", `
  const resultHasVisibleContent = ${resultHasVisibleContent.toString()};
  const readPosition = ${readInteractionPosition.toString()};
  return new Promise((resolve, reject) => {
    const started = performance.now();
    let previous = null, frames = 0;
    const frame = () => {
      const current = readPosition(document, position);
      const stable = current && JSON.stringify(current) === JSON.stringify(previous);
      frames = stable ? frames + 1 : current ? 1 : 0;
      previous = current;
      if (frames >= 2) resolve(current);
      else if (performance.now() - started < 30000) requestAnimationFrame(frame);
      else reject(new Error('interaction target positioning calibration timeout'));
    };
    requestAnimationFrame(frame);
  });
`);

function verifiedReadCycleReady(cycle, proof) {
  if (typeof cycle.cycle_id !== "string" || !cycle.cycle_id || !proof || proof.cycle_id !== cycle.cycle_id || proof.status !== "verified"
    || proof.pending !== 0 || proof.failed !== 0) return false;
  if (!["action", "company_id", "period", "snapshot"].every(key => typeof cycle.scope?.[key] === "string"
    && cycle.scope[key].length > 0 && proof.scope?.[key] === cycle.scope[key])) return false;
  if (!Array.isArray(proof.required_reads) || proof.required_reads.some(read => read.status !== "verified")) return false;
  return proof.required_reads.length > 0 || !cycle.require_new_response && proof.cache_verified === true;
}

function verifyEmployeeRootRows(visibleRows, rootRows, allowContinuation = false) {
  if (allowContinuation) assert(visibleRows >= rootRows, "employees: expanded refresh lost root employee rows");
  else assert.equal(visibleRows, rootRows, "employees: default employee rows incomplete");
}

function interactionReadUrl(value) {
  const url = new URL(value); url.hash = ""; url.searchParams.sort();
  return url.href;
}

const retainedRefreshKeys = Object.freeze([
  "brief_activity_retained_detail_refresh", "brief_open_items_retained_detail_refresh",
  "funds_book_retained_detail_refresh", "funds_bank_retained_refresh",
  "assets_fixed_list_retained_detail_refresh", "reports_statement_retained_detail_refresh",
  "employees_automatic_continuation_refresh", "employees_expanded_detail_refresh",
]);
function retainedRefreshSelection(config, environment = process.env) {
  const enabled = environment.STAGE9_RETAINED_REFRESH_ONLY === "1";
  assert(!enabled || config.interaction_only === true && !config.layout_only,
    "retained refresh requires interaction_only, without layout_only");
  return enabled;
}
function createRetainedResponseBodies() {
  const owners = new WeakMap(), snapshots = new WeakMap(), outstanding = new Map();
  return {
    start(request, generation, pageUrl) { owners.set(request, { generation, page_url: pageUrl, request_url: request.url() }); },
    capture(reply) {
      if (snapshots.has(reply)) return;
      const owner = owners.get(reply.request());
      // Capture bytes immediately, but parse/validate only in the original
      // validation phase. Rejections become owned data, never unhandled promises.
      let reading;
      try { reading = reply.body(); } catch (error) { reading = Promise.reject(error); }
      const promise = Promise.resolve(reading).then(body => ({ body }), error => ({ error }));
      const record = { owner, promise, url: reply.url() };
      snapshots.set(reply, record); outstanding.set(reply, record);
    },
    async read(reply, generation) {
      this.capture(reply);
      const record = snapshots.get(reply);
      assert(record.owner && record.owner.generation === generation && record.owner.request_url === reply.url(),
        "retained response: body belongs to another navigation generation or URL");
      const result = await record.promise;
      outstanding.delete(reply);
      if (result.error) throw result.error;
      return JSON.parse(result.body.toString("utf8"));
    },
    consumed(reply) { outstanding.delete(reply); },
    retire(generation) {
      assert(![...outstanding.values()].some(record => record.owner?.generation === generation),
        "retained response: navigation attempted before prior response validation/obsolete classification");
    },
  };
}
function verifyRetainedVisibleOrder(expectedIds, actualCount, label) {
  assert.equal(actualCount, expectedIds.length, `${label}: DOM differs from the complete verified current ordered collection`);
  assert.equal(new Set(expectedIds).size, expectedIds.length, `${label}: duplicate current ordered identity`);
}
function stage9RequestPageLimit(target) {
  const value = target.searchParams.get("limit"), cursor = target.searchParams.get("cursor");
  if (!cursor) { assert(value === null || value === "20", "first page must retain default 20"); return 20; }
  assert(target.searchParams.get("section") && target.searchParams.get("expected_version"), "continuation requires exact section and snapshot");
  assert(value === null || /^[1-9][0-9]*$/.test(value) && Number(value) <= 500, "continuation limit must be bounded to 500");
  return value === null ? 20 : Number(value);
}
function verifyRetainedResponseContract(validators, target, payload) {
  const suffix = { context: "Context", brief: "Brief", funds: "Funds", employees: "Employees", assets: "Assets",
    "quarterly-report": "QuarterlyReport", "brief-group": "BriefGroup", "business-status": "BusinessStatus" }[target.pathname.split("/").at(-1)];
  const validator = validators[`validateDashboard${suffix}Response`];
  assert(validator && validator(payload), `generated response contract rejected ${target.pathname}`);
}
function retainedItemIdentity(pathname, section, item, payload) {
  let identity;
  if (pathname === "/api/dashboard/brief-group" && section === "members") identity = payload.data.section === "activity" ? item.key : item.id;
  else if (pathname === "/api/dashboard/brief" && ["activity", "open_items"].includes(section)) identity = item.group_key;
  else if (section === "vouchers") identity = item.voucher_version_id;
  else identity = { employees: item.employee_id, assets: item.asset_id, projects: item.source_id,
    labor_workers: item.person_id, labor_sources: item.source_id, accounts: item.account_id && `${item.type}:${item.account_id}`,
    movements: item.id, statements: item.id, investment_products: item.fund_id, investment_events: item.id,
    settlement_events: item.id }[section];
  assert(typeof identity === "string" && identity.length, `${pathname}:${section}: missing actual collection identity`);
  return identity;
}
function retainedResource(url, payload, section) {
  const target = new URL(url), pathname = target.pathname;
  const scope = pathname === "/api/dashboard/brief-group" ? `${payload.data.section}:${target.searchParams.get("group_key")}:`
    : pathname === "/api/dashboard/business-status" ? `${target.searchParams.get("subject_id")}:`
    : pathname === "/api/dashboard/employees" && section === "labor_sources" ? `${target.searchParams.get("employee_id")}:` : "";
  return `${pathname}:${scope}${section}`;
}
function retainedCollectionIdentities(reads) {
  const groups = new Map();
  for (const { url, payload } of reads) {
    for (const [section, collection] of Object.entries(payload.data?.collections ?? {})) {
      const pathname = new URL(url).pathname, resource = retainedResource(url, payload, section);
      const identities = new URL(url).searchParams.has("cursor") ? groups.get(resource) ?? [] : [];
      for (const item of collection.items) {
        const identity = retainedItemIdentity(pathname, section, item, payload);
        if (section === "vouchers" && identities.includes(identity)) continue; // Shared vouchers may accompany multiple member pages.
        assert(!identities.includes(identity), `${resource}: duplicate identity across continuation pages`);
        identities.push(identity);
      }
      groups.set(resource, identities);
    }
  }
  return Object.fromEntries(groups);
}
function verifyRetainedAuxiliaryComplete(reads) {
  const chains = new Map();
  for (const { url, payload } of reads) {
    const target = new URL(url);
    if (!["/api/dashboard/brief-group", "/api/dashboard/business-status"].includes(target.pathname)) continue;
    const requiredSection = target.pathname === "/api/dashboard/brief-group" ? "members" : "settlement_events";
    assert(payload.data.collections?.[requiredSection], `${target.pathname}: missing required detail collection`);
    for (const [section, collection] of Object.entries(payload.data.collections)) {
      if (section !== requiredSection) continue;
      const resource = retainedResource(url, payload, section), ids = collection.items.map(item => retainedItemIdentity(target.pathname, section, item, payload));
      const old = target.searchParams.has("cursor") ? chains.get(resource) : null;
      assert(!target.searchParams.has("cursor") || old, `${resource}: continuation has no verified first page`);
      if (old) assert.equal(target.searchParams.get("cursor"), old.next_cursor, `${resource}: continuation cursor is not the prior verified cursor`);
      const chain = old ?? { ids: [], filtered_count: collection.page.filtered_count, version: payload.snapshot_version };
      assert.equal(payload.snapshot_version, chain.version, `${resource}: detail version changed across pages`);
      assert.equal(collection.page.filtered_count, chain.filtered_count, `${resource}: continuation total changed`);
      for (const id of ids) { assert(!chain.ids.includes(id), `${resource}: duplicate continuation identity`); chain.ids.push(id); }
      chain.has_more = collection.page.has_more; chain.next_cursor = collection.page.next_cursor;
      chains.set(resource, chain);
    }
  }
  for (const [resource, chain] of chains) {
    assert.equal(chain.ids.length, chain.filtered_count, `${resource}: incomplete unique detail count`);
    assert.equal(chain.has_more, false, `${resource}: detail continuation has not reached its terminal page`);
    assert.equal(chain.next_cursor, null, `${resource}: terminal detail retains a cursor`);
  }
  return Object.fromEntries(chains);
}
function retainedOrderedItems(reads, pathname, section) {
  let items = [];
  for (const read of reads) if (new URL(read.url).pathname === pathname && read.payload.data?.collections?.[section])
    items = new URL(read.url).searchParams.has("cursor") ? [...items, ...read.payload.data.collections[section].items] : [...read.payload.data.collections[section].items];
  return items;
}
function verifyRetainedTargetBinding(before, after, close = false) {
  assert(before?.ordered_ids?.length && before.open_indices.length, "retained target: no real pre-refresh expanded identity");
  assert.deepEqual(after, close ? { ...before, open_indices: [] } : before, "retained target: expanded identity/order/subject/selected resource changed");
}
function verifiedFundsDetailCache(priorReads, currentMain, binding) {
  assert(currentMain && binding?.subject_id && binding?.account && binding.open_indices.length === 1, "funds cache: missing current target proof");
  assert.equal(binding.ordered_ids[binding.open_indices[0]], binding.target_id, "funds cache: current expanded row identity is not bound");
  assert.equal(currentMain.contract_verified, true, "funds cache: current main was not contract verified");
  const target = new URL(currentMain.url), payload = currentMain.payload;
  assert.equal(target.pathname, "/api/dashboard/funds");
  assert.deepEqual(payload.data.selected_movement_account, binding.account, "funds cache: selected account changed");
  assert.equal(payload.snapshot_version, binding.snapshot, "funds cache: current main snapshot changed");
  const reads = priorReads.filter(read => new URL(read.url).pathname === "/api/dashboard/business-status");
  assert(reads.length, "funds cache: no previously verified detail");
  for (const read of reads) {
    assert.equal(read.contract_verified, true, "funds cache: prior detail was not contract verified");
    const url = new URL(read.url), detail = read.payload;
    assert.equal(url.searchParams.get("company_id"), target.searchParams.get("company_id"));
    assert.equal(url.searchParams.get("period"), target.searchParams.get("period"));
    assert.equal(detail.read_context.company_id, target.searchParams.get("company_id"));
    assert.equal(detail.selected_period.key, target.searchParams.get("period"));
    assert(typeof detail.snapshot_version === "string" && detail.snapshot_version, "funds cache: detail has no own version");
    if (url.searchParams.has("expected_version")) assert.equal(url.searchParams.get("expected_version"), detail.snapshot_version);
    else assert(!url.searchParams.has("cursor"), "funds cache: continuation missing its own version");
    assert.equal(url.searchParams.get("subject_id"), binding.subject_id);
    assert.equal(detail.data.identity.subject_id, binding.subject_id);
    assert.equal(url.searchParams.get("settlement_view"), "historical");
    assert.equal(detail.data.settlement_view, "historical");
    assert.equal(detail.data.detail_scope, null);
    assert(!url.searchParams.has("detail_scope_key") && !url.searchParams.has("voucher_version_id"), "funds cache: unrelated detail sub-scope");
  }
  verifyRetainedAuxiliaryComplete(reads);
  return reads;
}
function verifyRetainedCollectionIdentities(before, after, { closeActivityMembers = false } = {}) {
  for (const [resource, identities] of Object.entries(before)) {
    // Only the explicitly closing activity-member subtree has no refresh read.
    // Do not exempt main brief content, open-items members, or another resource.
    if (closeActivityMembers && resource.startsWith("/api/dashboard/brief-group:activity:")) continue;
    assert.deepEqual(after[resource], identities, `${resource}: refresh changed verified visible collection identities`);
  }
}
function interactionScopeAllows(url, scope) {
  const action = url.pathname.replace("/api/dashboard/", "");
  const auxiliary = scope.auxiliary_actions?.includes(action) === true
    || scope.retained_refresh === true && ["brief-group", "business-status"].includes(action)
      && (scope.action === "brief" || scope.action === "funds" && action === "business-status");
  const independentDetail = auxiliary && action === "business-status";
  const detailVersion = url.searchParams.get("expected_version");
  return url.searchParams.get("company_id") === scope.company_id
    && (action === "context" || (action === scope.action || auxiliary)
      && (action === "quarterly-report"
        ? `${url.searchParams.get("year")}-Q${url.searchParams.get("quarter")}` === scope.quarter
        : url.searchParams.get("period") === scope.period))
    && (!auxiliary || independentDetail || detailVersion === scope.snapshot)
    && (independentDetail ? Boolean(url.searchParams.get("subject_id"))
      && (detailVersion === null ? !url.searchParams.has("cursor")
        : (scope.business_status_versions ?? [scope.snapshot]).includes(detailVersion))
      : !url.searchParams.has("expected_version") || detailVersion === scope.snapshot);
}
function verifyRetainedRefreshState(before, after, label) {
  assert(before && before.rows.length && before.projection, `${label}: no real pre-refresh content`);
  const expected = before.behavior === "close_activity_members_retain_category" ? { ...before, expanded: [] } : before;
  if (before.behavior === "close_activity_members_retain_category") assert(before.expanded.length, `${label}: activity was not expanded before refresh`);
  assert.deepEqual(after, expected, `${label}: refresh lost previously visible identities, content, selection, mode or expected expanded target`);
}
function readRetainedRefreshState(document, selection) {
  const root = document.querySelector(selection.root);
  const visible = node => node.getClientRects().length && document.defaultView.getComputedStyle(node).visibility !== "hidden";
  const closing = selection.behavior === "close_activity_members_retain_category";
  const memberText = closing ? Array.from(root.querySelectorAll("#activity .group-members")).map(node => node.innerText) : [];
  const clean = text => {
    for (const detail of memberText) text = text.replace(detail, "");
    return text.replace(/\s+/g, " ").trim();
  };
  const rows = Array.from(root?.querySelectorAll(selection.rows) ?? []).filter(visible).map(node => {
    const summary = node.querySelector(":scope > summary") ?? node;
    // The complete row summary identifies the visible record; actual source IDs
    // are recorded separately from verified response collections, never invented.
    return closing ? clean(summary.innerText) : summary.innerText;
  });
  let projection = selection.projection;
  if (closing) {
    const value = JSON.parse(projection);
    value.content = value.content.map(clean);
    value.controls = Array.from(root.querySelectorAll(".select-trigger, [aria-current], [aria-pressed], [aria-selected], details"))
      .filter(node => !node.closest("#activity .group-members"))
      .map(node => [node.tagName, node.getAttribute("data-value") ?? node.value ?? null, node.open ?? null,
        node.getAttribute("aria-current"), node.getAttribute("aria-pressed"), node.getAttribute("aria-selected")]);
    projection = JSON.stringify(value);
  }
  return { projection, rows, behavior: selection.behavior ?? "retain_same_snapshot_visible_state",
    selected: Array.from(root?.querySelectorAll('.select-trigger, [aria-pressed="true"], [aria-selected="true"], [aria-current="true"]') ?? [])
      .filter(visible).map(node => [node.id, node.getAttribute("data-value"), node.innerText]),
    expanded: Array.from(root?.querySelectorAll('details[open], [aria-expanded="true"]') ?? [])
      .filter(node => visible(node) && (!closing || !node.closest("#activity .group-members")))
      .map(node => [node.id, closing ? clean((node.querySelector(":scope > summary") ?? node).innerText) : (node.querySelector(":scope > summary") ?? node).innerText]),
  };
}

function voucherTailDiagnosticSelection(config, environment = process.env) {
  const enabled = config.diagnostic_voucher_tail === true || environment.STAGE9_DIAGNOSTIC_VOUCHER_TAIL === "1";
  if (!enabled) return false;
  assert(config.diagnostic_voucher_tail === true, "voucher tail diagnostic must be explicitly labelled by the benchmark");
  assert(config.interaction_only === true && !config.layout_only && !config.navigation_only
    && environment.STAGE9_RETAINED_REFRESH_ONLY !== "1", "voucher tail diagnostic requires only existing interactions");
  assert(config.warmups === 1 && config.repeats === 3, "voucher tail diagnostic requires one warmup and three samples");
  return true;
}

async function startVoucherTailTrace(context, page, filename) {
  const client = await context.newCDPSession(page), events = [];
  client.on("Tracing.dataCollected", value => events.push(...value.value));
  await client.send("Tracing.start", { categories: "devtools.timeline,v8.execute,disabled-by-default-v8.cpu_profiler", transferMode: "ReportEvents" });
  return { async finish() {
    try {
      const completed = new Promise(resolve => client.once("Tracing.tracingComplete", resolve));
      await client.send("Tracing.end");
      await waitForStage9(() => completed, "voucher diagnostic renderer trace", Date.now() + 10000);
      fs.writeFileSync(filename, JSON.stringify({ diagnostic_only: true, sla_eligible: false, traceEvents: events }), { flag: "wx" });
      return { path: filename, event_count: events.length, diagnostic_only: true, sla_eligible: false };
    } finally { await client.detach(); }
  } };
}

function interactionReadMultiset(reads) {
  const counts = new Map();
  for (const read of reads) {
    const url = interactionReadUrl(typeof read === "string" ? read : read.url);
    counts.set(url, (counts.get(url) ?? 0) + 1);
  }
  return [...counts].sort(([a], [b]) => a.localeCompare(b)).map(([url, count]) => ({ url, count }));
}

function indexInteractionReads(fetches, resources, start) {
  const requests = new Map(), downloads = new Map();
  const byWire = new Map(), positions = new Map();
  for (const item of fetches) {
    if (!(item.start >= start)) continue;
    const record = requests.get(item.url) ?? { count: 0, successful: 0 };
    record.count += 1;
    if (item.state === "response" && item.status === 200) record.successful += 1;
    requests.set(item.url, record);
    if (item.wire_url) {
      const values = byWire.get(item.wire_url) ?? [];
      values.push(item); byWire.set(item.wire_url, values);
    }
  }
  for (const item of resources) {
    if (!(item.startTime >= start && item.responseEnd > start)) continue;
    const candidates = byWire.get(item.name);
    let observed;
    if (candidates) {
      let position = positions.get(item.name) ?? 0;
      while (position + 1 < candidates.length && candidates[position + 1].start <= item.startTime) position++;
      positions.set(item.name, position);
      if (candidates[position].start <= item.startTime) observed = candidates[position];
    }
    const url = interactionReadUrl(observed?.url ?? item.name);
    const record = downloads.get(url) ?? { count: 0, end: start };
    record.count += 1; record.end = Math.max(record.end, item.responseEnd);
    downloads.set(url, record);
  }
  return { requests, downloads };
}

function interactionReadPlanReady(selection, fetches, resources, start, index = null) {
  const plan = selection.read_plan;
  if (!plan || plan.cycle_id !== selection.cycle_id || !["action", "company_id", "period", "snapshot"].every(key => selection.scope?.[key]
    && plan.scope?.[key] === selection.scope[key])) return false;
  if (!Array.isArray(plan.required)) return false;
  if (!plan.required.length) return !selection.require_new_response && plan.cache_verified === true;
  index ??= indexInteractionReads(fetches, resources, start);
  const seen = new Set();
  return plan.required.every(read => {
    if (!Number.isInteger(read.count) || read.count < 1 || seen.has(read.url)) return false;
    seen.add(read.url);
    const url = new URL(read.url);
    if (!interactionScopeAllows(url, selection.scope)) return false;
    const requests = index.requests.get(read.url), downloads = index.downloads.get(read.url);
    return requests?.count === read.count && requests.successful === read.count && downloads?.count === read.count;
  });
}

// Independent interaction endpoint. The expected complete DOM is calibrated
// from the same real service/snapshot outside timing, never from fixture data.
const observeInteraction = new Function("selection", `
  if (selection.navigation && location.href !== selection.url) return;
  const projection = ${readRefreshProjection.toString()};
  const resultHasVisibleContent = ${resultHasVisibleContent.toString()};
  const interactionReadUrl = ${interactionReadUrl.toString()};
  const dashboardReadTarget = ${dashboardReadTarget.toString()};
  const interactionScopeAllows = ${interactionScopeAllows.toString()};
  const indexInteractionReads = ${indexInteractionReads.toString()};
  const interactionReadPlanReady = ${interactionReadPlanReady.toString()};
  const readPosition = ${readInteractionPosition.toString()};
  const positionReady = ${interactionPositionReady.toString()};
  window.__stage9InteractionCleanup?.();
  let observer, trigger, active = true;
  const originalFetch = window.fetch;
  const monitoredFetch = function(input, ...args) {
    const state = window.__stage9Interaction;
    const wireUrl = new URL(typeof input === 'string' ? input : input.url, location.href).href;
    const url = interactionReadUrl(dashboardReadTarget(wireUrl, args[0]?.method ?? 'GET', args[0]?.body).href);
    const required = state?.started && active && selection.read_plan.required.some(read => read.url === url);
    const read = required ? { url, wire_url: wireUrl, start: performance.now(), state: 'pending', status: null } : null;
    if (read) state.fetches.push(read);
    return originalFetch.call(this, input, ...args).then(response => {
      if (read) { read.status = response.status; read.state = 'response'; }
      if (selection.diagnostic_voucher_tail && read) {
        const originalJson = response.json;
        response.json = function(...args) {
          const diagnostic = window.__stage9VoucherTailMetrics;
          const span = { url, start: performance.now(), end: null, status: 'pending' };
          diagnostic?.json_reads.push(span);
          return originalJson.apply(this, args).then(value => {
            span.end = performance.now(); span.status = 'complete'; return value;
          }, error => { span.end = performance.now(); span.status = 'failed'; throw error; });
        };
      }
      return response;
    }, error => { if (read) read.state = 'failed'; throw error; });
  };
  window.fetch = monitoredFetch;
  performance.setResourceTimingBufferSize(2000);
  window.__stage9InteractionCleanup = () => {
    active = false; observer?.disconnect();
    if (trigger) { document.removeEventListener('click', trigger, true); document.removeEventListener('keydown', trigger, true); }
    if (window.fetch === monitoredFetch) window.fetch = originalFetch;
  };
  const start = () => {
    if (!active) return;
    if (window.__stage9Interaction?.started) return;
    const header = document.querySelector('.module-header');
    const state = window.__stage9Interaction = { started: true, start: selection.navigation ? 0 : performance.now(), end: null, flashes: [], cycle_id: selection.cycle_id, fetches: [] };
    if (selection.diagnostic_voucher_tail) {
      state.diagnostic = window.__stage9VoucherTailMetrics = {
        time_origin_epoch_ms: performance.timeOrigin,
        json_reads: [], frames: [], projections: [], observers: {
          interaction: { callbacks: 0, records: 0, added_nodes: 0, milliseconds: 0 },
          local: { callbacks: 0, records: 0, added_nodes: 0, milliseconds: 0 },
        },
        attribution: 'JSON spans include async body consumption; frame work includes observation. Neither isolates Ajv/Vue/layout CPU; use the independent CDP trace. Instrumentation overhead is retained, never subtracted.',
      };
    }
    let frames = 0, previous = null, previousPosition = null;
    const observeMutations = records => {
      if (!selection.navigation && !selection.refresh && (!header?.isConnected || header.getAttribute('aria-busy') !== 'false'
        || records.some(record => record.target === header && record.attributeName === 'aria-busy' && record.oldValue === 'true'))) state.flashes.push('whole-page loading');
    };
    observer = new MutationObserver(selection.diagnostic_voucher_tail ? records => {
      const started = performance.now(), metric = state.diagnostic.observers.interaction;
      try { observeMutations(records); }
      finally { metric.callbacks++; metric.records += records.length;
        metric.added_nodes += records.reduce((count, record) => count + record.addedNodes.length, 0);
        metric.milliseconds += performance.now() - started; }
    } : observeMutations);
    if (!selection.navigation && !selection.refresh) observer.observe(document.body, { subtree: true, attributes: true, attributeOldValue: true, childList: true });
    const frame = () => {
      if (!active) return;
      const diagnosticFrame = selection.diagnostic_voucher_tail ? { start: performance.now(), network_ready: false, basic_ready: false } : null;
      const root = document.querySelector(selection.root);
      const resources = performance.getEntriesByType('resource');
      const index = indexInteractionReads(state.fetches, resources, state.start);
      const networkReady = interactionReadPlanReady(selection, state.fetches, resources, state.start, index);
      if (networkReady) state.network_complete_at = selection.read_plan.required.reduce((end, read) => Math.max(end, index.downloads.get(read.url).end), state.start);
      const basicReady = networkReady && resultHasVisibleContent(document, root) && document.querySelector('.module-header')?.getAttribute('aria-busy') === 'false'
        && !Array.from(root.querySelectorAll('[role=status], [aria-busy=true]')).some(node => node.getClientRects().length && /正在读取|正在加载/.test(node.textContent ?? ''))
        && selection.counts.every(([selector, count]) => document.querySelectorAll(selector).length === count);
      // No complete-list text/control walk while downloads or local loading
      // are unfinished. The same complete projection is still required twice.
      const projectionStarted = selection.diagnostic_voucher_tail && basicReady ? performance.now() : null;
      const current = basicReady ? projection(document, selection.root) : null;
      if (selection.diagnostic_voucher_tail && basicReady) {
        const finished = performance.now();
        state.diagnostic.projections.push({ start: projectionStarted, end: finished,
          milliseconds: finished - projectionStarted, utf8_bytes: new TextEncoder().encode(current).length });
      }
      const position = basicReady && selection.position ? readPosition(document, selection.position) : null;
      const ready = basicReady && current === selection.projection && positionReady(selection.position, position);
      frames = ready && current === previous && JSON.stringify(position) === JSON.stringify(previousPosition) ? frames + 1 : ready ? 1 : 0;
      previous = current;
      previousPosition = position;
      if (diagnosticFrame) { diagnosticFrame.end = performance.now(); diagnosticFrame.network_ready = networkReady;
        diagnosticFrame.basic_ready = basicReady; diagnosticFrame.ready = ready; state.diagnostic.frames.push(diagnosticFrame); }
      if (frames >= 2) { state.end = performance.now(); if (selection.position) state.position = position; observer.disconnect(); }
      else if (performance.now() - state.start < 30000) requestAnimationFrame(frame);
      else { state.error = 'interaction completion timeout'; observer.disconnect(); }
    };
    requestAnimationFrame(frame);
  };
  if (selection.navigation) {
    if (location.href === selection.url) start();
  } else {
    window.__stage9Interaction = null;
    trigger = event => {
      if (event.type === 'keydown' && event.key !== 'Enter') return;
      if (!event.target.closest(selection.trigger)) return;
      document.removeEventListener('click', trigger, true); document.removeEventListener('keydown', trigger, true);
      start();
    };
    document.addEventListener('click', trigger, true); document.addEventListener('keydown', trigger, true);
  }
`);

function verifyExactInteractionTarget(payload, target, selected, section, identity) {
  const field = section === "employees" ? "employee_id" : "asset_id";
  assert.equal(target.pathname, `/api/dashboard/${section}`);
  assert.equal(target.searchParams.get("company_id"), selected.id);
  assert.equal(target.searchParams.get("period"), selected.period);
  assert.equal(target.searchParams.get(field), identity);
  assert(!target.searchParams.has("cursor") && !target.searchParams.has("section"), "exact interaction: paginated target search");
  assert.equal(payload.data?.[field], identity, "exact interaction: response selected another identity");
  const items = payload.data?.collections?.[section]?.items;
  assert(Array.isArray(items) && items.length === 1 && items[0][field] === identity, "exact interaction: target missing or unrelated rows");
}

const percentile = (sorted, proportion) => sorted[Math.ceil(sorted.length * proportion) - 1];
function stage9TimingSummary(timings, targetMs, complete = true) {
  assert(targetMs === null || [500, 600, 700, 1000].includes(targetMs), "unknown Stage 9 timing target");
  if (complete) assert(timings.length > 0 && timings.every(value => Number.isFinite(value) && value >= 0), "invalid completed Stage 9 samples");
  const sorted = complete ? [...timings].sort((a, b) => a - b) : [];
  return {
    target_ms: targetMs,
    median_ms: complete ? percentile(sorted, .5) : null,
    p95_ms: complete ? percentile(sorted, .95) : null,
    max_ms: complete ? sorted.at(-1) : null,
    over_target_count: targetMs === null ? null : timings.filter(value => Number.isFinite(value) && value >= targetMs).length,
    // Preserve the original diagnostic counter for comparison with old reports.
    over_500_ms: timings.filter(value => Number.isFinite(value) && value >= 500).length,
    over_700_ms: timings.filter(value => Number.isFinite(value) && value >= 700).length,
    over_1000_ms: timings.filter(value => Number.isFinite(value) && value >= 1000).length,
    ...(targetMs === 600 ? { over_600_ms: timings.filter(value => Number.isFinite(value) && value >= 600).length } : {}),
  };
}
function stage9RefreshTarget(config) {
  if (config.refresh_target_ms === null) return null;
  const target = config.refresh_target_ms ?? 500;
  assert([500, 700, 1000].includes(target), "unknown Stage 9 refresh target");
  return target;
}
function stage9MeasurementStatus(pages, sampleCount, completed = true) {
  const values = Object.values(pages);
  const complete = completed && Number.isInteger(sampleCount) && sampleCount > 0 && values.length > 0 && values.every(page => page.samples?.length === sampleCount
    && page.samples.every(value => Number.isFinite(value) && value >= 0));
  return complete ? "passed" : "failed";
}
function stage9InteractionStatus(results, targetMs) {
  const values = Object.values(results);
  if (values.some(value => value.status === "failed")
    || targetMs === null && (!values.length || values.some(value => value.status !== "measured"))) return "failed";
  return targetMs !== null && values.some(value => value.over_target_count) ? "over_target" : "passed";
}
function stage9RefreshThresholdResults(pages, sampleCount, completed = true) {
  const values = Object.values(pages);
  const complete = stage9MeasurementStatus(pages, sampleCount, completed) === "passed";
  return Object.fromEntries([500, 700, 1000].map(target => {
    const over = values.reduce((count, page) => count + (page.samples ?? []).filter(value => Number.isFinite(value) && value >= target).length, 0);
    return [target, { target_ms: target, status: complete ? over ? "over_target" : "passed" : "failed", over_target_count: over }];
  }));
}
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

async function waitForStage9(operation, phase, deadline = Date.now() + 30000) {
  const remaining = deadline - Date.now();
  assert(remaining > 0, `${phase}: stage9 wait deadline exceeded`);
  let timer;
  try {
    return await Promise.race([
      Promise.resolve().then(operation),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${phase}: stage9 wait deadline exceeded`)), remaining);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

function waitForStage9Frames(page, phase, deadline) {
  return waitForStage9(() => page.evaluate(() => new Promise(resolve =>
    requestAnimationFrame(() => requestAnimationFrame(resolve)))), phase, deadline);
}

function selectStage9Option(page, label, value, deadline) {
  const { selectDashboardOption } = require("./helpers/dashboard-select.cjs");
  return waitForStage9(() => selectDashboardOption(page, label, value), `select ${label}=${value}`, deadline);
}

async function closeStage9Browser(context, browser, failureReport) {
  const cleanupErrors = [];
  for (const [phase, operation] of [["context close", () => context.close()], ["browser close", () => browser.close()]]) {
    try { await waitForStage9(operation, phase, Date.now() + 5000); }
    catch (error) { cleanupErrors.push(sanitize(error.message)); }
  }
  if (cleanupErrors.length) {
    if (failureReport) failureReport.cleanup_errors = cleanupErrors;
    else throw new Error(cleanupErrors.join("; "));
  }
}

// Keep filter and cursor identity in diagnostics; never report login material.
function verifySelectedFilter(selection, target, payload, section, expected) {
  const employee = ["employees", "labor_workers"].includes(section);
  const label = employee ? "筛选人员" : "筛选资产";
  const field = employee ? "employee_filter" : "asset_filter";
  assert.equal(selection.filters.find(item => item.label === label)?.value, expected, `${section}: wrong selected UI filter`);
  assert.equal(target.searchParams.get(field), expected, `${section}: wrong requested filter`);
  assert.equal(payload.data?.[field], expected, `${section}: wrong response filter`);
}

// An action number marks listener entrance, not the actual selection change.
// A queued continuation of the previous selection can start inside that number.
function localReadMatchesSelection(record, action, snapshot, requestedSection = null, { fresh = false, cached = false } = {}) {
  if (!record || !action?.after || (!cached && record.started_action !== action.action_id)) return false;
  const target = new URL(record.url), selected = new URL(action.after.url);
  if (target.origin !== selected.origin || target.pathname !== `/api/dashboard/${action.page}`
    || !snapshot || target.searchParams.get("expected_version") !== snapshot
    || ["company_id", "period"].some(key => !selected.searchParams.get(key)
      || target.searchParams.get(key) !== selected.searchParams.get(key))) return false;
  const section = target.searchParams.get("section");
  if (!section || requestedSection && section !== requestedSection || fresh && target.searchParams.has("cursor")) return false;
  if (action.page === "employees" || action.page === "assets") {
    const employee = action.page === "employees", field = employee ? "employee_filter" : "asset_filter";
    const identity = employee ? "employee_id" : "asset_id";
    const label = employee ? "筛选人员" : "筛选资产";
    const expected = selected.searchParams.get(identity) ? "all"
      : action.after.filters?.find(item => item.label === label)?.value;
    if (!expected || target.searchParams.get(field) !== expected
      || (target.searchParams.get(identity) ?? "") !== (selected.searchParams.get(identity) ?? "")) return false;
    if (!employee && (target.searchParams.get("project_id") ?? "") !== (selected.searchParams.get("project_id") ?? "")) return false;
  }
  if (action.page === "funds" && section === "movements") {
    if (action.after.funds_view !== "fund-detail-tab-book") return false;
    const account = action.after.movement_account;
    if (!account || ["movement_account_type", "movement_account_id"].some(key => target.searchParams.get(key) !== account[key])) return false;
  }
  if (action.page === "funds" && section === "statements"
    && (action.after.funds_view !== "fund-detail-tab-bank"
      || (target.searchParams.get("statement_account_id") ?? "") !== (action.after.statement_account_id ?? ""))) return false;
  if (action.page === "brief") {
    // Collection pages never carry an overview's exact-voucher request.
    if (target.searchParams.has("voucher_version_id") || target.searchParams.has("voucher_number")) return false;
    if (section === "vouchers" && action.after.brief_view !== "按凭证") return false;
    if (section === "activity" && action.after.brief_view !== "按业务") return false;
  }
  return true;
}

async function verifyLocalActionReplies(replies, { isCurrent, verify, isObsolete, onCurrent }) {
  const processed = new Set(), payloads = [];
  while (processed.size < replies.length) {
    // Current complete bodies establish replacement proof before an aborted
    // old body is considered. Successful old bodies still receive validation.
    const entry = replies.find(item => !processed.has(item) && isCurrent(item))
      ?? replies.find(item => !processed.has(item));
    processed.add(entry);
    if (await isObsolete(entry)) continue;
    const payload = await verify(entry);
    if (isCurrent(entry)) { await onCurrent(entry, payload); payloads.push(payload); }
  }
  return payloads;
}

async function localReplyIsObsolete(reply, { failureFor, classifyObsolete, finish }) {
  const classify = failure => {
    assert(classifyObsolete(failure), `${failure.path ?? "dashboard"}: unapproved response failure ${failure.error ?? failure.status}`);
    return true;
  };
  // A cancelled response may already have a failed Request while its body
  // completion promise never resolves. Exact replacement proof, rather than
  // the action's entrance time, decides whether this old continuation is safe.
  const recorded = failureFor(reply);
  if (recorded) return classify(recorded);
  await finish(reply);
  const completed = failureFor(reply);
  return completed ? classify(completed) : false;
}
function verifyBriefGroup(payload, target, selected, snapshot) {
  assert.equal(payload.schema_version, responseVersions.dashboard_brief_group, "brief group: outdated contract");
  assert.equal(target.searchParams.get("company_id"), selected.id);
  assert.equal(target.searchParams.get("period"), selected.period);
  assert.equal(payload.read_context?.company_id, selected.id);
  assert.equal(payload.selected_period?.key, selected.period);
  assert.equal(payload.snapshot_version, snapshot);
  assert.equal(target.searchParams.get("expected_version"), snapshot);
  assert.equal(payload.data?.section, target.searchParams.get("section"));
  assert.equal(payload.data?.group_key, target.searchParams.get("group_key"));
  const { members, vouchers } = payload.data.collections;
  const limit = stage9RequestPageLimit(target);
  verifyCollection(members, "brief group members", { cursor: target.searchParams.has("cursor"), limit }); verifyCollection(vouchers, "brief group vouchers", { cursor: true, limit });
  assert(members.items.every(item => item.group_key === payload.data.group_key), "brief group: member outside selected group");
  if (payload.data.section === "activity") assert(members.items.every(item => item.detail_scope_key === null || item.detail_scope_key === item.key), "brief group: progress association mismatch");
  const ids = [...new Set(members.items.flatMap(item => item.voucher_version_id ? [item.voucher_version_id] : []))];
  assert.deepEqual(vouchers.items.map(item => item.voucher_version_id), ids, "brief group: voucher association mismatch");
  for (const item of vouchers.items) verifyVoucher(item);
  return payload;
}
function diagnosticUrl(value) {
  const url = new URL(value);
  url.username = ""; url.password = "";
  for (const key of [...url.searchParams.keys()]) {
    if (/ticket|capability|token|password|secret|credential/i.test(key)) url.searchParams.set(key, "[redacted]");
  }
  url.hash = "";
  return url.href;
}

function createRequestDiagnostics() {
  const records = new Map();
  // The cycle retains Request objects until it validates their responses. Keep
  // completion identity available without retaining every historical request.
  const completed = new WeakMap();
  let sequence = 0;
  return {
    get(request) { return records.get(request) ?? completed.get(request); },
    start(request, action, pageUrl) {
      const record = { request_id: ++sequence, url: diagnosticUrl(request.url()),
        started_action: action, started_page_url: diagnosticUrl(pageUrl), state: "pending" };
      records.set(request, record);
      return record;
    },
    finish(request, state, action, pageUrl) {
      const record = records.get(request);
      if (!record) return null;
      record.state = state;
      record.finished_action = action;
      record.finished_page_url = diagnosticUrl(pageUrl);
      records.delete(request);
      completed.set(request, record);
      return { ...record };
    },
    pending() { return [...records.values()].map(record => ({ ...record })); },
  };
}

function verifiedRequestRecord(diagnostics, verifiedRequests, request, label) {
  const record = diagnostics.get(request);
  assert(verifiedRequests.has(request) && record?.state === "finished", `${label}: required response not successfully verified`);
  return record;
}

function obsoleteLocalCancellation(failure, action, snapshot, previousActions = []) {
  if (failure.error !== "net::ERR_ABORTED" || failure.status || !action?.after || !snapshot) return null;
  const record = failure.request;
  if (!record || record.url !== failure.url || record.state !== "failed"
    || !Number.isInteger(record.request_id) || !(record.started_action === null || Number.isInteger(record.started_action))) return null;
  const observedPending = item => item.request_id === record.request_id && item.url === record.url && item.state === "pending"
    && item.started_action === record.started_action && item.started_page_url === record.started_page_url;
  const delayedFinish = (record.finished_action === null || (Number.isInteger(record.finished_action) && record.finished_action > action.action_id))
    && action.status === "passed" && action.pending_after?.some(observedPending);
  if (record.finished_action !== action.action_id && !delayedFinish) return null;
  const old = action.pending_before?.find(item => item.request_id === record.request_id);
  const previouslyPending = old && old.url === record.url && old.state === "pending"
    && old.started_action === record.started_action && old.started_page_url === record.started_page_url
    && (record.started_action === null || record.started_action < action.action_id);
  // Queued old continuations can start after entrance, including account changes
  // whose selection is local state. Resource identity proves which account was
  // superseded; a same-action number or an abort by itself proves nothing.
  const requestUrl = new URL(record.url);
  const previousAccountResource = action.page === "funds" && action.section === "movements"
    && requestUrl.pathname === "/api/dashboard/funds" && requestUrl.searchParams.get("section") === "movements"
    && ["movement_account_type", "movement_account_id"].every(key => action.before.movement_account?.[key]
      && requestUrl.searchParams.get(key) === action.before.movement_account[key])
    && ["movement_account_type", "movement_account_id"].some(key => action.before.movement_account?.[key] !== action.after.movement_account?.[key]);
  const beforeClickPage = !old && record.started_action === action.action_id
    && record.started_page_url === action.before.url && record.finished_page_url === action.after.url
    && (action.before.url !== action.after.url || previousAccountResource);
  if (!previouslyPending && !beforeClickPage) return null;
  if (record.finished_page_url !== action.after.url) return null;
  let startedInPreviousSelection = record.started_page_url === action.before.url;
  if (!startedInPreviousSelection && previouslyPending && action.page === "funds") {
    const display = previousActions.find(item => item.action_id === record.started_action);
    const withoutView = value => { const scoped = new URL(value); scoped.searchParams.delete("funds_view"); return scoped.href; };
    const views = { "fund-detail-tab-bank": "statements", "fund-detail-tab-book": "movements" };
    startedInPreviousSelection = display?.status === "passed" && display.page === "funds" && display.after
      && display.before.url === record.started_page_url && display.after.url === action.before.url
      && withoutView(display.before.url) === withoutView(display.after.url)
      && views[display.before.funds_view] && views[display.after.funds_view]
      && display.before.funds_view !== display.after.funds_view
      && ["movement_account_type", "movement_account_id"].every(key => action.before.movement_account?.[key]
        && display.before.movement_account?.[key] === action.before.movement_account[key]
        && display.after.movement_account?.[key] === action.before.movement_account[key])
      && (new URL(display.before.url).searchParams.get("funds_view") ?? "book") === (display.before.funds_view === "fund-detail-tab-bank" ? "bank" : "book")
      && (new URL(display.after.url).searchParams.get("funds_view") ?? "book") === (display.after.funds_view === "fund-detail-tab-bank" ? "bank" : "book")
      && display.pending_after?.some(item => item.request_id === record.request_id && item.url === record.url && item.state === "pending"
        && item.started_action === record.started_action && item.started_page_url === record.started_page_url)
      && [...(display.verified_reads ?? []), ...(display.verified_cached_reads ?? [])].some(read => {
        const target = new URL(read.url), oldTarget = new URL(record.url);
        return read.action_id === display.action_id && read.section === views[display.after.funds_view]
          && target.origin === oldTarget.origin && target.pathname === oldTarget.pathname
          && ["company_id", "period", "expected_version"].every(key => target.searchParams.get(key) === oldTarget.searchParams.get(key));
      });
  }
  if (!startedInPreviousSelection) return null;
  const url = new URL(record.url), before = new URL(action.before.url), after = new URL(action.after.url);
  const started = new URL(record.started_page_url), finished = new URL(record.finished_page_url);
  if ([url, after, started, finished].some(value => value.origin !== before.origin)
    || [after, started, finished].some(value => value.pathname !== before.pathname)) return null;
  for (const key of ["company_id", "period"]) {
    const selected = before.searchParams.get(key);
    if (!selected || [url, after, started, finished].some(value => value.searchParams.get(key) !== selected)) return null;
  }
  if (url.searchParams.get("expected_version") !== snapshot || !url.searchParams.get("cursor")) return null;
  const section = url.searchParams.get("section");
  const replacementVerified = targetSection => [...(action.verified_reads ?? []), ...(action.verified_cached_reads ?? [])]
    .some(read => {
      const current = new URL(read.url);
      return read.action_id === action.action_id && read.section === targetSection
        && current.origin === url.origin && current.pathname === url.pathname
        && ["company_id", "period", "expected_version"].every(key => current.searchParams.get(key) === url.searchParams.get(key));
    });
  if (action.page === "funds" && url.pathname === "/api/dashboard/funds") {
    const views = { "fund-detail-tab-bank": "statements", "fund-detail-tab-book": "movements" };
    if (views[action.before.funds_view] === section && views[action.after.funds_view]
      && action.before.funds_view !== action.after.funds_view
      && (started.searchParams.get("funds_view") ?? "book") === (section === "statements" ? "bank" : "book")
      && (finished.searchParams.get("funds_view") ?? "book") === (action.after.funds_view === "fund-detail-tab-bank" ? "bank" : "book")
      && replacementVerified(views[action.after.funds_view])) return "previous_funds_view_page";
    if (section === "movements" && action.section === "movements"
      && action.before.funds_view === "fund-detail-tab-book" && action.after.funds_view === "fund-detail-tab-book"
      && action.before.movement_account_index >= 0 && action.after.movement_account_index >= 0
      && action.before.movement_account_index !== action.after.movement_account_index) {
      const replaced = action.verified_reads?.some(read => {
        if (read.section !== section || read.action_id !== action.action_id) return false;
        const current = new URL(read.url);
        const keys = ["movement_account_type", "movement_account_id"];
        const otherScope = target => {
          const params = new URLSearchParams(target.searchParams);
          for (const key of [...keys, "cursor"]) params.delete(key);
          params.sort(); return params.toString();
        };
        return otherScope(current) === otherScope(url)
          && !current.searchParams.has("cursor")
          && ["company_id", "period", "expected_version"].every(key => current.searchParams.get(key) === url.searchParams.get(key))
          && current.pathname === url.pathname && current.origin === url.origin
          && keys.every(key => url.searchParams.get(key) && current.searchParams.get(key))
          && keys.every(key => url.searchParams.get(key) === action.before.movement_account?.[key])
          && keys.every(key => current.searchParams.get(key) === action.after.movement_account?.[key])
          && keys.some(key => url.searchParams.get(key) !== current.searchParams.get(key));
      });
      if (replaced) return "previous_movement_account_page";
    }
  }
  if ((action.page === "employees" && ["employees", "labor_workers"].includes(section) || action.page === "assets" && section === "assets") && url.pathname === `/api/dashboard/${action.page}`) {
    const field = action.page === "employees" ? "employee_filter" : "asset_filter";
    const defaultFilter = action.page === "employees" ? "employment_active" : "active";
    const label = action.page === "employees" ? "筛选人员" : "筛选资产";
    const previous = action.before.filters?.find(item => item.label === label)?.value;
    const current = action.after.filters?.find(item => item.label === label)?.value;
    const otherScope = target => {
      const params = new URLSearchParams(target.searchParams);
      for (const key of [field, "cursor", "limit"]) params.delete(key);
      params.sort(); return params.toString();
    };
    if (previous && current && previous !== current && url.searchParams.get(field) === previous
      && (before.searchParams.get(field) ?? defaultFilter) === previous
      && (after.searchParams.get(field) ?? defaultFilter) === current
      && (started.searchParams.get(field) ?? defaultFilter) === previous
      && (finished.searchParams.get(field) ?? defaultFilter) === current
      && action.verified_reads?.some(read => read.action_id === action.action_id && read.section === section
        && !new URL(read.url).searchParams.has("cursor")
        && ["company_id", "period", "expected_version"].every(key => new URL(read.url).searchParams.get(key) === url.searchParams.get(key))
        && new URL(read.url).pathname === url.pathname && new URL(read.url).origin === url.origin
        && otherScope(new URL(read.url)) === otherScope(url)
        && new URL(read.url).searchParams.get(field) === current)) return action.page === "employees" ? "previous_employee_filter_page" : "previous_asset_filter_page";
  }
  if (action.page === "brief" && url.pathname === "/api/dashboard/brief") {
    const sections = { "本月发生": "activity", "应收应付": "open_items" };
    if (sections[action.before.active_section] === section && sections[action.after.active_section]
      && action.before.active_section !== action.after.active_section
      && replacementVerified(sections[action.after.active_section])) return "previous_brief_section_page";
    if (action.before.active_section === "本月发生" && action.after.active_section === "本月发生"
      && ((section === "activity" && action.before.brief_view === "按业务" && action.after.brief_view === "按凭证")
        || (section === "vouchers" && action.before.brief_view === "按凭证" && action.after.brief_view === "按业务"))
      && replacementVerified(action.after.brief_view === "按凭证" ? "vouchers" : "activity")) return "previous_brief_display_page";
  }
  return null;
}

async function recordRefreshAttempt(attempts, metadata, operation, failureTiming, checkpoint = null) {
  const attempt = { ...metadata, status: "preparing", phase: "prepare", elapsed_ms: null };
  attempts.push(attempt);
  checkpoint?.(attempt);
  try {
    const elapsed = await operation(attempt);
    attempt.status = "complete";
    attempt.elapsed_ms = elapsed;
    return elapsed;
  } catch (error) {
    const timing = await waitForStage9(failureTiming, "failed attempt timing", Date.now() + 1000).catch(error => {
      attempt.timing_error = sanitize(error.message);
      return null;
    });
    attempt.status = timing?.started ? "failed" : "preparation_failed";
    if (timing?.started && Number.isFinite(timing.elapsed_ms)) attempt.elapsed_ms = timing.elapsed_ms;
    attempt.error = { type: error?.name || "Error", message: sanitize(error?.message || error) };
    throw error;
  } finally {
    // Durable progress is written after the measured endpoint and validation.
    // The initial preparing checkpoint is before any click/timer has begun.
    checkpoint?.(attempt);
  }
}

function verifyLocalRead(url, module, selected, snapshot, requestedSection = null) {
  assert.equal(url.pathname, `/api/dashboard/${module.action}`, `${module.key}: display change read unrelated data`);
  const section = url.searchParams.get("section");
  assert(section && (section === requestedSection
    || module.key === "employees" && requestedSection === "employees" && section === "labor_workers"
    || (defaultCollections[module.key]?.includes(section) && url.searchParams.get("cursor"))),
  `${module.key}: display change reloaded whole page or unrelated collection`);
  assert.equal(url.searchParams.get("company_id"), selected.id, "local filter: company changed");
  assert.equal(url.searchParams.get("period"), selected.period, "local filter: month changed");
  assert(snapshot && url.searchParams.get("expected_version") === snapshot,
    "local filter: snapshot boundary changed or missing");
  if (module.key === "employees" && section === "labor_sources")
    assert(url.searchParams.get("employee_id"), "labor detail: exact personnel identity is required");
  stage9RequestPageLimit(url);
}

// Pure association/value extraction, also serialized into the final browser
// evaluate. Resource timing is browser network work, not a source CPU profile.
function captureRefreshResources(entries, selection, clickStart, renderAt, timeOrigin, reads = []) {
  const actions = ["context", selection.action];
  const resources = Object.fromEntries(actions.map(action => [action, []]));
  for (const entry of entries) {
    if (!(entry.startTime >= clickStart)) continue;
    let url;
    try { url = observedDashboardResource(entry, reads)?.target; } catch { continue; }
    if (!url) continue;
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
    const value = { action, path: new URL(entry.name).pathname, query };
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
    else if (resources[action].length > 1 && !(action !== 'context'
      && resources[action][0].path === `/api/dashboard/${action}/retry`
      && resources[action].slice(1).every(value => value.path === `/api/dashboard/${action}`))) diagnostics.push(`${action}:duplicate_resources`);
    for (const entry of resources[action]) {
      if (entry.responseEnd === null || entry.responseEnd <= clickStart
        || entry.responseEnd < entry.startTime || entry.responseEnd > renderAt
        || entry.duration === null || entry.duration < 0)
        diagnostics.push(`${action}:invalid_resource_timestamps`);
    }
  }
  const responseEnd = diagnostics.length === 0
    ? Math.max(...actions.flatMap(action => resources[action].map(value => value.responseEnd))) : null;
  return {
    timeOrigin, clickStart, renderAt, resources, diagnostics,
    render_tail_ms: responseEnd === null ? null : renderAt - responseEnd,
    attribution: "Browser ResourceTiming timestamps are relative to timeOrigin; render tail includes response processing, JSON, reactive DOM work and the existing two RAF completion check, not source CPU attribution or isolated paint time.",
  };
}

// Constructed in Node, before measurements; browser execution uses no eval and
// captures diagnostics only after the original completion timestamp is set.
const captureRefreshMeasurement = new Function("selection", `
  const observedDashboardResource = ${observedDashboardResource.toString()};
  const capture = ${captureRefreshResources.toString()};
  const clickStart = window.__stage9RefreshStart;
  const renderAt = window.__stage9RenderedAt;
  const measured = renderAt - clickStart;
  const resources = capture(performance.getEntriesByType("resource").map(entry => entry.toJSON()),
    selection, clickStart, renderAt, performance.timeOrigin, window.__stage9DashboardReads ?? []);
  delete window.__stage9RefreshStart;
  delete window.__stage9RenderedAt;
  return { measured, resources, start_epoch_ms: performance.timeOrigin + clickStart, end_epoch_ms: performance.timeOrigin + renderAt };
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
function verifyLongTermAssets(summary) {
  assert(summary && typeof summary === "object", "brief: long-term asset summary missing");
  wireFen(summary.net_fen, "long-term asset net", true);
  for (const field of ["fixed_active_count", "intangible_active_count"]) {
    assert(Number.isSafeInteger(summary[field]) && summary[field] >= 0,
      `brief: invalid long-term asset ${field}`);
  }
}
function formatFen(value) {
  if (value === null) return "暂无法确定";
  const amount = wireFen(value, "display amount"), absolute = amount < 0n ? -amount : amount;
  return `${amount < 0n ? "−" : ""}¥${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(absolute / 100n)}.${String(absolute % 100n).padStart(2, "0")}`;
}
function sumFen(first, second) {
  const left = wireFen(first, "first amount", true), right = wireFen(second, "second amount", true);
  return left === null || right === null ? null : (left + right).toString();
}
function employeeOutstandingFen(data, schemaVersion) {
  assert.equal(schemaVersion, responseVersions.dashboard_employees, "employees: unsupported outstanding response version");
  const value = data.outstanding_remuneration_fen;
  wireFen(value, "unpaid remuneration", true);
  return value;
}
function verifyCollection(collection, label, { cursor = false, limit = 20 } = {}) {
  assert(collection && Array.isArray(collection.items) && collection.page, `${label}: collection missing`);
  const { page, items } = collection;
  for (const key of ["total_count", "filtered_count", "returned_count"]) {
    assert(Number.isSafeInteger(page[key]) && page[key] >= 0, `${label}: invalid ${key}`);
  }
  assert(page.filtered_count <= page.total_count, `${label}: filtered count exceeds full count`);
  assert.equal(items.length, page.returned_count, `${label}: incomplete collection`);
  assert(Number.isInteger(limit) && limit >= 1 && limit <= 500 && (cursor || limit === 20), `${label}: invalid exact request page bound`);
  assert(items.length <= limit, `${label}: page exceeds exact request bound`);
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
  assert(["known", "not_applicable", "unknown"].includes(voucher.business_amount_state), "voucher: invalid business amount state");
  wireFen(voucher.business_amount_fen, "voucher business amount", voucher.business_amount_state !== "known");
  assert.equal(voucher.business_amount_fen === null, voucher.business_amount_state !== "known", "voucher: business amount differs from its explicit state");
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

function verifyVisibleVoucherRows(rows, voucher) {
  verifyVoucher(voucher);
  assert.equal(rows.length, voucher.lines.length, "voucher: visible entry lines missing");
  let debit = 0n, credit = 0n;
  for (const [index, row] of rows.entries()) {
    const expected = voucher.lines[index];
    assert.equal(row.code, expected.code, "voucher: visible account code differs");
    assert.equal(row.account, expected.account, "voucher: visible account differs");
    for (const [side, field] of [["debit", "debit_fen"], ["credit", "credit_fen"]]) {
      assert.equal(row[side], wireFen(expected[field], field) === 0n ? "—" : formatFen(expected[field]),
        `voucher: visible ${side} amount differs`);
    }
    debit += wireFen(expected.debit_fen, "debit");
    credit += wireFen(expected.credit_fen, "credit");
  }
  assert.equal(debit, credit, "voucher: visible entries do not balance");
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
  for (const name of section ? [section] : defaultCollections[key]) verifyCollection(payload.data.collections?.[name], `${key}/${name}`, { cursor, limit: stage9RequestPageLimit(target) });
  if (key === "employees") {
    assert.deepEqual(Object.keys(payload.data.collections).sort(), (section ? [section] : defaultCollections.employees).toSorted(),
      "employees: response preloaded unrelated labor details or omitted personnel collections");
    if (section === "labor_sources") {
      const person = target.searchParams.get("employee_id");
      assert(person && payload.data.employee_id === person, "employees: labor detail has no exact personnel identity");
      assert(payload.data.collections.labor_sources.items.every(item => item.person_id === person),
        "employees: another person leaked into labor detail");
    }
  }
  if (key === "brief") {
    assert.equal(payload.scope, section ?? "overview", "brief: response scope does not match request");
    const { vouchers, activity } = payload.data.collections;
    if (!section) {
      verifyBriefState(payload.data, selected);
      assert(payload.data.financial_position && payload.data.workforce_cost, "brief: main financial/workforce summaries missing");
      verifyLongTermAssets(payload.data.long_term_assets);
      if (target.searchParams.has("voucher_version_id") || target.searchParams.has("voucher_number")) {
        assert(payload.data.focused_voucher, "brief: explicit exact voucher missing");
        assert.equal(vouchers?.items.length, 1, "brief: exact read preloaded unrelated vouchers");
        assert.deepEqual(vouchers.items[0], payload.data.focused_voucher, "brief: exact collection differs from focused voucher");
      } else assert.equal(vouchers, undefined, "brief: default refresh preloaded independent vouchers");
      assert.equal(activity.page.total_count, payload.data.group_count, "brief: activity group total lost");
      assert.equal(payload.data.collections.open_items.page.total_count, payload.data.open_items.group_count, "brief: open group total lost");
      if (requireVouchers) assert(payload.data.voucher_count > 0 && activity.page.total_count > 0, "brief: synthetic book returned no vouchers/business");
    } else {
      assert.deepEqual(Object.keys(payload.data), ["collections"], "brief: partial read repeated overview data");
      assert.deepEqual(Object.keys(payload.data.collections), [section], "brief: partial read returned unrelated collections");
    }
    for (const voucher of vouchers?.items ?? []) verifyVoucher(voucher);
    if (payload.data.focused_voucher) verifyVoucher(payload.data.focused_voucher);
  }
}


// Independent S9 delivery observer. The original qualified harness is preserved.
// Boundary follows the fixed service dispatch: only main roots without an exact
// section/snapshot use Delivery; context and on-demand auxiliary reads are native.
function verifyS9WirePayload(validators, target, wire, selected, quarter, allowPreparing = false) {
  const action = target.pathname.split('/').at(-1);
  const suffix = { context: 'Context', brief: 'Brief', funds: 'Funds', employees: 'Employees', assets: 'Assets',
    'quarterly-report': 'QuarterlyReport', 'brief-group': 'BriefGroup', 'business-status': 'BusinessStatus',
    'close-review': 'CloseReview' }[action];
  assert(suffix, `S9 observer: unplanned response ${target.pathname}`);
  assert.equal(target.searchParams.get('company_id'), selected.id, 'S9 observer: wrong request company');
  const report = action === 'quarterly-report';
  const period = report ? `${target.searchParams.get('year')}-Q${target.searchParams.get('quarter')}` : selected.period;
  if (report) assert.equal(period, quarter, 'S9 observer: wrong requested quarter');
  else if (action !== 'context') assert.equal(target.searchParams.get('period'), period, 'S9 observer: wrong requested period');
  const delivery = ['brief', 'funds', 'employees', 'assets', 'quarterly-report'].includes(action)
    && !target.searchParams.has('section') && !target.searchParams.has('expected_version');
  let payload = wire;
  if (delivery) {
    const validator = validators[`validateDashboard${suffix}DeliveryResponse`];
    assert(validator && validator(wire), `S9 observer: generated delivery contract rejected ${target.pathname}`);
    assert.equal(wire.company_id, selected.id, 'S9 observer: wrong delivery company');
    assert.equal(wire.period, period, 'S9 observer: wrong delivery period');
    if (allowPreparing && wire.state === 'preparing') return null;
    assert.equal(wire.state, 'ready', 'S9 observer: wire is not ready');
    payload = wire.response;
    const version = report ? payload.read_context.read_version : payload.snapshot_version;
    assert(typeof wire.model_version === 'string' && wire.model_version.length, 'S9 observer: missing model version');
    assert.equal(wire.model_version, version, 'S9 observer: delivery/native model version mismatch');
  }
  const validator = validators[`validateDashboard${suffix}Response`];
  assert(validator && validator(payload), `S9 observer: generated native contract rejected ${target.pathname}`);
  assert.equal(payload.read_context?.company_id ?? payload.current_company?.company_id ?? payload.company_id,
    selected.id, 'S9 observer: wrong native company');
  if (report) assert.equal(`${payload.period.year}-Q${payload.period.quarter}`, period, 'S9 observer: wrong native quarter');
  else if (action === 'close-review') assert.equal(payload.period, period, 'S9 observer: wrong native close-review period');
  else if (action !== 'context') assert.equal(payload.selected_period?.key, period, 'S9 observer: wrong native period');
  if (target.searchParams.has('expected_version')) assert.equal(payload.snapshot_version,
    target.searchParams.get('expected_version'), 'S9 observer: wrong native snapshot');
  return payload;
}
function recordS9ActualWire(file, reply, wire) {
  if (!file) return;
  fs.appendFileSync(file, JSON.stringify({ url: diagnosticUrl(reply.url()), status: reply.status(),
    wire, observer_sha256: require('node:crypto').createHash('sha256').update(fs.readFileSync(__filename)).digest('hex') }) + '\n');
}

async function run(config) {
  if (config.progress_path) fs.writeFileSync(config.progress_path, "", { flag: "wx" });
  const checkpoint = config.progress_path ? attempt => {
    try { fs.appendFileSync(config.progress_path, JSON.stringify({ kind: "attempt", checkpoint_epoch_ms: Date.now(), attempt }) + "\n"); }
    catch (error) { attempt.status = "failed"; attempt.error = { type: "ProgressWriteError", message: sanitize(error.message) }; throw error; }
  } : null;
  const voucherTailDiagnostic = voucherTailDiagnosticSelection(config);
  const retainedRefreshOnly = retainedRefreshSelection(config);
  const generatedValidators = await import("../src/api/generated/dashboardValidators.js");
  const actualWirePath = process.env.STAGE9_DELIVERY_OBSERVER_WIRE_LOG;
  if (actualWirePath) fs.writeFileSync(actualWirePath, "", { flag: "wx" });
  const retainedPayloads = [];
  const retainedBodies = createRetainedResponseBodies();
  let retainedGeneration = 0, retainedCaptureActive = false;
  const selectDashboardOption = selectStage9Option;
  assert(config?.origin && config?.ticket_url && config?.playwright_module);
  assert(Array.isArray(config.companies) && config.companies.length >= 1);
  const company = config.companies.find(item => item.state === "prepared")
    ?? config.companies.find(item => item.state === "closed")
    ?? config.companies[0];
  assert(company.id && company.period && company.state !== "empty");
  const quarter = config.quarter ?? `${company.period.slice(0, 4)}-Q${Math.ceil(Number(company.period.slice(5)) / 3)}`;
  const warmups = config.warmups ?? 3, samples = config.repeats ?? 30;
  const refreshTargetMs = stage9RefreshTarget(config);
  const interactionTargetMs = refreshTargetMs === null ? null : 600;
  assert(Number.isInteger(warmups) && warmups >= (config.interaction_only ? 0 : 1));
  assert(Number.isInteger(samples) && samples >= 1);
  const { chromium } = require(config.playwright_module);
  const browser = await chromium.launch({ channel: config.channel, headless: true });
  const browserEnvironment = { node: process.version, browser: browser.version(), channel: config.channel, viewport: { width: 1440, height: 1000 }, reduced_motion: 'reduce' };
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
  const page = await context.newPage();
  await page.addInitScript(installDashboardReadObserver);
  if (voucherTailDiagnostic) await page.addInitScript(() => { window.__stage9VoucherTailDiagnosticEnabled = true; });
  const refreshProjections = new Map();
  const refreshAttempts = [];
  const verifiedRequests = new WeakSet();
  const verifiedPayloads = new WeakMap();
  let interactionEndpointPending = false;
  async function waitForInteractionCandidate() {
    if (!interactionEndpointPending) return;
    await page.waitForFunction(() => window.__stage9Interaction?.error || Number.isFinite(window.__stage9Interaction?.end));
    const candidate = await page.evaluate(() => ({ end: window.__stage9Interaction?.end, error: window.__stage9Interaction?.error }));
    assert(!candidate.error && Number.isFinite(candidate.end), candidate.error ?? "missing browser interaction candidate");
    interactionEndpointPending = false;
  }
  const interactionAttempts = [], interactionResults = {};
  page.setDefaultTimeout(30000);
  await page.addInitScript(({ selectedCompany, selectedPeriod, selectedQuarter }) => {
    performance.setResourceTimingBufferSize(1000);
    const specs = {
      "/": { action: "brief", visible: ["#overview", "#activity", "#open-items", "#owner-tasks"] },
      "/funds": { action: "funds", visible: ["#funds-overview", "#bank-details"] },
      "/employees": { action: "employees", visible: ["#employees-overview", "#employee-list-title", "#employees-cost-value", "#employees-count-value", "#employees-gross-value", "#employees-contributions-value", "#employees-paid-value", "#employees-outstanding-value"] },
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
      const asset = document.querySelector("#overview .kpi.asset");
      return ["open", "closed", "covered"].includes(state)
        && ["true", "false"].includes(required)
        && (required === "true" ? state === "open" && prompt?.getClientRects().length
          && prompt.querySelector("button")?.getClientRects().length : !prompt)
        && !document.querySelector(".close-review, #monthly-review, #close-review-title")
        && asset?.getClientRects().length && asset.querySelector("strong")?.innerText.trim()
        && /^固定 [0-9]+ 项 · 无形 [0-9]+ 项$/.test(asset.querySelector("small")?.innerText.trim());
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
        && (spec.action !== "brief" || briefReady())
        && (spec.action !== "employees" || (spec.visible.slice(2)
          .every(selector => document.querySelector(selector)?.innerText.trim())
          && /^[0-9]+ *人$/.test(document.querySelector("#employees-count-value")?.innerText.trim())));
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
  const staticResources = createStaticResourceObserver(new URL(config.origin).origin);
  const resourceFailures = staticResources.failures;
  const pageErrors = [], apiFailures = [], closeReviewRequests = [], legacyJobsRequests = [];
  const navigate = url => { staticResources.navigating(); return page.goto(url); };
  const requestDiagnostics = createRequestDiagnostics(), localActions = [], obsoleteCancellations = [], layoutPageReplies = [];
  const layoutVerifiedReads = [];
  let layoutMovementAccount = null;
  const layoutAccounts = [];
  const allowedLocalAborts = new Set();
  let currentLocalAction = null;
  const allowedCloseReviewRequests = new Set();
  page.on("request", request => {
    staticResources.start(request);
    if (retainedRefreshOnly) retainedBodies.start(request, retainedGeneration, diagnosticUrl(page.url()));
    if (dashboardRequestTarget(request).pathname.startsWith("/api/")) requestDiagnostics.start(request, currentLocalAction, page.url());
    if (dashboardRequestTarget(request).pathname === "/api/dashboard/close-review") closeReviewRequests.push(request.url());
    if (dashboardRequestTarget(request).pathname === "/api/local/jobs") legacyJobsRequests.push("/api/local/jobs");
  });
  const allowedNavigationAborts = new Set();
  page.on("pageerror", error => pageErrors.push(sanitize(error.message)));
  page.on("response", response => {
    const target = dashboardResponseTarget(response);
    staticResources.response(response);
    if (retainedRefreshOnly && retainedCaptureActive && target.pathname.startsWith("/api/dashboard/")) retainedBodies.capture(response);
    if ((config.layout_only || config.interaction_only) && target.pathname.startsWith("/api/dashboard/")
      && (target.searchParams.has("section") || target.searchParams.has("cursor")
        || ["/api/dashboard/brief-group", "/api/dashboard/business-status"].includes(target.pathname))) layoutPageReplies.push(response);
    if (dashboardResponseTarget(response).pathname.startsWith("/api/") && response.status() >= 400) {
      apiFailures.push({ path: dashboardResponseTarget(response).pathname, status: response.status(),
        url: diagnosticUrl(response.url()), request: requestDiagnostics.finish(response.request(), "http_error", currentLocalAction, page.url()) });
    }
  });
  page.on("requestfinished", request => {
    staticResources.finished(request);
    requestDiagnostics.finish(request, "finished", currentLocalAction, page.url());
  });
  page.on("requestfailed", request => {
    const url = dashboardRequestTarget(request);
    staticResources.failed(request);
    if (url.pathname.startsWith("/api/")) apiFailures.push({
      path: url.pathname, company_id: url.searchParams.get("company_id"),
      period: url.searchParams.get("period"),
      error: sanitize(request.failure()?.errorText ?? "network_failed"),
      url: diagnosticUrl(request.url()),
      request: requestDiagnostics.finish(request, "failed", currentLocalAction, page.url()),
    });
  });
  const result = {};
  const partialNavigation = {};
  const layoutChecks = [];
  let layoutProgress = null;
  let failureReport = null;

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
    return waitForLater(page.waitForResponse(async reply => {
      const url = dashboardResponseTarget(reply);
      return matchesAction(url, action) && await readyReceipt(reply);
    }));
  }
  async function readyReceipt(reply) {
    const target = dashboardResponseTarget(reply);
    if (!Object.hasOwn(mainContractNames, target.pathname.split('/').at(-1) === 'quarterly-report' ? 'reports' : target.pathname.split('/').at(-1))
      || target.searchParams.has('section') || target.searchParams.has('expected_version')) return true;
    const wire = await reply.json();
    if (wire?.state !== 'preparing') return true;
    assert.equal(reply.status(), 200, 'preparing delivery HTTP failure');
    verifyS9WirePayload(generatedValidators, target, wire, company, quarter, true);
    verifiedRequests.add(reply.request());
    recordS9ActualWire(actualWirePath, reply, wire);
    return false;
  }
  function assertNoBrowserFailures() {
    for (const failure of apiFailures) {
      if (allowedLocalAborts.has(failure)) continue;
      for (const action of localActions.filter(item => item.status === "passed").reverse()) {
        const reason = obsoleteLocalCancellation(failure, action, refreshProjections.get(action.page)?.snapshot, localActions);
        if (reason) {
          allowedLocalAborts.add(failure); obsoleteCancellations.push({ ...failure, reason, superseded_by_action: action.action_id });
          break;
        }
      }
    }
    assert.deepEqual(legacyJobsRequests, [], "default page requested the retired task list");
    assert.deepEqual(closeReviewRequests.filter(url => !allowedCloseReviewRequests.has(url)), [], "default page requested close-review");
    assert.deepEqual(pageErrors, [], "browser error");
    staticResources.assertComplete();
    assert.deepEqual(apiFailures.filter(failure => !allowedNavigationAborts.has(failure) && !allowedLocalAborts.has(failure)), [], "API error");
  }
  async function verifyReply(reply, module, selected = company, { requireVouchers = true, allowEmployeeContinuation = false } = {}) {
    await waitForInteractionCandidate();
    const target = dashboardResponseTarget(reply);
    const cached = verifiedPayloads.get(reply.request());
    if (cached && (target.searchParams.has("section") || target.searchParams.has("cursor"))) {
      assert.equal(reply.status(), 200, `${module.key}: cached response failed`);
      assert.equal(target.searchParams.get("company_id"), selected.id, `${module.key}: cached response scope changed`);
      assert.equal(target.searchParams.get("period"), selected.period, `${module.key}: cached response period changed`);
      if (retainedRefreshOnly) retainedBodies.consumed(reply);
      return cached;
    }
    assert.equal(reply.status(), 200, `${module.key}: ${target.pathname} failed`);
    const wire = cached ? null : await waitForStage9(() => retainedRefreshOnly && retainedCaptureActive
      ? retainedBodies.read(reply, retainedGeneration) : reply.json(), `${module.key}: response JSON body`);
    if (!cached) recordS9ActualWire(actualWirePath, reply, wire);
    const selectedQuarter = selected.id === company.id ? quarter
      : `${selected.period.slice(0, 4)}-Q${Math.ceil(Number(selected.period.slice(5)) / 3)}`;
    const payload = cached ?? verifyS9WirePayload(generatedValidators, target, wire, selected, selectedQuarter);
    if (cached && retainedRefreshOnly) retainedBodies.consumed(reply);
    if (retainedRefreshOnly) {
      verifyRetainedResponseContract(generatedValidators, target, payload);
    }
    const auxiliaryContract = {
      "/api/dashboard/context": "dashboard_context",
      "/api/dashboard/close-review": "dashboard_close_review",
      "/api/dashboard/business-status": "dashboard_business_status",
      "/api/dashboard/brief-group": "dashboard_brief_group",
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
    if (target.pathname === "/api/dashboard/business-status") {
      assert.equal(payload.data?.identity?.subject_id, target.searchParams.get("subject_id"), "business detail: wrong subject");
      assert.equal(payload.selected_period?.key, selected.period, "business detail: wrong period");
      assert(typeof payload.snapshot_version === "string" && payload.snapshot_version, "business detail: missing own snapshot");
      if (target.searchParams.has("expected_version")) assert.equal(payload.snapshot_version, target.searchParams.get("expected_version"), "business detail: wrong snapshot");
      else assert(!target.searchParams.has("cursor"), "business detail: continuation missing its own snapshot");
      {
        assert.equal(payload.data.settlement_view, target.searchParams.get("settlement_view") ?? "current", "business detail: wrong settlement view");
        const key = target.searchParams.get("detail_scope_key"), voucher = target.searchParams.get("voucher_version_id");
        if (key === null) assert(payload.data.detail_scope === null && voucher === null, "business detail: unrelated detail sub-scope");
        else assert(payload.data.detail_scope?.key === key && voucher !== null && payload.data.detail_scope.voucher_version_id === voucher, "business detail: wrong exact detail sub-scope");
        verifyCollection(payload.data.collections?.settlement_events, "business detail settlement events",
          { cursor: target.searchParams.has("cursor"), limit: stage9RequestPageLimit(target) });
      }
    }
    if (target.pathname === "/api/dashboard/brief-group") verifyBriefGroup(payload, target, selected, refreshProjections.get("brief")?.snapshot);
    if (target.pathname === `/api/dashboard/${module.action}`) {
      stage9RequestPageLimit(target);
      verifyMainPayload(payload, module.key, selected, target, { requireVouchers });
      if (config.layout_only || config.interaction_only) {
        if (module.key === "funds" && (!target.searchParams.has("section") || target.searchParams.get("section") === "accounts") && payload.data.collections.accounts) {
          if (!target.searchParams.has("cursor")) layoutAccounts.length = 0;
          for (const account of payload.data.collections.accounts.items)
            if (!layoutAccounts.some(item => item.account_id === account.account_id && item.type === account.type)) layoutAccounts.push(account);
        }
        if (module.key === "funds" && (!target.searchParams.has("section") || target.searchParams.get("section") === "movements")
          && !target.searchParams.has("cursor")) {
          const account = payload.data.selected_movement_account;
          layoutMovementAccount = account ? { movement_account_type: account.type, movement_account_id: account.account_id } : null;
        }
        for (const section of target.searchParams.has("section") ? [target.searchParams.get("section")] : defaultCollections[module.key] ?? []) {
          const scoped = new URL(target);
          scoped.searchParams.set("section", section);
          scoped.searchParams.set("expected_version", payload.snapshot_version);
          if (module.key === "funds" && section === "movements" && payload.data.selected_movement_account) {
            scoped.searchParams.delete("movement_account_selection");
            scoped.searchParams.set("movement_account_type", payload.data.selected_movement_account.type);
            scoped.searchParams.set("movement_account_id", payload.data.selected_movement_account.account_id);
          }
          layoutVerifiedReads.push({ section, url: diagnosticUrl(scoped.href) });
        }
      }
      if (!target.searchParams.has("section") && !target.searchParams.has("cursor")) {
        await rendered(module, selected);
        await verifyMainVisible(payload, module, { allowEmployeeContinuation });
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
    verifiedRequests.add(reply.request());
    verifiedPayloads.set(reply.request(), payload);
    if (retainedRefreshOnly && !retainedPayloads.some(item => item.request === reply.request()))
      retainedPayloads.push({ request: reply.request(), url: diagnosticUrl(reply.url()), payload, contract_verified: true });
    return payload;
  }
  async function verifyMainVisible(payload, module, { allowEmployeeContinuation = false } = {}) {
    const data = module.key === "reports" ? payload : payload.data;
    const amounts = module.key === "brief" ? [
      ["#overview .hero strong", data.position.month_result_fen],
      ["#overview .kpi.funds strong", data.funds_overview.total_fen],
      ["#overview .kpi.asset strong", data.long_term_assets.net_fen],
      ["#overview .kpi.receivable strong", data.open_items.receivable_fen],
      ["#overview .kpi.payable strong", data.open_items.payable_fen],
    ] : module.key === "funds" ? [[".funds-total", data.total_fen]]
      : module.key === "employees" ? [["#employees-cost-value", data.employees.ledger_cost_fen],
        ["#employees-gross-value", sumFen(data.employees.gross_salary_fen, data.employees.annual_bonus_fen)],
        ["#employees-contributions-value", sumFen(data.employees.employer_social_insurance_fen, data.employees.employer_housing_fund_fen)],
        ["#employees-paid-value", data.employees.direct_net_payments_fen],
        ["#employees-outstanding-value", employeeOutstandingFen(data, payload.schema_version)]]
        : module.key === "assets" ? [[".assets-total", data.ledger_net_fen]] : [];
    for (const [selector, amount] of amounts) {
      assert.equal((await page.locator(selector).textContent())?.trim(), formatFen(amount), `${module.key}: full-company amount differs`);
    }
    if (module.key === "brief") {
      await verifyBriefVisible(data);
      const assets = data.long_term_assets;
      assert.equal((await page.locator("#overview .kpi.asset small").textContent())?.trim(),
        `固定 ${assets.fixed_active_count} 项 · 无形 ${assets.intangible_active_count} 项`,
        "brief: active asset counts differ from full summary");
      assert.equal(await page.locator("#overview .kpi").count(), 4, "brief: overview cards missing");
      if (data.focused_voucher) {
        assert.equal(await page.locator(".view-switch button[aria-pressed=true]").textContent(), "按凭证", "brief: exact voucher did not select voucher view");
        assert.equal(await page.locator("#selected-voucher .voucher-reference strong").textContent(), `凭证 ${data.focused_voucher.number}`);
        assert.equal(await page.locator(".voucher-card").count(), 1, "brief: exact voucher read unrelated vouchers");
      } else {
        assert.equal(await page.locator(".view-switch button[aria-pressed=true]").textContent(), "按业务", "brief: default view changed");
        const selectedLabel = (await page.locator("#activity nav[aria-label='业务分类'] button[aria-pressed=true] strong").textContent())?.trim();
        const selectedGroup = selectedLabel === "全部" ? null
          : data.activity_groups.find(group => group.label === selectedLabel);
        assert(selectedLabel === "全部" || selectedGroup, "brief: selected business group missing");
        const expectedItems = data.collections.activity.items.filter(item => !selectedGroup || item.group === selectedGroup.key);
        if (!retainedRefreshOnly) assert.equal(await page.locator("#activity .event-row").count(), expectedItems.length, "brief: selected business rows incomplete");
      }
      assert.equal(await page.locator("#owner-tasks > article:not(.owner-review-request)").count(), data.owner_tasks.length, "brief: owner tasks incomplete");
    } else if (module.key === "funds") {
      if (!retainedRefreshOnly) {
        assert.equal(await page.locator(".account-grid .account-card").count(), data.collections.accounts.items.length, "funds: default accounts incomplete");
        assert.equal(await page.locator(".investment-summary-table tbody tr").count(), data.collections.investment_products.items.length, "funds: default investments incomplete");
      }
      const selected = data.selected_movement_account;
      const current = page.locator(".fund-account-index button[aria-current=true]");
      assert.equal(await current.count(), selected === null ? 0 : 1, "funds: automatic account selection missing");
      if (!retainedRefreshOnly) assert.equal(await page.locator(".book-activity-row").count(), data.collections.movements.items.length, "funds: selected account rows incomplete");
      if (selected) for (const item of data.collections.movements.items) {
        assert.equal(item.account_id, selected.account_id, "funds: another account leaked into movements");
        assert.equal(item.account_type, selected.type, "funds: another account type leaked into movements");
      }
    } else if (module.key === "employees") {
      assert.equal((await page.locator("#employees-count-value").textContent())?.replace(/\s/g, ""),
        `${data.employees.in_period_count}人`, "employees: confirmed in-period headcount differs from full summary");
      const employeeRows = await page.locator(".employee-grid").first().locator(".employee-card").count();
      if (!retainedRefreshOnly) {
        verifyEmployeeRootRows(employeeRows, data.collections.employees.items.length, allowEmployeeContinuation);
        assert.equal(await page.locator("[aria-label='个人劳务明细记录'] .employee-card").count(), data.collections.labor_workers.items.length, "employees: default labor personnel rows incomplete");
      }
    } else if (module.key === "assets") {
      if (!retainedRefreshOnly) {
        assert.equal(await page.locator(".asset-grid .asset-card").count(), data.collections.assets.items.length, "assets: default asset rows incomplete");
        assert.equal(await page.locator(".project-card").count(), data.collections.projects.items.length, "assets: default project rows incomplete");
      }
    } else if (module.key === "reports") {
      const summary = data.summary;
      assert.equal(await page.locator(".summary-grid article").count(), summary === null ? 0 : 4, "reports: default summary incomplete");
      if (summary) {
        const profitRows = data.statements.find(statement => statement.key === "profit_statement")?.rows;
        const revenue = profitRows?.find(row => row.line === 1)?.values.current_fen ?? null;
        const cost = profitRows?.find(row => row.line === 2)?.values.current_fen ?? null;
        for (const [index, value] of [summary.assets_total_fen, revenue, summary.current_net_profit_fen, summary.current_cash_change_fen].entries()) {
          assert.equal((await page.locator(".summary-grid article strong").nth(index).textContent())?.trim(), formatFen(value), "reports: summary amount differs");
        }
        assert.equal((await page.locator(".summary-grid article small").nth(1).textContent())?.trim(), `营业成本 ${formatFen(cost)}`, "reports: operating cost differs from the current-quarter statement");
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
    await waitForStage9Frames(page, `${module.key}: rendered stable frames`);
  }
  async function navigationRendered(module) {
    await rendered(module);
    await page.waitForFunction(() => Number.isFinite(window.__stage9NavigationRenderedAt));
    return page.evaluate(() => ({
      elapsed_ms: window.__stage9NavigationRenderedAt,
      epoch_ms: window.__stage9NavigationRenderedEpochMs,
    }));
  }
  async function measure(module, resourceSamples, requireProjection = Boolean(resourceSamples), options = {}) {
    return recordRefreshAttempt(refreshAttempts, {
      page: module.key, kind: resourceSamples ? "sample" : config.foreground_stream ? "foreground" : "warmup",
      iteration: refreshAttempts.filter(attempt => attempt.page === module.key
        && attempt.kind === (resourceSamples ? "sample" : config.foreground_stream ? "foreground" : "warmup")).length + 1,
    }, async attempt => {
    if (requireProjection) assert(refreshProjections.has(module.key),
      `${module.key}: formal refresh requires a validated default DOM projection`);
    const contextReply = waitForLater(response("context"));
    const mainReply = options.requireRoot ? waitForLater(page.waitForResponse(async reply => {
      const url = dashboardResponseTarget(reply); return matchesAction(url, module.action) && !url.searchParams.has("section") && !url.searchParams.has("cursor") && await readyReceipt(reply);
    })) : waitForLater(response(module.action));
    const refreshButton = page.locator(".module-header button.refresh");
    await refreshButton.evaluate(observeHotRefresh, { action: module.action, key: module.key, companyId: company.id,
      period: company.period, quarter,
      visible: module.visible.split(", "), root: refreshRoots[module.key],
      projection: refreshProjections.get(module.key)?.dom, requireRoot: Boolean(options.requireRoot) });
    attempt.phase = "click";
    await refreshButton.click();
    attempt.phase = "render";
    await page.waitForFunction(() => window.__stage9RenderedAt !== null);
    const measurement = await page.evaluate(captureRefreshMeasurement, {
      action: module.action, companyId: company.id, period: company.period, quarter,
    });
    if (resourceSamples) resourceSamples.push(measurement.resources);
    attempt.elapsed_ms = measurement.measured;
    attempt.start_epoch_ms = measurement.start_epoch_ms;
    attempt.end_epoch_ms = measurement.end_epoch_ms;
    attempt.phase = "validate";
    for (const pending of [contextReply, mainReply]) {
      const reply = await pending;
      const payload = await verifyReply(reply, module);
      if (options.requireRoot) (attempt.verified_responses ??= []).push({ url: diagnosticUrl(reply.url()), snapshot_version: payload.snapshot_version });
    }
    await rendered(module);
    assertNoBrowserFailures();
    return measurement.measured;
    }, () => page.evaluate(() => ({
      started: Number.isFinite(window.__stage9RefreshStart),
      elapsed_ms: Number.isFinite(window.__stage9RefreshStart)
        ? performance.now() - window.__stage9RefreshStart : null,
    })).then(timing => ({
      ...timing,
      started: timing.started || Number.isFinite(refreshAttempts.at(-1)?.elapsed_ms),
      elapsed_ms: refreshAttempts.at(-1)?.elapsed_ms ?? timing.elapsed_ms,
    })), checkpoint);
  }
  async function measureCompanySwitch(target) {
    await navigate(address(modules[0]));
    await rendered(modules[0]);
    const actions = ["context", "brief"];
    const replies = [];
    const capture = reply => {
      const url = dashboardResponseTarget(reply);
      if (actions.some(action => url.pathname === `/api/dashboard/${action}`)
        && url.searchParams.get("company_id") === target.id) replies.push(reply);
    };
    const contextReply = waitForLater(page.waitForResponse(reply => {
      const url = dashboardResponseTarget(reply);
      return url.pathname === "/api/dashboard/context" && url.searchParams.get("company_id") === target.id;
    }));
    const failureStart = apiFailures.length;
    page.on("response", capture);
    try {
    await page.evaluate(selection => {
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
      const start = event => {
        const select = event.target;
        if (!(select instanceof HTMLSelectElement) || select.value !== selection.companyId
          || select.getAttribute("aria-label") !== "切换公司") return;
        document.removeEventListener("change", start, true);
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
      };
      document.addEventListener("change", start, { capture: true });
    }, { companyId: target.id, companyName: target.name });
    await selectDashboardOption(page, "切换公司", target.id);
    const selectedContext = await verifyReply(await contextReply, modules[0], target);
    const expectedPeriod = selectedContext.periods.some(item => item.key === company.period)
      ? company.period : selectedContext.default_period;
    assert(expectedPeriod && selectedContext.periods.some(item => item.key === expectedPeriod),
      "switch: context has no selectable default month");
    await page.waitForFunction(() => Number.isFinite(window.__stage9SwitchRenderedAt));
    const elapsed = await page.evaluate(() => window.__stage9SwitchRenderedAt - window.__stage9SwitchStart);
    const finalReply = action => replies.find(reply => {
      const url = dashboardResponseTarget(reply);
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
    await navigate(ticket.href);
    const firstNavigation = await navigationRendered(modules[0]);
    let firstBriefPayload;
    for (const reply of await Promise.all(firstReplies)) {
      const payload = await verifyReply(reply, modules[0]);
      if (dashboardResponseTarget(reply).pathname === "/api/dashboard/brief") firstBriefPayload = payload;
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
    async function expandActivityGroup(row, snapshot, activate = () => row.locator(".event-copy").click()) {
      const replies = [];
      const listener = reply => { if (dashboardResponseTarget(reply).pathname === "/api/dashboard/brief-group") replies.push(reply); };
      page.on("response", listener);
      try {
        const pending = response("brief-group");
        await activate();
        const first = await pending;
        assert.equal(first.status(), 200, "brief group: required read failed");
        await row.locator(".group-members .member-row").first().waitFor();
        await row.locator(".group-members .dashboard-pagination").waitFor({ state: "hidden" });
        await waitForStage9Frames(page, "brief group: stable frames");
        const payloads = [];
        const groupKey = new URL(first.url()).searchParams.get("group_key");
        for (const reply of replies) {
          assert.equal(reply.status(), 200, "brief group: member continuation failed");
          assert.equal(dashboardResponseTarget(reply).searchParams.get("group_key"), groupKey, "brief group: continuation changed selection");
          payloads.push(verifyBriefGroup(await waitForStage9(() => reply.json(), "brief group: response JSON body"), dashboardResponseTarget(reply), company, snapshot));
        }
        assert(payloads.length && payloads.at(-1).data.collections.members.page.has_more === false,
          "brief group: required member pages incomplete");
        assert.equal(await row.locator("[role=alert]").count(), 0, "brief group: member read failed");
        assertNoBrowserFailures();
        return payloads;
      } finally { page.off("response", listener); }
    }
    if (config.activity_interactions) {
      const requests = [];
      page.on("request", request => {
        const url = dashboardRequestTarget(request);
        if (url.pathname.startsWith("/api/dashboard/")) requests.push(url);
      });
      const businessRows = page.locator("#activity .event-row");
      assert(await businessRows.count(), "activity: no business row to inspect");
      const row = businessRows.first();
      const groupPayloads = await expandActivityGroup(row, firstBriefPayload.snapshot_version);
      requests.length = 0;
      const button = row.locator(".event-voucher-button:not(:disabled)").first();
      const number = (await button.textContent()).trim().match(/^凭证 ([1-9][0-9]*)$/)?.[1];
      assert(number, "activity: public voucher number missing");
      const voucher = groupPayloads.flatMap(payload => payload.data.collections.vouchers.items).find(item => item.number === number);
      assert(voucher, "activity: displayed voucher is outside the authenticated page");
      const tooltip = page.locator("#activity [role=tooltip]");
      assert.equal(await tooltip.count(), 0, "activity: hidden voucher details eagerly mounted");
      assert.equal(await row.locator(".business-detail-trigger:visible").count(), 0, "activity: duplicate business detail entry");
      await button.hover();
      await tooltip.waitFor({ state: "visible" });
      assert.equal(await tooltip.count(), 1, "activity: multiple voucher popovers mounted");
      await waitForStage9Frames(page, "brief voucher tooltip: stable frames");
      const previewBounds = await tooltip.boundingBox();
      assert(previewBounds && previewBounds.y >= 11 && previewBounds.y + previewBounds.height <= page.viewportSize().height - 11,
        "activity: voucher popover is clipped by the viewport");
      assert.equal((await tooltip.locator(".heading strong").textContent()).trim(), voucher.list_summary);
      assert.equal((await tooltip.locator(".amount b").textContent()).trim(), voucher.business_amount_state === "not_applicable" ? "—" : voucher.business_amount_state === "unknown" ? "待核实" : formatFen(voucher.business_amount_fen));
      const lines = tooltip.locator(".lines > span");
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
      await expandActivityGroup(row, firstBriefPayload.snapshot_version, () => page.getByRole("button", { name: "按业务", exact: true }).click());
      const businessReply = page.waitForResponse(reply => dashboardResponseTarget(reply).pathname === "/api/dashboard/business-status");
      await row.getByRole("button", { name: "业务进展", exact: true }).first().click();
      const reply = await businessReply;
      assert.equal(reply.status(), 200);
      const payload = await verifyReply(reply, modules.find(module => module.key === "brief"));
      const target = dashboardResponseTarget(reply);
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
      await expandActivityGroup(row, firstBriefPayload.snapshot_version, () => businessRows.first().press("Enter"));
      assert.equal(await businessRows.first().getAttribute("aria-expanded"), "true");
      await businessRows.first().press("Space");
      assert.equal(await businessRows.first().getAttribute("aria-expanded"), "false");
      assert(requests.length && requests.every(url => ["/api/dashboard/business-status", "/api/dashboard/brief-group"].includes(url.pathname)),
        "activity: row expansion reloaded unrelated page data");
      assert.equal(await page.locator(".module-header").getAttribute("aria-busy"), "false");
      await page.setViewportSize({ width: 375, height: 812 });
      await expandActivityGroup(row, firstBriefPayload.snapshot_version);
      await row.locator(".event-voucher-button:not(:disabled)").first().click();
      await page.waitForFunction(() => document.querySelector(".view-switch button[aria-pressed=true]")?.textContent.trim() === "按凭证");
      assert.equal(await page.locator("#activity [role=tooltip]:visible").count(), 0);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "activity: mobile horizontal overflow");
      if (config.screenshot_directory) await page.screenshot({ path: path.join(config.screenshot_directory, "activity-voucher-mobile.png") });
      assertNoBrowserFailures();
      return { status: "passed", activity_interactions: { hover: true, focus: true, local_voucher: true,
        row_toggle: true, keyboard: true, mobile: true, requests: requests.length }, navigation: navigationBase };
    }
    if (config.layout_only || config.interaction_only) {
      const layouts = layoutChecks;
      async function settleRequiredReads(module) {
        const deadline = Date.now() + 30000;
        const wait = (phase, operation) => waitForStage9(operation, `${module.key}: settle ${phase}`, deadline);
        while (true) {
          await waitForStage9Frames(page, `${module.key}: settle stable frames`, deadline);
          assertNoBrowserFailures();
          assert.equal(await wait("alerts", () => page.locator("[role=alert]").count()), 0, `${module.key}: final required read failed`);
          while (layoutPageReplies.length) {
            const reply = layoutPageReplies.shift();
            const pathname = dashboardResponseTarget(reply).pathname;
            if (config.interaction_only && !retainedRefreshOnly && pathname !== `/api/dashboard/${module.action}`
              && !["/api/dashboard/brief-group", "/api/dashboard/business-status"].includes(pathname)) continue;
            const owner = modules.find(item => pathname === `/api/dashboard/${item.action}`) ?? module;
            const failure = apiFailures.find(item => item.request?.request_id === requestDiagnostics.get(reply.request())?.request_id);
            if (failure && allowedLocalAborts.has(failure)) {
              if (retainedRefreshOnly) retainedBodies.consumed(reply);
              continue;
            }
            if ((config.layout_only || retainedRefreshOnly) && await localReplyIsObsolete(reply, {
              failureFor: response => apiFailures.find(item => item.request?.request_id === requestDiagnostics.get(response.request())?.request_id),
              classifyObsolete: item => allowedLocalAborts.has(item),
              finish: response => wait("response body finish", () => response.finished()),
            })) { if (retainedRefreshOnly) retainedBodies.consumed(reply); continue; }
            await wait("response validation", () => verifyReply(reply, owner));
          }
          const pending = requestDiagnostics.pending().filter(read => retainedRefreshOnly || !config.interaction_only
            || ["/api/dashboard/context", `/api/dashboard/${module.action}`, "/api/dashboard/brief-group", "/api/dashboard/business-status"].includes(new URL(read.url).pathname));
          if (!pending.length) {
            if (retainedRefreshOnly && retainedCaptureActive) await verifyRetainedVisibleCollections(module);
            return;
          }
          assert(Date.now() < deadline, `${module.key}: necessary API requests did not finish`);
        }
      }
      async function verifyRetainedVisibleCollections(module) {
        const pathname = `/api/dashboard/${module.action}`;
        const main = retainedPayloads.findLast(read => new URL(read.url).pathname === pathname && !new URL(read.url).searchParams.has("section"));
        assert(main, `${module.key}: complete DOM validation lacks the current verified root`);
        const check = async (section, selector, filter = () => true) => {
          const items = retainedOrderedItems(retainedPayloads, pathname, section).filter(filter);
          const ids = items.map(item => retainedItemIdentity(pathname, section, item, main.payload));
          verifyRetainedVisibleOrder(ids, await page.locator(selector).count(), `${module.key}/${section}`);
        };
        if (module.key === "brief") {
          const index = await page.evaluate(() => Array.from(document.querySelectorAll('#activity .index button'))
            .findIndex(node => node.getAttribute('aria-pressed') === 'true'));
          const group = main.payload.data.activity_groups[index];
          assert(group, "brief: selected activity category lacks current response identity");
          await check("activity", "#activity .event-row", item => item.group === group.key);
          const openIndex = await page.evaluate(() => Array.from(document.querySelectorAll('#open-items .open-index button'))
            .findIndex(node => node.getAttribute('aria-current') === 'true'));
          const category = main.payload.data.open_items.categories.filter(item => item.count)[openIndex];
          if (category) await check("open_items", "#open-items .open-event-row", item => item.category_key === category.key);
          else assert.equal(await page.locator("#open-items .open-event-row").count(), 0, "brief: rows without selected open category");
        } else if (module.key === "funds") {
          for (const [section, selector] of [["accounts", ".account-grid .account-card"], ["investment_products", ".investment-summary-table tbody tr"],
            ["movements", ".book-activity-row"], ["statements", ".bank-activity-item"]]) await check(section, selector);
        } else if (module.key === "employees") {
          await check("employees", '[aria-label="员工明细记录"] .employee-card');
          await check("labor_workers", '[aria-label="个人劳务明细记录"] .employee-card');
        } else if (module.key === "assets") {
          await check("assets", ".asset-grid .asset-card"); await check("projects", ".project-card");
        }
      }
      // These interaction checks are outside every default/cold/refresh timing.
      async function localChange(module, action, section = null, label = "display", expectedFilter = null) {
        const requests = [], replies = [];
        const actionId = localActions.length + 1;
        const deadline = Date.now() + 30000;
        const wait = (phase, operation) => waitForStage9(operation, `${module.key}/action ${actionId} ${label}: ${phase}`, deadline);
        const readSelection = async () => {
          const selection = await page.evaluate(() => ({
          url: location.href,
          funds_view: Array.from(document.querySelectorAll('#fund-detail-tab-bank, #fund-detail-tab-book')).find(node => node.getAttribute('aria-selected') === 'true')?.id ?? null,
          movement_account_index: Array.from(document.querySelectorAll('.fund-account-index > button')).findIndex(node => node.getAttribute('aria-current') === 'true'),
          active_section: document.querySelector('.section-nav [aria-current="location"]')?.textContent.trim() ?? null,
          brief_view: document.querySelector('.view-switch button[aria-pressed="true"]')?.textContent.trim() ?? null,
          filters: Array.from(document.querySelectorAll('.select-trigger')).map(node => ({ label: node.getAttribute('aria-label'), value: node.getAttribute('data-value') })),
          statement_account_id: document.querySelector('[aria-label="筛选银行流水账户"].select-trigger')?.getAttribute('data-value') ?? '',
        }));
          const accounts = layoutAccounts.map(item => ({ movement_account_type: item.type, movement_account_id: item.account_id }));
          if (layoutMovementAccount && !accounts.some(item => item.movement_account_type === layoutMovementAccount.movement_account_type
            && item.movement_account_id === layoutMovementAccount.movement_account_id)) accounts.unshift(layoutMovementAccount);
          selection.movement_account = accounts[selection.movement_account_index] ?? null;
          return selection;
        };
        const before = await wait("selection before", readSelection); before.url = diagnosticUrl(before.url);
        const diagnostic = { action_id: actionId, page: module.key, section, label,
          before, pending_before: requestDiagnostics.pending(), verified_reads: [], verified_cached_reads: [], status: "running" };
        localActions.push(diagnostic); currentLocalAction = actionId;
        const requestListener = request => {
          const url = dashboardRequestTarget(request);
          if (url.pathname.startsWith("/api/dashboard/")) requests.push(url);
        };
        const replyListener = reply => {
          if (dashboardResponseTarget(reply).pathname === `/api/dashboard/${module.action}`) replies.push({ reply });
        };
        await wait("install observer", () => page.evaluate(() => {
          const header = document.querySelector(".module-header");
          window.__stage9LocalRoot = header;
          window.__stage9LocalFailures = [];
          const observeLocal = () => {
            if (!header.isConnected || header.getAttribute("aria-busy") !== "false") window.__stage9LocalFailures.push("whole-page loading");
          };
          window.__stage9LocalObserver = new MutationObserver(window.__stage9VoucherTailDiagnosticEnabled ? records => {
            const metric = window.__stage9Interaction?.end === null ? window.__stage9VoucherTailMetrics?.observers.local : null, started = performance.now();
            try { observeLocal(); }
            finally { if (metric) { metric.callbacks++; metric.records += records.length;
              metric.added_nodes += records.reduce((count, record) => count + record.addedNodes.length, 0);
              metric.milliseconds += performance.now() - started; } }
          } : observeLocal);
          window.__stage9LocalObserver.observe(document.body, { subtree: true, attributes: true, childList: true });
        }));
        page.on("request", requestListener); page.on("response", replyListener);
        try {
          await wait("perform action", action);
          await wait("interaction candidate", waitForInteractionCandidate);
          diagnostic.after = await wait("selection after action", readSelection); diagnostic.after.url = diagnosticUrl(diagnostic.after.url);
          const snapshot = refreshProjections.get(module.key)?.snapshot;
          const fresh = expectedFilter !== null || section === "movements" && before.movement_account_index !== diagnostic.after.movement_account_index
            || section === "vouchers" && before.brief_view !== diagnostic.after.brief_view;
          const currentRead = (entry, required = false) => localReadMatchesSelection(requestDiagnostics.get(entry.reply.request()), diagnostic, snapshot,
            required ? section : null, { fresh: required && fresh });
          if (section && !replies.some(entry => currentRead(entry, true))) await wait("current selected response", () => page.waitForResponse(reply => currentRead({ reply }, true)));
          await wait("local loading", () => page.waitForFunction(() => !Array.from(document.querySelectorAll("[role=status], .empty"))
            .some(node => node.getClientRects().length && /正在读取所选|正在读取资金明细|正在读取银行流水|正在读取凭证/.test(node.textContent ?? ""))));
          await waitForStage9Frames(page, `${module.key}/action ${actionId} ${label}: stable frames`, deadline);
          diagnostic.after = await wait("settled selection", readSelection); diagnostic.after.url = diagnosticUrl(diagnostic.after.url);
          for (const url of requests) {
            verifyLocalRead(url, module, company, refreshProjections.get(module.key)?.snapshot, section);
          }
          // A view change can reuse a completely validated collection, after
          // the actual selected UI has settled. Specific filters/accounts are
          // matched too, so an old cache cannot prove a changed selection.
          diagnostic.verified_cached_reads = layoutVerifiedReads.filter(read => localReadMatchesSelection(read, diagnostic, snapshot, null, { cached: true }))
            .map(read => ({ ...read, action_id: actionId }));
          const classifyObsolete = failure => {
            if (allowedLocalAborts.has(failure)) return true;
            for (const candidate of [diagnostic, ...localActions.filter(item => item.status === "passed").reverse()]) {
              const reason = obsoleteLocalCancellation(failure, candidate, refreshProjections.get(candidate.page)?.snapshot, localActions);
              if (reason) {
                allowedLocalAborts.add(failure); obsoleteCancellations.push({ ...failure, reason, superseded_by_action: candidate.action_id });
                return true;
              }
            }
            return false;
          };
          const payloads = await wait("response validation", () => verifyLocalActionReplies(replies, {
            isCurrent: entry => currentRead(entry),
            isObsolete: ({ reply }) => localReplyIsObsolete(reply, {
              failureFor: response => apiFailures.find(item => item.request?.request_id === requestDiagnostics.get(response.request())?.request_id),
              classifyObsolete,
              finish: response => wait("response body finish", () => response.finished()),
            }),
            verify: ({ reply }) => verifyReply(reply, module),
            onCurrent: async ({ reply }, payload) => {
              verifiedRequestRecord(requestDiagnostics, verifiedRequests, reply.request(), `${module.key}: local response`);
              const url = dashboardResponseTarget(reply);
              const readSection = url.searchParams.get("section");
              if (module.key === "funds" && readSection === "movements") {
                assert.equal(payload.data?.selected_movement_account?.account_id, url.searchParams.get("movement_account_id"), "local account response: wrong selected account");
                assert.equal(payload.data?.selected_movement_account?.type, url.searchParams.get("movement_account_type"), "local account response: wrong selected type");
              }
              if (module.key === "employees" && ["employees", "labor_workers"].includes(readSection)) assert.equal(payload.data?.employee_filter, url.searchParams.get("employee_filter"), "local employee response: wrong selected filter");
              if (expectedFilter !== null && (readSection === section || module.key === "employees" && readSection === "labor_workers")) verifySelectedFilter(diagnostic.after, url, payload, readSection, expectedFilter);
              diagnostic.verified_reads.push({ action_id: actionId, section: readSection, url: diagnosticUrl(reply.url()) });
            },
          }));
          const requiredSections = module.key === "employees" && section === "employees" && expectedFilter !== null
            ? ["employees", "labor_workers"] : section ? [section] : [];
          for (const requiredSection of requiredSections) assert(diagnostic.verified_reads.some(read => localReadMatchesSelection({ ...read, started_action: actionId }, diagnostic, snapshot, requiredSection, { fresh })),
            `${module.key}/${requiredSection}: current selected response missing or incomplete`);
          diagnostic.after = await wait("final selection", readSelection); diagnostic.after.url = diagnosticUrl(diagnostic.after.url);
          assert.deepEqual(await wait("observer failures", () => page.evaluate(() => window.__stage9LocalFailures)), [], `${module.key}: local change flashed whole page`);
          assert.equal(await wait("header state", () => page.locator(".module-header").getAttribute("aria-busy")), "false");
          assert.equal(await wait("final alerts", () => page.locator("[role=alert]").count()), 0, `${module.key}: local read failed`);
          // Include newly validated complete bodies in the final cache proof.
          diagnostic.verified_cached_reads = layoutVerifiedReads.filter(read => localReadMatchesSelection(read, diagnostic, snapshot, null, { cached: true }))
            .map(read => ({ ...read, action_id: actionId }));
          for (const failure of apiFailures) {
            if (allowedLocalAborts.has(failure)) continue;
            classifyObsolete(failure);
          }
          assertNoBrowserFailures();
          diagnostic.status = "passed";
          return payloads;
        } catch (error) {
          diagnostic.status = "failed";
          diagnostic.error = sanitize(error?.message || error);
          throw error;
        } finally {
          diagnostic.pending_after = requestDiagnostics.pending();
          currentLocalAction = null;
          page.off("request", requestListener); page.off("response", replyListener);
          await waitForStage9(() => page.evaluate(() => window.__stage9LocalObserver?.disconnect()),
            `${module.key}/action ${actionId} ${label}: observer cleanup`, Date.now() + 1000).catch(error => {
            diagnostic.cleanup_error = sanitize(error.message);
            if (diagnostic.status !== "failed") throw error;
          });
        }
      }
      async function checkOwnerLayout(module, scenario) {
        const layout = await page.evaluate(() => {
          const visible = element => element.getClientRects().length
            && getComputedStyle(element).visibility !== "hidden";
          const textBounds = element => {
            const range = document.createRange();
            range.selectNodeContents(element);
            return Array.from(range.getClientRects()).map(rect => {
              let left = rect.left, right = rect.right;
              // Full report sheets intentionally scroll horizontally. Only
              // the part exposed by their scroll viewport is on the screen.
              for (let parent = element.parentElement; parent; parent = parent.parentElement) {
                if (innerWidth > 760 && parent.matches(".template-wrap")
                  && ["auto", "scroll"].includes(getComputedStyle(parent).overflowX)) {
                  const box = parent.getBoundingClientRect();
                  const start = box.left + parent.clientLeft;
                  left = Math.max(left, start); right = Math.min(right, start + parent.clientWidth);
                }
              }
              return { left, right, top: rect.top, bottom: rect.bottom };
            }).filter(rect => rect.right > rect.left);
          };
          const currencyOverflow = Array.from(document.querySelectorAll("strong, dd, td, .component-value-trigger, .balance-trigger, .total-help-trigger"))
            .filter(element => visible(element) && /[¥￥]/.test(element.textContent ?? ""))
            .flatMap(element => textBounds(element)
              .filter(rect => rect.left < -1 || rect.right > innerWidth + 1)
              .map(rect => ({ label: element.textContent, left: rect.left, right: rect.right })));
          const templateViewportOverflow = Array.from(document.querySelectorAll(".template-wrap"))
            .filter(visible).map(element => element.getBoundingClientRect())
            .filter(rect => rect.left < -1 || rect.right > innerWidth + 1)
            .map(rect => ({ left: rect.left, right: rect.right }));
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
            currency_overflow: currencyOverflow, template_viewport_overflow: templateViewportOverflow,
            hero_overlap: heroOverlap,
            mobile_funds_gaps: mobileFundsGaps,
          };
        });
        layouts.push({ page: module.key, scenario, ...layout });
        assert(layout.width <= layout.viewport + 1,
          `${module.key}/${scenario}: horizontal overflow at ${layout.viewport}px (${layout.width}px)`);
        assert.deepEqual(layout.technical, [], `${module.key}/${scenario}: technical content remains`);
        assert.deepEqual(layout.currency_overflow, [], `${module.key}/${scenario}: amount overflows the screen`);
        assert.deepEqual(layout.template_viewport_overflow, [], `${module.key}/${scenario}: template viewport overflows the screen`);
        assert.deepEqual(layout.hero_overlap, [], `${module.key}/${scenario}: hero amount overlaps another section`);
        if (layout.mobile_funds_gaps) {
          assert(layout.mobile_funds_gaps.before_rows <= 100,
            `${module.key}/${scenario}: excessive blank space before the movements`);
          assert(layout.mobile_funds_gaps.after_rows <= 60,
            `${module.key}/${scenario}: excessive blank space after the movements`);
        }
      }
      if (config.interaction_only) {
        retainedCaptureActive = retainedRefreshOnly;
        let previousRetainedModule = modules[0];
        const specs = [];
        async function openModule(module, query = {}) {
          if (retainedRefreshOnly) {
            await settleRequiredReads(previousRetainedModule);
            retainedBodies.retire(retainedGeneration);
            assert(!requestDiagnostics.pending().length, "retained response: navigation has unfinished prior requests");
            retainedGeneration += 1; previousRetainedModule = module;
          }
          retainedPayloads.length = 0;
          layoutVerifiedReads.length = 0; layoutMovementAccount = null; layoutAccounts.length = 0;
          const pending = [response("context"), response(module.action)];
          const destination = new URL(address(module));
          for (const [key, value] of Object.entries(query)) destination.searchParams.set(key, value);
          await navigate(destination.href);
          let main;
          for (const reply of await Promise.all(pending)) {
            const payload = await verifyReply(reply, module);
            if (dashboardResponseTarget(reply).pathname === `/api/dashboard/${module.action}`) main = payload;
          }
          await rendered(module); await settleRequiredReads(module);
          return main;
        }
        async function retainedState(spec) {
          const projection = await page.evaluate(captureRefreshProjection, { root: spec.root });
          return page.evaluate(({ source, selection }) => new Function("document", "selection", `return (${source})(document, selection)`)(document, selection),
            { source: readRetainedRefreshState.toString(), selection: { root: spec.root, rows: spec.rows, projection, behavior: spec.expected_refresh_behavior } });
        }
        async function targetBinding(spec, reads) {
          const section = spec.module.key === "brief" ? spec.category_section : spec.module.key === "funds" ? "movements" : spec.module.key;
          let items = retainedOrderedItems(reads, `/api/dashboard/${spec.module.action}`, section);
          if (spec.module.key === "brief") items = items.filter(item => (section === "activity" ? item.group : item.category_key) === spec.category_key);
          if (spec.module.key === "reports") items = reads.findLast(read => read.payload.statements)?.payload.statements ?? [];
          const selector = spec.module.key === "reports" ? ".statement-buttons > button" : spec.rows;
          const open_indices = await page.evaluate(selector => Array.from(document.querySelectorAll(selector))
            .flatMap((node, index) => node.open || node.getAttribute("aria-expanded") === "true" || node.querySelector("details[open]") ? [index] : []), selector);
          const ordered_ids = items.map(item => spec.module.key === "reports" ? item.key : retainedItemIdentity(`/api/dashboard/${spec.module.action}`, section, item, { data: {} }));
          assert.equal(await page.locator(selector).count(), ordered_ids.length, `${spec.key}: response order cannot be bound to actual rendered row positions`);
          const main = reads.findLast(read => new URL(read.url).pathname === `/api/dashboard/${spec.module.action}`);
          const item = items[open_indices[0]];
          return { ordered_ids, open_indices, target_id: item ? ordered_ids[open_indices[0]] : spec.target_identity,
            subject_id: item?.subject_id ?? null,
            snapshot: main?.payload.snapshot_version ?? main?.payload.read_context?.read_version,
            ...(spec.module.key === "funds" ? { account: main?.payload.data.selected_movement_account } : {}),
          };
        }
        async function verifyTarget(spec, reads) {
          if (!spec.requiresTarget && spec.key !== "employees_expanded_detail_refresh") return;
          const after = await targetBinding(spec, reads);
          verifyRetainedTargetBinding(spec.beforeTargetBinding, after, spec.expected_refresh_behavior === "close_activity_members_retain_category");
          spec.afterTargetBinding = after;
        }
        async function rememberRetained(spec) {
          spec.beforeRefresh = await retainedState(spec);
          assert(spec.beforeRefresh.rows.length, `${spec.key}: no real visible records`);
          spec.beforeResponseIdentities = retainedPayloads.map(({ url, payload }) => ({ url,
            snapshot_version: payload.snapshot_version ?? payload.read_context?.read_version,
            identities: Object.fromEntries(Object.entries(payload.data?.collections ?? {}).map(([section, collection]) =>
              [section, collection.items.map(item => retainedItemIdentity(new URL(url).pathname, section, item, payload))])),
          }));
          spec.beforeCollectionIdentities = retainedCollectionIdentities(retainedPayloads);
          spec.beforeAuxiliaryComplete = verifyRetainedAuxiliaryComplete(retainedPayloads);
          spec.beforeVerifiedReads = [...retainedPayloads];
          if (retainedRefreshOnly && (spec.requiresTarget || spec.key === "employees_expanded_detail_refresh")) {
            spec.beforeTargetBinding = await targetBinding(spec, retainedPayloads);
            assert(spec.beforeTargetBinding.open_indices.length === 1, `${spec.key}: no unique actual expanded target`);
            spec.target_identity = spec.beforeTargetBinding.target_id;
          }
          spec.refreshReadOffset = retainedPayloads.length;
        }
        async function retainedRefresh(spec) {
          spec.refreshEvidence = { status: "pending", verified_responses: [] };
          const replies = [response("context"), waitForLater(page.waitForResponse(async reply => {
            const url = dashboardResponseTarget(reply); return matchesAction(url, spec.module.action) && !url.searchParams.has("section") && !url.searchParams.has("cursor") && await readyReceipt(reply);
          }))];
          await page.locator('.module-header button.refresh').click();
          for (const reply of await Promise.all(replies)) {
            const payload = await verifyReply(reply, spec.module, company, { allowEmployeeContinuation: true });
            spec.refreshEvidence.verified_responses.push({ url: diagnosticUrl(reply.url()), snapshot_version: payload.snapshot_version ?? payload.read_context?.read_version });
          }
          await settleRequiredReads(spec.module);
          const fresh = retainedPayloads.slice(spec.refreshReadOffset);
          await verifyTarget(spec, fresh);
          let effective = fresh;
          if (spec.key === "funds_book_retained_detail_refresh" && !fresh.some(read => new URL(read.url).pathname === "/api/dashboard/business-status")) {
            const main = fresh.findLast(read => new URL(read.url).pathname === "/api/dashboard/funds" && !new URL(read.url).searchParams.has("section"));
            const cached = verifiedFundsDetailCache(spec.beforeVerifiedReads, main, spec.afterTargetBinding);
            effective = [...fresh, ...cached];
            spec.refreshEvidence.detail_cache = { status: "verified_same_snapshot_same_subject_account_and_open_row", main_url: main.url,
              snapshot_version: main.payload.snapshot_version, target: spec.afterTargetBinding, prior_verified_urls: cached.map(read => read.url) };
          }
          spec.afterAuxiliaryComplete = verifyRetainedAuxiliaryComplete(effective);
          spec.refreshEvidence.auxiliary_complete = spec.afterAuxiliaryComplete;
          spec.refreshEvidence.target_binding = spec.afterTargetBinding;
          spec.afterCollectionIdentities = retainedCollectionIdentities(effective);
          verifyRetainedCollectionIdentities(spec.beforeCollectionIdentities, spec.afterCollectionIdentities,
            { closeActivityMembers: spec.expected_refresh_behavior === "close_activity_members_retain_category" });
          spec.refreshEvidence.status = "verified";
        }
        async function change(module, operation, section = null, label = "interaction", filter = null) {
          const payloads = await localChange(module, operation, section, label, filter);
          await settleRequiredReads(module);
          return payloads;
        }
        async function detailChange(module, operation, visible) {
          const replies = [], collect = reply => {
            const target = dashboardResponseTarget(reply);
            if ([`/api/dashboard/${module.action}`, "/api/dashboard/brief-group", "/api/dashboard/business-status"].includes(target.pathname)) replies.push(reply);
          };
          page.on("response", collect);
          try {
            await operation(); await page.locator(visible).first().waitFor({ state: "visible" });
            await settleRequiredReads(module);
            const reads = [];
            for (const reply of replies) {
              const payload = verifiedPayloads.get(reply.request()) ?? await verifyReply(reply, module);
              reads.push({ url: dashboardResponseTarget(reply).href, payload });
            }
            assert(reads.length, "first-open detail has no actual verified response");
            verifyRetainedAuxiliaryComplete(reads);
            return reads;
          } finally { page.off("response", collect); }
        }
        async function selectFilter(module, value) {
          const label = module.key === "employees" ? "筛选人员" : "筛选资产";
          if (await page.getByRole("button", { name: label, exact: true }).getAttribute("data-value") !== value)
            await change(module, () => selectDashboardOption(page, label, value), module.key, `prepare ${value}`, value);
        }
        for (const module of modules.filter(item => ["employees", "assets"].includes(item.key))) {
          const values = module.key === "employees" ? ["in_period", "all"] : ["fixed", "all"];
          for (const value of values) specs.push({ key: `${module.key}_filter_${value}`, module, root: refreshRoots[module.key], requireNewResponse: true,
            trigger: `[role=option][data-value=${JSON.stringify(value)}]`, counts: [],
            prepare: async () => { await openModule(module); await selectFilter(module, values.find(item => item !== value)); return true; },
            perform: () => change(module, () => selectDashboardOption(page, module.key === "employees" ? "筛选人员" : "筛选资产", value), module.key, `timed ${value}`, value),
            verify: async () => assert.equal(await page.getByRole("button", { name: module.key === "employees" ? "筛选人员" : "筛选资产", exact: true }).getAttribute("data-value"), value),
          });
          let identity;
          specs.push({ key: `${module.key}_exact_target`, module, root: refreshRoots[module.key], navigation: true, requireNewResponse: true,
            counts: [[module.key === "employees" ? "#employee-card-target" : "#asset-card-target", 1]],
            position: { selector: module.key === "employees" ? "#employee-card-target" : "#asset-card-target", require_focus: true },
            prepare: async function () {
              const main = await openModule(module, module.key === "employees" ? { employee_filter: "all" } : {}), field = module.key === "employees" ? "employee_id" : "asset_id";
              const item = main.data.collections[module.key].items.find(item => item[field]);
              if (!item) return false;
              identity ??= item[field];
              const target = new URL(address(module)); target.searchParams.set(field, identity); this.url = target.href;
              if (module.key === "employees") { target.searchParams.set("employee_filter", "all"); this.url = target.href; }
              return true;
            },
            perform: async function () {
              const field = module.key === "employees" ? "employee_id" : "asset_id", requests = [];
              const collect = request => { if (dashboardRequestTarget(request).pathname.startsWith("/api/dashboard/")) requests.push(dashboardRequestTarget(request)); };
              const pending = [response("context"), waitForLater(page.waitForResponse(reply => dashboardResponseTarget(reply).pathname === `/api/dashboard/${module.action}` && dashboardResponseTarget(reply).searchParams.get(field) === identity))];
              page.on("request", collect);
              try {
                await navigate(this.url);
                for (const reply of await Promise.all(pending)) {
                  const payload = await verifyReply(reply, module);
                  if (dashboardResponseTarget(reply).pathname === `/api/dashboard/${module.action}`) verifyExactInteractionTarget(payload, dashboardResponseTarget(reply), company, module.key, identity);
                }
                await rendered(module);
                await page.locator(this.counts[0][0]).waitFor({ state: "visible" });
                await settleRequiredReads(module);
                for (const url of requests) {
                  assert.equal(url.searchParams.get("company_id"), company.id);
                  if (url.pathname === "/api/dashboard/context") continue;
                  assert.equal(url.pathname, `/api/dashboard/${module.action}`, "exact target read unrelated resource");
                  assert.equal(url.searchParams.get(field), identity);
                  assert(!url.searchParams.has("cursor") && !url.searchParams.has("section"), "exact target searched continuation pages");
                }
              } finally { page.off("request", collect); }
            }, verify: async () => {},
          });
        }
        const funds = modules.find(item => item.key === "funds"), brief = modules[0], employees = modules.find(item => item.key === "employees");
        for (const value of ["bank", "book"]) specs.push({ key: `funds_view_${value}`, module: funds, root: "#bank-details", counts: [], trigger: `#fund-detail-tab-${value}`,
          prepare: async () => { await openModule(funds); const other = value === "bank" ? "book" : "bank"; if (await page.locator(`#fund-detail-tab-${other}`).getAttribute("aria-selected") !== "true") await change(funds, () => page.locator(`#fund-detail-tab-${other}`).click()); return true; },
          perform: () => change(funds, () => page.locator(`#fund-detail-tab-${value}`).click()),
          verify: async () => assert.equal(await page.locator(`#fund-detail-tab-${value}`).getAttribute("aria-selected"), "true"),
        });
        for (const index of [1, 0]) specs.push({ key: `funds_account_${index + 1}`, module: funds, root: "#bank-details", requireNewResponse: true, counts: [], trigger: `.fund-account-index > button:nth-of-type(${index + 1})`,
          prepare: async () => {
            await openModule(funds); const buttons = page.locator(".fund-account-index > button"); if (await buttons.count() < 2) return false;
            const other = index === 0 ? 1 : 0; if (await buttons.nth(other).getAttribute("aria-current") !== "true") await change(funds, () => buttons.nth(other).click(), "movements"); return true;
          }, perform: () => change(funds, () => page.locator(".fund-account-index > button").nth(index).click(), "movements"),
          verify: async () => assert.equal(await page.locator(".fund-account-index > button").nth(index).getAttribute("aria-current"), "true"),
        });
        specs.push({ key: "brief_voucher_exact_target", module: brief, root: "#activity", navigation: true, requireNewResponse: true,
          counts: [["#selected-voucher .voucher-inline-detail", 1]], position: { selector: "#selected-voucher", require_focus: false },
          prepare: async function () {
            await openModule(brief);
            const payloads = await change(brief, () => page.getByRole("button", { name: "按凭证", exact: true }).click(), "vouchers");
            this.voucher = payloads.find(payload => payload.data.collections.vouchers)?.data.collections.vouchers.items[0];
            assert(this.voucher, "brief: no real voucher for exact navigation");
            const target = new URL(address(brief)); target.searchParams.set("voucher", this.voucher.voucher_version_id); this.url = target.href;
            return true;
          }, perform: async function () {
            const observed = [], collect = request => { if (dashboardRequestTarget(request).pathname.startsWith("/api/dashboard/")) observed.push(dashboardRequestTarget(request)); };
            const pending = [response("context"), response("brief")];
            page.on("request", collect);
            try {
              await navigate(this.url);
              for (const reply of await Promise.all(pending)) {
                const payload = await verifyReply(reply, brief);
                if (dashboardResponseTarget(reply).pathname === "/api/dashboard/brief") {
                  assert.equal(payload.data.focused_voucher?.voucher_version_id, this.voucher.voucher_version_id);
                  assert.deepEqual(payload.data.focused_voucher, this.voucher);
                }
              }
              await page.locator("#selected-voucher .voucher-inline-detail").waitFor({ state: "visible" });
              await settleRequiredReads(brief);
              for (const target of observed) {
                assert.equal(target.searchParams.get("company_id"), company.id);
                if (target.pathname === "/api/dashboard/context") continue;
                assert.equal(target.pathname, "/api/dashboard/brief");
                assert.equal(target.searchParams.get("voucher_version_id"), this.voucher.voucher_version_id);
                assert(!target.searchParams.has("section") && !target.searchParams.has("cursor"), "exact voucher searched pages");
              }
            } finally { page.off("request", collect); }
          }, verify: async function () {
            const rows = await page.locator("#selected-voucher .voucher-inline-detail tbody tr").evaluateAll(nodes => nodes.map(node => ({
              code: node.querySelector("td[data-label='科目'] small")?.textContent.trim(),
              account: node.querySelector("td[data-label='科目'] strong")?.textContent.trim(),
              debit: node.querySelector("td[data-label='借方']")?.textContent.trim(),
              credit: node.querySelector("td[data-label='贷方']")?.textContent.trim(),
            })));
            verifyVisibleVoucherRows(rows, this.voucher);
          },
        });
        for (const section of ["activity", "open_items"]) {
          const root = section === "activity" ? "#activity" : "#open-items", buttons = section === "activity" ? `${root} .index button` : `${root} .open-index button`;
          let expectedCount;
          specs.push({ key: `brief_${section}_selected_category_complete`, module: brief, root, counts: [], trigger: `${buttons}:nth-of-type(2)`,
            prepare: async function () {
              const main = await openModule(brief);
              const categories = section === "activity" ? main.data.activity_groups : main.data.open_items.categories.filter(item => item.count);
              if (categories.length < 2 || await page.locator(buttons).count() < 2) return false;
              expectedCount = categories[1].group_count;
              this.counts = [[section === "activity" ? "#activity .event-row" : "#open-items .open-event-row", expectedCount]];
              await change(brief, () => page.locator(buttons).first().click()); return true;
            }, perform: () => change(brief, () => page.locator(buttons).nth(1).click()),
            verify: async function () { assert.equal(await page.locator(this.counts[0][0]).count(), expectedCount, "selected category did not load its complete group count"); },
          });
        }
        specs.push({ key: "employees_expanded_detail_refresh", module: employees, root: refreshRoots.employees, counts: [['[aria-label="员工明细记录"] details.employee-card[open] .employee-detail', 1]], refresh: true, requireNewResponse: true, trigger: '.module-header button.refresh',
          rows: '[aria-label="员工明细记录"] .employee-card',
          prepare: async function () {
            await openModule(employees, { employee_filter: "all" }); const card = page.locator('[aria-label="员工明细记录"] details.employee-card').first(); if (!await card.count()) return false;
            await change(employees, () => card.locator(":scope > summary").click()); await card.locator(".employee-detail").waitFor({ state: "visible" });
            await rememberRetained(this); return true;
          }, perform: async function () {
            this.refreshEvidence = { status: "pending", verified_responses: [] };
            const replies = [waitForLater(response("context")), waitForLater(page.waitForResponse(async reply => {
              const url = dashboardResponseTarget(reply); return matchesAction(url, employees.action) && !url.searchParams.has("section") && !url.searchParams.has("cursor") && await readyReceipt(reply);
            }))];
            await page.locator('.module-header button.refresh').click();
            for (const reply of await Promise.all(replies)) {
              const payload = await verifyReply(reply, employees, company, { allowEmployeeContinuation: true });
              this.refreshEvidence.verified_responses.push({ url: diagnosticUrl(reply.url()), snapshot_version: payload.snapshot_version });
            }
            await settleRequiredReads(employees); this.refreshEvidence.status = "verified";
          }, verify: async function () {
            assert.equal(await page.locator('[aria-label="员工明细记录"] details.employee-card[open] .employee-detail').count(), 1, "refresh lost the expanded detail");
            verifyRetainedRefreshState(this.beforeRefresh, await retainedState(this), this.key);
            if (retainedRefreshOnly) {
              await verifyTarget(this, retainedPayloads.slice(this.refreshReadOffset));
              this.afterCollectionIdentities = retainedCollectionIdentities(retainedPayloads.slice(this.refreshReadOffset));
              verifyRetainedCollectionIdentities(this.beforeCollectionIdentities, this.afterCollectionIdentities);
            }
          },
        });
        for (const all of [false, true]) {
          let voucherCount;
          specs.push({ key: all ? "brief_vouchers_all_complete" : "brief_vouchers_next_page", module: brief, root: "#activity", counts: [], requireNewResponse: true,
            trigger: all ? '[role=switch][aria-label="改为全部显示凭证"]' : '.voucher-pagination button:last-of-type',
            prepare: async function () {
              const main = await openModule(brief); voucherCount = main.data.voucher_count; if (voucherCount <= 20) return false;
              await change(brief, () => page.getByRole("button", { name: "按凭证", exact: true }).click(), "vouchers");
              this.counts = [[".voucher-card", all ? voucherCount : Math.min(20, voucherCount - 20)]]; return true;
            }, perform: () => change(brief, () => all ? page.getByRole("switch", { name: "改为全部显示凭证", exact: true }).click() : page.getByRole("button", { name: "下一页", exact: true }).click(), all || voucherCount > 20 ? "vouchers" : null),
            verify: async function () {
              assert.equal(await page.locator(".voucher-card").count(), this.counts[0][1]);
              if (all) assert.equal(new Set(await page.locator(".voucher-reference strong").allTextContents()).size, voucherCount, "all vouchers lost or duplicated identities");
              else assert((await page.locator(".voucher-pagination strong").textContent()).trim().startsWith("2 /"));
            },
          });
        }
        for (const section of ["activity", "open_items"]) {
          const root = section === "activity" ? "#activity" : "#open-items";
          const row = `${root} ${section === "activity" ? ".event-row" : ".open-event-row"}[role=button]`;
          specs.push({ key: `brief_${section}_members_first_open`, module: brief, root, counts: [],
            requireNewResponse: true, auxiliaryActions: ["brief-group"], trigger: `${row}:first-of-type`,
            prepare: async function () {
              const main = await openModule(brief);
              await change(brief, () => page.getByRole("button", { name: "按业务", exact: true }).click());
              const categories = section === "activity" ? main.data.activity_groups : main.data.open_items.categories.filter(item => item.count);
              const size = item => section === "activity" ? item.event_count : item.count;
              const buttons = page.locator(section === "activity" ? "#activity .index button" : "#open-items .open-index button");
              let largestRow, selected;
              // Category totals are public upper bounds. Inspect another category
              // only while it can contain a larger group, entirely before timing.
              for (const category of categories.map((item, index) => ({ bound: size(item), index })).toSorted((a, b) => b.bound - a.bound)) {
                if (largestRow && category.bound <= largestRow.count) break;
                await change(brief, () => buttons.nth(category.index).click()); selected = category.index;
                const candidate = await page.locator(row).evaluateAll(nodes => nodes.map(node => ({
                  count: Number(node.querySelector(".business-list-matter small").textContent.trim().split(" ")[0]),
                  index: Array.from(node.parentElement.children).indexOf(node),
                })).toSorted((a, b) => b.count - a.count)[0]);
                if (candidate && (!largestRow || candidate.count > largestRow.count)) largestRow = { ...candidate, category: category.index };
              }
              assert(largestRow?.count > 20, `${section}: fixture must exercise real member continuation`);
              if (selected !== largestRow.category) await change(brief, () => buttons.nth(largestRow.category).click());
              this.memberCount = largestRow.count; this.trigger = `${row}:nth-child(${largestRow.index + 1})`;
              return true;
            }, perform: async function () {
              this.reads = await detailChange(brief, () => page.locator(this.trigger).click(), `${root} .group-members .member-row`);
            }, verify: async function () {
              const members = retainedOrderedItems(this.reads, "/api/dashboard/brief-group", "members");
              assert(members.length, `${section}: empty group members`);
              assert.equal(members.length, this.memberCount, `${section}: opened another group or missed members`);
              assert.equal(await page.locator(`${root} .group-members .member-row`).count(), members.length);
              assert.equal(await page.locator(`${root} .business-detail-panel`).count(), 0, "group first-open preloaded member progress");
            },
          });
        }
        specs.push({ key: "brief_business_progress_first_open", module: brief, root: "#activity", counts: [["#activity .business-detail-panel", 1]],
          requireNewResponse: true, auxiliaryActions: ["business-status"], trigger: "#activity .group-members .member-row > button",
          prepare: async function () {
            await openModule(brief);
            await change(brief, () => page.getByRole("button", { name: "按业务", exact: true }).click());
            await change(brief, () => page.locator("#activity .index button").first().click());
            await detailChange(brief, () => page.locator('#activity .event-row[role=button]').first().click(), "#activity .group-members .member-row");
            const target = page.locator("#activity .group-members").getByRole("button", { name: "业务进展", exact: true }).first();
            assert(await target.count(), "progress: no actual member entry");
            return true;
          }, perform: async function () {
            this.reads = await detailChange(brief, () => page.locator(this.trigger).first().click(), "#activity .business-detail-panel");
          }, verify: async function () {
            assert(this.reads.some(read => new URL(read.url).pathname === "/api/dashboard/business-status"));
            assert.equal(await page.locator("#activity .business-detail-panel").count(), 1);
          },
        });
        const laborCard = '[aria-label="个人劳务明细记录"] .employee-card';
        specs.push({ key: "employees_labor_sources_first_open", module: employees, root: refreshRoots.employees, counts: [],
          requireNewResponse: true, trigger: `${laborCard}:first-of-type > summary`,
          prepare: async function () { await openModule(employees); assert(await page.locator(laborCard).count(), "labor: no actual contractor"); return true; },
          perform: async function () {
            this.reads = await detailChange(employees, () => page.locator(laborCard).first().locator(":scope > summary").click(), `${laborCard} .labor-source-record`);
          }, verify: async function () {
            const sources = retainedOrderedItems(this.reads, "/api/dashboard/employees", "labor_sources");
            assert(sources.length, "labor: source detail empty");
            assert.equal(await page.locator(`${laborCard} .labor-source-record`).count(), sources.length);
            assert.equal(this.reads.at(-1).payload.data.collections.labor_sources.page.has_more, false);
            for (const source of sources) {
              const text = await page.locator(`[data-source-id=${JSON.stringify(source.source_id)}]`).textContent();
              for (const field of ["gross_fen", "booked_tax_fen", "net_fen"]) assert(text.includes(formatFen(source[field])), `labor: ${field} missing`);
            }
          },
        });
        specs.push({ key: "employees_labor_payments_first_open", module: employees, root: refreshRoots.employees, counts: [[`${laborCard} .business-detail-panel`, 1]],
          requireNewResponse: true, auxiliaryActions: ["business-status"], trigger: `${laborCard} .labor-source-payments > summary`,
          prepare: async function () {
            await openModule(employees);
            await detailChange(employees, () => page.locator(laborCard).first().locator(":scope > summary").click(), `${laborCard} .labor-source-record`);
            return true;
          }, perform: async function () {
            this.reads = await detailChange(employees, () => page.locator(`${laborCard} .labor-source-payments > summary`).first().click(), `${laborCard} .business-detail-panel`);
          }, verify: async function () { assert(this.reads.some(read => new URL(read.url).pathname === "/api/dashboard/business-status")); },
        });
        specs.push({ key: "funds_bank_batch_first_open", module: funds, root: "#bank-details", counts: [], trigger: ".bank-batch-trigger",
          prepare: async function () {
            const main = await openModule(funds);
            const payloads = await change(funds, () => page.locator("#fund-detail-tab-bank").click());
            assert(await page.locator(".bank-batch-trigger").count(), "bank: no actual reconciled multi-person batch");
            const rows = [main, ...payloads].filter(payload => payload.data.collections.statements).at(-1).data.collections.statements.items;
            const largest = Math.max(...rows.map(item => item.batch_payment?.items.length ?? 0));
            const index = rows.findIndex(item => item.batch_payment?.items.length === largest);
            this.batch = rows[index]?.batch_payment;
            assert(this.batch?.items.length > 1, "bank: rendered batch lacks exact response identity");
            this.trigger = `.bank-activity-item:nth-child(${index + 1}) .bank-batch-trigger`;
            assert(await page.locator(this.trigger).count(), "bank: response batch missing from rendered row");
            this.counts = [[".bank-batch-detail .bank-batch-items > li", this.batch.items.length]]; return true;
          }, perform: function () { return change(funds, () => page.locator(this.trigger).click()); },
          verify: async function () {
            const items = await page.locator(".bank-batch-detail .bank-batch-items > li").evaluateAll(nodes => nodes.map(node => [node.querySelector("strong").textContent.trim(), node.querySelector("b").textContent.trim()]));
            assert.deepEqual(items, this.batch.items.map(item => [item.party, formatFen(item.amount_fen)]));
            assert((await page.locator(".bank-batch-heading-meta").textContent()).includes(formatFen(this.batch.total_fen)));
          },
        });
        const reports = modules.find(item => item.key === "reports");
        for (let index = 0; index < 3; index++) for (const mode of ["statement", "template_on", "template_off"]) {
          const template = mode === "template_on";
          specs.push({ key: `reports_${index + 1}_${mode}`, module: reports, root: refreshRoots.reports, counts: [["#report-full", 1]],
            trigger: mode === "statement" ? `#report-statement-button-${index}` : ".template-switch",
            prepare: async function () {
              const main = await openModule(reports); this.statement = main.statements[index]; assert(this.statement, "reports: required statement missing");
              if (mode !== "statement") {
                await change(reports, () => page.locator(`#report-statement-button-${index}`).click());
                if (mode === "template_off") await change(reports, () => page.locator(".template-switch input").check());
              }
              return true;
            }, perform: () => change(reports, () => mode === "statement" ? page.locator(`#report-statement-button-${index}`).click()
              : template ? page.locator(".template-switch input").check() : page.locator(".template-switch input").uncheck()),
            verify: async function () {
              assert.equal(await page.locator(`#report-statement-button-${index}`).getAttribute("aria-selected"), "true");
              assert.equal(await page.locator(".template-switch input").isChecked(), template);
              const mainLines = {
                balance_sheet: [15, 29, 30, 41, 46, 47, 52, 53],
                profit_statement: [1, 2, 3, 11, 14, 18, 20, 21, 22, 24, 30, 31, 32],
                cash_flow_statement: [7, 13, 19, 20, 21, 22],
              }[this.statement.key];
              const expectedRows = template || !mainLines ? this.statement.rows
                : mainLines.map(line => this.statement.rows.find(row => row.line === line)).filter(Boolean);
              const expected = expectedRows.map(row => this.statement.columns.map(column => {
                const value = row.values[column.key];
                if (value === null) return template ? "—" : "暂无法确定";
                const formatted = formatFen(value);
                return template ? formatted.replace("¥", "").replace("−", "-") : formatted;
              }));
              if (template) {
                const actual = await page.locator(".tax-template-sheet .desktop-template-table td.line").evaluateAll(nodes => nodes
                  .filter(node => /^\d+$/.test(node.textContent.trim()))
                  .map(node => ({ line: Number(node.textContent.trim()), values: [node.nextElementSibling.textContent.trim(), node.nextElementSibling.nextElementSibling.textContent.trim()] })));
                assert.equal(actual.length, expectedRows.length, "report: template omitted or duplicated lines");
                assert.deepEqual(actual.toSorted((a, b) => a.line - b.line), expectedRows.map((row, offset) => ({ line: row.line, values: expected[offset] })).toSorted((a, b) => a.line - b.line), "report: full visible template amounts differ");
              } else {
                const actual = await page.locator("#report-full .statement-table-wrap > table tbody tr").evaluateAll(nodes => nodes
                  .map(node => Array.from(node.querySelectorAll("td.statement-number")).map(cell => cell.textContent.trim())));
                assert.deepEqual(actual, expected, "report: complete visible statement amounts differ");
              }
            },
          });
        }
        if (retainedRefreshOnly) {
          const strengthenedEmployee = specs.find(spec => spec.key === "employees_expanded_detail_refresh");
          specs.length = 0;
          function addRetained(key, module, rows, prepare, counts = []) {
            specs.push({ key, module, root: refreshRoots[module.key], rows, counts, refresh: true, requireNewResponse: true,
              trigger: '.module-header button.refresh',
              prepare: async function () {
                this.unverifiedReason = undefined;
                if (!await prepare.call(this)) { this.unverifiedReason ??= "no real matching business record/detail in the selected scope"; return false; }
                await settleRequiredReads(module); await rememberRetained(this); return true;
              }, perform: async function () { await retainedRefresh(this); },
              verify: async function () { verifyRetainedRefreshState(this.beforeRefresh, await retainedState(this), this.key); },
            });
          }
          for (const section of ["activity", "open_items"]) {
            const root = section === "activity" ? "#activity" : "#open-items";
            const buttons = `${root} ${section === "activity" ? ".index" : ".open-index"} button`;
            const rows = `${root} ${section === "activity" ? ".event-row" : ".open-event-row"}`;
            addRetained(`brief_${section}_retained_detail_refresh`, brief, rows, async function () {
              await openModule(brief);
              if (await page.locator(buttons).count() < 2) { this.unverifiedReason = "no real nondefault brief category"; return false; }
              await change(brief, () => page.locator(buttons).nth(1).click());
              const row = page.locator(`${rows}[role=button]`).first();
              if (!await row.count()) return false;
              await row.click(); await settleRequiredReads(brief);
              await row.locator(".group-members").waitFor({ state: "visible" });
              const groupReads = retainedPayloads.filter(item => new URL(item.url).pathname === "/api/dashboard/brief-group"
                && item.payload.data.section === section && item.payload.data.collections.members.items.length);
              if (!groupReads.length) { this.unverifiedReason = `${section}: real group has no verified members to retain`; return false; }
              this.target_identity = groupReads.at(-1).payload.data.group_key;
              const main = retainedPayloads.find(read => new URL(read.url).pathname === "/api/dashboard/brief" && !new URL(read.url).searchParams.has("section"))?.payload;
              this.category_section = section;
              this.category_key = (section === "activity" ? main.data.activity_groups : main.data.open_items.categories.filter(item => item.count))[1].key;
              this.requiresTarget = true;
              this.expected_refresh_behavior = section === "activity" ? "close_activity_members_retain_category" : "retain_same_snapshot_outer_group_reload_members_close_member_progress";
              this.counts = [[`${rows}[aria-expanded=true] .group-members`, section === "activity" ? 0 : 1]]; return true;
            });
          }
          addRetained("funds_book_retained_detail_refresh", funds, ".book-activity-row", async function () {
            await openModule(funds);
            const accounts = page.locator(".fund-account-index > button");
            if (await accounts.count() < 2) { this.unverifiedReason = "no real nondefault funds account"; return false; }
            await change(funds, () => accounts.nth(1).click(), "movements");
            const row = page.locator('.book-activity-row[role=button]').first(); if (!await row.count()) return false;
            await row.click(); await settleRequiredReads(funds);
            await row.locator(".business-detail-panel").waitFor({ state: "visible" });
            const detail = retainedPayloads.findLast(item => new URL(item.url).pathname === "/api/dashboard/business-status");
            assert(detail, "funds expanded detail lacks a generated-contract-verified business response");
            this.target_identity = detail.payload.data.identity.subject_id;
            this.requiresTarget = true;
            this.counts = [['.book-activity-row[aria-expanded=true] .business-detail-panel', 1]]; return true;
          });
          addRetained("funds_bank_retained_refresh", funds, ".bank-activity-item", async function () {
            await openModule(funds); await change(funds, () => page.locator("#fund-detail-tab-bank").click());
            if (!await page.locator(".bank-activity-item").count()) { this.unverifiedReason = "no actual bank statement rows"; return false; }
            assert.equal(await page.locator("#fund-detail-tab-bank").getAttribute("aria-selected"), "true"); return true;
          });
          const assets = modules.find(item => item.key === "assets"), reports = modules.find(item => item.key === "reports");
          addRetained("assets_fixed_list_retained_detail_refresh", assets, ".asset-grid > article", async function () {
            const main = await openModule(assets); await selectFilter(assets, "fixed");
            await page.getByRole("group", { name: "资产展示方式", exact: true }).getByRole("button", { name: "列表", exact: true }).click();
            const detail = page.locator("details.asset-list-details").first(); if (!await detail.count()) return false;
            await detail.locator(":scope > summary").click(); await detail.locator(".asset-list-detail").waitFor({ state: "visible" });
            this.target_identity = retainedPayloads.find(item => item.payload.data?.collections?.assets && !new URL(item.url).searchParams.has("cursor")
              && item.payload.data.asset_filter === "fixed")?.payload.data.collections.assets.items[0]?.asset_id;
            assert(this.target_identity, "fixed asset expanded without a verified asset identity");
            this.requiresTarget = true;
            this.counts = [["details.asset-list-details[open] .asset-list-detail", 1]]; return true;
          });
          addRetained("reports_statement_retained_detail_refresh", reports, "#report-full tbody tr", async function () {
            const main = await openModule(reports); if (main.statements.length < 2) { this.unverifiedReason = "no real nondefault statement"; return false; }
            await page.locator("#report-statement-button-1").click(); await page.locator("#report-full").waitFor({ state: "visible" });
            this.target_identity = main.statements[1].key; this.requiresTarget = true; this.counts = [["#report-full", 1]]; return true;
          });
          addRetained("employees_automatic_continuation_refresh", employees, '[aria-label="员工明细记录"] .employee-card', async function () {
            const main = await openModule(employees, { employee_filter: "all" });
            const total = main.data.collections.employees.page.filtered_count;
            if (total <= 20) { this.unverifiedReason = "employee selection has no real continuation beyond 20"; return false; }
            const navigation = page.locator('.section-nav').getByRole('button', { name: '员工明细', exact: true });
            await navigation.click();
            assert.equal(await navigation.getAttribute('aria-current'), 'location', "employees: actual employee section did not activate");
            await waitForStage9(() => page.waitForFunction(expected => document.querySelectorAll('[aria-label="员工明细记录"] .employee-card').length === expected, total),
              "employees: real automatic continuation", Date.now() + 30000);
            await settleRequiredReads(employees);
            assert(retainedPayloads.some(item => new URL(item.url).searchParams.has("cursor")
              && item.payload.data?.collections?.employees), "employee rows lack verified actual continuation");
            this.counts = [['[aria-label="员工明细记录"] .employee-card', total]]; return true;
          });
          specs.push(strengthenedEmployee);
          assert.deepEqual(specs.map(spec => spec.key), retainedRefreshKeys);
        }
        function requiredListener(spec, required) {
          return request => {
            const pathname = dashboardRequestTarget(request).pathname;
            if (pathname === `/api/dashboard/${spec.module.action}` || (spec.navigation || spec.refresh) && pathname === "/api/dashboard/context"
              || (retainedRefreshOnly || spec.auxiliaryActions?.length) && (spec.auxiliaryActions ?? ["brief-group", "business-status"]).some(action => pathname === `/api/dashboard/${action}`)) required.add(request);
          };
        }
        function verifiedInteractionReads(spec, required) {
          const reads = [];
          for (const request of required) {
            const record = requestDiagnostics.get(request);
            if (apiFailures.some(failure => failure.request?.request_id === record?.request_id && allowedLocalAborts.has(failure))) continue;
            verifiedRequestRecord(requestDiagnostics, verifiedRequests, request, spec.key);
            const target = dashboardRequestTarget(request);
            assert.equal(target.origin, new URL(config.origin).origin, `${spec.key}: required response origin changed`);
            assert(interactionScopeAllows(target, { action: spec.module.action, company_id: company.id, period: company.period,
              snapshot: refreshProjections.get(spec.module.key)?.snapshot, quarter, retained_refresh: retainedRefreshOnly,
              auxiliary_actions: spec.auxiliaryActions, business_status_versions: [verifiedPayloads.get(request)?.snapshot_version] }), `${spec.key}: required response scope changed`);
            reads.push({ request_id: record.request_id, status: "verified", url: diagnosticUrl(target.href) });
          }
          return reads;
        }
        if (voucherTailDiagnostic) {
          const selected = specs.find(spec => spec.key === "brief_vouchers_all_complete");
          assert(selected, "voucher tail diagnostic lost its actual all-vouchers scenario");
          specs.splice(0, specs.length, selected);
        }
        for (const spec of specs) {
          const result = interactionResults[spec.key] = { status: "preparing", measurement_scope: retainedRefreshOnly ? "retained_refresh_state" : "existing_interaction",
            timing_scope: spec.navigation ? "exact_target_navigation" : spec.refresh ? "expanded_hot_refresh" : "local_interaction",
            completion: "browser current-cycle successful resources and calibrated full continuation chain, then complete selected content for two stable frames"
              + (spec.position ? " with the focused target in the viewport at its calibrated position and unchanged geometry/scroll coordinates" : "")
              + "; Node validation accepts or rejects afterward",
            timing_includes: "product fetch/download and rendering plus limited browser observer bookkeeping; Node response/DOM validation excluded; default page timing unchanged", attempts: [] };
          let prepared;
          try { prepared = await spec.prepare(); }
          catch (error) {
            if (!retainedRefreshOnly) throw error;
            result.status = "failed"; result.failure_phase = "retained_refresh_prepare"; result.message = sanitize(error.message);
            result.prepare_diagnostic = await page.evaluate(({ root, rows }) => ({
              root_present: Boolean(document.querySelector(root)), row_count: document.querySelectorAll(rows ?? ':not(*)').length,
              active_section: document.querySelector('.section-nav button[aria-current]')?.textContent.trim() ?? null,
              employee_rows: document.querySelectorAll('[aria-label="员工明细记录"] .employee-card').length,
              labor_rows: document.querySelectorAll('[aria-label="个人劳务明细记录"] .employee-card').length,
              employee_filter: document.querySelector('[aria-label="筛选人员"].select-trigger')?.getAttribute('data-value') ?? null,
              busy: document.querySelector('.module-header')?.getAttribute('aria-busy') ?? null,
            }), { root: spec.root, rows: spec.rows });
            result.prepare_diagnostic.page_url = diagnosticUrl(page.url());
            result.prepare_diagnostic.pending_requests = requestDiagnostics.pending();
            continue;
          }
          if (!prepared) { result.status = "unverified"; result.reason = spec.unverifiedReason ?? "required real response target/category/page unavailable"; continue; }
          if (retainedRefreshOnly || spec.key === "employees_expanded_detail_refresh") {
            result.expected_refresh_behavior = spec.expected_refresh_behavior ?? "retain_same_snapshot_visible_state";
            result.before_refresh = spec.beforeRefresh; result.before_response_identities = spec.beforeResponseIdentities;
            result.before_collection_identities = spec.beforeCollectionIdentities;
            result.before_target_binding = spec.beforeTargetBinding;
            result.before_auxiliary_complete = spec.beforeAuxiliaryComplete;
            result.target_identity = spec.target_identity ?? null;
          }
          result.status = "calibrating";
          const calibratedRequests = new Set(), calibrateListener = requiredListener(spec, calibratedRequests);
          page.on("request", calibrateListener);
          let calibratedPosition;
          let calibrationError;
          try {
            await spec.perform(); await spec.verify();
            if (spec.position) calibratedPosition = await waitForStage9(() => page.evaluate(calibrateInteractionPosition, spec.position), `${spec.key}: positioning calibration`);
            assertNoBrowserFailures();
          }
          catch (error) { if (!retainedRefreshOnly) throw error; calibrationError = error; }
          finally { page.off("request", calibrateListener); }
          if (calibrationError) {
            result.status = "failed"; result.failure_phase = "retained_refresh_calibration";
            result.message = sanitize(calibrationError.message); result.after_refresh = await retainedState(spec);
            result.refresh_evidence = spec.refreshEvidence; continue;
          }
          const calibratedReads = verifiedInteractionReads(spec, calibratedRequests);
          const projection = await page.evaluate(captureRefreshProjection, { root: spec.root });
          assert(projection && JSON.parse(projection).content.length, `${spec.key}: empty calibrated target`);
          const selection = { root: spec.root, counts: spec.counts, projection, trigger: spec.trigger, navigation: Boolean(spec.navigation), refresh: Boolean(spec.refresh), url: spec.url,
            ...(voucherTailDiagnostic ? { diagnostic_voucher_tail: true } : {}),
            position: spec.position ? { ...spec.position, expected: calibratedPosition } : undefined,
            cycle_id: spec.key, require_new_response: Boolean(spec.requireNewResponse), scope: { action: spec.module.action, company_id: company.id, period: company.period, snapshot: refreshProjections.get(spec.module.key)?.snapshot,
              quarter, auxiliary_actions: spec.auxiliaryActions,
              business_status_versions: [...new Set([...calibratedRequests].filter(request => dashboardRequestTarget(request).pathname === "/api/dashboard/business-status")
                .map(request => verifiedPayloads.get(request)?.snapshot_version))],
              ...(retainedRefreshOnly ? { retained_refresh: true } : {}) } };
          selection.read_plan = { cycle_id: selection.cycle_id, scope: { ...selection.scope }, required: interactionReadMultiset(calibratedReads),
            cache_verified: !selection.require_new_response && calibratedReads.length === 0 && localActions.at(-1)?.status === "passed" };
          assert(verifiedReadCycleReady(selection, { ...selection.read_plan, cycle_id: selection.cycle_id, status: "verified", pending: 0, failed: 0, required_reads: calibratedReads }), `${spec.key}: no verified network/cache calibration`);
          result.read_plan = selection.read_plan;
          if (selection.position) result.position_plan = selection.position;
          if (spec.navigation) await page.addInitScript(observeInteraction, selection);
          for (const kind of ["warmup", "sample"]) for (let index = 0; index < (kind === "warmup" ? warmups : samples); index++) {
            try { await recordRefreshAttempt(result.attempts, { scenario: spec.key, kind, iteration: index + 1 }, async attempt => {
              interactionAttempts.push(attempt);
              assert(await spec.prepare(), `${spec.key}: real target disappeared`);
              if (retainedRefreshOnly || spec.key === "employees_expanded_detail_refresh") {
                attempt.before_refresh = spec.beforeRefresh; attempt.before_response_identities = spec.beforeResponseIdentities;
                attempt.before_collection_identities = spec.beforeCollectionIdentities;
                attempt.before_target_binding = spec.beforeTargetBinding;
                attempt.before_auxiliary_complete = spec.beforeAuxiliaryComplete;
                attempt.expected_refresh_behavior = spec.expected_refresh_behavior ?? "retain_same_snapshot_visible_state";
              }
              if (!spec.navigation) {
                if (!spec.refresh) assert.notEqual(await page.evaluate(captureRefreshProjection, { root: spec.root }), projection, `${spec.key}: preparation already shows the target`);
                await page.evaluate(observeInteraction, selection);
              }
              attempt.phase = "action";
              const required = new Set();
              let trace;
              if (voucherTailDiagnostic && kind === "sample" && index === 0 && config.diagnostic_trace_path) {
                trace = await startVoucherTailTrace(context, page, config.diagnostic_trace_path);
              }
              const collectRequired = requiredListener(spec, required);
              let operationFailed = false;
              page.on("request", collectRequired);
              try {
                interactionEndpointPending = true;
                await spec.perform();
              attempt.phase = "render";
              await page.waitForFunction(() => window.__stage9Interaction?.end !== null && window.__stage9Interaction?.end !== undefined || window.__stage9Interaction?.error);
              const state = await page.evaluate(() => window.__stage9Interaction);
              const timeOrigin = await page.evaluate(() => performance.timeOrigin);
              attempt.start_epoch_ms = timeOrigin + state.start;
              attempt.end_epoch_ms = timeOrigin + state.end;
              if (trace) { attempt.renderer_trace = await trace.finish(); trace = null; }
              if (state) assert(!state.error && Number.isFinite(state.end), state.error ?? "missing interaction endpoint");
              const milliseconds = state.end - state.start;
              attempt.elapsed_ms = milliseconds;
              if (voucherTailDiagnostic) {
                attempt.diagnostic_only = true; attempt.instrumented = true; attempt.sla_eligible = false;
                attempt.voucher_tail_diagnostic = state.diagnostic;
                attempt.voucher_tail_diagnostic.network_complete_at_ms = state.network_complete_at;
                attempt.voucher_tail_diagnostic.click_start_ms = state.start;
                attempt.voucher_tail_diagnostic.endpoint_ms = state.end;
                attempt.voucher_tail_diagnostic.fetches = state.fetches;
              }
              attempt.phase = "validate";
              assertNoBrowserFailures();
              const reads = verifiedInteractionReads(spec, required);
              assert.deepEqual(interactionReadMultiset(reads), selection.read_plan.required, `${spec.key}: necessary read multiset changed from verified same-snapshot calibration`);
              const proof = { cycle_id: selection.cycle_id, scope: selection.scope, status: "verified", pending: 0, failed: 0,
                required_reads: reads, cache_verified: selection.read_plan.cache_verified && localActions.at(-1)?.status === "passed" };
              assert(verifiedReadCycleReady(selection, proof), `${spec.key}: missing current-cycle response or validated cache`);
              attempt.read_cycle = { ...proof, observed_request_count: required.size, candidate_endpoint_ms: state.end, validation_after_endpoint: true,
                network_complete_at_ms: state.network_complete_at, render_tail_ms: state.end - state.network_complete_at,
                browser_fetch_count: state.fetches.length, path: selection.read_plan.required.length ? "new_network" : "validated_same_snapshot_cache" };
              if (selection.position) attempt.position = state.position;
              if (state) { assert(!state.error, state.error); assert.deepEqual(state.flashes, [], `${spec.key}: local change flashed whole-page loading`); }
              await spec.verify(); assertNoBrowserFailures();
              if (retainedRefreshOnly) {
                attempt.after_collection_identities = spec.afterCollectionIdentities;
                attempt.after_target_binding = spec.afterTargetBinding;
                attempt.after_auxiliary_complete = spec.afterAuxiliaryComplete;
              }
              assert.equal(required.size, attempt.read_cycle.observed_request_count, `${spec.key}: another necessary read started during final validation`);
              if (spec.refresh) attempt.refresh_evidence = spec.refreshEvidence;
              attempt.verified_reads = layoutVerifiedReads.map(item => ({ ...item }));
              return milliseconds;
              } catch (error) { operationFailed = true; throw error; }
              finally {
                if (trace) attempt.renderer_trace = await trace.finish();
                interactionEndpointPending = false; page.off("request", collectRequired);
                await waitForStage9(() => page.evaluate(() => window.__stage9InteractionCleanup?.()),
                  `${spec.key}: interaction observer cleanup`, Date.now() + 1000).catch(error => {
                  attempt.cleanup_error = sanitize(error.message);
                  if (!operationFailed) throw error;
                });
              }
            }, async () => {
              if (result.attempts.at(-1)?.phase === "prepare") return { started: false };
              const state = await page.evaluate(() => window.__stage9Interaction);
              if (spec.refresh) result.attempts.at(-1).refresh_evidence = spec.refreshEvidence;
              return { started: Boolean(state?.started), elapsed_ms: state?.end === null ? await page.evaluate(() => performance.now() - window.__stage9Interaction.start) : state?.end - state?.start };
            }, checkpoint); } catch { /* Keep every attempted sample, including failed and slow ones. */ }
          }
          const timings = result.attempts.filter(item => item.kind === "sample").map(item => item.elapsed_ms);
          const complete = result.attempts.every(item => item.status === "complete");
          Object.assign(result, { status: complete ? "measured" : "failed", samples: timings, ...stage9TimingSummary(timings, interactionTargetMs, complete) });
        }
        assertNoBrowserFailures();
        const interactionReport = { browser_environment: browserEnvironment, resource_failures: resourceFailures, status: stage9InteractionStatus(interactionResults, interactionTargetMs), target_ms: interactionTargetMs, timing_gate: interactionTargetMs !== null, measurement_mode: "interaction_timing",
          measurement_scope: retainedRefreshOnly ? "retained_refresh_state" : "existing_interaction", default_page_acceptance: false, warmups, sample_count: samples,
          interactions: interactionResults, interaction_attempts: interactionAttempts, local_actions: localActions, obsolete_cancellations: obsoleteCancellations,
          unverified: Object.entries(interactionResults).filter(([, value]) => value.status === "unverified").map(([key]) => key) };
        if (voucherTailDiagnostic) Object.assign(interactionReport, {
          status: Object.values(interactionResults).every(value => value.status === "measured") ? "diagnostic_complete" : "failed",
          diagnostic_only: true, instrumented: true, sla_eligible: false,
          measurement_mode: "voucher_tail_diagnostic", measurement_scope: "single_voucher_tail_diagnostic",
        });
        if (interactionReport.status === "failed") failureReport = interactionReport;
        return interactionReport;
      }
      for (const width of [320, 375, 768, 1440]) {
        await page.setViewportSize({ width, height: 900 });
        for (const module of modules) {
          layoutProgress = { page: module.key, viewport: width };
          const expected = [response("context"), response(module.action)];
          layoutVerifiedReads.length = 0;
          layoutMovementAccount = null;
          layoutAccounts.length = 0;
          await navigate(address(module));
          let mainPayload;
          for (const reply of await Promise.all(expected)) {
            const payload = await verifyReply(reply, module);
            if (dashboardResponseTarget(reply).pathname === `/api/dashboard/${module.action}`) mainPayload = payload;
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
            await localChange(module, () => selectDashboardOption(page, "筛选人员", "in_period"), "employees", "select in-period employees", "in_period");
            await localChange(module, () => selectDashboardOption(page, "筛选人员", "all"), "employees", "select all employees", "all");
          }
          if (module.key === "assets") {
            await localChange(module, () => selectDashboardOption(page, "筛选资产", "fixed"), "assets", "select fixed assets", "fixed");
            await localChange(module, () => selectDashboardOption(page, "筛选资产", "all"), "assets", "select all assets", "all");
          }
          if (module.key === "funds") {
            await localChange(module, () => page.locator("#fund-detail-tab-bank").click(), null, "select bank view");
            await localChange(module, () => page.locator("#fund-detail-tab-book").click(), null, "select book view");
            const accounts = page.locator(".fund-account-index > button");
            if (await accounts.count() > 1) {
              await localChange(module, () => accounts.nth(1).click(), "movements", "select second movement account");
              await localChange(module, () => accounts.first().click(), "movements", "select first movement account");
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
            const voucherReplies = await localChange(module, () => page.getByRole("button", { name: "按凭证", exact: true }).click(), "vouchers");
            const voucherCollection = voucherReplies.find(payload => payload.data?.collections.vouchers)?.data.collections.vouchers;
            assert(voucherCollection, "brief: initial voucher display response missing");
            const vouchers = voucherCollection.items;
            assert.equal(await page.locator(".voucher-card").count(), vouchers.length, "brief: first 20 vouchers incomplete");
            if (vouchers.length) {
              await localChange(module, () => page.locator(".voucher-row").first().click());
              const detail = page.locator(".voucher-inline-detail");
              await detail.waitFor();
              const visibleNumber = (await page.locator(".voucher-card.is-open .voucher-reference strong").textContent()).trim().replace(/^凭证\s+/, "");
              const selectedVoucher = vouchers.find(voucher => voucher.number === visibleNumber);
              assert(selectedVoucher, "voucher: visible identity missing from local response");
              const rows = await detail.locator("tbody tr").evaluateAll(nodes => nodes.map(node => ({
                code: node.querySelector("td[data-label='科目'] small")?.textContent.trim(),
                account: node.querySelector("td[data-label='科目'] strong")?.textContent.trim(),
                debit: node.querySelector("td[data-label='借方']")?.textContent.trim(),
                credit: node.querySelector("td[data-label='贷方']")?.textContent.trim(),
              })));
              verifyVisibleVoucherRows(rows, selectedVoucher);
              await checkOwnerLayout(module, "voucher_detail");
            }
            if (width === 375 && voucherCollection.page.has_more) {
              await localChange(module, async () => {
                await page.getByRole("button", { name: "下一页", exact: true }).click();
                await page.waitForFunction(() => document.querySelector(".voucher-pagination strong")?.textContent?.trim().startsWith("2 /"));
              }, "vouchers");
              assert.equal(await page.locator(".voucher-card").count(), Math.min(20, mainPayload.data.voucher_count - 20), "voucher: second page incomplete");
              await localChange(module, async () => {
                await page.getByRole("switch", { name: "改为全部显示凭证", exact: true }).click();
                await page.waitForFunction(total => document.querySelectorAll(".voucher-card").length === total
                  && document.querySelector(".voucher-view")?.getAttribute("aria-busy") === "false", mainPayload.data.voucher_count);
              }, mainPayload.data.voucher_count > 40 ? "vouchers" : null);
              const numbers = await page.locator(".voucher-reference strong").allTextContents();
              assert.equal(new Set(numbers).size, mainPayload.data.voucher_count, "voucher: all mode duplicated or lost vouchers");
              await localChange(module, () => page.getByRole("switch", { name: "改为分页显示凭证", exact: true }).click());
            }
            await localChange(module, () => page.getByRole("button", { name: "按业务", exact: true }).click());
          }
          let detail = page.locator(".business-status-details").first();
          let briefProgress = null;
          if (module.key === "brief") {
            const group = page.locator("#activity .event-row").first();
            assert(await group.count(), "brief: no activity group for business detail coverage");
            await expandActivityGroup(group, mainPayload.snapshot_version);
            briefProgress = group.getByRole("button", { name: "业务进展", exact: true }).first();
            assert(await briefProgress.count(), "brief: selected activity group has no progress entry; detail coverage unavailable");
            detail = briefProgress.locator("xpath=ancestor::li[1]").locator(".business-status-details");
          }
          if (await detail.count()) {
            const ancestors = detail.locator("xpath=ancestor::details[not(@open)]");
            const count = await ancestors.count();
            for (let index = 0; index < count; index++) {
              // Requery after each real click: opened ancestors leave this set.
              await detail.locator("xpath=ancestor::details[not(@open)]").first()
                .locator(":scope > summary").click();
            }
            const reply = response("business-status");
            if (module.key === "brief") await briefProgress.click();
            else if (module.key === "funds" && await detail.locator("summary.hidden-summary").count()) {
              await detail.locator("xpath=ancestor::*[contains(concat(' ',normalize-space(@class),' '),' book-activity-row ')]").click();
            }
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
              const url = dashboardRequestTarget(request);
              if (matchesAction(url, "close-review") && url.searchParams.get("preview_digest") === digest) allowedCloseReviewRequests.add(request.url());
            };
            page.on("request", markAllowed);
            try {
              await page.getByRole("button", { name: "查看本次核对内容", exact: true }).click();
              const reply = await pending, review = await verifyReply(reply, module);
              assert.equal(dashboardResponseTarget(reply).searchParams.get("preview_digest"), digest, "review: requested another preview");
              assert.equal(review.preview_digest, digest, "review: displayed another preview");
              await page.locator(".close-review").waitFor();
              await checkOwnerLayout(module, "on_demand_review");
              assertNoBrowserFailures();
              await page.getByRole("button", { name: "收起本次核对", exact: true }).click();
              await verifyBriefVisible(mainPayload.data);
            } finally { page.off("request", markAllowed); }
          }
          // Layout completion includes the current collection/detail reads;
          // navigation must not cancel them and then report a successful page.
          await settleRequiredReads(module);
        }
      }
      assertNoBrowserFailures();
      return { status: "passed", company_id: company.id, period: company.period,
        pages: {}, layouts, navigation: navigationBase, local_actions: localActions,
        obsolete_cancellations: obsoleteCancellations, pending_api_requests: requestDiagnostics.pending() };
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
      await navigate(address(module));
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
      result[module.key] = {
        samples: timings,
        resource_samples: resourceSamples,
        ...stage9TimingSummary(timings, refreshTargetMs),
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
    const thresholdResults = refreshTargetMs === null ? null : stage9RefreshThresholdResults(result, samples);
    return {
      status: thresholdResults ? thresholdResults[refreshTargetMs].status : stage9MeasurementStatus(result, samples), target_ms: refreshTargetMs, timing_gate: refreshTargetMs !== null,
      ...(thresholdResults ? { threshold_results: thresholdResults } : {}),
      browser_environment: browserEnvironment, resource_failures: resourceFailures, company_id: company.id, period: company.period, warmups, sample_count: samples, pages: result,
      refresh_attempts: refreshAttempts,
      navigation: { ...navigationBase, cold_open_ms: coldOpen, company_switch: companySwitch },
    };
  } catch (error) {
    failureReport = {
      browser_environment: browserEnvironment,
      status: "failed", message: sanitize(error?.message || error),
      ...(!config.interaction_only && !config.navigation_only && !config.layout_only
        ? { target_ms: refreshTargetMs, timing_gate: refreshTargetMs !== null, ...(refreshTargetMs === null ? {} : { threshold_results: stage9RefreshThresholdResults(result, samples, false) }) } : {}),
      ...(retainedRefreshOnly ? { measurement_scope: "retained_refresh_state", default_page_acceptance: false } : {}),
      company_id: company.id, period: company.period, pages: result,
      interactions: interactionResults, interaction_attempts: interactionAttempts,
      refresh_attempts: refreshAttempts,
      navigation: partialNavigation, warmups, sample_count: samples,
      page_errors: pageErrors, api_failures: apiFailures, resource_failures: resourceFailures,
      layouts: layoutChecks, layout_progress: layoutProgress,
      local_actions: localActions, pending_api_requests: requestDiagnostics.pending(),
      obsolete_cancellations: obsoleteCancellations,
    };
    return failureReport;
  } finally {
    await closeStage9Browser(context, browser, failureReport);
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
      if (result.status !== "passed" && !(result.status === "diagnostic_complete" && result.diagnostic_only === true && result.sla_eligible === false)) process.exitCode = 1;
    } catch (error) {
      process.stdout.write(`${JSON.stringify({ status: "failed", message: sanitize(error?.message || error) })}\n`);
      process.exitCode = 1;
    }
  });
}

module.exports = { waitForStage9, waitForStage9Frames, selectStage9Option, closeStage9Browser, localReadMatchesSelection, localReplyIsObsolete, verifyLocalActionReplies, interactionReadUrl, interactionReadMultiset, interactionReadPlanReady, verifiedRequestRecord, verifyEmployeeRootRows, verifiedReadCycleReady, resultHasVisibleContent, observeInteraction, verifyExactInteractionTarget, verifySelectedFilter, verifyBriefGroup, obsoleteLocalCancellation, diagnosticUrl, createRequestDiagnostics, waitForLater, recordRefreshAttempt, verifyLocalRead, verifyBriefState, verifyLongTermAssets, verifyCollection, verifyVoucher, verifyVisibleVoucherRows, verifyMainPayload, formatFen, sumFen, employeeOutstandingFen, captureRefreshResources, readRefreshProjection, observeHotRefresh };
Object.assign(module.exports, { retainedRefreshKeys, retainedRefreshSelection, interactionScopeAllows, verifyRetainedRefreshState, readRetainedRefreshState, verifyRetainedResponseContract, retainedCollectionIdentities, verifyRetainedCollectionIdentities, retainedItemIdentity, verifyRetainedAuxiliaryComplete, verifyRetainedTargetBinding, verifiedFundsDetailCache });
Object.assign(module.exports, { createRetainedResponseBodies, verifyRetainedVisibleOrder, stage9RequestPageLimit, stage9TimingSummary, stage9RefreshTarget, stage9MeasurementStatus, stage9InteractionStatus, stage9RefreshThresholdResults });
Object.assign(module.exports, { voucherTailDiagnosticSelection });

Object.assign(module.exports, { verifyS9WirePayload, recordS9ActualWire });

Object.assign(module.exports, { dashboardReadTarget, dashboardRequestTarget, dashboardResponseTarget, observedDashboardResource, installDashboardReadObserver });
Object.assign(module.exports, { createStaticResourceObserver });
