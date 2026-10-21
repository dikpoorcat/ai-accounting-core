"""One same-transaction selection for stock, flow, classification and their sources."""

import json
from collections import Counter, defaultdict
from types import MappingProxyType

from ai_accounting_modular.contracts.public import parse_fen, validate_contract
from ai_accounting_modular.ledger.opening.public import is_zero_origin

from ._balances import sum_amounts
from ._financial_cells import known, zeros
from ._financial_order import CHILD_KEYS, head_key, target_key
from ._formal_output import target_vouchers
from ._planning import stored_source_reference
from ._profit import profit_rows
from ._report_details import hydrate_report_contributions
from ._values import fail

_SOURCE_FIELDS = (
    "business_id",
    "result_slot",
    "calculation_key",
    "calculation_revision",
    "basis_digest",
    "target_digest",
)


def _identity(head):
    # A bool or float must not hit an already validated integer generation.
    return head["result_id"], type(head["generation"]), head["generation"]


class Selection:
    def __init__(self, repo, view, end, start, issues):
        self.repo, self.view, self.end, self.start, self.issues = repo, view, end, start, issues
        self.heads, self._stocks, self._flows, self._lines, self._children, self._bases = (
            {},
            {},
            {},
            {},
            {},
            {},
        )
        self.used = set()
        self._balance_cubes = {}
        self._details = {}
        self._sources, self._targets = {}, {}
        self._raw_targets = {}
        self._profit_rows = {}
        boundaries = repo.financial_boundaries(as_of=end if view == "as_posted" else None)
        self.opening = self.continuation = None
        for head in boundaries:
            self.heads[head["result_id"]] = head
            basis = self.basis(head)
            kind = self.boundary_kind(head)
            if basis["period"] != start:
                continue
            attribute = "opening" if kind == "opening.package" else "continuation"
            if getattr(self, attribute) is not None:
                fail("dependency_conflict", "同一建账起点存在多个正式期初或接续结果。")
            setattr(self, attribute, head)
        if self.continuation:
            data = self.basis(self.continuation)["input"]
            if self.opening is None or data["opening_package_id"] != self.opening["business_id"]:
                fail("dependency_conflict", "正式接续没有引用同一报告所采用的期初包。")

    def basis(self, head):
        result_id = head["result_id"]
        if result_id not in self._bases:
            self.load_bases([head])
        return self._bases[result_id]

    def load_bases(self, heads):
        missing = [head for head in heads if head["result_id"] not in self._bases]
        needed = [head["result_id"] for head in missing if "basis_json" not in head]
        bodies = {head["result_id"]: head for head in self.repo.results(needed)} if needed else {}
        for head in missing:
            value = json.loads(bodies.get(head["result_id"], head)["basis_json"])
            validate_contract("business-facts.schema.json", "AdoptedBasis", value)
            self._bases[head["result_id"]] = value

    def source(self, head):
        key = _identity(head)
        signature = tuple((type(head[field]), head[field]) for field in _SOURCE_FIELDS)
        saved = self._sources.get(key)
        if saved is None:
            saved = signature, stored_source_reference(head)
            self._sources[key] = saved
        elif saved[0] != signature:
            fail("storage_corrupt", "同一正式结果的精确来源在财务读取中不一致。")
        self.used.add(head["result_id"])
        # Sources enter public DTOs. Their scalar dictionaries cannot expose this cache.
        return dict(saved[1])

    def target(self, head):
        """Borrow an internal Target for read-only calculations; never export or mutate it."""
        key = _identity(head)
        if "target_json" not in head:
            self.load_targets([head])
        raw = head["target_json"] if "target_json" in head else self._raw_targets[key]
        saved = self._targets.get(key)
        if saved is None:
            saved = raw, json.loads(raw)
            self._targets[key] = saved
        elif saved[0] != raw:
            fail("storage_corrupt", "同一正式结果的目标正文在财务读取中不一致。")
        return saved[1]

    def load_targets(self, heads):
        """Batch only requested raw bodies; decoding stays at its original calculation step."""
        missing = [
            head
            for head in heads
            if "target_json" not in head and _identity(head) not in self._raw_targets
        ]
        if missing:
            bodies = {
                _identity(row): row["target_json"]
                for row in self.repo.financial_targets([head["result_id"] for head in missing])
            }
            for head in missing:
                if _identity(head) not in bodies:
                    fail("storage_corrupt", "财务读取所需的精确正式目标不存在。")
                self._raw_targets[_identity(head)] = bodies[_identity(head)]

    def profit_rows(self, head, lines):
        """Borrow main rows of already selected journals, only within this read selection."""
        key = _identity(head)
        if key not in self._profit_rows:
            self._profit_rows[key] = MappingProxyType(profit_rows(lines))
        return self._profit_rows[key]

    def boundary_kind(self, head):
        kind = head["business_kind"]
        if not isinstance(kind, str) or not kind:
            fail("storage_corrupt", "财务读取的正式结果缺少业务类型。")
        return kind

    def kind(self, head):
        return head["target_kind"]

    def stock(self, cutoff):
        if cutoff not in self._stocks:
            heads = self.repo.financial_heads(cutoff, view=self.view, metadata_only=True)
            selected = {
                h["result_id"]: h
                for h in sorted(heads, key=target_key)
                if self.boundary_kind(h) not in ("opening.package", "reports.continuation")
            }
            self.heads.update(selected)
            self._stocks[cutoff] = selected
        return self._stocks[cutoff]

    def flow(self, first, last):
        key = first, last
        if key not in self._flows:
            if self.view == "current":
                heads = [h for h in self.stock(last).values() if first <= h["business_period"]]
            else:
                heads = sorted(
                    self.repo.adoptions_between(first, last, metadata_only=True), key=head_key
                )
            heads = [
                h
                for h in heads
                if self.boundary_kind(h) not in ("opening.package", "reports.continuation")
            ]
            self.heads.update((h["result_id"], h) for h in heads)
            coefficients = Counter(h["result_id"] for h in heads)
            if self.view == "as_posted":
                ids = {h["baseline_result_id"] for h in heads if h["baseline_result_id"]}
                missing = ids - self.heads.keys()
                if missing:
                    self.heads.update(
                        (h["result_id"], h)
                        for h in self.repo.financial_results(missing, metadata_only=True)
                    )
                coefficients.subtract(
                    h["baseline_result_id"] for h in heads if h["baseline_result_id"]
                )
            self._flows[key] = {identity: sign for identity, sign in coefficients.items() if sign}
        return self._flows[key]

    def lines(self, heads):
        missing = [head for head in heads if head["result_id"] not in self._lines]
        if missing:
            journals = target_vouchers(self.repo, missing, headers_only=True)
            ids = {row["voucher_id"] for row in journals.values() if row is not None}
            grouped = defaultdict(list)
            for row in self.repo.financial_lines(ids) if ids else ():
                grouped[row["voucher_id"]].append(row)
            for head_id, voucher in journals.items():
                self._lines[head_id] = (
                    sorted(grouped[voucher["voucher_id"]], key=lambda row: row["line_no"])
                    if voucher
                    else []
                )
        for head in heads:
            if self._lines[head["result_id"]]:
                self.source(head)
        return {head["result_id"]: self._lines[head["result_id"]] for head in heads}

    def children(self, table, heads):
        cache = self._children.setdefault(table, {})
        missing = [head["result_id"] for head in heads if head["result_id"] not in cache]
        if missing:
            cache.update((key, []) for key in missing)
            for row in self.repo.children(table, missing):
                cache[row["result_id"]].append(row)
            field = CHILD_KEYS[table]
            for result_id in missing:
                cache[result_id].sort(key=lambda row: row[field])
        return {head["result_id"]: cache[head["result_id"]] for head in heads}

    def report_details(self, heads):
        missing = [head for head in heads if head["result_id"] not in self._details]
        if missing:
            self._details.update(hydrate_report_contributions(self.repo, missing))
        for head in heads:
            if self._details[head["result_id"]]:
                self.source(head)
        return {head["result_id"]: self._details[head["result_id"]] for head in heads}

    def position_resources(self, heads, accounts):
        ids = tuple(dict.fromkeys(head["result_id"] for head in heads))
        order = {identity: index for index, identity in enumerate(ids)}
        records = self.repo.financial_resource_rows(ids, accounts)
        grouped = {}
        for table, rows in records.items():
            values = defaultdict(list)
            for row in rows:
                values[row["result_id"]].append(row)
            field = CHILD_KEYS[table]
            grouped[table] = {
                identity: sorted(values[identity], key=lambda row: row[field])
                for identity in sorted(values, key=order.__getitem__)
            }
        return grouped

    def opening_rows(self):
        if self.opening is None:
            return None
        head = self.opening
        self.source(head)
        quantities = self.basis(head)["adopted_quantities"]
        values = [item for item in quantities if item["type"] == "opening_balance"]
        target = self.target(head)["values"]
        for field in ("debit_fen", "credit_fen"):
            if sum_amounts(parse_fen(item[field]) for item in values) != parse_fen(target[field]):
                fail("storage_corrupt", "正式期初的采用余额与完整目标总额不一致。")
        return values

    def prior(self, statement, first, last):
        count = 32 if statement == "profit" else 22
        result = zeros(count)
        if first >= self.start:
            return result
        column = (
            "year_to_date_fen"
            if first == self.start[:4] + "-01"
            else "quarter_to_date_fen"
            if first == f"{self.start[:4]}-{((int(self.start[5:]) - 1) // 3) * 3 + 1:02d}"
            else None
        )
        if self.continuation and column and last >= self.start:
            self.source(self.continuation)
            data = self.basis(self.continuation)["input"]
            return {
                item["line"]: known(parse_fen(item[column])) for item in data["prior_" + statement]
            }
        if self.opening and not self.continuation and last >= self.start:
            target = self.target(self.opening)
            if is_zero_origin(target, self.basis(self.opening)) and first[:4] == self.start[:4]:
                self.source(self.opening)
                return result
        return {
            line: self.issues.unknown(
                f"/{statement}_statement/{line}/prior",
                "接账前报表接续",
                "所查询范围缺少可采用的逐期或累计接续依据。",
            )
            for line in result
        }
