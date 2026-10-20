"""Formal production time is independent of a resource's attributed period."""

from __future__ import annotations

import json

from ._values import fail


class ResultTimes:
    """Read necessary dates once per result on this transaction's repository."""

    def __init__(self, repo, heads=()):
        self._repo = repo
        self._heads = {head["result_id"]: head for head in heads}
        self._days = {}

    def head(self, result_id):
        if result_id not in self._heads:
            self._heads[result_id] = self._repo.result(result_id)
        return self._heads[result_id]

    def day(self, result_id):
        if result_id not in self._days:
            head = self.head(result_id)
            if "basis_json" not in head:
                head = self._heads[result_id] = self._repo.result(result_id)
            basis = json.loads(head["basis_json"])
            self._days[result_id] = basis.get("effective_on")
        return self._days[result_id]

    def usage_day(self, row):
        necessary = self.day(row["result_id"])
        explicit = row.get("actual_on")
        if necessary is not None and explicit is not None and necessary != explicit:
            fail("dependency_conflict", "关系实际日与正式采用的必要实际日不一致。")
        return explicit or necessary

    def require_available(self, source_result_id, consumer_period, consumer_on=None):
        source = self.head(source_result_id)
        if consumer_period < source["business_period"]:
            fail("dependency_conflict", "资源使用不能早于来源正式产生的核算月份。")
        if consumer_on is not None:
            source_on = self.day(source_result_id)
            if source_on is not None and consumer_on < source_on:
                fail("dependency_conflict", "资源使用不能早于来源正式采用的必要实际日。")
