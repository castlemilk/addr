"""Build a reproducible, diverse review queue with one row per physical roof."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from shapely.geometry import Polygon

from .tiles import lonlat_to_tile_xy


def build_review_set(records, count=250, name="roof_training_250"):
    groups = defaultdict(list)
    # Units sharing a roof cannot count as independent training examples.
    for record in records:
        if not 30 <= record.get("area_m2", 0) <= 1000 or record.get("distance_meters", 0) > 20:
            continue
        center = record["centroid"]
        groups[(round(center["lon"], 6), round(center["lat"], 6))].append(record)
    strata = defaultdict(list)
    for duplicates in groups.values():
        record = min(duplicates, key=lambda r: (r.get("boundary_source") != "crm", r["address_id"]))
        polygon = Polygon(record["ring_meters"])
        if not polygon.is_valid or polygon.area <= 0:
            continue
        key = (record["roof_type"], "concave" if polygon.area / polygon.convex_hull.area < .9 else "compact",
               "small" if record["area_m2"] < 150 else "large" if record["area_m2"] > 350 else "medium")
        strata[key].append((record, [r["address_id"] for r in duplicates]))
    for items in strata.values():
        items.sort(key=lambda pair: (pair[0].get("boundary_source") != "crm", hashlib.sha256(f"roof-review-v1:{pair[0]['address_id']}".encode()).hexdigest()))
    chosen = []
    while len(chosen) < count and any(strata.values()):
        for key in sorted(strata):
            if strata[key] and len(chosen) < count:
                chosen.append(strata[key].pop(0))
    if len(chosen) < count:
        raise ValueError(f"Only {len(chosen)} distinct eligible roofs are available")
    entries = []
    for record, aliases in chosen:
        center = record["centroid"]
        tile_x, tile_y = map(int, lonlat_to_tile_xy(center["lon"], center["lat"], 17))
        group = f"z17:{tile_x}:{tile_y}"
        bucket = int(hashlib.sha256(group.encode()).hexdigest()[:8], 16) % 10
        split = "test" if bucket == 0 else "validation" if bucket == 1 else "train"
        entries.append({"address_id": record["address_id"], "full_address": record["full_address"],
                        "category": record["kind"], "tags": [record["roof_type"], split],
                        "shared_address_ids": aliases, "spatial_group": group, "split": split,
                        "annotation_status": "model-suggestion", "training_eligible": False})
    entries.sort(key=lambda e: (e["address_id"] != 29790, e["split"], e["address_id"]))
    return {"name": name, "title": f"Roof training review · {count} roofs",
            "description": "Diverse physical roofs with spatial train/validation/test groups. Model suggestions require review; estimated heights are not survey labels.",
            "selection_version": 1, "entries": entries}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, default=Path("public/address-roofs.json"))
    parser.add_argument("--count", type=int, default=250)
    parser.add_argument("--name", default="roof_training_250")
    args = parser.parse_args()
    cohort = build_review_set(json.loads(args.records.read_text()), args.count, args.name)
    out = Path("data/cohorts") / f"{args.name}.json"
    out.write_text(json.dumps(cohort, indent=2) + "\n")
    print(f"Prepared {len(cohort['entries'])} unique roofs: {out}")


if __name__ == "__main__":
    main()
