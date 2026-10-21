"""A selected obligation capture can carry its actual effective journal owners."""

from copy import deepcopy
from uuid import uuid4

import pytest
import test_adoption
from test_adoption import BUSINESS, COMPANY, adopt_declared, expense, result, withdrawal
from test_resource_indexes import split_resources

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.ledger import _read_origin_headers
from ai_accounting_modular.ledger.public import ObligationDirectoryQuery, create_ledger
from tests.modular.acceptance.performance.ledger_variant import MODES, repository_factory


@pytest.fixture(params=MODES)
def implementation(request, monkeypatch):
    factory = repository_factory(request.param)
    monkeypatch.setattr(test_adoption, "repository_factory", lambda: factory)


@pytest.fixture
def store(implementation, tmp_path, request):
    return test_adoption.store.__wrapped__(tmp_path, request)


def query(month="2026-01", current=None, **fields):
    return ObligationDirectoryQuery(month, current or month, include_origins=True, **fields)


def test_selected_vouchers_reuse_actual_headers_once_without_roundtrip_or_body_reads(
    store, monkeypatch
):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [split_resources(3)[0]], unit)
    with store.read_unit(COMPANY) as read:
        original = ledger.capture_obligation_directory(query(), read)
        expected = ledger.get_effective_voucher_directory([original.sources[0].reference], read)[0]
        assert original.selected_vouchers is None
        repo = read.repository("ledger")
        for name in (
            "read_heads",
            "results",
            "financial_results",
            "vouchers",
            "children",
            "read_voucher_lines",
        ):
            monkeypatch.setattr(repo, name, lambda *a: pytest.fail("重复读取目录头或完整账务正文"))
        monkeypatch.setattr(
            ledger, "get_effective_voucher_directory", lambda *a: pytest.fail("公开口回传")
        )
        checked, validate = [], _read_origin_headers.validate_contract_items

        def inspect(schema, definition, values, **fields):
            checked.append((definition, len(values)))
            return validate(schema, definition, values, **fields)

        monkeypatch.setattr(_read_origin_headers, "validate_contract_items", inspect)
        actual = ledger.capture_obligation_directory(query(include_vouchers=True), read)
    assert len(actual.selected) == 3
    assert actual.sources == original.sources and actual.origins == original.origins
    assert actual.selected_vouchers == {original.sources[0].reference["result_id"]: expected}
    assert checked.count(("ResultRef", 1)) == checked.count(("ResultSourceRef", 1)) == 1


def test_selected_only_map_does_not_resolve_a_current_only_correction(store, monkeypatch):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        unit.repository("ledger").freeze_heads("2026-01")
    january = result(store)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense("80000", seq=1)], unit, correction_period="2026-02")
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        seen, original = [], repo.read_voucher_headers

        def vouchers(ids):
            seen.extend(ids)
            return original(ids)

        monkeypatch.setattr(repo, "read_voucher_headers", vouchers)
        actual = ledger.capture_obligation_directory(
            query(current="2026-02", include_vouchers=True), read
        )
    assert len(actual.sources) == len(actual.origins) == 2
    assert set(actual.selected_vouchers) == {january["reference"]["result_id"]}
    assert set(seen) == set(actual.selected_vouchers)
    assert next(iter(actual.selected_vouchers.values())).period == "2026-01"


def test_inherited_voucher_retains_its_independently_checked_frozen_owner(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        unit.repository("ledger").freeze_heads("2026-01")
    january = result(store)
    formal = expense(seq=1)
    formal["plans"][0]["obligations"][0]["allowed_settlements"] = ["money"]
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [formal], unit, correction_period="2026-02")
    february = result(store)
    assert not february["vouchers"]
    with store.read_unit(COMPANY) as read:
        expected = ledger.get_effective_voucher_directory([february["reference"]], read)[0]
        actual = ledger.capture_obligation_directory(query("2026-02", include_vouchers=True), read)
    assert set(actual.selected_vouchers) == {february["reference"]["result_id"]}
    assert next(iter(actual.selected_vouchers.values())) == expected
    assert expected.result["result_id"] == january["reference"]["result_id"]
    assert set(actual.origins) == {source.source["result_id"] for source in actual.sources}
    assert january["reference"]["result_id"] not in actual.origins


