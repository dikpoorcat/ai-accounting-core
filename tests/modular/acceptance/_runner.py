"""Execute JSON actions and reject every unhandled oracle field."""

import json
from copy import deepcopy

from ai_accounting_modular.storage.public import module_schema

from ._observations import absent, get_path, normalized, subset
from ._process import Host
from ._support import SPEC


def resolve(value, captures, root):
    if isinstance(value, dict):
        if set(value) == {"$capture"}:
            return deepcopy(captures[value["$capture"]])
        return {key: resolve(item, captures, root) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, captures, root) for item in value]
    if isinstance(value, str) and value.startswith("$sandbox/"):
        selected = (root / value.removeprefix("$sandbox/")).resolve()
        assert selected.is_relative_to(root.resolve()), value
        return str(selected)
    return value


class Scenario:
    def __init__(self, case, root, mode):
        self.case, self.root = case, root
        self.host = Host(root, mode)
        self.captures, self.observations = {}, []
        scope = case.get("oracle_scope", {})
        self.scope = dict(
            business_ids=scope.get("business_ids", []), roles=scope.get("voucher_roles", [])
        )
        self.latest_snapshot = None
        self.stopped = False

    def snapshot(self):
        self.latest_snapshot = self.host.call("snapshot", **self.scope)
        return self.latest_snapshot

    def seed(self):
        for name in self.case["given"]:
            result = self.host.call("apply", request=SPEC["requests"][name])
            assert result.get("completion") == "complete", (name, result)
            assert result["documents"] == "ready", (name, result)

    def observe(self, snapshot=None):
        return normalized(snapshot or self.snapshot())

    def restart(self, build_label=None):
        self.host.restart(build_label)
        self.stopped = False

    def close(self):
        self.host.close()

    def invoke(self, step):
        from ._actions import ACTIONS

        action = step["invoke"]
        assert action in ACTIONS, "Unhandled invoke: " + action
        request = resolve(step.get("input", {}), self.captures, self.root)
        return ACTIONS[action](self, step, request)

    @staticmethod
    def _no_process_archives(owner):
        # Owner-only structural evidence: these current-state tables have no
        # request/approval/old-question archive. Unknown storage needs a new review;
        # a reassuring table name alone is not sufficient proof of absence.
        current_tables = {
            "application": {
                "application_state",
                "business_head",
                "current_dependency",
                "current_publication",
                "current_relation",
            },
            "records": {
                "company_note",
                "current_issue",
                "current_issue_scope",
                "document_set",
                "document_shard_head",
                "source",
                "source_use",
            },
        }
        assert module_schema(owner).tables == current_tables[owner]
        return 0

    def _voucher_reposts(self, snapshot):
        current = {item["voucher_id"] for item in snapshot["vouchers"]}
        original = {item["voucher_id"] for item in self.initial_snapshot["vouchers"]}
        return len(current - original)

    def execute(self, step):
        observed = self.invoke(step)
        expected = resolve(step.get("expect", {}), self.captures, self.root)
        special = {
            "same_as",
            "unchanged",
            "absent_current_payloads",
            "generation_increased_by",
            "minimal_tombstone_only",
            "business_absent",
            "obligation_reclassification",
            "reclassification_lines",
            "past_issue_or_answer_archives",
            "permanent_request_history_count",
        }
        subset(
            observed,
            {key: value for key, value in expected.items() if key not in special},
            self.case["id"] + "/" + step["id"],
        )
        for path, reference in expected.get("same_as", {}).items():
            if path == "scope" and "reading_targets" in self.captures[reference]:
                observed["scope"]["reading_targets"] = self.reading_heads()
            assert get_path(observed, path) == self.captures[reference], (
                path,
                observed.get(path),
                self.captures[reference],
            )
        if "generation_increased_by" in expected:
            previous = next(
                value
                for key, value in reversed(list(self.captures.items()))
                if key.endswith(".generation")
            )
            assert observed["generation"] - previous == expected["generation_increased_by"]
        if "absent_current_payloads" in expected:
            absent(observed["_after"], expected["absent_current_payloads"])
        if "unchanged" in expected:
            self.check_unchanged(observed, expected["unchanged"])
        if "minimal_tombstone_only" in expected:
            selected = next(
                item
                for item in observed["_after"]["businesses"]
                if item["state"]["business_id"] == expected["business"]["business_id"]
            )
            assert (
                set(selected) <= {"state", "results", "sources"}
                and selected["results"] == []
                and selected.get("sources", []) == []
            )
            assert (
                set(selected["state"]) == {"business_id", "seq", "state", "fact_digest", "results"}
                and selected["state"]["fact_digest"] is None
            )
        if "business_absent" in expected:
            assert not set(expected["business_absent"]) & {
                item["business_id"] for item in observed["_after"]["directory"]
            }
        for key, owner in (
            ("past_issue_or_answer_archives", "records"),
            ("permanent_request_history_count", "application"),
        ):
            if key in expected:
                assert self._no_process_archives(owner) == expected[key]
        if "past_issue_or_answer_archives" in expected:
            assert observed["_after"]["documents"]["issues"] == []
        if "obligation_reclassification" in expected:
            self.check_reclassification(observed, expected)
        for field in step.get("capture", []):
            self.captures[step["id"] + "." + field] = deepcopy(get_path(observed, field))
        self.observations.append(
            dict(
                step=step["id"],
                invoke=step["invoke"],
                input=SPEC["requests"][step["request"]]
                if "request" in step
                else resolve(step.get("input", {}), self.captures, self.root),
                expected=expected,
                actual={key: observed[key] for key in expected if key in observed},
                captured={field: get_path(observed, field) for field in step.get("capture", [])},
            )
        )
        (self.root / "acceptance-report.json").write_text(
            json.dumps(self.observations, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def check_unchanged(self, observed, fields):
        mapping = {
            "business_facts_and_seq": ("directory", "businesses"),
            "adopted_targets": ("results", "all_results"),
            "vouchers_and_numbers": ("vouchers",),
            "obligations_and_settlements": ("obligations", "events"),
            "money_occurrences": ("money",),
            "document_targets_and_set": ("documents", "document_targets"),
            "reading_targets": (),
        }
        for field in fields:
            assert field in mapping, field
            for key in mapping[field]:
                assert observed["_before"][key] == observed["_after"][key], (field, key)
        if "reading_targets" in fields:
            assert self.reading_heads() == self.captures["before.scope"]["reading_targets"]

    def reading_heads(self, *, prepare=False):
        heads = self.host.call("heads")
        if not prepare:
            assert heads["regenerated_component_count"] == 0, heads
            assert heads["preparing_scope_count"] == 0, heads
        return {key: heads[key] for key in ("scope_heads", "component_heads")}

    def check_reclassification(self, observed, expected):
        overpayment = next(
            item
            for item in observed["_after"]["businesses"]
            if item["state"]["business_id"] == SPEC["ids"]["overpayment"]
        )
        # This is the owner's exact adopted public relation, not the submitted request.
        events = [
            event
            for event in observed["_after"]["events"]
            if event["use"]["kind"] == "reclassification"
            and event["use"]["source"]["business_id"] == SPEC["ids"]["overpayment"]
        ]
        assert len(events) == 1, events
        use = events[0]["use"]
        subset(use["value"], expected["obligation_reclassification"])
        lines = [
            line for voucher in overpayment["results"][0]["vouchers"] for line in voucher["lines"]
        ]
        subset(
            [
                {key: line[key] for key in ("account_code", "debit_fen", "credit_fen")}
                for line in lines
            ],
            expected["reclassification_lines"],
        )
