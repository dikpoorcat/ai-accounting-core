"""Classify selected formal payroll resources without consulting current facts."""

from collections import defaultdict
from dataclasses import dataclass, fields

from ai_accounting_modular.contracts.public import (
    BusinessRef,
    ContractError,
    PageQuery,
    canonical_json,
    parse_fen,
)
from ai_accounting_modular.ledger.opening.public import opening_payroll_references
from ai_accounting_modular.ledger.public import AccountBalanceKey

from ._read_types import RemunerationAmounts, RemunerationRosterPerson, RemunerationSource


def remuneration_meaning(source):
    """Compare this owner's monetary and classification meaning, not evidence locators."""
    return (
        source.business_period,
        source.person_id,
        source.remuneration_type,
        source.expense_area,
        source.project_id,
        source.tax_state,
        source.tax_period,
        source.amounts,
    )


def tax_payroll_basis(content):
    """Locate the original payroll quantity and exact adoption cited by a tax target."""
    values = content.target["values"]
    identity = values["remuneration_id"]
    quantities = [
        item
        for item in content.basis["adopted_quantities"]
        if item["type"] == "remuneration"
        and item["source"] == {"business_id": identity, "slot": "main"}
    ]
    sources = [
        item
        for item in content.basis["source_results"]
        if item["business_id"] == identity and item["result_slot"] == "main"
    ]
    if len(quantities) != 1 or len(sources) != 1:
        raise ContractError("storage_corrupt", "薪酬税款缺少唯一的原报酬采用依据。")
    quantity = quantities[0]
    if any(
        quantity["values"][field] != values[field] for field in ("person_id", "remuneration_type")
    ):
        raise ContractError("storage_corrupt", "薪酬税款与原报酬人员或类别不符。")
    return quantity, sources[0]


def batches(values):
    values = tuple(values)
    for offset in range(0, len(values), 500):
        yield values[offset : offset + 500]


OUTSTANDING_ACCOUNTS = ("221101", "224104")


def outstanding_basis(ledger, month, read, selection):
    """A missing opening cannot make the observed obligations a complete accumulated balance."""
    rows = ledger.account_balances(
        tuple(AccountBalanceKey(code) for code in OUTSTANDING_ACCOUNTS),
        month,
        read,
        selection=selection,
    )
    issues = {}
    for row in rows:
        if row.ending["state"] != "known":
            for index in row.ending["issue_indexes"]:
                issue = row.issues[index]
                issues[canonical_json(issue)] = issue
    return tuple(row.ending["state"] == "known" for row in rows), tuple(issues.values())


@dataclass(frozen=True)
class _Source:
    person_id: str
    period: str
    kind: str
    component: str
    project_id: str | None
    reference: BusinessRef


