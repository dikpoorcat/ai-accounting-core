"""Bounded report reads retain historical money and one-transaction source views."""

import sqlite3

import pytest
from test_integrity_content import damage
from test_new_company_reports import profile as opening_profile
from test_new_company_reports import zero_tax
from test_opening_continuation import book as _opening_book
from test_reports import book as book  # noqa: F401
from test_reports import cit, close_quarter, profile, scenario

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.reports import (
    Reports,
    _report_tax_and_carry_refs,
    _statements,
    _unfrozen_voucher_ids,
    check_report_readiness,
)
from ai_accounting.kernel.types import YearMonth

opening_book = _opening_book


def test_unfrozen_voucher_candidates_seek_only_selected_months_and_keep_old_accounts():
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(
            "CREATE TABLE voucher_version(id TEXT PRIMARY KEY,voucher_id TEXT NOT NULL,"
            "period INTEGER NOT NULL);"
            "CREATE INDEX voucher_period ON voucher_version(period,voucher_id,id);"
            "CREATE TABLE voucher_line(version_id TEXT NOT NULL,account TEXT NOT NULL,"
            "line_no INTEGER NOT NULL);"
            "CREATE INDEX voucher_line_account ON voucher_line(account,version_id,line_no);"
        )
        connection.executemany(
            "INSERT INTO voucher_version VALUES(?,?,?)",
            [
                (f"frozen-{month}-{index}", f"voucher-{month}-{index}", month)
                for month in range(1, 12)
                for index in range(100)
            ]
            + [(f"open-{index}", f"open-voucher-{index}", 12) for index in range(100)]
            + [
                ("old-account", "old-account-voucher", 0),
                ("old-unrelated", "old-other-voucher", 0),
            ],
        )
        connection.executemany(
            "INSERT INTO voucher_line VALUES(?,?,?)",
            [("old-account", "2202", 1), ("old-unrelated", "1001", 1), ("frozen-1-0", "2202", 1)],
        )
        frozen = set(range(1, 12))

        def selected():
            steps = 0

            def tick():
                nonlocal steps
                steps += 100
                return 0

            connection.set_progress_handler(tick, 100)
            try:
                rows = _unfrozen_voucher_ids(connection, 1, 12, frozen)
            finally:
                connection.set_progress_handler(None, 0)
            return rows, steps

        expected = {f"open-{index}" for index in range(100)}
        before, before_steps = selected()
        assert before == expected
        connection.executemany(
            "INSERT INTO voucher_version VALUES(?,?,?)",
            [
                (f"later-frozen-{index}", f"later-frozen-voucher-{index}", 1)
                for index in range(1000)
            ],
        )
        after, after_steps = selected()
        assert after == expected
        assert after_steps < before_steps + 1000, (before_steps, after_steps)
        assert _unfrozen_voucher_ids(
            connection, 1, 12, frozen, historical_accounts={"2202"}
        ) == expected | {"old-account"}
        assert _unfrozen_voucher_ids(connection, 1, 12, frozen | {12}) == set()
        assert _unfrozen_voucher_ids(
            connection, 1, 12, frozen | {12}, historical_accounts={"2202"}
        ) == {"old-account"}
    finally:
        connection.close()


