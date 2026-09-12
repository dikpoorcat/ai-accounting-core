import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import * as Vue from "vue";
import ts from "typescript";

let harnessNumber = 0;
async function fundsApi(requestJson = async () => ({})) {
  const key = `fundsApiHarness${++harnessNumber}`;
  globalThis[key] = requestJson;
  const source = readFileSync(new URL("../src/api/funds.ts", import.meta.url), "utf8").replace(/import[^;]+;/g, "");
  const { outputText } = ts.transpileModule(`const requestJson = globalThis.${key};\n` + source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
  });
  return import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
}
async function fundsView(fetchFundsDashboard, refreshContext = async () => {}, query = {}) {
  const key = `fundsViewHarness${++harnessNumber}`;
  const route = Vue.reactive({ query: { company_id: "company-a", period: "2026-09", ...query }, hash: "" });
  const dashboardContext = Vue.ref(null);
  globalThis[key] = { Vue, route, fetchFundsDashboard, refreshContext, dashboardContext, api: await fundsApi() };
  globalThis.document = { getElementById: () => null, querySelector: () => null };
  const source = readFileSync(new URL("../src/views/FundsView.vue", import.meta.url), "utf8")
    .match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
    .replace(/import[\s\S]*?from "[^"]+";/g, "");
  const imports = `
    const { computed, nextTick, ref, watch } = globalThis.${key}.Vue;
    const onMounted = () => {}; const onBeforeUnmount = () => {};
    const { fetchFundsDashboard } = globalThis.${key};
    const { fundAccountLabel, rememberFundAccounts } = globalThis.${key}.api;
    const useRoute = () => globalThis.${key}.route;
    const navigate = async target => { globalThis.${key}.route.query = target.query; globalThis.${key}.route.hash = target.hash ?? ''; };
    const useRouter = () => ({ replace: navigate, push: navigate });
    const useDashboardContext = () => ({ context: globalThis.${key}.dashboardContext, load: async () => {}, refresh: globalThis.${key}.refreshContext });
    const useDashboardSections = (_items, initialId) => ({ activeSection: ref(initialId), focusSection() {}, positionSection() {}, lockSectionSync() {} });
    const dashboardErrorMessage = String;
    const isDashboardSnapshotChanged = error => error.code === 'dashboard_snapshot_changed';
    const fen = BigInt; const formatFen = String; const formatPositiveFen = String;
  `;
  const exported = "\nexport { loadFunds, loadMore, refresh, funds, snapshotVersion, selectedPeriod, selectedAccount, selectedBankAccount, selectedDetailView, loading, pageStates, requestError, accountOptions, responsePeriod, voucherTarget, changeAccountFilters, selectDetailView, updateNotice };";
  const { outputText } = ts.transpileModule(imports + source + exported, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
  });
  const view = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
  view.selectedPeriod.value = "2026-09";
  return { ...view, route, dashboardContext };
}

function data(account = "bank-a", next = "next") {
  const page = { has_more: !!next, next_cursor: next, total_count: 2, filtered_count: 2, returned_count: 1 };
  const value = {
    accounts: ["bank-a", "bank-b"].map(account_id => ({ type: "bank", account_id, code: account_id, name: account_id, reconciliation: { state: "complete" } })),
    movements: [{ id: `${account}-first`, account_type: "bank", account_id: account }],
    movement_page: { ...page }, movement_count: 4,
    bank_statement: { rows: [{ id: "old-statement" }], page: { ...page }, transaction_count: 4 },
    investments: { products: [], events: [{ id: "old-investment" }], page: { ...page }, event_count: 2 },
  };
  value.collections = {
    movements: { items: value.movements, page }, statements: { items: value.bank_statement.rows, page },
    investment_events: { items: value.investments.events, page }, accounts: { items: value.accounts, page: { ...page, returned_count: 2, has_more: false, next_cursor: null } },
    investment_products: { items: [], page: { ...page, returned_count: 0, total_count: 0, filtered_count: 0, has_more: false, next_cursor: null } },
  };
  return value;
}

