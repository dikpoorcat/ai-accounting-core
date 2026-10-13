"""Exact fund definitions, line assignments and balance continuity frozen by close v5."""

from __future__ import annotations

import json

from .contracts import KernelError
from .types import YearMonth, canonical, checked, digest, is_sha256_hex

CONTRACT = "ai-accounting-kernel/2/close-fund-details/1"
PARENTS = ("1001", "1002", "1012")
BALANCE_FIELDS = {
    "entity_id",
    "detail_code",
    "parent_account",
    "opening_fen",
    "debit_fen",
    "credit_fen",
    "adjustment_fen",
    "ending_fen",
}


def _invalid(reason):
    raise KernelError(
        "content_integrity_failed",
        "关账资金明细冻结依据不一致",
        component="close",
        record_id="*",
        reason=reason,
    )


def require_fund_close_snapshot(value):
    """Validate the frozen shape without consulting any current account profile."""
    if (
        not isinstance(value, dict)
        or set(value)
        != {"contract", "definitions", "sources", "adjustments", "balances", "summary", "digest"}
        or value["contract"] != CONTRACT
        or any(
            not isinstance(value[key], list)
            for key in ("definitions", "adjustments", "balances", "summary")
        )
        or not isinstance(value["sources"], dict)
        or not is_sha256_hex(value["digest"])
    ):
        _invalid("invalid_fund_close_snapshot")
    sources = value["sources"]
    if (
        set(sources) != {"contract", "period", "activation", "opening", "lines", "opening_lines"}
        or sources["contract"] != "ai-accounting-kernel/2/fund-line-details/1"
        or type(sources["period"]) is not int
        or (sources["activation"] is not None and type(sources["activation"]) is not int)
        or any(not isinstance(sources[key], list) for key in ("opening", "lines", "opening_lines"))
    ):
        _invalid("invalid_fund_close_sources")
    ids, codes = set(), set()
    for definition in value["definitions"]:
        if (
            not isinstance(definition, dict)
            or not {"entity_id", "detail_code", "parent_account", "digest", "identity"}
            <= definition.keys()
            or any(
                not isinstance(definition[key], str) or not definition[key]
                for key in ("entity_id", "detail_code", "parent_account")
            )
            or definition["parent_account"] not in PARENTS
            or not is_sha256_hex(definition["digest"])
            or not isinstance(definition["identity"], dict)
            or definition["entity_id"] in ids
            or definition["detail_code"] in codes
        ):
            _invalid("invalid_frozen_fund_definition")
        ids.add(definition["entity_id"])
        codes.add(definition["detail_code"])
    if [item["entity_id"] for item in value["definitions"]] != sorted(ids):
        _invalid("unordered_frozen_fund_definitions")
    balance_ids = set()
    for item in value["balances"]:
        if (
            not isinstance(item, dict)
            or set(item) != BALANCE_FIELDS
            or any(
                type(item[key]) is not int
                for key in (
                    "opening_fen",
                    "debit_fen",
                    "credit_fen",
                    "adjustment_fen",
                    "ending_fen",
                )
            )
            or item["debit_fen"] < 0
            or item["credit_fen"] < 0
            or item["entity_id"] not in ids
            or item["entity_id"] in balance_ids
            or item["ending_fen"]
            != item["opening_fen"] + item["debit_fen"] - item["credit_fen"] + item["adjustment_fen"]
        ):
            _invalid("invalid_frozen_fund_balance")
        for key in ("opening_fen", "debit_fen", "credit_fen", "adjustment_fen", "ending_fen"):
            checked(item[key])
        balance_ids.add(item["entity_id"])
    if balance_ids != ids or [item["entity_id"] for item in value["balances"]] != sorted(ids):
        _invalid("frozen_fund_balance_coverage_mismatch")
    if (
        digest({key: item for key, item in value.items() if key != "digest"}).hex()
        != value["digest"]
    ):
        _invalid("frozen_fund_snapshot_digest_mismatch")
    return value