def test_rare_report_metadata_selection_keeps_exact_adoption_and_bounded_work(book):
    reports = scenario(book)
    engine, save, publish, _ = book
    with engine.store.connection(read_only=True) as connection:
        profile_id, tax_id = (
            connection.execute(
                "SELECT a.fact_id FROM fact_current a WHERE a.subject_id=?", (subject,)
            ).fetchone()[0]
            for subject in ("profile", "cit-2026-03")
        )
    profile(save, publish, period="2026-02", name="未采用的新档案", subject="unadopted-profile")
    with engine.store.connection(read_only=True) as connection:
        later_id = connection.execute(
            "SELECT fact_id FROM fact_current WHERE subject_id='unadopted-profile'"
        ).fetchone()[0]

    def selected(refs):
        with QueryReads.snapshot(engine) as reads:
            steps = 0

            def tick():
                nonlocal steps
                steps += 100
                return 0

            reads.connection.set_progress_handler(tick, 100)
            try:
                profiles = reports._report_profiles(
                    reads.connection, refs, YearMonth("2026-03"), reads
                )
                tax = _report_tax_and_carry_refs(
                    reads.connection,
                    refs,
                    YearMonth("2026-01").ordinal,
                    YearMonth("2026-03").ordinal,
                    YearMonth("2026-01").ordinal,
                    omit_carry=False,
                )
            finally:
                reads.connection.set_progress_handler(None, 0)
            return {item.id for item in profiles}, tax, steps

    exact = {profile_id, tax_id}
    assert selected(exact)[:2] == ({profile_id}, {tax_id})
    wider = exact | {f"unrelated-fact-{index}" for index in range(2000)}
    base = selected(exact)
    expanded = selected(wider)
    assert expanded[:2] == base[:2]
    assert expanded[2] < base[2] + 3000, (base[2], expanded[2])
    assert selected(wider | {later_id})[0] == {later_id}


@pytest.mark.parametrize(
    "subject,table,sql",
    [
        (
            "profile",
            "fact_report_profile",
            "UPDATE fact_report_profile SET company_name='损坏' WHERE revision_id=?",
        ),
        (
            "cit-2026-03",
            "fact_report_income_tax_confirmation",
            "UPDATE fact_report_income_tax_confirmation "
            "SET explanation=explanation||'损坏' WHERE revision_id=?",
        ),
    ],
)
def test_report_rare_selected_fact_body_damage_still_rejects(book, subject, table, sql):
    reports = scenario(book)
    engine = book[0]
    with engine.store.connection(read_only=True) as connection:
        fact_id = connection.execute(
            "SELECT fact_id FROM fact_current WHERE subject_id=?", (subject,)
        ).fetchone()[0]
    damage(engine, table, sql, (fact_id,))
    with pytest.raises(KernelError, match="完整|摘要|一致|损坏"):
        reports.report(2026, 1)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with pytest.raises(KernelError, match="完整|摘要|一致|损坏"):
            check_report_readiness(engine.store, connection, YearMonth("2026-03"))


def test_quarter_report_rejects_damage_without_expanding_context(book):
    reports = scenario(book)
    close_quarter(book)
    engine = book[0]
    assert reports.closed_period_coverage(2026, 1)["complete"]
    Dashboard(engine).context()
    with engine.store.connection(read_only=True) as connection:
        fact_id = connection.execute(
            "SELECT fact_id FROM fact_current WHERE subject_id='profile'"
        ).fetchone()[0]
    damage(
        engine,
        "fact_report_profile",
        "UPDATE fact_report_profile SET bookkeeping_start=? WHERE revision_id=?",
        (YearMonth("2025-12").ordinal, fact_id),
    )
    with pytest.raises(KernelError, match="完整|摘要|一致|损坏"):
        reports.closed_period_coverage(2026, 1)
    # Context selects a quarter, but does not consume its report profile body.
    assert Dashboard(engine).context()["quarters"]
    with pytest.raises(KernelError, match="完整|摘要|一致|损坏"):
        Dashboard(engine).quarterly_report(2026, 1)


def test_readiness_without_profile_still_verifies_adopted_tax_body(book):
    engine, save, publish, _ = book
    cit(save, publish)
    with engine.store.connection(read_only=True) as connection:
        fact_id = connection.execute(
            "SELECT fact_id FROM fact_current WHERE subject_id='cit-2026-03'"
        ).fetchone()[0]
        connection.execute("BEGIN")
        assert check_report_readiness(engine.store, connection, YearMonth("2026-03")) == []
    damage(
        engine,
        "fact_report_income_tax_confirmation",
        "UPDATE fact_report_income_tax_confirmation "
        "SET explanation=explanation||'损坏' WHERE revision_id=?",
        (fact_id,),
    )
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with pytest.raises(KernelError, match="完整|摘要|一致|损坏"):
            check_report_readiness(engine.store, connection, YearMonth("2026-03"))