function response(value, snapshot = "version") {
  return { schema_version: 2, snapshot_version: snapshot, selected_period: { key: "2026-09", label: "2026 年 9 月" }, data: value };
}

test("account changes clear every old page and reject an earlier continuation", async () => {
  const calls = [];
  const view = await fundsView((period, signal, query) => new Promise(resolve => calls.push({ period, signal, query, resolve })));
  view.selectedAccount.value = "bank:bank-a";
  view.selectedBankAccount.value = "bank-a";
  view.funds.value = data();
  view.snapshotVersion.value = "version";
  const earlier = view.loadMore("book");
  assert.equal(calls[0].query.cursor, "next");
  assert.equal(calls[0].query.section, "movements");
  assert.equal(calls[0].query.movement_account_id, "bank-a");
  assert.equal(calls[0].query.statement_account_id, "bank-a");
  assert.equal(calls[0].query.expected_version, "version");

  view.selectedAccount.value = "bank:bank-b";
  const changed = view.loadFunds("2026-09");
  assert.equal(calls[0].signal.aborted, true);
  assert.equal(view.funds.value, null);
  assert.equal(view.snapshotVersion.value, "");
  assert.equal(calls[1].query.movement_account_id, "bank-b");
  assert.equal(calls[1].query.after_movement, undefined);
  calls[0].resolve(response(data("bank-a", null)));
  await earlier;
  assert.equal(view.funds.value, null);
  calls[1].resolve(response(data("bank-b", null), "new-version"));
  await changed;
  assert.deepEqual(view.funds.value.movements.map(row => row.account_id), ["bank-b"]);
  assert.equal(view.snapshotVersion.value, "new-version");
});

test("a late first page cannot replace a later account selection", async () => {
  const calls = [];
  const view = await fundsView((period, signal, query) => new Promise(resolve => calls.push({ period, signal, query, resolve })));
  view.funds.value = data();
  view.selectedAccount.value = "bank:bank-a";
  const first = view.loadFunds("2026-09");
  view.selectedAccount.value = "bank:bank-b";
  const second = view.loadFunds("2026-09");
  calls[1].resolve(response(data("bank-b", null)));
  await second;
  calls[0].resolve(response(data("bank-a", null)));
  await first;
  assert.deepEqual(view.funds.value.movements.map(row => row.account_id), ["bank-b"]);
  assert.equal(view.loading.value, false);
});

test("changing company while refreshing context cannot load the previous selection", async () => {
  let finish;
  const view = await fundsView(() => assert.fail("stale selection must not be fetched"), () => new Promise(resolve => { finish = resolve; }));
  view.funds.value = data();
  const refresh = view.refresh();
  view.route.query.company_id = "company-b";
  await Vue.nextTick();
  finish();
  await refresh;
  assert.equal(view.funds.value, null);
  assert.equal(view.selectedPeriod.value, "");
  assert.equal(view.selectedAccount.value, "");
});

test("funds API sends both account filters with the continuation version", async () => {
  let requested;
  const api = await fundsApi((url, options) => { requested = { url, options }; return Promise.resolve({}); });
  const controller = new AbortController();
  await api.fetchFundsDashboard("2026-09", controller.signal, {
    movement_account_type: "payment_platform", movement_account_id: "platform-a", statement_account_id: "bank-b",
    after_movement: "scope:movement:1", expected_version: "version",
  });
  const query = new URL(requested.url, "http://localhost").searchParams;
  assert.equal(query.get("movement_account_type"), "payment_platform");
  assert.equal(query.get("movement_account_id"), "platform-a");
  assert.equal(query.get("statement_account_id"), "bank-b");
  assert.equal(query.get("after_movement"), "scope:movement:1");
  assert.equal(query.get("expected_version"), "version");
  assert.equal(query.get("period"), "2026-09");
  assert.equal(requested.options.signal, controller.signal);
});

