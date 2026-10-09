import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { createServer } from "vite";
const samples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));

test("group API validates company, month, snapshot, section, group and exact paired vouchers", async () => {
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true }, server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  const oldWindow = globalThis.window, oldFetch = globalThis.fetch;
  try {
    const { fetchBriefGroup } = await server.ssrLoadModule("/src/api/briefGroup.ts");
    for (const sample of Object.values(samples).filter(item => item.command === "dashboard_brief_group")) {
      const value = structuredClone(sample.response), company = value.read_context.company_id, period = value.selected_period.key;
      globalThis.window = { location: { origin: "http://offline.invalid", search: `?company_id=${company}` } };
      let payload = value;
      globalThis.fetch = async path => {
        const query = new URL(path, window.location.origin).searchParams;
        assert.equal(query.get("company_id"), company); assert.equal(query.get("limit"), "20");
        return new Response(JSON.stringify(payload));
      };
      const read = () => fetchBriefGroup(company, period, value.data.section, value.data.group_key, value.snapshot_version);
      await read();
      for (const mutate of [
        item => item.read_context.company_id = "wrong",
        item => item.selected_period.key = "2000-01",
        item => item.snapshot_version = "wrong",
        item => item.data.group_key = "wrong",
        item => item.data.collections.members.items[0].group_key = "wrong",
        item => item.data.collections.members.page.returned_count++,
      ]) {
        payload = structuredClone(value); mutate(payload);
        await assert.rejects(read(), error => error.code === "DASHBOARD_SCHEMA_MISMATCH");
      }
      if (value.data.collections.vouchers.items.length) {
        payload = structuredClone(value); payload.data.collections.vouchers.items[0].voucher_version_id = "wrong";
        await assert.rejects(read(), error => error.code === "DASHBOARD_SCHEMA_MISMATCH");
      }
    }
  } finally { await server.close(); globalThis.window = oldWindow; globalThis.fetch = oldFetch; }
});
