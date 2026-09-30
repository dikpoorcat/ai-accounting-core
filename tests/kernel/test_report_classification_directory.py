"""Frozen classification keys prove old membership without scanning all headers."""

import json
import random
import sqlite3
import sys

import pytest
from test_integrity_content import damage
from test_reports import book as book  # noqa: F401
from test_reports import close_quarter, scenario

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import (
    _missing_adopted_report_classification_ids,
)
from ai_accounting.kernel.key_membership_filter import decode_keys_filter, may_contain
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.report_classification_directory import (
    _exceptional_entries,
    _insert,
    _lookup_many,
    _Nodes,
    _reachable_new_nodes,
    _root_content,
    classification_directory_scope,
)
from ai_accounting.kernel.types import YearMonth


def test_position_missing_typed_work_scales_with_classification_heads_not_other_kinds():
    from ai_accounting.kernel.position_v1 import _position_classification_ids

    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE subject(id TEXT PRIMARY KEY,kind TEXT)")
    connection.execute("CREATE INDEX subject_kind ON subject(kind,id)")
    connection.execute(
        "CREATE TABLE fact_revision(id TEXT PRIMARY KEY,subject_id TEXT,period INTEGER)"
    )
    connection.execute("CREATE INDEX fact_subject ON fact_revision(subject_id,id)")
    connection.execute(
        "CREATE TABLE fact_report_classification("
        "revision_id TEXT PRIMARY KEY,voucher_version_id TEXT)"
    )
    connection.execute(
        "CREATE TABLE fact_report_classification_counterparties(revision_id TEXT)"
    )
    connection.execute("CREATE TABLE fact_current(fact_id TEXT)")
    connection.execute("CREATE TABLE period_close(period INTEGER PRIMARY KEY)")
    connection.execute(
        "CREATE TABLE close_reference("
        "reference_type TEXT,reference_id TEXT,path TEXT,close_period INTEGER)"
    )
    connection.execute(
        "CREATE INDEX close_reference_lookup ON "
        "close_reference(reference_type,reference_id,close_period)"
    )
    connection.executemany(
        "INSERT INTO subject VALUES(?,?)",
        (("classified", "report_classification"), ("other", "expense")),
    )

    def measured(operation):
        vm = 0

        def count():
            nonlocal vm
            vm += 100
            return 0

        connection.set_progress_handler(count, 100)
        try:
            result = operation()
        finally:
            connection.set_progress_handler(None, 0)
        return result, vm

    months = {}
    for month in range(120):
        connection.executemany(
            "INSERT INTO fact_revision VALUES(?,?,?)",
            ((f"classification-{month}-{n}", "classified", month) for n in range(20)),
        )
        connection.executemany(
            "INSERT INTO fact_report_classification VALUES(?,?)",
            ((f"classification-{month}-{n}", f"voucher-{month}-{n}") for n in range(1, 20)),
        )
        connection.executemany(
            "INSERT INTO close_reference VALUES('fact',?,?,?)",
            ((f"classification-{month}-{n}",
              "readiness.financial_reports.facts[*]", month) for n in range(20)),
        )
        connection.executemany(
            "INSERT INTO fact_revision VALUES(?,?,?)",
            ((f"other-{month}-{n}", "other", month) for n in range(50)),
        )
        connection.execute("INSERT INTO period_close VALUES(?)", (month,))
        if month + 1 in (12, 48, 120):
            expected = {f"classification-{n}-0" for n in range(month + 1)}
            current, current_vm = measured(
                lambda month=month: _missing_adopted_report_classification_ids(connection, month)
            )
            historical, historical_vm = measured(
                lambda month=month: _position_classification_ids(connection, month)
            )
            assert current == expected
            assert historical == expected
            months[month + 1] = (current_vm, historical_vm)
    assert months[120][0] < 10 * months[12][0] + 2000
    assert months[120][1] < 10 * months[12][1] + 2000
    connection.close()


