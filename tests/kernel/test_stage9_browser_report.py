"""Guard the browser benchmark against accepting partial synthetic books."""

import json
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from scripts import benchmark_stage9_browser, stage9_verified_open_preview
from scripts.benchmark_stage9_browser import (
    browser_source_paths,
    measurement_scope,
    prepare_browser_service,
    prepare_browser_static_runtime,
    qualify_book_report,
    require_report_source,
    sanitized_browser_stderr,
    validate_book_report,
)
from scripts.benchmark_stage9_switch_company import complete_switch_report


def complete_report():
    periods = ("2016-01", "2016-02")
    report = {
        "status": "complete",
        "source": "synthetic-source",
        "company": {
            "id": "synthetic-company",
            "database_id": "synthetic-database",
            "name": "阶段九合成独立业务企业",
        },
        "integrity": {
            "status": "verified",
            "coverage": dict.fromkeys(
                ("sources", "historical_adoption", "projections", "read_indexes"), "verified"
            ),
            "limitations": [],
        },
        "distribution": "independent_local_pairs",
        "requested_months": 2,
        "monthly_business_count": 1000,
        "business_count": 2000,
        "months": [
            {"period": period, "business_count": 1000, "closed": index == 0}
            for index, period in enumerate(periods)
        ],
        "snapshots": {
            period: {
                "closed": index == 0,
                "owner_confirmation": "synthetic-proof",
                "preview_digest": "synthetic-preview",
            }
            for index, period in enumerate(periods)
        },
    }
    report["verified_open_preview"] = fake_current_preview(report)
    return report


def fake_current_preview(report):
    period = report["months"][-1]["period"]
    return {
        "source": report["source"],
        "company_id": report["company"]["id"],
        "database_id": report["company"]["database_id"],
        "period": period,
        "construction_preview_digest": report["snapshots"][period]["preview_digest"],
        "digest": "current-source-preview",
        "epochs": {"accounting": 0, "material": 0, "management": 0},
        "state": [1, 0, 0, 0, 1, 0],
    }


def qualification_input(tmp_path):
    (tmp_path / ".tmp").mkdir(exist_ok=True)
    book = complete_report()
    book.update(source=str(tmp_path / "old-source"), root=str(tmp_path / ".tmp/stage9-root"),
                employee_count=0, registered_object_count=50, measurements={"old": "timing"})
    book["verified_open_preview"] = fake_current_preview(book)
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(book), encoding="utf-8")
    return book, input_path, tmp_path / "qualification.json"


def test_explicit_qualification_uses_one_real_dispatch_and_fresh_proof(tmp_path, monkeypatch):
    from types import SimpleNamespace

    book, input_path, output_path = qualification_input(tmp_path)
    original = deepcopy(book)
    events = []
    engine = object()
    source = tmp_path

    def dispatch(command, arguments, *, session_token):
        events.append((command, arguments, session_token))
        return {"digest": "resident-real-preview"}

    def independent(selected_engine, **options):
        assert selected_engine is engine
        assert options["dimensions"] == {
            "distribution": "independent_local_pairs", "businesses": 1000,
            "month_stats": book["months"], "objects_count": 50, "business_count": 2000,
        }
        events.append("registered-verifier")
        preview = options["preview_close"]("2016-02", owner_confirmation="synthetic-proof")
        current = {**fake_current_preview(book), "source": str(source),
                   "digest": preview["digest"]}
        return {"integrity": deepcopy(original["integrity"]), "integrity_ms": 11,
                "preview_ms": 22, "checkpoint_sha256": "checkpoint-bytes",
                "verified_open_preview": current, "integrity_contract": {"version": 1}}

    monkeypatch.setattr(stage9_verified_open_preview, "verify_book_open_preview", independent)
    monkeypatch.setattr(benchmark_stage9_browser, "qualification_source_files",
                        lambda _: ({"file": "hash"}, "source-manifest"))
    app = SimpleNamespace(engine=lambda _: engine, dispatch=dispatch)
    result = qualify_book_report(
        app, token="session", book=book, input_path=input_path, output_path=output_path,
        source=source, workspace=tmp_path, company_name=book["company"]["name"], preparation_ms=3,
    )
    assert result["status"] == "complete" and result["measurements"] == {}
    assert result["source"] == str(source)
    assert result["verified_open_preview"]["digest"] == "resident-real-preview"
    assert events == ["registered-verifier", ("preview_close", {
        "company_id": book["company"]["id"], "period": "2016-02",
        "owner_confirmation": "synthetic-proof",
    }, "session")]
    assert result["qualification"]["original_source"] == original["source"]
    assert result["qualification"]["input_report_sha256"]
    assert result["qualification"]["checkpoint_sha256"] == "checkpoint-bytes"
    assert book == original and json.loads(input_path.read_text()) == original
    assert json.loads(output_path.read_text(encoding="utf-8"))["status"] == "complete"
    with pytest.raises(FileExistsError):
        qualify_book_report(app, token="session", book=book, input_path=input_path,
                            output_path=output_path, source=source,
                            workspace=tmp_path, company_name=book["company"]["name"])
    assert len(events) == 2


