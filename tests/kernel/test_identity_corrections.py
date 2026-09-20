"""Real publications, explicit entities and immutable identity correction receipts."""

import pytest
from schema_fixture import test_bundle

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.identity_corrections import IdentityCorrections
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store


@pytest.fixture
def identity_engine(tmp_path):
    engine = Engine(
        Store.create(
            tmp_path / "identity.sqlite",
            test_bundle(default_registry()),
            "company",
            "91310000123456789A",
            "database",
        )
    )
    evidence = engine.register_evidence(
        b"synthetic identity confirmation",
        "text/plain",
        "confirmation",
        request_id="identity-evidence",
    )["digest"]
    entities = Entities(engine)
    first = entities.register_entity("person", {}, source="synthetic", request_id="person-a")[
        "entity_id"
    ]
    second = entities.register_entity("person", {}, source="synthetic", request_id="person-b")[
        "entity_id"
    ]
    return engine, evidence, first, second


def expense(engine, evidence, party, subject="expense", amount=100):
    data = {
        "period": "2026-01",
        "counterparty_id": party,
        "amount_fen": amount,
        "expense_class": "administration",
        "creditor_kind": "employee",
    }
    engine.save_fact(
        "expense",
        subject,
        data,
        evidence=(evidence,),
        expected_revision=0,
        request_id="save-" + subject,
    )
    preview = engine.preview([subject])
    engine.confirm(
        [subject],
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="publish-" + subject,
    )
    return data


def confirm(engine, kwargs, request="correction"):
    command = IdentityCorrections(engine)
    preview = command.preview_identity_correction(**kwargs)
    result = command.confirm_identity_correction(
        **kwargs, preview_digest=preview["digest"], epochs=preview["epochs"], request_id=request
    )
    from ai_accounting.kernel.integrity import verify_integrity

    with engine.store.connection(read_only=True) as connection:
        verify_integrity(engine, connection)
    return preview, result


def test_reassign_is_atomic_and_repeatable(identity_engine):
    engine, evidence, first, second = identity_engine
    data = expense(engine, evidence, first)
    kwargs = dict(
        changes=[
            dict(
                subject_id="expense",
                expected_revision=1,
                action="reassign",
                data={**data, "counterparty_id": second},
            )
        ],
        evidence=[evidence],
        reason="same person, confirmed identity",
    )
    before = engine.trace(engine.preview(["expense"])["results"][0]["previous_calculation_id"])
    preview, result = confirm(engine, kwargs)
    assert result["status"] == "corrected"
    assert preview["results"][0]["mode"] == "open_replace"
    assert engine.trace(before["calculation"]["id"])["calculation"] == before["calculation"]
    assert (
        IdentityCorrections(engine).confirm_identity_correction(
            **kwargs,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="correction",
        )
        == result
    )
    kwargs["changes"][0].update(expected_revision=2, data=data)
    confirm(engine, kwargs, request="correct-again")
    with engine.store.connection(read_only=True) as connection:
        assert engine.store.current_fact(connection, "expense").fact.counterparty_id == first
        assert connection.execute("SELECT count(*) FROM identity_correction").fetchone()[0] == 2


def test_supersede_retains_history_and_cancels_only_duplicate(identity_engine):
    engine, evidence, first, second = identity_engine
    expense(engine, evidence, first, "duplicate", 100)
    expense(engine, evidence, second, "retained", 120)
    kwargs = dict(
        changes=[
            dict(
                subject_id="duplicate",
                expected_revision=1,
                action="supersede",
                replacement_subject_id="retained",
            )
        ],
        evidence=[evidence],
        reason="confirmed duplicate",
    )
    preview, _ = confirm(engine, kwargs)
    assert not preview["results"][0]["balances"]
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute("SELECT 1 FROM fact_current WHERE subject_id='duplicate'").fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM calculation WHERE subject_id='duplicate'"
            ).fetchone()[0]
            == 2
        )
    assert sum(r["credit"] for r in engine.overview("2026-01")["accounts"]) == 120


def test_correction_rolls_back_and_rejects_stale_preview(identity_engine):
    engine, evidence, first, second = identity_engine
    data = expense(engine, evidence, first)
    kwargs = dict(
        changes=[
            dict(
                subject_id="expense",
                expected_revision=1,
                action="reassign",
                data={**data, "counterparty_id": second},
            )
        ],
        evidence=[evidence],
        reason="confirmed",
    )
    command = IdentityCorrections(engine)
    preview = command.preview_identity_correction(**kwargs)
    with engine.store.connection(read_only=True) as connection:
        before = list(connection.iterdump())

    def fail(stage, connection):
        if stage == "published":
            raise RuntimeError("synthetic fault")

    engine.fault = fail
    with pytest.raises(RuntimeError):
        command.confirm_identity_correction(
            **kwargs,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="failed",
        )
    with engine.store.connection(read_only=True) as connection:
        assert list(connection.iterdump()) == before
    engine.fault = lambda *args: None
    Entities(engine).update_entity_profile(
        second,
        {"display_name": "changed"},
        source="synthetic",
        expected_revision=1,
        request_id="profile",
    )
    with pytest.raises(KernelError, match="已变化"):
        command.confirm_identity_correction(
            **kwargs, preview_digest=preview["digest"], epochs=preview["epochs"], request_id="stale"
        )


def test_closed_correction_preserves_frozen_manifest(identity_engine):
    from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods

    engine, evidence, first, second = identity_engine
    data = expense(engine, evidence, first)
    from test_materials import csv_spec

    from ai_accounting.kernel.materials import Materials

    proof = engine.register_evidence(
        b"name,amount,period\nexpense,1.00,2026-01\n",
        "text/csv",
        "expense.csv",
        request_id="expense-source",
    )["digest"]
    materials = Materials(engine)
    source = materials.receive(
        "source",
        dict(
            period="2026-01",
            evidence_digest=proof,
            category="transactions",
            purpose="business",
            specification=csv_spec(),
        ),
        evidence=(proof, evidence),
        expected_revision=0,
        request_id="material-source",
    )
    with engine.store.connection(read_only=True) as connection:
        fact = engine.store.current_fact(connection, "expense")
        calc = connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id='expense'"
        ).fetchone()[0]
    materials.resolve(
        "resolve",
        dict(
            period="2026-01",
            source_id="source",
            source_fact_id=source["fact_id"],
            location="CSV!B2",
            treatment="recognize",
            recognition_period="2026-01",
            links=[
                dict(
                    subject_id="expense",
                    fact_kind="expense",
                    fact_id=fact.id,
                    calculation_id=calc,
                    amount_field="fact.amount_fen",
                    amount_fen=100,
                    recognition_period="2026-01",
                )
            ],
        ),
        evidence=(evidence,),
        expected_revision=0,
        request_id="resolve",
    )
    periods = Periods(engine)
    for category in MATERIAL_CATEGORIES:
        active = category == "transactions"
        periods.inventory(
            "2026-01",
            category,
            evidence=[proof] if active else [],
            expected=int(active),
            no_business=not active,
            confirmation_evidence=evidence,
            request_id="inventory-" + category,
        )
    try:
        p = periods.preview_close("2026-01", owner_confirmation=evidence)
    except KernelError as exc:
        raise AssertionError(exc.details) from exc
    periods.close(
        "2026-01",
        owner_confirmation=evidence,
        preview_digest=p["digest"],
        epochs=p["epochs"],
        request_id="close",
    )
    frozen = periods.closed_report("2026-01")
    kwargs = dict(
        changes=[
            dict(
                subject_id="expense",
                expected_revision=1,
                action="reassign",
                data={**data, "counterparty_id": second},
            )
        ],
        evidence=[evidence],
        reason="confirmed",
        posting_period="2026-03",
    )
    preview, _ = confirm(engine, kwargs)
    assert preview["results"][0]["mode"] == "closed_correction"
    assert Periods(engine).closed_report("2026-01") == frozen


