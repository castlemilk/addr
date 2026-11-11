"""Round-5 human precision judge from rows (r5_precision): decisions and exclusions, the frozen-snapshot join, detection at a
cut, the precision block below and above the N rule, matched cuts, the re-judge of run dirs, the reviewer guide, the RULES
pre-registration and the report wording. No model is loaded and nothing under data/ is written: every fixture lives in
tmp_path. Wording: agreement between model estimates and human decisions or references; nothing is promoted."""
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from scripts.ml import scene_confidence
from scripts.ml.training.sam3_mac import r5_precision as r5

REPO = Path(__file__).resolve().parents[3]
RULES5 = REPO / 'data/reviews/eyes/training/sam3-mac/round5/RULES.json'
NDSM_RULES = REPO / 'data/reviews/eyes/training/distill-linz-chc-2025-0075/teacher/judges/ndsm_rules.json'
# The fields the amendments named through the restore-slice instant of 2026-10-06T05:05:00Z, in order: the frozen record. Entries are
# appended under the dated convention (previous_sha256, code_sha256, thresholds_changed False), so the live list is checked as starting
# with this history, never as equal to it.
FROZEN_AMENDED_FIELDS = ('ndsm_leg.loso.pre_registered_counts_on_4132_judge_rows.outline.part', 'written_before_note',
                         'human_precision.agent_text_regex', 'human_precision.fresh_marker_regex', 'human_precision.agent_fresh_decision',
                         'human_precision.joined_not_counted', 'human_precision.outputs',
                         # 2026-10-01 (reviewer "yes to defaults"): the frame-cut lineage hop (three fields) and the accepted-import route
                         'human_precision.join_order', 'human_precision.population_primary', 'human_precision.targets',
                         'human_precision.agent_fresh_decision', 'human_precision.classes_order',
                         # 2026-10-01 stream T: the accepted-import route tightened (accepted_by 'local'; geometry_reviewed on confirmed)
                         'human_precision.agent_fresh_decision',
                         # 2026-10-01 stream C: the design-sha chain rule (thresholds_changed-false hops) in the hop and the targets parenthetical
                         'human_precision.join_order', 'human_precision.targets',
                         # 2026-10-02 stream A: the takeover hop (D.replaces -> copy -> carried_from -> frozen key) after the reviewer's direction
                         'human_precision.join_order', 'human_precision.population_primary', 'human_precision.targets',
                         'human_precision.classes_order',
                         # 2026-10-02T00:21:02Z: the TAKEOVER RULE mirror appended to join_order (the WINDOW.json amendment's rules_mirror; D.derived_from reciprocal)
                         'human_precision.join_order',
                         # 2026-10-03 stream B (reviewer "yes to defaults" 15:10 and 19:40 AEST): the linear-merge mirror of the DESIGN.json field, a new top-level field
                         'linear_merge',
                         # 2026-10-06 stream A (reviewer "yes to defaults" on the slice-body census card, item 2): the body-carry replacer in the takeover hop
                         'human_precision.join_order', 'human_precision.targets',
                         # 2026-10-06T05:05:00Z (corrects the body-carry entries): a refused body link restores the slice as the asked target
                         'human_precision.join_order', 'human_precision.targets',
                         # 2026-10-06T08:51:58Z stream S1 (reviewer "go" 12:30 AEDT): RECIPE.json adopted (the not_committed item replaced) and the
                         # calibration field (val_adjacent_to_train naming every r5-* run) added from null
                         'not_committed', 'calibration')
RULES5_SHA_BEFORE_TAKEOVER = '97590a2a706f050df65c49a4da8aa46cb5e9c9cb8b3d2f84bc84e22d2adb2e92'
RULES5_SHA_BEFORE_TIGHTEN = 'df42a3e51bf7f61e2b991960f22c5e1c3b22c8bf2a63ff3b3e0979e3a60ac436'
RULES5_SHA_BEFORE_CHAIN = '738ed82a81bec20bcf732f9dc0281f077b58baf5515f8cee54a34b50b23c348d'
RULES5_SHA_BEFORE_LINEAR_MERGE = '89e92c5e66386b753314aca2510fd82f16b118a1d45b3e2f2641b8949362f8a9'
RULES5_SHA_BEFORE_RESTORE_SLICE = 'ff474a0950af7d8f42c7b59621a9bf579e57b4a3367bddc774e2d9bf0001a1b3'
RULES5_SHA_BEFORE_RECIPE_ADOPTION = '61ef73a69fead5e58034f6293ebd27e649711bcf3789428ab838ba5811301a2f'
# Amendment-list indices (0-based; the handover counts entries 1-based) where a convention began or an exception sits.
RULES5_PRE_REGISTRATION_BATCH = 7           # indices 0-6: one instant (2026-09-30T13:33:07Z) written with the pre-registration, before previous_sha256 was recorded
RULES5_INHERITED_SENTENCE_INDEX = 19        # 2026-10-02T00:21:02Z: the TAKEOVER RULE mirror copied WINDOW.json's "agreement never <banned word>" sentence into join_order
RULES5_THRESHOLDS_CHANGED_FROM = 20         # 2026-10-03T10:40:14Z (linear_merge): thresholds_changed / thresholds_changed_note / code_sha256 on every entry since
RULES5_AMENDMENTS_KEY_MOVED_AT = '2026-10-03T10:40:14Z'   # the linear-merge write moved the top-level amendments key from after copied_from to the end of the file
DESIGN5 = REPO / 'data/reviews/eyes/recapture/DESIGN.json'
AGENT_TEXT = 'Assistant visually traced from saved Google imagery on 2026-09-23. Image-based roof extent, not a surveyed wall footprint.'
BANNED = 'accuracy'   # the wording rule's banned word: asserted absent from every output


# --- fixtures ----------------------------------------------------------------------------------------------------------

def _obj(oid, status='candidate', kind='building', source='sam3-text-onnx', area=10., **extra):
    o = {'id': oid, 'kind': kind, 'label': 'Building', 'status': status, 'source': source,
         'geometry_reviewed': status == 'confirmed', 'model': {'area_m2': area}}
    o.update(extra)
    return o


def _live(oid, source='sam3-text-onnx', kind='building', notes=None):
    o = {'id': oid, 'kind': kind, 'source': source, 'status': 'candidate'}
    if notes:
        o['notes'] = notes
    return o


def _scene(root, sid, review=None, latest=None, revision=3, sig='sig', image_sha=None, review_sig=None, frame_transform=None):
    folder = root / sid
    folder.mkdir(parents=True, exist_ok=True)
    if latest is not None:
        snapshot = {'id': sid, 'model_signature': sig, 'image_sha256': image_sha or f'img-{sid}', 'objects': latest}
        if frame_transform is not None:
            snapshot['frame_transform'] = frame_transform
        json.dump(snapshot, open(folder / 'latest.json', 'w'))
    if review is not None:
        json.dump({'revision': revision, 'model_signature': review_sig or sig, 'updated_at': '2026-09-30T11:00:00.000Z',
                   'updated_by': 'local', 'history': [], 'objects': review}, open(folder / 'review.json', 'w'))
    return folder


def _pseudo(ann_id, image_id, scene, oid, area):
    return {'id': ann_id, 'image_id': image_id, 'scene_id': scene, 'provenance': 'machine_pseudo', 'object_id': oid, 'area_m2': area,
            'ref_id': None, 'tier': 'T3', 'boundary_reference': False, 'segmentation': [[0, 0, 4, 0, 4, 4]]}


def _ref_ann(ann_id, image_id, scene, oid, area, ref_id=None, tier='T1'):
    return {'id': ann_id, 'image_id': image_id, 'scene_id': scene, 'provenance': f'human_{tier}', 'object_id': oid, 'ref_id': ref_id,
            'area_m2': area, 'tier': tier, 'boundary_reference': True, 'segmentation': [[0, 0, 4, 0, 4, 4]]}


def _split(folder, name, images, anns):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'{name}.json'
    path.write_text(json.dumps({'images': images, 'annotations': anns, 'categories': [{'id': 1, 'name': 'building_roof'}]}))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _image(iid, scene):
    return {'id': iid, 'scene_id': scene, 'file_name': f'images/{iid}.png', 'width': 64, 'height': 64, 'pixel_size_m': .236,
            'sha256': f'img-{scene}', 'model_signature': 'sig'}


def _ctx_image(scene, pseudo_ids, references=0):
    return {'scene_id': scene, 'pixel_size_m': .236, 'width': 64, 'height': 64, 'hectares': .0228, 'pixel_band': '0_236',
            'pseudo_ids': list(pseudo_ids), 'footprints_in_tile': 0, 'references': references, 'tile_group': f'g-{scene}'}


def _ctx_ref(image_id, scene, ann_id, oid, area, ref_id=None, tier='T1'):
    return {'image_id': image_id, 'scene_id': scene, 'ann_id': ann_id, 'ref_id': ref_id, 'object_id': oid, 'tier': tier,
            'provenance': f'human_{tier}', 'area_m2': area, 'area_band': 'lt_25', 'image_identity': 'same_image'}


def _context(dataset, split, sha, images, refs, arms=('base', 'ctrl', 'tuned'), pairs=('tuned:base', 'tuned:ctrl', 'ctrl:base')):
    specs = ['base'] + [f'{a}=runs/{a}/trainable-final.pt' for a in arms[1:]]
    return {'version': 'test', 'dataset': str(dataset), 'split': split, 'images': images, 'refs': refs, 'arms': list(arms),
            'arm_specs': specs, 'pairs': list(pairs),
            'meta': {'rows_collected_at_threshold': .2, 'split_file_sha256': sha, 'judge_version_sha256': 'judge-sha',
                     'dataset_manifest_sha256': None, 'view_threshold': .2}}


def _pred(arm, image_id, score, area_m2=10., ref_overlaps=(), pseudo_overlaps=(), index=0):
    best = max((o['iou'] for o in ref_overlaps), default=0.)
    return {'arm': arm, 'image_id': image_id, 'scene_id': f's{image_id}', 'index': index, 'pixel_size_m': .236, 'image_ha': .0228,
            'score': score, 'area_px': 100, 'area_m2': area_m2, 'kind': 'pseudo_only', 'footprint_backed': None,
            'matched_ref_id': next((o['ref'] for o in ref_overlaps if o['iou'] >= .5), None), 'best_iou': best,
            'ref_overlaps': list(ref_overlaps), 'pseudo_overlaps': list(pseudo_overlaps)}


def _live_world(tmp_path):
    """Three scenes: s1 and s2 judged (image 1 pseudo rows, image 2 references), s3 an un-judged tile of the same split."""
    scenes = tmp_path / 'scenes'
    dataset = tmp_path / 'dataset-v2'
    images = [_image(1, 's1'), _image(2, 's2'), _image(3, 's3')]
    anns = [_pseudo(11, 1, 's1', 'p-roof', 12.), _pseudo(12, 1, 's1', 'p-not', 8.), _pseudo(13, 1, 's1', 'p-snap251', 25.1),
            _pseudo(14, 1, 's1', 'p-snap249', 24.9), _pseudo(15, 1, 's1', 'p-open', 10.), _pseudo(16, 1, 's1', 'p-unlisted', 9.),
            _pseudo(17, 1, 's1', 'p-open2', 6.), _pseudo(18, 1, 's1', 'p-gone', 11.),
            _ref_ann(21, 2, 's2', 'r-drawn', 9.5, 'x1'), _ref_ann(22, 2, 's2', 'o-other', 15., 'x2'),
            _ref_ann(23, 2, 's2', 'agent-traced', 11.9, None), _ref_ann(24, 2, 's2', 'r-prod', 20., None),
            _ref_ann(25, 2, 's2', 'r-gone', 14., None), _ref_ann(26, 2, 's2', 'agent-fresh', 10.5, None),
            _pseudo(31, 3, 's3', 'p-unjudged', 10.)]
    split_path, sha = _split(dataset, 'test', images, anns)
    ctx_images = {'1': _ctx_image('s1', [11, 12, 13, 14, 15, 17, 18]), '2': _ctx_image('s2', [], references=6)}
    refs = {'2:21': _ctx_ref(2, 's2', 21, 'r-drawn', 9.5, 'x1'), '2:22': _ctx_ref(2, 's2', 22, 'o-other', 15., 'x2'),
            '2:23': _ctx_ref(2, 's2', 23, 'agent-traced', 11.9), '2:24': _ctx_ref(2, 's2', 24, 'r-prod', 20.),
            '2:25': _ctx_ref(2, 's2', 25, 'r-gone', 14.), '2:26': _ctx_ref(2, 's2', 26, 'agent-fresh', 10.5)}
    context = _context(dataset, 'test', sha, ctx_images, refs)
    _scene(scenes, 's1', latest=[_live(o) for o in ('p-roof', 'p-not', 'p-snap251', 'p-snap249', 'p-open', 'p-unlisted', 'p-open2')],
           review=[_obj('p-roof', 'confirmed', area=12.), _obj('p-not', 'rejected', area=8.),
                   _obj('p-snap251', 'rejected', area=24.9),                       # snapshot 25.1: neither a target nor a count
                   _obj('p-snap249', 'confirmed', area=25.1),                      # snapshot 24.9: counted whatever the review says
                   _obj('p-unlisted', 'confirmed', area=9.),                       # pseudo row not in pseudo_ids: not joined
                   _obj('small-new', 'confirmed', source='reviewer', area=6.),     # judged tile, not in the snapshot
                   _obj('tiny1', 'rejected', source='reviewer', area=2.),          # under the 4 m2 export floor
                   _obj('p-gone', 'confirmed', area=11.)])                         # model source, no longer in latest.json
    _scene(scenes, 's2', latest=[_live('r-prod'), _live('r-drawn', source='reviewer'), _live('agent-traced', source='reviewer')],
           review=[_obj('r-drawn', 'confirmed', source='reviewer', area=9.5), _obj('ref-x2', 'confirmed', source='reviewer', area=15.),
                   _obj('agent-traced', 'confirmed', source='reviewer', area=11.9, notes=AGENT_TEXT),
                   _obj('r-prod', 'rejected', area=20.),
                   _obj('r-gone', 'confirmed', area=14.),                          # model-source reference, confirmed, not live
                   _obj('agent-fresh', 'confirmed', source='reviewer', area=10.5,  # the reviewer's marked decision on an
                        notes='[reviewer] roof ' + AGENT_TEXT)])                   # outline the assistant drew
    _scene(scenes, 's3', latest=[_live('p-unjudged')],
           review=[_obj('p-unjudged', 'rejected', area=10.),
                   _obj('tiny3', 'rejected', area=1.44, provenance={'human_decision': {'source': 'human:chat', 'fields': ['status']}})])
    return {'scenes': scenes, 'dataset': dataset, 'split': split_path, 'context': context}


def _row(joined, oid):
    return next(r for r in joined['rows'] if r['object_id'] == oid)


# --- load_decisions ----------------------------------------------------------------------------------------------------

def test_load_decisions_classes_exclusions_and_fact_columns(tmp_path, monkeypatch):
    scenes = tmp_path / 'scenes'
    carried = {'scene_id': 'house-1', 'object_id': 'old', 'method': 'georeferenced-affine'}
    review = [_obj('ui-roof', 'confirmed'), _obj('ui-not', 'rejected'),
              _obj('part', 'needs_adjustment', review_issues=['instances']), _obj('adj', 'needs_adjustment', review_issues=['outline']),
              _obj('cand', 'candidate'), _obj('sup', 'superseded', replacement_ids=['x']),
              _obj('chat-roof', 'confirmed', provenance={'human_decision': {'source': 'human:chat', 'fields': ['status']}}),
              _obj('mach-hc', 'confirmed', provenance={'source': 'machine:decision-policy-v2', 'human_confirmed': True}),
              _obj('mach', 'rejected', provenance={'source': 'machine:decision-policy-v2'}),
              _obj('fw', 'confirmed', fieldwork_import={'pending': True}),
              _obj('agent', 'confirmed', notes=AGENT_TEXT),
              _obj('agent-lower', 'rejected', notes='assistant image review 2026-09-23: a fabric cover'),
              _obj('agent-later', 'confirmed', notes='Reviewer: roof. ' + AGENT_TEXT),    # own words first, no [reviewer] marker
              _obj('fresh-roof', 'confirmed', notes='[reviewer] roof. ' + AGENT_TEXT),
              _obj('fresh-not', 'rejected', notes='[Reviewer 2026-09-30] not a roof | ' + AGENT_TEXT),
              _obj('fresh-conflict', 'confirmed', notes='[reviewer] not a roof ' + AGENT_TEXT),   # words and status disagree
              _obj('fresh-noclass', 'confirmed', notes='[reviewer] checked ' + AGENT_TEXT),
              _obj('chat-appended', 'confirmed', notes=AGENT_TEXT + ' | [reviewer 2026-09-30] roof',
                   provenance={'human_decision': {'source': 'human:chat', 'fields': ['notes', 'status']}}),
              _obj('carried', 'needs_adjustment', carried_from=carried, geometry_reviewed=True,
                   provenance={'human_decision': {'source': 'human:chat', 'fields': ['status']}}),
              _obj('carried-confirmed', 'confirmed', carried_from=carried),
              _obj('drawn', 'confirmed', source='reviewer'), _obj('gone', 'confirmed'),
              _obj('car', 'rejected', kind='car')]
    _scene(scenes, 'map-1', review=review, revision=28,
           latest=[_live(o['id']) for o in review if o['id'] not in ('gone', 'drawn')] + [_live('drawn', source='reviewer')])
    _scene(scenes, 'map-2', review=[_obj('sig-off', 'confirmed')], latest=[_live('sig-off')], sig='new', review_sig='old')
    _scene(scenes, 'map-3', review=[_obj('rev1', 'confirmed', notes='[reviewer] roof ' + AGENT_TEXT)], latest=[_live('rev1')], revision=1)
    _scene(scenes, 'qa_scratch', review=[_obj('qa-roof', 'confirmed')], latest=[_live('qa-roof')])
    loaded = r5.load_decisions(scenes)
    rows = {(r['scene_id'], r['object_id']): r for r in loaded['rows']}
    get = lambda oid, s='map-1': rows[(s, oid)]                                          # noqa: E731
    assert not any(s == 'qa_scratch' for s, _ in rows), 'qa_* scenes are never read'
    assert (get('ui-roof')['cls'], get('ui-roof')['provenance_kind'], get('ui-roof')['decided']) == ('roof', 'ui', True)
    assert (get('ui-not')['cls'], get('ui-not')['decided']) == ('not_roof', True)
    assert (get('part')['cls'], get('part')['decided']) == ('roof_part', False), 'part of a roof: counted apart, never not-roof'
    assert get('adj')['cls'] == get('cand')['cls'] == 'undecided'
    assert (get('sup')['cls'], get('sup')['stratum']) == ('excluded', 'superseded')
    assert (get('chat-roof')['cls'], get('chat-roof')['provenance_kind'], get('chat-roof')['decided']) == ('roof', 'chat', True)
    assert (get('mach-hc')['authority_blocker'], get('mach-hc')['stratum'], get('mach-hc')['decided']) == \
        ('machine_label', 'machine_label_human_confirmed', False)
    assert (get('mach')['stratum'], get('mach')['cls']) == ('machine_label', 'excluded')
    assert (get('fw')['authority_blocker'], get('fw')['stratum'], get('fw')['decided']) == ('fieldwork_pending', 'fieldwork_pending', False)
    for oid in ('agent', 'agent-lower'):
        assert (get(oid)['provenance_kind'], get(oid)['stratum'], get(oid)['decided'], get(oid)['fresh']) == \
            ('agent', 'agent_authored_excluded', False, 'no_marker')
    assert (get('agent-later')['provenance_kind'], get('agent-later')['fresh'], get('agent-later')['decided']) == ('agent', 'no_marker', False), \
        "own words without the [reviewer] marker and class words leave the assistant's status in place"
    assert get('agent-later')['agent_note'] is True and get('ui-roof')['agent_note'] is False and get('ui-roof')['fresh'] is None, \
        "the assistant's text is found anywhere in the notes"
    assert (get('fresh-roof')['cls'], get('fresh-roof')['provenance_kind'], get('fresh-roof')['fresh'], get('fresh-roof')['decided']) == \
        ('roof', 'ui', 'valid', True)
    assert (get('fresh-not')['cls'], get('fresh-not')['fresh'], get('fresh-not')['decided']) == ('not_roof', 'valid', True)
    assert (get('fresh-conflict')['stratum'], get('fresh-conflict')['fresh'], get('fresh-conflict')['decided']) == \
        ('agent_authored_excluded', 'class_conflict', False), 'words that disagree with the status never count'
    assert (get('fresh-noclass')['fresh'], get('fresh-noclass')['decided']) == ('no_marker', False)
    assert (get('chat-appended')['provenance_kind'], get('chat-appended')['fresh']) == ('agent', 'no_marker'), \
        'record-chat-feedback appends after the assistant text: still excluded'
    assert (get('rev1', 'map-3')['fresh'], get('rev1', 'map-3')['decided'], get('rev1', 'map-3')['stratum']) == \
        ('revision_1', False, 'agent_authored_excluded'), 'a marked note in the revision-1 (assistant) save is not a later reviewer save'
    assert (get('carried')['cls'], get('carried')['provenance_kind'], get('carried')['decided']) == ('undecided', 'carried_undecided', False)
    assert (get('carried-confirmed')['cls'], get('carried-confirmed')['decided']) == ('roof', True)
    assert get('ui-roof')['production'] is True and get('drawn')['production'] is False and get('gone')['production'] is False
    assert get('gone')['live'] is False and get('drawn')['live'] is True
    assert get('car')['cls'] == 'skipped' and get('car')['decided'] is False, 'kind other than building never counts'
    sha = hashlib.sha256((scenes / 'map-1' / 'review.json').read_bytes()).hexdigest()
    assert get('ui-roof')['review_sha256'] == sha and get('ui-roof')['review_revision'] == 28
    assert get('ui-roof')['review_updated_at'] == '2026-09-30T11:00:00.000Z' and get('ui-roof')['review_area_m2'] == 10.
    assert get('ui-roof')['model_signature_match'] is True and get('sig-off', 'map-2')['model_signature_match'] is False
    assert loaded['review_snapshot_sha256'] and loaded['reviews_read'] == 3
    # review_authority_blocker is reused, not re-implemented: a patched blocker excludes a plain UI object
    from scripts.ml import review_authority
    monkeypatch.setattr(review_authority, 'review_authority_blocker', lambda o: 'machine_label' if o['id'] == 'ui-roof' else None)
    patched = {r['object_id']: r for r in r5.load_decisions(scenes)['rows'] if r['scene_id'] == 'map-1'}
    assert patched['ui-roof']['stratum'] == 'machine_label' and patched['ui-roof']['decided'] is False
    assert patched['mach']['authority_blocker'] is None and patched['mach']['decided'] is True, 'no parallel machine check'


def test_classify_reuses_the_shared_carried_undecided_predicate(monkeypatch):
    """A carried object still needing adjustment is undecided whatever its other fields say (RULES human_precision.classes);
    the rule lives in review_authority.carried_undecided, shared with reference_registry.review_tier, not re-implemented here."""
    from scripts.ml import review_authority
    carried = {'scene_id': 'house-1', 'object_id': 'old', 'method': 'georeferenced-affine'}
    obj = _obj('carried', 'needs_adjustment', carried_from=carried, geometry_reviewed=True, roof_line_edits={'vertices': [], 'edges': []})
    assert r5.classify(obj, 2) == ('undecided', 'carried_undecided', 'carried_undecided', None)
    assert r5.classify(_obj('c', 'confirmed', carried_from=carried), 2) == ('roof', 'ui', None, None)
    assert r5.classify(_obj('r', 'rejected', carried_from=carried), 2) == ('not_roof', 'ui', None, None)
    assert r5.classify(_obj('m', 'needs_adjustment', carried_from=carried, provenance={'source': 'machine:test'}), 2)[:2] == \
        ('excluded', 'machine_label'), 'the authority blocker still comes first'
    # RULES: 'any object carrying carried_from'. A malformed non-dict value with needs_adjustment is still undecided, never roof_part.
    for odd in ('house-1', ['house-1'], True, 1):
        assert r5.classify(_obj('odd', 'needs_adjustment', carried_from=odd, review_issues=['instances']), 2) == \
            ('undecided', 'carried_undecided', 'carried_undecided', None), odd
    for empty in ({}, None, '', []):
        assert r5.classify(_obj('empty', 'needs_adjustment', carried_from=empty, review_issues=['instances']), 2) == \
            ('roof_part', 'ui', None, None), empty
    monkeypatch.setattr(review_authority, 'carried_undecided', lambda o: o['id'] == 'ui-roof')
    assert r5.classify(_obj('ui-roof', 'confirmed'), 2)[:2] == ('undecided', 'carried_undecided'), 'the shared predicate, no parallel check'
    assert r5.classify(obj, 2) == ('undecided', 'ui', None, None)


def test_fact_column_carried_and_classify_share_one_carry_test(tmp_path, monkeypatch):
    """load_decisions' 'carried' fact column and carried_undecided both read the carry through review_authority.carried, so no
    row can say carried=True with status needs_adjustment yet be classified by the plain status rule."""
    from scripts.ml import review_authority
    scenes = tmp_path / 'scenes'
    src = {'scene_id': 'house-1', 'object_id': 'old', 'method': 'georeferenced-affine'}
    review = [_obj('str', 'needs_adjustment', carried_from='house-1', review_issues=['instances']),
              _obj('dict', 'needs_adjustment', carried_from=src), _obj('dict-confirmed', 'confirmed', carried_from=src),
              _obj('empty', 'needs_adjustment', carried_from={}, review_issues=['instances']), _obj('plain', 'confirmed')]
    _scene(scenes, 'map-1', review=review, latest=[_live(o['id']) for o in review])
    rows = {r['object_id']: r for r in r5.load_decisions(scenes)['rows']}
    assert (rows['str']['carried'], rows['str']['cls'], rows['str']['stratum']) == (True, 'undecided', 'carried_undecided')
    assert (rows['dict']['carried'], rows['dict']['stratum']) == (True, 'carried_undecided')
    assert (rows['dict-confirmed']['carried'], rows['dict-confirmed']['cls']) == (True, 'roof'), "the reviewer's own decision on the new image"
    assert (rows['empty']['carried'], rows['empty']['cls']) == (False, 'roof_part')
    assert rows['plain']['carried'] is False
    assert not [r['object_id'] for r in rows.values()
                if r['carried'] and r['status'] == 'needs_adjustment' and r['stratum'] != 'carried_undecided']
    monkeypatch.setattr(review_authority, 'carried', lambda o: o['id'] == 'plain')
    patched = {r['object_id']: r for r in r5.load_decisions(scenes)['rows']}
    assert (patched['plain']['carried'], patched['str']['carried']) == (True, False), 'the fact column reads the shared helper'


def _import(pending=False, accepted_at='2026-10-01T02:48:34.389Z', accepted_by='local'):
    """fieldwork_import as task pull writes it (pending true) and as feedback.ts acceptPendingFeedback leaves it after a later
    ordinary local save (pending false, accepted_at, accepted_by)."""
    marker = {'pending': pending, 'origin': 'https://fieldwork.example', 'projectId': 'mooroolbark', 'itemId': 'addr-scene-map-1-mabc-r1',
              'revision': 2, 'cursor': '590'}
    if accepted_at is not None:
        marker['accepted_at'] = accepted_at
    if accepted_by is not None:
        marker['accepted_by'] = accepted_by
    return marker


