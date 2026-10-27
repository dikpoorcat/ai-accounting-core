"""Count-only summaries preserve Open roots and reject contradictory saved summaries."""

import pytest

from ai_accounting_modular.contracts.public import ContractError, canonical_json, loads_json
from ai_accounting_modular.reading import _brief_open, _monthly, _open_store
from ai_accounting_modular.reading import _brief_open_index as index
from ai_accounting_modular.reading import _brief_open_tree as blocks
from ai_accounting_modular.storage.public import block_digest
from tests.modular.acceptance.performance.ledger_variant import MODES, install_ledger_implementation
from tests.modular.application._working_case import case as case

from ._publication import body as stored_body
from ._publication import replace_body
from .test_brief_open import apply, members, page, publish
from .test_brief_open_incremental import change, scope, snapshot


def test_count_derived_summary_keeps_the_same_strict_integer_boundary():
    count = blocks.Count()
    assert count.summary_from_count(0) == 0
    assert count.summary_from_count(27) == 27
    for value in (None, False, True, -1, 1.0, "1"):
        with pytest.raises(ContractError) as error:
            count.summary_from_count(value)
        assert error.value.code == "storage_corrupt"


@pytest.fixture(params=MODES)
def count_case(tmp_path, request):
    with install_ledger_implementation(request.param):
        fixture = case.__wrapped__(tmp_path)
        try:
            yield next(fixture)
        finally:
            fixture.close()


def test_real_expense_patch_keeps_all_five_roots_pages_and_amounts(count_case, monkeypatch):
    case = count_case
    case.seed(expense=False)
    facts = [case.expense("1000") for _ in range(18)]
    apply(case, *facts)
    publish(case)
    change(case, facts[0], "1200")
    seen = []
    original = blocks.Count.summary_from_count

    def summary_from_count(self, count):
        seen.append(count)
        return original(self, count)

    with monkeypatch.context() as observed:
        observed.setattr(blocks.Count, "summary_from_count", summary_from_count)
        assert case.kernel.reading.run_pending(case.company)
    assert seen, "The real multi-leaf Open patch must reach the count-only strategy hook"
    actual = snapshot(case)
    public = page(case, actual)
    summary = public["open_items"]
    assert (summary["total_count"], summary["group_count"], summary["payable_fen"]) == (
        18,
        1,
        "18200",
    )
    group = public["collections"]["open_items"]["items"][0]
    first = members(case, actual, group["group_key"], limit=7)["collections"]["members"]
    rest = members(case, actual, group["group_key"], limit=20, cursor=first["page"]["next_cursor"])[
        "collections"
    ]["members"]
    values = first["items"] + rest["items"]
    assert len(values) == len({value["id"] for value in values}) == 18
    assert sum(int(value["outstanding_fen"]) for value in values) == 18200
    assert {value["subject_id"] for value in values} == {fact["business_id"] for fact in facts}
    with case.kernel.storage.read_unit(case.company) as read:
        repository = read.repository("reading")
        saved = stored_body(repository, _monthly.key(scope(case)), index.COMPONENT)
        cold = _brief_open.prepare(case.kernel.reading, scope(case), read)
        with monkeypatch.context() as legacy:
            # The optional hook's absence selects the original strict-child summary route.
            legacy.delattr(blocks.Count, "summary_from_count")
            original_route = _brief_open.prepare(case.kernel.reading, scope(case), read)
    actual_payload, full_payload, old_payload = map(
        loads_json, (saved["payload_json"], cold.payload_json, original_route.payload_json)
    )
    assert set(actual_payload["_roots"]) == set(blocks.ROLES)
    assert all(actual_payload["_roots"][role] for role in blocks.ROLES)
    assert actual_payload["_roots"] == full_payload["_roots"] == old_payload["_roots"]
    assert (
        actual_payload["_collections"]
        == full_payload["_collections"]
        == old_payload["_collections"]
    )
    assert actual_payload["open_items"] == full_payload["open_items"] == old_payload["open_items"]


@pytest.mark.parametrize("role", blocks.ROLES)
def test_a_resealed_saved_count_summary_must_equal_the_actual_node_count(case, role):
    case.seed(expense=False)
    apply(case, *(case.expense("1000") for _ in range(18)))
    publish(case)
    selected = _monthly.key(scope(case))
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        repository = unit.repository("reading")
        session = repository._session
        payload = loads_json(stored_body(repository, selected, index.COMPONENT)["payload_json"])
        identity = payload["_roots"][role][0]
        where = "WHERE scope_key=? AND collection_key=? AND item_id=?"
        node = loads_json(
            session.fetchone(
                "SELECT payload_json FROM read_item " + where,
                (_open_store.SCOPE, _open_store.NODES, identity),
            )[0]
        )
        assert node["summary"] == node["count"]
        node["summary"] += 1  # A valid nonnegative integer, but wrong for this strict node.
        raw = canonical_json(node)
        replacement = block_digest(raw)
        reference = loads_json(
            session.fetchone(
                "SELECT payload_json FROM read_item " + where,
                (_open_store.SCOPE, _open_store.REFS, identity),
            )[0]
        )
        assert reference["count"] == 1  # This fixture has exactly one live scope.
        reference["node"] = replacement
        for collection, body in (
            (_open_store.NODES, raw),
            (_open_store.REFS, canonical_json(reference)),
        ):
            assert (
                session.execute(
                    "UPDATE read_item SET item_id=?,sort_key=?,payload_json=? " + where,
                    (replacement, replacement, body, _open_store.SCOPE, collection, identity),
                )
                == 1
            )
        payload["_roots"][role][0] = replacement
        payload["_collections"] = blocks.manifests(payload["_roots"])
        payload["_voucher_display_head"] = blocks.display_head(payload["_roots"])
        resealed = index.seal(payload, (), payload["_item_epoch"])
        assert index.validate(loads_json(resealed.payload_json)) == payload["_item_epoch"]
        # Keep all raw SHA/locator/manifest/state bindings consistent. The sole defect
        # is the count strategy's saved summary, not a missing node or stale digest.
        replace_body(repository, selected, index.COMPONENT, resealed)
    with case.kernel.storage.read_unit(case.company) as read, pytest.raises(ContractError) as error:
        repository = read.repository("reading")
        index.base(repository, repository.state(selected, index.COMPONENT))
    assert error.value.code == "storage_corrupt"
