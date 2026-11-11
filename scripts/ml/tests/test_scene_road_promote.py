"""scene_road_promote: the write path of the road-corridor v3.1 bands, proven on a synthetic window in a temporary root.

The window is drawn in metres at 0.1 m/px, its snapshot written as scene_extract.save_scene writes one, its review as the app
writes one; the proposals are a run directory shaped like run-v31 (<scene>.review.json + metrics.json) with a numbered marks sheet.
Band 1 is the e31f case (a confirmed seed, a needs_adjustment sibling replaced only on accept, a needs_adjustment driveway twin
retired only on accept); band 2 is a candidate-only band (two candidate road pieces). The carry runs the editor's own
recoverSceneReview through scripts/scene-road-carry.mjs. Nothing under the repo's data/ is read or written (root = tmp_path).
"""
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime

import pytest
from scripts.ml import scene_road_promote as R
from shapely.geometry import Polygon

AFFINE = [[.1, 0.], [0., -.1], [0., 50.]]            # pixel -> metres: x_m = .1 px, y_m = 50 - .1 py
INVERSE = [[10., 0.], [0., -10.], [0., 500.]]
NODE = shutil.which('node')
needs_node = pytest.mark.skipif(NODE is None or not (R.REPO/'node_modules/typescript').exists(), reason='node + typescript needed for the carry')
WID = 'win-0000000000000001'
BAND1, BAND2, BAND3 = '7e80bb90a710f6b1', 'e1c3dff1c650bb92', '3af5406e4a64196c'
INTERVAL = (datetime(2026, 10, 7, 7, 59, 59, tzinfo=UTC), datetime(2026, 10, 7, 8, 0, 42, tzinfo=UTC))
SENTENCE = '1 right, 2 right'


def px(points_m):
    return [[round(x/.1, 2), round((50.-y)/.1, 2)] for x, y in points_m]


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def obj(oid, kind, ring_m, status='candidate', score=.5, **extra):
    return {'id': oid, 'kind': kind, 'label': kind.capitalize(), 'ring': px(ring_m), 'holes': [], 'status': status, 'geometry_reviewed': False,
            'source': 'sam3-text-onnx', 'score': score, 'prompt': kind,
            'model': {'height_m': .05, 'base_m': 0., 'height_source': 'class-prior', 'height_verified': False, 'width_m': 1., 'length_m': 2.,
                      'area_m2': round(Polygon(ring_m).area, 2), 'rotation_deg': 0., 'color': '#808080', 'footprint_area_px': 1.}, **extra}


def decided(o, status='confirmed', **extra):
    return {**o, 'status': status, 'geometry_reviewed': status == 'confirmed', **extra}


MODEL_OBJECTS = [
    obj('aaaaaaaaaaaaaaaa', 'road', rect(2, 5, 20, 12)),                         # band 2: two candidate road pieces
    obj('bbbbbbbbbbbbbbbb', 'road', rect(21, 5, 45, 12)),
    obj('cccccccccccccccc', 'road', rect(2, 25, 45, 37)),                        # band 1: the e31f case (confirmed in the review)
    obj('dddddddddddddddd', 'road', rect(46, 25, 47, 37)),                       # its needs_adjustment sibling
    obj('eeeeeeeeeeeeeeee', 'driveway', rect(2, 26, 44, 36)),                    # its needs_adjustment driveway twin (retire target)
    obj('ffffffffffffffff', 'car', rect(10, 28, 14.5, 30)),                      # a confirmed car on the road
    obj('1111111111111111', 'tree', rect(30, 32, 36, 38)),                       # a tree with the reviewer's words
    obj('2222222222222222', 'building', rect(5, 40, 15, 48)),
    obj('3333333333333333', 'road', rect(25, 14, 32, 22)),                       # band 3: a junction seed whose other arm stays
]


def band_record(band_id, ring_m, *, seed, seed_status, replaces=(), on_accept=(), retires=(), absorbs=(), twins=(), remainder=()):
    ring = px(ring_m)
    model = {'height_m': .08, 'base_m': 0., 'height_source': 'class-prior', 'height_verified': False, 'width_m': 12., 'length_m': 43.,
             'area_m2': round(Polygon(ring_m).area, 2), 'rotation_deg': 0., 'footprint_area_px': 1., 'color': '#84868c'}
    proposal = {'id': band_id, 'kind': 'road', 'label': 'Road', 'ring': ring, 'status': 'proposal', 'source': 'road-corridor-v3.1', 'geometry_reviewed': False,
                'score': .8, 'model': model, 'replaces': sorted(replaces), 'replaces_on_accept': sorted(on_accept), 'retires_on_accept': sorted(retires),
                'remainder_on_accept': [{'from': seed, 'area_m2': round(Polygon(r).area, 2), 'ring': px(r)} for r in remainder],
                'absorbs': [{'id': i, 'why': 'same-mask sibling', 'status': s, 'area_m2': 1.} for i, s in absorbs],
                'xclass_twin': [{'id': i, 'kind': 'driveway', 'status': s, 'decided': True, 'action': 'retires on accept'} for i, s in twins],
                'derived_from': sorted({*replaces, *on_accept, *retires}), 'cars_under': ['ffffffffffffffff'], 'assumption': 'parking lane counts as road'}
    return {'seed': seed, 'seed_kind': 'road', 'seed_status': seed_status, 'width_m': 12., 'width_source': 'two_kerb', 'cars_under_n': 1, 'vertices_after': len(ring),
            'retires_on_accept': [{'id': i, 'kind': 'driveway', 'status': 'needs_adjustment'} for i in retires], 'proposal': proposal}


