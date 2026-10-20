"""Keep exact adopted heads without treating them as newly calculated plans."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import validate_contract

from ._planning import stored_source_reference
from ._values import fail


def result_key(value):
    return value["business_id"], value["result_slot"]


class RetainedResults:
    def __init__(self, responsibilities, references, repo):
        owners = {item["business_id"]: item for item in responsibilities}
        self.references = {}
        for value in references:
            validate_contract("core.schema.json", "ResultRef", value)
            key = result_key(value)
            owner = owners.get(key[0])
            if (
                key in self.references
                or owner is None
                or owner["op"] != "recalculate"
                or key[1] not in owner["complete_result_slots"]
            ):
                fail("invalid_input", "保留结果须唯一对应未改事实的本单完整重算责任。")
            self.references[key] = deepcopy(value)
        self.check_current(repo)

    @property
    def keys(self):
        return self.references.keys()

    def check_current(self, repo, *, allow_frozen=False):
        if not self.references:
            return
        actual = {
            result_key(ref): ref
            for ref in repo.result_references(sorted({key[0] for key in self.references}))
        }
        for key, expected in self.references.items():
            found = actual.get(key)
            if allow_frozen and found is not None and expected["state"] == "open":
                expected = {**expected, "state": found["state"]}
            if found != expected:
                fail("conflict", "保留结果的当前身份、代次、期间或凭证已经变化。")


def check_changed_source_refs(repo, keys):
    """Inspect only current open consumers of these changed result slots.

    An immutable consumer or an exactly inherited frozen basis remains its own
    authority. Other consumers must follow the actual current producer, even
    when the application omitted that consumer from this submission.
    """
    if not keys:
        return
    current = {}
    for row in repo.current_basis_refs(keys):
        ref = {
            "business_id": row["producer_business_id"],
            "result_slot": row["producer_result_slot"],
            "result_id": row["producer_result_id"],
            "result_generation": row["producer_generation"],
            "content_digest": row["producer_content_digest"],
        }
        if row["baseline_result_id"] is not None and repo.inherited_basis_ref(
            row["baseline_result_id"], ref
        ):
            continue
        key = result_key(ref)
        if key not in current:
            current[key] = repo.current_result(*key, metadata_only=True)
        source = current[key]
        if source is None or source["target_state"] == "withdrawn":
            fail("dependency_conflict", "仍被当前正式结果采用的来源不能退出。")
        if stored_source_reference(source) != ref:
            fail("dependency_conflict", "来源已更正，当前消费者须采用同一正确结果或必要冻结依据。")
