import copy
import json
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw
from scripts.ml.roof_block_context import rings
from scripts.ml.roof_lot_review import (
    allocate_lot,
    cache_key,
    detect_lot,
    geometry,
    lot_review,
    pixel_scale,
    searches,
    validate_checkpoint,
)
from shapely.geometry import Polygon, box


@pytest.fixture
def site():
    scene = {'id': 's', 'model_signature': 'm', 'image_sha256': 'im', 'image_width': 200, 'image_height': 200,
             'pixel_to_meters': [[.5, 0], [0, -.5], [-50, 50]], 'objects': []}
    lots = [{'pfi': str(i), 'lot': str(i), 'polygons': rings(box(30, 30+(i-1)*50, 80, 80+(i-1)*50)),
             'horizontal_uncertainty_m': 1.5} for i in (1, 2)]
    lots.append({'pfi': 'cm', 'lot': 'CM1', 'polygons': rings(box(80, 30, 90, 130)), 'horizontal_uncertainty_m': 1.5})
    block = {'lots': lots, 'members': []}
    image = Image.new('RGB', (200, 200), '#38432a')
    ImageDraw.Draw(image).rectangle((35, 40, 70, 120), fill='white')
    return scene, block, image


def test_numbered_lots_are_exclusive_and_leave_driveway_empty(site):
    scene, block, _ = site
    planned = searches(scene, block)
    assert [p['lot'] for p in planned] == ['1', '2']
    first, second = [geometry(p['core']) for p in planned]
    assert first.intersection(second).area == 0
    assert first.union(second).intersection(box(80, 30, 90, 130)).area == 0
    assert all(p['crop'][2]-p['crop'][0] == p['crop'][3]-p['crop'][1] for p in planned)


def test_completely_overlapping_strata_retains_readable_partial_results(site, tmp_path):
    scene, block, image = site
    block['lots'][1]['polygons'] = copy.deepcopy(block['lots'][0]['polygons'])
    payload = observations(*site)
    result = lot_review(tmp_path, scene, block, image, observations=payload)
    assert result['status'] == 'partial'
    assert all(row['status'] == 'needs_context' and row['core'] == [] for row in result['lots'])
    assert rings(Polygon()) == []


def test_segmentation_does_not_fill_yards_or_parcel_holes(site):
    scene, block, image = site
    hole = box(45, 55, 50, 60)
    block['lots'][0]['polygons'] = rings(Polygon(box(30, 30, 80, 80).exterior, [hole.exterior]))
    search = searches(scene, block)[0]
    result = allocate_lot(scene, image, search, [{'score': .9, 'polygons': rings(box(35, 40, 70, 86))}])
    output = geometry(result['polygons'])
    assert output.difference(geometry(search['core'])).area < 1e-8
    assert output.intersection(hole).area == 0
    assert output.intersection(box(30, 30, 80, 39)).area == 0
    assert result['allocation_edges'] and result['roof_edges']
    assert result['roof_family'] == 'unresolved' and not result['eligible']


def test_no_roof_detection_never_falls_back_to_parcel(site):
    scene, block, image = site
    result = allocate_lot(scene, image, searches(scene, block)[0], [])
    assert result['status'] == 'no_roof_evidence' and not result['polygons']


def test_empty_filtered_masks_do_not_discard_valid_roof_proposals(site):
    scene, block, image = site
    empty = {'score': .99, 'polygons': []}
    valid = {'score': .9, 'polygons': rings(box(35, 40, 70, 75))}
    result = allocate_lot(scene, image, searches(scene, block)[0], [empty, valid])
    assert result['status'] == 'needs_review' and geometry(result['polygons']).area > 0
    assert allocate_lot(scene, image, searches(scene, block)[0], [empty])['status'] == 'no_roof_evidence'


def test_fixture_and_remote_roof_masks_are_not_unit_roofs(site):
    scene, block, image = site
    result = allocate_lot(scene, image, searches(scene, block)[0], [
        {'score': .99, 'polygons': rings(box(40, 45, 42, 47))},
        {'score': .99, 'polygons': rings(box(22, 25, 28, 80))}])
    assert result['status'] == 'no_roof_evidence'


