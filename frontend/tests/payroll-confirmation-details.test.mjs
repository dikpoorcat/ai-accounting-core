import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createMemoryHistory, createRouter } from "vue-router";

const fixtures = JSON.parse(
  readFileSync(new URL("./t4-ui-responses.json", import.meta.url), "utf8"),
);

test("business detail renders current and frozen payroll confirmation evidence", async () => {
  const data = structuredClone(fixtures["business-status"].data);
  const current = {
    mode: "monthly_plan",
    confirmation_fact_id: "fact-plan-exact",
    confirmation_subject_id: "employee-a-plan-2026-01",
    confirmation_revision: 2,
    confirmation_kind: "payroll_plan_v2",
    evidence: ["plan-evidence-digest"],
  };
  const frozen = {
    mode: "explicit_no_change",
    confirmation_fact_id: "fact-no-change-exact",
    confirmation_subject_id: "payroll-no-change-2026-01",
    confirmation_revision: 1,
    confirmation_kind: "payroll_no_change_v2",
    evidence: ["no-change-evidence-digest"],
  };
  data.current_business_result = {
    status: "published",
    payroll_confirmation: current,
  };
  data.frozen_adoption = {
    close_period: "2026-01",
    publication_id: "publication-frozen",
    calculation_id: "calculation-frozen",
    result_digest: "result-frozen",
    role: "direct",
    selection_proof: { basis: "direct_adoption" },
    payroll_confirmation: frozen,
  };
  globalThis.payrollConfirmationBusinessDetail = data;
  const server = await createServer({
    root: fileURLToPath(new URL("..", import.meta.url)),
    configFile: false,
    optimizeDeps: { noDiscovery: true },
    plugins: [{
      name: "seed-payroll-confirmation-business-detail",
      enforce: "pre",
      transform(code, id) {
        if (id.replaceAll("\\", "/").endsWith("/src/components/BusinessStatusDetails.vue")) {
          return code.replace(
            "ref<BusinessStatusData | null>(null)",
            "ref(globalThis.payrollConfirmationBusinessDetail)",
          );
        }
      },
    }, vue()],
    server: { middlewareMode: true, hmr: false, ws: false },
    appType: "custom",
  });
  try {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: "/", component: {} }],
    });
    await router.push("/?company_id=co&period=2026-01");
    const { default: component } = await server.ssrLoadModule(
      "/src/components/BusinessStatusDetails.vue",
    );
    const app = createSSRApp(component, {
      subjectId: data.identity.subject_id,
      period: "2026-01",
      snapshotVersion: "fixture",
    });
    app.use(router);
    const html = await renderToString(app);

    assert.equal((html.match(/查看工资确认依据/g) ?? []).length, 2);
    assert.match(html, /负责人确认本月工资方案/);
    assert.match(html, /负责人确认全员无变化/);
    assert.match(html, /fact-plan-exact/);
    assert.match(html, /plan-evidence-digest/);
    assert.match(html, /fact-no-change-exact/);
    assert.match(html, /no-change-evidence-digest/);
  } finally {
    await server.close();
    delete globalThis.payrollConfirmationBusinessDetail;
  }
});

