"""Publish one complete employee month and serve its indexed collections."""

from collections import Counter, defaultdict

from ai_accounting_modular.contracts.public import ContractError, PageQuery

from . import _employees_amounts as amounts
from . import _employees_relationships as relationships
from . import _monthly
from ._collections import CollectionFilter, IndexItem, prepare_items, prepare_model, wire_page
from ._employees_sources import labor_sources

_COLLECTIONS = ("employees", "labor_workers", "labor_sources")


def prepare(reader, scope, read):
    activity = reader.people.remuneration_activity(scope, read)
    objects, relations = relationships.people(reader, scope, read)
    by_id = {obj["object_id"]: obj for obj in objects}
    roster = {item.person_id: set(item.roles) for item in activity.roster}
    if set(roster) - set(by_id):
        raise ContractError("storage_corrupt", "正式薪酬人员缺少本公司人员身份。")
    persons = {person.person_id: person for person in activity.persons}
    events = defaultdict(list)
    for event in activity.settlements:
        events[event.person_id].append(event)
    sources = labor_sources(reader, activity, by_id, relations, read)
    counts = Counter(source["person_id"] for source in sources)
    employees, workers, entries = [], [], []
    month = scope["period_to"]
    employee_objects = [
        (index, obj)
        for index, obj in enumerate(objects)
        if "employee" in roster.get(obj["object_id"], ())
        or relationships.known_role(relations[obj["object_id"]], month)
        or (obj["object_id"] in persons and persons[obj["object_id"]].has_payroll_activity)
    ]
    employee_objects.sort(
        key=lambda item: relationships.employee_order(
            relations[item[1]["object_id"]], month, item[0]
        )
    )
    for index, (_, obj) in enumerate(employee_objects):
        person_id = obj["object_id"]
        relation = relations[person_id]
        value = amounts.employee(
            obj,
            relation,
            persons.get(person_id),
            events[person_id],
            month,
            outstanding_known=activity.summary.outstanding_employee_net_fen is not None,
        )
        employees.append(value)
        entries.append(
            IndexItem(
                "employees",
                person_id,
                f"{index:020d}",
                value,
                object_id=person_id,
                category=relationships.category(relation),
            )
        )
    for obj in objects:
        person_id = obj["object_id"]
        relation = relations[person_id]
        if (
            "labor" not in roster.get(person_id, ())
            and not relationships.known_role(relation, month, labor=True)
            and not counts[person_id]
        ):
            continue
        value = amounts.labor_worker(
            obj, relation, persons.get(person_id), counts[person_id], month
        )
        entries.append(
            IndexItem(
                "labor_workers",
                person_id,
                f"{len(workers):020d}",
                value,
                object_id=person_id,
                category=relationships.category(relation, labor=True),
            )
        )
        workers.append(value)
    entries.extend(
        IndexItem(
            "labor_sources",
            source["source_id"],
            f"{index:020d}",
            source,
            business_id=source["subject_id"],
            object_id=source["person_id"],
        )
        for index, source in enumerate(sources)
    )
    categories = {
        name: sorted({item.category for item in entries if item.collection == name})
        for name in ("employees", "labor_workers")
    }
    selected_ids = {item["employee_id"] for item in employees} | {
        item["person_id"] for item in workers
    }
    saved = amounts.summary(
        activity,
        employees,
        workers,
        checking=any(relations[person_id].issues for person_id in selected_ids),
    )
    saved.update(_owner_version=activity.version, _categories=categories)
    return prepare_model(saved, items=prepare_items(entries, collections=_COLLECTIONS))


def build(snapshot, read, query, *, identity):
    saved = snapshot.components["employees"]
    person_id = query.get("employee_id")
    if person_id is not None:
        objects = identity.get_batch([person_id], read)
        if not objects or objects[0]["kind"] != "person":
            raise ContractError("not_found", "本公司没有所定位的人员身份。")
    selected = query.get("employee_filter", "employment_active")
    focused_default = person_id is not None and "employee_filter" not in query
    requested = (query["section"],) if "section" in query else ("employees", "labor_workers")
    collections = {}
    for name in requested:
        categories = (
            ()
            if focused_default or name == "labor_sources" or selected == "all"
            else tuple(
                value for value in saved["_categories"][name] if selected in value.split("|")
            )
        )
        # An absent category must match nothing; an empty selector otherwise means all.
        if not focused_default and name != "labor_sources" and selected != "all" and not categories:
            categories = ("no_matching_relationship",)
        page = _monthly.collection(
            snapshot,
            "employees",
            name,
            CollectionFilter(object_id=person_id, categories=categories),
            PageQuery(query.get("limit", 20), query.get("cursor")),
            read,
        )
        collections[name] = wire_page(page)
    data = {name: value for name, value in saved.items() if not name.startswith("_")}
    data.update(employee_id=person_id, employee_filter=selected, collections=collections)
    return {
        "selected_period": snapshot.selected_period,
        "snapshot_version": snapshot.version,
        "data": data,
    }
