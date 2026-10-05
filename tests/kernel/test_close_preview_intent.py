"""Real close entry points retain authenticated conclusions, not a source graph."""

import json
from collections import defaultdict
from dataclasses import FrozenInstanceError, fields, replace
from hashlib import sha256

import pytest
from monthly_close_fixture import ready
from test_close_review_transport import prepared_company
from test_close_review_transport import resident as resident_fixture
from test_resident_service import PASSWORD

from ai_accounting.kernel.close_preview import ClosePreviewIntent
from ai_accounting.kernel.close_review import public_owner_review
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.security import IdentityError
from ai_accounting.kernel.security.native import SECOND
from ai_accounting.kernel.security.window import _owner_review_text, _owner_summary_text
from ai_accounting.kernel.types import canonical

resident = resident_fixture


def test_active_intent_keeps_only_immutable_exact_summary_and_no_caller_alias(resident):
    service, _, company, _, _, execute, _, preview = prepared_company(resident)
    expected = public_owner_review(preview["manifest"]["owner_review"])
    intent, summary = service.require_active_close_preview(
        company["id"], company["database_id"], "2026-01", preview["digest"]
    )
    assert isinstance(intent, ClosePreviewIntent)
    assert summary == expected
    assert json.loads(intent.summary_bytes) == expected
    assert type(intent.summary_bytes) is bytes
    assert not ({"manifest", "collections", "owner_review"} & {f.name for f in fields(intent)})
    assert len(intent.summary_bytes) < len(canonical(preview["manifest"]).encode("utf-8"))
    with pytest.raises(FrozenInstanceError):
        intent.period = "2026-02"
    summary["amounts"]["month_expense_fen"] = 777
    preview["manifest"]["owner_review"]["accounting_summary"]["month_expense_fen"] = 999
    assert execute("dashboard_close_review", period="2026-01")["owner_review"] == expected
    assert _owner_summary_text(expected) == _owner_review_text(
        {**preview["manifest"]["owner_review"],
         "accounting_summary": {
             **preview["manifest"]["owner_review"]["accounting_summary"],
             "month_expense_fen": 0,
         }}
    )
    service.close()
    assert service.close_previews == service.active_close_previews == {}


def test_intent_summary_sha_signature_and_all_bindings_reject_damage_before_stale(resident):
    service, engine, company, _, _, execute, read, preview = prepared_company(resident)
    key = (company["id"], company["database_id"], preview["digest"])
    intent = service.close_previews[key]
    altered = json.loads(intent.summary_bytes)
    altered["amounts"]["month_expense_fen"] += 1
    altered_bytes = canonical(altered).encode("utf-8")
    damages = [
        replace(intent, summary_bytes=altered_bytes),
        replace(intent, summary_bytes=altered_bytes, summary_sha256=sha256(altered_bytes).digest()),
        replace(intent, signature=b"x" * 32),
        replace(intent, company_id="another-company"),
        replace(intent, database_id="another-database"),
        replace(intent, period="2026-02"),
        replace(intent, preview_digest="0" * 64),
        replace(intent, epochs=(0, 0, 0)),
        replace(intent, read_version=(0, 0, 0, 1)),
        replace(intent, owner_confirmation="0" * 64),
    ]
    with engine.store.connection() as connection:
        connection.execute("UPDATE state SET management=management+1 WHERE id=1")
        connection.commit()
    for damaged in damages:
        service.close_previews[key] = damaged
        with pytest.raises(KernelError) as failure:
            execute("dashboard_close_review", period="2026-01")
        assert failure.value.code == "content_integrity_failed"
        status, _, _, wire = read()
        assert status == 500 and wire["code"] == "content_integrity_failed"
    service.close_previews[key] = intent
    assert execute("dashboard_close_review", period="2026-01")["reason"] == "snapshot_changed"


def test_repreview_retires_old_intent_and_cross_company_month_swap_is_rejected(resident):
    service, engine, company, proof, _, execute, _, preview = prepared_company(resident)
    first_key = (company["id"], company["database_id"], preview["digest"])
    first = service.close_previews[first_key]
    different = engine.register_evidence(
        b"explicit second owner confirmation", "text/plain", "second", request_id="second-proof"
    )["digest"]
    second = execute("preview_close", period="2026-01", owner_confirmation=different)
    second_key = (company["id"], company["database_id"], second["digest"])
    assert first_key not in service.close_previews
    second_intent = service.close_previews[second_key]
    ready(engine, proof, first="2026-02", last="2026-02")
    february = execute("preview_close", period="2026-02", owner_confirmation=proof)
    february_key = (company["id"], company["database_id"], february["digest"])
    other = service.catalog.create_company("91310000999999999X", "other synthetic company")
    other_engine = service.engine(other["id"])
    other_proof = other_engine.register_evidence(
        b"other synthetic confirmation", "text/plain", "other", request_id="other-proof"
    )["digest"]
    ready(other_engine, other_proof, first="2026-01", last="2026-01")
    other_preview = Periods(other_engine).preview_close("2026-01", owner_confirmation=other_proof)
    service._remember_close_preview(other_engine, other_preview, other_proof)
    other_key = (other["id"], other["database_id"], other_preview["digest"])
    for swapped in (first, service.close_previews[february_key], service.close_previews[other_key]):
        service.close_previews[second_key] = swapped
        with pytest.raises(KernelError) as failure:
            service.require_active_close_preview(
                company["id"], company["database_id"], "2026-01", second["digest"]
            )
        assert failure.value.code == "content_integrity_failed"
    service.close_previews[second_key] = second_intent


