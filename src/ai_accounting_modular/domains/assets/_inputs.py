"""Indexes owned by one synchronous domain evaluation, never by a service instance."""

from collections import defaultdict

from ._common import assets_in, reject

_BATCH = 500
_ASSET_KINDS = {"assets.acquisition", "assets.activation", "assets.disposal", "opening.package"}


def batches(values):
    values = tuple(values)
    for offset in range(0, len(values), _BATCH):
        yield values[offset : offset + _BATCH]


def reference_key(ref):
    return ref["business_id"], ref["slot"]


class AssetInputs:
    """One fixed candidate view: fetch shared sources once, then address by identity.

    A fresh instance is created for every describe/evaluate/validate call. The
    application cannot stage another result during such a synchronous call, so
    these indexes never survive a candidate change or a transaction boundary.
    """

    def __init__(self, context, *, facts=(), shadowed=()):
        self._context = context
        self._facts = {fact["business_id"]: fact for fact in facts}
        self._shadowed = set(shadowed)
        self._results, self._objects, self._costs = {}, {}, {}
        self._cards, self._activation_cards = {}, {}
        self._adopted_cards = {}
        self._related = defaultdict(dict)
        self._indexed_facts = set()
        self._loaded_assets = set()
        self._scopes_loaded = False
        for fact in self._facts.values():
            self._index_relations(fact)

    def __getattr__(self, name):
        return getattr(self._context, name)

    def get_fact(self, business_id):
        if business_id not in self._facts:
            self._facts[business_id] = self._context.get_fact(business_id)
        return self._facts[business_id]

    def get_object(self, object_id):
        if object_id not in self._objects:
            self._objects[object_id] = self._context.get_object(object_id)
        return self._objects[object_id]

    def get_result(self, business_id, result_slot):
        key = business_id, result_slot
        if key not in self._results:
            self._results[key] = self._context.get_result(business_id, result_slot)
        return self._results[key]

    def _index_relations(self, fact):
        if fact["kind"] not in _ASSET_KINDS or fact["business_id"] in self._indexed_facts:
            return
        self._indexed_facts.add(fact["business_id"])
        for item in assets_in(fact):
            self._related[item["asset_id"]][fact["business_id"]] = fact

    def load_assets(self, asset_ids):
        requested = set(asset_ids)
        if requested and not self._scopes_loaded:
            start = self.read_context.start_period
            facts = (
                *self._context.facts_in_scope(("opening.package",), start, start),
                *self._context.facts_in_scope(
                    ("assets.disposal",), start, self.evaluation_period_to
                ),
            )
            for fact in facts:
                if fact["business_id"] not in self._shadowed:
                    self._index_relations(fact)
            self._scopes_loaded = True
        missing = sorted(requested - self._loaded_assets)
        for batch in batches(missing):
            facts = self._context.related_facts_many(batch, "identity", producer_kind="object")
            for fact in facts:
                if fact["business_id"] not in self._shadowed:
                    self._index_relations(fact)
            self._loaded_assets.update(batch)

    def assets_related(self, asset_id, kind):
        self.load_assets((asset_id,))
        return tuple(
            fact for _, fact in sorted(self._related[asset_id].items()) if fact["kind"] == kind
        )

    def asset_card(self, fact, asset_id):
        business_id = fact["business_id"]
        if business_id not in self._cards:
            self._cards[business_id] = {item["asset_id"]: item for item in assets_in(fact)}
        return self._cards[business_id][asset_id]

    def activation_cards(self, fact, build):
        business_id = fact["business_id"]
        if business_id not in self._activation_cards:
            self._activation_cards[business_id] = build()
        return self._activation_cards[business_id]

    def adopted_asset_card(self, result, asset_id):
        key = result.source["business_id"], result.source["result_slot"]
        if key not in self._adopted_cards:
            cards = [
                item for item in result.basis["adopted_quantities"] if item["type"] == "asset_card"
            ]
            mapped = {item["asset_id"]: item for item in cards}
            if len(mapped) != len(cards):
                reject("正式来源对同一资产提供了重复卡片依据。")
            self._adopted_cards[key] = mapped
        card = self._adopted_cards[key].get(asset_id)
        if card is None:
            reject("正式来源缺少本资产的必要卡片依据。")
        return card

    def load_costs(self, refs):
        missing = {reference_key(ref): ref for ref in refs if reference_key(ref) not in self._costs}
        for batch in batches(missing.values()):
            definitions = self._context.resource_definitions(batch, "cost")
            expected = {reference_key(ref) for ref in batch}
            actual = {reference_key(item.reference) for item in definitions}
            if (
                len(definitions) != len(expected)
                or actual != expected
                or any(item.kind != "cost" for item in definitions)
            ):
                reject("公开成本批量读取缺失、重复或返回了其他身份。")
            self._costs.update((reference_key(item.reference), item) for item in definitions)

    def resource_definitions(self, refs, kind):
        if kind != "cost":
            return self._context.resource_definitions(refs, kind)
        self.load_costs(refs)
        return tuple(self._costs[reference_key(ref)] for ref in refs)