def opening_package(engine, evidence, members, period="2026-01", *, publish=True):
    from ai_accounting.kernel.domains.opening import CATEGORIES

    refs = []
    counts = dict.fromkeys(CATEGORIES.values(), 0)
    for kind, subject, fields in members:
        data = dict(period=period, package_id="opening", **fields)
        engine.save_fact(
            kind,
            subject,
            data,
            evidence=(evidence,),
            expected_revision=0,
            request_id="save-" + subject,
        )
        refs.append(dict(kind=kind, subject_id=subject))
        if kind == "opening_loan":
            refs[-1]["agreement_id"] = fields["agreement_id"]
        counts[CATEGORIES[kind]] += 1
    engine.save_fact(
        "opening_package",
        "opening",
        dict(
            period=period,
            package_id="opening",
            counts=counts,
            members=refs,
            completeness_confirmed=True,
        ),
        evidence=(evidence,),
        expected_revision=0,
        request_id="save-opening",
    )
    if not publish:
        return
    subjects = ["opening", *[subject for _, subject, _ in members]]
    p = engine.preview(subjects)
    engine.confirm(
        subjects, preview_digest=p["digest"], epochs=p["epochs"], request_id="publish-opening"
    )


@pytest.mark.parametrize("published", [False, True])
def test_unfrozen_opening_reassigns_fact_and_recomputes_package(identity_engine, published):
    engine, evidence, first, second = identity_engine
    data = dict(equity_kind="paid_in_capital", balance_fen=100, holder_or_basis_id=first)
    account = Entities(engine).register_entity(
        "fund_account", {}, account_type="cash", source="synthetic", request_id="opening-cash"
    )["entity_id"]
    target_account = Entities(engine).register_entity(
        "fund_account", {}, account_type="cash", source="synthetic", request_id="correct-cash"
    )["entity_id"]
    opening_package(
        engine,
        evidence,
        [
            ("opening_equity", "capital", data),
            (
                "opening_cash",
                "cash",
                dict(cash_account_id=account, balance_fen=100),
            ),
        ],
        publish=published,
    )
    preview, _ = confirm(
        engine,
        dict(
            changes=[
                dict(
                    subject_id="cash",
                    expected_revision=1,
                    action="reassign",
                    data=dict(
                        period="2026-01",
                        package_id="opening",
                        cash_account_id=target_account,
                        balance_fen=100,
                    ),
                )
            ],
            evidence=[evidence],
            reason="confirmed opening owner before first close",
        ),
    )
    assert preview["items"][0]["action"] == "reassign"
    assert {r["kind"] for r in preview["results"]} >= {"opening_package", "opening_cash"}
    with engine.store.connection(read_only=True) as connection:
        current = engine.store.current_fact(connection, "cash")
        assert current.revision == 2
        assert current.fact.cash_account_id == target_account
        assert (
            connection.execute(
                "SELECT count(*) FROM subject WHERE kind='opening_identity_binding'"
            ).fetchone()[0]
            == 0
        )


def test_frozen_fund_opening_reassigns_without_rewriting_economic_basis(identity_engine):
    from test_opening_continuation import _close_without_current_business

    from ai_accounting.kernel.identity_corrections import current_opening_bindings
    from ai_accounting.kernel.periods import Periods

    engine, evidence, first, _ = identity_engine
    entities = Entities(engine)
    accounts = [
        entities.register_entity(
            "fund_account",
            {},
            account_type="cash",
            source="synthetic",
            request_id="account-" + str(i),
        )["entity_id"]
        for i in range(3)
    ]
    data = dict(cash_account_id=accounts[0], balance_fen=10000)
    opening_package(
        engine,
        evidence,
        [
            ("opening_cash", "opening-bank", data),
            (
                "opening_equity",
                "capital",
                dict(equity_kind="paid_in_capital", balance_fen=10000, holder_or_basis_id=first),
            ),
        ],
    )
    _close_without_current_business(engine, "2026-01", evidence)
    frozen = Periods(engine).closed_report("2026-01")
    kwargs = dict(
        changes=[
            dict(
                subject_id="opening-bank",
                expected_revision=1,
                action="reassign",
                data=dict(period="2026-01", package_id="opening", **data),
            )
        ],
        evidence=[evidence],
        reason="confirmed same account",
        posting_period="2026-03",
    )
    kwargs["changes"][0]["data"]["cash_account_id"] = accounts[1]
    preview, _ = confirm(engine, kwargs)
    assert preview["results"][0]["kind"] == "opening_identity_binding"
    assert not preview["results"][0]["opening"]
    kwargs["changes"][0]["data"]["cash_account_id"] = accounts[2]
    again, _ = confirm(engine, kwargs, request="bank-again")
    assert again["results"][0]["mode"] == "open_replace"
    with engine.store.connection(read_only=True) as connection:
        assert (
            engine.store.current_fact(connection, "opening-bank").fact.cash_account_id
            == accounts[0]
        )
        values = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT balance_key,sum(amount) FROM period_balance "
                "WHERE category='cash' GROUP BY balance_key"
            )
        }
        assert values.get(accounts[0], 0) == 0
        assert values.get(accounts[1], 0) == 0
        assert values[accounts[2]] == 10000
        assert len(current_opening_bindings(connection)) == 1
    assert Periods(engine).closed_report("2026-01") == frozen
    kwargs["changes"][0]["data"]["cash_account_id"] = accounts[0]
    confirm(engine, kwargs, request="bank-back-to-original")
    with engine.store.connection(read_only=True) as connection:
        values = dict(
            connection.execute(
                "SELECT balance_key,sum(amount) FROM period_balance "
                "WHERE category='cash' GROUP BY balance_key"
            )
        )
        assert values[accounts[0]] == 10000
        assert values.get(accounts[2], 0) == 0


def save_model(engine, evidence, subject, fact):
    engine.save_fact(
        fact.kind,
        subject,
        fact.model_dump(mode="json"),
        evidence=(evidence,),
        expected_revision=0,
        request_id="save-" + subject,
    )


def publish_subjects(engine, subjects, request):
    p = engine.preview(subjects)
    return engine.confirm(
        subjects, preview_digest=p["digest"], epochs=p["epochs"], request_id=request
    )


