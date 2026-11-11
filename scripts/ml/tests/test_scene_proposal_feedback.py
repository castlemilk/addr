import copy
import hashlib
import json

import pytest
from PIL import Image
from scripts.ml.scene_proposal_feedback import supplemental_cases
from scripts.ml.tiles import tile_xy_to_lonlat


def proposal(identity, x):
    return {'id': identity, 'kind': 'car', 'ring': [[x, 1], [x+4, 1], [x+4, 5], [x, 5]],
            'source': 'sam3-text-onnx', 'prompt': 'car', 'score': .8, 'model': {'height_m': 1.5}}


@pytest.fixture
def frozen(tmp_path):
    image = tmp_path/'image.png'
    Image.new('RGB', (20, 20), 'grey').save(image)
    image_hash = hashlib.sha256(image.read_bytes()).hexdigest()
    predictions = [proposal('yes', 1), proposal('no', 7), proposal('unknown', 13)]
    reviewed = [{**predictions[0], 'status': 'confirmed', 'geometry_reviewed': True},
                {**predictions[1], 'status': 'rejected', 'geometry_reviewed': False},
                {**predictions[2], 'status': 'needs_adjustment', 'geometry_reviewed': False}]
    lon, lat = tile_xy_to_lonlat(118443.5, 80418.5, 17)
    row = {'export_schema_version': 5, 'scene_id': 'partial', 'split': 'train', 'spatial_group': 'z17:118443:80418',
           'image_path': str(image), 'image_sha256': image_hash, 'model_signature': 'model', 'stale': False,
           'centroid': {'lon': lon, 'lat': lat}, 'pixel_to_meters': [[.1, 0], [0, -.1], [0, 0]],
           'image_geography': {'status': 'verified'}, 'prediction': predictions,
           'review': {'revision': 3, 'model_signature': 'model', 'objects': reviewed, 'reviewed_classes': []}}
    def save(rows):
        encoded = '\n'.join(json.dumps(row) for row in rows)+'\n'
        (tmp_path/'dataset.jsonl').write_text(encoded)
        (tmp_path/'manifest.json').write_text(json.dumps({'version': hashlib.sha256(encoded.encode()).hexdigest()[:16]}))
    return tmp_path, row, save


def test_only_explicit_matches_supervise_partial_training_images(frozen):
    folder, row, save = frozen
    save([row])
    _, cases, ignored = supplemental_cases(folder, 'car', 'car', [])
    assert len(cases) == 1 and cases[0]['labels'] == [1, 0]
    assert [p['id'] for p in cases[0]['predictions']] == ['yes', 'no']
    assert ignored == {'unreviewed_proposal': 1}
    assert cases[0]['revision'] == 3 and cases[0]['spatial_group'] == row['spatial_group']
    row['review']['objects'][0]['review_issues'] = ['outline']
    save([row])
    _, cases, ignored = supplemental_cases(folder, 'car', 'car', [])
    assert cases[0]['labels'] == [0] and ignored['unreviewed_proposal'] == 2


@pytest.mark.parametrize('authority', ['fieldwork_import', 'provenance'])
def test_pending_fieldwork_and_machine_proposals_never_supervise(frozen, authority):
    folder, row, save = frozen
    for obj in row['review']['objects'][:2]:
        obj[authority] = {'pending': True} if authority == 'fieldwork_import' else {'source': 'machine:test'}
    save([row])
    assert supplemental_cases(folder, 'car', 'car', [])[1] == []
    if authority == 'fieldwork_import':
        for obj in row['review']['objects'][:2]:
            obj['fieldwork_import']['pending'] = False
        save([row])
        assert supplemental_cases(folder, 'car', 'car', [])[1][0]['labels'] == [1, 0]


def test_held_out_images_are_never_inspected_or_reused(frozen):
    folder, row, save = frozen
    for split in ['validation', 'test']:
        held = {'scene_id': split, 'split': split, 'spatial_group': row['spatial_group'], 'image_sha256': 'held',
                'review': {'must': 'not read'}, 'prediction': None}
        save([row, held])
        _, cases, ignored = supplemental_cases(folder, 'car', 'car', [])
        assert cases == [] and ignored['split_conflict'] == 1
        held['spatial_group'] = 'different'
        held['image_sha256'] = row['image_sha256']
        save([row, held])
        assert supplemental_cases(folder, 'car', 'car', [])[1] == []


def test_existing_images_conflicts_stale_reviews_and_changed_bytes_are_withheld(frozen):
    folder, row, save = frozen
    save([row])
    base = {'scene_id': row['scene_id'], 'split': 'train', 'spatial_group': row['spatial_group'], 'image_sha256': row['image_sha256']}
    assert supplemental_cases(folder, 'car', 'car', [base])[1] == []
    assert supplemental_cases(folder, 'car', 'another prompt', [])[1] == []
    conflict = copy.deepcopy(row['review']['objects'][0])
    conflict.update(id='conflicting-negative', status='rejected', geometry_reviewed=False)
    row['review']['objects'].append(conflict)
    save([row])
    _, cases, ignored = supplemental_cases(folder, 'car', 'car', [])
    assert cases[0]['labels'] == [0] and ignored['conflicting_proposal'] == 1
    row['stale'] = True
    save([row])
    assert supplemental_cases(folder, 'car', 'car', [])[1] == []
    row['stale'] = False
    save([row])
    (folder/'dataset.jsonl').write_text('modified')
    with pytest.raises(ValueError, match='frozen version'):
        supplemental_cases(folder, 'car', 'car', [])
    save([row])
    (folder/'image.png').write_bytes(b'changed')
    with pytest.raises(ValueError, match='image bytes changed'):
        supplemental_cases(folder, 'car', 'car', [])