def test_hex_digest_fast_check_keeps_exact_ascii_lowercase_acceptance():
    import ai_accounting.kernel.report_classification_directory as current
    import ai_accounting.kernel.report_classification_directory_v1 as released_v1

    class DigestText(str):
        pass

    values = [
        None, b"a" * 64, DigestText("a" * 64), "a" * 63, "a" * 65,
        "A" * 64, "０" * 64, "a" * 63 + "\n", "a" * 63 + "\x00",
        "a" * 63 + "\ud800", "0123456789abcdef" * 4,
    ]
    generator = random.Random(17)
    alphabet = "0123456789abcdefABCDEFgＧ\n\x00\ud800"
    values.extend(
        "".join(generator.choices(alphabet, k=generator.randrange(60, 69)))
        for _ in range(5000)
    )
    for value in values:
        expected = type(value) is str and len(value) == 64 and all(
            char in "0123456789abcdef" for char in value
        )
        assert current._hex_digest(value) is expected
        assert released_v1._hex_digest(value) is expected


def test_close_binds_complete_classification_key_and_reuses_unchanged_tree(book):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    with QueryReads.snapshot(engine) as reads:
        connection = reads.connection
        row = connection.execute(
            "SELECT c.revision_id,c.voucher_version_id,f.digest "
            "FROM fact_report_classification c JOIN fact_revision f ON f.id=c.revision_id"
        ).fetchone()
        scope = classification_directory_scope(
            connection,
            YearMonth("2026-03").ordinal,
            {row["voucher_version_id"], "missing-voucher"},
            reads=reads,
        )
        assert scope["membership"][row["voucher_version_id"]] == (
            (row["revision_id"], row["digest"].hex(), YearMonth("2026-02").ordinal, True),
        )
        assert scope["membership"]["missing-voucher"] == ()
        assert not scope["conflicts"] and not scope["unsafe"]
        feb, mar = connection.execute(
            "SELECT a.content,b.content FROM report_classification_directory a "
            "JOIN report_classification_directory b ON b.period=? WHERE a.period=?",
            (YearMonth("2026-03").ordinal, YearMonth("2026-02").ordinal),
        ).fetchone()
        assert json.loads(feb)["tree"] == json.loads(mar)["tree"]


def test_bound_filter_excludes_negative_keys_but_preserves_exact_hits(book, monkeypatch):
    import ai_accounting.kernel.report_classification_directory as directory

    engine = book[0]
    scenario(book)
    close_quarter(book)
    with QueryReads.snapshot(engine) as reads:
        connection = reads.connection
        row = connection.execute(
            "SELECT voucher_version_id FROM fact_report_classification"
        ).fetchone()
        actual_key = row[0]
        frozen = connection.execute(
            "SELECT content FROM report_classification_directory "
            "ORDER BY period DESC LIMIT 1"
        ).fetchone()
        root = json.loads(frozen[0])
        assert root["format"] == 2
        key_filter = decode_keys_filter(root["key_filter"])
        assert key_filter.key_count == root["keys"]
        assert may_contain(key_filter, actual_key)
        absent = [f"never-adopted-{number}" for number in range(100)]
        negatives = {key for key in absent if not may_contain(key_filter, key)}
        assert negatives
        seen = []
        original = directory._lookup_many

        def recorded(nodes, tree, keys, period):
            seen.extend(keys)
            return original(nodes, tree, keys, period)

        monkeypatch.setattr(directory, "_lookup_many", recorded)
        scope = classification_directory_scope(
            connection, YearMonth("2026-03").ordinal, {*absent, actual_key}, reads=reads
        )
        assert scope["membership"][actual_key]
        assert all(scope["membership"][key] == () for key in absent)
        assert actual_key in seen
        assert not negatives.intersection(seen)


def test_damaged_bound_filter_rejects_page_and_full_check(book):
    engine = book[0]
    report = scenario(book)
    close_quarter(book)
    damage(
        engine,
        "report_classification_directory",
        "UPDATE report_classification_directory "
        "SET content=json_set(content,'$.key_filter.bits_base64','%%%') "
        "WHERE period=(SELECT MAX(period) FROM report_classification_directory)",
    )
    with pytest.raises(KernelError) as page_error:
        report.report(2026, 1)
    assert page_error.value.code == "content_integrity_failed"
    with pytest.raises(KernelError) as verify_error:
        Maintenance(engine).verify_integrity()
    assert verify_error.value.code == "content_integrity_failed"