@pytest.mark.parametrize("damage", (
    "verifier", "input", "source", "coverage", "dimensions", "real-root",
))
def test_qualification_failure_keeps_new_failure_without_old_proof(tmp_path, monkeypatch, damage):
    from types import SimpleNamespace

    book, input_path, output_path = qualification_input(tmp_path)
    source_calls = []

    def inventory(_):
        source_calls.append(1)
        return ({"file": "changed" if damage == "source" and len(source_calls) > 1 else "hash"},
                "manifest")

    def independent(*args, **options):
        if damage == "verifier":
            raise ValueError("registered verification failed")
        if damage == "input":
            input_path.write_text(json.dumps({**book, "extra": "drift"}), encoding="utf-8")
        integrity = deepcopy(book["integrity"])
        if damage == "coverage":
            integrity["coverage"].pop("sources")
        return {"integrity": integrity, "checkpoint_sha256": "checkpoint",
                "verified_open_preview": {**fake_current_preview(book), "source": str(tmp_path)}}

    if damage == "dimensions":
        book["registered_object_count"] = True
        input_path.write_text(json.dumps(book), encoding="utf-8")
    if damage == "real-root":
        book["root"] = str(tmp_path / "real-company")
        input_path.write_text(json.dumps(book), encoding="utf-8")
    monkeypatch.setattr(benchmark_stage9_browser, "qualification_source_files", inventory)
    monkeypatch.setattr(stage9_verified_open_preview, "verify_book_open_preview", independent)
    with pytest.raises(ValueError):
        qualify_book_report(SimpleNamespace(engine=lambda _: object()), token="session", book=book,
                            input_path=input_path, output_path=output_path, source=tmp_path,
                            workspace=tmp_path, company_name=book["company"]["name"])
    failed = json.loads(output_path.read_text(encoding="utf-8"))
    assert failed["status"] == "qualification_failed" and failed["measurements"] == {}
    assert failed.get("verified_open_preview") != book["verified_open_preview"]
    assert failed["qualification"]["original_source"] == book["source"]