@pytest.mark.parametrize("closed", [False, True])
def test_payroll_conflict_resolution_recomputes_later_cumulative_state(identity_engine, closed):
    from test_payroll import (
        bonus,
        bonus_policy,
        contribution_policy,
        income_tax_policy,
        opening,
        payroll,
        profile,
    )

    from ai_accounting.kernel.domains.payroll import (
        AnnualBonusOpeningUsage,
        PayrollWithholdingActual,
    )

    engine, evidence, first, second = identity_engine
    save_model(engine, evidence, "contributions", contribution_policy())
    save_model(engine, evidence, "income-tax", income_tax_policy())
    save_model(
        engine,
        evidence,
        "actual-tax",
        PayrollWithholdingActual(
            period="2026-01", employee_id=second, withheld_tax_fen=12345, withholding_confirmed=True
        ),
    )
    for suffix, person in (("a", first), ("b", second)):
        save_model(
            engine,
            evidence,
            "profile-" + suffix,
            profile(
                employee_id=person,
                social_insurance_participating=False,
                social_insurance_base_fen=None,
            ),
        )
        save_model(engine, evidence, "tax-opening-" + suffix, opening(employee_id=person))
    facts = {}
    for subject, person, profile_id, month, amount in (
        ("jan-a", first, "profile-a", "2026-01", 1000000),
        ("jan-b", second, "profile-b", "2026-01", 1200000),
        ("feb-b", second, "profile-b", "2026-02", 1300000),
    ):
        facts[subject] = payroll(
            employee_id=person,
            profile_id=profile_id,
            period=month,
            accounting_gross_salary_fen=amount,
            tax_reported_salary_fen=amount,
        )
        save_model(engine, evidence, subject, facts[subject])
        publish_subjects(engine, [subject], "publish-" + subject)
    save_model(engine, evidence, "bonus-policy", bonus_policy())
    save_model(
        engine,
        evidence,
        "bonus-usage",
        AnnualBonusOpeningUsage(
            period="2026-01", employee_id=second, separate_method_already_used=False
        ),
    )
    save_model(
        engine,
        evidence,
        "bonus",
        bonus(
            employee_id=second, bonus_fen=1000000, tax_method="combined", regular_payroll_id="jan-b"
        ),
    )
    publish_subjects(engine, ["bonus", "feb-b"], "publish-bonus")
    frozen = None
    if closed:
        frozen = close_calculated_payroll(engine, evidence, ["jan-a", "jan-b", "bonus"])
    corrected = facts["jan-b"].model_dump(mode="json") | dict(
        accounting_gross_salary_fen=1500000, tax_reported_salary_fen=1500000
    )
    kwargs = dict(
        changes=[
            dict(
                subject_id="jan-a",
                expected_revision=1,
                action="supersede",
                replacement_subject_id="jan-b",
            ),
            dict(subject_id="jan-b", expected_revision=1, action="reassign", data=corrected),
        ],
        evidence=[evidence],
        reason="verified sole monthly payroll and duplicate",
        posting_period="2026-03" if closed else None,
    )
    command = IdentityCorrections(engine)
    with pytest.raises(KernelError) as missing:
        command.preview_identity_correction(**(kwargs | {"changes": kwargs["changes"][:1]}))
    assert missing.value.code == "needs_information"
    with pytest.raises(KernelError) as wrong:
        command.preview_identity_correction(
            **(
                kwargs
                | {
                    "changes": [
                        dict(
                            subject_id="jan-b",
                            expected_revision=1,
                            action="supersede",
                            replacement_subject_id="jan-a",
                        ),
                        dict(
                            subject_id="jan-a",
                            expected_revision=1,
                            action="reassign",
                            data=corrected,
                        ),
                    ]
                }
            )
        )
    assert wrong.value.code == "identity_payroll_retention"
    assert wrong.value.details["payroll_resolution"]["retained_subject_id"] == "jan-b"
    third = Entities(engine).register_entity(
        "person", {}, source="synthetic", request_id="payroll-third"
    )["entity_id"]
    with pytest.raises(KernelError) as fallback:
        command.preview_identity_correction(
            **(
                kwargs
                | {
                    "changes": [
                        kwargs["changes"][0],
                        kwargs["changes"][1] | {"data": corrected | {"employee_id": third}},
                    ]
                }
            )
        )
    assert fallback.value.code == "identity_payroll_retention"
    assert fallback.value.details["payroll_resolution"]["retained_subject_id"] == "jan-a"
    assert fallback.value.details["payroll_resolution"]["selection"] == "subject_id"
    preview, _ = confirm(engine, kwargs)
    retention = preview["items"][0]["payroll_resolution"]
    assert retention["retained_subject_id"] == "jan-b"
    assert [c["subject_id"] for c in retention["candidates"]] == ["jan-a", "jan-b"]
    feb = next(r for r in preview["results"] if r["subject_id"] == "feb-b")
    assert feb["values"]["prior_tax_state"]["cumulative_income_fen"] == 2500000
    assert feb["values"]["tax_state"]["cumulative_income_fen"] == 3800000
    assert feb["posting_period"] == "2026-02"
    jan = next(r for r in preview["results"] if r["subject_id"] == "jan-b")
    assert jan["values"]["actual_withholding"]["withheld_tax_fen"] == 12345
    if closed:
        from ai_accounting.kernel.periods import Periods

        assert jan["mode"] == "closed_correction"
        assert jan["posting_period"] == "2026-03"
        assert Periods(engine).closed_report("2026-01") == frozen


def close_calculated_payroll(engine, evidence, subjects):
    import json

    from test_materials import csv_spec

    from ai_accounting.kernel.materials import Materials
    from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods

    with engine.store.connection(read_only=True) as connection:
        rows = [
            dict(
                connection.execute(
                    "SELECT c.* FROM calculation c JOIN calculation_current h "
                    "ON h.calculation_id=c.id WHERE h.subject_id=?",
                    (sid,),
                ).fetchone()
            )
            for sid in subjects
        ]
    csv_rows = [
        f"{r['subject_id']},{json.loads(r['outcome'])['values']['gross_fen'] // 100}.00,2026-01"
        for r in rows
    ]
    proof = engine.register_evidence(
        ("name,amount,period\n" + "\n".join(csv_rows) + "\n").encode(),
        "text/csv",
        "payroll.csv",
        request_id="payroll-proof",
    )["digest"]
    materials = Materials(engine)
    source = materials.receive(
        "payroll-source",
        dict(
            period="2026-01",
            evidence_digest=proof,
            category="payroll",
            purpose="business",
            specification=csv_spec(),
        ),
        evidence=(proof,),
        expected_revision=0,
        request_id="payroll-source",
    )
    for index, row in enumerate(rows, 2):
        materials.resolve(
            "payroll-resolution-" + str(index),
            dict(
                period="2026-01",
                source_id="payroll-source",
                source_fact_id=source["fact_id"],
                location=f"CSV!B{index}",
                treatment="recognize",
                recognition_period="2026-01",
                links=[
                    dict(
                        subject_id=row["subject_id"],
                        fact_kind=row["kind"],
                        fact_id=row["fact_id"],
                        calculation_id=row["id"],
                        amount_field="result.gross_fen",
                        amount_fen=json.loads(row["outcome"])["values"]["gross_fen"],
                        recognition_period="2026-01",
                    )
                ],
            ),
            evidence=(evidence,),
            expected_revision=0,
            request_id="payroll-resolution-" + str(index),
        )
    periods = Periods(engine)
    for category in MATERIAL_CATEGORIES:
        periods.inventory(
            "2026-01",
            category,
            evidence=[proof] if category == "payroll" else [],
            expected=1 if category == "payroll" else 0,
            no_business=category != "payroll",
            confirmation_evidence=evidence,
            request_id="payroll-inventory-" + category,
        )
    try:
        p = periods.preview_close("2026-01", owner_confirmation=evidence)
    except KernelError as exc:
        raise AssertionError(exc.details) from exc
    periods.close(
        "2026-01",
        owner_confirmation=evidence,
        preview_digest=p["digest"],
        epochs=p["epochs"],
        request_id="close-payroll",
    )
    return periods.closed_report("2026-01")