def test_registration_strip_kept_separate_from_exclusive_lot_area(site):
    scene, block, image = site
    search = searches(scene, block)[0]
    result = allocate_lot(scene, image, search, [{'score': .9, 'polygons': rings(box(35, 40, 82, 86))}])
    roof, uncertain = geometry(result['polygons']), geometry(result['uncertain_polygons'])
    assert uncertain.area > 0 and uncertain.intersection(roof).area < 1e-8
    assert roof.difference(geometry(search['core'])).area < 1e-8
    assert all(max(x for x, y in line) <= 80.001 for line in result['roof_edges'])


@pytest.mark.parametrize('case', ['unknown_uncertainty', 'clipped', 'overlap'])
def test_ambiguous_lot_searches_abstain(site, case):
    scene, block, _ = site
    if case == 'unknown_uncertainty':
        block['lots'][0]['horizontal_uncertainty_m'] = None
    elif case == 'clipped':
        scene['image_width'] = 75
    else:
        block['lots'][1]['polygons'] = rings(box(35, 35, 75, 75))
    assert searches(scene, block)[0]['status'] == 'needs_context'


def test_crop_detection_stays_registered_and_keeps_original_rgb(site):
    _, _, image = site
    search = {'crop': [20, 30, 100, 110]}
    class Segmenter:
        def encode_image(self, crop):
            assert np.array_equal(np.asarray(crop), np.asarray(image)[30:110, 20:100])
            return 'features'

        def proposals(self, features, prompt, size, **_):
            assert features == 'features' and prompt == 'roof' and size == (80, 80)
            mask = np.zeros((80, 80), dtype=bool)
            mask[10:40, 15:50] = True
            return [SimpleNamespace(mask=mask, score=.9)]
    result = detect_lot(image, search, Segmenter())
    assert geometry(result[0]['polygons']).bounds == (35, 40, 70, 70)


def test_close_crop_cannot_erase_context_roof_or_add_fragmentation(site):
    scene, block, image = site
    search = searches(scene, block)[0]
    context = [box(35, 40, 70, 95)]
    detections = [{'score': .9, 'polygons': rings(box(40, 45, 65, 65))}]
    result = allocate_lot(scene, image, search, detections, context)
    assert result['boundary_strategy'] == 'context-retained'
    assert geometry(result['polygons']).equals(box(35, 40, 70, 80))
    assert result['scale_comparison']['lost_context_fraction'] > .5
    # No positive crop evidence must still abstain, even with a context prior.
    assert allocate_lot(scene, image, search, [], context)['status'] == 'no_roof_evidence'


def test_cache_identity_and_missing_inference_are_explicit(site, tmp_path):
    scene, block, image = site
    original = copy.deepcopy((scene, block))
    key = cache_key(scene, block)
    assert key != cache_key({**scene, 'image_sha256': 'different'}, block)
    changed = copy.deepcopy(block)
    changed['lots'][0]['horizontal_uncertainty_m'] = 2
    assert key != cache_key(scene, changed)
    result = lot_review(tmp_path, scene, block, image, tmp_path)
    assert all(lot['status'] == 'not_run' and not lot['polygons'] for lot in result['lots'])
    assert original == (scene, block)
    (tmp_path/f'{key}.json').write_text(json.dumps({'key': 'wrong', 'lots': []}))
    with pytest.raises(ValueError, match='different image'):
        lot_review(tmp_path, scene, block, image, tmp_path)


def test_lot_observations_are_frozen_with_the_candidate_batch(tmp_path):
    from scripts.ml.roof_review_loop import fingerprint, snapshot_equipment
    folder = tmp_path/'data/roof-review-loop/lot-segmentation'
    folder.mkdir(parents=True)
    before = fingerprint(tmp_path)
    (folder/'pending.partial').write_text('{}')
    assert fingerprint(tmp_path) == before
    (folder/'observations.json').write_text('{"lots": []}')
    assert fingerprint(tmp_path) != before
    batch = tmp_path/'batch'
    snapshot_equipment(tmp_path, batch)
    (folder/'observations.json').write_text('{"lots": [1]}')
    assert json.loads((batch/'code/lot-segmentation/observations.json').read_text()) == {'lots': []}


@pytest.mark.parametrize('uncertainty', [float('nan'), float('inf'), -1, 6, True, '1.5'])
def test_bad_uncertainty_never_generates_a_search(site, uncertainty):
    scene, block, _ = site
    block['lots'][0]['horizontal_uncertainty_m'] = uncertainty
    row = searches(scene, block)[0]
    assert row['status'] == 'needs_context' and row['horizontal_uncertainty_m'] is None
    json.dumps(row, allow_nan=False)


