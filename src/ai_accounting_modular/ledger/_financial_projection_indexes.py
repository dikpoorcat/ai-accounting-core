"""The financial model's ordered roles use one shared immutable tree implementation."""

from __future__ import annotations

from copy import deepcopy

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    checked_fen,
    loads_json,
    validate_contract,
    validate_contract_items,
)
from ai_accounting_modular.storage.public import Entry, Tree
from ai_accounting_modular.storage.public import block_digest as digest

from ._balances import sum_amounts
from ._cashflow import _ROWS, missing_classification
from ._financial_order import head_key, target_key
from ._financial_projection_bodies import COLUMN, validate_body
from ._financial_projection_fold import Fold, Notice, Unknown, combine, values_fold
from ._values import fail


def order_key(parts):
    return "".join(value.replace("\x00", "\x00\x01") + "\x00\x00" for value in parts)


def prefix_end(prefix):
    # All financial component keys end in the fixed NUL,NUL delimiter.
    return prefix[:-1] + "\x01"


def nullable(value):
    return "" if value is None else "\x01" + value


def dimension_key(code, party, obj):
    return order_key((code, nullable(party), nullable(obj)))


def journal_key(body, row):
    return dimension_key(*row[1:4]) + order_key(
        (
            body["business_period"],
            body["source"]["business_id"],
            body["source"]["result_slot"],
            f"{row[0]:019d}",
        )
    )


def body_head(body):
    return body["source"] | {
        "generation": body["source"]["result_generation"],
        "business_period": body["business_period"],
        "posting_period": body["posting_period"],
    }


def adoption_parts(head):
    period, business, slot, generation, identity = head_key(head)
    return period, business, slot, f"{generation:019d}", identity


def flow_source_used(body, phase):
    if phase == "profit":
        return bool(body["journal"] or body["has_report_details"])
    if phase == "cash":
        return bool(body["cash"])
    if phase == "pretax":
        return bool(body["journal"])
    raise ValueError("unknown financial flow phase")


def flow_value(identity, body, sign):
    pretax = (
        [
            body["source"]["business_id"],
            body["source"]["result_slot"],
            body["business_period"],
            *body["pretax"],
        ]
        if body["pretax"] is not None
        else None
    )
    return [identity, sign, pretax]


def _pretax_input(entry):
    value = entry.value
    if (
        type(value) is not list
        or len(value) != 3
        or type(value[0]) is not str
        or type(value[1]) is not int
        or value[1] == 0
    ):
        fail("storage_corrupt", "财务发生角色的真实采用系数损坏。")
    raw = value[2]
    if raw is None:
        return None
    if (
        type(raw) is not list
        or len(raw) != 6
        or type(raw[1]) is not str
        or not raw[1]
        or any(type(amount) is not int for amount in raw[3:])
    ):
        fail("storage_corrupt", "财务税前依据的真实薄切片损坏。")
    return value[1], raw


def _pretax_record(sign, raw):
    return {
        "business_id": raw[0],
        "result_slot": raw[1],
        "period": raw[2],
        "sign": sign,
        "values": raw[3:],
    }


def pretax_slice(entry):
    """The original per-item order also supplies exact diagnostics for failed batches."""
    selected = _pretax_input(entry)
    if selected is None:
        return None
    sign, raw = selected
    validate_contract("core.schema.json", "Id", raw[0])
    validate_contract("core.schema.json", "Month", raw[2])
    for amount in raw[3:]:
        checked_fen(amount)
    return _pretax_record(sign, raw)


def pretax_slices(entries):
    """Validate the same scalar contracts together; failed data keeps its first old error."""
    entries = tuple(entries)
    try:
        selected = [value for entry in entries if (value := _pretax_input(entry)) is not None]
        validate_contract_items("core.schema.json", "Id", [raw[0] for _, raw in selected])
        validate_contract_items("core.schema.json", "Month", [raw[2] for _, raw in selected])
        for _, raw in selected:
            for amount in raw[3:]:
                checked_fen(amount)
    except (ContractError, TypeError, ValueError):
        for entry in entries:
            pretax_slice(entry)
        raise
    return [_pretax_record(sign, raw) for sign, raw in selected]


