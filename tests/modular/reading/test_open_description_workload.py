"""Open group wording remains correct while hidden explanations stay out of hot reads."""

from copy import deepcopy
from dataclasses import replace

from ai_accounting_modular.contracts.public import ReadInputs, loads_json
from ai_accounting_modular.reading import (
    _brief_open,
    _brief_open_groups,
    _brief_open_update,
    _monthly,
)
from ai_accounting_modular.reading import (
    _brief_open_index as index,
)
from ai_accounting_modular.reading._collections import IndexItem
from ai_accounting_modular.reading._open_store import OpenPool
from ai_accounting_modular.reading._publication_guard import CaptureGuard
from tests.modular.application._working_case import case as case

from ._publication import body
from ._support import assemble, uid
from .test_brief_open import publish


def selected(company):
    return _monthly.normalize(
        {
            "company_id": company,
            "period_from": "2026-01",
            "period_to": "2026-01",
            "view": "as_posted",
        }
    )


def current_page(reader, storage, company):
    with storage.read_unit(company) as read:
        snapshot = _monthly.ready_snapshot(reader, selected(company), (index.COMPONENT,), read)
        assert snapshot is not None
        return _brief_open.page(snapshot, read, {"limit": 1})


def test_group_wording_handles_shared_and_distinct_notes_through_incremental_removal(case):
    case.seed(expense=False)
    first, second, third = (case.expense("100") for _ in range(3))

    def add(fact, text):
        result = case.app.accounting_apply(
            case.request(
                fact,
                notes=[
                    {
                        "scope": "business:" + fact["business_id"],
                        "expected_seq": 0,
                        "text": text,
                        "evidence_ids": [case.source_id],
                    }
                ],
            )
        )
        assert result["completion"] == "complete"

    def withdraw(fact):
        result = case.app.accounting_apply(
            case.request(
                mutations=[
                    {
                        "op": "withdraw",
                        "expected_seq": 1,
                        "business_id": fact["business_id"],
                    }
                ]
            )
        )
        assert result["completion"] == "complete"

    def check(description, count):
        case.kernel.reading.run_pending(case.company)
        incremental = current_page(case.kernel.reading, case.kernel.storage, case.company)
        items = incremental["collections"]["open_items"]["items"]
        if count:
            assert len(items) == 1
            assert items[0]["description"] == description
            assert items[0]["member_count"] == count
        else:
            assert items == []
        publish(case)
        cold = current_page(case.kernel.reading, case.kernel.storage, case.company)
        assert cold["open_items"] == incremental["open_items"]
        assert cold["collections"]["open_items"]["items"] == items

    add(first, "同一用途")
    publish(case)
    check("同一用途", 1)
    add(second, "同一用途")
    check("同一用途", 2)
    add(third, "另一用途")
    check("管理费用", 3)
    withdraw(third)
    check("同一用途", 2)
    withdraw(first)
    check("同一用途", 1)
    withdraw(second)
    check(None, 0)


def member(position):
    identity = f"member-{position:04d}"
    value = {
        "id": identity,
        "group_key": "open:fixed",
        "category_key": "supplier_payables",
        "party": "固定供应商",
        "subject_id": uid(identity),
        "_matter_title": "管理费用",
        "_description": f"隐藏事项说明-{position:04d}-" + "中文说明" * 15,
        "_note_scope": "business:" + uid(identity),
        "source_amount_fen": "100",
        "paid_fen": "0",
        "other_settled_fen": "0",
        "outstanding_fen": "100",
        "current_outstanding_fen": "100",
        "status": "open",
        "current_status": "open",
    }
    return IndexItem(
        "open_members",
        identity,
        f"{position:08d}",
        value,
        business_id=value["subject_id"],
        group_key=value["group_key"],
        category=value["category_key"],
    )


