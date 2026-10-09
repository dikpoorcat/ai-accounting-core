import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createMemoryHistory, createRouter } from "vue-router";

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
    assert.match(html, /2026年8月 · 社保与公积金/); assert.match(html, /2026年9月 · 社保与公积金/);
    for (const label of ["原应付", "实际已付", "抵销／代付", "月末待付", "后续进展", "个人社保", "公司社保", "个人公积金", "公司公积金", "该月末未列待付款项"]) assert(html.includes(label));
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
