import assert from "node:assert/strict";
import { createServer as createHttpServer } from "node:http";
import { execFile } from "node:child_process";
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
const { localApiProxy, readLocalServiceMetadata } = await loader.ssrLoadModule("/local-api-proxy.ts");

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
  const statePath = join(directory, ".service.json");
  const requests = [];
  const backends = [];
  const startBackend = async (custom = handler, port = 0) => {
    const backend = createHttpServer(async (req, res) => {
      let body = "";
      for await (const chunk of req) body += chunk;
      requests.push({ path: req.url, headers: req.headers, body });
      if (custom) return custom(req, res, body);
      res.setHeader("Content-Type", "application/json");
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
    return { ...current };
  };
  const plugin = await localApiProxy(statePath, discover);
  const vite = await createServer({ root, configFile: false, plugins: [plugin],
    optimizeDeps: { noDiscovery: true }, server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  vite.middlewares.use((_req, res) => { res.end("frontend-page"); });
  const frontend = createHttpServer(vite.middlewares);
  const port = await listen(frontend);
  const request = (path = "/api/dashboard/context", options = {}) => fetch(`http://127.0.0.1:${port}${path}`, options);
  t.after(async () => {
    await close(frontend);
    await vite.close();
    for (const backend of backends) if (backend.listening) await close(backend);
    await rm(directory, { recursive: true, force: true });
  });
  return { first, requests, startBackend, request, statePath,
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

test("changed state discovers the new port before sending and updates Origin and ticket capability", async t => {
  const f = await fixture(t);
  assert.equal((await f.request()).status, 200);
  const second = await f.startBackend();
  await f.change({ port: second.port, capability: "private-test-capability-two", stateText: "state-two" });
  const response = await f.request("/api/browser-ticket?development=1", {
    method: "POST", headers: { Origin: "http://127.0.0.1:5173", "X-Local-Capability": "user-supplied" }, body: "{}",
  });
  assert.equal(response.status, 200);
  const received = f.requests.at(-1);
  assert.equal(received.headers.host, `127.0.0.1:${second.port}`);
  assert.equal(received.headers.origin, `http://127.0.0.1:${second.port}`);
  assert.equal(received.headers["x-local-capability"], "private-test-capability-two");
  await f.request("/api/dashboard/context", { headers: { "X-Local-Capability": "user-supplied" } });
  assert.equal(f.requests.at(-1).headers["x-local-capability"], undefined);
});

test("changed capability on the same port is refreshed", async t => {
  const f = await fixture(t);
  await f.change({ capability: "same-port-new-capability", stateText: "same-port-restarted" });
  assert.equal((await f.request("/api/browser-ticket", { method: "POST", body: "{}" })).status, 200);
  assert.equal(f.requests.at(-1).headers["x-local-capability"], "same-port-new-capability");
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


test("real service-info verifies a synthetic catalog and rejects identity or state races", {
  skip: process.platform !== "win32", timeout: 60_000,
}, async t => {
  const directory = await mkdtemp(join(tmpdir(), "finance-service-info-test-"));
  const dataRoot = join(directory, "root");
  const statePath = join(dataRoot, ".service.json");
  const capability = "synthetic-private-cli-capability";
  let identity;
  let healthOverrides = {};
  let alterDuringHealth = false;
  let healthRequests = 0;
  const backend = createHttpServer(async (req, res) => {
    healthRequests++;
    assert.equal(req.url, "/api/health");
    assert.equal(req.headers["x-local-capability"], capability);
    if (alterDuringHealth) {
      // Both texts parse to the same valid metadata. A restart race must still fail closed.
      await writeFile(statePath, `${await readFile(statePath, "utf8")}\n`);
    }
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ status: "ready", protocol: 2, ...identity,
      execution_mode: "normal", replay_scope_digest: null, ...healthOverrides }));
  });
  const port = await listen(backend);
  t.after(async () => { await close(backend); await rm(directory, { recursive: true, force: true }); });
  const script = `
import json, os, sys
from pathlib import Path
from ai_accounting.kernel.catalog import Catalog
from ai_accounting.kernel.security.windows import write_protected_json
catalog = Catalog(Path(sys.argv[1]))
with catalog.connection(read_only=True) as connection:
    catalog_id = connection.execute("SELECT instance_id FROM catalog_identity WHERE id=1").fetchone()[0]
identity = {"catalog_id": catalog_id, "database_format": catalog.database_format(), "build_id": "synthetic-cli-build"}
metadata = {**identity, "protocol": 2, "pid": os.getpid(), "port": int(sys.argv[2]), "capability": "synthetic-private-cli-capability"}
write_protected_json(catalog.root / ".service.json", metadata)
print(json.dumps(identity))
`;
  const { stdout } = await promisify(execFile)(join(root, "..", ".tmp-kernel-venv", "Scripts", "python.exe"),
    ["-I", "-X", "utf8", "-c", script, dataRoot, String(port)],
    { cwd: join(root, ".."), encoding: "utf8", timeout: 20_000, windowsHide: true });
  assert.ok(!stdout.includes(capability));
  identity = JSON.parse(stdout);
  const baseline = await readFile(statePath, "utf8");
  const repositoryRoot = join(root, "..");
  const verified = await readLocalServiceMetadata(repositoryRoot, dataRoot);
  assert.deepEqual(verified, { port, capability, catalogId: identity.catalog_id, stateText: baseline });
  const rejectSafely = async () => assert.rejects(readLocalServiceMetadata(repositoryRoot, dataRoot), error => {
    assert.ok(!String(error).includes(capability));
    assert.ok(!String(error).includes("synthetic-cli-build"));
    return /本地会计服务不可用/.test(error.message);
  });
  const before = healthRequests;
  await writeFile(statePath, JSON.stringify({ ...JSON.parse(baseline), protocol: 1 }));
  await rejectSafely();
  assert.equal(healthRequests, before, "invalid metadata protocol must fail before contacting the port");
  await writeFile(statePath, JSON.stringify({ ...JSON.parse(baseline), build_id: "changed-state-build" }));
  await rejectSafely();
  await writeFile(statePath, baseline);
  for (const overrides of [{ protocol: 1 }, { build_id: "different-health-build" }, { catalog_id: "different-health-catalog" }]) {
    healthOverrides = overrides;
    await rejectSafely();
  }
  healthOverrides = {};
  alterDuringHealth = true;
  await rejectSafely();
  alterDuringHealth = false;
  await writeFile(statePath, baseline);
  assert.equal((await readLocalServiceMetadata(repositoryRoot, dataRoot)).catalogId, identity.catalog_id);
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