test("independent continuations merge into the latest snapshot and failures stay local", async () => {
  const calls = [];
  const view = await fundsView((period, signal, query) => new Promise((resolve, reject) => calls.push({ query, resolve, reject })));
  view.funds.value = data(); view.snapshotVersion.value = "version";
  const book = view.loadMore("book"), bank = view.loadMore("bank");
  assert.equal(calls.length, 2);
  assert.equal(view.pageStates.value.book.loading, true);
  assert.equal(view.pageStates.value.bank.loading, true);
  calls[0].reject(new Error("账面续页暂不可用")); await book;
  assert.match(view.pageStates.value.book.error, /账面续页暂不可用/);
  assert.equal(view.requestError.value, "");
  const nextBank = data(); nextBank.bank_statement.rows = [{ id: "bank-next" }]; nextBank.collections.statements.items = nextBank.bank_statement.rows;
  calls[1].resolve(response(nextBank)); await bank;
  assert.deepEqual(view.funds.value.bank_statement.rows.map(item => item.id), ["old-statement", "bank-next"]);
  const retry = view.loadMore("book");
  const nextBook = data("bank-a", null); nextBook.movements = [{ id: "book-next" }]; nextBook.collections.movements.items = nextBook.movements;
  calls[2].resolve(response(nextBook)); await retry;
  assert.equal(view.pageStates.value.book.error, "");
  assert.deepEqual(view.funds.value.movements.map(item => item.id), ["bank-a-first", "book-next"]);
  assert.equal(view.funds.value.bank_statement.rows.length, 2);
});

test("route restoration preserves account filters and view while retrieving a fresh first page", async () => {
  const calls = [];
  const view = await fundsView(async (period, signal, query) => { calls.push(query); return response(data()); }, undefined,
    { movement_account_type: "bank", movement_account_id: "bank-b", statement_account_id: "bank-b", funds_view: "bank" });
  view.dashboardContext.value = { current_company: { company_id: "company-a" }, periods: [{ key: "2026-09" }], default_period: "2026-09" };
  await Vue.nextTick(); await Vue.nextTick();
  assert.equal(view.selectedAccount.value, "bank:bank-b");
  assert.equal(view.selectedBankAccount.value, "bank-b");
  assert.equal(view.selectedDetailView.value, "bank");
  assert.equal(calls[0].movement_account_id, "bank-b");
  assert.equal(calls[0].cursor, undefined);
  view.selectedAccount.value = "bank:bank-a"; view.changeAccountFilters();
  await Vue.nextTick(); await Vue.nextTick();
  assert.equal(view.route.query.movement_account_id, "bank-a");
  assert.equal(view.funds.value !== null, true);
  view.selectDetailView("book");
  assert.equal(view.route.query.funds_view, "book");
});

test("selected later-page account label survives filtering without changing returned counts", async () => {
  const first = data(); first.accounts = [first.accounts[0]];
  first.collections.accounts = { items: first.accounts, page: { total_count: 101, filtered_count: 101, returned_count: 1, has_more: true, next_cursor: "account-next" } };
  const later = data(); later.accounts = [{ ...later.accounts[0], account_id: "bank-z", name: "后页账户", code: "101" }];
  later.collections.accounts = { items: later.accounts, page: { ...first.collections.accounts.page, has_more: false, next_cursor: null } };
  const view = await fundsView(async (period, signal, query) => response(query.section === "accounts" ? later : first));
  await view.loadFunds("2026-09"); await view.loadMore("accounts");
  view.selectedAccount.value = "bank:bank-z";
  await view.loadFunds("2026-09");
  assert.equal(view.accountOptions.value.find(item => item.value === "bank:bank-z").label, "后页账户（101）");
  assert.equal(view.funds.value.accounts.length, 1);
  assert.equal(view.funds.value.collections.accounts.page.returned_count, 1);
  view.route.query.company_id = "company-b"; await Vue.nextTick();
  view.selectedPeriod.value = "2026-09"; view.selectedAccount.value = "bank:bank-z";
  await view.loadFunds("2026-09");
  assert.equal(view.accountOptions.value.find(item => item.value === "bank:bank-z").label, "所选账户（名称尚未加载）");
});