def frozen_fund_account_definitions(header, read_section):
    """Read formal identity only where the authenticated close froze it."""
    version = header.root["small"]["format_version"]
    if type(version) is not int:
        _invalid("unsupported_frozen_fund_metadata_version")
    if version == 4:
        # The original contract froze management profiles, not formal fund
        # definitions. Current definitions cannot become old frozen metadata.
        return {}
    if version != 5:
        _invalid("unsupported_frozen_fund_metadata_version")
    saved = require_fund_close_snapshot(read_section("fund_details"))
    from .fund_accounts import IDENTITY_FIELDS, PARENTS

    for definition in saved["definitions"]:
        account_type = definition.get("account_type")
        if (not isinstance(account_type, str) or account_type not in IDENTITY_FIELDS
            or definition["parent_account"] != PARENTS[account_type]
            or set(definition["identity"]) != set(IDENTITY_FIELDS[account_type])
            or any(not isinstance(value, str) or not value.strip()
                   for value in definition["identity"].values())
            or not isinstance(definition.get("source"), str) or not definition["source"].strip()
            or not is_sha256_hex(definition.get("evidence_digest"))):
            _invalid("invalid_frozen_fund_definition")
    return {item["entity_id"]: item for item in saved["definitions"]}


def _sources(connection, manifest):
    from .fund_line_details import snapshot_fund_details

    return snapshot_fund_details(
        connection,
        YearMonth(manifest["period"]).ordinal,
        version_ids=[item["id"] for item in manifest["vouchers"]],
        calculation_ids=(
            [manifest["opening_calculation_id"]]
            if manifest["opening_calculation_id"] is not None
            else []
        ),
    )


def _adjustments(connection, manifest, definitions):
    """Adopt typed opening-identity movements separately from actual money activity."""
    adopted = {item["calculation_id"]: item for item in manifest.get("adopted_results", [])}
    if not adopted:
        return []
    accounts = {item["entity_id"]: item for item in definitions}
    result = []
    for row in connection.execute(
        "SELECT c.* FROM json_each(?) ids JOIN calculation c ON c.id=ids.value "
        "WHERE c.kind='opening_identity_binding' ORDER BY c.id",
        (canonical(sorted(adopted)),),
    ):
        outcome = json.loads(row["outcome"])
        if (
            digest(outcome) != row["digest"]
            or row["digest"].hex() != adopted[row["id"]]["result_digest"]
        ):
            _invalid("fund_identity_adjustment_source_mismatch")
        effects, parent_totals = [], {parent: 0 for parent in PARENTS}
        for effect in outcome["balances"]:
            if effect["category"] not in ("bank", "cash", "platform"):
                continue
            account = accounts.get(effect["key"])
            if account is None or account["account_type"] != effect["category"]:
                _invalid("fund_identity_adjustment_account_missing")
            amount = checked(effect["amount"])
            effects.append(
                {
                    "entity_id": account["entity_id"],
                    "detail_code": account["detail_code"],
                    "parent_account": account["parent_account"],
                    "amount_fen": amount,
                }
            )
            parent = account["parent_account"]
            parent_totals[parent] = checked(parent_totals[parent] + amount)
        if any(parent_totals.values()) or (effects and outcome["lines"]):
            _invalid("fund_identity_adjustment_changes_parent")
        if effects:
            result.append(
                {
                    "calculation_id": row["id"],
                    "result_digest": row["digest"].hex(),
                    "fact_id": row["fact_id"],
                    "effects": effects,
                }
            )
    return result


