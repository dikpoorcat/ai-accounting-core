"""Lazy presentation metadata keeps original exact-version and supplement semantics."""

from types import SimpleNamespace

import pytest
from entity_fixture import seed_entities
from test_banking import book as _bank_book
from test_engine import close, publish, save
from test_engine import engine as _engine

from ai_accounting.kernel import entities
from ai_accounting.kernel.dashboard import _Snapshot
from ai_accounting.kernel.dashboard_metadata import Records, initialize_metadata
from ai_accounting.kernel.dashboard_reads import FrozenCloseView, FrozenFacts
from ai_accounting.kernel.display import Display
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.exports import Exports
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth

engine, bank_book = _engine, _bank_book


def test_employee_candidate_scope_reuses_only_successful_live_snapshot(engine, monkeypatch):
    calls = []
    fail_once = {"2026-02"}

    def selected(connection, period, *, registry, entity_ids=None):
        assert registry is engine.store.registry
        scope = None if entity_ids is None else frozenset(entity_ids)
        calls.append((connection, period, scope))
        if period in fail_once:
            fail_once.remove(period)
            raise RuntimeError("candidate read failed")
        return ["employee-for-" + period] if scope is None else sorted(scope)

    monkeypatch.setattr(entities, "employee_entities", selected)
    with QueryReads.snapshot(engine) as reads:
        snapshot = SimpleNamespace(
            reads=reads,
            connection=reads.connection,
            store=engine.store,
            period="2026-01",
        )
        records = Records(snapshot, "profile", kind="employee")
        first_query, first_params = records._query(records=True, identifiers={"first"})
        second_query, second_params = records._query(records=True, identifiers={"second"})
        assert first_query == second_query
        assert first_params[-1] != second_params[-1]
        records._query(records=True, identifiers={"first"})
        records._query(records=True)
        records._query(records=True)
        assert calls == [
            (reads.connection, "2026-01", frozenset({"first"})),
            (reads.connection, "2026-01", frozenset({"second"})),
            (reads.connection, "2026-01", None),
        ]
        snapshot.period = "2026-02"
        with pytest.raises(RuntimeError, match="candidate read failed"):
            records._query(records=True, identifiers={"first"})
        assert (
            "employee_entities",
            engine.store.company_id,
            engine.store.database_id,
            "2026-02",
            frozenset({"first"}),
        ) not in reads._report_snapshot_cache
        records._query(records=True, identifiers={"first"})
        records._query(records=True, identifiers={"first"})
        records._query(records=True, identifiers={"second"})
        assert [(period, scope) for _, period, scope in calls[3:]] == [
            ("2026-02", frozenset({"first"})),
            ("2026-02", frozenset({"first"})),
            ("2026-02", frozenset({"second"})),
        ]
    assert reads._report_snapshot_cache == {}
    with QueryReads.snapshot(engine) as another:
        snapshot.reads = another
        snapshot.connection = another.connection
        records = Records(snapshot, "profile", kind="employee")
        records._query(records=True, identifiers={"first"})
        assert calls[-1][1:] == ("2026-02", frozenset({"first"}))
        assert len(calls) == 7
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        snapshot.reads = QueryReads(engine, connection)
        snapshot.connection = connection
        records = Records(snapshot, "profile", kind="employee")
        records._query(records=True, identifiers={"first"})
        records._query(records=True, identifiers={"first"})
        records._query(records=True, identifiers={"second"})
        assert len(calls) == 10
        assert snapshot.reads._report_snapshot_cache == {}


def test_frozen_employee_candidates_use_the_frozen_management_scope(engine, monkeypatch):
    def current_membership(*_args, **_kwargs):
        raise AssertionError("Frozen scope must not enumerate current employment")

    monkeypatch.setattr(entities, "employee_entities", current_membership)
    with QueryReads.snapshot(engine) as reads:
        snapshot = SimpleNamespace(
            reads=reads,
            connection=reads.connection,
            store=engine.store,
            period="2026-01",
            month=YearMonth("2026-01").ordinal,
            close=SimpleNamespace(
                section=lambda name: (
                    {
                        "employee_entities": ["frozen-employee"],
                    }
                    if name == "management_snapshot"
                    else None
                )
            ),
        )
        records = Records(snapshot, "profile", kind="employee", frozen=True)
        _query, parameters = records._query(records=True, identifiers={"frozen-employee"})
        assert parameters[-2:] == ['["frozen-employee"]', '["frozen-employee"]']
        assert not any(key[0] == "employee_entities" for key in reads._report_snapshot_cache)


