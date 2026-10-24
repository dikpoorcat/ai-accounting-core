import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { after, test } from "node:test";
import * as Vue from "vue";
import { renderToString } from "@vue/server-renderer";
import ts from "typescript";
import vuePlugin from "@vitejs/plugin-vue";
import { createServer } from "vite";
import { validateCompanyNoteResponse } from "../src/api/generated/companyNote.js";
import { validateCompanyNoteQuery } from "../src/api/generated/companyNoteQuery.js";

const source = path => readFileSync(new URL(path, import.meta.url), "utf8");
const companyA = "00000000-0000-4000-8000-000000000101";
const companyB = "00000000-0000-4000-8000-000000000102";
const response = (company_id = companyA, text = "当前公司说明", text_format = "plain_text") => ({
  company: { company_id, database_id: "00000000-0000-4000-8000-000000000201", credit_code: "913100000000000001", name: "合成公司" },
  description: { text_format, text },
});
const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false,
  plugins: [vuePlugin()], optimizeDeps: { noDiscovery: true },
  server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
after(() => server.close());
const Body = (await server.ssrLoadModule("/src/components/CompanyNoteBody.vue")).default;
const render = (text, format = "markdown") => renderToString(Vue.createSSRApp(Body, { text, format }));

test("company note: strict independent contracts require company identity and explicit readable format", () => {
  assert(validateCompanyNoteQuery({ company_id: companyA }));
  assert(!validateCompanyNoteQuery({ company_id: companyA, period: "2026-08" }));
  assert(!validateCompanyNoteQuery({ company_id: companyA, text: "不能写入" }));
  assert(!validateCompanyNoteQuery({}));
  assert(validateCompanyNoteResponse(response()));
  assert(validateCompanyNoteResponse({ ...response(), description: null }));
  for (const description of [{ text: "缺格式" }, { text_format: "html", text: "<b>错误</b>" },
    { text_format: "markdown", text: "" }, { text_format: "markdown", text: "内容", html: "不能有副本" }]) {
    assert(!validateCompanyNoteResponse({ ...response(), description }));
  }
});

test("company note: actual API uses only company_id and rejects wrong-company or malformed responses", async () => {
  const savedWindow = globalThis.window, savedFetch = globalThis.fetch;
  const calls = [];
  let returned = response();
  globalThis.window = { location: { origin: "http://localhost", search: `?company_id=${companyB}&period=2026-08&quarter=2026-Q3` } };
  globalThis.fetch = async (url, options) => {
    calls.push({ url: new URL(url, "http://localhost"), options });
    return new Response(JSON.stringify(returned));
  };
  try {
    const { fetchCompanyNote } = await server.ssrLoadModule("/src/api/companyNote.ts");
    assert.deepEqual(await fetchCompanyNote(companyA), response());
    assert.deepEqual([...calls[0].url.searchParams], [["company_id", companyA]]);
    assert.equal(calls[0].url.pathname, "/api/dashboard/company-note");
    assert.equal(calls[0].options.cache, "no-store");
    returned = response(companyB);
    await assert.rejects(fetchCompanyNote(companyA), error => error.code === "DASHBOARD_SCHEMA_MISMATCH");
    returned = { ...response(), description: { text: "缺格式" } };
    await assert.rejects(fetchCompanyNote(companyA), error => error.code === "DASHBOARD_SCHEMA_MISMATCH");
    const count = calls.length;
    await assert.rejects(fetchCompanyNote(""), error => error.status === 400);
    assert.equal(calls.length, count);
  } finally { globalThis.window = savedWindow; globalThis.fetch = savedFetch; }
});

test("company note: article renders supported syntax through fixed Vue elements", async () => {
  const html = await render("# 背景\n\n## 日常安排\n\n**重要说明**\n\n- 甲\n- 乙\n\n3. 第三项\n4. 第四项\n\n> 引用\n\n| 事项 | 金额 |\n| :--- | ---: |\n| 工资 | 100 |\n\n[公开资料](https://example.invalid/info)\n\n<https://example.invalid/another>");
  for (const fragment of ["<h2", "<h3", "<strong", "<ul", '<ol start="3"', "<li", "<blockquote", "<table", "<thead", "<tbody", "<th", "<td", 'class="align-right"', 'href="https://example.invalid/info"', 'rel="noopener noreferrer"']) assert(html.includes(fragment), fragment);
  assert(!html.includes("<h1"));
  assert.equal((html.match(/<a /g) ?? []).length, 2);
});

test("company note: plain text preserves punctuation and line breaks without interpreting Markdown", async () => {
  const text = "# 标题\n*、_、1. 和 <公司>\n<script>alert(1)</script>";
  const html = await render(text, "plain_text");
  assert(html.includes("# 标题\n*、_、1. 和 &lt;公司&gt;\n&lt;script&gt;alert(1)&lt;/script&gt;"));
  assert(!/<h[1-6]|<script|<li|<strong/.test(html));
});

test("company note: unsafe HTML, links and images stay readable without executable nodes or arbitrary attributes", async () => {
  const html = await render('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n[坏链接](javascript:alert(1))\n\n[邮件](mailto:a@example.invalid)\n\n[相对](//example.invalid/file)\n\n![图片说明](https://example.invalid/image.png)\n\n<table onclick=alert(1) style=position:fixed><tr><td>原文</td></tr></table>\n\nhttps://example.invalid/plain');
  assert(!/<script|<img|<iframe|<video|<a\s|<[a-z][^>]*\s(?:onerror|onclick|style)=/.test(html), html);
  for (const fragment of ["&lt;script&gt;alert(1)&lt;/script&gt;", "javascript:alert(1)", "图片说明", "https://example.invalid/image.png", "原文", "https://example.invalid/plain"]) assert(html.includes(fragment), fragment);
});

