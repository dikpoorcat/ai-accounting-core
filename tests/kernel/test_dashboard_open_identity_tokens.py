"""Fact display tokens retain source proofs, complete groups and exact pages."""

from collections import Counter
from types import SimpleNamespace

import pytest
from material_fixture import supporting_text
from test_banking import book as book
from test_banking import close_month, inventories
from test_dashboard_open_groups import expenses, snapshot
from test_dashboard_provenance import profile
from test_integrity_content import damage
from test_opening_continuation import book as opening_book_fixture

from ai_accounting.kernel import dashboard_open_groups as groups
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard_sort import IDENTITY_FACT_FIELDS

PERIOD = "2026-09"
opening_book = opening_book_fixture


def _read_all(engine, period=PERIOD):
    with snapshot(engine, period) as snap:
        first = groups.open_group_page(snap, limit=2)
        pages, members, cursor = [first], {}, first["collection"]["page"]["next_cursor"]
        while cursor is not None:
            page = groups.open_group_page(snap, after=cursor, limit=2)
            pages.append(page)
            cursor = page["collection"]["page"]["next_cursor"]
        for page in pages:
            for row in page["collection"]["items"]:
                detail = groups.open_group_members(snap, row["group_key"], limit=3)
                parts = [detail]
                while detail["page"]["has_more"]:
                    detail = groups.open_group_members(
                        snap, row["group_key"], after=detail["page"]["next_cursor"], limit=3,
                    )
                    parts.append(detail)
                members[row["group_key"]] = parts
        return pages, members


def _fallback(engine, monkeypatch):
    with monkeypatch.context() as legacy:
        legacy.setattr(groups, "_fact_identity_tokens", lambda _: {})
        return _read_all(engine)


def _source(index, fact_id="fact", **changes):
    return {
        "key": f"expense:source-{index}:primary", "name": "primary",
        "source_business": {"kind": "expense", "subject_id": f"source-{index}"},
        "source_fact_id": fact_id, "category": "payable", "category_key": "supplier_payables",
        "counterparty_id": "supplier", **changes,
    }


def _identities(sources, scalars, *, tokens=None, supplements=None):
    snap = SimpleNamespace(store=SimpleNamespace(company_id="company"))
    display, group, cohorts = {}, {}, {}
    return [groups._identity(snap, source, scalars, supplements, display_cache=display,
                             group_cache=group, cohort_cache=cohorts, fact_tokens=tokens)
            for source in sources]


@pytest.mark.parametrize("fact_count,source_count", [(9, 21), (90, 210)])
def test_fact_tokens_avoid_repeated_complete_inputs_per_source(fact_count, source_count):
    reads = Counter()

    class Observed(dict):
        def get(self, key, default=None):
            if key in IDENTITY_FACT_FIELDS:
                reads[key] += 1
            return super().get(key, default)

    scalars = {f"fact-{index}": Observed(creditor_kind="supplier", expense_class="administration")
               for index in range(fact_count)}
    sources = [_source(index, f"fact-{index % fact_count}") for index in range(source_count)]
    expected = _identities(sources, scalars)
    full_reads = sum(reads.values())
    reads.clear()
    tokens = groups._fact_identity_tokens(scalars)
    actual = _identities(sources, scalars, tokens=tokens)
    token_reads = sum(reads.values())
    assert actual == expected
    assert len({id(item) for item in actual}) == 1
    assert len(tokens) == fact_count and len(set(tokens.values())) == 1
    assert token_reads == 9 * fact_count + 2
    assert full_reads == 8 * source_count + 2
    assert token_reads < full_reads
    print(f"fact/source={fact_count}/{source_count}: exact identity field reads "
          f"{full_reads}->{token_reads}")


