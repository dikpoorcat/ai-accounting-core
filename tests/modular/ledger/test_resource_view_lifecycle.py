"""One unit owns resource-view reuse; detached results survive its cleanup."""

import gc
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from weakref import ref

import pytest
from test_adoption import BUSINESS, COMPANY, adopt_declared, expense
from test_adoption import store as store

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.ledger.public import create_ledger

RESOURCE = {"business_id": BUSINESS, "slot": "primary"}


@contextmanager
def disabled_gc():
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()


@pytest.mark.parametrize("mode", ["read", "write"])
@pytest.mark.parametrize("exceptional", [False, True])
def test_finished_unit_releases_actual_resource_view_cycle_without_gc(store, mode, exceptional):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)

    with disabled_gc():
        ending = (
            pytest.raises(RuntimeError, match="synthetic resource read exit")
            if exceptional
            else nullcontext()
        )
        with ending:
            with getattr(store, mode + "_unit")(COMPANY) as context:
                repository = context.repository("ledger")
                data = ledger.resource_definitions([RESOURCE], "obligation", context)
                view = repository.resource_view()
                assert view.base is repository and view._definitions
                assert repository.resource_view() is view
                references = ref(repository), ref(view)
                saved = deepcopy(data)
                if exceptional:
                    raise RuntimeError("synthetic resource read exit")
        # Retain the closed context. Neither the repository/view cycle nor its
        # loaded formal rows should need a later cyclic-GC pass to disappear.
        assert context._active is False
        del repository, view
        assert all(reference() is None for reference in references)
        assert data == saved and data[0].value["amount_fen"] == "100000"


def test_resource_views_reuse_exact_cutoff_and_refresh_after_actual_adoption(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        repository = unit.repository("ledger")
        current = repository.resource_view()
        january = repository.resource_view(as_of="2026-01")
        february = repository.resource_view(as_of="2026-02")
        assert current is repository.resource_view()
        assert january is repository.resource_view(as_of="2026-01")
        assert february is repository.resource_view(as_of="2026-02")
        assert len({id(current), id(january), id(february)}) == 3
        old = ledger.resource_definitions([RESOURCE], "obligation", unit)[0]
        before = repository.resource_revision

        adopt_declared(ledger, [expense("80000", seq=1)], unit)
        replacement = repository.resource_view()
        assert repository.resource_revision > before and replacement is not current
        assert replacement is repository.resource_view()
        assert repository.resource_view(as_of="2026-01") is not january
        assert repository.resource_view(as_of="2026-02") is not february
        actual = ledger.resource_definitions([RESOURCE], "obligation", unit)[0]
        assert actual.value["amount_fen"] == "80000" and old.value["amount_fen"] == "100000"
        # A borrowed old view keeps the existing revision-refresh behavior.
        heads = current.current_heads((BUSINESS,))
        assert current.definition_maps(heads, "obligation")[BUSINESS]["primary"] == actual


def test_retained_resource_view_keeps_pure_data_but_cannot_use_closed_sql(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
    with store.read_unit(COMPANY) as context:
        repository = context.repository("ledger")
        data = ledger.resource_definitions([RESOURCE], "obligation", context)
        view = repository.resource_view()
        definitions = deepcopy(view._definitions)
        saved = deepcopy(data)
    assert repository._resource_views == {}
    assert view._definitions == definitions and data == saved
    with pytest.raises(ContractError) as view_error:
        view.period_heads("2026-01", "2026-01")  # Unloaded rows require actual SQL.
    assert view_error.value.code == "invalid_operation"
    with pytest.raises(ContractError) as repository_error:
        repository.current_metadata((BUSINESS,))
    assert repository_error.value.code == "invalid_operation"
    with pytest.raises(ContractError) as context_error:
        context.repository("ledger")
    assert context_error.value.code == "invalid_operation"
