from __future__ import annotations

import inspect

import pytest

from ai_accounting.coa import seed_organization
from ai_accounting.models import Organization
from ai_accounting.taxpayer_identity import normalize_taxpayer_identification_number


def test_taxpayer_identification_number_is_normalized_and_checksum_validated() -> None:
    assert (
        normalize_taxpayer_identification_number(" 91330106ma1234567t ")
        == "91330106MA1234567T"
    )

    for invalid in (
        "91330106MA1234567X",
        "91330106MI1234567T",
        "91330106MA123456",
    ):
        with pytest.raises(ValueError, match="INVALID_TAXPAYER_IDENTIFICATION_NUMBER"):
            normalize_taxpayer_identification_number(invalid)


def test_organization_creation_requires_an_explicit_taxpayer_identification_number() -> None:
    parameter = inspect.signature(seed_organization).parameters[
        "taxpayer_identification_number"
    ]
    assert parameter.default is inspect.Parameter.empty

    with pytest.raises(ValueError, match="INVALID_TAXPAYER_IDENTIFICATION_NUMBER"):
        Organization(
            name="无效税号企业",
            taxpayer_identification_number="91330106MA1234567X",
        )
