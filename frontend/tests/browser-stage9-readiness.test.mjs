import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import vm from "node:vm";
import * as generatedValidators from "../src/api/generated/dashboardValidators.js";
const { retainedRefreshKeys, retainedRefreshSelection, interactionScopeAllows, verifyRetainedRefreshState, readRetainedRefreshState, verifyRetainedResponseContract, retainedCollectionIdentities, verifyRetainedCollectionIdentities, verifyRetainedAuxiliaryComplete, verifyRetainedTargetBinding, verifiedFundsDetailCache } = createRequire(import.meta.url)("./browser-stage9-hot-refresh.cjs");
const { createRetainedResponseBodies, verifyRetainedVisibleOrder, stage9RequestPageLimit, verifyCollection, stage9TimingSummary } = createRequire(import.meta.url)("./browser-stage9-hot-refresh.cjs");
const { voucherTailDiagnosticSelection } = createRequire(import.meta.url)("./browser-stage9-hot-refresh.cjs");

test("voucher tail diagnostic is explicit, narrow and cannot inherit formal sample counts", () => {
  const diagnostic = { diagnostic_voucher_tail: true, interaction_only: true, warmups: 1, repeats: 3 };
  assert.equal(voucherTailDiagnosticSelection({ interaction_only: true, warmups: 3, repeats: 30 }, {}), false);
  assert.equal(voucherTailDiagnosticSelection(diagnostic, {}), true);
  assert.equal(voucherTailDiagnosticSelection(diagnostic, { STAGE9_DIAGNOSTIC_VOUCHER_TAIL: "1" }), true);
  for (const changed of [{ diagnostic_voucher_tail: false }, { warmups: 3 }, { repeats: 30 }, { interaction_only: false }, { layout_only: true }, { navigation_only: "cold" }]) {
    assert.throws(() => voucherTailDiagnosticSelection({ ...diagnostic, ...changed }, { STAGE9_DIAGNOSTIC_VOUCHER_TAIL: "1" }));
  }
  assert.throws(() => voucherTailDiagnosticSelection(diagnostic, { STAGE9_RETAINED_REFRESH_ONLY: "1" }));
});

test("default refresh and other interactions use separate strict targets without losing slow samples", () => {
  const samples = [499.9, 500, 599.9, 600, 700];
  const before = [...samples];
  const refresh = stage9TimingSummary(samples, 500);
  const interaction = stage9TimingSummary(samples, 600);
  assert.equal(refresh.target_ms, 500);
  assert.equal(refresh.over_target_count, 4);
  assert.equal(interaction.target_ms, 600);
  assert.equal(interaction.over_target_count, 2);
  assert.equal(interaction.over_600_ms, 2);
  assert.equal(interaction.over_500_ms, 4);
  assert.equal(interaction.max_ms, 700);
  assert.equal(interaction.median_ms, 599.9);
  assert.deepEqual(samples, before);
  assert.throws(() => stage9TimingSummary(samples, 800), /unknown Stage 9/);
});

test("an incomplete interaction keeps finite slow timings and does not claim completed statistics", () => {
  const result = stage9TimingSummary([700, null, 610, undefined], 600, false);
  assert.equal(result.over_target_count, 2);
  assert.equal(result.over_500_ms, 2);
  assert.equal(result.median_ms, null);
  assert.equal(result.p95_ms, null);
  assert.equal(result.max_ms, null);
});

test("retained response bytes are captured before navigation and consumed only by their original generation", async () => {
  const bodies = createRetainedResponseBodies(); let navigated = false, reads = 0;
  const request = { url: () => "http://localhost/api/dashboard/brief?company_id=B&period=2026-01" };
  const reply = { request: () => request, url: request.url, body: () => {
    reads++; if (navigated) throw new Error("body navigated away"); return Promise.resolve(Buffer.from('{"identity":"original"}'));
  } };
  bodies.start(request, 7, "http://localhost/?company_id=B"); bodies.capture(reply);
  assert.equal(reads, 1);
  assert.throws(() => bodies.retire(7), /prior response validation/);
  navigated = true;
  await assert.rejects(bodies.read(reply, 8), /another navigation generation/);
  assert.deepEqual(await bodies.read(reply, 7), { identity: "original" }); bodies.retire(7);
  assert.equal(reads, 1);
});
test("owned response body and malformed JSON errors remain failures without an unhandled rejection or next-case leak", async () => {
  for (const failure of ["body", "json"]) {
    const bodies = createRetainedResponseBodies();
    const request = { url: () => "http://localhost/api/dashboard/business-status?company_id=B" };
    const reply = { request: () => request, url: request.url,
      body: () => failure === "body" ? Promise.reject(new Error("original body failure")) : Promise.resolve(Buffer.from("broken json")) };
    bodies.start(request, 2, "http://localhost/funds"); bodies.capture(reply);
    await Promise.resolve(); await Promise.resolve();
    await assert.rejects(bodies.read(reply, 2), failure === "body" ? /original body failure/ : /JSON|Unexpected token/);
    bodies.retire(2);
  }
});
test("a captured obsolete response is released only after explicit cancellation classification", async () => {
  const bodies = createRetainedResponseBodies();
  const request = { url: () => "http://localhost/api/dashboard/brief-group?company_id=B&section=activity" };
  const reply = { request: () => request, url: request.url, body: () => Promise.reject(new Error("canceled old member body")) };
  bodies.start(request, 4, "http://localhost/brief"); bodies.capture(reply);
  await Promise.resolve(); await Promise.resolve();
  assert.throws(() => bodies.retire(4), /prior response validation/);
  bodies.consumed(reply); bodies.retire(4);
});
test("root first twenty containing two category rows cannot override a verified three-row continuation DOM", () => {
  const read = (cursor, items) => ({ url: `http://localhost/api/dashboard/brief?company_id=B&period=2026-01&section=activity${cursor ? "&cursor=real-next" : ""}`,
    payload: { data: { collections: { activity: { items: items.map(group_key => ({ group_key })) } } } } });
  const before = retainedCollectionIdentities([read(false, ["category-A", "category-B"]), read(true, ["category-C"])]);
  verifyRetainedVisibleOrder(before["/api/dashboard/brief:activity"], 3, "brief selected category");
  assert.throws(() => verifyRetainedVisibleOrder(before["/api/dashboard/brief:activity"], 2, "brief"), /complete verified current/);
  const wrong = retainedCollectionIdentities([read(false, ["category-A", "category-B"]), read(true, ["wrong-C"])]);
  assert.throws(() => verifyRetainedCollectionIdentities(before, wrong), /changed verified visible/);
});
test("bounded cursor batch 500 is allowed only with an exact section/snapshot; every noncursor first page remains twenty", () => {
  const target = new URL("http://localhost/api/dashboard/employees?company_id=B&period=2026-01&section=employees&expected_version=snap&cursor=next&limit=500");
  assert.equal(stage9RequestPageLimit(target), 500);
  const page = { items: Array.from({ length: 500 }, (_, id) => ({ id })), page: { total_count: 520, filtered_count: 520, returned_count: 500, has_more: false, next_cursor: null } };
  verifyCollection(page, "continuation", { cursor: true, limit: stage9RequestPageLimit(target) });
  assert.throws(() => verifyCollection(page, "root"), /page exceeds|truncated/);
  for (const [key, value] of [["limit", "501"], ["limit", "0"], ["limit", "1.5"], ["limit", "-20"], ["section", ""], ["expected_version", ""], ["cursor", ""]]) {
    const changed = new URL(target); changed.searchParams.set(key, value); assert.throws(() => stage9RequestPageLimit(changed));
  }
  target.searchParams.delete("cursor"); assert.throws(() => stage9RequestPageLimit(target), /first page/);
  target.searchParams.set("limit", "20"); assert.equal(stage9RequestPageLimit(target), 20);
});

function retainedMemberRead(section, ids, { cursor = null, total = ids.length, hasMore = false, group = "same-group" } = {}) {
  return { url: `http://localhost/api/dashboard/brief-group?company_id=B&period=2026-01&expected_version=snap-B&section=${section}&group_key=${group}${cursor ? `&cursor=${cursor}` : ""}`,
    payload: { data: { section, collections: { members: { items: ids.map(id => ({ group_key: group, ...(section === "activity" ? { key: id } : { id }) })),
      page: { filtered_count: total, has_more: hasMore, next_cursor: hasMore ? "next-real-cursor" : null } } } } } };
}
test("brief members sharing one group retain every exact member identity and original response order", () => {
  for (const section of ["activity", "open_items"]) {
    const before = retainedCollectionIdentities([retainedMemberRead(section, ["member-2", "member-1"])]);
    assert.deepEqual(Object.values(before), [["member-2", "member-1"]]);
    const swapped = retainedCollectionIdentities([retainedMemberRead(section, ["member-1", "member-2"])]);
    assert.throws(() => verifyRetainedCollectionIdentities(before, swapped), /changed verified visible/);
  }
});
test("retained expanded target binds actual row index and ordered ID even when two rows have identical visible text", () => {
  const before = { ordered_ids: ["employee-A", "employee-B"], open_indices: [1], target_id: "employee-B", subject_id: null, snapshot: "snap" };
  verifyRetainedTargetBinding(before, structuredClone(before));
  for (const change of [{ ordered_ids: ["employee-B", "employee-A"], target_id: "employee-A" }, { open_indices: [0], target_id: "employee-A" },
    { subject_id: "other-subject" }, { snapshot: "other-snap" }])
    assert.throws(() => verifyRetainedTargetBinding(before, { ...before, ...change }), /expanded identity\/order\/subject/);
  verifyRetainedTargetBinding(before, { ...before, open_indices: [] }, true);
  assert.throws(() => verifyRetainedTargetBinding(before, { ...before, ordered_ids: ["other", "employee-B"], open_indices: [] }, true), /changed/);
});
test("detail completeness requires actual unique members equal total and a terminal cursor page", () => {
  const ids = Array.from({ length: 22 }, (_, index) => `member-${index}`);
  const first = retainedMemberRead("activity", ids.slice(0, 20), { total: 22, hasMore: true });
  const last = retainedMemberRead("activity", ids.slice(20), { cursor: "next-real-cursor", total: 22 });
  const complete = verifyRetainedAuxiliaryComplete([first, last]);
  assert.equal(Object.values(complete)[0].ids.length, 22);
  assert.throws(() => verifyRetainedAuxiliaryComplete([first]), /incomplete unique detail count/);
  assert.throws(() => verifyRetainedAuxiliaryComplete([first, retainedMemberRead("activity", [ids[0], ids[21]], { cursor: "next-real-cursor", total: 22 })]), /duplicate continuation identity/);
  assert.throws(() => verifyRetainedAuxiliaryComplete([last]), /no verified first page/);
  assert.throws(() => verifyRetainedAuxiliaryComplete([retainedMemberRead("activity", ids, { total: 22, hasMore: true })]), /terminal page/);
  assert.throws(() => verifyRetainedAuxiliaryComplete([first, retainedMemberRead("activity", ids.slice(20), { cursor: "next-real-cursor", total: 23 })]), /total changed/);
});
function retainedFundsCacheFixture() {
  const account = { type: "bank", account_id: "account-B" };
  const binding = { ordered_ids: ["movement-B"], open_indices: [0], target_id: "movement-B", subject_id: "subject-B", account, snapshot: "snap-B" };
  const main = { contract_verified: true, url: "http://localhost/api/dashboard/funds?company_id=B&period=2026-01",
    payload: { snapshot_version: "snap-B", data: { selected_movement_account: account } } };
  const detail = { contract_verified: true,
    url: "http://localhost/api/dashboard/business-status?company_id=B&period=2026-01&expected_version=snap-B&subject_id=subject-B&settlement_view=historical",
    payload: { snapshot_version: "snap-B", read_context: { company_id: "B" }, selected_period: { key: "2026-01" },
      data: { identity: { subject_id: "subject-B" }, settlement_view: "historical", detail_scope: null,
        collections: { settlement_events: { items: [{ id: "settlement-B" }], page: { filtered_count: 1, has_more: false, next_cursor: null } } } } } };
  return { main, detail, binding };
}
test("ordinary funds detail reuses only previously verified exact-target complete cache under this refresh's main snapshot", () => {
  const { main, detail, binding } = retainedFundsCacheFixture();
  assert.deepEqual(verifiedFundsDetailCache([detail], main, binding), [detail]);
  for (const change of [{ snapshot: "new-snapshot" }, { subject_id: "other-subject" }, { account: { type: "bank", account_id: "other-account" } },
    { target_id: "other-row" }, { open_indices: [] }])
    assert.throws(() => verifiedFundsDetailCache([detail], main, { ...binding, ...change }));
  assert.throws(() => verifiedFundsDetailCache([detail], { ...main, contract_verified: false }, binding), /current main was not contract verified/);
  assert.throws(() => verifiedFundsDetailCache([{ ...detail, contract_verified: false }], main, binding), /prior detail was not contract verified/);
  assert.throws(() => verifiedFundsDetailCache([], main, binding), /no previously verified detail/);
});
test("funds cache rejects wrong company month sub-scope and partial settlement continuation", () => {
  const { main, detail, binding } = retainedFundsCacheFixture();
  for (const [key, value] of [["company_id", "A"], ["period", "2026-02"], ["expected_version", "old-snap"], ["subject_id", "subject-A"], ["settlement_view", "current"], ["detail_scope_key", "unrelated"]]) {
    const changed = structuredClone(detail), url = new URL(changed.url); url.searchParams.set(key, value); changed.url = url.href;
    assert.throws(() => verifiedFundsDetailCache([changed], main, binding));
  }
  const partial = structuredClone(detail); partial.payload.data.collections.settlement_events.page.filtered_count = 21;
  partial.payload.data.collections.settlement_events.page.has_more = true;
  assert.throws(() => verifiedFundsDetailCache([partial], main, binding), /incomplete unique detail count/);
  const missing = structuredClone(detail); missing.payload.data.collections = {};
  assert.throws(() => verifiedFundsDetailCache([missing], main, binding), /missing required detail collection/);
});