def flow_entries(values, *, view, first, last, anchors, include_cash):
    """Retain real Source use even if its amounts are all zero or purely BS.

    Values are (Source_atom, body, net coefficient). Counter cancellation happens
    before this boundary; the first positive consumer locates a retained baseline.
    """
    result = []
    for identity, body, coefficient in values:
        if not coefficient:
            continue
        if not (
            flow_source_used(body, "profit")
            or body["profit"]
            or (include_cash and flow_source_used(body, "cash"))
        ):
            continue
        head = body_head(body)
        if view == "current":
            key = order_key(target_key(head))
        elif first <= body["posting_period"] <= last:
            key = order_key(("0", *adoption_parts(head)))
        else:
            if head["result_id"] not in anchors:
                fail("storage_corrupt", "财务发生范围缺少冻结旧腿的首次正式消费定位。")
            key = order_key(("1", *adoption_parts(anchors[head["result_id"]])))
        result.append(Entry(key, flow_value(identity, body, coefficient)))
    return tuple(result)


class PlainStrategy:
    def leaf(self, entries):
        return None

    def branch(self, children):
        return None

    def encode_summary(self, summary):
        if summary is not None:
            fail("storage_corrupt", "财务正文目录不应含金额摘要。")
        return None

    decode_summary = encode_summary


class BodyDirectory:
    """The single company body catalog; returned bodies are internal read-only borrows."""

    BODIES = "_financial_source_bodies"

    def __init__(self, store, descriptor=None):
        self.store = store
        self.tree = Tree("financial-source-bodies/2", PlainStrategy(), store)
        self.root = self.tree.root(descriptor)
        self._bodies, self._lines = {}, {}
        self._writes, self._deletes = {}, set()

    @staticmethod
    def _reference(entry):
        if entry is None or type(value := entry.value) is not str:
            fail("storage_corrupt", "财务角色引用的正式来源正文不在同一公司目录内。")
        try:
            validate_contract("core.schema.json", "Digest", value)
        except ContractError:
            fail("storage_corrupt", "财务角色引用的正式来源正文不在同一公司目录内。")
        return value

    @staticmethod
    def _decode(raw, reference, identity):
        try:
            if type(raw) is not str or digest(raw) != reference:
                raise ValueError
            body = loads_json(raw)
            if canonical_json(body) != raw:
                raise ValueError
            return validate_body(body, identity)
        except (ContractError, TypeError, ValueError, RecursionError) as error:
            raise ContractError(
                "storage_corrupt", "财务来源正文与目录摘要或身份不一致。"
            ) from error

    def get_many(self, identities):
        identities = tuple(identities)
        needed = tuple(dict.fromkeys(value for value in identities if value not in self._bodies))
        if needed:
            references = tuple(
                self._reference(entry) for entry in self.tree.get_many(self.root, needed)
            )
            actual = self.store.read_blocks((self.BODIES, reference) for reference in references)
            for identity, reference, raw in zip(needed, references, actual, strict=True):
                raw = self._writes.get(reference, raw)
                self._bodies[identity] = self._decode(raw, reference, identity)
        return tuple(self._bodies[identity] for identity in identities)

    def prepare(self, captured, removed=()):
        """One chain owns each atom; publish its body and thin locator together."""
        removed = tuple(dict.fromkeys(removed))
        if set(captured) & set(removed):
            fail("storage_corrupt", "同一财务来源不能同时写入和退出。")
        keys = (*captured, *removed)
        old = dict(zip(keys, self.tree.get_many(self.root, keys), strict=True))
        # Retired legs remain available in this read while roles replace their leaves.
        self.get_many(removed)
        prepared = {}
        for identity, body in captured.items():
            body = validate_body(deepcopy(body), identity)
            raw = canonical_json(body)
            reference = digest(raw)
            if (old[identity] is not None and self._reference(old[identity]) != reference) or (
                identity in self._bodies and canonical_json(self._bodies[identity]) != raw
            ):
                fail("storage_corrupt", "同一正式来源不能静默改写财务正文。")
            prepared[identity] = body, raw, reference
        references = tuple(value[2] for value in prepared.values())
        actual = self.store.read_blocks((self.BODIES, reference) for reference in references)
        for (identity, (_, raw, reference)), saved in zip(prepared.items(), actual, strict=True):
            saved = self._writes.get(reference, saved)
            if (saved is not None and saved != raw) or (
                saved is None and old[identity] is not None
            ):
                fail("storage_corrupt", "已有财务来源正文缺失或与实际不可变原文不一致。")
        updates = {}
        for (identity, (body, raw, reference)), saved in zip(prepared.items(), actual, strict=True):
            if self._writes.get(reference, saved) is None:
                self._writes[reference] = raw
            self._deletes.discard(reference)
            self._bodies[identity] = body
            updates[identity] = Entry(identity, reference)
        for identity in removed:
            reference = self._reference(old[identity])
            if reference in self._writes:
                del self._writes[reference]
            else:
                self._deletes.add(reference)
            updates[identity] = None
        self.root = self.tree.patch(self.root, updates)

    def changes(self):
        return (
            tuple((self.BODIES, key, raw) for key, raw in sorted(self._writes.items())),
            tuple((self.BODIES, key) for key in sorted(self._deletes)),
        )

    def line(self, identity, line_no):
        if identity not in self._lines:
            body = self.get_many((identity,))[0]
            self._lines[identity] = {row[0]: row for row in body["journal"]}
        row = self._lines[identity].get(line_no)
        if row is None:
            fail("storage_corrupt", "财务余额角色引用的正式分录行不存在。")
        return row


