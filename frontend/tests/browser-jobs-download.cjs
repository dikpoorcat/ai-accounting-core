// Historical v1 response fixture: current packaged-browser seam is browser-t5-integration.cjs.
async page => {
  // Isolated transport fixtures: no real company service or file is accessed.
  const base = "http://127.0.0.1:5178";
  const companies = [{ company_id: "download-a", name: "下载甲公司", status: "active" }, { company_id: "download-b", name: "下载乙公司", status: "active" }];
  const errors = [], downloads = [], requests = [];
  let mode = "success", releaseHeld;
  page.on("pageerror", error => errors.push(error.message));
  page.on("download", download => downloads.push(download.suggestedFilename()));
  await page.goto("about:blank");
  await page.route(`${base}/api/**`, async route => {
    const request = route.request();
    const path = "/" + request.url().split("/").slice(3).join("/").split("?")[0];
    const query = Object.fromEntries((request.url().split("?")[1] || "").split("&").filter(Boolean).map(item => item.split("=").map(decodeURIComponent)));
    const reply = (data, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(data) });
    if (path === "/api/security-request") return reply({ provisioned: true, authenticated: true });
    if (path === "/api/dashboard/context") {
      const current = companies.find(company => company.company_id === query.company_id) || companies[0];
      return reply({ schema_version: 2, company: current.name, companies, current_company: current, periods: [], quarters: [], default_period: null, default_quarter: null });
    }
    if (path === "/api/dashboard/brief") return reply({ schema_version: 1, selected_period: null, data: null });
    if (path === "/api/local/jobs") return reply([
      { id: "browser-report", kind: "report_export", status: "succeeded", attempts: 1, last_error: null, result: {}, download_available: true, download_file_name: "经过校验的季度报表.xlsx" },
      { id: "cli-report", kind: "report_export", status: "succeeded", attempts: 1, last_error: null, result: { path: "C:/untrusted.xlsx" }, download_available: false, download_file_name: null },
      { id: "pending-report", kind: "report_export", status: "pending", attempts: 0, last_error: null, result: null, download_available: false, download_file_name: null },
      { id: "backup", kind: "portable_backup", status: "succeeded", attempts: 1, last_error: null, result: {}, download_available: false, download_file_name: null },
    ]);
    if (path === "/api/local/report-export/browser-report/download") {
      requests.push(query.company_id);
      if (mode === "error") return reply({ code: "report_download_invalid" }, 409);
      if (mode === "expired") return reply({ code: "owner_session_required" }, 401);
      const deliver = () => route.fulfill({ status: 200, contentType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", body: "synthetic fixture" });
      if (mode === "hold") { releaseHeld = deliver; return; }
      return deliver();
    }
    throw new Error(`Unexpected API ${path}`);
  });
  await page.goto(`${base}/?company_id=download-a`);
  await page.getByRole("button", { name: "后台任务", exact: true }).click();
  const button = page.getByRole("button", { name: "下载报表", exact: true });
  await button.waitFor();
  if (await button.count() !== 1) throw new Error("nonbrowser or incomplete job offers download");
  const completed = page.waitForEvent("download");
  await button.click(); await completed;
  if (downloads[0] !== "经过校验的季度报表.xlsx") throw new Error("download did not use verified filename");
  mode = "error";
  await button.click();
  await page.getByRole("alert").filter({ hasText: "报表文件尚不可下载" }).waitFor();
  mode = "hold";
  const started = page.waitForRequest(request => request.url().includes("/browser-report/download"));
  await button.click(); await started;
  await page.getByLabel("切换公司").selectOption("download-b");
  await page.getByRole("button", { name: "下载报表", exact: true }).waitFor();
  if (!releaseHeld) throw new Error("download was not held");
  await releaseHeld().catch(() => undefined);
  // A subsequent request establishes that the old company response cannot resume the UI.
  mode = "error";
  await page.getByRole("button", { name: "下载报表", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "报表文件尚不可下载" }).waitFor();
  if (downloads.length !== 1 || requests.at(-1) !== "download-b") throw new Error("download leaked across company switch");
  mode = "expired";
  await page.getByRole("button", { name: "下载报表", exact: true }).click();
  await page.getByRole("button", { name: "负责人登录", exact: true }).waitFor();
  if (await page.getByRole("heading", { name: "最近后台任务" }).count()) throw new Error("expired session retained jobs");
  if (errors.length) throw new Error(errors.join("\n"));
  console.log(JSON.stringify({ passed: true, downloads: downloads.length, companies: [...new Set(requests)], scenarios: ["verified-only", "filename", "error", "company-switch", "expired-session"] }));
}
