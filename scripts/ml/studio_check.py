"""3D-mode eyes: validate the Deep 3D Studio mesh for an address.

Two independent checks feed one verdict:

1. Shape metrics from the record itself — solidity (area / convex-hull
   area), reflex (concave) vertex count, partition coverage, plane count.
   An L-house reads concave (solidity < ~0.95 and/or reflex vertices); a
   convex heptagon with solidity 1.00 is a bar, whatever the address is
   called. Optional ``expected`` pins that belief: ``concave`` requires a
   notch, ``convex`` requires none.
2. Rendered-canvas eyes — the studio is screenshotted headlessly, the app's
   own ``[Deep3D]`` console line is captured (wall/roof tri counts), and the
   canvas pixels are analysed: mesh fill fraction in the central frame
   (catches the silent empty-mesh failure) plus edge density (catches a
   degenerate flat render).

Usage:
    python -m scripts.ml.studio_check --address-id 29790 --expect concave
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
from collections.abc import Sequence
from pathlib import Path

from PIL import Image, ImageFilter
from shapely.geometry import Polygon
from shapely.ops import orient

from .address_review import DEFAULT_RECORDS, build_context
from .addresses import METERS_PER_DEGREE

REPO_ROOT = Path(__file__).resolve().parents[2]
STUDIO_CHECK_MJS = REPO_ROOT / "scripts" / "studio-check.mjs"

#: Central-frame mesh fill below this means (almost) nothing rendered.
MIN_MESH_FILL = 0.10

# NOTE: edge density is reported for info only, not veredicted: the shot is
# clipped to the canvas box but UI chrome (panels/text) overlaps it, so text
# edges keep the density above any useful threshold. The deterministic
# empty-mesh signal is the wall/roof tri counts from the app's console line.


def ring_polygon_lonlat(record: dict) -> Polygon | None:
    centroid = record.get("centroid") or {}
    ring = record.get("ring_meters") or []
    if not centroid or len(ring) < 3:
        return None
    cos_lat = math.cos(math.radians(centroid["lat"]))
    polygon = Polygon(
        [
            (
                centroid["lon"] + x / (METERS_PER_DEGREE * cos_lat),
                centroid["lat"] + y / METERS_PER_DEGREE,
            )
            for x, y in ring
        ]
    )
    if polygon.is_empty or not polygon.is_valid:
        return None
    return polygon


# Interior angle at/above this marks a real notch rather than digitizer
# wobble (calibration: wobbles sit ~209°, true L-notches 220-270°).
NOTCH_ANGLE_DEG = 225.0

#: Solidity below this reads concave regardless of vertex angles.
SOLIDITY_CONCAVE_BELOW = 0.95


def shape_metrics(record: dict) -> dict:
    """Concavity/shape facts about the record ring (metre space)."""
    polygon = ring_polygon_lonlat(record)
    centroid = record.get("centroid") or {"lat": 0.0}
    cos_lat = math.cos(math.radians(centroid["lat"]))
    if polygon is None:
        return {"ok": False, "reason": "no valid ring"}
    ring = list(orient(polygon, sign=1.0).exterior.coords)[:-1]
    reflex_angles: list[float] = []
    for i in range(len(ring)):
        x0, y0 = ring[i - 1]
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % len(ring)]
        incoming = (x1 - x0, y1 - y0)
        outgoing = (x2 - x1, y2 - y1)
        turn = incoming[0] * outgoing[1] - incoming[1] * outgoing[0]
        dot = incoming[0] * outgoing[0] + incoming[1] * outgoing[1]
        bend = math.degrees(math.atan2(abs(turn), dot))
        if turn < 0:  # right turn on a CCW ring = concave notch
            reflex_angles.append(round(180.0 + bend, 1))
    area_m2 = polygon.area * (METERS_PER_DEGREE**2) * cos_lat
    hull = polygon.convex_hull
    solidity = polygon.area / hull.area if hull.area > 0 else 0.0
    max_reflex = max(reflex_angles) if reflex_angles else 0.0
    concave = solidity < SOLIDITY_CONCAVE_BELOW or max_reflex >= NOTCH_ANGLE_DEG
    parts = (record.get("rooflines") or {}).get("parts", [])
    covered = sum(part.get("area_m2", 0.0) for part in parts)
    return {
        "ok": True,
        "vertices": len(ring),
        "reflex_vertices": len(reflex_angles),
        "max_reflex_deg": max_reflex,
        "solidity": round(solidity, 3),
        "concave": concave,
        "area_m2": round(area_m2, 1),
        "parts": len(parts),
        "coverage": round(covered / area_m2, 3) if area_m2 else 0.0,
    }


def analyse_canvas(path: Path) -> dict:
    """Mesh fill + edge density of a studio canvas screenshot."""
    image = Image.open(path).convert("RGB")
    width, height = image.size
    corners = []
    for box in ((0, 0, 12, 12), (width - 12, 0, width, 12), (0, height - 12, width, height),
                (width - 12, height - 12, width, height)):
        crop = image.crop(box)
        pixels = list(crop.getdata())
        corners.append([sum(channel) / len(pixels) for channel in zip(*pixels, strict=True)])
    background = [sum(values) / len(values) for values in zip(*corners, strict=True)]
    central = image.crop((width // 4, height // 4, 3 * width // 4, 3 * height // 4))
    pixels = list(central.getdata())
    fill = sum(
        1
        for red, green, blue in pixels
        if abs(red - background[0]) + abs(green - background[1]) + abs(blue - background[2]) > 45
    ) / max(1, len(pixels))
    edges = central.convert("L").filter(ImageFilter.FIND_EDGES)
    edge_pixels = list(edges.getdata())
    density = sum(1 for value in edge_pixels if value > 30) / max(1, len(edge_pixels))
    return {
        "mesh_fill": round(fill, 3),
        "edge_density": round(density, 4),
        "canvas_px": [width, height],
    }


def verdict(
    *,
    shape: dict,
    wall_tris: int,
    roof_tris: int,
    canvas: dict,
    page_errors: list,
    expected: str | None,
) -> tuple[str, list[str]]:
    findings: list[str] = []
    if shape.get("ok"):
        if expected == "concave" and not shape["concave"]:
            findings.append(
                f"expected an L (concave) footprint but the ring is convex "
                f"(solidity {shape['solidity']}, max notch "
                f"{shape['max_reflex_deg']}°) — wrong building or missing wing"
            )
        elif expected == "convex" and shape["concave"]:
            findings.append(
                f"expected a convex footprint but the ring has "
                f"{shape['reflex_vertices']} reflex vertices"
            )
        if shape["coverage"] < 0.95:
            findings.append(f"partition covers only {shape['coverage']:.0%} of the ring")
    if not wall_tris or not roof_tris:
        findings.append(
            f"studio built an empty mesh (wall {wall_tris} / roof {roof_tris} tris)"
        )
    if isinstance(canvas, dict) and "mesh_fill" in canvas:
        if canvas["mesh_fill"] < MIN_MESH_FILL:
            findings.append(f"canvas nearly empty (mesh fill {canvas['mesh_fill']:.0%})")
    if page_errors:
        findings.append(f"page errors: {page_errors[0]}")
    return ("REVIEW" if findings else "PASS"), findings


def check(
    *,
    query: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    address_id: int | None = None,
    expected: str | None = None,
    records_path: Path = DEFAULT_RECORDS,
    out_dir: Path | None = None,
) -> dict:
    context = build_context(
        query=query, lat=lat, lon=lon, address_id=address_id, records_path=records_path
    )
    if context is None:
        return {"error": "no matching address record"}
    record = context.record
    shape = shape_metrics(record)
    address_point = record.get("address_point") or record.get("centroid", {})
    target_dir = out_dir or (REPO_ROOT / "data" / "reviews" / str(record["address_id"]))
    target_dir.mkdir(parents=True, exist_ok=True)
    shot = target_dir / "studio-check.png"
    stats_path = target_dir / "studio-check.json"
    try:
        completed = subprocess.run(
            [
                "node",
                str(STUDIO_CHECK_MJS),
                "--lat",
                str(address_point["lat"]),
                "--lon",
                str(address_point["lon"]),
                "--out",
                str(shot),
                "--stats",
                str(stats_path),
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        stats = (
            json.loads(stats_path.read_text())
            if stats_path.exists()
            else {"error": (completed.stderr or completed.stdout)[-500:]}
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"error": f"studio capture failed: {error}", "shape": shape}
    canvas = analyse_canvas(shot) if shot.exists() else {"error": "no screenshot"}
    wall_tris = int(stats.get("wallTris") or 0)
    roof_tris = int(stats.get("roofTris") or 0)
    state, findings = verdict(
        shape=shape,
        wall_tris=wall_tris,
        roof_tris=roof_tris,
        canvas=canvas if isinstance(canvas, dict) else {},
        page_errors=list(stats.get("errors") or []),
        expected=expected,
    )
    return {
        "address_id": record["address_id"],
        "full_address": record["full_address"],
        "shape": shape,
        "mesh": {"wall_tris": wall_tris, "roof_tris": roof_tris},
        "canvas": canvas,
        "shot": str(shot),
        "verdict": state,
        "findings": findings,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default=None)
    parser.add_argument("--lat", type=float, default=None)
    parser.add_argument("--lon", type=float, default=None)
    parser.add_argument("--address-id", type=int, default=None)
    parser.add_argument("--expect", choices=["concave", "convex"], default=None)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS)
    parser.add_argument("--out-dir", type=Path, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(
        json.dumps(
            check(
                query=args.address,
                lat=args.lat,
                lon=args.lon,
                address_id=args.address_id,
                expected=args.expect,
                records_path=args.records,
                out_dir=args.out_dir,
            ),
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
