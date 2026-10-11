"""Complete pending groups retain exact historical money and bounded members."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from test_banking import book as book
from test_banking import close_month, inventories
from test_dashboard_provenance import profile
from test_integrity_content import damage
from test_opening_continuation import _close_without_current_business
from test_opening_continuation import book as opening_book_fixture
from test_payroll import contribution_policy, income_tax_policy, opening, payroll
from test_payroll import profile as payroll_profile
from test_payroll_corrections import Company, payment

from ai_accounting.kernel import dashboard_open_groups as group_reader
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import _Snapshot
from ai_accounting.kernel.dashboard_open_groups import (
    _group,
    _identity,
    open_group_members,
    open_group_page,
)
from ai_accounting.kernel.domains.payroll import PayrollBounded
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.query_reads import QueryReads

opening_book = opening_book_fixture


@contextmanager
def snapshot(engine, period):
    with QueryReads.snapshot(engine) as reads:
        snap = _Snapshot(engine, reads.connection, period, reads)
        try:
            yield snap
        finally:
            snap.release()


def expenses(book, count, period="2026-09", party="supplier", prefix="expense"):
    engine, save, publish, proof = book
    subjects = []
    for index in range(count):
        subject = f"{prefix}-{index:03}"
        save(
            "expense",
            subject,
            {
                "period": period,
                "counterparty_id": party,
                "amount_fen": 100 + index,
                "expense_class": "administration",
                "creditor_kind": "supplier",
            },
        )
        subjects.append(subject)
    publish(*subjects)
    return sum(100 + index for index in range(count))


def test_more_than_twenty_object_groups_paginate_complete_money_without_bodies(book, monkeypatch):
    engine, _, _, _ = book
    amount = sum(
        expenses(book, 1, party=f"supplier-{index:03}", prefix=f"expense-{index:03}")
        for index in range(26)
    )

    def no_body(*args, **kwargs):
        raise AssertionError("group summaries must not hydrate facts or results")

    def no_facts(self, identifiers):
        assert not list(identifiers), "group summaries must not hydrate fact bodies"
        return {}

    monkeypatch.setattr(QueryReads, "facts", no_facts)
    monkeypatch.setattr(QueryReads, "verify_selected_content", no_body)
    with snapshot(engine, "2026-09") as snap:
        first = open_group_page(snap)
        second = open_group_page(snap, after=first["collection"]["page"]["next_cursor"])
        complete = open_group_page(snap, limit=100)
        assert first["group_count"] == first["total_count"] == 26
        assert first["payable_fen"] == amount
        assert first["categories"][0]["group_count"] == 26
        assert first["categories"][0]["count"] == 26
        assert first["categories"][0]["outstanding_fen"] == amount
        assert first["collection"]["page"]["returned_count"] == 20
        assert second["collection"]["page"]["returned_count"] == 6
        assert not second["collection"]["page"]["has_more"]
        rows = first["collection"]["items"] + second["collection"]["items"]
        assert rows == complete["collection"]["items"]
        assert len({row["group_key"] for row in rows}) == 26
        assert sum(row["outstanding_fen"] for row in rows) == amount
        assert all(row["member_count"] == 1 for row in rows)


@pytest.mark.parametrize("closed", [False, True])
def test_complete_group_spans_months_and_members_are_twenty_bounded(book, closed, monkeypatch):
    engine, save, publish, proof = book
    amount = expenses(book, 25)
    profile(engine, "counterparty", "supplier", display_name="同名供应商")
    profile(engine, "business", "expense-000", purpose="九月场地费", note="已收到发票")
    if closed:
        assert (
            close_month(inventories(engine, proof, "2026-09", {"transactions"}), proof, "2026-09")[
                "status"
            ]
            == "closed"
        )
        profile(engine, "business", "expense-000", 1, purpose="后来改写用途", note="后来备注")
    amount += expenses(book, 1, "2026-10", prefix="later")
    other = expenses(book, 1, "2026-10", party="other-supplier", prefix="other")
    profile(engine, "counterparty", "other-supplier", display_name="同名供应商")
    ledger = engine.ledger("2026-10")
    if closed:
        with snapshot(engine, "2026-09") as snap:
            historical_group = open_group_page(snap)["collection"]["items"][0]
            historical_members = open_group_members(snap, historical_group["group_key"])["items"]
            assert (
                next(
                    row["purpose"]
                    for row in historical_members
                    if row["subject_id"] == "expense-000"
                )
                == "九月场地费；已收到发票"
            )

    def no_body(*args, **kwargs):
        raise AssertionError("pending groups must not hydrate facts or results")

    monkeypatch.setattr(QueryReads, "facts", no_body)
    monkeypatch.setattr(QueryReads, "verify_selected_content", no_body)
    with snapshot(engine, "2026-10") as snap:
        first = open_group_page(snap, limit=1)
        assert first["total_count"] == 27 and first["group_count"] == 2
        assert first["payable_fen"] == amount + other
        assert first["collection"]["page"]["total_count"] == 2
        second = open_group_page(snap, after=first["collection"]["page"]["next_cursor"], limit=1)
        groups = first["collection"]["items"] + second["collection"]["items"]
        group = next(item for item in groups if item["member_count"] == 26)
        assert group["source_amount_fen"] == group["outstanding_fen"] == amount
        category = first["categories"][0]
        assert category["count"] == 27 and category["group_count"] == 2
        members = open_group_members(snap, group["group_key"], limit=20)
        assert len(members["items"]) == 20 and members["page"]["total_count"] == 26
        assert all(
            item["date"] is None and item["recognition"]["precision"] == "month"
            for item in members["items"]
        )
        assert all(item["voucher_version_id"] is not None for item in members["items"])
        purpose = next(
            item["purpose"] for item in members["items"] if item["subject_id"] == "expense-000"
        )
        assert purpose == ("后来改写用途；后来备注" if closed else "九月场地费；已收到发票")
        tail = open_group_members(
            snap, group["group_key"], after=members["page"]["next_cursor"], limit=20
        )
        assert len(tail["items"]) == 6 and not tail["page"]["has_more"]
        assert [item["source_period"] for item in tail["items"]] == ["2026-09"] * 5 + ["2026-10"]
        assert len({item["id"] for item in members["items"] + tail["items"]}) == 26
    assert engine.ledger("2026-10") == ledger


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("kind", ["payroll", "payroll_bounded"])
def test_payroll_social_and_housing_are_two_matters_across_months(
    tmp_path, closed, kind, monkeypatch
):
    company = Company(tmp_path / "four-contributions.sqlite")
    policy = contribution_policy()
    housing = policy.rules[0].model_copy(
        update={
            "code": "housing",
            "base_kind": "housing_fund",
            "employee_rate": "0.04",
            "employer_rate": "0.04",
        }
    )
    company.save(policy.model_copy(update={"rules": (*policy.rules, housing)}), "contributions")
    company.save(
        payroll_profile(
            effective_to="2026-02", housing_fund_base_fen=1000000, housing_fund_participating=True
        ),
        "profile",
    )
    company.save(income_tax_policy(), "income-tax")
    company.save(opening(), "opening")
    wages = [payroll(), payroll(period="2026-02")]
    if kind == "payroll_bounded":
        wages = [PayrollBounded(**wage.model_dump(mode="json")) for wage in wages]
    company.save(wages[0], "january")
    company.save(wages[1], "february")
    company.confirm_payroll("january", "february")
    company.publish("january")
    net_amount = company.current("january", kind).values["net_fen"]
    with snapshot(company.engine, "2026-01") as snap:
        original = {
            item["id"]: item
            for group in open_group_page(snap)["collection"]["items"]
            if group["description"] in {"社保", "公积金"}
            for item in open_group_members(snap, group["group_key"])["items"]
        }
    for subject in ("january", "february"):
        profile(
            company.engine,
            "business",
            subject,
            purpose="依据模拟工资资料第六行核对",
            note="模拟负责人复核说明",
        )
        Periods(company.engine).management(
            subject,
            payment_period=None,
            payment_category=None,
            expected_revision=0,
            request_id=f"management:{subject}",
        )
    if closed:
        company.close("2026-01")
    company.publish("february")
    later_payment = payment(amount=10000)
    company.save(
        later_payment.model_copy(
            update={
                "allocations": tuple(
                    allocation.model_copy(update={"source_kind": kind})
                    for allocation in later_payment.allocations
                ),
            }
        ),
        "later-payment",
    )
    company.publish("later-payment")
    ledger = company.engine.ledger("2026-02")
    with snapshot(company.engine, "2026-02") as snap:
        data = open_group_page(snap)
        groups = data["collection"]["items"]
        for title, components in (
            ("社保", {"employee_social", "employer_social"}),
            ("公积金", {"employee_housing", "employer_housing"}),
        ):
            contribution = next(row for row in groups if row["description"] == title)
            assert contribution["member_count"] == 4
            original_prime = snap.metadata.prime_profiles

            def no_business_profiles(profile_kind, identifiers, _original_prime=original_prime):
                assert profile_kind != "business", "contribution members do not read business notes"
                return _original_prime(profile_kind, identifiers)

            def no_management(*args, **kwargs):
                raise AssertionError("contribution members do not read management notes")

            with monkeypatch.context() as patch:
                patch.setattr(snap.metadata, "prime_profiles", no_business_profiles)
                patch.setattr(snap.management, "prime", no_management)
                members = open_group_members(snap, contribution["group_key"])["items"]
            assert len({row["contribution_group_key"] for row in members}) == 2
            assert {row["contribution_component"] for row in members} == components
            assert {row["payroll_period"] for row in members} == {"2026-01", "2026-02"}
            assert all(row["purpose"] is None for row in members)
            assert all(row["voucher_version_id"] and row["voucher_number"] for row in members)
            for row in members:
                if row["id"] in original:
                    assert row == original[row["id"]]
        tax = next(row for row in groups if row["description"] == "个人所得税")
        assert tax["category_key"] == "tax_payables"
        net = next(row for row in groups if row["description"] == "实发工资")
        assert net["member_count"] == 2 and net["outstanding_fen"] == 2 * net_amount - 10000
        assert all(
            row["purpose"] is None for row in open_group_members(snap, net["group_key"])["items"]
        )
    with snapshot(company.engine, "2026-01") as snap:
        net = next(
            row
            for row in open_group_page(snap)["collection"]["items"]
            if row["description"] == "实发工资"
        )
        assert net["outstanding_fen"] == net_amount
        assert net["current_outstanding_fen"] == net_amount - 10000
    assert company.engine.ledger("2026-02") == ledger
    with company.engine.store.connection(read_only=True) as connection:
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT purpose,note FROM display_profile_revision ORDER BY entity_id"
            )
        ] == [("依据模拟工资资料第六行核对", "模拟负责人复核说明")] * 2
        assert "note" not in {
            row["name"] for row in connection.execute("PRAGMA table_info(management_revision)")
        }


def test_group_identity_retains_formal_matter_direction_and_unknown_is_independent(book):
    engine, *_ = book
    base = {
        "key": "expense:a:primary",
        "source_business": {"kind": "expense", "subject_id": "a"},
        "name": "primary",
        "source_fact_id": "fact",
        "category": "payable",
        "category_key": "supplier_payables",
        "counterparty_id": None,
    }
    with snapshot(engine, "2026-09") as snap:
        first = _identity(snap, base, {})["group_key"]
        assert _identity(snap, base | {"key": "expense:b:primary"}, {})["group_key"] != first
        named = base | {"counterparty_id": "supplier"}
        assert (
            _identity(snap, named, {})["group_key"]
            != _identity(snap, named | {"key": "expense:b:primary"}, {})["group_key"]
        )
        scalars = {"fact": {"creditor_kind": "supplier", "expense_class": "administration"}}
        first = _identity(snap, named, scalars)["group_key"]
        assert _identity(snap, named | {"key": "expense:b:primary"}, scalars)["group_key"] == first
        assert (
            _identity(
                snap,
                named | {"category": "receivable", "category_key": "other_receivables"},
                scalars,
            )["group_key"]
            != first
        )
        assert _identity(snap, named | {"name": "different"}, scalars)["group_key"] != first


@pytest.mark.parametrize("closed", [False, True])
def test_opening_contributions_use_employee_identity_and_keep_no_voucher_members(
    opening_book, closed
):
    engine, save, publish, package, proof = opening_book
    components = (
        "net",
        "withheld_tax",
        "employee_social",
        "employer_social",
        "employee_housing",
        "employer_housing",
    )
    package(
        [
            ("opening_cash", "cash-start", {"cash_account_id": "cash", "balance_fen": 12000}),
            *[
                (
                    "opening_payroll_payable",
                    "prior-" + component,
                    {
                        "employee_id": "employee",
                        "recipient_id": "employee" if component == "net" else "authority",
                        "payroll_period": "2025-12",
                        "component": component,
                        "outstanding_fen": 2000,
                    },
                )
                for component in components
            ],
        ]
    )
    for component in components:
        subject = "prior-" + component
        profile(engine, "business", subject, purpose="模拟期初核算依据", note="模拟期初复核说明")
        Periods(engine).management(
            subject,
            payment_period=None,
            payment_category=None,
            expected_revision=0,
            request_id=f"management:{subject}",
        )
    if closed:
        _close_without_current_business(engine, "2026-01", proof)
    ledger = engine.ledger("2026-01")
    with snapshot(engine, "2026-01") as snap:
        groups = open_group_page(snap)["collection"]["items"]
        assert len(groups) == 4
        for title in ("社保", "公积金"):
            contribution = next(row for row in groups if row["description"] == title)
            assert contribution["member_count"] == 2 and contribution["outstanding_fen"] == 4000
            members = open_group_members(snap, contribution["group_key"])["items"]
            assert {row["payroll_period"] for row in members} == {"2025-12"}
            assert {row["source_period"] for row in members} == {"2026-01"}
            assert all(row["voucher_version_id"] is None for row in members)
            assert all(row["purpose"] is None for row in members)
            assert all(
                row["source_amount_fen"] == row["outstanding_fen"] == 2000 for row in members
            )
        opening_source = {
            "key": "opening_payroll_payable:prior-net:primary",
            "source_business": {"kind": "opening_payroll_payable"},
            "source_fact_id": "opening-fact",
            "category_key": "payroll_payables",
            "category": "payable",
            "name": "primary",
            "counterparty_id": "employee",
        }
        wage_source = opening_source | {
            "key": "payroll:wage:net",
            "source_business": {"kind": "payroll"},
            "source_fact_id": "wage-fact",
            "name": "net",
        }
        scalars = {
            "opening-fact": {"component": "net", "employee_id": "employee"},
            "wage-fact": {"employee_id": "employee"},
        }
        assert (
            _identity(snap, opening_source, scalars)["group_key"]
            == _identity(snap, wage_source, scalars)["group_key"]
        )
    assert engine.ledger("2026-01") == ledger


def test_frozen_group_rejects_modified_member_state(book):
    engine, save, publish, proof = book
    expenses(book, 2)
    assert (
        close_month(inventories(engine, proof, "2026-09", {"transactions"}), proof, "2026-09")[
            "status"
        ]
        == "closed"
    )
    damage(
        engine,
        "settlement_state_revision",
        "UPDATE settlement_state_revision SET payload=json_set(payload,'$.source_amount',9999) "
        "WHERE obligation_key='expense:expense-001:primary'",
    )
    with snapshot(engine, "2026-09") as snap:
        with pytest.raises(KernelError) as caught:
            open_group_page(snap)
        assert caught.value.code == "content_integrity_failed"


def test_frozen_member_uses_its_precise_voucher_after_later_correction(book):
    engine, save, publish, proof = book
    expenses(book, 1)
    assert (
        close_month(inventories(engine, proof, "2026-09", {"transactions"}), proof, "2026-09")[
            "status"
        ]
        == "closed"
    )
    with snapshot(engine, "2026-09") as snap:
        group = open_group_page(snap)["collection"]["items"][0]
        original = open_group_members(snap, group["group_key"])["items"][0]
    save(
        "expense",
        "expense-000",
        {
            "period": "2026-09",
            "counterparty_id": "supplier",
            "amount_fen": 200,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
        revision=1,
    )
    preview = engine.preview(["expense-000"], posting_period="2026-10")
    engine.confirm(
        ["expense-000"],
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        posting_period="2026-10",
        request_id="later-correction",
    )
    with snapshot(engine, "2026-09") as snap:
        group = open_group_page(snap)["collection"]["items"][0]
        member = open_group_members(snap, group["group_key"])["items"][0]
        assert member["voucher_version_id"] == original["voucher_version_id"]
        assert member["source_amount_fen"] == member["outstanding_fen"] == 100
        assert member["current_outstanding_fen"] == 200
        assert member["source_period"] == member["recognition"]["period"] == "2026-09"


def test_group_status_keeps_mixed_overpayment_unknown_and_partial(book):
    engine, *_ = book
    first = {
        "key": "first",
        "category_key": "supplier_payables",
        "source_amount_fen": 100,
        "paid_fen": 200,
        "other_settled_fen": 0,
        "remaining_fen": -100,
        "settlement_status": "over_settled",
    }
    second = first | {
        "key": "second",
        "paid_fen": 0,
        "remaining_fen": 100,
        "settlement_status": "open",
    }
    data = {
        "groups": {"group": [first, second]},
        "identities": {
            "first": {"party_id": None, "missing_party": "未提供", "description": "费用"}
        },
        "current_by_key": {
            "first": first | {"remaining_fen": 0, "settlement_status": "settled"},
            "second": second,
        },
    }
    with snapshot(engine, "2026-09") as snap:
        group = _group(snap, data, "group")
        assert group["outstanding_fen"] == 0 and group["status"] == "over_settled"
        assert group["current_outstanding_fen"] == 100 and group["current_status"] == "partial"
        data["groups"]["group"][1] = second | {
            "remaining_fen": None,
            "settlement_status": "unestablished",
        }
        group = _group(snap, data, "group")
        assert group["outstanding_fen"] is None and group["status"] == "unestablished"


@pytest.mark.parametrize(
    "values,expected,status",
    [
        (
            [(100, 200, 0, -100, "over_settled"), (100, 0, 0, 100, "open")],
            (200, 200, 0, 0),
            "over_settled",
        ),
        (
            [
                (None, 0, 0, None, "unestablished"),
                (2**63 - 1, 0, 0, None, "unestablished"),
                (1, 0, 0, None, "unestablished"),
            ],
            (None, 0, 0, None),
            "unestablished",
        ),
        ([(2**63 - 1, 0, 0, 1, "open"), (1, 0, 0, 1, "open")], None, "open"),
        ([(0, 2**63 - 1, 0, None, "unknown"), (0, 1, 0, None, "unknown")], None, "unestablished"),
    ],
)
def test_record_group_checked_none_and_abnormal_status(values, expected, status):
    fields = (
        "source_amount_fen",
        "paid_fen",
        "other_settled_fen",
        "remaining_fen",
        "settlement_status",
    )
    rows = [
        {
            "key": str(index),
            "category_key": "supplier_payables",
            **dict(zip(fields, value, strict=True)),
        }
        for index, value in enumerate(values)
    ]
    data = {
        "groups": {"group": rows},
        "identities": {"0": {"party_id": None, "missing_party": "未提供", "description": "费用"}},
        "current_by_key": {row["key"]: row for row in rows},
    }
    snap = SimpleNamespace(party_field=lambda *args, **kwargs: {"party": {"name": "未提供"}})
    if expected is None:
        with pytest.raises(ValueError, match="signed 64-bit integer"):
            _group(snap, data, "group")
    else:
        row = _group(snap, data, "group")
        assert tuple(row[field] for field in (*fields[:3], "outstanding_fen")) == expected
        assert row["current_outstanding_fen"] == expected[-1]
        assert row["status"] == row["current_status"] == status


def test_closed_all_object_and_member_pages_keep_complete_counts(book):
    from test_dashboard_open_identity_tokens import _read_all

    engine, _, _, proof = book
    total = sum(
        expenses(book, 1, party=f"supplier-{index}", prefix=f"group-{index}") for index in range(26)
    )
    assert (
        close_month(inventories(engine, proof, "2026-09", {"transactions"}), proof, "2026-09")[
            "status"
        ]
        == "closed"
    )
    pages, members = _read_all(engine)
    rows = [row for page in pages for row in page["collection"]["items"]]
    assert len(rows) == len({row["id"] for row in rows}) == 26
    assert all(
        page["payable_fen"] == total and page["group_count"] == page["total_count"] == 26
        for page in pages
    )
    assert all(page["current_outstanding"]["payable_fen"] == total for page in pages)
    assert sum(row["outstanding_fen"] for row in rows) == total
    selected = [row for parts in members.values() for page in parts for row in page["items"]]
    assert len(selected) == len({row["id"] for row in selected}) == 26
    assert {row["subject_id"] for row in selected} == {f"group-{index}-000" for index in range(26)}
    assert all(row["outstanding_fen"] == row["source_amount_fen"] == 100 for row in selected)


def test_failed_record_group_does_not_publish_partial_snapshot_success(book, monkeypatch):
    engine, _, _, proof = book
    expenses(book, 3)
    close_month(inventories(engine, proof, "2026-09", {"transactions"}), proof, "2026-09")
    original = group_reader._record_data

    def fail_after_proof(snap):
        result = original(snap)
        assert len(result["historical"]["obligations"]) == 3
        raise ValueError("failed complete group reduction")

    with snapshot(engine, "2026-09") as snap:
        with monkeypatch.context() as failed:
            failed.setattr(group_reader, "_record_data", fail_after_proof)
            with pytest.raises(ValueError, match="failed complete group reduction"):
                open_group_page(snap)
            assert not hasattr(snap, "_open_group_data")
        assert open_group_page(snap)["total_count"] == 3
