from __future__ import annotations

import pytest

from ._support import SyntheticProducer, make_storage, service


@pytest.fixture
def file_case(tmp_path):
    storage, companies = make_storage(tmp_path)
    producer = SyntheticProducer()
    files = service(storage, producer)
    try:
        yield storage, companies, producer, files
    finally:
        files.close()