test("retained refresh opt-in selects exactly seven new branches and the strengthened employee branch", () => {
  assert.equal(retainedRefreshSelection({ interaction_only: true }, {}), false);
  assert.equal(retainedRefreshSelection({ interaction_only: true }, { STAGE9_RETAINED_REFRESH_ONLY: "1" }), true);
  for (const config of [{}, { layout_only: true }, { interaction_only: true, layout_only: true }])
    assert.throws(() => retainedRefreshSelection(config, { STAGE9_RETAINED_REFRESH_ONLY: "1" }), /requires interaction_only/);
  assert.equal(retainedRefreshKeys.length, 8);
  assert.equal(new Set(retainedRefreshKeys).size, 8);
  assert(!retainedRefreshKeys.includes("brief_vouchers_all_complete"));
  assert(retainedRefreshKeys.includes("employees_automatic_continuation_refresh"));
});

test("retained refresh rejects replacing pre-refresh identities, counts, selected filter, mode, open target or complete content", () => {
  const before = { rows: ["employee-1 summary", "employee-21 summary"], selected: [["filter", "all", "all"]],
    expanded: [["", "employee-21 detail amounts"]], projection: "full business content and list mode", behavior: "retain_same_snapshot_visible_state" };
  verifyRetainedRefreshState(before, structuredClone(before), "employees");
  for (const change of [{ rows: before.rows.slice(0, 1) }, { rows: ["employee-2 summary", before.rows[1]] },
    { selected: [["filter", "in_period", "in period"]] }, { expanded: [] }, { expanded: [["", "employee-1 detail amounts"]] },
    { projection: "full business content and cards mode" }, { projection: "summary only" }])
    assert.throws(() => verifyRetainedRefreshState(before, { ...before, ...change }, "employees"), /lost previously visible/);
});

test("brief activity closes its real old detail on refresh while preserving the already selected category and summaries", () => {
  const before = { rows: ["group-2 amount"], selected: [["", null, "second category"]], expanded: [["", "group-2 amount"]],
    projection: "all business summary content minus explicitly closing member subtree", behavior: "close_activity_members_retain_category" };
  verifyRetainedRefreshState(before, { ...before, expanded: [] }, "activity");
  assert.throws(() => verifyRetainedRefreshState(before, before, "activity"), /expected expanded target/);
  assert.throws(() => verifyRetainedRefreshState(before, { ...before, expanded: [], selected: [] }, "activity"), /lost previously visible/);
  assert.throws(() => verifyRetainedRefreshState({ ...before, expanded: [] }, { ...before, expanded: [] }, "activity"), /not expanded before/);
});

test("retained read plan includes real auxiliary chain only under exact company month and snapshot", () => {
  const scope = { retained_refresh: true, action: "brief", company_id: "company-B", period: "2026-01", snapshot: "snapshot-B", quarter: "2026-Q1" };
  const group = new URL("http://localhost/api/dashboard/brief-group?company_id=company-B&period=2026-01&expected_version=snapshot-B&section=activity&group_key=real-group");
  assert(interactionScopeAllows(group, scope));
  for (const [key, value] of [["company_id", "company-A"], ["period", "2026-02"], ["expected_version", "snapshot-A"]]) {
    const changed = new URL(group); changed.searchParams.set(key, value); assert(!interactionScopeAllows(changed, scope));
  }
  assert(!interactionScopeAllows(group, { ...scope, retained_refresh: false }));
  assert(!interactionScopeAllows(group, { ...scope, action: "employees" }));
  assert(!interactionScopeAllows(group, { ...scope, action: "funds" }));
  const detail = new URL(group); detail.pathname = "/api/dashboard/business-status";
  assert(interactionScopeAllows(detail, { ...scope, action: "funds" }));
  const reports = new URL("http://localhost/api/dashboard/quarterly-report?company_id=company-B&year=2026&quarter=1");
  assert(interactionScopeAllows(reports, { ...scope, action: "quarterly-report" }));
  reports.searchParams.set("quarter", "2"); assert(!interactionScopeAllows(reports, { ...scope, action: "quarterly-report" }));
});

test("retained auxiliary response cannot pass just a schema version and plausible scope", () => {
  for (const action of ["brief-group", "business-status", "brief", "funds", "employees", "assets", "quarterly-report", "context"])
    assert.throws(() => verifyRetainedResponseContract(generatedValidators, new URL(`http://localhost/api/dashboard/${action}`),
      { schema_version: 1, company_id: "company-B", snapshot_version: "snapshot-B", data: {} }), /generated response contract rejected/);
  assert.throws(() => verifyRetainedResponseContract(generatedValidators, new URL("http://localhost/api/dashboard/unknown"), {}), /generated response contract rejected/);
});

test("actual retained IDs replace a superseded selection and append only verified cursor pages", () => {
  const read = (suffix, ids) => ({ url: `http://localhost/api/dashboard/employees?company_id=B&period=2026-01${suffix}`,
    payload: { data: { collections: { employees: { items: ids.map(employee_id => ({ employee_id })) } } } } });
  const before = retainedCollectionIdentities([read("&employee_filter=in_period", ["old-filter-only"]),
    read("&section=employees&employee_filter=all", ["employee-1"]), read("&section=employees&cursor=real-cursor&employee_filter=all", ["employee-21"])]);
  assert.deepEqual(before, { "/api/dashboard/employees:employees": ["employee-1", "employee-21"] });
  verifyRetainedCollectionIdentities(before, retainedCollectionIdentities([read("&employee_filter=all", ["employee-1"]),
    read("&section=employees&cursor=real-cursor&employee_filter=all", ["employee-21"])]));
  assert.throws(() => verifyRetainedCollectionIdentities(before, retainedCollectionIdentities([read("&employee_filter=all", ["employee-1"])])), /changed verified visible collection identities/);
  assert.throws(() => verifyRetainedCollectionIdentities(before, retainedCollectionIdentities([read("&employee_filter=all", ["employee-1", "different-ID-same-label"])])), /changed verified visible collection identities/);
  assert.throws(() => retainedCollectionIdentities([read("", [""])]), /missing actual collection identity/);
});

test("activity intentional close exempts only its member resource, never main or another module", () => {
  const before = { "/api/dashboard/brief-group:activity:members": ["old-member"], "/api/dashboard/brief:activity": ["visible-group"] };
  verifyRetainedCollectionIdentities(before, { "/api/dashboard/brief:activity": ["visible-group"] }, { closeActivityMembers: true });
  assert.throws(() => verifyRetainedCollectionIdentities(before, {}, { closeActivityMembers: true }), /changed verified visible/);
  assert.throws(() => verifyRetainedCollectionIdentities(before, { "/api/dashboard/brief:activity": ["visible-group"] }), /changed verified visible/);
  assert.throws(() => verifyRetainedCollectionIdentities({ "/api/dashboard/brief-group:open_items:members": ["real-open-member"] }, {}, { closeActivityMembers: true }), /changed verified visible/);
});

test("retained browser endpoint requires both auxiliary fetch and completed download with exact real scope", () => {
  const url = "http://localhost/api/dashboard/brief-group?company_id=B&expected_version=snap-B&group_key=real-group&period=2026-01&section=open_items";
  const scope = { action: "brief", company_id: "B", period: "2026-01", snapshot: "snap-B", retained_refresh: true };
  const selection = { cycle_id: "retained", scope, require_new_response: true,
    read_plan: { cycle_id: "retained", scope: { ...scope }, required: [{ url, count: 1 }] } };
  const fetches = [{ url, start: 11, state: "response", status: 200 }], downloads = [{ name: url, startTime: 11, responseEnd: 20 }];
  assert(interactionReadPlanReady(selection, fetches, downloads, 10));
  assert(!interactionReadPlanReady(selection, fetches, [], 10));
  assert(!interactionReadPlanReady(selection, [{ ...fetches[0], status: 500 }], downloads, 10));
  const changed = new URL(url); changed.searchParams.set("expected_version", "old-snap");
  const stale = { ...selection, read_plan: { ...selection.read_plan, required: [{ url: changed.href, count: 1 }] } };
  assert(!interactionReadPlanReady(stale, [{ ...fetches[0], url: changed.href }], [{ ...downloads[0], name: changed.href }], 10));
});

test("activity projection removes only the explicitly closing real member subtree and retains every other business amount", () => {
  const detail = { innerText: "real member amount 17.00" };
  let open = true;
  const row = { id: "", getClientRects: () => [1], querySelector: () => null, closest: () => null,
    get innerText() { return `group-2 200.00${open ? `\n${detail.innerText}` : ""}`; } };
  const selected = { id: "category-2", innerText: "category-2 200.00", getClientRects: () => [1], getAttribute: () => null };
  const root = { querySelectorAll(selector) {
    if (selector === ".event-row") return [row];
    if (selector === "#activity .group-members") return open ? [detail] : [];
    if (selector.startsWith("details[open]")) return open ? [row] : [];
    if (selector.includes('[aria-pressed="true"]')) return [selected];
    return [];
  } };
  const document = { querySelector: () => root, defaultView: { getComputedStyle: () => ({ visibility: "visible" }) } };
  const selection = { root: ".brief-content", rows: ".event-row", behavior: "close_activity_members_retain_category",
    projection: JSON.stringify({ content: ["headline 999.00", `group-2 200.00\n${detail.innerText}`], controls: [] }) };
  const before = readRetainedRefreshState(document, selection);
  open = false;
  const after = readRetainedRefreshState(document, { ...selection, projection: JSON.stringify({ content: ["headline 999.00", "group-2 200.00"], controls: [] }) });
  verifyRetainedRefreshState(before, after, "activity");
  const damaged = readRetainedRefreshState(document, { ...selection, projection: JSON.stringify({ content: ["headline 998.00", "group-2 200.00"], controls: [] }) });
  assert.throws(() => verifyRetainedRefreshState(before, damaged, "activity"), /lost previously visible/);
});

const { waitForStage9, waitForStage9Frames, selectStage9Option, closeStage9Browser, localReadMatchesSelection, localReplyIsObsolete, verifyLocalActionReplies, interactionReadUrl, interactionReadMultiset, interactionReadPlanReady, verifiedReadCycleReady, verifiedRequestRecord, verifyEmployeeRootRows, resultHasVisibleContent, observeInteraction, verifyExactInteractionTarget, recordRefreshAttempt, verifySelectedFilter, verifyBriefGroup, obsoleteLocalCancellation, diagnosticUrl, createRequestDiagnostics, readRefreshProjection, observeHotRefresh, verifyLongTermAssets, sumFen, employeeOutstandingFen } = createRequire(import.meta.url)("./browser-stage9-hot-refresh.cjs");

test("stage9 frame wait rejects when RAF never calls back, with its action and phase", async () => {
  const page = { evaluate: operation => vm.runInNewContext(`(${operation.toString()})()`, {
    requestAnimationFrame() {},
  }) };
  await assert.rejects(waitForStage9Frames(page, "employees/action 11: stable frames", Date.now() + 20),
    /employees\/action 11: stable frames: stage9 wait deadline exceeded/);
  const ready = { evaluate: operation => vm.runInNewContext(`(${operation.toString()})()`, {
    requestAnimationFrame: callback => callback(),
  }) };
  await waitForStage9Frames(ready, "normal frames", Date.now() + 1000);
});

test("stage9 selector bounds the real helper's continuously changing scroll calibration", async () => {
  let frames = 0;
  const callbacks = [], environment = { scrollX: 0, scrollY: 0, node: { focus() {} },
    requestAnimationFrame: callback => callbacks.push(callback) };
  const trigger = {
    scrollIntoViewIfNeeded: async () => {},
    evaluate: operation => {
      const pending = vm.runInNewContext(`(${operation.toString()})(node)`, environment);
      // Scroll changes across every observed frame, then RAF stops entirely.
      // No persistent asynchronous loop remains after the rejection.
      for (let index = 0; index < 12; index++) {
        environment.scrollY += 1; frames += 1; callbacks.shift()();
      }
      return pending;
    },
  };
  const page = { getByRole: () => trigger, keyboard: { press: () => { throw new Error("unstable selector must not select"); } } };
  await assert.rejects(selectStage9Option(page, "筛选员工", "all", Date.now() + 20),
    /select 筛选员工=all: stage9 wait deadline exceeded/);
  assert.equal(frames, 12);
});

test("stage9 unfinished response bodies reject without inventing verified selection proof", async () => {
  for (const phase of ["response body finish", "response JSON body"]) {
    const replies = [{ reply: { finished: () => new Promise(() => {}), json: () => new Promise(() => {}) } }];
    const proofs = [];
    const validation = () => verifyLocalActionReplies(replies, {
      isCurrent: () => true,
      isObsolete: async ({ reply }) => { if (phase === "response body finish") await reply.finished(); return false; },
      verify: ({ reply }) => reply.json(),
      onCurrent: async entry => proofs.push(entry),
    });
    await assert.rejects(waitForStage9(validation, `assets/action 7: ${phase}`, Date.now() + 20),
      error => error.message === `assets/action 7: ${phase}: stage9 wait deadline exceeded`);
    assert.deepEqual(proofs, []);
  }
});

