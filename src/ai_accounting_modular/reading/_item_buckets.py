"""Pure 256-way reading directories; each owner fixes its format and collections."""

from hashlib import sha256

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    content_digest,
    loads_json,
)


def corrupt():
    raise ContractError("storage_corrupt", "读取集合分桶摘要不完整，请重新生成。")


def key(collection, identity):
    return (
        collection + ":" + sha256(canonical_json([collection, identity]).encode()).hexdigest()[:2]
    )


def prepare(collection, bucket, values, *, namespace, private, keyed=False):
    """One canonical factory for complete and incremental physical buckets."""
    if not values or any(
        not isinstance(identity, str)
        or not identity
        or key(collection, identity) != collection + ":" + bucket
        for identity in values
    ):
        corrupt()
    if private:
        if any(not isinstance(value, dict) for value in values.values()):
            corrupt()
        field, entries = "values", [[identity, values[identity]] for identity in sorted(values)]
    else:
        if any(
            not isinstance(value, tuple)
            or len(value) != 2
            or not isinstance(value[0], str)
            or not value[0]
            or not _digest(value[1])
            for value in values.values()
        ):
            corrupt()
        field, entries = (
            "triples",
            sorted([order, identity, digest] for identity, (order, digest) in values.items()),
        )
    entries_json = canonical_json(entries)
    prefix = canonical_json(
        {
            "format": namespace,
            "kind": "private" if private else "mirror",
            "collection": collection,
            "bucket": bucket,
        }
    )
    raw = prefix[:-1] + ',"' + field + '":' + entries_json + "}"
    body_digest = sha256(raw.encode("utf-8")).hexdigest()
    result = {"count": len(values), "digest": body_digest}
    if not private:
        result.update(
            digest=sha256(entries_json.encode("utf-8")).hexdigest(), body_digest=body_digest
        )
    if keyed:
        result["keys_digest"] = content_digest(sorted(values))
    return result, raw


def read(raw, collection, bucket, expected, *, namespace, private, keyed=False):
    """Actual bytes, exact membership and canonical order all belong to the header."""
    try:
        if sha256(raw.encode("utf-8")).hexdigest() != expected.get(
            "body_digest", expected["digest"]
        ):
            corrupt()
        value = loads_json(raw)
        field = "values" if private else "triples"
        if (
            not isinstance(value, dict)
            or set(value) != {"format", "kind", "collection", "bucket", field}
            or value["format"] != namespace
            or value["kind"] != ("private" if private else "mirror")
            or value["collection"] != collection
            or value["bucket"] != bucket
            or not isinstance(value[field], list)
        ):
            corrupt()
        entries, values = value[field], {}
        for entry in entries:
            if not isinstance(entry, list) or len(entry) != (2 if private else 3):
                corrupt()
            identity = entry[0] if private else entry[1]
            if not isinstance(identity, str) or identity in values:
                corrupt()
            values[identity] = entry[1] if private else (entry[0], entry[2])
        descriptor, canonical = prepare(
            collection, bucket, values, namespace=namespace, private=private, keyed=keyed
        )
        if descriptor != expected or canonical != raw:
            corrupt()
        return values
    except (ContractError, KeyError, TypeError, ValueError, AttributeError) as error:
        raise ContractError(
            "storage_corrupt", "读取集合桶正文或目录不完整，请重新生成。"
        ) from error


def _digest(value):
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def manifest(roots):
    return {
        collection: {
            "count": sum(value["count"] for value in directory.values()),
            "digest": content_digest(
                [
                    [bucket, value["count"], value["digest"]]
                    for bucket, value in sorted(directory.items())
                ]
            ),
        }
        for collection, directory in sorted(roots.items())
    }


def display_head(directory):
    return content_digest(
        [
            [bucket, value["count"], value["keys_digest"]]
            for bucket, value in sorted(directory.items())
        ]
    )


def validate(roots, collections, keyed, public):
    keyed = (keyed,) if isinstance(keyed, str) else keyed
    if not isinstance(roots, dict) or set(roots) != set(collections):
        corrupt()
    for collection, directory in roots.items():
        if not isinstance(directory, dict):
            corrupt()
        fields = {"count", "digest"}
        if collection in keyed:
            fields.add("keys_digest")
        if collection in public:
            fields.add("body_digest")
        for bucket, value in directory.items():
            if (
                not isinstance(bucket, str)
                or len(bucket) != 2
                or any(char not in "0123456789abcdef" for char in bucket)
                or not isinstance(value, dict)
                or set(value) != fields
                or type(value["count"]) is not int
                or value["count"] <= 0
                or any(not _digest(value[field]) for field in fields - {"count"})
            ):
                corrupt()
