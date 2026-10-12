"""One typed readiness boundary for owner native/HTTP/CLI/MCP reads."""

from .contracts import KernelError
from .response_contracts import RESPONSE_ADAPTERS, validate_response


def deliver(service, command, payload, authority):
    from .dashboard import Dashboard
    from .dashboard_async import delivery_state

    engine = service.engine(payload["company_id"], dashboard_read=True)
    engine.commit_guard = lambda: service._commit_authority(authority)
    period = payload.get("period")
    external_period = period
    if command == "dashboard_quarterly_report":
        year, quarter = payload["year"], payload["quarter"]
        external_period = f"{year:04d}-Q{quarter}"
        with engine.store.connection(read_only=True) as connection:
            row = connection.execute(
                "SELECT period FROM dashboard_projection_request WHERE period BETWEEN ? AND ? "
                "ORDER BY period DESC LIMIT 1",
                (f"{year:04d}-{quarter * 3 - 2:02d}", f"{year:04d}-{quarter * 3:02d}"),
            ).fetchone()
            period = row[0] if row else None
    elif period is None:
        period = Dashboard(engine).context()["default_period"]
        external_period = period
    envelope = {"schema_version": 6, "company_id": payload["company_id"], "period": external_period}
    if period is None:
        response = validate_response(
            command, service._dispatch(command, payload, authority=authority)
        )
        return RESPONSE_ADAPTERS[command + "_delivery"].validate_python(
            {
                **envelope,
                "state": "ready",
                "model_version": response.get("snapshot_version"),
                "response": response,
            }
        )
    state = delivery_state(engine, period)
    if state["state"] != "ready":
        return RESPONSE_ADAPTERS[command + "_delivery"].validate_python({**envelope, **state})
    try:
        response = validate_response(
            command, service._dispatch(command, payload, authority=authority)
        )
        latest = delivery_state(engine, period)
        if latest["state"] != "ready" or latest["model_version"] != state["model_version"]:
            return RESPONSE_ADAPTERS[command + "_delivery"].validate_python(
                {
                    **envelope,
                    "state": "preparing",
                    "model_version": None,
                    "retry_after_ms": 500,
                }
            )
        return RESPONSE_ADAPTERS[command + "_delivery"].validate_python(
            {
                **envelope,
                "state": "ready",
                "model_version": response.get(
                    "snapshot_version", response["read_context"]["read_version"]
                ),
                "response": response,
            }
        )
    except KernelError as exc:
        if exc.code != "content_integrity_failed":
            raise
        return RESPONSE_ADAPTERS[command + "_delivery"].validate_python(
            {
                **envelope,
                "state": "failed",
                "model_version": None,
                "reason": "corrupt",
            }
        )
