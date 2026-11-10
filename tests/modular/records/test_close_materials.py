"""Closing observes bounded source membership and preserves only adopted evidence."""

from uuid import uuid4

import pytest
from test_documents import _prepare, _publish, company_identity
from test_period_reset import store

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.records.public import RecordsChange, RecordsContext, open_standard_bundle


def capture(case, *ids):
    storage, (company, _), records = case
    with storage.read_unit(company.company_id) as read:
        return records.capture_close_materials("2026-02", "2026-03", read, source_ids=ids)


def test_material_capture_uses_period_unassigned_and_explicit_sources_without_file_reads(
    records_case, monkeypatch
):
    storage, (company, _), records = records_case
    selected = store(records_case, "范围内", "2026-02", file=True)
    unknown = store(records_case, "未归期")
    cross = store(records_case, "明确跨期", "2026-01")
    store(records_case, "无关历史", "2025-12")
    (company.company_root / selected["relative_path"]).unlink()
    with storage.read_unit(company.company_id) as read:
        repository = read.repository("records")

        def forbidden(*args, **kwargs):
            raise AssertionError("资料集合版本不得加载全库来源正文或原件。")

        monkeypatch.setattr(repository, "all_sources", forbidden)
        monkeypatch.setattr(repository, "sources", forbidden)
        value = records.capture_close_materials(
            "2026-02", "2026-03", read, source_ids=(cross["source_id"],)
        )
    assert value.source_ids == tuple(
        sorted(source["source_id"] for source in (selected, unknown, cross))
    )
    with pytest.raises(ContractError) as error:
        capture(records_case, str(uuid4()))
    assert error.value.code == "invalid_input"
    assert error.value.fact_issues[0]["code"] == "source_not_found"


def test_material_version_follows_membership_and_content_not_file_placement(records_case):
    storage, (company, _), records = records_case
    selected = store(records_case, "所选原件", "2026-02", file=True)
    first = capture(records_case)
    store(records_case, "无关历史", "2025-12")
    assert capture(records_case) == first
    old_path = company.company_root / selected["relative_path"]
    new_relative = f"originals/2026-02/{selected['source_id']}/整理后的原件.txt"
    new_path = company.company_root / new_relative
    new_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.rename(new_path)
    renamed = selected | {"relative_path": new_relative, "original_name": "整理后的原件.txt"}
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(
                RecordsChange(sources=(renamed,)), RecordsContext(unit, str(uuid4()))
            ),
            unit,
        )
    assert capture(records_case) == first
    incoming = store(records_case, "后来收到的资料", "2026-03")
    second = capture(records_case)
    assert second.version != first.version and incoming["source_id"] in second.source_ids
    with storage.write_unit(company.company_id) as unit:
        records.discard_sources(
            (incoming["source_id"],), unit, retained_source_ids=(), protected_paths=()
        )
    assert capture(records_case) == first
    unknown = store(records_case, "待确定归期")
    assert unknown["source_id"] in capture(records_case).source_ids
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(
                RecordsChange(sources=(unknown | {"archive_month": "2026-04"},)),
                RecordsContext(unit, str(uuid4())),
            ),
            unit,
        )
    assert capture(records_case) == first


def test_frozen_close_sources_are_protected_but_not_current_standard_materials(records_case):
    storage, (company, _), records = records_case
    source = store(records_case, "已采用的关账覆盖说明", "2026-01", file=True)
    with storage.write_unit(company.company_id) as unit:
        records.replace_source_uses("period_close", "2026-01", (source["source_id"],), unit)
        records.ensure_document_targets(unit)
    assert _publish(records_case, _prepare(records_case, [], []))["state"] == "ready"
    bundle = open_standard_bundle(company.company_root / "standard/current.json")
    assert bundle.sources == ()
    old = company.company_root / source["relative_path"]
    renamed = old.with_name("重新整理后的关账依据.txt")
    old.rename(renamed)
    source = source | {
        "relative_path": renamed.relative_to(company.company_root).as_posix(),
        "original_name": renamed.name,
    }
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(
                RecordsChange(sources=(source,)), RecordsContext(unit, str(uuid4()))
            ),
            unit,
        )
    with storage.read_unit(company.company_id) as read:
        assert records.document_status(read)["state"] == "ready"
        assert records.source_discard_protection(
            (source["source_id"],), read, retained_source_ids=(), protected_paths=()
        ) == (source["source_id"],)
        snapshot = records.verify_snapshot(
            company_identity(company),
            [],
            [],
            read,
            frozen_close_source_uses=(("2026-01", source["source_id"]),),
        )
        assert source["relative_path"] in {item.relative_path for item in snapshot.files}
        with pytest.raises(ContractError) as error:
            records.verify_snapshot(company_identity(company), [], [], read)
        assert error.value.code == "storage_corrupt"


def test_leaving_current_note_does_not_drop_its_frozen_close_source(records_case):
    storage, (company, _), records = records_case
    source = store(records_case, "本月现实工作说明", "2026-01")
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(
                RecordsChange(
                    notes=(
                        {
                            "scope": "work:2026-01",
                            "expected_seq": 0,
                            "text": "已按来源安排办理。",
                            "evidence_ids": [source["source_id"]],
                        },
                    )
                ),
                RecordsContext(unit, str(uuid4())),
            ),
            unit,
        )
        records.replace_source_uses("period_close", "2026-01", (source["source_id"],), unit)
    _publish(records_case, _prepare(records_case, [], []))
    assert len(open_standard_bundle(company.company_root / "standard/current.json").sources) == 1
    with storage.write_unit(company.company_id) as unit:
        records.clear_notes(("work:2026-01",), str(uuid4()), unit)
    _publish(records_case, _prepare(records_case, [], None))
    bundle = open_standard_bundle(company.company_root / "standard/current.json")
    assert bundle.sources == bundle.notes == ()
    with storage.read_unit(company.company_id) as read:
        records.verify_snapshot(
            company_identity(company),
            [],
            [],
            read,
            frozen_close_source_uses=(("2026-01", source["source_id"]),),
        )
