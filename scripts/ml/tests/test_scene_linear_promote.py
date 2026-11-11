"""scene_linear_promote: the promotion path of the linear-continuity merge, proven on a synthetic window in a temporary root.

The window is drawn in metres at 0.1 m/px (the shape the scenes store), its snapshot written exactly as scene_extract.save_scene
writes one (indent=2, signature = sha256 of the sorted-key JSON without model_signature), its review as the app writes one.
The carry runs the editor's own recoverSceneReview through scripts/scene-linear-carry.mjs (node + typescript from the repo).
Every expectation is a byte, a count, an id or a status; nothing here is accuracy. No model is loaded; nothing under the repo's
data/ is read or written (root = tmp_path).
"""
import json
import shutil
import subprocess
import sys

import pytest
from scripts.ml import scene_linear_promote as P
from shapely.geometry import Polygon

AFFINE = [[.1, 0.], [0., -.1], [0., 50.]]            # pixel -> metres: x_m = .1 px, y_m = 50 - .1 py
INVERSE = [[10., 0.], [0., -10.], [0., 500.]]        # metres -> pixels
NODE = shutil.which('node')
needs_node = pytest.mark.skipif(NODE is None or not (P.REPO/'node_modules/typescript').exists(), reason='node + typescript needed for the carry')


def px(points_m):
    return [[round(x/.1, 2), round((50.-y)/.1, 2)] for x, y in points_m]


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def obj(oid, kind, ring_m, status='candidate', score=.5, **extra):
    return {'id': oid, 'kind': kind, 'label': kind.capitalize(), 'ring': px(ring_m), 'status': status, 'geometry_reviewed': False,
            'source': 'sam3-text-onnx', 'score': score, 'prompt': kind,
            'model': {'height_m': .05, 'base_m': 0., 'height_source': 'class-prior', 'height_verified': False, 'width_m': 1., 'length_m': 2.,
                      'area_m2': round(Polygon(ring_m).area, 2), 'rotation_deg': 0., 'color': '#808080', 'footprint_area_px': 1.}, **extra}


def window(root, scene_id='win-0000000000000001', objects=None, review_objects=None, revision=3, groups=None):
    """A window folder as the store holds one; returns (latest dict, review dict)."""
    folder = root/'data/scenes'/scene_id
    (folder/'models').mkdir(parents=True)
    objects = objects if objects is not None else [
        obj('aaaaaaaaaaaaaaaa', 'driveway', rect(10, 20, 20, 23), score=.4),          # 30 m2 and 29.1 m2 pieces 0.3 m apart: one join
        obj('bbbbbbbbbbbbbbbb', 'driveway', rect(20.3, 20, 30, 23), score=.4),
        obj('cccccccccccccccc', 'footpath', rect(10, 30, 20, 31.5), score=.6),
        obj('dddddddddddddddd', 'building', rect(30, 30, 40, 38), score=.9),
        obj('eeeeeeeeeeeeeeee', 'tree', rect(5, 5, 8, 8), score=.7)]
    scene = {'schema_version': 1, 'id': scene_id, 'address_id': 29772, 'cohort': 'windows', 'title': 'Synthetic window', 'centroid': [145.1, -37.8],
             'image_sha256': 'ab'*32, 'image_width': 500, 'image_height': 500, 'pixel_to_meters': AFFINE, 'meters_to_pixel': INVERSE,
             'objects': objects, 'groups': groups if groups is not None else [{'id': 'dddddddddddddddd', 'label': 'Building'}], 'scans': {'driveway': {'status': 'complete', 'count': 2, 'prompt': 'driveway'}},
             'region': {'provider': 'google', 'imagery_zoom': 21}, 'frame_transform': {'source_scene_id': 'roof_training_250-29772', 'source_model_signature': 'cd'*32, 'verified': True},
             'window_frame': {'lots': ['1\\PS123456'], 'size_px': 500}, 'property_roofs': {'version': 'pr-v1', 'roofs': []},
             'analysis': {'input_geometry': 'imagery-only'}, 'height_note': 'Heights are class priors.'}
    scene['model_signature'] = P.signature(scene)
    text = json.dumps(scene, indent=2)
    (folder/'latest.json').write_text(text)
    (folder/'models'/f"{scene['model_signature']}.json").write_text(text)
    review = None
    if review_objects is not False:
        review = {'revision': revision, 'model_signature': scene['model_signature'],
                  'objects': review_objects if review_objects is not None else json.loads(json.dumps(objects)),
                  'groups': list(scene['groups']), 'reviewed_classes': [], 'basemap_mode': 'raw', 'updated_at': '2026-10-02T00:00:00Z', 'updated_by': 'local'}
        (folder/'review.json').write_text(json.dumps(review, indent=2))
    return scene, review


