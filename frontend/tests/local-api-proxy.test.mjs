import assert from "node:assert/strict";
import { createServer as createHttpServer, request as httpRequest } from "node:http";
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { after, test } from "node:test";
import { createServer } from "vite";

const root = fileURLToPath(new URL("..", import.meta.url));
const loader = await createServer({ root, configFile: false, optimizeDeps: { noDiscovery: true },
  server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
after(() => loader.close());
const { localApiProxy, localServiceStatePath, readLocalServiceMetadata } = await loader.ssrLoadModule("/local-api-proxy.ts");
const serviceProtocol = "ai-accounting-modular-service/1";
const fingerprint = value => createHash("sha256").update(value).digest("hex");

async function listen(server, port = 0) {
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", resolve);
  });
  return server.address().port;
}
async function close(server) {
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
async function fixture(t, handler) {
  const directory = await mkdtemp(join(tmpdir(), "finance-proxy-test-"));
  const statePath = localServiceStatePath(join(directory, "catalog.sqlite3"));
  const requests = [];
  const backends = [];
  const startBackend = async (custom = handler, port = 0) => {
    const backend = createHttpServer(async (req, res) => {
      let body = "";
      for await (const chunk of req) body += chunk;
      requests.push({ path: req.url, headers: req.headers, body });
      if (custom) return custom(req, res, body);
      res.setHeader("Content-Type", "application/json");
      if (req.url === "/private/browser-ticket") {
        res.end(JSON.stringify({ protocol: serviceProtocol, catalog_id: current.catalogId,
          result: { url: `http://${req.headers.host}/#ticket=synthetic-browser-ticket` } }));
        return;
      }
      res.end(JSON.stringify({ path: req.url, body }));
    });
    backends.push(backend);
    return { server: backend, port: await listen(backend, port) };
  };
  const first = await startBackend();
  let current = { port: first.port, capability: "private-test-capability-one", catalogId: "synthetic-catalog", stateText: "state-one" };
  await writeFile(statePath, current.stateText);
  let discoveryCount = 0;
  let unavailable = false;
  let gate;
  const discover = async () => {
    discoveryCount++;
    if (gate) await gate;
    if (unavailable) throw new Error(`offline ${current.capability}`);
    return { ...current, stateFingerprint: fingerprint(current.stateText) };
  };
  const plugin = await localApiProxy(statePath, discover);
  const vite = await createServer({ root, configFile: false, plugins: [plugin],
    optimizeDeps: { noDiscovery: true }, server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  vite.middlewares.use((_req, res) => { res.end("frontend-page"); });
  const frontend = createHttpServer(vite.middlewares);
  const port = await listen(frontend);
  const origin = `http://127.0.0.1:${port}`;
  const rawRequest = (path = "/api/dashboard/context", options = {}) => fetch(`${origin}${path}`, options);
  const request = (path, options = {}) => rawRequest(path, {
    ...options, headers: { Origin: origin, ...options.headers },
  });
  t.after(async () => {
    await close(frontend);
    await vite.close();
    for (const backend of backends) if (backend.listening) await close(backend);
    await rm(directory, { recursive: true, force: true });
  });
  return { first, requests, startBackend, request, rawRequest, origin, statePath,
    count: () => discoveryCount,
    setUnavailable: value => { unavailable = value; },
    setGate: value => { gate = value; },
    metadata: () => current,
    async change(overrides) { current = { ...current, ...overrides }; await writeFile(statePath, current.stateText); },
  };
}
async function unavailable(response, capability) {
  assert.equal(response.status, 503);
  const text = await response.text();
  assert.equal(JSON.parse(text).code, "local_service_unavailable");
  assert.ok(!text.includes(capability));
}

test("changed state discovers the new port and bridges only the ticket request with private capability", async t => {
  const f = await fixture(t);
  assert.equal((await f.request()).status, 200);
  const second = await f.startBackend();
  await f.change({ port: second.port, capability: "private-test-capability-two", stateText: "state-two" });
  const response = await f.request("/api/browser-ticket?development=1", {
    method: "POST", headers: { "X-Modular-Capability": "user-supplied", Cookie: "browser-cookie=untrusted" }, body: "{}",
  });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { url: `http://127.0.0.1:${second.port}/#ticket=synthetic-browser-ticket` });
  const received = f.requests.at(-1);
  assert.equal(received.path, "/private/browser-ticket");
  assert.equal(received.body, "{}");
  assert.equal(received.headers.host, `127.0.0.1:${second.port}`);
  assert.equal(received.headers.origin, undefined);
  assert.equal(received.headers.cookie, undefined);
  assert.equal(received.headers["x-modular-capability"], "private-test-capability-two");
  await f.request("/api/dashboard/context?company_id=synthetic", { headers: {
    "X-Local-Capability": "old-user-supplied", "X-Modular-Capability": "user-supplied", Cookie: "catalog-scope-session=synthetic",
  } });
  assert.equal(f.requests.at(-1).path, "/api/dashboard/context?company_id=synthetic");
  assert.equal(f.requests.at(-1).headers.origin, `http://127.0.0.1:${second.port}`);
  assert.equal(f.requests.at(-1).headers.cookie, "catalog-scope-session=synthetic");
  assert.equal(f.requests.at(-1).headers["x-local-capability"], undefined);
  assert.equal(f.requests.at(-1).headers["x-modular-capability"], undefined);
});

test("changed capability on the same port is refreshed", async t => {
  const f = await fixture(t);
  await f.change({ capability: "same-port-new-capability", stateText: "same-port-restarted" });
  assert.equal((await f.request("/api/browser-ticket", { method: "POST", body: "{}" })).status, 200);
  assert.equal(f.requests.at(-1).headers["x-modular-capability"], "same-port-new-capability");
});

test("ticket bridge rejects missing or foreign origins, a foreign Host, and non-POST requests", async t => {
  const f = await fixture(t);
  for (const options of [
    { method: "POST", body: "{}" },
    { method: "POST", headers: { Origin: "https://foreign.example" }, body: "{}" },
    { headers: { Origin: f.origin } },
  ]) {
    const response = await f.rawRequest("/api/browser-ticket", options);
    assert.equal(response.status, 403, JSON.stringify(options));
  }
  const foreignHostStatus = await new Promise((resolve, reject) => {
    const request = httpRequest(`${f.origin}/api/browser-ticket`, { method: "POST", headers: {
      Host: "foreign.example", Origin: f.origin, "Content-Length": "2",
    } }, response => { response.resume(); resolve(response.statusCode); });
    request.on("error", reject);
    request.end("{}");
  });
  assert.equal(foreignHostStatus, 403);
  assert.equal((await f.request("/api/dashboard/context", { headers: { Origin: "https://foreign.example" } })).status, 403);
  assert.equal(f.requests.length, 0);
  assert.equal(await (await f.request("/private/info")).text(), "frontend-page");
  assert.equal(await (await f.request("/apiary")).text(), "frontend-page");
});

test("ticket bridge validates private identity and URL, and never exposes the private envelope", async t => {
  let envelope;
  let status = 200;
  const f = await fixture(t, (_req, res) => {
    res.writeHead(status, { "Content-Type": "application/json", "Set-Cookie": "private-cookie=discarded" });
    res.end(JSON.stringify(envelope));
  });
  const url = `http://127.0.0.1:${f.first.port}/#ticket=synthetic-safe-ticket`;
  const valid = { protocol: serviceProtocol, catalog_id: f.metadata().catalogId,
    result: { url, capability: f.metadata().capability }, capability: f.metadata().capability };
  for (const invalid of [
    { ...valid, protocol: "old-service-protocol" },
    { ...valid, catalog_id: "foreign-catalog" },
    { ...valid, result: { url: "https://foreign.example/#ticket=unsafe" } },
    { ...valid, result: { url: `http://127.0.0.1:${f.first.port}/private/stop#ticket=unsafe` } },
    { ...valid, result: { url: `http://127.0.0.1:${f.first.port}/#ticket=unsafe&capability=secret` } },
    { ...valid, result: { url: `http://127.0.0.1:${f.first.port}/#ticket=` } },
    { ...valid, private_error: f.metadata().capability, result: null },
  ]) {
    envelope = invalid;
    await unavailable(await f.request("/api/browser-ticket", { method: "POST", body: "{}" }), f.metadata().capability);
  }
  status = 401;
  envelope = valid;
  await unavailable(await f.request("/api/browser-ticket", { method: "POST", body: "{}" }), f.metadata().capability);
  status = 200;
  const response = await f.request("/api/browser-ticket", { method: "POST", body: "{}" });
  assert.deepEqual(await response.json(), { url });
  assert.equal(response.headers.get("set-cookie"), null);
});

test("connection failure rediscovers without replaying and the next request uses the new service", async t => {
  const f = await fixture(t);
  await close(f.first.server);
  const second = await f.startBackend();
  // Leave the discovery file unchanged to exercise the connection error path.
  await f.change({ port: second.port });
  await unavailable(await f.request(), f.metadata().capability);
  assert.equal(f.requests.length, 0);
  assert.ok(f.count() >= 2);
  assert.equal((await f.request()).status, 200);
  assert.equal(f.requests.length, 1);
});

test("temporary offline discovery recovers and does not block frontend pages", async t => {
  const f = await fixture(t);
  await close(f.first.server);
  f.setUnavailable(true);
  await unavailable(await f.request(), f.metadata().capability);
  assert.equal(await (await f.request("/ordinary-page")).text(), "frontend-page");
  const second = await f.startBackend();
  await f.change({ port: second.port, stateText: "recovered" });
  f.setUnavailable(false);
  assert.equal((await f.request()).status, 200);
});

test("a replaced catalog is rejected and its endpoint receives no request", async t => {
  const f = await fixture(t);
  await f.change({ catalogId: "different-catalog", stateText: "different-root-identity" });
  await unavailable(await f.request(), f.metadata().capability);
  assert.equal(f.requests.length, 0);
});

test("concurrent requests share a discovery while state is changing", async t => {
  const f = await fixture(t);
  const before = f.count();
  let release;
  f.setGate(new Promise(resolve => { release = resolve; }));
  await f.change({ stateText: "concurrent-restart" });
  const pending = Array.from({ length: 6 }, () => f.request());
  // Wait for the callback to enter, then let all middleware checks join it.
  for (let index = 0; index < 100 && f.count() === before; index++) {
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  assert.equal(f.count(), before + 1);
  await new Promise(resolve => setTimeout(resolve, 30));
  release();
  const responses = await Promise.all(pending);
  assert.ok(responses.every(response => response.status === 200));
  assert.equal(f.count(), before + 1);
});

test("a request cancelled during discovery is not sent to the replacement service", async t => {
  const f = await fixture(t);
  const before = f.count();
  let release;
  f.setGate(new Promise(resolve => { release = resolve; }));
  await f.change({ stateText: "discovery-pending" });
  const controller = new AbortController();
  const pending = f.request("/api/cancel-before-send", { signal: controller.signal });
  const aborted = assert.rejects(pending, error => error.name === "AbortError");
  for (let index = 0; index < 100 && f.count() === before; index++) {
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  assert.equal(f.count(), before + 1);
  controller.abort();
  await aborted;
  // The server must observe the closed client before discovery completes.
  await new Promise(resolve => setTimeout(resolve, 30));
  release();
  assert.equal((await f.request()).status, 200);
  assert.equal(f.requests.length, 1);
  assert.equal(f.requests[0].path, "/api/dashboard/context");
});

test("a POST whose backend disconnects after receipt is never sent again", async t => {
  const f = await fixture(t, (req) => req.socket.destroy());
  const response = await f.request("/api/security-request", { method: "POST", body: JSON.stringify({ operation: "request" }) });
  await unavailable(response, f.metadata().capability);
  assert.equal(f.requests.length, 1);
  assert.equal(f.requests[0].body, JSON.stringify({ operation: "request" }));
});

test("malformed or missing state fails closed without disclosing private state", async t => {
  const f = await fixture(t);
  f.setUnavailable(true);
  await writeFile(f.statePath, "invalid-state-private-test-capability-one");
  await unavailable(await f.request(), f.metadata().capability);
  assert.equal(f.requests.length, 0);
  await rm(f.statePath);
  await unavailable(await f.request(), f.metadata().capability);
  assert.equal(await (await f.request("/apiary")).text(), "frontend-page");
});


test("real V3 discovery decrypts synthetic state and rejects catalog, protocol and state races", {
  skip: process.platform !== "win32", timeout: 60_000,
}, async t => {
  const directory = await mkdtemp(join(tmpdir(), "finance-modular-discovery-test-"));
  const catalogPath = join(directory, "catalog.sqlite3");
  const statePath = localServiceStatePath(catalogPath);
  const capability = "synthetic-private-cli-capability-with-32-characters";
  let identity;
  let infoOverrides = {};
  let alterDuringInfo = false;
  let infoRequests = 0;
  let baseline;
  const backend = createHttpServer(async (req, res) => {
    infoRequests++;
    assert.equal(req.url, "/private/info");
    assert.equal(req.method, "POST");
    assert.equal(req.headers["x-modular-capability"], capability);
    assert.equal(req.headers.origin, undefined);
    if (alterDuringInfo) await writeFile(statePath, Buffer.concat([baseline, Buffer.from([0])]));
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ protocol: serviceProtocol, catalog_id: identity.catalog_id,
      result: identity, ...infoOverrides }));
  });
  const port = await listen(backend);
  t.after(async () => { await close(backend); await rm(directory, { recursive: true, force: true }); });
  const script = `
import json, sys
from pathlib import Path
from uuid import uuid4
from ai_accounting_modular.storage.public import OwnerProfile, Storage, create_catalog
from ai_accounting_modular.host.runtime import _state
catalog = Path(sys.argv[1]).absolute()
if not catalog.exists():
    create_catalog(catalog, OwnerProfile(str(uuid4()), "Synthetic owner", "synthetic-password-hash"))
identity = {"catalog_id": Storage(catalog).catalog_identity(), "catalog_path": str(catalog),
            "protocol": _state.PROTOCOL, "port": int(sys.argv[2])}
metadata = {**identity, "capability": "synthetic-private-cli-capability-with-32-characters", **json.loads(sys.argv[3])}
_state.publish(catalog, metadata)
print(json.dumps(identity))
`;
  const repositoryRoot = join(root, "..");
  const publish = async (overrides = {}) => {
    const { stdout } = await promisify(execFile)(join(repositoryRoot, ".tmp-kernel-venv", "Scripts", "python.exe"),
      ["-I", "-X", "utf8", "-c", script, catalogPath, String(port), JSON.stringify(overrides)],
      { cwd: repositoryRoot, encoding: "utf8", timeout: 20_000, windowsHide: true });
    assert.ok(!stdout.includes(capability));
    return JSON.parse(stdout);
  };
  identity = await publish();
  baseline = await readFile(statePath);
  assert.ok(!baseline.includes(Buffer.from(capability)), "Windows service state must remain encrypted");
  const verified = await readLocalServiceMetadata(repositoryRoot, catalogPath);
  assert.deepEqual(verified, { port, capability, catalogId: identity.catalog_id, stateFingerprint: fingerprint(baseline) });
  const rejectSafely = async () => assert.rejects(readLocalServiceMetadata(repositoryRoot, catalogPath), error => {
    assert.ok(!String(error).includes(capability));
    return /本地会计服务不可用/.test(error.message);
  });
  for (const overrides of [{ protocol: "ai-accounting-kernel/2" }, { catalog_id: "different-catalog" },
    { catalog_path: join(directory, "other.sqlite3") }]) {
    const before = infoRequests;
    await publish(overrides);
    await rejectSafely();
    assert.equal(infoRequests, before, "invalid state must fail before contacting its port");
  }
  await writeFile(statePath, baseline);
  for (const overrides of [{ protocol: "different-protocol" }, { catalog_id: "different-live-catalog" }, { result: undefined }]) {
    infoOverrides = overrides;
    await rejectSafely();
  }
  infoOverrides = {};
  alterDuringInfo = true;
  await rejectSafely();
  alterDuringInfo = false;
  await writeFile(statePath, baseline);
  assert.equal((await readLocalServiceMetadata(repositoryRoot, catalogPath)).catalogId, identity.catalog_id);
});

test("streamed binary chunks, multiple cookies and business errors pass through without discovery", async t => {
  const payload = Buffer.from([0, 255, 1, 128, 13, 10, 0, 42]);
  const f = await fixture(t, (req, res) => {
    if (req.url === "/api/business-error") {
      res.writeHead(409, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ status: "rejected", code: "preview_expired" }));
      return;
    }
    res.writeHead(200, { "Content-Type": "application/octet-stream", "Set-Cookie": [
      "finance_session=synthetic-session; HttpOnly; SameSite=Strict; Path=/",
      "finance_surface=synthetic-surface; HttpOnly; SameSite=Strict; Path=/",
    ] });
    res.write(payload.subarray(0, 3));
    setImmediate(() => { res.write(payload.subarray(3, 6)); res.end(payload.subarray(6)); });
  });
  const baseline = f.count();
  const response = await f.request("/api/report-file");
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("content-type"), "application/octet-stream");
  assert.deepEqual(response.headers.getSetCookie(), [
    "finance_session=synthetic-session; HttpOnly; SameSite=Strict; Path=/",
    "finance_surface=synthetic-surface; HttpOnly; SameSite=Strict; Path=/",
  ]);
  assert.deepEqual(Buffer.from(await response.arrayBuffer()), payload);
  const businessError = await f.request("/api/business-error");
  assert.equal(businessError.status, 409);
  assert.deepEqual(await businessError.json(), { status: "rejected", code: "preview_expired" });
  assert.equal(f.count(), baseline);
});

