"""Read-only accounting content checks against saved, never recalculated, sources.

Reference directories are checked last: their corruption cannot hide a source.
Missing historical adoption evidence is corruption under the single close contract.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import asdict

from .contracts import KernelError, Read
from .projections import require_projections
from .schema import sequence_model, table_name
from .types import YearMonth, canonical, checked, digest, sum_fen


def _invalid(component, ident, reason):
    raise KernelError(
        "content_integrity_failed",
        "已保存的核算内容或来源关系不一致",
        component=component,
        record_id=str(ident),
        reason=reason,
    )


def _object(raw, component, ident):
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        _invalid(component, ident, "invalid_json")
    if not isinstance(value, dict):
        _invalid(component, ident, "invalid_object")
    return value


def _lines(lines, component, ident, *, opening=False):
    if not isinstance(lines, list):
        _invalid(component, ident, "invalid_lines")
    result = []
    for line in lines:
        if not isinstance(line, dict) or set(line) != {"account", "debit", "credit", "cashflow"}:
            _invalid(component, ident, "invalid_line_shape")
        account, debit, credit, cashflow = (
            line[key] for key in ("account", "debit", "credit", "cashflow")
        )
        if (
            not isinstance(account, str)
            or not account
            or type(debit) is not int
            or type(credit) is not int
            or debit < 0
            or credit < 0
            or bool(debit) == bool(credit)
            or cashflow is not None
            and (not isinstance(cashflow, str) or not cashflow)
            or opening
            and cashflow is not None
        ):
            _invalid(component, ident, "invalid_line")
        checked(debit)
        checked(credit)
        result.append((account, debit, credit, cashflow))
    if result and (
        len(result) < 2 or sum_fen(row[1] for row in result) != sum_fen(row[2] for row in result)
    ):
        _invalid(component, ident, "unbalanced_lines")
    return result


def _outcome(row):
    ident = row["id"]
    outcome = _object(row["outcome"], "calculation", ident)
    if digest(outcome) != row["digest"]:
        _invalid("calculation", ident, "result_digest_mismatch")
    if not isinstance(outcome.get("values"), dict) or not isinstance(outcome.get("balances"), list):
        _invalid("calculation", ident, "invalid_outcome_shape")
    _lines(outcome.get("lines"), "calculation", ident)
    opening = outcome.get("opening", False)
    opening_lines = outcome.get("opening_lines", [])
    if type(opening) is not bool or opening_lines and (not opening or outcome["lines"]):
        _invalid("calculation", ident, "invalid_opening")
    _lines(opening_lines, "calculation", ident, opening=True)
    for balance in outcome["balances"]:
        if (
            not isinstance(balance, dict)
            or set(balance) != {"category", "key", "amount"}
            or not isinstance(balance["category"], str)
            or not balance["category"]
            or not isinstance(balance["key"], str)
            or not balance["key"]
        ):
            _invalid("calculation", ident, "invalid_balance_effect")
        checked(balance["amount"])
    return outcome


def _rows(connection, table, key, identifiers):
    if identifiers is None:
        return list(connection.execute(f"SELECT * FROM {table}"))
    if not identifiers:
        return []
    return list(
        connection.execute(
            f"SELECT t.* FROM {table} t JOIN json_each(?) ids ON t.{key}=ids.value",
            (canonical(sorted(identifiers)),),
        )
    )


def _source_set(connection, calculation_ids):
    """Expand saved dependencies and voucher owners, not current scope matches."""
    if calculation_ids is None:
        return None
    return {
        row[0]
        for row in connection.execute(
            "WITH RECURSIVE lineage(id) AS (SELECT value FROM json_each(?) UNION "
            "SELECT d.upstream_id FROM lineage ids JOIN dependency_calculation d "
            "ON d.calculation_id=ids.id UNION "
            "SELECT v.calculation_id FROM lineage ids JOIN calculation c ON c.id=ids.id "
            "JOIN calculation_publication p ON p.calculation_id=c.id "
            "JOIN calculation o INDEXED BY calculation_subject ON o.subject_id=c.subject_id "
            "JOIN voucher_version v INDEXED BY voucher_calculation ON v.calculation_id=o.id "
            "WHERE v.voucher_id=p.voucher_id UNION "
            "SELECT original.calculation_id FROM lineage ids JOIN voucher_version v "
            "ON v.calculation_id=ids.id JOIN voucher_version original "
            "ON original.id=v.reverses_id UNION "
            "SELECT old.calculation_id FROM lineage ids JOIN calculation_publication p "
            "ON p.calculation_id=ids.id JOIN calculation_publication old "
            "ON old.id=p.previous_publication_id WHERE old.calculation_id IS NOT NULL UNION "
            "SELECT p.baseline_calculation_id FROM lineage ids JOIN calculation_publication p "
            "ON p.calculation_id=ids.id WHERE p.baseline_calculation_id IS NOT NULL) "
            "SELECT id FROM lineage",
            (canonical(sorted(set(calculation_ids))),),
        )
    }


def _check_facts(engine, connection, identifiers):
    rows = _rows(connection, "fact_revision", "id", identifiers)
    if identifiers is not None and len(rows) != len(identifiers):
        _invalid("fact", "*", "referenced_fact_missing")
    subject_kinds = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT s.id,s.kind FROM json_each(?) ids JOIN subject s ON s.id=ids.value",
            (canonical(sorted({row["subject_id"] for row in rows})),),
        )
    }
    raw_facts = engine.store.fact_data_many(connection, [row["id"] for row in rows])
    kinds = defaultdict(set)
    for row in rows:
        kinds[subject_kinds.get(row["subject_id"])].add(row["id"])
    for kind, fact_ids in kinds.items():
        if kind not in engine.store.registry.models:
            _invalid("fact", "*", "unknown_fact_kind")
        for name, field in engine.store.registry.models[kind].model_fields.items():
            if sequence_model(field.annotation) is None:
                continue
            next_numbers = defaultdict(int)
            for child in connection.execute(
                f"SELECT c.revision_id,c.item_no FROM {table_name(kind)}_{name} c "
                "JOIN json_each(?) ids ON ids.value=c.revision_id ORDER BY c.revision_id,c.item_no",
                (canonical(sorted(fact_ids)),),
            ):
                if child["item_no"] != next_numbers[child["revision_id"]]:
                    _invalid("fact", child["revision_id"], "fact_child_order_mismatch")
                next_numbers[child["revision_id"]] += 1
    facts = {}
    for row in rows:
        ident = row["id"]
        kind = subject_kinds.get(row["subject_id"])
        if kind not in engine.store.registry.models:
            _invalid("fact", ident, "unknown_fact_kind")
        raw = raw_facts[ident]
        if digest(raw) != row["digest"]:
            _invalid("fact", ident, "fact_digest_mismatch")
        if raw.get("period") != str(YearMonth.from_ordinal(row["period"])):
            _invalid("fact", ident, "fact_period_mismatch")
        facts[ident] = {**dict(row), "kind": kind, "data": raw}
    payload = canonical(sorted(facts))
    sealed = {
        r[0]
        for r in connection.execute(
            "SELECT s.fact_id FROM fact_seal s JOIN json_each(?) ids ON ids.value=s.fact_id",
            (payload,),
        )
    }
    if sealed != set(facts):
        _invalid("fact", "*", "fact_seal_missing")
    evidence_ids = set()
    for row in connection.execute(
        "SELECT e.fact_id,e.evidence_digest FROM fact_evidence e "
        "JOIN json_each(?) ids ON ids.value=e.fact_id",
        (payload,),
    ):
        facts[row[0]].setdefault("evidence", []).append(row[1].hex())
        evidence_ids.add(row[1])
    for fact in facts.values():
        fact.setdefault("evidence", [])
    return facts, evidence_ids


def _check_evidence(connection, evidence_ids, *, verified=None):
    if evidence_ids is not None and not evidence_ids:
        return 0
    count = 0
    query = "SELECT digest,content FROM evidence"
    parameters = ()
    if evidence_ids is not None:
        query = (
            "SELECT e.digest,e.content FROM json_each(?) ids "
            "JOIN evidence e ON e.digest=unhex(ids.value)"
        )
        parameters = (canonical(sorted(value.hex() for value in evidence_ids)),)
    for row in connection.execute(query, parameters):
        if hashlib.sha256(row["content"]).digest() != row["digest"]:
            _invalid("evidence", row["digest"].hex(), "evidence_digest_mismatch")
        if verified is not None:
            verified.add(bytes(row["digest"]))
        count += 1
    if evidence_ids is not None and count != len(evidence_ids):
        _invalid("evidence", "*", "evidence_missing")
    return count


def _check_sources(engine, connection, calculation_ids=None, *, extra_fact_ids=()):
    identifiers = _source_set(connection, calculation_ids)
    calculations = {
        row["id"]: dict(row) for row in _rows(connection, "calculation", "id", identifiers)
    }
    if identifiers is not None and set(calculations) != identifiers:
        _invalid("calculation", "*", "referenced_calculation_missing")
    dependencies = defaultdict(set)
    dependency_facts = defaultdict(set)
    for row in _rows(connection, "dependency_calculation", "calculation_id", identifiers):
        dependencies[row["calculation_id"]].add(row["upstream_id"])
    for row in _rows(connection, "dependency_fact", "calculation_id", identifiers):
        dependency_facts[row["calculation_id"]].add(row["fact_id"])
    stored_reads = defaultdict(list)
    for row in _rows(connection, "dependency_scope", "calculation_id", identifiers):
        stored_reads[row["calculation_id"]].append(
            Read(
                row["source"],
                row["kind"],
                row["scope_key"],
                None
                if row["before_period"] == 119988
                else YearMonth.from_ordinal(row["before_period"]),
            )
        )
    fact_ids = {row["fact_id"] for row in calculations.values()}
    fact_ids.update(ident for values in dependency_facts.values() for ident in values)
    fact_ids.update(extra_fact_ids)
    facts, evidence_ids = _check_facts(
        engine, connection, fact_ids if identifiers is not None else None
    )
    verified_evidence = set()
    evidence_count = _check_evidence(
        connection, evidence_ids if identifiers is not None else None, verified=verified_evidence
    )
    seals = {
        row[0]
        for row in connection.execute(
            "SELECT s.calculation_id FROM json_each(?) ids JOIN calculation_seal s "
            "ON s.calculation_id=ids.value",
            (canonical(sorted(calculations)),),
        )
    }
    publications = {
        row["calculation_id"]: dict(row)
        for row in _rows(
            connection,
            "calculation_publication",
            "calculation_id",
            identifiers,
        )
        if row["calculation_id"] is not None
    }
    from .publication import verify_publication_chain

    verify_publication_chain(
        connection, subject_ids={row["subject_id"] for row in calculations.values()}
    )
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
    }
    member_ids = set()
    if "asset_batch_member" in tables:
        member_ids = {
            row["member_calculation_id"]
            for row in _rows(connection, "asset_batch_member", "member_calculation_id", identifiers)
        }
    for ident, row in calculations.items():
        if ident not in seals:
            _invalid("calculation", ident, "calculation_seal_missing")
        fact = facts.get(row["fact_id"])
        if fact is None or (row["subject_id"], row["kind"], row["period"]) != (
            fact["subject_id"],
            fact["kind"],
            fact["period"],
        ):
            _invalid("calculation", ident, "calculation_fact_identity_mismatch")
        if row["fact_id"] not in dependency_facts[ident]:
            _invalid("calculation", ident, "own_fact_dependency_missing")
        if not dependencies[ident] <= calculations.keys() or ident in dependencies[ident]:
            _invalid("calculation", ident, "invalid_calculation_dependency")
        if ident not in publications and ident not in member_ids:
            _invalid("calculation", ident, "publication_or_adoption_missing")
        row["decoded"] = _outcome(row)
        # The released calculation identity seals saved reads and version IDs.
        # dependency_fact additionally always contains the owning fact; that
        # fact may or may not also have been selected, hence two exact variants.
        versions = dependencies[ident] | (dependency_facts[ident] - {row["fact_id"]})
        payload = {
            "fact": row["fact_id"],
            "outcome": row["decoded"],
            "reads": [asdict(read) for read in sorted(stored_reads[ident], key=repr)],
            "program": row["program_version"],
        }
        valid_keys = {
            "c_" + digest({**payload, "versions": sorted(candidate)}).hex()
            for candidate in (versions, versions | {row["fact_id"]})
        }
        if ident not in valid_keys:
            _invalid("calculation", ident, "calculation_input_digest_mismatch")
    # Iterative graph traversal also handles long legitimate cumulative chains.
    completed, active = set(), set()
    for ident in calculations:
        stack = [(ident, False)]
        while stack:
            item, returning = stack.pop()
            if returning:
                active.discard(item)
                completed.add(item)
            elif item not in completed:
                if item in active:
                    _invalid("calculation", item, "dependency_cycle")
                active.add(item)
                stack.append((item, True))
                stack.extend((parent, False) for parent in dependencies[item])
    vouchers = {
        row["id"]: dict(row)
        for row in _rows(connection, "voucher_version", "calculation_id", identifiers)
    }
    numbered = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT v.id,v.number FROM json_each(?) ids JOIN voucher v ON v.id=ids.value",
            (
                canonical(
                    sorted(
                        {row["voucher_id"] for row in vouchers.values()}
                        | {
                            row["voucher_id"]
                            for row in publications.values()
                            if row["voucher_id"] is not None
                        }
                    )
                ),
            ),
        )
    }
    lines = defaultdict(list)
    payload = canonical(sorted(vouchers))
    for row in connection.execute(
        "SELECT l.* FROM json_each(?) ids JOIN voucher_line l ON ids.value=l.version_id "
        "ORDER BY l.version_id,l.line_no",
        (payload,),
    ):
        if row["line_no"] != len(lines[row["version_id"]]) + 1:
            _invalid("voucher", row["version_id"], "voucher_line_order_mismatch")
        lines[row["version_id"]].append(
            {key: row[key] for key in ("account", "debit", "credit", "cashflow")}
        )
    for ident, row in vouchers.items():
        actual = _lines(lines[ident], "voucher", ident)
        if not actual or sum_fen(line[1] for line in actual) != row["total"]:
            _invalid("voucher", ident, "voucher_total_mismatch")
        if row["voucher_id"] not in numbered:
            _invalid("voucher", ident, "voucher_number_missing")
        row["number"] = numbered[row["voucher_id"]]
        row["lines"] = lines[ident]
        publication = publications.get(row["calculation_id"])
        if publication is None or publication["posting_period"] != row["period"]:
            _invalid("voucher", ident, "voucher_publication_mismatch")
        if row["reverses_id"] is not None:
            original = vouchers.get(row["reverses_id"])
            if (
                original is None
                or original["period"] >= row["period"]
                or original["reverses_id"] is not None
            ):
                _invalid("voucher", ident, "invalid_reversal_source")
            expected = [
                (line["account"], line["credit"], line["debit"], line["cashflow"])
                for line in lines[original["id"]]
            ]
        else:
            if publication["voucher_id"] != row["voucher_id"]:
                _invalid("voucher", ident, "voucher_publication_identity_mismatch")
            expected = _lines(
                calculations[row["calculation_id"]]["decoded"]["lines"],
                "calculation",
                row["calculation_id"],
            )
        if actual != expected:
            _invalid("voucher", ident, "voucher_lines_mismatch")
    # Non-impact reviews may reuse a voucher produced by an older calculation.
    # A cleared result may retain its reserved number with no current version.
    represented = {
        (
            voucher["voucher_id"],
            calculations[voucher["calculation_id"]]["subject_id"],
            voucher["period"],
            canonical(voucher["lines"]),
        )
        for voucher in vouchers.values()
        if voucher["reverses_id"] is None
    }
    for ident, publication in publications.items():
        voucher_id = publication["voucher_id"]
        if voucher_id is not None and voucher_id not in numbered:
            _invalid("calculation", ident, "publication_voucher_missing")
        if (
            calculations[ident]["decoded"]["lines"]
            and (
                voucher_id,
                calculations[ident]["subject_id"],
                publication["posting_period"],
                canonical(calculations[ident]["decoded"]["lines"]),
            )
            not in represented
        ):
            _invalid("calculation", ident, "publication_lines_unrepresented")
    for ident, item in publications.items():
        if item["mode"] != "review_no_impact":
            continue
        previous = connection.execute(
            "SELECT calculation_id FROM calculation_publication WHERE id=?",
            (item["previous_publication_id"],),
        ).fetchone()[0]
        before, after = calculations[previous]["decoded"], calculations[ident]["decoded"]
        if any(
            before[field] != after[field]
            for field in ("lines", "balances", "opening", "opening_lines")
        ):
            _invalid("publication", ident, "no_impact_accounting_mismatch")
    for ident, calculation in calculations.items():
        if calculation["kind"] in {"asset_activation_batch", "asset_consumption_month"}:
            from .asset_batches import frozen_members

            try:
                frozen_members(connection, ident)
            except KernelError:
                _invalid("calculation", ident, "asset_membership_mismatch")
    return {
        "calculations": calculations,
        "facts": facts,
        "publications": publications,
        "dependencies": dependencies,
        "dependency_facts": dependency_facts,
        "vouchers": vouchers,
        "evidence_count": evidence_count,
        "verified_evidence": verified_evidence,
    }


def _totals(lines, component, ident):
    if not isinstance(lines, list):
        _invalid(component, ident, "invalid_trial_balance")
    totals = {}
    for line in lines:
        if not isinstance(line, dict) or not {"account", "debit", "credit"} <= line.keys():
            _invalid(component, ident, "invalid_trial_balance_line")
        account = line["account"]
        if not isinstance(account, str) or not account:
            _invalid(component, ident, "invalid_trial_balance_account")
        amounts = [checked(line["debit"]), checked(line["credit"])]
        if min(amounts) < 0:
            _invalid(component, ident, "invalid_trial_balance_amount")
        previous = totals.setdefault(account, [0, 0])
        previous[0] = checked(previous[0] + amounts[0])
        previous[1] = checked(previous[1] + amounts[1])
    return {key: value for key, value in totals.items() if value != [0, 0]}


def _merge(left, right):
    result = {key: list(value) for key, value in left.items()}
    for key, value in right.items():
        previous = result.setdefault(key, [0, 0])
        previous[0] = checked(previous[0] + value[0])
        previous[1] = checked(previous[1] + value[1])
    return {key: value for key, value in result.items() if value != [0, 0]}


def _opening_basis(source, manifest, period):
    """Read the explicitly adopted independent opening; never infer from totals."""
    ident = manifest["opening_calculation_id"]
    if ident is None:
        return {}
    row = source["calculations"].get(ident)
    if row is None or not row["decoded"].get("opening"):
        _invalid("close", period, "opening_adoption_mismatch")
    if row["kind"] == "opening_package":
        from .opening_adoption import _DETAIL_KINDS, _detail_shape, _package_contract

        package = {
            **row,
            "period": str(YearMonth.from_ordinal(row["period"])),
            "outcome": row["decoded"],
            "fact_data": source["facts"][row["fact_id"]]["data"],
        }
        facts = {
            key: {**value, "period": str(YearMonth.from_ordinal(value["period"]))}
            for key, value in source["facts"].items()
        }
        contract = _package_contract(package, facts, source["dependency_facts"][ident])
        if contract is None:
            _invalid("close", period, "opening_package_content_mismatch")
        declarations, _ = contract
        adopted_details = {
            item["subject_id"]: source["calculations"][item["calculation_id"]]
            for item in manifest["adopted_results"]
            if source["calculations"][item["calculation_id"]]["kind"] in _DETAIL_KINDS
        }
        if set(adopted_details) != set(declarations):
            _invalid("close", period, "opening_member_adoption_mismatch")
        for subject, member in declarations.items():
            detail = adopted_details[subject]
            if (
                detail["fact_id"] != member["fact_id"]
                or detail["kind"] != member["kind"]
                or not _detail_shape(detail["decoded"])
                or detail["decoded"]["values"] != member["values"]
                or source["dependencies"][detail["id"]] != {ident}
            ):
                _invalid("close", period, "opening_member_adoption_mismatch")
    return _totals(row["decoded"].get("opening_lines", []), "calculation", ident)


class _FrozenAdoptionReads:
    """Supply the existing asset-adoption proof with already verified raw rows."""

    def __init__(self, source):
        self.source = source

    def prime_parents(self, identifiers):
        return None

    def parents(self, ident):
        return self.source["dependencies"][ident]

    def calculations(self, identifiers):
        result = {}
        for ident in identifiers:
            row = self.source["calculations"][ident]
            publication = self.source["publications"].get(ident)
            result[ident] = {
                **row,
                "outcome": row["decoded"],
                "period": str(YearMonth.from_ordinal(row["period"])),
                "posting_period": str(YearMonth.from_ordinal(publication["posting_period"]))
                if publication
                else None,
                "result_digest": row["digest"].hex(),
                "fact_data": self.source["facts"][row["fact_id"]]["data"],
            }
        return result


def _check_snapshot_references(connection, source, manifest, period):
    snapshot = manifest.get("management_snapshot")
    if snapshot is None:
        return
    if not isinstance(snapshot, dict):
        _invalid("close", period, "invalid_management_snapshot")
    if (
        "digest" in snapshot
        and snapshot["digest"]
        != digest({key: value for key, value in snapshot.items() if key != "digest"}).hex()
    ):
        _invalid("close", period, "management_snapshot_digest_mismatch")
    groups = (
        ("typed_facts", "fact_revision"),
        ("profiles", "display_profile_revision"),
        ("entity_profiles", "entity_profile_revision"),
        ("management", "management_revision"),
        ("payees", "payee_revision"),
        ("company_note", "company_note_revision"),
        ("commentary", "period_commentary_revision"),
        ("commentary_latest", "period_commentary_revision"),
    )
    for name, table in groups:
        if name not in snapshot:
            continue
        entries = snapshot.get(name, [])
        if entries is None:
            continue
        if name in {"company_note", "commentary", "commentary_latest"}:
            entries = [entries]
        if not isinstance(entries, list) or any(
            not isinstance(item, dict) or "id" not in item for item in entries
        ):
            _invalid("close", period, "invalid_management_reference")
        identities = {item["id"] for item in entries}
        records = {row["id"]: row for row in _rows(connection, table, "id", identities)}
        for entry in entries:
            record = records.get(entry["id"])
            if record is None:
                _invalid("close", period, "management_source_missing")
            for key, value in entry.items():
                if key == "content_validity" or key == "kind" and name == "typed_facts":
                    if (
                        key == "kind"
                        and name == "typed_facts"
                        and source["facts"][entry["id"]]["kind"] != value
                    ):
                        _invalid("close", period, "management_source_content_mismatch")
                    continue
                if key not in record.keys():
                    _invalid("close", period, "unsupported_management_reference")
                actual = record[key].hex() if isinstance(record[key], bytes) else record[key]
                if actual != value:
                    _invalid("close", period, "management_source_content_mismatch")


def _check_job_sources(engine, connection, source):
    """Frozen export plans have their own saved content digests."""
    from .read_indexes import JOB_KINDS, _job_references

    for row in connection.execute(
        "SELECT * FROM jobs WHERE kind IN (SELECT value FROM json_each(?))",
        (canonical(JOB_KINDS),),
    ):
        payload = _object(row["payload"], "job", row["id"])
        plan = payload.get("plan")
        if not isinstance(plan, dict) or not isinstance(plan.get("digest"), str):
            _invalid("job", row["id"], "unsupported_export_plan")
        omitted = {"digest"}
        if row["kind"] == "report_export":
            omitted.add("epochs")
        elif row["kind"] == "tax_import":
            omitted.add("status")
        if (
            plan["digest"]
            != digest({key: value for key, value in plan.items() if key not in omitted}).hex()
        ):
            _invalid("job", row["id"], "export_plan_digest_mismatch")
        if (plan.get("company_id"), plan.get("database_id")) != (
            engine.store.company_id,
            engine.store.database_id,
        ):
            _invalid("job", row["id"], "export_plan_identity_mismatch")
        for _, _, kind, ident, *_ in _job_references(row):
            if kind == "calculation" and ident not in source["calculations"]:
                _invalid("job", row["id"], "export_calculation_missing")
            if kind == "fact" and ident not in source["facts"]:
                _invalid("job", row["id"], "export_fact_missing")
            if (
                kind == "version"
                and ident not in source["facts"]
                and ident not in source["calculations"]
            ):
                _invalid("job", row["id"], "export_version_missing")


def _check_audit_sources(connection):
    from .provenance import _result
    from .read_indexes import AUDIT_ACTIONS

    for row in connection.execute(
        "SELECT a.id,a.payload,r.result FROM audit a LEFT JOIN request r ON r.id=a.request_id "
        "WHERE a.action IN (SELECT value FROM json_each(?))",
        (canonical(AUDIT_ACTIONS),),
    ):
        payload = _object(row["payload"], "audit", row["id"])
        if row["result"] is None or _result(payload) != _object(
            row["result"], "request", row["id"]
        ):
            _invalid("audit", row["id"], "audit_request_result_mismatch")


def _check_close_approval(row, manifest, period):
    """Tie the frozen receipt to the immutable, consumed company-local grant."""
    approval = manifest["approval"]
    if approval is None:
        return None
    versions = manifest["read_version"]
    preview_manifest = {**manifest, "approval": None}
    preview_digest = digest(
        [
            preview_manifest,
            versions["accounting"],
            versions["material"],
            versions["management"],
        ]
    ).hex()
    if (
        row is None
        or row["consumed_at"] is None
        or not row["confirmed_at"] <= row["consumed_at"] < row["expires_at"]
        or (
            row["catalog_instance_id"],
            row["company_id"],
            row["database_id"],
            row["period"],
            row["preview_digest"].hex(),
            row["accounting_epoch"],
            row["material_epoch"],
            row["management_epoch"],
            row["owner_id"],
            row["credential_version"],
            row["confirmed_at"],
        )
        != (
            approval["catalog_instance_id"],
            manifest["company_id"],
            manifest["database_id"],
            period,
            approval["preview_digest"],
            versions["accounting"],
            versions["material"],
            versions["management"],
            approval["owner_id"],
            approval["credential_version"],
            approval["confirmed_at"],
        )
        or approval["preview_digest"] != preview_digest
    ):
        _invalid("close", period, "close_approval_mismatch")
    return approval["approval_id"]


def _check_closes(
    engine, connection, source, *, through_period=None, verify_financial_position=True
):
    from .close_contract import direct_calculation_ids, require_close_contract

    previous_digest, previous_trial, previous_period = None, None, None
    previous_sequence = 0
    maximum_sequence = connection.execute(
        "SELECT coalesce(max(sequence),0) FROM calculation_publication"
    ).fetchone()[0]
    count = 0
    used_approval_ids = set()
    parameters = (through_period,) if through_period is not None else ()
    restriction = " WHERE period<=?" if through_period is not None else ""
    closes = []
    for row in connection.execute(
        "SELECT * FROM period_close" + restriction + " ORDER BY period", parameters
    ):
        period = row["period"]
        if hashlib.sha256(row["manifest"].encode("utf-8")).digest() != row["digest"]:
            _invalid("close", period, "manifest_digest_mismatch")
        manifest = require_close_contract(_object(row["manifest"], "close", period))
        closes.append((row, manifest))
    approval_ids = [
        manifest["approval"]["approval_id"]
        for _, manifest in closes
        if manifest["approval"] is not None
    ]
    approval_rows = (
        {
            row["id"]: row
            for row in connection.execute(
                "SELECT * FROM security_close_approval "
                "WHERE id IN (SELECT value FROM json_each(?))",
                (canonical(approval_ids),),
            )
        }
        if approval_ids
        else {}
    )
    coverage_inventory_references = []
    for row, manifest in closes:
        references = manifest["material_coverage"].get("inventory_versions")
        if not isinstance(references, list):
            _invalid("close", row["period"], "material_inventory_references_missing")
        for reference in references:
            if (
                not isinstance(reference, dict)
                or set(reference) != {"inventory_id", "period", "category", "content_digest"}
                or type(reference["inventory_id"]) is not int
                or not isinstance(reference["period"], str)
                or not isinstance(reference["category"], str)
                or not isinstance(reference["content_digest"], str)
            ):
                _invalid("close", row["period"], "material_inventory_reference_invalid")
            coverage_inventory_references.append((row["period"], reference))
    inventory_ids = {
        ident for _, manifest in closes for ident in manifest["inventories"].values()
    } | {reference["inventory_id"] for _, reference in coverage_inventory_references}
    inventories = {
        item["id"]: item for item in _rows(connection, "material_revision", "id", inventory_ids)
    }
    inventory_items = {ident: [] for ident in inventory_ids}
    if inventory_ids:
        for item in connection.execute(
            "SELECT i.inventory_id,i.evidence_digest FROM json_each(?) ids "
            "JOIN material_item i ON i.inventory_id=ids.value "
            "ORDER BY i.inventory_id,i.evidence_digest",
            (canonical(sorted(inventory_ids)),),
        ):
            inventory_items[item["inventory_id"]].append(item["evidence_digest"].hex())
    from .materials import _inventory_reference

    for period, reference in coverage_inventory_references:
        inventory = inventories.get(reference["inventory_id"])
        if (
            inventory is None
            or _inventory_reference(inventory, inventory_items[reference["inventory_id"]])
            != reference
        ):
            _invalid("close", period, "material_inventory_reference_mismatch")
    evidence_ids = {bytes.fromhex(manifest["owner_confirmation"]) for _, manifest in closes} | {
        bytes(item["evidence_digest"]) for item in inventories.values()
    }
    _check_evidence(connection, evidence_ids - source["verified_evidence"])
    expected_adoptions_by_period, current_vouchers_by_period = defaultdict(set), defaultdict(set)
    boundaries = canonical(
        [[row["period"], manifest["publication_sequence"]] for row, manifest in closes]
    )
    for item in connection.execute(
        "SELECT json_extract(b.value,'$[0]'),p.calculation_id FROM json_each(?) b "
        "JOIN calculation_publication p ON p.posting_period=json_extract(b.value,'$[0]') "
        "WHERE p.sequence<=json_extract(b.value,'$[1]') AND p.calculation_id IS NOT NULL "
        "AND NOT EXISTS(SELECT 1 FROM calculation_publication later "
        "WHERE later.previous_publication_id=p.id "
        "AND later.sequence<=json_extract(b.value,'$[1]'))",
        (boundaries,),
    ):
        expected_adoptions_by_period[item[0]].add(item[1])
    for item in connection.execute(
        "SELECT v.period,h.version_id FROM json_each(?) b "
        "JOIN voucher_version v ON v.period=json_extract(b.value,'$[0]') "
        "JOIN voucher_current h ON h.version_id=v.id",
        (boundaries,),
    ):
        current_vouchers_by_period[item[0]].add(item[1])
    for row, manifest in closes:
        period = row["period"]
        if (
            (manifest["period"], manifest["company_id"], manifest["database_id"])
            != (
                str(YearMonth.from_ordinal(period)),
                engine.store.company_id,
                engine.store.database_id,
            )
            or manifest["previous_close_digest"] != previous_digest
            or manifest["previous_close_period"] != previous_period
        ):
            _invalid("close", period, "manifest_identity_or_chain_mismatch")
        approval = manifest["approval"]
        approval_id = _check_close_approval(
            approval_rows.get(approval["approval_id"]) if approval is not None else None,
            manifest,
            period,
        )
        if approval_id is not None:
            if approval_id in used_approval_ids:
                _invalid("close", period, "duplicate_close_approval")
            used_approval_ids.add(approval_id)
        if not previous_sequence <= manifest["publication_sequence"] <= maximum_sequence:
            _invalid("close", period, "publication_boundary_mismatch")
        previous_sequence = manifest["publication_sequence"]
        members = direct_calculation_ids(manifest)
        if not members <= source["calculations"].keys():
            _invalid("close", period, "manifest_source_missing")
        adopted = {item["calculation_id"]: item for item in manifest["adopted_results"]}
        for ident, declaration in adopted.items():
            calculation = source["calculations"][ident]
            publication = source["publications"].get(ident)
            if publication is None or (
                publication["id"],
                calculation["subject_id"],
                calculation["fact_id"],
                calculation["digest"].hex(),
                str(YearMonth.from_ordinal(calculation["period"])),
                publication["posting_period"],
            ) != (
                declaration["publication_id"],
                declaration["subject_id"],
                declaration["fact_id"],
                declaration["result_digest"],
                declaration["source_period"],
                period,
            ):
                _invalid("close", period, "direct_adoption_source_mismatch")
            outcome = calculation["decoded"]
            role = (
                "asset_batch_owner"
                if calculation["kind"] in {"asset_activation_batch", "asset_consumption_month"}
                else "opening_basis"
                if outcome["opening"]
                else "journal_basis"
                if outcome["lines"]
                else "state_only"
            )
            if declaration["role"] != role:
                _invalid("close", period, "direct_adoption_role_mismatch")
        if set(adopted) != expected_adoptions_by_period[period]:
            _invalid("close", period, "direct_adoption_set_mismatch")
        voucher_ids, voucher_lines = set(), []
        for reference in manifest["vouchers"]:
            voucher = source["vouchers"].get(reference["id"])
            if (
                voucher is None
                or voucher["period"] != period
                or any(
                    reference[key] != voucher[key]
                    for key in (
                        "id",
                        "voucher_id",
                        "calculation_id",
                        "total",
                        "number",
                        "reverses_id",
                    )
                )
            ):
                _invalid("close", period, "manifest_voucher_content_mismatch")
            basis = adopted.get(reference["adopted_calculation_id"])
            owner = source["calculations"][voucher["calculation_id"]]
            if (
                basis is None
                or basis["subject_id"] != owner["subject_id"]
                or reference["result_digest"] != basis["result_digest"]
            ):
                _invalid("close", period, "voucher_adoption_mismatch")
            basis_publication = source["publications"][basis["calculation_id"]]
            if (
                voucher["reverses_id"] is None
                and basis_publication["voucher_id"] != voucher["voucher_id"]
            ):
                _invalid("close", period, "voucher_adoption_mismatch")
            voucher_ids.add(voucher["id"])
            voucher_lines.extend(voucher["lines"])
        if voucher_ids != current_vouchers_by_period[period]:
            _invalid("close", period, "manifest_voucher_set_mismatch")
        batch_owners = {
            ident for ident, item in adopted.items() if item["role"] == "asset_batch_owner"
        }
        declared_batches = set()
        for adoption in manifest["asset_batch_adoptions"]:
            if not isinstance(adoption, dict) or set(adoption) != {
                "owner_calculation_id",
                "membership_digest",
            }:
                _invalid("close", period, "asset_adoption_identity_mismatch")
            ident = adoption["owner_calculation_id"]
            if ident not in batch_owners or ident in declared_batches:
                _invalid("close", period, "asset_adoption_identity_mismatch")
            if (
                source["calculations"][ident]["decoded"]["values"]["membership_digest"]
                != adoption["membership_digest"]
            ):
                _invalid("close", period, "asset_adoption_digest_mismatch")
            declared_batches.add(ident)
        if batch_owners != declared_batches:
            _invalid("close", period, "asset_adoption_set_mismatch")
        _check_snapshot_references(connection, source, manifest, period)
        for references in manifest["readiness"].values():
            if not isinstance(references, dict) or set(references) != {"facts", "calculations"}:
                _invalid("close", period, "readiness_references_missing")
            for field in ("facts", "calculations"):
                if (
                    not isinstance(references[field], list)
                    or not set(references[field]) <= source[field].keys()
                ):
                    _invalid("close", period, "readiness_source_missing")
        from .periods import MATERIAL_CATEGORIES

        if set(manifest["inventories"]) != set(MATERIAL_CATEGORIES):
            _invalid("close", period, "material_inventory_set_mismatch")
        for category, inventory_id in manifest["inventories"].items():
            inventory = inventories.get(inventory_id)
            if inventory is None or (inventory["period"], inventory["category"]) != (
                period,
                category,
            ):
                _invalid("close", period, "material_inventory_source_mismatch")
        coverage = manifest["material_coverage"]
        saved_coverage = {
            key: value
            for key, value in coverage.items()
            if key not in {"status", "coverage_digest"}
        } | {"issues": []}
        if (
            coverage.get("status") != "complete"
            or coverage.get("coverage_digest") != digest(saved_coverage).hex()
            or not isinstance(coverage.get("fact_ids"), list)
            or not set(coverage["fact_ids"]) <= source["facts"].keys()
        ):
            _invalid("close", period, "material_coverage_mismatch")
        from .asset_card_adoption import prove_asset_card_adoptions

        reads = _FrozenAdoptionReads(source)
        try:
            prove_asset_card_adoptions(
                reads,
                close_period=period,
                manifest=manifest,
                metadata=reads.calculations(members),
                independent_proofs={
                    ident: {"basis": "manifest_voucher_root"}
                    for ident in {reference["calculation_id"] for reference in manifest["vouchers"]}
                    | {reference["adopted_calculation_id"] for reference in manifest["vouchers"]}
                },
            )
        except KernelError:
            _invalid("close", period, "asset_card_adoption_mismatch")
        trial = _totals(manifest["trial_balance"], "close", period)
        if len(trial) != len(manifest["trial_balance"]):
            # Zero totals may be represented by a balanced but empty account only once.
            if len({item["account"] for item in manifest["trial_balance"]}) != len(
                manifest["trial_balance"]
            ):
                _invalid("close", period, "duplicate_trial_account")
        if sum_fen(value[0] for value in trial.values()) != sum_fen(
            value[1] for value in trial.values()
        ):
            _invalid("close", period, "unbalanced_trial_balance")
        if previous_trial is None:
            baseline = _opening_basis(source, manifest, period)
        else:
            if manifest["opening_calculation_id"] is not None:
                _invalid("close", period, "opening_after_first_close")
            baseline = previous_trial
        if _merge(baseline, _totals(voucher_lines, "close", period)) != trial:
            _invalid("close", period, "trial_balance_source_mismatch")
        from .close_review import verify_owner_review_integrity

        verify_owner_review_integrity(
            connection,
            engine,
            manifest,
            verify_financial_position=verify_financial_position,
        )
        previous_digest, previous_trial, previous_period = (
            row["digest"].hex(),
            trial,
            manifest["period"],
        )
        count += 1
    approval_parameters = (through_period,) if through_period is not None else ()
    approval_restriction = " AND period<=?" if through_period is not None else ""
    consumed_approval_ids = {
        row[0]
        for row in connection.execute(
            "SELECT id FROM security_close_approval WHERE consumed_at IS NOT NULL"
            + approval_restriction,
            approval_parameters,
        )
    }
    if consumed_approval_ids != used_approval_ids:
        _invalid("close", "*", "orphaned_close_approval")
    return count, []


def _check_heads(connection):
    for table, target, head_id, key in (
        ("fact_current", "fact_revision", "fact_id", "subject_id"),
        ("calculation_current", "calculation", "calculation_id", "subject_id"),
        ("voucher_current", "voucher_version", "version_id", "voucher_id"),
    ):
        if connection.execute(
            f"SELECT 1 FROM {table} h LEFT JOIN {target} s ON s.id=h.{head_id} "
            f"WHERE s.id IS NULL OR h.{key}<>s.{key} LIMIT 1"
        ).fetchone():
            _invalid("heads", table, "current_identity_mismatch")


def verify_integrity(engine, connection, *, include_projections=True, include_indexes=True):
    """Check all preserved versions in the caller's snapshot, including old ones."""
    try:
        ignored_tables = set()
        if not include_indexes:
            ignored_tables.update(
                ("read_index_source", "close_reference", "job_reference", "audit_reference")
            )
            ignored_tables.update(
                (
                    "entity_reference_recorded",
                    "entity_reference_current",
                    "discovery_fact_history",
                    "discovery_fact_current",
                )
            )
        if not include_projections:
            ignored_tables.update(
                (
                    "monthly_account",
                    "monthly_cashflow",
                    "balance",
                    "opening_account",
                    "period_balance",
                    "settlement_change",
                    "settlement_projection_seal",
                    "period_balance_seal",
                )
            )
        checks = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        if any(
            message != "ok"
            and message not in {f"CHECK constraint failed in {table}" for table in ignored_tables}
            for message in checks
        ):
            _invalid("sqlite", "*", "sqlite_integrity_check_failed")
        if any(
            row[0] not in ignored_tables for row in connection.execute("PRAGMA foreign_key_check")
        ):
            _invalid("sqlite", "*", "foreign_key_check_failed")
        _check_heads(connection)
        source = _check_sources(engine, connection)
        close_count, limitations = _check_closes(
            engine,
            connection,
            source,
            verify_financial_position=include_projections and include_indexes,
        )
        _check_job_sources(engine, connection, source)
        _check_audit_sources(connection)
        from .duplicates import verify_duplicate_checks
        from .entities import verify_entities
        from .identity_corrections import verify_identity_corrections

        verify_entities(connection)
        verify_identity_corrections(engine, connection)
        verify_duplicate_checks(connection)
        if include_projections:
            from .settlement_projection import require_settlement_projection

            require_projections(connection, verified_calculations=source["calculations"])
            require_settlement_projection(
                engine, connection, verified_calculations=source["calculations"]
            )
        if include_indexes:
            from .discovery_indexes import verify_discovery_indexes
            from .entity_references import verify_entity_references
            from .read_indexes import verify_read_indexes

            verify_entity_references(connection, registry=engine.store.registry)
            verify_discovery_indexes(connection)

            if connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='read_index_source'"
            ).fetchone():
                verify_read_indexes(connection)
        return {
            "status": "verified",
            "coverage": {
                "sources": "verified",
                "historical_adoption": "verified",
                "projections": "verified" if include_projections else "not_checked",
                "read_indexes": "verified" if include_indexes else "not_checked",
            },
            "limitations": limitations,
            "counts": {
                "facts": len(source["facts"]),
                "calculations": len(source["calculations"]),
                "vouchers": len(source["vouchers"]),
                "closes": close_count,
                "evidence": source["evidence_count"],
            },
        }
    except KernelError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, sqlite3.Error) as exc:
        raise KernelError(
            "content_integrity_failed",
            "已保存的核算内容无法按其存储合同核验",
            component="content",
            record_id="*",
            reason="invalid_stored_content",
        ) from exc


