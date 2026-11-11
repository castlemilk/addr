"""Versioned roof/image/graph/object training export with target-specific gates."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path

import numpy as np

from .annotation_check import check_annotation
from .review_feedback import boundary_signature

OBJECT_CLASSES = ("solar_panel", "skylight", "chimney")


def model_signature(parts):
    rows = []
    for part in parts:
        ring = part.get("ring_meters", part.get("ring", []))
        z = part.get("ring_z", part.get("z", []))
        rows.append(";".join(f"{x:.5f},{y:.5f},{z[i]:.5f}" for i, (x, y) in enumerate(ring)))
    return hashlib.sha256("|".join(rows).encode()).hexdigest()


def export_dataset(cohort_name, *, root=Path(".")):
    cohort = json.loads((root / f"data/cohorts/{cohort_name}.json").read_text())
    report_dir = root / f"data/reviews/cohorts/{cohort_name}"
    report = json.loads((report_dir / "report.json").read_text())
    predictions = {r["address_id"]: r for r in report["results"]}
    records = {r["address_id"]: r for r in json.loads((root / "public/address-roofs.json").read_text())}
    feedback_path = root / "data/reviews/feedback.json"
    feedback = json.loads(feedback_path.read_text()) if feedback_path.exists() else {}
    object_path = report_dir / "objects.json"
    objects = json.loads(object_path.read_text()) if object_path.exists() else {}
    rows, counts = [], Counter()
    coco = {"images": [], "annotations": [], "categories": [{"id": i + 1, "name": k} for i, k in enumerate(("solar_panel", "skylight", "chimney"))]}
    class_coco = {kind: {"images": [], "annotations": [], "categories": [{"id": 1, "name": kind}]} for kind in OBJECT_CLASSES}
    for entry in cohort["entries"]:
        address_id = entry["address_id"]
        record, prediction = records[address_id], predictions.get(address_id)
        if prediction is None:
            counts["missing_predictions"] += 1
            continue
        image_path = report_dir / str(address_id) / "site-clean.png"
        image_hash = hashlib.sha256(image_path.read_bytes()).hexdigest() if image_path.exists() else None
        image_blob = root / "data/training/images" / f"{image_hash}.png" if image_hash else image_path
        if image_hash and not image_blob.exists():
            image_blob.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(image_path, image_blob)
        review = feedback.get(f"{cohort_name}:{address_id}", {})
        signature = model_signature(prediction["parts"])
        current_boundary = review.get("boundary_signature") == boundary_signature(record)
        current_model = review.get("model_signature") == signature
        approved = review.get("status") == "approved" and current_boundary and bool(image_hash)
        annotation = review.get("annotation")
        reviewed_classes = review.get("object_reviewed_classes", list(OBJECT_CLASSES) if review.get("objects_verified") else [])
        shape_valid = bool(annotation and current_model and check_annotation(annotation, record["ring_meters"], record["eave_height"])["passed"])
        eligible = {"boundary": current_boundary and bool(image_hash) and review.get("status") in ("approved", "needs_adjustment")
                    and review.get("boundary_verified") is True and not {"boundary", "wrong_building"}.intersection(review.get("issues", [])),
                    "roof_shape": approved and shape_valid and review.get("apex_verified") is True,
                    "objects": current_boundary and current_model and bool(image_hash) and review.get("status") != "rejected" and review.get("objects_verified") is True
                    and bool(reviewed_classes) and all(o["status"] != "candidate" for o in review.get("objects", []) if o["kind"] in reviewed_classes)}
        # An explicit roof-family instruction is useful independently of an
        # unverified apex position or absolute height.
        family_reviewed = annotation and current_boundary and current_model and review.get("roof_form_verified") is True and review.get("status") != "rejected"
        family = annotation.get("roof_type") if eligible["roof_shape"] or family_reviewed else record.get("roof_type") if record.get("roof_form_source") == "reviewer" else None
        for target, ready in eligible.items():
            counts[f"{target}_eligible"] += int(ready)
        counts["family_labelled_roofs"] += int(family in ("hipped", "gabled"))
        counts[f"split_{entry['split']}"] += 1
        xy = np.asarray(prediction["footprint_coords"])
        pixels = np.asarray(prediction["ring_pixels"])
        affine = np.linalg.lstsq(np.c_[xy, np.ones(len(xy))], pixels, rcond=None)[0]
        row = {"schema_version": 1, "address_id": address_id, "spatial_group": entry["spatial_group"], "split": entry["split"],
               "centroid": record["centroid"], "boundary_signature": boundary_signature(record), "model_signature": signature,
               "image": {"path": str(image_blob), "source_path": str(image_path), "sha256": image_hash, "providers": ["google", "esri"], "local_to_pixel_affine": affine.tolist()},
               "prediction": {"pipeline_version": prediction["pipeline_version"], "parts": prediction["parts"], "lines": prediction["rooflines"],
                              "roof_form_selection": prediction.get("roof_form_selection"), "objects": objects.get(str(address_id), {}).get("objects", [])},
               "reference": {"boundary": record["ring_meters"] if eligible["boundary"] else None,
                             "roof_graph": annotation if eligible["roof_shape"] else None,
                             "roof_family": family, "roof_family_source": "reviewed-graph" if eligible["roof_shape"] else "reviewed-family" if family_reviewed else "explicit-reviewer-correction" if family else None,
                             "objects": review.get("objects", []) if eligible["objects"] else None,
                             "object_reviewed_classes": reviewed_classes if eligible["objects"] else []},
               "training_eligible": eligible, "height_supervision": "relative-estimate-only", "review": review}
        rows.append(row)
        if eligible["objects"]:
            image_entry = {"id": address_id, "file_name": str(image_blob), "width": 512, "height": 512, "split": entry["split"]}
            all_classes = set(reviewed_classes) == set(OBJECT_CLASSES)
            if all_classes:
                coco["images"].append(image_entry)
            for kind in reviewed_classes:
                class_coco[kind]["images"].append(image_entry)
            for obj in review.get("objects", []):
                if obj["status"] != "confirmed" or obj["kind"] not in reviewed_classes:
                    continue
                polygon = np.c_[np.asarray(obj["ring_meters"]), np.ones(len(obj["ring_meters"]))] @ affine
                lo, hi = polygon.min(axis=0), polygon.max(axis=0)
                from shapely.geometry import Polygon
                annotation_entry = {"id": len(coco["annotations"]) + 1, "image_id": address_id,
                    "category_id": ["solar_panel", "skylight", "chimney"].index(obj["kind"]) + 1,
                    "segmentation": [polygon.ravel().tolist()], "bbox": [*lo.tolist(), *(hi - lo).tolist()],
                    "area": Polygon(polygon).area, "iscrowd": 0}
                if all_classes:
                    coco["annotations"].append(annotation_entry)
                target = class_coco[obj["kind"]]
                target["annotations"].append({**annotation_entry, "id": len(target["annotations"]) + 1, "category_id": 1})
    encoded = "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n"
    version = hashlib.sha256(encoded.encode()).hexdigest()[:16]
    out = root / f"data/training/{cohort_name}/{version}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "dataset.jsonl").write_text(encoded)
    (out / "objects-coco.json").write_text(json.dumps(coco, indent=2))
    for kind, labels in class_coco.items():
        (out / f"{kind}-coco.json").write_text(json.dumps(labels, indent=2))
    manifest = {"schema_version": 1, "version": version, "cohort": cohort_name, "roofs": len(rows), **dict(counts),
                "dataset": str(out / "dataset.jsonl"), "objects_coco": str(out / "objects-coco.json"),
                "object_class_exports": {kind: {"file": str(out / f"{kind}-coco.json"), "images": len(labels["images"]), "instances": len(labels["annotations"])} for kind, labels in class_coco.items()},
                "status": "review-required" if not counts["roof_shape_eligible"] else "reviewed-labels-available",
                "note": "Predictions are not labels. Reviewed negatives stay explicit. Absolute heights are not supervised."}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (out.parent / "latest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", default="roof_training_250")
    args = parser.parse_args()
    print(json.dumps(export_dataset(args.cohort), indent=2))


if __name__ == "__main__":
    main()
