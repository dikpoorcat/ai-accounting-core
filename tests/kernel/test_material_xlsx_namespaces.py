"""Embedded DrawingML coordinates never become worksheet data or size limits."""

import json
from io import BytesIO
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest
from test_materials import csv_spec, workbook

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.materials import Specification, inspect_bytes

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"


def with_coordinates(raw, *, namespace=DRAWING):
    result = BytesIO()
    with ZipFile(BytesIO(raw)) as original, ZipFile(result, "w") as target:
        for member in original.infolist():
            content = original.read(member.filename)
            if member.filename == "xl/worksheets/sheet1.xml":
                root = ElementTree.fromstring(content)
                extension = ElementTree.SubElement(root, f"{{{MAIN}}}extLst")
                anchor = ElementTree.SubElement(extension, f"{{{DRAWING}}}twoCellAnchor")
                corner = ElementTree.SubElement(anchor, f"{{{DRAWING}}}from")
                # Includes names that would either crash, enlarge the budget,
                # or counterfeit a business amount if matched by local name.
                ElementTree.SubElement(corner, f"{{{namespace}}}col").text = "8"
                ElementTree.SubElement(corner, f"{{{namespace}}}row").text = "900000"
                ElementTree.SubElement(
                    corner, f"{{{namespace}}}dimension", {"ref": "A1:XFD1048576"}
                )
                fake = ElementTree.SubElement(corner, f"{{{namespace}}}row", {"r": "5"})
                cell = ElementTree.SubElement(fake, f"{{{namespace}}}c", {"r": "B5"})
                ElementTree.SubElement(cell, f"{{{namespace}}}v").text = "99999.99"
                # Foreign cells nested in a real data row are also non-business.
                data_row = root.find(f"{{{MAIN}}}sheetData/{{{MAIN}}}row[@r='2']")
                foreign = ElementTree.SubElement(data_row, f"{{{DRAWING}}}c", {"r": "Z2"})
                ElementTree.SubElement(foreign, f"{{{DRAWING}}}v").text = "10000"
                content = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
            target.writestr(member, content)
    return result.getvalue()


@pytest.mark.parametrize("namespace", (DRAWING, MAIN))
def test_embedded_coordinates_are_ignored_and_real_hidden_cells_remain(namespace):
    raw = workbook()
    decorated = with_coordinates(raw, namespace=namespace)
    spec = Specification.model_validate_json(
        json.dumps(csv_spec() | {"format": "xlsx", "total_rows": {"明细": [4]}})
    )
    parsed = inspect_bytes(decorated, spec)
    assert parsed == inspect_bytes(raw, spec)
    assert [item["amount_fen"] for item in parsed["items"]] == [1000, 2000]
    assert parsed["items"][1]["hidden"] is True
    assert parsed["control_totals"][0]["actual_fen"] == 3000
    # The original decorated bytes still retain their drawing coordinates.
    with ZipFile(BytesIO(decorated)) as archive:
        assert b"twoCellAnchor" in archive.read("xl/worksheets/sheet1.xml")


def with_full_sheet_formatting(raw, *, outside_value=None, outside_formula=False):
    result = BytesIO()
    with ZipFile(BytesIO(raw)) as original, ZipFile(result, "w") as target:
        for member in original.infolist():
            content = original.read(member.filename)
            if member.filename == "xl/worksheets/sheet1.xml":
                root = ElementTree.fromstring(content)
                root.find(f"{{{MAIN}}}dimension").set("ref", "A1:XFD1048576")
                columns = root.find(f"{{{MAIN}}}cols")
                if columns is None:
                    columns = ElementTree.Element(f"{{{MAIN}}}cols")
                    root.insert(1, columns)
                ElementTree.SubElement(
                    columns,
                    f"{{{MAIN}}}col",
                    {"min": "2", "max": "16384", "width": "12", "hidden": "1"},
                )
                rows = root.find(f"{{{MAIN}}}sheetData")
                row = ElementTree.SubElement(
                    rows, f"{{{MAIN}}}row", {"r": "1048576", "s": "1", "hidden": "1"}
                )
                cell = ElementTree.SubElement(row, f"{{{MAIN}}}c", {"r": "XFD1048576", "s": "1"})
                if outside_value is not None:
                    ElementTree.SubElement(cell, f"{{{MAIN}}}v").text = outside_value
                if outside_formula:
                    ElementTree.SubElement(cell, f"{{{MAIN}}}f").text = "1+2"
                content = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
            target.writestr(member, content)
    return result.getvalue()


def test_full_sheet_empty_formats_keep_actual_hidden_cells_and_original_budget():
    raw = with_full_sheet_formatting(workbook())
    spec = Specification.model_validate_json(
        json.dumps(csv_spec() | {"format": "xlsx", "total_rows": {"明细": [4]}})
    )
    parsed = inspect_bytes(raw, spec)
    assert not parsed["issues"]
    assert [item["amount_fen"] for item in parsed["items"]] == [1000, 2000]
    assert all(item["hidden"] for item in parsed["items"])
    assert parsed["control_totals"][0]["actual_fen"] == 3000


@pytest.mark.parametrize("value,formula", [("1", False), (None, True), ("0", True)])
def test_actual_hidden_value_or_formula_outside_budget_still_fails(value, formula):
    raw = with_full_sheet_formatting(workbook(), outside_value=value, outside_formula=formula)
    with pytest.raises(KernelError) as error:
        inspect_bytes(raw, Specification(format="xlsx"))
    assert error.value.code == "material_too_large"
