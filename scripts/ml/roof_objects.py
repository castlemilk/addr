"""Detect reviewable rooftop objects with the existing SAM3 model, once per tile."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from shapely.geometry import Polygon

from .ortho import OrthoCompositor
from .rooflines import Frame
from .sam3 import Sam3TextSegmenter
from .segmentation import mask_to_polygons
from .tiles import lonlat_to_tile_xy


def assign_objects(proposals, footprint, pixel_to_meter):
    objects = []
    for kind, score, polygon in proposals:
        local = Polygon([pixel_to_meter(x, y) for x, y in polygon.exterior.coords])
        if not local.is_valid or local.area < .35 or local.area > 180:
            continue
        # A neighbour's panels must not become this roof's training labels.
        overlap = local.intersection(footprint).area / local.area
        if overlap < .8:
            continue
        clipped = local.intersection(footprint)
        if clipped.geom_type != "Polygon" or clipped.interiors:
            continue
        ring = [[round(x, 3), round(y, 3)] for x, y in clipped.exterior.coords[:-1]]
        if any(Polygon(o["ring_meters"]).intersection(clipped).area / max(.01, clipped.area) > .7 for o in objects):
            continue
        objects.append({"id": hashlib.sha256(f"{kind}:{ring}".encode()).hexdigest()[:16],
                        "kind": kind, "ring_meters": ring, "score": round(score, 4),
                        "status": "candidate", "source": "sam3-text-onnx"})
    return objects


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", default="roof_training_250")
    parser.add_argument("--records", type=Path, default=Path("public/address-roofs.json"))
    parser.add_argument("--models", type=Path, default=Path("scripts/ml/models/sam3"))
    args = parser.parse_args()
    rows = {r["address_id"]: r for r in json.loads(args.records.read_text())}
    cohort = json.loads((Path("data/cohorts") / f"{args.cohort}.json").read_text())
    out = Path("data/reviews/cohorts") / args.cohort / "objects.json"
    results = json.loads(out.read_text()) if out.exists() else {}
    groups = defaultdict(list)
    for entry in cohort["entries"]:
        record = rows[entry["address_id"]]
        if results.get(str(record["address_id"]), {}).get("status") == "complete":
            continue
        groups[tuple(map(int, lonlat_to_tile_xy(record["centroid"]["lon"], record["centroid"]["lat"], 18)))].append(record)
    model = Sam3TextSegmenter(args.models)
    compositor = OrthoCompositor(zoom=18)
    for index, ((x, y), records) in enumerate(groups.items(), 1):
        try:
            window, _ = compositor.window(x, y)
            features = model.encode_image(window.image)
            proposals = []
            for kind, prompt in (("solar_panel", "solar panel"), ("skylight", "skylight")):
                for proposal in model.proposals(features, prompt, window.image.size, threshold=.45):
                    for polygon in mask_to_polygons(proposal.mask, min_area_px=6, simplify_px=.7):
                        proposals.append((kind, proposal.score, polygon))
            for record in records:
                frame = Frame(record, window, 18, x, y)
                objects = assign_objects(proposals, Polygon(record["ring_meters"]), frame.pixel_to_metre)
                results[str(record["address_id"])] = {"status": "complete", "objects": objects,
                    "detector": "sam3-text-onnx", "prompts": ["solar panel", "skylight"],
                    "image_sha256": hashlib.sha256(window.image.tobytes()).hexdigest(), "tile": [18, x, y]}
            print(f"[{index}/{len(groups)}] {len(records)} roofs, {sum(len(results[str(r['address_id'])]['objects']) for r in records)} candidates", flush=True)
        except Exception as error:  # noqa: BLE001
            for record in records:
                results[str(record["address_id"])] = {"status": "failed", "error": str(error), "objects": []}
            print(f"[{index}/{len(groups)}] failed: {error}", flush=True)
        temporary = out.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(results, indent=2))
        temporary.replace(out)


if __name__ == "__main__":
    main()
