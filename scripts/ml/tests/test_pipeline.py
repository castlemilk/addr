import json

import numpy as np
from PIL import Image, ImageDraw
from scripts.ml.imagery import ImageryWindow
from scripts.ml.pipeline import (
    PipelineConfig,
    PipelineContext,
    process_tile,
    run,
    vegetation_mask,
)
from scripts.ml.segmentation import HeuristicBuildingSegmenter
from scripts.ml.tiles import iter_tiles, read_mbtiles_metadata, read_mbtiles_tile, tile_span_meters


def _bbox():
    return (145.310, -37.792, 145.316, -37.788)


def _synthetic_window(z=16, x=59220, y=40207, size=256):
    image = Image.new("RGB", (size, size), (150, 150, 150))
    draw = ImageDraw.Draw(image)
    draw.rectangle([60, 80, 140, 150], fill=(55, 55, 55))
    draw.ellipse([195, 55, 205, 65], fill=(60, 160, 70))
    window = ImageryWindow(
        image=image,
        z=z,
        x=x,
        y=y,
        imagery_zoom=z + 1,
        out_px=size,
        margin_px=0,
        scale=1.0,
        base_px=size,
        tile_span_meters=tile_span_meters(-37.79, z),
    )
    return window


class _FakeFetcher:
    def __init__(self, window):
        self._window = window

    def window(self, z, x, y):
        return self._window


class _FakeDepthEstimator:
    def estimate(self, image):
        array = np.asarray(image.convert("RGB"), dtype=np.float32)
        red, green, blue = array[..., 0], array[..., 1], array[..., 2]
        exg = (2.0 * green - red - blue) / (red + green + blue + 1.0)
        luminance = 0.299 * red + 0.587 * green + 0.114 * blue
        depth = np.full(array.shape[:2], 0.1, dtype=np.float32)
        depth[(exg <= 0.05) & (luminance < 120.0)] = 0.5
        depth[exg > 0.05] = 0.9
        return depth


def _tree_config(tmp_path, **overrides):
    values = dict(
        bbox=None,
        zooms=[],
        procedural=True,
        heuristic=True,
        use_osm=False,
        out_px=256,
        margin_px=0,
        min_area_m2=20.0,
        with_heights=True,
        assume_height=7.0,
        with_trees=True,
        tree_min_area_m2=6.0,
        tree_min_height_m=3.0,
        cache_dir=tmp_path / "imagery",
    )
    values.update(overrides)
    return PipelineConfig(**values)


def test_vegetation_mask_detects_canopy_not_ground():
    window = _synthetic_window()
    mask = vegetation_mask(window.image)
    assert mask[60, 200]
    assert not mask[200, 200]


def test_tree_and_building_extraction_with_shared_calibration(tmp_path):
    window = _synthetic_window()
    ctx = PipelineContext(
        fetcher=_FakeFetcher(window),
        segmenter=HeuristicBuildingSegmenter(),
        depth_estimator=_FakeDepthEstimator(),
        osm_footprints=[],
    )
    cfg = _tree_config(tmp_path)
    result = process_tile(cfg, ctx, window.z, window.x, window.y, write_preview=False)

    assert result.ml_count >= 1
    assert result.tree_count >= 1

    building_props = [feature.properties for feature in result.features]
    assert any(props.get("kind") == "building" for props in building_props)
    assert all(props.get("area_m2", 0) > 0 for props in building_props)
    assert all(props.get("roof_type") in {"flat", "hipped", "gabled"} for props in building_props)
    assert all(props.get("ridge_height", 0) >= props.get("eave_height", 0) for props in building_props)
    assert all("ridge_angle" in props for props in building_props)

    tree_props = [feature.properties for feature in result.tree_features]
    assert all(props.get("kind") == "tree" for props in tree_props)
    assert all(props["height"] >= 3.0 for props in tree_props)
    assert all(props["min_height"] >= 1.5 for props in tree_props)

    first_building = next(p for p in building_props if p.get("kind") == "building")
    first_tree = tree_props[0]
    assert first_building["height"] == 7.0
    assert first_tree["height"] > first_building["height"]


def test_trees_disabled_by_default(tmp_path):
    window = _synthetic_window()
    ctx = PipelineContext(
        fetcher=_FakeFetcher(window),
        segmenter=HeuristicBuildingSegmenter(),
        depth_estimator=_FakeDepthEstimator(),
        osm_footprints=[],
    )
    cfg = _tree_config(tmp_path, with_trees=False)
    result = process_tile(cfg, ctx, window.z, window.x, window.y, write_preview=False)
    assert result.tree_count == 0
    assert result.features


