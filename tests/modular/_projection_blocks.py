"""In-memory publication adapter for the ledger's public immutable preparation port."""


class Blocks:
    header_json = None

    def __init__(self):
        self.values = {}

    def get_blocks(self, keys):
        return tuple(self.values.get(key) for key in keys)

    def publish(self, plan):
        for key in plan.deletes:
            del self.values[key]
        self.values.update(((collection, key), raw) for collection, key, raw in plan.writes)
        self.header_json = plan.header_json
