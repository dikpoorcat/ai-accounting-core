"""Coordinate a typed bank-file request using payment and durable-file public ports."""

from uuid import uuid4

from ai_accounting_modular.contracts.public import ContractError, validate_contract


def _without_action(request):
    return {key: value for key, value in request.items() if key not in {"action", "request_id"}}


def _result(state, inspection, task=None):
    result = {"state": state, "inspection": inspection}
    if task is not None:
        result["task"] = task
    return validate_contract("core.schema.json", "PaymentExportResult", result)


def run(application, request):
    validate_contract("core.schema.json", "PaymentExportRequest", request)
    if application.payments is None or application.files is None:
        raise ContractError("unsupported", "当前实例未装配银行代发表办理。")
    action, company = request["action"], request["company_id"]
    selected = _without_action(request)
    if action == "status":
        return _status(application, selected)
    with application.storage.read_unit(company) as read:
        if action == "inspect":
            return _result("inspected", application.payments.inspect(selected, read))
        inspection, plan = application.payments.prepare(selected, read)
    if plan is None:
        # An empty search does not prove that all real-world work is known.
        known_zero = inspection["candidates"] and inspection["total_fen"] == "0"
        only_template = all(
            issue["field"] in {"/template_source_id", "/template_layout"}
            for issue in inspection["issues"]
        )
        return _result(
            "no_payments" if known_zero and only_template else "needs_information", inspection
        )
    receipt = application.files.ensure_current(
        plan, plan["scope_head"], request.get("request_id") or str(uuid4())
    )
    return _result(receipt["state"], inspection, receipt)


def _status(application, request):
    current = application.files.current_plan(
        request["company_id"], "payment_export", application.payments.scope_key(request)
    )
    selected = dict(request)
    if current is not None and current.plan is not None:
        template = current.plan["template"]
        selected.setdefault("template_source_id", template["source_id"])
        if selected["template_source_id"] == template["source_id"]:
            selected.setdefault("template_layout", template["layout"])
    with application.storage.read_unit(request["company_id"]) as read:
        inspection = application.payments.inspect(
            selected, read, resolve_template="template_layout" not in selected
        )
    if current is None:
        return _result("not_generated", inspection)
    receipt = current.receipt
    if current.plan is not None and receipt["scope_head"] != inspection["scope_head"]:
        return _result("expired", inspection)
    return _result(receipt["state"], inspection, receipt)
