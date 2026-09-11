"""Group controls verify original leaves without becoming additional business amounts."""

from io import BytesIO

import pytest
from openpyxl import Workbook
from test_materials import Company, codes, csv_spec

from ai_accounting.kernel import materials
from ai_accounting.kernel.materials import Specification, inspect_bytes
from ai_accounting.kernel.types import canonical


def grouped_spec(sheet="CSV", format="csv"):
    return csv_spec() | {
        "format": format,
        "controls": [
            {"location": f"{sheet}!B6", "scope": "all_detail_column"},
            {"location": f"{sheet}!B7", "locations": [f"{sheet}!B2", f"{sheet}!B3"]},
            {"location": f"{sheet}!B8", "locations": [f"{sheet}!B4", f"{sheet}!B5"]},
            {"location": f"{sheet}!B9", "locations": [f"{sheet}!B2", f"{sheet}!B4"]},
            {"location": f"{sheet}!B10", "locations": [f"{sheet}!B3", f"{sheet}!B5"]},
        ],
    }


ROWS = [
    ("reference", "amount", "period"),
    ("a-receipted", "10", "2026-01"),
    ("a-unreceipted", "20", "2026-01"),
    ("b-receipted", "30", "2026-02"),
    ("b-unreceipted", "40", "2026-02"),
    ("all", "100", ""),
    ("person-a", "30", ""),
    ("person-b", "70", ""),
    ("receipted", "40", ""),
    ("unreceipted", "60", ""),
]


def original(format, rows=ROWS):
    if format == "csv":
        return "\n".join(",".join(row) for row in rows).encode(), "CSV"
    output = BytesIO()
    if format == "xlsx":
        book = Workbook()
        sheet = book.active
        sheet.title = "明细"
        for row in rows:
            sheet.append(row)
        sheet.row_dimensions[3].hidden = True
        book.save(output)
        book.close()
    else:
        import xlwt

        book = xlwt.Workbook()
        sheet = book.add_sheet("明细")
        for index, row in enumerate(rows):
            for column, value in enumerate(row):
                sheet.write(index, column, value)
        sheet.row(2).hidden = True
        book.save(output)
    return output.getvalue(), "明细"


@pytest.mark.parametrize("format", ["csv", "xls", "xlsx"])
def test_cross_cutting_groups_and_grand_total_count_hidden_business_once(format):
    raw, sheet = original(format)
    parsed = inspect_bytes(
        raw, Specification.model_validate_json(canonical(grouped_spec(sheet, format)))
    )
    assert not parsed["issues"]
    assert [row["amount_fen"] for row in parsed["items"]] == [1000, 2000, 3000, 4000]
    assert [row["actual_fen"] for row in parsed["control_totals"]] == [
        10000,
        3000,
        7000,
        4000,
        6000,
    ]
    if format != "csv":
        assert parsed["items"][1]["hidden"]


def test_future_group_error_does_not_require_future_posting_to_finish_current_month(tmp_path):
    company = Company(tmp_path)
    rows = [*ROWS]
    rows[7] = ("person-b-wrong", "71", "")
    raw, _ = original("csv", rows)
    source, _ = company.source(raw, spec=grouped_spec())
    company.resolve(source, "CSV!B2", [company.expense("one", 1000)])
    company.resolve(source, "CSV!B3", [company.expense("two", 2000)])
    january = company.materials.check("2026-01")
    assert january["status"] == "complete"
    assert january["file_status"] == "needs_information"
    february = company.materials.check("2026-02")
    assert "material_total_mismatch" in codes(february)
    assert february["coverage_count"] == 2


@pytest.mark.parametrize(
    "control,expected",
    [
        ({"locations": ["CSV!B2", "CSV!B2"]}, "material_control_duplicate_member"),
        ({"locations": ["CSV!B999"]}, "material_control_member_invalid"),
        ({"locations": ["CSV!B7"]}, "material_control_member_invalid"),
        (
            {"ranges": [{"sheet": "CSV", "column": "B", "first_row": 99, "last_row": 100}]},
            "material_control_empty_range",
        ),
    ],
)
def test_invalid_or_recursive_group_members_never_count_as_a_complete_control(control, expected):
    spec = grouped_spec()
    spec["controls"][1] = {"location": "CSV!B7", **control}
    assert expected in codes(
        inspect_bytes(original("csv")[0], Specification.model_validate_json(canonical(spec)))
    )


@pytest.mark.parametrize("format", ["csv", "xls", "xlsx"])
def test_grouped_control_rows_are_separate_from_business_capacity(format, monkeypatch):
    monkeypatch.setattr(materials, "MAX_DATA_ROWS", 4)
    raw, sheet = original(format)
    parsed = inspect_bytes(
        raw, Specification.model_validate_json(canonical(grouped_spec(sheet, format)))
    )
    assert len(parsed["items"]) == 4 and len(parsed["control_totals"]) == 5


def test_many_small_range_controls_use_one_original_index(monkeypatch):
    rows = [("reference", "amount", "period"), *((str(i), "1", "2026-01") for i in range(2000))]
    controls = []
    for index in range(200):
        rows.append(("control", "1", ""))
        controls.append(
            {
                "location": f"CSV!B{2002 + index}",
                "ranges": [
                    {"sheet": "CSV", "column": "B", "first_row": 2 + index, "last_row": 2 + index}
                ],
            }
        )
    raw, _ = original("csv", rows)
    calls, real = [], materials._row_number

    def count(location):
        calls.append(location)
        return real(location)

    monkeypatch.setattr(materials, "_row_number", count)
    parsed = inspect_bytes(
        raw, Specification.model_validate_json(canonical(csv_spec() | {"controls": controls}))
    )
    assert not parsed["issues"]
    assert len(calls) <= 2 * 2000


def test_public_control_summary_does_not_expand_all_group_members(tmp_path):
    company = Company(tmp_path)
    raw, _ = original("csv")
    evidence = company.evidence(raw)
    result = company.materials.inspect(evidence, grouped_spec(), limit=1)
    assert result["control_totals"][0]["member_count"] == 4
    assert "member_locations" not in result["control_totals"][0]


def test_duplicate_control_range_budget_is_checked_before_retaining_all_members(monkeypatch):
    from ai_accounting.kernel.contracts import KernelError

    monkeypatch.setattr(materials, "MAX_NONEMPTY_CELLS", 100)
    spec = grouped_spec()
    spec["controls"][1] = {
        "location": "CSV!B7",
        "ranges": [{"sheet": "CSV", "column": "B", "first_row": 2, "last_row": 5}] * 100,
    }
    with pytest.raises(KernelError) as rejected:
        inspect_bytes(original("csv")[0], Specification.model_validate_json(canonical(spec)))
    assert rejected.value.code == "material_too_large"