@pytest.mark.parametrize('case', ['duplicate', 'bowtie', 'nonfinite', 'edge'])
def test_malformed_or_edge_parcels_are_withheld(site, case):
    scene, block, _ = site
    if case == 'duplicate':
        block['lots'][1]['pfi'] = '1'
    elif case == 'bowtie':
        block['lots'][0]['polygons'] = [[[[30, 30], [70, 70], [30, 70], [70, 30]]]]
    elif case == 'nonfinite':
        block['lots'][0]['polygons'][0][0][0][0] = float('nan')
    else:
        block['lots'][0]['polygons'] = rings(box(2, 30, 20, 80))
    row = searches(scene, block)[0]
    assert row['status'] == 'needs_context'
    json.dumps(row, allow_nan=False)


@pytest.mark.parametrize('frame', [[[0, 0], [0, .5], [0, 0]], [[.5, 0], [0, .3], [0, 0]], [[.5, 0], [0, -.5], [float('inf'), 0]]])
def test_invalid_registration_rejected(site, frame):
    scene, _, _ = site
    with pytest.raises(ValueError):
        pixel_scale({**scene, 'pixel_to_meters': frame})


@pytest.mark.parametrize('score', [float('nan'), float('inf'), True, -1, 1.1, '.9'])
def test_bad_scores_do_not_become_confidence(site, score):
    scene, block, image = site
    with pytest.raises(ValueError, match='score'):
        allocate_lot(scene, image, searches(scene, block)[0], [{'score': score, 'polygons': rings(box(35, 40, 70, 70))}])


def test_small_courtyard_and_separate_surfaces_survive_high_score_crop(site):
    scene, block, image = site
    search = searches(scene, block)[0]
    exterior = box(35, 40, 70, 75)
    hole = box(50, 50, 52, 52)
    baseline = Polygon(exterior.exterior, [hole.exterior])
    crop = [{'score': .99, 'polygons': rings(exterior)}]
    result = allocate_lot(scene, image, search, crop, [baseline])
    assert result['scale_comparison']['courtyard_change']
    assert geometry(result['polygons']).intersection(hole).area == 0
    separated = [box(35, 40, 70, 55), box(35, 56, 70, 75)]
    result = allocate_lot(scene, image, search, crop, separated)
    assert result['scale_comparison']['adds_fragments_or_corners']
    assert len(geometry(result['polygons']).geoms) == 2


def observations(scene, block, image):
    search = searches(scene, block)[0]
    return {'key': cache_key(scene, block), 'scene_id': scene['id'], 'image_sha256': scene['image_sha256'],
            'model_signature': scene['model_signature'], 'detector_weights': {'model': 'hash'},
            'lots': [{'pfi': search['pfi'], 'crop': search['crop'],
                      'detections': [{'score': .9, 'polygons': rings(box(35, 40, 70, 75))}]}]}


def test_partial_and_stale_observations_cannot_be_reported_complete(site, tmp_path):
    scene, block, image = site
    payload = observations(*site)
    result = lot_review(tmp_path, scene, block, image, observations=payload)
    assert result['status'] == 'partial'
    assert [lot['status'] for lot in result['lots']] == ['needs_review', 'not_run']
    payload['lots'][0]['crop'][0] += 1
    result = lot_review(tmp_path, scene, block, image, observations=payload)
    assert result['lots'][0]['status'] == 'failed'
    with pytest.raises(ValueError, match='misregistered'):
        validate_checkpoint(payload, payload['key'], payload['detector_weights'], searches(scene, block))


@pytest.mark.parametrize('case', ['duplicate', 'unknown', 'image', 'outside'])
def test_observation_source_and_coordinates_verified(site, tmp_path, case):
    scene, block, image = site
    payload = observations(*site)
    if case == 'duplicate':
        payload['lots'] *= 2
    elif case == 'unknown':
        payload['lots'][0]['pfi'] = 'other'
    elif case == 'image':
        payload['image_sha256'] = 'wrong'
    else:
        payload['lots'][0]['detections'][0]['polygons'] = rings(box(10, 10, 180, 180))
        result = lot_review(tmp_path, scene, block, image, observations=payload)
        assert result['lots'][0]['status'] == 'failed'
        return
    with pytest.raises(ValueError):
        lot_review(tmp_path, scene, block, image, observations=payload)