def test_qualification_source_requires_complete_inventory_and_rejects_drift(tmp_path, monkeypatch):
    from scripts import snapshot_stage9_source

    files = {"src/example.py": "original"}
    monkeypatch.setattr(snapshot_stage9_source, "_inventory", lambda _: dict(files))
    manifest = {"status": "complete", "target": str(tmp_path), "files": dict(files),
                "file_count": 1, "sha256": snapshot_stage9_source._digest(files)}
    (tmp_path / "source-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert benchmark_stage9_browser.qualification_source_files(tmp_path)[0] == files
    files["src/example.py"] = "drift"
    with pytest.raises(ValueError, match="fixed inventory"):
        benchmark_stage9_browser.qualification_source_files(tmp_path)


@pytest.mark.parametrize("alias", ("input", "timing-output", "gate", "existing"))
def test_qualification_cli_refuses_reused_or_shared_output(tmp_path, monkeypatch, capsys, alias):
    folder = tmp_path / ".tmp"
    folder.mkdir()
    input_path, timing_path = folder / "stage9-input.json", folder / "stage9-timing.json"
    gate = folder / "stage9-ready.json"
    qualification = {"input": input_path, "timing-output": timing_path,
                     "gate": gate, "existing": folder / "stage9-existing.json"}[alias]
    if alias == "existing":
        qualification.write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "benchmark_stage9_browser.py", "--workspace", str(tmp_path),
        "--book-report", str(input_path), "--output", str(timing_path),
        "--qualify-output", str(qualification), "--node", "unused-node",
        "--playwright-module", "unused-module", "--timing-ready-file", str(gate),
        "--timing-start-file", str(folder / "stage9-start"),
    ])
    with pytest.raises(SystemExit, match="2"):
        benchmark_stage9_browser.main()
    assert "distinct new synthetic file" in capsys.readouterr().err
    if alias == "existing":
        assert qualification.read_text() == "preserve"
    assert not timing_path.exists()


def test_qualification_cli_explicitly_rejects_switch_before_preparation(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "benchmark_stage9_browser.py", "--book-report", "unused-input",
        "--output", "unused-output", "--qualify-output", "unused-qualification",
        "--switch-book-report", "unused-switch", "--node", "unused-node",
        "--playwright-module", "unused-module",
    ])
    with pytest.raises(SystemExit, match="2"):
        benchmark_stage9_browser.main()
    assert "does not support company switching" in capsys.readouterr().err


def test_limited_integrity_cannot_become_a_timed_page_sample():
    report = complete_report()
    report["integrity"]["limitations"] = ["synthetic coverage gap"]
    with pytest.raises(ValueError, match="integrity must be verified"):
        validate_book_report(report, company_name="阶段九合成独立业务企业")


@pytest.mark.parametrize(
    "section", ("sources", "historical_adoption", "projections", "read_indexes")
)
@pytest.mark.parametrize("state", (None, "failed", "partial", "limited"))
def test_every_integrity_coverage_must_be_explicitly_verified(section, state):
    report = complete_report()
    if state is None:
        report["integrity"]["coverage"].pop(section)
    else:
        report["integrity"]["coverage"][section] = state
    with pytest.raises(ValueError, match="complete coverage"):
        validate_book_report(report, company_name="阶段九合成独立业务企业")


@pytest.mark.parametrize("field", ("coverage", "limitations"))
@pytest.mark.parametrize("value", (None, "missing", {}))
def test_integrity_cannot_omit_coverage_or_explicit_empty_limitations(field, value):
    report = complete_report()
    if value == "missing":
        report["integrity"].pop(field)
    else:
        report["integrity"][field] = value
    with pytest.raises(ValueError, match="complete coverage"):
        validate_book_report(report, company_name="阶段九合成独立业务企业")


def test_timing_gate_waits_until_after_ready_without_exposing_credentials(tmp_path, monkeypatch):
    ready, release = tmp_path / "ready.json", tmp_path / "release"
    waits = []

    def allow_timing(delay):
        waits.append(delay)
        assert json.loads(ready.read_text()) == {
            "status": "timing_ready", "pid": 123, "output": "report.json"
        }
        release.touch()

    monkeypatch.setattr(benchmark_stage9_browser.os, "getpid", lambda: 123)
    monkeypatch.setattr(benchmark_stage9_browser.time, "sleep", allow_timing)
    benchmark_stage9_browser.wait_for_timing_release(ready, release, output="report.json")
    assert waits == [0.2]


def test_timing_gate_rejects_stale_release_and_times_out_without_starting(tmp_path):
    ready, release = tmp_path / "ready.json", tmp_path / "release"
    release.touch()
    with pytest.raises(ValueError, match="after the ready"):
        benchmark_stage9_browser.wait_for_timing_release(ready, release, output="report.json")
    assert not ready.exists()
    release.unlink()
    with pytest.raises(TimeoutError, match="No timing release"):
        benchmark_stage9_browser.wait_for_timing_release(
            ready, release, output="report.json", timeout=0
        )
    assert ready.is_file() and not release.exists()