def _balances(connection, manifest, definitions, sources, previous_manifest, adjustments):
    balances = {
        item["entity_id"]: {
            "entity_id": item["entity_id"],
            "detail_code": item["detail_code"],
            "parent_account": item["parent_account"],
            "opening_fen": 0,
            "debit_fen": 0,
            "credit_fen": 0,
            "adjustment_fen": 0,
            "ending_fen": 0,
        }
        for item in definitions
    }
    previous = (previous_manifest or {}).get("fund_details")
    if previous is not None:
        require_fund_close_snapshot(previous)
        for item in previous["balances"]:
            if item["entity_id"] not in balances:
                _invalid("previous_fund_definition_missing")
            balances[item["entity_id"]]["opening_fen"] = item["ending_fen"]
    else:
        opening_ids = set()
        frozen_openings = None
        if manifest["previous_close_digest"] is not None:
            from .close_storage import verified_header
            from .period_balance_freeze import read_frozen_balances

            previous_period = YearMonth(manifest["previous_close_period"]).ordinal
            previous_row = connection.execute(
                "SELECT * FROM period_close WHERE period=?", (previous_period,)
            ).fetchone()
            if previous_row is None:
                _invalid("opening_fund_previous_close_missing")
            previous_header = verified_header(connection, previous_row)
            if previous_header.logical_digest.hex() != manifest["previous_close_digest"]:
                _invalid("opening_fund_previous_close_mismatch")
            frozen_openings = {
                item["key"]: item["ending"]
                for item in read_frozen_balances(
                    connection, previous_header, category=("bank", "cash", "platform")
                )
            }
        for item in sources["opening"]:
            if item["entity_id"] not in balances or item["entity_id"] in opening_ids:
                _invalid("opening_fund_definition_missing")
            opening_ids.add(item["entity_id"])
            balance = balances[item["entity_id"]]
            if (item["parent_account"], item["detail_code"]) != (
                balance["parent_account"],
                balance["detail_code"],
            ):
                _invalid("opening_fund_definition_mismatch")
            if manifest["previous_close_digest"] is not None and (
                item["source_digest"] != manifest["previous_close_digest"]
                or item["source_period"] != YearMonth(manifest["previous_close_period"]).ordinal
                or item["first_period"] != YearMonth(manifest["period"]).ordinal
            ):
                _invalid("opening_fund_previous_close_mismatch")
            if frozen_openings is not None and item["amount"] != frozen_openings.get(
                item["entity_id"], 0
            ):
                _invalid("opening_fund_frozen_balance_mismatch")
            balances[item["entity_id"]]["opening_fen"] = checked(item["amount"])
        if frozen_openings is not None and any(
            amount != 0 and entity_id not in opening_ids
            for entity_id, amount in frozen_openings.items()
        ):
            _invalid("opening_fund_frozen_balance_coverage_mismatch")
        outcomes = {}
        for item in sources["opening_lines"]:
            ident = item["calculation_id"]
            if ident not in outcomes:
                row = connection.execute(
                    "SELECT outcome FROM calculation WHERE id=?", (ident,)
                ).fetchone()
                if row is None:
                    _invalid("opening_fund_calculation_missing")
                outcomes[ident] = json.loads(row["outcome"])["opening_lines"]
            try:
                line = outcomes[ident][item["line_no"] - 1]
                balance = balances[item["entity_id"]]
            except (KeyError, IndexError):
                _invalid("opening_fund_line_missing")
            if (item["parent_account"], item["detail_code"], line["account"]) != (
                balance["parent_account"],
                balance["detail_code"],
                balance["parent_account"],
            ):
                _invalid("opening_fund_line_identity_mismatch")
            balance["opening_fen"] = checked(
                balance["opening_fen"] + line["debit"] - line["credit"]
            )
    for item in sources["lines"]:
        row = connection.execute(
            "SELECT account,debit,credit FROM voucher_line WHERE version_id=? AND line_no=?",
            (item["version_id"], item["line_no"]),
        ).fetchone()
        balance = balances.get(item["entity_id"])
        if (
            row is None
            or balance is None
            or (item["parent_account"], item["detail_code"])
            != (balance["parent_account"], balance["detail_code"])
            or row["account"] != balance["parent_account"]
        ):
            _invalid("frozen_fund_line_identity_mismatch")
        for key in ("debit", "credit"):
            balance[key + "_fen"] = checked(balance[key + "_fen"] + row[key])
    adjustment_totals = {}
    for adjustment in adjustments:
        for effect in adjustment["effects"]:
            entity_id = effect["entity_id"]
            adjustment_totals[entity_id] = checked(
                adjustment_totals.get(entity_id, 0) + effect["amount_fen"]
            )
    for item in balances.values():
        item["adjustment_fen"] = adjustment_totals.get(item["entity_id"], 0)
        item["ending_fen"] = checked(
            item["opening_fen"] + item["debit_fen"] - item["credit_fen"] + item["adjustment_fen"]
        )
    return [balances[key] for key in sorted(balances)]


