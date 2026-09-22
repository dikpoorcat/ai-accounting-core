"""The single stored close contract: direct adoption, never an ancestry snapshot."""

import re

from .contracts import KernelError
from .types import YearMonth

CLOSE_FORMAT = "ai-accounting-kernel/2/period-close"
CLOSE_FORMAT_VERSION = 4
ADOPTION_ROLES = frozenset({"journal_basis", "state_only", "opening_basis", "asset_batch_owner"})
APPROVAL_FIELDS = frozenset(
    {
        "approval_id",
        "preview_digest",
        "owner_id",
        "catalog_instance_id",
        "credential_version",
        "confirmed_at",
        "method",
    }
)


def _invalid(reason):
    raise KernelError(
        "content_integrity_failed",
        "关账冻结内容缺少完整、明确的采用依据",
        component="close",
        record_id="*",
        reason=reason,
    )


def require_close_approval(value):
    """Validate the exact security receipt frozen into a password-approved close."""
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != APPROVAL_FIELDS
        or not isinstance(value["approval_id"], str)
        or re.fullmatch(r"[0-9a-f]{32}", value["approval_id"]) is None
        or not isinstance(value["preview_digest"], str)
        or re.fullmatch(r"[0-9a-f]{64}", value["preview_digest"]) is None
        or not isinstance(value["owner_id"], str)
        or not value["owner_id"]
        or not isinstance(value["catalog_instance_id"], str)
        or not value["catalog_instance_id"]
        or type(value["credential_version"]) is not int
        or value["credential_version"] < 1
        or type(value["confirmed_at"]) is not int
        or value["confirmed_at"] < 0
        or value["method"] != "local_password_reauthentication"
    ):
        _invalid("invalid_close_approval")
    return value