def test_complete_book_selects_last_open_month():
    assert (
        validate_book_report(complete_report(), company_name="阶段九合成独立业务企业") == "2016-02"
    )


def test_complete_shape_can_be_checked_before_independent_integrity_verification():
    report = complete_report()
    report["status"] = "measuring"
    report.pop("integrity")
    original = deepcopy(report)
    assert (
        validate_book_report(report, company_name="阶段九合成独立业务企业", require_verified=False)
        == "2016-02"
    )
    assert report == original
    with pytest.raises(ValueError):
        validate_book_report(report, company_name="阶段九合成独立业务企业")
    report["business_count"] -= 1
    with pytest.raises(ValueError):
        validate_book_report(report, company_name="阶段九合成独立业务企业", require_verified=False)


@pytest.mark.parametrize(
    "mutation", ("short", "closed", "missing_preview", "count", "missing_requested")
)
def test_partial_or_mislabeled_book_cannot_enter_browser(mutation):
    report = deepcopy(complete_report())
    if mutation == "short":
        report["months"].pop()
        report["snapshots"].pop("2016-02")
        report["business_count"] = 1000
    elif mutation == "closed":
        report["months"][-1]["closed"] = True
        report["snapshots"]["2016-02"]["closed"] = True
    elif mutation == "missing_preview":
        report["snapshots"]["2016-02"]["preview_digest"] = ""
    elif mutation == "missing_requested":
        report.pop("requested_months")
    else:
        report["business_count"] -= 1
    with pytest.raises(ValueError):
        validate_book_report(report, company_name="阶段九合成独立业务企业")


def test_mixed_book_must_keep_requested_months_to_prove_completion():
    report = complete_report()
    report["distribution"] = "mixed_cumulative"
    report["company"]["name"] = "阶段九合成规模企业"
    report.pop("requested_months")
    with pytest.raises(ValueError):
        validate_book_report(report, company_name="阶段九合成规模企业")


def test_measurement_scope_separates_final_page_candidates_from_diagnostics():
    report = complete_report()
    report.update(distribution="mixed_cumulative", employee_count=50, requested_months=12)
    options = {"repeats": 30, "warmups": 3, "instrument": False, "navigation_only": None}
    assert measurement_scope(report, **options) == "main_page_candidate"
    assert measurement_scope(report, **(options | {"static_build": "dist"})) == "diagnostic"
    assert measurement_scope(report, **(options | {"repeats": 3})) == "diagnostic"
    assert measurement_scope(report, **(options | {"instrument": True})) == "diagnostic"
    assert measurement_scope(report, **(options | {"navigation_only": "cold"})) == "navigation_cold"
    report.update(distribution="independent_local_pairs", registered_object_count=50)
    assert measurement_scope(report, **options) == "independent_page_candidate"
    report.update(distribution="mixed_cumulative", employee_count=200, monthly_business_count=5000)
    assert measurement_scope(report, **options) == "pressure_diagnostic"


def test_browser_build_and_harness_must_share_selected_source(tmp_path):
    source = tmp_path / "selected"
    release = source / "src/ai_accounting/static/dashboard"
    dist = source / "frontend/dist"
    harness = source / "frontend/tests/browser-stage9-hot-refresh.cjs"
    dist.mkdir(parents=True)
    harness.parent.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    with pytest.raises(ValueError, match="release static build"):
        browser_source_paths(source)
    harness.write_text("// synthetic harness", encoding="utf-8")
    assert browser_source_paths(source, static_build="dist") == (dist, harness)
    with pytest.raises(ValueError, match="release static build"):
        browser_source_paths(source)
    release.mkdir(parents=True)
    (release / "index.html").write_text("<!doctype html>", encoding="utf-8")
    assert browser_source_paths(source) == (release, harness)
    with pytest.raises(ValueError, match="Unknown Stage 9"):
        browser_source_paths(source, static_build="unknown")


