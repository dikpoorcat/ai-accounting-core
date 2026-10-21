import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const { createStaticResourceObserver } = createRequire(import.meta.url)("./browser-stage9-hot-refresh.cjs");
const origin = "http://127.0.0.1:12345";
const request = (type = "stylesheet", error = "net::ERR_ABORTED", host = origin) => ({
  url: () => `${host}/assets/component.${type === "script" ? "js" : "css"}`,
  resourceType: () => type,
  failure: () => ({ errorText: error }),
});
const response = (req, status = 200, contentType = "text/css") => ({
  request: () => req, url: req.url, status: () => status,
  headers: () => ({ "content-type": contentType }),
});

test("successful same-origin scripts and styles finish without resource failures", () => {
  const observer = createStaticResourceObserver(origin);
  for (const type of ["stylesheet", "script"]) {
    const req = request(type);
    observer.start(req);
    observer.response(response(req, 200, type === "script" ? "text/javascript" : "text/css; charset=utf-8"));
    observer.finished(req);
  }
  observer.assertComplete();
  assert.deepEqual(observer.failures, []);
});

test("HTTP errors, wrong stylesheet MIME and real network failures reject qualification", () => {
  for (const failure of ["http", "mime", "network"]) {
    const observer = createStaticResourceObserver(origin);
    const req = request(failure === "network" ? "script" : "stylesheet", "net::ERR_CONNECTION_REFUSED");
    observer.start(req);
    if (failure === "network") observer.failed(req);
    else observer.response(response(req, failure === "http" ? 404 : 200, failure === "mime" ? "text/html" : "text/css"));
    assert.throws(() => observer.assertComplete(), /same-origin script or stylesheet failed/);
    assert.equal(observer.failures.length, 1);
    assert.equal(observer.failures[0].required, true);
  }
});

test("only explicitly superseded pending requests may retain a navigation cancellation", () => {
  const observer = createStaticResourceObserver(origin), old = request();
  observer.start(old);
  observer.navigating();
  observer.failed(old);
  observer.assertComplete();
  assert.equal(observer.failures[0].navigation_cancelled, true);
  const current = request("script");
  observer.start(current);
  observer.failed(current);
  assert.equal(observer.failures[1].navigation_cancelled, false);
  assert.throws(() => observer.assertComplete());
});

test("navigation cannot excuse connection failures, HTTP failures or completed requests", () => {
  for (const failure of ["network", "http", "already_finished"]) {
    const observer = createStaticResourceObserver(origin);
    const req = request("script", failure === "network" ? "net::ERR_CONNECTION_REFUSED" : "net::ERR_ABORTED");
    observer.start(req);
    if (failure === "already_finished") observer.finished(req);
    observer.navigating();
    if (failure === "http") observer.response(response(req, 500, "text/javascript"));
    else observer.failed(req);
    assert.throws(() => observer.assertComplete());
  }
});

test("non-required resource errors remain in the report without changing this qualification", () => {
  const observer = createStaticResourceObserver(origin);
  for (const req of [request("image"), request("script", "net::ERR_FAILED", "https://other.invalid")]) {
    observer.start(req);
    observer.failed(req);
  }
  observer.assertComplete();
  assert.equal(observer.failures.length, 2);
  assert(observer.failures.every(item => item.required === false));
});
