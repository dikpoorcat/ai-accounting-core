"""Same-snapshot range reuse preserves every frozen root and stored node byte."""

import hashlib
import json
from collections import Counter

import pytest
from test_integrity_content import damage
from test_reports import book as book  # noqa: F401
from test_reports import close_quarter, scenario
from test_settlement_late_reviews import prepared, review
from test_settlement_period_scopes import allocation, cash_payment, setup

from ai_accounting.kernel import report_classification_directory as directory
from ai_accounting.kernel import settlement_freeze as freeze
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.domains.cash import CashFunding
from ai_accounting.kernel.types import YearMonth


class _PublicationRow(dict):
    def __init__(self, row, reads):
        super().__init__(row)
        self.reads = reads

    def __getitem__(self, key):
        self.reads[key] += 1
        return super().__getitem__(key)


class _PublicationConnection:
    """Count actual publication-row field access, without changing SQL/results."""

    def __init__(self, connection):
        self.connection = connection
        self.reads = Counter()

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, *args):
        result = self.connection.execute(sql, *args)
        if " ".join(sql.split()).upper() == "SELECT * FROM CALCULATION_PUBLICATION":
            return [_PublicationRow(row, self.reads) for row in result]
        return result


def _full_scan_late(connection):
    """Independent original candidate selection over the actual publication set."""
    publications = list(connection.execute("SELECT * FROM calculation_publication"))
    closes = list(connection.execute("SELECT * FROM period_close ORDER BY period"))
    reads = Counter()
    rows = [_PublicationRow(row, reads) for row in publications]
    late = {}
    from ai_accounting.kernel.content_history_context import close_reader

    for close in closes:
        highwater = close_reader().verified_header(connection, close).root["small"][
            "publication_sequence"
        ]
        late[close["period"]] = [
            row["id"] for row in rows
            if row["posting_period"] == close["period"] and row["sequence"] > highwater
        ]
    return late, reads, len(publications), len(closes)


def test_settlement_candidate_work_is_one_pass_despite_unrelated_months_and_subjects(
    tmp_path, request
):
    company = setup(tmp_path)
    company.close("2026-01")
    company.save(
        cash_payment(
            "2026-02", 800000,
            allocation("labor_project_cost", "cost", "net", 800000),
        ),
        "balance-paid",
    )
    company.publish("balance-paid")
    company.close("2026-02")
    company.close("2026-03")

    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        frozen_before = freeze._authoritative_freezes(company.engine, connection)
    for month in ("2026-04", "2026-05"):
        subjects = []
        for number in range(8):
            subject = f"unrelated-capital-{month}-{number}"
            company.save(
                CashFunding(
                    period=YearMonth(month), actual_date=f"{month}-{number + 1:02d}",
                    owner_id="owner", cash_account_id="cash",
                    funding_kind="capital", amount_fen=100 + number,
                ),
                subject,
            )
            subjects.append(subject)
        company.publish(*subjects)

    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        late, old_reads, publication_count, close_count = _full_scan_late(connection)
        assert close_count == 3 and all(not rows for rows in late.values())
        counted = _PublicationConnection(connection)
        rebuilt = freeze._authoritative_freezes(company.engine, counted)
        assert rebuilt == frozen_before  # Every original root, block and state revision.
        assert not freeze._frozen_difference(connection, *rebuilt)
        assert old_reads["posting_period"] == publication_count * close_count
        assert counted.reads["posting_period"] == publication_count
        request.node.user_properties.extend([
            ("publication_count", publication_count),
            ("closed_months", close_count),
            ("original_period_field_reads", old_reads["posting_period"]),
            ("scoped_period_field_reads", counted.reads["posting_period"]),
        ])


def test_late_reviews_still_reconstruct_original_highwater_and_check_exact_sources(
    tmp_path, monkeypatch
):
    company = prepared(tmp_path)
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        frozen_before = freeze._authoritative_freezes(company.engine, connection)
    for number in (1, 2):
        review(company, monkeypatch, number)
    checked = []
    original = freeze._verify_late_reviews

    def recorded(connection, period, publications, **kwargs):
        checked.append((period, [row["id"] for row in publications]))
        return original(connection, period, publications, **kwargs)

    monkeypatch.setattr(freeze, "_verify_late_reviews", recorded)
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        expected_late, _, _, _ = _full_scan_late(connection)
        assert sum(map(len, expected_late.values())) == 2
        rebuilt = freeze._authoritative_freezes(company.engine, connection)
        assert rebuilt == frozen_before
        assert dict(checked) == expected_late
        assert not freeze._frozen_difference(connection, *rebuilt)