def test_browser_wait_guard_defers_rejection_without_swallowing_it():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    harness = Path(__file__).resolve().parents[2] / "frontend/tests/browser-stage9-hot-refresh.cjs"
    script = """
const assert = require('node:assert/strict');
const { waitForLater } = require(process.argv[1]);
(async () => {
  const error = new Error('expected timeout');
  const pending = waitForLater(Promise.reject(error));
  await new Promise(resolve => setTimeout(resolve, 20));
  await assert.rejects(pending, actual => actual === error);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        [node, "--unhandled-rejections=strict", "-e", script, str(harness)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_browser_default_brief_preserves_month_state_and_required_owner_prompt():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    harness = Path(__file__).resolve().parents[2] / "frontend/tests/browser-stage9-hot-refresh.cjs"
    script = """
const assert = require('node:assert/strict');
const { verifyBriefState } = require(process.argv[1]);
const digest = 'a'.repeat(64), other = 'b'.repeat(64);
const prepared = { state: 'prepared', preview_digest: digest };
const ready = { month_state: 'open', owner_review_request: { preview_digest: digest } };
verifyBriefState(ready, prepared);
verifyBriefState({ month_state: 'open', owner_review_request: null });
for (const state of ['closed', 'covered']) {
  verifyBriefState({ month_state: state, owner_review_request: null }, { state });
  assert.throws(() => verifyBriefState({ ...ready, month_state: state }), /requests owner review/);
}
assert.throws(() => verifyBriefState({ month_state: 'open', owner_review_request: null }, prepared),
  /lost its owner review prompt/);
assert.throws(() => verifyBriefState(
  { month_state: 'closed', owner_review_request: null }, prepared),
  /lost its open month state/);
assert.throws(() => verifyBriefState(ready, { state: 'closed' }), /source month state changed/);
assert.throws(() => verifyBriefState(ready, { ...prepared, preview_digest: other }),
  /preview was replaced/);
for (const month_state of [undefined, null, 'prepared', 'stale']) {
  assert.throws(() => verifyBriefState({ ...ready, month_state }), /invalid typed month state/);
}
const invalidLocators = [undefined, {}, { preview_digest: '' }, { preview_digest: 123 },
  { preview_digest: 'invalid' }];
for (const owner_review_request of invalidLocators) {
  assert.throws(() => verifyBriefState({ ...ready, owner_review_request }),
    /invalid typed owner review locator/);
}
"""
    result = subprocess.run(
        [node, "--unhandled-rejections=strict", "-e", script, str(harness)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_browser_stderr_diagnostic_redacts_secrets_and_keeps_timeout():
    diagnostic = sanitized_browser_stderr(
        "page.waitForResponse: Timeout 30000ms exceeded at "
        "http://127.0.0.1:1000/#ticket=secret-ticket "
        "Bearer secret-token C:\\private\\runner.js password=secret-password",
        secrets=("secret-ticket", "secret-token", "secret-password"),
    )
    assert "Timeout 30000ms exceeded" in diagnostic
    assert "secret-ticket" not in diagnostic
    assert "secret-token" not in diagnostic
    assert "secret-password" not in diagnostic
    assert "C:\\private" not in diagnostic


def test_browser_current_contracts_preserve_complete_pages_and_voucher_amounts():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    harness = Path(__file__).resolve().parents[2] / "frontend/tests/browser-stage9-hot-refresh.cjs"
    script = """
const assert = require('node:assert/strict');
const { verifyCollection, verifyVoucher, verifyMainPayload, formatFen } = require(process.argv[1]);
const schemas = require(require('node:path').join(require('node:path').dirname(process.argv[1]),
  '../src/api/generated/dashboardResponseSchemas.json'));
const version = name => schemas[name].properties.schema_version.const
  ?? schemas[name].properties.schema_version.enum[0];
