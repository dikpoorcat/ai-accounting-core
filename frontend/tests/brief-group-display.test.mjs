import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createMemoryHistory, createRouter } from "vue-router";

const contributionMember = (id, changes = {}) => ({ id, group_key: "employee", category_key: "payroll_payables", party: "甲员工", description: "个人社保", purpose: "社保缴费", date: null, recognition: { period: "2026-07", label: "2026-07 · 按月确认" }, subject_id: id, status: "open", current_status: "settled", source_amount_fen: "52500", paid_fen: "0", other_settled_fen: "0", outstanding_fen: "52500", current_outstanding_fen: "0", contribution_group_key: "employee-a", contribution_component: "employee_social", payroll_period: "2026-07", voucher_version_id: "exact-88", ...changes });
async function renderMembers(server, members, { hasMore = false, expandedPart = "" } = {}) {
  const { default: component } = await server.ssrLoadModule("/src/components/brief/BriefGroupMembers.vue");
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/", component: {} }] }); await router.push("/?company_id=a");
  const data = { section: "open_items", group_key: "employee", collections: { members: { items: members, page: { has_more: hasMore } }, vouchers: { items: [] } } };
  const wrapped = { ...component, setup(props, context) { const state = component.setup(props, context); state.data.value = data; state.expandedPart.value = expandedPart; return state; } };
  const voucher = (id, number) => ({ voucher_version_id: id, number, date: null, recognition: { label: "2026-07 · 按月确认" }, list_summary: "社保计提", state: "已入账", business_amount_label: "社保金额", business_amount_fen: "52500", lines: [] });
  const app = createSSRApp(wrapped, { section: "open_items", groupKey: "employee", expanded: true, period: "2026-07", openSummary: { cutoff_period: "2026-07", current_cutoff_period: "2026-09", categories: [] }, direction: "payable", periodClosed: true, voucherIndex: new Map([["exact-88", voucher("exact-88", "88")], ["exact-89", voucher("exact-89", "89")]]) }); app.use(router);
  return renderToString(app);
}

test("monthly contribution table owns single records and compresses missing and settled components", async () => {
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true }, plugins: [vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  try {
    const html = await renderMembers(server, [contributionMember("personal"), contributionMember("company", { description: "单位社保", contribution_component: "employer_social", purpose: "公司社保缴费", source_amount_fen: "132000", outstanding_fen: "132000" })]);
    assert.equal((html.match(/class="contribution-row"/g) ?? []).length, 2);
    assert.doesNotMatch(html, /class="member-row"/);
    assert.equal((html.match(/¥525\.00/g) ?? []).length, 2, "original and period-end amount stay distinct; no third duplicate member amount");
    assert.equal((html.match(/社保缴费/g) ?? []).length, 2);
    assert.equal((html.match(/凭证 88/g) ?? []).length, 2, "both component rows retain their exact voucher entrance");
    assert.equal((html.match(/>业务进展<\/button>/g) ?? []).length, 2);
    assert.doesNotMatch(html, /公积金|该月末未列待付款项/);
    assert.match(html, /截至2026年9月末.*上述待付款项均已结清/);
    assert.doesNotMatch(html, /class="contribution-part"/);
    assert.doesNotMatch(html, /尚未结算|单位社保/);
  } finally { await server.close(); }
});

test("multi-record components keep exact dates, purposes and vouchers while unmatched records stay visible", async () => {
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true }, plugins: [vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  try {
    const records = [contributionMember("first", { date: "2026-07-15", purpose: "第一笔补缴" }), contributionMember("second", { date: "2026-07-20", purpose: "第二笔补缴", voucher_version_id: "exact-89" }), contributionMember("no-identity", { contribution_group_key: null, purpose: "缺员工身份" }), contributionMember("no-period", { payroll_period: null, purpose: "缺工资月" }), contributionMember("no-component", { contribution_component: null, purpose: "缺分项" })];
    const html = await renderMembers(server, records, { expandedPart: 'contribution:["employee-a","2026-07","social"]:employee_social' });
    assert.match(html, /aria-expanded="true"[^>]*>共 2 笔/);
    assert.match(html, /¥1,050\.00/);
    for (const text of ["2026-07-15", "2026-07-20", "第一笔补缴", "第二笔补缴", "凭证 88", "凭证 89", "缺员工身份", "缺工资月", "缺分项"]) assert(html.includes(text), text);
    assert.equal((html.match(/class="member-row"/g) ?? []).length, 5, "only multi-record children and unmatched ordinary records retain member rows");
    const partial = await renderMembers(server, records, { hasMore: true });
    assert.doesNotMatch(partial, /class="contribution-row"/);
    assert.equal((partial.match(/class="member-row"/g) ?? []).length, 5, "incomplete grouping retains all already loaded records");
  } finally { await server.close(); }
});

