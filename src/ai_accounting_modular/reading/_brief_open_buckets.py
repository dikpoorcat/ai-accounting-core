"""BriefOpen's fixed 256-way content directory; identities never change public ordering."""

from hashlib import sha256

from ai_accounting_modular.contracts.public import ContractError, canonical_json, content_digest


def corrupt():
    raise ContractError("storage_corrupt", "待收待付分桶摘要不完整，请重新生成。")


def key(collection, identity):
    return (
        collection + ":" + sha256(canonical_json([collection, identity]).encode()).hexdigest()[:2]
    )


def describe(values, *, displays=False):
    result = {
        "count": len(values),
        "digest": content_digest(
            sorted([order, identity, digest] for identity, (order, digest) in values.items())
        ),
    }
    if displays:
        result["keys_digest"] = content_digest(sorted(values))
    return result


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


def validate(roots, collections, displays):
    if not isinstance(roots, dict) or set(roots) != set(collections):
        corrupt()
    for collection, directory in roots.items():
        if not isinstance(directory, dict):
            corrupt()
        fields = (
            {"count", "digest", "keys_digest"} if collection == displays else {"count", "digest"}
        )
        for bucket, value in directory.items():
            if (
                not isinstance(bucket, str)
                or len(bucket) != 2
                or any(char not in "0123456789abcdef" for char in bucket)
                or not isinstance(value, dict)
                or set(value) != fields
                or type(value["count"]) is not int
                or value["count"] <= 0
                or any(
                    not isinstance(value[field], str)
                    or len(value[field]) != 64
                    or any(char not in "0123456789abcdef" for char in value[field])
                    for field in fields - {"count"}
                )
            ):
                corrupt()
