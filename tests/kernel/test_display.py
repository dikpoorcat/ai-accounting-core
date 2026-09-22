"""Display metadata cannot become accounting facts or rewrite historical closes."""

import sqlite3

import pytest
from entity_fixture import seed_registration_entities
from monthly_close_fixture import close_months, ready
from pydantic import TypeAdapter, ValidationError
from test_opening_continuation import book as book  # noqa: F401

from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.display import Display
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities, EntityProfile
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store


def profile(**changes):
    return {
        "display_name": "明确提供姓名",
        "employment_status": "active",
        **changes,
    }


def register_employee(engine, *, data=None, proof=None, request_id="profile"):
    saved = Entities(engine).register_entity(
        "person",
        profile(**(data or {})),
        source="负责人明确提供的展示资料",
        evidence_digest=proof,
        request_id=request_id,
    )
    item = next(
        item
        for item in Entities(engine).find_entities(kind="person")["items"]
        if item["entity_id"] == saved["entity_id"]
    )
    return saved, item["profile"]


def database_state(engine):
    with engine.store.connection(read_only=True) as connection:
        return {
            "epochs": engine.store.epochs(connection),
            **{
                table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
                for table in (
                    "fact_revision",
                    "calculation",
                    "voucher_version",
                    "period_close",
                    "display_profile_revision",
                    "entity",
                    "entity_profile_revision",
                    "period_commentary_revision",
                    "period_commentary_basis",
                    "audit",
                    "request",
                )
            },
        }


@pytest.mark.parametrize("start", [None, "2026-01", "2026-01-12"])
def test_profiles_keep_explicit_precision_and_only_advance_management(book, start):
    engine, _, _, _, proof = book
    display = Display(engine)
    before = database_state(engine)
    data = {}
    if start is not None:
        data["employment_start"] = start
    saved, current = register_employee(engine, data=data, proof=proof)
    assert current["employment_start"] == start and current["employment_status"] == "active"
    assert current["employment_end"] is None and current["evidence_digest"] == proof
    assert (
        Entities(engine).register_entity(
            "person",
            profile(**data),
            source="负责人明确提供的展示资料",
            evidence_digest=proof,
            request_id="profile",
        )
        == saved
    )
    after = database_state(engine)
    assert after["epochs"] == {**before["epochs"], "management": before["epochs"]["management"] + 1}
    for key in ("fact_revision", "calculation", "voucher_version", "period_close"):
        assert after[key] == before[key]
    displayed = display.display_profiles()["profiles"]["employee"][saved["entity_id"]]
    assert displayed["id"] == saved["profile_id"]
    assert displayed["display_name"] == "明确提供姓名"
    assert displayed["employment_start"] == start


@pytest.mark.parametrize(
    "changes",
    [
        {"salary_fen": 100},
        {"resident_employee": True},
        {"employment_start": "2026-02-30"},
        {"employment_start": "2026-02", "employment_end": "2026-01-31"},
        {"kind": "asset", "employment_start": "2026-01"},
        {"source": ""},
    ],
)
def test_typed_profiles_reject_accounting_fields_and_invalid_dates_atomically(book, changes):
    engine, *_ = book
    before = database_state(engine)
    with pytest.raises((ValidationError, ValueError)):
        Entities(engine).register_entity(
            "person",
            profile(**{key: value for key, value in changes.items() if key != "source"}),
            source=changes.get("source", "负责人明确提供的展示资料"),
            request_id="bad",
        )
    assert database_state(engine) == before


