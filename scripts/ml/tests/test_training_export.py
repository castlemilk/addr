"""Dataset export: split by tile, provenance weights, machine-provenance exclusion, IGNORE pixels, manifest hashes.

Runs in the main ML venv; nothing here imports torch.
"""
import hashlib
import io
import json
import math
from contextlib import closing
from pathlib import Path

import numpy as np
from PIL import Image
from scripts.ml import eyes_index, scene_lot_tags
from scripts.ml import reference_registry as registry
from scripts.ml.tiles import tile_xy_to_lonlat
from scripts.ml.training import common, export_dataset

SIZE = 400          # px
PX_M = .25          # m per px -> 100 m frame, well inside one z17 tile (~241 m here)
TILE = (118442, 80417)


def frame(tile_x, tile_y):
    lon, lat = tile_xy_to_lonlat(tile_x, tile_y, 17)
    half = SIZE / 2 * PX_M
    return {'centroid': {'lon': lon, 'lat': lat}, 'image_width': SIZE, 'image_height': SIZE,
            'pixel_to_meters': [[PX_M, 0], [0, -PX_M], [-half, half]],
            'meters_to_pixel': [[1 / PX_M, 0], [0, -1 / PX_M], [SIZE / 2, SIZE / 2]]}


def square(x, y, size=40):
    return [[x, y], [x + size, y], [x + size, y + size], [x, y + size]]


def building(oid, ring, status='candidate', source='sam3-text-onnx', **extra):
    return {'id': oid, 'kind': 'building', 'label': 'Building', 'ring': ring, 'status': status,
            'geometry_reviewed': False, 'source': source, 'score': .8, 'model': {}, **extra}


