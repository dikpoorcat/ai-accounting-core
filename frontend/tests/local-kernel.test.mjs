import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { after, test } from "node:test";
import { createServer } from "vite";

const server = await createServer({
  root: fileURLToPath(new URL("..", import.meta.url)),
  configFile: false,
  optimizeDeps: { noDiscovery: true },
  server: { middlewareMode: true, hmr: false, ws: false },
  appType: "custom",
});
after(() => server.close());
const api = await server.ssrLoadModule("/src/api/localKernel.ts");
const money = await server.ssrLoadModule("/src/utils/money.ts");

test("reserve facts and mixed payroll use the current business labels", () => {
  assert.equal(api.localBusinessName("managed_reserve_expense"), "备用金支出");
  assert.equal(api.localBusinessName("managed_reserve_refund"), "备用金退款");
  assert.equal(api.localBusinessName("payroll_reserve_payment"), "净薪及备用金支出付款");
  for (const retired of ["managed_reserve_scope", "managed_reserve_bank_expense", "managed_reserve_obligation_settlement", "platform_boundary_disposition"]) {
    assert.equal(api.localBusinessName(retired), "其他业务");
  }
});

test("launch ticket is removed before one same-origin exchange and never reused on refresh", async () => {
  const events = [];
  globalThis.window = {
    location: { hash: "#ticket=one-use", pathname: "/", search: "?period=2026-09" },
    history: { replaceState(_state, _title, url) { events.push(["strip", url]); window.location.hash = ""; } },
  };
  globalThis.fetch = async (url, options) => {
    events.push(["fetch", url]);
    assert.equal(window.location.hash, "");
    assert.equal(options.credentials, "same-origin");
    assert.equal(options.redirect, "error");
    assert.equal(options.cache, "no-store");
    assert.deepEqual(JSON.parse(options.body), { ticket: "one-use" });
    assert.equal(options.headers.Authorization, undefined);
    return new Response(JSON.stringify({ status: "ready", authenticated: false }));
  };
  await api.consumeLocalTicket();
  await api.consumeLocalTicket();
  assert.deepEqual(events, [["strip", "/?period=2026-09"], ["fetch", "/api/browser-session"]]);
});

test("development obtains a one-use browser ticket without exposing the service capability", async () => {
  const events = [];
  globalThis.window = {
    location: { hash: "", pathname: "/", search: "" },
    history: { replaceState() { assert.fail("a generated ticket never enters the address bar"); } },
  };
  globalThis.fetch = async (url, options) => {
    events.push([url, JSON.parse(options.body)]);
    assert.equal(options.headers.Authorization, undefined);
    if (url === "/api/browser-ticket") {
      return new Response(JSON.stringify({ url: "http://127.0.0.1:54321/#ticket=development-ticket" }));
    }
    assert.equal(url, "/api/browser-session");
    return new Response(JSON.stringify({ status: "ready", authenticated: true }));
  };
  await api.consumeLocalTicket(true);
  assert.deepEqual(events, [
    ["/api/browser-ticket", {}],
    ["/api/browser-session", { ticket: "development-ticket" }],
  ]);
});

test("obsolete owner token fragments are discarded without sending them", async () => {
  window.location.hash = "#token=must-not-send";
  window.history.replaceState = () => { window.location.hash = ""; };
  globalThis.fetch = async () => { assert.fail("token must not be exchanged"); };
  await api.consumeLocalTicket();
  assert.equal(window.location.hash, "");
});

test("native security requests carry operation kind and use same-origin cookies", async () => {
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "/api/security-request");
    assert.equal(options.credentials, "same-origin");
    assert.deepEqual(JSON.parse(options.body), { operation: "request", payload: { kind: "login" } });
    assert.equal(options.headers.Authorization, undefined);
    return new Response(JSON.stringify({ schema_version: 1, request_id: "request-1", kind: "login", status: "waiting_for_user",
      catalog_instance_id: "catalog-1", error_code: null, operation_committed: null,
      login_completed: false, recovery_code_acknowledged: false }));
  };
  assert.equal((await api.localSecurity("request", { kind: "login" })).status, "waiting_for_user");
});

test("large cents remain exact", () => {
  assert.equal(money.formatFen("9007199254740993"), "¥90,071,992,547,409.93");
});

test("expired identity directs the owner to the native window", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ status: "rejected", code: "OWNER_SESSION_EXPIRED" }), { status: 401 });
  await assert.rejects(api.localSecurity("session_status"), (error) => error.message.includes("本机安全窗口"));
});

test("unlaunched browser is directed to the local launcher", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ code: "launcher_required" }), { status: 403 });
  await assert.rejects(api.localSecurity("session_status"), (error) => error.message.includes("记账启动器"));
});


test("missing money is explicitly unavailable rather than shown as zero", () => {
  assert.equal(money.formatFen(null), "暂无法确定");
  assert.equal(money.formatPositiveFen(undefined), "未提供");
  assert.equal(money.formatFen("0"), "¥0.00");
});
