"""Commentary content, submission concurrency and frozen adoption are distinct."""

import json
from types import SimpleNamespace

import pytest
from test_display import database_state, profile, save_commentary
from test_exports import setup as setup  # noqa: F401
from test_opening_continuation import book as book  # noqa: F401
from test_payroll import payroll
from test_payroll_tax_declarations import adopt, declare

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.display import Display
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.types import canonical, digest


def expense(period):
    return {
        "period": period,
        "amount_fen": 100,
        "counterparty_id": "supplier",
        "creditor_kind": "supplier",
        "expense_class": "administration",
    }


def test_future_write_preserves_content_but_not_submission_and_relevant_sources_expire(book):
    engine, save, publish, _, proof = book
    # Establish the page's selected month through the same typed business API.
    save("expense", "baseline", expense("2026-01"))
    publish("baseline")
    display = Display(engine)
    saved = save_commentary(display)
    before_future = display.preview_period_commentary("2026-01")
    save("expense", "future", expense("2026-02"))
    after_future = display.preview_period_commentary("2026-01")
    assert after_future["content_digest"] == before_future["content_digest"]
    assert after_future["context_digest"] != before_future["context_digest"]
    assert after_future["current"]["id"] == saved["id"]
    page = Dashboard(engine).brief("2026-01")
    page_content = page["data"]["management_commentary_details"]
    assert page_content["current"]["id"] == saved["id"]
    assert page_content["current"]["content_validity"]["status"] == "current"
    assert page["data"]["management_commentary"] == saved["text"]
    assert page["read_semantics"]["knowledge"] == "current_knowledge"
    before = database_state(engine)
    with pytest.raises(KernelError) as stale:
        display.update_period_commentary(
            "2026-01",
            "过期提交",
            context_digest=before_future["context_digest"],
            expected_revision=1,
            source="确认",
            request_id="old-preview",
        )
    assert stale.value.code == "preview_expired" and database_state(engine) == before
    save("expense", "current", expense("2026-01"))
    assert display.preview_period_commentary("2026-01")["status"] == "stale"
    save_commentary(display, request_id="review-pending")
    publish("current")
    assert display.preview_period_commentary("2026-01")["status"] == "stale"
    save_commentary(display, request_id="review-published")
    Periods(engine).inventory(
        "2026-01",
        "transactions",
        evidence=[],
        expected=0,
        no_business=True,
        confirmation_evidence=proof,
        request_id="inventory",
    )
    assert display.preview_period_commentary("2026-01")["status"] == "stale"


def test_cross_month_pending_cause_is_not_filtered_out(book):
    engine, save, publish, _, _ = book
    agreement = {
        "period": "2026-01",
        "lender_id": "bank",
        "lender_is_licensed": True,
        "currency": "CNY",
        "annual_rate_percent": "3",
        "day_count_basis": "actual_360",
        "maturity_date": "2027-01-01",
        "loan_term": "short_term",
    }
    save("loan_agreement", "agreement", agreement)
    save(
        "loan_drawdown",
        "loan",
        {
            "period": "2026-01",
            "agreement_id": "agreement",
            "principal_fen": 10000,
            "actual_date": "2026-01-12",
            "bank_account_id": "bank",
        },
    )
    publish("loan")
    display = Display(engine)
    save_commentary(display)
    changed = save("loan_agreement", "agreement", {**agreement, "period": "2026-02"}, revision=1)
    preview = display.preview_period_commentary("2026-01")
    assert preview["status"] == "stale"
    assert {"subject_id": "loan", "cause_id": changed["fact_id"]} in preview["content_basis"][
        "pending"
    ]
    assert preview["content_basis"]["accounting_sources"]["vouchers"][0]["period"] == "2026-01"


def test_related_later_payroll_sources_are_exact_and_unrelated_future_wages_are_excluded(setup):
    company, _, _ = setup
    display = Display(company.engine)
    original = company.engine.ledger("2026-01")
    save_commentary(display)
    declaration_fact, declaration = declare(
        company, period="2026-02", declaration_date="2026-02-06"
    )
    assert display.preview_period_commentary("2026-01")["status"] == "stale"
    _, basis = adopt(company, declaration, period="2026-02")
    save_commentary(display, request_id="review-actuals")
    before = display.preview_period_commentary("2026-01")
    assert {declaration["fact_id"], basis["fact_id"]} <= {
        row["id"] for row in before["basis"]["supplementary_sources"]
    }
    company.save(payroll(period="2026-03"), "march")
    after = display.preview_period_commentary("2026-01")
    assert after["content_digest"] == before["content_digest"] and after["status"] == "current"
    # A same-content revision is still a new precise declaration source.
    changed = company.engine.amend_fact(
        declaration_fact.kind,
        "declared",
        declaration_fact.model_dump(mode="json"),
        evidence=(company.owner_confirmation,),
        expected_revision=1,
        recording_error_confirmed=True,
        request_id=company.request(),
    )
    after = display.preview_period_commentary("2026-01")
    assert after["status"] == "stale"
    assert changed["fact_id"] in {row["id"] for row in after["basis"]["supplementary_sources"]}
    with company.engine.store.connection(read_only=True) as connection:
        adopted = company.engine.store.fact(connection, basis["fact_id"])
        assert adopted.fact.declaration_fact_id == declaration["fact_id"]
    assert company.engine.ledger("2026-01") == original