test("voucher targets use the declared accounting month and positive journal reference", async () => {
  const view = await fundsView(async () => response(data())); await view.loadFunds("2026-09");
  assert.deepEqual(view.voucherTarget("12", view.responsePeriod.value), { path: "/", query: { company_id: "company-a", period: "2026-09", voucher: "12" } });
  assert.equal(view.voucherTarget("摘要 12", "2026-09"), null);
  assert.equal(view.voucherTarget("12", ""), null);
  assert.equal(view.voucherTarget("0", "2026-09"), null);
});

test("an old September snapshot refresh cannot finish the notice of a newer September refresh after A-B-A", async () => {
  const calls = [], refreshes = [];
  const view = await fundsView(
    (period, signal, query) => new Promise((resolve, reject) => calls.push({ period, signal, query, resolve, reject })),
    () => new Promise((resolve, reject) => refreshes.push({ resolve, reject })),
  );
  const snapshotChanged = () => Object.assign(new Error("changed"), { code: "dashboard_snapshot_changed" });
  view.funds.value = data(); view.snapshotVersion.value = "old-september";
  const oldContinuation = view.loadMore("book");
  calls[0].reject(snapshotChanged()); await Vue.nextTick();
  assert.equal(refreshes.length, 1);

  view.route.query.period = "2026-10"; view.selectedPeriod.value = "2026-10";
  await Vue.nextTick();
  view.route.query.period = "2026-09"; view.selectedPeriod.value = "2026-09";
  await Vue.nextTick();
  const newFirstPage = view.loadFunds("2026-09");
  calls[1].resolve(response(data(), "new-september")); await newFirstPage;
  const newContinuation = view.loadMore("book");
  calls[2].reject(snapshotChanged()); await Vue.nextTick();
  assert.equal(refreshes.length, 2);

  refreshes[0].resolve(); await oldContinuation;
  assert.equal(view.updateNotice.value, "资料已更新，正在重新读取。", "late old refresh must not finish the newer pending notice");
  assert.equal(view.funds.value, null);
  assert.equal(view.snapshotVersion.value, "");
  assert.equal(calls.length, 3, "old refresh must not issue another first page");

  refreshes[1].resolve(); await Vue.nextTick();
  assert.equal(calls[3].query.cursor, undefined);
  assert.equal(view.updateNotice.value, "资料已更新，正在重新读取。", "context completion alone must not announce successful data loading");
  calls[3].resolve(response(data("bank-b", null), "latest-september")); await newContinuation;
  assert.equal(view.snapshotVersion.value, "latest-september");
  assert.equal(view.updateNotice.value, "资料已更新，已重新读取当前筛选。");
});

test("a snapshot recovery failure reports retry guidance for either context or first-page failure", async () => {
  for (const failureAt of ["context", "first-page"]) {
    let requested = 0;
    const view = await fundsView(async () => {
      requested += 1;
      if (requested === 1) throw Object.assign(new Error("changed"), { code: "dashboard_snapshot_changed" });
      throw new Error("first-page failed");
    }, async () => { if (failureAt === "context") throw new Error("context failed"); });
    view.funds.value = data(); view.snapshotVersion.value = "old";
    await view.loadMore("book");
    assert.equal(view.updateNotice.value, "资料已更新，请重新读取当前筛选。", failureAt);
    assert.match(view.requestError.value, /failed/);
    assert.equal(view.funds.value, null);
    assert.equal(view.snapshotVersion.value, "");
  }
});