def test_classification_of_replaced_version_keeps_original_reference_only_validation(book):
    reports = scenario(book)
    engine, save, publish, _ = book
    with engine.store.connection(read_only=True) as connection:
        original_id = connection.execute(
            "SELECT v.id FROM voucher_version v JOIN calculation c ON c.id=v.calculation_id "
            "WHERE c.subject_id='capital'"
        ).fetchone()[0]
        original_fact = engine.store.current_fact(connection, "capital")
    engine.amend_fact(
        "cash_funding",
        "capital",
        {
            "period": "2026-01",
            "actual_date": "2026-01-03",
            "owner_id": "owner",
            "funding_kind": "capital",
            "amount_fen": 60000,
            "cash_account_id": "cash",
        },
        evidence=original_fact.evidence,
        expected_revision=1,
        recording_error_confirmed=True,
        request_id="correct-capital",
    )
    publish("capital")
    fact_id = save(
        "report_classification",
        "classification-of-replaced-version",
        {
            "period": "2026-01",
            "voucher_version_id": original_id,
            "profit_details": [
                {"line_no": 99, "detail_code": "management_other", "amount_fen": 100}
            ],
        },
    )["fact_id"]
    result = reports.report(2026, 1)
    assert result["status"] == "ready", result["fact_issues"]
    assert fact_id in result["report_fact_ids"]


@pytest.mark.parametrize("problem", ["missing", "future_period", "conflicting"])
def test_unselected_classification_references_remain_validated(book, monkeypatch, problem):
    reports = scenario(book)
    engine, save, publish, _ = book
    voucher_id = "v-does-not-exist"
    if problem == "future_period":
        save(
            "cash_funding",
            "future-capital",
            {
                "period": "2026-04",
                "actual_date": "2026-04-03",
                "owner_id": "owner",
                "funding_kind": "capital",
                "amount_fen": 100,
                "cash_account_id": "cash",
            },
        )
        publish("future-capital")
        with engine.store.connection(read_only=True) as connection:
            voucher_id = connection.execute(
                "SELECT v.id FROM voucher_version v JOIN calculation c ON c.id=v.calculation_id "
                "WHERE c.subject_id='future-capital'"
            ).fetchone()[0]
    reference_ids = {
        save(
            "report_classification",
            f"bad-reference-{index}",
            {
                "period": "2026-03",
                "voucher_version_id": voucher_id,
                "profit_details": [
                    {"line_no": 1, "detail_code": "management_other", "amount_fen": 100}
                ],
            },
        )["fact_id"]
        for index in range(2 if problem == "conflicting" else 1)
    }
    decoded = set()
    original = QueryReads.fact_versions

    def capture(self, identifiers):
        identifiers = set(identifiers)
        decoded.update(identifiers)
        return original(self, identifiers)

    monkeypatch.setattr(QueryReads, "fact_versions", capture)
    result = reports.report(2026, 1)
    expected_field = (
        "report_classification"
        if problem == "conflicting"
        else "report_classification.voucher_version_id"
    )
    assert result["status"] == "needs_information"
    assert any(
        item["field"] == expected_field and item.get("voucher_version_id") == voucher_id
        for item in result["fact_issues"]
    )
    assert reference_ids <= set(result["report_fact_ids"])
    assert not reference_ids & decoded
    if problem == "missing":
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            problems = check_report_readiness(engine.store, connection, YearMonth("2026-03"))
        assert any(item["field"] == expected_field for item in problems)
        assert not reference_ids & decoded


