"""Offline AI building-tile pipeline.

Fetches high-resolution aerial imagery, segments building footprints with a
pretrained model, optionally estimates per-building heights from monocular
depth, merges OpenStreetMap cadastre, and writes Google-Maps-style building
outlines/extrusions into a gzip-compressed MVT MBTiles archive that the Go
tile microservice serves unchanged at /tiles/{z}/{x}/{y}.pbf.

Usage:
    python -m scripts.ml.pipeline --bbox 145.30,-37.80,145.33,-37.78 \
        --zooms 16 --out data/australia_tiles.mbtiles
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import numpy as np
from PIL import Image, ImageDraw
from shapely.geometry import Polygon, box, mapping
from shapely.ops import transform

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "scripts.ml"

from .depth import (
    OnnxDepthEstimator,
    estimate_heights,
    estimate_meters_per_unit,
)
from .footprints import LocalFootprint, footprint_properties, merge_footprints
from .imagery import ImageryFetcher, ImageryWindow
from .osm import OsmFootprint, fetch_osm_buildings
from .presets import DEFAULT_SEGMENTER, SEGMENTER_PRESETS, get_preset
from .roof import model_roof
from .sam3 import Sam3TextSegmenter
from .segmentation import (
    HeuristicBuildingSegmenter,
    OnnxBuildingSegmenter,
    binary_close,
    binary_open,
    iter_polygons,
    mask_to_polygons,
    polygons_to_local,
)
from .tiles import (
    MVT_BUFFER,
    TILE_EXTENT,
    MBTilesWriter,
    MVTFeature,
    encode_mvt_tile,
    iter_tiles,
    lonlat_to_tile_local,
    tile_bounds,
    tile_local_to_lonlat,
    vector_layers_json,
)

LOGGER = logging.getLogger("ml.pipeline")
MODELS_DIR = Path(__file__).resolve().parent / "models"
DEFAULT_DEPTH_MODEL = MODELS_DIR / "depth_anything_v2_small.onnx"


@dataclass
class PipelineConfig:
    bbox: tuple[float, float, float, float] | None = None
    zooms: list[int] = field(default_factory=lambda: [16])
    out_path: Path = Path("data/australia_tiles.mbtiles")
    provider: str = "google"
    cache_dir: Path = Path("data/imagery")
    imagery_offset: int = 2
    out_px: int = 512
    margin_px: int = 32
    seg_preset: str = DEFAULT_SEGMENTER
    seg_model: Path | None = None
    depth_model: Path | None = None
    building_classes: tuple[int, ...] | None = None
    threshold: float | None = None
    seg_normalization: str | None = None
    seg_stride: int | None = None
    seg_close_iterations: int = 1
    min_area_m2: float = 25.0
    simplify_px: float = 1.25
    with_heights: bool = False
    assume_height: float = 7.0
    meters_per_unit: float | None = None
    with_trees: bool = False
    tree_min_area_m2: float = 6.0
    tree_max_area_m2: float = 900.0
    tree_min_height_m: float = 3.0
    tree_max_height_m: float = 30.0
    assume_tree_height: float = 9.0
    exg_threshold: float = 0.05
    sam3: bool = False
    sam3_models_dir: Path | None = None
    sam3_prompt: str = "house"
    sam3_threshold: float = 0.4
    sam3_min_iou: float = 0.3
    sam3_add_detections: bool = True
    sam3_trees: bool = False
    sam3_tree_prompt: str = "tree"
    use_osm: bool = True
    overpass_endpoint: str | None = None
    heuristic: bool = False
    procedural: bool = False
    require_models: bool = False
    workers: int = 8
    preview_dir: Path | None = None
    preview_limit: int = 12
    geojson_path: Path | None = None
    limit_tiles: int | None = None


@dataclass
class PipelineContext:
    fetcher: ImageryFetcher
    segmenter: OnnxBuildingSegmenter | HeuristicBuildingSegmenter
    depth_estimator: OnnxDepthEstimator | None
    osm_footprints: list[OsmFootprint]
    sam3: Sam3TextSegmenter | None = None


@dataclass
class TileResult:
    features: list[MVTFeature] = field(default_factory=list)
    tree_features: list[MVTFeature] = field(default_factory=list)
    geojson_features: list[dict] = field(default_factory=list)
    ml_count: int = 0
    osm_count: int = 0
    tree_count: int = 0
    merged_count: int = 0


def build_segmenter(cfg: PipelineConfig):
    if cfg.heuristic:
        LOGGER.info("using heuristic building segmenter (offline mode)")
        return HeuristicBuildingSegmenter()
    preset = get_preset(cfg.seg_preset)
    model_path = Path(cfg.seg_model) if cfg.seg_model else MODELS_DIR / preset.target
    if not model_path.exists():
        if cfg.require_models:
            raise FileNotFoundError(
                f"segmentation model not found at {model_path}; "
                f"run `python -m scripts.ml.fetch_models --presets {preset.key}`"
            )
        LOGGER.warning(
            "segmentation model not found at %s; falling back to heuristic segmenter "
            "(run `python -m scripts.ml.fetch_models` for real ML inference)",
            model_path,
        )
        return HeuristicBuildingSegmenter()

    LOGGER.info(
        "loading segmentation model",
        extra={"model": str(model_path), "preset": preset.key},
    )
    return OnnxBuildingSegmenter(
        model_path,
        building_classes=cfg.building_classes or preset.building_classes,
        input_size=preset.input_size,
        threshold=cfg.threshold if cfg.threshold is not None else preset.threshold,
        normalization=cfg.seg_normalization or preset.normalization,
        output_mode=preset.output_mode,
        activation=preset.activation,
        layout=preset.layout,
        stride=cfg.seg_stride if cfg.seg_stride is not None else (preset.stride or None),
        close_iterations=cfg.seg_close_iterations,
    )


def build_depth_estimator(cfg: PipelineConfig) -> OnnxDepthEstimator | None:
    if not cfg.with_heights:
        return None
    model_path = Path(cfg.depth_model) if cfg.depth_model else DEFAULT_DEPTH_MODEL
    if not model_path.exists():
        if cfg.require_models:
            raise FileNotFoundError(f"depth model not found at {model_path}")
        LOGGER.warning(
            "depth model not found at %s; building heights will be omitted",
            model_path,
        )
        return None
    LOGGER.info("loading depth model", extra={"model": str(model_path)})
    return OnnxDepthEstimator(model_path)


def _distance_units(lat: float, z: int, meters: float) -> float:
    span = 40075016.68557849 * math.cos(math.radians(lat)) / (1 << z)
    return meters / (span / TILE_EXTENT)


def vegetation_mask(image: Image.Image, exg_threshold: float = 0.05) -> np.ndarray:
    """Excess Green index mask for canopy/vegetation pixels."""
    array = np.asarray(image.convert("RGB"), dtype=np.float32)
    red, green, blue = array[..., 0], array[..., 1], array[..., 2]
    total = red + green + blue + 1.0
    exg = (2.0 * green - red - blue) / total
    luminance = 0.299 * red + 0.587 * green + 0.114 * blue
    mask = (exg > exg_threshold) & (green > 35.0) & (luminance > 20.0)
    mask = binary_close(mask, 1)
    return binary_open(mask, 1)


def _merge_sam3_polygons(
    polygons_px: list[Polygon],
    proposal_polygons: list[tuple[Polygon, float]],
    *,
    min_iou: float,
    meters_per_px: float,
    add_detections: bool,
    add_score_threshold: float,
    add_min_area_m2: float,
    add_max_area_m2: float,
) -> list[Polygon]:
    """Replaces/extends pixel-space polygons with better-overlapping SAM3 proposals.

    Each base polygon is replaced by its best proposal when the IoU and area
    ratio are plausible; high-confidence unmatched proposals are appended so
    SAM3 can recover objects the base detector missed.
    """
    merged: list[Polygon] = []
    used: set[int] = set()
    for polygon in polygons_px:
        best_index = -1
        best_iou = 0.0
        for index, (candidate, _) in enumerate(proposal_polygons):
            if candidate.area <= 0 or polygon.area <= 0:
                continue
            union = polygon.union(candidate).area
            if union <= 0:
                continue
            iou = polygon.intersection(candidate).area / union
            if iou > best_iou:
                best_iou = iou
                best_index = index
        if best_index >= 0 and best_iou >= min_iou:
            candidate, _ = proposal_polygons[best_index]
            ratio = candidate.area / max(polygon.area, 1e-6)
            if 0.5 <= ratio <= 2.0:
                merged.append(candidate)
                used.add(best_index)
                continue
        merged.append(polygon)

    if add_detections:
        for index, (candidate, score) in enumerate(proposal_polygons):
            if index in used or score < add_score_threshold:
                continue
            area_m2 = candidate.area * meters_per_px**2
            if not add_min_area_m2 <= area_m2 <= add_max_area_m2:
                continue
            overlap = 0.0
            for existing in merged:
                union = candidate.union(existing).area
                if union > 0:
                    overlap = max(overlap, candidate.intersection(existing).area / union)
            if overlap < 0.3:
                merged.append(candidate)
    return merged


def _sam3_proposal_polygons(
    sam3: Sam3TextSegmenter,
    features: list[np.ndarray],
    window: ImageryWindow,
    prompt: str,
    threshold: float,
    min_area_px: float,
    simplify_px: float,
) -> list[tuple[Polygon, float]]:
    proposals = sam3.proposals(features, prompt, window.image.size, threshold=threshold)
    polygons: list[tuple[Polygon, float]] = []
    for proposal in proposals:
        parts = mask_to_polygons(
            proposal.mask, min_area_px=min_area_px, simplify_px=simplify_px
        )
        if not parts:
            continue
        best = max(parts, key=lambda part: part.area)
        polygons.append((best, proposal.score))
    return polygons


def _refine_roofs_with_sam3(
    cfg: PipelineConfig,
    window: ImageryWindow,
    polygons_px: list[Polygon],
    sam3: Sam3TextSegmenter,
    features: list[np.ndarray] | None = None,
) -> list[Polygon]:
    """Refines DINOv3 building footprints with SAM3 text-prompted roof outlines.

    The vision encoder runs once per tile; each SAM3 proposal is matched to the
    best-overlapping DINOv3 footprint and replaces it when the overlap and area
    ratio are plausible. High-confidence unmatched proposals are added so SAM3
    can recover roofs the detector missed.
    """
    if not polygons_px:
        return polygons_px
    try:
        if features is None:
            features = sam3.encode_image(window.image)
        meters_per_px = max(window.meters_per_pixel, 1e-6)
        proposal_polygons = _sam3_proposal_polygons(
            sam3,
            features,
            window,
            cfg.sam3_prompt,
            cfg.sam3_threshold,
            max(8.0, cfg.min_area_m2 / (meters_per_px**2)),
            max(0.0, cfg.simplify_px * window.scale),
        )
    except Exception as error:  # noqa: BLE001
        LOGGER.warning("SAM3 refinement failed (%s); keeping segmenter outlines", error)
        return polygons_px

    refined = _merge_sam3_polygons(
        polygons_px,
        proposal_polygons,
        min_iou=cfg.sam3_min_iou,
        meters_per_px=meters_per_px,
        add_detections=cfg.sam3_add_detections,
        add_score_threshold=max(cfg.sam3_threshold, 0.6),
        add_min_area_m2=10.0,
        add_max_area_m2=1500.0,
    )

    LOGGER.info(
        "SAM3 refined roofs",
        extra={"prompt": cfg.sam3_prompt, "proposals": len(proposal_polygons), "out": len(refined)},
    )
    return refined


def _building_footprints(
    cfg: PipelineConfig,
    window: ImageryWindow,
    mask: np.ndarray,
    depth: np.ndarray | None,
    sam3: Sam3TextSegmenter | None = None,
    sam3_features: list[np.ndarray] | None = None,
) -> tuple[list[LocalFootprint], float | None]:
    meters_per_px = max(window.meters_per_pixel, 1e-6)
    meters_per_local = window.tile_span_meters / TILE_EXTENT
    min_area_px = max(8.0, cfg.min_area_m2 / (meters_per_px**2))
    simplify_px = max(0.0, cfg.simplify_px * window.scale)
    polygons_px = mask_to_polygons(
        mask, min_area_px=min_area_px, simplify_px=simplify_px
    )
    if not polygons_px:
        return [], None
    if sam3 is not None:
        polygons_px = _refine_roofs_with_sam3(
            cfg, window, polygons_px, sam3, sam3_features
        )

    meters_per_unit: float | None = None
    heights: list[float | None] = [None] * len(polygons_px)
    if depth is not None:
        meters_per_unit = cfg.meters_per_unit or estimate_meters_per_unit(
            polygons_px, depth, assume_height=cfg.assume_height
        )
        heights = estimate_heights(
            polygons_px, depth, meters_per_unit=meters_per_unit
        )

    footprints: list[LocalFootprint] = []
    for polygon, height in zip(polygons_px, heights, strict=True):
        levels = max(1, round(height / 3.1)) if height else None
        roof = model_roof(polygon, height, meters_per_px)
        local_parts = polygons_to_local(
            [polygon],
            lambda px, py: window.pixel_to_local(px, py),
            clip_buffer=MVT_BUFFER,
        )
        for part in local_parts:
            footprints.append(
                LocalFootprint(
                    geometry=part,
                    source="ml",
                    height=height,
                    levels=levels,
                    area_m2=round(part.area * meters_per_local**2, 1),
                    kind="building",
                    roof_type=roof.roof_type,
                    eave_height=roof.eave_height,
                    ridge_height=roof.ridge_height,
                    ridge_angle_deg=roof.ridge_angle_deg,
                )
            )
    return footprints, meters_per_unit


def _tree_footprints(
    cfg: PipelineConfig,
    window: ImageryWindow,
    depth: np.ndarray | None,
    meters_per_unit: float | None,
    sam3: Sam3TextSegmenter | None = None,
    sam3_features: list[np.ndarray] | None = None,
) -> list[LocalFootprint]:
    if depth is None:
        return []
    meters_per_px = max(window.meters_per_pixel, 1e-6)
    meters_per_local = window.tile_span_meters / TILE_EXTENT
    mask = vegetation_mask(window.image, cfg.exg_threshold)
    min_area_px = max(4.0, cfg.tree_min_area_m2 / (meters_per_px**2))
    simplify_px = max(0.0, cfg.simplify_px * window.scale)

    polygons_px = mask_to_polygons(
        mask, min_area_px=min_area_px, simplify_px=simplify_px
    )

    if sam3 is not None and cfg.sam3_trees:
        try:
            if sam3_features is None:
                sam3_features = sam3.encode_image(window.image)
            proposal_polygons = _sam3_proposal_polygons(
                sam3,
                sam3_features,
                window,
                cfg.sam3_tree_prompt,
                cfg.sam3_threshold,
                min_area_px,
                simplify_px,
            )
            polygons_px = _merge_sam3_polygons(
                polygons_px,
                proposal_polygons,
                min_iou=cfg.sam3_min_iou,
                meters_per_px=meters_per_px,
                add_detections=cfg.sam3_add_detections,
                add_score_threshold=max(cfg.sam3_threshold, 0.6),
                add_min_area_m2=cfg.tree_min_area_m2,
                add_max_area_m2=cfg.tree_max_area_m2,
            )
        except Exception as error:  # noqa: BLE001
            LOGGER.warning(
                "SAM3 tree refinement failed (%s); keeping vegetation canopies", error
            )

    if not polygons_px:
        return []

    if meters_per_unit is None:
        meters_per_unit = estimate_meters_per_unit(
            polygons_px, depth, assume_height=cfg.assume_tree_height
        )
    heights = estimate_heights(polygons_px, depth, meters_per_unit=meters_per_unit)

    footprints: list[LocalFootprint] = []
    for polygon, height in zip(polygons_px, heights, strict=True):
        if height is None or height < cfg.tree_min_height_m:
            continue
        area_m2 = polygon.area * meters_per_px**2
        if area_m2 > cfg.tree_max_area_m2:
            continue
        height = min(height, cfg.tree_max_height_m)
        canopy_base = round(max(1.5, height * 0.35), 1)
        local_parts = polygons_to_local(
            [polygon],
            lambda px, py: window.pixel_to_local(px, py),
            clip_buffer=MVT_BUFFER,
        )
        for part in local_parts:
            footprints.append(
                LocalFootprint(
                    geometry=part,
                    source="ml",
                    height=height,
                    min_height=canopy_base,
                    area_m2=round(part.area * meters_per_local**2, 1),
                    kind="tree",
                )
            )
    return footprints


def _osm_tile_footprints(
    osm_footprints: list[OsmFootprint], z: int, x: int, y: int
) -> list[LocalFootprint]:
    if not osm_footprints:
        return []
    west, south, east, north = tile_bounds(x, y, z)
    clip = box(
        -MVT_BUFFER, -MVT_BUFFER, TILE_EXTENT + MVT_BUFFER, TILE_EXTENT + MVT_BUFFER
    )
    result: list[LocalFootprint] = []
    for footprint in osm_footprints:
        min_lon, min_lat, max_lon, max_lat = footprint.bbox()
        if max_lon < west or min_lon > east or max_lat < south or min_lat > north:
            continue
        local_ring = [
            lonlat_to_tile_local(lon, lat, x, y, z) for lon, lat in footprint.ring
        ]
        geometry = Polygon(local_ring)
        if not geometry.is_valid:
            geometry = geometry.buffer(0)
        if geometry.is_empty:
            continue
        clipped = geometry.intersection(clip)
        for part in iter_polygons(clipped):
            result.append(
                LocalFootprint(
                    geometry=part,
                    source="osm",
                    height=footprint.height,
                    min_height=footprint.min_height,
                    levels=footprint.levels,
                )
            )
    return result


def _mvt_feature(footprint: LocalFootprint) -> MVTFeature:
    exterior = [(int(round(px)), int(round(py))) for px, py in footprint.geometry.exterior.coords]
    holes = [
        [(int(round(px)), int(round(py))) for px, py in interior.coords]
        for interior in footprint.geometry.interiors
    ]
    return MVTFeature.from_rings(
        exterior, holes, properties=footprint_properties(footprint)
    )


def _geojson_feature(
    footprint: LocalFootprint, z: int, x: int, y: int
) -> dict:
    projector = cast(
        Any, lambda lx, ly: tile_local_to_lonlat(lx, ly, x, y, z)
    )
    geometry = transform(projector, footprint.geometry)
    return {
        "type": "Feature",
        "properties": footprint_properties(footprint),
        "geometry": mapping(geometry),
    }


def process_tile(
    cfg: PipelineConfig,
    ctx: PipelineContext,
    z: int,
    x: int,
    y: int,
    write_preview: bool = True,
) -> TileResult:
    window = ctx.fetcher.window(z, x, y)
    mask = ctx.segmenter.segment(window.image)
    depth = (
        ctx.depth_estimator.estimate(window.image)
        if ctx.depth_estimator is not None
        else None
    )
    sam3_features = (
        ctx.sam3.encode_image(window.image)
        if ctx.sam3 is not None and (cfg.sam3 or cfg.sam3_trees)
        else None
    )
    ml, meters_per_unit = _building_footprints(
        cfg,
        window,
        mask,
        depth,
        ctx.sam3 if cfg.sam3 else None,
        sam3_features,
    )
    trees = (
        _tree_footprints(
            cfg,
            window,
            depth,
            meters_per_unit,
            ctx.sam3 if cfg.sam3_trees else None,
            sam3_features,
        )
        if cfg.with_trees and depth is not None
        else []
    )
    osm = _osm_tile_footprints(ctx.osm_footprints, z, x, y)
    west, south, east, north = tile_bounds(x, y, z)
    center_lat = (south + north) / 2.0
    merged = merge_footprints(
        ml,
        osm,
        max_center_distance=_distance_units(center_lat, z, 25.0),
    )

    result = TileResult(
        ml_count=len(ml),
        osm_count=len(osm),
        tree_count=len(trees),
        merged_count=len(merged),
    )
    for footprint in merged:
        result.features.append(_mvt_feature(footprint))
        result.geojson_features.append(_geojson_feature(footprint, z, x, y))
    for tree in trees:
        result.tree_features.append(_mvt_feature(tree))
        result.geojson_features.append(_geojson_feature(tree, z, x, y))

    if cfg.preview_dir is not None and write_preview:
        _write_preview(window, merged, trees, z, x, y, cfg.preview_dir)
    return result


def _write_preview(
    window: ImageryWindow,
    footprints: list[LocalFootprint],
    trees: list[LocalFootprint],
    z: int,
    x: int,
    y: int,
    preview_dir: Path,
) -> None:
    image = window.image.copy()
    draw = ImageDraw.Draw(image)

    def draw_footprint(footprint: LocalFootprint, color: tuple[int, int, int]) -> None:
        rings = [footprint.geometry.exterior, *footprint.geometry.interiors]
        for ring in rings:
            points = [window.local_to_pixel(lx, ly) for lx, ly in ring.coords]
            if len(points) >= 2:
                draw.line(points, fill=color, width=3)
        if footprint.height:
            centroid = footprint.geometry.centroid
            px, py = window.local_to_pixel(centroid.x, centroid.y)
            draw.text(
                (px, py),
                f"{footprint.height:.0f}m",
                fill=(15, 23, 42),
                stroke_width=2,
                stroke_fill=(255, 255, 255),
            )

    for tree in trees:
        draw_footprint(tree, (22, 163, 74))

    for footprint in footprints:
        color = (220, 38, 38) if footprint.source == "ml" else (37, 99, 235)
        if footprint.source == "osm+ml":
            color = (124, 58, 237)
        draw_footprint(footprint, color)

    preview_dir.mkdir(parents=True, exist_ok=True)
    image.save(preview_dir / f"{z}_{x}_{y}.png")


def build_context(cfg: PipelineConfig) -> PipelineContext:
    segmenter = build_segmenter(cfg)
    depth_estimator = build_depth_estimator(cfg)

    sam3: Sam3TextSegmenter | None = None
    if cfg.sam3 or cfg.sam3_trees:
        sam3_dir = Path(cfg.sam3_models_dir) if cfg.sam3_models_dir else MODELS_DIR / "sam3"
        try:
            sam3 = Sam3TextSegmenter(sam3_dir)
        except FileNotFoundError as error:
            if cfg.require_models:
                raise
            LOGGER.warning(
                "SAM3 models unavailable (%s); continuing without SAM3 refinement", error
            )

    osm_footprints: list[OsmFootprint] = []
    bbox = cfg.bbox
    if cfg.use_osm and not cfg.procedural and bbox is not None:
        try:
            LOGGER.info("fetching OpenStreetMap buildings from Overpass")
            osm_footprints = fetch_osm_buildings(
                bbox, endpoint=cfg.overpass_endpoint
            )
            LOGGER.info("fetched OSM footprints", extra={"count": len(osm_footprints)})
        except Exception as error:
            LOGGER.warning("Overpass fetch failed (%s); continuing without OSM", error)

    fetcher = ImageryFetcher(
        provider=cfg.provider,
        cache_dir=cfg.cache_dir,
        imagery_offset=cfg.imagery_offset,
        out_px=cfg.out_px,
        margin_px=cfg.margin_px,
        workers=cfg.workers,
        procedural=cfg.procedural,
    )
    return PipelineContext(
        fetcher=fetcher,
        segmenter=segmenter,
        depth_estimator=depth_estimator,
        osm_footprints=osm_footprints,
        sam3=sam3,
    )


def generate_tile_mvt(
    cfg: PipelineConfig, ctx: PipelineContext, z: int, x: int, y: int
) -> bytes:
    """Generates a single MVT tile payload (uncompressed) for realtime serving."""
    result = process_tile(cfg, ctx, z, x, y, write_preview=False)
    layers: dict[str, list[MVTFeature]] = {}
    if result.features:
        layers["building"] = result.features
    if result.tree_features:
        layers["tree"] = result.tree_features
    if not layers:
        return b""
    return encode_mvt_tile(layers)


def run(cfg: PipelineConfig) -> dict:
    if cfg.bbox is None:
        raise ValueError("pipeline runs require a bbox")
    bbox = cfg.bbox
    started = time.time()
    ctx = build_context(cfg)
    writer = MBTilesWriter(cfg.out_path)
    writer.set_metadata(
        {
            "name": "addr AI building footprints",
            "description": "ML building footprints + heights for Google-style map tiles",
            "format": "pbf",
            "type": "overlay",
            "version": "2",
            "minzoom": str(min(cfg.zooms)),
            "maxzoom": str(max(cfg.zooms)),
            "bounds": ",".join(f"{value:.6f}" for value in bbox),
            "attribution": "AI-derived footprints; © OpenStreetMap contributors",
            "json": vector_layers_json(
                {
                    "building": {
                        "minzoom": min(cfg.zooms),
                        "maxzoom": max(cfg.zooms),
                        "fields": {
                            "height": "Number",
                            "min_height": "Number",
                            "render_height": "Number",
                            "render_min_height": "Number",
                            "source": "String",
                            "levels": "Number",
                            "area_m2": "Number",
                            "kind": "String",
                            "confidence": "Number",
                        },
                    },
                    **(
                        {
                            "tree": {
                                "minzoom": min(cfg.zooms),
                                "maxzoom": max(cfg.zooms),
                                "fields": {
                                    "height": "Number",
                                    "min_height": "Number",
                                    "render_height": "Number",
                                    "render_min_height": "Number",
                                    "source": "String",
                                    "area_m2": "Number",
                                    "kind": "String",
                                },
                            }
                        }
                        if cfg.with_trees
                        else {}
                    ),
                }
            ),
        }
    )

    geojson_features: list[dict] = []
    processed = 0
    total_ml = 0
    total_osm = 0
    total_trees = 0
    total_features = 0
    previews_written = 0

    try:
        for z in cfg.zooms:
            for x, y in iter_tiles(bbox, z):
                if cfg.limit_tiles is not None and processed >= cfg.limit_tiles:
                    break
                write_preview = (
                    cfg.preview_dir is not None
                    and previews_written < cfg.preview_limit
                )
                try:
                    result = process_tile(cfg, ctx, z, x, y, write_preview)
                except Exception as error:
                    LOGGER.error(
                        "tile failed", extra={"z": z, "x": x, "y": y, "error": str(error)}
                    )
                    continue

                layers: dict[str, list[MVTFeature]] = {}
                if result.features:
                    layers["building"] = result.features
                if result.tree_features:
                    layers["tree"] = result.tree_features
                data = encode_mvt_tile(layers)
                writer.write_tile(z, x, y, data)
                geojson_features.extend(result.geojson_features)
                total_ml += result.ml_count
                total_osm += result.osm_count
                total_trees += result.tree_count
                total_features += result.merged_count
                processed += 1
                if write_preview:
                    previews_written += 1

                if processed % 10 == 0 or processed == 1:
                    LOGGER.info(
                        "tiles processed",
                        extra={
                            "tiles": processed,
                            "features": total_features,
                            "last": f"{z}/{x}/{y}",
                        },
                    )
            if cfg.limit_tiles is not None and processed >= cfg.limit_tiles:
                break
    finally:
        writer.close()

    if cfg.geojson_path is not None:
        cfg.geojson_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.geojson_path.write_text(
            json.dumps({"type": "FeatureCollection", "features": geojson_features})
        )

    summary = {
        "tiles": processed,
        "ml_footprints": total_ml,
        "osm_footprints": total_osm,
        "trees": total_trees,
        "merged_footprints": total_features,
        "mbtiles": str(cfg.out_path),
        "elapsed_s": round(time.time() - started, 2),
    }
    LOGGER.info("build complete: %s", json.dumps(summary))
    return summary


def _parse_bbox(value: str) -> tuple[float, float, float, float]:
    parts = [float(part) for part in value.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            "bbox must be min_lon,min_lat,max_lon,max_lat"
        )
    return parts[0], parts[1], parts[2], parts[3]


def _parse_zooms(value: str) -> list[int]:
    return [int(part) for part in value.split(",") if part.strip()]


def _parse_classes(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split(",") if part.strip())


def parse_args(argv: list[str] | None = None) -> PipelineConfig:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.ml.pipeline",
        description=(
            "Convert high-resolution aerial imagery into building footprint "
            "MVT tiles using segmentation + monocular depth."
        ),
    )
    parser.add_argument("--bbox", required=True, type=_parse_bbox, help="min_lon,min_lat,max_lon,max_lat")
    parser.add_argument("--zooms", default="16", type=_parse_zooms, help="comma separated output zooms, e.g. 15,16")
    parser.add_argument("--out", default="data/australia_tiles.mbtiles", type=Path)
    parser.add_argument("--provider", default="google", choices=["google", "google-hybrid", "esri"])
    parser.add_argument("--imagery-dir", default="data/imagery", type=Path)
    parser.add_argument("--imagery-offset", default=2, type=int, help="imagery zoom = output zoom + offset")
    parser.add_argument("--out-px", default=512, type=int)
    parser.add_argument("--margin-px", default=32, type=int)
    parser.add_argument(
        "--seg-preset",
        default=DEFAULT_SEGMENTER,
        choices=sorted(SEGMENTER_PRESETS),
        help="pretrained segmenter to use",
    )
    parser.add_argument("--seg-model", default=None, type=Path, help="override model file path")
    parser.add_argument("--depth-model", default=None, type=Path)
    parser.add_argument("--building-classes", default=None, type=_parse_classes, help="override multiclass indices")
    parser.add_argument("--threshold", default=None, type=float, help="override preset probability threshold")
    parser.add_argument("--seg-normalization", default=None, choices=["unit", "imagenet", "hot", "none"])
    parser.add_argument("--seg-stride", default=None, type=int, help="sliding window stride in pixels")
    parser.add_argument("--seg-close-iterations", default=1, type=int)
    parser.add_argument("--min-area-m2", default=25.0, type=float)
    parser.add_argument("--simplify-px", default=1.25, type=float)
    parser.add_argument("--heights", action="store_true", help="estimate heights with the depth model")
    parser.add_argument("--assume-height", default=7.0, type=float, help="meters used to calibrate relative depth")
    parser.add_argument("--meters-per-unit", default=None, type=float)
    parser.add_argument("--trees", action="store_true", help="extract tree canopies into a tree layer (requires --heights)")
    parser.add_argument("--tree-min-area-m2", default=6.0, type=float)
    parser.add_argument("--tree-max-area-m2", default=900.0, type=float)
    parser.add_argument("--tree-min-height", default=3.0, type=float)
    parser.add_argument("--tree-max-height", default=30.0, type=float)
    parser.add_argument("--assume-tree-height", default=9.0, type=float)
    parser.add_argument("--exg-threshold", default=0.05, type=float)
    parser.add_argument("--sam3", action="store_true", help="refine roof outlines with SAM3 text prompts")
    parser.add_argument("--sam3-models-dir", default=None, type=Path)
    parser.add_argument("--sam3-prompt", default="house", help='SAM3 concept prompt (e.g. "house", "building roof")')
    parser.add_argument("--sam3-threshold", default=0.4, type=float)
    parser.add_argument("--sam3-min-iou", default=0.3, type=float)
    parser.add_argument("--no-sam3-add", action="store_true", help="only refine, never add SAM3-only detections")
    parser.add_argument("--sam3-trees", action="store_true", help="also refine/complete tree canopies with SAM3")
    parser.add_argument("--sam3-tree-prompt", default="tree", help='SAM3 concept prompt for canopies (e.g. "tree", "tree canopy")')
    parser.add_argument("--no-osm", action="store_true", help="skip Overpass OSM cadastre merge")
    parser.add_argument("--overpass-endpoint", default=None)
    parser.add_argument("--heuristic", action="store_true", help="force the offline heuristic segmenter")
    parser.add_argument("--procedural", action="store_true", help="use synthetic offline imagery")
    parser.add_argument("--require-models", action="store_true")
    parser.add_argument("--workers", default=8, type=int)
    parser.add_argument("--preview-dir", default=None, type=Path)
    parser.add_argument("--preview-limit", default=12, type=int)
    parser.add_argument("--geojson", default=None, type=Path)
    parser.add_argument("--limit-tiles", default=None, type=int)
    parser.add_argument("--verbose", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return PipelineConfig(
        bbox=args.bbox,
        zooms=args.zooms,
        out_path=args.out,
        provider=args.provider,
        cache_dir=args.imagery_dir,
        imagery_offset=args.imagery_offset,
        out_px=args.out_px,
        margin_px=args.margin_px,
        seg_preset=args.seg_preset,
        seg_model=args.seg_model,
        depth_model=args.depth_model,
        building_classes=args.building_classes,
        threshold=args.threshold,
        seg_normalization=args.seg_normalization,
        seg_stride=args.seg_stride,
        seg_close_iterations=args.seg_close_iterations,
        min_area_m2=args.min_area_m2,
        simplify_px=args.simplify_px,
        with_heights=args.heights,
        assume_height=args.assume_height,
        meters_per_unit=args.meters_per_unit,
        with_trees=args.trees,
        tree_min_area_m2=args.tree_min_area_m2,
        tree_max_area_m2=args.tree_max_area_m2,
        tree_min_height_m=args.tree_min_height,
        tree_max_height_m=args.tree_max_height,
        assume_tree_height=args.assume_tree_height,
        exg_threshold=args.exg_threshold,
        sam3=args.sam3,
        sam3_models_dir=args.sam3_models_dir,
        sam3_prompt=args.sam3_prompt,
        sam3_threshold=args.sam3_threshold,
        sam3_min_iou=args.sam3_min_iou,
        sam3_add_detections=not args.no_sam3_add,
        sam3_trees=args.sam3_trees,
        sam3_tree_prompt=args.sam3_tree_prompt,
        use_osm=not args.no_osm,
        overpass_endpoint=args.overpass_endpoint,
        heuristic=args.heuristic,
        procedural=args.procedural,
        require_models=args.require_models,
        workers=args.workers,
        preview_dir=args.preview_dir,
        preview_limit=args.preview_limit,
        geojson_path=args.geojson,
        limit_tiles=args.limit_tiles,
    )


def main(argv: list[str] | None = None) -> int:
    cfg = parse_args(argv)
    run(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