def decided(o, status='confirmed', **extra):
    return {**o, 'status': status, 'geometry_reviewed': status == 'confirmed', **extra}


def files_under(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_canonical_treats_integral_floats_as_ints_like_json_stringify():
    assert P.canonical({'ring': [[409.0, 152.0]], 'base_m': 0.0, 'n': -0.0}) == P.canonical({'ring': [[409, 152]], 'base_m': 0, 'n': 0})
    assert P.canonical({'a': 1.5}) != P.canonical({'a': 1})


def test_signature_matches_save_scene_formula():
    scene = {'id': 'x', 'objects': [], 'model_signature': 'stale'}
    import hashlib
    assert P.signature(scene) == hashlib.sha256(json.dumps({'id': 'x', 'objects': []}, sort_keys=True).encode()).hexdigest()


def test_merge_view_overlays_review_decisions_and_blocks_review_only_candidates(tmp_path):
    scene, review = window(tmp_path)
    review['objects'][0] = decided(review['objects'][0])                                   # the reviewer confirmed piece a
    review['objects'].append(obj('ffffffffffffffff', 'driveway', rect(40, 40, 42, 41)))     # reviewer-drawn candidate, not in the model
    view, info = P.merge_view(scene['objects'], review['objects'])
    by_id = {o['id']: o for o in view}
    assert by_id['aaaaaaaaaaaaaaaa']['status'] == 'confirmed' and by_id['bbbbbbbbbbbbbbbb']['status'] == 'candidate'
    assert by_id['ffffffffffffffff']['status'] == 'needs_adjustment' and info == {'review_only': ['ffffffffffffffff'], 'review_only_candidates_blocked': ['ffffffffffffffff']}
    plan = P.plan_merge(scene, review)
    assert plan['nothing_to_merge'] and plan['refused_features'] == [] and plan['refusals_by_reason'] == {}   # a decided piece is not a candidate
    with pytest.raises(P.Refusal, match='review_missing_model_objects'):
        P.merge_view(scene['objects'], review['objects'][1:])


def test_plan_and_snapshot_drop_members_keep_fields_and_recompute_signature(tmp_path):
    scene, review = window(tmp_path)
    plan = P.plan_merge(scene, review)
    assert not plan['nothing_to_merge'] and plan['members'] == ['aaaaaaaaaaaaaaaa', 'bbbbbbbbbbbbbbbb'] and len(plan['features']) == 1
    feature, row = plan['features'][0], plan['feature_rows'][0]
    assert feature['status'] == 'candidate' and feature['source'] == 'linear-merge-v1' and feature['kind'] == 'driveway'
    assert feature['replaces'] == ['aaaaaaaaaaaaaaaa', 'bbbbbbbbbbbbbbbb'] and [m['id'] for m in feature['merged_from']] == feature['replaces'] and 'derived_from' not in feature
    assert row['refused'] == [] and row['largest_member_m2'] == 30. and row['area_m2'] >= 59.1 and row['bridge_m2'] is not None
    new = P.build_snapshot(scene, plan['features'], plan['members'])
    for key in scene:
        if key not in ('objects', 'model_signature'):
            assert json.dumps(new[key], sort_keys=True) == json.dumps(scene[key], sort_keys=True), key
    ids = [o['id'] for o in new['objects']]
    assert ids == ['cccccccccccccccc', 'dddddddddddddddd', 'eeeeeeeeeeeeeeee', feature['id']]
    assert new['model_signature'] == P.signature(new) != scene['model_signature'] and list(new)[-1] == 'model_signature'
    assert json.loads(P.snapshot_bytes(new)) == new


def test_model_layer_feature_carries_replaces_and_merged_from_but_no_derived_from(tmp_path):
    scene, review = window(tmp_path)
    plan = P.plan_merge(scene, review)
    feature = plan['features'][0]
    assert 'derived_from' not in feature and feature['replaces'] == ['aaaaaaaaaaaaaaaa', 'bbbbbbbbbbbbbbbb']
    assert [m['id'] for m in feature['merged_from']] == feature['replaces'] and list(P.MODEL_LAYER_LINEAGE) == ['replaces', 'merged_from']
    snapshot = P.build_snapshot(scene, plan['features'], plan['members'])
    assert all('derived_from' not in o for o in snapshot['objects']) and 'replaces' in snapshot['objects'][-1]
    copy = P.review_layer_feature(feature)                                                   # the review layer's copy carries the pair's other end
    keys = list(copy)
    assert copy['derived_from'] == feature['replaces'] and keys[keys.index('replaces')+1] == 'derived_from'
    assert {k: v for k, v in copy.items() if k != 'derived_from'} == feature
    assert P.review_layer_feature(copy) == copy                                              # idempotent


def test_result_floor_refuses_a_speck_chain(tmp_path):
    specks = [obj('aaaaaaaaaaaaaaaa', 'driveway', rect(10, 20, 10.9, 20.9), score=.4),      # 0.81 + 0.81 m2, 0.1 m apart: the
              obj('bbbbbbbbbbbbbbbb', 'driveway', rect(11, 20, 11.9, 20.9), score=.4)]      # feature passes 1 m2, no member does
    scene, review = window(tmp_path, objects=specks)
    plan = P.plan_merge(scene, review)
    # the module refuses the cluster itself ('result_floor', stream A); the promotion's own floor is the fallback when it does not
    assert plan['nothing_to_merge']
    assert plan['refusals_by_reason'].get('result_floor') == 1 or [r['refused'] for r in plan['refused_features']] == [['below_result_floor']]


def test_nothing_to_merge_leaves_the_window_untouched(tmp_path):
    scene, review = window(tmp_path)
    review['objects'][0] = decided(review['objects'][0])
    review['objects'][1] = decided(review['objects'][1])
    (tmp_path/'data/scenes'/scene['id']/'review.json').write_text(json.dumps(review, indent=2))
    before = files_under(tmp_path/'data')
    record = P.promote(scene['id'], root=tmp_path, run_dir=tmp_path/'run', registry=tmp_path/'run/registry.jsonl', lot_tags=False, scratch=False, log=lambda _: None)
    assert record['outcome'] == 'nothing_to_merge' and files_under(tmp_path/'data') == before
    assert (tmp_path/'run'/scene['id']/'promote.json').exists() and not (tmp_path/'run/registry.jsonl').exists()


@needs_node
def test_dry_run_writes_nothing_under_data_scenes(tmp_path):
    scene, review = window(tmp_path)
    before = files_under(tmp_path/'data')
    record = P.promote(scene['id'], root=tmp_path, run_dir=tmp_path/'run', registry=tmp_path/'run/registry.jsonl', lot_tags=False, dry_run=True, log=lambda _: None)
    assert record['outcome'] == 'dry_run' and files_under(tmp_path/'data') == before
    out = tmp_path/'run'/scene['id']
    assert sorted(p.name for p in out.iterdir()) == ['latest.before.json', 'latest.dry.json', 'promote.dry.json', 'review.before.json', 'review.dry.json']
    planned = json.loads((out/'review.dry.json').read_text())
    assert planned['revision'] == 4 and planned['updated_by'] == P.UPDATED_BY and planned['model_signature'] == record['new_model_signature']


@needs_node
def test_promote_then_rollback_restores_bytes(tmp_path):
    scene, review = window(tmp_path)
    review['objects'][3] = decided(review['objects'][3], notes='good roof')                   # a confirmed building, unchanged evidence
    review['objects'][4] = decided(review['objects'][4], status='rejected')
    review['objects'][1] = {**review['objects'][1], 'notes': 'looks like the same pad'}        # a touched candidate member (the editor inherits it)
    folder = tmp_path/'data/scenes'/scene['id']
    (folder/'review.json').write_text(json.dumps(review, indent=2))
    before = files_under(tmp_path/'data')
    registry = tmp_path/'run/registry.jsonl'
    record = P.promote(scene['id'], root=tmp_path, run_dir=tmp_path/'run', registry=registry, lot_tags=False, scratch=False, log=lambda _: None)
    assert record['outcome'] == 'promoted'
    new_sig = record['new_model_signature']
    latest = json.loads((folder/'latest.json').read_text())
    assert latest['model_signature'] == new_sig and (folder/'models'/f'{new_sig}.json').read_bytes() == (folder/'latest.json').read_bytes()
    assert (folder/'models'/f"{scene['model_signature']}.json").read_bytes() == before[f"scenes/{scene['id']}/models/{scene['model_signature']}.json"]
    feature_id = record['features'][0]['id']
    assert [o['id'] for o in latest['objects']] == ['cccccccccccccccc', 'dddddddddddddddd', 'eeeeeeeeeeeeeeee', feature_id]
    model_feature = latest['objects'][-1]
    assert 'derived_from' not in model_feature and model_feature['replaces'] == ['aaaaaaaaaaaaaaaa', 'bbbbbbbbbbbbbbbb'] and len(model_feature['merged_from']) == 2
    assert record['restore_verbatim'] is True and record['restored_verbatim'] == [] and record['model_layer_lineage'] == ['replaces', 'merged_from']
    carried = json.loads((folder/'review.json').read_text())
    assert carried['revision'] == 4 and carried['updated_by'] == P.UPDATED_BY and carried['model_signature'] == new_sig
    by_id = {o['id']: o for o in carried['objects']}
    assert P.canonical(by_id['dddddddddddddddd']) == P.canonical(review['objects'][3])          # decided, byte-identical
    assert P.canonical(by_id['eeeeeeeeeeeeeeee']) == P.canonical(review['objects'][4])
    assert P.canonical(by_id['cccccccccccccccc']) == P.canonical(review['objects'][2])          # untouched candidate, the model's dict
    for member in ('aaaaaaaaaaaaaaaa', 'bbbbbbbbbbbbbbbb'):                                        # members stay in the review layer as
        assert by_id[member]['status'] == 'superseded' and by_id[member]['replacement_ids'] == [feature_id]   # superseded -> feature
        assert P.canonical({k: v for k, v in by_id[member].items() if k not in ('status', 'replacement_ids')}) == P.canonical({k: v for k, v in review['objects'][['aaaaaaaaaaaaaaaa', 'bbbbbbbbbbbbbbbb'].index(member)].items() if k != 'status'})
    assert by_id[feature_id]['status'] == 'needs_adjustment' and by_id[feature_id]['notes'] == 'looks like the same pad'   # the touched member's words
    assert by_id[feature_id]['derived_from'] == ['aaaaaaaaaaaaaaaa', 'bbbbbbbbbbbbbbbb'] == by_id[feature_id]['replaces']
    assert record['carry']['review_layer_derived_from'] == [feature_id] and record['carry']['restore_verbatim'] is True
    assert record['carry']['members_untouched'] == ['aaaaaaaaaaaaaaaa'] and record['carry']['members_inherited'] == {feature_id: ['bbbbbbbbbbbbbbbb']}
    assert record['carry']['members_superseded_in_review'] == ['aaaaaaaaaaaaaaaa', 'bbbbbbbbbbbbbbbb'] and len(carried['objects']) == len(review['objects'])+1
    assert carried['history'][-1]['revision'] == 3 and 'history' not in carried['history'][-1] and carried['basemap_mode'] == 'raw'
    rows = [json.loads(line) for line in registry.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]['role'] == 'linear_merge' and rows[0]['replaced_member_ids'] == ['aaaaaaaaaaaaaaaa', 'bbbbbbbbbbbbbbbb']
    assert rows[0]['review_revision_before'] == 3 and rows[0]['review_revision_after'] == 4 and rows[0]['new_model_signature'] == new_sig
    assert rows[0]['features'][0]['id'] == feature_id and 'written_at' in rows[0] and 'design_sha256' in rows[0]
    assert rows[0]['restored_verbatim'] == [] and rows[0]['model_layer_lineage'] == ['replaces', 'merged_from']
    promote_record = json.loads((tmp_path/'run'/scene['id']/'promote.json').read_text())
    assert promote_record['new_latest_sha256'] == P.sha((folder/'latest.json').read_bytes()) and promote_record['new_review_sha256'] == P.sha((folder/'review.json').read_bytes())
    # rollback: the two files are the byte copies again, the registry row is gone, the new models file stays
    result = P.rollback(scene['id'], root=tmp_path, run_dir=tmp_path/'run', registry=registry, log=lambda _: None)
    after = files_under(tmp_path/'data')
    assert after[f"scenes/{scene['id']}/latest.json"] == before[f"scenes/{scene['id']}/latest.json"]
    assert after[f"scenes/{scene['id']}/review.json"] == before[f"scenes/{scene['id']}/review.json"]
    assert set(after) == set(before) | {f"scenes/{scene['id']}/models/{new_sig}.json"} and result['stays']['models_file'] == f'models/{new_sig}.json'
    assert registry.read_text() == ''
    with pytest.raises(P.Refusal, match='already_rolled_back'):
        P.rollback(scene['id'], root=tmp_path, run_dir=tmp_path/'run', registry=registry, log=lambda _: None)


@needs_node
def test_restore_verbatim_is_the_default_and_no_restore_verbatim_refuses(tmp_path):
    scene, review = window(tmp_path)
    drawn = decided(obj('ffffffffffffffff', 'building', rect(40, 10, 45, 15)), notes='shed')    # reviewer-drawn confirmed object
    review['objects'].append(drawn)
    folder = tmp_path/'data/scenes'/scene['id']
    (folder/'review.json').write_text(json.dumps(review, indent=2))
    before = files_under(tmp_path/'data')
    with pytest.raises(P.Refusal, match='assertion_failed') as refusal:
        P.promote(scene['id'], root=tmp_path, run_dir=tmp_path/'run', registry=tmp_path/'run/registry.jsonl', lot_tags=False, scratch=False, restore_verbatim=False, log=lambda _: None)
    assert 'ffffffffffffffff' in refusal.value.detail and '--restore-verbatim' in refusal.value.detail and files_under(tmp_path/'data') == before
    lines = []
    plan = P.promote(scene['id'], root=tmp_path, run_dir=tmp_path/'run', registry=tmp_path/'run/registry.jsonl', lot_tags=False, dry_run=True, log=lines.append)
    assert plan['outcome'] == 'dry_run' and plan['restored_verbatim'] == ['ffffffffffffffff'] and files_under(tmp_path/'data') == before
    assert len(lines) == 1 and lines[0].startswith('PLAN win-0000000000000001: 1 feature(s): driveway ') and "restored verbatim 1 ['ffffffffffffffff']" in lines[0]
    assert 'model-layer lineage replaces+merged_from' in lines[0] and 'review rev 3 -> 4' in lines[0]
    record = P.promote(scene['id'], root=tmp_path, run_dir=tmp_path/'run', registry=tmp_path/'run/registry.jsonl', lot_tags=False, scratch=False, log=lambda _: None)
    assert record['outcome'] == 'promoted' and record['restore_verbatim'] is True and record['carry']['restored_verbatim'] == ['ffffffffffffffff'] == record['restored_verbatim']
    by_id = {o['id']: o for o in json.loads((folder/'review.json').read_text())['objects']}
    assert P.canonical(by_id['ffffffffffffffff']) == P.canonical(drawn)
    rows = [json.loads(line) for line in (tmp_path/'run/registry.jsonl').read_text().splitlines()]
    assert rows[0]['restored_verbatim'] == ['ffffffffffffffff']


# --- the batch -------------------------------------------------------------------------------------------------------------

def fake_counter(primary_n=25, hop=True):
    return {'primary_n': primary_n, 'primary_by_class': {'roof': 20, 'not_roof': 5}, 'primary_rows': [['scene', 'id', 12.5]]*primary_n, 'targets_remaining': 64,
            'takeover_rows': [{'id': 't1', 'scene_id': 'win-x', 'status': 'candidate'}], 'carried_rows': [{'id': 'c1', 'design_sha_match': hop, 'status': 'needs_adjustment'}],
            'scenes': 9, 'computed_at': 'now'}


def test_compare_counters_ignores_the_design_sha_match_hop_label_and_names_the_field_that_moved():
    same = P.compare_counters(fake_counter(hop=True), fake_counter(hop=False))
    assert same['identical'] and same['differing_fields'] == [] and same['before']['carried_rows']['rows'] == 1
    moved = P.compare_counters(fake_counter(), fake_counter(primary_n=26))
    assert not moved['identical'] and moved['differing_fields'] == ['primary_n', 'primary_rows'] and moved['after']['primary_n']['value'] == 26
    assert list(same['before']) == list(P.COUNTER_FIELDS)


def test_preflight_skips_a_stale_review_and_nothing_to_merge_without_writing(tmp_path):
    scene, review = window(tmp_path)
    folder = tmp_path/'data/scenes'/scene['id']
    assert P.preflight(scene['id'], root=tmp_path) is None
    (folder/'review.json').write_text(json.dumps({**review, 'model_signature': 'ee'*32}, indent=2))
    before = files_under(tmp_path/'data')
    reason, detail = P.preflight(scene['id'], root=tmp_path)
    assert reason == 'review_stale' and 'eeeeeeeeeeee' in detail and files_under(tmp_path/'data') == before
    review['objects'][0] = decided(review['objects'][0])
    (folder/'review.json').write_text(json.dumps(review, indent=2))
    assert P.preflight(scene['id'], root=tmp_path)[0] == 'nothing_to_merge'
    assert P.preflight('win-0000000000000009', root=tmp_path)[0] == 'no_latest'
    (folder/'review.json').unlink()
    assert P.preflight(scene['id'], root=tmp_path)[0] == 'no_review'


def test_quiet_intruders_ignore_qa_scratch_this_batch_and_reviews_the_registry_says_this_program_wrote(tmp_path):
    import os
    from datetime import UTC, datetime, timedelta
    reviews = {}
    for wid in ('win-00000000000000a1', 'win-00000000000000a2', 'win-00000000000000a3', 'qa_scratch'):
        folder = tmp_path/'data/scenes'/wid
        folder.mkdir(parents=True)
        (folder/'review.json').write_text(json.dumps({'revision': 1, 'id': wid}))
        reviews[wid] = folder/'review.json'
    since = datetime.now(UTC) - timedelta(hours=1)
    old_time = (since - timedelta(hours=1)).timestamp()
    os.utime(reviews['win-00000000000000a3'], (old_time, old_time))                                          # before the window: quiet
    registry = tmp_path/'registry.jsonl'
    registry.write_text(json.dumps({'version': P.REGISTRY_VERSION, 'role': P.ROLE, 'window_scene_id': 'win-00000000000000a1',
                                    'new_review_sha256': P.sha(reviews['win-00000000000000a1'].read_bytes())})+'\n' +
                        json.dumps({'role': 'other', 'window_scene_id': 'win-00000000000000a2', 'new_review_sha256': P.sha(reviews['win-00000000000000a2'].read_bytes())})+'\n')
    written = P.program_written(registry)
    assert written == {'win-00000000000000a1': {P.sha(reviews['win-00000000000000a1'].read_bytes())}}
    assert [i['window_scene_id'] for i in P.quiet_intruders(tmp_path, since, (), written)] == ['win-00000000000000a2']       # a1 is this program's bytes, qa_ ignored
    assert [i['window_scene_id'] for i in P.quiet_intruders(tmp_path, since, {'win-00000000000000a2'}, written)] == []
    reviews['win-00000000000000a1'].write_text(json.dumps({'revision': 2, 'id': 'win-00000000000000a1'}))          # someone saved over this program's bytes
    assert [i['window_scene_id'] for i in P.quiet_intruders(tmp_path, since, (), written)] == ['win-00000000000000a1', 'win-00000000000000a2']


@needs_node
def test_batch_stops_at_the_first_counter_difference_and_rolls_that_window_back(tmp_path):
    scene_a, _ = window(tmp_path, 'win-000000000000000a')
    scene_b, _ = window(tmp_path, 'win-000000000000000b')
    before = files_under(tmp_path/'data')
    calls = []

    def counter():                       # before a: baseline; after a: primary_n moved; after the rollback: baseline (hop label flipped, ignored)
        calls.append(len(calls))
        return fake_counter(primary_n=26) if len(calls) == 2 else fake_counter(hop=len(calls) == 1)
    registry = tmp_path/'run/registry.jsonl'
    summary = P.batch(['win-000000000000000a', 'win-000000000000000b'], root=tmp_path, run_dir=tmp_path/'run', counter=counter, registry=registry,
                      lot_tags=False, scratch=False, log=lambda _: None)
    assert summary['stopped_at'] == 'win-000000000000000a' and summary['stop_reason'] == 'counter differs: primary_n,primary_rows'
    assert summary['rolled_back'] == ['win-000000000000000a'] and summary['promoted'] == [] and summary['skipped'] == [] and len(calls) == 3
    after = files_under(tmp_path/'data')
    new_sig = json.loads((tmp_path/'run/batch/win-000000000000000a.json').read_text())['new_model_signature']
    assert {k: v for k, v in after.items() if 'models/'+new_sig not in k} == before      # both windows at their bytes; a's new models file stays
    entry = json.loads((tmp_path/'run/batch/win-000000000000000a.json').read_text())
    assert entry['outcome'] == 'rolled_back' and entry['differing_fields'] == ['primary_n', 'primary_rows'] and entry['counter_restored_to_before'] is True
    assert entry['rollback']['restored']['latest_json_sha256'] == P.sha(before['scenes/win-000000000000000a/latest.json'])
    assert (tmp_path/'run/batch/win-000000000000000a.counter.before.json').exists() and (tmp_path/'run/batch/win-000000000000000a.counter.after-rollback.json').exists()
    assert not (tmp_path/'run/batch/win-000000000000000b.json').exists() and not (tmp_path/'run/win-000000000000000b').exists()
    assert registry.read_text() == '' and json.loads((tmp_path/'run/batch/summary.json').read_text())['stopped_at'] == 'win-000000000000000a'
    assert json.loads((tmp_path/'run/win-000000000000000a/promote.json').read_text())['rolled_back_at']


@needs_node
def test_batch_skips_stale_and_nothing_to_merge_windows_and_promotes_the_rest(tmp_path):
    stale, stale_review = window(tmp_path, 'win-000000000000000c')
    (tmp_path/'data/scenes/win-000000000000000c/review.json').write_text(json.dumps({**stale_review, 'model_signature': 'ee'*32}, indent=2))
    nothing, nothing_review = window(tmp_path, 'win-000000000000000d')
    nothing_review['objects'][0] = decided(nothing_review['objects'][0])
    (tmp_path/'data/scenes/win-000000000000000d/review.json').write_text(json.dumps(nothing_review, indent=2))
    good, _ = window(tmp_path, 'win-000000000000000e')
    before = files_under(tmp_path/'data')
    calls = []
    summary = P.batch(['win-000000000000000c', 'win-000000000000000d', 'win-000000000000000e'], root=tmp_path, run_dir=tmp_path/'run',
                      counter=lambda: (calls.append(1), fake_counter(hop=len(calls) % 2 == 0))[1], registry=tmp_path/'run/registry.jsonl', lot_tags=False, scratch=False, log=lambda _: None)
    assert summary['skipped'] == ['win-000000000000000c', 'win-000000000000000d'] and summary['promoted'] == ['win-000000000000000e']
    assert summary['stopped_at'] is None and summary['failed'] == [] and len(calls) == 2
    entries = {i: json.loads((tmp_path/'run/batch'/f'win-000000000000000{i}.json').read_text()) for i in 'cde'}
    assert entries['c']['outcome'] == 'skipped' and entries['c']['reason'] == 'review_stale'
    assert entries['d']['outcome'] == 'skipped' and entries['d']['reason'] == 'nothing_to_merge'
    assert entries['e']['outcome'] == 'promoted' and entries['e']['counter_identical'] is True and entries['e']['restored_verbatim'] == []
    after = files_under(tmp_path/'data')
    for i in 'cd':
        assert {k: v for k, v in after.items() if f'/win-000000000000000{i}/' in k} == {k: v for k, v in before.items() if f'/win-000000000000000{i}/' in k}
    assert json.loads(after['scenes/win-000000000000000e/review.json'])['revision'] == 4
    assert len((tmp_path/'run/registry.jsonl').read_text().splitlines()) == 1
    assert [w['window_scene_id'] for w in json.loads((tmp_path/'run/batch/summary.json').read_text())['windows']] == ['win-000000000000000c', 'win-000000000000000d', 'win-000000000000000e']


@needs_node
def test_cli_dry_run_prints_one_plan_line_per_window(tmp_path):
    window(tmp_path, 'win-000000000000000f')
    nothing, nothing_review = window(tmp_path, 'win-0000000000000010')
    nothing_review['objects'][0] = decided(nothing_review['objects'][0])
    (tmp_path/'data/scenes/win-0000000000000010/review.json').write_text(json.dumps(nothing_review, indent=2))
    before = files_under(tmp_path/'data')
    proc = subprocess.run([sys.executable, '-m', 'scripts.ml.scene_linear_promote', '--root', str(tmp_path), '--run-dir', str(tmp_path/'run'), '--registry', str(tmp_path/'r.jsonl'),
                           '--no-lot-tags', '--dry-run', 'win-000000000000000f', 'win-0000000000000010'], capture_output=True, text=True, cwd=str(P.REPO))
    lines = proc.stdout.strip().splitlines()
    assert proc.returncode == 0 and len(lines) == 2, proc.stdout+proc.stderr
    assert lines[0].startswith('PLAN win-000000000000000f: 1 feature(s): driveway ') and 'restored verbatim 0 []' in lines[0]
    assert lines[1].startswith('NOTHING TO MERGE win-0000000000000010:') and files_under(tmp_path/'data') == before


def test_cli_refuses_a_non_window_id(tmp_path):
    proc = subprocess.run([sys.executable, '-m', 'scripts.ml.scene_linear_promote', '--root', str(tmp_path), '--run-dir', str(tmp_path/'run'),
                           '--registry', str(tmp_path/'r.jsonl'), '--no-lot-tags', '--no-scratch', 'roof_training_250-29772'], capture_output=True, text=True, cwd=str(P.REPO))
    assert proc.returncode == 1 and 'not_a_window_id' in proc.stdout and not (tmp_path/'run').exists()
