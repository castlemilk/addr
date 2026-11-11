import json

import numpy as np
import pytest
from PIL import Image
from scripts.ml.scene_extract import save_scene
from scripts.ml.scene_region import capture_region, corrected_roofs, region_frame
from scripts.ml.tiles import WORLD_SPAN_M, lonlat_to_tile_xy

BOUNDS = [145.3121, -37.7908, 145.313, -37.7900]


def test_region_frame_preserves_north_and_metric_round_trip():
    scene = region_frame(BOUNDS)
    assert scene['id'] == region_frame(BOUNDS)['id']
    assert scene['id'] != region_frame(BOUNDS, 'esri')['id']
    affine, inverse = np.array(scene['pixel_to_meters']), np.array(scene['meters_to_pixel'])
    points = np.array([[0, 0], [256, 256], [512, 512]])
    local = np.c_[points, np.ones(3)] @ affine
    assert local[0, 0] < 0 < local[2, 0]
    assert local[0, 1] > 0 > local[2, 1]
    assert np.allclose(np.c_[local, np.ones(3)] @ inverse, points)
    west, south, east, north = scene['region']['bounds']
    a, b = np.array(lonlat_to_tile_xy(west, north, 0)), np.array(lonlat_to_tile_xy(east, south, 0))
    assert abs((b[0]-a[0])-(b[1]-a[1])) < 1e-12
    actual = (b[0]-a[0])*WORLD_SPAN_M*np.cos(np.radians(scene['centroid']['lat']))
    assert abs(actual-scene['region']['span_m']) < .001


@pytest.mark.parametrize('bounds', [[0, 0, 1, 1], [145, -37, 145, -36], [0, 89, .01, 89.001], [0, 0, .000001, .000001], [0, 0, float('nan'), 1]])
def test_region_rejects_invalid_or_excessive_area(bounds):
    with pytest.raises(ValueError):
        region_frame(bounds)


@pytest.mark.parametrize('size', [10, 250])
@pytest.mark.parametrize('lat', [-37.79044, 0, 75])
def test_region_accepts_advertised_width_limits_after_coordinate_rounding(size, lat):
    north = size / 2 / (WORLD_SPAN_M / 360)
    east = north / np.cos(np.radians(lat))
    scene = region_frame([145.31252-east, lat-north, 145.31252+east, lat+north])
    assert abs(scene['region']['span_m']-size) < .02


def test_capture_requires_complete_imagery_and_preserves_reviews(tmp_path):
    class Fetcher:
        def _child(self, z, x, y):
            return Image.new('RGB', (256, 256), (80, 100, 120))
    scene, image, data = capture_region(BOUNDS, root=tmp_path, fetcher=Fetcher())
    assert image.size == (512, 512)
    assert image.getpixel((0, 0)) == (80, 100, 120)
    saved = save_scene(scene, data, tmp_path)
    folder = tmp_path/'data/scenes'/scene['id']
    (folder/'review.json').write_text('{"revision":3}')
    assert save_scene(scene, data, tmp_path)['model_signature'] == saved['model_signature']
    assert json.loads((folder/'review.json').read_text())['revision'] == 3
    class Missing:
        def _child(self, *args):
            return None
    with pytest.raises(ValueError, match='unavailable'):
        capture_region(BOUNDS, root=tmp_path, fetcher=Missing())


def test_corrected_roof_keeps_shape_and_requires_new_image_review(tmp_path):
    frame = region_frame(BOUNDS)
    (tmp_path/'public').mkdir()
    record = {'address_id': 1, 'full_address': 'Test roof', 'boundary_source': 'crm', 'centroid': frame['centroid'],
              'ring_meters': [[-5, -5], [5, -5], [5, 5], [-5, 5]], 'roof_type': 'gabled', 'eave_height': 3,
              'rooflines': {'structure_audit': {'passed': True}, 'parts': [{'ring': [[-5, -5], [5, -5], [5, 0], [-5, 0]], 'z': [3, 3, 5, 5]}]}}
    (tmp_path/'public/address-roofs.json').write_text(json.dumps([record, record]))
    objects = corrected_roofs(frame, Image.new('RGB', (512, 512)), root=tmp_path)
    assert len(objects) == 1
    assert objects[0]['roof_type'] == 'gabled'
    assert objects[0]['roof_parts'][0]['ring_z'] == [3, 3, 5, 5]
    assert objects[0]['status'] == 'candidate' and not objects[0]['geometry_reviewed']
    assert objects[0]['model']['height_verified'] is False