test("stage9 deadlines clear timers after success, rejection and timeout, and retain original errors", async () => {
  let now = 100, timerId = 0;
  const active = new Map(), cleared = [];
  const wait = vm.runInNewContext(`(${waitForStage9.toString()})`, {
    assert, Date: { now: () => now },
    setTimeout: (callback, milliseconds) => { const id = ++timerId; active.set(id, { callback, milliseconds }); return id; },
    clearTimeout: id => { cleared.push(id); active.delete(id); },
  });
  const payload = { data: { employee_filter: "in_period" } };
  assert.equal(await wait(() => payload, "validated response", 200), payload);
  assert.equal(active.size, 0);
  const original = new Error("response violated its own contract");
  await assert.rejects(wait(() => Promise.reject(original), "validation", 200), error => error === original);
  assert.equal(active.size, 0);
  const pending = wait(() => new Promise(() => {}), "settle response finish", 150);
  const rejection = assert.rejects(pending, /settle response finish: stage9 wait deadline exceeded/);
  assert.equal(active.values().next().value.milliseconds, 50);
  now = 150; active.values().next().value.callback(); await rejection;
  assert.equal(active.size, 0); assert.equal(cleared.length, 3);
  let called = false;
  await assert.rejects(wait(() => { called = true; }, "remaining local deadline", 150), /deadline exceeded/);
  assert.equal(called, false); assert.equal(timerId, 3);
});

test("failed attempt timing cannot hang or erase partial evidence and the original error", async () => {
  const attempts = [], original = new Error("current selected response mismatch");
  await assert.rejects(recordRefreshAttempt(attempts, { scenario: "employee selection", kind: "sample", iteration: 1 }, async attempt => {
    attempt.phase = "validate"; attempt.elapsed_ms = 42; attempt.verified_reads = [{ section: "employees" }];
    throw original;
  }, () => new Promise(() => {})), error => error === original);
  assert.equal(attempts[0].error.message, original.message);
  assert.match(attempts[0].timing_error, /failed attempt timing: stage9 wait deadline exceeded/);
  assert.equal(attempts[0].phase, "validate"); assert.equal(attempts[0].elapsed_ms, 42);
  assert.deepEqual(attempts[0].verified_reads, [{ section: "employees" }]);
});

test("browser cleanup attempts both public closes and retains an existing failed report", async () => {
  const calls = [], original = { status: "failed", message: "selected body mismatch", layouts: [{ page: "employees" }] };
  const context = { close: async () => { calls.push("context"); throw new Error("context connection closed"); } };
  const browser = { close: async () => { calls.push("browser"); } };
  await closeStage9Browser(context, browser, original);
  assert.deepEqual(calls, ["context", "browser"]);
  assert.equal(original.message, "selected body mismatch"); assert.equal(original.status, "failed");
  assert.deepEqual(original.layouts, [{ page: "employees" }]);
  assert.deepEqual(original.cleanup_errors, ["context connection closed"]);
  await assert.rejects(closeStage9Browser(context, browser, null), /context connection closed/);
});

function localSelectionFixture(page = "employees", section = page) {
  const scope = "company_id=synthetic&period=2016-12";
  const action = { action_id: 11, page, section,
    before: { url: `https://synthetic.invalid/${page}?${scope}`, brief_view: "按业务", movement_account_index: 0 },
    after: { url: `https://synthetic.invalid/${page}?${scope}&${page === "assets" ? "asset_filter=fixed" : "employee_filter=in_period"}`,
      filters: [{ label: "筛选员工", value: "in_period" }, { label: "筛选资产", value: "fixed" }], brief_view: "按凭证",
      funds_view: "fund-detail-tab-book", movement_account_index: 1, statement_account_id: "bank-2",
      movement_account: { movement_account_type: "bank", movement_account_id: "current" } },
  };
  const record = { started_action: 11, url: `https://synthetic.invalid/api/dashboard/${page}?${scope}&section=${section}&expected_version=snapshot&${page === "assets" ? "asset_filter=fixed" : "employee_filter=in_period"}` };
  return { action, record };
}

test("local filter completion binds exact current selection and a new first-page Request, not listener entrance", () => {
  for (const page of ["employees", "assets"]) {
    const { action, record } = localSelectionFixture(page);
    assert(localReadMatchesSelection(record, action, "snapshot", page, { fresh: true }));
    const field = page === "employees" ? "employee_filter" : "asset_filter", current = page === "employees" ? "in_period" : "fixed";
    for (const mutate of [
      r => { r.url = r.url.replace(`${field}=${current}`, `${field}=${page === "employees" ? "employment_active" : "active"}`) + "&cursor=old-tail"; },
      r => { r.started_action = 10; },
      r => { r.started_action = null; },
      r => { r.url += "&cursor=current-tail"; },
      r => { r.url = r.url.replace("company_id=synthetic", "company_id=other"); },
      r => { r.url = r.url.replace("2016-12", "2016-11"); },
      r => { r.url = r.url.replace("expected_version=snapshot", "expected_version=other"); },
      r => { r.url = r.url.replace(`section=${page}`, "section=unrelated"); },
      r => { r.url = r.url.replace("synthetic.invalid", "other.invalid"); },
      r => { r.url += `&${page === "employees" ? "employee_id" : "asset_id"}=unrelated`; },
    ]) {
      const wrong = structuredClone(record); mutate(wrong);
      assert.equal(localReadMatchesSelection(wrong, action, "snapshot", page, { fresh: true }), false);
    }
    const tail = { ...record, url: record.url + "&cursor=current-tail" };
    assert(localReadMatchesSelection(tail, action, "snapshot", page), "current automatic tails still receive validation");
    assert(localReadMatchesSelection({ ...record, started_action: 10 }, action, "snapshot", page, { cached: true }), "previously validated exact cache is separate from a new read");
  }
});

test("local account/view and voucher paging evidence use the selected resource and preserve cache-only actions", async () => {
  const funds = localSelectionFixture("funds", "movements");
  funds.record.url += "&movement_account_type=bank&movement_account_id=current";
  assert(localReadMatchesSelection(funds.record, funds.action, "snapshot", "movements", { fresh: true }));
  assert.equal(localReadMatchesSelection({ ...funds.record, url: funds.record.url.replace("movement_account_id=current", "movement_account_id=old") }, funds.action, "snapshot", "movements"), false);
  assert.equal(localReadMatchesSelection({ ...funds.record, url: funds.record.url.replace("movement_account_type=bank", "movement_account_type=cash") }, funds.action, "snapshot", "movements"), false);
  funds.action.after.funds_view = "fund-detail-tab-bank";
  assert.equal(localReadMatchesSelection(funds.record, funds.action, "snapshot"), false);
  funds.record.url = funds.record.url.replace("section=movements", "section=statements") + "&statement_account_id=bank-2";
  assert(localReadMatchesSelection(funds.record, funds.action, "snapshot", "statements"));
  assert.equal(localReadMatchesSelection({ ...funds.record, url: funds.record.url.replace("statement_account_id=bank-2", "statement_account_id=bank-1") }, funds.action, "snapshot", "statements"), false);
  const brief = localSelectionFixture("brief", "vouchers");
  assert(localReadMatchesSelection(brief.record, brief.action, "snapshot", "vouchers", { fresh: true }));
  const next = { ...brief.record, url: brief.record.url + "&cursor=next-page" };
  assert(localReadMatchesSelection(next, brief.action, "snapshot", "vouchers"));
  assert.equal(localReadMatchesSelection(next, brief.action, "snapshot", "vouchers", { fresh: true }), false);
  assert.equal(localReadMatchesSelection({ ...next, started_action: 10 }, brief.action, "snapshot", "vouchers"), false);
  brief.action.after.brief_view = "按业务";
  assert.equal(localReadMatchesSelection(next, brief.action, "snapshot", "vouchers"), false);
  const untouched = () => { throw new Error("local-only action must not invent a network requirement"); };
  assert.deepEqual(await verifyLocalActionReplies([], { isCurrent: untouched, verify: untouched, isObsolete: untouched, onCurrent: untouched }), []);
});

test("successful queued old-filter body is validated but cannot satisfy or fail the current filter assertion", async () => {
  const { action, record } = localSelectionFixture();
  const old = { record: { ...record, url: record.url.replace("employee_filter=in_period", "employee_filter=employment_active") + "&cursor=old-tail" }, filter: "employment_active" };
  const current = { record, filter: "in_period" };
  const validated = [], proofs = [];
  const result = await verifyLocalActionReplies([old, current], {
    isCurrent: entry => localReadMatchesSelection(entry.record, action, "snapshot", "employees"),
    isObsolete: async () => false,
    verify: async entry => { validated.push(entry.filter); return { data: { employee_filter: entry.filter } }; },
    onCurrent: async (entry, payload) => {
      verifySelectedFilter(action.after, new URL(entry.record.url), payload, "employees", "in_period"); proofs.push(entry);
    },
  });
  assert.deepEqual(validated, ["in_period", "employment_active"]);
  assert.deepEqual(proofs, [current]); assert.equal(result.length, 1);
  assert.equal(result[0].data.employee_filter, "in_period");
  await assert.rejects(verifyLocalActionReplies([old, current], {
    isCurrent: entry => entry === current, isObsolete: async () => false,
    verify: async entry => { if (entry === old) throw new Error("old body violates its own contract"); return {}; }, onCurrent: async () => {},
  }), /old body violates its own contract/);
});

test("aborted old body is skipped only after exact current replacement proof; current aborts and failed replacements reject", async () => {
  const fixture = cancellationFixture({ page: "employees", section: "employees", beforeQuery: "employee_filter=in_period", afterQuery: "employee_filter=all" });
  fixture.action.section = "employees";
  const old = { kind: "old" }, current = { kind: "current" };
  const calls = [];
  const options = {
    isCurrent: entry => entry === current,
    isObsolete: async entry => entry === old && obsoleteLocalCancellation(fixture.failure, fixture.action, "snapshot") !== null,
    verify: async entry => { calls.push(entry.kind); if (entry === old) throw new Error("unproved abort"); return {}; },
    onCurrent: async () => { fixture.action.verified_reads = [{ action_id: 7, section: "employees", url: fixture.failure.url.replace("employee_filter=in_period", "employee_filter=all").replace("&cursor=page21", "") }]; },
  };
  await verifyLocalActionReplies([old, current], options); assert.deepEqual(calls, ["current"]);
  fixture.action.verified_reads = [];
  await assert.rejects(verifyLocalActionReplies([old], options), /unproved abort/);
  await assert.rejects(verifyLocalActionReplies([old, current], { ...options, verify: async () => { throw new Error("current body aborted"); } }), /current body aborted/);
  assert.deepEqual(fixture.action.verified_reads, []);
});

test("asset filter obsolete cancellation requires exact identity, scope, cursor and verified current filter", () => {
  const { failure, action } = cancellationFixture({ page: "assets", section: "assets", beforeQuery: "asset_filter=active", afterQuery: "asset_filter=fixed" });
  failure.url += "&asset_filter=active"; failure.request.url = failure.url; action.pending_before[0].url = failure.url;
  action.verified_reads = [{ action_id: 7, section: "assets", url: failure.url.replace("asset_filter=active", "asset_filter=fixed").replace("&cursor=page21", "") }];
  assert.equal(obsoleteLocalCancellation(failure, action, "snapshot"), "previous_asset_filter_page");
  const initial = structuredClone({ failure, action });
  initial.failure.request.started_action = initial.action.pending_before[0].started_action = null;
  assert.equal(obsoleteLocalCancellation(initial.failure, initial.action, "snapshot"), "previous_asset_filter_page", "initial auto tail still has exact recorded pending identity");
  initial.action.pending_before = [];
  assert.equal(obsoleteLocalCancellation(initial.failure, initial.action, "snapshot"), null, "null action number alone is never obsolete proof");
  const queued = structuredClone({ failure, action });
  queued.action.pending_before = []; queued.failure.request.started_action = 7;
  assert.equal(obsoleteLocalCancellation(queued.failure, queued.action, "snapshot"), "previous_asset_filter_page", "old URL-bound tail queued inside the action is independently associated");
  queued.failure.request.started_page_url = queued.action.after.url;
  assert.equal(obsoleteLocalCancellation(queued.failure, queued.action, "snapshot"), null);
  const delayed = structuredClone({ failure, action });
  delayed.action.status = "passed"; delayed.action.pending_after = [{ ...delayed.failure.request, state: "pending" }];
  delayed.failure.request.finished_action = null;
  assert.equal(obsoleteLocalCancellation(delayed.failure, delayed.action, "snapshot"), "previous_asset_filter_page");
  delayed.action.pending_after = [];
  assert.equal(obsoleteLocalCancellation(delayed.failure, delayed.action, "snapshot"), null);
  for (const mutate of [
    f => { f.action.verified_reads = []; },
    f => { f.failure.error = "net::ERR_CONNECTION_RESET"; },
    f => { f.failure.request.started_page_url = f.action.after.url; },
    f => { f.failure.request.finished_page_url = f.action.before.url; },
    f => { f.failure.request.request_id++; },
    f => { f.failure.url += "&asset_id=unrelated"; f.failure.request.url = f.failure.url; f.action.pending_before[0].url = f.failure.url; },
    f => { f.action.verified_reads[0].url += "&project_id=unrelated"; },
    f => { f.action.verified_reads[0].url = f.action.verified_reads[0].url.replace("expected_version=snapshot", "expected_version=other"); },
    f => { f.action.verified_reads[0].action_id = 6; },
  ]) {
    const wrong = structuredClone({ failure, action }); mutate(wrong);
    assert.equal(obsoleteLocalCancellation(wrong.failure, wrong.action, "snapshot"), null);
  }
});