def profile(engine, kind, entity_id, revision=0, **fields):
    entity_kind, account_type = {
        "employee": ("person", None),
        "asset": ("asset", None),
        "fund_account": ("fund_account", "bank"),
    }[kind]
    with engine.store.connection(read_only=True) as connection:
        exists = connection.execute("SELECT 1 FROM entity WHERE id=?", (entity_id,)).fetchone()
    if exists is None:
        seed_entities(engine, ((entity_id, entity_kind, account_type),))
    evidence_digest = fields.pop("evidence_digest", None)
    event = fields.pop("workforce_event", None)
    directory = Entities(engine)
    options = {
        "source": f"明确资料 {revision + 1}",
        "expected_revision": revision + 1,
        "request_id": f"entity-profile:{kind}:{entity_id}:{revision}",
        "evidence_digest": evidence_digest,
    }
    if event is not None:
        result = directory.register_worker(dict(fields, **event), entity_id=entity_id, **options)
    else:
        result = directory.update_entity_profile(entity_id, fields, **options)
    return {"id": result["profile_id"], **result}


def detached_snapshot(engine, connection, period="2026-01"):
    snapshot = _Snapshot.__new__(_Snapshot)
    snapshot.engine, snapshot.store = engine, engine.store
    snapshot.connection, snapshot.period = connection, period
    snapshot.month = YearMonth(period).ordinal
    close = connection.execute(
        "SELECT * FROM period_close WHERE period=?", (snapshot.month,)
    ).fetchone()
    snapshot.reads = QueryReads(engine, connection)
    snapshot.close = FrozenCloseView(snapshot.reads, close) if close else None
    snapshot.profile_cache, snapshot.source_metadata = {}, {}
    snapshot.semantic_cache = {}
    snapshot.frozen_fact_ids = FrozenFacts(snapshot)
    snapshot.metadata = initialize_metadata(snapshot)
    return snapshot


def test_profile_key_iteration_and_entity_batch_do_not_decode_other_records(engine, monkeypatch):
    for index in range(30):
        profile(engine, "employee", f"employee-{index:03}", display_name=f"姓名 {index}")
        profile(engine, "asset", f"asset-{index:03}", display_name=f"资产 {index}")
    decoded = []
    original = entities._profile_record

    def watched(record):
        decoded.append(record["id"])
        return original(record)

    monkeypatch.setattr(entities, "_profile_record", watched)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        snapshot = detached_snapshot(engine, connection)
        assert decoded == []
        assert len(set(snapshot.profiles["asset"])) == 30
        assert decoded == []
        snapshot.metadata.prime_profiles("asset", {f"asset-{index:03}" for index in range(5)})
        assert len(decoded) == 5
        assert snapshot.profile("asset", "asset-003")["display_name"] == "资产 3"
        assert len(decoded) == 5
        assert snapshot.profiles["employee"].cache == {}