def test_frozen_payroll_opening_binding_feeds_new_person_cumulative(identity_engine):
    from test_opening_continuation import _close_without_current_business
    from test_payroll import contribution_policy, income_tax_policy, opening, payroll, profile

    from ai_accounting.kernel.domains.opening import OpeningPayrollState

    engine, evidence, first, second = identity_engine
    fields = opening(employee_id=first).model_dump(mode="json")
    fields.pop("period")
    fields["separate_method_already_used"] = False
    opening_package(engine, evidence, [("opening_payroll_state", "employee-opening", fields)])
    _close_without_current_business(engine, "2026-01", evidence)
    corrected = OpeningPayrollState(period="2026-01", package_id="opening", **fields).model_dump(
        mode="json"
    ) | dict(employee_id=second)
    confirm(
        engine,
        dict(
            changes=[
                dict(
                    subject_id="employee-opening",
                    expected_revision=1,
                    action="reassign",
                    data=corrected,
                )
            ],
            evidence=[evidence],
            reason="same employee",
            posting_period="2026-02",
        ),
    )
    save_model(engine, evidence, "contributions", contribution_policy())
    save_model(engine, evidence, "income-tax", income_tax_policy())
    save_model(
        engine,
        evidence,
        "profile",
        profile(
            employee_id=second,
            withholding_start_date="2026-02",
            social_insurance_participating=False,
            social_insurance_base_fen=None,
        ),
    )
    save_model(engine, evidence, "feb-payroll", payroll(employee_id=second, period="2026-02"))
    p = engine.preview(["feb-payroll"])
    assert p["results"][0]["values"]["employee_id"] == second
    assert p["results"][0]["values"]["tax_state"]["cumulative_income_fen"] == 1000000
    publish_subjects(engine, ["feb-payroll"], "publish-payroll")


def test_asset_identity_differs_from_acquisition_and_reassigns_owned_members(identity_engine):
    from ai_accounting.kernel.asset_batches import AssetBatches, frozen_members

    engine, evidence, supplier, _ = identity_engine
    entities = Entities(engine)
    assets = [
        entities.register_entity("asset", {}, source="synthetic", request_id=f"asset-{i}")[
            "entity_id"
        ]
        for i in range(2)
    ]
    acquisition = dict(
        period="2026-01",
        asset_id=assets[0],
        asset_type="fixed",
        acquisition_date="2026-01-02",
        supplier_id=supplier,
        cost_fen=12000,
        acquisition_basis="direct_purchase",
    )
    engine.save_fact(
        "asset",
        "purchase-business",
        acquisition,
        evidence=(evidence,),
        expected_revision=0,
        request_id="purchase",
    )
    publish_subjects(engine, ["purchase-business"], "publish-purchase")
    batches = AssetBatches(engine)
    activation = dict(
        period="2026-01",
        asset_id=assets[0],
        in_use_date="2026-01-03",
        useful_life_months=3,
        residual_fen=0,
        benefit_area="administration",
        rounding_policy="floor_final_remainder",
    )
    kwargs = dict(
        subject_id="activation-owner",
        period="2026-01",
        members=[dict(subject_id="activation-card", expected_revision=0, data=activation)],
        evidence=(evidence,),
        expected_revision=0,
    )
    p = batches.prepare_activation_batch(**kwargs)
    batches.confirm_activation_batch(
        **kwargs, preview_digest=p["digest"], epochs=p["epochs"], request_id="activate"
    )
    for period in ("2026-01", "2026-02"):
        kwargs = dict(period=period, evidence=(evidence,), expected_revision=0)
        p = batches.prepare_consumption_month(**kwargs)
        batches.confirm_consumption_month(
            **kwargs, preview_digest=p["digest"], epochs=p["epochs"], request_id="consume-" + period
        )
    with engine.store.connection(read_only=True) as connection:
        subjects = [
            r[0]
            for r in connection.execute(
                "SELECT subject_id FROM fact_current JOIN subject ON subject.id=subject_id "
                "WHERE kind IN ('asset','asset_activation','asset_consumption')"
            )
        ]
        changes = [
            dict(
                subject_id=sid,
                expected_revision=1,
                action="reassign",
                data=engine.store.current_fact(connection, sid).fact.model_dump(mode="json")
                | dict(asset_id=assets[1]),
            )
            for sid in subjects
        ]
    p, result = confirm(
        engine, dict(changes=changes, evidence=[evidence], reason="same physical asset")
    )
    assert {r["kind"] for r in p["results"]} >= {
        "asset",
        "asset_activation",
        "asset_activation_batch",
        "asset_consumption",
        "asset_consumption_month",
    }
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM calculation_publication p "
                "JOIN calculation c ON c.id=p.calculation_id "
                "WHERE c.kind IN ('asset_activation','asset_consumption')"
            ).fetchone()[0]
            == 0
        )
        for result_item in result["results"]:
            if result_item["subject_id"] == "activation-owner":
                assert (
                    frozen_members(connection, result_item["calculation_id"])[0]["asset_id"]
                    == assets[1]
                )
        balances = dict(
            connection.execute(
                "SELECT balance_key,sum(amount) FROM period_balance "
                "WHERE category='asset' GROUP BY balance_key"
            )
        )
        assert balances.get(f"asset:{assets[0]}:carrying", 0) == 0
        assert balances[f"asset:{assets[1]}:carrying"] == 8000


