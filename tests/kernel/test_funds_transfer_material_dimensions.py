"""Two bank originals cover one internal transfer without doubling its accounting."""

from io import BytesIO

import pytest
from entity_fixture import seed_fact_entities
from pydantic import ValidationError
from test_materials import Company, codes

from ai_accounting.kernel.contracts import Calculation, KernelError, Read
from ai_accounting.kernel.domains.transactions import FundsTransfer
from ai_accounting.kernel.materials import _amount_basis
from ai_accounting.kernel.types import canonical

AMOUNT = 15_000_000


def transfer_fact(day="2026-01-08"):
    return FundsTransfer.model_validate_json(
        canonical(
            {
                "period": "2026-01",
                "actual_date": day,
                "source_bank_account_id": "bank-source",
                "destination_bank_account_id": "bank-destination",
                "amount_fen": AMOUNT,
            }
        )
    )


def publish_transfer(company, subject="transfer", day="2026-01-08"):
    fact = transfer_fact(day)
    seed_fact_entities(company.engine, fact)
    company.engine.save_fact(
        fact.kind,
        subject,
        fact.model_dump(mode="json"),
        evidence=(company.proof,),
        expected_revision=0,
        request_id=company.request(),
    )
    preview = company.engine.preview([subject])
    result = company.engine.confirm(
        [subject],
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id=company.request(),
    )["results"][0]
    return {"calculation_id": result["calculation_id"]}


