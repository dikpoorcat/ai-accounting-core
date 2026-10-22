"""Private exact component proofs, reusable only inside one unchanged read transaction."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import (
    ContractError,
    UnitOfWork,
    content_digest,
    loads_json,
)

from . import _dependencies


def _binding(scope_key, component, read, state, payload):
    if state is None or state["state"] != "ready":
        raise ContractError("read_model_not_ready", "所选读取集合尚未就绪。")
    if (
        payload is None
        or payload["head"] != state["ready_head"]
        or state["ready_head"] != state["wanted_head"]
    ):
        raise ContractError("storage_corrupt", "读取集合与已就绪范围不一致。")
    return (
        read.company_id,
        read.database_id,
        scope_key,
        component,
        tuple(sorted(state.items())),
        tuple(sorted(payload.items())),
    )


class _ComponentView:
    def __init__(self, scope_key, component, read, state, payload):
        repository = read.repository("reading")
        binding = _binding(scope_key, component, read, state, payload)
        try:
            summary = loads_json(payload["payload_json"])
            if not isinstance(summary, dict) or content_digest(summary) != state["content_digest"]:
                raise ValueError
        except (ContractError, ValueError, TypeError) as error:
            raise ContractError("storage_corrupt", "读取集合目录的内容摘要不一致。") from error
        directory = None
        if component == "brief_activity":
            from ._brief_activity_directory import Directory

            directory = Directory(repository, scope_key, summary)
        elif component == "brief_open_items":
            from ._brief_open_index import Directory

            directory = Directory(repository, scope_key, summary)
        self._read, self._binding = read, binding
        self._state, self._summary = dict(state), summary
        self.directory = directory

    def matches(self, read, binding):
        return self._read is read and self._binding == binding

    @property
    def state(self):
        return dict(self._state)

    def monthly_summary(self):
        _dependencies.inputs(self._summary)
        return deepcopy(
            {
                key: value
                for key, value in self._summary.items()
                if key not in {"_inputs", "_item_epoch"}
            }
        )

    def manifest(self, collection):
        manifests = self._summary.get("_collections")
        if not isinstance(manifests, dict):
            raise ContractError("storage_corrupt", "读取集合目录的内容摘要不一致。")
        value = manifests.get(collection)
        if not isinstance(value, dict) or set(value) != {"count", "digest"}:
            raise ContractError("invalid_input", "所选模型不提供这个读取集合。")
        return dict(value)


def ready(scope_key, component, read):
    """Re-read real state/raw every time; callers never supply a checked flag or proof."""
    repository = read.repository("reading")
    state = repository.state(scope_key, component)
    payload = (
        repository.payload(scope_key, component) if state and state["state"] == "ready" else None
    )
    binding = _binding(scope_key, component, read, state, payload)
    # UnitOfWork publicly identifies writable/maintenance transactions. Such a
    # transaction can change buckets without changing the component header.
    writable = isinstance(read, UnitOfWork)
    identity = scope_key, component
    current = repository._component_views.get(identity) if not writable else None
    if type(current) is _ComponentView and current.matches(read, binding):
        return current
    view = _ComponentView(scope_key, component, read, state, payload)
    if not writable:
        repository._component_views[identity] = view
    return view