test("company note: unsupported layout keeps readable text, with no new component or code execution", async () => {
  const html = await render("*斜体原文*\n\n~~删除线原文~~\n\n`内联代码`\n\n```js\nwindow.shouldNeverRun()\n```\n\n---\n\n结尾");
  for (const fragment of ["*斜体原文*", "~~删除线原文~~", "`内联代码`", "```js\nwindow.shouldNeverRun()\n```", "结尾"]) assert(html.includes(fragment), fragment);
  assert(!/<em|<s>|<code|<pre|<hr|<script/.test(html));
});

let sequence = 0;
async function page(query = { company_id: companyA }) {
  const key = `companyNotePage${++sequence}`, calls = [], unmount = [];
  const route = Vue.reactive({ query });
  const context = Vue.ref({ current_company: { company_id: companyA, name: "合成甲公司" } });
  globalThis[key] = { Vue, route, context, unmount,
    fetchCompanyNote: (...args) => new Promise((resolve, reject) => calls.push({ args, resolve, reject })) };
  const script = source("../src/views/CompanyNoteView.vue").match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1].replace(/import[\s\S]*?from "[^"]+";/g, "");
  const { outputText } = ts.transpileModule(`
    const env = globalThis.${key};
    const { computed, ref, shallowRef, watch } = env.Vue;
    const onBeforeUnmount = fn => env.unmount.push(fn);
    const useRoute = () => env.route, useDashboardContext = () => ({ context: env.context });
    const fetchCompanyNote = env.fetchCompanyNote;
    export function setup() { ${script}; return { load, response, loading, failed, companyName }; }
  `, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
  const scope = Vue.effectScope();
  const { setup } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
  const result = scope.run(setup);
  delete globalThis[key];
  return { ...result, route, calls, close: () => { unmount.forEach(fn => fn()); scope.stop(); } };
}
async function flush() { for (let index = 0; index < 4; index++) { await Promise.resolve(); await Vue.nextTick(); } }

test("company note: no company means no request, and changing period alone never rereads the body", async () => {
  const p = await page({});
  try {
    assert.equal(p.calls.length, 0);
    p.route.query.company_id = companyA;
    assert.equal(p.calls.length, 1);
    p.calls[0].resolve(response()); await flush();
    p.route.query.period = "2026-01"; p.route.query.quarter = "2026-Q1";
    assert.equal(p.calls.length, 1);
    assert.equal(p.response.value.description.text, "当前公司说明");
  } finally { p.close(); }
});

test("company note: A → B → A accepts only A2, clears prior text and cancels on leaving", async () => {
  const p = await page();
  try {
    p.route.query.company_id = companyB;
    assert(p.calls[0].args[1].aborted);
    assert.equal(p.companyName.value, "");
    assert.equal(p.response.value, null);
    p.route.query.company_id = companyA;
    assert(p.calls[1].args[1].aborted);
    p.calls[2].resolve(response(companyA, "A2")); await flush();
    p.calls[1].resolve(response(companyB, "迟到B"));
    p.calls[0].reject(new Error("迟到A1失败")); await flush();
    assert.equal(p.response.value.description.text, "A2");
    assert.equal(p.failed.value, false);
    assert.equal(p.loading.value, false);
    const pending = p.load();
    p.close();
    assert(p.calls[3].args[1].aborted);
    p.calls[3].resolve(response(companyA, "离开后返回")); await pending;
    assert.equal(p.response.value, null);
  } finally { p.close(); }
});

test("company note: refresh failure retains prior content and is distinct from a genuine empty result", async () => {
  const p = await page();
  try {
    p.calls[0].resolve(response()); await flush();
    const refresh = p.load();
    assert.equal(p.response.value.description.text, "当前公司说明");
    p.calls[1].reject(new Error("无法读取")); await refresh;
    assert.equal(p.failed.value, true);
    assert.equal(p.loading.value, false);
    assert.equal(p.response.value.description.text, "当前公司说明");
    const retry = p.load();
    p.calls[2].resolve({ ...response(), description: null }); await retry;
    assert.equal(p.failed.value, false);
    assert.equal(p.response.value.description, null);
    p.route.query.company_id = companyB;
    assert.equal(p.response.value, null);
    assert.equal(p.failed.value, false);
    p.calls[3].reject(new Error("首次读取失败")); await flush();
    assert.equal(p.failed.value, true);
    assert.equal(p.response.value, null);
  } finally { p.close(); }
});

test("company note: tool entry is outside the original five business navigation items", () => {
  const app = source("../src/App.vue");
  const navigation = app.match(/const navItems = \[([\s\S]*?)\] as const/)[1];
  assert.equal((navigation.match(/name:/g) ?? []).length, 5);
  const footer = app.slice(app.indexOf('<div class="sidebar-footer">'));
  assert(footer.indexOf('aria-label="公司说明"') < footer.indexOf('aria-label="负责人身份"'));
  assert(footer.includes('class="theme-button company-note-link"'));
  assert(app.includes('authenticated && requiresPeriod && !sidebarCollapsed'));
  assert(!app.includes("fetchCompanyNote"));
});