@pytest.mark.parametrize("problem", [None, "period", "line_no", "account", "conflicting"])
def test_previous_year_classification_validation_uses_scalar_rows(book, monkeypatch, problem):
    engine, save, publish, _ = book
    save(
        "report_profile",
        "profile",
        {
            "period": "2025-01",
            "company_name": "合成公司",
            "accounting_standard": "small_enterprise",
            "bookkeeping_start": "2025-01",
            "newly_established_zero_opening_confirmed": True,
        },
    )
    save(
        "cash_funding",
        "old-capital",
        {
            "period": "2025-01",
            "actual_date": "2025-01-03",
            "owner_id": "owner",
            "funding_kind": "capital",
            "amount_fen": 1000,
            "cash_account_id": "cash",
        },
    )
    publish("old-capital")
    cit(save, publish)
    with engine.store.connection(read_only=True) as connection:
        voucher_id, calculation_id = connection.execute(
            "SELECT v.id,v.calculation_id FROM voucher_version v "
            "JOIN calculation c ON c.id=v.calculation_id WHERE c.subject_id='old-capital'"
        ).fetchone()
    data = {
        "period": "2025-02" if problem == "period" else "2025-01",
        "voucher_version_id": voucher_id,
        "cash_details": [
            {"line_no": 99 if problem == "line_no" else 1, "category": 15, "amount_fen": 1000}
        ],
    }
    if problem == "account":
        data.pop("cash_details")
        data["profit_details"] = [
            {"line_no": 1, "detail_code": "management_other", "amount_fen": 1000}
        ]
    reference_ids = {
        save("report_classification", f"old-classification-{index}", data)["fact_id"]
        for index in range(2 if problem == "conflicting" else 1)
    }
    decoded_facts, decoded_calculations = set(), set()
    original_facts, original_calculations = QueryReads.fact_versions, QueryReads.calculations

    def capture_facts(self, identifiers):
        identifiers = set(identifiers)
        decoded_facts.update(identifiers)
        return original_facts(self, identifiers)

    def capture_calculations(self, identifiers):
        identifiers = set(identifiers)
        decoded_calculations.update(identifiers)
        return original_calculations(self, identifiers)

    monkeypatch.setattr(QueryReads, "fact_versions", capture_facts)
    monkeypatch.setattr(QueryReads, "calculations", capture_calculations)
    result = Reports(engine).report(2026, 1)
    if problem is None:
        assert result["status"] == "ready", result["fact_issues"]
    else:
        expected_field = {
            "period": "report_classification.period",
            "line_no": "report_classification.line_no",
            "account": "report_classification.line_no",
            "conflicting": "report_classification",
        }[problem]
        assert result["status"] == "needs_information"
        assert any(
            item["field"] == expected_field and item.get("voucher_version_id") == voucher_id
            for item in result["fact_issues"]
        )
    assert reference_ids <= set(result["report_fact_ids"])
    assert not reference_ids & decoded_facts
    assert calculation_id not in decoded_calculations


def test_previous_year_direct_accounts_do_not_decode_unrelated_outcomes(book, monkeypatch):
    engine, save, publish, _ = book
    save(
        "report_profile",
        "profile",
        {
            "period": "2025-01",
            "company_name": "合成公司",
            "accounting_standard": "small_enterprise",
            "bookkeeping_start": "2025-01",
            "newly_established_zero_opening_confirmed": True,
        },
    )
    for subject, period, amount in (
        ("old-capital", "2025-01", 1000),
        ("new-capital", "2026-01", 700),
    ):
        save(
            "cash_funding",
            subject,
            {
                "period": period,
                "actual_date": period + "-03",
                "owner_id": "owner",
                "funding_kind": "capital",
                "amount_fen": amount,
                "cash_account_id": "cash",
            },
        )
        publish(subject)
    cit(save, publish)
    with engine.store.connection(read_only=True) as connection:
        old_id = connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id='old-capital'"
        ).fetchone()[0]
    decoded = set()
    original = QueryReads.calculations

    def capture(self, identifiers):
        identifiers = set(identifiers)
        decoded.update(identifiers)
        return original(self, identifiers)

    monkeypatch.setattr(QueryReads, "calculations", capture)
    result = Reports(engine).report(2026, 1)
    assert result["status"] == "ready", result["fact_issues"]
    assert old_id not in decoded
    assert result["statements"]["balance_sheet"]["1"] == {
        "name": result["statements"]["balance_sheet"]["1"]["name"],
        "beginning_fen": 1000,
        "ending_fen": 1700,
    }
    cash = result["statements"]["cash_flow_statement"]
    assert cash["15"]["current_fen"] == 700
    assert cash["21"]["current_fen"] == 1000
    assert cash["22"]["current_fen"] == 1700