def test_frozen_original_and_supplements_have_separate_validity(book, monkeypatch):
    from test_close_range import ready

    engine, *_ = book
    proof = book[-1]
    ready(engine, proof, last="2026-01")
    display, periods = Display(engine), Periods(engine)
    original = save_commentary(display)
    preview = periods.preview_close("2026-01", owner_confirmation=proof)
    periods.close(
        "2026-01",
        owner_confirmation=proof,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="close",
    )
    frozen = periods.closed_report("2026-01")
    supplement = save_commentary(display, text="后来核对的说明", request_id="supplement")
    display.save_display_profile(
        profile(employment_start="2026-01"), expected_revision=0, request_id="profile"
    )
    current = display.preview_period_commentary("2026-01")
    assert current["current"]["id"] == original["id"]
    assert current["frozen"]["content_validity"]["status"] == "frozen"
    assert current["supplements"][0]["id"] == supplement["id"]
    assert current["supplements"][0]["content_validity"]["status"] == "stale"
    page = Dashboard(engine).brief("2026-01")
    page_content = page["data"]["management_commentary_details"]
    assert page["data"]["management_commentary"] == original["text"]
    assert page_content["current"]["id"] == original["id"]
    assert page_content["frozen"]["content_validity"]["status"] == "frozen"
    assert page_content["supplements"][0]["id"] == supplement["id"]
    assert page_content["supplements"][0]["content_validity"]["status"] == "stale"
    assert page["read_semantics"]["accounting"] == "frozen_close"
    assert page["read_semantics"]["knowledge"] == "current_knowledge"
    preparation = page["data"]["period_preparation"]
    assert preparation["closure"]["state"] == "exact_close"
    assert preparation["frozen_readiness"]["status"] == "ready"
    assert preparation["current_followups"]["affects_frozen_readiness"] is False
    assert periods.closed_report("2026-01") == frozen
    ledger = engine.ledger("2026-01")
    original_record = Display._record

    def damaged_read(row):
        record = original_record(row)
        if record and record.get("id") == original["id"] and "text" in record:
            record["text"] = "冻结原文读取损坏"
        return record

    monkeypatch.setattr(Display, "_record", staticmethod(damaged_read))
    damaged = display.preview_period_commentary("2026-01")
    assert damaged["frozen"]["text"] == "冻结原文读取损坏"
    assert damaged["frozen"]["content_validity"]["status"] == "unverifiable"
    assert damaged["current"] is None and damaged["status"] == "stale"
    assert engine.ledger("2026-01") == ledger


def test_commentary_and_basis_rollback_together_and_no_commentary_skips_content(book, monkeypatch):
    engine, *_ = book
    display = Display(engine)
    preview = display.preview_period_commentary("2026-01")
    before = database_state(engine)

    def fail(point, connection):
        if point == "published":
            raise RuntimeError("synthetic write failure")

    monkeypatch.setattr(engine, "fault", fail)
    with pytest.raises(RuntimeError):
        display.update_period_commentary(
            "2026-01",
            "说明",
            context_digest=preview["context_digest"],
            expected_revision=0,
            source="确认",
            request_id="failure",
        )
    assert database_state(engine) == before

    def unexpected(*args, **kwargs):
        raise AssertionError("ordinary empty commentary constructed content")

    monkeypatch.setattr(Display, "_content_basis", unexpected)
    with engine.store.connection(read_only=True) as connection:
        assert Display.commentary(connection, "2026-01")["status"] == "not_provided"
        assert Display.snapshot(connection, "2026-01")["commentary_status"] == "not_provided"


@pytest.mark.parametrize(
    "damage", ["missing", "contract", "digest", "association", "malformed", "commentary"]
)
def test_untrusted_basis_only_invalidates_its_commentary(book, damage):
    engine, *_ = book
    display = Display(engine)
    saved = save_commentary(display)
    with engine.store.connection(read_only=True) as connection:
        context = Display._context(connection, "2026-01", registry=engine.store.registry)
        raw = dict(connection.execute("SELECT * FROM period_commentary_basis").fetchone())
        if damage == "missing":
            raw = None
        elif damage == "contract":
            raw["contract"] = "unknown-content-v99"
        elif damage == "digest":
            raw["content_digest"] = digest("not the content")
        elif damage == "association":
            envelope = json.loads(raw["basis"])
            envelope["adoption"]["commentary_id"] = "another-commentary"
            raw["basis"] = canonical(envelope)
        elif damage == "malformed":
            raw["basis"] = "[]"
        elif damage == "commentary":
            saved = {**saved, "text": "正文已损坏但仍保留原摘要"}
        # Model a retained corrupt read, without weakening production triggers.
        proxy = SimpleNamespace(execute=lambda *_: SimpleNamespace(fetchone=lambda: raw))
        for frozen_digest in (None, saved["digest"]):
            validity = Display._validity(proxy, saved, context, frozen_digest=frozen_digest)
            assert validity["status"] == "unverifiable"
            assert validity.get("method") != "legacy_strict"
            if damage == "commentary":
                assert validity["reason"] == "commentary_digest_mismatch"
    assert engine.ledger("2026-01") == []
