import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from scripts.ml.scene_training import export_scenes, mask_rle
from scripts.ml.tiles import tile_xy_to_lonlat
from shapely.geometry import Polygon


def decode_rle(segmentation):
    return np.repeat(np.arange(len(segmentation['counts'])) % 2, segmentation['counts']).reshape(segmentation['size'], order='F')


@pytest.mark.parametrize('reverse', [False, True])
def test_gap_mask_encoding_preserves_pixels_and_coco_column_order(reverse):
    ring = [[0, 0], [10, 0], [10, 8], [0, 8]]
    hole = [[2, 2], [4, 2], [4, 5], [2, 5]]
    if reverse:
        ring, hole = ring[::-1], hole[::-1]
    mask, area = mask_rle(Polygon(ring, [hole]), 12, 9)
    expected = np.zeros((9, 12), dtype=bool)
    expected[:8, :10] = True
    expected[2:5, 2:4] = False
    assert mask['counts'][0] == 0, 'Foreground at the origin needs an initial empty background run'
    assert area == 74
    np.testing.assert_array_equal(decode_rle(mask), expected)


def test_export_preserves_reviewed_gaps_and_requires_class_completion(scene_dataset):
    root, folder, review = scene_dataset
    hole = [[3, 2], [5, 2], [5, 4], [3, 4]]
    review['objects'][0]['holes'] = [hole]
    (folder/'review.json').write_text(json.dumps(review))
    partial = export_scenes(root)
    assert partial['class_exports'] == {}
    row = json.loads(Path(partial['dataset']).read_text())
    assert row['export_schema_version'] == 6 and row['reference'][0]['holes'] == [hole]
    review['reviewed_classes'] = ['car']
    (folder/'review.json').write_text(json.dumps(review))
    full = export_scenes(root)
    annotation = json.loads((Path(full['dataset']).parent/'car-coco.json').read_text())['annotations'][0]
    expected = np.zeros((20, 20), dtype=bool)
    expected[1:5, 1:9] = True
    expected[2:4, 3:5] = False
    np.testing.assert_array_equal(decode_rle(annotation['segmentation']), expected)
    assert annotation['area'] == 28 and annotation['bbox'] == [1, 1, 8, 4]
    assert annotation['iscrowd'] == 0
    review['objects'][0]['holes'] = [[[3, 2], [12, 2], [12, 4], [3, 4]]]
    (folder/'review.json').write_text(json.dumps(review))
    with pytest.raises(ValueError, match='Invalid reviewed polygon'):
        export_scenes(root)


def test_replaced_outlines_are_neither_targets_nor_false_positives(scene_dataset):
    root, folder, review = scene_dataset
    old = copy.deepcopy(review['objects'][0])
    old.update(id='old-car', status='superseded', geometry_reviewed=False, replacement_ids=['car'])
    review['objects'][0]['derived_from'] = ['old-car']
    review['objects'].append(old)
    review['reviewed_classes'] = ['car']
    (folder/'review.json').write_text(json.dumps(review))
    exported = export_scenes(root)
    assert exported['reviewed_objects'] == 1 and exported['false_positives'] == 0
    row = json.loads(Path(exported['dataset']).read_text())
    assert row['reference'][0]['derived_from'] == ['old-car']
    coco = json.loads((Path(exported['dataset']).parent/'car-coco.json').read_text())
    assert len(coco['annotations']) == 1
    review['objects'][0].update(status='needs_adjustment', geometry_reviewed=False)
    (folder/'review.json').write_text(json.dumps(review))
    pending = export_scenes(root)
    assert pending['reviewed_objects'] == 0 and pending['class_exports'] == {}