def test_frozen_asset_opening_binding_continues_and_can_be_reassigned_back(identity_engine):

    from ai_accounting.kernel.asset_batches import AssetBatches
    from ai_accounting.kernel.periods import Periods

    engine, evidence, owner, _ = identity_engine
    entities = Entities(engine)
    assets = [
        entities.register_entity("asset", {}, source="synthetic", request_id=f"asset-{i}")[
            "entity_id"
        ]
        for i in range(2)
    ]
    fields = dict(
        asset_id=assets[0],
        asset_type="fixed",
        cost_fen=12000,
        accumulated_fen=4000,
        in_use_date="2025-11-01",
        useful_life_months=3,
        completed_months=1,
        residual_fen=0,
        benefit_area="administration",
        rounding_policy="floor_final_remainder",
    )
    opening_package(
        engine,
        evidence,
        [
            ("opening_asset", "original-card", fields),
            (
                "opening_equity",
                "capital",
                dict(equity_kind="paid_in_capital", balance_fen=8000, holder_or_basis_id=owner),
            ),
        ],
    )
    batches = AssetBatches(engine)
    kwargs = dict(period="2026-01", evidence=(evidence,), expected_revision=0)
    p = batches.prepare_consumption_month(**kwargs)
    batches.confirm_consumption_month(
        **kwargs, preview_digest=p["digest"], epochs=p["epochs"], request_id="consume-january"
    )
    from test_materials import csv_spec

    from ai_accounting.kernel.materials import Materials
    from ai_accounting.kernel.periods import MATERIAL_CATEGORIES

    proof = engine.register_evidence(
        b"name,amount,period\ndepreciation,40.00,2026-01\n",
        "text/csv",
        "depreciation.csv",
        request_id="depreciation-evidence",
    )["digest"]
    materials = Materials(engine)
    source = materials.receive(
        "depreciation-source",
        dict(
            period="2026-01",
            evidence_digest=proof,
            category="assets",
            purpose="business",
            specification=csv_spec(),
        ),
        evidence=(proof,),
        expected_revision=0,
        request_id="depreciation-source",
    )
    owner_row = next(r for r in p["results"] if r["kind"] == "asset_consumption_month")
    materials.resolve(
        "depreciation-resolution",
        dict(
            period="2026-01",
            source_id="depreciation-source",
            source_fact_id=source["fact_id"],
            location="CSV!B2",
            treatment="recognize",
            recognition_period="2026-01",
            links=[
                dict(
                    subject_id=owner_row["subject_id"],
                    fact_kind="asset_consumption_month",
                    fact_id=owner_row["fact_id"],
                    calculation_id=owner_row["calculation_id"],
                    amount_field="result.consumption_fen",
                    amount_fen=4000,
                    recognition_period="2026-01",
                )
            ],
        ),
        evidence=(evidence,),
        expected_revision=0,
        request_id="depreciation-resolution",
    )
    periods = Periods(engine)
    for category in MATERIAL_CATEGORIES:
        periods.inventory(
            "2026-01",
            category,
            evidence=[proof] if category == "assets" else [],
            expected=int(category == "assets"),
            no_business=category != "assets",
            confirmation_evidence=evidence,
            request_id="inventory-" + category,
        )
    try:
        p = periods.preview_close("2026-01", owner_confirmation=evidence)
    except KernelError as exc:
        raise AssertionError(exc.details) from exc
    periods.close(
        "2026-01",
        owner_confirmation=evidence,
        preview_digest=p["digest"],
        epochs=p["epochs"],
        request_id="close-january",
    )
    frozen = Periods(engine).closed_report("2026-01")
    with engine.store.connection(read_only=True) as connection:
        member_subject = connection.execute(
            "SELECT id FROM subject WHERE kind='asset_consumption'"
        ).fetchone()[0]
        member = engine.store.current_fact(connection, member_subject)
    corrected = dict(period="2026-01", package_id="opening", **fields) | dict(asset_id=assets[1])
    changes = [
        dict(subject_id="original-card", expected_revision=1, action="reassign", data=corrected),
        dict(
            subject_id=member_subject,
            expected_revision=1,
            action="reassign",
            data=member.fact.model_dump(mode="json") | dict(asset_id=assets[1]),
        ),
    ]
    p, _ = confirm(
        engine,
        dict(
            changes=changes,
            evidence=[evidence],
            reason="same asset identity",
            posting_period="2026-02",
        ),
    )
    assert any(r["kind"] == "opening_identity_binding" for r in p["results"])
    assert Periods(engine).closed_report("2026-01") == frozen

    kwargs = dict(period="2026-02", evidence=(evidence,), expected_revision=0)
    p = batches.prepare_consumption_month(**kwargs)
    owner = next(r for r in p["results"] if r["kind"] == "asset_consumption_month")
    assert owner["values"]["members"][0]["asset_id"] == assets[1]
    assert owner["values"]["consumption_fen"] == 4000
    batches.confirm_consumption_month(
        **kwargs, preview_digest=p["digest"], epochs=p["epochs"], request_id="consume-february"
    )


def test_fact_only_identity_change_and_supersession_cycle(identity_engine):
    from test_payroll import profile

    engine, evidence, first, second = identity_engine
    fact = profile(employee_id=first)
    save_model(engine, evidence, "profile", fact)
    p, _ = confirm(
        engine,
        dict(
            changes=[
                dict(
                    subject_id="profile",
                    expected_revision=1,
                    action="reassign",
                    data=fact.model_dump(mode="json") | dict(employee_id=second),
                )
            ],
            evidence=[evidence],
            reason="confirmed employment identity",
        ),
    )
    assert p["results"] == []
    expense(engine, evidence, first, "one")
    expense(engine, evidence, second, "two")
    with pytest.raises(KernelError, match="不能同时被替代"):
        IdentityCorrections(engine).preview_identity_correction(
            changes=[
                dict(
                    subject_id="one",
                    expected_revision=1,
                    action="supersede",
                    replacement_subject_id="two",
                ),
                dict(
                    subject_id="two",
                    expected_revision=1,
                    action="supersede",
                    replacement_subject_id="one",
                ),
            ],
            evidence=[evidence],
            reason="invalid cycle",
        )


def test_frozen_obligation_binding_payment_uses_exact_binding_and_retains_cash(identity_engine):
    from test_opening_continuation import _close_without_current_business

    from ai_accounting.kernel.periods import Periods

    engine, evidence, first, second = identity_engine
    account = Entities(engine).register_entity(
        "fund_account", {}, account_type="cash", source="synthetic", request_id="cash"
    )["entity_id"]
    fields = dict(
        counterparty_id=first,
        nature="customer_receivable",
        outstanding_fen=20000,
        business_reference="confirmed-invoice",
    )
    opening_package(
        engine,
        evidence,
        [
            ("opening_obligation", "receivable", fields),
            (
                "opening_equity",
                "capital",
                dict(equity_kind="retained_earnings", balance_fen=20000, holder_or_basis_id=first),
            ),
        ],
    )
    _close_without_current_business(engine, "2026-01", evidence)
    frozen = Periods(engine).closed_report("2026-01")
    kwargs = dict(
        changes=[
            dict(
                subject_id="receivable",
                expected_revision=1,
                action="reassign",
                data=dict(period="2026-01", package_id="opening", **fields)
                | dict(counterparty_id=second),
            )
        ],
        evidence=[evidence],
        reason="same customer",
        posting_period="2026-02",
    )
    p, _ = confirm(engine, kwargs)
    binding_id = next(
        r["calculation_id"] for r in p["results"] if r["kind"] == "opening_identity_binding"
    )
    payment = dict(
        period="2026-02",
        actual_date="2026-02-05",
        direction="inflow",
        cash_account_id=account,
        counterparty_id=second,
        amount_fen=12000,
        allocations=[
            dict(
                source_kind="opening_obligation",
                source_id="receivable",
                obligation="primary",
                amount_fen=12000,
            )
        ],
    )
    engine.save_fact(
        "cash_payment",
        "receipt",
        payment,
        evidence=(evidence,),
        expected_revision=0,
        request_id="receipt",
    )
    p = engine.preview(["receipt"])
    assert p["results"][0]["values"]["settlements"][0]["binding_calculation_id"] == binding_id
    publish_subjects(engine, ["receipt"], "publish-receipt")
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT sum(amount) FROM period_balance WHERE category='cash' AND balance_key=?",
                (account,),
            ).fetchone()[0]
            == 12000
        )
    assert Periods(engine).closed_report("2026-01") == frozen
    with pytest.raises(KernelError, match="真实资金流水不能"):
        IdentityCorrections(engine).preview_identity_correction(
            changes=[
                dict(
                    subject_id="receipt",
                    expected_revision=1,
                    action="supersede",
                    replacement_subject_id="receivable",
                )
            ],
            evidence=[evidence],
            reason="must preserve cash",
        )