const copy = value => JSON.parse(JSON.stringify(value));
const collection = (total = 0) => ({ items: Array.from({length: Math.min(20, total)}, () => ({})),
  page: {total_count: total, filtered_count: total, returned_count: Math.min(20, total),
    has_more: total > 20, next_cursor: total > 20 ? 'next' : null} });
verifyCollection(collection(48), 'main');
let broken = collection(48); broken.items.pop(); broken.page.returned_count--;
assert.throws(() => verifyCollection(broken, 'main'), /truncated/);
broken = collection(48); broken.page.has_more = false; broken.page.next_cursor = null;
assert.throws(() => verifyCollection(broken, 'main'), /continuation differs/);
broken = collection(48); broken.page.filtered_count = 49;
assert.throws(() => verifyCollection(broken, 'main'), /exceeds full count/);
const last = collection(8); last.page.total_count = 48; last.page.filtered_count = 48;
verifyCollection(last, 'cursor', {cursor: true});
assert.throws(() => verifyCollection(last, 'main'), /truncated/);
const amount = '900719925474099301';
const voucher = {number: '120', voucher_version_id: 'version', subject_id: 'subject',
  type: '销售', kind: 'sale', summary: '完整业务摘要', list_summary: '销售收款',
  business_amount_label: '业务金额', business_amount_fen: '-101', amount_fen: amount,
  state: '已入账', date: null,
  recognition: {precision: 'month', period: '2016-12', date: null, label: '按月确认'},
  asset: null, asset_members: [],
  lines: [{line_number: 1, code: '1002', account: '银行存款', debit_fen: amount, credit_fen: '0',
    party: '客户', party_state: 'known', source_label: '',
    parties: [{name: '客户', amount_fen: amount}]},
    {line_number: 2, code: '6001', account: '收入', debit_fen: '0', credit_fen: amount,
      party: '', party_state: 'not_applicable', source_label: '', parties: []}]};
verifyVoucher(voucher);
assert.equal(formatFen(amount), '¥9,007,199,254,740,993.01');
assert.equal(formatFen('-101'), '−¥1.01');
for (const mutate of [v => v.number = 120, v => v.lines.pop(),
    v => v.lines[1].credit_fen = '1', v => v.lines[1].line_number = 3,
    v => v.amount_fen = '1', v => v.lines[0].debit_fen = 100,
    v => delete v.business_amount_label, v => delete v.lines[0].parties]) {
  broken = copy(voucher); mutate(broken); assert.throws(() => verifyVoucher(broken));
}
const selected = {id: 'company', period: '2016-12'};
const base = {read_context: {company_id: 'company', as_of: '2017-01-10', read_version: 'read'},
  snapshot_version: 'snapshot', selected_period: {key: selected.period},
  read_semantics: {knowledge: 'current_knowledge', accounting: 'as_posted',
    system_time_replay: false}};
const sections = {brief: ['activity', 'vouchers', 'open_items'],
  funds: ['accounts', 'movements', 'statements', 'investment_products', 'investment_events'],
  employees: ['employees', 'labor_sources'], assets: ['assets', 'projects']};