@pytest.mark.parametrize(
    ("year", "quarter", "source"),
    [(2026, 1, "open"), (2026, 1, "closed"), (2027, 1, "open")],
)
def test_rooted_and_full_classification_paths_have_identical_report_payload(
    book, monkeypatch, year, quarter, source
):
    import ai_accounting.kernel.reports as reports_module

    engine = book[0]
    report = scenario(book)
    close_quarter(book)
    with QueryReads.snapshot(engine) as reads:
        rooted = report._report(
            year, quarter, source=source, connection=reads.connection, reads=reads
        )
    original = reports_module._report_classifications

    def full(*args, **kwargs):
        return original(*args, **(kwargs | {"rooted": False}))

    monkeypatch.setattr(reports_module, "_report_classifications", full)
    with QueryReads.snapshot(engine) as reads:
        baseline = report._report(
            year, quarter, source=source, connection=reads.connection, reads=reads
        )
    assert rooted == baseline


@pytest.mark.parametrize("additional_businesses", [0, 32])
def test_full_report_loads_fallback_references_only_when_directory_cannot_answer(
    book, monkeypatch, additional_businesses
):
    """Measure actual SQL work, retaining the complete public source list."""
    from test_reports import classify

    import ai_accounting.kernel.reports as reports_module

    engine, save, publish, _ = book
    report = scenario(book)
    subjects = []
    for number in range(additional_businesses):
        subject = f"additional-cost-{number}"
        save("expense", subject, {
            "period": "2026-02", "counterparty_id": "supplier",
            "amount_fen": 101 + number, "expense_class": "administration",
            "creditor_kind": "supplier",
        })
        subjects.append(subject)
    if subjects:
        publish(*subjects)
    for number, subject in enumerate(subjects):
        classify(engine, save, publish, subject=subject, amount=101 + number)
    close_quarter(book)

    def measured_report():
        work = {"fallback_vm_steps": 0}
        active = False

        def profile_call(frame, event, _arg):
            nonlocal active
            if frame.f_code.co_name == "fallback_classification_references":
                if event == "call":
                    active = True
                elif event == "return":
                    active = False

        def tick():
            if active:
                work["fallback_vm_steps"] += 1
            return 0

        with QueryReads.snapshot(engine) as reads:
            reads.connection.set_progress_handler(tick, 1)
            previous = sys.getprofile()
            sys.setprofile(profile_call)
            try:
                result = report._report(
                    2026, 1, source="open", connection=reads.connection, reads=reads
                )
            finally:
                sys.setprofile(previous)
                reads.connection.set_progress_handler(None, 0)
        return result, work

    rooted, rooted_work = measured_report()
    monkeypatch.setattr(reports_module, "_rooted_classification_headers", lambda *_a, **_k: None)
    fallback, fallback_work = measured_report()
    assert rooted == fallback
    assert rooted["status"] == "ready", rooted["fact_issues"]
    with engine.store.connection(read_only=True) as connection:
        expected_sources = {
            row[0] for row in connection.execute(
                "SELECT a.fact_id FROM fact_current a JOIN subject s ON s.id=a.subject_id "
                "WHERE s.kind IN ('report_profile','report_classification',"
                "'report_income_tax_confirmation')"
            )
        }
    assert len(expected_sources) == additional_businesses + 3
    assert set(rooted["report_fact_ids"]) == expected_sources
    assert rooted_work["fallback_vm_steps"] == 0
    assert fallback_work["fallback_vm_steps"] > 0

    if additional_businesses == 0:
        # These same-kind facts are in a later open month and cannot enter the
        # Q1 source set. They must not turn an exact reference batch into a scan
        # of all report-classification subjects and revisions.
        with engine.store.connection(read_only=True) as connection:
            voucher_id = connection.execute(
                "SELECT voucher_version_id FROM fact_report_classification LIMIT 1"
            ).fetchone()[0]
        for number in range(48):
            save(
                "report_classification",
                f"future-classification-{number}",
                {
                    "period": "2026-04",
                    "voucher_version_id": voucher_id,
                    "profit_details": [
                        {
                            "line_no": 1,
                            "detail_code": "management_entertainment",
                            "amount_fen": 10000,
                        }
                    ],
                },
            )
        unchanged, later_work = measured_report()
        assert unchanged | {"epochs": fallback["epochs"]} == fallback
        assert later_work["fallback_vm_steps"] <= fallback_work["fallback_vm_steps"] + 300


