import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import ts from "typescript";

async function importTypeScript(relative) {
  const source = readFileSync(new URL(relative, import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
  });
  return import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
}

const api = await importTypeScript("../src/api/localKernel.ts");
const money = await importTypeScript("../src/utils/money.ts");

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
    return new Response(JSON.stringify({ request_id: "request-1", status: "waiting_for_user" }));
  };
  assert.equal((await api.localSecurity("request", { kind: "login" })).status, "waiting_for_user");
});

test("large cents remain exact and numeric money responses are rejected", async () => {
  const overview = { accounts: [{ account: "1002", debit: "9007199254740993", credit: "0" }] };
  globalThis.fetch = async (_url, options) => {
    assert.equal(options.credentials, "same-origin");
    return new Response(JSON.stringify(overview));
  };
  assert.equal((await api.fetchLocalOverview("company-1", "2026-09")).accounts[0].debit, "9007199254740993");
  assert.equal(money.formatFen("9007199254740993"), "¥90,071,992,547,409.93");
  overview.accounts[0].debit = 9007199254740992;
  await assert.rejects(api.fetchLocalOverview("company-1", "2026-09"), { code: "LOCAL_MONEY_FORMAT" });
});

test("expired identity directs the owner to the native window", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ status: "rejected", code: "OWNER_SESSION_EXPIRED" }), { status: 401 });
  await assert.rejects(api.fetchLocalCompanies(), (error) => error.message.includes("本机安全窗口"));
});

test("unlaunched browser is directed to the local launcher", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ code: "launcher_required" }), { status: 403 });
  await assert.rejects(api.localSecurity("session_status"), (error) => error.message.includes("记账启动器"));
});


test("recent jobs request is bounded, scoped to one company and read only", async () => {
  const controller = new AbortController();
  const jobs = [{ id: "j1", kind: "portable_backup", status: "pending", attempts: 0, last_error: null, result: null }];
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "/api/local/jobs?company_id=company-2&limit=20");
    assert.equal(options.method, undefined);
    assert.equal(options.body, undefined);
    assert.equal(options.signal, controller.signal);
    assert.equal(options.credentials, "same-origin");
    return new Response(JSON.stringify(jobs));
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
    return new Response(JSON.stringify([{ id: "older-job", status: "running" }]));
  };
  assert.equal((await api.fetchLocalJob("company-2", "older-job"))[0].id, "older-job");
});

test("missing money is explicitly unavailable rather than shown as zero", () => {
  assert.equal(money.formatFen(null), "暂无法确定");
  assert.equal(money.formatPositiveFen(undefined), "未提供");
  assert.equal(money.formatFen("0"), "¥0.00");
});

test("report check counts remain integers while monetary totals require strings", () => {
  api.verifyMoneyStrings({ checks: { passed: 2, total: 3 }, summary: { current_net_profit_fen: "9007199254740993" } });
  assert.throws(() => api.verifyMoneyStrings({ checks: { passed: 2, total: 3 }, summary: { current_net_profit_fen: 100 } }), { code: "LOCAL_MONEY_FORMAT" });
});

test("employee field provenance is distinct from money while actual nested amounts remain strict", () => {
  const provenance = (field) => ({ source_type: "fact", id: "synthetic-profile", revision: 1,
    field, source: null, evidence_digest: null, evidence: [], basis: "frozen", recorded_at: null });
  const employee = {
    social_insurance_base_fen: "9007199254740993", housing_fund_base_fen: null, declared_tax_fen: "0",
    field_sources: {
      social_insurance_base_fen: provenance("social_insurance_base_fen"),
      housing_fund_base_fen: provenance("housing_fund_base_fen"),
      declared_tax_fen: provenance("declared_tax_fen"),
    },
  };
  const payload = { data: { employees: { items: [employee] }, collections: { employees: { items: [employee] } } } };
  const before = structuredClone(payload);
  api.verifyMoneyStrings(payload);
  assert.deepEqual(payload, before);
  for (const invalid of [100, { source_type: "fact", id: "not-a-money-value" }]) {
    assert.throws(() => api.verifyMoneyStrings({ social_insurance_base_fen: invalid }), { code: "LOCAL_MONEY_FORMAT" });
  }
  assert.throws(() => api.verifyMoneyStrings({ field_sources: {
    social_insurance_base_fen: { ...provenance("social_insurance_base_fen"), source: { amount_fen: 100 } },
  } }), { code: "LOCAL_MONEY_FORMAT" });
});

test("only successful report tasks explicitly approved by the service offer downloads", () => {
  const job = { kind: "report_export", status: "succeeded", download_available: true };
  assert.equal(api.localJobDownloadAvailable(job), true);
  for (const change of [{ download_available: false }, { download_available: undefined }, { status: "running" }, { status: "failed" }, { kind: "portable_backup" }]) {
    assert.equal(api.localJobDownloadAvailable({ ...job, ...change }), false);
  }
});