function cancellationFixture({ page = "funds", section = "statements", beforeQuery = "funds_view=bank", afterQuery = "funds_view=book" } = {}) {
  const route = page === "brief" ? "/" : `/${page}`;
  const root = "https://synthetic.invalid", scope = "company_id=synthetic&period=2016-12";
  const beforeUrl = `${root}${route}?${scope}&${beforeQuery}`;
  const afterUrl = `${root}${route}?${scope}&${afterQuery}`;
  const requestUrl = `${root}/api/dashboard/${page}?${scope}&section=${section}&cursor=page21&expected_version=snapshot&movement_account_type=bank&movement_account_id=old&employee_filter=in_period`;
  const pending = { request_id: 15, url: requestUrl, started_action: 6, started_page_url: beforeUrl, state: "pending" };
  const failure = { path: `/api/dashboard/${page}`, error: "net::ERR_ABORTED", url: requestUrl,
    request: { ...pending, state: "failed", finished_action: 7, finished_page_url: afterUrl } };
  const action = { action_id: 7, page, section: null, before: { url: beforeUrl, funds_view: "fund-detail-tab-bank", movement_account_index: 0, active_section: "本月发生" },
    after: { url: afterUrl, funds_view: "fund-detail-tab-book", movement_account_index: 0, active_section: "应收应付" }, pending_before: [pending], verified_reads: [],
    verified_cached_reads: [{ action_id: 7, section: page === "brief" ? "open_items" : "movements",
      url: requestUrl.replace(`section=${section}`, `section=${page === "brief" ? "open_items" : "movements"}`) }] };
  action.before.movement_account = { movement_account_type: "bank", movement_account_id: "old" };
  action.after.movement_account = { movement_account_type: "bank", movement_account_id: "new" };
  if (["employees", "assets"].includes(page)) {
    const field = page === "employees" ? "employee_filter" : "asset_filter", label = page === "employees" ? "筛选员工" : "筛选资产";
    for (const selection of [action.before, action.after]) selection.filters = [{ label,
      value: new URL(selection.url).searchParams.get(field) ?? (page === "employees" ? "employment_active" : "active") }];
  }
  return { failure, action };
}

function queuedSelectionAbort(page) {
  const employee = page === "employees", asset = page === "assets", section = page === "funds" ? "movements" : page;
  const field = employee ? "employee_filter" : asset ? "asset_filter" : "movement_account_id";
  const previous = employee ? "employment_active" : asset ? "active" : "old", current = employee ? "in_period" : asset ? "fixed" : "new";
  const f = cancellationFixture({ page, section,
    beforeQuery: page === "funds" ? "funds_view=book" : "",
    afterQuery: page === "funds" ? "funds_view=book" : `${field}=${current}` });
  f.action.section = section;
  if (page === "funds") {
    f.action.before.funds_view = f.action.after.funds_view = "fund-detail-tab-book";
    f.action.after.movement_account_index = 1;
  }
  const target = new URL(`https://synthetic.invalid/api/dashboard/${page}?company_id=synthetic&period=2016-12&section=${section}&expected_version=snapshot&cursor=old-tail`);
  target.searchParams.set(field, previous);
  if (page === "funds") target.searchParams.set("movement_account_type", "bank");
  f.failure.url = f.failure.request.url = target.href;
  f.failure.request.started_action = f.action.action_id;
  f.action.pending_before = [];
  target.searchParams.delete("cursor"); target.searchParams.set(field, current);
  const first = { action_id: f.action.action_id, section, url: target.href };
  f.action.verified_reads = [first]; f.action.verified_cached_reads = [];
  return { ...f, first, field, previous, current,
    reason: employee ? "previous_employee_filter_page" : asset ? "previous_asset_filter_page" : "previous_movement_account_page" };
}

test("recorded same-action old selection aborts skip unfinished bodies only after the new first page is fully verified", async () => {
  for (const page of ["employees", "assets", "funds"]) {
    const f = queuedSelectionAbort(page), old = { kind: "old" }, current = { kind: "current" };
    let oldFinishCalls = 0;
    f.action.verified_reads = [];
    const obsolete = reply => localReplyIsObsolete(reply, {
      failureFor: candidate => candidate === old ? f.failure : null,
      classifyObsolete: failure => obsoleteLocalCancellation(failure, f.action, "snapshot") !== null,
      finish: async candidate => { if (candidate === old) { oldFinishCalls++; return new Promise(() => {}); } },
    });
    await assert.rejects(obsolete(old), /unapproved response failure net::ERR_ABORTED/);
    const validated = [];
    const payloads = await waitForStage9(() => verifyLocalActionReplies([old, current], {
      isCurrent: entry => entry === current, isObsolete: obsolete,
      verify: async entry => { validated.push(entry); return { data: { [f.field]: f.current } }; },
      onCurrent: async (_, payload) => {
        assert.equal(payload.data[f.field], f.current);
        f.action.verified_reads.push(f.first);
      },
    }), `${page}: queued abort validation`, Date.now() + 1000);
    assert.deepEqual(validated, [current]); assert.equal(payloads.length, 1); assert.equal(oldFinishCalls, 0);
    assert.equal(obsoleteLocalCancellation(f.failure, f.action, "snapshot"), f.reason);
  }
});

test("same-action abort exclusions reject current or unproved bodies across employee, asset and account scopes", async () => {
  for (const page of ["employees", "assets", "funds"]) {
    const mutations = [
      f => { f.action.verified_reads = []; },
      f => { f.action.verified_reads[0].url += "&cursor=current-tail"; },
      f => { f.failure.url = f.failure.url.replace("cursor=old-tail", "cursor="); f.failure.request.url = f.failure.url; },
      f => { f.failure.url = f.failure.url.replace(`${f.field}=${f.previous}`, `${f.field}=${f.current}`); f.failure.request.url = f.failure.url; },
      f => { f.failure.url = f.failure.url.replace("company_id=synthetic", "company_id=another"); f.failure.request.url = f.failure.url; },
      f => { f.failure.url = f.failure.url.replace("2016-12", "2016-11"); f.failure.request.url = f.failure.url; },
      f => { f.failure.url = f.failure.url.replace("expected_version=snapshot", "expected_version=other"); f.failure.request.url = f.failure.url; },
      f => { f.failure.url += "&asset_id=unrelated"; f.failure.request.url = f.failure.url; },
      f => { f.failure.request.url += "&tracking_mismatch=yes"; },
      f => { f.failure.error = "net::ERR_CONNECTION_RESET"; },
      f => { f.failure.request.started_action++; },
      f => { f.failure.request.finished_action++; },
      f => {
        if (page === "funds") f.action.after.movement_account = { ...f.action.before.movement_account };
        else f.action.after.filters = structuredClone(f.action.before.filters);
      },
    ];
    for (const mutate of mutations) {
      const f = queuedSelectionAbort(page); mutate(f);
      assert.equal(obsoleteLocalCancellation(f.failure, f.action, "snapshot"), null);
      await assert.rejects(localReplyIsObsolete({}, {
        failureFor: () => f.failure,
        classifyObsolete: failure => obsoleteLocalCancellation(failure, f.action, "snapshot") !== null,
        finish: () => { throw new Error("recorded unapproved failure must reject before its body wait"); },
      }), /unapproved response failure/);
    }
  }
});

test("ordinary old responses finish and validate, and a cancellation arriving during finish still needs exact replacement proof", async () => {
  const f = queuedSelectionAbort("employees"), reply = {}, validated = [];
  const options = { failureFor: () => null, classifyObsolete: () => { throw new Error("ordinary response has no cancellation proof"); },
    finish: async () => validated.push("finished") };
  await verifyLocalActionReplies([reply], { isCurrent: () => false,
    isObsolete: entry => localReplyIsObsolete(entry, options),
    verify: async () => { validated.push("validated"); return {}; }, onCurrent: () => { throw new Error("old body cannot prove current selection"); } });
  assert.deepEqual(validated, ["finished", "validated"]);
  let failure = null;
  assert.equal(await localReplyIsObsolete(reply, { failureFor: () => failure,
    classifyObsolete: value => obsoleteLocalCancellation(value, f.action, "snapshot") !== null,
    finish: async () => { failure = f.failure; } }), true);
});

test("only a pending previous-view cursor cancelled by the next local view action is obsolete", () => {
  const { failure, action } = cancellationFixture();
  assert.equal(obsoleteLocalCancellation(failure, action, "snapshot"), "previous_funds_view_page");
  assert.equal(failure.error, "net::ERR_ABORTED", "classification retains the original failed request");
  for (const change of [
    f => { f.failure.error = "net::ERR_CONNECTION_RESET"; },
    f => { f.failure.status = 500; },
    f => { f.failure.request.started_action = 7; },
    f => { f.failure.request.finished_action = 8; },
    f => { f.action.pending_before = []; },
    f => { f.action.after.funds_view = f.action.before.funds_view; },
    f => { f.failure.url = f.failure.url.replace("cursor=page21", ""); f.failure.request.url = f.failure.url; f.action.pending_before[0].url = f.failure.url; },
    f => { f.failure.url = f.failure.url.replace("section=statements", "section=movements"); f.failure.request.url = f.failure.url; f.action.pending_before[0].url = f.failure.url; },
    f => { f.action.after.url = f.action.after.url.replace("2016-12", "2016-11"); },
    f => { f.action.verified_cached_reads = []; },
    f => { f.failure.request.started_page_url += "&employee_id=another"; },
    f => { f.action.verified_cached_reads[0].url = f.action.verified_cached_reads[0].url.replace("expected_version=snapshot", "expected_version=other"); },
  ]) {
    const f = cancellationFixture(); change(f);
    assert.equal(obsoleteLocalCancellation(f.failure, f.action, "snapshot"), null);
  }
  assert.equal(obsoleteLocalCancellation(failure, action, "another-snapshot"), null);
});

test("previous movement-account page needs changed UI selection and a verified current replacement", () => {
  const { failure, action } = cancellationFixture({ section: "movements", beforeQuery: "funds_view=book", afterQuery: "funds_view=book" });
  action.before.funds_view = action.after.funds_view = "fund-detail-tab-book";
  action.section = "movements"; action.after.movement_account_index = 1;
  assert.equal(obsoleteLocalCancellation(failure, action, "snapshot"), null);
  action.verified_reads = [{ action_id: 7, section: "movements", url: failure.url.replace("movement_account_id=old", "movement_account_id=new").replace("&cursor=page21", "") }];
  assert.equal(obsoleteLocalCancellation(failure, action, "snapshot"), "previous_movement_account_page");
  action.verified_reads[0].url = failure.url;
  assert.equal(obsoleteLocalCancellation(failure, action, "snapshot"), null);
});

test("employee-filter replacement and brief root-section changes require exact previous scope", () => {
  const employee = cancellationFixture({ page: "employees", section: "employees", beforeQuery: "employee_filter=in_period", afterQuery: "employee_filter=all" });
  assert.equal(obsoleteLocalCancellation(employee.failure, employee.action, "snapshot"), null);
  employee.action.verified_reads = [{ action_id: 7, section: "employees", url: employee.failure.url.replace("employee_filter=in_period", "employee_filter=all").replace("&cursor=page21", "") }];
  assert.equal(obsoleteLocalCancellation(employee.failure, employee.action, "snapshot"), "previous_employee_filter_page");
  employee.action.verified_reads[0].url = employee.action.verified_reads[0].url.replace("expected_version=snapshot", "expected_version=other");
  assert.equal(obsoleteLocalCancellation(employee.failure, employee.action, "snapshot"), null);
  const brief = cancellationFixture({ page: "brief", section: "activity", beforeQuery: "", afterQuery: "" });
  assert.equal(obsoleteLocalCancellation(brief.failure, brief.action, "snapshot"), "previous_brief_section_page");
  brief.action.after.active_section = "本月发生";
  assert.equal(obsoleteLocalCancellation(brief.failure, brief.action, "snapshot"), null, "changing a category in the same section does not obsolete its root page");
  brief.action.before.brief_view = "按业务"; brief.action.after.brief_view = "按凭证";
  brief.action.verified_reads = [{ action_id: 7, section: "vouchers", url: brief.failure.url.replace("section=activity", "section=vouchers") }];
  assert.equal(obsoleteLocalCancellation(brief.failure, brief.action, "snapshot"), "previous_brief_display_page");
  brief.action.before.brief_view = "按凭证";
  assert.equal(obsoleteLocalCancellation(brief.failure, brief.action, "snapshot"), null, "an activity abort not bound to the previously selected business view stays a failure");
});

test("auto continuation starting inside the action before a URL-bound selection click needs verified replacement", () => {
  for (const options of [{}, { page: "employees", section: "employees", beforeQuery: "employee_filter=in_period", afterQuery: "employee_filter=all" }]) {
    const f = cancellationFixture(options);
    // Real layout failure: request 18 was pending at entry, request 19 starts
    // within action 7 at the old full URL, then the click cancels it at the new URL.
    f.action.pending_before[0].request_id = 18;
    f.failure.request.request_id = 19;
    f.failure.request.started_action = 7;
    if (options.page === "employees") f.action.verified_reads = [{ action_id: 7, section: "employees",
      url: f.failure.url.replace("employee_filter=in_period", "employee_filter=all").replace("&cursor=page21", "") }];
    const reason = options.page === "employees" ? "previous_employee_filter_page" : "previous_funds_view_page";
    assert.equal(obsoleteLocalCancellation(f.failure, f.action, "snapshot"), reason);
    for (const mutate of [
      g => { g.failure.request.started_page_url = g.action.after.url; },
      g => { g.failure.request.finished_page_url = g.action.before.url; },
      g => { g.failure.request.started_page_url += "&unrelated_selection=other"; },
      g => { g.failure.request.started_action = 8; },
      g => { g.failure.request.finished_action = 8; },
      g => { g.failure.status = 503; },
      g => { g.failure.error = "net::ERR_CONNECTION_RESET"; },
      g => { g.failure.url = g.failure.url.replace("cursor=page21", "cursor="); g.failure.request.url = g.failure.url; },
      g => { g.action.verified_reads = []; g.action.verified_cached_reads = []; },
    ]) {
      const wrong = structuredClone(f); mutate(wrong);
      assert.equal(obsoleteLocalCancellation(wrong.failure, wrong.action, "snapshot"), null);
    }
  }
});