def test_classify_accepted_fieldwork_import_is_the_reviewers_decision_on_an_agent_noted_object(tmp_path):
    """RULES human_precision.agent_fresh_decision, amendment 2026-10-01 (reviewer: "yes to defaults", card item 1): an agent-noted
    object that arrived through task pull as a pending draft is the reviewer's decision once fieldwork_import.pending is false
    with accepted_at set and accepted_by the local app principal 'local' (only feedback.ts acceptPendingFeedback writes them, on a
    later ordinary local save through src/app/api/scenes/route.ts -> scene-store.ts saveScene, principal.principal), provenance
    carries no machine:* source and the status is confirmed WITH geometry_reviewed true (roof) or rejected (not_roof). Pending
    stays fieldwork_pending; pending false without the acceptance fields, or accepted_by another principal (another assistant's
    'codex'), stays agent_authored_excluded; a machine source is still excluded by the blocker first; confirmed without
    geometry_reviewed (acceptPendingFeedback flips pending on confirmed only with it) is undecided, never roof; needs_adjustment or
    candidate after acceptance is undecided (never roof_part); the typed marker remains the only route for agent-noted objects
    that were never pending imports; carried copies are unaffected. Tightening amendment 2026-10-01 (second entry on
    agent_fresh_decision) restores the two PROPOSED conditions the 06:03:14Z text dropped."""
    codex = 'Assistant image review · Codex · 2026-10-01: Continuous pale corrugated roof plane. Reviewed the selected outline.'
    chat = {'human_decision': {'source': 'human:chat', 'fields': ['status']}}
    carried = {'scene_id': 'house-1', 'object_id': 'old', 'method': 'georeferenced-affine'}
    assert r5.classify(_obj('acc-roof', 'confirmed', notes=codex, fieldwork_import=_import(), geometry_reviewed=True), 10) == ('roof', 'ui', None, None)
    assert r5.classify(_obj('acc-not', 'rejected', notes=codex, fieldwork_import=_import()), 10) == ('not_roof', 'ui', None, None)
    assert r5.classify(_obj('acc-chat', 'confirmed', notes=codex, fieldwork_import=_import(), provenance=chat, geometry_reviewed=True), 10) == \
        ('roof', 'chat', None, None)
    # restored condition 1: accepted_by must be the local app principal; another assistant's principal is no acceptance
    for other in ('codex', 'Codex', 'fieldwork', ' local', 'local '):
        assert r5.classify(_obj('by-other', 'confirmed', notes=codex, fieldwork_import=_import(accepted_by=other), geometry_reviewed=True), 10) == \
            ('excluded', 'agent', 'agent_authored_excluded', None), other
        assert r5.classify(_obj('by-other', 'rejected', notes=codex, fieldwork_import=_import(accepted_by=other)), 10) == \
            ('excluded', 'agent', 'agent_authored_excluded', None), other
        assert r5.accepted_import(_obj('x', fieldwork_import=_import(accepted_by=other))) is False, other
    # restored condition 2: confirmed counts as roof only with geometry_reviewed true (acceptPendingFeedback accepts confirmed only
    # WITH geometry_reviewed); confirmed without it is undecided, never roof; rejected needs no geometry_reviewed, as in feedback.ts
    for unreviewed in (False, None, 'yes', 1):
        assert r5.classify(_obj('acc-unrev', 'confirmed', notes=codex, fieldwork_import=_import(), geometry_reviewed=unreviewed), 10) == \
            ('undecided', 'ui', None, None), unreviewed
    absent = _obj('acc-unrev', 'confirmed', notes=codex, fieldwork_import=_import())
    del absent['geometry_reviewed']                                   # the fixture defaults it to True on confirmed
    assert r5.classify(absent, 10) == ('undecided', 'ui', None, None), 'geometry_reviewed absent: undecided'
    assert r5.classify(_obj('acc-not', 'rejected', notes=codex, fieldwork_import=_import(), geometry_reviewed=False), 10) == ('not_roof', 'ui', None, None)
    assert r5.classify(_obj('plain-unrev', 'confirmed', fieldwork_import=_import(), geometry_reviewed=False), 10) == ('roof', 'ui', None, None), \
        'no agent note: the geometry_reviewed read belongs to the accepted-import route only (population_primary unchanged)'
    assert r5.classify(_obj('pending', 'confirmed', notes=codex, fieldwork_import=_import(pending=True)), 10) == \
        ('excluded', 'fieldwork_pending', 'fieldwork_pending', 'fieldwork_pending'), 'a pending draft is never a decision'
    for marker in (_import(accepted_at=None), _import(accepted_by=None), _import(accepted_at=''), _import(accepted_by=''),
                   _import(accepted_at=None, accepted_by=None)):
        assert r5.classify(_obj('half', 'confirmed', notes=codex, fieldwork_import=marker), 10) == \
            ('excluded', 'agent', 'agent_authored_excluded', None), marker
    assert r5.classify(_obj('machine', 'confirmed', notes=codex, fieldwork_import=_import(),
                            provenance={'source': 'machine:decision-policy-v2', 'human_confirmed': True}), 10) == \
        ('excluded', 'machine_label', 'machine_label_human_confirmed', 'machine_label'), 'the authority blocker still comes first'
    assert r5.classify(_obj('reopened', 'needs_adjustment', notes=codex, fieldwork_import=_import(), review_issues=['instances']), 10) == \
        ('undecided', 'ui', None, None), 'needs_adjustment after acceptance is undecided, never roof_part'
    assert r5.classify(_obj('cand', 'candidate', notes=codex, fieldwork_import=_import()), 10) == ('undecided', 'ui', None, None)
    assert r5.classify(_obj('sup', 'superseded', notes=codex, fieldwork_import=_import(), replacement_ids=['x']), 10) == \
        ('excluded', 'ui', 'superseded', None)
    assert r5.classify(_obj('conflict', 'confirmed', notes='[reviewer] not a roof ' + codex, fieldwork_import=_import()), 10) == \
        ('excluded', 'agent', 'agent_authored_excluded', None), 'typed words that disagree with the status still exclude'
    assert r5.classify(_obj('marked', 'confirmed', notes='[reviewer] roof ' + codex, fieldwork_import=_import()), 10) == ('roof', 'ui', None, None), \
        'a typed marker governs: the marker route reads neither accepted_by nor geometry_reviewed'
    assert r5.classify(_obj('never-import', 'confirmed', notes=codex), 10) == ('excluded', 'agent', 'agent_authored_excluded', None), \
        'an agent-noted object that was never a pending import still needs the typed marker'
    assert r5.classify(_obj('plain-acc', 'confirmed', fieldwork_import=_import()), 10) == ('roof', 'ui', None, None), 'no agent note: as before'
    assert r5.classify(_obj('copy', 'needs_adjustment', notes=codex, fieldwork_import=_import(), carried_from=carried), 10) == \
        ('undecided', 'carried_undecided', 'carried_undecided', None), 'a carried copy still needing adjustment is unaffected'
    assert r5.classify(_obj('copy-roof', 'confirmed', notes=codex, fieldwork_import=_import(), carried_from=carried, geometry_reviewed=True), 10) == \
        ('roof', 'ui', None, None)
    # the fact column names the route, and the live-decisions pass counts the accepted draft as the reviewer's (ui) decision
    scenes = tmp_path / 'scenes'
    review = [_obj('acc-roof', 'confirmed', notes=codex, fieldwork_import=_import(), geometry_reviewed=True),
              _obj('half', 'confirmed', notes=codex, fieldwork_import=_import(accepted_by=None), geometry_reviewed=True),
              _obj('by-codex', 'confirmed', notes=codex, fieldwork_import=_import(accepted_by='codex'), geometry_reviewed=True),
              _obj('acc-unrev', 'confirmed', notes=codex, fieldwork_import=_import(), geometry_reviewed=False),
              _obj('pending', 'confirmed', notes=codex, fieldwork_import=_import(pending=True)),
              _obj('reopened', 'needs_adjustment', notes=codex, fieldwork_import=_import(), review_issues=['instances'])]
    _scene(scenes, 'map-1', review=review, latest=[_live(o['id']) for o in review], revision=10)
    rows = {r['object_id']: r for r in r5.load_decisions(scenes)['rows']}
    assert (rows['acc-roof']['cls'], rows['acc-roof']['provenance_kind'], rows['acc-roof']['fresh'], rows['acc-roof']['decided'],
            rows['acc-roof']['agent_note']) == ('roof', 'ui', 'accepted_import', True, True)
    assert (rows['half']['stratum'], rows['half']['fresh'], rows['half']['decided']) == ('agent_authored_excluded', 'no_marker', False)
    assert (rows['by-codex']['stratum'], rows['by-codex']['fresh'], rows['by-codex']['decided']) == ('agent_authored_excluded', 'no_marker', False)
    assert (rows['acc-unrev']['cls'], rows['acc-unrev']['fresh'], rows['acc-unrev']['decided'], rows['acc-unrev']['geometry_reviewed']) == \
        ('undecided', 'accepted_import', False, False)
    assert (rows['pending']['stratum'], rows['pending']['fresh'], rows['pending']['decided']) == ('fieldwork_pending', 'no_marker', False)
    assert (rows['reopened']['cls'], rows['reopened']['fresh'], rows['reopened']['decided']) == ('undecided', 'accepted_import', False)
    # targets: a counted accepted draft is excluded as decided (never asked again, not listed as agent_noted); a pending or half-
    # accepted one keeps its exclusion; an accepted draft re-opened to needs_adjustment stays listed apart (RULES targets: no
    # Assistant note), never an ordinary target
    assert [r5._target_exclusion(rows[k]) for k in ('acc-roof', 'half', 'by-codex', 'acc-unrev', 'pending', 'reopened')] == \
        ['decided', 'agent_noted', 'agent_noted', 'agent_noted', 'fieldwork_pending', 'agent_noted']
    assert r5.accepted_import(_obj('x', fieldwork_import=_import())) is True
    assert r5.accepted_import(_obj('x', fieldwork_import=_import(pending=None))) is False, 'pending must be false, not merely absent'
    assert r5.accepted_import(_obj('x', fieldwork_import='accepted')) is False and r5.accepted_import(_obj('x')) is False


SCENES_LIVE = REPO / 'data/scenes'
SIX_ACCEPTED_3C76 = ('f18f05e57677b4d6', '995244b3e1216ea7', '1617b4981f07c25d', 'd9407498009cc185', '527bea1058365899', 'c8ee3ba173e4daeb')


def test_the_six_accepted_drafts_on_map_3c7647c8dbada27f_still_read_roof_under_the_tightened_route():
    """Read-only pin on the live scene: the reviewer's six accepts (02:48 to 03:17Z, accepted_by 'local', confirmed with
    geometry_reviewed true) are the only accepted imports there and each classifies roof / ui with fresh accepted_import, so the
    tightening moves none of them (applied-rules-2026-10-01.json six_accepted_drafts_map_3c7647c8dbada27f lists the same six)."""
    path = SCENES_LIVE / 'map-3c7647c8dbada27f' / 'review.json'
    if not path.exists():
        pytest.skip('live scene absent (data/ not checked out)')
    review = json.loads(path.read_bytes())
    objects = {o['id']: o for o in review['objects'] if isinstance(o, dict) and 'id' in o}
    accepted = sorted(oid for oid, o in objects.items() if r5.accepted_import(o))
    assert accepted == sorted(SIX_ACCEPTED_3C76), accepted
    for oid in SIX_ACCEPTED_3C76:
        o = objects[oid]
        assert (o['fieldwork_import']['accepted_by'], o['status'], o['geometry_reviewed']) == ('local', 'confirmed', True), oid
        assert r5.fresh_state(o, review['revision']) == 'accepted_import', oid
        assert r5.classify(o, review['revision']) == ('roof', 'ui', None, None), oid


# --- join --------------------------------------------------------------------------------------------------------------

def test_join_refuses_a_split_that_is_not_the_frozen_snapshot(tmp_path):
    w = _live_world(tmp_path)
    decisions = r5.load_decisions(w['scenes'])
    bad = {**w['context'], 'meta': {**w['context']['meta'], 'split_file_sha256': '0' * 64}}
    with pytest.raises(SystemExit, match='frozen snapshot'):
        r5.join(bad, w['split'], decisions)
    with pytest.raises(SystemExit):
        r5.join(w['context'], w['split'].read_bytes() + b' ', decisions)


def test_join_p_and_r_paths_populations_and_unjoined_reasons(tmp_path):
    w = _live_world(tmp_path)
    joined = r5.join(w['context'], w['split'], r5.load_decisions(w['scenes']))
    roof, part_r = _row(joined, 'p-roof'), _row(joined, 'r-prod')
    assert (roof['path'], roof['ann_id'], roof['snapshot_area_m2'], roof['primary']) == ('P', 11, 12., True)
    assert (part_r['path'], part_r['ref_keys'], part_r['primary'], part_r['human_drawn']) == ('R', ['2:24'], True, False)
    drawn, alias = _row(joined, 'r-drawn'), _row(joined, 'ref-x2')
    assert (drawn['path'], drawn['primary'], drawn['human_drawn']) == ('R', False, True), 'human-drawn refs are a recall stratum'
    assert (alias['path'], alias['ref_keys'], alias['human_drawn']) == ('R', ['2:22'], True), 'ref-<ref_id> is accepted'
    agent = _row(joined, 'agent-traced')
    assert (agent['agent_traced_reference'], agent['primary'], agent['human_drawn']) == (True, False, False)
    assert joined['refs']['2:23']['agent_traced_reference'] is True and joined['refs']['2:21']['agent_traced_reference'] is False
    unlisted = _row(joined, 'p-unlisted')
    assert unlisted['path'] is None and unlisted['unjoined_reasons'] == ['not_in_snapshot'], 'P needs the id in pseudo_ids'
    assert _row(joined, 'small-new')['unjoined_reasons'] == ['not_in_snapshot']
    assert _row(joined, 'tiny1')['unjoined_reasons'] == ['under_min_area']
    assert _row(joined, 'p-unjudged')['unjoined_reasons'] == ['not_judged_image']
    assert _row(joined, 'tiny3')['unjoined_reasons'] == ['not_judged_image', 'under_min_area']
    assert all(r['unjoined_reasons'] == [] for r in joined['rows'] if r['path']), 'joined rows carry no reason'
    for oid, path in (('p-gone', 'P'), ('r-gone', 'R')):
        gone = _row(joined, oid)
        assert (gone['path'], gone['live'], gone['primary'], gone['human_drawn'], gone['not_counted_reason']) == \
            (path, False, False, False, 'not_live'), 'a model-source decision no longer live is listed, never human-drawn'
    fresh = _row(joined, 'agent-fresh')
    assert (fresh['decided'], fresh['fresh'], fresh['agent_traced_reference'], fresh['human_drawn'], fresh['not_counted_reason']) == \
        (True, 'valid', True, False, 'agent_traced_geometry'), 'an outline the assistant drew never enters the human-drawn stratum'
    assert joined['refs']['2:26']['agent_traced_reference'] is True, 'agent-traced stays set whatever a later decision says'
    assert all(r['not_counted_reason'] is None for r in joined['rows'] if r['primary'] or r['human_drawn'])
    summary = r5.population(joined)
    assert summary['primary_decided'] == 4 and summary['by_class'] == {'roof': 2, 'not_roof': 2} and summary['scenes'] == 2
    assert summary['human_drawn_lt_25'] == 2 and summary['agent_authored_excluded'] == 1
    assert summary['unjoined_by_reason'] == {'not_in_snapshot': 2, 'under_min_area': 2, 'not_judged_image': 2, 'carry_unverified': 0,
                                             'carry_design_mismatch': 0, 'carry_snapshot_mismatch': 0, 'carry_unregistered': 0,
                                             'carry_method': 0}
    assert summary['joined_not_counted'] == 3 and summary['joined_not_counted_by_reason'] == \
        {'not_live': 2, 'agent_traced_geometry': 1, 'duplicate_decision_on_recapture': 0, 'ambiguous_carry': 0, 'ambiguous_takeover': 0,
         'other': 0}
    traced = r5.agent_traced_references(joined)
    assert (traced['references'], traced['agent_traced'], traced['keys']) == (6, 2, ['2:23', '2:26'])


def test_join_area_gate_reads_the_snapshot_in_both_directions(tmp_path):
    w = _live_world(tmp_path)
    decisions = r5.load_decisions(w['scenes'])
    joined = r5.join(w['context'], w['split'], decisions)
    over, under = _row(joined, 'p-snap251'), _row(joined, 'p-snap249')
    assert (over['review_area_m2'], over['snapshot_area_m2'], over['small'], over['primary']) == (24.9, 25.1, False, False)
    assert (under['review_area_m2'], under['snapshot_area_m2'], under['small'], under['primary']) == (25.1, 24.9, True, True)
    counted = r5.count(block='v2', context=w['context'], split_path=w['split'], scenes_root=w['scenes'], prior_set=None, rules_path=None)
    ids = {o['id'] for o in counted['already_counted']}
    assert 'p-snap249' in ids and 'p-snap251' not in ids
    targets = {o['id'] for s in counted['next_scenes'] for o in s['objects']}
    assert 'p-snap251' not in targets and 'p-open' in targets and 'p-open2' in targets


# --- detected ----------------------------------------------------------------------------------------------------------

def test_detected_is_strict_at_the_cut_needs_iou_half_and_refuses_uncollected_cuts():
    target_p = {'path': 'P', 'ann_id': 11, 'ref_keys': []}
    target_r = {'path': 'R', 'ann_id': None, 'ref_keys': ['2:24']}
    rows = [_pred('base', 1, .45, pseudo_overlaps=[{'id': 11, 'iou': .9}]), _pred('base', 1, .6, pseudo_overlaps=[{'id': 11, 'iou': .49}]),
            _pred('base', 2, .7, ref_overlaps=[{'ref': '2:24', 'iou': .5}]), _pred('base', 1, .95, pseudo_overlaps=[{'id': 99, 'iou': .9}])]
    assert r5.detected(rows, target_p, .45) == (False, .49, None), 'a row at exactly the cut is not above it'
    assert r5.detected(rows, target_p, .44) == (True, .9, .45)
    assert r5.detected(rows, target_r, .45) == (True, .5, .7), 'IoU 0.5 is a match (reference_eval.MATCH_IOU)'
    assert r5.detected(rows, target_r, .7) == (False, 0., None)
    with pytest.raises(SystemExit, match='collection threshold'):
        r5.detected(rows, target_p, .1, collected=.2)


# --- precision_block ---------------------------------------------------------------------------------------------------

def test_precision_block_below_the_rule_prints_na_and_the_per_object_facts(tmp_path):
    w = _live_world(tmp_path)
    joined = r5.join(w['context'], w['split'], r5.load_decisions(w['scenes']))
    preds = [_pred('base', 1, .4679, pseudo_overlaps=[{'id': 11, 'iou': .93}]), _pred('ctrl', 1, .7759, pseudo_overlaps=[{'id': 11, 'iou': .9}]),
             _pred('tuned', 1, .6478, pseudo_overlaps=[{'id': 11, 'iou': .83}]), _pred('tuned', 1, .3, pseudo_overlaps=[{'id': 11, 'iou': .95}]),
             _pred('tuned', 1, .7, area_m2=8., pseudo_overlaps=[{'id': 12, 'iou': .6}]),
             _pred('tuned', 2, .9, area_m2=20., ref_overlaps=[{'ref': '2:24', 'iou': .8}]), _pred('base', 1, .9, area_m2=5.),
             _pred('base', 1, .45, area_m2=8., pseudo_overlaps=[{'id': 12, 'iou': .97}])]           # a tie at the cut is not above it
    block = r5.precision_block(w['context'], joined['rows'], preds, {a: .45 for a in w['context']['arms']}, 't045')
    assert block['n_rule']['passes'] is False and block['shares'] is None
    assert block['read'] == 'n/a (N=4, roof=2, not_roof=2, scenes=2)'
    assert [p['pair'] for p in block['pairs']] == ['tuned:base', 'tuned:ctrl'], 'pairing arms come from context pairs (tuned:*)'
    assert all(set(p) == {'pair', 'second', 'first', 'read'} for p in block['pairs']), 'no pair statistic below the rule'
    facts = {f['object_id']: f for f in block['facts']}
    assert set(facts) == {'p-roof', 'p-not', 'p-snap249', 'r-prod'}
    base, tuned = facts['p-roof']['arms']['base'], facts['p-roof']['arms']['tuned']
    assert (base['max_score'], base['iou_at_max_score'], base['detected']) == (.4679, .93, True)
    assert base['survival'] == {'0.45': True, '0.50': False, '0.55': False, '0.60': False, '0.65': False, '0.70': False,
                                '0.75': False, '0.80': False}
    assert (tuned['max_score'], tuned['iou_at_max_score'], tuned['last_surviving_cut']) == (.6478, .83, .6)
    assert facts['r-prod']['arms']['tuned']['detected'] is True and facts['r-prod']['arms']['base']['max_score'] is None
    tie = facts['p-not']['arms']['base']
    assert (tie['detected'], tie['best_iou'], tie['max_score'], tie['last_surviving_cut']) == (False, 0., .45, None), 'strict > at the cut'
    assert block['object_counts']['tuned'] == {'roof': {'decided': 2, 'detected': 1}, 'not_roof': {'decided': 2, 'detected': 2}}
    assert block['prediction_level']['tuned'] == {'predictions_lt_25': 3, 'judged_roof': 1, 'judged_not_roof': 2, 'judged_part': 0,
                                                  'undecided': 0}
    assert block['prediction_level']['base']['undecided'] == 1 and block['prediction_level']['base']['judged_roof'] == 1
    json.dumps(block)
    matched = r5.precision_block(w['context'], joined['rows'], preds, {'base': .45, 'ctrl': .75, 'tuned': .65}, 'matched')
    assert matched['facts'][0]['arms']['tuned']['cut'] in (.45, .65, .75)
    assert {f['object_id']: f['arms']['tuned']['detected'] for f in matched['facts']}['p-roof'] is False, '0.6478 is below 0.65'
    with pytest.raises(SystemExit):
        r5.precision_block(w['context'], joined['rows'], preds, {'base': .45, 'tuned': .6}, 'matched')   # ctrl has no cut
    with pytest.raises(SystemExit, match='collection threshold'):
        r5.precision_block(w['context'], joined['rows'], preds, {'base': .1, 'ctrl': .45, 'tuned': .45}, 't045')


def _rule_world(n_scenes=7, per_scene=6):
    """Synthetic N-rule-passing population (42 decisions on 7 scenes, 21 per class) with numpy-typed inputs."""
    rows, preds, ann = [], [], 100
    images = {}
    for s in range(n_scenes):
        scene, iid = f'scene-{s}', s + 1
        ids = []
        for k in range(per_scene):
            ann += 1
            ids.append(ann)
            cls = 'roof' if (s + k) % 2 == 0 else 'not_roof'
            rows.append({'scene_id': scene, 'object_id': f'o{ann}', 'status': 'confirmed' if cls == 'roof' else 'rejected', 'cls': cls,
                         'provenance_kind': 'ui' if k else 'chat', 'primary': True, 'path': 'P', 'ann_id': np.int64(ann), 'ref_keys': [],
                         'image_id': iid, 'snapshot_area_m2': np.float64(10. + k), 'review_area_m2': 10. + k, 'decided': True})
            base_hit = cls == 'roof' and k % 3 != 0                         # base finds two of three roofs, no not-roof
            tuned_hit = cls == 'roof' or k == 1                             # tuned finds every roof and one not-roof per scene
            ctrl_hit = cls == 'roof' and k != 2
            for arm, hit, score in (('base', base_hit, .5), ('tuned', tuned_hit, .7), ('ctrl', ctrl_hit, .6)):
                if hit:
                    preds.append(_pred(arm, iid, np.float64(score), pseudo_overlaps=[{'id': np.int64(ann), 'iou': np.float64(.8)}]))
        images[str(iid)] = _ctx_image(scene, ids)
    context = {'arms': ['base', 'ctrl', 'tuned'], 'pairs': ['tuned:base', 'tuned:ctrl', 'ctrl:base'], 'images': images, 'refs': {},
               'meta': {'rows_collected_at_threshold': .2}}
    return context, rows, preds


def test_precision_block_rule_passing_runs_wilson_mcnemar_and_the_cluster_bootstrap(monkeypatch):
    context, rows, preds = _rule_world()
    calls = []
    real = scene_confidence.wilson

    def spy(successes, total):
        calls.append((type(successes), type(total)))
        return real(successes, total)
    monkeypatch.setattr(scene_confidence, 'wilson', spy)
    with pytest.raises(ValueError):
        real(np.int64(3), np.int64(10))                                     # why every call casts
    assert r5.wilson95(np.int64(3), np.int64(10)) == real(3, 10)
    block = r5.precision_block(context, rows, preds, {a: .45 for a in context['arms']}, 't045', draws=500)
    n = block['n_rule']
    assert n['passes'] is True and (n['N'], n['roof'], n['not_roof'], n['scenes']) == (42, 21, 21, 7)
    assert block['read'].startswith('rule met (N=42')
    assert calls and all(t == (int, int) for t in calls), 'wilson only ever sees python ints'
    tuned = block['shares']['tuned']
    assert tuned['confirmed_detected_share']['successes'] == 21 and tuned['confirmed_detected_share']['total'] == 21
    roof_hit_by_tuned_not = sum(1 for r in rows if r['cls'] == 'not_roof' and r['object_id'] in
                                {f"o{int(p['pseudo_overlaps'][0]['id'])}" for p in preds if p['arm'] == 'tuned'})
    assert tuned['rejected_detected_share']['successes'] == roof_hit_by_tuned_not
    assert tuned['confirmed_detected_share']['wilson95'] == real(21, 21)
    k, t = tuned['detected_roof_share']['successes'], tuned['detected_roof_share']['total']
    assert (k, t) == (21, 21 + roof_hit_by_tuned_not) and tuned['detected_roof_share']['wilson95'] == real(k, t)
    pair = next(p for p in block['pairs'] if p['pair'] == 'tuned:base')
    base_roof = sum(1 for r in rows if r['cls'] == 'roof' and int(r['ann_id']) in
                    {int(p['pseudo_overlaps'][0]['id']) for p in preds if p['arm'] == 'base'})
    assert pair['discordant']['roof'] == {'second_only': 21 - base_roof, 'first_only': 0}
    assert pair['discordant']['not_roof'] == {'second_only': roof_hit_by_tuned_not, 'first_only': 0}
    assert pair['mcnemar_p']['roof'] == r5.mcnemar_p(21 - base_roof, 0) and 0 < pair['mcnemar_p']['roof'] < .05
    assert r5.mcnemar_p(0, 0) == 1. and r5.mcnemar_p(3, 3) == 1. and abs(r5.mcnemar_p(5, 0) - .0625) < 1e-9
    boot = pair['bootstrap']['confirmed_detected_share']
    assert boot['draws'] == 500 and boot['draws_defined'] == 500 and len(boot['ci95']) == 2 and boot['ci95'][0] <= boot['ci95'][1]
    assert abs(boot['observed'] - (1 - base_roof / 21)) < 1e-3
    again = r5.precision_block(context, rows, preds, {a: .45 for a in context['arms']}, 't045', draws=500)
    assert again['pairs'] == block['pairs'], 'seeded bootstrap'
    assert pair['keeps_human_precision'] is False, 'tuned detects more not-roof than base + 1'
    ctrl_pair = next(p for p in block['pairs'] if p['pair'] == 'tuned:ctrl')
    assert ctrl_pair['keeps_human_precision'] is False and 'keeps_human_precision' in ctrl_pair
    matched = r5.precision_block(context, rows, preds, {'base': .45, 'ctrl': .55, 'tuned': .6}, 'matched', draws=200)
    assert all(p['keeps_human_precision'] is None for p in matched['pairs']), 'the flag is read on the 0.45 view only'
    json.dumps(block)