def test_real_payroll_preview_summary_and_native_window_have_identical_money(resident):
    from test_payroll import contribution_policy, income_tax_policy, opening, payroll, profile
    from test_payroll_corrections import Company

    service, engine, company, proof, _, execute, read, _ = prepared_company(resident, prepare=False)
    helper = Company.__new__(Company)
    helper.engine, helper.sequence = engine, 0
    helper.materials, helper.owner_confirmation = defaultdict(list), proof
    for fact, subject in (
        (profile(), "profile"),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
        (opening(), "opening"),
        (payroll(), "january"),
    ):
        helper.save(fact, subject)
    helper.confirm_payroll("january")
    helper.publish("january")
    for category in MATERIAL_CATEGORIES:
        evidence = sorted({ev for month, ev in helper.materials[category] if month <= "2026-01"})
        Periods(engine).inventory(
            "2026-01", category, evidence=evidence, expected=len(evidence),
            no_business=not evidence, confirmation_evidence=proof, request_id=helper.request(),
        )
    preview = execute("preview_close", period="2026-01", owner_confirmation=proof)
    expected = public_owner_review(preview["manifest"]["owner_review"])
    assert expected["amounts"]["month_expense_fen"] > 0
    assert execute("dashboard_close_review", period="2026-01")["owner_review"] == expected
    assert read()[3]["owner_review"]["amounts"]["month_expense_fen"] == str(
        expected["amounts"]["month_expense_fen"]
    )
    request = service.security_controller.request(
        kind="approve_period_close", company_id=company["id"], database_id=company["database_id"],
        period="2026-01", preview_digest=preview["digest"], epochs=preview["epochs"],
    )
    status, _, _, inspected = resident[3].native("native_inspect", request_id=request["request_id"])
    assert status == 200, inspected
    assert inspected["facts"]["owner_review"] == expected
    assert _owner_summary_text(inspected["facts"]["owner_review"]) == _owner_review_text(
        preview["manifest"]["owner_review"]
    )


def test_real_close_password_cancel_expiry_and_failed_close_keep_atomic_approval(
    resident, monkeypatch
):
    from ai_accounting.kernel import close_storage

    service, engine, company, proof, _, execute, _, preview = prepared_company(resident)
    controller, http = service.security_controller, resident[3]
    context = {
        "kind": "approve_period_close", "company_id": company["id"],
        "database_id": company["database_id"], "period": "2026-01",
        "preview_digest": preview["digest"], "epochs": preview["epochs"],
    }
    cancelled = controller.request(**context)["request_id"]
    assert controller.cancel(cancelled)["status"] == "cancelled"
    assert http.native("native_execute", request_id=cancelled,
                       password=PASSWORD.get_secret_value())[0] == 400
    expired = controller.request(**context)["request_id"]
    controller._records[expired].created_at -= 30 * 60 * SECOND
    assert controller.status(expired)["status"] == "expired"
    assert http.native("native_execute", request_id=expired,
                       password=PASSWORD.get_secret_value())[0] == 400
    active = controller.request(**context)["request_id"]
    with pytest.raises(IdentityError, match="PRIVATE_CHANNEL"):
        controller.dispatch("native_inspect", {"request_id": active})
    status, _, _, failed = http.native(
        "native_execute", request_id=active, password="incorrect synthetic password"
    )
    assert status != 200 and failed["code"] == "IDENTITY_AUTHENTICATION_FAILED"
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM security_close_approval").fetchone()[0] == 0
    # Keep the actual persisted throttle; advance this synthetic service clock.
    now = service.security.now()
    monkeypatch.setattr(service.security, "now", lambda: now + 31 * SECOND)
    status, _, _, approved = http.native(
        "native_execute", request_id=active, password=PASSWORD.get_secret_value()
    )
    assert status == 200, approved
    assert http.native("native_execute", request_id=active,
                       password=PASSWORD.get_secret_value())[0] == 400
    original = close_storage.write_close

    def failed_write(connection, *_args, **_kwargs):
        # The service has consumed approval inside this same company transaction.
        assert connection.execute(
            "SELECT consumed_at FROM security_close_approval WHERE id=?",
            (approved["approval_id"],),
        ).fetchone()[0] is not None
        raise RuntimeError("synthetic failure after approval consumption")

    monkeypatch.setattr(close_storage, "write_close", failed_write)
    close_payload = {
        "period": "2026-01", "owner_confirmation": proof, "preview_digest": preview["digest"],
        "epochs": preview["epochs"], "approval_id": approved["approval_id"], "request_id": "close",
    }
    with pytest.raises(RuntimeError, match="after approval consumption"):
        execute("close", **close_payload)
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT consumed_at FROM security_close_approval WHERE id=?",
            (approved["approval_id"],),
        ).fetchone()[0] is None
        assert connection.execute("SELECT count(*) FROM period_close").fetchone()[0] == 0
    assert service.require_active_close_preview(
        company["id"], company["database_id"], "2026-01", preview["digest"]
    )[0].owner_confirmation == proof
    monkeypatch.setattr(close_storage, "write_close", original)
    assert execute("close", **close_payload)["status"] == "closed"
    assert service.close_previews == service.active_close_previews == {}