def test_many_distinct_hidden_notes_do_not_enter_the_descriptor_body_or_group_first_page(
    tmp_path,
    monkeypatch,
    request,
):
    book = assemble(tmp_path, established=False)
    scope_key = _monthly.key(selected(book.company))
    loaded, get_blocks = [], OpenPool.get_blocks

    def observed(pool, keys):
        values = get_blocks(pool, keys)
        loaded.extend(value for value in values if value is not None)
        return values

    monkeypatch.setattr(OpenPool, "get_blocks", observed)
    for count in (24, 360):
        members = [member(position) for position in range(count)]
        summary, entries, folds, categories = _brief_open_groups.prepare(
            members,
            "2026-01",
            "2026-01",
            complete=True,
        )
        entries.extend(members)
        entries.extend(
            index.item(index.FOLDS, identity, value) for identity, value in folds.items()
        )
        with book.storage.maintenance_unit(book.company) as unit:
            repository = unit.repository("reading")
            repository.reset()
            repository.register(scope_key, index.COMPONENT, unit.head, index.FORMAT_VERSION)
            model = index.complete(
                {"open_items": summary, "_category_folds": categories},
                entries,
                unit.head,
                repository=repository,
                scope_key=scope_key,
            )
            model = replace(
                model,
                model=replace(
                    model.model,
                    inputs=ReadInputs(
                        note_scopes=tuple(item.value["_note_scope"] for item in members)
                    ),
                ),
            )
            guard = CaptureGuard.capture(repository.state(scope_key, index.COMPONENT), unit.head)
            assert repository.publish(
                scope_key,
                index.COMPONENT,
                unit.head,
                index.FORMAT_VERSION,
                model,
                guard=guard,
                current_head=unit.head,
            )
            saved = body(repository, scope_key, index.COMPONENT)["payload_json"]
            descriptor = repository.payload(scope_key, index.COMPONENT)["payload_json"]
            assert "note_scopes" not in saved and "note_scopes" not in descriptor
            assert "_inputs" not in loads_json(saved)
            assert all(item.value["_description"] not in saved for item in members)
        loaded.clear()
        page = current_page(book.reading, book.storage, book.company)
        group = page["collections"]["open_items"]["items"][0]
        assert group["description"] == "管理费用" and group["member_count"] == count
        assert group["outstanding_fen"] == str(count * 100)
        assert not any("隐藏事项说明-" in raw for raw in loaded)
        request.node.user_properties.extend(
            [
                (f"open_{count}_selected_nodes", len(loaded)),
                (f"open_{count}_selected_bytes", sum(len(raw.encode()) for raw in loaded)),
            ]
        )
        # An amount-only change must not enumerate or query the group's text
        # directory, regardless of how many distinct hidden explanations it has.
        with book.storage.read_unit(book.company) as read:
            repository = read.repository("reading")
            directory = index.base(repository, repository.state(scope_key, index.COMPONENT))
            get = directory.get

            def selected_get(collection, identities, _get=get, **kwargs):
                assert collection != index.DESCRIPTIONS
                return _get(collection, identities, **kwargs)

            directory.get = selected_get
            previous = members[-1]
            changed = replace(
                previous,
                value=dict(
                    previous.value,
                    source_amount_fen="200",
                    outstanding_fen="200",
                    current_outstanding_fen="200",
                ),
            )
            patched, deleted = [], set()
            _brief_open_update._groups(
                directory,
                {previous.item_id: previous.value},
                [changed],
                deepcopy(categories),
                patched,
                deleted,
            )
            _, expected_entries, expected_folds, _ = _brief_open_groups.prepare(
                [*members[:-1], changed],
                "2026-01",
                "2026-01",
                complete=True,
            )
            assert (
                next(item.value for item in patched if item.collection == index.FOLDS)
                == expected_folds["open:fixed"]
            )
            assert next(item.value for item in patched if item.collection == "open_items") == next(
                item.value for item in expected_entries if item.collection == "open_items"
            )