def test_superseded_business_can_be_restored_by_exact_reassign(identity_engine):
    from ai_accounting.kernel.identity_corrections import superseded_subject_ids

    engine, evidence, first, second = identity_engine
    original = expense(engine, evidence, first, "discarded", 100)
    expense(engine, evidence, second, "retained", 120)
    confirm(
        engine,
        dict(
            changes=[
                dict(
                    subject_id="discarded",
                    expected_revision=1,
                    action="supersede",
                    replacement_subject_id="retained",
                )
            ],
            evidence=[evidence],
            reason="first evidence indicated duplicate",
            entity_resolution=dict(source_entity_id=first, target_entity_id=second),
        ),
    )
    p, _ = confirm(
        engine,
        dict(
            changes=[
                dict(subject_id="discarded", expected_revision=1, action="reassign", data=original)
            ],
            evidence=[evidence],
            reason="new evidence proves independent original business",
        ),
        request="restore",
    )
    assert p["items"][0]["reinstated"] is True
    with engine.store.connection(read_only=True) as connection:
        assert "discarded" not in superseded_subject_ids(connection)
        assert engine.store.current_fact(connection, "discarded").revision == 2
    assert sum(r["credit"] for r in engine.overview("2026-01")["accounts"]) == 220


def test_correction_cannot_claim_unrelated_entity_resolution(identity_engine):
    engine, evidence, first, second = identity_engine
    third = Entities(engine).register_entity(
        "person", {}, source="synthetic", request_id="unrelated"
    )["entity_id"]
    data = expense(engine, evidence, first)
    with pytest.raises(KernelError, match="本次"):
        IdentityCorrections(engine).preview_identity_correction(
            changes=[
                dict(
                    subject_id="expense",
                    expected_revision=1,
                    action="reassign",
                    data=data | dict(counterparty_id=second),
                )
            ],
            evidence=[evidence],
            reason="identity confirmation",
            entity_resolution=dict(source_entity_id=first, target_entity_id=third),
        )


def test_identity_reduction_preserves_actual_payment_and_requires_exact_recovery(identity_engine):
    engine, evidence, first, second = identity_engine
    original = expense(engine, evidence, first)
    account = Entities(engine).register_entity(
        "fund_account", {}, account_type="cash", source="synthetic", request_id="cash"
    )["entity_id"]
    payment = dict(
        period="2026-01",
        actual_date="2026-01-03",
        direction="outflow",
        cash_account_id=account,
        counterparty_id=first,
        amount_fen=100,
        allocations=[
            dict(source_kind="expense", source_id="expense", obligation="primary", amount_fen=100)
        ],
    )
    engine.save_fact(
        "cash_payment",
        "payment",
        payment,
        evidence=(evidence,),
        expected_revision=0,
        request_id="payment",
    )
    publish_subjects(engine, ["payment"], "pay")
    kwargs = dict(
        changes=[
            dict(
                subject_id="expense",
                expected_revision=1,
                action="reassign",
                data=original | dict(counterparty_id=second, amount_fen=80),
            ),
            dict(
                subject_id="payment",
                expected_revision=1,
                action="reassign",
                data=payment | dict(counterparty_id=second),
            ),
        ],
        evidence=[evidence],
        reason="correct obligation, unchanged real cash",
    )
    with pytest.raises(KernelError):
        IdentityCorrections(engine).preview_identity_correction(**kwargs)
    recovery = dict(
        period="2026-01",
        source_kind="expense",
        source_id="expense",
        obligation_name="primary",
        counterparty_id=second,
        amount_fen=20,
        recovery_right_confirmed=True,
    )
    engine.save_fact(
        "overpayment",
        "recovery",
        recovery,
        evidence=(evidence,),
        expected_revision=0,
        request_id="recovery",
    )
    p, _ = confirm(engine, kwargs)
    assert (
        next(r for r in p["results"] if r["subject_id"] == "recovery")["values"]["overpayment_fen"]
        == 20
    )
    with engine.store.connection(read_only=True) as connection:
        assert engine.store.current_fact(connection, "payment").fact.amount_fen == 100
        assert (
            connection.execute(
                "SELECT sum(amount) FROM period_balance WHERE category='cash' AND balance_key=?",
                (account,),
            ).fetchone()[0]
            == -100
        )


def test_correction_approval_binds_repair_revision_and_exact_result(identity_engine):
    from ai_accounting.kernel.identity_corrections import verify_identity_corrections

    engine, evidence, first, second = identity_engine
    data = expense(engine, evidence, first)
    kwargs = dict(
        changes=[
            dict(
                subject_id="expense",
                expected_revision=1,
                action="reassign",
                data=data | dict(counterparty_id=second),
            )
        ],
        evidence=[evidence],
        reason="confirmed scope",
    )
    api = IdentityCorrections(engine)
    p = api.preview_identity_correction(**kwargs)
    with engine.store.connection() as connection:
        connection.execute("UPDATE state SET read_repair_revision=read_repair_revision+1")
        connection.commit()
    with pytest.raises(KernelError, match="变化"):
        api.confirm_identity_correction(
            **kwargs, preview_digest=p["digest"], epochs=p["epochs"], request_id="stale-repair"
        )
    confirm(engine, kwargs)
    with engine.store.connection() as connection:
        connection.execute("DROP TRIGGER identity_correction_item_immutable")
        connection.execute("UPDATE identity_correction_item SET calculation_id=NULL")
        connection.commit()
        with pytest.raises(KernelError, match="采用计算"):
            verify_identity_corrections(engine, connection)


