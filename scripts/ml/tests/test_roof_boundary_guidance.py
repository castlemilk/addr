import copy

import numpy as np
from PIL import Image, ImageDraw
from scripts.ml import roof_boundary_guidance as guidance
from shapely.geometry import Polygon, box


def inputs(ring, *, holes=(), trees=()):
    obj = {'id': 'roof', 'kind': 'building', 'source': 'sam3-text-onnx', 'ring': ring,
           'holes': list(holes), 'boundary_refinement': {'raw_ring': ring, 'raw_holes': list(holes)},
           'model': {'height_m': 4.}}
    scene = {'pixel_to_meters': [[.2, 0], [0, -.2], [0, 48.]], 'objects': [obj, *trees]}
    return obj, scene


def test_coloured_on_roof_shadow_cannot_establish_material_exit():
    rgb = np.zeros((240, 240, 3), dtype=np.uint8)+40
    colour = np.array([210, 140, 90])
    rgb[40:181, 40:181] = colour
    rgb[40:181, 174:181] = colour*.3
    polygon = box(40, 40, 181, 181)
    best, _, _ = guidance._profile(np.array([181., 40.]), np.array([181., 181.]), rgb, polygon, Polygon(), 8.)
    assert best['anchor_count'] >= 3
    assert best['offset_pixels'] < -4
    assert best['material_exit_share'] == 0, 'Illumination scales coloured RGB; it does not change material.'


def test_off_roof_dark_band_is_only_an_unaccepted_comparison_and_holes_stay_exact():
    image = Image.new('RGB', (240, 240), (100, 65, 30))
    draw = ImageDraw.Draw(image)
    draw.rectangle((34, 34, 186, 186), fill=(65, 65, 65))
    draw.rectangle((40, 40, 180, 180), fill=(220, 220, 220))
    hole = [[90, 90], [110, 90], [110, 110], [90, 110]]
    draw.polygon(hole, fill=(100, 65, 30))
    obj, scene = inputs(list(map(list, box(34, 34, 186, 186).exterior.coords[:-1])), holes=[hole])
    before = copy.deepcopy(obj)
    candidate, report = guidance.boundary_candidate(obj, scene, image)
    assert report['changed'] and not report['accepted']
    assert report['reason'] == 'ambiguous-brightness-edge-comparison'
    assert candidate['holes'] == [hole] and obj == before
    assert report['photometry_source'] == 'original_rgb_only'
    assert Polygon(candidate['ring']).area < Polygon(obj['ring']).area
    assert report['max_visible_displacement_m'] <= 1.6


def test_green_exterior_is_available_material_evidence_but_not_a_tree_label():
    rgb = np.zeros((240, 240, 3), dtype=np.uint8)+np.array([40, 110, 40], dtype=np.uint8)
    rgb[40:181, 40:181] = 220
    best, _, _ = guidance._profile(np.array([181., 40.]), np.array([181., 181.]), rgb,
                                   box(40, 40, 181, 181), Polygon(), 8.)
    assert best['available_samples'] >= 3 and best['anchor_count'] >= 3
    assert best['material_exit_share'] > .9 and best['green_exterior_share'] > .9


def test_enhanced_hallucinated_edge_cannot_validate_uniform_original(monkeypatch):
    image = Image.new('RGB', (240, 240), (180, 180, 180))
    obj, scene = inputs(list(map(list, box(40, 40, 180, 180).exterior.coords[:-1])))
    nominated = np.array([[174., 40.], [174., 180.]])
    monkeypatch.setattr(guidance, '_discovery_lines', lambda _: [nominated])
    candidate, report = guidance.boundary_candidate(obj, scene, image, upscale={'status': 'available'})
    assert candidate == obj and not report['changed'] and not report['accepted']
    assert report['enhanced_discovery_segments'] == 1
    _, cached = guidance.boundary_candidate(obj, scene, image, upscale={'status': 'available', 'cache_hit': True})
    _, fresh = guidance.boundary_candidate(obj, scene, image, upscale={'status': 'available', 'cache_hit': False})
    assert cached == fresh and 'cache_hit' not in cached['upscale']


def test_crop_and_entirely_hidden_boundary_remain_unchanged():
    image = Image.new('RGB', (240, 240), (40, 110, 40))
    for shape in (box(0, 30, 180, 180), box(30, 30, 180, 180)):
        obj, scene = inputs(list(map(list, shape.exterior.coords[:-1])))
        candidate, report = guidance.boundary_candidate(obj, scene, image)
        assert candidate == obj and not report['changed'] and not report['accepted']


def test_partial_canopy_retains_inferred_continuation_and_visible_recess():
    ring = [[30, 30], [190, 30], [190, 65], [181, 80], [176, 100],
            [182, 112], [190, 130], [190, 170], [100, 170], [100, 200], [30, 200]]
    tree = {'id': 'tree', 'kind': 'tree', 'source': 'sam3-text-onnx',
            'ring': list(map(list, box(170, 60, 210, 135).exterior.coords[:-1]))}
    obj, scene = inputs(ring, trees=[tree])
    scene['pixel_to_meters'] = [[.1, 0], [0, -.1], [0, 24.]]
    image = Image.new('RGB', (240, 240), (110, 70, 40))
    draw = ImageDraw.Draw(image)
    draw.polygon([(30, 30), (190, 30), (190, 170), (100, 170), (100, 200), (30, 200)], fill=(220, 220, 220))
    draw.rectangle((170, 60, 210, 135), fill=(40, 110, 40))
    candidate, report = guidance.boundary_candidate(obj, scene, image)
    assert report['changed'] and report['inferred_edges']
    shape = Polygon(candidate['ring'])
    assert shape.convex_hull.area/shape.area > 1.04
    assert all(edge['status'] == 'inferred_continuation_not_observed' for edge in report['inferred_edges'])