def test_feedback_preserves_2d_targets_but_withholds_outline_problems(scene_dataset):
    root, folder, review = scene_dataset
    review['reviewed_classes'] = ['car']
    review['objects'][0]['review_issues'] = ['shape_3d', 'height']
    (folder/'review.json').write_text(json.dumps(review))
    exported = export_scenes(root)
    row = json.loads(Path(exported['dataset']).read_text())
    assert exported['reviewed_objects'] == 1 and exported['class_exports'] == {'car': 1}
    assert row['feedback'] == [{'id': 'car', 'kind': 'car', 'issues': ['shape_3d', 'height']}]
    assert 'model' not in row['reference'][0] and 'review_issues' not in row['reference'][0]
    assert 'roof_annotation' not in row['reference'][0]
    for issue in ('outline', 'class', 'instances'):
        # Defend against contradictory persisted records independently of API validation.
        review['objects'][0]['review_issues'] = [issue]
        (folder/'review.json').write_text(json.dumps(review))
        held = export_scenes(root)
        assert held['reviewed_objects'] == 0 and held['class_exports'] == {}
        row = json.loads(Path(held['dataset']).read_text())
        assert row['feedback'][0]['issues'] == [issue]
    review['objects'][0]['review_issues'] = []
    review['objects'][0].update(status='needs_adjustment', geometry_reviewed=False)
    (folder/'review.json').write_text(json.dumps(review))
    assert export_scenes(root)['class_exports'] == {}, 'Clearing feedback never grants approval'


@pytest.mark.parametrize('issues', [None, 'height', {}, ['height', 'height'], ['bad'], [False], [[]]])
def test_export_rejects_malformed_feedback(scene_dataset, issues):
    root, folder, review = scene_dataset
    review['objects'][0]['review_issues'] = issues
    (folder/'review.json').write_text(json.dumps(review))
    with pytest.raises(ValueError, match='Invalid review feedback'):
        export_scenes(root)


@pytest.fixture
def scene_dataset(tmp_path):
    folder = tmp_path / 'data/scenes/test-1'
    folder.mkdir(parents=True)
    image = b'fixed image fixture'
    digest = hashlib.sha256(image).hexdigest()
    (folder / f'{digest}.png').write_bytes(image)
    objects = [{'id': 'car', 'kind': 'car', 'label': 'Car', 'ring': [[1, 1], [9, 1], [9, 5], [1, 5]],
                'status': 'confirmed', 'geometry_reviewed': True,
                'model': {'height_m': 1.5, 'height_verified': False}, 'roof_annotation': {'unverified': True}}]
    model = {'id': 'test-1', 'address_id': 1, 'cohort': 'test', 'image_sha256': digest, 'image_width': 20,
             'image_height': 20, 'pixel_to_meters': [[.1, 0], [0, .1], [0, 0]], 'centroid': dict(zip(('lon', 'lat'), tile_xy_to_lonlat(118441.5, 80418.5, 17), strict=True)),
             'model_signature': 'current', 'objects': objects}
    (folder/'latest.json').write_text(json.dumps(model))
    review = {'revision': 1, 'model_signature': 'current', 'objects': objects, 'groups': [], 'reviewed_classes': []}
    (folder/'review.json').write_text(json.dumps(review))
    (tmp_path/'data/cohorts').mkdir()
    (tmp_path/'data/cohorts/test.json').write_text(json.dumps({'entries': [{'address_id': 1, 'split': 'val', 'spatial_group': 'z17:118441:80418'}]}))
    return tmp_path, folder, review


def test_scene_export_requires_complete_layer_and_excludes_unverified_3d(scene_dataset):
    root, folder, review = scene_dataset
    partial = export_scenes(root)
    assert partial['reviewed_objects'] == 1
    assert partial['class_exports'] == {}
    row = json.loads(Path(partial['dataset']).read_text())
    assert 'model' not in row['reference'][0]
    assert 'roof_annotation' not in row['reference'][0]
    assert row['split'] == 'val'
    review['reviewed_classes'] = ['car', 'tree']  # Explicitly reviewed empty tree layer is a valid negative.
    (folder/'review.json').write_text(json.dumps(review))
    full = export_scenes(root)
    assert full['version'] != partial['version']
    assert full['class_exports'] == {'car': 1, 'tree': 1}
    tree = json.loads((Path(full['dataset']).parent/'tree-coco.json').read_text())
    assert len(tree['images']) == 1 and not tree['annotations']
    car = json.loads((Path(full['dataset']).parent/'car-coco.json').read_text())
    assert car['images'][0]['spatial_group'] == 'z17:118441:80418'
    assert car['annotations'][0]['area'] == 32
    assert export_scenes(root)['version'] == full['version']