def test_frozen_opening_supersession_requires_balanced_named_details_and_is_reversible(
    identity_engine,
):
    from test_opening_continuation import _close_without_current_business

    from ai_accounting.kernel.periods import Periods

    engine, evidence, first, second = identity_engine
    entities = Entities(engine)
    accounts = [
        entities.register_entity(
            "fund_account", {}, account_type="cash", source="synthetic", request_id=f"cash-{i}"
        )["entity_id"]
        for i in range(3)
    ]
    opening_package(
        engine,
        evidence,
        [
            ("opening_cash", "cash-a", dict(cash_account_id=accounts[0], balance_fen=100)),
            ("opening_cash", "cash-b", dict(cash_account_id=accounts[1], balance_fen=120)),
            (
                "opening_equity",
                "capital-a",
                dict(equity_kind="paid_in_capital", balance_fen=100, holder_or_basis_id=first),
            ),
            (
                "opening_equity",
                "capital-b",
                dict(equity_kind="paid_in_capital", balance_fen=120, holder_or_basis_id=second),
            ),
        ],
    )
    _close_without_current_business(engine, "2026-01", evidence)
    frozen = Periods(engine).closed_report("2026-01")
    confirm(
        engine,
        dict(
            changes=[
                dict(
                    subject_id="cash-b",
                    expected_revision=1,
                    action="reassign",
                    data=dict(
                        period="2026-01",
                        package_id="opening",
                        cash_account_id=accounts[2],
                        balance_fen=120,
                    ),
                )
            ],
            evidence=[evidence],
            reason="retained account identity already corrected",
            posting_period="2026-03",
        ),
        request="rebind-retained",
    )
    changes = [
        dict(
            subject_id="cash-a",
            expected_revision=1,
            action="supersede",
            replacement_subject_id="cash-b",
        )
    ]
    with pytest.raises(KernelError, match="未平衡"):
        IdentityCorrections(engine).preview_identity_correction(
            changes=changes,
            evidence=[evidence],
            reason="confirmed duplicate cash",
            posting_period="2026-03",
        )
    changes.append(
        dict(
            subject_id="capital-a",
            expected_revision=1,
            action="supersede",
            replacement_subject_id="capital-b",
        )
    )
    p, _ = confirm(
        engine,
        dict(
            changes=changes,
            evidence=[evidence],
            reason="confirmed duplicate cash and capital basis",
            posting_period="2026-03",
        ),
    )
    owner = next(r for r in p["results"] if r["kind"] == "opening_basis_correction")
    assert sum(line["debit"] for line in owner["lines"]) == 100
    assert owner["posting_period"] == "2026-03"
    retained_data = dict(
        period="2026-01", package_id="opening", cash_account_id=accounts[1], balance_fen=120
    )
    confirm(
        engine,
        dict(
            changes=[
                dict(
                    subject_id="cash-b", expected_revision=1, action="reassign", data=retained_data
                )
            ],
            evidence=[evidence],
            reason="correct retained account again",
            posting_period="2026-03",
        ),
        request="retained-again",
    )
    with engine.store.connection(read_only=True) as connection:
        assert engine.store.current_fact(connection, "cash-a").fact.balance_fen == 100
        original_fact_id = engine.store.current_fact(connection, "cash-a").id
        assert (
            connection.execute(
                "SELECT entity_id FROM entity_reference_current "
                "WHERE fact_id=? AND path='cash_account_id'",
                (original_fact_id,),
            ).fetchone()[0]
            == accounts[1]
        )
        assert (
            connection.execute(
                "SELECT entity_id FROM entity_reference_recorded "
                "WHERE fact_id=? AND path='cash_account_id'",
                (original_fact_id,),
            ).fetchone()[0]
            == accounts[0]
        )
        assert (
            connection.execute(
                "SELECT sum(amount) FROM period_balance WHERE category='cash' AND balance_key=?",
                (accounts[0],),
            ).fetchone()[0]
            == 0
        )
        changes = [
            dict(
                subject_id=sid,
                expected_revision=1,
                action="reassign",
                data=engine.store.current_fact(connection, sid).fact.model_dump(mode="json"),
            )
            for sid in ("cash-a", "capital-a")
        ]
    confirm(
        engine,
        dict(
            changes=changes,
            evidence=[evidence],
            reason="later proof restores both exact original details",
            posting_period="2026-03",
        ),
        request="restore-opening",
    )
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT sum(amount) FROM period_balance WHERE category='cash' AND balance_key=?",
                (accounts[0],),
            ).fetchone()[0]
            == 100
        )
    assert Periods(engine).closed_report("2026-01") == frozen
    confirm(
        engine,
        dict(
            changes=[
                dict(
                    subject_id="cash-b",
                    expected_revision=1,
                    action="reassign",
                    data=retained_data | dict(cash_account_id=accounts[2]),
                )
            ],
            evidence=[evidence],
            reason="later retained account correction leaves restored scope independent",
            posting_period="2026-03",
        ),
        request="retained-after-restore",
    )
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT entity_id FROM entity_reference_current "
                "WHERE fact_id=? AND path='cash_account_id'",
                (original_fact_id,),
            ).fetchone()[0]
            == accounts[0]
        )


def test_conflicting_frozen_payroll_bases_choose_one_without_adding_cumulative_amounts(
    identity_engine,
):
    from test_opening_continuation import _close_without_current_business
    from test_payroll import contribution_policy, income_tax_policy, opening, payroll, profile

    engine, evidence, first, second = identity_engine
    members = []
    for sid, person, amount in (("basis-a", first, 1000000), ("basis-b", second, 2000000)):
        fields = opening(
            employee_id=person,
            period="2026-04",
            through_period="2026-03",
            cumulative_income_fen=amount,
        ).model_dump(mode="json")
        fields.pop("period")
        fields["separate_method_already_used"] = False
        members.append(("opening_payroll_state", sid, fields))
    opening_package(engine, evidence, members, period="2026-04")
    _close_without_current_business(engine, "2026-04", evidence)
    p, _ = confirm(
        engine,
        dict(
            changes=[
                dict(
                    subject_id="basis-a",
                    expected_revision=1,
                    action="supersede",
                    replacement_subject_id="basis-b",
                )
            ],
            evidence=[evidence],
            reason="retained original cumulative basis explicitly verified",
            posting_period="2026-05",
        ),
    )
    owner = next(r for r in p["results"] if r["kind"] == "opening_basis_correction")
    assert not owner["lines"] and not owner["balances"]
    save_model(engine, evidence, "contributions", contribution_policy())
    save_model(engine, evidence, "income-tax", income_tax_policy())
    save_model(
        engine,
        evidence,
        "profile",
        profile(
            employee_id=second,
            withholding_start_date="2026-04",
            social_insurance_participating=False,
            social_insurance_base_fen=None,
        ),
    )
    save_model(
        engine, evidence, "late-april-payroll", payroll(employee_id=second, period="2026-04")
    )
    p = engine.preview(["late-april-payroll"], posting_period="2026-05")
    assert p["results"][0]["values"]["tax_state"]["cumulative_income_fen"] == 3000000
    engine.confirm(
        ["late-april-payroll"],
        posting_period="2026-05",
        preview_digest=p["digest"],
        epochs=p["epochs"],
        request_id="publish-late-april",
    )


