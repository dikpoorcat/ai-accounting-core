import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import ts from "typescript";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createMemoryHistory, createRouter } from "vue-router";

import {
  validateDashboardContextResponse,
  validateDashboardFundsResponse,
} from "../src/api/generated/dashboardValidators.js";

const samples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));
const validators = {
  dashboard_context: validateDashboardContextResponse,
  dashboard_funds: validateDashboardFundsResponse,
};
let clientHarnessSequence = 0;

function findField(value, predicate) {
  if (!value || typeof value !== "object") return null;
  for (const [key, field] of Object.entries(value)) {
    if (predicate(key, field)) return { owner: value, key };
    const nested = findField(field, predicate);
    if (nested) return nested;
  }
  return null;
}

function changedField(response, predicate, value) {
  const changed = structuredClone(response);
  const field = findField(changed, predicate);
  assert(field, "expected fixture field was not found");
  field.owner[field.key] = value;
  return changed;
}

test("generated validators accept every current backend response branch", () => {
  assert.equal(Object.keys(samples).length, 15);
  for (const [name, sample] of Object.entries(samples)) {
    const validator = validators[sample.command];
    assert(validator, `${name}: missing validator`);
    assert.equal(validator(sample.response), true, `${name}: ${JSON.stringify(validator.errors)}`);
  }
  assert.equal("generated_at" in samples.empty_context.response, false);
  assert.equal("disclaimer" in samples.empty_context.response, false);
  assert.equal(samples.funds_without_period.response.data, null);
  assert.equal(samples.deferred_funds.response.data.period_preparation, null);
  assert.notEqual(samples.cash_funds.response.data.period_preparation, null);
});

test("generated funds validator enforces canonical int64 strings without coercing counts", () => {
  const response = samples.bank_funds.response;
  const money = (key, value) => key.endsWith("_fen") && typeof value === "string";
  for (const value of ["9223372036854775807", "-9223372036854775808"]) {
    assert.equal(validateDashboardFundsResponse(changedField(response, money, value)), true, value);
  }
  for (const value of ["9223372036854775808", "-9223372036854775809", "01", "-0", "1\n", " 1", "1 ", 1, 1.5, true]) {
    assert.equal(validateDashboardFundsResponse(changedField(response, money, value)), false, String(value));
  }
  assert.equal(validateDashboardFundsResponse(changedField(response, key => key === "movement_count", "1")), false);
});

async function clientHarness(response, companyId = response.current_company?.company_id ?? "company-from-location") {
  const source = readFileSync(new URL("../src/api/client.ts", import.meta.url), "utf8")
    .replace(/import[\s\S]*?from "[^"]+";/g, "");
  const calls = [];
  const environment = {
    window: { location: { origin: "http://dashboard.invalid", search: `?company_id=${companyId}` } },
    LocalApiError: class LocalApiError extends Error {},
    requestLocalJson(path) { calls.push(path); return Promise.resolve(response); },
    verifyMoneyStrings() { assert.fail("generated response must not use the legacy money-field heuristic"); },
    validDashboardContract() { assert.fail("generated response must not use the legacy dashboard shape check"); },
    validateDashboardContextResponse,
    validateDashboardFundsResponse,
  };
  const prefix = `const { window, LocalApiError, requestLocalJson, verifyMoneyStrings, validDashboardContract, validateDashboardContextResponse, validateDashboardFundsResponse } = environment;\n`;
  const { outputText } = ts.transpileModule(`const environment = globalThis.dashboardGeneratedClient;\n${prefix}${source}`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
  });
  globalThis.dashboardGeneratedClient = environment;
  try {
    return { client: await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}#${++clientHarnessSequence}`), calls };
  } finally {
    delete globalThis.dashboardGeneratedClient;
  }
}

test("context and funds request and validate with the company-complete final URL", async () => {
  const context = await clientHarness(samples.company_with_period.response);
  assert.equal((await context.client.requestDashboardContext()).schema_version, 2);
  assert.equal(context.calls[0], `/api/dashboard/context?company_id=${samples.company_with_period.response.current_company.company_id}`);

  const funds = await clientHarness(samples.bank_funds.response);
  assert.equal((await funds.client.requestDashboardFunds("/api/dashboard/funds?period=2026-09")).schema_version, 4);
  assert.equal(funds.calls[0], "/api/dashboard/funds?period=2026-09&company_id=company-from-location");

  const malformed = await clientHarness(changedField(samples.bank_funds.response, (key, value) => key.endsWith("_fen") && typeof value === "string", 100));
  await assert.rejects(
    malformed.client.requestDashboardFunds("/api/dashboard/funds?period=2026-09"),
    error => error.code === "DASHBOARD_SCHEMA_MISMATCH",
  );
});

