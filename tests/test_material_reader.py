from hashlib import sha256
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from ai_accounting.material_reader import inspect_material
from ai_accounting.material_schemas import MaterialSourceInput


def inspect_book(
    tmp_path,
    monkeypatch,
    rows,
    *,
    configure=lambda sheet: None,
    transform_bytes=lambda raw: raw,
    **spec,
):
    book = Workbook()
    sheet = book.active
    sheet.title = "报销"
    for row in rows:
        sheet.append(row)
    configure(sheet)
    path = tmp_path / "原件.xlsx"
    book.save(path)
    raw = transform_bytes(path.read_bytes())
    path.write_bytes(raw)
    settings = SimpleNamespace(finance_evidence_dir=tmp_path, finance_max_evidence_bytes=4_000_000)
    monkeypatch.setattr("ai_accounting.material_reader.get_settings", lambda: settings)
    evidence = SimpleNamespace(
        id="413401ec-cf67-4aeb-8b5b-2b06dd26d500",
        storage_path=str(path),
        original_name=path.name,
        sha256=sha256(raw).hexdigest(),
    )
    return inspect_material(evidence, MaterialSourceInput(evidence_id=evidence.id, **spec))


def test_hidden_row_and_column_are_read_and_name_is_preserved(tmp_path, monkeypatch):
    def hidden(sheet):
        sheet.row_dimensions[3].hidden = True
        sheet.column_dimensions["B"].hidden = True

    source = inspect_book(
        tmp_path,
        monkeypatch,
        [["姓名", "金额"], ["甲", "1.00"], ["杨彪", "3076.87"]],
        configure=hidden,
        columns=[{"column": "A", "role": "context"}, {"column": "B", "role": "amount"}],
    )
    assert not source["issues"]
    assert [item["amount_fen"] for item in source["items"]] == [100, 307687]
    assert "杨彪" in source["items"][1]["excerpt"]
    assert next(cell for cell in source["coverage"] if cell["location"] == "报销!B3")["hidden"]


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (None, "MATERIAL_AMOUNT_MISSING"),
        ("=1+2", "MATERIAL_FORMULA_RESULT_MISSING"),
        ("不详", "MATERIAL_AMOUNT_UNREADABLE"),
        ("—", "MATERIAL_AMOUNT_UNREADABLE"),
        ("1.001", "MATERIAL_AMOUNT_PRECISION"),
    ],
)
def test_unknown_amount_is_never_silently_zero(tmp_path, monkeypatch, value, code):
    source = inspect_book(
        tmp_path,
        monkeypatch,
        [["姓名", "金额"], ["甲", 1], ["杨彪", value]],
        columns=[{"column": "A", "role": "context"}, {"column": "B", "role": "amount"}],
    )
    assert any(i["code"] == code and i["location"] == "报销!B3" for i in source["issues"])
    assert (
        next(item for item in source["items"] if item["location"] == "报销!B3")["amount_fen"]
        is None
    )


def test_unmapped_amount_column_and_wrong_total_block(tmp_path, monkeypatch):
    source = inspect_book(
        tmp_path,
        monkeypatch,
        [["姓名", "报销", "补贴"], ["甲", 2, 3], ["合计", 4, 3]],
        columns=[{"column": "A", "role": "context"}, {"column": "B", "role": "amount"}],
        total_rows=[3],
    )
    assert {i["code"] for i in source["issues"]} == {
        "MATERIAL_COLUMN_UNMAPPED",
        "MATERIAL_TOTAL_MISMATCH",
    }
    assert len(source["items"]) == 1


def test_different_sheets_keep_their_own_column_mapping(tmp_path, monkeypatch):
    def extra(sheet):
        other = sheet.parent.create_sheet("补贴")
        other.append(["金额", "姓名"])
        other.append(["3076.87", "杨彪"])

    source = inspect_book(
        tmp_path,
        monkeypatch,
        [["姓名", "报销"], ["甲", 2]],
        configure=extra,
        columns=[{"column": "A", "role": "context"}, {"column": "B", "role": "amount"}],
        sheet_columns={
            "补贴": [{"column": "A", "role": "amount"}, {"column": "B", "role": "context"}]
        },
    )
    assert not source["issues"]
    assert {i["location"] for i in source["items"]} == {"报销!B2", "补贴!A2"}


@pytest.mark.parametrize(
    "raw_amount,expected", [("90071992547409.93", 9007199254740993), ("3076.8700000000001", None)]
)
def test_excel_numeric_money_uses_original_decimal_text(
    tmp_path, monkeypatch, raw_amount, expected
):
    from io import BytesIO
    from xml.etree import ElementTree
    from zipfile import ZipFile

    def original_numeric_xml(raw):
        output = BytesIO()
        with ZipFile(BytesIO(raw)) as source, ZipFile(output, "w") as target:
            for info in source.infolist():
                data = source.read(info.filename)
                if info.filename == "xl/worksheets/sheet1.xml":
                    document = ElementTree.fromstring(data)
                    value = document.find(".//{*}c[@r='B2']/{*}v")
                    value.text = raw_amount
                    data = ElementTree.tostring(document)
                target.writestr(info, data)
        return output.getvalue()

    source = inspect_book(
        tmp_path,
        monkeypatch,
        [["姓名", "金额"], ["杨彪", 1]],
        transform_bytes=original_numeric_xml,
        columns=[{"column": "A", "role": "context"}, {"column": "B", "role": "amount"}],
    )
    assert source["items"][0]["amount_fen"] == expected
    assert bool(source["issues"]) == (expected is None)