@pytest.mark.parametrize('status', ['confirmed', 'rejected'])
@pytest.mark.parametrize('authority', ['fieldwork_import', 'provenance'])
def test_fieldwork_and_machine_objects_are_not_labels_or_empty_layers(scene_dataset, status, authority):
    from scripts.ml.scene_training import collect_scenes

    root, folder, review = scene_dataset
    obj = review['objects'][0]
    obj['status'] = status
    obj[authority] = {'pending': True} if authority == 'fieldwork_import' else {'source': 'machine:test'}
    review['reviewed_classes'] = ['car']
    (folder/'review.json').write_text(json.dumps(review))
    encoded, classes, report = collect_scenes(root)
    row = json.loads(encoded)
    assert row['reference'] == [] and row['false_positives'] == [] and classes == {}
    layer = next(c for c in report['classes'] if c['kind'] == 'car')['layers'][0]
    assert layer['pending'] == 1 and layer['blockers'] == ['pending']
    if authority == 'fieldwork_import':
        obj['fieldwork_import']['pending'] = False
        (folder/'review.json').write_text(json.dumps(review))
        encoded, classes, _ = collect_scenes(root)
        row = json.loads(encoded)
        assert len(row['reference']) == (status == 'confirmed')
        assert len(row['false_positives']) == (status == 'rejected')
        assert 'car' in classes


def test_stale_review_cannot_become_training_labels(scene_dataset):
    root, folder, review = scene_dataset
    review['model_signature'] = 'previous-model'
    review['reviewed_classes'] = ['car']
    (folder/'review.json').write_text(json.dumps(review))
    manifest = export_scenes(root)
    assert manifest['reviewed_objects'] == 0
    assert manifest['class_exports'] == {}
    assert json.loads(Path(manifest['dataset']).read_text())['stale'] is True


def test_partial_visibility_is_explicit_without_inventing_hidden_geometry(scene_dataset):
    root, folder, review = scene_dataset
    review['reviewed_classes'] = ['car']
    for visibility in ['full', 'occluded', 'truncated', 'occluded_truncated', None]:
        if visibility:
            review['objects'][0]['visibility'] = visibility
        else:
            review['objects'][0].pop('visibility')
        (folder/'review.json').write_text(json.dumps(review))
        result = export_scenes(root)
        row = json.loads(Path(result['dataset']).read_text())
        assert row['outline_semantics'] == 'visible'
        assert row['reference'][0].get('visibility') == visibility
        labels = json.loads((Path(result['dataset']).parent/'car-coco.json').read_text())
        label = labels['annotations'][0]
        assert label['attributes'] == {'outline': 'visible', 'visibility': visibility or 'unassessed'}
        assert label['area'] == 32 and label['segmentation'] == [[1, 1, 9, 1, 9, 5, 1, 5]]
        assert 'model' not in row['reference'][0]
    review['objects'][0]['visibility'] = 'guess'
    (folder/'review.json').write_text(json.dumps(review))
    with pytest.raises(ValueError, match='Invalid visibility'):
        export_scenes(root)


def test_scene_export_rejects_changed_image_bytes(scene_dataset):
    root, folder, _ = scene_dataset
    next(folder.glob('*.png')).write_bytes(b'changed')
    with pytest.raises(ValueError, match='Image bytes'):
        export_scenes(root)


