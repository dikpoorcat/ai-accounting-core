import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp, createRenderer, h, isReactive, nextTick, reactive, shallowReactive, ssrContextKey } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createMemoryHistory, createRouter } from "vue-router";

const components = ["employee_social", "employee_housing", "employer_social", "employer_housing"];
const item = (id, changes = {}) => ({ id, category_key: "payroll_payables", party: "同名员工", description: "社保公积金", status: "open", current_status: "open", source_amount_fen: "10000", paid_fen: "0", other_settled_fen: "0", outstanding_fen: "10000", current_outstanding_fen: "10000", subject_id: `subject-${id}`, contribution_group_key: "employee-a", contribution_component: "employee_social", payroll_period: "2026-09", ...changes });
const allComponents = () => components.map((component, index) => item(`part-${index}`, { contribution_component: component }));
const summary = count => ({ complete: true, unestablished_count: 0, issues: [], cutoff_period: "2026-09", current_cutoff_period: "2026-10", receivable_count: 0, receivable_fen: "0", payable_count: count, payable_fen: "40000", total_count: count, categories: [{ key: "payroll_payables", label: "待付工资、社保与个税", direction: "payable", unit: "笔", count, loaded_count: count, outstanding_fen: "40000" }] });

test("social and housing obligations aggregate only explicit identity and payroll period", async t => {
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true }, plugins: [{ name: "seed-local-contribution-expansion", enforce: "pre", transform(code, id) { if (id.replaceAll("\\", "/").endsWith("/src/components/brief/BriefOpenItems.vue")) return code.replace('const expandedItemId = ref("");', 'const expandedItemId = ref(globalThis.socialGroupExpansion || "");'); } }, vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  try {
    const { default: component } = await server.ssrLoadModule("/src/components/brief/BriefOpenItems.vue");
    const { groupBriefOpenItems, createBriefOpenItemGrouping, contributionProgressRows } = await server.ssrLoadModule("/src/utils/briefOpenItems.ts");
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/", component: {} }] }); await router.push("/?company_id=a&period=2026-09");
    const propsFor = (items, changes = {}) => { const grouped = groupBriefOpenItems(summary(items.length), items, changes.itemsComplete !== false); return { items: grouped.items, openItems: grouped.summary, itemsComplete: true, period: "2026-09", periodLabel: "2026年9月", periodStatus: "closed", snapshotVersion: "fixture", ...changes }; };
    async function render(items, changes = {}, expanded = false) { const props = propsFor(items, changes); globalThis.socialGroupExpansion = expanded ? props.items[0].id : ""; try { const app = createSSRApp(component, props); app.use(router); return await renderToString(app); } finally { delete globalThis.socialGroupExpansion; } }
    const rowCount = html => (html.match(/class="open-event-row[^"]*"/g) || []).length;
    await t.test("four components collapse across business and opening sources while salary and tax remain separate", async () => {
      const items = [...allComponents(), item("opening", { subject_id: "opening-source", contribution_component: "employee_social", outstanding_fen: "5000", source_amount_fen: "5000" }), item("salary", { contribution_group_key: null, contribution_component: null, payroll_period: null, description: "实发工资" }), item("tax", { contribution_group_key: null, contribution_component: null, payroll_period: null, description: "个人所得税" })];
      const html = await render(items);
      assert.equal(rowCount(html), 3); assert.match(html, /¥450\.00/); assert.match(html, /实发工资/); assert.match(html, /个人所得税/);
      assert.doesNotMatch(html, /employee-a|opening-source|part-0/);
    });
    await t.test("same display name never merges independent group identities or payroll months", async () => {
      const html = await render([item("one"), item("two", { contribution_group_key: "employee-b" }), item("three", { payroll_period: "2026-08" }), item("four", { contribution_component: null }), item("five", { payroll_period: null }), item("six", { contribution_group_key: null })]);
      assert.equal(rowCount(html), 6);
    });
    await t.test("group object and matter stay on separate lines, and only cross-month expanded details name the payroll month", async () => {
      const html = await render(allComponents());
      const copy = html.match(/<span class="open-event-copy"[^>]*>[\s\S]*?<\/strong>[\s\S]*?<\/span>/)[0];
      assert.match(copy, /<strong[^>]*>同名员工/); assert.match(copy, /<small[^>]*>社保与公积金<\/small>/);
      assert.doesNotMatch(copy, /2026|年|月|工资所属/);
      const sameMonth = await render(allComponents(), {}, true); assert.doesNotMatch(sameMonth, /工资所属月/);
      const crossMonth = await render([item("previous-month", { payroll_period: "2026-08" })], {}, true);
      assert.match(crossMonth, /工资所属月：2026年8月/);
      const previousCopy = crossMonth.match(/<span class="open-event-copy"[^>]*>[\s\S]*?<\/strong>[\s\S]*?<\/span>/)[0];
      assert.match(previousCopy, /<small[^>]*>社保与公积金<\/small>/); assert.doesNotMatch(previousCopy, /2026|年|月|工资所属/);
    });
    await t.test("incomplete pages cannot publish partial grouped amounts even after a read failure", async () => {
      for (const itemsError of [null, "暂时无法继续读取"]) {
        const html = await render([item("first")], { itemsComplete: false, itemsError });
        const money = html.match(/<span class="open-event-money"[^>]*>[\s\S]*?<\/span>/)[0];
        assert.doesNotMatch(money, /¥/); assert.match(html, /正在汇总|尚未读全/);
      }
    });
    await t.test("big integer totals preserve cents and any unknown member stays unknown", async () => {
      const huge = [item("huge", { outstanding_fen: "9007199254740993" }), item("cent", { contribution_component: "employee_housing", outstanding_fen: "1" })];
      assert.match(await render(huge), /¥90,071,992,547,409\.94/);
      const html = await render([item("known"), item("unknown", { contribution_component: "employee_housing", outstanding_fen: null })]);
      assert.match(html, /待核对/); const money = html.match(/<span class="open-event-money"[^>]*>[\s\S]*?<\/span>/)[0]; assert.doesNotMatch(money, /¥/);
    });
    await t.test("offsetting excess and unpaid amounts must not advertise settlement", async () => {
      const grouped = groupBriefOpenItems(summary(2), [item("excess", { status: "over_settled", current_status: "over_settled", outstanding_fen: "-10000", current_outstanding_fen: "-10000" }), item("unpaid", { contribution_component: "employee_housing" })], true);
      assert.equal(grouped.items[0].outstanding_fen, "0"); assert.notEqual(grouped.items[0].status, "settled"); assert.notEqual(grouped.items[0].current_status, "settled");
    });
    await t.test("raw pagination counts remain immutable while completed display counts describe groups", () => {
      const items = allComponents(), original = summary(items.length), preserved = structuredClone(original);
      const pending = groupBriefOpenItems(original, items.slice(0, 1), false);
      assert.equal(pending.summary.total_count, 4); assert.equal(pending.summary.categories[0].count, 4);
      const complete = groupBriefOpenItems(original, items, true);
      assert.equal(complete.summary.total_count, 1); assert.equal(complete.summary.payable_count, 1); assert.equal(complete.summary.categories[0].count, 1);
      assert.equal(complete.summary.payable_fen, original.payable_fen); assert.deepEqual(original, preserved); assert.equal(items.length, 4);
    });
    await t.test("incremental pages retain display and member identities, finish once, and reset on a new raw scope", () => {
      let moneyReads = 0;
      const first = item("first", { outstanding_fen: "9007199254740993" });
      Object.defineProperty(first, "outstanding_fen", { enumerable: true, get() { moneyReads++; return "9007199254740993"; } });
      const records = [first, item("next", { contribution_component: "employer_social", outstanding_fen: "7" }), item("ordinary", { contribution_group_key: null, contribution_component: null, payroll_period: null })];
      const pristine = records.map(record => ({ ...record })); records.forEach(Object.freeze);
      const aggregate = createBriefOpenItemGrouping(() => shallowReactive([])), raw = [records[0]], original = summary(3);
      const initial = aggregate(original, raw, false), rows = initial.items, group = rows[0], members = group.contributionMembers;
      assert(isReactive(rows));
      for (const field of ["source_amount_fen", "paid_fen", "other_settled_fen", "outstanding_fen", "current_outstanding_fen"]) assert.equal(group[field], null);
      const beforeAppend = moneyReads;
      raw.push(records[1], records[2]); const appended = aggregate(original, raw, false);
      assert.strictEqual(appended.items, rows); assert.strictEqual(appended.items[0], group); assert.strictEqual(group.contributionMembers, members);
      assert.deepEqual(members.map(record => record.id), ["first", "next"]); assert.strictEqual(members[0], records[0]); assert.strictEqual(members[1], records[1]); assert.strictEqual(rows[1], records[2]);
      assert.equal(moneyReads, beforeAppend, "continuation read prior-page money before completion"); assert.equal(group.outstanding_fen, null);
      const complete = aggregate(original, raw, true);
      assert.strictEqual(complete.items, rows); assert.strictEqual(complete.items[0], group); assert.strictEqual(group.contributionMembers, members);
      assert.equal(group.outstanding_fen, "9007199254741000"); assert.equal(complete.summary.total_count, 2);
      const afterFinish = moneyReads;
      for (let unrelatedRefresh = 0; unrelatedRefresh < 3; unrelatedRefresh++) assert.strictEqual(aggregate(original, raw, true), complete);
      assert.equal(moneyReads, afterFinish, "unrelated collection updates rescanned completed obligations");
      const reset = aggregate(original, [...raw], true);
      assert.notStrictEqual(reset.items, rows); assert.notStrictEqual(reset.items[0], group); assert.notStrictEqual(reset.items[0].contributionMembers, members);
      assert.equal(reset.items[0].outstanding_fen, group.outstanding_fen); assert(isReactive(reset.items));
      assert.deepEqual(records.map(record => ({ ...record })), pristine);
    });
    await t.test("mixed corrected or withdrawn contributions retain pending status and correction notices", () => {
      for (const terminal of ["reversed", "withdrawn"]) for (const pending of ["open", "partial"]) {
        const inputs = [item("corrected", { status: terminal, current_status: terminal, outstanding_fen: "0", current_outstanding_fen: "0" }), item("pending", { status: pending, current_status: pending })];
        const group = groupBriefOpenItems(summary(2), inputs, true).items[0], progress = contributionProgressRows(group).find(row => row.present), notice = terminal === "reversed" ? "含已更正款项" : "含已撤回款项";
        assert.equal(group.status, pending); assert.equal(group.current_status, pending); assert.deepEqual(group.contributionNotices, [notice]);
        assert.equal(progress.status, pending); assert.equal(progress.currentStatus, pending); assert.deepEqual(progress.notices, [notice]); assert.deepEqual(progress.currentNotices, [notice]);
      }
    });
    await t.test("unestablished or unknown states cannot imply open or settled contributions", () => {
      for (const state of ["unestablished", "checking", null, "unknown"]) {
        const group = groupBriefOpenItems(summary(2), [item("unresolved", { status: state, current_status: state, outstanding_fen: "0", current_outstanding_fen: "0" }), item("settled", { status: "settled", current_status: "settled", outstanding_fen: "0", current_outstanding_fen: "0" })], true).items[0];
        assert.equal(group.status, "checking"); assert.equal(group.current_status, "checking");
        const progress = contributionProgressRows(group).find(row => row.present); assert.equal(progress.status, "checking"); assert.equal(progress.currentStatus, "checking");
      }
    });
    await t.test("four fixed progress components leave absent original and paid money unknown", () => {
      const grouped = groupBriefOpenItems(summary(1), [item("only")], true);
      const rows = contributionProgressRows(grouped.items[0]);
      assert.equal(rows.length, 4); assert.deepEqual(new Set(rows.map(row => row.component)), new Set(components));
      const present = rows.find(row => row.component === "employee_social"); assert.equal(present.present, true); assert.equal(present.sourceAmountFen, "10000");
      for (const row of rows.filter(row => !row.present)) {
        assert.equal(row.sourceAmountFen, null); assert.equal(row.paidFen, null); assert.equal(row.otherSettledFen, null); assert.equal(row.outstandingFen, null); assert.equal(row.currentOutstandingFen, null); assert.equal(row.changed, false);
      }
    });
    await t.test("unknown fields propagate independently and progress uses BigInt", () => {
      const grouped = groupBriefOpenItems(summary(2), [item("huge", { source_amount_fen: "9007199254740993", paid_fen: "9007199254740993", other_settled_fen: "9007199254740993", outstanding_fen: "9007199254740993", current_outstanding_fen: "9007199254740993" }), item("extra", { source_amount_fen: "1", paid_fen: "1", other_settled_fen: null, outstanding_fen: "1", current_outstanding_fen: null })], true);
      const row = contributionProgressRows(grouped.items[0]).find(row => row.present);
      assert.equal(row.sourceAmountFen, "9007199254740994"); assert.equal(row.paidFen, "9007199254740994"); assert.equal(row.otherSettledFen, null); assert.equal(row.outstandingFen, "9007199254740994"); assert.equal(row.currentOutstandingFen, null); assert.equal(row.currentStatus, "checking");
    });
    await t.test("latest progress detects each obligation's change even when component or group totals match", () => {
      const grouped = groupBriefOpenItems(summary(3), [item("one", { current_outstanding_fen: "5000", current_status: "partial" }), item("two", { current_outstanding_fen: "15000", current_status: "over_settled" }), item("unchanged", { contribution_component: "employer_social" })], true);
      const rows = contributionProgressRows(grouped.items[0]), changed = rows.filter(row => row.changed);
      assert.equal(changed.length, 1); assert.equal(changed[0].component, "employee_social"); assert.equal(changed[0].outstandingFen, changed[0].currentOutstandingFen); assert.equal(changed[0].currentStatus, "over_settled");
      assert.equal(rows.find(row => row.component === "employer_social").changed, false);
      const noCurrent = groupBriefOpenItems(summary(1), [item("missing-current", { current_status: null, current_outstanding_fen: null })], true);
      assert.equal(contributionProgressRows(noCurrent.items[0]).find(row => row.present).changed, false);
    });
    await t.test("local group table keeps fixed missing components honest and shows only changed latest progress", async () => {
      const html = await render([item("historical", { paid_fen: "2000", other_settled_fen: "1000", outstanding_fen: "7000", current_outstanding_fen: "0", current_status: "settled" })], {}, true);
      assert.match(html, /contribution-detail/); assert.equal((html.match(/<tbody[^>]*>[\s\S]*?<\/tbody>/)[0].match(/<tr\b/g) || []).length, 4);
      for (const label of ["个人社保", "公司社保", "个人公积金", "公司公积金", "原应付", "实际已付", "抵销／代付", "月末待付", "该月末未列待付款项"]) assert(html.includes(label), label);
      assert.match(html, /后续进展/); assert.match(html, /¥70\.00/); assert.match(html, /¥20\.00/); assert.match(html, /¥10\.00/);
      assert.doesNotMatch(html, /business-status-details|subject-historical|historical|employee-a/);
      const unchanged = await render(allComponents(), {}, true); assert.doesNotMatch(unchanged, /后续进展/);
      const unknown = await render([item("unknown", { source_amount_fen: null, paid_fen: null, other_settled_fen: null, outstanding_fen: null, current_outstanding_fen: null, status: "checking", current_status: "checking" })], {}, true);
      assert.match(unknown, /待核对/); assert.doesNotMatch(unknown.match(/<tbody[^>]*>[\s\S]*?<\/tbody>/)[0], /¥0\.00/);
    });
    await t.test("group rows use one expansion and reset across category, month, company and snapshot", async () => {
      const props = reactive(propsFor([item("first"), item("second", { contribution_group_key: "employee-b" })]));
      let state;
      const host = createRenderer({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} });
      const wrapped = { ...component, ssrRender: undefined, setup(_actual, context) { state = component.setup(props, context); return state; }, render: () => h("div") };
      const app = host.createApp(wrapped, props); app.use(router); app.provide(ssrContextKey, { modules: new Set() }); app.mount({});
      try {
        const target = {}, event = key => ({ key, target, currentTarget: target, preventDefault() { this.prevented = true; } });
        state.itemKeydown(props.items[0], event("Enter")); assert.equal(state.expandedItemId.value, props.items[0].id);
        state.itemKeydown(props.items[1], event(" ")); assert.equal(state.expandedItemId.value, props.items[1].id);
        state.itemKeydown(props.items[0], { ...event("Enter"), target: {} }); assert.equal(state.expandedItemId.value, props.items[1].id);
        for (const change of [() => { props.snapshotVersion = "v2"; }, () => { props.period = "2026-10"; }, () => { state.selectedCategoryKey.value = "different"; }, () => router.push("/?company_id=b")]) {
          state.toggleItem(props.items[0], event("Enter")); assert.equal(state.expandedItemId.value, props.items[0].id); await change(); await nextTick(); assert.equal(state.expandedItemId.value, "");
        }
      } finally { app.unmount(); }
    });
  } finally { await server.close(); }
});