def setup(root, *, review_objects=None, revision=3, updated_at='2026-10-02T00:00:00Z', history=None, junction=False):
    """A window, its review and a run-v31-shaped proposals directory with a marks sheet; returns (scene, review, proposals, sheet)."""
    folder = root/'data/scenes'/WID
    (folder/'models').mkdir(parents=True)
    scene = {'schema_version': 1, 'id': WID, 'address_id': 29772, 'cohort': 'windows', 'title': 'Synthetic window', 'centroid': [145.1, -37.8],
             'image_sha256': 'ab'*32, 'image_width': 500, 'image_height': 500, 'pixel_to_meters': AFFINE, 'meters_to_pixel': INVERSE,
             'objects': json.loads(json.dumps(MODEL_OBJECTS)), 'groups': [], 'scans': {'road': {'status': 'complete', 'count': 4, 'prompt': 'road'}},
             'region': {'provider': 'google', 'imagery_zoom': 21}, 'window_frame': {'lots': ['1\\PS123456'], 'size_px': 500}, 'analysis': {'input_geometry': 'imagery-only'}}
    scene['model_signature'] = R.L.signature(scene)
    text = json.dumps(scene, indent=2)
    (folder/'latest.json').write_text(text)
    (folder/'models'/f"{scene['model_signature']}.json").write_text(text)
    if review_objects is None:
        review_objects = json.loads(json.dumps(MODEL_OBJECTS))
        review_objects[2] = decided(review_objects[2])
        review_objects[3] = decided(review_objects[3], 'needs_adjustment')
        review_objects[4] = decided(review_objects[4], 'needs_adjustment')
        review_objects[5] = decided(review_objects[5])
        review_objects[6] = decided(review_objects[6], 'needs_adjustment', notes='two crowns, split later')
    review = {'revision': revision, 'model_signature': scene['model_signature'], 'objects': review_objects, 'groups': [], 'reviewed_classes': [],
              'basemap_mode': 'raw', 'updated_at': updated_at, 'updated_by': 'local', **({'history': history} if history is not None else {})}
    (folder/'review.json').write_text(json.dumps(review, indent=2))
    proposals = root/'prop'
    proposals.mkdir()
    records = [band_record(BAND1, rect(1, 24.5, 48, 37.5), seed='cccccccccccccccc', seed_status='confirmed', on_accept=('cccccccccccccccc', 'dddddddddddddddd'),
                           retires=('eeeeeeeeeeeeeeee',), absorbs=(('dddddddddddddddd', 'needs_adjustment'),), twins=(('eeeeeeeeeeeeeeee', 'needs_adjustment'),)),
               band_record(BAND2, rect(1, 4.5, 46, 12.5), seed='aaaaaaaaaaaaaaaa', seed_status='candidate', replaces=('aaaaaaaaaaaaaaaa', 'bbbbbbbbbbbbbbbb'),
                           absorbs=(('bbbbbbbbbbbbbbbb', 'candidate'),))]
    lines = [f"1. `{WID}` band `{BAND1}` from road `cccccccccccccccc` (confirmed): ...", f"2. `{WID}` band `{BAND2}` from road `aaaaaaaaaaaaaaaa` (candidate): ..."]
    if junction:
        records.append(band_record(BAND3, rect(25, 13.5, 32.5, 18), seed='3333333333333333', seed_status='candidate', replaces=('3333333333333333',),
                                   remainder=[rect(25, 18.2, 32, 22)]))
        lines.append(f"3. `{WID}` band `{BAND3}` from road `3333333333333333` (candidate): junction ...")
    (proposals/f'{WID}.review.json').write_text(json.dumps({'scene': WID, 'layer': 'review', 'proposals': records, 'exits': []}))
    (proposals/'metrics.json').write_text(json.dumps({'written_at': '2026-10-07T08:00:41.939550+00:00'}))
    sheet = root/'MARKS.md'
    sheet.write_text('# MARKS\n\n' + '\n'.join(lines) + '\n')
    return scene, review, proposals, sheet


def bands_of(root, proposals, sheet):
    return R.load_bands(root, proposals=proposals, sheet=sheet)


def marks_file(root, marks, name='marks.json', **extra):
    path = root/name
    path.write_text(json.dumps({'marks': marks, 'sentence': SENTENCE, 'at': '2026-10-07T20:05:00+11:00', 'source': 'human:chat', **extra}))
    return R.read_marks(path)