def test_false_positive_is_not_a_positive_and_pending_objects_block_coverage(scene_dataset):
    root, folder, review = scene_dataset
    review['objects'][0]['status'] = 'rejected'
    review['objects'][0]['geometry_reviewed'] = False
    review['reviewed_classes'] = ['car']
    (folder/'review.json').write_text(json.dumps(review))
    manifest = export_scenes(root)
    assert manifest['false_positives'] == 1 and manifest['reviewed_objects'] == 0
    labels = json.loads((Path(manifest['dataset']).parent/'car-coco.json').read_text())
    assert not labels['annotations']
    review['objects'][0]['status'] = 'needs_adjustment'
    (folder/'review.json').write_text(json.dumps(review))
    assert export_scenes(root)['class_exports'] == {}


def test_map_regions_export_without_a_roof_cohort_and_quarantine_split_crossings(scene_dataset):
    root, folder, review = scene_dataset
    model = json.loads((folder/'latest.json').read_text())
    model.update(address_id=None, cohort=None, region={'spatial_group': 'z17:118442:80417', 'split': 'train'},
                 centroid=dict(zip(('lon', 'lat'), tile_xy_to_lonlat(118442.5, 80417.5, 17), strict=True)))
    (folder/'latest.json').write_text(json.dumps(model))
    review['reviewed_classes'] = ['car']
    (folder/'review.json').write_text(json.dumps(review))
    assert export_scenes(root)['class_exports'] == {'car': 1}
    model['region']['split'] = 'unassigned'
    (folder/'latest.json').write_text(json.dumps(model))
    manifest = export_scenes(root)
    assert manifest['class_exports'] == {}
    row = json.loads(Path(manifest['dataset']).read_text())
    assert row['split'] == 'unassigned' and len(row['reference']) == 1


def coverage_class(root, kind='car'):
    from scripts.ml.scene_training import scene_coverage
    return next(c for c in scene_coverage(root)['classes'] if c['kind'] == kind)


def test_evaluation_agrees_between_live_coverage_and_export_without_mutating_labels(scene_dataset):
    from scripts.ml.scene_training import scene_coverage
    root, folder, review = scene_dataset
    model = json.loads((folder/'latest.json').read_text())
    model['scans'] = {'car': {'status': 'complete', 'count': 1}}
    (folder/'latest.json').write_text(json.dumps(model))
    review['reviewed_classes'] = ['car']
    (folder/'review.json').write_text(json.dumps(review))
    original = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    live = scene_coverage(root)
    exported = export_scenes(root)
    evaluation = json.loads(Path(exported['evaluation']).read_text())
    assert evaluation == live['evaluation']
    assert evaluation['dataset_version'] == exported['version']
    car = next(c for c in evaluation['classes'] if c['kind'] == 'car')
    assert car['splits']['validation']['matched'] == 1
    assert car['splits']['validation']['mean_outline_iou'] == 1
    assert original == {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}