def _summary(balances, manifest):
    trial = {item["account"]: item["debit"] - item["credit"] for item in manifest["trial_balance"]}
    result = []
    for parent in PARENTS:
        selected = [item for item in balances if item["parent_account"] == parent]
        totals = {
            key: checked(sum(item[key] for item in selected))
            for key in ("opening_fen", "debit_fen", "credit_fen", "adjustment_fen", "ending_fen")
        }
        if totals["ending_fen"] != trial.get(parent, 0):
            _invalid("fund_detail_parent_trial_mismatch")
        result.append({"parent_account": parent, "account_count": len(selected), **totals})
    return result


def _verify_account_balances(
    connection, manifest, definitions, balances, *, require_index_marker=True
):
    """Compare each exact account to the verified period-balance source/freeze."""
    if not definitions:
        return
    from .period_balances import balance_totals

    expected = {
        (item["category"], item["key"]): item["amount"]
        for item in balance_totals(
            connection,
            YearMonth(manifest["period"]).ordinal,
            category=("bank", "cash", "platform"),
            keys={item["entity_id"] for item in definitions},
            require_index_marker=require_index_marker,
        )
    }
    account_types = {item["entity_id"]: item["account_type"] for item in definitions}
    if any(
        item["ending_fen"] != expected.get((account_types[item["entity_id"]], item["entity_id"]), 0)
        for item in balances
    ):
        _invalid("fund_detail_account_balance_mismatch")


def build_fund_close_snapshot(connection, manifest, *, previous_manifest=None):
    from .fund_accounts import fund_account_definitions

    definitions = sorted(fund_account_definitions(connection), key=lambda item: item["entity_id"])
    sources = _sources(connection, manifest)
    adjustments = _adjustments(connection, manifest, definitions)
    balances = _balances(connection, manifest, definitions, sources, previous_manifest, adjustments)
    _verify_account_balances(connection, manifest, definitions, balances)
    result = {
        "contract": CONTRACT,
        "definitions": definitions,
        "sources": sources,
        "adjustments": adjustments,
        "balances": balances,
        "summary": _summary(balances, manifest),
    }
    result["digest"] = digest(result).hex()
    return require_fund_close_snapshot(result)


def verify_fund_close_snapshot(
    connection, manifest, *, previous_manifest=None, require_index_marker=True
):
    """Reprove only exact immutable definitions and adopted line IDs from this close."""
    if manifest["format_version"] == 4:
        return
    from .fund_accounts import historical_fund_account

    saved = require_fund_close_snapshot(manifest["fund_details"])
    definitions = sorted(
        [
            historical_fund_account(connection, item["entity_id"], item["digest"])
            for item in saved["definitions"]
        ],
        key=lambda item: item["entity_id"],
    )
    if definitions != saved["definitions"]:
        _invalid("frozen_fund_definition_source_mismatch")
    sources = _sources(connection, manifest)
    if sources != saved["sources"]:
        _invalid("frozen_fund_line_source_mismatch")
    adjustments = _adjustments(connection, manifest, definitions)
    if adjustments != saved["adjustments"]:
        _invalid("frozen_fund_identity_adjustment_mismatch")
    balances = _balances(connection, manifest, definitions, sources, previous_manifest, adjustments)
    _verify_account_balances(
        connection, manifest, definitions, balances, require_index_marker=require_index_marker
    )
    if balances != saved["balances"] or _summary(balances, manifest) != saved["summary"]:
        _invalid("frozen_fund_balance_source_mismatch")
