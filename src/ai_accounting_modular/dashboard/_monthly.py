"""Keep month page envelopes and read semantics consistent across the existing UI."""

from ai_accounting_modular.contracts.public import ContractError, content_digest


def response(delivery, schema_version, now):
    if delivery.state != "ready":
        raise ContractError(
            "content_integrity_failed" if delivery.reason == "corrupt" else "temporary_failure",
            "所选页面读取内容损坏，请重新生成。"
            if delivery.reason == "corrupt"
            else "所选页面尚未准备好，请稍后重试。",
        )
    payload = delivery.payload
    selected = payload["selected_period"]
    closed = selected is not None and selected["status"] == "closed"
    return {
        "schema_version": schema_version,
        "snapshot_version": delivery.version,
        "selected_period": selected,
        "read_semantics": {
            "knowledge": "current_knowledge",
            "accounting": "as_posted",
            "business_basis": "frozen_adoption" if closed else "current_known",
            "display": "frozen_with_current_supplements" if closed else "current",
            "system_time_replay": False,
            "recorded_at": "system_recording_time",
            "recording_period": "business_recording_period",
            "recorded_later": "business_recording_period_after_selected_period",
        },
        "read_context": {
            "company_id": payload["company_id"],
            "database_id": payload["database_id"],
            "as_of": now.date().isoformat(),
            "read_version": delivery.version
            or content_digest(
                {
                    "company_id": payload["company_id"],
                    "database_id": payload["database_id"],
                    "selection": "no_established_month",
                }
            ),
        },
        "data": payload["data"],
    }


def envelope(company_id, delivery, ready_response=None):
    result = {
        "schema_version": 6,
        "company_id": company_id,
        "period": delivery.scope.get("period_to"),
        "model_version": delivery.version,
        "state": delivery.state,
    }
    if delivery.state == "ready":
        result["response"] = ready_response
    elif delivery.state == "preparing":
        result["retry_after_ms"] = 100
    else:
        result["reason"] = delivery.reason
    return result