def test_ui_only_sensitivity_read_drops_chat_rows_and_reads_shares_only_when_the_rule_passes():
    context, rows, preds = _rule_world()
    block = r5.precision_block(context, rows, preds, {a: .45 for a in context['arms']}, 't045', draws=200)
    ui = r5.ui_only(block['facts'])
    assert (ui['N'], ui['roof'], ui['not_roof'], ui['scenes'], ui['chat_removed'], ui['passes']) == (35, 17, 18, 7, 7, True)
    assert ui['read'] == 'rule met (N=35, roof=17, not_roof=18, scenes=7)'
    tuned = ui['shares']['tuned']
    assert (tuned['confirmed_detected_share']['successes'], tuned['confirmed_detected_share']['total']) == (17, 17)
    assert (tuned['rejected_detected_share']['successes'], tuned['rejected_detected_share']['total']) == (4, 18)
    assert tuned['rejected_detected_share']['wilson95'] == scene_confidence.wilson(4, 18)
    few = r5.ui_only(block['facts'][:10])
    assert few['passes'] is False and few['shares'] is None and few['read'] == 'n/a (N=8, roof=4, not_roof=4, scenes=2)'
    json.dumps(ui)


# --- matched_cuts ------------------------------------------------------------------------------------------------------

def test_matched_cuts_read_top_level_arm_thresholds_and_raise_when_absent(tmp_path):
    run = tmp_path / 'r4-combo-seed2'
    (run / 'reference-eval-v2-matched').mkdir(parents=True)
    flat = {'meta': None, 'threshold': .45, 'view': 'matched_density', 'arm_thresholds_source': 'x/calibration.json (auto-discovered)',
            'arm_thresholds': {'base': .45, 'ctrl': .75, 'twin': .6, 'tuned': .55}}
    json.dump(flat, open(run / 'reference-eval-v2-matched' / 'summary.json', 'w'))
    cuts, source = r5.matched_cuts(run, 'v2', arms=['base', 'ctrl', 'twin', 'tuned'])
    assert cuts == {'base': .45, 'ctrl': .75, 'twin': .6, 'tuned': .55} and 'calibration.json' in source
    with pytest.raises(SystemExit, match='combo'):
        r5.matched_cuts(run, 'v2', arms=['base', 'combo', 'tuned'])
    with pytest.raises(SystemExit):
        r5.matched_cuts(run, 'v1')
    json.dump({'meta': {'arm_thresholds': flat['arm_thresholds']}}, open(run / 'reference-eval-v2-matched' / 'summary.json', 'w'))
    with pytest.raises(SystemExit, match='top-level arm_thresholds'):
        r5.matched_cuts(run, 'v2')


# --- rejudge -----------------------------------------------------------------------------------------------------------

def _rules_file(tmp_path, **extra):
    path = tmp_path / 'RULES.json'
    rules = {'version': 'sam3-round5-rules-v1', 'written_before': '2026-09-30T12:00:00Z', 'amendments': [],
             'human_precision': {'version': 'sam3-mac-human-precision-v1',
                                 'n_rule': {'primary_decided_min': 30, 'per_class_min': 8, 'scenes_min': 6},
                                 'grid': [.45, .5, .55, .6, .65, .7, .75, .8], 'bootstrap': {'draws': 2000, 'seed': 20260928},
                                 'agent_text_regex': r5.AGENT_TEXT.pattern, 'fresh_marker_regex': r5.FRESH_MARK.pattern}}
    rules.update(extra)
    path.write_text(json.dumps(rules))
    return path


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write_run(run, block, context, preds, cuts=None):
    folder = run / f'reference-eval-{block}'
    folder.mkdir(parents=True, exist_ok=True)
    json.dump(context, open(folder / 'context.json', 'w'))
    (folder / 'predictions.jsonl').write_text(''.join(json.dumps(p) + '\n' for p in preds))
    (folder / 'timings.jsonl').write_text('')
    json.dump({'version': 'sam3-mac-reference-eval-v2', 'threshold': .2}, open(folder / 'summary.json', 'w'))
    (run / f'reference-eval-{block}-t045').mkdir(exist_ok=True)
    json.dump({'threshold': .45}, open(run / f'reference-eval-{block}-t045' / 'summary.json', 'w'))
    if cuts is not None:
        (run / f'reference-eval-{block}-matched').mkdir(exist_ok=True)
        json.dump({'threshold': .45, 'arm_thresholds': cuts, 'arm_thresholds_source': 'calibration.json'},
                  open(run / f'reference-eval-{block}-matched' / 'summary.json', 'w'))


def _sha_tree(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*'))
            if p.is_file() and p.name in ('summary.json', 'predictions.jsonl', 'context.json')}


def _rejudge_world(tmp_path):
    """One round-3 run dir (blocks v2, v1, v2val) beside the non-run entries a real round dir holds, and one sweep dir."""
    w = _live_world(tmp_path)
    val_path, val_sha = _split(tmp_path / 'dataset-v2', 'val', [_image(7, 's7')], [_pseudo(71, 7, 's7', 'v-open', 9.)])
    v1_path, v1_sha = _split(tmp_path / 'dataset-v1', 'test', [_image(1, 's1')], [_pseudo(11, 1, 's1', 'p-roof', 12.)])
    sam3 = tmp_path / 'sam3-mac'
    root3 = sam3 / 'round3'
    run = root3 / 'r3-a-seed1'
    preds = [_pred('tuned', 1, .6478, pseudo_overlaps=[{'id': 11, 'iou': .83}]), _pred('base', 1, .4679, pseudo_overlaps=[{'id': 11, 'iou': .93}])]
    _write_run(run, 'v2', w['context'], preds, cuts={'base': .45, 'ctrl': .8, 'tuned': .6})
    _write_run(run, 'v1', _context(tmp_path / 'dataset-v1', 'test', v1_sha, {'1': _ctx_image('s1', [11])}, {}), preds,
               cuts={'base': .45, 'ctrl': .8, 'tuned': .6})
    _write_run(run, 'v2val', _context(tmp_path / 'dataset-v2', 'val', val_sha, {'7': _ctx_image('s7', [71])}, {}), [])
    for name in ('r3-a-seed1.eval.log', 'r3-a-seed1.train.log', 'r3-a-seed1.train.pid', 'r3-a-seed1.train.wall', 'REPORT.md'):
        (root3 / name).write_text('x')
    for name in ('coco-r3-a-v1', 'exports', 'smoke'):
        (root3 / name / 'reference-eval-v1').mkdir(parents=True)
    (sam3 / 'sweep' / 's1-raw-seed1').mkdir(parents=True)
    (sam3 / 'sweep' / 's1-raw-seed1.eval.log').write_text('x')
    return w, sam3, root3


def test_rejudge_reads_only_run_dirs_writes_block_files_and_ledger_rows_and_leaves_runs_unchanged(tmp_path):
    w, sam3, root3 = _rejudge_world(tmp_path)
    before = _sha_tree(sam3)
    out, rules_path = tmp_path / 'round5', _rules_file(tmp_path)
    result = r5.rejudge([root3], rules_path, out, scenes_root=w['scenes'], repo=tmp_path)
    assert _sha_tree(sam3) == before, 'run dirs are read, never written'
    assert result['runs'] == ['round3/r3-a-seed1'] and result['not_rejudgeable_from_rows'] == ['sweep/s1-raw-seed1']
    files = sorted(p.relative_to(out).as_posix() for p in (out / 'human-precision').rglob('*.json'))
    assert files == [f'human-precision/r3-a-seed1/{b}.json' for b in ('v1-matched', 'v1-t045', 'v2-matched', 'v2-t045', 'v2val-t045')]
    rows = [json.loads(line) for line in open(out / 'human-precision.jsonl')]
    assert len(rows) == 5 and {(r['block'], r['view']) for r in rows} == {('v2', 't045'), ('v2', 'matched'), ('v1', 't045'),
                                                                        ('v1', 'matched'), ('v2val', 't045')}
    need = {'decisions_total', 'primary_decided', 'primary_by_class', 'primary_by_provenance', 'agent_authored_excluded', 'scenes', 'n_rule',
            'pairs', 'rules_sha256', 'split_file_sha256', 'judge_version_sha256', 'code_sha256', 'computed_at', 'rescope_note'}
    for row in rows:
        assert need <= set(row), need - set(row)
        assert row['rescope_note'].startswith('reference_eval.py') and '1 run dirs (3 unique checkpoints)' in row['rescope_note']
        assert '1 sweep checkpoints not re-judged' in row['rescope_note']
        assert row['rules_sha256'] == _sha(rules_path) and row['rules_amendments'] == [] and row['batch_id'] == result['batch_id']
        assert row['pairs'] == ['tuned:base', 'tuned:ctrl']
    v2 = next(r for r in rows if (r['block'], r['view']) == ('v2', 't045'))
    assert v2['primary_decided'] == 4 and v2['primary_by_class'] == {'roof': 2, 'not_roof': 2} and v2['scenes'] == 2
    assert v2['n_rule']['passes'] is False and v2['read'] == 'n/a (N=4, roof=2, not_roof=2, scenes=2)'
    assert v2['split_file_sha256'] == w['context']['meta']['split_file_sha256']
    assert v2['ui_only_read'] == 'n/a (N=4, roof=2, not_roof=2, scenes=2)' and v2['human_drawn_lt_25'] == 2
    assert v2['joined_not_counted_by_reason'] == {'not_live': 2, 'agent_traced_geometry': 1, 'duplicate_decision_on_recapture': 0,
                                                  'ambiguous_carry': 0, 'ambiguous_takeover': 0, 'other': 0}
    matched = next(r for r in rows if (r['block'], r['view']) == ('v2', 'matched'))
    assert matched['cuts'] == {'base': .45, 'ctrl': .8, 'tuned': .6} and matched['cuts_source'] == 'calibration.json'
    block = json.load(open(out / 'human-precision/r3-a-seed1/v2-t045.json'))
    assert block['facts'] and BANNED not in json.dumps(block).lower()
    assert (block['ui_only']['read'], block['ui_only']['chat_removed'], block['ui_only']['shares']) == \
        ('n/a (N=4, roof=2, not_roof=2, scenes=2)', 0, None)
    assert {(u['object_id'], u['reason']) for u in block['joined_not_counted']} == \
        {('p-gone', 'not_live'), ('r-gone', 'not_live'), ('agent-fresh', 'agent_traced_geometry')}
    batch = out / 'batches' / result['batch_id'].replace(':', '-')
    assert (batch / 'RULES.json').read_bytes() == rules_path.read_bytes(), 'each batch keeps the RULES bytes it read'
    assert (batch / 'r5_precision.py').read_bytes() == Path(r5.__file__).read_bytes() and _sha(batch / 'r5_precision.py') == \
        rows[0]['code_sha256'] == result['code_sha256'], 'each batch keeps the module bytes it ran'
    for row in rows:
        copy = batch / 'human-precision' / Path(row['block_file']).parent.name / Path(row['block_file']).name
        assert _sha(copy) == row['block_file_sha256'] == _sha(tmp_path / row['block_file'])
    report = r5.report(out / 'human-precision.jsonl', out / 'HUMAN-PRECISION.md', repo=tmp_path)
    text = (out / 'HUMAN-PRECISION.md').read_text()
    assert text == report and 'agreement' in text and BANNED not in text.lower()
    assert 'n/a (N=4, roof=2, not_roof=2, scenes=2)' in text and 'rescope' in text.lower() and 'not_rejudgeable_from_rows' in text
    assert '3 unique checkpoints' in text and 'runs/tuned/trainable-final.pt' in text and 'sweep/s1-raw-seed1' in text
    assert 'agent-authored' in text and '| p-roof |' in text and 'denominator' in text.lower()
    assert 'confirmed/rejected building decisions after exclusions (all scenes)' in text and 'excluded before the count' in text
    assert 'UI-only sensitivity, block v2: n/a (N=4, roof=2, not_roof=2, scenes=2); chat rows removed: 0' in text
    assert '| prov |' in text and '| p-roof | roof | ui |' in text, 'provenance shown per row'
    assert 'joined, not counted' in text and 'p-gone' in text and 'agent_traced_geometry' in text
    assert '## Ledger batches' in text and result['batch_id'] in text
    assert (batch / 'HUMAN-PRECISION.md').read_text() == text, 'the report of a batch is kept beside its block files'


def test_rejudge_second_batch_appends_keeps_the_first_verifiable_and_refuses_a_batch_id_collision(tmp_path, monkeypatch):
    w, sam3, root3 = _rejudge_world(tmp_path)
    out, rules_path = tmp_path / 'round5', _rules_file(tmp_path)
    monkeypatch.setattr(r5, 'now_iso', lambda: '2026-09-30T13:00:00Z')
    r5.rejudge([root3], rules_path, out, scenes_root=w['scenes'], repo=tmp_path)
    first_report = r5.report(out / 'human-precision.jsonl', out / 'HUMAN-PRECISION.md', repo=tmp_path)
    kept = out / 'batches' / '2026-09-30T13-00-00Z' / 'HUMAN-PRECISION.md'
    assert kept.read_text() == first_report
    kept.write_text(first_report + 'kept by an earlier code revision\n')           # a report already kept for this batch
    assert r5.report(out / 'human-precision.jsonl', out / 'HUMAN-PRECISION.md', repo=tmp_path) == first_report
    assert kept.read_text() == first_report + 'kept by an earlier code revision\n', "a batch's report is written once"
    kept.write_text(first_report)
    first_rules = rules_path.read_bytes()
    latest_before = _sha(out / 'human-precision/r3-a-seed1/v2-t045.json')
    with pytest.raises(SystemExit, match='batch id'):
        r5.rejudge([root3], rules_path, out, scenes_root=w['scenes'], repo=tmp_path)
    assert len(open(out / 'human-precision.jsonl').readlines()) == 5, 'a refused batch appends nothing'
    assert _sha(out / 'human-precision/r3-a-seed1/v2-t045.json') == latest_before, 'and overwrites nothing'
    amendment = {'at': '2026-09-30T13:10:00Z', 'field': 'x.y', 'from': 1, 'to': 2, 'reason': 'test amendment'}
    amended = _rules_file(tmp_path, x={'y': 2}, amendments=[amendment])
    monkeypatch.setattr(r5, 'now_iso', lambda: '2026-09-30T13:20:00Z')
    r5.rejudge([root3], amended, out, scenes_root=w['scenes'], repo=tmp_path)
    rows = [json.loads(line) for line in open(out / 'human-precision.jsonl')]
    assert [r['batch_id'] for r in rows] == ['2026-09-30T13:00:00Z'] * 5 + ['2026-09-30T13:20:00Z'] * 5
    assert {r['rules_sha256'] for r in rows[5:]} == {_sha(amended)} and rows[5]['rules_amendments'] == [{'at': amendment['at'],
                                                                                                         'field': 'x.y'}]
    first = out / 'batches' / '2026-09-30T13-00-00Z'
    assert (first / 'RULES.json').read_bytes() == first_rules and _sha(first / 'RULES.json') == rows[0]['rules_sha256']
    for row in rows[:5]:
        copy = first / 'human-precision' / Path(row['block_file']).parent.name / Path(row['block_file']).name
        assert _sha(copy) == row['block_file_sha256'], 'batch-1 block files stay verifiable after batch 2 rewrote the latest copies'
    text = r5.report(out / 'human-precision.jsonl', out / 'HUMAN-PRECISION.md', repo=tmp_path)
    assert 'batch `2026-09-30T13:20:00Z`' in text and '## Ledger batches' in text and '2026-09-30T13:00:00Z' in text and 'x.y' in text
    assert text.count('| yes (sha256 verified) |') == 2, 'both batches keep the module bytes they ran'
    (first / 'r5_precision.py').unlink()
    assert '| not kept |' in r5.report(out / 'human-precision.jsonl', tmp_path / 'again.md', repo=tmp_path)
    assert (first / 'HUMAN-PRECISION.md').read_text() == first_report, 'a later report never rewrites an earlier batch report'


def test_load_rules_validates_amendments_and_the_registered_patterns(tmp_path):
    rules, sha, data = r5.load_rules(_rules_file(tmp_path))
    assert sha == hashlib.sha256(data).hexdigest() and rules['amendments'] == []
    good = {'at': '2026-09-30T13:10:00Z', 'field': 'x.y', 'from': 1, 'to': 2, 'reason': 'test'}
    assert r5.load_rules(_rules_file(tmp_path, x={'y': 2}, amendments=[good]))[0]['x'] == {'y': 2}
    for bad, match in (({**good, 'reason': ''}, 'reason'), ({k: v for k, v in good.items() if k != 'at'}, "'at'"),
                       ({**good, 'at': '2026-09-30T11:00:00Z'}, 'written_before'), ({**good, 'to': 3}, 'x.y'),
                       ({**good, 'field': 'x.z'}, 'x.z')):
        with pytest.raises(SystemExit, match=match):
            r5.load_rules(_rules_file(tmp_path, x={'y': 2}, amendments=[bad]))
    hp = json.loads(_rules_file(tmp_path).read_text())['human_precision']
    with pytest.raises(SystemExit, match='pattern'):
        r5.load_rules(_rules_file(tmp_path, human_precision={**hp, 'fresh_marker_regex': '^x'}))


# --- count and guide ---------------------------------------------------------------------------------------------------

def _guide_world(tmp_path):
    scenes, dataset = tmp_path / 'scenes', tmp_path / 'dataset-v2'
    images, anns, ctx_images, ann = [], [], {}, 100
    for s in range(8):
        scene, iid = f'map-{s}', s + 1
        images.append(_image(iid, scene))
        ids, live, review = [], [], []
        for k in range(5):
            ann += 1
            oid = f'{scene}-o{k}'
            anns.append(_pseudo(ann, iid, scene, oid, 5. + k + s / 10))
            ids.append(ann)
            live.append(_live(oid))
        if s == 0:
            review = [_obj('map-0-o4', 'confirmed', area=9.), _obj('map-0-o3', 'superseded', area=8.)]   # decided and superseded
        if s == 1:
            review = [_obj('map-1-o4', 'needs_adjustment', review_issues=['instances'], area=9.1),
                      _obj('map-1-o3', 'needs_adjustment', area=8.1,
                           notes='[reviewer 2026-09-30] same sub-division block',
                           provenance={'human_decision': {'source': 'human:chat', 'fields': ['notes', 'status']}})]
        if s == 2:
            live = live[:4]                                                        # map-2-o4 no longer live
        ctx_images[str(iid)] = _ctx_image(scene, ids)
        _scene(scenes, scene, latest=live, review=review or None)
    images.append(_image(20, 'map-agent'))
    anns += [_ref_ann(201, 20, 'map-agent', 'f104', 23.32), _ref_ann(202, 20, 'map-agent', 'review-east', 11.95),
             _ref_ann(203, 20, 'map-agent', 'big-agent', 170.), _pseudo(204, 20, 'map-agent', 'fabric', 13.4)]
    ctx_images['20'] = _ctx_image('map-agent', [204], references=3)
    _scene(scenes, 'map-agent', latest=[_live('f104'), _live('review-east', source='reviewer'), _live('big-agent'), _live('fabric')],
           review=[_obj('f104', 'confirmed', area=23.32, notes=AGENT_TEXT), _obj('review-east', 'confirmed', source='reviewer', area=11.95,
                                                                                 notes=AGENT_TEXT),
                   _obj('big-agent', 'confirmed', area=170., notes=AGENT_TEXT),
                   _obj('fabric', 'needs_adjustment', kind='other', area=13.4, notes='Assistant image review 2026-09-23: fabric cover')])
    images.append(_image(30, 'house-unjudged'))
    _scene(scenes, 'house-unjudged', latest=[_live('u1'), _live('u2')],
           review=[_obj('u2', 'rejected', area=1.44, provenance={'human_decision': {'source': 'human:chat', 'fields': ['status']}})])
    split_path, sha = _split(dataset, 'test', images, anns)
    refs = {'20:201': _ctx_ref(20, 'map-agent', 201, 'f104', 23.32), '20:202': _ctx_ref(20, 'map-agent', 202, 'review-east', 11.95),
            '20:203': _ctx_ref(20, 'map-agent', 203, 'big-agent', 170.)}
    context = _context(dataset, 'test', sha, ctx_images, refs)
    prior = tmp_path / 'REVIEW-SET-prior.json'
    json.dump({'date': '2026-09-29', 'groups': {'small_roof_detections_test_tiles': [['house-unjudged', 'u1', 3.1, 'Building'],
                                                                                    ['house-unjudged', 'u2', 1.4, 'Building'],
                                                                                    ['map-0', 'map-0-o1', 6.0, 'Building']]}},
              open(prior, 'w'))
    return {'scenes': scenes, 'split': split_path, 'context': context, 'prior': prior}


def test_count_json_contract_and_live_targets(tmp_path):
    g = _guide_world(tmp_path)
    c = r5.count(block='v2', context=g['context'], split_path=g['split'], scenes_root=g['scenes'], prior_set=g['prior'], rules_path=None)
    need = {'version', 'block', 'primary_n', 'primary_by_class', 'primary_by_provenance', 'agent_authored_excluded', 'human_drawn_lt_25', 'scenes',
            'n_rule', 'targets_total', 'targets_remaining', 'next_scenes', 'already_counted', 'agent_authored_required', 'deferred_unjudged',
            'review_snapshot_sha256', 'computed_at'}
    assert need <= set(c), need - set(c)
    assert c['version'] == 'sam3-mac-human-precision-v1' and c['block'] == 'v2'
    assert c['primary_n'] == 1 and c['primary_by_class'] == {'roof': 1, 'not_roof': 0} and c['primary_by_provenance'] == {'ui': 1, 'chat': 0}
    assert c['scenes'] == 1 and c['n_rule'] == {'primary_decided_min': 30, 'per_class_min': 8, 'scenes_min': 6, 'passes': False}
    assert c['agent_authored_excluded'] == 2 and c['human_drawn_lt_25'] == 0
    targets = [(s['scene_id'], o['id']) for s in c['next_scenes'] for o in s['objects']]
    ids = {oid for _, oid in targets}
    assert 'map-0-o4' not in ids and 'map-0-o3' not in ids and 'map-1-o4' not in ids and 'map-2-o4' not in ids
    assert 'fabric' not in ids and 'f104' not in ids, 'agent-noted objects are listed apart, never as ordinary targets'
    assert 'map-1-o3' in ids and c['targets_remaining'] == len(targets) == 8 * 5 - 4
    assert c['targets_total'] == 8 * 5 - 1 + 1, 'live small snapshot objects on judged tiles (map-2-o4 is gone; fabric counts)'
    note = next(o for s in c['next_scenes'] for o in s['objects'] if o['id'] == 'map-1-o3')['note']
    assert note.startswith('[reviewer 2026-09-30]')
    url = next(o for s in c['next_scenes'] for o in s['objects'] if o['id'] == 'map-3-o0')['url']
    assert url == 'http://localhost:3008/scenes?scene=map-3&status=all&layer=edited&object=map-3-o0'
    assert [o['id'] for o in c['agent_authored_required']] == ['f104', 'review-east'], 'the small assistant-decided trio (here two)'
    assert [(o['fresh_state'], o['enters']) for o in c['agent_authored_required']] == \
        [('no_marker', 'primary'), ('no_marker', 'listed_assistant_drawn_outline')], 'where a fresh decision would count'
    assert c['joined_not_counted'] == [] and c['human_drawn_rows'] == []
    assert [o['id'] for o in c['agent_authored_other']] == ['fabric']
    assert {(o['scene_id'], o['id']) for o in c['deferred_unjudged']} == {('house-unjudged', 'u1'), ('house-unjudged', 'u2')}
    assert [(o['scene_id'], o['id'], o['unjoined_reasons']) for o in c['decided_not_judged']] == \
        [('house-unjudged', 'u2', ['not_judged_image', 'under_min_area'])]
    assert [o['id'] for o in c['already_counted']] == ['map-0-o4'] and c['primary_rows'] == [['map-0', 'map-0-o4', 'confirmed', 9.0,
                                                                                                'sam3-text-onnx', 'P']]
    json.dumps(c)


def test_guide_lists_only_live_undecided_targets_interleaved_across_six_scenes(tmp_path):
    g = _guide_world(tmp_path)
    md, js = tmp_path / 'GUIDE.md', tmp_path / 'SET.json'
    out = r5.guide(block='v2', out_md=md, out_set=js, context=g['context'], split_path=g['split'], scenes_root=g['scenes'],
                   prior_set=g['prior'], rules_path=None)
    text, data = md.read_text(), json.load(open(js))
    order = [(t['scene_id'], t['id']) for t in data['groups']['targets']]
    assert ('map-0', 'map-0-o4') not in order, 'a decided object is never asked again'
    assert len({s for s, _ in order[:24]}) >= 6, 'the first 24 targets cover six scenes'
    first = [oid for s, oid in order if s == 'map-3']
    assert first == ['map-3-o4', 'map-3-o3', 'map-3-o2', 'map-3-o1', 'map-3-o0'], 'area-descending within a scene'
    firsts = [s for s, _ in order[:4]]
    assert len(set(firsts)) == 1 and order[4][0] != order[0][0], 'four per scene, then the next scene'
    for heading in ('## Targets', '## Already counted', '## Required fresh decisions (agent-authored review)', '## Deferred: un-judged tiles'):
        assert heading in text, heading
    for t in data['groups']['targets']:
        line = next(line for line in text.splitlines() if t['url'] in line)
        assert t['scene_id'] in line and f"{t['area_m2']:.1f} m²" in line
    assert 'f104' in text and 'review-east' in text and 'u1' in text
    assert '`[reviewer] roof`' in text and '`[reviewer] not a roof`' in text, 'the guide names the fresh-decision marker'
    assert BANNED not in text.lower() and 'label' not in text.lower().replace('labelled', '')
    assert out['targets_remaining'] == len(order) and data['counts']['primary_n'] == 1


# --- RULES -------------------------------------------------------------------------------------------------------------

def _serialise_rules(rules):
    """The byte form every dated RULES amendment script writes: indent 1, ensure_ascii False, one trailing newline."""
    return json.dumps(rules, indent=1, ensure_ascii=False).encode() + b'\n'