def test_coverage_is_read_only_and_scan_zero_is_not_a_checked_negative(scene_dataset):
    from scripts.ml.scene_training import scene_coverage
    root, folder, review = scene_dataset
    model = json.loads((folder/'latest.json').read_text())
    model['scans'] = {'tree': {'status': 'complete', 'count': 0}}
    (folder/'latest.json').write_text(json.dumps(model))
    before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    report = scene_coverage(root)
    assert report['scenes'] == 1 and report['latest_export'] is None and not report['export_current']
    car, tree, solar = [coverage_class(root, kind) for kind in ('car', 'tree', 'solar_panel')]
    assert car['references'] == car['withheld'] == 1
    assert tree['layers'][0]['scan'] == 'complete' and tree['layers'][0]['blockers'] == ['unchecked']
    assert solar['layers'][0]['scan'] == 'not_scanned'
    assert all(c['splits']['validation']['images'] == 0 for c in (car, tree, solar))
    assert before == {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    # An explicit whole-image check may establish a negative even without inference.
    review['reviewed_classes'] = ['solar_panel']
    (folder/'review.json').write_text(json.dumps(review))
    solar = coverage_class(root, 'solar_panel')
    assert solar['splits']['validation'] == {'images': 1, 'masks': 0, 'empty_images': 1, 'groups': 1}


def test_unchecked_confirmation_blocks_coco_instead_of_becoming_a_negative(scene_dataset):
    root, folder, review = scene_dataset
    review['objects'][0]['geometry_reviewed'] = False
    review['reviewed_classes'] = ['car']
    (folder/'review.json').write_text(json.dumps(review))
    car = coverage_class(root)
    assert car['pending'] == 1 and car['references'] == 0
    assert car['layers'][0]['blockers'] == ['pending']
    assert 'object=car' in car['layers'][0]['review_url']
    assert export_scenes(root)['class_exports'] == {}


def test_coverage_matches_export_and_detects_saved_changes(scene_dataset):
    from scripts.ml.scene_training import scene_coverage
    root, folder, review = scene_dataset
    review['reviewed_classes'] = ['car', 'tree']
    (folder/'review.json').write_text(json.dumps(review))
    result = export_scenes(root)
    report = scene_coverage(root)
    assert report['version'] == result['version'] and report['export_current']
    for kind, images in result['class_exports'].items():
        counts = coverage_class(root, kind)['splits']['validation']
        coco = json.loads((Path(result['dataset']).parent/f'{kind}-coco.json').read_text())
        assert counts['images'] == images == len(coco['images'])
        assert counts['masks'] == len(coco['annotations'])
        assert result['class_splits'][kind]['validation'] == counts
    review['objects'][0]['status'] = 'needs_adjustment'
    (folder/'review.json').write_text(json.dumps(review))
    assert not scene_coverage(root)['export_current']
    review['model_signature'] = 'old'
    (folder/'review.json').write_text(json.dumps(review))
    car = coverage_class(root)
    assert car['references'] == 0 and 'stale' in car['layers'][0]['blockers']


@pytest.mark.parametrize('split,group,reason', [('unexpected', 'block', 'unassigned'), ('train', None, 'missing_group')])
def test_unresolved_split_is_visible_and_withheld(scene_dataset, split, group, reason):
    root, folder, review = scene_dataset
    (root/'data/cohorts/test.json').write_text(json.dumps({'entries': [{'address_id': 1, 'split': split, 'spatial_group': group}]}))
    review['reviewed_classes'] = ['car']
    (folder/'review.json').write_text(json.dumps(review))
    assert reason in coverage_class(root)['layers'][0]['blockers']
    assert export_scenes(root)['class_exports'] == {}


@pytest.mark.parametrize('same_image,same_group', [(True, False), (False, True)])
def test_cross_split_repetition_is_withheld_but_same_split_counts_groups_once(scene_dataset, same_image, same_group):
    import shutil
    root, folder, review = scene_dataset
    review['reviewed_classes'] = ['car']
    (folder/'review.json').write_text(json.dumps(review))
    sibling = folder.parent/'test-2'
    shutil.copytree(folder, sibling)
    model = json.loads((sibling/'latest.json').read_text())
    model.update(id='test-2', address_id=2)
    if not same_image:
        image = b'different frame'
        model['image_sha256'] = hashlib.sha256(image).hexdigest()
        (sibling/f"{model['image_sha256']}.png").write_bytes(image)
    (sibling/'latest.json').write_text(json.dumps(model))
    entries = [{'address_id': 1, 'split': 'validation', 'spatial_group': 'z17:118441:80418'},
               {'address_id': 2, 'split': 'test', 'spatial_group': 'z17:118441:80418' if same_group else 'block-2'}]
    (root/'data/cohorts/test.json').write_text(json.dumps({'entries': entries}))
    assert all('split_conflict' in layer['blockers'] for layer in coverage_class(root)['layers'])
    assert export_scenes(root)['class_exports'] == {}
    entries[1]['split'] = 'val'  # Alias must not conflict with validation.
    (root/'data/cohorts/test.json').write_text(json.dumps({'entries': entries}))
    assert coverage_class(root)['splits']['validation'] == {'images': 2, 'masks': 2, 'empty_images': 0, 'groups': 1 if same_group else 2}
    assert export_scenes(root)['class_exports'] == {'car': 2}


def test_coverage_empty_and_qa_exclusion(tmp_path, scene_dataset):
    import shutil

    from scripts.ml.scene_training import scene_coverage
    root, folder, _ = scene_dataset
    empty = tmp_path/'empty'
    assert scene_coverage(empty)['scenes'] == 0 and not empty.exists()
    shutil.copytree(folder, folder.parent/'qa_invalid')
    (folder.parent/'qa_invalid/latest.json').write_text('invalid fixture must not load')
    assert scene_coverage(root)['scenes'] == 1


def test_whole_image_crossing_keeps_references_but_withholds_coco(scene_dataset):
    from scripts.ml.scene_training import scene_coverage
    root, folder, review = scene_dataset
    review['reviewed_classes'] = ['car', 'tree']
    (folder/'review.json').write_text(json.dumps(review))
    model = json.loads((folder/'latest.json').read_text())
    model['centroid'] = dict(zip(('lon', 'lat'), tile_xy_to_lonlat(118441.999, 80418.5, 17), strict=True))
    (folder/'latest.json').write_text(json.dumps(model))
    report = scene_coverage(root)
    car = next(c for c in report['classes'] if c['kind'] == 'car')
    assert car['references'] == car['withheld'] == 1
    assert 'image_geography' in car['layers'][0]['blockers']
    assert car['layers'][0]['image_geography']['status'] == 'cross_split'
    exported = export_scenes(root)
    assert not exported['class_exports']
    row = json.loads(Path(exported['dataset']).read_text())
    assert len(row['reference']) == 1 and row['split'] == 'val'
    assert row['image_geography']['splits'] == ['train', 'validation']


@pytest.fixture
def roof_feedback_dataset(scene_dataset):
    root, folder, review = scene_dataset
    model = json.loads((folder/'latest.json').read_text())
    model['meters_to_pixel'] = [[10, 0], [0, 10], [0, 0]]
    (folder/'latest.json').write_text(json.dumps(model))
    obj = review['objects'][0]
    obj.update(kind='building', label='Reviewed roof', visibility='full', roof_annotation={
        'schema_version': 1, 'roof_type': 'gabled',
        'vertices': [[.1, .1, 3], [.9, .1, 3], [.9, .5, 3], [.1, .5, 3], [.1, .3, 7], [.9, .3, 8]],
        'faces': [[0, 1, 5, 4], [4, 5, 2, 3]], 'edges': [{'a': 4, 'b': 5, 'kind': 'ridge'}]})
    obj['roof_feedback'] = {'outline': {'ring': copy.deepcopy(obj['ring']), 'holes': []}, 'family': 'gabled',
                            'linework': {**copy.deepcopy(obj['roof_annotation']), 'vertices': [p[:2] for p in obj['roof_annotation']['vertices']]}}
    del obj['roof_feedback']['linework']['schema_version'], obj['roof_feedback']['linework']['roof_type']
    (folder/'review.json').write_text(json.dumps(review))
    return root, folder, review


def test_roof_feedback_exports_explicit_image_targets_without_heights(roof_feedback_dataset):
    root, folder, review = roof_feedback_dataset
    manifest = export_scenes(root)
    assert manifest['class_exports'] == {}, 'Independent roof observations do not complete the building mask layer'
    rows = [json.loads(line) for line in Path(manifest['roof_feedback']['dataset']).read_text().splitlines()]
    assert len(rows) == 1 and rows[0]['revision'] == 1 and rows[0]['split'] == 'val'
    assert rows[0]['schema_version'] == 1 and rows[0]['dataset_version'] == manifest['version']
    reference = rows[0]['reference']
    assert reference['family'] == 'gabled' and reference['height_supervision'] == 'none'
    assert reference['linework']['coordinate_space'] == 'image_pixels'
    np.testing.assert_allclose(reference['linework']['vertices'], [[1, 1], [9, 1], [9, 5], [1, 5], [1, 3], [9, 3]])
    assert all(len(p) == 2 for p in reference['linework']['vertices'])
    assert 'model' not in reference and 'roof_annotation' not in reference
    assert manifest['roof_feedback']['targets']['family']['splits']['validation'] == {'objects': 1, 'groups': 1}
    # Raising an estimated apex does not revoke a checked image-plane observation.
    review['objects'][0]['roof_annotation']['vertices'][4][2] = 20
    (folder/'review.json').write_text(json.dumps(review))
    after = export_scenes(root)
    assert json.loads(Path(after['roof_feedback']['dataset']).read_text())['reference'] == reference
    assert after['version'] != manifest['version'], 'The audit still records the changed height estimate'


@pytest.mark.parametrize('authority,reason', [('fieldwork_import', 'fieldwork_pending'), ('provenance', 'machine_label')])
def test_roof_targets_require_local_human_authority(roof_feedback_dataset, authority, reason):
    from scripts.ml.scene_training import collect_scenes

    root, folder, review = roof_feedback_dataset
    obj = review['objects'][0]
    obj[authority] = {'pending': True} if authority == 'fieldwork_import' else {'source': 'machine:test'}
    (folder/'review.json').write_text(json.dumps(review))
    encoded, _, report = collect_scenes(root)
    assert json.loads(encoded)['roof_reference'] == []
    layer = report['roof_feedback']['layers'][0]
    assert layer['blockers'] == {'family': [reason], 'linework': [reason]}
    if authority == 'fieldwork_import':
        obj['fieldwork_import']['pending'] = False
        (folder/'review.json').write_text(json.dumps(review))
        encoded, _, _ = collect_scenes(root)
        reference = json.loads(encoded)['roof_reference'][0]
        assert reference['family'] == 'gabled' and reference['linework']


@pytest.mark.parametrize('case', ['unchecked', 'outline', 'partial', 'unassessed', 'pending', 'rejected', 'superseded', 'flag', 'stale', 'split', 'geography'])
def test_roof_targets_withhold_unreviewed_or_changed_evidence(roof_feedback_dataset, case):
    root, folder, review = roof_feedback_dataset
    obj = review['objects'][0]
    if case == 'unchecked':
        del obj['roof_feedback']
    elif case == 'outline':
        obj['ring'][0][0] = 2
    elif case == 'partial':
        obj['visibility'] = 'occluded'
    elif case == 'unassessed':
        del obj['visibility']
    elif case in ('pending', 'rejected', 'superseded'):
        obj.update(status={'pending': 'needs_adjustment'}.get(case, case), geometry_reviewed=False)
    elif case == 'flag':
        obj['review_issues'] = ['class']
    elif case == 'stale':
        review['model_signature'] = 'old'
    elif case == 'split':
        path = root/'data/cohorts/test.json'
        cohort = json.loads(path.read_text())
        cohort['entries'][0]['split'] = 'unassigned'
        path.write_text(json.dumps(cohort))
    elif case == 'geography':
        model = json.loads((folder/'latest.json').read_text())
        model['centroid'] = {'lon': 145.1, 'lat': -37.8}
        (folder/'latest.json').write_text(json.dumps(model))
    (folder/'review.json').write_text(json.dumps(review))
    exported = export_scenes(root)
    assert Path(exported['roof_feedback']['dataset']).read_text() == ''
    assert exported['roof_feedback']['objects'] == 0


@pytest.mark.parametrize('case', ['xy', 'edge', 'faces', 'shape_flag', 'overlap', 'gap', 'bad_line', 'outside_point'])
def test_linework_check_is_independent_and_geometrically_audited(roof_feedback_dataset, case):
    root, folder, review = roof_feedback_dataset
    obj = review['objects'][0]
    graph = obj['roof_annotation']
    if case == 'xy':
        graph['vertices'][4][1] += .02
    elif case == 'edge':
        graph['edges'][0]['kind'] = 'hip'
    elif case == 'faces':
        graph['faces'][0] = graph['faces'][0][::-1]
    elif case == 'shape_flag':
        obj['review_issues'] = ['shape_3d']
    elif case in ('overlap', 'gap'):
        graph['faces'] = [graph['faces'][0], graph['faces'][0]] if case == 'overlap' else graph['faces'][:1]
        obj['roof_feedback']['linework']['faces'] = copy.deepcopy(graph['faces'])
    elif case == 'bad_line':
        graph['edges'] = [{'a': 0, 'b': 2, 'kind': 'ridge'}]
        obj['roof_feedback']['linework']['edges'] = copy.deepcopy(graph['edges'])
    elif case == 'outside_point':
        graph['vertices'].append([5, 5, 3])
        obj['roof_feedback']['linework']['vertices'].append([5, 5])
    (folder/'review.json').write_text(json.dumps(review))
    exported = export_scenes(root)
    reference = json.loads(Path(exported['roof_feedback']['dataset']).read_text())['reference']
    assert reference['family'] == 'gabled' and 'linework' not in reference
    assert exported['roof_feedback']['targets']['linework']['withheld'] == 1


def test_outline_swipe_or_proposal_cannot_create_roof_labels(roof_feedback_dataset):
    root, folder, review = roof_feedback_dataset
    del review['objects'][0]['roof_feedback']
    review['reviewed_classes'] = ['building']
    (folder/'review.json').write_text(json.dumps(review))
    exported = export_scenes(root)
    assert exported['class_exports'] == {'building': 1}
    assert not Path(exported['roof_feedback']['dataset']).read_text()


def test_independent_line_edits_do_not_approve_the_old_mesh(roof_feedback_dataset):
    root, folder, review = roof_feedback_dataset
    obj = review['objects'][0]
    obj['roof_line_edits'] = {
        'schema_version': 1,
        'outline': {'ring': copy.deepcopy(obj['ring']), 'holes': []},
        'vertices': [[.1, .2], [.9, .2]],
        'edges': [{'a': 0, 'b': 1, 'kind': 'ridge'}],
        'checked': True,
        'notes': 'The ridge is offset from the estimated mesh.',
    }
    (folder/'review.json').write_text(json.dumps(review))
    exported = export_scenes(root)
    reference = json.loads(Path(exported['roof_feedback']['dataset']).read_text())['reference']
    assert reference['family'] == 'gabled' and 'linework' not in reference
    assert exported['roof_feedback']['targets']['linework']['withheld'] == 1
    from scripts.ml.scene_roof_feedback import roof_targets
    model = json.loads((folder/'latest.json').read_text())
    assert 'separate_line_edits' in roof_targets(obj, model, [])[2]['linework']


def test_roof_feedback_quarantines_duplicate_images_across_splits(roof_feedback_dataset):
    from scripts.ml.scene_training import scene_coverage
    root, folder, review = roof_feedback_dataset
    model = json.loads((folder/'latest.json').read_text())
    duplicate = root/'data/scenes/test-duplicate'
    duplicate.mkdir()
    model.update(id='test-duplicate', address_id=None, cohort=None,
                 region={'spatial_group': 'z17:118442:80417', 'split': 'train'},
                 centroid=dict(zip(('lon', 'lat'), tile_xy_to_lonlat(118442.5, 80417.5, 17), strict=True)))
    (duplicate/'latest.json').write_text(json.dumps(model))
    image = f"{model['image_sha256']}.png"
    (duplicate/image).write_bytes((folder/image).read_bytes())
    (duplicate/'review.json').write_text(json.dumps(review))
    exported = export_scenes(root)
    assert exported['roof_feedback']['objects'] == 0
    assert not Path(exported['roof_feedback']['dataset']).read_text()
    coverage = scene_coverage(root)['roof_feedback']
    assert all('split_conflict' in layer['blockers']['family'] for layer in coverage['layers'])
    assert coverage['targets']['family']['withheld'] == 2
