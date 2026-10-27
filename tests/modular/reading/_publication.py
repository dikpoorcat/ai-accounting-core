"""Capture real test lifecycles and locate their descriptor-bound owner bodies."""

from ai_accounting_modular.contracts.public import canonical_json, content_digest, loads_json
from ai_accounting_modular.reading import _descriptor, _subscription_keys
from ai_accounting_modular.reading._publication_guard import CaptureGuard
from ai_accounting_modular.reading._subscriptions import Directory


class CapturedState(dict):
    def __init__(self, state, head):
        super().__init__(state)
        self.guard = CaptureGuard.capture(state, head)


class PreparedSummary(dict):
    def __init__(self, model):
        super().__init__(
            (key, value)
            for key, value in loads_json(model.payload_json).items()
            if key not in {"_item_epoch", "_semantic_digest"}
        )
        self.inputs = model.inputs


def body(repository, scope_key, component):
    state = repository.state(scope_key, component)
    row = repository.payload(scope_key, component)
    descriptor = _descriptor.read(scope_key, component, state, row)
    return repository.component_body(scope_key, component, descriptor)


def assert_subscriptions(repository, scope_key, component, inputs):
    directory = Directory(repository)
    known, root = directory._target(scope_key, component)
    assert known
    actual = {directory._selector(entry) for entry in directory.forward.entries(root)}
    month = directory._month(scope_key, component)
    assert actual == _subscription_keys.selectors(inputs) | _subscription_keys.automatic(
        component, month
    )


def replace_body(repository, scope_key, component, model):
    """Seal a deliberately forged owner body, leaving its deeper defect to the test."""
    state = repository.state(scope_key, component)
    row = repository.payload(scope_key, component)
    descriptor = _descriptor.read(scope_key, component, state, row)
    descriptor.update(
        body_digest=model.content_digest,
        semantic_digest=loads_json(model.payload_json).get(
            "_semantic_digest", model.content_digest
        ),
    )
    repository._session.execute(
        "UPDATE read_item SET payload_json=? WHERE scope_key=? AND component_key=? "
        "AND collection_key=? AND item_id=?",
        (model.payload_json, scope_key, component, _descriptor.BODY_COLLECTION, component),
    )
    repository._session.execute(
        "UPDATE read_component SET payload_json=? WHERE scope_key=? AND component_key=?",
        (canonical_json(descriptor), scope_key, component),
    )
    repository._session.execute(
        "UPDATE read_scope_head SET content_digest=? WHERE scope_key=? AND component_key=?",
        (content_digest(descriptor), scope_key, component),
    )