def test_profiles_reject_conflicts_missing_sources_and_faults_atomically(book):
    engine, *_ = book
    entities = Entities(engine)
    saved, _ = register_employee(engine, request_id="save")
    before = database_state(engine)
    with pytest.raises(KernelError) as conflict:
        entities.register_entity(
            "person",
            profile(display_name="另一姓名"),
            source="负责人明确提供的展示资料",
            request_id="save",
        )
    assert conflict.value.code == "idempotency_conflict" and database_state(engine) == before
    with pytest.raises(KernelError) as conflict:
        entities.update_entity_profile(
            saved["entity_id"],
            profile(),
            source="负责人明确提供的展示资料",
            expected_revision=0,
            request_id="another",
        )
    assert conflict.value.code == "entity_profile_version_conflict"
    assert database_state(engine) == before
    with pytest.raises(NeedsInformation):
        entities.update_entity_profile(
            saved["entity_id"],
            profile(),
            source="负责人明确提供的展示资料",
            evidence_digest="00" * 32,
            expected_revision=1,
            request_id="absent-proof",
        )
    assert database_state(engine) == before

    def fail(stage, _connection):
        if stage == "commit":
            raise RuntimeError("interrupted")

    engine.fault = fail
    with pytest.raises(RuntimeError):
        entities.update_entity_profile(
            saved["entity_id"],
            profile(note="修订"),
            source="负责人明确提供的展示资料",
            expected_revision=1,
            request_id="fault",
        )
    assert database_state(engine) == before
    with engine.store.connection() as connection:
        for sql in (
            "UPDATE entity_profile_revision SET source='overwrite' WHERE id=?",
            "DELETE FROM entity_profile_revision WHERE id=?",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(sql, (saved["profile_id"],))


def save_commentary(display, period="2026-01", text="确认本期经营情况", request_id="commentary"):
    preview = display.preview_period_commentary(period)
    return display.update_period_commentary(
        period,
        text,
        context_digest=preview["context_digest"],
        source="已核对的月度资料",
        expected_revision=preview["revision"],
        request_id=request_id,
    )


def test_commentary_context_conflicts_and_repeat_are_atomic(book):
    engine, *_ = book
    display = Display(engine)
    preview = display.preview_period_commentary("2026-01")
    register_employee(engine)
    before = database_state(engine)
    with pytest.raises(KernelError) as stale:
        display.update_period_commentary(
            "2026-01",
            "旧资料结论",
            context_digest=preview["context_digest"],
            source="人工核对",
            expected_revision=0,
            request_id="stale",
        )
    assert stale.value.code == "preview_expired" and database_state(engine) == before
    saved = save_commentary(display)
    assert saved["supplementary"] is False
    before = database_state(engine)
    # Replay uses the original revision and original basis, even after later changes.
    result = display.update_period_commentary(
        "2026-01",
        "确认本期经营情况",
        context_digest=saved["context_digest"],
        source="已核对的月度资料",
        expected_revision=0,
        request_id="commentary",
    )
    assert result == saved and database_state(engine) == before
    with pytest.raises(KernelError) as conflict:
        display.update_period_commentary(
            "2026-01",
            "并发结论",
            context_digest=saved["context_digest"],
            source="人工核对",
            expected_revision=0,
            request_id="conflict",
        )
    assert conflict.value.code == "period_commentary_conflict"
    with engine.store.connection() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM period_commentary_revision")


@pytest.mark.parametrize("following_months", [False, True])
def test_close_freezes_profiles_and_commentary_and_supplement_never_rewrites_it(
    book, following_months
):
    engine, _, _, _, proof = book
    ready(engine, proof)
    display, periods = Display(engine), Periods(engine)
    person, _ = register_employee(engine, request_id="person")
    commentary = save_commentary(display)
    close_months(periods, proof, last="2026-03" if following_months else "2026-01")
    frozen = periods.closed_report("2026-01")
    assert frozen["management_snapshot"]["entity_profiles"][0]["id"] == person["profile_id"]
    assert frozen["management_snapshot"]["commentary"]["id"] == commentary["id"]
    Entities(engine).update_entity_profile(
        person["entity_id"],
        profile(display_name="后补姓名"),
        source="负责人明确提供的展示资料",
        expected_revision=1,
        request_id="update",
    )
    supplement = save_commentary(display, text="后补说明", request_id="supplement")
    assert supplement["supplementary"] is True
    assert periods.closed_report("2026-01") == frozen
    with engine.store.connection(read_only=True) as connection:
        assert (
            Display.profiles(connection, "2026-01")["employee"][person["entity_id"]]["id"]
            == person["profile_id"]
        )
        assert (
            Display.profiles(connection)["employee"][person["entity_id"]]["display_name"]
            == "后补姓名"
        )
        result = Display.commentary(connection, "2026-01")
        assert result["frozen"]["id"] == commentary["id"]
        assert result["current"] == result["frozen"]
        assert [item["id"] for item in result["supplements"]] == [supplement["id"]]


def test_metadata_change_expires_close_preview_without_accounting_mutation(book):
    engine, _, _, _, proof = book
    ready(engine, proof)
    periods = Periods(engine)
    preview = periods.preview_close("2026-01", owner_confirmation=proof)
    register_employee(engine, request_id="person")
    before = database_state(engine)
    with pytest.raises(KernelError) as stale:
        periods.close(
            "2026-01",
            owner_confirmation=proof,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="close",
        )
    assert stale.value.code == "preview_expired" and database_state(engine) == before


def test_employment_dates_describe_management_precision_in_typed_schema():
    schema = TypeAdapter(EntityProfile).json_schema()
    start = schema["properties"]["employment_start"]
    assert start["anyOf"][0]["x-accounting-fact"]["role"] == "management"
    assert start["anyOf"][0]["x-accounting-fact"]["allowed_precision"] == ["month", "day"]


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "fund_account", "active": False},
        {
            "kind": "asset",
            "category_label": "办公设备",
            "rights_description": "自有",
            "useful_life_basis": "负责人确认的使用计划",
        },
        {
            "kind": "business",
            "counterparty_id": "supplier",
            "beneficiary_id": "person",
            "handler_id": "owner",
        },
    ],
)
def test_finite_profile_fields_preserve_original_display_capabilities(book, data):
    engine, save, _, _, _ = book
    kind = data["kind"]
    if kind != "business":
        accounting_epoch = database_state(engine)["epochs"]["accounting"]
        saved = Entities(engine).register_entity(
            kind,
            {key: value for key, value in data.items() if key != "kind"},
            account_type="bank" if kind == "fund_account" else None,
            source="明确管理资料",
            request_id="profile",
        )
        result = next(
            item["profile"]
            for item in Entities(engine).find_entities(kind=kind)["items"]
            if item["entity_id"] == saved["entity_id"]
        )
        for key, value in data.items():
            if key != "kind":
                assert result[key] == value
    else:
        references = {
            key: Entities(engine).register_entity(
                "organization" if key == "counterparty_id" else "person",
                {},
                source="明确管理资料",
                request_id="profile-" + key,
            )["entity_id"]
            for key in ("counterparty_id", "beneficiary_id", "handler_id")
        }
        save(
            "expense",
            "item",
            {
                "period": "2026-01",
                "counterparty_id": references["counterparty_id"],
                "amount_fen": 1,
                "expense_class": "administration",
                "creditor_kind": "supplier",
            },
        )
        accounting_epoch = database_state(engine)["epochs"]["accounting"]
        result = Display(engine).save_display_profile(
            {
                "kind": "business",
                "entity_id": "item",
                "source": "明确管理资料",
                **references,
            },
            expected_revision=0,
            request_id="profile",
        )
        assert all(result[key] == value for key, value in references.items())
    assert database_state(engine)["epochs"]["accounting"] == accounting_epoch