def test_frozen_metadata_and_missing_fields_keep_original_sources(engine):
    save(engine)
    publish(engine)
    old = profile(engine, "fund_account", "bank", active=False, note="")
    periods = Periods(engine)
    periods.management(
        "charge",
        payment_period=None,
        payment_category=None,
        expected_revision=0,
        request_id="metadata-before-close",
    )
    Display(engine).save_display_profile(
        {"kind": "business", "entity_id": "charge", "note": "", "source": "明确业务说明"},
        expected_revision=0,
        request_id="business-note-before-close",
    )
    proof = engine.register_evidence(
        b"synthetic payee", "text/plain", "payee", request_id="payee-proof"
    )["digest"]
    export = Exports(engine)
    seed_entities(engine, (("party", "organization", None),))
    old_payee = export.save_payee(
        "party",
        name="原名称",
        account="123",
        evidence_digest=proof,
        expected_revision=0,
        request_id="old-payee",
    )
    close(engine)
    newer = profile(engine, "fund_account", "bank", 1, active=True, note="后来补齐")
    periods.management(
        "charge",
        payment_period=None,
        payment_category=None,
        expected_revision=1,
        request_id="metadata-after-close",
    )
    Display(engine).save_display_profile(
        {"kind": "business", "entity_id": "charge", "note": "后来说明", "source": "后补业务说明"},
        expected_revision=1,
        request_id="business-note-after-close",
    )
    export.save_payee(
        "party",
        name="新名称",
        account="456",
        evidence_digest=proof,
        expected_revision=1,
        request_id="new-payee",
    )
    profile(
        engine,
        "employee",
        "later-person",
        display_name="后来录入姓名",
        workforce_event={"action": "hire", "effective_date": "2026-01"},
    )
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        snapshot = detached_snapshot(engine, connection)
        assert (
            dict(snapshot.profiles["fund_account"])
            == Display.profiles(connection, "2026-01")["fund_account"]
        )
        assert "later-person" not in snapshot.profiles["employee"]
        later = snapshot.profile("employee", "later-person")
        assert later["field_sources"]["display_name"]["basis"] == "current_supplement"
        selected = snapshot.profile("fund_account", "bank")
        assert selected["active"] is False
        assert selected["field_sources"]["active"]["id"] == old["id"]
        assert selected["note"] == "后来补齐"
        assert selected["field_sources"]["note"]["id"] == newer["id"]
        management = snapshot.management["charge"]
        business = snapshot.profile("business", "charge")
        assert business["note"] == "后来说明"
        assert business["field_sources"]["note"]["basis"] == "current_supplement"
        assert management["id"] in snapshot.frozen_management_ids
        party = snapshot.party_details("party")
        assert party["name"] == "原名称" and party["id"] == old_payee["payee_revision_id"]
        assert party["field_sources"]["name"]["basis"] == "frozen"


@pytest.mark.parametrize("registered_before_close", [True, False])
def test_bank_account_number_keeps_exact_formal_frozen_or_supplement_source(
    engine,
    registered_before_close,
):
    save(engine)
    publish(engine)
    proof = engine.register_evidence(
        b"synthetic sourced bank identity",
        "text/plain",
        "bank-identity",
        request_id="bank-identity-proof",
    )["digest"]
    directory = Entities(engine)
    data = {
        "bank_name": "合成银行",
        "bank_account_number": "111",
        "account_holder_name": "合成公司",
    }

    def register_account(number):
        return directory.register_entity(
            "fund_account",
            data | {"bank_account_number": number},
            account_type="bank",
            source="明确合成开户资料",
            evidence_digest=proof,
            request_id="bank-" + number,
        )

    old = register_account("111") if registered_before_close else None
    close(engine)
    if old:
        directory.correct_fund_account_identity(
            old["entity_id"],
            account_type="bank",
            data=data | {"bank_account_number": "222"},
            expected_identity_digest=old["fund_account"]["digest"],
            recording_error_confirmed=True,
            source="后来有据纠正的账户号码",
            evidence_digest=proof,
            request_id="correct-number",
        )
        saved, expected, basis = old, "111", "frozen"
    else:
        saved, expected, basis = register_account("222"), "222", "current_supplement"
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        selected = detached_snapshot(engine, connection).profile("fund_account", saved["entity_id"])
        assert selected["account_number"] == expected
        source = selected["field_sources"]["account_number"]
        assert (source["field"], source["basis"]) == ("identity.bank_account_number", basis)
        assert source["source_type"] == "fund_account_identity"
        assert source["id"] == saved["fund_account"]["digest"]


@pytest.mark.parametrize("kind", ["asset", "employee", "counterparty"])
def test_formal_bank_account_number_does_not_replace_other_profile_numbers(kind):
    selected = entities.display_profile(
        {"display_number": "管理编号"},
        kind,
        fund_account={"identity": {"bank_account_number": "622200009876"}},
    )
    assert selected["display_number"] == "管理编号" and "account_number" not in selected


def test_current_bank_account_number_uses_only_formal_registered_identity(engine):
    from ai_accounting.kernel.fund_accounts import require_fund_account

    profile(engine, "fund_account", "bank", display_name="负责人管理名称")
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        account = require_fund_account(connection, "bank")
        selected = detached_snapshot(engine, connection).profile("fund_account", "bank")
        assert selected["account_number"] == account["identity"]["bank_account_number"]
        assert selected["display_name"] == "负责人管理名称"
        source = selected["field_sources"]["account_number"]
        assert (source["id"], source["field"], source["basis"]) == (
            account["digest"],
            "identity.bank_account_number",
            "current",
        )


