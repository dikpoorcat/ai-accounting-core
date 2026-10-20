"""Month bounds filter the current index before owner bodies are exported."""

from copy import deepcopy

from ai_accounting_modular.application._context import DomainRegistry, SubmissionContext

from .test_publications import Ledger, Owner, direct, fact, identity, relation, seed
from .test_publications import system as system


def link(number):
    return {**direct(number, 100), "read_mode": "fact"}


def context(read, owner, *, facts=None):
    return SubmissionContext(
        read,
        DomainRegistry([owner]),
        None,
        Ledger(),
        None,
        facts=facts,
        evaluation_period_to="2026-12",
    )


def test_month_lookup_never_exports_unrelated_month_bodies(system):
    january, december = fact(1), fact(2, "2026-12")
    seed(system, [january, december], relations=[link(1), link(2)])
    owner = Owner([january, december], {})
    storage, company = system
    with storage.read_unit(company) as read:
        view = context(read, owner)
        assert view.related_facts(
            identity(100), "calculation", period_from="2026-12", period_to="2026-12"
        ) == (december,)
    assert owner.exported == [identity(2)]


def test_current_month_filter_keeps_final_overlay_move_and_withdrawal(system):
    january, december = fact(1), fact(2, "2026-12")
    seed(system, [january, december], relations=[link(1), link(2)])
    moved = deepcopy(january)
    moved["period"] = "2026-12"
    owner = Owner([january, december], {})
    storage, company = system
    with storage.read_unit(company) as read:
        view = context(read, owner, facts={identity(1): moved, identity(2): None})
        view.set_relations((identity(1), identity(2)), (relation(link(1)),))
        assert view.related_facts(
            identity(100), "calculation", period_from="2026-12", period_to="2026-12"
        ) == (moved,)
        assert (
            view.related_facts(
                identity(100), "calculation", period_from="2026-01", period_to="2026-01"
            )
            == ()
        )
    assert owner.exported == []


def test_direct_membership_changes_without_changing_public_numeric_dependency(system):
    current, edge = fact(1), link(1)
    seed(system, [current], dependencies=[edge], relations=[])
    storage, company = system
    with storage.read_unit(company) as read:
        repo = read.repository("application")
        assert repo.dependencies([identity(1)]) == (edge,)
        assert repo.related_ids("business", {identity(100)}, "calculation") == ()
    with storage.write_unit(company) as unit:
        assert unit.repository("application").replace_relations(
            [identity(1)], [relation(edge)], search_revision=unit.next_head
        )
    with storage.read_unit(company) as read:
        assert read.repository("application").dependencies([identity(1)]) == (edge,)
        assert read.repository("application").related_ids(
            "business", {identity(100)}, "calculation"
        ) == (identity(1),)
        before = read.head
    with storage.write_unit(company) as unit:
        assert not unit.repository("application").replace_relations(
            [identity(1)], [relation(edge)], search_revision=unit.next_head
        )
    with storage.read_unit(company) as read:
        assert read.head == before


def test_object_search_uses_only_fact_relations_and_computation_changes_do_not_stamp_it(system):
    current = fact(1)
    derived = {**direct(1, 100), "producer_kind": "object", "read_mode": "fact"}
    direct_object = {**derived, "producer_id": identity(101)}
    seed(system, [current], dependencies=[derived], relations=[])
    storage, company = system
    query = {
        "family": "operations",
        "period_from": "2026-01",
        "period_to": "2026-01",
        "limit": 10,
        "object_id": identity(100),
    }
    with storage.read_unit(company) as read:
        repo = read.repository("application")
        revision = repo.heads([identity(1)])[identity(1)]["search_revision"]
        assert repo.search_page(query) == []
    with storage.write_unit(company) as unit:
        repo = unit.repository("application")
        assert repo.replace_dependencies([identity(1)], [derived, direct_object])
        assert repo.heads([identity(1)])[identity(1)]["search_revision"] == revision
        assert repo.replace_relations(
            [identity(1)], [relation(direct_object)], search_revision=unit.next_head
        )
        assert repo.heads([identity(1)])[identity(1)]["search_revision"] == unit.next_head
    with storage.read_unit(company) as read:
        repo = read.repository("application")
        assert repo.search_page(query) == []
        assert [
            item["business_id"] for item in repo.search_page({**query, "object_id": identity(101)})
        ] == [identity(1)]