test("generated requests reject responses from a different selection or inconsistent page", async () => {
  const wrongCompany = await clientHarness(samples.company_with_period.response, "different-company");
  await assert.rejects(wrongCompany.client.requestDashboardContext(), error => error.code === "DASHBOARD_SCHEMA_MISMATCH");

  const validFunds = samples.page_movements.response;
  for (const path of [
    "/api/dashboard/funds?period=2026-02&section=movements",
    `/api/dashboard/funds?period=2026-01&section=movements&expected_version=wrong`,
    "/api/dashboard/funds?period=2026-01&section=accounts",
  ]) {
    const harness = await clientHarness(validFunds);
    await assert.rejects(harness.client.requestDashboardFunds(path), error => error.code === "DASHBOARD_SCHEMA_MISMATCH");
  }

  const inconsistent = structuredClone(validFunds);
  inconsistent.data.collections.movements.page.returned_count += 1;
  const pageHarness = await clientHarness(inconsistent);
  await assert.rejects(
    pageHarness.client.requestDashboardFunds("/api/dashboard/funds?period=2026-01&section=movements"),
    error => error.code === "DASHBOARD_SCHEMA_MISMATCH",
  );

  const missingAlias = structuredClone(validFunds);
  delete missingAlias.data.movement_page;
  const missingAliasHarness = await clientHarness(missingAlias);
  await assert.rejects(
    missingAliasHarness.client.requestDashboardFunds("/api/dashboard/funds?period=2026-01&section=movements"),
    error => error.code === "DASHBOARD_SCHEMA_MISMATCH",
  );

  for (const [hasMore, nextCursor] of [[false, "unexpected"], [true, null]]) {
    const cursorMismatch = structuredClone(validFunds);
    cursorMismatch.data.collections.movements.page.has_more = hasMore;
    cursorMismatch.data.collections.movements.page.next_cursor = nextCursor;
    const cursorHarness = await clientHarness(cursorMismatch);
    await assert.rejects(
      cursorHarness.client.requestDashboardFunds("/api/dashboard/funds?period=2026-01&section=movements"),
      error => error.code === "DASHBOARD_SCHEMA_MISMATCH",
    );
  }

  const filteredAlias = structuredClone(samples.bank_funds.response);
  filteredAlias.data.collections.statements.page.total_count += 1;
  const filteredHarness = await clientHarness(filteredAlias);
  assert.equal(
    (await filteredHarness.client.requestDashboardFunds("/api/dashboard/funds?period=2026-09")).schema_version,
    4,
  );
});

test("current backend samples also satisfy request-dependent context and pagination rules", async () => {
  for (const name of ["empty_context", "company_without_period", "company_with_period"]) {
    const response = samples[name].response;
    const companyId = response.current_company?.company_id;
    const harness = await clientHarness(response, companyId ?? "");
    const path = companyId ? `/api/dashboard/context?company_id=${companyId}` : "/api/dashboard/context";
    assert.equal((await harness.client.requestDashboardContext()).schema_version, 2, name);
    assert.equal(harness.calls[0], path, name);
  }

  const filteredAccountId = samples.filtered_bank_funds.response.data.movements[0].account_id;
  const fundsRequests = {
    funds_without_period: "/api/dashboard/funds",
    cash_funds: "/api/dashboard/funds?period=2026-01",
    deferred_funds: "/api/dashboard/funds?period=2026-01&preparation=deferred",
    page_accounts: "/api/dashboard/funds?period=2026-01&section=accounts",
    page_movements: "/api/dashboard/funds?period=2026-01&section=movements",
    page_statements: "/api/dashboard/funds?period=2026-01&section=statements",
    page_investment_products: "/api/dashboard/funds?period=2026-01&section=investment_products",
    page_investment_events: "/api/dashboard/funds?period=2026-01&section=investment_events",
    account_filter: "/api/dashboard/funds?period=2026-01",
    bank_funds: "/api/dashboard/funds?period=2026-09",
    filtered_bank_funds: `/api/dashboard/funds?period=2026-09&movement_account_type=bank&movement_account_id=${filteredAccountId}&statement_account_id=${filteredAccountId}&limit=1`,
    frozen_funds: "/api/dashboard/funds?period=2026-09",
  };
  for (const [name, path] of Object.entries(fundsRequests)) {
    const harness = await clientHarness(samples[name].response, "");
    let response;
    try {
      response = await harness.client.requestDashboardFunds(path);
    } catch (error) {
      assert.fail(`${name}: ${error.code ?? error}`);
    }
    assert.equal(response.schema_version, 4, name);
    assert.equal(harness.calls[0], path, name);
  }
});