test("same-action brief and account aborts without URL evidence remain failures", () => {
  for (const options of [{ page: "brief", section: "activity", beforeQuery: "", afterQuery: "" },
    { section: "movements", beforeQuery: "funds_view=book", afterQuery: "funds_view=book" }]) {
    const f = cancellationFixture(options);
    f.action.pending_before = [];
    f.failure.request.started_action = 7;
    f.action.section = "movements"; f.action.after.movement_account_index = 1;
    f.action.verified_reads = [{ action_id: 7, section: "movements", url: f.failure.url.replace("movement_account_id=old", "movement_account_id=new") }];
    assert.equal(obsoleteLocalCancellation(f.failure, f.action, "snapshot"), null);
  }
});

test("pending account continuation may cross only its verified prior funds display action", () => {
  const f = cancellationFixture({ section: "movements", beforeQuery: "funds_view=book", afterQuery: "funds_view=book" });
  f.action.action_id = 8; f.action.section = "movements";
  f.action.before.funds_view = f.action.after.funds_view = "fund-detail-tab-book";
  f.action.after.movement_account_index = 1;
  f.action.after.movement_account = { movement_account_type: "cash", movement_account_id: "new" };
  const record = f.failure.request;
  record.request_id = 20; record.started_action = 7; record.finished_action = 8;
  record.started_page_url = f.action.before.url.replace("funds_view=book", "funds_view=bank");
  f.action.pending_before = [{ ...record, state: "pending" }];
  f.action.verified_reads = [{ action_id: 8, section: "movements", url: f.failure.url
    .replace("movement_account_type=bank", "movement_account_type=cash").replace("movement_account_id=old", "movement_account_id=new").replace("&cursor=page21", "") }];
  const display = { action_id: 7, page: "funds", status: "passed",
    before: { url: record.started_page_url, funds_view: "fund-detail-tab-bank", movement_account: { ...f.action.before.movement_account } },
    after: { url: f.action.before.url, funds_view: "fund-detail-tab-book", movement_account: { ...f.action.before.movement_account } },
    pending_after: [{ ...record, state: "pending" }],
    verified_cached_reads: [{ action_id: 7, section: "movements", url: f.failure.url }] };
  assert.equal(obsoleteLocalCancellation(f.failure, f.action, "snapshot", [display]), "previous_movement_account_page");
  for (const change of [
    (g, d) => { d.status = "failed"; },
    (g, d) => { d.pending_after = []; },
    (g, d) => { d.pending_after[0].request_id = 21; },
    (g, d) => { d.verified_cached_reads = []; },
    (g, d) => { d.verified_cached_reads[0].url = d.verified_cached_reads[0].url.replace("snapshot", "other"); },
    (g, d) => { d.before.url += "&employee_id=another"; g.failure.request.started_page_url = d.before.url; g.action.pending_before[0].started_page_url = d.before.url; },
    (g, d) => { d.before.funds_view = "fund-detail-tab-book"; },
    (g, d) => { d.after.movement_account.movement_account_id = "other"; },
    (g, d) => { g.action.before.movement_account.movement_account_id = "other"; },
    (g, d) => { g.action.after.movement_account.movement_account_type = "bank"; },
    (g, d) => { g.action.before.movement_account = null; },
    (g, d) => { g.action.verified_reads = []; },
    (g, d) => { g.failure.error = "net::ERR_CONNECTION_RESET"; },
    (g, d) => { g.failure.status = 500; },
    (g, d) => { g.failure.request.started_action = 8; g.action.pending_before[0].started_action = 8; },
  ]) {
    const wrong = structuredClone(f), prior = structuredClone(display); change(wrong, prior);
    assert.equal(obsoleteLocalCancellation(wrong.failure, wrong.action, "snapshot", [prior]), null);
  }
  assert.equal(obsoleteLocalCancellation(f.failure, f.action, "snapshot"), null);
});

test("queued old account continuations use exact resources even when the action does not change the URL", () => {
  const f = cancellationFixture({ section: "movements", beforeQuery: "funds_view=book", afterQuery: "funds_view=book" });
  f.action.section = "movements"; f.action.after.movement_account_index = 1;
  f.action.before.funds_view = f.action.after.funds_view = "fund-detail-tab-book";
  f.action.after.movement_account = { movement_account_type: "cash", movement_account_id: "new" };
  f.action.pending_before[0].request_id = 243;
  f.failure.request.request_id = 244; f.failure.request.started_action = 7;
  f.action.verified_reads = [
    { action_id: 7, section: "movements", url: f.failure.url }, // an old cursor may also finish during the action
    { action_id: 7, section: "movements", url: f.failure.url.replace("movement_account_type=bank", "movement_account_type=cash").replace("movement_account_id=old", "movement_account_id=new").replace("&cursor=page21", "") },
  ];
  assert.equal(obsoleteLocalCancellation(f.failure, f.action, "snapshot"), "previous_movement_account_page");
  for (const mutate of [
    g => { g.failure.url = g.failure.url.replace("movement_account_id=old", "movement_account_id=new"); g.failure.request.url = g.failure.url; },
    g => { g.failure.url = g.failure.url.replace("movement_account_type=bank", "movement_account_type=cash"); g.failure.request.url = g.failure.url; },
    g => { g.action.before.movement_account = null; },
    g => { g.action.after.movement_account = g.action.before.movement_account; },
    g => { g.action.after.movement_account_index = 0; },
    g => { g.action.verified_reads = []; },
    g => { g.action.verified_reads[1].url = g.action.verified_reads[1].url.replace("synthetic&period", "another&period"); },
    g => { g.action.verified_reads[1].url = g.action.verified_reads[1].url.replace("2016-12", "2016-11"); },
    g => { g.action.verified_reads[1].url = g.action.verified_reads[1].url.replace("expected_version=snapshot", "expected_version=other"); },
    g => { g.action.verified_reads[1].url = g.action.verified_reads[1].url.replace("employee_filter=in_period", "employee_filter=all"); },
    g => { g.failure.url += "&employee_id=unrelated"; g.failure.request.url = g.failure.url; },
    g => { g.failure.error = "net::ERR_CONNECTION_RESET"; },
    g => { g.failure.status = 500; },
  ]) {
    const wrong = structuredClone(f); mutate(wrong);
    assert.equal(obsoleteLocalCancellation(wrong.failure, wrong.action, "snapshot"), null);
  }
  // Delayed failure events need the same request still observed at the end of
  // the successfully verified replacement action, never just a later number.
  f.action.status = "passed";
  f.action.pending_after = [{ ...f.failure.request, state: "pending" }];
  for (const finish of [8, null]) {
    const delayed = structuredClone(f); delayed.failure.request.finished_action = finish;
    assert.equal(obsoleteLocalCancellation(delayed.failure, delayed.action, "snapshot"), "previous_movement_account_page");
    delayed.action.pending_after = [];
    assert.equal(obsoleteLocalCancellation(delayed.failure, delayed.action, "snapshot"), null);
  }
});

test("request diagnostics bind a cancelled prior page to its own action and exact filter", () => {
  const diagnostics = createRequestDiagnostics();
  const page = "https://synthetic.invalid/funds?company_id=synthetic&period=2016-12&funds_view=book";
  const old = { url: () => "https://synthetic.invalid/api/dashboard/funds?company_id=synthetic&period=2016-12&section=movements&movement_account_id=old&cursor=old-page&expected_version=snapshot" };
  const current = { url: () => old.url().replace("movement_account_id=old", "movement_account_id=current") };
  diagnostics.start(old, 1, page);
  const before = diagnostics.pending();
  diagnostics.start(current, 2, page);
  const failure = diagnostics.finish(old, "failed", 2, page.replace("funds_view=book", "funds_view=bank"));
  assert.equal(failure.started_action, 1);
  assert.equal(failure.finished_action, 2);
  assert.equal(new URL(failure.url).searchParams.get("movement_account_id"), "old");
  assert.equal(new URL(failure.url).searchParams.get("cursor"), "old-page");
  assert.equal(new URL(failure.finished_page_url).searchParams.get("funds_view"), "bank");
  assert.equal(before[0].state, "pending", "before-action evidence must not mutate after failure");
  assert.equal(diagnostics.pending().length, 1);
  assert.equal(new URL(diagnostics.pending()[0].url).searchParams.get("movement_account_id"), "current");
  assert.equal(diagnostics.finish(current, "failed", 2, page).started_action, 2,
    "a required current-action failure stays distinguishable from a prior request");
  assert.deepEqual(diagnostics.pending(), []);
});

test("request diagnostics distinguish identical URLs across actions and retain unassociated failures", () => {
  const diagnostics = createRequestDiagnostics(), page = "https://synthetic.invalid/assets";
  const first = { url: () => "https://synthetic.invalid/api/dashboard/assets?section=assets&cursor=same" };
  const second = { url: first.url };
  diagnostics.start(first, null, page);
  diagnostics.start(second, 1, page);
  const failure = diagnostics.finish(first, "failed", 1, page);
  assert.equal(failure.started_action, null);
  assert.notEqual(failure.request_id, diagnostics.pending()[0].request_id);
  assert.equal(diagnostics.finish({}, "failed", 1, page), null);
  diagnostics.finish(second, "finished", 1, page);
  assert.deepEqual(diagnostics.pending(), []);
});

test("finished diagnostics remain consumable by the real required-read gate after leaving pending", () => {
  const diagnostics = createRequestDiagnostics(), verified = new WeakSet();
  const page = "https://synthetic.invalid/employees?company_id=synthetic&period=2016-12";
  const request = { url: () => "https://synthetic.invalid/api/dashboard/employees?company_id=synthetic&period=2016-12&employee_filter=all" };
  const started = diagnostics.start(request, 7, page);
  verified.add(request);
  assert.throws(() => verifiedRequestRecord(diagnostics, verified, request, "employee-filter"), /not successfully verified/);
  diagnostics.finish(request, "finished", 7, page);
  assert.deepEqual(diagnostics.pending(), []);
  const consumed = verifiedRequestRecord(diagnostics, verified, request, "employee-filter");
  assert.equal(consumed.request_id, started.request_id);
  assert.equal(consumed.started_action, 7, "local response listeners retain exact action association");
  assert.equal(consumed.finished_action, 7);
  assert.equal(consumed.state, "finished");
  const later = { url: request.url };
  diagnostics.start(later, 8, page);
  assert.equal(verifiedRequestRecord(diagnostics, verified, request, "employee-filter").started_action, 7,
    "identical later URL does not overwrite the completed cycle's request identity");
  diagnostics.finish(later, "failed", 8, page);
  verified.add(later);
  assert.throws(() => verifiedRequestRecord(diagnostics, verified, later, "employee-filter"), /not successfully verified/);
  const unverified = { url: request.url };
  diagnostics.start(unverified, 9, page);
  diagnostics.finish(unverified, "finished", 9, page);
  assert.throws(() => verifiedRequestRecord(diagnostics, verified, unverified, "employee-filter"), /not successfully verified/);
  assert.deepEqual(diagnostics.pending(), []);
});

test("diagnostic URLs redact credentials while preserving API scope and navigation selection", () => {
  const url = new URL(diagnosticUrl("https://user:password@synthetic.invalid/api/dashboard/funds?company_id=synthetic&period=2016-12&section=movements&cursor=page&ticket=private&access_token=private&capability=private#private"));
  assert.equal(url.username, ""); assert.equal(url.password, ""); assert.equal(url.hash, "");
  for (const key of ["ticket", "access_token", "capability"]) assert.equal(url.searchParams.get(key), "[redacted]");
  assert.equal(url.searchParams.get("section"), "movements");
  assert.equal(url.searchParams.get("cursor"), "page");
});
const selection = { key: "assets", action: "assets", companyId: "synthetic", period: "2026-09",
  quarter: "2026-Q3", root: ".asset-result", visible: ["#assets-overview"] };
const employeeValueIds = ["cost", "count", "gross", "contributions", "paid", "outstanding"]
  .map(field => `employees-${field}-value`);

function fixture({ brief = false, employees = false } = {}) {
  const node = (innerText, visible = true) => ({ innerText, visible,
    getClientRects() { return this.visible ? [{}] : []; } });
  const summary = node("合计 ¥123.45");
  const assetAmount = node("¥678.90"), assetCounts = node("固定 2 项 · 无形 1 项");
  const asset = { ...node("长期资产净值"), querySelector: selector =>
    ({ strong: assetAmount, small: assetCounts })[selector] ?? null };
  const employeeValues = Object.fromEntries(employeeValueIds.map(id =>
    [id, node(id === "employees-count-value" ? "3人" : "¥123.45")]));
  const employeeNote = node("本月有薪酬记录 2 人 · 另有 1 人在册状态未确认");
  if (brief) Object.defineProperty(summary, "innerText", { get: () =>
    `合计 ¥123.45\n${asset.visible ? `${assetAmount.innerText}\n${assetCounts.innerText}` : ""}` });
  if (employees) Object.defineProperty(summary, "innerText", { get: () =>
    [...Object.values(employeeValues).filter(value => value.visible).map(value => value.innerText),
      employeeNote.visible ? employeeNote.innerText : ""].join("\n") });
  const rows = node("默认20行，包括资产及项目");
  const root = { children: [summary, rows, node("未打开的详情", false)], querySelector: () => null,
    querySelectorAll: () => [], getAttribute: name =>
      ({ "data-month-state": "closed", "data-owner-review-required": "false" })[name] ?? null };
  let busy = "false";
  const header = { getAttribute: () => busy };
  const company = node("合成公司"), period = { value: "2026-09", getAttribute() { return this.value; } }, status = node("已关账");
  const document = { defaultView: { getComputedStyle: () => ({ visibility: "visible" }) },
    querySelector: selector => ({ ".asset-result": root, "#assets-overview": summary,
      ".employee-result": root, "#employees-overview": summary, "#employee-list-title": rows,
      ...Object.fromEntries(Object.entries(employeeValues).map(([id, value]) => [`#${id}`, value])),
      ".brief-content": root, "#overview": summary, "#overview .kpi.asset": brief ? asset : null,
      ".module-header": header, ".company-switcher-name": company,
      ".module-header .select-trigger": period, ".module-header .period-status": status })[selector] ?? null };
  return { document, root, rows, company, period, status, asset, assetAmount, assetCounts,
    employeeValues, employeeNote,
    setBusy: value => { busy = value; } };
}

