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

const job = (overrides = {}) => ({
  id: "j1", kind: "report_export", status: "succeeded", attempts: 1,
  error_code: null, error_message: null, download_available: true, download_file_name: "report.xlsx",
  delivery_status: "verified", delivery_message: null, ...overrides,
});
const jobsResponse = (companyId, items) => ({
  schema_version: 2, company_id: companyId, database_id: `db-${companyId}`, items,
});

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

test("large cents remain exact while browser jobs expose no raw result payload", async () => {
  const jobs = [job()];
  globalThis.fetch = async (_url, options) => {
    assert.equal(options.credentials, "same-origin");
    return new Response(JSON.stringify(jobsResponse("company-1", jobs)));
  };
  assert.equal((await api.fetchLocalJobs("company-1")).at(0).id, "j1");
  assert.equal(money.formatFen("9007199254740993"), "¥90,071,992,547,409.93");
  const malformed = { ...jobsResponse("company-1", jobs), items: [{ ...jobs[0], result: { total_fen: "9007199254740993" } }] };
  globalThis.fetch = async () => new Response(JSON.stringify(malformed));
  await assert.rejects(api.fetchLocalJobs("company-1"), { code: "LOCAL_JOBS_RESPONSE" });
});

test("expired identity directs the owner to the native window", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ status: "rejected", code: "OWNER_SESSION_EXPIRED" }), { status: 401 });
  await assert.rejects(api.fetchLocalJobs("company-1"), (error) => error.message.includes("本机安全窗口"));
});

test("unlaunched browser is directed to the local launcher", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ code: "launcher_required" }), { status: 403 });
  await assert.rejects(api.localSecurity("session_status"), (error) => error.message.includes("记账启动器"));
});


test("recent jobs request is bounded, scoped to one company and read only", async () => {
  const controller = new AbortController();
  const jobs = [job({ kind: "portable_backup", status: "pending", attempts: 0,
    download_available: false, download_file_name: null, delivery_status: "pending" })];
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "/api/local/jobs?company_id=company-2&limit=20");
    assert.equal(options.method, undefined);
    assert.equal(options.body, undefined);
    assert.equal(options.signal, controller.signal);
    assert.equal(options.credentials, "same-origin");
    return new Response(JSON.stringify(jobsResponse("company-2", jobs)));
  };
  assert.deepEqual(await api.fetchLocalJobs("company-2", controller.signal), jobs);
});

test("queued, running, failed and unknown jobs never imply completion", () => {
  for (const status of ["pending", "running", "failed", "unrecognized"]) {
    assert.notEqual(api.localJobStatus(status), "已完成");
  }
  assert.equal(api.localJobStatus("succeeded"), "已完成");
  assert.equal(api.localJobName("payment_export"), "银行代发文件");
  assert.equal(api.localJobName("unknown"), "后台任务");
  assert.match(api.localJobMessage({ status: "failed", attempts: 3, last_error: "SECRET_RAW_ERROR" }), /自动重试次数已用尽/);
  assert.doesNotMatch(api.localJobMessage({ status: "failed", attempts: 1, last_error: "SECRET_RAW_ERROR" }), /SECRET_RAW_ERROR/);
  assert.match(api.localJobMessage({ status: "succeeded", delivery_status: "invalid", delivery_message: "报表文件校验失败，请重新生成" }), /校验失败/);
  assert.doesNotMatch(api.localJobMessage({ status: "succeeded", delivery_status: "invalid" }), /文件已生成/);
});


test("an exact background job is queried even when outside the recent list", async () => {
  globalThis.fetch = async (url) => {
    assert.equal(url, "/api/local/jobs?company_id=company-2&job_id=older-job&limit=1");
    return new Response(JSON.stringify(jobsResponse("company-2", [job({ id: "older-job", status: "running",
      download_available: false, download_file_name: null, delivery_status: "pending" })])));
  };
  assert.equal((await api.fetchLocalJob("company-2", "older-job"))[0].id, "older-job");
});

test("missing money is explicitly unavailable rather than shown as zero", () => {
  assert.equal(money.formatFen(null), "暂无法确定");
  assert.equal(money.formatPositiveFen(undefined), "未提供");
  assert.equal(money.formatFen("0"), "¥0.00");
});

test("only successful report tasks explicitly approved by the service offer downloads", () => {
  const job = { kind: "report_export", status: "succeeded", download_available: true };
  assert.equal(api.localJobDownloadAvailable(job), true);
  for (const change of [{ download_available: false }, { download_available: undefined }, { status: "running" }, { status: "failed" }, { kind: "portable_backup" }]) {
    assert.equal(api.localJobDownloadAvailable({ ...job, ...change }), false);
  }
});
