"""Legal object names remain readable through Open's actual grouping and sort keys."""

from copy import deepcopy
from uuid import uuid4

from ai_accounting_modular.contracts.public import loads_json
from ai_accounting_modular.reading import _brief_open_index, _monthly
from ai_accounting_modular.reading._funds_items import name_key
from ai_accounting_modular.reading._open_store import NODES
from tests.modular.application._working_case import case as case

from .test_brief_open import apply, page, publish


def test_nine_legal_long_names_with_two_matters_survive_full_patch_and_pages(case):
    case.seed(expense=False)
    names = [letter + "双" * 199 for letter in "abcdefghi"]
    assert all(len(name) == 200 for name in names)
    assert name_key(names[0])[0] == "a" + "shuang" * 199
    objects, facts = [], []
    for name in names:
        identity = str(uuid4())
        objects.append(
            {
                "object_id": identity,
                "kind": "organization",
                "name": name,
                "expected_seq": 0,
                "data": {},
            }
        )
        for matter in ("administration", "sales"):
            fact = case.expense("100")
            fact["data"].update(creditor_id=identity, expense_class=matter)
            facts.append(fact)
    apply(case, *facts, objects=objects)
    snapshot = publish(case)
    with case.kernel.storage.read_unit(case.company) as read:
        repository = read.repository("reading")
        directory = _brief_open_index.base(
            repository, repository.state(snapshot.scope_key, _monthly.BRIEF_OPEN_ITEMS)
        )
        root = directory.roots["open_items"]
        raw = directory.pool.get_blocks(((NODES, root.digest),))[0]
        node = loads_json(raw)
        assert node["kind"] == "branch" and node["oversize"]
        assert len(raw.encode("utf-8")) > node["max_bytes"] == 65536

    def groups(selected):
        result, cursor = [], None
        for size in (7, 7, 4):
            response = page(case, selected, limit=7, cursor=cursor)
            collection = response["collections"]["open_items"]
            assert collection["page"]["total_count"] == 18
            assert len(collection["items"]) == size
            result.extend(collection["items"])
            cursor = collection["page"]["next_cursor"]
            assert collection["page"]["has_more"] == (len(result) < 18)
        assert cursor is None
        return result

    before = groups(snapshot)
    assert [(item["party"], item["description"]) for item in before] == [
        (name, description) for name in names for description in ("管理费用", "销售费用")
    ]
    assert all(item["outstanding_fen"] == "100" for item in before)
    revised = deepcopy(facts[0])
    revised["data"]["amount_fen"] = "200"
    apply(case, mutations=[{"op": "replace", "expected_seq": 1, "fact": revised}])
    assert case.kernel.reading.run_pending(case.company)
    with case.kernel.storage.read_unit(case.company) as read:
        after = _monthly.ready_snapshot(
            case.kernel.reading, snapshot.scope, (_monthly.BRIEF_OPEN_ITEMS,), read
        )
    actual = groups(after)
    assert [(item["party"], item["description"]) for item in actual] == [
        (item["party"], item["description"]) for item in before
    ]
    assert [item["outstanding_fen"] for item in actual] == ["200", *(["100"] * 17)]