test("default projection covers every visible result block and company/period/status", () => {
  const f = fixture(), original = readRefreshProjection(f.document, selection.root);
  assert(!original.includes("未打开的详情"));
  for (const [node, field, value] of [[f.rows, "innerText", "仅19行"], [f.company, "innerText", "另一公司"],
    [f.period, "value", "2026-08"], [f.status, "innerText", "未关账"], [f.rows, "visible", false]]) {
    const old = node[field]; node[field] = value;
    assert.notEqual(readRefreshProjection(f.document, selection.root), original);
    node[field] = old;
  }
  f.root.children.push({ innerText: "遗漏的个人劳务或基金附属", getClientRects: () => [{}] });
  assert.notEqual(readRefreshProjection(f.document, selection.root), original);
});

test("report projection ignores only the known observation clock, retaining period and summary", () => {
  const f = fixture(), clock = { innerText: "2026年第三季度 · 更新于 10/4 12:30" };
  f.root.querySelector = () => clock;
  f.rows.innerText = `${clock.innerText}\n净利润 ¥123.45`;
  const original = readRefreshProjection(f.document, selection.root);
  clock.innerText = "2026年第三季度 · 更新于 10/4 12:31";
  f.rows.innerText = `${clock.innerText}\n净利润 ¥123.45`;
  assert.equal(readRefreshProjection(f.document, selection.root), original);
  f.rows.innerText = `${clock.innerText}\n净利润 ¥123.46`;
  assert.notEqual(readRefreshProjection(f.document, selection.root), original);
});

test("default projection also proves the visible selection and collapsed detail state", () => {
  const f = fixture();
  const control = { tagName: "DETAILS", open: false, getAttribute: () => null };
  f.root.querySelectorAll = () => [control];
  const original = readRefreshProjection(f.document, selection.root);
  control.open = true;
  assert.notEqual(readRefreshProjection(f.document, selection.root), original);
  assert.equal(readRefreshProjection(f.document, ".missing-result"), null);
});

function observerFixture({ projection = true, brief = false, employees = false, requireRoot = false } = {}) {
  const f = fixture({ brief, employees }), events = [], frames = [], window = {};
  const selected = brief ? { ...selection, key: "brief", action: "brief", root: ".brief-content", visible: ["#overview"] }
    : employees ? { ...selection, key: "employees", action: "employees", root: ".employee-result",
      visible: ["#employees-overview", "#employee-list-title", ...employeeValueIds.map(id => `#${id}`)] } : selection;
  let now = 100, callback, mutations, disconnected = false;
  let resources = [];
  const performance = { now: () => { events.push("now"); return now; },
    setResourceTimingBufferSize: () => events.push("buffer"), clearResourceTimings: () => events.push("clear"),
    getEntriesByType: () => resources };
  class MutationObserver {
    constructor(fn) { mutations = fn; }
    observe() {}
    disconnect() { disconnected = true; }
  }
  const context = vm.createContext({ document: f.document, window, performance, MutationObserver, URL,
    requestAnimationFrame: fn => frames.push(fn) });
  const observe = vm.runInContext(`(${observeHotRefresh.toString()})`, context);
  const expected = projection ? readRefreshProjection(f.document, selected.root) : undefined;
  observe({ addEventListener: (name, fn, options) => {
    assert.equal(name, "click"); assert.equal(options.capture, true); assert.equal(options.once, true); callback = fn;
  } }, { ...selected, projection: expected, requireRoot });
  callback();
  return { ...f, events, window, get disconnected() { return disconnected; },
    busy() { f.setBusy("true"); mutations([{ oldValue: "false" }]); },
    ready() { f.setBusy("false"); mutations([{ oldValue: "true" }]); },
    network(query = "") { resources = ["context", selected.action].map(action => ({
      name: `https://synthetic.invalid/api/dashboard/${action}?company_id=synthetic&period=2026-09${action === 'context' ? '' : query}`,
      startTime: 101, responseEnd: 110 })); },
    frame(time) { now = time; assert(frames.length); frames.shift()(); } };
}

test("capture starts before resource clearing; old DOM cannot finish without busy witness", () => {
  const f = observerFixture();
  assert.deepEqual(f.events.slice(0, 3), ["now", "buffer", "clear"]);
  assert.equal(f.window.__stage9RefreshStart, 100);
  f.network(); f.frame(120); f.frame(140);
  assert.equal(f.window.__stage9RenderedAt, null);
  f.busy(); f.ready(); f.frame(160);
  assert.equal(f.window.__stage9RenderedAt, null);
  f.frame(180);
  assert.equal(f.window.__stage9RenderedAt, 180);
  assert(f.disconnected);
});

test("expanded hot refresh requires a new full root response, not a continuation resource", () => {
  const f = observerFixture({ employees: true, requireRoot: true });
  f.busy(); f.ready(); f.network("&section=employees&cursor=page21"); f.frame(120); f.frame(140);
  assert.equal(f.window.__stage9RenderedAt, null);
  f.network(); f.frame(160); f.frame(180);
  assert.equal(f.window.__stage9RenderedAt, 180);
});

function interactionFixture({ diagnostic = false, navigation = false, currentUrl = "https://synthetic.invalid/target", contents = false, readsVerified = true, requireNewResponse = true, position = null, fetchImplementation = async () => ({ status: 200 }) } = {}) {
  const f = fixture(), frames = [], events = new Map(), window = { fetch: fetchImplementation };
  const work = { url_parses: 0, projection_reads: 0 };
  class ObservedURL extends URL { constructor(...args) { super(...args); work.url_parses += 1; } }
  let rowText = f.rows.innerText;
  Object.defineProperty(f.rows, "innerText", { get() { work.projection_reads += 1; return rowText; }, set(value) { rowText = value; } });
  const header = f.document.querySelector(".module-header"); header.isConnected = true;
  f.root.getClientRects = () => contents ? [] : [{}];
  f.document.defaultView.getComputedStyle = node => ({ visibility: node.hidden ? "hidden" : "visible", display: node === f.root && contents ? "contents" : "block" });
  let targetPosition = position?.expected ?? [40, 150, 500, 450, 0, 300, 1440, 1000];
  const target = { getClientRects: () => target.hidden ? [] : [{}], contains: () => false,
    getBoundingClientRect: () => ({ left: targetPosition[0], top: targetPosition[1], right: targetPosition[2], bottom: targetPosition[3] }) };
  const querySelector = f.document.querySelector;
  f.document.querySelector = selector => selector === "#position-target" ? target : querySelector(selector);
  f.document.activeElement = target;
  function setPosition(value) {
    targetPosition = value;
    Object.assign(f.document.defaultView, { scrollX: value[4], scrollY: value[5], innerWidth: value[6], innerHeight: value[7] });
  }
  setPosition(targetPosition);
  let now = 100, rows = 2, mutations;
  f.document.body = {};
  f.document.querySelectorAll = selector => selector === ".complete-row" ? Array(rows).fill({}) : [];
  f.document.addEventListener = (name, listener) => events.set(name, listener);
  f.document.removeEventListener = name => events.delete(name);
  class MutationObserver { constructor(listener) { mutations = listener; } observe() {} disconnect() {} }
  let resources = [];
  const context = vm.createContext({ document: f.document, window, URL: ObservedURL, TextEncoder, location: { href: currentUrl },
    performance: { now: () => now, getEntriesByType: () => resources, setResourceTimingBufferSize() {} }, MutationObserver, requestAnimationFrame: callback => frames.push(callback) });
  const observe = vm.runInContext(`(${observeInteraction.toString()})`, context);
  const cycle = { cycle_id: "current-action", require_new_response: requireNewResponse,
    scope: { action: "assets", company_id: "synthetic", period: "2026-09", snapshot: "snapshot" } };
  const proof = changes => ({ ...cycle, status: "verified", pending: 0, failed: 0,
    required_reads: [{ request_id: 1, status: "verified" }], ...changes });
  const readUrl = interactionReadUrl("https://synthetic.invalid/api/dashboard/assets?company_id=synthetic&period=2026-09&expected_version=snapshot");
  const plan = { cycle_id: cycle.cycle_id, scope: { ...cycle.scope }, required: [{ url: readUrl, count: 1 }], cache_verified: false };
  function publish(changes = {}) {
    const p = proof(changes), count = p.required_reads.length;
    plan.cycle_id = p.cycle_id; plan.scope = p.scope; plan.cache_verified = p.cache_verified === true;
    plan.required = count ? [{ url: readUrl, count }] : [];
    window.__stage9Interaction.fetches = Array.from({ length: count }, (_, index) => ({ url: readUrl, start: 101,
      state: p.pending && index === count - 1 ? "pending" : p.failed || p.required_reads[index].status !== "verified" ? "failed" : "response", status: p.failed ? 500 : 200 }));
    resources = Array.from({ length: count }, () => ({ name: readUrl, startTime: 101, responseEnd: 110 }));
  }
  observe({ ...cycle, root: selection.root, projection: readRefreshProjection(f.document, selection.root),
    read_plan: plan, counts: [[".complete-row", 2]], trigger: ".target", navigation, position, diagnostic_voucher_tail: diagnostic, url: "https://synthetic.invalid/target" });
  if (window.__stage9Interaction && readsVerified) publish();
  return { ...f, window, header, setRows(value) { rows = value; },
    trigger(type = "click", key = undefined, match = true) { events.get(type)?.({ type, key, target: { closest: () => match ? {} : null } }); if (window.__stage9Interaction && readsVerified) publish(); },
    verifyReads: publish,
    plan, readUrl, originalFetch: fetchImplementation, target, setPosition, work,
    setResources(value) { resources = value; },
    setFetches(value) { window.__stage9Interaction.fetches = value; },
    mutation(oldValue) { mutations([{ target: header, attributeName: "aria-busy", oldValue, addedNodes: [] }]); },
    frame(time) { now = time; assert(frames.length); frames.shift()(); } };
}

test("diagnostic retains full download, complete content and two frames while recording actual observation work", () => {
  const f = interactionFixture({ diagnostic: true, readsVerified: false }); f.trigger();
  f.frame(120); assert.equal(f.window.__stage9Interaction.end, null);
  assert.equal(f.window.__stage9Interaction.diagnostic.projections.length, 0);
  f.verifyReads(); f.setRows(1); f.frame(140);
  assert.equal(f.window.__stage9Interaction.diagnostic.projections.length, 0, "partial content does not invoke the full projection");
  f.setRows(2); f.mutation("false"); f.frame(160);
  assert.equal(f.window.__stage9Interaction.end, null);
  f.frame(180); assert.equal(f.window.__stage9Interaction.end, 180);
  const d = f.window.__stage9Interaction.diagnostic;
  assert.equal(d.projections.length, 2); assert(d.projections.every(value => value.utf8_bytes > 0));
  assert.equal(d.observers.interaction.callbacks, 1); assert.equal(d.observers.interaction.records, 1);
  assert.equal(d.frames.length, 4); assert.equal(f.window.__stage9Interaction.network_complete_at, 110);
});

test("browser candidate waits for actual current-cycle fetch success and full download, before Node validation", async () => {
  let release;
  const response = { status: 200, json() { throw new Error("observer must not read a body"); }, clone() { throw new Error("observer must not clone a body"); } };
  const f = interactionFixture({ readsVerified: false, fetchImplementation: () => new Promise(resolve => { release = resolve; }) });
  f.trigger();
  const requested = f.window.fetch(f.readUrl);
  f.frame(120); f.frame(140); assert.equal(f.window.__stage9Interaction.end, null);
  release(response); assert.equal(await requested, response);
  f.frame(160); f.frame(180); assert.equal(f.window.__stage9Interaction.end, null, "headers alone cannot finish download");
  f.setResources([{ name: f.readUrl, startTime: 100, responseEnd: 190 }]);
  f.frame(200); f.frame(220); assert.equal(f.window.__stage9Interaction.end, 220);
  assert.equal(f.window.__stage9Interaction.reads, undefined, "no Node gate is needed for browser candidate time");
  f.window.__stage9InteractionCleanup(); assert.equal(f.window.fetch, f.originalFetch);
});

test("HTTP and network errors cannot finish a calibrated fetch plan and cleanup restores fetch", async () => {
  for (const fail of [false, true]) {
    const f = interactionFixture({ readsVerified: false, fetchImplementation: async () => {
      if (fail) throw new Error("network failure"); return { status: 500 };
    } });
    f.trigger();
    if (fail) await assert.rejects(f.window.fetch(f.readUrl), /network failure/);
    else await f.window.fetch(f.readUrl);
    f.setResources([{ name: f.readUrl, startTime: 100, responseEnd: 110 }]);
    f.frame(120); f.frame(140); assert.equal(f.window.__stage9Interaction.end, null);
    f.window.__stage9InteractionCleanup(); assert.equal(f.window.fetch, f.originalFetch);
  }
});

