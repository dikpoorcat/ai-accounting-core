"""Actual source bytes and published SQLite facts establish row-level coverage."""

from io import BytesIO

import pytest
from openpyxl import Workbook

from ai_accounting.kernel.domains.transactions import Expense
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.materials import (
    Materials,
    MaterialSource,
    Specification,
    inspect_bytes,
    register,
)
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store


class Company:
    def __init__(self, tmp_path):
        registry = default_registry()
        if MaterialSource.kind not in registry.models:
            register(registry)
        self.engine = Engine(
            Store.create(
                tmp_path / "materials.sqlite", registry, "company", "91310000123456789A", "database"
            )
        )
        self.materials = Materials(self.engine)
        self.sequence = 0
        self.proof = self.evidence("负责人逐项确认".encode(), "confirmation.txt")

    def request(self):
        self.sequence += 1
        return str(self.sequence)

    def evidence(self, raw, name="source.csv"):
        return self.engine.register_evidence(
            raw, "application/octet-stream", name, request_id=self.request()
        )["digest"]

    def source(
        self,
        raw=b"name,amount,period\na,10.00,2026-01\nb,20.00,2026-01\n",
        subject="source",
        period="2026-01",
        spec=None,
        **changes,
    ):
        ev = self.evidence(raw)
        data = (
            dict(
                period=period,
                evidence_digest=ev,
                category="transactions",
                purpose="business",
                specification=spec or csv_spec(),
            )
            | changes
        )
        result = self.materials.receive(
            subject, data, evidence=(ev, self.proof), expected_revision=0, request_id=self.request()
        )
        return result, ev

    def expense(self, subject, amount, period="2026-01", revision=0, proof=None):
        fact = Expense(
            period=period,
            counterparty_id="supplier",
            amount_fen=amount,
            expense_class="administration",
            creditor_kind="supplier",
        )
        result = self.engine.save_fact(
            fact.kind,
            subject,
            fact.model_dump(mode="json"),
            evidence=(proof or self.proof,),
            expected_revision=revision,
            request_id=self.request(),
        )
        preview = self.engine.preview([subject])
        published = self.engine.confirm(
            [subject],
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id=self.request(),
        )["results"][0]
        return dict(
            subject_id=subject,
            fact_kind="expense",
            fact_id=result["fact_id"],
            calculation_id=published["calculation_id"],
            amount_field="fact.amount_fen",
            amount_fen=amount,
            recognition_period=period,
        )

    def resolve(self, source, location, links=(), subject=None, revision=0, **changes):
        data = (
            dict(
                period="2026-01",
                source_id=source["subject_id"],
                source_fact_id=source["fact_id"],
                location=location,
                treatment="recognize",
                recognition_period="2026-01",
                links=list(links),
            )
            | changes
        )
        return self.materials.resolve(
            subject or "resolution-" + location,
            data,
            evidence=(self.proof,),
            expected_revision=revision,
            request_id=self.request(),
        )


def csv_spec():
    return {
        "format": "csv",
        "columns": [
            {"column": "A", "role": "context"},
            {"column": "B", "role": "amount"},
            {"column": "C", "role": "recognition_period"},
        ],
    }


@pytest.fixture
def company(tmp_path):
    return Company(tmp_path)


def codes(result):
    return {issue["code"] for issue in result["issues"]}


def test_file_citation_does_not_hide_unprocessed_second_row(company):
    source, ev = company.source()
    first = company.expense("first", 1000, proof=ev)
    assert "material_item_unresolved" in codes(company.materials.check("2026-01"))
    company.resolve(source, "CSV!B2", [first])
    result = company.materials.check("2026-01")
    assert [item["complete"] for item in result["coverage"]] == [True, False]
    second = company.expense("second", 2000)
    company.resolve(source, "CSV!B3", [second])
    assert company.materials.check("2026-01")["status"] == "complete"


def workbook(*, formula=False, unmapped=False):
    book = Workbook()
    sheet = book.active
    sheet.title = "明细"
    sheet.append(["名称", "金额", "期间"])
    sheet.append(["第一笔", 10, "2026-01"])
    sheet.append(["隐藏笔", 20, "2026-01"])
    sheet.row_dimensions[3].hidden = True
    sheet.append(["合计", "=SUM(B2:B3)" if formula else 30, "2026-01"])
    if unmapped:
        sheet["D2"] = "另有费用"
    buffer = BytesIO()
    book.save(buffer)
    book.close()
    return buffer.getvalue()


def test_hidden_rows_and_control_totals_are_read_from_actual_xlsx():
    spec = Specification.model_validate_json(
        __import__("json").dumps(csv_spec() | {"format": "xlsx", "total_rows": {"明细": [4]}})
    )
    parsed = inspect_bytes(workbook(), spec)
    assert not parsed["issues"]
    assert len(parsed["items"]) == 2
    assert parsed["items"][1]["hidden"]
    assert parsed["control_totals"][0]["actual_fen"] == 3000
    assert "material_column_unmapped" in codes(inspect_bytes(workbook(unmapped=True), spec))
    assert "material_formula_result_missing" in codes(inspect_bytes(workbook(formula=True), spec))