class _Folds:
    def branch(self, children):
        result = {}
        for child in children:
            result = combine(result, child.summary)
        return result

    def encode_summary(self, summary):
        return {key: value.record() for key, value in summary.items()}

    def decode_summary(self, value):
        allowed = {
            "p!",
            "c!",
            *("p" + str(n) for n in range(1, 33)),
            *("c" + str(n) for n in range(1, 21)),
        }
        if type(value) is not dict or any(
            type(key) is not str or key not in allowed for key in value
        ):
            fail("storage_corrupt", "财务角色的有序金额摘要格式损坏。")
        return {key: Fold.from_record(raw) for key, raw in value.items()}


class FlowStrategy(_Folds):
    def __init__(self, bodies, *, include_cash=True):
        self.bodies, self.include_cash = bodies, include_cash

    def leaf(self, entries):
        values = [entry.value for entry in entries]
        if any(
            type(value) is not list
            or len(value) != 3
            or type(value[0]) is not str
            or type(value[1]) is not int
            or value[1] == 0
            for value in values
        ):
            fail("storage_corrupt", "财务发生角色的正式来源或正负采用系数损坏。")
        bodies = self.bodies.get_many(value[0] for value in values)
        result = {}
        for value, body in zip(values, bodies, strict=True):
            _, sign, _ = value
            if value != flow_value(value[0], body, sign):
                fail("storage_corrupt", "财务税前薄切片与实际来源正文不一致。")
            notes = tuple(Notice.from_record(value) for value in body["notices"])
            local = {"p!": Fold(notices=notes)} if notes else {}
            for line, amount in body["profit"].items():
                value = (
                    Unknown(tuple(notes[index] for index in amount))
                    if type(amount) is list
                    else sum_amounts((sign * amount,))
                )
                local["p" + line] = Fold.one(value)
            cash, cash_notes = {}, ()
            for movement in body["cash"] if self.include_cash else ():
                if movement is None:
                    missing = _CashIssues()
                    for line in range(1, 21):
                        note = missing_classification(missing, line, COLUMN)
                        cash.setdefault(line, []).append(Unknown((note,), resets_initial=True))
                    cash_notes = missing.notices
                else:
                    for category, amount in movement:
                        cash.setdefault(_ROWS[category][0], []).append(
                            sum_amounts((sign * amount,))
                        )
            local.update(("c" + str(line), values_fold(amounts)) for line, amounts in cash.items())
            if cash_notes:
                local["c!"] = Fold(notices=cash_notes)
            result = combine(result, local)
        return result