def test_missing_frozen_node_rejects_page_and_full_check_then_repairs(book):
    engine = book[0]
    report = scenario(book)
    close_quarter(book)
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM report_classification_node")
        connection.commit()
    with pytest.raises(KernelError) as caught:
        report.report(2026, 1)
    assert caught.value.code == "content_integrity_failed"
    with pytest.raises(KernelError):
        Maintenance(engine).verify_integrity()
    assert Maintenance(engine).rebuild_projections(
        request_id="repair-classification-directory"
    )["changed"]
    assert Maintenance(engine).verify_integrity()["status"] == "verified"
    assert report.report(2026, 1)["statements"]


def test_new_open_classification_conflicts_with_exact_frozen_voucher(book):
    engine, save, _, _ = book
    report = scenario(book)
    close_quarter(book)
    with engine.store.connection(read_only=True) as connection:
        voucher = connection.execute(
            "SELECT voucher_version_id FROM fact_report_classification"
        ).fetchone()[0]
    save(
        "report_classification",
        "later-conflict",
        {
            "period": "2026-04",
            "voucher_version_id": voucher,
            "profit_details": [
                {
                    "line_no": 1,
                    "detail_code": "management_entertainment",
                    "amount_fen": 10000,
                }
            ],
        },
    )
    problems = report.report(2026, 2)["fact_issues"]
    assert any(
        item["field"] == "report_classification"
        and item["voucher_version_id"] == voucher
        for item in problems
    )


def test_rooted_multiple_conflicts_keep_full_header_issue_order(book):
    from ai_accounting.kernel.reports import (
        _rooted_classification_headers,
        _validated_report_classification_headers,
    )

    engine, save, _, _ = book
    scenario(book)
    close_quarter(book)
    with engine.store.connection(read_only=True) as connection:
        old_key = connection.execute(
            "SELECT voucher_version_id FROM fact_report_classification"
        ).fetchone()[0]
        other_key = connection.execute(
            "SELECT c.version_id FROM calculation_publication p "
            "JOIN voucher_current c ON c.voucher_id=p.voucher_id "
            "WHERE p.subject_id='capital'"
        ).fetchone()[0]
    for index, key in enumerate((other_key, old_key, other_key)):
        save(
            "report_classification",
            f"april-conflict-{index}",
            {
                "period": "2026-04",
                "voucher_version_id": key,
                "profit_details": [
                    {
                        "line_no": 1,
                        "detail_code": "management_entertainment",
                        "amount_fen": 10000,
                    }
                ],
            },
        )
    with QueryReads.snapshot(engine) as reads:
        connection = reads.connection
        references = {
            row[0]
            for row in connection.execute("SELECT revision_id FROM fact_report_classification")
        }
        expected_problems = []
        expected_headers = _validated_report_classification_headers(
            connection,
            reads,
            references,
            YearMonth("2026-04"),
            "open",
            expected_problems,
            source_vouchers={old_key, other_key},
        )
        actual_problems = []
        actual_headers = _rooted_classification_headers(
            connection,
            reads,
            {old_key, other_key},
            YearMonth("2026-04"),
            "open",
            actual_problems,
        )
    assert actual_problems == expected_problems
    assert actual_headers == expected_headers
    assert sum(item["field"] == "report_classification" for item in actual_problems) == 2


def test_same_year_decoded_classification_checks_saved_body(book, monkeypatch):
    import ai_accounting.kernel.report_flow as flow_module

    engine = book[0]
    report = scenario(book)
    close_quarter(book)
    damage(
        engine,
        "fact_report_classification_profit_details",
        "UPDATE fact_report_classification_profit_details "
        "SET detail_code='management_other' "
        "WHERE detail_code='management_entertainment'",
    )
    # A changed selected flow takes the ordinary exact-line path, which reads
    # and uses the classification fact itself.
    monkeypatch.setattr(flow_module, "read_report_flow", lambda *_args, **_kwargs: None)
    with pytest.raises(KernelError) as caught:
        report.report(2026, 1)
    assert caught.value.code == "content_integrity_failed"