def test_fact_tokens_keep_types_missing_null_and_exact_values():
    scalars = {
        "bool": {"side": True}, "int": {"side": 1}, "float": {"side": 1.0},
        "missing": {}, "null": {"side": None}, "text": {"side": "1"},
        "employee": {"employee_id": "员工"}, "other": {"employee_id": "另一员工"},
    }
    tokens = groups._fact_identity_tokens(scalars)
    assert len({tokens[name] for name in ("bool", "int", "float", "text")}) == 4
    assert tokens["missing"] == tokens["null"]
    assert tokens["employee"] != tokens["other"]
    assert all(type(value) is int for value in tokens.values())
    sources = [_source(index, fact_id) for index, fact_id in enumerate(scalars)]
    assert _identities(sources, scalars, tokens=tokens) == _identities(sources, scalars)


@pytest.mark.parametrize("value", [[], {}, ["supplier"], {"name": "supplier"}])
def test_composite_identity_inputs_retain_original_exception_route(value):
    scalars = {"fact": {"creditor_kind": value, "expense_class": "administration"}}
    tokens = groups._fact_identity_tokens(scalars)
    assert tokens == {"fact": None}
    sources = [_source(0)]
    with pytest.raises(TypeError) as original:
        _identities(sources, scalars)
    with pytest.raises(TypeError) as optimized:
        _identities(sources, scalars, tokens=tokens)
    assert str(original.value) == str(optimized.value)


def test_fact_tokens_do_not_share_source_components_direction_party_or_supplement():
    scalars = {"fact": {"creditor_kind": "supplier", "expense_class": "administration"}}
    sources = [
        _source(0), _source(1, category="receivable", category_key="other_receivables"),
        _source(2, counterparty_id="other"), _source(3, name="unknown"),
        _source(4, counterparty_id=None), _source(5, counterparty_id=None),
        _source(6, counterparty_id=None),
    ]
    supplements = {sources[4]["key"]: {"party_id": "supplier"},
                   sources[5]["key"]: {"party_id": "other"}}
    original = _identities(sources, scalars, supplements=supplements)
    optimized = _identities(sources, scalars, tokens=groups._fact_identity_tokens(scalars),
                            supplements=supplements)
    assert optimized == original
    assert len({item.group_key for item in optimized[:4]}) == 4
    assert optimized[4].group_key == optimized[0].group_key
    assert optimized[5].group_key == optimized[2].group_key
    assert optimized[6].group_key not in {optimized[0].group_key, optimized[2].group_key}


@pytest.mark.parametrize("count", [8, 16])
def test_fact_tokens_keep_complete_summary_and_all_group_member_pages(
    book, count, monkeypatch,
):
    engine, _, _, proof = book
    total = 0
    for party, name in (("first", "张供应商"), ("second", "李供应商"), ("third", "张供应商")):
        total += expenses(book, count, party=party, prefix=party)
        profile(engine, "counterparty", party, display_name=name)
    close_month(inventories(engine, proof, PERIOD, {"transactions"}), proof, PERIOD)
    expected = _fallback(engine, monkeypatch)
    with snapshot(engine, PERIOD) as snap:
        summary = groups.open_group_page(snap, summary_only=True)
        assert summary["payable_fen"] == total
        assert summary["group_count"] == 3 and summary["total_count"] == 3 * count
        assert len(snap._open_group_data["groups"]) == 3
        assert "dashboard_fact_sort_scalars" not in snap.reads._report_snapshot_cache
        group = snap._open_group_data["ordered"][0]
        groups.open_group_page(snap, limit=1)
        first = groups.open_group_members(snap, group, limit=3)
        assert len(first["items"]) == 3 and first["page"]["total_count"] == count
        assert len(snap.reads._report_snapshot_cache["dashboard_fact_sort_scalars"]) == 3
        again = groups.open_group_members(snap, group, limit=3)
        assert again == first
        assert len(snap.reads._report_snapshot_cache["dashboard_fact_sort_scalars"]) == 3
        with pytest.raises(KernelError, match="分页位置") as failure:
            groups.open_group_members(snap, group, after="different-group-member", limit=3)
        assert failure.value.code == "dashboard_snapshot_changed"
        with pytest.raises(KernelError) as failure:
            groups.open_group_page(snap, after="invalid-group")
        assert failure.value.code == "dashboard_snapshot_changed"
        with pytest.raises(KernelError) as failure:
            groups.open_group_members(snap, "invalid-group")
        assert failure.value.code == "dashboard_group_not_found"
    assert _read_all(engine) == expected