@pytest.mark.parametrize(
    "amount,period,expected",
    [(900, "2026-01", "material_split_mismatch"), (1000, "2026-02", "material_period_mismatch")],
)
def test_original_amount_and_period_cannot_be_overridden(company, amount, period, expected):
    source, _ = company.source(b"name,amount,period\na,10.00,2026-01\n")
    link = company.expense("expense", amount, period)
    company.resolve(source, "CSV!B2", [link])
    assert expected in codes(company.materials.check("2026-01"))


def test_split_one_original_row_to_two_actual_business_results(company):
    source, _ = company.source(b"name,amount,period\na,30.00,2026-01\n")
    first, second = company.expense("first", 1000), company.expense("second", 2000)
    company.resolve(source, "CSV!B2", [first, second])
    assert company.materials.check("2026-01")["status"] == "complete"


def test_changed_or_deleted_current_result_reopens_original_row(company):
    source, _ = company.source(b"name,amount,period\na,10.00,2026-01\n")
    link = company.expense("expense", 1000)
    company.resolve(source, "CSV!B2", [link])
    company.expense("expense", 1200, revision=1)
    assert "material_result_stale" in codes(company.materials.check("2026-01"))
    preview = company.engine.preview_delete("expense")
    company.engine.delete(
        "expense",
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id=company.request(),
    )
    assert "material_result_missing" in codes(company.materials.check("2026-01"))


def test_duplicate_documents_need_explicit_relation_instead_of_double_allocation(company):
    source, _ = company.source(b"name,amount,period\na,10.00,2026-01\n", "original")
    duplicate, _ = company.source(b"name,amount,period\ncopy,10.00,2026-01\n", "copy")
    link = company.expense("expense", 1000)
    company.resolve(source, "CSV!B2", [link], subject="original-resolution")
    company.resolve(duplicate, "CSV!B2", [link], subject="copy-resolution")
    assert "material_business_overallocated" in codes(company.materials.check("2026-01"))
    company.resolve(
        duplicate,
        "CSV!B2",
        subject="copy-resolution",
        revision=1,
        treatment="duplicate",
        duplicate_source_id="original",
        duplicate_location="CSV!B2",
        reason="同一业务的重复凭据",
    )
    assert company.materials.check("2026-01")["status"] == "complete"


def test_other_period_is_checked_at_both_receipt_and_recognition_months(company):
    source, _ = company.source(b"name,amount,period\na,10.00,2026-02\n")
    earlier = company.materials.check("2026-01")
    assert earlier["status"] == "complete" and earlier["file_status"] == "needs_information"
    assert company.materials.check("2026-02")["status"] == "needs_information"
    link = company.expense("expense", 1000, "2026-02")
    company.resolve(
        source, "CSV!B2", [link], treatment="other_period", recognition_period="2026-02"
    )
    assert company.materials.check("2026-01")["status"] == "complete"
    assert company.materials.check("2026-02")["status"] == "complete"


def test_text_location_is_verified_and_supporting_evidence_has_explicit_purpose(company):
    specification = {
        "format": "text",
        "all_pages_reviewed": True,
        "passages": [{"location": "第1段", "page": 1, "excerpt": "工资规则依据"}],
    }
    source, _ = company.source(
        "工资规则依据".encode(),
        spec=specification,
        purpose="supporting",
        supporting_purpose="政策核对",
    )
    company.resolve(source, "第1段", treatment="supporting", reason="政策参考，不代表新增业务")
    assert company.materials.check("2026-01")["status"] == "complete"
    assert "material_excerpt_missing" in codes(
        inspect_bytes(
            b"other content",
            Specification.model_validate_json(__import__("json").dumps(specification)),
        )
    )


def test_unknown_business_position_cannot_be_invented(company):
    source, _ = company.source(b"name,amount,period\na,10.00,2026-01\n")
    link = company.expense("expense", 1000)
    company.resolve(source, "CSV!B99", [link])
    assert "material_location_unknown" in codes(company.materials.check("2026-01"))


def test_pdf_requires_every_actual_page_position():
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    buffer = BytesIO()
    writer.write(buffer)
    spec = Specification.model_validate_json(
        '{"format":"pdf","all_pages_reviewed":true,"passages":[{"location":"第1页","page":1,"excerpt":"人工阅读第1页为空白页"}]}'
    )
    assert "material_pages_unreviewed" in codes(inspect_bytes(buffer.getvalue(), spec))