for (const [key, names] of Object.entries(sections)) {
  const payload = {...copy(base), schema_version: version('dashboard_' + key),
    data: {collections: Object.fromEntries(names.map(name => [name, collection()]))}};
  if (key === 'brief') Object.assign(payload.data, {month_state: 'open', owner_review_request: null,
    voucher_count: 0, activity_count: 0, focused_voucher: null,
    financial_position: {assets_fen: '0', liabilities_fen: '0', equity_fen: '0'},
    workforce_cost: {has_activity: false},
    long_term_assets: {net_fen: '0', fixed_active_count: 0, intangible_active_count: 0}});
  const url = new URL('http://test/api/dashboard/' + key);
  verifyMainPayload(payload, key, selected, url, {requireVouchers: false});
  const old = copy(payload); old.schema_version--;
  assert.throws(() => verifyMainPayload(old, key, selected, url), /outdated response contract/);
  const missing = copy(payload); delete missing.data.collections[names[0]];
  assert.throws(() => verifyMainPayload(missing, key, selected, url, {requireVouchers: false}),
    /collection missing/);
  const wrongMonth = copy(payload); wrongMonth.selected_period.key = '2016-11';
  assert.throws(() => verifyMainPayload(wrongMonth, key, selected, url), /another month/);
  const wrongCompany = copy(payload); wrongCompany.read_context.company_id = 'other';
  assert.throws(() => verifyMainPayload(wrongCompany, key, selected, url), /another company/);
  const local = new URL(url); local.searchParams.set('section', names[0]);
  local.searchParams.set('expected_version', 'other');
  assert.throws(() => verifyMainPayload(payload, key, selected, local), /crossed snapshots/);
  if (key === 'brief') {
    for (const summary of ['financial_position', 'workforce_cost']) {
      const absentSummary = copy(payload); delete absentSummary.data[summary];
      assert.throws(() => verifyMainPayload(absentSummary, key, selected, url,
        {requireVouchers: false}), /main financial.*workforce summaries missing/);
    }
    const missingVouchers = copy(payload); delete missingVouchers.data.collections.vouchers;
    assert.throws(() => verifyMainPayload(missingVouchers, key, selected, url,
      {requireVouchers: false}), /vouchers.*missing/);
  }
}
const report = {...copy(base), schema_version: version('dashboard_quarterly_report'), statements:
  ['balance_sheet', 'profit_statement', 'cash_flow_statement'].map(key => ({key,
    columns: [{key: 'current_fen'}], rows: [{values: {current_fen: amount}}]}))};
verifyMainPayload(report, 'reports', selected, new URL('http://test/api/dashboard/quarterly-report'));
const oldReport = copy(report); oldReport.schema_version--;
assert.throws(() => verifyMainPayload(oldReport, 'reports', selected, new URL('http://test')),
  /outdated response contract/);
report.statements[2].rows[0].values.current_fen = 1.01;
assert.throws(() => verifyMainPayload(report, 'reports', selected, new URL('http://test')),
  /integer fen/);
