import copy

import numpy as np
from PIL import Image
from scripts.ml.roof_assembly import assemble_roof_candidates
from scripts.ml.roof_structure import audit_roof
from scripts.ml.sam3 import Sam3Proposal
from scripts.ml.scene_extract import extract_objects
from scripts.ml.scene_roof import propose_roof
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

FRAME = np.array([[.1, 0], [0, -.1], [0, 16.]])
IMAGE = Image.new('RGB', (180, 160), (140, 110, 100))


def candidate(p, identity, score=.9):
    return p, score, identity, {}


def test_attached_sections_form_one_surface_with_retained_evidence_and_shared_planes():
    main, garage = box(20, 20, 100, 80), box(99, 35, 140, 70)
    candidates = [candidate(main, 'main'), candidate(garage, 'garage', .8)]
    joined, score, identity, metadata = assemble_roof_candidates(candidates, IMAGE, FRAME)[0]
    assert joined.geom_type == 'Polygon' and not joined.interiors
    assert joined.symmetric_difference(main.union(garage)).area < 1e-7
    assert score == .8 and len(metadata['assembly']['components']) == 2
    assert metadata['assembly']['junctions'][0]['contact_length_m'] >= 1.5
    assert identity == assemble_roof_candidates(candidates[::-1], IMAGE, FRAME)[0][2]
    ring = np.asarray(joined.exterior.coords[:-1]) @ FRAME[:2] + FRAME[2]
    roof = propose_roof(ring.tolist(), 3.2, 2., 'auto', image=IMAGE,
                        meters_to_pixel=[[10, 0], [0, -10], [0, 160]])
    assert audit_roof(roof['parts'], Polygon(ring), 3.2)['passed']
    surface = unary_union([Polygon(p['ring_meters']) for p in roof['parts']])
    assert surface.geom_type == 'Polygon' and surface.symmetric_difference(Polygon(ring)).area < 1e-6


def test_detached_point_touch_corner_overlap_and_contrasting_material_stay_separate():
    main = box(20, 20, 100, 80)
    for other in [box(102, 35, 140, 70), box(100, 80, 140, 120), box(99, 79, 140, 120)]:
        assert len(assemble_roof_candidates([candidate(main, 'main'), candidate(other, 'other')], IMAGE, FRAME)) == 2
    pixels = np.asarray(IMAGE).copy()
    pixels[:, 100:] = [20, 150, 240]
    assert len(assemble_roof_candidates([candidate(main, 'main'), candidate(box(99, 35, 140, 70), 'garage')],
                                        Image.fromarray(pixels), FRAME)) == 2


def test_real_courtyard_and_duplicate_masks_are_not_filled_or_assembled():
    hole = box(40, 35, 60, 60)
    main = Polygon(box(20, 20, 100, 80).exterior, [hole.exterior])
    joined, *_ = assemble_roof_candidates([candidate(main, 'main'), candidate(box(99, 35, 140, 70), 'garage')], IMAGE, FRAME)[0]
    assert len(joined.interiors) == 1 and joined.intersection(hole).area == 0
    assert len(assemble_roof_candidates([candidate(main, 'main'), candidate(main, 'duplicate')], IMAGE, FRAME)) == 2


def test_fresh_whole_roof_replaces_clipped_model_seed_without_reading_review_or_mutating_inputs():
    masks = []
    for x1, y1, x2, y2 in [(20, 20, 100, 80), (99, 35, 140, 70)]:
        mask = np.zeros((160, 180), dtype=bool)
        mask[y1:y2, x1:x2] = True
        masks.append(Sam3Proposal(mask, .9, (x1, y1, x2, y2)))
    class Detector:
        def encode_image(self, image):
            return None

        def proposals(self, *args, **kwargs):
            return masks
    seed = {'id': 'clipped', 'kind': 'building', 'source': 'sam3-text-onnx',
            'ring': [[50, 20], [100, 20], [100, 80], [50, 80]], 'geometry_reviewed': False}
    child = {'id': 'panel', 'kind': 'solar_panel', 'parent_id': 'clipped', 'group_id': 'clipped'}
    original = copy.deepcopy([seed, child])
    objects, scans = extract_objects(IMAGE, 'image', FRAME, Detector(), objects=[seed, child], kinds=['building'])
    assert [seed, child] == original
    assert len(objects) == 2 and scans['building']['count'] == 1
    roof = next(o for o in objects if o['kind'] == 'building')
    assert roof['source'] == 'sam3-text-onnx' and roof['status'] == 'candidate'
    assert not roof['geometry_reviewed'] and not roof.get('roof_line_edits')
    assert next(o for o in objects if o['id'] == 'panel')['parent_id'] == roof['id']
    assert Polygon(roof['ring']).area > 6000