test("cleanup before an action removes its listeners so a later click cannot start a stale cycle", () => {
  const f = interactionFixture({ readsVerified: false });
  f.window.__stage9InteractionCleanup();
  f.trigger();
  assert.equal(f.window.__stage9Interaction, null);
  assert.equal(f.window.fetch, f.originalFetch);
});

test("calibrated required resources preserve URL multiplicity, exact scope and current-sample completion", () => {
  const url = interactionReadUrl("https://synthetic.invalid/api/dashboard/assets?company_id=synthetic&period=2026-09&expected_version=snapshot&cursor=tail");
  const scope = { action: "assets", company_id: "synthetic", period: "2026-09", snapshot: "snapshot" };
  const selection = { cycle_id: "cycle", scope, require_new_response: true, read_plan: { cycle_id: "cycle", scope, required: interactionReadMultiset([url, url]) } };
  assert.deepEqual(selection.read_plan.required, [{ url, count: 2 }]);
  const fetches = [{ url, start: 101, state: "response", status: 200 }, { url, start: 102, state: "pending", status: null }];
  const resources = [{ name: url, startTime: 101, responseEnd: 110 }];
  assert.equal(interactionReadPlanReady(selection, fetches, resources, 100), false);
  fetches[1].state = "response"; fetches[1].status = 200;
  assert.equal(interactionReadPlanReady(selection, fetches, resources, 100), false, "one download cannot satisfy two equal URLs");
  resources.push({ name: url, startTime: 102, responseEnd: 120 });
  assert.equal(interactionReadPlanReady(selection, fetches, resources, 100), true);
  resources[1].startTime = 99; assert.equal(interactionReadPlanReady(selection, fetches, resources, 100), false);
  resources[1].startTime = 102;
  for (const key of ["company_id", "period", "expected_version"]) {
    const wrong = new URL(url); wrong.searchParams.set(key, "other");
    const old = selection.read_plan.required; selection.read_plan.required = [{ url: interactionReadUrl(wrong.href), count: 1 }];
    assert.equal(interactionReadPlanReady(selection, [{ url: interactionReadUrl(wrong.href), start: 101, state: "response", status: 200 }], [{ name: wrong.href, startTime: 101, responseEnd: 110 }], 100), false);
    selection.read_plan.required = old;
  }
});

test("unfinished downloads and local loading never walk the growing complete DOM projection", () => {
  const f = interactionFixture({ readsVerified: false }); f.trigger();
  f.work.projection_reads = 0;
  let cumulativeTextReads = 0;
  const originalChildren = f.root.children.slice();
  const extraRows = () => Array.from({ length: 100 }, () => ({ getClientRects: () => [{}],
    get innerText() { cumulativeTextReads += 1; return "additional cumulative visible rows"; } }));
  f.root.children.push(...extraRows());
  const resources = Array.from({ length: 400 }, (_, index) => ({ name: `https://synthetic.invalid/unrelated-${index}`, startTime: 101, responseEnd: 110 }));
  f.setResources(resources);
  f.frame(120); f.frame(140); assert.equal(f.work.projection_reads, 0, "missing current body must not trigger complete-list text reads");
  f.verifyReads({ pending: 1 });
  f.root.children.push(...extraRows());
  f.frame(160); f.frame(180); assert.equal(f.work.projection_reads, 0, "pending tails must not trigger complete-list text reads");
  f.verifyReads(); f.setBusy("true");
  f.frame(200); f.frame(220); assert.equal(f.work.projection_reads, 0, "even completed bodies must wait for local readiness before a complete-list walk");
  assert.equal(cumulativeTextReads, 0, "neither the first 100 nor the next 100 cumulative rows may be scanned while unfinished");
  f.root.children = originalChildren;
  f.setBusy("false"); f.frame(240);
  assert.equal(f.work.projection_reads, 1); assert.equal(f.window.__stage9Interaction.end, null);
  f.frame(260); assert.equal(f.work.projection_reads, 2); assert.equal(f.window.__stage9Interaction.end, 260);
});

test("each frame indexes unrelated resources and required tails once while retaining exact completion and network end", () => {
  for (const [tailCount, unrelatedCount] of [[1, 20], [25, 400], [50, 800]]) {
    const f = interactionFixture({ readsVerified: false }); f.trigger();
    const urls = Array.from({ length: tailCount }, (_, index) => interactionReadUrl(`${f.readUrl}&cursor=page-${index}`));
    f.plan.required = urls.map(url => ({ url, count: 1 }));
    const fetches = urls.map(url => ({ url, start: 101, state: "response", status: 200 }));
    const resources = [...urls.map((name, index) => ({ name, startTime: 101, responseEnd: 110 + index })),
      ...Array.from({ length: unrelatedCount }, (_, index) => ({ name: `https://synthetic.invalid/unrelated-${index}`, startTime: 101, responseEnd: 999 }))];
    f.setFetches(fetches); f.setResources(resources);
    f.work.url_parses = 0; f.work.projection_reads = 0;
    f.frame(200);
    assert.equal(f.work.url_parses, unrelatedCount + 2 * tailCount, "resource normalization and plan scope checking must be additive, never tails times resources");
    assert.equal(f.work.projection_reads, 1);
    assert.equal(f.window.__stage9Interaction.network_complete_at, 109 + tailCount, "unrelated resource endpoints cannot extend the required network end");
    f.setResources([...resources, { name: urls[0], startTime: 101, responseEnd: 115 }]);
    f.frame(220); assert.equal(f.window.__stage9Interaction.end, null, "an extra current-cycle body must reset completion");
    f.setResources(resources); fetches[0].state = "failed";
    f.frame(240); assert.equal(f.window.__stage9Interaction.end, null, "indexed download presence cannot replace successful fetch identity");
    fetches[0].state = "response"; f.setFetches([...fetches, { ...fetches[0] }]);
    f.frame(260); assert.equal(f.window.__stage9Interaction.end, null, "an extra fetch cannot reuse one completed body");
    f.setFetches(fetches); f.setResources(resources.filter(item => item.name !== urls[0]));
    f.frame(280); assert.equal(f.window.__stage9Interaction.end, null, "a missing tail body remains incomplete");
    f.setResources(resources); f.frame(300); assert.equal(f.window.__stage9Interaction.end, null);
    f.frame(320); assert.equal(f.window.__stage9Interaction.end, 320);
  }
});

test("local timing starts at the selected real action and requires complete content for two frames", () => {
  const f = interactionFixture();
  f.trigger("click", undefined, false); f.trigger("keydown", "ArrowDown");
  assert.equal(f.window.__stage9Interaction, null);
  f.trigger("keydown", "Enter"); assert.equal(f.window.__stage9Interaction.start, 100);
  f.setRows(1); f.frame(120); f.frame(140); assert.equal(f.window.__stage9Interaction.end, null);
  f.setRows(2); const full = f.rows.innerText; f.rows.innerText = "old content";
  f.frame(160); f.frame(180); assert.equal(f.window.__stage9Interaction.end, null);
  f.rows.innerText = full; f.frame(200); assert.equal(f.window.__stage9Interaction.end, null);
  f.frame(220); assert.equal(f.window.__stage9Interaction.end, 220);
});

test("display contents result completes only with visible rendered children and two stable frames", () => {
  const f = interactionFixture({ contents: true }); f.trigger();
  f.frame(120); assert.equal(f.window.__stage9Interaction.end, null);
  f.frame(140); assert.equal(f.window.__stage9Interaction.end, 140);
});

test("identical visible content cannot end before the current required response is verified", () => {
  const f = interactionFixture({ contents: true, readsVerified: false }); f.trigger();
  f.frame(120); f.frame(140); f.frame(160);
  assert.equal(f.window.__stage9Interaction.end, null);
  f.verifyReads(); f.frame(180); assert.equal(f.window.__stage9Interaction.end, null);
  f.frame(200); assert.equal(f.window.__stage9Interaction.end, 200);
  assert.equal(f.window.__stage9Interaction.start, 100);
});

test("wrong scope, stale cycle, failed request, and unfinished required continuation never open the endpoint", () => {
  for (const proof of [
    { cycle_id: "previous-action" },
    ...["action", "company_id", "period", "snapshot"].map(key => ({ scope: { action: "assets", company_id: "synthetic", period: "2026-09", snapshot: "snapshot", [key]: "other" } })),
    { scope: { action: "assets", company_id: "synthetic", period: "2026-09" } },
    { failed: 1 }, { required_reads: [{ status: "failed" }] },
    { pending: 1, required_reads: [{ status: "verified" }, { status: "pending" }] },
    { required_reads: [] },
  ]) {
    const f = interactionFixture({ readsVerified: false }); f.trigger(); f.verifyReads(proof);
    f.frame(120); f.frame(140); assert.equal(f.window.__stage9Interaction.end, null);
    f.frame(30101); assert.match(f.window.__stage9Interaction.error, /timeout/);
  }
});

test("a required continuation and a changing complete projection reset the stable frame count", () => {
  const f = interactionFixture({ readsVerified: false }); f.trigger();
  f.verifyReads(); f.frame(120);
  f.verifyReads({ pending: 1 }); f.frame(140);
  f.verifyReads({ required_reads: [{ request_id: 1, status: "verified" }, { request_id: 2, status: "verified" }] });
  f.frame(160); assert.equal(f.window.__stage9Interaction.end, null);
  const complete = f.rows.innerText; f.rows.innerText = "incomplete continuation"; f.frame(180);
  f.rows.innerText = complete; f.frame(200); assert.equal(f.window.__stage9Interaction.end, null);
  f.frame(220); assert.equal(f.window.__stage9Interaction.end, 220);
});

test("pure display cache needs explicit validation and cannot masquerade as a new response", () => {
  const f = interactionFixture({ readsVerified: false, requireNewResponse: false }); f.trigger();
  f.verifyReads({ required_reads: [], cache_verified: false }); f.frame(120); f.frame(140);
  assert.equal(f.window.__stage9Interaction.end, null);
  f.verifyReads({ required_reads: [], cache_verified: true }); f.frame(160); f.frame(180);
  assert.equal(f.window.__stage9Interaction.end, 180);
  const required = interactionFixture({ readsVerified: false }); required.trigger();
  required.verifyReads({ required_reads: [], cache_verified: true }); required.frame(120); required.frame(140);
  assert.equal(required.window.__stage9Interaction.end, null);
});

test("expanded employee root verification accepts completed tails without redefining default twenty rows", () => {
  verifyEmployeeRootRows(20, 20);
  assert.throws(() => verifyEmployeeRootRows(50, 20), /default employee rows incomplete/);
  verifyEmployeeRootRows(50, 20, true);
  assert.throws(() => verifyEmployeeRootRows(19, 20, true), /lost root employee rows/);
  const f = interactionFixture({ readsVerified: false }); f.trigger();
  f.verifyReads({ pending: 1, required_reads: [{ status: "verified" }, { status: "verified" }, { status: "pending" }] });
  f.frame(120); f.frame(140); assert.equal(f.window.__stage9Interaction.end, null);
  f.verifyReads({ required_reads: [{ status: "verified" }, { status: "verified" }, { status: "verified" }] });
  f.setRows(1); f.frame(160); f.frame(180); assert.equal(f.window.__stage9Interaction.end, null);
  f.setRows(2); f.frame(200); f.frame(220); assert.equal(f.window.__stage9Interaction.end, 220);
});

test("boxless result visibility requires actual rendered descendants rather than hidden or empty nodes", () => {
  const doc = { defaultView: { getComputedStyle: node => ({ display: node.display, visibility: node.visibility ?? "visible" }) } };
  const leaf = { display: "block", getClientRects: () => [{}] };
  const root = { display: "contents", children: [leaf], getClientRects: () => [] };
  assert.equal(resultHasVisibleContent(doc, root), true);
  leaf.visibility = "hidden"; assert.equal(resultHasVisibleContent(doc, root), false);
  leaf.visibility = "visible"; leaf.display = "none"; assert.equal(resultHasVisibleContent(doc, root), false);
  leaf.display = "block"; leaf.getClientRects = () => []; assert.equal(resultHasVisibleContent(doc, root), false);
  root.children = []; assert.equal(resultHasVisibleContent(doc, root), false);
  leaf.getClientRects = () => [{}]; root.children = [{ display: "contents", children: [leaf] }];
  assert.equal(resultHasVisibleContent(doc, root), true);
  root.display = "none"; assert.equal(resultHasVisibleContent(doc, root), false);
  assert.equal(resultHasVisibleContent(doc, null), false);
});

test("display contents result rejects hidden children, empty results, and unready page", () => {
  for (const condition of ["hidden", "empty", "unready"]) {
    const f = interactionFixture({ contents: true }); f.trigger();
    if (condition === "hidden") for (const child of f.root.children) child.hidden = true;
    if (condition === "empty") f.root.children = [];
    if (condition === "unready") f.setBusy("true");
    f.frame(120); f.frame(140); assert.equal(f.window.__stage9Interaction.end, null, condition);
    f.frame(30101); assert.match(f.window.__stage9Interaction.error, /timeout/, condition);
  }
});

test("local timing records transient whole-page busy mutations and disconnection", () => {
  const f = interactionFixture(); f.trigger();
  f.mutation("true"); assert.equal(f.window.__stage9Interaction.flashes.length, 1);
  f.header.isConnected = false; f.mutation("false");
  assert.equal(f.window.__stage9Interaction.flashes.length, 2);
});