def write_scene(root, scene_id, tile_xy, objects, review_objects=None, scene_extra=None):
    rng = np.random.default_rng(1)
    pixels = rng.integers(0, 255, (SIZE, SIZE, 3), dtype=np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(pixels).save(buffer, format='PNG')
    data = buffer.getvalue()
    sha = hashlib.sha256(data).hexdigest()
    scene = {'schema_version': 1, 'id': scene_id, **frame(*tile_xy), 'image_sha256': sha, 'objects': objects,
             'groups': [], 'scans': {}, 'title': scene_id, **(scene_extra or {})}
    signature = hashlib.sha256(json.dumps(scene, sort_keys=True).encode()).hexdigest()
    scene['model_signature'] = signature
    folder = root / 'data/scenes' / scene_id
    (folder / 'models').mkdir(parents=True)
    (folder / f'{sha}.png').write_bytes(data)
    (folder / 'latest.json').write_text(json.dumps(scene))
    (folder / 'models' / f'{signature}.json').write_text(json.dumps(scene))
    if review_objects is not None:
        (folder / 'review.json').write_text(json.dumps({'revision': 1, 'model_signature': signature, 'objects': review_objects,
                                                        'updated_at': '2026-09-27T00:00:00Z'}))
    return scene


def make_repo(root):
    x, y = TILE
    # Reference tile: a scene with human decisions of every kind.
    model_objects = [building('conf', square(20, 20)), building('edit', square(100, 20)), building('lines', square(200, 20)),
                     building('machine', square(300, 20)), building('bad', square(20, 120)), building('unedited', square(100, 120)),
                     building('plain', square(200, 120)), building('seed', square(300, 120), source='roof-pipeline')]
    review = [building('conf', square(20, 20), 'confirmed', geometry_reviewed=True),
              building('edit', square(104, 24), 'needs_adjustment'),
              building('lines', square(200, 20), 'needs_adjustment', roof_line_edits={'vertices': [[0, 0]], 'edges': [], 'checked': False}),
              building('machine', square(300, 20), 'confirmed', geometry_reviewed=True, provenance={'source': 'machine:decision-policy-v2'}),
              building('bad', square(20, 120), 'rejected'),
              building('unedited', square(100, 120), 'needs_adjustment'),
              building('manual', square(20, 220), 'confirmed', geometry_reviewed=True)]
    write_scene(root, 'ref-scene', (x + .5, y + .5), model_objects, review)
    # A neighbour whose centre sits in the next tile west but whose frame crosses 24 m into the reference tile.
    write_scene(root, 'edge-scene', (x - .05, y + .5), [building('inside', square(20, 180)), building('crossing', square(340, 180))])
    # Two far tiles with only pseudo-labels; far-a also carries a machine label on an otherwise unreviewed tile.
    write_scene(root, 'far-a', (x + 10.5, y + .5), [building('a1', square(20, 20)), building('a2', square(100, 100)),
                                                    building('a3', square(250, 250))],
                [building('a3', square(250, 250), 'confirmed', geometry_reviewed=True, provenance={'source': 'machine:decision-policy-v2'})])
    # far-b also carries a seed whose straight edge is an earlier 220 px frame line (context expansion), not the roof.
    write_scene(root, 'far-b', (x + 20.5, y + .5), [building('b1', square(20, 20)),
                                                    building('b2', square(180, 200), clipped_by_frame=[0, 0, 220, 400])])
    return root


def export(tmp_path, name='out', **kwargs):
    return export_dataset.export(make_repo_once(tmp_path), tmp_path / name, seed=7, **kwargs)


def make_repo_once(tmp_path):
    if not (tmp_path / 'data/scenes').exists():
        make_repo(tmp_path)
    return tmp_path


def label_at(out, manifest, scene_id, x, y):
    image = next(i for i in manifest['images'] if i['scene_id'] == scene_id)
    return int(np.asarray(Image.open(out / image['label_file']))[y, x])


def test_split_by_tile_and_provenance_weights(tmp_path):
    manifest = export(tmp_path)
    splits = {img['scene_id']: img['split'] for img in manifest['images']}
    assert splits['ref-scene'] == 'test'
    assert splits['edge-scene'] != 'test' and splits['far-a'] != 'test' and splits['far-b'] != 'test'
    test = json.loads((tmp_path / 'out' / 'test.json').read_text())
    by_id = {a['object_id']: a for a in test['annotations']}
    assert by_id['conf']['provenance'] == 'human_T1' and by_id['conf']['weight'] == 10. and by_id['conf']['boundary_reference']
    assert by_id['edit']['provenance'] == 'human_T2' and by_id['edit']['weight'] == 8. and by_id['edit']['outline_edited']
    assert by_id['edit']['bbox'][:2] == [104., 24.]  # the human ring, not the model ring
    assert by_id['lines']['provenance'] == 'human_T2_lines_only' and by_id['lines']['weight'] == 3.
    assert not by_id['lines']['boundary_reference']   # its outline is SAM3's, never a boundary reference
    assert by_id['manual']['provenance'] == 'human_T1'
    assert by_id['plain']['provenance'] == 'machine_pseudo' and by_id['plain']['weight'] == 1.
    for excluded in ('machine', 'bad', 'unedited', 'seed'):
        assert excluded not in by_id
    assert manifest['counts']['test']['human_T1'] == 2 and manifest['counts']['test']['_images_with_human_reference'] == 1
    assert all(img['split'] == 'test' for img in test['images'])


def test_machine_provenance_is_excluded_and_ignored_not_background(tmp_path):
    manifest = export(tmp_path)
    out = tmp_path / 'out'
    assert manifest['exclusions']['machine_provenance'] == 2      # one on the reference tile, one on far-a
    # far-a's machine-labelled roof is neither a training instance nor background: its pixels are IGNORE.
    split = next(i['split'] for i in manifest['images'] if i['scene_id'] == 'far-a')
    ids = {a['object_id'] for a in json.loads((out / f'{split}.json').read_text())['annotations'] if a['scene_id'] == 'far-a'}
    assert ids == {'a1', 'a2'}
    assert label_at(out, manifest, 'far-a', 270, 270) == common.IGNORE
    # Rejected stays background; unedited needs_adjustment and machine labels are IGNORE.
    assert label_at(out, manifest, 'ref-scene', 40, 140) == 0
    assert label_at(out, manifest, 'ref-scene', 120, 140) == common.IGNORE
    assert label_at(out, manifest, 'ref-scene', 320, 40) == common.IGNORE
    # An uncovered roof-pipeline seed is IGNORE, never background.
    assert label_at(out, manifest, 'ref-scene', 320, 140) == common.IGNORE
    assert manifest['ignore_reasons']['machine_provenance'] == 2
    card = (out / 'DATASET_CARD.md').read_text()
    assert '2 review objects with machine provenance' in card


def test_train_scene_touching_test_tile_drops_instances_and_writes_ignore(tmp_path):
    manifest = export(tmp_path)
    out = tmp_path / 'out'
    edge = next(img for img in manifest['images'] if img['scene_id'] == 'edge-scene')
    assert edge['test_tiles_ignored'] == 1 and edge['n_instances'] == 1
    label = np.asarray(Image.open(out / edge['label_file']))
    assert label.dtype == np.uint16
    assert (label == common.IGNORE).any() and (label == 1).any()
    assert (label[:, :200] == common.IGNORE).sum() == 0   # the strip is on the east side only
    # The dropped crossing roof is IGNORE on the train side of the boundary too (no false background).
    assert label[190, 340] == common.IGNORE
    split_json = json.loads((out / f"{edge['split']}.json").read_text())
    assert {a['object_id'] for a in split_json['annotations'] if a['scene_id'] == 'edge-scene'} == {'inside'}


def test_split_does_not_depend_on_scene_selection(tmp_path):
    full = export(tmp_path, 'full')
    alone = export(tmp_path, 'alone', scene_ids=['edge-scene', 'far-a'])
    full_split = {i['scene_id']: i['split'] for i in full['images']}
    assert {i['scene_id']: i['split'] for i in alone['images']} == {k: full_split[k] for k in ('edge-scene', 'far-a')}
    # The reference tile is still known when its scene is not exported, so the strip is still IGNORE.
    edge = next(i for i in alone['images'] if i['scene_id'] == 'edge-scene')
    assert edge['test_tiles_ignored'] == 1
    assert alone['counts']['test'] == {}


def test_export_is_deterministic_and_hashes_match(tmp_path):
    first = export(tmp_path, 'one')
    second = export(tmp_path, 'two')
    assert [(i['scene_id'], i['split']) for i in first['images']] == [(i['scene_id'], i['split']) for i in second['images']]
    for image in first['images']:
        assert common.sha256_file(tmp_path / 'one' / image['file']) == image['sha256']
        assert common.sha256_file(tmp_path / 'one' / image['label_file']) == image['label_sha256']
    for name, digest in first['files'].items():
        assert common.sha256_file(tmp_path / 'one' / name) == digest
    # Label rasters are byte-identical across runs (JSON files differ only by their created_at stamps).
    assert [i['label_sha256'] for i in first['images']] == [i['label_sha256'] for i in second['images']]
    other = export_dataset.export(tmp_path, tmp_path / 'three', seed=8)
    assert {i['scene_id'] for i in other['images'] if i['split'] == 'test'} == {'ref-scene'}
    card = (tmp_path / 'one' / 'DATASET_CARD.md').read_text()
    assert 'held-out test' in card and 'human_T1' in card and ' of ' in card


def test_label_index_matches_annotation_and_references_file(tmp_path):
    export(tmp_path)
    out = tmp_path / 'out'
    test = json.loads((out / 'test.json').read_text())
    image = test['images'][0]
    label = np.asarray(Image.open(out / image['label_file']))
    for annotation in test['annotations']:
        polygon = common.coco_to_polygon(annotation)
        assert label[int(polygon.centroid.y), int(polygon.centroid.x)] == annotation['label_index']
    refs = json.loads((out / 'references.json').read_text())['references']
    assert {r['object_id'] for r in refs} == {'conf', 'edit', 'lines', 'manual'}
    assert {r['object_id'] for r in refs if r['boundary_reference']} == {'conf', 'edit', 'manual'}
    control = json.loads((out / 'sam3_control.json').read_text())['images']
    assert set(control) == {'ref-scene'}
    assert {r['object_id'] for r in control['ref-scene']} == {'conf', 'edit', 'lines', 'machine', 'bad', 'unedited', 'plain'}


def test_eyes_references_merge_place_and_respect_newer_reviews(tmp_path):
    root = make_repo(tmp_path)
    scene = json.loads((root / 'data/scenes/ref-scene/latest.json').read_text())
    polygon = registry.valid(registry.local_polygon({'ring': square(100, 300)}, scene))[0]

    def row(ref_id, tier, scene_id, object_id, geometry_local=True, image=None):
        from scripts.ml.scene_confidence import geographic
        from shapely.geometry import mapping
        wgs = geographic({'ring': square(100, 300)}, scene)
        payload = {'object_kind': 'building', 'independent_geometry': True, 'provenance': 'review:confirmed',
                   'geometry_wgs84': mapping(wgs), 'geometry_local_m': mapping(polygon) if geometry_local else None,
                   'image_sha256': scene['image_sha256'] if geometry_local else None, 'boundary_image_sha256': image}
        return {'ref_id': ref_id, 'tier': tier, 'source_kind': 'review' if scene_id else 'crm', 'scene_id': scene_id,
                'object_id': object_id, 'spatial_group': registry.spatial_group(wgs), 'split': 'train',
                'west': 0, 'south': 0, 'east': 0, 'north': 0, 'version': registry.VERSION, 'payload': payload}
    with closing(eyes_index.connection(root)) as db:
        eyes_index.ensure_schema(db)
        eyes_index.replace_rows(db, 'references', [
            row('r-conf', 'T1', 'ref-scene', 'conf'),            # joins the review.json instance (ref_id attached)
            row('r-machine', 'T1', 'ref-scene', 'machine'),      # stale: review.json now says machine label
            row('r-gone', 'T2', 'ref-scene', 'gone'),            # object no longer in review.json: added from eyes
            row('r-crm', 'T2', None, None, geometry_local=False, image='f' * 64)])   # WGS84-only, another image
    manifest = export_dataset.export(root, tmp_path / 'out', seed=7)
    test = json.loads((tmp_path / 'out' / 'test.json').read_text())
    by_id = {a['object_id']: a for a in test['annotations']}
    assert by_id['conf']['ref_id'] == 'r-conf'
    assert 'machine' not in by_id and manifest['exclusions']['eyes_ref_superseded_by_review'] == 1
    assert by_id['gone']['source'] == 'eyes.references' and by_id['gone']['provenance'] == 'human_T2'
    assert by_id['gone']['bbox'][:2] == [100., 300.]      # projected with reference_agreement into pixels
    crm = by_id['ref-r-crm']
    assert crm['image_identity'] == 'different_image' and crm['evaluation_only'] and crm['label_index'] is None
    assert abs(crm['bbox'][0] - 100.) < .5 and abs(crm['bbox'][1] - 300.) < .5
    assert manifest['n_boundary_references_same_image'] == manifest['n_boundary_references'] - 1


def test_scene_ids_and_limit_restrict_export(tmp_path):
    manifest = export(tmp_path, scene_ids=['far-a', 'missing'])
    assert [i['scene_id'] for i in manifest['images']] == ['far-a']
    assert manifest['counts']['test'] == {}
    assert Path(tmp_path / 'out' / 'test.json').exists()


def test_seed_cut_by_an_earlier_frame_is_ignore_not_a_pseudo_label(tmp_path):
    manifest = export(tmp_path)
    out = tmp_path / 'out'
    split = next(i['split'] for i in manifest['images'] if i['scene_id'] == 'far-b')
    ids = {a['object_id'] for a in json.loads((out / f'{split}.json').read_text())['annotations'] if a['scene_id'] == 'far-b'}
    assert ids == {'b1'}, 'the frame-cut seed is not taught as a roof outline'
    assert label_at(out, manifest, 'far-b', 200, 220) == common.IGNORE, 'nor as background'
    assert label_at(out, manifest, 'far-b', 300, 300) == 0
    assert manifest['exclusions']['clipped_by_frame'] == 1 and manifest['ignore_reasons']['clipped_by_frame'] == 1


def test_human_instances_carry_the_model_outline_they_were_edited_from(tmp_path):
    manifest = export(tmp_path)
    test = json.loads((tmp_path / 'out' / 'test.json').read_text())
    by_id = {a['object_id']: a for a in test['annotations']}
    model_coco = common.polygon_to_coco(common.ring_polygon(square(100, 20)))
    assert by_id['edit']['segmentation_model'] == model_coco['segmentation'] and by_id['edit']['holes_model'] == []
    assert by_id['edit']['edit_kind'] == 'outline' and by_id['edit']['segmentation'] != by_id['edit']['segmentation_model']
    assert by_id['conf']['edit_kind'] == 'none' and by_id['conf']['segmentation_model'] == by_id['conf']['segmentation']
    assert by_id['lines']['edit_kind'] == 'none' and by_id['lines']['segmentation_model'] is not None
    assert by_id['manual']['edit_kind'] == 'manual' and by_id['manual']['segmentation_model'] is None
    assert by_id['plain']['edit_kind'] is None and by_id['plain']['segmentation_model'] is None   # pseudo-labels have no pair
    assert 'machine' not in by_id
    # The pair numbers are agreement between the reviewer outline and the model estimate it replaced, not accuracy.
    assert abs(by_id['edit']['model_hausdorff_m'] - 4 * math.sqrt(2) * PX_M) < 1e-3
    assert abs(by_id['edit']['model_iou'] - 1296 / 1904) < 1e-4 and by_id['edit']['model_vertex_delta'] == 0
    assert by_id['edit']['holes_changed'] is False and by_id['edit']['outline_edited'] is True
    assert by_id['conf']['model_iou'] == 1. and by_id['conf']['model_hausdorff_m'] == 0. and by_id['conf']['holes_changed'] is False
    assert by_id['manual']['model_iou'] is None and by_id['manual']['model_hausdorff_m'] is None and by_id['manual']['holes_changed'] is None
    assert by_id['plain']['model_iou'] is None
    assert manifest['n_human_with_model_pair'] == 3 and manifest['n_outline_edited_with_model_pair'] == 1
    assert abs(manifest['model_iou_median'] - 1296 / 1904) < 1e-4 and abs(manifest['model_hausdorff_m_median'] - 4 * math.sqrt(2) * PX_M) < 1e-3
    assert manifest['schema_additions'] == ['segmentation_model', 'holes_model', 'model_iou', 'model_hausdorff_m',
                                            'model_vertex_delta', 'holes_changed', 'edit_kind']
    refs = {r['object_id']: r for r in json.loads((tmp_path / 'out' / 'references.json').read_text())['references']}
    assert refs['edit']['segmentation_model'] == model_coco['segmentation'] and refs['edit']['model_hausdorff_m'] == by_id['edit']['model_hausdorff_m']
    card = (tmp_path / 'out' / 'DATASET_CARD.md').read_text()
    assert '3 human outlines carry the model estimate' in card and 'not accuracy' in card
    assert all('not accuracy' in line or 'an accuracy' in line for line in card.splitlines() if 'accuracy' in line)


def test_hole_only_edit_is_a_human_t2_label_not_ignore(tmp_path):
    """Add interior gap then Save outline · Still needs work: ring unchanged, one hole added -> human_T2, not IGNORE."""
    root = make_repo_once(tmp_path)
    folder = root / 'data/scenes/ref-scene'
    review = json.loads((folder / 'review.json').read_text())
    hole = [[110, 130], [120, 130], [120, 140], [110, 140]]
    for o in review['objects']:
        if o['id'] == 'unedited':
            o['holes'] = [hole]
    (folder / 'review.json').write_text(json.dumps(review))
    manifest = export_dataset.export(root, tmp_path / 'holes', seed=7)
    test = json.loads((tmp_path / 'holes' / 'test.json').read_text())
    gap = next(a for a in test['annotations'] if a['object_id'] == 'unedited')
    assert gap['provenance'] == 'human_T2' and gap['registry_provenance'] == 'review:needs_adjustment_edited'
    assert gap['edit_kind'] == 'holes_only' and gap['holes_changed'] is True and gap['outline_edited'] is True
    assert gap['holes'] and gap['holes_model'] == [] and gap['model_iou'] < 1.
    assert manifest['exclusions'].get('needs_adjustment_unedited', 0) == 0
    assert label_at(tmp_path / 'holes', manifest, 'ref-scene', 115, 135) == 0, 'the gap is background inside the roof'
    assert label_at(tmp_path / 'holes', manifest, 'ref-scene', 105, 125) == gap['label_index']


# Frame-cut objects on window scenes (lot-*, win-*: latest.json carries window_frame). The 512 px window cuts roofs
# at its edge; the straight edge is the frame, not the roof, so such outlines are IGNORE, never labels or background.
WINDOW_FRAME = {'version': 'frame-cut-window-v1'}
# Captured from the unmodified module (before the frame_cut rule) on write_window_scene(with_window=False).
BASELINE_NON_WINDOW = {'split': 'test', 'n_instances': 3, 'ignore_pixels': 0,
                       'label_sha256': '1f5c60bb78382b369aa1ae3c236682c874d1aa4a444c55897a4e928fac65dc8f',
                       'annotations': [('cut-cand', 'machine_pseudo'), ('cut-conf', 'human_T1'), ('inner', 'machine_pseudo')],
                       'labels': {(20, 40): 2, (220, 380): 1, (220, 120): 3}}


def write_window_scene(root, with_window):
    """A far tile: a confirmed human ring touching x=0, a candidate pseudo ring touching y=height, one interior roof."""
    x, y = TILE
    model = [building('cut-conf', square(0, 20)), building('cut-cand', square(200, SIZE - 40)), building('inner', square(200, 100))]
    review = [building('cut-conf', square(0, 20), 'confirmed', geometry_reviewed=True)]
    write_scene(root, 'win-scene', (x + 30.5, y + .5), model, review, scene_extra={'window_frame': WINDOW_FRAME} if with_window else None)
    return root


def _scene_annotations(out, scene_id):
    return sorted((a['object_id'], a['provenance']) for split in ('train', 'val', 'test')
                  for a in json.loads((out / f'{split}.json').read_text())['annotations'] if a['scene_id'] == scene_id)


def test_frame_cut_helper_uses_the_scene_lot_tags_edge_tolerance():
    assert export_dataset.FRAME_CUT_EDGE_PX == scene_lot_tags.DESIGN['edge_px'] == 1.
    edge = export_dataset.FRAME_CUT_EDGE_PX
    assert export_dataset.frame_cut(common.ring_polygon(square(0, 20)), SIZE, SIZE, edge), 'touches x=0'
    assert export_dataset.frame_cut(common.ring_polygon(square(200, SIZE - 40)), SIZE, SIZE, edge), 'touches y=height'
    assert export_dataset.frame_cut(common.ring_polygon(square(.5, 20)), SIZE, SIZE, edge), '0.5 px inside the edge'
    assert not export_dataset.frame_cut(common.ring_polygon(square(1.5, 20)), SIZE, SIZE, edge), '1.5 px inside the edge'
    assert export_dataset.frame_cut(common.ring_polygon(square(SIZE - 20, 20)), SIZE, SIZE, edge), 'not covered by the frame'
    assert export_dataset.frame_cut(common.ring_polygon(square(SIZE + 10, 20)), SIZE, SIZE, edge), 'wholly outside the frame'
    assert not export_dataset.frame_cut(common.ring_polygon(square(200, 100)), SIZE, SIZE, edge)


def test_window_scene_frame_cut_objects_are_ignore_not_labels_or_background(tmp_path):
    root = write_window_scene(make_repo_once(tmp_path), with_window=True)
    manifest = export_dataset.export(root, tmp_path / 'out', seed=7)
    out = tmp_path / 'out'
    assert _scene_annotations(out, 'win-scene') == [('inner', 'machine_pseudo')], 'neither frame-cut object is a label'
    assert label_at(out, manifest, 'win-scene', 20, 40) == common.IGNORE, 'the confirmed human ring touching x=0 is IGNORE'
    assert label_at(out, manifest, 'win-scene', 220, SIZE - 20) == common.IGNORE, 'the candidate ring touching y=height is IGNORE'
    assert label_at(out, manifest, 'win-scene', 220, 120) == 1, 'the interior roof is still the one label'
    assert label_at(out, manifest, 'win-scene', 100, 300) == 0, 'open ground stays background'
    assert manifest['exclusions']['frame_cut_human'] == 1 and manifest['exclusions']['frame_cut'] == 1
    assert manifest['ignore_reasons']['frame_cut'] == 2
    image = next(i for i in manifest['images'] if i['scene_id'] == 'win-scene')
    assert image['n_instances'] == 1 and image['ignore_pixels'] > 0
    refs = json.loads((out / 'references.json').read_text())['references']
    assert not [r for r in refs if r['scene_id'] == 'win-scene'], 'a frame-cut human outline is not a reference'
    assert manifest['counts'][image['split']].get('human_T1', 0) == (2 if image['split'] == 'test' else 0)
    card = (out / 'DATASET_CARD.md').read_text()
    assert 'frame_cut' in card and '1 human outlines' in card and 'window' in card


def test_non_window_scene_with_frame_touching_objects_exports_exactly_as_before(tmp_path):
    root = write_window_scene(make_repo_once(tmp_path), with_window=False)
    manifest = export_dataset.export(root, tmp_path / 'out', seed=7)
    out = tmp_path / 'out'
    image = next(i for i in manifest['images'] if i['scene_id'] == 'win-scene')
    assert {k: image[k] for k in ('split', 'n_instances', 'ignore_pixels', 'label_sha256')} == {
        k: BASELINE_NON_WINDOW[k] for k in ('split', 'n_instances', 'ignore_pixels', 'label_sha256')}
    assert _scene_annotations(out, 'win-scene') == BASELINE_NON_WINDOW['annotations']
    assert {xy: label_at(out, manifest, 'win-scene', *xy) for xy in BASELINE_NON_WINDOW['labels']} == BASELINE_NON_WINDOW['labels']
    assert 'frame_cut' not in manifest['exclusions'] and 'frame_cut_human' not in manifest['exclusions']
    assert 'frame_cut' not in manifest['ignore_reasons']
    refs = json.loads((out / 'references.json').read_text())['references']
    assert [r['object_id'] for r in refs if r['scene_id'] == 'win-scene'] == ['cut-conf']