def _get_path(data, dotted):
    for part in str(dotted).split('.'):
        data = data[int(part)] if isinstance(data, list) else data[part]
    return data


def _set_path(data, dotted, value):
    """Set the field a dotted amendment path names (list steps are indices); value None removes it (a from of null: the field was absent)."""
    parts = str(dotted).split('.')
    node = _get_path(data, '.'.join(parts[:-1])) if len(parts) > 1 else data
    last = parts[-1]
    if isinstance(node, list):
        if value is None:
            del node[int(last)]
        elif int(last) == len(node):
            node.append(value)
        else:
            node[int(last)] = value
    elif value is None:
        node.pop(last, None)
    else:
        node[last] = value


def _instants(amendments):
    """Consecutive index ranges sharing one `at`, oldest first: [(at, first index, end index)]."""
    out, start = [], 0
    for i in range(1, len(amendments) + 1):
        if i == len(amendments) or amendments[i]['at'] != amendments[start]['at']:
            out.append((amendments[start]['at'], start, i))
            start = i
    return out


def _rules_before_each_instant(rules):
    """Walk the amendments backwards: revert each instant's named fields to their `from` values and drop its entries, serialise, and yield
    (at, previous_sha256 recorded on the instant, sha256 of the reconstructed bytes), newest first. A match means the recorded sha is the
    file's sha before that instant and that nothing beyond the named fields changed in it. Before RULES5_AMENDMENTS_KEY_MOVED_AT the
    amendments key followed copied_from (the linear-merge write moved it to the end of the file)."""
    state, amendments = json.loads(_serialise_rules(rules)), rules['amendments']
    for at, first, end in reversed(_instants(amendments)):
        for entry in reversed(amendments[first:end]):
            _set_path(state, entry['field'], entry['from'])
        state['amendments'] = amendments[:first]
        if at <= RULES5_AMENDMENTS_KEY_MOVED_AT:
            items = [(k, v) for k, v in state.items() if k != 'amendments']
            items.insert([k for k, _ in items].index('copied_from') + 1, ('amendments', state['amendments']))
            state = dict(items)
        yield at, amendments[first].get('previous_sha256'), hashlib.sha256(_serialise_rules(state)).hexdigest()


def _banned(value):
    return json.dumps(value, ensure_ascii=False).lower().count(BANNED)


def _mentions(value, text):
    """True when text is a substring of value (a string) or of any string nested in it (lists, dicts): the containment behind added_text /
    removed_text, whose field may be a text, a list of items or a dict added whole."""
    if isinstance(value, str):
        return text in value
    if isinstance(value, dict):
        return any(_mentions(v, text) for v in value.values())
    return isinstance(value, list) and any(_mentions(v, text) for v in value)


def _by_field(amendments):
    out = {}
    for entry in amendments:
        out.setdefault(entry['field'], []).append(entry)
    return out


# the sentences the live texts must keep: the design-sha chain rule (2026-10-01 stream C) and the takeover hop (2026-10-02 stream A)
CHAIN_RULE_PHRASES = {'human_precision.join_order': ('thresholds_changed is exactly false', 'design_sha_via_chain', 'scripts/ml/design_chain.py',
                                                     'written before a threshold change is refused, as is an unknown sha'),
                      'human_precision.targets': ('thresholds_changed-false entries',)}
CHAIN_RULE_SUPERSEDED = {'human_precision.join_order': 'equal to the current sha256', 'human_precision.targets': "equal to DESIGN.json's current sha256"}
TAKEOVER_HOP_PHRASES = {'human_precision.join_order': ('D.replaces -> copy C -> C.carried_from', 'one replaces hop'),
                        'human_precision.population_primary': ('every copy in D.replaces', 'ambiguous_takeover', 'duplicate_decision_on_recapture', 'one physical roof'),
                        'human_precision.targets': ('superseded_by_takeover', 'listing the frozen keys it covers'),
                        'human_precision.classes_order': ('superseded_by_takeover', 'no marker needed')}