@pytest.mark.parametrize("codec_version", [4, 5])
def test_frozen_fund_metadata_uses_only_its_declared_close_format(
    engine, monkeypatch, codec_version,
):
    from close_storage_fixture import use_v4_close_manifest

    from ai_accounting.kernel.close_storage import decode_close
    from ai_accounting.kernel.fund_accounts import require_fund_account

    if codec_version == 4:
        # Generate the explicit historical contract with the normal close
        # writer. No immutable saved close is rewritten to satisfy this test.
        use_v4_close_manifest(monkeypatch)
    save(engine)
    publish(engine)
    original = profile(engine, "fund_account", "bank", display_name="冻结账户名称")
    profile(engine, "asset", "asset", display_name="冻结资产名称", display_number="原资产号")
    profile(engine, "employee", "employee", display_name="冻结员工名称",
            workforce_event={"action": "hire", "effective_date": "2026-01"})
    with engine.store.connection(read_only=True) as connection:
        old_account = require_fund_account(connection, "bank")
    close(engine)

    def frozen_bytes():
        with engine.store.connection(read_only=True) as connection:
            return {table: tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
                    for table in ("period_close", "close_storage_directory", "close_storage_block",
                                  "close_reference")}

    immutable = frozen_bytes()
    proof = engine.register_evidence(
        b"synthetic exact account number correction", "text/plain", "correction",
        request_id="v4-v5-correction-proof",
    )["digest"]
    newer = Entities(engine).correct_fund_account_identity(
        "bank", account_type="bank", data=old_account["identity"] | {"bank_account_number": "222"},
        expected_identity_digest=old_account["digest"], recording_error_confirmed=True,
        source="后来明确纠正账号", evidence_digest=proof, request_id="v4-v5-correct-number",
    )["fund_account"]
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        row = connection.execute("SELECT * FROM period_close").fetchone()
        manifest = decode_close(connection, row)
        assert manifest["format_version"] == codec_version
        assert ("fund_details" in manifest) is (codec_version == 5)
        if codec_version == 4:
            from ai_accounting.kernel import close_storage_v1
            from ai_accounting.kernel.close_fund_details import frozen_fund_account_definitions

            historical_header = close_storage_v1.verified_header(connection, row)

            def forbidden_section(_name):
                raise AssertionError("The declared v4 reader has no frozen fund section")

            assert frozen_fund_account_definitions(historical_header, forbidden_section) == {}
        snapshot = detached_snapshot(engine, connection)
        frozen = snapshot.profiles["fund_account"]["bank"]
        assert frozen["id"] == original["id"]
        assert frozen["display_name"] == "冻结账户名称"
        assert frozen["account_number"] == (
            None if codec_version == 4 else old_account["identity"]["bank_account_number"]
        )
        direct = Display.profiles(connection, "2026-01")
        assert direct["fund_account"]["bank"] == frozen
        assert direct["asset"]["asset"]["display_number"] == "原资产号"
        assert snapshot.profiles["employee"]["employee"]["display_name"] == "冻结员工名称"
        selected = snapshot.profile("fund_account", "bank")
        source = selected["field_sources"]["account_number"]
        if codec_version == 4:
            assert selected["account_number"] == "222"
            assert (source["id"], source["basis"]) == (newer["digest"], "current_supplement")
        else:
            assert selected["account_number"] == old_account["identity"]["bank_account_number"]
            assert (source["id"], source["basis"]) == (old_account["digest"], "frozen")
    assert frozen_bytes() == immutable