def test_context_coverage_uses_report_close_rules_without_building_statements(book, monkeypatch):
    reports = scenario(book)
    close_quarter(book)
    expected = reports.report(2026, 1, source="closed")

    def no_statements(*args, **kwargs):
        raise AssertionError("context must not build quarterly statements")

    monkeypatch.setattr(reports, "_report", no_statements)
    assert reports.closed_period_coverage(2026, 1)["complete"] is True
    assert not any(
        item["field"] in {"period", "closed_periods"} for item in expected["fact_issues"]
    )
    following = reports.closed_period_coverage(2026, 2)
    assert following["complete"] is False
    assert {item["field"] for item in following["fact_issues"]} == {"period", "closed_periods"}


def test_report_and_browser_sources_share_the_callers_snapshot(book):
    reports = scenario(book)
    engine, save, publish, _ = book
    with QueryReads.snapshot(engine) as reads:
        connection = reads.connection
        before = reports._report(2026, 1, source="open", connection=connection, reads=reads)
        profile(save, publish, period="2026-02", name="后来名称", subject="later-profile")
        repeated = reports._report(2026, 1, source="open", connection=connection, reads=reads)
        assert repeated["digest"] == before["digest"]
        assert repeated["epochs"] == before["epochs"]
        assert repeated["report_fact_ids"] == before["report_fact_ids"]
    assert reports.report(2026, 1)["organization"]["name"] == "后来名称"


def test_unknown_party_zero_net_is_not_replaced_by_account_totals():
    rows = [
        {
            "account": "2202",
            "amount": amount,
            "period": 10,
            "version_id": f"unknown-{amount}",
            "line_no": 1,
        }
        for amount in (100, -100)
    ]
    problems = []
    statements = _statements(
        rows, 20, 20, 22, problems, account_balances={19: {"2202": 0}, 22: {"2202": 0}}
    )
    assert statements["balance_sheet"]["33"]["ending_fen"] is None
    assert statements["balance_sheet"]["30"]["ending_fen"] is None
    assert {item["version_id"] for item in problems} == {"unknown-100", "unknown--100"}


def test_unproven_opening_contract_does_not_fall_back_to_current_zero(opening_book, monkeypatch):
    from ai_accounting.kernel.business_queries import BusinessQueries

    engine, save, _, package, _ = opening_book
    package([], period="2026-01")
    opening_profile(save, "2026-01")
    zero_tax(save, 2026, 1)
    with engine.store.connection(read_only=True) as connection:
        current = QueryReads(engine, connection).calculation(
            connection.execute(
                "SELECT calculation_id FROM calculation_current WHERE subject_id='opening'"
            ).fetchone()[0]
        )
    candidate = {
        "calculation_id": current["id"],
        "fact_id": current["fact_id"],
        "kind": "opening_package",
        "result_digest": current["result_digest"],
        "has_journal_lines": False,
        "trace_only": True,
    }
    # Exercise Reports' use of T3 uncertainty independently of T3's graph tests.
    monkeypatch.setattr(
        BusinessQueries,
        "_selected_accounting",
        lambda *args, **kwargs: {
            "through_period": {
                "state_results": [],
                "unestablished_state_selections": [
                    {
                        "reason": "state_selection_unavailable",
                        "candidates": [candidate],
                    }
                ],
            },
        },
    )
    result = Reports(engine).report(2026, 1)
    assert result["opening_source"] is None
    assert result["status"] == "needs_information"
    assert result["statements"]["balance_sheet"]["1"]["beginning_fen"] is None
    assert result["statements"]["cash_flow_statement"]["21"]["current_fen"] is None
    problem = next(
        item for item in result["fact_issues"] if item["field"] == "opening_package.selection"
    )
    assert problem["candidates"] == [candidate]
    assert problem["trace_targets"] == [{"calculation_id": current["id"]}]