test("client cancellation of a slow request does not rediscover and later requests succeed", async t => {
  let started;
  const entered = new Promise(resolve => { started = resolve; });
  let disconnected;
  const closed = new Promise(resolve => { disconnected = resolve; });
  const f = await fixture(t, (req, res) => {
    if (req.url === "/api/slow") {
      res.once("close", disconnected);
      started();
      return;
    }
    res.end("healthy");
  });
  const baseline = f.count();
  const controller = new AbortController();
  const pending = f.request("/api/slow", { signal: controller.signal });
  // Attach rejection handling before abort to prevent an unhandled promise race.
  const aborted = assert.rejects(pending, error => error.name === "AbortError");
  await entered;
  controller.abort();
  await aborted;
  await closed;
  assert.equal(await (await f.request("/api/healthy")).text(), "healthy");
  assert.equal(f.count(), baseline);
});

test("a delayed old endpoint error does not invalidate a newly discovered connection", async t => {
  let entered;
  const started = new Promise(resolve => { entered = resolve; });
  let disconnect;
  const f = await fixture(t, req => {
    disconnect = () => req.socket.destroy();
    entered();
  });
  const baseline = f.count();
  const oldRequest = f.request("/api/delayed-old-request");
  await started;
  const replacement = await f.startBackend((_req, res) => res.end("replacement"));
  await f.change({ port: replacement.port, capability: "replacement-private-capability", stateText: "replacement-state" });
  assert.equal(await (await f.request("/api/current")).text(), "replacement");
  assert.equal(f.count(), baseline + 1);
  disconnect();
  await unavailable(await oldRequest, f.metadata().capability);
  assert.equal(await (await f.request("/api/still-current")).text(), "replacement");
  assert.equal(f.count(), baseline + 1, "an error belonging to the old endpoint must not invalidate the replacement");
});
