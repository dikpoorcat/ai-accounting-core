"""Verify real frozen export plans."""

import pytest
from test_exports import queue
from test_exports import setup as payment_setup_fixture
from test_integrity_content import damage, verify
from test_payroll_preparation import company
from test_reports import book as report_book_fixture
from test_reports import close_quarter, scenario
from test_tax_import import complete_details

from ai_accounting.kernel.backup import verify_file
from ai_accounting.kernel.contracts import KernelError

payment_setup = payment_setup_fixture
report_book = report_book_fixture


def assert_plan_is_verified_and_damage_rejected(engine):
    assert verify(engine)["status"] == "verified"
    assert verify_file(engine.store.path)["verification"]["status"] == "verified"
    damage(engine, "jobs", "UPDATE jobs SET payload=json_set(payload,'$.plan.extra_corruption',1)")
    with pytest.raises(KernelError) as failure:
        verify(engine, include_indexes=False)
    assert failure.value.details["reason"] == "export_plan_digest_mismatch"


def test_actual_payment_export_plan_is_fully_verified(payment_setup, tmp_path):
    instance, exporter, template = payment_setup
    queue(instance, exporter, template, tmp_path / "payment")
    assert_plan_is_verified_and_damage_rejected(instance.engine)


def test_actual_tax_import_plan_is_fully_verified(tmp_path):
    instance = company(tmp_path)
    exporter = complete_details(instance)
    plan = exporter.preview("2026-01")
    assert plan["status"] == "ready"
    exporter.confirm(
        "2026-01",
        preview_digest=plan["digest"],
        output_directory=str(tmp_path / "tax"),
        request_id=instance.request(),
    )
    assert_plan_is_verified_and_damage_rejected(instance.engine)


def test_actual_report_export_plan_and_frozen_quarter_are_fully_verified(report_book, tmp_path):
    exporter = scenario(report_book)
    close_quarter(report_book)
    plan = exporter.preview_export(2026, 1)
    exporter.confirm_export(
        2026,
        1,
        preview_digest=plan["digest"],
        epochs=plan["epochs"],
        output_directory=str(tmp_path / "reports"),
        request_id="report-job",
    )
    assert_plan_is_verified_and_damage_rejected(report_book[0])
