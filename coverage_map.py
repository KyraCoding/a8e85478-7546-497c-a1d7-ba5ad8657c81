import gzip
import json
from pathlib import Path


class Coverage:
    def __init__(self, path=None):
        path = path or Path(__file__).parent / "maps" / "coverage.json.gz"
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            data = json.load(stream)
        self.nodes = data["nodes"]
        self.missing = set(data["missing"])
        self._lines = data["lines"]
        self._reads = data["reads"]
        self._masks = [int(bits, 16) for bits in data["masks"]]

    def present(self, path):
        return path in self._lines

    def has_line(self, path, n):
        indices = self._lines.get(path, ())
        return 0 < n < len(indices) and bool(indices[n])

    def _members(self, mask):
        result = set()
        while mask:
            lowest = mask & -mask
            result.add(self.nodes[lowest.bit_length() - 1])
            mask ^= lowest
        return result

    def lines(self, path, numbers):
        indices = self._lines.get(path, ())
        mask = 0
        for n in numbers:
            if 0 < n < len(indices):
                mask |= self._masks[indices[n]]
        return self._members(mask)

    def fixture(self, path):
        return self._members(self._masks[self._reads.get(path, 0)])

    def file_execution(self, path):
        mask = 0
        for i in set(self._lines.get(path, ())):
            mask |= self._masks[i]
        return self._members(mask)
