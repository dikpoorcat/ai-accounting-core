"""Retired diagnostics cannot emit current acceptance reports."""

import sys

import pytest

from scripts.benchmark_stage9_parallel_work import main


@pytest.mark.parametrize("existing", [False, True])
def test_retired_diagnostic_preserves_input_and_output(tmp_path, monkeypatch, capsys, existing):
    book, output = tmp_path / "stage9-book.json", tmp_path / "stage9-work.json"
    book.write_text("historical synthetic book", encoding="utf-8")
    if existing:
        output.write_text("historical result", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "benchmark_stage9_parallel_work.py", "--book-report", str(book), "--output", str(output),
    ])
    with pytest.raises(SystemExit) as retired:
        main()
    assert retired.value.code == 2
    assert "Retired: complete-brief preparation workers" in capsys.readouterr().err
    assert book.read_text(encoding="utf-8") == "historical synthetic book"
    if existing:
        assert output.read_text(encoding="utf-8") == "historical result"
    else:
        assert not output.exists()
