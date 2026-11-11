"""Per-address review tooling.

Resolves an address from the generated records / G-NAF, gathers CRM metadata,
boundary overrides and POIs, and renders:

  * a top-down site image with the extracted and CRM boundaries overlaid on
    satellite imagery (``site_image``)
  * a rendered 3D view of the extracted roof mesh via the Deep 3D Studio
    (``image_3d``)

Used by the MCP server (``scripts.ml.mcp_server``) and directly from the CLI:
``python -m scripts.ml.address_review metadata --address "27 CAMBRIDGE ROAD"``.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import mapbox_vector_tile
from PIL import Image, ImageDraw
from shapely.geometry import Polygon

from .addresses import METERS_PER_DEGREE
from .imagery import ImageryFetcher, ImageryWindow
from .tiles import lonlat_to_tile_local, tile_local_to_lonlat

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TILES = REPO_ROOT / "data" / "australia_tiles.mbtiles"
DEFAULT_DB = REPO_ROOT / "data" / "australia_geo.db"
DEFAULT_RECORDS = REPO_ROOT / "public" / "address-roofs.json"
DEFAULT_OUT = REPO_ROOT / "data" / "reviews"

EXTRACTED_COLOR = (230, 57, 70)  # extracted roof
TREE_COLOR = (16, 130, 94)  # extracted canopies
OVERRIDE_ROOF_COLOR = (249, 115, 22)  # CRM roof override
OVERRIDE_TREE_COLOR = (34, 197, 94)  # CRM tree override
OVERRIDE_OBJECT_COLOR = (56, 189, 248)  # CRM other object
PROPOSAL_BOX_COLOR = (232, 121, 249)  # user-drawn roof-box proposal
NEIGHBOUR_COLOR = (148, 163, 184)
ADDRESS_COLOR = (37, 99, 235)


@dataclass
class AddressContext:
    record: dict
    meta: dict | None = None
    features: list[dict] = field(default_factory=list)
    pois: list[dict] = field(default_factory=list)
    tile_buildings: list[tuple[Polygon, dict]] = field(default_factory=list)
    tile_trees: list[tuple[Polygon, dict]] = field(default_factory=list)
    # Nearby G-NAF pins (number/street/point) for attribution eyeballing:
    # whose pins fall inside/nearest the ring should match the record address.
    nearby_pins: list[dict] = field(default_factory=list)


def load_records(records_path: Path = DEFAULT_RECORDS) -> list[dict]:
    payload = json.loads(records_path.read_text())
    if isinstance(payload, dict):
        payload = payload.get("records", [])
    return list(payload)


def find_record(
    *,
    query: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    address_id: int | None = None,
    records_path: Path = DEFAULT_RECORDS,
) -> dict | None:
    records = load_records(records_path)
    if address_id is not None:
        for record in records:
            if int(record["address_id"]) == int(address_id):
                return record
        return None
    if query:
        needle = query.strip().upper()
        matches = [record for record in records if needle in record["full_address"].upper()]
        if not matches:
            return None
        matches.sort(key=lambda item: len(item["full_address"]))
        return matches[0]
    if lat is not None and lon is not None:
        best: tuple[float, dict] | None = None
        for record in records:
            point = record["address_point"]
            dx = (point["lon"] - lon) * METERS_PER_DEGREE * math.cos(math.radians(lat))
            dy = (point["lat"] - lat) * METERS_PER_DEGREE
            distance = math.hypot(dx, dy)
            if best is None or distance < best[0]:
                best = (distance, record)
        if best and best[0] <= 80:
            return best[1]
    return None


def load_crm(address_id: int, db_path: Path = DEFAULT_DB) -> tuple[dict | None, list[dict], list[dict]]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        meta_row = connection.execute(
            "SELECT * FROM crm_address_meta WHERE address_id = ?", (address_id,)
        ).fetchone()
        meta = dict(meta_row) if meta_row else None
        if meta and meta.get("tags"):
            meta["tags"] = json.loads(meta["tags"])
        feature_rows = connection.execute(
            "SELECT * FROM crm_features WHERE address_id = ? ORDER BY id", (address_id,)
        ).fetchall()
        features = []
        for row in feature_rows:
            feature = dict(row)
            if feature.get("properties"):
                feature["properties"] = json.loads(feature["properties"])
            features.append(feature)
        poi_rows = connection.execute(
            "SELECT * FROM crm_pois WHERE address_id = ? ORDER BY id", (address_id,)
        ).fetchall()
        pois = [dict(row) for row in poi_rows]
    except sqlite3.OperationalError:
        return None, [], []
    finally:
        connection.close()
    return meta, features, pois


def load_nearby_pins(
    lat: float, lon: float, db_path: Path = DEFAULT_DB, radius_m: float = 150.0
) -> list[dict]:
    """G-NAF address points near a location for attribution eyeballing."""
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT id, unit, number, street, full_address, lat, lon FROM addresses "
            "WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
            (
                lat - radius_m / METERS_PER_DEGREE,
                lat + radius_m / METERS_PER_DEGREE,
                lon - radius_m / (METERS_PER_DEGREE * math.cos(math.radians(lat))),
                lon + radius_m / (METERS_PER_DEGREE * math.cos(math.radians(lat))),
            ),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()
    pins = []
    for row in rows:
        unit = (row[1] or "").strip()
        label = row[2] or ""
        if unit:
            label = f"{unit.split()[-1]}/{label}"
        pins.append(
            {
                "address_id": row[0],
                "label": label,
                "full_address": row[4],
                "lat": row[5],
                "lon": row[6],
            }
        )
    pins.sort(
        key=lambda pin: math.hypot(
            (pin["lon"] - lon) * math.cos(math.radians(lat)), pin["lat"] - lat
        )
    )
    return pins[:12]


def tile_for(lat: float, lon: float, z: int) -> tuple[int, int]:
    n = 2**z
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def load_tile_layers(
    lat: float,
    lon: float,
    tiles_path: Path = DEFAULT_TILES,
    z: int = 16,
    radius_m: float = 140.0,
) -> tuple[list[tuple[Polygon, dict]], list[tuple[Polygon, dict]]]:
    x, y = tile_for(lat, lon, z)
    connection = sqlite3.connect(f"file:{tiles_path}?mode=ro", uri=True)
    row = connection.execute(
        "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
        (z, x, (1 << z) - 1 - y),
    ).fetchone()
    connection.close()
    if row is None:
        return [], []
    data = row[0]
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    layers = mapbox_vector_tile.decode(data, default_options={"y_coord_down": True})

    def collect(layer_name: str) -> list[tuple[Polygon, dict]]:
        layer = layers.get(layer_name)
        if not layer:
            return []
        collected: list[tuple[Polygon, dict]] = []
        for feature in layer["features"]:
            geometry = feature["geometry"]
            parts = (
                [geometry["coordinates"]]
                if geometry["type"] == "Polygon"
                else geometry["coordinates"]
            )
            for part in parts:
                world = [
                    [tile_local_to_lonlat(px, py, x, y, z, layer.get("extent", 4096)) for px, py in ring]
                    for ring in part
                ]
                try:
                    polygon = Polygon(world[0], world[1:])
                except (ValueError, IndexError):
                    continue
                if polygon.is_empty:
                    continue
                centroid = polygon.centroid
                dx = (centroid.x - lon) * METERS_PER_DEGREE * math.cos(math.radians(lat))
                dy = (centroid.y - lat) * METERS_PER_DEGREE
                if math.hypot(dx, dy) <= radius_m:
                    collected.append((polygon, feature["properties"]))
        return collected

    return collect("building"), collect("tree")


def build_context(
    *,
    query: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    address_id: int | None = None,
    tiles_path: Path = DEFAULT_TILES,
    db_path: Path = DEFAULT_DB,
    records_path: Path = DEFAULT_RECORDS,
) -> AddressContext | None:
    record = find_record(
        query=query, lat=lat, lon=lon, address_id=address_id, records_path=records_path
    )
    if record is None:
        return None
    point = record["centroid"]
    buildings, trees = load_tile_layers(point["lat"], point["lon"], tiles_path)
    meta, features, pois = load_crm(int(record["address_id"]), db_path)
    nearby_pins = load_nearby_pins(point["lat"], point["lon"], db_path)
    return AddressContext(
        record=record,
        meta=meta,
        features=features,
        pois=pois,
        tile_buildings=buildings,
        tile_trees=trees,
        nearby_pins=nearby_pins,
    )


def metadata_summary(context: AddressContext) -> dict:
    record = context.record
    return {
        "address_id": record["address_id"],
        "full_address": record["full_address"],
        "address_point": record["address_point"],
        "centroid": record["centroid"],
        "kind": record["kind"],
        "unit": record.get("unit"),
        "area_m2": round(record["area_m2"], 1),
        "width_m": record["width_meters"],
        "depth_m": record["depth_meters"],
        "roof_type": record["roof_type"],
        "eave_height_m": record["eave_height"],
        "ridge_height_m": record["ridge_height"],
        "ridge_angle_deg": record["ridge_angle"],
        "height_m": record["height"],
        "levels": record.get("levels"),
        "distance_address_to_roof_m": round(record["distance_meters"], 2),
        "quality_score": record.get("quality_score", 100),
        "quality_grade": record.get("quality_grade", "A"),
        "quality_flags": record.get("quality_flags", []),
        "isolation": record.get("isolation", "clean"),
        "canopy_over_roof_m2": record.get("canopy_over_roof_m2", 0.0),
        "canopy_over_roof_share": record.get("canopy_over_roof_share", 0.0),
        "overlapping_trees": record.get("overlapping_trees", 0),
        "boundary_source": record.get("boundary_source", "extracted"),
        "address_count": record["address_count"],
        "neighbours": record.get("neighbours", [])[:6],
        "shared_with": record.get("shared_with", []),
        "crm": {
            "meta": context.meta,
            "features": [
                {
                    "id": feature["id"],
                    "kind": feature["kind"],
                    "label": feature.get("label"),
                    "updated_at": feature["updated_at"],
                    "updated_by": feature["updated_by"],
                }
                for feature in context.features
            ],
            "pois": [
                {
                    "id": poi["id"],
                    "name": poi["name"],
                    "category": poi.get("category"),
                    "notes": poi.get("notes"),
                }
                for poi in context.pois
            ],
        },
        "tile_extracted": {
            "buildings_nearby": len(context.tile_buildings),
            "trees_nearby": len(context.tile_trees),
        },
    }


def _polygon_pixels(
    polygon: Polygon, window: ImageryWindow
) -> list[tuple[float, float]]:
    pixels = []
    for lon, lat in polygon.exterior.coords:
        lx, ly = window.local_to_pixel(*lonlat_to_tile_local(lon, lat, window.x, window.y, window.z))
        pixels.append((lx, ly))
    return pixels


def _ring_pixels(record: dict, window: ImageryWindow) -> list[tuple[float, float]]:
    centroid = record["centroid"]
    pixels = []
    for mx, my in record["ring_meters"]:
        lon = centroid["lon"] + mx / (METERS_PER_DEGREE * math.cos(math.radians(centroid["lat"])))
        lat = centroid["lat"] + my / METERS_PER_DEGREE
        lx, ly = window.local_to_pixel(*lonlat_to_tile_local(lon, lat, window.x, window.y, window.z))
        pixels.append((lx, ly))
    return pixels


def _choose_zoom(span_meters: float) -> int:
    if span_meters > 140:
        return 16
    if span_meters > 60:
        return 17
    return 18


def render_site_image(
    context: AddressContext,
    out_path: Path,
    max_px: int = 1024,
) -> Path:
    record = context.record
    centroid = record["centroid"]
    span = max(record["width_meters"], record["depth_meters"], 25.0)
    zoom = _choose_zoom(span)
    fetcher = ImageryFetcher(
        provider="google",
        cache_dir=str(REPO_ROOT / "data" / "imagery"),
        imagery_offset=1,
        out_px=576,
        margin_px=32,
    )
    tile_x, tile_y = tile_for(centroid["lat"], centroid["lon"], zoom)
    window = fetcher.window(zoom, tile_x, tile_y)

    image = window.image.copy()
    draw = ImageDraw.Draw(image, "RGBA")

    def draw_ring(pixels: list[tuple[float, float]], color, width: int, fill=None) -> None:
        if len(pixels) < 3:
            return
        if fill:
            draw.polygon(pixels, fill=fill)
        draw.line(pixels + [pixels[0]], fill=color, width=width, joint="curve")

    # Neighbouring extracted buildings (context)
    for polygon, _ in context.tile_buildings:
        pixels = _polygon_pixels(polygon, window)
        draw_ring(pixels, (*NEIGHBOUR_COLOR, 150), 2)

    # Extracted canopies (context)
    for polygon, _ in context.tile_trees:
        pixels = _polygon_pixels(polygon, window)
        draw_ring(pixels, (*TREE_COLOR, 170), 2)

    # Extracted roof boundary for this address
    roof_pixels = _ring_pixels(record, window)
    draw_ring(roof_pixels, EXTRACTED_COLOR, 4, fill=(*EXTRACTED_COLOR, 45))

    # CRM overrides
    for feature in context.features:
        try:
            geometry = json.loads(feature["geometry_json"])
        except (TypeError, ValueError):
            continue
        ring = geometry["coordinates"][0] if geometry["type"] == "Polygon" else None
        if not ring:
            continue
        pixels = [
            window.local_to_pixel(*lonlat_to_tile_local(lon, lat, window.x, window.y, window.z))
            for lon, lat in ring
        ]
        if feature["kind"] == "roof":
            draw_ring(pixels, (*OVERRIDE_ROOF_COLOR, 255), 4, fill=(*OVERRIDE_ROOF_COLOR, 60))
        elif feature["kind"] == "tree":
            draw_ring(pixels, (*OVERRIDE_TREE_COLOR, 255), 3, fill=(*OVERRIDE_TREE_COLOR, 60))
        elif (feature.get("label") or "").startswith("roof-box"):
            draw_ring(pixels, (*PROPOSAL_BOX_COLOR, 255), 4, fill=(*PROPOSAL_BOX_COLOR, 60))
        else:
            draw_ring(pixels, (*OVERRIDE_OBJECT_COLOR, 255), 3, fill=(*OVERRIDE_OBJECT_COLOR, 60))

    # Nearby G-NAF pins with house numbers (drawn first so the own pin,
    # CRM rings, and labels sit on top). Attribution check at a glance: the
    # pins inside/nearest the red ring should belong to the record address.
    try:
        from PIL import ImageFont

        pin_font = ImageFont.load_default(size=22)
    except Exception:  # noqa: BLE001
        pin_font = None
    for nearby in context.nearby_pins:
        if nearby["address_id"] == record["address_id"]:
            continue
        pixel = window.local_to_pixel(
            *lonlat_to_tile_local(nearby["lon"], nearby["lat"], window.x, window.y, window.z)
        )
        draw.ellipse(
            [pixel[0] - 5, pixel[1] - 5, pixel[0] + 5, pixel[1] + 5],
            fill="white",
            outline=(80, 80, 80),
            width=2,
        )
        draw.text(
            (pixel[0] + 8, pixel[1] - 10),
            nearby["label"],
            fill="white",
            stroke_width=3,
            stroke_fill=(30, 30, 30),
            font=pin_font,
        )

    # Address pin
    pin = window.local_to_pixel(
        *lonlat_to_tile_local(
            record["address_point"]["lon"],
            record["address_point"]["lat"],
            window.x,
            window.y,
            window.z,
        )
    )
    draw.ellipse([pin[0] - 6, pin[1] - 6, pin[0] + 6, pin[1] + 6], fill=ADDRESS_COLOR, outline="white", width=2)

    # Crop around the roof with breathing room, then upscale if small
    xs = [point[0] for point in roof_pixels]
    ys = [point[1] for point in roof_pixels]
    pad = max(90.0, (max(xs) - min(xs)) * 0.55, (max(ys) - min(ys)) * 0.55)
    left = max(0, int(min(xs) - pad))
    top = max(0, int(min(ys) - pad))
    right = min(image.width, int(max(xs) + pad))
    bottom = min(image.height, int(max(ys) + pad))
    crop = image.crop((left, top, right, bottom))
    if max(crop.size) < max_px:
        scale = max_px / max(crop.size)
        crop = crop.resize((int(crop.width * scale), int(crop.height * scale)), Image.Resampling.LANCZOS)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    crop.save(out_path)

    # Scale bar + label
    labeled = crop.copy()
    draw = ImageDraw.Draw(labeled, "RGBA")
    meters_per_pixel = window.meters_per_pixel / (crop.width / (right - left))
    bar_meters = 10 if meters_per_pixel * 120 < 25 else 25
    bar_px = bar_meters / meters_per_pixel
    margin = 14
    y0 = labeled.height - margin - 8
    draw.rectangle([margin, y0, margin + bar_px, y0 + 5], fill="white")
    draw.text((margin, y0 - 16), f"{bar_meters} m", fill="white", stroke_width=2, stroke_fill=(20, 20, 20))
    label = f"{record['full_address']} · {record['kind']} · roof {record['roof_type']} {record['area_m2']:.0f}m²"
    draw.text((margin, margin), label, fill="white", stroke_width=2, stroke_fill=(20, 20, 20))
    if context.features:
        draw.text(
            (margin, margin + 18),
            f"CRM overrides: {', '.join(feature['kind'] for feature in context.features)}",
            fill=OVERRIDE_ROOF_COLOR,
            stroke_width=2,
            stroke_fill=(20, 20, 20),
        )
    labeled.save(out_path)
    return out_path


def render_3d_image(lat: float, lon: float, out_path: Path, timeout: float = 180.0) -> Path:
    script = REPO_ROOT / "scripts" / "studio-shot.mjs"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "node",
            str(script),
            "--lat",
            f"{lat:.7f}",
            "--lon",
            f"{lon:.7f}",
            "--out",
            str(out_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return out_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_argument_group("address")
    target.add_argument("--address", type=str, default=None, help="substring of the address")
    target.add_argument("--lat", type=float, default=None)
    target.add_argument("--lon", type=float, default=None)
    target.add_argument("--address-id", type=int, default=None)
    parser.add_argument("--tiles", type=Path, default=DEFAULT_TILES)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--list", action="store_true", help="list matching addresses and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list:
        needle = (args.address or "").upper()
        for record in load_records():
            if not needle or needle in record["full_address"].upper():
                print(
                    f"{record['address_id']:>6}  {record['full_address']:<55} "
                    f"{record['kind']:<8} {record['area_m2']:>7.0f}m²"
                )
        return 0

    context = build_context(
        query=args.address,
        lat=args.lat,
        lon=args.lon,
        address_id=args.address_id,
        tiles_path=args.tiles,
        db_path=args.db,
    )
    if context is None:
        print("no matching address record", file=sys.stderr)
        return 1
    summary = metadata_summary(context)
    print(json.dumps(summary, indent=2))
    slug = str(context.record["address_id"])
    site = render_site_image(context, args.out_dir / slug / "site.png")
    print(f"site image: {site}")
    lat = context.record["centroid"]["lat"]
    lon = context.record["centroid"]["lon"]
    image_3d = render_3d_image(lat, lon, args.out_dir / slug / "3d.png")
    print(f"3d image: {image_3d}")
    (args.out_dir / slug / "metadata.json").write_text(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