def files_under(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def kwargs(root, **extra):
    return {'root': root, 'run_dir': root/'run', 'registry': root/'run/registry.jsonl', 'lot_tags': False, 'scratch': False, 'interval': INTERVAL,
            'log': lambda _: None, **extra}


# --- pure ---------------------------------------------------------------------------------------------------------------

def test_lineage_issue_mirrors_the_app_rules():
    a = {'id': 'a', 'status': 'superseded', 'replacement_ids': ['b'], 'geometry_reviewed': False}
    b = {'id': 'b', 'status': 'candidate', 'derived_from': ['a'], 'geometry_reviewed': False}
    assert R.lineage_issue([a, b]) is None
    assert R.lineage_issue([{**a, 'replacement_ids': ['b', 'c']}, b, {'id': 'c', 'status': 'candidate'}]) == 'The replacement history is incomplete.'
    assert R.lineage_issue([{**a, 'geometry_reviewed': True}, b]) == 'Replaced outlines must retain their replacements and cannot be approved.'
    assert R.lineage_issue([{**a, 'status': 'candidate'}, b]) == 'Replaced outlines must retain their replacements and cannot be approved.'
    assert R.lineage_issue([a, {**b, 'derived_from': ['a', 'a']}]) == 'Replacement history must refer to existing, distinct objects.'
    assert R.lineage_issue([{**b, 'derived_from': ['z']}, a]) == 'Replacement history must refer to existing, distinct objects.'   # first object first, as the app
    assert R.lineage_issue([a, {**b, 'derived_from': ['z']}]) == 'The replacement history is incomplete.'


def test_pin_reads_the_revision_saved_before_the_run_and_refuses_a_save_inside_it():
    history = [{'revision': 1, 'updated_at': '2026-10-01T00:00:00Z', 'model_signature': 's'}, {'revision': 2, 'updated_at': '2026-10-07T00:24:41Z', 'model_signature': 's'}]
    review = {'revision': 3, 'updated_at': '2026-10-07T08:41:07.392Z', 'model_signature': 's', 'history': history}
    pin = R.pin_revision(review, INTERVAL)
    assert pin['revision'] == 2 and pin['model_signature'] == 's'
    assert R.pin_revision({**review, 'updated_at': '2026-10-06T00:00:00Z', 'history': history[:1]}, INTERVAL)['revision'] == 3
    with pytest.raises(R.Refusal, match='pin_ambiguous'):
        R.pin_revision({**review, 'updated_at': '2026-10-07T08:00:10Z'}, INTERVAL)
    with pytest.raises(R.Refusal, match='pin_unknown'):
        R.pin_revision({'revision': 1, 'updated_at': '2026-10-07T09:00:00Z', 'model_signature': 's'}, INTERVAL)


def test_marks_read_the_first_word_and_need_the_chat_sentence_for_an_accept(tmp_path):
    marks = marks_file(tmp_path, {'1': 'right', '2': 'wrong (too wide)', '3': 'unclear'})
    assert marks['marks'] == {1: 'right', 2: 'wrong', 3: 'unclear'} and marks['sentence'] == SENTENCE and not marks['simulated']
    (tmp_path/'bare.json').write_text(json.dumps({'marks': {'1': 'right'}}))
    with pytest.raises(R.Refusal, match='marks_without_chat'):
        R.read_marks(tmp_path/'bare.json')
    assert marks_file(tmp_path, {'1': 'right'}, name='marks-SIMULATED.json')['simulated'] is True


def test_marks_read_a_decisions_transcription_as_is(tmp_path):
    path = tmp_path/'decisions-2026-10-07.json'
    path.write_text(json.dumps({'version': 'road-decisions-v1', 'source': 'human:chat', 'transcribed_at': '2026-10-07T09:02:51Z', 'transcribed_by': 'agent',
                                'sentence': 'yes to all defaults', 'marks_by_band_number': {'1': 'right', '2': 'right', '8': 'not_applied'}}))
    marks = R.read_marks(path)
    assert marks['marks'] == {1: 'right', 2: 'right', 8: 'not_applied'} and marks['at'] == '2026-10-07T09:02:51Z' and marks['at_field'] == 'transcribed_at'
    assert not marks['simulated'] and marks['transcribed_by'] == 'agent'
    bands = {n: {'number': n, 'id': f'{n:016x}', 'window': 'w'} for n in range(1, 18)}
    selected, accepted, skipped = R.select_bands(bands, marks=marks)
    assert [b['number'] for b in selected] == [1, 2] and accepted == {1, 2} and {'number': 8, 'reason': 'marked not_applied'} in skipped
    decision = R.provenance_for(bands[1], marks)['human_decision']
    assert decision['at'] == '2026-10-07T09:02:51Z' and decision['at_field'] == 'transcribed_at' and decision['marks_transcribed_by'] == 'agent'


def test_a_partial_proposals_dir_loads_the_bands_it_holds(tmp_path):
    setup(tmp_path)
    sheet = tmp_path/'MARKS.md'
    sheet.write_text(sheet.read_text() + '4. `win-00000000000000ff` band `00000000000000ff` from road `00000000000000fe` (candidate): ...\n')
    bands = R.load_bands(tmp_path, proposals=tmp_path/'prop', sheet=sheet)
    assert bands[4]['missing'] and 'proposal' not in bands[4] and bands[1]['proposal']['id'] == BAND1
    selected, _, skipped = R.select_bands(bands)                                    # the default set: the band without a file is named
    assert [b['number'] for b in selected] == [1, 2] and any(s['number'] == 4 and 'proposal file missing' in s['reason'] for s in skipped)
    selected, accepted, _ = R.select_bands(bands, marks={'marks': {1: 'right', 4: 'right'}})
    assert [b['number'] for b in selected] == [1] and accepted == {1}
    with pytest.raises(R.Refusal, match='proposal_file_missing'):
        R.select_bands(bands, wanted=['4'])


def test_default_set_skips_band_8_and_marks_select_only_right(tmp_path):
    bands = {n: {'number': n, 'id': f'{n:016x}', 'window': 'w'} for n in range(1, 18)}
    selected, accepted, skipped = R.select_bands(bands)
    assert [b['number'] for b in selected] == [n for n in range(1, 18) if n != 8] and accepted == set() and skipped[0]['number'] == 8
    selected, accepted, skipped = R.select_bands(bands, marks={'marks': {1: 'right', 2: 'wrong', 8: 'right'}})
    assert [b['number'] for b in selected] == [1, 8] and accepted == {1, 8} and {'number': 2, 'reason': 'marked wrong'} in skipped
    assert [b['number'] for b in R.select_bands(bands, wanted=['3', f'{5:016x}'])[0]] == [3, 5]


def test_band_without_accept_is_refused_for_a_confirmed_member_and_nothing_is_written(tmp_path):
    scene, review, proposals, sheet = setup(tmp_path)
    bands = bands_of(tmp_path, proposals, sheet)
    plan = R.plan_window(scene, review, [bands[1]], set())
    reasons = {r[0] for r in plan['bands'][0]['refused']}
    assert plan['nothing_to_apply'] and 'needs_reviewer_accept' in reasons and plan['members'] == []
    before = files_under(tmp_path/'data')
    record = R.promote(WID, bands=[bands[1]], **kwargs(tmp_path))
    assert record['outcome'] == 'nothing_to_apply' and files_under(tmp_path/'data') == before and not (tmp_path/'run/registry.jsonl').exists()
    # band 1 with the accept: the confirmed seed, its sibling and the retire target are members
    plan = R.plan_window(scene, review, [bands[1]], {1}, marks_file(tmp_path, {'1': 'right'}))
    assert plan['bands'][0]['refused'] == [] and plan['members'] == ['cccccccccccccccc', 'dddddddddddddddd', 'eeeeeeeeeeeeeeee']
    assert plan['bands'][0]['retire_targets'] == ['eeeeeeeeeeeeeeee'] and plan['specs'][0]['provenance']['human_decision']['source'] == 'human:chat'


def test_members_dropped_from_the_model_layer_and_replaces_set(tmp_path):
    scene, review, proposals, sheet = setup(tmp_path)
    bands = bands_of(tmp_path, proposals, sheet)
    plan = R.plan_window(scene, review, [bands[2]], set())
    assert plan['members'] == ['aaaaaaaaaaaaaaaa', 'bbbbbbbbbbbbbbbb'] and len(plan['features']) == 1
    feature = plan['features'][0]
    assert feature['id'] == BAND2 and feature['kind'] == 'road' and feature['source'] == 'road-corridor-v3.1' and feature['status'] == 'candidate'
    assert feature['replaces'] == plan['members'] and 'derived_from' not in feature and feature['road_band']['marks_sheet_number'] == 2
    new = R.L.build_snapshot(scene, plan['features'], plan['members'])
    ids = [o['id'] for o in new['objects']]
    assert 'aaaaaaaaaaaaaaaa' not in ids and 'bbbbbbbbbbbbbbbb' not in ids and ids[-1] == BAND2
    assert new['model_signature'] == R.L.signature(new) != scene['model_signature']
    for key in scene:
        if key not in ('objects', 'model_signature'):
            assert json.dumps(new[key], sort_keys=True) == json.dumps(scene[key], sort_keys=True), key


def test_a_lineage_only_member_is_not_reviewer_touched(tmp_path):
    objects = json.loads(json.dumps(MODEL_OBJECTS))
    objects[0] = {**objects[0], 'derived_from': ['9999999999999999'], 'ring': [[int(x), int(y)] if float(x).is_integer() and float(y).is_integer() else [x, y] for x, y in objects[0]['ring']]}
    objects.append({**obj('9999999999999999', 'road', rect(2, 5, 20, 12)), 'status': 'superseded', 'replacement_ids': ['aaaaaaaaaaaaaaaa']})
    scene, review, proposals, sheet = setup(tmp_path, review_objects=objects)
    model_by_id = {o['id']: o for o in scene['objects']}
    assert not R.reviewer_touched(objects[0], model_by_id['aaaaaaaaaaaaaaaa'])
    assert R.reviewer_touched({**objects[0], 'notes': 'kerb is wrong'}, model_by_id['aaaaaaaaaaaaaaaa'])
    plan = R.plan_window(scene, review, [bands_of(tmp_path, proposals, sheet)[2]], set())
    assert plan['bands'][0]['refused'] == []
    edited = [*objects[:1], {**objects[1], 'notes': 'kerb is wrong'}, *objects[2:]]
    plan = R.plan_window(scene, {**review, 'objects': edited}, [bands_of(tmp_path, proposals, sheet)[2]], set())
    assert [r[0] for r in plan['bands'][0]['refused']] == ['needs_reviewer_accept']


def test_stale_proposal_is_refused_when_the_review_moved_on(tmp_path):
    later = [{'revision': 3, 'updated_at': '2026-10-02T00:00:00Z', 'model_signature': None}]
    scene, review, proposals, sheet = setup(tmp_path, revision=4, updated_at='2026-10-07T08:41:04Z', history=later)
    bands = bands_of(tmp_path, proposals, sheet)
    before = files_under(tmp_path/'data')
    with pytest.raises(R.Refusal, match='stale_proposal') as refusal:
        R.promote(WID, bands=[bands[2]], **kwargs(tmp_path))
    assert 'rev 4' in refusal.value.detail and files_under(tmp_path/'data') == before


def test_stale_proposal_is_refused_when_a_lineage_status_differs_from_the_run(tmp_path):
    scene, review, proposals, sheet = setup(tmp_path)
    review['objects'][4] = decided(review['objects'][4])                    # the reviewer confirmed the retire target since the run
    (tmp_path/'data/scenes'/WID/'review.json').write_text(json.dumps(review, indent=2))
    bands = bands_of(tmp_path, proposals, sheet)
    with pytest.raises(R.Refusal, match='stale_proposal') as refusal:
        R.promote(WID, bands=[bands[1]], marks=marks_file(tmp_path, {'1': 'right'}), **kwargs(tmp_path))
    assert 'eeeeeeeeeeeeeeee reads confirmed, the run recorded needs_adjustment' in refusal.value.detail


def test_simulated_marks_never_reach_a_real_window(tmp_path):
    scene, review, proposals, sheet = setup(tmp_path)
    bands = bands_of(tmp_path, proposals, sheet)
    with pytest.raises(R.Refusal, match='simulated_marks_on_real_window'):
        R.promote(WID, bands=[bands[1]], marks=marks_file(tmp_path, {'1': 'right'}, name='marks-SIMULATED.json'), **kwargs(tmp_path))


# --- the carry (node) ----------------------------------------------------------------------------------------------------

@needs_node
def test_accept_path_confirms_the_band_and_supersedes_the_confirmed_seed_and_the_retire_target(tmp_path):
    scene, review, proposals, sheet = setup(tmp_path)
    bands = bands_of(tmp_path, proposals, sheet)
    marks = marks_file(tmp_path, {'1': 'right', '2': 'right'})
    folder = tmp_path/'data/scenes'/WID
    record = R.promote(WID, bands=[bands[1], bands[2]], marks=marks, **kwargs(tmp_path))
    assert record['outcome'] == 'promoted' and record['applied'] == [1, 2] and record['review_revision_after'] == 4
    latest = json.loads((folder/'latest.json').read_text())
    model_ids = [o['id'] for o in latest['objects']]
    for member in ('aaaaaaaaaaaaaaaa', 'bbbbbbbbbbbbbbbb', 'cccccccccccccccc', 'dddddddddddddddd', 'eeeeeeeeeeeeeeee'):
        assert member not in model_ids
    model_band = {o['id']: o for o in latest['objects']}[BAND1]
    assert model_band['replaces'] == ['cccccccccccccccc', 'dddddddddddddddd', 'eeeeeeeeeeeeeeee'] and 'derived_from' not in model_band and model_band['status'] == 'candidate'
    carried = json.loads((folder/'review.json').read_text())
    by_id = {o['id']: o for o in carried['objects']}
    band = by_id[BAND1]
    assert band['status'] == 'confirmed' and band['geometry_reviewed'] is True and band['derived_from'] == model_band['replaces']
    decision = band['provenance']['human_decision']
    assert decision['source'] == 'human:chat' and decision['sentence'] == SENTENCE and decision['at'] == '2026-10-07T20:05:00+11:00' and decision['band'] == 1
    assert by_id['cccccccccccccccc']['status'] == 'superseded' and by_id['cccccccccccccccc']['replacement_ids'] == [BAND1]
    assert by_id['cccccccccccccccc']['geometry_reviewed'] is False and by_id['dddddddddddddddd']['replacement_ids'] == [BAND1]
    retired = by_id['eeeeeeeeeeeeeeee']
    assert retired['status'] == 'superseded' and retired['replacement_ids'] == [BAND1] and 'duplicate of the street' in retired['notes']
    assert by_id[BAND2]['status'] == 'confirmed' and by_id['aaaaaaaaaaaaaaaa']['replacement_ids'] == [BAND2]
    for oid in ('ffffffffffffffff', '1111111111111111', '2222222222222222'):          # reviewer-touched non-members byte-identical
        original = next(o for o in review['objects'] if o['id'] == oid)
        assert list(by_id[oid]) == list(original) and R.canonical(by_id[oid]) == R.canonical(original)   # same keys in order, same values (JSON.stringify writes 20.0 as 20)
    assert R.lineage_issue(carried['objects']) is None and carried['updated_by'] == R.UPDATED_BY and carried['history'][-1]['revision'] == 3
    rows = [json.loads(line) for line in (tmp_path/'run/registry.jsonl').read_text().splitlines()]
    assert len(rows) == 1 and rows[0]['role'] == 'road_band' and rows[0]['replaced_member_ids'] == record['replaced_member_ids']
    assert [b['number'] for b in rows[0]['bands']] == [1, 2] and rows[0]['marks']['sentence'] == SENTENCE


@needs_node
def test_candidate_band_without_accept_stays_a_candidate_and_leaves_decided_objects_alone(tmp_path):
    scene, review, proposals, sheet = setup(tmp_path)
    bands = bands_of(tmp_path, proposals, sheet)
    record = R.promote(WID, bands=[bands[1], bands[2]], **kwargs(tmp_path))
    assert record['applied'] == [2] and record['refused_bands'] == [1]
    by_id = {o['id']: o for o in json.loads((tmp_path/'data/scenes'/WID/'review.json').read_text())['objects']}
    assert by_id[BAND2]['status'] == 'candidate' and by_id[BAND2]['geometry_reviewed'] is False and 'provenance' not in by_id[BAND2]
    assert by_id['cccccccccccccccc']['status'] == 'confirmed' and by_id['eeeeeeeeeeeeeeee']['status'] == 'needs_adjustment' and BAND1 not in by_id


@needs_node
def test_junction_band_keeps_the_seed_s_other_arm_as_a_remainder_piece(tmp_path):
    scene, review, proposals, sheet = setup(tmp_path, junction=True)
    bands = bands_of(tmp_path, proposals, sheet)
    record = R.promote(WID, bands=[bands[3]], **kwargs(tmp_path))
    assert record['outcome'] == 'promoted'
    remainder = [f for f in record['features'] if f['role'] == 'remainder']
    assert len(remainder) == 1 and remainder[0]['replaces'] == ['3333333333333333']
    by_id = {o['id']: o for o in json.loads((tmp_path/'data/scenes'/WID/'review.json').read_text())['objects']}
    assert by_id['3333333333333333']['replacement_ids'] == [BAND3, remainder[0]['id']] and by_id[remainder[0]['id']]['derived_from'] == ['3333333333333333']
    assert R.lineage_issue(list(by_id.values())) is None


@needs_node
def test_rollback_restores_the_bytes(tmp_path):
    scene, review, proposals, sheet = setup(tmp_path)
    bands = bands_of(tmp_path, proposals, sheet)
    before = files_under(tmp_path/'data')
    record = R.promote(WID, bands=[bands[1]], marks=marks_file(tmp_path, {'1': 'right'}), **kwargs(tmp_path))
    new_sig = record['new_model_signature']
    result = R.rollback(WID, root=tmp_path, run_dir=tmp_path/'run', log=lambda _: None)
    after = files_under(tmp_path/'data')
    assert after[f'scenes/{WID}/latest.json'] == before[f'scenes/{WID}/latest.json'] and after[f'scenes/{WID}/review.json'] == before[f'scenes/{WID}/review.json']
    assert set(after) == set(before) | {f'scenes/{WID}/models/{new_sig}.json'} and result['registry_row_removed']
    assert (tmp_path/'run/registry.jsonl').read_text() == ''
    with pytest.raises(R.Refusal, match='already_rolled_back'):
        R.rollback(WID, root=tmp_path, run_dir=tmp_path/'run', log=lambda _: None)


@needs_node
def test_dry_run_writes_nothing_outside_the_run_dir(tmp_path):
    setup(tmp_path, junction=True)
    before = {k: v for k, v in files_under(tmp_path).items() if not k.startswith('run/')}
    proc = subprocess.run([sys.executable, '-m', 'scripts.ml.scene_road_promote', '--root', str(tmp_path), '--run-dir', str(tmp_path/'run'),
                           '--proposals', str(tmp_path/'prop'), '--marks-sheet', str(tmp_path/'MARKS.md'), '--registry', str(tmp_path/'r.jsonl'),
                           '--dry-run'], capture_output=True, text=True, cwd=str(R.REPO))
    lines = proc.stdout.strip().splitlines()
    assert proc.returncode == 0 and len(lines) == 1, proc.stdout+proc.stderr
    assert lines[0].startswith(f'PLAN {WID}: 2 band(s): #2 {BAND2} candidate') and f'#1 {BAND1} refused needs_reviewer_accept' in lines[0]
    after = {k: v for k, v in files_under(tmp_path).items() if not k.startswith('run/')}
    assert after == before and not (tmp_path/'r.jsonl').exists()
    plan = json.loads((tmp_path/'run'/f'{WID}.json').read_text())
    assert plan['applied'] == [2, 3] and plan['refused_bands'] == [1] and (tmp_path/'run/plan.txt').read_text().strip() == lines[0]


@needs_node
def test_scratch_copy_stands_for_its_window_and_writes_no_registry_row(tmp_path):
    setup(tmp_path)
    bands = bands_of(tmp_path, proposals=tmp_path/'prop', sheet=tmp_path/'MARKS.md')
    before = files_under(tmp_path/'data/scenes'/WID)
    scratch = R.make_scratch(tmp_path, WID, tag='t')
    assert scratch.startswith('qa_road_t_') and R.source_of(tmp_path, scratch) == WID
    record = R.promote(scratch, bands=[bands[1]], marks=marks_file(tmp_path, {'1': 'right'}, name='marks-SIMULATED.json'), **kwargs(tmp_path))
    assert record['outcome'] == 'promoted' and record['registry'] is None and not (tmp_path/'run/registry.jsonl').exists()
    assert files_under(tmp_path/'data/scenes'/WID) == before
    R.remove_scratch(tmp_path, scratch)
    assert not (tmp_path/'data/scenes'/scratch).exists()


@needs_node
def test_batch_rolls_back_a_window_whose_counter_moved(tmp_path):
    setup(tmp_path)
    bands = bands_of(tmp_path, proposals=tmp_path/'prop', sheet=tmp_path/'MARKS.md')
    before = files_under(tmp_path/'data')
    calls = []

    def counter():
        calls.append(1)
        base = {'primary_n': 25, 'primary_by_class': {}, 'primary_rows': [], 'targets_remaining': 64, 'takeover_rows': [], 'carried_rows': []}
        return {**base, 'primary_n': 26} if len(calls) == 2 else base
    summary = R.batch([WID], [bands[2]], counter=counter, **{k: v for k, v in kwargs(tmp_path).items() if k != 'log'}, log=lambda _: None)
    assert summary['rolled_back'] == [WID] and summary['stopped_at'] == WID and len(calls) == 3
    after = files_under(tmp_path/'data')
    assert {k: v for k, v in after.items() if '/models/' not in k} == {k: v for k, v in before.items() if '/models/' not in k}
    assert (tmp_path/'run/registry.jsonl').read_text() == ''


# --- the coverage guard (7 Oct): bands never silently drop member area outside them -------------------------------------------

WID2 = 'win-0000000000000002'
COVER_A, COVER_B, COVER_C = '0000000000000a01', '0000000000000b02', '00000000000000f5'


def cover_record(band_id, ring_m, members, *, seed=None, seed_status='candidate', on_accept=(), remainder=()):
    record = band_record(band_id, ring_m, seed=seed or sorted(members or on_accept)[0], seed_status=seed_status, replaces=members, on_accept=on_accept,
                         remainder=remainder)
    record['proposal']['cars_under'] = []
    return record


def coverage_window(root, model_objects, records, *, review_objects=None):
    """A second synthetic window (WID2) holding only `model_objects`, its review (`review_objects` or the model's copy) and a
    run-v31-shaped proposals directory + marks sheet numbering `records` 1.. in order; returns (scene, review, bands)."""
    folder = root/'data/scenes'/WID2
    (folder/'models').mkdir(parents=True)
    scene = {'schema_version': 1, 'id': WID2, 'address_id': 29773, 'cohort': 'windows', 'title': 'Coverage window', 'centroid': [145.1, -37.8],
             'image_sha256': 'cd'*32, 'image_width': 500, 'image_height': 500, 'pixel_to_meters': AFFINE, 'meters_to_pixel': INVERSE,
             'objects': json.loads(json.dumps(model_objects)), 'groups': [], 'scans': {'road': {'status': 'complete', 'count': len(model_objects), 'prompt': 'road'}},
             'region': {'provider': 'google', 'imagery_zoom': 21}, 'window_frame': {'lots': ['2\\PS123456'], 'size_px': 500}, 'analysis': {'input_geometry': 'imagery-only'}}
    scene['model_signature'] = R.L.signature(scene)
    text = json.dumps(scene, indent=2)
    (folder/'latest.json').write_text(text)
    (folder/'models'/f"{scene['model_signature']}.json").write_text(text)
    review = {'revision': 3, 'model_signature': scene['model_signature'], 'objects': review_objects or json.loads(json.dumps(model_objects)), 'groups': [],
              'reviewed_classes': [], 'basemap_mode': 'raw', 'updated_at': '2026-10-02T00:00:00Z', 'updated_by': 'local'}
    (folder/'review.json').write_text(json.dumps(review, indent=2))
    proposals = root/'prop2'
    proposals.mkdir()
    (proposals/f'{WID2}.review.json').write_text(json.dumps({'scene': WID2, 'layer': 'review', 'proposals': records, 'exits': []}))
    (proposals/'metrics.json').write_text(json.dumps({'written_at': '2026-10-07T08:00:41.939550+00:00'}))
    sheet = root/'MARKS2.md'
    sheet.write_text('# MARKS\n\n' + '\n'.join(f"{n}. `{WID2}` band `{r['proposal']['id']}` from road `{r['seed']}` ({r['seed_status']}): ..."
                                               for n, r in enumerate(records, 1)) + '\n')
    return scene, review, R.load_bands(root, proposals=proposals, sheet=sheet)


def holed(o, *holes_m):
    return {**o, 'holes': [px(h) for h in holes_m]}


# band A over x 0-20, y 0-10 m; band B over x 20-40, y 0-10 m (touching A at x 20)
THRESHOLD_OBJECTS = [
    obj('a1a1a1a1a1a1a1a1', 'road', rect(1, 9.51, 3, 10.51)),        # 0.49 of it inside band A: kept as it is
    obj('a2a2a2a2a2a2a2a2', 'road', rect(4, 9.49, 6, 10.49)),        # 0.51 inside: superseded
    obj('a3a3a3a3a3a3a3a3', 'road', rect(8, 0, 10, 10.95)),          # superseded; 1.9 m2 outside: absorbed
    obj('a4a4a4a4a4a4a4a4', 'road', rect(12, 0, 14, 11.05)),         # superseded; 2.1 m2 outside: one remainder
    obj('a5a5a5a5a5a5a5a5', 'road', rect(15, 2, 25, 4)),             # 0.5 inside A (exactly); the 10 m2 outside A lies in band B
    obj('b1b1b1b1b1b1b1b1', 'road', rect(20.5, .5, 39.5, 9.5)),      # band B's own member
]


def threshold_plan(tmp_path, accepted=frozenset()):
    scene, review, bands = coverage_window(tmp_path, THRESHOLD_OBJECTS, [
        cover_record(COVER_A, rect(0, 0, 20, 10), ['a1a1a1a1a1a1a1a1', 'a2a2a2a2a2a2a2a2', 'a3a3a3a3a3a3a3a3', 'a4a4a4a4a4a4a4a4', 'a5a5a5a5a5a5a5a5']),
        cover_record(COVER_B, rect(20, 0, 40, 10), ['b1b1b1b1b1b1b1b1'])])
    return scene, review, bands, R.plan_window(scene, review, [bands[1], bands[2]], set(accepted))


def test_a_member_mostly_outside_its_band_is_kept_just_below_half_and_superseded_just_above(tmp_path):
    scene, review, bands, plan = threshold_plan(tmp_path)
    row = next(r for r in plan['bands'] if r['id'] == COVER_A)
    assert row['refused'] == [] and plan['applied'] == [1, 2]
    assert [k['id'] for k in row['kept_outside_member']] == ['a1a1a1a1a1a1a1a1'] and row['kept_outside_member'][0]['inside_share'] == pytest.approx(.49, abs=1e-3)
    assert 'a1a1a1a1a1a1a1a1' not in row['members'] and 'a1a1a1a1a1a1a1a1' not in plan['members']
    assert {'a2a2a2a2a2a2a2a2', 'a5a5a5a5a5a5a5a5'} <= set(row['members'])                 # 0.51 and exactly 0.5 inside: superseded
    band = next(f for f in plan['features'] if f['id'] == COVER_A)
    assert band['replaces'] == row['members'] and band['road_band']['kept_outside_member'] == ['a1a1a1a1a1a1a1a1']
    new = R.L.build_snapshot(scene, plan['features'], plan['members'])
    assert next(o for o in new['objects'] if o['id'] == 'a1a1a1a1a1a1a1a1') == next(o for o in scene['objects'] if o['id'] == 'a1a1a1a1a1a1a1a1')


def test_an_outside_part_of_2_m2_or_more_stays_as_a_remainder_and_a_smaller_one_is_absorbed_and_listed(tmp_path):
    _, _, _, plan = threshold_plan(tmp_path)
    row = next(r for r in plan['bands'] if r['id'] == COVER_A)
    cov = row['coverage']
    assert [(r['from'], r['area_m2']) for r in cov['remainders']] == [(['a4a4a4a4a4a4a4a4'], 2.1)]        # 2.1 m2: kept
    absorbed = {tuple(a['from']): a['area_m2'] for a in cov['absorbed']}
    assert absorbed[('a3a3a3a3a3a3a3a3',)] == pytest.approx(1.9, abs=1e-3)                              # 1.9 m2: absorbed, listed
    assert absorbed[('a2a2a2a2a2a2a2a2',)] == pytest.approx(.98, abs=1e-3)
    assert not any('a5a5a5a5a5a5a5a5' in a['from'] for a in cov['absorbed']) and not any('a5a5a5a5a5a5a5a5' in r['from'] for r in cov['remainders'])   # under band B
    assert cov['uncovered_after_m2'] == pytest.approx(cov['absorbed_m2'], abs=.01)
    remainder = next(f for f in plan['features'] if f['id'] == cov['remainders'][0]['id'])
    assert remainder['kind'] == 'road' and remainder['status'] == 'candidate' and remainder['geometry_reviewed'] is False and remainder['replaces'] == ['a4a4a4a4a4a4a4a4']
    assert remainder['road_band']['origin'] == 'coverage' and remainder['road_band']['band'] == COVER_A and 'derived_from' not in remainder
    assert R.app_ring_issues(remainder['ring'], remainder['holes'], 500, 500) == []
    assert row['remainder_ids'] == [remainder['id']] and plan['specs'][0]['remainder_ids'] == [remainder['id']]
    assert Polygon(remainder['ring']).area*.01 == pytest.approx(2.1, abs=1e-3)


# the band-14 shape (a junction piece wholly outside the band) and the band-15 shape (a 20 m2 parking-lane strip outside a too-narrow
# band, shared by the road piece and its driveway-prompt twin; the road piece has a car cut out of the strip)
SHAPE_OBJECTS = [
    obj('c1c1c1c1c1c1c1c1', 'road', rect(.5, .5, 19.5, 9.5)),
    obj('c2c2c2c2c2c2c2c2', 'road', rect(21, 2, 24, 6)),                               # band 14's 600a: wholly outside, 12 m2
    holed(obj('d1d1d1d1d1d1d1d1', 'road', rect(0, 20, 40, 28.5)), rect(10, 28.1, 14, 28.4)),
    holed(obj('d2d2d2d2d2d2d2d2', 'driveway', rect(0, 20.2, 40, 28.5)), rect(10, 28.1, 14, 28.4)),   # the twin's mask avoids the same car
]


@needs_node
def test_band_14_shape_a_member_wholly_outside_stays_and_band_15_shape_strip_becomes_one_remainder(tmp_path):
    scene, review, bands = coverage_window(tmp_path, SHAPE_OBJECTS, [
        cover_record(COVER_A, rect(0, 0, 20, 10), ['c1c1c1c1c1c1c1c1', 'c2c2c2c2c2c2c2c2']),
        cover_record(COVER_B, rect(0, 20, 40, 28), ['d1d1d1d1d1d1d1d1', 'd2d2d2d2d2d2d2d2'])])
    record = R.promote(WID2, bands=[bands[1], bands[2]], **kwargs(tmp_path))
    assert record['outcome'] == 'promoted' and record['applied'] == [1, 2]
    rows = {r['id']: r for r in record['bands']}
    assert [k['id'] for k in rows[COVER_A]['kept_outside_member']] == ['c2c2c2c2c2c2c2c2'] and rows[COVER_A]['members'] == ['c1c1c1c1c1c1c1c1']
    assert rows[COVER_A]['kept_outside_member'][0]['inside_share'] == 0 and rows[COVER_A]['coverage']['remainders'] == []
    cov = rows[COVER_B]['coverage']
    assert len(cov['remainders']) == 1 and cov['remainders'][0]['from'] == ['d1d1d1d1d1d1d1d1', 'd2d2d2d2d2d2d2d2'] and cov['remainders'][0]['area_m2'] == 20.0
    assert cov['remainders'][0]['holes'] == 0 and cov['uncovered_after_m2'] == 0 and cov['absorbed'] == []
    rid = cov['remainders'][0]['id']
    folder = tmp_path/'data/scenes'/WID2
    latest = json.loads((folder/'latest.json').read_text())
    model = {o['id']: o for o in latest['objects']}
    assert 'c2c2c2c2c2c2c2c2' in model and 'c1c1c1c1c1c1c1c1' not in model and 'd1d1d1d1d1d1d1d1' not in model and model[rid]['replaces'] == ['d1d1d1d1d1d1d1d1', 'd2d2d2d2d2d2d2d2']
    by_id = {o['id']: o for o in json.loads((folder/'review.json').read_text())['objects']}
    kept_before = next(o for o in review['objects'] if o['id'] == 'c2c2c2c2c2c2c2c2')
    assert list(by_id['c2c2c2c2c2c2c2c2']) == list(kept_before) and R.canonical(by_id['c2c2c2c2c2c2c2c2']) == R.canonical(kept_before)   # exactly as it was
    assert by_id['c1c1c1c1c1c1c1c1']['replacement_ids'] == [COVER_A]
    for m in ('d1d1d1d1d1d1d1d1', 'd2d2d2d2d2d2d2d2'):
        assert by_id[m]['status'] == 'superseded' and by_id[m]['replacement_ids'] == [COVER_B, rid]
    remainder = by_id[rid]
    assert remainder['kind'] == 'road' and remainder['status'] == 'candidate' and remainder['derived_from'] == ['d1d1d1d1d1d1d1d1', 'd2d2d2d2d2d2d2d2']
    assert remainder['holes'] == [] and R.app_ring_issues(remainder['ring'], [], 500, 500) == [] and R.lineage_issue(list(by_id.values())) is None


@needs_node
def test_an_accepted_band_whose_decided_member_has_a_remainder_sorting_after_the_band_promotes(tmp_path):
    """The recovery hands a reviewer-touched member's words to the LAST feature replacing it; COVER_C sorts before its remainder."""
    review_objects = json.loads(json.dumps(SHAPE_OBJECTS))
    review_objects[2] = decided(review_objects[2], notes='parking lane is road')
    scene, review, bands = coverage_window(tmp_path, SHAPE_OBJECTS, [
        cover_record(COVER_C, rect(0, 20, 40, 28), ['d2d2d2d2d2d2d2d2'], seed='d1d1d1d1d1d1d1d1', seed_status='confirmed', on_accept=['d1d1d1d1d1d1d1d1'])],
        review_objects=review_objects)
    record = R.promote(WID2, bands=[bands[1]], marks=marks_file(tmp_path, {'1': 'right'}), **kwargs(tmp_path))
    rid = record['bands'][0]['coverage']['remainders'][0]['id']
    assert record['outcome'] == 'promoted' and rid > COVER_C
    by_id = {o['id']: o for o in json.loads((tmp_path/'data/scenes'/WID2/'review.json').read_text())['objects']}
    assert by_id[COVER_C]['status'] == 'confirmed' and by_id[rid]['status'] == 'candidate' and 'notes' not in by_id[rid]
    assert by_id['d1d1d1d1d1d1d1d1']['status'] == 'superseded' and by_id['d1d1d1d1d1d1d1d1']['notes'] == 'parking lane is road'


def test_a_junction_seed_kept_outside_its_band_keeps_its_arms_whole(tmp_path):
    objects = [obj('e1e1e1e1e1e1e1e1', 'road', rect(0, 0, 10, 30))]                 # 0.2 inside the band: kept, its arm piece is not cut
    scene, review, bands = coverage_window(tmp_path, objects, [
        cover_record(COVER_A, rect(0, 0, 10, 6), ['e1e1e1e1e1e1e1e1'], remainder=[rect(0, 6.2, 10, 30)])])
    plan = R.plan_window(scene, review, [bands[1]], set())
    row = plan['bands'][0]
    assert [k['id'] for k in row['kept_outside_member']] == ['e1e1e1e1e1e1e1e1'] and row['junction_remainder_skipped'][0]['from'] == 'e1e1e1e1e1e1e1e1'
    assert plan['nothing_to_apply'] and [r[0] for r in row['refused']] == ['nothing_to_replace']


def test_a_member_split_into_more_pieces_than_the_app_can_link_refuses_the_band(tmp_path):
    from shapely.geometry import box
    from shapely.ops import unary_union
    comb = unary_union([box(.5, .5, 39.5, 9.5), *(box(x, 9, x+1, 13) for x in range(1, 33, 4))])     # 8 teeth of 3 m2 outside the band
    objects = [obj('f1f1f1f1f1f1f1f1', 'road', list(comb.exterior.coords)[:-1])]
    scene, review, bands = coverage_window(tmp_path, objects, [cover_record(COVER_A, rect(0, 0, 40, 10), ['f1f1f1f1f1f1f1f1'])])
    plan = R.plan_window(scene, review, [bands[1]], set())
    row = plan['bands'][0]
    assert len(row['coverage']['remainders']) == 8 and [r[0] for r in row['refused']] == ['member_replacements_over_cap'] and plan['nothing_to_apply']


def test_a_retire_target_mostly_outside_its_band_refuses_the_band(tmp_path):
    objects = [obj('g1g1g1g1g1g1g1g1', 'road', rect(0, 0, 20, 10)), obj('g2g2g2g2g2g2g2g2', 'driveway', rect(0, 8, 20, 30))]
    record = band_record(COVER_A, rect(0, 0, 20, 10), seed='g1g1g1g1g1g1g1g1', seed_status='candidate', replaces=('g1g1g1g1g1g1g1g1',), retires=('g2g2g2g2g2g2g2g2',))
    record['retires_on_accept'][0]['status'] = 'candidate'
    scene, review, bands = coverage_window(tmp_path, objects, [record])
    plan = R.plan_window(scene, review, [bands[1]], {1}, marks_file(tmp_path, {'1': 'right'}))
    assert [r[0] for r in plan['bands'][0]['refused']] == ['retire_target_mostly_outside_band'] and plan['nothing_to_apply']


def test_a_remainder_ring_the_app_refuses_refuses_the_band(tmp_path):
    zigzag = [(39.5 - .15*k, 11. if k % 2 == 0 else 11.2) for k in range(251)]      # SAM's ragged edge: 251 vertices outside the band
    objects = [obj('h1h1h1h1h1h1h1h1', 'road', [(.5, .5), (39.5, .5), *zigzag, (.5, 11.), (.5, .5)][:-1])]
    scene, review, bands = coverage_window(tmp_path, objects, [cover_record(COVER_A, rect(0, 0, 40, 10), ['h1h1h1h1h1h1h1h1'])])
    plan = R.plan_window(scene, review, [bands[1]], set())
    row = plan['bands'][0]
    assert row['coverage']['remainders'][0]['vertices'] > 200 and [r[0] for r in row['refused']] == ['remainder_ring_app_issue'] and plan['nothing_to_apply']
