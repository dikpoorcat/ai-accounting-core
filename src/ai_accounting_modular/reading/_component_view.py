"""Strict actual component summaries and views owned by one read transaction."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import (
    ContractError,
    UnitOfWork,
    content_digest,
    loads_json,
)

from . import _descriptor


def summary(scope_key, component, state, row, *, repository=None):
    """Validate actual header bindings, content digest and the owner's summary contract."""
    try:
        if repository is not None:
            descriptor = _descriptor.read(scope_key, component, state, row)
            row = repository.component_body(scope_key, component, descriptor)
            state = _descriptor.body_state(state, descriptor)
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
        if content_digest(payload) != state["content_digest"]:
            raise ValueError
        if (
            owner is not None
            and "descriptor_semantic_digest" in state
            and payload.get("_semantic_digest") != state["descriptor_semantic_digest"]
        ):
            raise ValueError
        epoch = None
        if component == "brief_activity":
            epoch = owner.validate(payload, scope_key)
        elif owner is not None:
            epoch = owner.validate(payload)
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
        binding = _binding(scope_key, component, read, state, payload)
        self._read, self._binding = read, binding
        self._scope_key, self._component = scope_key, component
        self._state = dict(state)
        self._descriptor = _descriptor.read(scope_key, component, state, payload)
        self._summary = self._directory = None
        self._epoch = None
        self._counts = {} if not isinstance(read, UnitOfWork) else None

    def matches(self, read, binding):
        return self._read is read and self._binding == binding

    @property
    def state(self):
        return dict(self._state)

    @property
    def semantic_digest(self):
        return self._descriptor["semantic_digest"]

    def _load(self):
        if self._summary is None:
            row = self._read.repository("reading").component_body(
                self._scope_key, self._component, self._descriptor
            )
            self._summary, self._epoch = summary(
                self._scope_key,
                self._component,
                _descriptor.body_state(self._state, self._descriptor),
                row,
            )
        return self._summary

    @property
    def directory(self):
        if self._component not in {"brief_activity", "brief_open_items"}:
            return None
        if self._directory is None:
            if self._component == "brief_activity":
                from ._brief_activity_directory import Directory
            else:
                from ._brief_open_index import Directory

            self._directory = Directory.from_summary(
                self._read.repository("reading"), self._scope_key, self._load(), self._epoch
            )
            if self._counts is not None and self._component == "brief_activity":
                from ._collections import CollectionFilter

                self._counts.update(
                    ((collection, CollectionFilter()), (total, total))
                    for collection, total in self._directory.total_counts.items()
                )
        return self._directory

    def monthly_summary(self):
        value = self._load()
        if self._component == "brief_activity":
            from . import _brief_activity_index as owner
        elif self._component == "brief_open_items":
            from . import _brief_open_index as owner
        else:
            owner = None
        if owner is not None:
            return owner.public_summary(value)
        return deepcopy({key: item for key, item in value.items() if key != "_collections"})

    def manifest(self, collection):
        manifests = self._load().get("_collections")
        if not isinstance(manifests, dict):
            raise ContractError("storage_corrupt", "读取集合目录的内容摘要不一致。")
        value = manifests.get(collection)
        if not isinstance(value, dict) or set(value) != {"count", "digest"}:
            raise ContractError("invalid_input", "所选模型不提供这个读取集合。")
        return dict(value)

    def item_counts(self, collection, query):
        """Reuse exact SQL counts only while this actual read publication remains bound."""
        from ._collections import CollectionFilter

        repository = self._read.repository("reading")
        if self._counts is None:
            return repository.item_counts(self._scope_key, self._component, collection, query)
        identity = collection, query
        if identity in self._counts:
            return self._counts[identity]
        total_key = collection, CollectionFilter()
        # The repository can omit an already verified total without changing the
        # exact filtered count. Keep the SQL decision in its existing owner.
        total = self._counts.get(total_key)
        result = repository.item_counts(
            self._scope_key,
            self._component,
            collection,
            query,
            total=None if total is None else total[0],
        )
        self._counts[identity] = result
        self._counts[total_key] = result[0], result[0]
        return result


def ready(scope_key, component, read):
    """Re-read real state/raw every time; callers never supply a checked flag or proof."""
    repository = read.repository("reading")
    state = repository.state(scope_key, component)
    payload = (
        repository.payload(scope_key, component) if state and state["state"] == "ready" else None
    )
    binding = _binding(scope_key, component, read, state, payload)
    # UnitOfWork publicly identifies writable/maintenance transactions. Such a
    # transaction can change stored indexes without changing the component header.
    writable = isinstance(read, UnitOfWork)
    identity = scope_key, component
    current = repository._component_views.get(identity) if not writable else None
    if type(current) is _ComponentView and current.matches(read, binding):
        return current
    view = _ComponentView(scope_key, component, read, state, payload)
    if not writable:
        repository._component_views[identity] = view
    return view