@pytest.mark.parametrize("damage", [
    "missing_section", "missing_definitions", "bad_digest", "missing_type", "missing_number",
    "missing_source", "missing_evidence",
])
def test_v5_frozen_fund_metadata_never_defaults_a_damaged_section(engine, monkeypatch, damage):
    from ai_accounting.kernel.close_fund_details import frozen_fund_account_definitions
    from ai_accounting.kernel.contracts import KernelError

    save(engine)
    publish(engine)
    profile(engine, "fund_account", "bank", display_name="冻结账户名称")
    close(engine)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        snapshot = detached_snapshot(engine, connection)
        original = snapshot.close.section

        def damaged(name):
            import copy

            assert name == "fund_details"
            if damage == "missing_section":
                raise KernelError("content_integrity_failed", "missing frozen fund section")
            value = copy.deepcopy(original(name))
            if damage == "missing_definitions":
                del value["definitions"]
            elif damage == "bad_digest":
                value["digest"] = "00" * 32
            else:
                from ai_accounting.kernel.types import digest

                definition = next(item for item in value["definitions"]
                                  if item["entity_id"] == "bank")
                if damage == "missing_number":
                    del definition["identity"]["bank_account_number"]
                else:
                    del definition[{
                        "missing_type": "account_type", "missing_source": "source",
                        "missing_evidence": "evidence_digest",
                    }[damage]]
                # Authenticate the fixture's changed logical shape so the
                # reader reaches its explicit field contract, not only digest.
                value["digest"] = digest({key: item for key, item in value.items()
                                          if key != "digest"}).hex()
            return value

        with pytest.raises(KernelError) as error:
            frozen_fund_account_definitions(snapshot.close.header, damaged)
        assert error.value.code == "content_integrity_failed"
        monkeypatch.setattr(snapshot.close, "section", damaged)
        with pytest.raises(KernelError):
            snapshot.profiles["fund_account"]["bank"]
        assert snapshot.profiles["fund_account"].cache == {}


@pytest.mark.parametrize("codec_version", [4, 5])
def test_nonempty_closed_funds_produces_all_default_pages_without_changing_frozen_bytes(
    bank_book, monkeypatch, codec_version,
):
    from close_storage_fixture import use_v4_close_manifest
    from test_dashboard_bank_source_proof import closed_banks

    from ai_accounting.kernel.dashboard import Dashboard
    from ai_accounting.kernel.dashboard_async import _produce
    from ai_accounting.kernel.response_contracts import validate_response

    if codec_version == 4:
        use_v4_close_manifest(monkeypatch)
    engine = closed_banks(bank_book)[0]

    def authority():
        with engine.store.connection(read_only=True) as connection:
            return {
                table: tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
                for table in ("state", "fact_revision", "calculation", "period_close",
                              "close_storage_directory", "close_storage_block", "close_reference")
            }

    before = authority()
    native = Dashboard(engine).funds("2026-09", movement_account_selection="first")["data"]
    assert native["total_fen"] == 1500
    assert len(native["collections"]["accounts"]["items"]) == 2
    produced = _produce(engine, "2026-09")
    assert produced["status"] == "ready"
    dashboard = Dashboard(engine)
    for name in ("brief", "funds", "assets", "employees"):
        options = {"movement_account_selection": "first"} if name == "funds" else {}
        page = getattr(dashboard, name)("2026-09", **options)
        validate_response("dashboard_" + name, page)
        if name == "funds":
            assert page["data"]["total_fen"] == native["total_fen"]
            assert page["data"]["collections"]["accounts"] == native["collections"]["accounts"]
    validate_response("dashboard_quarterly_report", dashboard.quarterly_report(2026, 3))
    assert authority() == before


def test_tax_identity_scope_loads_only_requested_employee_candidates(bank_book, monkeypatch):
    engine, save, _, _ = bank_book
    expected = set()
    for index in range(12):
        result = save(
            "tax_import_identity_v2",
            f"tax-{index}",
            {
                "period": "2026-09",
                "employee_id": "target" if index < 2 else f"person-{index}",
                "employee_code": "0007" if index < 2 else str(index),
                "name": f"候选姓名 {index}",
                "document_type": "居民身份证",
                "document_number": "001234567890123456",
            },
        )
        if index < 2:
            expected.add(result["fact_id"])
    loaded = set()
    original = engine.store.facts

    def watched(connection, identifiers):
        identifiers = set(identifiers)
        loaded.update(identifiers)
        return original(connection, identifiers)

    monkeypatch.setattr(engine.store, "facts", watched)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        snapshot = detached_snapshot(engine, connection, "2026-09")
        assert loaded == set()
        party = snapshot.party_details("target")
        assert party["name"] == "候选姓名 0、候选姓名 1（姓名资料待核对）"
        assert set(party["conflicting_ids"]) == expected == loaded
        assert snapshot.party_code("target") == "0007"
        assert loaded == expected
        assert set(snapshot.tax_identity_candidates.cache) == {"target"}