def test_unread_old_body_damage_keeps_rooted_flow_but_full_integrity_rejects(book):
    engine = book[0]
    report = scenario(book)
    close_quarter(book)
    expected = report.report(2026, 1)
    damage(
        engine,
        "fact_report_classification_profit_details",
        "UPDATE fact_report_classification_profit_details "
        "SET detail_code='management_other' "
        "WHERE detail_code='management_entertainment'",
    )
    assert report.report(2026, 1) == expected
    with pytest.raises(KernelError) as caught:
        Maintenance(engine).verify_integrity()
    assert caught.value.code == "content_integrity_failed"


def test_batched_lookup_loads_one_sql_statement_per_tree_level():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE report_classification_node(digest BLOB PRIMARY KEY,content TEXT)"
    )
    writer = _Nodes(connection)
    root = None
    keys = [f"voucher-{number:04d}" for number in range(1000)]
    for number, key in enumerate(keys):
        root = _insert(
            writer, root, key, [f"fact-{number}", "00" * 32, 1, number % 10 != 0], 0, 1
        )
    connection.executemany(
        "INSERT INTO report_classification_node VALUES(?,?)",
        _reachable_new_nodes(writer, root, 1).items(),
    )
    statements = []
    connection.set_trace_callback(statements.append)
    reader = _Nodes(connection)
    result = _lookup_many(reader, root, [*keys, "missing-voucher"], 1)
    connection.set_trace_callback(None)
    assert all(result[key][0][0] == f"fact-{number}" for number, key in enumerate(keys))
    assert result["missing-voucher"] == ()
    selects = [sql for sql in statements if "report_classification_node" in sql]
    assert len(selects) < 30
    statements.clear()
    connection.set_trace_callback(statements.append)
    exceptional_reader = _Nodes(connection)
    conflicts, unsafe = _exceptional_entries(exceptional_reader, root, 1)
    connection.set_trace_callback(None)
    assert not conflicts and len(unsafe) == 100
    exceptional_selects = [sql for sql in statements if "report_classification_node" in sql]
    assert len(exceptional_selects) < 30
    connection.close()


def test_filter_keeps_conflict_and_unsafe_keys_on_exact_tree_path():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE report_classification_node(digest BLOB PRIMARY KEY,content TEXT)"
    )
    writer = _Nodes(connection)
    tree = None
    tree = _insert(writer, tree, "conflicted", ["first", "11" * 32, 1, False], 0, 1)
    tree = _insert(writer, tree, "conflicted", ["second", "22" * 32, 2, True], 0, 1)
    tree = _insert(writer, tree, "ordinary", ["third", "33" * 32, 2, True], 0, 1)
    root = json.loads(_root_content(1, b"\x00" * 32, tree, writer, {"conflicted", "ordinary"}))
    key_filter = decode_keys_filter(root["key_filter"])
    assert root["conflicts"] == 1 and root["unsafe"] == 1
    assert all(may_contain(key_filter, key) for key in ("conflicted", "ordinary"))
    connection.executemany(
        "INSERT INTO report_classification_node VALUES(?,?)",
        _reachable_new_nodes(writer, tree, 1).items(),
    )
    reader = _Nodes(connection)
    conflicts, unsafe = _exceptional_entries(reader, tree, 1)
    assert conflicts == ("conflicted",)
    assert unsafe == (("conflicted", "first", "11" * 32, 1, False),)
    assert len(_lookup_many(reader, tree, conflicts, 1)["conflicted"]) == 2
    connection.close()


def test_released_v1_directory_reader_is_separate_and_checks_the_bound_root(book):
    from ai_accounting.kernel.content_history_context import (
        historical_content,
        report_classification_directory_reader,
    )
    from ai_accounting.kernel.report_flow import require_report_flow

    engine = book[0]
    scenario(book)
    close_quarter(book)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        expected = require_report_flow(engine, connection)["expected_rows"]
        with historical_content(1):
            reader = report_classification_directory_reader()
            assert reader.__name__.endswith("report_classification_directory_v1")
            assert reader.require_classification_directory(
                engine, connection, _expected_flows=expected
            )["roots"] == 3