"""
    result = subprocess.run(
        [node, "--unhandled-rejections=strict", "-e", script, str(harness)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "mutation, accepted",
    [
        ("missing_file", False),
        ("invalid_json", False),
        ("missing_contract", False),
        ("missing_version", False),
        ("string_version", False),
        ("multiple_versions", False),
        ("conflicting_versions", False),
        ("not_required", False),
        ("enum_version", True),
    ],
)
def test_browser_harness_requires_its_own_generated_versions(tmp_path, mutation, accepted):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    source = Path(__file__).resolve().parents[2]
    harness = tmp_path / "frontend/tests/browser-stage9-hot-refresh.cjs"
    schemas_path = tmp_path / "frontend/src/api/generated/dashboardResponseSchemas.json"
    harness.parent.mkdir(parents=True)
    schemas_path.parent.mkdir(parents=True)
    shutil.copyfile(source / "frontend/tests/browser-stage9-hot-refresh.cjs", harness)
    schemas = json.loads(
        (source / "frontend/src/api/generated/dashboardResponseSchemas.json").read_text(
            encoding="utf-8"
        )
    )
    asset = schemas["dashboard_assets"]
    version = asset["properties"]["schema_version"]
    if mutation == "missing_contract":
        del schemas["dashboard_assets"]
    elif mutation == "missing_version":
        del asset["properties"]["schema_version"]
    elif mutation == "string_version":
        version["const"] = str(version["const"])
    elif mutation == "multiple_versions":
        version["enum"] = [version.pop("const"), 999]
    elif mutation == "conflicting_versions":
        version["enum"] = [version["const"] + 1]
    elif mutation == "not_required":
        asset["required"].remove("schema_version")
    elif mutation == "enum_version":
        version["enum"] = [version.pop("const")]
    if mutation != "missing_file":
        schemas_path.write_text(
            "invalid JSON" if mutation == "invalid_json" else json.dumps(schemas),
            encoding="utf-8",
        )
    result = subprocess.run(
        [node, "--unhandled-rejections=strict", "-e", "require(process.argv[1])", str(harness)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if accepted:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        assert "generated schema version" in result.stderr or mutation in {
            "missing_file", "invalid_json"
        }


def test_browser_report_requires_exact_absolute_source(tmp_path):
    source = tmp_path / "source"
    with pytest.raises(ValueError, match="absolute source"):
        require_report_source({}, source)
    with pytest.raises(ValueError, match="differs"):
        require_report_source({"source": str(tmp_path / "other")}, source)
    require_report_source({"source": str(source)}, source)


def test_browser_prepares_static_runtime_before_opening_synthetic_catalog(monkeypatch, tmp_path):
    from ai_accounting.kernel import daemon, service

    events = []
    prepared = (object(), {"prepared-command": object()})

    def prepare():
        events.append("static")
        return prepared

    class FakeService:
        def __init__(self, root, *, enable_read_pool, _static_runtime):
            assert events == ["static"]
            assert root == tmp_path
            assert enable_read_pool is True
            assert _static_runtime is prepared
            events.append("catalog")

    monkeypatch.setattr(daemon, "_prepare_static_runtime", prepare)
    monkeypatch.setattr(service, "LocalService", FakeService)
    static_runtime, static_startup_ms = prepare_browser_static_runtime()
    app, startup_ms = prepare_browser_service(tmp_path, static_runtime, static_startup_ms)
    assert isinstance(app, FakeService)
    assert startup_ms >= static_startup_ms >= 0
    assert events == ["static", "catalog"]


def test_browser_static_freeze_precedes_book_report_json_load(monkeypatch, tmp_path):
    from ai_accounting.kernel import daemon

    # The CLI source selector changes these process globals before reading the
    # intentionally incomplete report. Keep that in-process test isolated.
    monkeypatch.setenv("STAGE9_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "path", sys.path.copy())
    (tmp_path / ".tmp").mkdir()
    report_path = tmp_path / ".tmp/stage9-synthetic-book.json"
    report_path.write_text(
        json.dumps({"source": str(Path(__file__).resolve().parents[2])}), encoding="utf-8"
    )
    output_path = tmp_path / ".tmp/stage9-unused-result.json"
    events = []
    original_read_text = Path.read_text

    def read_text(path, *args, **kwargs):
        if path == report_path:
            events.append("report")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(daemon, "_prepare_static_runtime", lambda: events.append("static") or ())
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(benchmark_stage9_browser.sys, "prefix", str(tmp_path / ".tmp-kernel-venv"))
    monkeypatch.setattr(
        benchmark_stage9_browser.sys,
        "argv",
        [
            "benchmark_stage9_browser.py",
            "--book-report",
            str(report_path),
            "--workspace",
            str(tmp_path),
            "--output",
            str(output_path),
            "--node",
            str(tmp_path / "node"),
            "--playwright-module",
            str(tmp_path / "playwright"),
        ],
    )
    with pytest.raises(KeyError, match="root"):
        benchmark_stage9_browser.main()
    assert events == ["static", "report"]
    assert not output_path.exists()


def test_switch_report_passes_same_complete_book_gate(tmp_path):
    class Book:
        def describe(self):
            report = complete_report()
            report["company"].update(id="switch", name="阶段九合成切换企业")
            report["requested_months"] = 1
            report["monthly_business_count"] = 40
            report["business_count"] = 40
            report["months"] = [{"period": "2016-01", "business_count": 40, "closed": False}]
            report["snapshots"] = {"2016-01": report["snapshots"]["2016-01"]}
            report["snapshots"]["2016-01"]["closed"] = False
            return report

    format_value = {"status": "released", "version": 1}
    report = complete_switch_report(
        Book(), complete_report()["integrity"], format_value, tmp_path, "primary", tmp_path
    )
    assert report["primary_company_id"] == "primary"
    assert report["integrity_contract"] == format_value
    assert report["source"] == str(tmp_path)
    report["verified_open_preview"] = fake_current_preview(report)
    assert validate_book_report(report, company_name="阶段九合成切换企业") == "2016-01"