def verify_publication(engine, connection, calculation_ids):
    """Verify the actual frozen inputs and voucher sources of affected results."""
    try:
        source = _check_sources(engine, connection, calculation_ids)
        return {
            "status": "verified",
            "calculations": len(source["calculations"]),
            "vouchers": len(source["vouchers"]),
        }
    except KernelError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, sqlite3.Error) as exc:
        raise KernelError(
            "content_integrity_failed",
            "本次发布来源核验失败",
            component="publication",
            record_id="*",
            reason="invalid_stored_content",
        ) from exc


def verify_sources(engine, connection, *, calculation_ids=(), fact_ids=()):
    """Validate saved inputs before a transaction publishes their replacement."""
    result = verify_publication(engine, connection, calculation_ids)
    try:
        facts, evidence = _check_facts(engine, connection, set(fact_ids))
        _check_evidence(connection, evidence)
        return {**result, "facts": len(facts)}
    except KernelError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, sqlite3.Error) as exc:
        raise KernelError(
            "content_integrity_failed",
            "本次事实来源核验失败",
            component="fact",
            record_id="*",
            reason="invalid_stored_content",
        ) from exc


def verify_prepared_sources(engine, connection, prepared, *, new_fact_ids=()):
    """Check existing trace inputs before writing an entire publication batch."""
    prepared = tuple(prepared)
    planned = {item.calculation_id for item in prepared if item.calculation_id is not None}
    previous = {
        item.previous_calculation_id
        for item in prepared
        if item.previous_calculation_id is not None
    }
    calculations = set(previous)
    facts = {item.version.id for item in prepared}
    for item in prepared:
        for read, selected in item.context.trace().selections:
            (facts if read.source == "fact" else calculations).update(
                value.id for value in selected
            )
    stored_calculations = {
        row[0]
        for row in connection.execute(
            "SELECT c.id FROM json_each(?) ids JOIN calculation c ON c.id=ids.value",
            (canonical(sorted(calculations)),),
        )
    }
    if previous - stored_calculations or calculations - stored_calculations - planned:
        _invalid("publication", "*", "existing_calculation_source_missing")
    stored_facts = {
        row[0]
        for row in connection.execute(
            "SELECT f.id FROM json_each(?) ids JOIN fact_revision f ON f.id=ids.value",
            (canonical(sorted(facts)),),
        )
    }
    if facts - stored_facts - set(new_fact_ids):
        _invalid("publication", "*", "existing_fact_source_missing")
    return verify_sources(
        engine, connection, calculation_ids=stored_calculations, fact_ids=stored_facts
    )