def bank_original(company, subject, direction, *, positive_columns=False, xls=False):
    if positive_columns:
        rows = [
            ["name", "incoming", "outgoing", "period"],
            [
                subject,
                150000 if direction == "inflow" else 0,
                150000 if direction == "outflow" else 0,
                "2026-01",
            ],
        ]
        columns = [
            {"column": "A", "role": "context"},
            {"column": "B", "role": "amount", "funds_direction": "inflow"},
            {"column": "C", "role": "amount", "funds_direction": "outflow"},
            {"column": "D", "role": "recognition_period"},
        ]
        used, unused = ("B", "C") if direction == "inflow" else ("C", "B")
        amount = AMOUNT
    else:
        amount = AMOUNT if direction == "inflow" else -AMOUNT
        rows = [["name", "amount", "period"], [subject, amount // 100, "2026-01"]]
        columns = [
            {"column": "A", "role": "context"},
            {"column": "B", "role": "amount", "funds_direction": "signed_net"},
            {"column": "C", "role": "recognition_period"},
        ]
        used, unused = "B", None
    if xls:
        import xlwt

        book = xlwt.Workbook()
        sheet = book.add_sheet("bank")
        for index, row in enumerate(rows):
            for column, value in enumerate(row):
                sheet.write(index, column, value)
        output = BytesIO()
        book.save(output)
        raw, sheet_name, format = output.getvalue(), "bank", "xls"
    else:
        raw = "\n".join(",".join(map(str, row)) for row in rows).encode()
        sheet_name, format = "CSV", "csv"
    source, _ = company.source(
        raw, subject=subject, category="bank", spec={"format": format, "columns": columns}
    )
    if unused:
        company.resolve(
            source,
            f"{sheet_name}!{unused}2",
            subject=subject + "-zero",
            treatment="no_accounting",
            non_accounting_reason="zero_amount",
            reason="Original unused bank column is zero",
        )
    return source, f"{sheet_name}!{used}2", amount


def resolve_bank(company, original, link, field, subject):
    source, location, amount = original
    company.resolve(
        source,
        location,
        [{**link, "amount_fen": amount, "amount_field": field}],
        subject=subject,
    )


@pytest.mark.parametrize("positive_columns", [False, True])
def test_both_real_bank_sides_cover_one_transfer_and_one_voucher(tmp_path, positive_columns):
    company = Company(tmp_path)
    link = publish_transfer(company)
    source = bank_original(company, "source-original", "outflow", positive_columns=positive_columns)
    destination = bank_original(
        company, "destination-original", "inflow", positive_columns=positive_columns
    )
    resolve_bank(company, source, link, "fact.source_amount_fen", "source-link")
    resolve_bank(company, destination, link, "fact.destination_amount_fen", "destination-link")
    result = company.materials.check("2026-01")
    assert result["status"] == "complete", result["issues"]
    with company.engine.store.connection(read_only=True) as connection:
        calculation = company.engine.store.select(
            connection, Read("calculation", "funds_transfer", "@transfer")
        )[0]
        assert calculation.values == {"actual_date": "2026-01-08", "amount_fen": AMOUNT}
        assert connection.execute("SELECT COUNT(*) FROM voucher").fetchone()[0] == 1
        assert connection.execute("SELECT SUM(amount) FROM balance").fetchone()[0] == 0
    assert company.engine.overview("2026-01")["cashflow"] == []


def test_positive_xls_outgoing_and_signed_text_incoming_use_current_calculation(tmp_path):
    company = Company(tmp_path)
    link = publish_transfer(company)
    outgoing = bank_original(company, "source-original", "outflow", positive_columns=True, xls=True)
    resolve_bank(company, outgoing, link, "result.amount_fen", "source-link")
    incoming, _ = company.source(
        b"Original bank receipt: +150000.00 yuan",
        subject="destination-original",
        category="bank",
        spec={
            "format": "text",
            "all_pages_reviewed": True,
            "passages": [
                {
                    "location": "incoming-transfer",
                    "page": 1,
                    "excerpt": "Original bank receipt: +150000.00 yuan",
                    "amount_fen": AMOUNT,
                    "recognition_period": "2026-01",
                }
            ],
        },
    )
    company.resolve(
        incoming,
        "incoming-transfer",
        [{**link, "amount_fen": AMOUNT, "amount_field": "fact.destination_amount_fen"}],
        subject="destination-link",
    )
    result = company.materials.check("2026-01")
    assert result["status"] == "complete", result["issues"]
    with company.engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM voucher").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM calculation WHERE kind='funds_transfer'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "field", ["fact.amount_fen", "result.amount_fen", "fact.source_amount_fen"]
)
def test_source_aliases_share_capacity_and_still_reject_real_double_allocation(tmp_path, field):
    company = Company(tmp_path)
    link = publish_transfer(company)
    resolve_bank(company, bank_original(company, "source", "outflow"), link, field, "source-link")
    resolve_bank(
        company,
        bank_original(company, "source-extra", "outflow"),
        link,
        "fact.source_amount_fen",
        "source-extra-link",
    )
    result = company.materials.check("2026-01")
    assert "material_business_overallocated" in codes(result)
    assert result["status"] == "needs_information"


def test_destination_capacity_still_rejects_real_double_allocation(tmp_path):
    company = Company(tmp_path)
    link = publish_transfer(company)
    for subject in ("destination", "destination-extra"):
        resolve_bank(
            company,
            bank_original(company, subject, "inflow"),
            link,
            "fact.destination_amount_fen",
            subject + "-link",
        )
    assert "material_business_overallocated" in codes(company.materials.check("2026-01"))


@pytest.mark.parametrize("positive_columns", [False, True])
@pytest.mark.parametrize(
    "direction,field",
    [("outflow", "fact.destination_amount_fen"), ("inflow", "result.amount_fen")],
)
def test_original_direction_cannot_claim_the_other_account_side(
    tmp_path, direction, field, positive_columns
):
    company = Company(tmp_path)
    link = publish_transfer(company)
    original = bank_original(company, "wrong-side", direction, positive_columns=positive_columns)
    resolve_bank(company, original, link, field, "wrong-side-link")
    assert "material_funds_direction_mismatch" in codes(company.materials.check("2026-01"))


def test_zero_link_does_not_invent_an_incoming_transfer_side(tmp_path):
    company = Company(tmp_path)
    link = publish_transfer(company)
    source, _ = company.source(b"name,amount,period\nzero,0,2026-01\n")
    company.resolve(
        source,
        "CSV!B2",
        [{**link, "amount_fen": 0, "amount_field": "fact.destination_amount_fen"}],
    )
    assert "material_funds_direction_mismatch" in codes(company.materials.check("2026-01"))


def outgoing_group_data(company, link, second_direction="outflow"):
    source, proof = company.source(
        b"outgoing,other,period\n60000,90000,2026-01\n",
        spec={
            "format": "csv",
            "columns": [
                {"column": "A", "role": "amount", "funds_direction": "outflow"},
                {"column": "B", "role": "amount", "funds_direction": second_direction},
                {"column": "C", "role": "recognition_period"},
            ],
        },
    )
    data = {
        "period": "2026-01",
        "source_id": source["subject_id"],
        "source_fact_id": source["fact_id"],
        "members": [
            {"location": "CSV!A2", "amount_fen": 6_000_000},
            {"location": "CSV!B2", "amount_fen": 9_000_000},
        ],
        "group_amount_fen": AMOUNT,
        "links": [{**link, "amount_fen": AMOUNT, "amount_field": "fact.source_amount_fen"}],
        "joint_basis_confirmed": True,
        "basis_evidence_digest": company.proof,
        "basis_location": "confirmed synthetic group",
        "reason": "Both original rows cover this outgoing transfer",
    }
    return data, (proof, company.proof)


@pytest.mark.parametrize("second_direction", ["outflow", "inflow"])
def test_group_direction_comes_from_every_original_member_and_failure_is_atomic(
    tmp_path, second_direction
):
    company = Company(tmp_path)
    link = publish_transfer(company)
    data, evidence = outgoing_group_data(company, link, second_direction)
    if second_direction == "outflow":
        company.materials.resolve_group(
            "group",
            data,
            evidence=evidence,
            expected_revision=0,
            request_id=company.request(),
        )
        result = company.materials.check("2026-01")
        assert result["status"] == "complete", result["issues"]
    else:
        with pytest.raises(KernelError) as failure:
            company.materials.resolve_group(
                "group",
                data,
                evidence=evidence,
                expected_revision=0,
                request_id=company.request(),
            )
        assert failure.value.code == "material_funds_direction_mismatch"
        with company.engine.store.connection(read_only=True) as connection:
            assert connection.execute("SELECT 1 FROM subject WHERE id='group'").fetchone() is None


@pytest.mark.parametrize(
    "direction,field",
    [("outflow", "result.amount_fen"), ("inflow", "fact.destination_amount_fen")],
)
def test_group_and_regular_links_share_only_their_actual_account_side(tmp_path, direction, field):
    company = Company(tmp_path)
    link = publish_transfer(company)
    data, evidence = outgoing_group_data(company, link)
    company.materials.resolve_group(
        "group",
        data,
        evidence=evidence,
        expected_revision=0,
        request_id=company.request(),
    )
    resolve_bank(
        company,
        bank_original(company, "regular-original", direction),
        link,
        field,
        "regular-link",
    )
    result = company.materials.check("2026-01")
    if direction == "outflow":
        assert "material_business_overallocated" in codes(result)
        assert result["status"] == "needs_information"
    else:
        assert result["status"] == "complete", result["issues"]


def test_equal_independent_transfers_have_independent_capacities(tmp_path):
    company = Company(tmp_path)
    for index, day in enumerate(("2026-01-08", "2026-01-15")):
        link = publish_transfer(company, f"transfer-{index}", day)
        for direction, field in (
            ("outflow", "fact.source_amount_fen"),
            ("inflow", "fact.destination_amount_fen"),
        ):
            subject = f"{direction}-{index}"
            resolve_bank(
                company, bank_original(company, subject, direction), link, field, subject + "-link"
            )
    result = company.materials.check("2026-01")
    assert result["status"] == "complete", result["issues"]
    with company.engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM voucher").fetchone()[0] == 2


def test_material_dimensions_do_not_add_input_or_persisted_business_facts():
    fact = transfer_fact()
    original = fact.model_dump(mode="json")
    assert fact.source_amount_fen == fact.destination_amount_fen == original["amount_fen"]
    assert set(original) == {
        "period",
        "actual_date",
        "source_bank_account_id",
        "destination_bank_account_id",
        "amount_fen",
    }
    schema = FundsTransfer.model_json_schema()
    assert set(schema["properties"]) == set(original)
    dimensions = schema["x-material-amount-dimensions"]
    for side in ("source", "destination"):
        assert dimensions[side]["amount_field"] == f"fact.{side}_amount_fen"
        assert dimensions[side]["account_field"] == f"fact.{side}_bank_account_id"
        with pytest.raises(ValidationError):
            FundsTransfer.model_validate_json(canonical(original | {f"{side}_amount_fen": AMOUNT}))


@pytest.mark.parametrize(
    "field,values",
    [
        ("fact.source_amount_fen", {"amount_fen": AMOUNT + 1}),
        (
            "fact.destination_amount_fen",
            {"amount_fen": AMOUNT, "destination_amount_fen": AMOUNT + 1},
        ),
    ],
)
def test_derived_amount_is_checked_against_other_representations(field, values):
    fact = transfer_fact()
    calculation = Calculation(
        "calculation",
        "transfer",
        "funds_transfer",
        fact.period,
        values,
        "fact",
        "00" * 32,
    )
    with pytest.raises(KernelError) as failure:
        _amount_basis(fact, calculation, field)
    assert failure.value.code == "material_amount_basis_conflict"
