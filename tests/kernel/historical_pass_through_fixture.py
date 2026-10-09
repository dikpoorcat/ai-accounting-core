"""Explicit prior registration rules for isolated synthetic historical sources."""

from contextlib import contextmanager
from copy import deepcopy

import pytest

from ai_accounting.kernel.domains.transactions import PassThrough


@contextmanager
def prior_pass_through_registration():
    """Create former nullable records only within an explicit test fixture scope."""
    field = PassThrough.model_fields["beneficiary_id"]
    metadata = deepcopy(field.json_schema_extra)
    metadata["x-accounting-fact"].pop("required_for_registration", None)
    metadata["x-accounting-fact"].pop("requires_named_entity", None)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(field, "json_schema_extra", metadata)
        yield
