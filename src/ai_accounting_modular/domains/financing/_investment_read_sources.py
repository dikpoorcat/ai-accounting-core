"""Resolve exact public adopted result identities for investment projections."""

from ._common import fail
from ._read_funds import _contents


def source_contents(ledger, sources, read, cache):
    unique = {}
    for source in sources:
        previous = unique.setdefault(source["result_id"], source)
        if previous != source:
            fail("同一次投资读取采用的正式来源身份相互冲突。", "storage_corrupt")
        if source["result_id"] in cache and cache[source["result_id"]].source != source:
            fail("投资读取的缓存正文与精确采用身份不符。", "storage_corrupt")
    needed = [source for key, source in unique.items() if key not in cache]
    located = []
    for offset in range(0, len(needed), 500):
        batch = needed[offset : offset + 500]
        located.extend(ledger.get_adopted_sources(batch, read))
    if {entry.source["result_id"] for entry in located} != {
        source["result_id"] for source in needed
    }:
        fail("投资读取所需的精确正式来源不完整。", "storage_corrupt")
    bodies = _contents(ledger, [entry.reference for entry in located], read, cache)
    result = {key: cache[key] for key in unique if key in cache}
    for entry in located:
        body = bodies[entry.reference["result_id"]]
        if entry.source != unique[entry.source["result_id"]] or body.source != entry.source:
            fail("投资读取的正文与精确采用身份不符。", "storage_corrupt")
        result[entry.source["result_id"]] = body
    return result
