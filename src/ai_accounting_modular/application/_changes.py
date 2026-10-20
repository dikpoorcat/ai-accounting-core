"""Classify every business participant before any owner saves content."""

from copy import deepcopy
from dataclasses import dataclass

from ai_accounting_modular.contracts.public import MAX_FEN, ContractError, content_digest

from ._handoff import supports


def normalize_fact(fact):
    value = deepcopy(fact)
    value["evidence_ids"] = sorted(set(value["evidence_ids"]))
    return value


def fact_digest(fact):
    return content_digest(normalize_fact(fact))


@dataclass(frozen=True)
class BusinessChange:
    mutation: dict
    before: dict | None
    digest: str
    changed: bool

    @property
    def business_id(self):
        return self.mutation.get("business_id") or self.mutation["fact"]["business_id"]

    @property
    def fact(self):
        return self.mutation.get("fact")

    @property
    def seq(self):
        return (self.before["seq"] if self.before else 0) + self.changed

    def head(self, command_id, *, natural_key):
        fact, old = self.fact, self.before
        return {
            "business_id": self.business_id,
            "family": fact["kind"].split(".", 1)[0] if fact else old["family"],
            "kind": fact["kind"] if fact else old["kind"],
            "natural_key": natural_key if fact else None,
            "seq": self.seq,
            "state": "active" if fact else "withdrawn",
            "period": fact["period"] if fact else None,
            "archive_month": fact["period"] if fact else None,
            "current_digest": self.digest if fact else None,
            "latest_op": self.mutation["op"],
            "latest_expected_seq": self.mutation["expected_seq"],
            "latest_command_id": command_id,
            "latest_content_digest": self.digest,
        }


def classify(mutation, before):
    mutation = deepcopy(mutation)
    if "fact" in mutation:
        mutation["fact"] = normalize_fact(mutation["fact"])
    op, expected = mutation["op"], mutation["expected_seq"]
    business_id = mutation.get("business_id") or mutation["fact"]["business_id"]
    digest = (
        fact_digest(mutation["fact"])
        if "fact" in mutation
        else content_digest({"business_id": business_id, "op": op})
    )
    if before is None:
        if op != "create":
            raise ContractError("conflict", "业务尚未创建，不能修改、撤去或恢复。")
        return BusinessChange(mutation, None, digest, True)
    retry = (
        before["seq"] == expected + 1
        and before["latest_op"] == op
        and before["latest_expected_seq"] == expected
        and before["latest_content_digest"] == digest
    )
    same = before["state"] == "active" and before["current_digest"] == digest
    if op == "create":
        if same:
            return BusinessChange(mutation, before, digest, False)
    elif op == "withdraw":
        if before["state"] == "withdrawn" and retry:
            return BusinessChange(mutation, before, digest, False)
        if before["state"] == "active" and before["seq"] == expected:
            return _changed(mutation, before, digest)
    elif op == "replace":
        if before["state"] == "active" and (before["seq"] == expected or retry):
            if mutation["fact"]["kind"] != before["kind"] and not supports(
                before["kind"], mutation["fact"]["kind"]
            ):
                raise ContractError(
                    "unsupported_change_kind", "该跨业务类型更正不在已实现的分类交接范围。"
                )
            return (
                BusinessChange(mutation, before, digest, False)
                if same
                else _changed(mutation, before, digest)
            )
    elif op == "restore":
        if before["state"] == "active" and same and retry:
            return BusinessChange(mutation, before, digest, False)
        if before["state"] == "withdrawn" and before["seq"] == expected:
            if mutation["fact"]["kind"] != before["kind"]:
                raise ContractError("unsupported_change_kind", "恢复须使用原业务类型。")
            return _changed(mutation, before, digest)
    raise ContractError(
        "conflict", f"业务前提已变化，当前序号为{before['seq']}，状态为{before['state']}。"
    )


def _changed(mutation, before, digest):
    if before["seq"] >= MAX_FEN:
        raise ContractError("conflict", "业务当前序号已达上限。")
    return BusinessChange(mutation, before, digest, True)