def test_stale_commentary_is_history_and_does_not_block_close(book):
    engine, _, _, _, proof = book
    ready(engine, proof, last="2026-01")
    display, periods = Display(engine), Periods(engine)
    saved = save_commentary(display)
    register_employee(engine)
    current = display.preview_period_commentary("2026-01")
    assert current["status"] == "stale" and current["current"] is None
    assert current["latest"]["id"] == saved["id"]
    preview = periods.preview_close("2026-01", owner_confirmation=proof)
    snapshot = preview["manifest"]["management_snapshot"]
    assert snapshot["commentary"] is None and snapshot["commentary_status"] == "stale"
    assert snapshot["commentary_latest"]["id"] == saved["id"]
    assert (
        periods.close(
            "2026-01",
            owner_confirmation=proof,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="close",
        )["status"]
        == "closed"
    )


def test_commentary_preview_contains_actual_posting_month_amounts_and_business_summary(book):
    engine, save, publish, _, _ = book
    amount = 9007199254740993
    save(
        "expense",
        "first-month",
        {
            "period": "2026-01",
            "amount_fen": amount,
            "counterparty_id": "supplier",
            "creditor_kind": "supplier",
            "expense_class": "administration",
        },
    )
    publish("first-month")
    save(
        "expense",
        "second-month",
        {
            "period": "2026-02",
            "amount_fen": 120,
            "counterparty_id": "supplier",
            "creditor_kind": "supplier",
            "expense_class": "administration",
        },
    )
    publish("second-month")
    display = Display(engine)
    january = display.preview_period_commentary("2026-01")["basis"]["accounting_summary"]
    february = display.preview_period_commentary("2026-02")["basis"]["accounting_summary"]
    assert january["month_expense_fen"] == str(amount)
    assert january["month_result_fen"] == str(-amount)
    assert february["month_expense_fen"] == "120"
    assert january["business_summary"] == [
        {"kind": "expense", "reversal": False, "count": 1, "total_fen": str(amount)}
    ]
    assert january["funds"] == {"cash_fen": "0", "bank_fen": "0", "platform_fen": "0"}


