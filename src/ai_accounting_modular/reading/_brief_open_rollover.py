"""Select a checked previous month and recheck its exact proof before a fork."""

from ai_accounting_modular.contracts.public import ContractError, loads_json

from . import _brief_open_index as index


def initial(repository, state):
    return (
        state["state"] == "preparing"
        and state["component_key"] == index.COMPONENT
        and state["format_version"] == index.FORMAT_VERSION
        and state["ready_head"] == 0
        and state["content_digest"] is None
        and repository.payload(state["scope_key"], index.COMPONENT) is None
    )


def _scope(key):
    from . import _monthly

    value = loads_json(key)
    if set(value) != {"page", "scope"} or value["page"] != "monthly":
        return None
    normalized = _monthly.normalize(value["scope"])
    return normalized if _monthly.key(normalized) == key else None


def _ready(repository, state):
    return (
        state is not None
        and state["component_key"] == index.COMPONENT
        and state["format_version"] == index.FORMAT_VERSION
        and state["state"] == "ready"
        and state["ready_head"] == state["wanted_head"]
        and not repository.open_pending_rows(state["scope_key"])
    )


def select(repository, scope, current_cutoff, start_period):
    from . import _monthly

    for state in repository.previous_ready(_monthly.key(scope), index.COMPONENT):
        if not _ready(repository, state):
            continue
        try:
            previous = _scope(state["scope_key"])
            if (
                previous is not None
                and previous["company_id"] == scope["company_id"]
                and start_period <= previous["period_to"] < scope["period_to"]
            ):
                directory = index.base(repository, state)
                payload = directory.payload if directory is not None else None
                if (
                    payload is not None
                    and payload["open_items"]["cutoff_period"] == previous["period_to"]
                    and payload["open_items"]["current_cutoff_period"] == current_cutoff
                ):
                    return state, payload, directory
        except (ContractError, KeyError, TypeError, ValueError):
            continue
    return None


def matches(repository, patch, target_scope, proposed):
    try:
        previous, target = _scope(patch.base_scope), _scope(target_scope)
        if (
            previous is None
            or target is None
            or previous["company_id"] != target["company_id"]
            or previous["period_to"] >= target["period_to"]
            or proposed["open_items"]["cutoff_period"] != target["period_to"]
        ):
            return False
        state = repository.state(patch.base_scope, index.COMPONENT)
        if not _ready(repository, state) or (state["ready_head"], state["content_digest"]) != (
            patch.base_ready,
            patch.base_digest,
        ):
            return False
        directory = index.base(repository, state)
        payload = directory.payload if directory is not None else None
        return (
            payload is not None
            and payload["open_items"]["cutoff_period"] == previous["period_to"]
            and payload["open_items"]["current_cutoff_period"]
            == proposed["open_items"]["current_cutoff_period"]
            and directory.epoch == patch.item_epoch
        )
    except (ContractError, KeyError, TypeError, ValueError):
        return False