test("current funds samples pass the API consumer and render bank, filter, and empty-period branches", async () => {
  const previousWindow = globalThis.window;
  globalThis.window = { location: { origin: "http://localhost", search: "" } };
  const server = await createServer({
    root: fileURLToPath(new URL("..", import.meta.url)),
    configFile: false,
    optimizeDeps: { noDiscovery: true },
    plugins: [{
      name: "seed-current-funds-contract",
      enforce: "pre",
      transform(code, id) {
        if (!id.replaceAll("\\", "/").endsWith("/src/views/FundsView.vue")) return;
        return code
          .replace('const selectedPeriod = ref("")', "const selectedPeriod = ref(globalThis.currentFundsPeriod)")
          .replace("const funds = ref<FundsData | null>(null)", "const funds = ref(globalThis.currentFundsData)")
          .replace("const initializing = ref(true)", "const initializing = ref(false)")
          .replace(
            "const periods = computed(() => context.value?.periods ?? []);",
            'const periods = computed(() => globalThis.currentFundsPeriod ? [{ key: globalThis.currentFundsPeriod, year: 2026, month: 9, label: "2026 年 9 月", short_label: "9 月", status: "open", start_date: "2026-09-01", end_date: "2026-09-30", closed_at: null }] : []);',
          )
          .replaceAll("{ immediate: true }", "{ immediate: false }");
      },
    }, vue()],
    server: { middlewareMode: true, hmr: false, ws: false },
    appType: "custom",
  });
  try {
    const { requestDashboardFunds } = await server.ssrLoadModule("/src/api/client.ts");
    const { default: FundsView } = await server.ssrLoadModule("/src/views/FundsView.vue");
    const filteredAccountId = samples.filtered_bank_funds.response.data.movements[0].account_id;
    const cases = [
      {
        name: "bank_funds",
        request: "/api/dashboard/funds?period=2026-09",
        route: "/?period=2026-09&funds_view=bank",
        expected: [/月末账面资金/, /other-row/, /账户 1/],
      },
      {
        name: "filtered_bank_funds",
        request: `/api/dashboard/funds?period=2026-09&movement_account_type=bank&movement_account_id=${filteredAccountId}&statement_account_id=${filteredAccountId}&limit=1`,
        route: `/?period=2026-09&funds_view=bank&statement_account_id=${filteredAccountId}`,
        expected: [/other-row/, /所选银行账户（名称尚未加载）/, /账户 2/],
      },
      {
        name: "funds_without_period",
        request: "/api/dashboard/funds",
        route: "/",
        expected: [/还没有可查看的资金月份/],
      },
    ];
    for (const current of cases) {
      globalThis.fetch = async () => new Response(JSON.stringify(samples[current.name].response));
      let accepted;
      try {
        accepted = await requestDashboardFunds(current.request);
      } catch (error) {
        assert.fail(`${current.name}: ${error.code ?? error}`);
      }
      globalThis.currentFundsData = accepted.data;
      globalThis.currentFundsPeriod = accepted.selected_period?.key ?? "";
      const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/", component: {} }] });
      await router.push(current.route);
      const app = createSSRApp(FundsView);
      app.use(router);
      const html = await renderToString(app);
      for (const expected of current.expected) assert.match(html, expected, current.name);
    }
  } finally {
    await server.close();
    delete globalThis.currentFundsData;
    delete globalThis.currentFundsPeriod;
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});