def test_missing_original_amount_requires_confirmation_and_rechecks_control(company):
    spec = csv_spec() | {"total_rows": {"CSV": [4]}}
    source, _ = company.source(
        b"name,amount,period\na,10.00,2026-01\nb,,2026-01\ntotal,30.00,2026-01\n", spec=spec
    )
    company.resolve(source, "CSV!B2", [company.expense("a", 1000)])
    second = company.expense("b", 2000)
    company.resolve(source, "CSV!B3", [second], amount_fen=2000)
    assert "material_amount_missing" in codes(company.materials.check("2026-01"))
    company.resolve(
        source,
        "CSV!B3",
        [second],
        amount_fen=2000,
        revision=1,
        reason="负责人根据原单逐笔核对，明确遗漏金额为20元",
    )
    assert company.materials.check("2026-01")["status"] == "complete"


def test_formula_total_needs_explicit_recovered_value(company):
    source, _ = company.source(
        workbook(formula=True), spec=csv_spec() | {"format": "xlsx", "total_rows": {"明细": [4]}}
    )
    company.resolve(source, "明细!B2", [company.expense("a", 1000)])
    company.resolve(source, "明细!B3", [company.expense("b", 2000)])
    assert "material_formula_result_missing" in codes(company.materials.check("2026-01"))
    company.resolve(
        source,
        "明细!B4",
        treatment="control_total",
        amount_fen=3000,
        reason="负责人在原工作簿重新计算并确认总额",
    )
    assert company.materials.check("2026-01")["status"] == "complete"


def test_unrelated_history_is_not_parsed_and_cross_period_allocation_is_checked(
    company, monkeypatch
):
    from ai_accounting.kernel import materials

    source, _ = company.source(b"name,amount,period\na,10.00,2026-01\n")
    link = company.expense("expense", 1000)
    company.resolve(source, "CSV!B2", [link])
    company.source(b"name,amount,period\nunresolved,12.00,2025-12\n", "old", period="2025-12")
    later, _ = company.source(
        b"name,amount,period\nagain,10.00,2026-01\n", "later", period="2026-02"
    )
    company.resolve(later, "CSV!B2", [link], subject="later-resolution", period="2026-02")
    original, calls = materials.inspect_bytes, []

    def record(raw, spec):
        calls.append(raw)
        return original(raw, spec)

    monkeypatch.setattr(materials, "inspect_bytes", record)
    result = company.materials.check("2026-01")
    assert "material_business_overallocated" in codes(result)
    assert len(calls) == 2  # current month and explicitly assigned January row only
    assert not any(b"unresolved,12.00" in raw for raw in calls)


def test_duplicate_cycle_never_counts_as_processing(company):
    first, _ = company.source(b"name,amount,period\na,10.00,2026-01\n", "a")
    second, _ = company.source(b"name,amount,period\nb,10.00,2026-01\n", "b")
    for source, target in ((first, "b"), (second, "a")):
        company.resolve(
            source,
            "CSV!B2",
            subject="resolution-" + target,
            treatment="duplicate",
            duplicate_source_id=target,
            duplicate_location="CSV!B2",
            reason="重复资料",
        )
    assert "material_duplicate_cycle" in codes(company.materials.check("2026-01"))


def test_biff8_original_includes_hidden_rows_sheets_and_exact_fen():
    import xlwt

    book = xlwt.Workbook()
    sheet = book.add_sheet("原始明细")
    sheet.visibility = 1
    for row, values in enumerate(
        (
            ("名称", "金额", "期间"),
            ("a", 10.01, "2026-01"),
            ("b", 20.02, "2026-01"),
            ("合计", 30.03, "2026-01"),
        )
    ):
        for column, value in enumerate(values):
            sheet.write(row, column, value)
    sheet.row(2).hidden = True
    output = BytesIO()
    book.save(output)
    spec = Specification.model_validate_json(
        __import__("json").dumps(csv_spec() | {"format": "xls", "total_rows": {"原始明细": [4]}})
    )
    result = inspect_bytes(output.getvalue(), spec)
    assert not result["issues"]
    assert [item["amount_fen"] for item in result["items"]] == [1001, 2002]
    assert all(item["hidden"] for item in result["items"])


def test_workflow_step_remains_incomplete_until_actual_rows_are_resolved(company):
    from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
    from ai_accounting.kernel.workflow import Workflow

    source, evidence = company.source(b"name,amount,period\na,10.00,2026-01\n")
    for category in MATERIAL_CATEGORIES:
        items = [evidence] if category == "transactions" else []
        Periods(company.engine).inventory(
            "2026-01",
            category,
            evidence=items,
            expected=len(items),
            no_business=not items,
            confirmation_evidence=company.proof,
            request_id=company.request(),
        )
    service = Workflow(company.engine)
    step = service.query("2026-01", as_of="2026-01-31")["steps"][4]
    assert step["status"] == "needs_information"
    assert any(item.get("code") == "material_item_unresolved" for item in step["fact_issues"])
    company.resolve(source, "CSV!B2", [company.expense("expense", 1000)])
    assert service.query("2026-01", as_of="2026-01-31")["steps"][4]["status"] == "ready"