class _CashIssues:
    def __init__(self):
        self.notices = ()

    def unknown(self, field, meaning, message, *, code):
        note = Notice(field, code, meaning, message)
        self.notices += (note,)
        return note


class JournalStrategy:
    """Mixed dimensions hold no giant map; exact dimension ranges have one fold."""

    def __init__(self, bodies):
        self.bodies = bodies

    def leaf(self, entries):
        values = [entry.value for entry in entries]
        if any(
            type(value) is not list
            or len(value) != 2
            or type(value[0]) is not str
            or type(value[1]) is not int
            or value[1] < 1
            for value in values
        ):
            fail("storage_corrupt", "财务余额角色的精确分录行身份损坏。")
        self.bodies.get_many(value[0] for value in values)
        dimension, result, mixed = None, Fold(), False
        for entry, (identity, line_no) in zip(entries, values, strict=True):
            body = self.bodies.get_many((identity,))[0]
            row = self.bodies.line(identity, line_no)
            if journal_key(body, row) != entry.key:
                fail("storage_corrupt", "财务余额角色的分录身份或核算顺序损坏。")
            actual = dimension_key(*row[1:4])
            if dimension is not None and actual != dimension:
                mixed = True
            if dimension is None:
                dimension = actual
            if not mixed:
                result = result.then(Fold.one(sum_amounts((row[4], -row[5]))))
        return (dimension, result) if dimension is not None and not mixed else None

    def branch(self, children):
        result, dimension = Fold(), None
        for child in children:
            value = child.summary
            if value is None or (dimension is not None and dimension != value[0]):
                return None
            dimension = value[0]
            result = result.then(value[1])
        return (dimension, result) if dimension is not None else None

    def encode_summary(self, summary):
        return None if summary is None else [summary[0], summary[1].record()]

    def decode_summary(self, raw):
        if raw is None:
            return None
        if type(raw) is not list or len(raw) != 2 or type(raw[0]) is not str:
            fail("storage_corrupt", "财务余额角色的维度摘要格式损坏。")
        return raw[0], Fold.from_record(raw[1])


class CashBalanceStrategy:
    """The original cash-before call checks one final sum, not every prefix."""

    def __init__(self, bodies):
        self.bodies = bodies

    def leaf(self, entries):
        values = [entry.value for entry in entries]
        if any(
            type(value) is not list
            or len(value) != 2
            or type(value[0]) is not str
            or type(value[1]) is not int
            or value[1] < 1
            for value in values
        ):
            fail("storage_corrupt", "财务资金起点的精确分录身份损坏。")
        self.bodies.get_many(value[0] for value in values)
        total = 0
        for entry, (identity, line) in zip(entries, values, strict=True):
            body = self.bodies.get_many((identity,))[0]
            row = self.bodies.line(identity, line)
            key = order_key((*target_key(body_head(body)), f"{line:019d}"))
            if entry.key != key or row[1] not in ("1001", "1002", "1012"):
                fail("storage_corrupt", "财务资金起点的分录范围或顺序损坏。")
            total += sum_amounts((row[4], -row[5]))
        return total

    def branch(self, children):
        return sum(child.summary for child in children)

    def encode_summary(self, value):
        if type(value) is not int:
            fail("storage_corrupt", "财务资金起点的整数摘要损坏。")
        return value

    decode_summary = encode_summary
