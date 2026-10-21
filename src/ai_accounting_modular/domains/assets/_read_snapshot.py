"""Select formal lifecycle evidence once for one synchronous asset projection."""

from collections import defaultdict

from ai_accounting_modular.contracts.public import ContractError, ReadInputs
from ai_accounting_modular.ledger.public import CostPositionQuery

from ._common import add_months, total
from ._inputs import batches
from ._read_support import directory, objects, selected_scope, values
from ._read_types import AssetPaymentSummary

_LIFECYCLE = ("assets.acquisition", "assets.activation", "assets.disposal", "opening.package")
_FLOW = ("assets.acquisition", "assets.activation", "assets.disposal", "assets.consumption")
_DIRECTORY = (*_LIFECYCLE, "assets.consumption")


def quantities(content):
    return {
        item["asset_id"]: item
        for item in content.basis.get("adopted_quantities", ())
        if item["type"] == "asset_card"
    }


class AssetSnapshot:
    def __init__(self, owner, scope, read):
        self.scope, self.selection = selected_scope(scope, read)
        self.owner, self.read = owner, read
        self.cutoff = min(self.scope["period_to"], self.selection.as_of or self.scope["period_to"])
        self.contents, self.mains = {}, {}
        self._card_inputs, self._consumption_items = {}, {}
        self._payments = {}
        self.asset_objects = tuple(objects(owner._identity, "asset", read))
        self.objects = {obj["object_id"]: obj for obj in self.asset_objects}
        self.entries = tuple(directory(owner._ledger, self.scope, _DIRECTORY, read))
        self.load_refs(
            entry.reference for entry in self.entries if entry.reference["result_slot"] == "main"
        )
        self.origins, self.activations, self.exits = {}, {}, {}
        for content in self.mains.values():
            kind = content.target["kind"]
            if kind == "assets.acquisition":
                for card in content.target["values"]["assets"]:
                    self._unique(self.origins, card["asset_id"], (content, card), "成本来源")
            elif kind == "opening.package":
                for asset_id, card in quantities(content).items():
                    self._unique(self.origins, asset_id, (content, card), "成本来源")
                    self._unique(self.activations, asset_id, (content, card), "启用来源")
            elif kind == "assets.activation":
                for asset_id, card in quantities(content).items():
                    self._unique(self.activations, asset_id, (content, card), "启用来源")
            elif kind == "assets.disposal":
                self._unique(self.exits, content.target["values"]["asset_id"], content, "退出来源")
        if (set(self.origins) | set(self.activations) | set(self.exits)) - set(self.objects):
            raise ContractError("storage_corrupt", "正式资产来源缺少本公司资产身份。")
        self.consumptions = self._consumptions()
        self.flows = tuple(values(owner._ledger, self.scope, _FLOW, read))
        self.project_costs = owner._ledger.capture_cost_directory(
            CostPositionQuery(self.cutoff, categories=("project",)),
            read,
            selection=self.selection,
        )
        self.ensure_mains(item.definition.reference["business_id"] for item in self.project_costs)
        self.payment_sources = self._payment_sources()
        payable_ids = {
            business for sources in self.payment_sources.values() for business in sources
        }
        payable_ids.update(item.definition.reference["business_id"] for item in self.project_costs)
        self.payables = defaultdict(list)
        for batch in batches(sorted(payable_ids)):
            for item in owner._ledger.get_obligation_positions(
                self.cutoff, read, business_ids=batch, selection=self.selection
            ):
                if item.definition.value["direction"] == "payable":
                    self.payables[item.definition.reference["business_id"]].append(item)
        extra_ids = set()
        extra_ids.update(item.definition.value["object_id"] for item in self.project_costs)
        for content in self.mains.values():
            data = content.basis.get("input", {})
            extra_ids.update(
                data[key]
                for key in ("project_id", "supplier_id", "person_id", "buyer_id")
                if key in data
            )
        for batch in batches(sorted(extra_ids - set(self.objects))):
            self.objects.update(
                (obj["object_id"], obj) for obj in owner._identity.get_batch(batch, read)
            )
        self.project_order = (
            {
                obj["object_id"]: index
                for index, obj in enumerate(objects(owner._identity, "project", read))
            }
            if self.project_costs
            else {}
        )

    @staticmethod
    def _unique(destination, asset_id, value, meaning):
        if asset_id in destination:
            raise ContractError("storage_corrupt", "正式资产存在重复的" + meaning + "。")
        destination[asset_id] = value

    def load_refs(self, refs):
        wanted = {ref["result_id"]: ref for ref in refs if ref["result_id"] not in self.contents}
        for batch in batches(wanted.values()):
            for content in self.owner._ledger.get_adopted_contents(batch, self.read):
                self.contents[content.reference["result_id"]] = content
                if content.reference["result_slot"] == "main":
                    self.mains[content.reference["business_id"]] = content

    def ensure_mains(self, ids):
        for batch in batches(sorted(set(ids) - set(self.mains))):
            self.load_refs(
                entry.reference
                for entry in directory(
                    self.owner._ledger,
                    self.scope,
                    (),
                    self.read,
                    business_ids=batch,
                    result_slots=("main",),
                )
            )

    def input_cards(self, content):
        key = content.reference["result_id"]
        if key not in self._card_inputs:
            data = content.basis["input"]
            items = (
                (member["data"] for member in data["members"] if member["data"]["type"] == "asset")
                if content.target["kind"] == "opening.package"
                else data["items"]
            )
            self._card_inputs[key] = {item["asset_id"]: item for item in items}
        return self._card_inputs[key]

    def consumption_items(self, content):
        key = content.reference["result_id"]
        if key not in self._consumption_items:
            self._consumption_items[key] = {
                item["asset_id"]: item for item in content.target["values"]["items"]
            }
        return self._consumption_items[key]

    def _consumptions(self):
        entries = {
            (entry.reference["business_id"], entry.reference["result_slot"]): entry
            for entry in self.entries
            if entry.business_kind == "assets.consumption"
        }
        chosen = {}
        self.consumption_periods = {}
        for asset_id, (content, card) in self.activations.items():
            end = min(
                self.cutoff, add_months(card["consumption_start"], card["useful_life_months"] - 1)
            )
            if asset_id in self.exits:
                end = min(end, self.exits[asset_id].reference["business_period"])
            first = max(card["consumption_start"], self.read.start_period)
            if end >= first:
                self.consumption_periods[asset_id] = end
                chosen[asset_id] = entries.get(
                    (content.reference["business_id"], "depreciation:" + end)
                )
        self.load_refs(entry.reference for entry in chosen.values() if entry is not None)
        return {
            asset_id: self.contents[entry.reference["result_id"]] if entry is not None else None
            for asset_id, entry in chosen.items()
        }

    def _payment_sources(self):
        result = {}
        for asset_id, (content, _) in self.origins.items():
            data = content.basis.get("input", {})
            if content.target["kind"] == "opening.package":
                result[asset_id] = ()
            elif data["type"] == "project_completion":
                result[asset_id] = tuple(
                    sorted({item["source"]["business_id"] for item in data["sources"]})
                )
            else:
                result[asset_id] = (content.reference["business_id"],)
        return result

    def payment(self, business_ids, scope):
        key = tuple(business_ids), scope
        if key in self._payments:
            return self._payments[key]
        if scope in {"opening", "unestablished"}:
            return AssetPaymentSummary(scope, (), 0, None, None, None, None)
        selected = {
            (item.definition.reference["business_id"], item.definition.reference["slot"]): item
            for business_id in business_ids
            for item in self.payables[business_id]
        }
        items = tuple(selected[key] for key in sorted(selected))
        result = AssetPaymentSummary(
            scope,
            tuple(item.definition.reference for item in items),
            len(items),
            total(item.amounts.amount_fen for item in items),
            total(item.amounts.money_settled_fen for item in items),
            total(
                item.amounts.offset_settled_fen
                + item.amounts.agency_settled_fen
                + item.amounts.transferred_out_fen
                - item.amounts.excess_reclassified_fen
                for item in items
            ),
            total(item.amounts.remaining_fen for item in items),
        )
        self._payments[key] = result
        return result

    def require_object(self, object_id, kind=None):
        obj = self.objects.get(object_id)
        if obj is None or (kind is not None and obj["kind"] != kind):
            raise ContractError("storage_corrupt", "正式资产或项目结果缺少所需的真实对象身份。")
        return obj

    def read_inputs(self):
        return ReadInputs(
            result_kinds=_DIRECTORY,
            cost_categories=("project",),
            business_ids=tuple(
                sorted(
                    {content.reference["business_id"] for content in self.contents.values()}
                    | set(self.payables)
                )
            ),
            object_ids=tuple(sorted(self.objects)),
            object_kinds=("asset",),
        )