test("exact navigation timing is separately based on navigation timeOrigin and full target completion", () => {
  const other = interactionFixture({ navigation: true, currentUrl: "https://synthetic.invalid/other" });
  assert.equal(other.window.__stage9Interaction, undefined);
  const f = interactionFixture({ navigation: true });
  assert.equal(f.window.__stage9Interaction.start, 0);
  f.setRows(1); f.frame(120); assert.equal(f.window.__stage9Interaction.end, null);
  f.setRows(2); f.frame(140); f.frame(160); assert.equal(f.window.__stage9Interaction.end, 160);
});

test("exact positioning cannot finish with matching text while the real target is outside the viewport or still scrolling", () => {
  const expected = [40, 150, 500, 450, 0, 300, 1440, 1000];
  const f = interactionFixture({ navigation: true, position: { selector: "#position-target", require_focus: true, expected } });
  f.setPosition([40, 1100, 500, 1400, 0, 0, 1440, 1000]);
  f.frame(120); f.frame(140); assert.equal(f.window.__stage9Interaction.end, null, "a nonzero box outside the viewport is not a positioned target");
  f.setPosition([40, 500, 500, 800, 0, 0, 1440, 1000]);
  f.frame(160); f.frame(180); assert.equal(f.window.__stage9Interaction.end, null, "entering the viewport before the calibrated destination is insufficient");
  f.setPosition([40, 150.5, 500, 450.5, 0, 299.5, 1440, 1000]); f.frame(200);
  f.setPosition(expected); f.frame(220); assert.equal(f.window.__stage9Interaction.end, null, "movement within rounding tolerance still resets the stable frame count");
  f.frame(240); assert.equal(f.window.__stage9Interaction.end, 240);
  assert.deepEqual(Array.from(f.window.__stage9Interaction.position), expected);
});

test("exact positioning requires the actual target and focus, but a tall mobile card may be partially visible", () => {
  const expected = [0, 100, 375, 2100, 0, 500, 375, 667];
  const f = interactionFixture({ navigation: true, position: { selector: "#position-target", require_focus: true, expected } });
  f.document.activeElement = null;
  f.frame(120); f.frame(140); assert.equal(f.window.__stage9Interaction.end, null, "text and geometry cannot replace target focus");
  f.document.activeElement = f.target; f.target.hidden = true;
  f.frame(160); f.frame(180); assert.equal(f.window.__stage9Interaction.end, null, "a hidden target cannot complete positioning");
  f.target.hidden = false;
  f.frame(200); f.frame(220); assert.equal(f.window.__stage9Interaction.end, 220, "a card taller than the viewport must not time out merely because its bottom is below the screen");
});

test("ordinary paging and local display changes retain their endpoint without a positioning plan", () => {
  const f = interactionFixture(); f.trigger();
  f.setPosition([40, 1100, 500, 1400, 0, 0, 1440, 1000]); f.frame(120);
  f.setPosition([40, 1200, 500, 1500, 0, 25, 1440, 1000]); f.frame(140);
  assert.equal(f.window.__stage9Interaction.end, 140);
});

test("interaction timeout remains an error instead of completing an incomplete automatic continuation", () => {
  const f = interactionFixture(); f.trigger(); f.setRows(1); f.frame(30101);
  assert.equal(f.window.__stage9Interaction.end, null);
  assert.match(f.window.__stage9Interaction.error, /timeout/);
});

test("exact employee and asset targets require real response identities and reject page scanning", () => {
  for (const section of ["employees", "assets"]) {
    const field = section === "employees" ? "employee_id" : "asset_id", identity = "actual-id";
    const url = new URL(`https://synthetic.invalid/api/dashboard/${section}?company_id=synthetic&period=2016-12&${field}=${identity}`);
    const selected = { id: "synthetic", period: "2016-12" };
    const payload = { data: { [field]: identity, collections: { [section]: { items: [{ [field]: identity }] } } } };
    verifyExactInteractionTarget(payload, url, selected, section, identity);
    for (const [key, value] of [["company_id", "other"], ["period", "2016-11"], [field, "other"], ["cursor", "page21"], ["section", section]]) {
      const wrong = new URL(url); wrong.searchParams.set(key, value);
      assert.throws(() => verifyExactInteractionTarget(payload, wrong, selected, section, identity));
    }
    for (const mutate of [p => { p.data[field] = "other"; }, p => { p.data.collections[section].items[0][field] = "other"; }, p => { p.data.collections[section].items.push({ [field]: "another" }); }]) {
      const wrong = structuredClone(payload); mutate(wrong);
      assert.throws(() => verifyExactInteractionTarget(wrong, url, selected, section, identity));
    }
  }
});

test("interaction attempts preserve slow completion and subsequent contract errors individually", async () => {
  const attempts = [];
  await recordRefreshAttempt(attempts, { scenario: "local", kind: "sample", iteration: 1 }, async () => 700, async () => ({}));
  await assert.rejects(recordRefreshAttempt(attempts, { scenario: "local", kind: "sample", iteration: 2 }, async attempt => {
    attempt.phase = "validate"; throw new Error("response contract mismatch");
  }, async () => ({ started: true, elapsed_ms: 650 })), /contract/);
  assert.deepEqual(attempts.map(item => [item.iteration, item.status, item.elapsed_ms]), [[1, "complete", 700], [2, "failed", 650]]);
});

test("late default rows block completion and mismatches reset the two-frame proof", () => {
  const f = observerFixture();
  f.busy(); f.ready(); f.network();
  const full = f.rows.innerText;
  f.rows.innerText = "金额已更新但项目或第20行未完成";
  f.frame(120); f.frame(140); assert.equal(f.window.__stage9RenderedAt, null);
  f.rows.innerText = full; f.frame(160);
  f.rows.innerText = "又缺少一行"; f.frame(180);
  f.rows.innerText = full; f.frame(200); assert.equal(f.window.__stage9RenderedAt, null);
  f.frame(220); assert.equal(f.window.__stage9RenderedAt, 220);
});

test("first unprojected warmup retains the legacy guard and both RAFs", () => {
  const f = observerFixture({ projection: false });
  f.network(); f.frame(120); assert.equal(f.window.__stage9RenderedAt, null);
  f.frame(140); assert.equal(f.window.__stage9RenderedAt, 140);
});

test("brief long-term asset amount and both complete counts are typed, preserving unknown and negative net", () => {
  for (const net_fen of [null, "0", "-123", "456"]) {
    assert.doesNotThrow(() => verifyLongTermAssets({ net_fen, fixed_active_count: 0, intangible_active_count: 2 }));
  }
  for (const value of [undefined, {}, { net_fen: 123, fixed_active_count: 0, intangible_active_count: 0 },
    { net_fen: "0", fixed_active_count: -1, intangible_active_count: 0 },
    { net_fen: "0", fixed_active_count: 1, intangible_active_count: 0.5 },
    { net_fen: "0", fixed_active_count: 1 }]) assert.throws(() => verifyLongTermAssets(value));
});

test("brief card, amount or counts missing blocks even the unprojected first warmup", () => {
  for (const [field, missing] of [["visible", false], ["amount", ""], ["counts", "固定 2 项"]]) {
    const f = observerFixture({ brief: true, projection: false });
    const target = field === "visible" ? f.asset : field === "amount" ? f.assetAmount : f.assetCounts;
    const property = field === "visible" ? "visible" : "innerText", old = target[property];
    target[property] = missing;
    f.busy(); f.ready(); f.network(); f.frame(120); f.frame(140);
    assert.equal(f.window.__stage9RenderedAt, null, `${field} was missing`);
    target[property] = old; f.frame(160); assert.equal(f.window.__stage9RenderedAt, null);
    f.frame(180); assert.equal(f.window.__stage9RenderedAt, 180);
  }
});

test("brief long-term net and counts participate in the existing two-frame stable projection", () => {
  for (const [field, changed] of [["assetAmount", "¥678.91"], ["assetCounts", "固定 3 项 · 无形 1 项"]]) {
    const f = observerFixture({ brief: true }), node = f[field], old = node.innerText;
    f.busy(); f.ready(); f.network(); node.innerText = changed;
    f.frame(120); f.frame(140); assert.equal(f.window.__stage9RenderedAt, null);
    node.innerText = old; f.frame(160); assert.equal(f.window.__stage9RenderedAt, null);
    f.frame(180); assert.equal(f.window.__stage9RenderedAt, 180);
  }
});

test("employee combined overview amounts remain exact and preserve either unknown input", () => {
  assert.equal(sumFen("9007199254740993", "7"), "9007199254741000");
  assert.equal(sumFen("-123", "23"), "-100");
  assert.equal(sumFen("0", "0"), "0");
  for (const [first, second] of [[null, "123"], ["123", null], [null, null]])
    assert.equal(sumFen(first, second), null);
});

test("employee unpaid headline reads the full remuneration field in contract 11, preserving zero and unknown", () => {
  const data = { employees: { outstanding_net_fen: "12345" }, outstanding_remuneration_fen: "67890" };
  assert.equal(employeeOutstandingFen(data, 11), "67890");
  for (const value of ["0", null, "9007199254740993"]) {
    data.outstanding_remuneration_fen = value;
    assert.equal(employeeOutstandingFen(data, 11), value);
  }
  delete data.outstanding_remuneration_fen;
  assert.throws(() => employeeOutstandingFen(data, 11), "Missing total must not fall back to wages");
  for (const version of [9, 10, 12]) assert.throws(() => employeeOutstandingFen(data, version));
});

test("each of the six employee overview values must be visible and populated before the first warmup completes", () => {
  for (const id of employeeValueIds) for (const [field, missing] of [["visible", false], ["innerText", ""]]) {
    const f = observerFixture({ employees: true, projection: false }), value = f.employeeValues[id], original = value[field];
    value[field] = missing;
    f.busy(); f.ready(); f.network(); f.frame(120); f.frame(140);
    assert.equal(f.window.__stage9RenderedAt, null, `${id} was not ready`);
    value[field] = original; f.frame(160); assert.equal(f.window.__stage9RenderedAt, null);
    f.frame(180); assert.equal(f.window.__stage9RenderedAt, 180);
  }
  const f = observerFixture({ employees: true, projection: false });
  f.employeeValues["employees-count-value"].innerText = "人";
  f.busy(); f.ready(); f.network(); f.frame(120); f.frame(140);
  assert.equal(f.window.__stage9RenderedAt, null, "Count unit alone was mistaken for a ready headcount");
});

test("all employee metrics, conditional notes and default detail rows participate in the two-frame projection", () => {
  for (const target of [...employeeValueIds, "note", "rows"]) {
    const f = observerFixture({ employees: true });
    const node = target === "note" ? f.employeeNote : target === "rows" ? f.rows : f.employeeValues[target];
    const original = node.innerText;
    node.innerText = target === "employees-count-value" ? "4人" : "内容或第20条尚未完成";
    f.busy(); f.ready(); f.network(); f.frame(120); f.frame(140);
    assert.equal(f.window.__stage9RenderedAt, null, `${target} differed from the complete page`);
    node.innerText = original; f.frame(160); assert.equal(f.window.__stage9RenderedAt, null);
    f.frame(180); assert.equal(f.window.__stage9RenderedAt, 180);
  }
});


test("selected employee and asset filters bind UI, request and response independently", () => {
  for (const [section, label, field, value] of [["employees", "筛选员工", "employee_filter", "in_period"], ["assets", "筛选资产", "asset_filter", "fixed"]]) {
    const selected = { filters: [{ label, value }] }, target = new URL(`https://synthetic.invalid/api/dashboard/${section}?${field}=${value}`), payload = { data: { [field]: value } };
    verifySelectedFilter(selected, target, payload, section, value);
    assert.throws(() => verifySelectedFilter({ filters: [{ label, value: "all" }] }, target, payload, section, value), /UI filter/);
    assert.throws(() => verifySelectedFilter(selected, new URL(`https://synthetic.invalid/?${field}=all`), payload, section, value), /requested filter/);
    assert.throws(() => verifySelectedFilter(selected, target, { data: { [field]: "all" } }, section, value), /response filter/);
  }
});

test("grouped brief reads bind member pages to group, snapshot and voucher associations without overview vouchers", () => {
  const page = { total_count: 1, filtered_count: 1, returned_count: 1, has_more: false, next_cursor: null };
  const selected = { id: "synthetic", period: "2016-12" }, snapshot = "snapshot";
  const target = new URL("https://synthetic.invalid/api/dashboard/brief-group?company_id=synthetic&period=2016-12&section=activity&group_key=group&expected_version=snapshot");
  const payload = { schema_version: 3, read_context: { company_id: selected.id }, selected_period: { key: selected.period }, snapshot_version: snapshot,
    data: { section: "activity", group_key: "group", collections: { members: { items: [{ key: "member", detail_scope_key: null, group_key: "group", voucher_version_id: null }], page }, vouchers: { items: [], page: { ...page, total_count: 0, filtered_count: 0, returned_count: 0 } } } } };
  assert.equal(verifyBriefGroup(payload, target, selected, snapshot), payload);
  for (const mutate of [p => { p.schema_version++; }, p => { p.snapshot_version = "other"; }, p => { p.data.group_key = "other"; }, p => { p.data.section = "open_items"; }, p => { p.data.collections.members.items[0].group_key = "other"; }, p => { p.data.collections.members.items[0].voucher_version_id = "missing"; }, p => { p.data.collections.members.page.returned_count = 0; }]) {
    const wrong = structuredClone(payload); mutate(wrong);
    assert.throws(() => verifyBriefGroup(wrong, target, selected, snapshot));
  }
  const continued = structuredClone(payload);
  continued.data.collections.members.page = { ...page, total_count: 21, filtered_count: 21 };
  const cursor = new URL(target); cursor.searchParams.set("cursor", "page21");
  verifyBriefGroup(continued, cursor, selected, snapshot);
  assert.throws(() => verifyBriefGroup(continued, target, selected, snapshot), /truncated/);
});