def test_offline_pipeline_end_to_end(tmp_path):
    bbox = _bbox()
    cfg = PipelineConfig(
        bbox=bbox,
        zooms=[16],
        out_path=tmp_path / "tiles.mbtiles",
        provider="google",
        cache_dir=tmp_path / "imagery",
        procedural=True,
        heuristic=True,
        use_osm=False,
        out_px=256,
        margin_px=0,
        imagery_offset=1,
        min_area_m2=10.0,
        preview_dir=tmp_path / "previews",
        preview_limit=1,
        geojson_path=tmp_path / "buildings.geojson",
        limit_tiles=1,
    )
    summary = run(cfg)

    assert summary["tiles"] == 1
    assert summary["ml_footprints"] >= 1
    assert summary["merged_footprints"] >= 1

    z = 16
    x, y = next(iter(iter_tiles(bbox, z)))
    data = read_mbtiles_tile(cfg.out_path, z, x, y)
    assert data is not None
    assert len(data) > 0

    metadata = read_mbtiles_metadata(cfg.out_path)
    assert metadata["format"] == "pbf"
    assert metadata["minzoom"] == "16"
    assert metadata["maxzoom"] == "16"
    assert "building" in metadata["json"]

    geojson = json.loads(cfg.geojson_path.read_text())
    assert geojson["type"] == "FeatureCollection"
    assert len(geojson["features"]) >= 1
    properties = geojson["features"][0]["properties"]
    assert properties["source"] in {"ml", "osm", "osm+ml"}

    previews = list((tmp_path / "previews").glob("*.png"))
    assert len(previews) == 1


def test_pipeline_preview_limit(tmp_path):
    bbox = (145.310, -37.792, 145.320, -37.788)
    cfg = PipelineConfig(
        bbox=bbox,
        zooms=[16],
        out_path=tmp_path / "tiles.mbtiles",
        procedural=True,
        heuristic=True,
        use_osm=False,
        out_px=256,
        margin_px=0,
        preview_dir=tmp_path / "previews",
        preview_limit=1,
        limit_tiles=3,
    )
    summary = run(cfg)
    assert summary["tiles"] == min(3, len(list(iter_tiles(bbox, 16))))
    assert len(list((tmp_path / "previews").glob("*.png"))) == 1


def _square(x: float, y: float, size: float = 10.0):
    from shapely.geometry import Polygon

    return Polygon(
        [
            (x - size / 2, y - size / 2),
            (x + size / 2, y - size / 2),
            (x + size / 2, y + size / 2),
            (x - size / 2, y + size / 2),
        ]
    )


def test_merge_sam3_polygons_replaces_and_adds():
    from scripts.ml.pipeline import _merge_sam3_polygons

    existing = [_square(0, 0), _square(100, 0)]
    proposals = [(_square(0.5, 0.5), 0.9), (_square(300, 300, 40.0), 0.8)]
    merged = _merge_sam3_polygons(
        existing,
        proposals,
        min_iou=0.3,
        meters_per_px=0.25,
        add_detections=True,
        add_score_threshold=0.6,
        add_min_area_m2=10.0,
        add_max_area_m2=1500.0,
    )
    assert len(merged) == 3
    assert merged[0].centroid.x == 0.5
    assert merged[1].centroid.x == 100
    assert merged[2].centroid.x == 300


def test_merge_sam3_polygons_respects_add_gate_area():
    from scripts.ml.pipeline import _merge_sam3_polygons

    proposals = [(_square(300, 300, 4.0), 0.8)]
    merged = _merge_sam3_polygons(
        [],
        proposals,
        min_iou=0.3,
        meters_per_px=0.25,
        add_detections=True,
        add_score_threshold=0.6,
        add_min_area_m2=10.0,
        add_max_area_m2=1500.0,
    )
    assert merged == []


def test_merge_sam3_polygons_keeps_low_iou_base():
    from scripts.ml.pipeline import _merge_sam3_polygons

    existing = [_square(0, 0)]
    proposals = [(_square(200, 200), 0.9)]
    merged = _merge_sam3_polygons(
        existing,
        proposals,
        min_iou=0.3,
        meters_per_px=0.25,
        add_detections=False,
        add_score_threshold=0.6,
        add_min_area_m2=10.0,
        add_max_area_m2=1500.0,
    )
    assert merged == existing