def test_commentary_preview_cannot_be_reused_for_an_identical_other_company(tmp_path):
    engines = [
        Engine(
            Store.create(
                tmp_path / (company + ".sqlite"),
                production_bundle(),
                company,
                company,
                company + "-db",
            )
        )
        for company in ("first", "second")
    ]
    first, second = (Display(engine) for engine in engines)
    preview = first.preview_period_commentary("2026-01")
    before = database_state(engines[1])
    with pytest.raises(KernelError) as rejected:
        second.update_period_commentary(
            "2026-01",
            "不可串用的经营结论",
            context_digest=preview["context_digest"],
            source="负责人",
            expected_revision=0,
            request_id="commentary",
        )
    assert rejected.value.code == "preview_expired"
    assert database_state(engines[1]) == before


@pytest.mark.parametrize("following_months", [False, True])
def test_close_preserves_independent_management_fact_versions_without_future_facts(
    book, following_months
):
    engine, save, _, _, proof = book
    ready(engine, proof)
    declaration = {
        "period": "2026-01",
        "employee_id": "employee",
        "tax_period": "2026-01",
        "income_category": "wages",
        "declared_tax_fen": 123,
        "declaration_confirmed": True,
    }
    seed_registration_entities(engine, "payroll_tax_declaration_actual", declaration)
    current = save("payroll_tax_declaration_actual", "january-declaration", declaration)
    future = save(
        "payroll_tax_declaration_actual",
        "february-declaration",
        {
            **declaration,
            "period": "2026-02",
            "tax_period": "2026-02",
        },
    )
    periods = Periods(engine)
    close_months(periods, proof, last="2026-03" if following_months else "2026-01")
    frozen = periods.closed_report("2026-01")
    rows = frozen["management_snapshot"]["typed_facts"]
    assert [(item["id"], item["kind"], item["revision"]) for item in rows] == [
        (current["fact_id"], "payroll_tax_declaration_actual", 1)
    ]
    assert future["fact_id"] not in {item["id"] for item in rows}
    if following_months:
        february = periods.closed_report("2026-02")["management_snapshot"]["typed_facts"]
        assert {item["id"] for item in february} == {current["fact_id"], future["fact_id"]}
    # Independent declarations have no calculator and must not require fake publications.
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM calculation").fetchone()[0] == 0


def test_independent_management_fact_changes_expire_commentary_basis(book):
    engine, save, _, _, _ = book
    display = Display(engine)
    saved = save_commentary(display)
    before = database_state(engine)["epochs"]
    data = {
        "period": "2026-01",
        "employee_id": "employee",
        "tax_period": "2026-01",
        "income_category": "wages",
        "declared_tax_fen": 123,
        "declaration_confirmed": True,
    }
    seed_registration_entities(engine, "payroll_tax_declaration_actual", data)
    declaration = save("payroll_tax_declaration_actual", "january-declaration", data)
    current = display.preview_period_commentary("2026-01")
    assert current["status"] == "stale" and current["latest"]["id"] == saved["id"]
    assert current["basis"]["typed_facts"][0]["id"] == declaration["fact_id"]
    after = database_state(engine)["epochs"]
    assert after["accounting"] == before["accounting"] and after["material"] == before["material"]