class Contents:
    def __init__(self, ledger, read, selection):
        self.ledger, self.read, self.selection = ledger, read, selection
        self.exact, self.selected, self.loaded, self.opening = {}, {}, set(), {}
        self.remunerations = {}
        self.roster_business_ids = set()

    def load(self, refs):
        missing = {ref["result_id"]: ref for ref in refs if ref["result_id"] not in self.exact}
        for batch in batches(missing.values()):
            for content in self.ledger.get_adopted_contents(batch, self.read):
                self.exact[content.reference["result_id"]] = content

    def select(self, business_ids):
        for batch in batches(sorted(set(business_ids) - self.loaded)):
            refs = self.ledger.get_result_refs(batch, self.read, selection=self.selection)
            refs = [ref for ref in refs if ref["result_slot"] == "main"]
            self.load(refs)
            self.selected.update((ref["business_id"], self.exact[ref["result_id"]]) for ref in refs)
            self.loaded.update(batch)

    def load_sources(self, sources):
        sources = tuple(sources)
        for item in sources:
            if item["result_id"] in self.exact and self.exact[item["result_id"]].source != item:
                raise ContractError("storage_corrupt", "薪酬采用来源与精确正文不一致。")
        missing = {
            item["result_id"]: item for item in sources if item["result_id"] not in self.exact
        }
        for batch in batches(missing.values()):
            found = self.ledger.get_adopted_sources(batch, self.read)
            if len(found) != len(batch):
                raise ContractError("storage_corrupt", "薪酬采用来源缺少精确定位。")
            self.load(item.reference for item in found)
            if any(self.exact[item["result_id"]].source != item for item in batch):
                raise ContractError("storage_corrupt", "薪酬采用来源与精确正文不一致。")

    def remuneration(self, content):
        if content.target["kind"] != "people.remuneration":
            return None
        key = content.reference["result_id"]
        if key not in self.remunerations:
            values, basis = content.target["values"], content.basis
            data = basis["input"]
            destination = data.get("destination", {})
            self.remunerations[key] = RemunerationSource(
                content.reference,
                content.source,
                basis["period"],
                values["person_id"],
                values["remuneration_type"],
                destination.get("area", data.get("expense_area")),
                destination.get("project_id"),
                values["tax_state"],
                values.get("tax_period"),
                RemunerationAmounts(
                    **{
                        field.name: parse_fen(values[field.name])
                        for field in fields(RemunerationAmounts)
                    }
                ),
            )
        return self.remunerations[key]

    def opening_members(self, content):
        result_id = content.reference["result_id"]
        if result_id not in self.opening:
            self.opening[result_id] = {
                item.source["slot"]: item for item in opening_payroll_references(content)
            }
        return self.opening[result_id]

    def roster(self, scope):
        roles, opening_refs = defaultdict(set), []
        page = PageQuery(500)
        while True:
            found = self.ledger.query_adopted_targets(
                scope,
                ("people.remuneration", "opening.package"),
                page,
                self.read,
                result_slots=("main",),
            )
            for item in found.items:
                if item.target["kind"] == "people.remuneration":
                    self.roster_business_ids.add(item.reference["business_id"])
                    values = item.target["values"]
                    roles[values["person_id"]].add(
                        "labor" if values["remuneration_type"] == "labor" else "employee"
                    )
                elif item.target["kind"] == "opening.package":
                    opening_refs.append(item.reference)
            if found.next_cursor is None:
                break
            page = PageQuery(500, found.next_cursor, found.version)
        self.load(opening_refs)
        for reference in opening_refs:
            for member in self.opening_members(self.exact[reference["result_id"]]).values():
                roles[member.person_id].add("employee")
        return tuple(
            RemunerationRosterPerson(
                person_id, tuple(role for role in ("employee", "labor") if role in own)
            )
            for person_id, own in sorted(roles.items())
        )

    def activity_sources(self, entries, positions, events):
        selected = {
            item.effective["result_id"]: self.exact[item.effective["result_id"]] for item in entries
        }
        parents = [
            item.parent_remuneration_source
            for item in entries
            if item.parent_remuneration_source is not None
        ]
        self.load_sources(parents)
        selected.update((item["result_id"], self.exact[item["result_id"]]) for item in parents)
        refs = [
            item.parent_source if item.recovery else item.position.definition.reference
            for item in positions
        ]
        refs.extend(
            item.parent_source if item.recovery else item.event.use.value["source"]
            for item in events
        )
        for ref in refs:
            content = self.selected[ref["business_id"]]
            selected[content.reference["result_id"]] = content
        tax_parents = [
            source
            for content in selected.values()
            if content.target["kind"] == "people.remuneration_tax"
            for source in content.basis["source_results"]
            if source["business_id"] == content.target["values"]["remuneration_id"]
            and source["result_slot"] == "main"
        ]
        self.load_sources(tax_parents)
        selected.update((item["result_id"], self.exact[item["result_id"]]) for item in tax_parents)
        result = [
            source
            for content in selected.values()
            if (source := self.remuneration(content)) is not None
        ]
        return tuple(
            sorted(
                result,
                key=lambda item: (
                    item.business_period,
                    item.person_id,
                    item.reference["business_id"],
                    item.reference["posting_period"],
                    item.reference["result_id"],
                ),
            )
        )

    def source(self, ref):
        content = self.selected.get(ref["business_id"])
        if content is None:
            raise ContractError("storage_corrupt", "薪酬资源缺少所选采用的正式来源。")
        remuneration = self.remuneration(content)
        if remuneration is not None:
            if ref["slot"] not in {
                "net_pay",
                "tax",
                "employee_social",
                "employee_housing",
                "employer_social",
                "employer_housing",
            }:
                raise ContractError("storage_corrupt", "正式薪酬引用了不存在的组成。")
            return _Source(
                remuneration.person_id,
                remuneration.business_period,
                remuneration.remuneration_type,
                ref["slot"],
                remuneration.project_id,
                ref,
            )
        if content.target["kind"] == "people.remuneration_tax":
            values = content.target["values"]
            quantity, _ = tax_payroll_basis(content)
            if ref["slot"] != "tax":
                raise ContractError("storage_corrupt", "薪酬税款引用缺少正式报酬组成。")
            return _Source(
                values["person_id"],
                quantity["period"],
                values["remuneration_type"],
                "tax",
                quantity.get("project_id"),
                ref,
            )
        if content.target["kind"] == "opening.package":
            item = self.opening_members(content).get(ref["slot"])
            if item is not None:
                return _Source(
                    item.person_id,
                    item.payroll_period,
                    "opening",
                    "net_pay" if item.component == "net" else item.component,
                    None,
                    item.source,
                )
        return None
