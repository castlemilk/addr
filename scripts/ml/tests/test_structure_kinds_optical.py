"""Optical stages: frame-to-tile mapping, deskew, watermark filtering, shadow heights, vehicle/OCR payload shape."""
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest
from scripts.ml import structure_kinds as sk
from scripts.ml import structure_kinds_optical as opt
from scripts.ml.tiles import tile_bounds

sys.path.insert(0, str(Path(__file__).parent))
from test_structure_kinds import build_scene  # noqa: E402


def test_frame_mapping_places_the_analysis_tile_corner_on_the_margin():
    z, x, y = 17, 118440, 80408
    west, south, east, north = tile_bounds(x, y, z)
    # a 1152 px scene = 1024 px tile + 64 px margin each side, as saved by the pipeline
    span_x, span_y = (east-west)*64/1024, (north-south)*64/1024
    scene = {'analysis_tile': [z, x, y], 'image_width': 1152, 'image_height': 1152,
             'region': {'bounds': [west-span_x, south-span_y, east+span_x, north+span_y]}}
    cx, cy = opt.tile_corner_px(scene)
    assert abs(cx-64) < .5 and abs(cy-64) < .5
    tx, ty = opt.pixel_to_tilexy(scene, 64, 64, 20)
    assert abs(tx-x*8) < .01 and abs(ty-y*8) < .01


def test_frame_mapping_works_without_an_analysis_tile():
    west, south, east, north = tile_bounds(118440, 80408, 17)
    scene = {'image_width': 100, 'image_height': 100, 'region': {'bounds': [west, south, east, north]}}
    assert opt.tile_corner_px(scene) is None
    tx, _ = opt.pixel_to_tilexy(scene, 0, 0, 17)
    assert abs(tx-118440) < 1e-6


def test_deskew_makes_the_long_axis_horizontal():
    image = np.zeros((200, 200, 3), np.uint8)
    ring = np.array([[60, 40], [140, 40], [140, 60], [60, 60]], float)   # 80 x 20 box, axis-aligned
    theta = math.radians(35)
    c = np.array([100, 100])
    rot = np.array([[math.cos(theta), -math.sin(theta)], [math.sin(theta), math.cos(theta)]])
    ring_r = (ring-[100, 50]) @ rot.T+c
    cv2.fillPoly(image, [ring_r.round().astype(np.int32)], (200, 200, 200))
    out = opt.deskew(image, ring_r, 35., pad_px=10, upscale=2)
    pts = out['ring']
    width, height = np.ptp(pts[:, 0]), np.ptp(pts[:, 1])
    assert width > 3*height and out['upscale'] == 2


@pytest.mark.parametrize('text,expected', [('Google', True), ('coonle', True), ('©2026 Google', True), ('2026', True),
                                           ('888', True), ('SMITHS', False), ('ENSY', False), ('ACME STORAGE', False)])
def test_watermark_filter(text, expected):
    assert opt.watermark_like(text) is expected


def test_relative_heights_use_primary_roofs_as_reference():
    lengths = {'house1': 3., 'house2': 3.2, 'house3': 2.8, 'shed': 1.5, 'tall': 6., 'none': None}
    areas = {'house1': 200, 'house2': 180, 'house3': 150, 'shed': 20, 'tall': 40, 'none': 30}
    out = opt.relative_heights(lengths, areas)
    assert out['shed']['height_estimate_m'] == pytest.approx(1.5, abs=.01)
    assert out['tall']['height_estimate_m'] == pytest.approx(6., abs=.01)
    assert out['none']['height_estimate_m'] is None and out['house1']['reference_shadow_m'] == 3.


def test_relative_heights_without_reference_are_none():
    out = opt.relative_heights({'a': 1., 'b': 2.}, {'a': 10, 'b': 20})
    assert all(v['height_estimate_m'] is None for v in out.values())


def test_shadow_direction_and_lengths_on_a_synthetic_lit_scene():
    scene, canvas, _ = build_scene()
    shapes = sk.building_shapes(scene)
    frame = (scene['image_height'], scene['image_width'])
    px_m = sk.pixel_size_m(scene)
    objects, mask_all = opt._object_rasters(scene, shapes, frame)
    lum = np.full(frame, 150., np.float32)
    # paint a 2 m shadow band south (image +y) of every roof, and darker roofs stay bright
    for o in objects.values():
        ys, xs = np.nonzero(o['mask'])
        for dy in range(1, int(2/px_m)+1):
            yy = np.clip(ys+dy, 0, frame[0]-1)
            lum[yy, xs] = np.minimum(lum[yy, xs], 90.)
    for o in objects.values():
        lum[o['mask']] = 170.
    direction, drop = opt.shadow_direction(lum, list(objects.values()), mask_all, px_m)
    assert abs(((direction-90)+180) % 360-180) <= 25 and drop > 20
    lengths = opt.shadow_lengths(lum, objects, mask_all, 90., px_m)
    house = lengths['house']
    assert house is not None and 1.4 <= house <= 2.6


def test_assess_scene_payload_shape_without_optional_stages(tmp_path):
    scene, canvas, _ = build_scene()
    rows = opt.assess_scene(scene, canvas, ['shed', 'container'])
    assert {r['object_id'] for r in rows} == {'shed', 'container'}
    for r in rows:
        p = r['payload']
        assert r['version'] == opt.VERSION and p['ocr'] is None and p['vehicles'] is None and p['hires'] is None
        assert 'deskew_angle_deg' in p['stages'] and 'height' in p
        json.dumps(p)