def test_round5_rules_json_is_pre_registered():
    """The live RULES.json, read rather than pinned to a snapshot: the pre-registration and its constants; every amendment's shape (at
    after written_before and in time order, field, from, to, reason; previous_sha256 from the first dated entry; thresholds_changed exactly
    false, a note and the module sha from the linear-merge mirror on); the previous_sha256 chain (reverting each instant's named fields
    reproduces the sha the instant recorded, so the fields amended are exactly the ones the entries name); per-field continuity (each entry
    amends the text in force; the file holds the last text); the banned word (nothing an entry adds carries it; the one recorded exception,
    the TAKEOVER RULE mirror's inherited sentence, is pinned by index); and the frozen history of fields, which later entries only extend."""
    from scripts.ml.training.sam3_mac import r3_record as rec
    if not RULES5.exists():
        pytest.skip('round-5 RULES.json absent (data/ not checked out)')
    raw = RULES5.read_bytes()
    rules = json.loads(raw)
    assert rules['version'] == 'sam3-round5-rules-v1'
    assert _serialise_rules(rules) == raw, 'the file is the indent-1 ensure_ascii-False round trip the dated amendment scripts write'
    amendments = rules['amendments']
    ats = [a['at'] for a in amendments]
    assert ats == sorted(ats) and all(at > rules['written_before'] for at in ats), 'amendments are appended in time order after the pre-registration'
    module = 'scripts/ml/training/sam3_mac/r5_precision.py'
    for i, a in enumerate(amendments):
        assert {'at', 'field', 'from', 'to', 'reason'} <= set(a) and a['reason'].strip(), (i, a.get('field'))
        assert a['from'] != a['to'], (i, 'an amendment changes the text')
        assert a.get('thresholds_changed') in (None, False), (i, 'RULES has never changed a threshold')
        if i >= RULES5_PRE_REGISTRATION_BATCH:
            assert len(a['previous_sha256']) == 64 and set(a['previous_sha256']) <= set('0123456789abcdef'), (i, 'a dated entry names the whole-file sha before it')
        if i >= RULES5_THRESHOLDS_CHANGED_FROM:
            assert a['thresholds_changed'] is False and a['thresholds_changed_note'].strip() and a['code_sha256'][module], (i, 'the dated convention')
        if 'added_text' in a:
            assert a['added_text'] and _mentions(a['to'], a['added_text']) and not _mentions(a['from'], a['added_text']), (i, 'added_text is new in to')
        if 'removed_text' in a:
            assert a['removed_text'] and _mentions(a['from'], a['removed_text']) and not _mentions(a['to'], a['removed_text']), (i, 'removed_text has left to')
        if 'corrects' in a:
            assert a['corrects'] in ats[:i], (i, 'a correction names an earlier instant')
    batch = amendments[:RULES5_PRE_REGISTRATION_BATCH]
    assert len({a['at'] for a in batch}) == 1 and not any('previous_sha256' in a for a in batch), \
        'indices 0-6 are the one pre-registration batch, written before previous_sha256 was recorded'
    instants = _instants(amendments)
    assert instants[0] == (batch[0]['at'], 0, RULES5_PRE_REGISTRATION_BATCH)
    for at, first, end in instants[1:]:
        assert len({a['previous_sha256'] for a in amendments[first:end]}) == 1, (at, 'one instant, one previous_sha256')
    previous = [amendments[first]['previous_sha256'] for _, first, _ in instants[1:]]
    assert len(set(previous)) == len(previous) and hashlib.sha256(raw).hexdigest() not in previous, 'every instant changed the file'
    chain = list(_rules_before_each_instant(rules))
    assert [at for at, recorded, _ in chain if recorded is None] == [batch[0]['at']]
    for at, recorded, reconstructed in chain:
        if recorded is not None:
            assert reconstructed == recorded, (at, 'reverting the named fields reproduces the sha before the instant: the fields amended are the ones named')
    loaded, _, _ = r5.load_rules(RULES5)                              # load_rules refuses unless the file holds each last amended value
    by_field = _by_field(amendments)
    for field, entries in by_field.items():
        for earlier, later in zip(entries, entries[1:], strict=False):
            assert later['from'] == earlier['to'], (field, later['at'], 'each entry amends the text in force')
        assert _get_path(rules, field) == entries[-1]['to'] == _get_path(loaded, field), field
        if 'added_text' in entries[-1]:
            assert _mentions(_get_path(rules, field), entries[-1]['added_text']), (field, 'the newest added text is in force')
        if 'removed_text' in entries[-1]:
            assert not _mentions(_get_path(rules, field), entries[-1]['removed_text']), (field, 'the newest removed text is gone')
    fields = [a['field'] for a in amendments]
    assert fields[:len(FROZEN_AMENDED_FIELDS)] == list(FROZEN_AMENDED_FIELDS), 'the frozen history is unchanged; entries are only appended'
    assert amendments[len(FROZEN_AMENDED_FIELDS) - 1]['previous_sha256'] == RULES5_SHA_BEFORE_RECIPE_ADOPTION
    assert next(a for a in amendments if a['field'] == 'calibration')['previous_sha256'] == RULES5_SHA_BEFORE_RECIPE_ADOPTION
    assert [a for a in amendments if a['field'] == 'human_precision.targets'][-1]['previous_sha256'] == RULES5_SHA_BEFORE_RESTORE_SLICE
    for a in amendments[len(FROZEN_AMENDED_FIELDS):]:
        assert a['at'] > amendments[len(FROZEN_AMENDED_FIELDS) - 1]['at'] and a['thresholds_changed'] is False, ('an appended entry is dated and keeps thresholds', a['at'])
    # the banned word: nothing an entry adds carries it; the inherited sentence is the one exception, pinned by index with its source
    inherited = f'agreement never {BANNED}'
    for i, a in enumerate(amendments):
        assert _banned({k: v for k, v in a.items() if k not in ('from', 'to')}) == 0, (i, 'reason, notes, added_text and checks never carry the banned word')
        if i == RULES5_INHERITED_SENTENCE_INDEX:
            assert (a['at'], a['field']) == ('2026-10-02T00:21:02Z', 'human_precision.join_order') and 'TAKEOVER RULE' in a['to']
            assert _banned(a['from']) == 0 and a['to'].lower().count(BANNED) == 1 and inherited in a['to'].lower(), 'the mirror inherited WINDOW.json\'s sentence once'
        else:
            assert _banned(a['to']) == _banned(a['from']), (i, 'an entry never adds the banned word; inherited text carries it as often as before')
    assert all(_banned(a) == 0 for a in amendments[:RULES5_INHERITED_SENTENCE_INDEX]), 'no entry before the mirror carries the banned word'
    window_path = DESIGN5.with_name('WINDOW.json')
    if window_path.exists():
        window = json.load(open(window_path))
        assert inherited in json.dumps(window, ensure_ascii=False).lower() and _banned(window) == 1, 'the sentence is the WINDOW.json takeover rule\'s, once'
    assert _banned({k: v for k, v in rules.items() if k not in ('wording', 'amendments', 'human_precision')}) == 0, 'only the wording rule names the banned word'
    assert _banned({k: v for k, v in rules['human_precision'].items() if k != 'join_order'}) == 0
    assert rules['human_precision']['join_order'].lower().count(BANNED) == 1 and inherited in rules['human_precision']['join_order'].lower(), \
        'the inherited sentence is the only occurrence outside the wording rule'
    # the pre-registration's own content
    outline = next(a for a in amendments if a['field'].endswith('.outline.part'))
    assert (outline['from'], outline['to']) == (219, 0) and 'section 10' in outline['reason']
    loso = rules['ndsm_leg']['loso']['pre_registered_counts_on_4132_judge_rows']
    assert loso['outline'] == {'keep': 2965, 'review': 810, 'ignore': 357, 'part': 0}
    assert all(sum(t.values()) == 4132 for t in loso.values()), 'every drop tuple covers the 4,132 judge rows'
    if NDSM_RULES.exists():                                         # W3's own pre-registration of the same tuples
        assert json.load(open(NDSM_RULES))['loso']['pre_registered_counts_on_4132_judge_rows'] == loso
    assert 'part 219' not in rules['written_before_note'] and 'existed' not in rules['written_before_note']
    assert loaded['human_precision']['agent_text_regex'] == r5.AGENT_TEXT.pattern
    route = by_field['human_precision.agent_fresh_decision']
    assert len(route) >= 3 and route[1]['at'] == '2026-10-01T06:03:14Z'
    tighten = route[2]
    assert tighten['previous_sha256'] == RULES5_SHA_BEFORE_TIGHTEN and tighten['at'] > route[1]['at']
    assert "accepted_by is exactly the local app principal 'local'" in tighten['to'] and 'src/app/api/scenes/route.ts' in tighten['to']
    assert 'confirmed with geometry_reviewed true (class roof' in tighten['to'] and 'or confirmed with geometry_reviewed not true, is undecided' in tighten['to']
    assert "accepted_by other than 'local', is no acceptance" in tighten['to']
    assert 'are both set' in tighten['from'] and 'the acceptance already required geometry_reviewed' in tighten['from'], 'the dropped-condition text is what was amended'
    assert 'PROPOSED' in tighten['reason'] and 'yes to defaults' in tighten['reason'] and 'read at write time' in tighten['reason']
    live_route = loaded['human_precision']['agent_fresh_decision']
    assert live_route == route[-1]['to'] and live_route.count("'local'") >= 2 and r5.LOCAL_PRINCIPAL == 'local'
    for phrase in ("accepted_by is exactly the local app principal 'local'", "accepted_by other than 'local', is no acceptance"):
        assert phrase in live_route, (phrase, 'the tightened route is in force')
    assert rules['copied_from']['version'] == 'sam3-round4-rules-v1'
    assert rules['copied_from']['sha256'] == '863e9b515e8bb807fd481955aa002a3eb8a8d1ed80c4d95a607ed09c9e5ca304'
    numeric = {k: v for k, v in rules['decision_rules'].items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    assert numeric == rec.RULES_DEFAULT
    hp = rules['human_precision']
    assert hp['version'] == r5.VERSION and hp['n_rule']['primary_decided_min'] == 30
    assert {k: hp['n_rule'][k] for k in ('primary_decided_min', 'per_class_min', 'scenes_min')} == \
        {'primary_decided_min': 30, 'per_class_min': 8, 'scenes_min': 6} == r5.N_RULE
    assert hp['threshold_source'].startswith('val only')
    excluded = ' '.join(hp['provenance']['excluded'])
    assert '^Assistant (visually traced|image review)' in excluded and 'agent-authored' in excluded
    assert 'scripts/ml/review_authority.py' in excluded
    assert tuple(hp['grid']) == r5.GRID and hp['bootstrap']['seed'] == r5.BOOT_SEED and hp['bootstrap']['draws'] == r5.BOOT_DRAWS
    assert rules['written_before'] < '2026-10-01' and rules['rescope'].startswith('reference_eval.py unedited; no inference')


def test_round5_rules_record_the_design_sha_chain_rule():
    """The hop's RULES text pinned 'the current sha256 of DESIGN.json'; design_chain accepts a recorded sha behind thresholds_changed-false
    amendments, so a dated amendment (two entries at one instant, one previous_sha256: join_order and the targets parenthetical that mirrors
    it) states the chain rule. Those two entries are a frozen record and are checked as written; the live texts have since been extended
    (takeover hop, body carry, restore slice), so the rule is checked as PRESENT in the text in force and carried through every later entry,
    never as equal to the 2026-10-01 snapshot."""
    if not RULES5.exists():
        pytest.skip('round-5 RULES.json absent (data/ not checked out)')
    rules = json.load(open(RULES5))
    chain = [a for a in rules['amendments'] if a.get('previous_sha256') == RULES5_SHA_BEFORE_CHAIN]
    assert [a['field'] for a in chain] == ['human_precision.join_order', 'human_precision.targets'] and len({a['at'] for a in chain}) == 1
    assert chain[0]['at'] > '2026-10-01T07:36:04Z', 'dated after the tightening entry whose sha it names'
    join_order, targets = chain
    assert 'equal to the current sha256 of data/reviews/eyes/recapture/DESIGN.json' in join_order['from'] and "is not DESIGN.json's current sha256" in join_order['from']
    for entry in chain:
        for phrase in CHAIN_RULE_PHRASES[entry['field']]:
            assert phrase in entry['to'] and phrase not in entry['from'], (entry['field'], phrase)
        assert CHAIN_RULE_SUPERSEDED[entry['field']] in entry['from'] and CHAIN_RULE_SUPERSEDED[entry['field']] not in entry['to'], entry['field']
    for phrase in ('counter-H-design-probe.json', 'counter-C-before.json', 'counter-C-after.json', 'read at write time', 'thresholds_changed true'):
        assert phrase in join_order['reason'], phrase
    assert BANNED not in (join_order['to'] + targets['to'] + join_order['reason'] + targets['reason']).lower()
    loaded, _, _ = r5.load_rules(RULES5)
    by_field = _by_field(rules['amendments'])
    for entry in chain:
        later = [a for a in by_field[entry['field']] if a['at'] > entry['at']]
        assert later and later[0]['from'] == entry['to'], (entry['field'], 'the first later entry amended the chain-rule text')
        live = loaded['human_precision'][entry['field'].rsplit('.', 1)[1]]
        assert live == later[-1]['to'], (entry['field'], 'the file holds the newest text')
        for phrase in CHAIN_RULE_PHRASES[entry['field']]:
            assert phrase in live, (entry['field'], phrase, 'the chain rule is in force')
            assert all(phrase in a['to'] for a in later), (entry['field'], phrase, 'every later entry carried the chain rule forward')
        assert CHAIN_RULE_SUPERSEDED[entry['field']] not in live, (entry['field'], 'the superseded wording never came back')


def test_round5_rules_record_the_takeover_hop():
    """The reviewer's direction of 2026-10-02 ("from these images, its all still happening, the orange bounding box cuts off so the
    object boundary looks bad") opened the takeover: a whole-roof detection D of the window's model takes over the frozen slices
    carried into its frame. Four entries at one instant under the sha the file had before them state the one replaces hop
    (join_order), the count through it (population_primary), the asked target (targets) and the superseded_by_takeover stratum
    (classes_order). The four entries are a frozen record, checked as written; join_order and targets were extended afterwards (the
    TAKEOVER RULE mirror, body carry, restore slice), so the hop is checked as PRESENT in each text in force and carried through every
    later entry, and the file holds the newest text of each field."""
    if not RULES5.exists():
        pytest.skip('round-5 RULES.json absent (data/ not checked out)')
    rules = json.load(open(RULES5))
    entries = [a for a in rules['amendments'] if a.get('previous_sha256') == RULES5_SHA_BEFORE_TAKEOVER]
    assert [a['field'] for a in entries] == list(TAKEOVER_HOP_PHRASES) and len({a['at'] for a in entries}) == 1
    assert entries[0]['at'] > '2026-10-01T08:28:04Z', 'dated after the chain-rule entries whose resulting sha it names'
    join_order, population, targets, classes = entries
    chain = [a for a in rules['amendments'] if a.get('previous_sha256') == RULES5_SHA_BEFORE_CHAIN]
    assert join_order['from'] == chain[0]['to'] and targets['from'] == chain[1]['to'], 'each entry amends the text in force'
    assert 'replaces' not in join_order['from'] and 'takeover' not in population['from'] and 'takeover' not in targets['from']
    for text in (join_order['to'], population['to'], targets['to']):
        assert text.startswith(join_order['from'][:40]) or text.startswith(population['from'][:40]) or text.startswith(targets['from'][:40])
    for entry in entries:
        for phrase in TAKEOVER_HOP_PHRASES[entry['field']]:
            assert phrase in entry['to'], (entry['field'], phrase)
    direction = 'from these images, its all still happening, the orange bounding box cuts off so the object boundary looks bad'
    for a in entries:
        assert direction in a['reason'], a['field']
        assert 'read at write time' in a['reason'] and a['code_sha256']['scripts/ml/training/sam3_mac/r5_precision.py']
    assert 'counter-takeover-before.json' in join_order['reason'] and 'counter-takeover-after.json' in join_order['reason']
    assert BANNED not in json.dumps(entries).lower()
    loaded, _, _ = r5.load_rules(RULES5)
    by_field = _by_field(rules['amendments'])
    for entry in entries:
        later = [a for a in by_field[entry['field']] if a['at'] > entry['at']]
        live = loaded['human_precision'][entry['field'].rsplit('.', 1)[1]]
        assert live == (later[-1] if later else entry)['to'], (entry['field'], 'the file holds the newest text')
        if later:
            assert later[0]['from'] == entry['to'], (entry['field'], 'the first later entry amended the hop text')
        for phrase in TAKEOVER_HOP_PHRASES[entry['field']]:
            assert phrase in live, (entry['field'], phrase, 'the takeover hop is in force')
            assert all(phrase in a['to'] for a in later), (entry['field'], phrase, 'every later entry carried the hop forward')
    assert {f for f, e in by_field.items() if e[-1]['at'] > join_order['at']} >= {'human_precision.join_order', 'human_precision.targets'}, \
        'join_order and targets were extended after the takeover (the mirror, body carry, restore slice)'


def test_round5_rules_mirror_the_linear_merge_design_note():
    """2026-10-03 (reviewer "yes to defaults" 15:10 AEST on the linear-continuity card and 19:40 AEST on the marks card): the road /
    footpath / driveway continuity merge is a DESIGN.json field (linear_merge) with a dated thresholds_changed-false amendment, mirrored
    into RULES as a new top-level field of the same name (from null). Linear kinds are not counted, so the mirror instant names no
    human_precision field; load_rules checks the file holds the mirror text; the banned word is absent."""
    from scripts.ml import design_chain
    if not RULES5.exists() or not DESIGN5.exists():
        pytest.skip('round-5 RULES.json or recapture DESIGN.json absent (data/ not checked out)')
    rules = json.load(open(RULES5))
    mirror = [a for a in rules['amendments'] if a['field'] == 'linear_merge']
    assert len(mirror) == 1 and mirror[0]['previous_sha256'] == RULES5_SHA_BEFORE_LINEAR_MERGE and mirror[0]['at'] > '2026-10-02T00:21:02Z'
    entry = mirror[0]
    assert entry['from'] is None and entry['thresholds_changed'] is False and rules['linear_merge'] == entry['to']
    assert entry['to'].startswith('LINEAR MERGE, the round-5 mirror of data/reviews/eyes/recapture/DESIGN.json field linear_merge')
    design, design_sha, accepted = design_chain.read(DESIGN5)
    assert design_sha in entry['to'] and design['linear_merge'] in entry['to'], 'the mirror carries the DESIGN rule text and names the DESIGN sha it mirrors'
    design_entry = [a for a in design['amendments'] if a['field'] == 'linear_merge'][-1]
    assert design_entry['thresholds_changed'] is False and design_entry['to'] == design['linear_merge']
    assert accepted[design_entry['previous_sha256']] == {'match': design_chain.VIA_CHAIN, 'hops_back': 1, 'amendment_index': len(design['amendments']),
                                                        'amendment_at': design_entry['at'], 'amendment_field': 'linear_merge'}
    for phrase in ('linear kinds are NOT counted', 'kind building', 'primary_n, primary_by_class, primary_rows, targets_remaining, takeover_rows',
                   'thresholds_changed false', 'result_floor_m2 {"feature": 1.0, "largest_member": 1.0}', '"pool", "water", "building"',
                   'd_m {"driveway": 0.6, "footpath": 0.8, "road": 1.0}', 'bridge_max_ratio 2.0', 'union_ratio_max 1.15', 'TRAINING LABELS stay pieces'):
        assert phrase in entry['to'], phrase
    for phrase in ('15:10 AEST', '19:40 AEST', '"yes to defaults"', 'read at write time', 'linear kinds are not counted'):
        assert phrase in entry['reason'], phrase
    assert BANNED not in (entry['to'] + entry['reason']).lower()
    assert {a['field'] for a in rules['amendments'] if a['at'] == entry['at']} == {'linear_merge'}, 'the mirror instant names no human_precision field'
    loaded, _, _ = r5.load_rules(RULES5)
    assert loaded['linear_merge'] == entry['to'] and loaded['human_precision'] == rules['human_precision']


def test_cli_count_json_runs_on_a_fixture(tmp_path, capsys):
    g = _guide_world(tmp_path)
    run = tmp_path / 'run'
    (run / 'reference-eval-v2').mkdir(parents=True)
    json.dump(g['context'], open(run / 'reference-eval-v2' / 'context.json', 'w'))
    assert r5.main(['count', '--json', '--block', 'v2', '--run', str(run), '--scenes', str(g['scenes']), '--prior-set', str(g['prior']),
                    '--rules', str(tmp_path / 'absent.json'), '--repo', str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['primary_n'] == 1 and out['rules_sha256'] is None and out['block'] == 'v2'


def test_cli_recapture_dir_defaults_to_the_live_registry_and_an_absent_dir_is_an_empty_registry(tmp_path, monkeypatch):
    """/api/review-progress calls count without flags, so the registry windows are read from data/reviews/eyes/recapture by
    default once they exist; until then (or on an absent folder) the lineage is empty and every carry is refused by name."""
    assert r5.RECAPTURE_DIR == REPO / 'data/reviews/eyes/recapture'
    seen = {}

    def fake_count(block='v2', **kwargs):
        seen.update(kwargs)
        return {'primary_n': 0}
    monkeypatch.setattr(r5, 'count', fake_count)
    assert r5.main(['count', '--json', '--block', 'v2']) == 0
    assert seen['recapture_dir'] == Path(r5.RECAPTURE_DIR), 'the flag absent: the live registry folder'
    assert r5.main(['count', '--json', '--recapture-dir', str(tmp_path / 'elsewhere')]) == 0
    assert seen['recapture_dir'] == tmp_path / 'elsewhere'
    empty = r5.load_lineage(tmp_path / 'absent')
    assert (empty['design_sha256'], empty['registry'], empty['registry_files'], empty['registry_rows']) == (None, {}, [], 0)
    assert r5.registered(empty, ('s1', 'o1'), 'win-1') is False and r5.registry_windows(empty, ('s1', 'o1')) == []


# --- lineage: a decision on a recapture reaches the frozen detection only through carried_from ------------------------
#
# Design .run/focus-recapture-design-2026-10-01.md section 5 (count rule): for a review object with a non-empty carried_from
# the frozen key is (carried_from.scene_id, carried_from.object_id); P/R path, snapshot area, the 4 m2 floor, live, production
# and source are read at the frozen key in the SOURCE scene; class, provenance kind, stratum, fresh state, revision and notes
# from the copy's own review object. Five named refusals leave the row unjoined. Exactly one decision per frozen key: a counted
# source decision wins (copies listed duplicate_decision_on_recapture); copies in two scenes or twice in one count nowhere
# (ambiguous_carry). No geometric match ever joins a decision. The live image-sha requirement is met through the lineage:
# frame_transform.source_image_sha256 must equal the frozen split's image sha for the source scene.

DESIGN_TEXT = json.dumps({'version': 'recapture-design-v1', 'max_shift_m': .7, 'min_ncc': .35, 'min_overlap_fraction': .6,
                          'match_iou': .5, 'close_iou': .9})
DESIGN_SHA = hashlib.sha256(DESIGN_TEXT.encode()).hexdigest()
CARRY_NOTE = ('Carried from s1 through the georeferenced frame as a frame-cut target: the earlier frame cut this outline at its '
              'edge; decide roof / not a roof on this image.')


def _transform(source='s1', *, verified=True, design_sha=DESIGN_SHA, source_sig='sig', source_sha=None, method='georeferenced-affine'):
    return {'version': 'frame-transform-v1', 'method': method, 'source_scene_id': source, 'source_model_signature': source_sig,
            'source_image_sha256': source_sha or f'img-{source}', 'source_size': [512, 512], 'source_registration': 'region',
            'metre_offset': [1., 2.], 'pixel_affine': [[1., 0.], [0., 1.], [10., 20.]],
            'overlap': {'source_px_inside_target': 262144, 'fraction': 1.}, 'residual': {'shift_m': .1, 'ncc': .8},
            'design': {'max_shift_m': .7, 'min_ncc': .35, 'min_overlap_fraction': .6, 'match_iou': .5, 'close_iou': .9,
                       'design_sha256': design_sha}, 'verified': verified}


def _copy(oid, status, source_oid, source='s1', *, sig='sig', method='georeferenced-affine', area=10., **extra):
    """A target-carry copy as the design's Python carry writes it (bare lead sentence, needs_adjustment) or as the reviewer left it."""
    carried = {'scene_id': source, 'model_signature': sig, 'object_id': source_oid, 'status': 'candidate', 'geometry_reviewed': False,
               'iou_with_new': 0, 'matched_id': None, 'method': method, 'role': 'frame_cut_target'}
    return _obj(oid, status, area=area, carried_from=carried, notes=extra.pop('notes', CARRY_NOTE), **extra)


def _window(scenes, wid, copies, *, transform, revision=2, live=()):
    _scene(scenes, wid, review=copies, latest=[_live(o) for o in live], revision=revision, sig=f'sig-{wid}', frame_transform=transform)


def _registry_row(source_oid, window, *, source='s1', role='carried_source', verified=True, **extra):
    row = {'source_scene_id': source, 'object_id': source_oid, 'window_scene_id': window, 'carried_object_id': source_oid, 'role': role,
           'frame_transform_verified': verified, 'design_sha256': DESIGN_SHA, 'window_sha256': 'w' * 64, 'written_at': '2026-10-01T05:00:00Z'}
    row.update(extra)
    return row


def _recapture_dir(tmp_path, rows=(), design=DESIGN_TEXT, name='windows-2026-10-01.jsonl'):
    folder = tmp_path / 'recapture'
    folder.mkdir(exist_ok=True)
    if design is not None:
        (folder / 'DESIGN.json').write_text(design)
    if rows:
        with open(folder / name, 'a') as handle:
            handle.writelines(json.dumps(r) + '\n' for r in rows)
    return folder


def _count(w, recapture):
    return r5.count(block='v2', context=w['context'], split_path=w['split'], scenes_root=w['scenes'], prior_set=None, rules_path=None,
                    recapture_dir=recapture)


def test_load_lineage_reads_the_design_sha_and_a_dated_registry_with_withdrawals(tmp_path):
    empty = r5.load_lineage(tmp_path / 'absent')
    assert empty['design_sha256'] is None and empty['registry'] == {} and empty['registry_files'] == []
    rows = [_registry_row('p-open', 'win-1'), _registry_row('p-open2', 'win-1', role='uncarried_source'),
            _registry_row('p-roof', 'win-2'), {**_registry_row('p-roof', 'win-2'), 'withdrawn': True, 'written_at': '2026-10-01T06:00:00Z'}]
    folder = _recapture_dir(tmp_path, rows)
    lineage = r5.load_lineage(folder)
    assert lineage['design_sha256'] == DESIGN_SHA == hashlib.sha256((folder / 'DESIGN.json').read_bytes()).hexdigest()
    assert lineage['registry_files'] == ['windows-2026-10-01.jsonl'] and lineage['registry_rows'] == 4
    assert r5.registered(lineage, ('s1', 'p-open'), 'win-1') is True
    assert r5.registered(lineage, ('s1', 'p-open'), 'win-9') is False, 'the row names one window'
    assert r5.registered(lineage, ('s1', 'p-open2'), 'win-1') is False, 'an uncarried_source row is not a carry pointer'
    assert r5.registered(lineage, ('s1', 'p-roof'), 'win-2') is False, 'a withdrawn row (rollback appends, never rewrites) removes the pointer'
    assert r5.registry_windows(lineage, ('s1', 'p-open2')) == ['win-1'], 'listed beside the old target whatever the role'
    assert r5.registry_windows(lineage, ('s1', 'p-roof')) == []


def test_load_decisions_reads_frame_transform_carried_from_and_the_source_latest_the_hop_needs(tmp_path):
    scenes = tmp_path / 'scenes'
    _scene(scenes, 'src', latest=[_live('old')])                                       # no review.json, not in extra_live
    ft = _transform('src')
    _window(scenes, 'win-1', [_copy('old', 'needs_adjustment', 'old', source='src'), _copy('odd', 'confirmed', 'x', source='qa_never')],
            transform=ft)
    loaded = r5.load_decisions(scenes)
    assert loaded['live']['win-1']['frame_transform'] == ft and loaded['live']['src']['frame_transform'] is None
    assert 'src' in loaded['live'] and 'old' in loaded['live']['src']['objects'], 'the source latest.json is read for the hop'
    assert 'qa_never' not in loaded['live'], 'a qa_ source is never read'
    rows = {r['object_id']: r for r in loaded['rows']}
    assert rows['old']['carried'] is True and rows['old']['carried_from']['object_id'] == 'old'
    assert rows['old']['carried_from']['scene_id'] == 'src' and loaded['latest_read'] == 2


def test_join_hop_reads_the_frozen_key_in_the_source_and_the_decision_from_the_copy(tmp_path):
    w = _live_world(tmp_path)
    copies = [_copy('p-open', 'confirmed', 'p-open'),                                       # the reviewer's roof on the new frame
              _copy('p-open2', 'needs_adjustment', 'p-open2'),                              # still the target: undecided
              _copy('p-gone', 'rejected', 'p-gone'),                                        # frozen object no longer live at the source
              _copy('p-snap251', 'rejected', 'p-snap251'),                                  # snapshot 25.1: joined, not small
              _copy('p-unlisted', 'confirmed', 'p-unlisted'),                               # not in pseudo_ids: not_in_snapshot
              _copy('chat', 'rejected', 'p-not', provenance={'human_decision': {'source': 'human:chat', 'fields': ['status']}}),
              _copy('agent-copy', 'confirmed', 'p-snap249', notes=AGENT_TEXT),             # agent text, no marker: excluded
              _copy('fresh-copy', 'rejected', 'p-roof', notes='[reviewer] not a roof ' + AGENT_TEXT)]
    _window(w['scenes'], 'win-1', copies, transform=_transform(), live=['new-roof'])
    rows = [_registry_row(o['carried_from']['object_id'], 'win-1') for o in copies]
    lineage = r5.load_lineage(_recapture_dir(tmp_path, rows))
    joined = r5.join(w['context'], w['split'], r5.load_decisions(w['scenes']), lineage=lineage)
    by = {(r['scene_id'], r['object_id']): r for r in joined['rows']}
    roof = by[('win-1', 'p-open')]
    assert (roof['path'], roof['ann_id'], roof['snapshot_area_m2'], roof['cls'], roof['provenance_kind']) == ('P', 15, 10., 'roof', 'ui')
    assert (roof['live'], roof['production'], roof['source'], roof['primary'], roof['decided']) == (True, True, 'sam3-text-onnx', True, True)
    assert roof['frozen_key'] == ['s1', 'p-open'] and roof['judged_scene'] is True and roof['image_sha_match'] is True
    assert roof['lineage']['refusals'] == [] and roof['lineage']['window_scene_id'] == 'win-1' and roof['unjoined_reasons'] == []
    assert roof['review_revision'] == 2 and roof['human_drawn'] is False, 'a carried copy never enters the human-drawn stratum'
    undecided = by[('win-1', 'p-open2')]
    assert (undecided['cls'], undecided['stratum'], undecided['path'], undecided['primary']) == ('undecided', 'carried_undecided', 'P', False)
    gone = by[('win-1', 'p-gone')]
    assert (gone['path'], gone['live'], gone['production'], gone['primary'], gone['not_counted_reason']) == ('P', False, False, False, 'not_live')
    big = by[('win-1', 'p-snap251')]
    assert (big['path'], big['small'], big['primary']) == ('P', False, False), 'the 25 m2 gate reads the frozen row at the frozen key'
    assert by[('win-1', 'p-unlisted')]['unjoined_reasons'] == ['not_in_snapshot'] and by[('win-1', 'p-unlisted')]['path'] is None
    chat = by[('win-1', 'chat')]
    assert (chat['frozen_key'], chat['path'], chat['cls'], chat['provenance_kind']) == (['s1', 'p-not'], 'P', 'not_roof', 'chat')
    assert chat['primary'] is False and chat['not_counted_reason'] == 'duplicate_decision_on_recapture', 'p-not is decided and counted at s1'
    agent = by[('win-1', 'agent-copy')]
    assert (agent['cls'], agent['stratum'], agent['fresh'], agent['primary']) == ('excluded', 'agent_authored_excluded', 'no_marker', False)
    fresh = by[('win-1', 'fresh-copy')]
    assert (fresh['cls'], fresh['fresh'], fresh['decided'], fresh['primary'], fresh['not_counted_reason']) == \
        ('not_roof', 'valid', True, False, 'duplicate_decision_on_recapture'), 'the marker rule is unchanged; p-roof is counted at s1'
    assert by[('s1', 'p-roof')]['primary'] is True and by[('s1', 'p-not')]['primary'] is True, 'the source decisions still count'
    assert ('win-1', 'new-roof') not in by, 'a window object without a review row is nothing to the count'
    assert all(r['lineage'] is None and r['frozen_key'] == [r['scene_id'], r['object_id']] for r in joined['rows'] if not r['carried'])
    summary = r5.population(joined)
    assert summary['primary_decided'] == 5 and summary['by_class'] == {'roof': 3, 'not_roof': 2}, 'p-open joins the four of _live_world'
    assert summary['joined_not_counted_by_reason']['duplicate_decision_on_recapture'] == 2
    assert summary['joined_not_counted_by_reason']['not_live'] == 3 and summary['strata']['carried_undecided'] == 1


def test_join_hop_named_refusals_leave_the_row_unjoined(tmp_path):
    w = _live_world(tmp_path)
    s = w['scenes']
    _window(s, 'win-method', [_copy('a', 'confirmed', 'p-open', method='identical-frame')], transform=_transform())
    _window(s, 'win-unverified', [_copy('b', 'confirmed', 'p-open')], transform=_transform(verified=False))
    _window(s, 'win-design', [_copy('c', 'confirmed', 'p-open')], transform=_transform(design_sha='0' * 64))
    _window(s, 'win-stale-sig', [_copy('d', 'confirmed', 'p-open', sig='stale')], transform=_transform())
    _window(s, 'win-stale-image', [_copy('e', 'confirmed', 'p-open')], transform=_transform(source_sha='img-other'))
    _window(s, 'win-other-source', [_copy('f', 'confirmed', 'p-open', source='s2')], transform=_transform('s1'))
    _window(s, 'win-unregistered', [_copy('g', 'confirmed', 'p-open')], transform=_transform())
    _window(s, 'win-no-transform', [_copy('h', 'confirmed', 'p-open', method='other', sig='stale')], transform=None)
    _window(s, 'win-string', [_obj('i', 'confirmed', carried_from='s1')], transform=_transform())
    rows = [_registry_row('p-open', win) for win in ('win-method', 'win-unverified', 'win-design', 'win-stale-sig', 'win-stale-image',
                                                      'win-no-transform', 'win-string')]
    rows.append(_registry_row('p-open', 'win-other-source', source='s2'))
    lineage = r5.load_lineage(_recapture_dir(tmp_path, rows))
    joined = r5.join(w['context'], w['split'], r5.load_decisions(s), lineage=lineage)
    by = {r['object_id']: r for r in joined['rows'] if r['scene_id'].startswith('win-')}
    expect = {'a': ['carry_method'], 'b': ['carry_unverified'], 'c': ['carry_design_mismatch'], 'd': ['carry_snapshot_mismatch'],
              'e': ['carry_snapshot_mismatch'], 'f': ['carry_snapshot_mismatch'], 'g': ['carry_unregistered'],
              'h': ['carry_unverified', 'carry_design_mismatch', 'carry_snapshot_mismatch', 'carry_method'],
              'i': ['carry_snapshot_mismatch', 'carry_unregistered', 'carry_method']}      # a verified window, a carry that names no key
    for oid, reasons in expect.items():
        row = by[oid]
        assert row['unjoined_reasons'] == reasons, (oid, row['unjoined_reasons'])
        assert row['lineage']['refusals'] == reasons and row['path'] is None and row['primary'] is False and row['production'] is False
        assert row['decided'] is True and row['cls'] == 'roof', 'the copy is a decision; it reaches no frozen row'
    assert by['a']['frozen_key'] == ['s1', 'p-open'] and by['i']['frozen_key'] is None, 'a non-dict carried_from names no key'
    assert r5.population(joined)['unjoined_by_reason'] == {'not_in_snapshot': 2, 'under_min_area': 2, 'not_judged_image': 2,
                                                           'carry_unverified': 2, 'carry_design_mismatch': 2, 'carry_snapshot_mismatch': 5,
                                                           'carry_unregistered': 2, 'carry_method': 3}
    assert r5.population(joined)['primary_decided'] == 4, 'no refused copy moves the count'
    # without a lineage argument every carried copy is unregistered and the design is unknown
    bare = r5.join(w['context'], w['split'], r5.load_decisions(s))
    assert {'carry_unregistered', 'carry_design_mismatch'} <= set(next(r for r in bare['rows'] if r['object_id'] == 'g')['unjoined_reasons'])
    assert r5.population(bare)['primary_decided'] == 4


def test_join_one_decision_per_frozen_key(tmp_path):
    w = _live_world(tmp_path)
    s = w['scenes']
    # p-open carried into two verified, registered windows: the key counts nowhere even though only one copy is decided
    _window(s, 'win-1', [_copy('p-open', 'confirmed', 'p-open'), _copy('p-open2', 'confirmed', 'p-open2'),
                         _copy('p-open2-carried', 'needs_adjustment', 'p-open2')], transform=_transform())      # twice in one window
    _window(s, 'win-2', [_copy('p-open', 'needs_adjustment', 'p-open'),
                         _copy('p-snap249', 'rejected', 'p-snap249')], transform=_transform())                   # source p-snap249 counted
    _window(s, 'win-3', [_copy('p-open', 'confirmed', 'p-open')], transform=_transform(verified=False))         # refused: not a copy that joins
    rows = [_registry_row('p-open', 'win-1'), _registry_row('p-open2', 'win-1'), _registry_row('p-open', 'win-2'),
            _registry_row('p-snap249', 'win-2'), _registry_row('p-open', 'win-3', verified=False)]
    lineage = r5.load_lineage(_recapture_dir(tmp_path, rows))
    joined = r5.join(w['context'], w['split'], r5.load_decisions(s), lineage=lineage)
    by = {(r['scene_id'], r['object_id']): r for r in joined['rows']}
    one, two = by[('win-1', 'p-open')], by[('win-2', 'p-open')]
    assert (one['primary'], one['not_counted_reason'], one['lineage']['copies']) == (False, 'ambiguous_carry', 2)
    assert (two['primary'], two['not_counted_reason'], two['cls'], two['lineage']['copies']) == (False, None, 'undecided', 2), \
        'an undecided copy lists nothing as not counted, but the key is ambiguous'
    assert by[('win-3', 'p-open')]['unjoined_reasons'] == ['carry_unverified', 'carry_unregistered'], \
        'a refused copy does not make the key ambiguous (its registry row records the unverified capture too)'
    a, b = by[('win-1', 'p-open2')], by[('win-1', 'p-open2-carried')]
    assert (a['primary'], a['not_counted_reason'], b['lineage']['copies']) == (False, 'ambiguous_carry', 2), 'twice in one window'
    dup = by[('win-2', 'p-snap249')]
    assert (dup['primary'], dup['not_counted_reason'], dup['lineage']['source_decision_counted']) == \
        (False, 'duplicate_decision_on_recapture', True)
    assert by[('s1', 'p-snap249')]['primary'] is True, 'the source decision wins and still counts once'
    summary = r5.population(joined)
    assert summary['primary_decided'] == 4 and summary['joined_not_counted_by_reason']['ambiguous_carry'] == 2
    assert summary['joined_not_counted_by_reason']['duplicate_decision_on_recapture'] == 1


def test_join_a_source_decision_that_does_not_count_never_blocks_the_copy(tmp_path):
    """995244b3e1216ea7's case: confirmed on the old frame with the Codex note and no marker (excluded), carried, decided on the window."""
    w = _live_world(tmp_path)
    review = json.load(open(w['scenes'] / 's1' / 'review.json'))
    review['objects'].append(_obj('p-open2', 'confirmed', area=6., notes='Assistant image review · Codex 2026-09-30: roof slice'))
    json.dump(review, open(w['scenes'] / 's1' / 'review.json', 'w'))
    _window(w['scenes'], 'win-1', [_copy('p-open2', 'rejected', 'p-open2')], transform=_transform())
    lineage = r5.load_lineage(_recapture_dir(tmp_path, [_registry_row('p-open2', 'win-1')]))
    joined = r5.join(w['context'], w['split'], r5.load_decisions(w['scenes']), lineage=lineage)
    by = {(r['scene_id'], r['object_id']): r for r in joined['rows']}
    assert by[('s1', 'p-open2')]['cls'] == 'excluded' and by[('s1', 'p-open2')]['primary'] is False
    copy = by[('win-1', 'p-open2')]
    assert (copy['primary'], copy['cls'], copy['not_counted_reason'], copy['lineage']['source_decision_counted']) == (True, 'not_roof', None, False)
    assert r5.population(joined)['by_class'] == {'roof': 2, 'not_roof': 3}


def test_targets_exclude_a_recaptured_target_once_and_ask_its_copy_on_the_window(tmp_path):
    w = _live_world(tmp_path)
    before = _count(w, _recapture_dir(tmp_path))                                   # no registry, no window
    base_targets = {(s['scene_id'], o['id']) for s in before['next_scenes'] for o in s['objects']}
    assert ('s1', 'p-open') in base_targets and ('s1', 'p-open2') in base_targets
    _window(w['scenes'], 'win-1', [_copy('p-open', 'needs_adjustment', 'p-open'), _copy('p-open2', 'confirmed', 'p-open2')],
            transform=_transform())
    rows = [_registry_row('p-open', 'win-1'), _registry_row('p-open2', 'win-1')]
    c = _count(w, _recapture_dir(tmp_path, rows))
    targets = {(s['scene_id'], o['id']): o for s in c['next_scenes'] for o in s['objects']}
    assert ('s1', 'p-open') not in targets and ('win-1', 'p-open') in targets, 'asked once, on the window'
    copy = targets[('win-1', 'p-open')]
    assert copy['area_m2'] == 10. and copy['url'] == r5.url_for('win-1', 'p-open') and copy['source_scene_id'] == 's1'
    assert copy['source_object_id'] == 'p-open' and copy['note'].startswith('Carried from s1')
    assert c['target_exclusions']['recaptured:win-1'] == 2 and c['target_exclusions']['decided'] == before['target_exclusions']['decided'] + 1
    assert c['targets_total'] == before['targets_total'], 'the pool counts frozen keys; a copy is the same key'
    assert c['targets_remaining'] == before['targets_remaining'] - 1, 'p-open moved to the window; p-open2 is decided there'
    counted = {(o['scene_id'], o['id']): o for o in c['already_counted']}
    assert counted[('win-1', 'p-open2')]['frozen_key'] == ['s1', 'p-open2'] and c['primary_n'] == before['primary_n'] + 1
    assert 'frozen_key' not in counted[('s1', 'p-roof')], 'direct rows keep their shape'
    assert c['primary_rows'][-1] == ['win-1', 'p-open2', 'confirmed', 6.0, 'sam3-text-onnx', 'P'], \
        'the copy is listed on the window scene with the frozen area and the source-scene source'
    assert c['lineage']['registry_rows'] == 2 and c['lineage']['carried_copies'] == 2 and c['lineage']['carried_joined'] == 2
    listed = {(o['scene_id'], o['id']): o for o in c['carried_rows']}
    assert listed[('win-1', 'p-open')]['refusals'] == [] and listed[('win-1', 'p-open')]['class'] == 'undecided'
    for key in ('primary_n', 'primary_by_class', 'scene_ids', 'agent_authored_excluded', 'targets_total', 'targets_remaining'):
        json.dumps(c[key])
    assert BANNED not in json.dumps(c).lower()


def test_targets_keep_the_old_target_when_the_copy_cannot_count(tmp_path):
    w = _live_world(tmp_path)
    s = w['scenes']
    _window(s, 'win-u', [_copy('p-open', 'needs_adjustment', 'p-open')], transform=_transform(verified=False))     # unverified capture
    _window(s, 'win-a', [_copy('p-open2', 'needs_adjustment', 'p-open2')], transform=_transform())               # p-open2 in two windows
    _window(s, 'win-b', [_copy('p-open2', 'needs_adjustment', 'p-open2'),
                         _copy('p-roof', 'needs_adjustment', 'p-roof')], transform=_transform())                 # p-roof decided at s1
    rows = [_registry_row('p-open', 'win-u', verified=False), _registry_row('p-open2', 'win-a'), _registry_row('p-open2', 'win-b'),
            _registry_row('p-roof', 'win-b')]
    c = _count(w, _recapture_dir(tmp_path, rows))
    targets = {(s_['scene_id'], o['id']): o for s_ in c['next_scenes'] for o in s_['objects']}
    assert ('s1', 'p-open') in targets and targets[('s1', 'p-open')]['window'] == ['win-u'], 'the old target stays and lists its window'
    assert ('win-u', 'p-open') not in targets
    assert ('s1', 'p-open2') in targets and targets[('s1', 'p-open2')]['window'] == ['win-a', 'win-b']
    assert ('win-a', 'p-open2') not in targets and ('win-b', 'p-open2') not in targets, 'ambiguous copies are never asked'
    assert ('win-b', 'p-roof') not in targets and ('s1', 'p-roof') not in targets
    ex = c['target_exclusions']
    assert ex.get('carry_unverified') == 1 and ex.get('ambiguous_carry') == 2 and ex.get('decided_on_source') == 1
    assert 'recaptured:win-u' not in ex and 'recaptured:win-a' not in ex and 'recaptured:win-b' not in ex
    assert c['targets_remaining'] == len(targets)


def test_join_and_targets_agree_when_a_second_copy_is_re_kinded(tmp_path):
    """A decided building copy in win-1 and a copy the reviewer re-kinded (kind tree, cls skipped) in win-2 on one frozen key: both
    are carried copies, so the key is ambiguous_carry for join() AND for targets() (the old target stays). The re-kinded copy is
    emitted with its lineage instead of being dropped, so the copies tally, join() and targets() read the same rows."""
    w = _live_world(tmp_path)
    s = w['scenes']
    before = _count(w, _recapture_dir(tmp_path))
    _window(s, 'win-1', [_copy('p-open', 'confirmed', 'p-open')], transform=_transform())
    _window(s, 'win-2', [_copy('p-open', 'needs_adjustment', 'p-open', kind='tree')], transform=_transform())
    recapture = _recapture_dir(tmp_path, [_registry_row('p-open', 'win-1'), _registry_row('p-open', 'win-2')])
    lineage = r5.load_lineage(recapture)
    joined = r5.join(w['context'], w['split'], r5.load_decisions(s), lineage=lineage)
    by = {(r['scene_id'], r['object_id']): r for r in joined['rows']}
    decided, tree = by[('win-1', 'p-open')], by[('win-2', 'p-open')]
    assert (decided['primary'], decided['not_counted_reason'], decided['lineage']['copies']) == (False, 'ambiguous_carry', 2)
    assert (tree['cls'], tree['kind'], tree['decided'], tree['primary'], tree['provenance_kind']) == ('skipped', 'tree', False, False, None)
    assert tree['frozen_key'] == ['s1', 'p-open'] and tree['lineage']['copies'] == 2 and tree['lineage']['refusals'] == []
    assert tree['path'] == 'P' and tree['not_counted_reason'] is None, 'a re-kinded copy is a listing, never a decision'
    assert all(r['carried'] for r in joined['rows'] if r['cls'] == 'skipped'), 'direct non-building rows are still dropped'
    assert r5.population(joined)['strata'].get('kind_not_building') == 1 and r5.population(joined)['primary_decided'] == 4
    c = _count(w, recapture)
    targets = {(s_['scene_id'], o['id']): o for s_ in c['next_scenes'] for o in s_['objects']}
    assert ('s1', 'p-open') in targets and targets[('s1', 'p-open')]['window'] == ['win-1', 'win-2'], 'the old target stays'
    assert ('win-1', 'p-open') not in targets and ('win-2', 'p-open') not in targets
    ex = c['target_exclusions']
    assert ex.get('ambiguous_carry') == 2 and not any(k.startswith('recaptured:') for k in ex)
    assert c['primary_n'] == before['primary_n'] and c['targets_remaining'] == before['targets_remaining']
    assert c['lineage']['carried_copies'] == 2 and c['lineage']['carried_joined'] == 2 and c['lineage']['ambiguous_carry'] == 1
    listed = {(o['scene_id'], o['id']): o for o in c['carried_rows']}
    assert listed[('win-2', 'p-open')]['class'] == 'skipped' and listed[('win-2', 'p-open')]['copies'] == 2


def test_targets_ask_nowhere_when_the_single_copy_was_re_kinded(tmp_path):
    """One carried copy re-kinded to tree: the key is asked nowhere (old target recaptured:<window>, copy kind_not_building), as a
    re-kinded object on its own scene is never asked again; the copy is listed with its lineage and counts nothing."""
    w = _live_world(tmp_path)
    before = _count(w, _recapture_dir(tmp_path))
    _window(w['scenes'], 'win-1', [_copy('p-open', 'needs_adjustment', 'p-open', kind='tree')], transform=_transform())
    c = _count(w, _recapture_dir(tmp_path, [_registry_row('p-open', 'win-1')]))
    targets = {(s['scene_id'], o['id']) for s in c['next_scenes'] for o in s['objects']}
    assert ('s1', 'p-open') not in targets and ('win-1', 'p-open') not in targets
    assert c['target_exclusions']['recaptured:win-1'] == 1 and c['target_exclusions']['kind_not_building'] == 1
    assert c['targets_total'] == before['targets_total'] and c['targets_remaining'] == before['targets_remaining'] - 1
    assert c['primary_n'] == before['primary_n'] and c['lineage']['carried_copies'] == 1 and c['lineage']['carried_joined'] == 1


def test_registered_needs_frame_transform_verified_and_the_current_design_sha_on_the_registry_row(tmp_path):
    """Amendment entry 3 follows a VERIFIED registry row: a row with frame_transform_verified false, or a design_sha256 that is not
    DESIGN.json's, is no registration (carry_unregistered) even beside a verified live window; the copy is refused, the old
    target stays and lists the window."""
    w = _live_world(tmp_path)
    s = w['scenes']
    before = _count(w, _recapture_dir(tmp_path))
    _window(s, 'win-1', [_copy('p-open', 'confirmed', 'p-open')], transform=_transform())              # verified live window
    _window(s, 'win-2', [_copy('p-open2', 'confirmed', 'p-open2')], transform=_transform())            # verified live window
    rows = [_registry_row('p-open', 'win-1', verified=False), _registry_row('p-open2', 'win-2', design_sha256='0' * 64)]
    recapture = _recapture_dir(tmp_path, rows)
    lineage = r5.load_lineage(recapture)
    assert r5.registered(lineage, ('s1', 'p-open'), 'win-1') is False and r5.registered(lineage, ('s1', 'p-open2'), 'win-2') is False
    assert r5.registry_windows(lineage, ('s1', 'p-open')) == ['win-1'], 'still listed beside the old target'
    joined = r5.join(w['context'], w['split'], r5.load_decisions(s), lineage=lineage)
    by = {(r['scene_id'], r['object_id']): r for r in joined['rows']}
    for key in (('win-1', 'p-open'), ('win-2', 'p-open2')):
        assert by[key]['unjoined_reasons'] == ['carry_unregistered'] and by[key]['primary'] is False, key
    assert r5.population(joined)['primary_decided'] == before['primary_n']
    c = _count(w, recapture)
    targets = {(s_['scene_id'], o['id']): o for s_ in c['next_scenes'] for o in s_['objects']}
    assert ('s1', 'p-open') in targets and targets[('s1', 'p-open')]['window'] == ['win-1']
    assert ('s1', 'p-open2') in targets and targets[('s1', 'p-open2')]['window'] == ['win-2']
    assert ('win-1', 'p-open') not in targets and ('win-2', 'p-open2') not in targets
    ex = c['target_exclusions']
    assert ex.get('carry_unregistered') == 2 and not any(k.startswith('recaptured:') for k in ex)
    assert c['primary_n'] == before['primary_n'] and c['targets_remaining'] == before['targets_remaining']


def test_count_gate_fields_are_identical_with_an_empty_registry_and_without_one(tmp_path):
    """No recapture on disk: the lineage code path changes none of the six gated fields nor the PRIMARY rows."""
    w = _live_world(tmp_path)
    without = _count(w, tmp_path / 'no-such-dir')
    with_design = _count(w, _recapture_dir(tmp_path, [], design=DESIGN_TEXT))
    with_registry = _count(w, _recapture_dir(tmp_path, [_registry_row('p-open', 'win-never')]))      # a pointer to a window that does not exist
    gated = ('primary_n', 'primary_by_class', 'scene_ids', 'agent_authored_excluded', 'targets_total', 'targets_remaining', 'primary_rows')
    for c in (with_design, with_registry):
        assert {k: c[k] for k in gated} == {k: without[k] for k in gated}
    assert without['lineage']['design_sha256'] is None and with_design['lineage']['design_sha256'] == DESIGN_SHA
    old = next(o for s in with_registry['next_scenes'] for o in s['objects'] if o['id'] == 'p-open')
    assert old['window'] == ['win-never'], 'a registered window without a copy that joins only lists itself beside the old target'


def test_report_header_names_every_listing_and_unjoined_reason():
    assert r5.REASONS == ('not_in_snapshot', 'under_min_area', 'not_judged_image', 'carry_unverified', 'carry_design_mismatch',
                          'carry_snapshot_mismatch', 'carry_unregistered', 'carry_method')
    assert r5.NOT_COUNTED == ('not_live', 'agent_traced_geometry', 'duplicate_decision_on_recapture', 'ambiguous_carry', 'ambiguous_takeover', 'other')
    assert r5.CARRY_REFUSALS == r5.REASONS[3:]


def test_lineage_accepts_rows_and_transforms_behind_a_thresholds_unchanged_design_amendment_and_refuses_a_threshold_change(tmp_path):
    """Probe of 2026-10-01T07:49Z (.run/focus-recapture/capture-2026-10-01.log): an identity amendment appended to DESIGN.json
    moved its whole-file sha and the exact comparison refused every carried copy by name (carry_design_mismatch 80 /
    carry_unregistered 80, targets back to 3). The hop accepts a recorded sha that is the current sha or the previous_sha256
    of an amendment reached from the newest entry through thresholds_changed-false entries, names the hop
    (design_sha_via_chain) on the row and in the lineage summary, and still refuses a sha behind a threshold change or an
    unknown sha. The six gated fields never move."""
    w = _live_world(tmp_path)
    s = w['scenes']
    _window(s, 'win-1', [_copy('p-open', 'needs_adjustment', 'p-open')], transform=_transform())   # transform under DESIGN_SHA
    recapture = _recapture_dir(tmp_path, [_registry_row('p-open', 'win-1')])                       # row under DESIGN_SHA
    base = _count(w, recapture)
    current = {'design_sha_current': 1, 'design_sha_via_chain': 0, 'refused': 0}
    assert base['lineage']['design_sha256'] == DESIGN_SHA and base['lineage']['carried_joined'] == 1
    assert base['lineage']['carried_design_sha_match'] == {'transform': current, 'registry': current}
    assert base['target_exclusions'].get('recaptured:win-1') == 1
    gated = ('primary_n', 'primary_by_class', 'scene_ids', 'agent_authored_excluded', 'targets_total', 'targets_remaining', 'primary_rows')
    entry = {'at': '2026-10-01T08:00:00Z', 'field': 'identity', 'previous_sha256': DESIGN_SHA, 'thresholds_changed': False}
    identity = json.dumps({**json.loads(DESIGN_TEXT), 'identity': 'win-<hash16>', 'amendments': [entry]})
    new_sha = hashlib.sha256(identity.encode()).hexdigest()
    recapture = _recapture_dir(tmp_path, design=identity)                                          # same folder, rows kept
    lineage = r5.load_lineage(recapture)
    hop = {'match': 'design_sha_via_chain', 'hops_back': 1, 'amendment_index': 1, 'amendment_at': entry['at'], 'amendment_field': 'identity'}
    assert lineage['design_sha256'] == new_sha
    assert lineage['design_accepted'] == {new_sha: {'match': 'design_sha_current', 'hops_back': 0}, DESIGN_SHA: hop}
    assert r5.registered(lineage, ('s1', 'p-open'), 'win-1') is True and r5.registration(lineage, ('s1', 'p-open'), 'win-1') == hop
    live = r5.load_decisions(s)['live']['win-1']
    cf = _copy('p-open', 'needs_adjustment', 'p-open')['carried_from']
    assert r5.carry_refusals(cf, live, 'img-s1', True, lineage) == []
    assert r5.carry_refusals(cf, live, 'img-s1', True, new_sha) == ['carry_design_mismatch'], 'a bare sha in the lineage position keeps the exact rule'
    assert r5.carry_refusals(cf, live, 'img-s1', True, DESIGN_SHA) == []
    c = _count(w, recapture)
    assert {k: c[k] for k in gated} == {k: base[k] for k in gated}
    via = {'design_sha_current': 0, 'design_sha_via_chain': 1, 'refused': 0}
    assert c['lineage']['design_sha256'] == new_sha and c['lineage']['carried_joined'] == 1
    assert c['lineage']['carried_refused_by_reason']['carry_design_mismatch'] == 0 == c['lineage']['carried_refused_by_reason']['carry_unregistered']
    assert c['lineage']['carried_design_sha_match'] == {'transform': via, 'registry': via}
    assert c['lineage']['design_accepted'] == lineage['design_accepted']
    assert c['carried_rows'][0]['refusals'] == [] and c['carried_rows'][0]['design_sha_match'] == {'transform': hop, 'registry': hop}
    assert c['target_exclusions'].get('recaptured:win-1') == 1
    # the same entry as a THRESHOLD change: the probe's refusals by name, the old target back on its source scene
    threshold = json.dumps({**json.loads(identity), 'amendments': [{**entry, 'thresholds_changed': True}]})
    recapture = _recapture_dir(tmp_path, design=threshold)
    lineage = r5.load_lineage(recapture)
    assert set(lineage['design_accepted']) == {hashlib.sha256(threshold.encode()).hexdigest()}
    assert r5.registered(lineage, ('s1', 'p-open'), 'win-1') is False and r5.registration(lineage, ('s1', 'p-open'), 'win-1') is None
    assert r5.carry_refusals(cf, live, 'img-s1', False, lineage) == ['carry_design_mismatch', 'carry_unregistered']
    c2 = _count(w, recapture)
    assert {k: c2[k] for k in gated} == {k: base[k] for k in gated}
    refused = {'design_sha_current': 0, 'design_sha_via_chain': 0, 'refused': 1}
    assert c2['lineage']['carried_joined'] == 0
    assert c2['lineage']['carried_refused_by_reason']['carry_design_mismatch'] == 1 == c2['lineage']['carried_refused_by_reason']['carry_unregistered']
    assert c2['lineage']['carried_design_sha_match'] == {'transform': refused, 'registry': refused}
    assert c2['carried_rows'][0]['design_sha_match'] == {'transform': None, 'registry': None}
    assert c2['carried_rows'][0]['refusals'] == ['carry_design_mismatch', 'carry_unregistered']
    assert c2['target_exclusions'].get('carry_design_mismatch') == 1 and not any(k.startswith('recaptured:') for k in c2['target_exclusions'])
    targets = {(s_['scene_id'], o['id']): o for s_ in c2['next_scenes'] for o in s_['objects']}
    assert ('s1', 'p-open') in targets and targets[('s1', 'p-open')]['window'] == ['win-1'] and ('win-1', 'p-open') not in targets
    # an unknown sha (a chain that never names the recorded one) is refused the same way
    unknown = json.dumps({**json.loads(identity), 'amendments': [{**entry, 'previous_sha256': 'f' * 64}]})
    lineage = r5.load_lineage(_recapture_dir(tmp_path, design=unknown))
    assert r5.registered(lineage, ('s1', 'p-open'), 'win-1') is False
    assert r5.carry_refusals(cf, live, 'img-s1', True, lineage) == ['carry_design_mismatch']


# --- takeover: a whole-roof detection of the window's model stands in for the frozen slices it took over ----------------
#
# Reviewer direction (2026-10-02): "from these images, its all still happening, the orange bounding box cuts off so the object
# boundary looks bad". The orange line is the projected OLD tile edge; the red outline is the frozen slice carried as-is; the
# whole roof is a separate new detection D. TAKEOVER RULE (WINDOW.json amendment, mirrored in RULES.json): a carried copy C
# taken over by D becomes status superseded with replacement_ids [D.id]; D.replaces lists the copy ids it took over and stays
# candidate. The counter follows ONE replaces hop: D.replaces -> copy C -> C.carried_from -> frozen key. A reviewer decision on
# D counts for the frozen key of EVERY copy in D.replaces with D's class and provenance; the source decision still wins
# (duplicate_decision_on_recapture); a copy listed in two detections' replaces counts nowhere (ambiguous_takeover); a taken-over
# copy is not a target (superseded_by_takeover) and D is asked instead, once, listing the frozen keys it covers.


def _taken(oid, source_oid, by, **extra):
    """A carried copy as the takeover leaves it: status superseded, replacement_ids [D.id], carried_from and every other field kept."""
    return _copy(oid, 'superseded', source_oid, replacement_ids=[by], **extra)


def _detection(oid, status, replaces, **extra):
    """A building detection of the window's current model (no carried_from) that took over the listed copies."""
    return _obj(oid, status, replaces=list(replaces), **extra)


def _rows_of(joined, scene, oid):
    return sorted((r for r in joined['rows'] if (r['scene_id'], r['object_id']) == (scene, oid)), key=lambda r: str(r['frozen_key']))


def test_takeover_hop_counts_a_decision_on_the_detection_for_each_replaced_copy_frozen_key(tmp_path):
    w = _live_world(tmp_path)
    chat = {'human_decision': {'source': 'human:chat', 'fields': ['status']}}
    review = [_taken('c-open', 'p-open', 'd-roof'), _taken('c-open2', 'p-open2', 'd-roof', area=6.),
              _detection('d-roof', 'confirmed', ['c-open2', 'c-open'], area=31.),              # unsorted on disk: read sorted
              _taken('c-gone', 'p-gone', 'd-chat'), _detection('d-chat', 'rejected', ['c-gone'], provenance=chat)]
    _window(w['scenes'], 'win-1', review, transform=_transform(), live=['d-roof', 'd-chat'])
    rows = [_registry_row(o, 'win-1') for o in ('p-open', 'p-open2', 'p-gone')]
    lineage = r5.load_lineage(_recapture_dir(tmp_path, rows))
    decisions = r5.load_decisions(w['scenes'])
    facts = {(r['scene_id'], r['object_id']): r for r in decisions['rows']}
    d = facts[('win-1', 'd-roof')]
    assert (d['replaces'], d['replaces_held'], d['replaces_refused'], d['carried'], d['taken_over_by']) == \
        (['c-open', 'c-open2'], ['c-open', 'c-open2'], {}, False, [])
    c = facts[('win-1', 'c-open')]
    assert (c['taken_over_by'], c['replacement_ids'], c['cls'], c['stratum'], c['carried']) == \
        (['d-roof'], ['d-roof'], 'excluded', 'superseded_by_takeover', True)
    assert c['carried_from']['object_id'] == 'p-open', 'the copy keeps carried_from'
    joined = r5.join(w['context'], w['split'], decisions, lineage=lineage)
    one, two = _rows_of(joined, 'win-1', 'd-roof')
    assert one['frozen_key'] == ['s1', 'p-open'] and two['frozen_key'] == ['s1', 'p-open2'], 'one row per frozen key D covers'
    for row, ann, area, copy in ((one, 15, 10., 'c-open'), (two, 17, 6., 'c-open2')):
        assert (row['path'], row['ann_id'], row['snapshot_area_m2'], row['cls'], row['provenance_kind']) == ('P', ann, area, 'roof', 'ui')
        assert (row['live'], row['production'], row['source'], row['primary'], row['decided']) == (True, True, 'sam3-text-onnx', True, True)
        assert row['lineage']['takeover'] == {'copy_id': copy, 'claims': 1} and row['lineage']['refusals'] == []
        assert (row['lineage']['copies'], row['lineage']['source_decision_counted'], row['not_counted_reason']) == (1, False, None)
        assert row['lineage']['window_scene_id'] == 'win-1' and row['judged_scene'] is True and row['human_drawn'] is False
    copy_row, = _rows_of(joined, 'win-1', 'c-open')
    assert (copy_row['cls'], copy_row['stratum'], copy_row['decided'], copy_row['not_counted_reason']) == \
        ('excluded', 'superseded_by_takeover', False, None), 'the taken-over copy is not a decision'
    assert copy_row['frozen_key'] == ['s1', 'p-open'] and copy_row['path'] == 'P' and copy_row['lineage']['copies'] == 1
    assert 'takeover' not in copy_row['lineage'], 'a copy row carries the carry hop alone'
    gone, = _rows_of(joined, 'win-1', 'd-chat')
    assert (gone['frozen_key'], gone['cls'], gone['provenance_kind'], gone['live'], gone['production']) == \
        (['s1', 'p-gone'], 'not_roof', 'chat', False, False), "D's class and provenance, the frozen key's liveness"
    assert (gone['primary'], gone['not_counted_reason']) == (False, 'not_live')
    assert ('win-1', 'd-roof') not in {(r['scene_id'], r['object_id']) for r in joined['rows'] if r['lineage'] is None}, \
        'a detection with a held takeover is never also an ordinary window row'
    summary = r5.population(joined)
    assert summary['primary_decided'] == 6 and summary['by_class'] == {'roof': 4, 'not_roof': 2}, 'two frozen keys count through D'
    assert summary['by_provenance'] == {'ui': 6, 'chat': 0} and summary['scenes'] == 3
    assert summary['strata']['superseded_by_takeover'] == 3 and 'superseded' not in summary['strata']
    assert summary['joined_not_counted_by_reason']['not_live'] == 3 and summary['joined_not_counted_by_reason']['ambiguous_takeover'] == 0
    assert not any(r['stratum'] == 'superseded_by_takeover' for r in joined['rows'] if r['decided']), 'a taken-over copy never decides'
    assert all(r['lineage'] is None for r in joined['rows'] if not r['carried'] and not r.get('replaces_held')), 'direct rows unchanged'


def test_takeover_source_decision_wins_ambiguous_carry_holds_and_two_claimants_count_nowhere(tmp_path):
    w = _live_world(tmp_path)
    s = w['scenes']
    _window(s, 'win-1', [_taken('c-roof', 'p-roof', 'd-dup'), _detection('d-dup', 'rejected', ['c-roof']),         # p-roof decided at s1
                         _taken('c-open', 'p-open', 'd-amb'), _detection('d-amb', 'confirmed', ['c-open']),        # p-open also in win-2
                         _taken('c-open2', 'p-open2', 'd1'), _detection('d1', 'confirmed', ['c-open2']),
                         _detection('d2', 'candidate', ['c-open2'])],                                              # two claimants
            transform=_transform(), live=['d-dup', 'd-amb', 'd1', 'd2'])
    _window(s, 'win-2', [_copy('p-open', 'needs_adjustment', 'p-open')], transform=_transform())
    rows = [_registry_row(o, 'win-1') for o in ('p-roof', 'p-open', 'p-open2')] + [_registry_row('p-open', 'win-2')]
    recapture = _recapture_dir(tmp_path, rows)
    decisions = r5.load_decisions(s)
    assert {r['object_id']: r['taken_over_by'] for r in decisions['rows'] if r['scene_id'] == 'win-1' and r['carried']} == \
        {'c-roof': ['d-dup'], 'c-open': ['d-amb'], 'c-open2': ['d1', 'd2']}
    joined = r5.join(w['context'], w['split'], decisions, lineage=r5.load_lineage(recapture))
    by = {(r['scene_id'], r['object_id']): r for r in joined['rows']}
    dup = by[('win-1', 'd-dup')]
    assert (dup['cls'], dup['primary'], dup['not_counted_reason'], dup['lineage']['source_decision_counted']) == \
        ('not_roof', False, 'duplicate_decision_on_recapture', True)
    assert by[('s1', 'p-roof')]['primary'] is True, 'the source decision wins and counts once'
    amb = by[('win-1', 'd-amb')]
    assert (amb['primary'], amb['not_counted_reason'], amb['lineage']['copies']) == (False, 'ambiguous_carry', 2)
    d1, d2 = by[('win-1', 'd1')], by[('win-1', 'd2')]
    assert (d1['cls'], d1['primary'], d1['not_counted_reason'], d1['lineage']['takeover']) == \
        ('roof', False, 'ambiguous_takeover', {'copy_id': 'c-open2', 'claims': 2})
    assert (d2['cls'], d2['primary'], d2['not_counted_reason'], d2['lineage']['takeover']['claims']) == ('undecided', False, None, 2), \
        'an undecided claimant lists nothing as not counted, but the copy counts nowhere'
    taken = by[('win-1', 'c-open2')]
    assert (taken['stratum'], taken['taken_over_by'], taken['decided'], taken['not_counted_reason']) == \
        ('superseded_by_takeover', ['d1', 'd2'], False, None)
    summary = r5.population(joined)
    assert summary['primary_decided'] == 4 and summary['joined_not_counted_by_reason'] == \
        {'not_live': 2, 'agent_traced_geometry': 1, 'duplicate_decision_on_recapture': 1, 'ambiguous_carry': 1, 'ambiguous_takeover': 1, 'other': 0}
    c = _count(w, recapture)
    targets = {(s_['scene_id'], o['id']): o for s_ in c['next_scenes'] for o in s_['objects']}
    assert ('win-1', 'd1') not in targets and ('win-1', 'd2') not in targets and ('s1', 'p-open2') not in targets, 'asked nowhere'
    assert ('s1', 'p-open') in targets and targets[('s1', 'p-open')]['window'] == ['win-1', 'win-2'], 'the old target stays'
    assert ('win-1', 'd-amb') not in targets and ('win-1', 'd-dup') not in targets and ('win-1', 'c-roof') not in targets
    ex = c['target_exclusions']
    assert ex['ambiguous_takeover'] == 3 and ex['ambiguous_carry'] == 3 and ex['decided_on_source'] == 2, 'the copy and every claimant are listed'
    assert ex['recaptured:win-1'] == 1 and 'superseded_by_takeover' not in ex and 'superseded' not in ex
    assert c['primary_n'] == 4 and c['lineage']['ambiguous_takeover'] == 1 and c['lineage']['takeover_rows'] == 4
    assert c['lineage']['takeover_joined'] == 4 and c['lineage']['takeover_primary'] == 0 and c['lineage']['superseded_by_takeover'] == 3
    assert c['lineage']['replacement_mismatch'] == 1, 'c-open2 names d1 back, not d2: tallied, never a refusal (the hop reads D.replaces alone)'
    assert c['lineage']['carried_copies'] == 4 and c['lineage']['carried_joined'] == 4, 'the copies tally never counts a detection'


def test_targets_ask_the_detection_once_instead_of_the_taken_over_copies(tmp_path):
    w = _live_world(tmp_path)
    before = _count(w, _recapture_dir(tmp_path))
    review = [_taken('c-open', 'p-open', 'd-new'), _taken('c-open2', 'p-open2', 'd-new', area=6.),
              _detection('d-new', 'candidate', ['c-open', 'c-open2'], area=31., notes='whole roof on the new frame')]
    _window(w['scenes'], 'win-1', review, transform=_transform(), live=['d-new'])
    recapture = _recapture_dir(tmp_path, [_registry_row('p-open', 'win-1'), _registry_row('p-open2', 'win-1')])
    c = _count(w, recapture)
    targets = {(s['scene_id'], o['id']): o for s in c['next_scenes'] for o in s['objects']}
    assert ('win-1', 'd-new') in targets and sum(1 for k in targets if k == ('win-1', 'd-new')) == 1, 'asked once'
    d = targets[('win-1', 'd-new')]
    assert d['frozen_keys'] == [['s1', 'p-open'], ['s1', 'p-open2']] and d['replaces'] == ['c-open', 'c-open2']
    assert d['area_m2'] == 10. and d['url'] == r5.url_for('win-1', 'd-new') and d['note'] == 'whole roof on the new frame'
    assert d['source_scene_id'] == 's1' and 'source_object_id' not in d, 'the frozen keys it covers, not one source object'
    for key in (('s1', 'p-open'), ('s1', 'p-open2'), ('win-1', 'c-open'), ('win-1', 'c-open2')):
        assert key not in targets, key
    ex = c['target_exclusions']
    assert ex['recaptured:win-1'] == 2 and ex['superseded_by_takeover'] == 2 and 'superseded' not in ex
    assert c['targets_total'] == before['targets_total'] and c['targets_remaining'] == before['targets_remaining'] - 1
    assert c['primary_n'] == before['primary_n'] and c['primary_rows'] == before['primary_rows']
    lin = c['lineage']
    assert (lin['carried_copies'], lin['carried_joined'], lin['takeover_rows'], lin['takeover_joined'], lin['takeover_primary']) == (2, 2, 2, 2, 0)
    assert lin['superseded_by_takeover'] == 2 and lin['ambiguous_takeover'] == 0
    listed = {(o['scene_id'], o['id'], o['copy_id']): o for o in c['takeover_rows']}
    assert listed[('win-1', 'd-new', 'c-open')]['frozen_key'] == ['s1', 'p-open'] and listed[('win-1', 'd-new', 'c-open')]['class'] == 'undecided'
    assert listed[('win-1', 'd-new', 'c-open2')]['source_url'] == r5.url_for('s1', 'p-open2') and listed[('win-1', 'd-new', 'c-open2')]['claims'] == 1
    assert {(o['scene_id'], o['id']) for o in c['carried_rows']} == {('win-1', 'c-open'), ('win-1', 'c-open2')}, 'carried_rows lists copies alone'
    # the reviewer confirms D: both frozen keys count with D's class and provenance, D is never asked again
    folder = w['scenes'] / 'win-1'
    data = json.load(open(folder / 'review.json'))
    data['objects'][2]['status'] = 'confirmed'
    data['objects'][2]['geometry_reviewed'] = True
    data['revision'] = 3
    json.dump(data, open(folder / 'review.json', 'w'))
    c2 = _count(w, recapture)
    targets = {(s['scene_id'], o['id']) for s in c2['next_scenes'] for o in s['objects']}
    assert ('win-1', 'd-new') not in targets and c2['targets_remaining'] == before['targets_remaining'] - 2
    assert c2['primary_n'] == before['primary_n'] + 2 and c2['primary_by_class']['roof'] == before['primary_by_class']['roof'] + 2
    assert c2['target_exclusions']['decided'] == before['target_exclusions']['decided'] + 2
    counted = [o for o in c2['already_counted'] if (o['scene_id'], o['id']) == ('win-1', 'd-new')]
    assert [o['frozen_key'] for o in counted] == [['s1', 'p-open'], ['s1', 'p-open2']] and [o['replaces_copy'] for o in counted] == ['c-open', 'c-open2']
    assert ['win-1', 'd-new', 'confirmed', 10.0, 'sam3-text-onnx', 'P'] in c2['primary_rows'] and \
        ['win-1', 'd-new', 'confirmed', 6.0, 'sam3-text-onnx', 'P'] in c2['primary_rows']
    assert c2['lineage']['takeover_primary'] == 2 and 'win-1' in c2['scene_ids']
    # D re-kinded by the reviewer: a listing, never a decision; the keys are asked nowhere
    data['objects'][2]['kind'] = 'tree'
    data['objects'][2]['status'] = 'candidate'
    json.dump(data, open(folder / 'review.json', 'w'))
    c3 = _count(w, recapture)
    targets = {(s['scene_id'], o['id']) for s in c3['next_scenes'] for o in s['objects']}
    assert ('win-1', 'd-new') not in targets and ('s1', 'p-open') not in targets
    assert c3['target_exclusions']['kind_not_building'] == 2 and c3['target_exclusions']['superseded_by_takeover'] == 2
    assert c3['primary_n'] == before['primary_n'] and c3['targets_remaining'] == before['targets_remaining'] - 2
    assert all(o['class'] == 'skipped' for o in c3['takeover_rows']) and c3['lineage']['takeover_joined'] == 2
    assert c3['lineage']['replacement_mismatch'] == 0
    assert BANNED not in json.dumps(c3).lower()


def test_takeover_links_that_do_not_hold_leave_copies_and_detections_exactly_as_before(tmp_path):
    """A superseded copy no detection names is plain superseded; a detection whose replaces names a copy that is still
    needs_adjustment, a missing id, a non-carried object, or that is itself a carried copy, reaches no frozen key: the copy stays
    the target as before and the detection is an ordinary window object (not_judged_image when decided)."""
    w = _live_world(tmp_path)
    s = w['scenes']
    before = _count(w, _recapture_dir(tmp_path))
    review = [_copy('c-hand', 'superseded', 'p-open', replacement_ids=['elsewhere']),                 # superseded by hand, unnamed
              _copy('c-open2', 'needs_adjustment', 'p-open2'),                                        # still the target
              _detection('d-early', 'confirmed', ['c-open2', 'ghost', 'd-plain'], area=20.),         # names a live copy, a missing id, a plain object
              _obj('d-plain', 'candidate'),
              _copy('d-carried', 'confirmed', 'p-snap249', replaces=['c-hand'])]                      # a copy with replaces: no second hop
    _window(s, 'win-1', review, transform=_transform(), live=['d-early', 'd-plain'])
    recapture = _recapture_dir(tmp_path, [_registry_row(o, 'win-1') for o in ('p-open', 'p-open2', 'p-snap249')])
    decisions = r5.load_decisions(s)
    facts = {r['object_id']: r for r in decisions['rows'] if r['scene_id'] == 'win-1'}
    assert (facts['c-hand']['stratum'], facts['c-hand']['taken_over_by'], facts['c-hand']['replacement_ids']) == ('superseded', [], ['elsewhere'])
    assert (facts['d-early']['replaces_held'], facts['d-early']['replaces_refused']) == \
        ([], {'c-open2': 'copy_not_superseded', 'ghost': 'copy_missing', 'd-plain': 'copy_not_carried'})
    assert (facts['d-carried']['replaces_held'], facts['d-carried']['replaces_refused'], facts['d-carried']['carried']) == \
        ([], {'c-hand': 'replacer_carried'}, True)
    assert facts['d-plain']['replaces'] is None and facts['d-plain']['replaces_held'] == [] and facts['d-plain']['replaces_refused'] == {}
    joined = r5.join(w['context'], w['split'], decisions, lineage=r5.load_lineage(recapture))
    by = {(r['scene_id'], r['object_id']): r for r in joined['rows']}
    early = by[('win-1', 'd-early')]
    assert (early['lineage'], early['frozen_key'], early['path'], early['unjoined_reasons']) == (None, ['win-1', 'd-early'], None, ['not_judged_image'])
    assert sum(1 for r in joined['rows'] if r['object_id'] == 'd-early') == 1, 'no takeover row'
    carried = by[('win-1', 'd-carried')]
    assert carried['frozen_key'] == ['s1', 'p-snap249'] and carried['not_counted_reason'] == 'duplicate_decision_on_recapture'
    assert 'takeover' not in carried['lineage'] and carried['lineage']['copies'] == 1
    assert by[('win-1', 'c-hand')]['stratum'] == 'superseded' and by[('win-1', 'c-open2')]['cls'] == 'undecided'
    assert r5.population(joined)['primary_decided'] == 4 and r5.population(joined)['strata'].get('superseded_by_takeover') is None
    c = _count(w, recapture)
    targets = {(s_['scene_id'], o['id']): o for s_ in c['next_scenes'] for o in s_['objects']}
    assert ('win-1', 'c-open2') in targets and ('s1', 'p-open2') not in targets, 'the copy stays the target'
    assert ('win-1', 'd-early') not in targets and ('win-1', 'c-hand') not in targets and ('s1', 'p-open') not in targets
    ex = c['target_exclusions']
    assert ex['superseded'] == 1 and 'superseded_by_takeover' not in ex and ex['recaptured:win-1'] == 2 and ex['decided_on_source'] == 1
    assert c['lineage']['takeover_rows'] == 0 and c['lineage']['superseded_by_takeover'] == 0 and c['lineage']['carried_copies'] == 3
    assert c['primary_n'] == before['primary_n'] and c['targets_remaining'] == before['targets_remaining'] - 1
    assert any(o['id'] == 'd-early' and o['unjoined_reasons'] == ['not_judged_image'] for o in c['decided_not_judged'])


def test_takeover_detection_follows_the_ordinary_classification_rules(tmp_path):
    """D carries no agent text, so no marker is needed; when D does carry it the marker rules govern unchanged, a blocker still
    comes first, and D's decision is never read through the copy's notes."""
    w = _live_world(tmp_path)
    s = w['scenes']
    review = [_taken('c-open', 'p-open', 'd-agent', notes=AGENT_TEXT), _detection('d-agent', 'confirmed', ['c-open'], notes=AGENT_TEXT),
              _taken('c-open2', 'p-open2', 'd-fresh'), _detection('d-fresh', 'rejected', ['c-open2'], notes='[reviewer] not a roof ' + AGENT_TEXT),
              _taken('c-gone', 'p-gone', 'd-mach'),
              _detection('d-mach', 'confirmed', ['c-gone'], provenance={'source': 'machine:decision-policy-v2', 'human_confirmed': True})]
    _window(s, 'win-1', review, transform=_transform(), live=['d-agent', 'd-fresh', 'd-mach'])
    recapture = _recapture_dir(tmp_path, [_registry_row(o, 'win-1') for o in ('p-open', 'p-open2', 'p-gone')])
    joined = r5.join(w['context'], w['split'], r5.load_decisions(s), lineage=r5.load_lineage(recapture))
    by = {(r['scene_id'], r['object_id']): r for r in joined['rows']}
    agent = by[('win-1', 'd-agent')]
    assert (agent['cls'], agent['stratum'], agent['fresh'], agent['primary'], agent['frozen_key']) == \
        ('excluded', 'agent_authored_excluded', 'no_marker', False, ['s1', 'p-open'])
    fresh = by[('win-1', 'd-fresh')]
    assert (fresh['cls'], fresh['fresh'], fresh['primary'], fresh['provenance_kind'], fresh['frozen_key']) == \
        ('not_roof', 'valid', True, 'ui', ['s1', 'p-open2'])
    mach = by[('win-1', 'd-mach')]
    assert (mach['cls'], mach['stratum'], mach['primary']) == ('excluded', 'machine_label_human_confirmed', False)
    assert by[('win-1', 'c-open')]['stratum'] == 'agent_authored_excluded', "the copy's own notes classify the copy, never D"
    assert r5.population(joined)['primary_decided'] == 5 and r5.population(joined)['by_class'] == {'roof': 2, 'not_roof': 3}
    c = _count(w, recapture)
    targets = {(s_['scene_id'], o['id']) for s_ in c['next_scenes'] for o in s_['objects']}
    assert not targets & {('win-1', 'd-agent'), ('win-1', 'd-fresh'), ('win-1', 'd-mach'), ('s1', 'p-open'), ('s1', 'p-open2')}
    ex = c['target_exclusions']
    assert ex['agent_noted'] == 1 and ex['superseded_by_takeover'] == 2 and ex['not_live'] == 1 and 'machine_label' not in ex, \
        "p-gone is not live at its source: its key is excluded before its copy or claimant is read, as before the hop"
    assert [(o['id'], o['frozen_key'], o['class']) for o in c['already_counted'] if o['scene_id'] == 'win-1'] == \
        [('d-fresh', ['s1', 'p-open2'], 'not_roof')], "the reviewer's marked decision on D counts at the frozen key"
    assert any(o['id'] == 'd-agent' for o in c['agent_authored_other']), 'listed for a fresh decision like any agent-noted object'


def test_precision_block_keeps_the_frozen_keys_of_one_detection_apart(tmp_path):
    w = _live_world(tmp_path)
    review = [_taken('c-open', 'p-open', 'd-roof'), _taken('c-open2', 'p-open2', 'd-roof'), _detection('d-roof', 'confirmed', ['c-open', 'c-open2'])]
    _window(w['scenes'], 'win-1', review, transform=_transform(), live=['d-roof'])
    lineage = r5.load_lineage(_recapture_dir(tmp_path, [_registry_row('p-open', 'win-1'), _registry_row('p-open2', 'win-1')]))
    joined = r5.join(w['context'], w['split'], r5.load_decisions(w['scenes']), lineage=lineage)
    preds = [_pred(arm, 1, .9, pseudo_overlaps=[{'id': 15, 'iou': .8}]) for arm in ('base', 'ctrl', 'tuned')]      # p-open hit, p-open2 not
    block = r5.precision_block(w['context'], joined['rows'], preds, {a: .45 for a in w['context']['arms']}, 't045', draws=50)
    facts = [f for f in block['facts'] if (f['scene_id'], f['object_id']) == ('win-1', 'd-roof')]
    assert [(f['ann_id'], f['snapshot_area_m2'], f['arms']['tuned']['detected']) for f in facts] == [(15, 10., True), (17, 6., False)]
    assert all(f['url'] == r5.url_for('win-1', 'd-roof') and f['class'] == 'roof' for f in facts)
    assert block['n_rule']['N'] == 6 and block['object_counts']['tuned']['roof'] == {'decided': 4, 'detected': 1}


# --- the two RULES fact columns and the seven facts_columns_not_filters (2026-10-05) ----------------------------------------
# RULES population_primary: "a window-scene object without carried_from is listed decision_on_body_not_target when it covers at
# least 0.8 of a re-projected frozen slice's own area, and lineage_agreement on the carried copy records whether that whole-roof
# decision agrees; both are fact columns." Facts are recorded, never filters: no count, class, share or target may move.

SQ = lambda x0, y0, x1, y1: [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]      # noqa: E731


def _add_rings(scenes, sid, rings):
    """Give the live latest.json objects of a fixture scene the pixel rings the slice re-projection reads."""
    path = scenes / sid / 'latest.json'
    latest = json.load(open(path))
    for o in latest['objects']:
        if o['id'] in rings:
            o['ring'] = rings[o['id']]
    json.dump(latest, open(path, 'w'))


def _block_preds():
    return [_pred(arm, 1, .9, pseudo_overlaps=[{'id': 11, 'iou': .8}]) for arm in ('base', 'ctrl', 'tuned')]


def test_block_facts_rows_carry_all_seven_facts_columns_and_the_two_lineage_facts(tmp_path):
    w = _live_world(tmp_path)
    joined = r5.join(w['context'], w['split'], r5.load_decisions(w['scenes']))
    block = r5.precision_block(w['context'], joined['rows'], _block_preds(), {a: .45 for a in w['context']['arms']}, 't045')
    rules = json.loads(RULES5.read_text())['human_precision']['facts_columns_not_filters']
    assert list(r5.FACTS_COLUMNS) == rules == ['review_area_m2', 'model_signature_match', 'image_sha_match', 'geometry_reviewed',
                                               'review_revision', 'review_updated_at', 'review_sha256']
    assert block['facts'], 'the fixture has facts rows'
    for f in block['facts']:
        assert set(r5.FACTS_COLUMNS) <= set(f), f"facts row {f['object_id']} lacks {set(r5.FACTS_COLUMNS) - set(f)}"
        assert set(r5.LINEAGE_FACTS) | {'body_covers', 'lineage_agreement_detail'} <= set(f)
        assert f['review_sha256'] == hashlib.sha256((w['scenes'] / f['scene_id'] / 'review.json').read_bytes()).hexdigest()
        assert f['review_updated_at'] == '2026-09-30T11:00:00.000Z' and f['review_revision'] == 3
        assert f['model_signature_match'] is True and f['image_sha_match'] is True
        assert f['geometry_reviewed'] is (f['status'] == 'confirmed'), 'the saved flag, as the fixture writes it'
        assert (f['decision_on_body_not_target'], f['body_covers'], f['lineage_agreement'], f['lineage_agreement_detail']) == \
            (False, [], None, None), 'no window in this world: the defaults, recorded on every row'
    assert set(r5.LINEAGE_FACTS) == {'decision_on_body_not_target', 'lineage_agreement'}
    json.dumps(block)
    assert BANNED not in json.dumps(block)


def test_decision_on_body_not_target_reads_cover_of_the_reprojected_slice_own_area(tmp_path):
    w = _live_world(tmp_path)
    _add_rings(w['scenes'], 's1', {'p-open': SQ(0, 0, 10, 10), 'p-open2': SQ(20, 0, 30, 10)})   # the frozen slices in the source frame
    review = [_copy('c-open', 'confirmed', 'p-open', ring=SQ(10, 20, 20, 30)),                    # the carry's own projection (+10, +20)
              _copy('c-open2', 'rejected', 'p-open2', ring=SQ(30, 20, 32, 22), geometry_reviewed=True),   # the reviewer shrank this copy
              _copy('c-gone', 'confirmed', 'p-gone', ring=SQ(50, 50, 60, 60)),                     # p-gone is not live in s1
              _obj('d-85', 'confirmed', ring=SQ(10, 20, 18.5, 30)),                                # 85 of the slice's 100 px
              _obj('d-50', 'rejected', ring=SQ(30, 20, 35, 30)),                                   # 50 of 100 (100 % of the edited copy ring)
              _obj('d-gone', 'confirmed', ring=SQ(50, 50, 60, 60)),
              _obj('d-none', 'confirmed'),                                                          # no ring: nothing to cover with
              _obj('tree', 'confirmed', kind='tree', ring=SQ(10, 20, 20, 30))]                     # not a building: no listing
    _window(w['scenes'], 'win-1', review, transform=_transform(), live=['d-85', 'd-50', 'd-gone', 'd-none'])
    rows = {(r['scene_id'], r['object_id']): r for r in r5.load_decisions(w['scenes'])['rows']}
    d85 = rows[('win-1', 'd-85')]
    assert d85['decision_on_body_not_target'] is True and d85['body_cover_max'] == .85
    assert d85['body_covers'] == [{'copy_id': 'c-open', 'frozen_key': ['s1', 'p-open'], 'cover': .85, 'slice_ring': 'reprojected'}]
    d50 = rows[('win-1', 'd-50')]
    assert (d50['decision_on_body_not_target'], d50['body_covers'], d50['body_cover_max']) == (False, [], .5), \
        "the slice is the re-projected frozen ring, not the copy's edited ring (which d-50 covers entirely)"
    gone = rows[('win-1', 'd-gone')]
    assert gone['decision_on_body_not_target'] is True and gone['body_covers'] == \
        [{'copy_id': 'c-gone', 'frozen_key': ['s1', 'p-gone'], 'cover': 1., 'slice_ring': 'copy_ring'}], 'source not live: the copy ring'
    assert (rows[('win-1', 'd-none')]['decision_on_body_not_target'], rows[('win-1', 'd-none')]['body_cover_max']) == (False, None)
    assert (rows[('win-1', 'tree')]['decision_on_body_not_target'], rows[('win-1', 'tree')]['body_covers']) == (False, [])
    for cid in ('c-open', 'c-open2', 'c-gone'):
        assert rows[('win-1', cid)]['decision_on_body_not_target'] is False, 'a carried copy is never a body'
    assert all(r['decision_on_body_not_target'] is False and r['body_cover_max'] is None for k, r in rows.items() if k[0] == 's1')
    listing = r5.lineage_listing({'rows': list(rows.values())})
    assert [o['id'] for o in listing['decision_on_body_not_target']] == ['d-85', 'd-gone'] and listing['body_cover_min'] == .8
    assert [o['id'] for o in listing['lineage_agreement']] == ['c-gone', 'c-open', 'c-open2']
    # a fact, never a class or a join: the bodies stay what the ordinary rules make them and the copies read as before
    assert (d85['cls'], d50['cls'], d85['carried'], rows[('win-1', 'c-open')]['cls'], rows[('win-1', 'c-open2')]['cls']) == \
        ('roof', 'not_roof', False, 'roof', 'not_roof')


def test_lineage_agreement_compares_the_slice_decision_with_the_source_and_the_whole_roof_decision(tmp_path):
    w = _live_world(tmp_path)
    _add_rings(w['scenes'], 's1', {'p-open': SQ(0, 0, 10, 10)})
    review = [_copy('c-agree', 'confirmed', 'p-roof'),                        # source p-roof confirmed: roof = roof
              _copy('c-differ', 'confirmed', 'p-not'),                        # source p-not rejected: roof != not_roof
              _copy('c-open', 'confirmed', 'p-open', ring=SQ(10, 20, 20, 30)),   # source p-open has no review object: undecided
              _obj('b-open', 'confirmed', ring=SQ(10, 20, 19, 30)),           # a body covering 0.9 of c-open's slice, decided roof
              _taken('c-taken', 'p-snap249', 'd-yes'), _detection('d-yes', 'confirmed', ['c-taken']),     # D roof = source roof
              _taken('c-taken2', 'p-snap251', 'd-no'), _detection('d-no', 'confirmed', ['c-taken2']),     # D roof != source not_roof
              _taken('c-two', 'p-unlisted', 'd-a'), _detection('d-a', 'confirmed', ['c-two']), _detection('d-b', 'rejected', ['c-two']),
              _copy('c-undecided', 'needs_adjustment', 'p-unjudged', source='s3')]   # carried_undecided against a rejected source
    _window(w['scenes'], 'win-1', review, transform=_transform(), live=['b-open', 'd-yes', 'd-no', 'd-a', 'd-b'])
    rows = {(r['scene_id'], r['object_id']): r for r in r5.load_decisions(w['scenes'])['rows']}
    get = lambda oid: rows[('win-1', oid)]                                      # noqa: E731
    agree = get('c-agree')
    assert agree['lineage_agreement'] is True
    assert agree['lineage_agreement_detail']['reference'] == {'from': 'copy', 'object_id': 'c-agree', 'class': 'roof'}
    assert agree['lineage_agreement_detail']['source'] == {'scene_id': 's1', 'object_id': 'p-roof', 'review_read': True, 'found': True,
                                                            'status': 'confirmed', 'class': 'roof', 'stratum': None, 'same_class': True,
                                                            'agrees': True}
    assert agree['lineage_agreement_detail']['copy'] == {'status': 'confirmed', 'class': 'roof', 'stratum': None, 'taken_over_by': []}
    assert agree['lineage_agreement_detail']['takeover'] == [] and agree['lineage_agreement_detail']['body'] == []
    differ = get('c-differ')
    assert differ['lineage_agreement'] is False and differ['lineage_agreement_detail']['source']['class'] == 'not_roof'
    assert differ['lineage_agreement_detail']['source']['same_class'] is False and differ['lineage_agreement_detail']['source']['agrees'] is False
    opened = get('c-open')
    assert opened['lineage_agreement_detail']['source'] == {'scene_id': 's1', 'object_id': 'p-open', 'review_read': True, 'found': False,
                                                             'status': None, 'class': 'undecided', 'stratum': None, 'same_class': False,
                                                             'agrees': None}, \
        'an undecided source is no decision to compare (same_class records the strict reading)'
    assert opened['lineage_agreement_detail']['body'] == [{'object_id': 'b-open', 'status': 'confirmed', 'class': 'roof', 'stratum': None,
                                                            'cover': .9, 'same_class': True, 'agrees': True}]
    assert opened['lineage_agreement'] is True, 'the whole-roof decision on the covering body agrees'
    assert get('b-open')['decision_on_body_not_target'] is True and get('b-open')['lineage_agreement'] is None, 'a body is not a copy'
    taken = get('c-taken')
    assert taken['lineage_agreement'] is True and taken['cls'] == 'excluded'
    assert taken['lineage_agreement_detail']['reference'] == {'from': 'takeover', 'object_id': 'd-yes', 'class': 'roof'}
    assert taken['lineage_agreement_detail']['takeover'] == [{'object_id': 'd-yes', 'status': 'confirmed', 'class': 'roof', 'stratum': None,
                                                              'cover': None}], 'no ring on d-yes: its cover of the slice is unknown'
    assert taken['lineage_agreement_detail']['copy'] == {'status': 'superseded', 'class': 'excluded', 'stratum': 'superseded_by_takeover',
                                                         'taken_over_by': ['d-yes']}
    assert taken['lineage_agreement_detail']['source']['class'] == 'roof' and taken['lineage_agreement_detail']['source']['agrees'] is True
    taken2 = get('c-taken2')
    assert taken2['lineage_agreement'] is False and taken2['lineage_agreement_detail']['source']['class'] == 'not_roof'
    two = get('c-two')
    assert two['lineage_agreement'] is None and two['lineage_agreement_detail']['reference'] == {'from': 'ambiguous_takeover', 'object_id': None,
                                                                                                 'class': None}
    assert [t['object_id'] for t in two['lineage_agreement_detail']['takeover']] == ['d-a', 'd-b']
    assert two['lineage_agreement_detail']['source'] == {'scene_id': 's1', 'object_id': 'p-unlisted', 'review_read': True, 'found': True,
                                                          'status': 'confirmed', 'class': 'roof', 'stratum': None, 'same_class': None,
                                                          'agrees': None}
    undecided = get('c-undecided')
    assert (undecided['cls'], undecided['lineage_agreement']) == ('undecided', None)
    assert undecided['lineage_agreement_detail']['source'] == {'scene_id': 's3', 'object_id': 'p-unjudged', 'review_read': True, 'found': True,
                                                                'status': 'rejected', 'class': 'not_roof', 'stratum': None, 'same_class': False,
                                                                'agrees': None}
    assert undecided['lineage_agreement_detail']['copy']['stratum'] == 'carried_undecided'
    for d in ('d-yes', 'd-no', 'd-a', 'd-b'):
        assert get(d)['lineage_agreement'] is None and get(d)['lineage_agreement_detail'] is None, 'lineage_agreement sits on the copy'
    assert all(r['lineage_agreement'] is None for k, r in rows.items() if k[0] != 'win-1')
    listing = r5.lineage_listing({'rows': list(rows.values())})
    assert listing['lineage_agreement_tally'] == {'False': 2, 'None': 2, 'True': 3}   # c-agree, c-open (body), c-taken / c-differ, c-taken2 / c-two, c-undecided
    assert [(o['id'], o['lineage_agreement'], o['frozen_key']) for o in listing['lineage_agreement']][:2] == \
        [('c-agree', True, ['s1', 'p-roof']), ('c-differ', False, ['s1', 'p-not'])]
    json.dumps(listing)


def test_the_fact_columns_change_no_count_no_class_no_target_and_no_share(tmp_path, monkeypatch):
    """Regression: a block read with the facts and the same block read with lineage_facts a no-op (no fact column on any row)
    give byte-identical population, classes, targets, block counts and count() output outside the facts themselves."""
    w = _live_world(tmp_path)
    _add_rings(w['scenes'], 's1', {'p-open': SQ(0, 0, 10, 10), 'p-open2': SQ(20, 0, 30, 10), 'p-roof': SQ(40, 0, 50, 10)})
    chat = {'human_decision': {'source': 'human:chat', 'fields': ['status']}}
    review = [_taken('c-open', 'p-open', 'd-roof', ring=SQ(10, 20, 20, 30)), _taken('c-open2', 'p-open2', 'd-roof', area=6., ring=SQ(30, 20, 40, 30)),
              _detection('d-roof', 'confirmed', ['c-open2', 'c-open'], area=31., ring=SQ(10, 20, 40, 30)),       # covers both slices fully
              _taken('c-gone', 'p-gone', 'd-chat'), _detection('d-chat', 'rejected', ['c-gone'], provenance=chat),
              _copy('c-roof', 'rejected', 'p-roof', ring=SQ(50, 20, 60, 30)),                                   # disagrees with the source
              _obj('b-roof', 'confirmed', ring=SQ(50, 20, 59, 30)), _obj('b-far', 'rejected', ring=SQ(200, 200, 210, 210))]
    _window(w['scenes'], 'win-1', review, transform=_transform(), live=['d-roof', 'd-chat', 'b-roof', 'b-far'])
    recapture = _recapture_dir(tmp_path, [_registry_row(o, 'win-1') for o in ('p-open', 'p-open2', 'p-gone', 'p-roof')])
    lineage = r5.load_lineage(recapture)
    cuts = {a: .45 for a in w['context']['arms']}
    facts_keys = set(r5.LINEAGE_FACTS) | {'body_covers', 'body_cover_max', 'lineage_agreement_detail'}

    def read():
        decisions = r5.load_decisions(w['scenes'])
        joined = r5.join(w['context'], w['split'], decisions, lineage=lineage)
        block = r5.precision_block(w['context'], joined['rows'], _block_preds(), cuts, 't045', draws=50)
        pool, remaining, excluded = r5.targets(joined, decisions, lineage)
        rows = sorted((r['scene_id'], r['object_id'], str(r['frozen_key']), r['cls'], r['stratum'], r['primary'], r['decided'], r['human_drawn'],
                       r['not_counted_reason'], tuple(r['unjoined_reasons']), r['path']) for r in joined['rows'])
        return decisions, joined, block, (pool, remaining, excluded), rows

    with_facts = read()
    assert any(r.get('decision_on_body_not_target') for r in with_facts[0]['rows']) and \
        any(r.get('lineage_agreement') is False for r in with_facts[0]['rows']), 'the facts are present and not all trivial'
    c_with = _count(w, recapture)
    monkeypatch.setattr(r5, 'lineage_facts', lambda *args, **kwargs: None)
    without = read()
    assert not any(k in r for r in without[0]['rows'] for k in facts_keys), 'the no-op leaves no fact column on any row'
    c_without = _count(w, recapture)
    dump = lambda v: json.dumps(v, sort_keys=True, default=str)                                   # noqa: E731
    assert dump(r5.population(with_facts[1])) == dump(r5.population(without[1]))
    assert with_facts[4] == without[4], 'every row keeps its class, stratum, primary flag, listing and path'
    assert dump(with_facts[3]) == dump(without[3]), 'targets, pool and exclusions are identical'
    strip = lambda block: {k: v for k, v in block.items() if k != 'facts'}                        # noqa: E731
    assert dump(strip(with_facts[2])) == dump(strip(without[2])), 'N rule, object counts, shares, pairs and prediction level identical'
    facts_with = [{k: v for k, v in f.items() if k not in facts_keys} for f in with_facts[2]['facts']]
    facts_without = [{k: v for k, v in f.items() if k not in facts_keys} for f in without[2]['facts']]
    assert dump(facts_with) == dump(facts_without), 'the facts rows differ only in the fact columns'
    assert all(f[k] is None for f in without[2]['facts'] for k in facts_keys if k != 'body_cover_max')
    ignore = {'computed_at', 'computed_at_local', 'lineage_facts'}
    assert dump({k: v for k, v in c_with.items() if k not in ignore}) == dump({k: v for k, v in c_without.items() if k not in ignore}), \
        'count(): N, classes, provenance, scenes, targets, strata, carried_rows and takeover_rows identical'
    assert c_with['primary_n'] == 6 and c_with['primary_by_class'] == {'roof': 4, 'not_roof': 2}, \
        'the fixture counts: the four base decisions and D once per slice (c-roof is a duplicate of a counted source, c-gone not live)'
    # c-open / c-open2: D roof against undecided sources (None); c-gone: D not_roof against a confirmed source; c-roof: rejected against
    # a confirmed source and a confirmed covering body (both False)
    assert c_with['lineage_facts']['lineage_agreement_tally'] == {'False': 2, 'None': 2} and \
        [o['id'] for o in c_with['lineage_facts']['decision_on_body_not_target']] == ['b-roof', 'd-roof']
    assert c_without['lineage_facts'] == {**c_with['lineage_facts'], 'decision_on_body_not_target': [], 'lineage_agreement': [],
                                          'lineage_agreement_tally': {}, 'reference_from_tally': {}, 'source_tally': {}}
    taken = {o['id']: o for o in c_with['lineage_facts']['lineage_agreement']}
    assert [(t['object_id'], t['cover']) for t in taken['c-open']['detail']['takeover']] == [('d-roof', 1.)], "the taker's own cover of the slice"
    assert c_with['lineage_facts']['source_tally'] == {'None/undecided/None': 2, 'confirmed/roof/None': 2}, \
        'p-open / p-open2 have no s1 review object; p-gone and p-roof are confirmed there'
    assert c_with['facts_columns'] == list(r5.FACTS_COLUMNS)


def test_report_per_object_table_shows_the_seven_facts_columns_and_the_two_lineage_facts(tmp_path):
    """HUMAN-PRECISION.md's per-object table renders, between path and the arms, every FACTS_COLUMNS name and both LINEAGE_FACTS
    exactly as recorded on the block file's facts rows (recorded, never filters: every facts row is a table row, none is added or
    dropped by a fact), and the cell formats read yes / no / - , slices + max cover, agrees / differs / -."""
    assert r5._fact_cells({'review_area_m2': 12.345, 'model_signature_match': True, 'image_sha_match': False, 'geometry_reviewed': None,
                           'review_revision': 7, 'review_updated_at': '2026-10-01T00:00:00Z', 'review_sha256': 'abc',
                           'decision_on_body_not_target': True, 'body_covers': [{'cover': .85}, {'cover': .9}], 'lineage_agreement': False}) == \
        ['12.3', 'yes', 'no', '-', '7', '2026-10-01T00:00:00Z', 'abc', 'yes (2 slices, max cover 0.90)', 'differs']
    assert r5._fact_cells({'decision_on_body_not_target': True, 'body_covers': [{'cover': 1.}], 'lineage_agreement': True})[7:] == \
        ['yes (1 slice, max cover 1.00)', 'agrees']
    assert r5._fact_cells({'decision_on_body_not_target': False, 'lineage_agreement': None}) == ['-'] * 7 + ['no', '-']
    assert r5._fact_cells({}) == ['-'] * 9, 'a facts row of an earlier batch without the keys renders as not recorded'
    w, sam3, root3 = _rejudge_world(tmp_path)
    out, rules_path = tmp_path / 'round5', _rules_file(tmp_path)
    r5.rejudge([root3], rules_path, out, scenes_root=w['scenes'], repo=tmp_path)
    text = r5.report(out / 'human-precision.jsonl', out / 'HUMAN-PRECISION.md', repo=tmp_path)
    assert 'human_precision.facts_columns_not_filters' in text and 'recorded, never filters' in text and BANNED not in text.lower()
    headers = [line for line in text.splitlines() if line.startswith('| scene | object | class | prov | snapshot m² | path | ')]
    assert headers, 'the per-object table is printed'
    table = [line for line in text.splitlines() if line.startswith('| ') and line not in headers and not line.startswith('| ---')]
    for block_name in ('v2', 'v1'):
        block = json.load(open(out / f'human-precision/r3-a-seed1/{block_name}-t045.json'))
        header = next(h for h in headers if [c.strip() for c in h.strip('|').split('|')][15:] == block['arms'])
        cols = [c.strip() for c in header.strip('|').split('|')]
        assert cols[:6] == ['scene', 'object', 'class', 'prov', 'snapshot m²', 'path']
        assert cols[6:15] == list(r5.FACTS_COLUMNS) + list(r5.LINEAGE_FACTS) == \
            ['review_area_m2', 'model_signature_match', 'image_sha_match', 'geometry_reviewed', 'review_revision', 'review_updated_at',
             'review_sha256', 'decision_on_body_not_target', 'lineage_agreement']
        assert block['facts'], 'the fixture has facts rows in this block'
        for f in block['facts']:
            cells = r5._fact_cells(f)
            assert cells[6] == f['review_sha256'] == hashlib.sha256((w['scenes'] / f['scene_id'] / 'review.json').read_bytes()).hexdigest()
            assert cells[5] == f['review_updated_at'] and cells[7:] == ['no', '-'], 'no window in this world: the defaults, rendered'
            prefix = f"| {f['scene_id']} | {f['object_id']} | {f['class'].replace('_', '-')} | {f['provenance_kind']} | "
            rows = [line for line in table if line.startswith(prefix) and ' | '.join(cells) in line]
            assert rows, f'{prefix} with its nine fact cells is a table row'
        n_rows = sum(1 for line in table if any(line.startswith(f"| {f['scene_id']} | {f['object_id']} | ") for f in block['facts']))
        assert n_rows >= len(block['facts']), 'every facts row is a table row (no fact filters a row out)'


# --- body carry: a carried whole-roof body as the takeover replacer (reviewer "yes to defaults" 2026-10-06, census card item 2) ----

BODY_NOTE = ('Whole-roof body carried from n1 through the georeferenced frame (verified transform); it replaces the frame-cut slice. '
             'Decide roof / not a roof on this image.')


def _body(oid, replaces, *, source='n1', source_oid='big', status='candidate', area=120., role='whole_roof_body', **extra):
    """A whole-roof body as scene_focus body-carry writes it: a carried copy of role whole_roof_body whose replaces (and the
    editor's derived_from reciprocal) name the one slice it took over; status candidate until the reviewer decides it."""
    carried = {'scene_id': source, 'model_signature': f'sig-{source}', 'object_id': source_oid, 'status': 'candidate',
               'geometry_reviewed': False, 'iou_with_new': 0, 'matched_id': None, 'method': 'georeferenced-affine', 'role': role}
    return _obj(oid, status, area=area, source='carried-body-v1', carried_from=carried, replaces=list(replaces),
                derived_from=list(replaces), notes=extra.pop('notes', BODY_NOTE), **extra)


def _body_row(window, body_id, slice_id, *, source='n1', source_oid='big', verified=True, shift=.01, ncc=.9, **extra):
    """One registry row of role body_carry as the lot-29362 live write appended it (residual, cover, the slice it replaced)."""
    row = {'version': 'windows-registry-v1', 'role': 'body_carry', 'window_scene_id': window, 'source_scene_id': source,
           'object_id': source_oid, 'carried_object_id': body_id, 'slice_object_id': slice_id, 'frame_transform_verified': verified,
           'frame_transform_role': 'frame_cut_target', 'residual': {'shift_px': [.1, .1], 'shift_m': shift, 'ncc': ncc, 'samples': 1000},
           'slice_cover': .95, 'body_area_m2': 120., 'design_sha256': DESIGN_SHA, 'window_sha256': 'w' * 64,
           'written_at': '2026-10-06T00:00:00Z'}
    row.update(extra)
    return row


def _targets(c):
    return {(sc['scene_id'], o['id']): o for sc in c['next_scenes'] for o in sc['objects']}


def _append_rows(recapture, rows, name='windows-2026-10-06.jsonl'):
    with open(recapture / name, 'a') as handle:
        handle.writelines(json.dumps(r) + '\n' for r in rows)


def test_body_carry_replacer_is_read_as_the_window_detection_and_asked_for_the_slice_key(tmp_path):
    """A verified body_carry replacer over an undecided slice: the hop follows body.replaces -> slice -> carried_from -> frozen key
    exactly as for the window's own detection; the body is the asked target for the slice's key (targets_remaining and N
    unchanged by the carry itself); the body is still listed as its own refused carried row; a decision on the body counts
    at the slice's frozen key."""
    w = _live_world(tmp_path)
    s = w['scenes']
    _window(s, 'win-1', [_copy('c-open', 'needs_adjustment', 'p-open')], transform=_transform())    # the slice alone: asked as before
    recapture = _recapture_dir(tmp_path, [_registry_row('p-open', 'win-1')])
    before = _count(w, recapture)
    assert ('win-1', 'c-open') in _targets(before)
    _window(s, 'win-1', [_taken('c-open', 'p-open', 'b-body'), _body('b-body', ['c-open'])], transform=_transform())
    _append_rows(recapture, [_body_row('win-1', 'b-body', 'c-open')])
    lineage = r5.load_lineage(recapture)
    assert [r['role'] for r in lineage['body_carry'][('win-1', 'b-body')]] == ['body_carry']
    assert lineage['design_values'] == {'max_shift_m': .7, 'min_ncc': .35} and lineage['registry_rows'] == 2
    assert r5.registered(lineage, ('n1', 'big'), 'win-1') is False, 'a body_carry row is not a carried_source pointer'
    decisions = r5.load_decisions(s, lineage=lineage)
    facts = {(r['scene_id'], r['object_id']): r for r in decisions['rows']}
    b = facts[('win-1', 'b-body')]
    assert (b['replaces_held'], b['replaces_refused'], b['carried'], b['replacer_kind'], b['cls']) == \
        (['c-open'], {}, True, 'body_carry', 'undecided')
    assert b['body_registration']['refusal'] is None and b['body_registration']['residual'] == {'shift_m': .01, 'ncc': .9}
    assert b['body_registration']['design_sha_match'] == {'match': 'design_sha_current', 'hops_back': 0}
    c = facts[('win-1', 'c-open')]
    assert (c['taken_over_by'], c['stratum'], c['cls'], c['replacer_kind']) == (['b-body'], 'superseded_by_takeover', 'excluded', None)
    joined = r5.join(w['context'], w['split'], decisions, lineage=lineage)
    own, over = _rows_of(joined, 'win-1', 'b-body')
    assert over['frozen_key'] == ['s1', 'p-open'] and over['path'] == 'P' and over['snapshot_area_m2'] == 10.
    assert over['lineage']['takeover'] == {'copy_id': 'c-open', 'claims': 1, 'replacer': 'body_carry', 'registration': b['body_registration']}
    assert (over['lineage']['refusals'], over['lineage']['copies'], over['cls'], over['decided'], over['primary']) == ([], 1, 'undecided', False, True), \
        'in the primary population at the slice key, undecided'
    assert own['frozen_key'] == ['n1', 'big'] and 'takeover' not in own['lineage'] and 'carry_unregistered' in own['lineage']['refusals'], \
        'the body is still its own carried row, refused as before'
    c = _count(w, recapture)
    targets = _targets(c)
    assert ('win-1', 'b-body') in targets and ('win-1', 'c-open') not in targets and ('s1', 'p-open') not in targets
    assert targets[('win-1', 'b-body')]['replacer'] == 'body_carry' and targets[('win-1', 'b-body')]['frozen_keys'] == [['s1', 'p-open']]
    assert targets[('win-1', 'b-body')]['replaces'] == ['c-open'] and targets[('win-1', 'b-body')]['area_m2'] == 10.
    assert (c['targets_remaining'], c['primary_n'], c['primary_by_class']) == (before['targets_remaining'], before['primary_n'], before['primary_by_class'])
    assert c['target_exclusions']['superseded_by_takeover'] == 1 and c['target_exclusions']['recaptured:win-1'] == 1
    assert len(c['carried_rows']) == len(before['carried_rows']) + 1, 'one carried row more: the body on its own hop'
    assert c['lineage']['takeover_rows'] == before['lineage']['takeover_rows'] + 1 == 1
    assert (c['lineage']['body_carry_rows'], c['lineage']['body_carry_joined'], c['lineage']['body_carry_links_refused']) == (1, 1, {})
    body_row, = [o for o in c['takeover_rows'] if o['id'] == 'b-body']
    assert (body_row['replacer'], body_row['copy_id'], body_row['registration']['slice_cover'], body_row['refusals']) == ('body_carry', 'c-open', .95, [])
    assert BANNED not in json.dumps(c).lower()
    _window(s, 'win-1', [_taken('c-open', 'p-open', 'b-body'), _body('b-body', ['c-open'], status='confirmed')], transform=_transform())
    after = _count(w, recapture)
    assert after['primary_n'] == before['primary_n'] + 1 and after['targets_remaining'] == before['targets_remaining'] - 1
    assert any(r['id'] == 'b-body' and r['frozen_key'] == ['s1', 'p-open'] and r['replaces_copy'] == 'c-open' and r['class'] == 'roof'
               for r in after['already_counted']), 'the decision on the body counts at the slice frozen key'
    assert ['win-1', 'b-body', 'confirmed'] == [r for r in after['primary_rows'] if r[1] == 'b-body'][0][:3]
    assert ('win-1', 'b-body') not in _targets(after)


def test_body_carry_links_that_do_not_hold_are_named_and_followed_nowhere(tmp_path):
    """A body over a decided slice (copy_decided), without a registry row or with an unverified one (body_unregistered), two rows
    (body_registered_twice), a residual outside DESIGN (body_residual_outside_design), a slice that does not name the body back
    (copy_not_reciprocal), replaces naming two copies (body_replaces_not_single), a body over a body (two_hops) and a carried
    replacer of another role (replacer_carried, as before): every link is recorded by name; no takeover row; the slices read as
    before and a decided slice still counts as its own decision."""
    w = _live_world(tmp_path)
    s = w['scenes']
    review = [_copy('c-dec', 'confirmed', 'p-open2', replacement_ids=['b-dec']), _body('b-dec', ['c-dec'], source_oid='x1'),
              _taken('c-unreg', 'x2', 'b-unreg'), _body('b-unreg', ['c-unreg'], source_oid='x2'),
              _taken('c-unver', 'x3', 'b-unver'), _body('b-unver', ['c-unver'], source_oid='x3'),
              _taken('c-twice', 'x4', 'b-twice'), _body('b-twice', ['c-twice'], source_oid='x4'),
              _taken('c-far', 'x5', 'b-far'), _body('b-far', ['c-far'], source_oid='x5'),
              _taken('c-recip', 'x6', 'elsewhere'), _body('b-recip', ['c-recip'], source_oid='x6'),
              _taken('c-pair1', 'x7', 'b-pair'), _taken('c-pair2', 'x8', 'b-pair'), _body('b-pair', ['c-pair1', 'c-pair2'], source_oid='x7'),
              _taken('c-hop', 'p-open', 'b-one'), _body('b-one', ['c-hop'], source_oid='x9', status='superseded', replacement_ids=['b-two']),
              _body('b-two', ['b-one'], source_oid='x10'),
              _taken('c-role', 'x11', 'd-role'), _copy('d-role', 'candidate', 'x12', replaces=['c-role'])]
    _window(s, 'win-1', review, transform=_transform())
    rows = [_registry_row(o, 'win-1') for o in ('p-open', 'p-open2')]
    rows += [_body_row('win-1', 'b-unver', 'c-unver', source_oid='x3', verified=False),
             _body_row('win-1', 'b-twice', 'c-twice', source_oid='x4'), _body_row('win-1', 'b-twice', 'c-twice', source_oid='x4'),
             _body_row('win-1', 'b-far', 'c-far', source_oid='x5', shift=.71),
             _body_row('win-1', 'b-recip', 'c-recip', source_oid='x6'), _body_row('win-1', 'b-pair', 'c-pair1', source_oid='x7'),
             _body_row('win-1', 'b-one', 'c-hop', source_oid='x9'), _body_row('win-1', 'b-two', 'b-one', source_oid='x10'),
             _body_row('win-1', 'b-dec', 'c-dec', source_oid='x1')]
    recapture = _recapture_dir(tmp_path, rows)
    lineage = r5.load_lineage(recapture)
    assert r5.body_registration(lineage, 'win-1', 'b-unreg')['refusal'] == 'body_unregistered'
    assert r5.body_registration(lineage, 'win-1', 'b-far')['refusal'] == 'body_residual_outside_design'
    assert r5.body_registration(None, 'win-1', 'b-dec')['refusal'] == 'body_unregistered', 'no lineage refuses by name'
    decisions = r5.load_decisions(s, lineage=lineage)
    facts = {r['object_id']: r for r in decisions['rows'] if r['scene_id'] == 'win-1'}
    refused = {oid: facts[oid]['replaces_refused'] for oid in facts if facts[oid]['replaces_refused']}
    assert refused == {'b-dec': {'c-dec': 'copy_decided'}, 'b-unreg': {'c-unreg': 'body_unregistered'},
                       'b-unver': {'c-unver': 'body_unregistered'}, 'b-twice': {'c-twice': 'body_registered_twice'},
                       'b-far': {'c-far': 'body_residual_outside_design'}, 'b-recip': {'c-recip': 'copy_not_reciprocal'},
                       'b-pair': {'c-pair1': 'body_replaces_not_single', 'c-pair2': 'body_replaces_not_single'},
                       'b-two': {'b-one': 'two_hops'}, 'd-role': {'c-role': 'replacer_carried'}}
    assert all(name in r5.TAKEOVER_REFUSALS for links in refused.values() for name in links.values())
    assert facts['b-one']['replaces_held'] == ['c-hop'], 'the first hop holds; the second is refused on the body over it'
    assert all(facts[o]['replaces_held'] == [] for o in refused)
    assert facts['c-dec']['cls'] == 'roof' and facts['c-dec']['taken_over_by'] == [] and facts['c-dec']['stratum'] is None
    assert facts['c-unreg']['stratum'] == 'superseded' and facts['b-one']['stratum'] == 'superseded'
    assert all(facts[o]['replacer_kind'] == 'body_carry' for o in refused if o != 'd-role') and facts['d-role']['replacer_kind'] == 'detection'
    joined = r5.join(w['context'], w['split'], decisions, lineage=lineage)
    assert [r['object_id'] for r in joined['rows'] if r['lineage'] and r['lineage'].get('takeover')] == ['b-one']
    dec, = _rows_of(joined, 'win-1', 'c-dec')
    assert dec['primary'] and dec['cls'] == 'roof' and dec['frozen_key'] == ['s1', 'p-open2'], 'a decided slice counts as its own decision'
    for oid in refused:
        own, = _rows_of(joined, 'win-1', oid)
        assert 'takeover' not in own['lineage'] and own['lineage']['frozen_key'] is not None, 'an ordinary carried row, nothing more'
    c = _count(w, recapture)
    assert c['lineage']['body_carry_rows'] == 1 and c['lineage']['body_carry_joined'] == 1
    assert c['lineage']['body_carry_links_refused'] == {'copy_decided': 1, 'body_unregistered': 2, 'body_registered_twice': 1,
                                                        'body_residual_outside_design': 1, 'copy_not_reciprocal': 1,
                                                        'body_replaces_not_single': 2, 'two_hops': 1}
    targets = _targets(c)
    assert ('win-1', 'b-two') not in targets and ('win-1', 'b-one') not in targets and ('s1', 'p-open') not in targets, \
        'the key under a two-hop chain is asked nowhere until the data is repaired by hand'
    assert ('win-1', 'c-hop') not in targets and ('win-1', 'c-dec') not in targets
    assert c['target_exclusions']['superseded'] >= 1 and BANNED not in json.dumps(c).lower()


def test_refused_body_carry_link_restores_the_slice_as_the_asked_target(tmp_path):
    """A body over an undecided slice whose link the hop refuses (the registry row withdrawn, unverified, a residual outside
    DESIGN, the slice not naming the body back): the body is listed nowhere and the SLICE is asked again for its frozen key, as
    before the body (body_refused names the body and the refusal), so targets_remaining, N and the asked keys read exactly as
    with no body at all; the lineage summary names the restore (body_carry_slices_restored) beside the refusal
    (body_carry_links_refused); no 'superseded' exclusion appears. A held link (control) asks the body instead."""
    w = _live_world(tmp_path)
    s = w['scenes']
    _window(s, 'win-1', [_copy('c-open', 'needs_adjustment', 'p-open')], transform=_transform())
    no_body = _count(w, _recapture_dir(tmp_path, [_registry_row('p-open', 'win-1')]))
    assert ('win-1', 'c-open') in _targets(no_body) and no_body['lineage']['body_carry_slices_restored'] == 0
    cases = {'withdrawn': ([_body_row('win-1', 'b-body', 'c-open'), _body_row('win-1', 'b-body', 'c-open', withdrawn=True)], 'body_unregistered'),
             'unverified': ([_body_row('win-1', 'b-body', 'c-open', verified=False)], 'body_unregistered'),
             'outside-design': ([_body_row('win-1', 'b-body', 'c-open', ncc=.2)], 'body_residual_outside_design'),
             'not-reciprocal': ([_body_row('win-1', 'b-body', 'c-open')], 'copy_not_reciprocal')}
    for name, (rows, reason) in cases.items():
        (tmp_path / name).mkdir()
        folder = _recapture_dir(tmp_path / name, [_registry_row('p-open', 'win-1')] + rows)
        taken = _taken('c-open', 'p-open', 'elsewhere' if reason == 'copy_not_reciprocal' else 'b-body')
        _window(s, 'win-1', [taken, _body('b-body', ['c-open'])], transform=_transform())
        c = _count(w, folder)
        targets = _targets(c)
        assert ('win-1', 'b-body') not in targets and ('s1', 'p-open') not in targets, name
        slice_item = targets[('win-1', 'c-open')]
        assert slice_item['body_refused'] == {'b-body': reason} and 'replacer' not in slice_item, name
        assert (slice_item['source_scene_id'], slice_item['source_object_id'], slice_item['area_m2']) == ('s1', 'p-open', 10.), name
        assert set(targets) == set(_targets(no_body)), name
        for key in ('primary_n', 'primary_by_class', 'primary_rows', 'scenes', 'targets_remaining', 'targets_total', 'already_counted'):
            assert c[key] == no_body[key], (name, key)
        assert c['target_exclusions']['recaptured:win-1'] == 1 and 'superseded' not in c['target_exclusions'], name
        assert c['lineage']['body_carry_links_refused'] == {reason: 1} and c['lineage']['body_carry_rows'] == 0, name
        assert c['lineage']['body_carry_slices_restored'] == 1 and c['lineage']['takeover_rows'] == no_body['lineage']['takeover_rows'], name
        assert len(c['carried_rows']) == len(no_body['carried_rows']) + 1, (name, 'the body is still its own refused carried row')
        assert BANNED not in json.dumps(c).lower()
    (tmp_path / 'held').mkdir()
    folder = _recapture_dir(tmp_path / 'held', [_registry_row('p-open', 'win-1'), _body_row('win-1', 'b-body', 'c-open')])
    _window(s, 'win-1', [_taken('c-open', 'p-open', 'b-body'), _body('b-body', ['c-open'])], transform=_transform())
    held = _count(w, folder)
    assert ('win-1', 'b-body') in _targets(held) and ('win-1', 'c-open') not in _targets(held)
    assert held['lineage']['body_carry_slices_restored'] == 0 and held['targets_remaining'] == no_body['targets_remaining']


LIVE_WINDOW = REPO / 'data/scenes/lot-29362/review.json'


@pytest.mark.skipif(not LIVE_WINDOW.exists(), reason='the live lot-29362 window is not on this machine')
def test_live_lot_29362_body_is_read_through_the_amended_hop_and_counts_nothing(tmp_path):
    """The lot-29362 body carry of 2026-10-06 (registry role body_carry, the slice 26458105626a3f1a superseded): the body's link
    holds and is emitted on the slice's hop, whose frozen key (lot-29358, 26458105626a3f1a) is not in block v2 (the slice's own
    carry is refused carry_snapshot_mismatch there), so the body is neither primary nor a target; the count is identical to one
    whose registry withdraws every body_carry row (the amended hop moves nothing on the live data). Reads only."""
    lineage = r5.load_lineage()
    rows = lineage['body_carry'].get(('lot-29362', '0996b19eb84ddba5')) or []
    assert len(rows) == 1 and rows[0]['slice_object_id'] == '26458105626a3f1a' and rows[0]['frame_transform_verified'] is True
    assert r5.body_registration(lineage, 'lot-29362', '0996b19eb84ddba5')['refusal'] is None
    c = r5.count(block='v2')
    body, = [o for o in c['takeover_rows'] if (o['scene_id'], o['id']) == ('lot-29362', '0996b19eb84ddba5')]
    assert (body['replacer'], body['copy_id'], body['frozen_key']) == ('body_carry', '26458105626a3f1a', ['lot-29358', '26458105626a3f1a'])
    assert body['primary'] is False and body['refusals'] == ['carry_snapshot_mismatch'], 'the slice key is outside block v2: unjoined'
    own, = [o for o in c['carried_rows'] if (o['scene_id'], o['id']) == ('lot-29362', '0996b19eb84ddba5')]
    assert 'carry_unregistered' in own['refusals'] and own['primary'] is False
    assert not any(r[0] == 'lot-29362' for r in c['primary_rows']) and not any(r['scene_id'] == 'lot-29362' for r in c['already_counted'])
    assert not any(sc['scene_id'] == 'lot-29362' for sc in c['next_scenes'])
    folder = tmp_path / 'recapture'
    folder.mkdir()
    for path in sorted(r5.RECAPTURE_DIR.glob('*.json*')):
        (folder / path.name).write_bytes(path.read_bytes())
    withdrawn = [{**r, 'withdrawn': True} for rows_ in lineage['body_carry'].values() for r in rows_]
    assert len(withdrawn) == len(lineage['body_carry']) >= 1 and any(r['window_scene_id'] == 'lot-29362' for r in withdrawn), \
        'one live body_carry row per (window, body): lot-29362 and, since 6 October, the six target carries'
    _append_rows(folder, withdrawn, name='windows-2026-10-06-test-withdrawn.jsonl')
    without = r5.count(block='v2', recapture_dir=folder)
    assert without['lineage']['body_carry_rows'] == 0 and c['lineage']['body_carry_rows'] == len(withdrawn)
    assert without['lineage']['body_carry_links_refused'] == {'body_unregistered': len(withdrawn)}
    assert c['lineage']['body_carry_slices_restored'] == 0 and c['lineage']['body_carry_links_refused'] == {}, 'no live body link is refused'
    asked, asked_without = _targets(c), _targets(without)
    bodies_asked = sorted(k for k, o in asked.items() if o.get('replacer') == 'body_carry')
    decided = [o for o in c['takeover_rows'] if o.get('replacer') == 'body_carry' and not o['refusals'] and o['primary'] and o['class'] in ('roof', 'not_roof')]
    # withdrawing every row hands each asked body's key back to its SLICE (restored, never dropped); a decided body's decision
    # no longer joins and its slice is asked again, so N moves by exactly the decided bodies and targets_remaining the other way
    for window, body in bodies_asked:
        slice_id, = asked[(window, body)]['replaces']
        assert (window, body) not in asked_without and asked_without[(window, slice_id)]['body_refused'] == {body: 'body_unregistered'}
        assert asked_without[(window, slice_id)]['source_object_id'] == asked[(window, body)]['frozen_keys'][0][1]
    assert without['lineage']['body_carry_slices_restored'] == len(bodies_asked) + len(decided)
    assert without['primary_n'] == c['primary_n'] - len(decided) and without['targets_remaining'] == c['targets_remaining'] + len(decided)
    assert without['targets_total'] == c['targets_total'] and set(asked_without) - set(asked) == {(w, asked[(w, b)]['replaces'][0]) for w, b in bodies_asked} | \
        {(o['scene_id'], o['copy_id']) for o in decided}
    assert [r for r in c['primary_rows'] if r not in without['primary_rows']] == [r for r in c['primary_rows'] if r[1] in {o['id'] for o in decided}]
    if not decided:
        for key in ('primary_n', 'primary_by_class', 'primary_rows', 'scenes', 'targets_remaining', 'targets_total', 'already_counted'):
            assert c[key] == without[key], key
    assert len(c['takeover_rows']) == len(without['takeover_rows']) + len(withdrawn) and len(c['carried_rows']) == len(without['carried_rows'])
    session = REPO / '.run/session-2026-10-06-rollout/A/counter-before.json'
    if session.exists():
        before = json.load(open(session))
        if before.get('review_snapshot_sha256') == c.get('review_snapshot_sha256'):   # the live reviews as the session read them
            assert (c['primary_n'], c['targets_remaining']) == (before['primary_n'], before['targets_remaining']) == (32, 57)