def verify_close_integrity(engine, connection, period):
    """A new close cannot waive the independent all-account projection check."""
    month = YearMonth(period).ordinal if isinstance(period, str) else period
    # Closed voucher heads are immutable; this proves the cumulative amounts
    # without substituting today's facts or calculators for historical results.
    ids = {
        row[0]
        for row in connection.execute(
            "SELECT c.id FROM calculation_current h JOIN calculation c ON c.id=h.calculation_id "
            "WHERE c.period<=? UNION SELECT v.calculation_id FROM voucher_current h "
            "JOIN voucher_version v ON v.id=h.version_id WHERE v.period<=?",
            (month, month),
        )
    }
    from .read_indexes import _close_references

    fact_ids, evidence_ids = set(), set()
    for row in connection.execute("SELECT * FROM period_close WHERE period<=?", (month,)):
        if hashlib.sha256(row["manifest"].encode("utf-8")).digest() != row["digest"]:
            _invalid("close", row["period"], "manifest_digest_mismatch")
        manifest = _object(row["manifest"], "close", row["period"])
        from .close_contract import direct_calculation_ids

        ids.update(direct_calculation_ids(manifest))
        for _, _, kind, ident, _ in _close_references(row):
            if kind == "fact":
                fact_ids.add(ident)
            elif kind == "calculation":
                ids.add(ident)
        proof = manifest.get("owner_confirmation")
        if proof is not None:
            try:
                evidence_ids.add(bytes.fromhex(proof))
            except (TypeError, ValueError):
                _invalid("close", row["period"], "invalid_owner_evidence")
    try:
        source = _check_sources(engine, connection, ids, extra_fact_ids=fact_ids)
        _check_evidence(
            connection,
            evidence_ids - source["verified_evidence"],
            verified=source["verified_evidence"],
        )
        _, limitations = _check_closes(engine, connection, source, through_period=month)
    except KernelError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, sqlite3.Error) as exc:
        raise KernelError(
            "content_integrity_failed",
            "关账采用的历史来源核验失败",
            component="close",
            record_id=str(period),
            reason="invalid_stored_content",
        ) from exc
    require_projections(
        connection, through_period=month, verified_calculations=source["calculations"]
    )
    from .settlement_projection import require_settlement_projection

    require_settlement_projection(engine, connection, verified_calculations=source["calculations"])
    return {"status": "verified", "limitations": limitations}
