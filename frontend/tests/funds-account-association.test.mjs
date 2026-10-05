import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { createServer } from "vite";

test("funds validate the selected account even when its movement page is empty", async () => {
  const samples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));
  const original = structuredClone(samples.cash_funds.response);
  const first = original.data.collections.accounts.items[0];
  const selected = { type: first.type, account_id: first.account_id };
  original.data.selected_movement_account = selected;
  original.data.collections.movements = {
    items: [], page: { total_count: original.data.movement_count, filtered_count: 0,
      returned_count: 0, has_more: false, next_cursor: null },
  };
  const server = await createServer({
    root: fileURLToPath(new URL("..", import.meta.url)), configFile: false,
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom",
  });
  const previousWindow = globalThis.window, previousFetch = globalThis.fetch;
  globalThis.window = { location: { origin: "http://localhost", search: `?company_id=${original.read_context.company_id}` } };
  let value = original;
  globalThis.fetch = async () => new Response(JSON.stringify(value));
  try {
    const api = await server.ssrLoadModule("/src/api/funds.ts");
    const period = original.selected_period.key;
    const explicit = { movement_account_type: selected.type, movement_account_id: selected.account_id };
    await api.fetchFundsDashboard(period, undefined, { movement_account_selection: "first" });
    await api.fetchFundsDashboard(period, undefined, explicit);
    value = structuredClone(original);
    value.data.selected_movement_account.account_id = "wrong-account";
    await assert.rejects(api.fetchFundsDashboard(period, undefined, { movement_account_selection: "first" }), error => error.status === 502);
    await assert.rejects(api.fetchFundsDashboard(period, undefined, explicit), error => error.status === 502);
    value.data.selected_movement_account = null;
    await assert.rejects(api.fetchFundsDashboard(period, undefined, explicit), error => error.status === 502);
    await api.fetchFundsDashboard(period);
    value.data.collections.accounts = { items: [], page: { total_count: 0, filtered_count: 0,
      returned_count: 0, has_more: false, next_cursor: null } };
    await api.fetchFundsDashboard(period, undefined, { movement_account_selection: "first" });
    value.data.selected_movement_account = selected;
    await assert.rejects(api.fetchFundsDashboard(period, undefined, { movement_account_selection: "first" }), error => error.status === 502);
  } finally {
    await server.close(); globalThis.window = previousWindow; globalThis.fetch = previousFetch;
  }
});
