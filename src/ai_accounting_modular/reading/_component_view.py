"""Fresh component views and bounded, database-independent checks of exact summary bytes."""

from collections import OrderedDict
from copy import deepcopy
from threading import Lock

from ai_accounting_modular.contracts.public import (
    ContractError,
    UnitOfWork,
    content_digest,
    loads_json,
)

from . import _dependencies

_SUMMARY_LIMIT = 16
_SUMMARY_BYTES = 8 * 1024 * 1024
_summary_checks = OrderedDict()
_summary_lock = Lock()


def summary(scope_key, component, state, row):
    """Parse anew; reuse only scalar checks of the same complete persisted raw."""
    try:
        raw = row["payload_json"]
        payload = loads_json(raw)
        if (
            not isinstance(payload, dict)
            or row["head"] != state["ready_head"]
            or (state["scope_key"], state["component_key"]) != (scope_key, component)
            or (row["scope_key"], row["component_key"]) != (scope_key, component)
        ):
            raise ValueError
        owner = None
        if component == "brief_activity":
            from . import _brief_activity_index as owner
        elif component == "brief_open_items":
            from . import _brief_open_index as owner
        version = state["format_version"]
        if owner is not None and version != owner.FORMAT_VERSION:
            raise ValueError
        identity = scope_key, component
        cacheable = (
            owner is not None
            and type(raw) is str
            and type(scope_key) is str
            and type(component) is str
            and type(version) is int
        )
        with _summary_lock:
            checked = _summary_checks.get(identity) if cacheable else None
            if checked is not None and checked[:2] == (version, raw):
                _summary_checks.move_to_end(identity)
            else:
                checked = None
        if checked is None:
            digest = content_digest(payload)
            epoch = None
            if component == "brief_activity":
                epoch = owner.validate(payload, scope_key)
            elif owner is not None:
                epoch = owner.validate(payload)
            if owner is not None:
                _dependencies.inputs(payload)
        else:
            _, _, digest, epoch, _ = checked
        if digest != state["content_digest"]:
            raise ValueError
        if checked is None and cacheable:
            size = len(raw.encode("utf-8"))
            with _summary_lock:
                # Candidate payloads are checked outside this cache; publication
                # may recheck only its persisted base here.
                _summary_checks.pop(identity, None)
                if size <= _SUMMARY_BYTES:
                    _summary_checks[identity] = version, raw, digest, epoch, size
                    while (
                        len(_summary_checks) > _SUMMARY_LIMIT
                        or sum(value[4] for value in _summary_checks.values()) > _SUMMARY_BYTES
                    ):
                        _summary_checks.popitem(last=False)
        return payload, epoch
    except (ContractError, KeyError, ValueError, TypeError) as error:
        raise ContractError("storage_corrupt", "读取集合目录的内容摘要不一致。") from error


def _binding(scope_key, component, read, state, payload):
    if state is None or state["state"] != "ready":
        raise ContractError("read_model_not_ready", "所选读取集合尚未就绪。")
    if (
        payload is None
        or payload["head"] != state["ready_head"]
        or state["ready_head"] != state["wanted_head"]
        or (state["scope_key"], state["component_key"]) != (scope_key, component)
        or (payload["scope_key"], payload["component_key"]) != (scope_key, component)
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
        directory = None
        if component == "brief_activity":
            from ._brief_activity_directory import Directory

            directory = Directory.from_row(repository, state, payload)
        elif component == "brief_open_items":
            from ._brief_open_index import Directory

            directory = Directory.from_row(repository, state, payload)
        value = (
            directory.payload
            if directory is not None
            else summary(scope_key, component, state, payload)[0]
        )
        self._read, self._binding = read, binding
        self._state, self._summary = dict(state), value
        self.directory = directory

    def matches(self, read, binding):
        return self._read is read and self._binding == binding

    @property
    def state(self):
        return dict(self._state)

    def monthly_summary(self):
        if self.directory is None:
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
