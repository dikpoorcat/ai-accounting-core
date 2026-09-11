import sys

import pytest

from ai_accounting.kernel.security.primitives import IdentityError
from ai_accounting.kernel.security.windows import (
    read_protected_bytes,
    read_protected_json,
    write_protected_bytes,
    write_protected_json,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows DACL boundary")


def test_private_metadata_roundtrip_and_atomic_replace(tmp_path):
    path = tmp_path / "service.json"
    write_protected_json(path, {"pid": 1, "purpose": "synthetic test"})
    assert read_protected_json(path) == {"pid": 1, "purpose": "synthetic test"}
    write_protected_json(path, {"pid": 2})
    assert read_protected_json(path) == {"pid": 2}
    assert len(list(tmp_path.iterdir())) == 1


def test_unprotected_existing_metadata_is_rejected(tmp_path):
    path = tmp_path / "service.json"
    path.write_text("{}")
    with pytest.raises(IdentityError, match="STATE_ACCESS_DENIED"):
        read_protected_json(path)
    with pytest.raises(IdentityError, match="STATE_ACCESS_DENIED"):
        write_protected_json(path, {"pid": 2})
    assert path.read_text() == "{}"


def test_private_metadata_content_and_size_limits(tmp_path):
    path = tmp_path / "service.json"
    write_protected_bytes(path, b"[]")
    with pytest.raises(IdentityError, match="STATE_INVALID"):
        read_protected_json(path)
    with pytest.raises(IdentityError, match="STATE_INVALID"):
        read_protected_bytes(path, max_bytes=1)
    with pytest.raises(IdentityError, match="STATE_INVALID"):
        write_protected_bytes(path, b"x" * 65537)