def require_close_contract(manifest):
    """Reject missing/unknown formats; no historical inference or default filling."""
    required = {
        "format",
        "format_version",
        "period",
        "company_id",
        "database_id",
        "previous_close_period",
        "previous_close_digest",
        "publication_sequence",
        "adopted_results",
        "vouchers",
        "opening_calculation_id",
        "asset_batch_adoptions",
        "asset_card_adoptions",
        "inventories",
        "owner_confirmation",
        "readiness",
        "management_snapshot",
        "material_coverage",
        "trial_balance",
        "report_classification",
        "read_version",
        "approval",
        "owner_review",
    }
    if (
        not isinstance(manifest, dict)
        or set(manifest) != required
        or manifest.get("format") != CLOSE_FORMAT
        or type(manifest.get("format_version")) is not int
        or manifest.get("format_version") != CLOSE_FORMAT_VERSION
    ):
        _invalid("unsupported_close_contract")
    if type(manifest["publication_sequence"]) is not int or manifest["publication_sequence"] < 0:
        _invalid("invalid_publication_boundary")
    if not isinstance(manifest.get("management_snapshot"), dict) or not isinstance(
        manifest["management_snapshot"].get("entity_profiles"), list
    ):
        _invalid("missing_entity_profile_adoption")
    management = manifest["management_snapshot"]
    employees = management.get("employee_entities")
    if (
        not isinstance(employees, list)
        or any(not isinstance(value, str) for value in employees)
        or employees != sorted(set(employees))
        or not set(employees) <= {item["entity_id"] for item in management["entity_profiles"]}
    ):
        _invalid("invalid_employee_profile_adoption")
    try:
        YearMonth(manifest["period"])
        if manifest["previous_close_period"] is not None:
            if YearMonth(manifest["previous_close_period"]) >= YearMonth(manifest["period"]):
                raise ValueError("previous period")
            if len(bytes.fromhex(manifest["previous_close_digest"])) != 32:
                raise ValueError("previous digest")
        if len(bytes.fromhex(manifest["owner_confirmation"])) != 32:
            raise ValueError("owner evidence")
    except (TypeError, ValueError):
        _invalid("invalid_close_identity")
    for field in (
        "adopted_results",
        "vouchers",
        "asset_batch_adoptions",
        "asset_card_adoptions",
        "trial_balance",
    ):
        if not isinstance(manifest[field], list):
            _invalid("invalid_close_collection")
    for field in (
        "inventories",
        "readiness",
        "management_snapshot",
        "material_coverage",
        "report_classification",
        "read_version",
    ):
        if not isinstance(manifest[field], dict):
            _invalid("invalid_close_object")
    versions = manifest["read_version"]
    if set(versions) != {"accounting", "material", "management", "read_repair_revision"} or any(
        type(value) is not int or value < 0 for value in versions.values()
    ):
        _invalid("invalid_close_read_version")
    require_close_approval(manifest["approval"])
    from .close_review import require_owner_review

    require_owner_review(manifest["owner_review"])
    if (manifest["previous_close_period"] is None) != (manifest["previous_close_digest"] is None):
        _invalid("invalid_previous_close")
    fields = {
        "publication_id",
        "calculation_id",
        "result_digest",
        "subject_id",
        "fact_id",
        "source_period",
        "posting_period",
        "role",
    }
    calculations, publications, subjects = set(), set(), set()
    for item in manifest["adopted_results"]:
        if (
            not isinstance(item, dict)
            or set(item) != fields
            or any(not isinstance(value, str) or not value for value in item.values())
            or item["role"] not in ADOPTION_ROLES
        ):
            _invalid("invalid_direct_adoption")
        if (
            item["calculation_id"] in calculations
            or item["publication_id"] in publications
            or item["subject_id"] in subjects
        ):
            _invalid("duplicate_direct_adoption")
        calculations.add(item["calculation_id"])
        publications.add(item["publication_id"])
        subjects.add(item["subject_id"])
        try:
            YearMonth(item["source_period"])
            YearMonth(item["posting_period"])
            if len(bytes.fromhex(item["result_digest"])) != 32:
                raise ValueError("digest")
        except (TypeError, ValueError):
            _invalid("invalid_direct_adoption_identity")
        if item["posting_period"] != manifest["period"]:
            _invalid("direct_adoption_period_mismatch")
    opening = [
        item["calculation_id"]
        for item in manifest["adopted_results"]
        if item["role"] == "opening_basis"
    ]
    if len(opening) > 1 or manifest["opening_calculation_id"] != (opening[0] if opening else None):
        _invalid("opening_adoption_mismatch")
    voucher_ids = set()
    for item in manifest["vouchers"]:
        fields = {
            "id",
            "voucher_id",
            "calculation_id",
            "total",
            "number",
            "adopted_calculation_id",
            "result_digest",
            "reverses_id",
        }
        if (
            not isinstance(item, dict)
            or set(item) != fields
            or any(
                not isinstance(item[field], str) or not item[field]
                for field in fields - {"total", "number", "reverses_id"}
            )
            or any(
                type(item[field]) is not int or item[field] <= 0 for field in ("total", "number")
            )
            or (item["reverses_id"] is not None and not isinstance(item["reverses_id"], str))
            or item["adopted_calculation_id"] not in calculations
        ):
            _invalid("invalid_direct_voucher")
        if item["id"] in voucher_ids:
            _invalid("duplicate_direct_voucher")
        voucher_ids.add(item["id"])
    for item in manifest["asset_batch_adoptions"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"owner_calculation_id", "membership_digest"}
            or any(not isinstance(value, str) or not value for value in item.values())
        ):
            _invalid("invalid_asset_batch_adoption")
    for item in manifest["asset_card_adoptions"]:
        fields = {
            "contract_version",
            "asset_id",
            "calculation_id",
            "result_digest",
            "acceptance_calculation_id",
            "acceptance_result_digest",
        }
        if (
            not isinstance(item, dict)
            or set(item) != fields
            or type(item["contract_version"]) is not int
            or item["contract_version"] != 1
            or any(
                not isinstance(item[key], str) or not item[key]
                for key in fields - {"contract_version"}
            )
        ):
            _invalid("invalid_asset_card_adoption")
    return manifest


def direct_calculation_ids(manifest):
    """All explicitly named source results, not an assertion they share a role."""
    require_close_contract(manifest)
    identifiers = {item["calculation_id"] for item in manifest["adopted_results"]}
    identifiers.update(item["calculation_id"] for item in manifest["vouchers"])
    for item in manifest["asset_card_adoptions"]:
        identifiers.update((item["calculation_id"], item["acceptance_calculation_id"]))
    identifiers.update(item["owner_calculation_id"] for item in manifest["asset_batch_adoptions"])
    return identifiers