@pytest.mark.parametrize("damage", ["missing", "bad_generation"])
def test_an_inherited_owner_is_checked_independently_not_invented_from_the_requested_head(
    store, monkeypatch, damage
):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        unit.repository("ledger").freeze_heads("2026-01")
    old_id = result(store)["reference"]["result_id"]
    formal = expense(seq=1)
    formal["plans"][0]["obligations"][0]["allowed_settlements"] = ["money"]
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [formal], unit, correction_period="2026-02")
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        original, calls = repo.read_heads, 0

        def owner_rows(ids):
            nonlocal calls
            assert set(ids) == {old_id}
            calls += 1
            rows = original(ids)
            # The original resolver first follows the baseline. Its output
            # owner must still have independently checked actual metadata.
            if calls == 2:
                if damage == "missing":
                    return []
                rows[0]["generation"] = True
            return rows

        monkeypatch.setattr(repo, "read_heads", owner_rows)
        with pytest.raises(ContractError) as error:
            ledger.capture_obligation_directory(query("2026-02", include_vouchers=True), read)
    assert calls == 2 and error.value.code == "storage_corrupt"


@pytest.mark.parametrize("retirement", ["withdrawal", "empty_target"])
def test_retirement_never_borrows_a_baseline_voucher_after_its_reversal(store, retirement):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        unit.repository("ledger").freeze_heads("2026-01")
    with store.write_unit(COMPANY) as unit:
        if retirement == "withdrawal":
            corrected = withdrawal(ledger.get_result_source(BUSINESS, "main", unit))
        else:
            corrected = expense("0", seq=1)
            corrected["plans"][0].update(lines=[], obligations=[])
            corrected["plans"][0]["basis"]["input"].pop("report_details")
        adopt_declared(ledger, [corrected], unit, correction_period="2026-02")
    latest = result(store)
    assert [row["role"] for row in latest["vouchers"]] == ["reversal"]
    with store.read_unit(COMPANY) as read:
        assert ledger.get_effective_voucher_directory([latest["reference"]], read) == (None,)
        assert (
            ledger.capture_obligation_directory(
                query("2026-02", include_vouchers=True), read
            ).selected_vouchers
            == {}
        )


def test_capture_rejects_conflicting_actual_target_states_of_the_same_result(store, monkeypatch):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [split_resources(2)[0]], unit)
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        original = repo.read_obligation_directory_rows

        def contradictory(*args, **kwargs):
            rows = original(*args, **kwargs)
            assert len(rows) == 2
            rows[1]["target_state"] = "withdrawn"
            return rows

        monkeypatch.setattr(repo, "read_obligation_directory_rows", contradictory)
        with pytest.raises(ContractError) as error:
            ledger.capture_obligation_directory(query(include_vouchers=True), read)
    assert error.value.code == "storage_corrupt"


def test_requested_empty_and_unrequested_do_not_resolve_any_journals(store, monkeypatch):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        monkeypatch.setattr(
            repo, "read_voucher_headers", lambda *a: pytest.fail("未请求凭证却读取")
        )
        assert ledger.capture_obligation_directory(query(), read).selected_vouchers is None
        assert (
            ledger.capture_obligation_directory(
                query(business_ids=(), include_vouchers=True), read
            ).selected_vouchers
            == {}
        )
        assert (
            ledger.capture_obligation_directory(
                query(categories=("reimbursement",), include_vouchers=True), read
            ).selected_vouchers
            == {}
        )


def test_a_selected_unjournaled_target_is_explicit_none_not_an_absent_key(store):
    ledger = create_ledger()
    # The shared trusted-plan contract also supports formal targets without a
    # journal. The resolver must use the actual stored journal relation.
    change = expense()
    change["plans"][0]["lines"] = []
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [change], unit)
    with store.read_unit(COMPANY) as read:
        actual = ledger.capture_obligation_directory(query(include_vouchers=True), read)
    assert len(actual.selected) == 1
    assert actual.selected_vouchers == {actual.sources[0].source["result_id"]: None}


@pytest.mark.parametrize("flag", [0, 1, None, "true"])
def test_voucher_choice_requires_an_actual_bool(store, flag):
    with store.read_unit(COMPANY) as read, pytest.raises(ContractError) as error:
        create_ledger().capture_obligation_directory(query(include_vouchers=flag), read)
    assert error.value.code == "invalid_input"


@pytest.mark.parametrize(
    "damage,code",
    [
        ("duplicate", "invalid_input"),
        ("too_many", "invalid_input"),
        ("bool_generation", "invalid_input"),
        ("extra_field", "invalid_input"),
        ("generation", "snapshot_changed"),
        ("business", "snapshot_changed"),
    ],
)
def test_existing_public_voucher_locator_boundary_stays_strict(store, damage, code):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
    reference = deepcopy(result(store)["reference"])
    requested = [reference]
    if damage == "duplicate":
        requested *= 2
    elif damage == "too_many":
        requested *= 501
    elif damage == "bool_generation":
        reference["generation"] = True
    elif damage == "extra_field":
        reference["already_checked"] = True
    elif damage == "generation":
        reference["generation"] += 1
    else:
        reference["business_id"] = str(uuid4())
    with store.read_unit(COMPANY) as read, pytest.raises(ContractError) as error:
        ledger.get_effective_voucher_directory(requested, read)
    assert error.value.code == code
