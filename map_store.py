import gzip
import hashlib
import json
import marshal
from pathlib import Path


MAPS = Path(__file__).resolve().parent / "maps"
PACKED = MAPS.parent / "packed_maps"


def read_json_map(path):
    with gzip.open(path, "rb") as stream:
        return json.load(stream)


def _packed_path(path):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return PACKED / f"{path.stem}-{digest}.marshal"


def read_map(path):
    if path.parent == MAPS and PACKED.is_dir():
        try:
            return marshal.loads(_packed_path(path).read_bytes(), allow_code=False)
        except (OSError, EOFError, ValueError, TypeError):
            pass
    return read_json_map(path)


if __name__ == "__main__":
    PACKED.mkdir(exist_ok=True)
    for path in MAPS.glob("*.json.gz"):
        _packed_path(path).write_bytes(marshal.dumps(read_json_map(path)))
