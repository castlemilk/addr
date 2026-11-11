"""Batch evaluation and contact sheet generator for roof review cohorts.

Loads a cohort specification (e.g. data/cohorts/suburban_benchmark.json),
extracts deep multi-layer rooflines, faces, and 3D meshes for each address,
and emits:
  1. data/reviews/cohorts/<cohort>/contact-sheet.png (visual review grid)
  2. data/reviews/cohorts/<cohort>/report.json (metrics, face/cut counts, quality scores)
  3. Per-address diagnostic overlays under data/reviews/cohorts/<cohort>/<address_id>/

Usage:
    python -m scripts.ml.roof_cohort --cohort suburban_benchmark
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from .address_review import DEFAULT_RECORDS, find_record
from .review_feedback import rejected_cuts_for
from .roof_reflection import reflect_on_roof_analysis
from .roof_signals import detect_roof_mask, evaluate_roof_accuracy
from .rooflines import RooflineExtractor, render_mesh

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_COHORTS_DIR = REPO_ROOT / "data" / "cohorts"
DEFAULT_REVIEWS_DIR = REPO_ROOT / "data" / "reviews"
DEFAULT_FEEDBACK_PATH = REPO_ROOT / "data" / "reviews" / "feedback.json"


def load_feedback(feedback_path: Path = DEFAULT_FEEDBACK_PATH, cohort: str = "suburban_benchmark") -> dict[int, dict]:
    if not feedback_path.exists():
        return {}
    try:
        data = json.loads(feedback_path.read_text())
        result = {int(k): v for k, v in data.items() if k.isdigit()} if cohort == "suburban_benchmark" else {}
        prefix = f"{cohort}:"
        result.update({int(k[len(prefix):]): v for k, v in data.items()
                       if k.startswith(prefix) and k[len(prefix):].isdigit()})
        return result
    except Exception:
        return {}


def load_cohort(cohort_arg: str, cohorts_dir: Path = DEFAULT_COHORTS_DIR) -> dict:
    path = Path(cohort_arg)
    if not path.is_file():
        path = cohorts_dir / f"{cohort_arg}.json"
    if not path.exists():
        raise SystemExit(f"Cohort definition not found at {path}")
    return json.loads(path.read_text())


def evaluate_cohort(
    cohort_data: dict,
    *,
    records_path: Path = DEFAULT_RECORDS,
    out_dir: Path | None = None,
    zoom: int = 18,
    feedback_path: Path = DEFAULT_FEEDBACK_PATH,
) -> dict:
    cohort_name = cohort_data.get("name", "cohort")
    if out_dir is None:
        out_dir = DEFAULT_REVIEWS_DIR / "cohorts" / cohort_name
    out_dir.mkdir(parents=True, exist_ok=True)

    feedback_store = load_feedback(feedback_path, cohort_name)
    entries = cohort_data.get("entries", [])
    if not entries:
        raise SystemExit("Cohort contains no entries.")

    extractor = RooflineExtractor(zoom=zoom)
    results: list[dict[str, Any]] = []
    tile_crops: list[tuple[dict, Image.Image, Image.Image]] = []

    print(f"Evaluating cohort '{cohort_name}' ({len(entries)} roofs)...")

    started_total = time.perf_counter()

    for idx, entry in enumerate(entries, 1):
        address_id = entry.get("address_id")
        record = find_record(address_id=address_id, records_path=records_path)
        if record is None and entry.get("full_address"):
            record = find_record(query=entry["full_address"], records_path=records_path)
        if record is None:
            print(f"[{idx}/{len(entries)}] Address {address_id} not found in records, skipping.")
            continue

        item_feedback = feedback_store.get(int(record["address_id"]), {})
        rejected_lines = rejected_cuts_for(record, item_feedback)

        addr_dir = out_dir / str(record["address_id"])
        addr_dir.mkdir(parents=True, exist_ok=True)
        site_debug_path = addr_dir / "site-overlay.png"
        mesh_path = addr_dir / "lod2-mesh.png"

        t0 = time.perf_counter()
        try:
            res = extractor.extract(
                record,
                debug_path=site_debug_path,
                rejected_lines=rejected_lines,
            )
            render_mesh(res, mesh_path, size=512)
            elapsed = time.perf_counter() - t0
        except Exception as err:
            print(f"[{idx}/{len(entries)}] Failed extracting {record['address_id']}: {err}", file=sys.stderr)
            continue

        # Summarize line kinds
        line_kinds: dict[str, int] = {}
        for line in res.get("rooflines", []):
            k = line.get("kind", "ridge")
            line_kinds[k] = line_kinds.get(k, 0) + 1

        part_kinds: dict[str, int] = {}
        for part in res.get("parts", []):
            k = part.get("kind", "main_pitch")
            part_kinds[k] = part_kinds.get(k, 0) + 1

        # Compute accuracy signals
        clean_path = addr_dir / "site-clean.png"
        signals_dict: dict[str, Any] = {}
        reflection_dict: dict[str, Any] = {}
        if clean_path.exists():
            import numpy as np
            clean_rgb = np.asarray(Image.open(clean_path).convert("RGB"))
            ring_px = res.get("ring_pixels", [])
            sig = evaluate_roof_accuracy(
                clean_rgb,
                ring_px,
                expected_topology=entry.get("expected_topology"),
                footprint_area_m2=res.get("footprint_area_m2"),
            )
            signals_dict = sig.to_dict()

            ring_m = res.get("footprint_coords") or [[round(x, 2), round(y, 2)] for x, y in record.get("ring_meters", [])]
            refl = reflect_on_roof_analysis(clean_rgb, ring_m, ring_px, footprint_area_m2=res.get("footprint_area_m2"))
            reflection_dict = refl.to_dict()

            # Save visual roof evidence mask (green = roof, red = false intrusion, amber = missed roof)
            center_seed = (256, 256)
            if len(ring_px) >= 3:
                pts_arr = np.array(ring_px, dtype=np.float32)
                center_seed = (int(round(float(pts_arr[:, 0].mean()))), int(round(float(pts_arr[:, 1].mean()))))
            mask_arr, _ = detect_roof_mask(clean_rgb, center_seed=center_seed)
            mask_rgb = np.zeros((512, 512, 3), dtype=np.uint8)
            mask_rgb[mask_arr] = [34, 197, 94]  # green for detected roof
            if ring_px:
                import cv2
                poly_mask = np.zeros((512, 512), dtype=bool)
                cv2.fillPoly(poly_mask.view(np.uint8), [np.array(ring_px, dtype=np.int32)], 1)
                intrusion = poly_mask & (~mask_arr)
                missing = mask_arr & (~poly_mask)
                mask_rgb[intrusion] = [239, 68, 68]  # red for false intrusion
                mask_rgb[missing] = [234, 179, 8]   # amber for missed roof
            Image.fromarray(mask_rgb).save(addr_dir / "roof-mask.png")

        entry_summary = {
            "address_id": record["address_id"],
            "pipeline_version": res.get("pipeline_version"),
            "geometry_source": res.get("geometry_source"),
            "height_source": res.get("height_source"),
            "structure_audit": res.get("structure_audit"),
            "image_structure_audit": res.get("image_structure_audit"),
            "prior_error": res.get("prior_error"),
            "eave_regularization": res.get("eave_regularization"),
            "roof_form_selection": res.get("roof_form_selection"),
            "centroid": res.get("centroid"),
            "footprint_coords": res.get("footprint_coords"),
            "full_address": record["full_address"],
            "category": entry.get("category", record.get("kind", "house")),
            "expected_topology": entry.get("expected_topology"),
            "tags": entry.get("tags", []),
            "footprint_area_m2": res["footprint_area_m2"],
            "face_count": res["faces"],
            "group_count": res["groups"],
            "roofline_count": len(res.get("rooflines", [])),
            "line_kinds": line_kinds,
            "part_kinds": part_kinds,
            "ring_pixels": res.get("ring_pixels", []),
            "parts": res.get("parts", []),
            "rooflines": res.get("rooflines", []),
            "signals": signals_dict,
            "reflection": reflection_dict,
            "elapsed_ms": round(elapsed * 1000, 1),
            "site_image": str(site_debug_path),
            "mesh_image": str(mesh_path),
            "feedback": item_feedback,
        }
        results.append(entry_summary)


        site_img = Image.open(site_debug_path).convert("RGB") if site_debug_path.exists() else Image.new("RGB", (512, 512), (30, 30, 30))
        mesh_img = Image.open(mesh_path).convert("RGB") if mesh_path.exists() else Image.new("RGB", (512, 512), (30, 30, 30))
        tile_crops.append((entry_summary, site_img, mesh_img))

        print(
            f"  [{idx:>2}/{len(entries)}] {record['address_id']:>5} | "
            f"{entry_summary['face_count']:>2} faces, {entry_summary['roofline_count']:>2} cuts | "
            f"{entry_summary['footprint_area_m2']:>5.1f}m² | {elapsed*1000:>4.0f}ms | "
            f"{record['full_address'][:38]}"
        )

    total_time = time.perf_counter() - started_total

    # Render contact sheet
    contact_sheet_path = out_dir / "contact-sheet.png"
    render_cohort_sheet(tile_crops, contact_sheet_path, title=f"Cohort: {cohort_name} ({len(results)} roofs)")

    report_payload = {
        "cohort": cohort_name,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_eval_time_sec": round(total_time, 2),
        "total_roofs": len(results),
        "mean_time_per_roof_ms": round((total_time / len(results) * 1000) if results else 0, 1),
        "results": results,
    }

    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report_payload, indent=2))
    print(f"\nCohort report saved to: {report_path}")
    print(f"Contact sheet saved to: {contact_sheet_path}")

    return report_payload


def render_cohort_sheet(
    entries: list[tuple[dict, Image.Image, Image.Image]],
    out_path: Path,
    title: str = "Roof Cohort Evaluation",
) -> None:
    if not entries:
        return

    card_w = 480
    card_h = 290
    cols = 2
    rows = math.ceil(len(entries) / cols)

    header_h = 60
    margin = 16
    total_w = cols * card_w + (cols + 1) * margin
    total_h = header_h + rows * card_h + (rows + 1) * margin

    canvas = Image.new("RGB", (total_w, total_h), (18, 20, 24))
    draw = ImageDraw.Draw(canvas)

    # Header title
    draw.rectangle([(0, 0), (total_w, header_h)], fill=(28, 32, 40))
    draw.text((20, 18), title, fill=(240, 240, 245))
    draw.text(
        (total_w - 320, 22),
        "Green=Ridge  Cyan=Hip  Amber=Step  Purple=Valley",
        fill=(160, 170, 185),
    )

    for i, (summary, site_img, mesh_img) in enumerate(entries):
        col = i % cols
        row = i // cols
        x = margin + col * (card_w + margin)
        y = header_h + margin + row * (card_h + margin)

        # Card container
        draw.rectangle([(x, y), (x + card_w, y + card_h)], fill=(26, 29, 36), outline=(45, 52, 64))

        # Title bar
        draw.rectangle([(x, y), (x + card_w, y + 36)], fill=(34, 38, 48))
        label = f"{summary['address_id']} — {summary['full_address'][:32]}"
        draw.text((x + 10, y + 10), label, fill=(255, 255, 255))
        badge = f"{summary['face_count']}f / {summary['roofline_count']}c | {summary['footprint_area_m2']:.0f}m²"
        draw.text((x + card_w - 140, y + 10), badge, fill=(56, 189, 248))

        # Side by side: 2D Site image (left) + 3D mesh (right)
        preview_sz = 210
        s_resized = site_img.resize((preview_sz, preview_sz), Image.Resampling.LANCZOS)
        m_resized = mesh_img.resize((preview_sz, preview_sz), Image.Resampling.LANCZOS)

        canvas.paste(s_resized, (x + 16, y + 44))
        canvas.paste(m_resized, (x + preview_sz + 36, y + 44))

        # Bottom legend/tags
        draw.text(
            (x + 16, y + preview_sz + 50),
            f"Category: {summary['category']} | {summary['elapsed_ms']:.0f}ms | Lines: {summary.get('line_kinds', {})}",
            fill=(140, 150, 165),
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", default="suburban_benchmark", help="Cohort name or path to JSON")
    parser.add_argument("--out", type=Path, default=None, help="Output directory")
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS, help="Path to address-roofs.json")
    parser.add_argument("--zoom", type=int, default=18)
    args = parser.parse_args()

    cohort_data = load_cohort(args.cohort)
    evaluate_cohort(cohort_data, records_path=args.records, out_dir=args.out, zoom=args.zoom)


if __name__ == "__main__":
    main()
