import copy
import hashlib
import json

import pytest
from PIL import Image
from scripts.ml.scene_batch import plan_batch, run_batch
from scripts.ml.tiles import tile_xy_to_lonlat


@pytest.fixture
def batch_sources(tmp_path):
    cohort = 'batch_test'
    folder = tmp_path/'data/reviews/cohorts'/cohort
    folder.mkdir(parents=True)
    entries, reports, records = [], [], []
    for aid, split, x, y in [(1, 'train', 118443, 80418), (2, 'validation', 118441, 80418),
                              (3, 'train', 118443, 80418), (4, 'test', 118444, 80418)]:
        source = folder/str(aid)
        source.mkdir()
        Image.new('RGB', (512, 512), (aid*20, 100, 50)).save(source/'site-clean.png')
        entries.append({'address_id': aid, 'split': split, 'spatial_group': f'z17:{x}:{y}'})
        reports.append({'address_id': aid, 'footprint_coords': [[0, 0], [10, 0], [10, 10], [0, 10]],
                        'ring_pixels': [[100, 400], [200, 400], [200, 300], [100, 300]], 'parts': [], 'rooflines': []})
        lon, lat = tile_xy_to_lonlat(x+.5, y+.5, 17)
        records.append({'address_id': aid, 'centroid': {'lat': lat, 'lon': lon}, 'full_address': f'Roof {aid}'})
    (folder/'report.json').write_text(json.dumps({'results': reports}))
    (tmp_path/'public').mkdir()
    (tmp_path/'public/address-roofs.json').write_text(json.dumps(records))
    (tmp_path/'data/cohorts').mkdir()
    (tmp_path/'data/cohorts'/f'{cohort}.json').write_text(json.dumps({'entries': entries}))
    return tmp_path, cohort


class EmptySegmenter:
    def __init__(self):
        self.calls = 0

    def encode_image(self, image):
        self.calls += 1

    def proposals(self, *args, **kwargs):
        return []


def test_batch_spreads_areas_withholds_test_and_preserves_existing_reviews(batch_sources):
    root, cohort = batch_sources
    existing = root/'data/scenes'/f'{cohort}-1'
    existing.mkdir(parents=True)
    # Even an orphan review must never be overwritten by batch extraction.
    (existing/'review.json').write_text('{"revision":7}')
    plan = plan_batch(cohort, ['car', 'road'], 1, root)
    assert [j['address_id'] for j in plan['jobs']] == [2]
    assert {j['reason'] for j in plan['excluded']} == {'existing', 'batch_limit', 'held_out_test'}
    assert (existing/'review.json').read_text() == '{"revision":7}'
    assert not (existing/'latest.json').exists()


def test_batch_freezes_sources_and_resumes_without_touching_published_scenes(batch_sources):
    root, cohort = batch_sources
    plan = plan_batch(cohort, ['car', 'road'], 3, root)
    assert [j['address_id'] for j in plan['jobs']] == [2, 1, 3]
    directory = root/'batch'
    directory.mkdir()
    segmenter = EmptySegmenter()
    state = run_batch(plan, directory, root, lambda: segmenter)
    assert state['counts'] == {'complete': 3} and segmenter.calls == 3
    folder = root/'data/scenes'/f'{cohort}-2'
    model = json.loads((folder/'latest.json').read_text())
    assert model['scans']['car']['status'] == 'complete'
    assert all(o['status'] == 'candidate' and not o['geometry_reviewed'] and not o['model']['height_verified'] for o in model['objects'])
    assert not (folder/'review.json').exists(), 'Proposals do not create checked negatives'
    original = (folder/'latest.json').read_bytes()
    (folder/'review.json').write_text('{"revision":9}')
    def forbidden():
        pytest.fail('A completed batch must not load the detector again')
    resumed = run_batch(plan, directory, root, forbidden)
    assert resumed['counts'] == {'complete': 3}
    assert (folder/'latest.json').read_bytes() == original
    assert (folder/'review.json').read_text() == '{"revision":9}'
    assert hashlib.sha256((folder/f"{model['image_sha256']}.png").read_bytes()).hexdigest() == model['image_sha256']


def test_batch_records_failure_and_retries_only_unpublished_jobs(batch_sources):
    root, cohort = batch_sources
    plan = plan_batch(cohort, ['car'], 3, root)
    original = copy.deepcopy(plan)
    plan['jobs'][0]['source_signature'] = 'changed'
    directory = root/'batch'
    directory.mkdir()
    segmenter = EmptySegmenter()
    state = run_batch(plan, directory, root, lambda: segmenter)
    assert state['status'] == 'needs_retry' and state['counts'] == {'failed': 1, 'complete': 2}
    assert segmenter.calls == 2
    assert not (root/'data/scenes'/f'{cohort}-2'/'latest.json').exists()
    resumed = run_batch(original, directory, root, lambda: segmenter)
    assert resumed['status'] == 'complete' and segmenter.calls == 3


def test_batch_withholds_changed_split_and_invalid_inputs(batch_sources):
    root, cohort = batch_sources
    path = root/'data/cohorts'/f'{cohort}.json'
    value = json.loads(path.read_text())
    value['entries'][1]['split'] = 'train'
    path.write_text(json.dumps(value))
    plan = plan_batch(cohort, ['car'], 250, root)
    assert any(row['scene_id'] == f'{cohort}-2' and row['reason'] == 'split_mismatch' for row in plan['excluded'])
    for args in [('../escape', ['car'], 1), (cohort, ['car', 'car'], 1), (cohort, ['unknown'], 1), (cohort, ['car'], 301)]:
        with pytest.raises(ValueError):
            plan_batch(*args, root)
