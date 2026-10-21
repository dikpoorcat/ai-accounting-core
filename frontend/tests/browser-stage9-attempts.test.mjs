import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { once } from "node:events";

const { recordRefreshAttempt } = createRequire(import.meta.url)("./browser-stage9-hot-refresh.cjs");

test("refresh attempts preserve a timed timeout after earlier successful samples", async () => {
  const attempts = [];
  await recordRefreshAttempt(attempts, { page: "brief", kind: "sample", iteration: 1 },
    async () => 520, async () => null);
  const error = new Error("Timeout 30000ms exceeded at http://localhost/?ticket=private");
  error.name = "TimeoutError";
  await assert.rejects(recordRefreshAttempt(attempts, { page: "brief", kind: "sample", iteration: 2 },
    async attempt => { attempt.phase = "render"; throw error; },
    async () => ({ started: true, elapsed_ms: 30001 })), actual => actual === error);
  assert.equal(attempts[0].status, "complete");
  assert.equal(attempts[0].elapsed_ms, 520);
  assert.equal(attempts[1].status, "failed");
  assert.equal(attempts[1].phase, "render");
  assert.equal(attempts[1].elapsed_ms, 30001);
  assert.equal(attempts[1].error.type, "TimeoutError");
  assert(!JSON.stringify(attempts).includes("private"));
});

test("preparation failures and post-render validation failures remain distinct", async () => {
  const attempts = [], error = new Error("invalid payload");
  await assert.rejects(recordRefreshAttempt(attempts, { kind: "warmup" },
    async () => { throw error; }, async () => ({ started: false })), error);
  await assert.rejects(recordRefreshAttempt(attempts, { kind: "sample" },
    async attempt => { attempt.phase = "validate"; attempt.elapsed_ms = 450; throw error; },
    async () => ({ started: true, elapsed_ms: 450 })), error);
  assert.equal(attempts[0].status, "preparation_failed");
  assert.equal(attempts[0].elapsed_ms, null);
  assert.equal(attempts[1].status, "failed");
  assert.equal(attempts[1].elapsed_ms, 450);
});

test("durable checkpoints bracket an attempt outside its measured endpoint and retain a slow failed attempt", async () => {
  const attempts = [], checkpoints = [];
  const persist = attempt => checkpoints.push(structuredClone(attempt));
  await recordRefreshAttempt(attempts, { page: "brief", kind: "sample", iteration: 1 },
    async () => { assert.equal(checkpoints.at(-1).status, "preparing"); return 520; },
    async () => null, persist);
  assert.equal(checkpoints.at(-1).status, "complete");
  assert.equal(checkpoints.at(-1).elapsed_ms, 520);
  await assert.rejects(recordRefreshAttempt(attempts, { page: "brief", kind: "sample", iteration: 2 },
    async () => { throw new Error("actual timeout"); }, async () => ({ started: true, elapsed_ms: 30001 }), persist));
  assert.equal(checkpoints.length, 4);
  assert.equal(checkpoints.at(-1).status, "failed");
  assert.equal(checkpoints.at(-1).elapsed_ms, 30001);
});

test("a real killed process leaves its completed sample and the next unfinished attempt on disk", { timeout: 5000 }, async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "stage9-progress-"));
  const filename = path.join(directory, "attempts.jsonl");
  const harness = createRequire(import.meta.url).resolve("./browser-stage9-hot-refresh.cjs");
  const script = `
    const fs = require('node:fs');
    const {recordRefreshAttempt} = require(${JSON.stringify(harness)});
    const attempts = [], save = attempt => fs.appendFileSync(${JSON.stringify(filename)}, JSON.stringify(attempt)+'\\n');
    (async () => {
      await recordRefreshAttempt(attempts, {page:'brief',kind:'sample',iteration:1}, async()=>520, async()=>null, save);
      await recordRefreshAttempt(attempts, {page:'brief',kind:'sample',iteration:2},
        ()=>new Promise(()=>{process.stdout.write('second-started');setInterval(()=>{},1000)}), async()=>null, save);
    })();`;
  const child = spawn(process.execPath, ["-e", script], { stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
  const closed = once(child, "close");
  try {
    const [output] = await once(child.stdout, "data"); assert.equal(String(output), "second-started");
    child.kill("SIGKILL"); await closed;
    const records = fs.readFileSync(filename, "utf8").trim().split("\n").map(line => JSON.parse(line));
    assert.equal(records[1].status, "complete"); assert.equal(records[1].elapsed_ms, 520);
    assert.equal(records[2].iteration, 2); assert.equal(records[2].status, "preparing");
  } finally {
    if (child.exitCode === null && child.signalCode === null) { child.kill("SIGKILL"); await closed; }
    assert.equal(path.dirname(path.resolve(directory)), path.resolve(os.tmpdir()));
    fs.rmSync(directory, { recursive: true });
  }
});
