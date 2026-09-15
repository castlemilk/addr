import numpy as np
import pytest
from scripts.ml.imagery import ImageryFetcher, procedural_tile
from scripts.ml.segmentation import (
    HeuristicBuildingSegmenter,
    binary_close,
    iter_polygons,
    mask_to_polygons,
    polygons_to_local,
)


def _rect_mask(width=128, height=128, x0=16, y0=16, x1=96, y1=96):
    mask = np.zeros((height, width), dtype=bool)
    mask[y0:y1, x0:x1] = True
    return mask


def test_mask_to_polygons_rectangle():
    polygons = mask_to_polygons(_rect_mask(), min_area_px=10, simplify_px=0)
    assert len(polygons) == 1
    assert polygons[0].area == pytest.approx(80 * 80, rel=1e-6)
    min_x, min_y, max_x, max_y = polygons[0].bounds
    assert (min_x, min_y, max_x, max_y) == pytest.approx((16, 16, 96, 96))


def test_mask_to_polygons_min_area_filters_small():
    mask = np.zeros((64, 64), dtype=bool)
    mask[4:8, 4:8] = True
    mask[40:60, 40:60] = True
    polygons = mask_to_polygons(mask, min_area_px=100, simplify_px=0)
    assert len(polygons) == 1


def test_mask_to_polygons_preserves_hole():
    mask = _rect_mask()
    mask[40:70, 40:70] = False
    polygons = mask_to_polygons(mask, min_area_px=10, simplify_px=0)
    assert len(polygons) == 1
    assert len(polygons[0].interiors) == 1
    total_area = sum(part.area for part in iter_polygons(polygons[0]))
    assert total_area == pytest.approx(80 * 80 - 30 * 30, rel=1e-6)


def test_mask_to_polygons_handles_empty():
    assert mask_to_polygons(np.zeros((32, 32), dtype=bool)) == []


def test_binary_close_fills_pinholes():
    mask = _rect_mask()
    mask[50, 50] = False
    closed = binary_close(mask, iterations=1)
    assert closed[50, 50]


def test_heuristic_segmenter_finds_building_on_procedural_image():
    image = procedural_tile(16, 30000, 20000, size=256)
    segmenter = HeuristicBuildingSegmenter()
    mask = segmenter.segment(image)
    assert mask.any()
    polygons = mask_to_polygons(mask, min_area_px=100, simplify_px=0)
    assert len(polygons) >= 1
    assert max(polygon.area for polygon in polygons) > 1000


def test_polygons_to_local_scales_and_clips():
    polygon = mask_to_polygons(_rect_mask(), min_area_px=10, simplify_px=0)[0]
    local = polygons_to_local(
        [polygon], lambda px, py: (px * 8.0, py * 8.0), clip_buffer=64
    )
    assert len(local) == 1
    min_x, min_y, max_x, max_y = local[0].bounds
    assert (min_x, min_y, max_x, max_y) == pytest.approx((128, 128, 768, 768))


def test_procedural_window_is_deterministic():
    fetcher = ImageryFetcher(procedural=True, out_px=256, margin_px=0)
    window = fetcher.window(16, 30000, 20000)
    again = fetcher.window(16, 30000, 20000)
    assert np.array_equal(np.asarray(window.image), np.asarray(again.image))


def test_window_pixel_local_transform_identity_scale():
    fetcher = ImageryFetcher(procedural=True, out_px=512, margin_px=0)
    window = fetcher.window(16, 30000, 20000)
    assert window.scale == pytest.approx(1.0)
    assert window.pixel_to_local(512, 256) == pytest.approx((4096.0, 2048.0))
    assert window.local_to_pixel(4096, 2048) == pytest.approx((512.0, 256.0))


def test_window_margin_transform():
    fetcher = ImageryFetcher(procedural=True, out_px=512, margin_px=32)
    window = fetcher.window(16, 30000, 20000)
    assert window.pixel_to_local(32, 32) == pytest.approx((0.0, 0.0))
    assert window.local_to_pixel(0, 0) == pytest.approx((32.0, 32.0))
