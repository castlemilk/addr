import copy
import hashlib
import json
from types import SimpleNamespace

import cv2
import numpy as np
from PIL import Image
from scripts.ml.imagery_dates import acquisition
from scripts.ml.scene_temporal import start, status
from scripts.ml.temporal_evidence import match_objects, reflect_objects, register, stable_composite
from shapely.geometry import Polygon


def textured(seed=7):
    rng = np.random.default_rng(seed)
    a = rng.integers(0, 256, (512, 512, 3), dtype=np.uint8)
    return cv2.GaussianBlur(a, (3, 3), .6)


def test_registration_checks_alignment_and_warp_borders():
    base = textured()
    source = cv2.warpAffine(base, np.float32([[1, 0, 5], [0, 1, -3]]), (512, 512))
    aligned, valid, report = register(Image.fromarray(base), Image.fromarray(source))
    assert report['status'] == 'accepted'
    assert report['check_inliers'] >= 10 and report['check_coverage'] >= .2
    assert np.allclose(np.array(report['transform'])[:, 2], [-5, 3], atol=.15)
    assert not valid[-1].all() and valid.mean() > .6
    assert np.abs(np.asarray(aligned)[valid].astype(float)-base[valid]).mean() < 3


def test_duplicate_and_unrelated_images_cannot_supply_evidence():
    image = Image.fromarray(textured())
    assert register(image, image)[2]['status'] == 'duplicate'
    assert register(image, Image.fromarray(textured(77)))[2]['status'] == 'rejected'
    assert register(image, Image.new('RGB', (128, 128)))[2]['status'] == 'rejected'


def test_composite_preserves_changed_occluded_and_unregistered_pixels():
    base = textured()
    alternative = np.clip(base.astype(int)+3, 0, 255).astype(np.uint8)
    alternative[90:160, 90:160] = [0, 200, 0]  # changed canopy, not a roof gap to fill
    valid = np.ones(base.shape[:2], bool)
    valid[:25] = False
    combined, used, report = stable_composite(Image.fromarray(base), [Image.fromarray(alternative)], [valid])
    assert used.any() and not used[100:150, 100:150].any()
    assert not used[:25].any()
    assert np.array_equal(np.asarray(combined)[~used], base[~used])
    assert not report['resolution_increased'] and not report['hidden_pixels_inpainted']


def object_(identity='a', kind='building', dx=0, holes=None):
    return {'id': identity, 'kind': kind, 'ring': [[10+dx, 10], [40+dx, 10], [40+dx, 40], [10+dx, 40]],
            'holes': holes or [], 'status': 'candidate', 'model': {'height_m': 3}, 'geometry_reviewed': False}


def captures(objects):
    return [{'objects': objects, 'valid': np.ones((64, 64), bool), 'acquisition_date': date, 'acquisition_id': date}
            for date in ('2023-01-01', '2025-01-01')]


def test_roof_consensus_retains_courtyard_and_never_mutates_sources():
    hole = [[[20, 20], [25, 20], [25, 25], [20, 25]]]
    original = object_(holes=hole)
    other = object_('b', dx=.3, holes=hole)
    before = copy.deepcopy(original)
    rows, proposals = reflect_objects([original], captures([other]), (64, 64), .2)
    assert rows[0]['status'] == 'consistent' and len(proposals) == 1
    candidate = proposals[0]
    assert len(candidate['holes']) == 1
    assert Polygon(candidate['ring'], candidate['holes']).intersection(Polygon(hole[0])).area == 0
    assert not candidate['geometry_reviewed'] and original == before


def test_trees_do_not_merge_across_dates_and_missing_views_do_not_vote():
    tree = object_(kind='tree')
    views = captures([object_('other', kind='tree')])
    views[1]['objects'] = []
    rows, proposed = reflect_objects([tree], views, (64, 64), .2)
    assert not proposed and rows[0]['status'] == 'needs_review'
    assert rows[0]['geometry_policy'] == 'separate_per_date'
    assert rows[0]['observations'][1]['iou'] is None


def test_duplicate_acquisition_or_poor_local_registration_cannot_confirm_roof():
    original = object_()
    views = captures([object_('b')])
    views[1]['acquisition_id'] = views[0]['acquisition_id']
    assert reflect_objects([original], views, (64, 64), .2)[0][0]['status'] == 'needs_review'
    views = captures([object_('b')])
    views[1]['valid'][:] = False
    assert not reflect_objects([original], views, (64, 64), .2)[1]


def test_shared_roof_split_is_ambiguous_and_large_changes_are_held():
    a = object_()
    b, c = object_('b'), object_('c')
    b['ring'] = [[10, 10], [25, 10], [25, 40], [10, 40]]
    c['ring'] = [[25, 10], [40, 10], [40, 40], [25, 40]]
    assert match_objects([a], [b, c], 'building')['a']['ambiguous']
    assert not reflect_objects([a], captures([b, c]), (64, 64), .2)[1]
    assert not reflect_objects([a], captures([object_('b', dx=3)]), (64, 64), .5)[1]


def test_acquisition_is_not_release_date_and_resolution_changes_are_not_new_flights():
    def metadata(stamp=1735689600000, resolution=.3):
        return {'attributes': {'SRC_DATE2': stamp, 'SRC_DESC': 'camera', 'NICE_DESC': 'provider', 'SRC_RES': resolution, 'SRC_ACC': 5}}
    a, b = acquisition([metadata()]), acquisition([metadata(resolution=.4)])
    assert a['acquisition_date'] == '2025-01-01'
    assert a['acquisition_id'] == b['acquisition_id']
    assert acquisition([metadata(), metadata(1704067200000)]) is None
    assert acquisition([]) is None


def test_start_is_idempotent_and_preserves_source_and_review(tmp_path, monkeypatch):
    directory = tmp_path/'data/scenes/test-scene'
    directory.mkdir(parents=True)
    image = Image.fromarray(textured())
    image.save(directory/'image.png')
    data = (directory/'image.png').read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    (directory/f'{digest}.png').write_bytes(data)
    source = {'id': 'test-scene', 'title': 'Test scene', 'image_sha256': digest, 'image_width': 512, 'image_height': 512,
              'region': {'bounds': [145.3, -37.8, 145.31, -37.79]}}
    raw = json.dumps(source).encode()
    (directory/'latest.json').write_bytes(raw)
    (directory/'review.json').write_text('protected review')
    calls = []
    monkeypatch.setattr('scripts.ml.scene_temporal.subprocess.Popen', lambda *a, **k: calls.append((a, k)) or SimpleNamespace(pid=123))
    first = start(tmp_path, 'test-scene')
    second = start(tmp_path, 'test-scene')
    assert first['run'] == second['run'] and len(calls) == 1
    assert (directory/'latest.json').read_bytes() == raw
    assert (directory/'review.json').read_text() == 'protected review'
    assert not status(tmp_path, first['run'])['stale']
    (directory/'latest.json').write_text('{}')
    assert status(tmp_path, first['run'])['stale']