def test_fact_tokens_keep_unknown_matters_separate_by_exact_source_key(opening_book, monkeypatch):
    engine, _, _, package, proof = opening_book
    package([
        ("opening_equity", "prior-loss", {
            "equity_kind": "retained_earnings", "balance_fen": -3000,
            "holder_or_basis_id": "prior-book",
        }),
        *[("opening_obligation", f"unknown-{index}", {
            "counterparty_id": "supplier", "nature": "other_payable",
            "outstanding_fen": 1000 + index * 1000, "business_reference": f"prior-{index}",
        }) for index in range(2)],
    ], period=PERIOD)
    supporting_text(engine, proof, period=PERIOD)
    close_month(inventories(engine, proof, PERIOD, {"transactions"}), proof, PERIOD)
    expected = _fallback(engine, monkeypatch)
    with snapshot(engine, PERIOD) as snap:
        page = groups.open_group_page(snap)
        assert page["total_count"] == page["group_count"] == 2
        assert page["payable_fen"] == 3000
        assert all(row["member_count"] == 1 for row in page["collection"]["items"])
        assert len({row["group_key"] for row in page["collection"]["items"]}) == 2
    assert _read_all(engine) == expected


@pytest.mark.parametrize("entry", ["group", "members", "summary"])
def test_fact_tokens_keep_all_source_facts_proved_independently_of_page_bodies(
    book, entry,
):
    engine, *_ = book
    expenses(book, 4)
    proof = book[-1]
    close_month(inventories(engine, proof, PERIOD, {"transactions"}), proof, PERIOD)
    with snapshot(engine, PERIOD) as snap:
        summary = groups.open_group_page(snap, summary_only=True)
        assert summary["total_count"] == 4
        group = snap._open_group_data["ordered"][0]
    damage(engine, "fact_expense", "UPDATE fact_expense SET amount_fen=amount_fen+1 "
           "WHERE revision_id IN (SELECT id FROM fact_revision WHERE subject_id='expense-003')")
    with snapshot(engine, PERIOD) as snap:
        with pytest.raises(KernelError) as failure:
            if entry == "members":
                groups.open_group_members(snap, group, limit=1)
            else:
                groups.open_group_page(snap, limit=1, summary_only=entry == "summary")
        assert failure.value.code == "content_integrity_failed"


@pytest.mark.parametrize("entry", ["group", "members", "summary"])
def test_record_group_proves_all_bodies_but_member_page_keeps_its_exact_scope(book, entry):
    engine, *_ = book
    expenses(book, 4)
    close_month(inventories(engine, book[-1], PERIOD, {"transactions"}), book[-1], PERIOD)
    with snapshot(engine, PERIOD) as snap:
        groups.open_group_page(snap, summary_only=True)
        group = snap._open_group_data["ordered"][0]
        first = groups.open_group_members(snap, group, limit=1)
        unselected = next(row[0] for row in snap.connection.execute(
            "SELECT c.calculation_id FROM calculation_current c "
            "JOIN subject s ON s.id=c.subject_id WHERE s.kind='expense' AND c.subject_id<>?",
            (first["items"][0]["subject_id"],),
        ))
    damage(engine, "calculation", "UPDATE calculation SET outcome=json_set("
           "outcome,'$.values.creditor_kind','employee') WHERE id=?", (unselected,))
    with snapshot(engine, PERIOD) as snap:
        if entry == "group":
            with pytest.raises(KernelError) as failure:
                groups.open_group_page(snap)
            assert failure.value.code == "content_integrity_failed"
        elif entry == "members":
            assert groups.open_group_members(snap, group, limit=1) == first
        else:
            assert groups.open_group_page(snap, summary_only=True)["total_count"] == 4