test("later mixed or unknown contribution states never collapse into a settled summary", async () => {
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true }, plugins: [vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  try {
    for (const currentStatus of ["partial", "checking", "over_settled", "reversed", "withdrawn", null]) {
      const html = await renderMembers(server, [contributionMember("settled"), contributionMember("other", { contribution_component: "employer_social", current_status: currentStatus, current_outstanding_fen: currentStatus === "over_settled" ? "-100" : currentStatus === "partial" ? "100" : currentStatus == null ? null : "0" })]);
      assert.doesNotMatch(html, /上述待付款项均已结清/);
      assert.match(html, /class="contribution-part"/);
      if (currentStatus === "over_settled") assert.match(html, /−¥1\.00/);
    }
  } finally { await server.close(); }
});

test("group member lists preserve purposes, exact vouchers and monthly contribution breakdowns", async () => {
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true }, plugins: [vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  try {
    const { default: component } = await server.ssrLoadModule("/src/components/brief/BriefGroupMembers.vue");
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/", component: {} }] }); await router.push("/?company_id=a");
    const summary = { cutoff_period: "2026-09", current_cutoff_period: "2026-10", categories: [] };
    const member = (id, period) => ({ id, group_key: "employee", category_key: "payroll_payables", party: "甲员工", description: "个人社保", purpose: `${period}工资对应社保`, date: null, recognition: { period, label: `${period} · 按月确认` }, subject_id: id, status: "open", current_status: "partial", source_amount_fen: "10000", paid_fen: "0", other_settled_fen: "0", outstanding_fen: "10000", current_outstanding_fen: "5000", contribution_group_key: "employee-a", contribution_component: "employee_social", payroll_period: period, voucher_version_id: null });
    const members = [member("aug", "2026-08"), member("sep", "2026-09")];
    const data = { section: "open_items", group_key: "employee", collections: { members: { items: members, page: { has_more: false } }, vouchers: { items: [] } } };
    const wrapped = { ...component, setup(props, context) { const state = component.setup(props, context); state.data.value = data; return state; } };
    const app = createSSRApp(wrapped, { section: "open_items", groupKey: "employee", expanded: true, period: "2026-09", openSummary: summary, direction: "payable", periodClosed: true, voucherIndex: new Map() }); app.use(router);
    const html = await renderToString(app);
    assert.match(html, /2026-08工资对应社保/); assert.match(html, /2026-09工资对应社保/);
    assert.match(html, /2026年8月 · 社保/); assert.match(html, /2026年9月 · 社保/);
    for (const label of ["原应付", "实际已付", "抵销／代付", "月末待付", "后续进展", "个人社保", "公司社保", "该月末未列待付款项"]) assert(html.includes(label));
    assert.match(html, /¥100\.00/); assert.match(html, /¥50\.00/);
    assert.equal((html.match(/aria-expanded="true"/g) ?? []).length, 0, "listing members never opens their business progress");
    assert.doesNotMatch(html, /employee-a|group_key|subject_id/);
  } finally { await server.close(); }
});

test("shared voucher preview retains all entries and exact unknown or huge business amounts", async () => {
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true }, plugins: [vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  try {
    const { default: component } = await server.ssrLoadModule("/src/components/brief/BriefVoucherPreview.vue");
    for (const amount of [null, "0", "9007199254740993"]) {
      const voucher = { voucher_version_id: "exact", number: "21", date: null, recognition: { label: "2026-09 · 按月确认" }, list_summary: "代收押金", state: "更正原业务", business_amount_label: "代收金额", business_amount_fen: amount, lines: [{ line_number: 1, account: "银行存款", debit_fen: "100", credit_fen: "0" }, { line_number: 2, account: "其他应付款", debit_fen: "0", credit_fen: "100" }] };
      const html = await renderToString(createSSRApp(component, { voucher, active: true }));
      assert.equal((html.match(/role="tooltip"/g) ?? []).length, 1); assert.match(html, /凭证 21/); assert.match(html, /银行存款/); assert.match(html, /其他应付款/); assert.match(html, /更正原业务/);
      assert(html.includes(amount === null ? "暂无法确定" : amount === "0" ? "¥0.00" : "¥90,071,992,547,409.93"));
    }
  } finally { await server.close(); }
});


test("social and housing monthly panels only name missing components of their own matter", async () => {
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true }, plugins: [vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  try {
    for (const [component, ownLabel, missingLabel, otherLabel] of [["employee_social", "社保", "公司社保", "公积金"], ["employee_housing", "公积金", "公司公积金", "社保"]]) {
      const html = await renderMembers(server, [contributionMember("one", { contribution_component: component, description: ownLabel, purpose: "原资料用途" })]);
      assert(html.includes(`2026年7月 · ${ownLabel}`));
      assert(html.includes(`${missingLabel}：该月末未列待付款项`));
      assert.doesNotMatch(html, new RegExp(otherLabel));
      assert(html.includes("原资料用途"));
    }
  } finally { await server.close(); }
});