def _independent_reachable(nodes, root):
    """Enumerate finished-root payloads independently of the production collector."""
    found = {}
    pending = [root] if root is not None else []
    while pending:
        checksum = pending.pop()
        if checksum in found:
            continue
        content = nodes.overlay[checksum]
        found[checksum] = content
        value = json.loads(content)
        if value["kind"] == "branch":
            pending.extend(bytes.fromhex(child) for child in value["children"].values())
    return found


class _NodeProbe:
    def __init__(self, nodes):
        self.nodes = nodes
        self.overlay = nodes.overlay
        self.reads = 0

    def read(self, *args):
        self.reads += 1
        return self.nodes.read(*args)


def test_finished_tree_union_expands_each_subtree_once_and_excludes_intermediates(request):
    nodes = directory._Nodes(None)
    root = None
    expected, actual = {}, {}
    old_work = new_work = 0
    for month in range(6):
        # Many initial parties, then small changes and unchanged monthly roots.
        for number in range(40 if month == 0 else int(month < 3)):
            key = f"voucher-{month}-{number}"
            entry = [
                f"fact-{month}-{number}", hashlib.sha256(key.encode()).hexdigest(), month, True,
            ]
            root = directory._insert(nodes, root, key, entry, 0, month)
        independent = _independent_reachable(nodes, root)
        expected.update(independent)
        baseline = _NodeProbe(nodes)
        assert directory._reachable_new_nodes(baseline, root, month) == independent
        old_work += baseline.reads
        scoped = _NodeProbe(nodes)
        actual.update(directory._reachable_new_nodes(scoped, root, month, expanded=actual))
        new_work += scoped.reads
        assert actual == expected
    assert new_work == len(expected)
    assert old_work > new_work * 3
    assert nodes.overlay.keys() - expected.keys()  # Copy-on-write intermediate nodes.
    request.node.user_properties.extend([
        ("original_node_expansions", old_work), ("scoped_node_expansions", new_work),
        ("finished_root_union_nodes", len(expected)),
        ("unreachable_intermediate_nodes", len(nodes.overlay.keys() - expected.keys())),
    ])


def test_classification_compare_old_and_scoped_traversals_match_all_roots_and_payloads(
    book, monkeypatch, request
):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    original = directory._reachable_new_nodes
    work = Counter()

    def measured(nodes, tree, period, *, expanded=()):
        probe = _NodeProbe(nodes)
        found = original(probe, tree, period, expanded=expanded)
        work["scoped"] += probe.reads
        return found

    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with monkeypatch.context() as scoped:
            scoped.setattr(directory, "_reachable_new_nodes", measured)
            result = directory.compare_classification_directory(engine, connection)
        assert not result["changed"] and result["roots"] == 3
        assert work["scoped"] == result["nodes"] > 0

        def old_collector(nodes, tree, period, *, expanded=()):
            probe = _NodeProbe(nodes)
            found = original(probe, tree, period)  # Original complete per-month traversal.
            work["original"] += probe.reads
            return found

        with monkeypatch.context() as scoped:
            scoped.setattr(directory, "_reachable_new_nodes", old_collector)
            assert directory.compare_classification_directory(engine, connection) == result
        assert work["original"] > work["scoped"]
        request.node.user_properties.extend([
            ("original_node_expansions", work["original"]),
            ("scoped_node_expansions", work["scoped"]),
            ("closed_months", result["roots"]),
            ("all_actual_nodes_checked", result["nodes"]),
        ])


@pytest.mark.parametrize("fault", ["missing", "payload", "extra", "root"])
def test_scoped_classification_compare_retains_complete_actual_damage_rejection(book, fault):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    if fault == "missing":
        sql = "DELETE FROM report_classification_node"
    elif fault == "payload":
        sql = "UPDATE report_classification_node SET content='{}'"
    elif fault == "extra":
        sql = "INSERT INTO report_classification_node VALUES(zeroblob(32),'{}')"
    else:
        sql = "UPDATE report_classification_directory SET content='{}'"
    damage(
        engine,
        "report_classification_directory" if fault == "root" else "report_classification_node",
        sql,
    )
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with pytest.raises(KernelError) as failure:
            directory.require_classification_directory(engine, connection)
        assert failure.value.code == "content_integrity_failed"
