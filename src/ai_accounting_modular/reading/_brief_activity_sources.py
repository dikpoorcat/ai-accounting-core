"""Resolve exact formal bodies once; never substitute a currently selected business."""

from ai_accounting_modular.contracts.public import ContractError, PageQuery
from ai_accounting_modular.ledger.opening.public import opening_payroll_references
from ai_accounting_modular.ledger.public import AdoptedContent


def same_reference(left, right):
    # State and optional voucher conveniences can change without changing adoption identity.
    return all(
        left[key] == right[key]
        for key in (
            "business_id",
            "result_id",
            "result_slot",
            "generation",
            "business_period",
            "posting_period",
        )
    )


def pages(method, scope, read, **kwargs):
    cursor = None
    while True:
        page = method(scope, (), PageQuery(500, cursor), read, **kwargs)
        yield from page.items
        cursor = page.next_cursor
        if cursor is None:
            return


class Contents:
    def __init__(self, ledger, read):
        self.ledger, self.read, self.saved = ledger, read, {}
        self._opening = {}

    def reuse(self, contents):
        """Borrow exact Ledger outputs explicitly passed by this preparation's caller."""
        for content in contents:
            if not isinstance(content, AdoptedContent):
                raise ContractError("storage_corrupt", "复用正文须来自同次正式读取。")
            ref, source = content.reference, content.source
            if (
                type(ref["generation"]) is not int
                or type(source["result_generation"]) is not int
                or source["result_generation"] != ref["generation"]
                or any(
                    source[key] != ref[key] for key in ("business_id", "result_id", "result_slot")
                )
            ):
                raise ContractError("storage_corrupt", "复用正文的定位与完整来源身份不一致。")
            identity = ref["result_id"], ref["generation"]
            old = self.saved.get(identity)
            if old is not None and (
                not same_reference(old.reference, ref)
                or old.source != source
                or old.target != content.target
                or old.basis != content.basis
            ):
                raise ContractError("storage_corrupt", "复用正文的精确定位或完整来源相互冲突。")
            if old is None:
                self.saved[identity] = content

    def opening(self, content):
        identity = content.source["result_id"], content.source["result_generation"]
        if identity not in self._opening:
            self._opening[identity] = {
                (item.source["business_id"], item.source["slot"]): item
                for item in opening_payroll_references(content)
            }
        return self._opening[identity]

    def load(self, references):
        requested = {}
        for ref in references:
            identity = ref["result_id"], ref["generation"]
            old = self.saved.get(identity)
            if old is not None:
                if not same_reference(old.reference, ref):
                    raise ContractError("storage_corrupt", "同一正式结果的精确定位不一致。")
            else:
                if identity in requested and not same_reference(requested[identity], ref):
                    raise ContractError("storage_corrupt", "同一正式结果出现相互冲突的定位。")
                requested[identity] = ref
        values = list(requested.values())
        for start in range(0, len(values), 500):
            expected = values[start : start + 500]
            actual = self.ledger.get_adopted_contents(expected, self.read)
            if len(actual) != len(expected):
                raise ContractError("storage_corrupt", "本月业务所需的精确正式正文不完整。")
            for content, ref in zip(actual, expected, strict=True):
                if not same_reference(content.reference, ref):
                    raise ContractError("storage_corrupt", "正式正文与请求的采用定位不一致。")
                self.saved[ref["result_id"], ref["generation"]] = content

    def get(self, ref):
        return self.saved[ref["result_id"], ref["generation"]]

    def sources(self, contents):
        self.load_sources(
            source for content in contents for source in content.basis["source_results"]
        )

    def load_sources(self, sources):
        requested = {}
        for source in sources:
            identity = source["result_id"], source["result_generation"]
            old = self.saved.get(identity)
            if old is not None:
                if old.source != source:
                    raise ContractError("storage_corrupt", "正式业务采用来源的摘要不一致。")
            else:
                if identity in requested and requested[identity] != source:
                    raise ContractError("storage_corrupt", "同一正式来源出现相互冲突的摘要。")
                requested[identity] = source
        values = list(requested.values())
        for start in range(0, len(values), 500):
            expected = values[start : start + 500]
            actual = self.ledger.get_adopted_sources(expected, self.read)
            if len(actual) != len(expected) or any(
                item.source != source for item, source in zip(actual, expected, strict=True)
            ):
                raise ContractError("storage_corrupt", "正式业务的来源定位不完整。")
            self.load([item.reference for item in actual])

    def source(self, content, business_id, *, result_slot=None):
        found = [
            source
            for source in content.basis["source_results"]
            if source["business_id"] == business_id
            and (result_slot is None or source["result_slot"] == result_slot)
        ]
        if len(found) != 1:
            raise ContractError("storage_corrupt", "业务分项缺少唯一的精确正式来源。")
        source = found[0]
        identity = source["result_id"], source["result_generation"]
        if identity not in self.saved:
            self.load_sources((source,))
        actual = self.saved[identity]
        if actual.source != source:
            raise ContractError("storage_corrupt", "业务分项的精确来源与正式正文不一致。")
        return actual


def identities(identity, ids, read):
    found, values = {}, sorted(set(ids))
    for start in range(0, len(values), 500):
        found.update(
            (item["object_id"], item)
            for item in identity.get_batch(values[start : start + 500], read)
        )
    return found
