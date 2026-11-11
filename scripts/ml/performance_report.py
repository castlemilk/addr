"""Unified performance report for the address system.

Aggregates coverage, per-address quality grades, isolation/structural conflict
metrics, and renders a contact sheet of the worst addresses so a reviewer (human
or agent) can see the state of the system at a glance.

Usage:
    python -m scripts.ml.performance_report \
        --tiles data/australia_tiles.mbtiles \
        --records public/address-roofs.json \
        --out data/reviews/performance.json \
        --sheet data/reviews/performance-sheet.png --sheet-count 9
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from PIL import Image, ImageDraw

from .address_review import build_context, load_records, render_site_image
from .addresses import load_addresses
from .isolation import analyze
from .tiles import read_mbtiles_metadata

SHEET_TILE = 420
SHEET_COLUMNS = 3


def build_report(
    tiles_path: Path,
    records_path: Path,
    db_path: Path | None,
    bbox: tuple[float, float, float, float] | None,
) -> dict:
    records = load_records(records_path)
    grades = collections.Counter(record.get("quality_grade", "A") for record in records)
    flags = collections.Counter(
        flag for record in records for flag in record.get("quality_flags", [])
    )
    isolation = collections.Counter(record.get("isolation", "clean") for record in records)

    addresses_total = None
    if db_path is not None and db_path.exists():
        addresses = load_addresses(db_path)
        if bbox is not None:
            addresses_total = sum(
                1
                for address in addresses
                if bbox[0] <= address["lon"] <= bbox[2] and bbox[1] <= address["lat"] <= bbox[3]
            )
        else:
            addresses_total = len(addresses)

    structural = analyze(tiles_path, records_path)

    tile_stats = {}
    try:
        metadata = read_mbtiles_metadata(tiles_path)
        payload = json.loads(metadata.get("quality_json") or "{}")
        tiles = payload.get("tiles", [])
        tile_stats = {
            "count": len(tiles),
            "totals": payload.get("totals", {}),
            "sparse": [
                {"z": tile["z"], "x": tile["x"], "y": tile["y"], "buildings": tile["buildings"]}
                for tile in tiles
                if tile.get("buildings", 0) < 3
            ][:20],
        }
    except Exception:  # noqa: BLE001
        tile_stats = {}
    worst = sorted(
        records,
        key=lambda record: (record.get("quality_score", 100), -record["area_m2"]),
    )[:50]
    worst_summary = [
        {
            "address_id": record["address_id"],
            "full_address": record["full_address"],
            "quality_score": record.get("quality_score", 100),
            "quality_grade": record.get("quality_grade", "A"),
            "quality_flags": record.get("quality_flags", []),
            "isolation": record.get("isolation", "clean"),
            "canopy_over_roof_share": record.get("canopy_over_roof_share", 0.0),
            "area_m2": record["area_m2"],
            "kind": record["kind"],
            "boundary_source": record.get("boundary_source", "extracted"),
        }
        for record in worst
    ]

    return {
        "records": len(records),
        "addresses_total": addresses_total,
        "coverage": round(len(records) / addresses_total, 3) if addresses_total else None,
        "kinds": dict(collections.Counter(record["kind"] for record in records)),
        "quality_grades": dict(grades),
        "quality_flags": dict(flags),
        "isolation_grades": dict(isolation),
        "average_quality": round(
            sum(record.get("quality_score", 100) for record in records) / max(1, len(records)), 1
        ),
        "structural": {
            "buildings": structural["buildings"],
            "trees": structural["trees_analyzed"],
            "trees_touching_buildings": structural["trees_touching_buildings"],
            "trees_mostly_inside_buildings": structural["trees_mostly_inside_buildings"],
            "tree_tree_overlap_pairs": structural["tree_tree_overlap_pairs"],
            "records_with_canopy_over_roof": structural["records_with_canopy_over_roof"],
        },
        "worst": worst_summary,
        "tiles": tile_stats,
    }


def render_contact_sheet(
    records: list[dict], out_path: Path, count: int, crm_only: bool = False
) -> int:
    if crm_only:
        records = [record for record in records if record.get("boundary_source") == "crm"]
    targets = sorted(
        records,
        key=lambda record: (record.get("quality_score", 100), -record["area_m2"]),
    )[: max(1, count)]
    columns = SHEET_COLUMNS
    rows = (len(targets) + columns - 1) // columns
    sheet = Image.new("RGB", (SHEET_TILE * columns, SHEET_TILE * rows + 24 * rows), (15, 23, 42))
    draw = ImageDraw.Draw(sheet)
    rendered = 0
    for index, record in enumerate(targets):
        context = build_context(address_id=record["address_id"])
        if context is None:
            continue
        path = render_site_image(
            context, Path("data/reviews") / str(record["address_id"]) / "site.png"
        )
        try:
            image = Image.open(path).convert("RGB")
        except OSError:
            continue
        image = image.resize((SHEET_TILE, SHEET_TILE), Image.Resampling.LANCZOS)
        column = index % columns
        row = index // columns
        x = column * SHEET_TILE
        y = row * (SHEET_TILE + 24)
        sheet.paste(image, (x, y))
        label = (
            f"{record['address_id']}  score {record.get('quality_score', 100)} "
            f"({record.get('quality_grade', 'A')})  {','.join(record.get('quality_flags', []))[:46]}"
        )
        draw.rectangle([x, y, x + SHEET_TILE, y + 20], fill=(15, 23, 42))
        draw.text((x + 4, y + 4), label, fill=(226, 232, 240))
        rendered += 1
    sheet.save(out_path)
    return rendered


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tiles", type=Path, required=True)
    parser.add_argument("--records", type=Path, default=Path("public/address-roofs.json"))
    parser.add_argument("--db", type=Path, default=Path("data/australia_geo.db"))
    parser.add_argument("--out", type=Path, default=Path("data/reviews/performance.json"))
    parser.add_argument("--sheet", type=Path, default=None)
    parser.add_argument("--sheet-count", type=int, default=9)
    parser.add_argument("--crm-only", action="store_true", help="sheet only CRM-corrected addresses")
    parser.add_argument(
        "--bbox",
        type=str,
        default="145.308,-37.797,145.320,-37.786",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    parts = [float(part) for part in args.bbox.split(",")]
    bbox = (parts[0], parts[1], parts[2], parts[3])
    report = build_report(args.tiles, args.records, args.db, bbox)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2))

    rendered = 0
    if args.sheet:
        rendered = render_contact_sheet(
            load_records(args.records), args.sheet, args.sheet_count, crm_only=args.crm_only
        )

    grades = report["quality_grades"]
    tiles = report.get("tiles", {})
    sparse = len(tiles.get("sparse", [])) if tiles else 0
    print(
        f"records={report['records']}/{report['addresses_total']} coverage={report['coverage']} "
        f"avg_quality={report['average_quality']} grades={grades} "
        f"isolation={report['isolation_grades']} tiles={tiles.get('count', 0)} "
        f"sparse_tiles={sparse} sheet_cells={rendered}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