def test_opening_asset_economic_supersession_rebuilds_month_owner_without_member_voucher(
    identity_engine,
):
    from ai_accounting.kernel.asset_batches import AssetBatches

    engine, evidence, first, second = identity_engine
    entities = Entities(engine)
    assets = [
        entities.register_entity("asset", {}, source="synthetic", request_id=f"asset-{i}")[
            "entity_id"
        ]
        for i in range(2)
    ]
    members = []
    for suffix, asset, party, cost, accumulated in (
        ("a", assets[0], first, 12000, 4000),
        ("b", assets[1], second, 15000, 5000),
    ):
        members.append(
            (
                "opening_asset",
                "card-" + suffix,
                dict(
                    asset_id=asset,
                    asset_type="fixed",
                    cost_fen=cost,
                    accumulated_fen=accumulated,
                    in_use_date="2025-11-01",
                    useful_life_months=3,
                    completed_months=1,
                    residual_fen=0,
                    benefit_area="administration",
                    rounding_policy="floor_final_remainder",
                ),
            )
        )
        members.append(
            (
                "opening_equity",
                "capital-" + suffix,
                dict(
                    equity_kind="paid_in_capital",
                    balance_fen=cost - accumulated,
                    holder_or_basis_id=party,
                ),
            )
        )
    opening_package(engine, evidence, members)
    batches = AssetBatches(engine)
    kwargs = dict(period="2026-01", evidence=(evidence,), expected_revision=0)
    p = batches.prepare_consumption_month(**kwargs)
    batches.confirm_consumption_month(
        **kwargs, preview_digest=p["digest"], epochs=p["epochs"], request_id="january"
    )
    by_asset = {
        r["values"]["asset_id"]: r["subject_id"]
        for r in p["results"]
        if r["kind"] == "asset_consumption"
    }
    changes = [
        dict(
            subject_id=source,
            expected_revision=1,
            action="supersede",
            replacement_subject_id=target,
        )
        for source, target in (
            ("card-a", "card-b"),
            ("capital-a", "capital-b"),
            (by_asset[assets[0]], by_asset[assets[1]]),
        )
    ]
    p, _ = confirm(
        engine,
        dict(
            changes=changes,
            evidence=[evidence],
            reason="one physical asset with the retained economic basis",
        ),
    )
    owner = next(r for r in p["results"] if r["kind"] == "asset_consumption_month")
    assert owner["values"]["member_count"] == 1
    assert owner["values"]["consumption_fen"] == 5000
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM calculation_publication p "
                "JOIN calculation c ON c.id=p.calculation_id WHERE c.kind='asset_consumption'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT sum(amount) FROM period_balance WHERE category='asset' AND balance_key=?",
                (f"asset:{assets[0]}:carrying",),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT sum(amount) FROM period_balance WHERE category='asset' AND balance_key=?",
                (f"asset:{assets[1]}:carrying",),
            ).fetchone()[0]
            == 5000
        )
    p = batches.prepare_consumption_month(
        period="2026-02", evidence=(evidence,), expected_revision=0
    )
    assert (
        next(r for r in p["results"] if r["kind"] == "asset_consumption_month")["values"][
            "consumption_fen"
        ]
        == 5000
    )
    with engine.store.connection(read_only=True) as connection:
        restore = []
        for sid in ("card-a", "capital-a", by_asset[assets[0]]):
            row = connection.execute(
                "SELECT id FROM fact_revision WHERE subject_id=? ORDER BY revision DESC LIMIT 1",
                (sid,),
            ).fetchone()
            fact = engine.store.fact(connection, row[0])
            restore.append(
                dict(
                    subject_id=sid,
                    expected_revision=fact.revision,
                    action="reassign",
                    data=fact.fact.model_dump(mode="json"),
                )
            )
    p, _ = confirm(
        engine,
        dict(
            changes=restore,
            evidence=[evidence],
            reason="new explicit evidence restores original card and economic basis",
        ),
        request="restore-asset",
    )
    owner = next(r for r in p["results"] if r["kind"] == "asset_consumption_month")
    assert owner["values"]["member_count"] == 2
    assert owner["values"]["consumption_fen"] == 9000


def test_opening_loan_binding_is_used_by_subsequent_interest(identity_engine):
    engine, evidence, first, second = identity_engine
    account = Entities(engine).register_entity(
        "fund_account", {}, account_type="cash", source="synthetic", request_id="cash"
    )["entity_id"]
    agreement = dict(
        period="2026-01",
        lender_id=first,
        lender_is_licensed=True,
        currency="CNY",
        annual_rate_percent="12",
        day_count_basis="actual_360",
        maturity_date="2027-01-01",
        loan_term="short_term",
    )
    engine.save_fact(
        "loan_agreement",
        "agreement",
        agreement,
        evidence=(evidence,),
        expected_revision=0,
        request_id="agreement",
    )
    loan = dict(
        agreement_id="agreement",
        lender_id=first,
        loan_term="short_term",
        principal_fen=120000,
        accrued_interest_fen=0,
        interest_start="2026-01-01",
    )
    opening_package(
        engine,
        evidence,
        [
            ("opening_loan", "principal", loan),
            ("opening_cash", "cash-opening", dict(cash_account_id=account, balance_fen=120000)),
        ],
    )
    confirm(
        engine,
        dict(
            changes=[
                dict(
                    subject_id="agreement",
                    expected_revision=1,
                    action="reassign",
                    data=agreement | dict(lender_id=second),
                ),
                dict(
                    subject_id="principal",
                    expected_revision=1,
                    action="reassign",
                    data=dict(period="2026-01", package_id="opening", **loan)
                    | dict(lender_id=second),
                ),
            ],
            evidence=[evidence],
            reason="same lending organization",
        ),
    )
    engine.save_fact(
        "loan_interest",
        "interest",
        dict(
            period="2026-01",
            agreement_id="agreement",
            drawdown_id="principal",
            period_start="2026-01-01",
            period_end_exclusive="2026-02-01",
        ),
        evidence=(evidence,),
        expected_revision=0,
        request_id="interest",
    )
    p = engine.preview(["interest"])
    assert p["results"][0]["values"]["obligations"][0]["counterparty_id"] == second
    publish_subjects(engine, ["interest"], "publish-interest")


def test_opening_fund_product_binding_preserves_exact_redemption_cost(identity_engine):
    engine, evidence, owner, settlement_party = identity_engine
    entities = Entities(engine)
    funds = [
        entities.register_entity("fund_product", {}, source="synthetic", request_id=f"fund-{i}")[
            "entity_id"
        ]
        for i in range(2)
    ]
    classification = "readily_redeemable_held_not_over_one_year"
    fields = dict(
        fund_id=funds[0],
        original_lot_reference="verified-lot",
        classification=classification,
        cost_basis="documented_cost",
        cost_fen=10000,
    )
    opening_package(
        engine,
        evidence,
        [
            ("opening_money_fund", "lot", fields),
            (
                "opening_equity",
                "capital",
                dict(equity_kind="paid_in_capital", balance_fen=10000, holder_or_basis_id=owner),
            ),
        ],
    )
    confirm(
        engine,
        dict(
            changes=[
                dict(
                    subject_id="lot",
                    expected_revision=1,
                    action="reassign",
                    data=dict(period="2026-01", package_id="opening", **fields)
                    | dict(fund_id=funds[1]),
                )
            ],
            evidence=[evidence],
            reason="same specific fund product",
        ),
    )
    engine.save_fact(
        "money_fund_redemption",
        "redemption",
        dict(
            period="2026-02",
            fund_id=funds[1],
            counterparty_id=settlement_party,
            classification=classification,
            cost_basis="documented_cost",
            costs=[dict(source_kind="opening_money_fund", source_id="lot", cost_fen=5000)],
            net_proceeds_fen=5100,
        ),
        evidence=(evidence,),
        expected_revision=0,
        request_id="redemption",
    )
    p = engine.preview(["redemption"])
    assert p["results"][0]["values"]["cost_fen"] == 5000
    publish_subjects(engine, ["redemption"], "publish-redemption")
