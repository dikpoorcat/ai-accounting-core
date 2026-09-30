import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const samples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};
const cases = [
  {
    name: "business details", module: "businessStatus", validator: "dashboardBusinessStatus",
    response: () => structuredClone(samples.business_status.response),
    invoke: (api, value, signal) => api.fetchBusinessStatus(value.selected_period.key, value.data.identity.subject_id, signal),
    company: value => value.read_context.company_id,
  },
  {
    name: "period preparation", module: "periodPreparation", validator: "dashboardPeriodPreparation",
    response: () => structuredClone(samples.period_preparation.response),
    invoke: (api, value, signal) => api.fetchPeriodPreparation(value.read_context, value.period, signal),
    company: value => value.read_context.company_id,
  },
  {
    name: "background jobs", module: "localKernel", validator: "browserJobs",
    response: () => ({ schema_version: 2, company_id: "company-a", database_id: "database-a", items: [] }),
    invoke: (api, value, signal) => api.fetchLocalJobs(value.company_id, signal),
    company: value => value.company_id,
  },
  {
    name: "report export", module: "reports", validator: "reportExportReceipt",
    response: () => ({ status: "queued", job_id: "job-a", preview_digest: "preview-a" }),
    invoke(api, _value, signal) {
      const report = structuredClone(samples.quarterly_report.response);
      report.export = { ...report.export, available: true, preview_digest: "preview-a", epochs: { accounting: 1, material: 2, management: 3 } };
      return api.requestQuarterlyExport("company-a", report, "request-a", signal);
    },
    company: () => "company-a",
  },
];

async function harness(item) {
  const entered = deferred(), release = deferred(), loaded = [];
  const server = await createServer({
    root: fileURLToPath(new URL("..", import.meta.url)), configFile: false,
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom",
    plugins: [{
      name: "observe-optional-module-loading",
      async load(id) {
        if (id.endsWith(`/generated/${item.validator}.js`)) {
          loaded.push(id);
          entered.resolve();
          await release.promise;
        }
      },
    }],
  });
  const api = await server.ssrLoadModule(`/src/api/${item.module}.ts`);
  assert.deepEqual(loaded, [], "merely loading a page API must not load an optional validator");
  return { api, entered, release, loaded, close: async () => { release.resolve(); await server.close(); } };
}

for (const item of cases) {
  test(`${item.name}: load only on use, preserve company and validate every response`, async () => {
    const h = await harness(item);
    const value = item.response(), company = item.company(value), calls = [];
    const oldWindow = globalThis.window, oldFetch = globalThis.fetch;
    globalThis.window = { location: { origin: "http://offline.invalid", search: `?company_id=${company}` } };
    globalThis.fetch = async (path, options) => {
      calls.push({ path, options });
      const submittedCompany = options.body ? JSON.parse(options.body).company_id : new URL(path, window.location.origin).searchParams.get("company_id");
      assert.equal(submittedCompany, company);
      return new Response(JSON.stringify(value));
    };
    try {
      const pending = item.invoke(h.api, value);
      await h.entered.promise;
      assert.equal(calls.length, 0, "load failure must not leave a submitted write or unvalidated read");
      window.location.search = "?company_id=other-company";
      h.release.resolve();
      await pending;
      assert.equal(calls.length, 1);
      assert.equal(h.loaded.length, 1);
      window.location.search = `?company_id=${company}`;
      globalThis.fetch = async () => new Response(JSON.stringify({ ...value, unexpected: "reject" }));
      await assert.rejects(item.invoke(h.api, value), error => error.status === 502);
    } finally {
      await h.close(); globalThis.window = oldWindow; globalThis.fetch = oldFetch;
    }
  });

  test(`${item.name}: switching away during module load cancels before sending`, async () => {
    const h = await harness(item), value = item.response(), controller = new AbortController();
    const oldWindow = globalThis.window, oldFetch = globalThis.fetch;
    let sent = 0;
    globalThis.window = { location: { origin: "http://offline.invalid", search: `?company_id=${item.company(value)}` } };
    globalThis.fetch = async () => { sent++; return new Response(JSON.stringify(value)); };
    try {
      const pending = item.invoke(h.api, value, controller.signal);
      const rejected = assert.rejects(pending, { name: "AbortError" });
      await h.entered.promise;
      controller.abort(); h.release.resolve();
      await rejected;
      assert.equal(sent, 0);
    } finally {
      await h.close(); globalThis.window = oldWindow; globalThis.fetch = oldFetch;
    }
  });
}

test("period preparation checks month, version, date and database against the captured scope", async () => {
  const item = cases[1], h = await harness(item), original = item.response();
  const oldWindow = globalThis.window, oldFetch = globalThis.fetch;
  globalThis.window = { location: { origin: "http://offline.invalid", search: "?company_id=other-company" } };
  h.release.resolve();
  try {
    for (const [field, value] of [["period", "2099-03"], ["read_version", "changed"], ["as_of", "2099-03-01"], ["database_id", "changed"]]) {
      const changed = structuredClone(original);
      if (field === "period") changed.period = value;
      else changed.read_context[field] = value;
      globalThis.fetch = async () => new Response(JSON.stringify(changed));
      await assert.rejects(item.invoke(h.api, original), error => ["DASHBOARD_SCHEMA_MISMATCH", "dashboard_snapshot_changed"].includes(error.code));
    }
  } finally {
    await h.close(); globalThis.window = oldWindow; globalThis.fetch = oldFetch;
  }
});
