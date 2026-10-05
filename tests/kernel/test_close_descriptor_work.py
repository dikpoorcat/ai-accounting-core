"""Authenticated descriptor access stays linear when complete bucket reads grow."""

import sqlite3

import pytest

from ai_accounting.kernel import close_storage
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.types import canonical


class CountedDescriptors(list):
    """Count actual authenticated descriptor visits, not SQL rows or scans."""

    def __init__(self, entries):
        super().__init__(entries)
        self.visits = 0

    def __iter__(self):
        for entry in super().__iter__():
            self.visits += 1
            yield entry

    def __getitem__(self, index):
        result = super().__getitem__(index)
        self.visits += len(result) if isinstance(index, slice) else 1
        return result


@pytest.mark.parametrize("bucket_count", [32, 128, 256])
def test_complete_bucket_reads_preserve_leaves_with_linear_descriptor_work(
    monkeypatch, bucket_count
):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE period_close(period INTEGER PRIMARY KEY);"
        + close_storage.CLOSE_STORAGE_DDL
    )
    connection.execute("INSERT INTO period_close VALUES(1)")
    field = "adopted_results"
    subjects = {}
    for index in range(100000):
        subject = f"synthetic-subject-{index}"
        subjects.setdefault(close_storage._bucket(subject), subject)
        if len(subjects) == 256:
            break
    assert len(subjects) == 256
    subjects = dict(sorted(subjects.items())[:bucket_count])
    descriptors, expected = [], []
    for position, (bucket, subject) in enumerate(subjects.items()):
        leaf = {"subject_id": subject, "calculation_id": f"calculation-{position}"}
        expected.append(leaf)
        body = canonical([[position, leaf]])
        directory = canonical([[0, close_storage._sha(body).hex(), 1]])
        connection.execute(
            "INSERT INTO close_storage_block VALUES(?,?,?,?,?,?)",
            (1, field, bucket, 0, body, close_storage._sha(body)),
        )
        connection.execute(
            "INSERT INTO close_storage_directory VALUES(?,?,?,?,?)",
            (1, field, bucket, directory, close_storage._sha(directory)),
        )
        descriptors.append([bucket, close_storage._sha(directory).hex(), 1])

    # Instrument only after the normal decoder authenticates the persisted
    # subroot against its committed SHA. No descriptor lookup is replaced.
    counted = []
    original_decode = close_storage._decode_family

    def decode(*args):
        subroot = original_decode(*args)
        entries = CountedDescriptors(subroot["directories"][field])
        subroot["directories"][field] = entries
        counted.append(entries)
        return subroot

    monkeypatch.setattr(close_storage, "_decode_family", decode)

    def commit(entries):
        content = canonical({"directories": {field: entries, "vouchers": []}})
        digest = close_storage._sha(content)
        connection.execute(
            "INSERT OR REPLACE INTO close_storage_subroot VALUES(?,?,?,?)",
            (1, "accounting", content, digest),
        )
        return close_storage.CloseHeader(
            1, b"l" * 32, b"s" * 32, {"subroots": {"accounting": digest.hex()}}
        )

    def read_group(header, subroot, requested, many):
        if many:
            rows = close_storage._buckets_rows_many(
                connection, [(header, subroot, field, requested)]
            )
            return {bucket: values for _, _, bucket, values in rows}
        return close_storage._buckets_rows(connection, header, subroot, field, requested)

    try:
        header = commit(descriptors)
        subroot = close_storage._family(connection, header, "accounting")
        serial = {
            bucket: close_storage._bucket_rows(connection, header, subroot, field, bucket)
            for bucket in subjects
        }
        assert [entry for values in serial.values() for entry in values] == [
            [position, leaf] for position, leaf in enumerate(expected)
        ]
        for many in (False, True):
            counted[-1].visits = 0
            assert read_group(header, subroot, [*subjects, 300, 300], many) == {
                **serial, 300: []
            }
            # A generous linear bound allows additional bounded passes, while
            # rejecting per-bucket rescans at each increasing synthetic size.
            assert counted[-1].visits <= 3 * bucket_count
            assert read_group(header, subroot, [], many) == {}
        counted.clear()
        assert close_storage.read_section(connection, header, field) == expected
        assert sum(entries.visits for entries in counted) <= 3 * bucket_count

        first = descriptors[0]
        conflicting = [first[0], "0" * 64, first[2]]
        for entries, succeeds in (([first, conflicting], True), ([conflicting, first], False)):
            header = commit(entries)
            subroot = close_storage._family(connection, header, "accounting")
            for many in (False, True):
                if succeeds:
                    assert read_group(header, subroot, [first[0], first[0]], many) == {
                        first[0]: serial[first[0]]
                    }
                else:
                    with pytest.raises(KernelError) as failure:
                        read_group(header, subroot, [first[0]], many)
                    assert failure.value.code == "content_integrity_failed"

        header = commit(descriptors)
        subroot = close_storage._family(connection, header, "accounting")
        connection.execute(
            "UPDATE close_storage_block SET content='[]' WHERE bucket=?", (first[0],)
        )
        for many in (False, True):
            with pytest.raises(KernelError) as failure:
                read_group(header, subroot, list(subjects), many)
            assert failure.value.code == "content_integrity_failed"
        with pytest.raises(KernelError) as failure:
            close_storage.read_section(connection, header, field)
        assert failure.value.code == "content_integrity_failed"
    finally:
        connection.close()
