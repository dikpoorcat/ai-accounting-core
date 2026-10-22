"""BriefOpen keeps its existing bucket namespace and exact canonical format."""

from . import _item_buckets
from ._item_buckets import corrupt as corrupt
from ._item_buckets import display_head as display_head
from ._item_buckets import key as key
from ._item_buckets import manifest as manifest
from ._item_buckets import validate as validate

FORMAT = "ai-accounting-reading-open-bucket/1"


def prepare(collection, bucket, values, *, private, displays=False):
    return _item_buckets.prepare(
        collection, bucket, values, namespace=FORMAT, private=private, keyed=displays
    )


def read(raw, collection, bucket, expected, *, private, displays=False):
    return _item_buckets.read(
        raw, collection, bucket, expected, namespace=FORMAT, private=private, keyed=displays
    )
