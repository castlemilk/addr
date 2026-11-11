import json

from scripts.ml.review_feedback import boundary_signature
from scripts.ml.training_dataset import export_dataset, model_signature


def test_export_keeps_predictions_separate_and_class_specific_negatives(tmp_path):
    def write(path, value):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value))
    ring = [[0, 0], [10, 0], [10, 10], [0, 10]]
    record = {'address_id': 1, 'centroid': {'lat': -37.8, 'lon': 145.3}, 'ring_meters': ring, 'eave_height': 3}
    parts = [{'ring_meters': ring, 'ring_z': [3] * 4}]
    prediction = {'address_id': 1, 'parts': parts, 'rooflines': [], 'pipeline_version': 5,
                  'footprint_coords': ring, 'ring_pixels': [[0, 0], [512, 0], [512, 512], [0, 512]]}
    write('data/cohorts/qa.json', {'entries': [{'address_id': 1, 'split': 'train', 'spatial_group': 'tile'}]})
    write('public/address-roofs.json', [record])
    write('data/reviews/cohorts/qa/report.json', {'results': [prediction]})
    image = tmp_path / 'data/reviews/cohorts/qa/1/site-clean.png'
    image.parent.mkdir()
    image.write_bytes(b'immutable image fixture')
    obj = {'id': 'solar', 'kind': 'solar_panel', 'status': 'confirmed', 'source': 'reviewer',
           'ring_meters': [[2, 2], [4, 2], [4, 4], [2, 4]]}
    review = {'boundary_signature': boundary_signature(record), 'model_signature': model_signature(parts),
              'status': 'needs_adjustment', 'objects_verified': True, 'object_reviewed_classes': ['solar_panel'],
              'objects': [obj, {**obj, 'id': 'false-skylight', 'kind': 'skylight', 'status': 'rejected'}]}
    write('data/reviews/feedback.json', {'qa:1': review})
    manifest = export_dataset('qa', root=tmp_path)
    row = json.loads((tmp_path / manifest['dataset']).read_text())
    assert row['training_eligible'] == {'boundary': False, 'roof_shape': False, 'objects': True}
    assert row['reference']['roof_graph'] is None
    assert row['prediction']['parts'] == parts
    assert len(row['reference']['objects']) == 2  # False positives retained separately.
    assert manifest['object_class_exports']['solar_panel']['instances'] == 1
    assert manifest['object_class_exports']['skylight']['images'] == 0
    assert not json.loads((tmp_path / manifest['objects_coco']).read_text())['images']
    assert export_dataset('qa', root=tmp_path)['version'] == manifest['version']
    image.write_bytes(b'changed source image')
    assert (tmp_path / row['image']['path']).read_bytes() == b'immutable image fixture'
    review['model_signature'] = 'stale'
    write('data/reviews/feedback.json', {'qa:1': review})
    stale = export_dataset('qa', root=tmp_path)
    assert stale['objects_eligible'] == 0
    review['model_signature'] = model_signature(parts)
    review['boundary_verified'] = True
    review['issues'] = ['apex']
    write('data/reviews/feedback.json', {'qa:1': review})
    boundary_only = export_dataset('qa', root=tmp_path)
    assert boundary_only['boundary_eligible'] == 1
    assert boundary_only['roof_shape_eligible'] == 0
    review['issues'] = ['boundary']
    write('data/reviews/feedback.json', {'qa:1': review})
    assert export_dataset('qa', root=tmp_path)['boundary_eligible'] == 0
