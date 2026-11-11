#!/usr/bin/env python3
"""Reproduce the canonical report using a restored snapshot's own reader code."""

import argparse
import hashlib
import importlib
import json
import sys
from pathlib import Path


def canonical(value):
    return json.dumps(
        {key: entry for key, entry in value.items() if key != "fetched_at"},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("restored_root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.restored_root.resolve(strict=True)
    expected = json.loads((root / "data/fieldwork-derived/mooroolbark/processing.json").read_bytes())
    sys.path.insert(0, str(root))
    reader = importlib.import_module("scripts.ml.scene_processing")
    if not Path(reader.__file__).resolve().is_relative_to(root):
        raise RuntimeError("The reader must come from the restored snapshot")
    actual = reader.inventory(root)
    actual_bytes, expected_bytes = canonical(actual), canonical(expected)
    if actual_bytes != expected_bytes:
        raise RuntimeError("Restored inventory differs from the frozen canonical report")
    result = {
        "canonicalInventoryMatches": True,
        "method": "Restored reader code and restored data; exact JSON excluding fetched_at",
        "canonicalSha256": hashlib.sha256(actual_bytes).hexdigest(),
        "reader": str(Path(reader.__file__).relative_to(root)),
        "roofs": len(actual["features"]), "bands": actual["counts"],
        "scanObjects": actual["objects"], "tiles": len(actual["queue"]),
        "addressCounts": actual.get("address_counts", {}),
    }
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
