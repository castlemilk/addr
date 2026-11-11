import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw
from scripts.ml.roof_surface import refine_roof_surface, straighten_roof_boundary
from scripts.ml.sam3 import Sam3Proposal
from scripts.ml.scene_extract import extract_objects
from shapely.affinity import rotate, translate
from shapely.geometry import LineString, Polygon

FRAME = np.array([[.1, 0], [0, -.1], [0, 10.]])


def test_roof_texture_holes_fill_but_distinct_openings_and_recesses_remain():
    ring = [[5, 5], [95, 5], [95, 95], [60, 95], [60, 75], [5, 75]]
    texture = [[15, 15], [26, 15], [26, 21], [20, 19], [21, 27], [15, 24]]
    courtyard = [[40, 30], [65, 30], [65, 55], [40, 55]]
    green = [[70, 15], [75, 16], [76, 22], [72, 25], [68, 20]]
    image = Image.new('RGB', (100, 100), (125, 120, 118))
    draw = ImageDraw.Draw(image)
    draw.polygon([tuple(p) for p in texture], fill=(106, 103, 102))
    draw.polygon([tuple(p) for p in green], fill=(35, 115, 45))
    source = Polygon(ring, [texture, courtyard, green])
    result, audit = refine_roof_surface(source, image, FRAME)
    assert len(result.interiors) == 2
    assert result.intersection(Polygon(texture)).area == Polygon(texture).area
    assert result.intersection(Polygon(courtyard)).area == 0
    assert result.intersection(Polygon(green)).area == 0
    assert result.intersection(Polygon([[10, 80], [50, 80], [50, 90], [10, 90]])).area == 0
    assert len(audit['raw_holes']) == 3 and sum(h['filled'] for h in audit['holes']) == 1
    assert len(source.interiors) == 3, 'Input predictions are immutable'


def test_large_irregular_gaps_and_dark_contrasting_gaps_are_preserved():
    large = [[10, 10], [50, 10], [50, 20], [40, 25], [50, 60], [10, 60]]
    dark = [[60, 60], [70, 62], [72, 68], [62, 70]]
    image = Image.new('RGB', (100, 100), (140, 140, 140))
    ImageDraw.Draw(image).polygon([tuple(p) for p in dark], fill=(10, 10, 10))
    result, _ = refine_roof_surface(Polygon([[0, 0], [99, 0], [99, 99], [0, 99]], [large, dark]), image, FRAME)
    assert len(result.interiors) == 2


def test_jagged_eaves_are_simplified_with_bounded_metric_shift():
    ring = [[10, 10], *[[x, 90+(1 if x % 4 else 0)] for x in range(10, 91, 2)], [90, 10]]
    source = Polygon(ring)
    result, audit = refine_roof_surface(source, Image.new('RGB', (100, 100), 'gray'), FRAME)
    assert len(result.exterior.coords) < len(source.exterior.coords) / 2
    assert audit['outer_deviation_m'] <= .25
    assert result.symmetric_difference(source).area/source.area <= .02


@pytest.mark.parametrize('reverse', [False, True])
def test_clipped_pixel_steps_become_eaves_without_erasing_wings_or_openings(reverse):
    fixture = json.loads((Path(__file__).parent/'fixtures/clipped-stepped-roof.json').read_text())
    ring = fixture['ring'][::-1] if reverse else fixture['ring']
    source = Polygon(ring, fixture['holes'])
    result, report = straighten_roof_boundary(source, (512, 512), fixture['pixel_to_meters'])
    assert 10 <= len(result.exterior.coords)-1 <= 20
    assert report['max_deviation_m'] < .45
    assert report['area_change_fraction'] <= .03
    assert [list(h.coords) for h in result.interiors] == [list(h.coords) for h in source.interiors]
    # The left edge is a cut through the image, not an eave to infer beyond it.
    crop_edge = LineString([(0, 0), (0, 512)])
    assert result.bounds[0] == 0
    assert result.boundary.intersection(crop_edge).length > 50
    assert result.convex_hull.area/result.area > 1.2, 'Keep the L and projecting front wing'


@pytest.mark.parametrize('angle', [0, 31, 87])
def test_fitted_sides_preserve_diagonals_and_large_recesses_at_any_orientation(angle):
    shape = Polygon([(20, 20), (160, 20), (180, 40), (180, 100), (100, 100),
                     (100, 180), (20, 180)])
    shape = translate(rotate(shape, angle, origin=(100, 100)), xoff=40, yoff=40)
    # Add high-frequency segmentation wobble along each side, keeping corners.
    ring = []
    for a, b in zip(list(shape.exterior.coords)[:-1], list(shape.exterior.coords)[1:], strict=True):
        a, b = np.asarray(a), np.asarray(b)
        normal = np.array([-(b-a)[1], (b-a)[0]])/np.linalg.norm(b-a)
        for i, t in enumerate(np.linspace(0, 1, 20, endpoint=False)):
            ring.append(a+t*(b-a)+normal*(.7*(-1)**i if i else 0))
    result, _ = straighten_roof_boundary(Polygon(ring), (300, 300), FRAME)
    assert len(result.exterior.coords)-1 == 7
    assert result.boundary.hausdorff_distance(shape.boundary) < 1.5
    assert result.symmetric_difference(shape).area/shape.area < .01


def test_later_large_roof_is_not_suppressed_by_an_earlier_tiny_fragment():
    small, roof = np.zeros((100, 100), dtype=bool), np.zeros((100, 100), dtype=bool)
    small[20:31, 20:31] = True  # 1.21 m²; retained by the area gate.
    roof[10:90, 10:90] = True
    class Segmenter:
        def encode_image(self, image):
            return None
        def proposals(self, *args, **kwargs):
            return [Sam3Proposal(small, .95, (20, 20, 31, 31)), Sam3Proposal(roof, .9, (10, 10, 90, 90))]
    objects, _ = extract_objects(Image.new('RGB', (100, 100), 'gray'), 'frame', FRAME, Segmenter(), kinds=['building'])
    assert len(objects) == 1 and objects[0]['model']['area_m2'] > 60
    assert not objects[0]['geometry_reviewed'] and objects[0]['status'] == 'candidate'
    raw = objects[0]['boundary_refinement']['raw_ring']
    assert objects[0]['id'] == hashlib.sha256(f'frame:building:{raw}'.encode()).hexdigest()[:16]


def test_tiny_building_speckles_are_filtered_without_changing_other_classes():
    mask = np.zeros((100, 100), dtype=bool)
    mask[10:15, 10:15] = True
    class Segmenter:
        def encode_image(self, image):
            return None
        def proposals(self, *args, **kwargs):
            return [Sam3Proposal(mask, .95, (10, 10, 15, 15))]
    objects, scans = extract_objects(Image.new('RGB', (100, 100), 'gray'), 'frame', FRAME, Segmenter(), kinds=['building', 'solar_panel'])
    assert scans['building']['count'] == 0
    assert len(objects) == 1 and objects[0]['kind'] == 'solar_panel'
    assert 'boundary_refinement' not in objects[0]


def test_large_roof_sections_collapse_but_adjacent_buildings_and_courtyard_roofs_remain():
    whole = np.zeros((150, 150), dtype=bool)
    whole[10:90, 10:90] = True
    whole[30:60, 30:60] = False  # Genuine regular courtyard.
    section = np.zeros_like(whole)
    section[60:90, 10:90] = True  # More than a third of the roof, same instance.
    adjacent = np.zeros_like(whole)
    adjacent[10:90, 92:140] = True
    inner = np.zeros_like(whole)
    inner[35:55, 35:55] = True  # Separate small roof in the courtyard.

    class Segmenter:
        def encode_image(self, image):
            return None

        def proposals(self, *args, **kwargs):
            return [Sam3Proposal(mask, .9, (0, 0, 150, 150)) for mask in [section, whole, adjacent, inner]]

    objects, scans = extract_objects(Image.new('RGB', (150, 150), 'gray'), 'frame', FRAME, Segmenter(), kinds=['building'])
    assert scans['building']['count'] == len(objects) == 3
    assert sorted(round(o['model']['area_m2']) for o in objects) == [4, 38, 55]
    assert len(next(o for o in objects if o['model']['area_m2'] > 50)['holes']) == 1


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('angle', [0, 29])
def test_attached_roof_corner_cannot_force_long_eaves_back_to_pixel_steps(reverse, angle):
    fixture = json.loads((Path(__file__).parent/'fixtures/attached-stepped-roof.json').read_text())
    source = rotate(Polygon(fixture['ring'], fixture['holes']), angle)
    if reverse:
        source = Polygon(list(source.exterior.coords)[::-1], [h.coords for h in source.interiors])
    result, report = straighten_roof_boundary(source, fixture['image_size'], fixture['pixel_to_meters'])
    assert report['method'] == 'bounded-shared-eaves'
    assert len(result.exterior.coords)-1 <= 25
    assert report['max_deviation_m'] <= report['deviation_limit_m'] <= .45
    assert report['area_change_fraction'] <= .03
    assert [list(h.coords) for h in result.interiors] == [list(h.coords) for h in source.interiors]
    frame = np.asarray(fixture['pixel_to_meters'])
    metric = np.asarray(result.exterior.coords) @ frame[:2]+frame[2]
    assert sum(np.linalg.norm(np.diff(metric, axis=0), axis=1) > 10) >= 3, 'Keep long straight eave runs'
    assert result.convex_hull.area/result.area > 1.3, 'Keep the garage, L wing and projecting gable'


def test_unstable_shared_eave_fit_retains_bounded_legacy_alternative(monkeypatch):
    from scripts.ml import roof_surface
    def fail(*args, **kwargs):
        raise ValueError('Unstable fit')
    monkeypatch.setattr(roof_surface, 'least_squares', fail)
    fixture = json.loads((Path(__file__).parent/'fixtures/attached-stepped-roof.json').read_text())
    source = Polygon(fixture['ring'], fixture['holes'])
    result, report = straighten_roof_boundary(source, fixture['image_size'], fixture['pixel_to_meters'])
    assert result.is_valid and report['method'] != 'bounded-shared-eaves'
    assert report['max_deviation_m'] <= .45 and report['area_change_fraction'] <= .03
