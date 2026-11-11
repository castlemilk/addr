import copy
import hashlib
import io
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from scripts.ml import promote_profile as pp
from scripts.ml import scene_confidence as c
from scripts.ml.scene_region import region_frame

SCAN = 'mooroolbark-500114985968d40b-17-1-2'
CAPTURE = 'house-101'
MACHINE = 'house-102'
HUMAN = 'house-103'
PROTECTED = 'house-104'
MISMATCH = 'mooroolbark-500114985968d40b-17-1-3'
MAP = 'map-0123456789abcdef'
OLD_CODE = {'pipeline': 'sam3-scene-roof-v11', 'roof_model': 'colour-line-heuristic', 'geometry_code': 'a' * 64,
            'roof_line_code': 'b' * 64, 'topology_version': 'roof-topology-v4'}


def png():
    image = Image.new('RGB', (512, 512), '#6f7a6a')
    draw = ImageDraw.Draw(image)
    draw.rectangle((180, 200, 330, 300), fill='#b8b8b8')
    out = io.BytesIO()
    image.save(out, format='PNG')
    return out.getvalue()


def old_roof(obj, kind='hipped'):
    """Fitter fields from an 'older fitter' (deliberately unlike anything the current one writes)."""
    obj.update(roof_annotation={'schema_version': 1, 'roof_type': kind, 'vertices': [[0, 0, 3]], 'faces': [[0]], 'edges': []},
               roof_parts=[{'ring_meters': [[0, 0], [1, 0], [1, 1]], 'ring_z': [3, 3, 3]}] * 3,
               roof_lines=[{'line_meters': [[0, 0], [1, 1]], 'kind': 'hip'}] * 5, roof_type=kind,
               roof_geometry_source='image-guided-prior', roof_geometry_note='Older fitter note.')
    return obj


def write_scene(root, scene_id, *, scan=False, pipeline=True, extra=None):
    data = png()
    scene = {**region_frame([145.31, -37.7905, 145.311, -37.7895]), 'id': scene_id, 'title': f'Test {scene_id}',
             'image_width': 512, 'image_height': 512, 'image_sha256': hashlib.sha256(data).hexdigest(),
             'objects': [old_roof({'id': 'roof-a', 'kind': 'building', 'status': 'candidate', 'label': 'Roof', 'score': .9,
                                   'ring': [[180, 200], [330, 200], [330, 300], [180, 300]], 'model': {'height_m': 3.2},
                                   'boundary_refinement': {'raw': True}, 'group_id': 'g1'}),
                         {'id': 'tree-a', 'kind': 'tree', 'status': 'candidate', 'label': 'Tree', 'score': .7,
                          'ring': [[20, 20], [60, 20], [60, 60], [20, 60]], 'model': {}}],
             'groups': [{'id': 'g1'}]}
    if pipeline:
        scene['analysis'] = {**OLD_CODE, 'roof_proposals': 1, 'roof_failures': 0, 'input_geometry': 'imagery-only',
                             'recomputed_from_model': 'c' * 64}
    if scan:
        scene.update(suburb_run=pp.FROZEN_RUN, owned_object_ids=['roof-a', 'tree-a'])
        scene['analysis']['inference_profile'] = 'f' * 64
    scene.update(extra or {})
    scene['model_signature'] = pp.model_signature(scene)
    folder = root / 'data/scenes' / scene_id
    (folder / 'models').mkdir(parents=True, exist_ok=True)
    (folder / f"{scene['image_sha256']}.png").write_bytes(data)
    body = pp.scene_bytes(scene)
    (folder / 'latest.json').write_bytes(body)
    (folder / 'models' / f"{scene['model_signature']}.json").write_bytes(body)
    return scene


def write_review(root, scene_id, objects_fn):
    latest = json.loads((root / 'data/scenes' / scene_id / 'latest.json').read_bytes())
    objects = objects_fn(copy.deepcopy(latest['objects']))
    review = {'revision': 1, 'model_signature': latest['model_signature'], 'objects': objects, 'groups': [], 'reviewed_classes': []}
    (root / 'data/scenes' / scene_id / 'review.json').write_text(json.dumps(review, indent=2))


def audit(scene, image):
    return [{'object_id': o['id'], 'kind': o['kind'], 'issues': [], 'geometry_passed': True, 'area_m2': 100.,
             'cost': {'observed_segments': 4 if o.get('roof_type') == 'flat' else 1, 'image_line_mismatch_m': .2}}
            for o in scene['objects']]


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(c, 'audit_scene', audit)
    frozen = {}
    for sid, scan in ((SCAN, True), (MISMATCH, True), (CAPTURE, False), (MACHINE, False), (HUMAN, False), (PROTECTED, False)):
        frozen[sid] = write_scene(tmp_path, sid, scan=scan)['model_signature']
    write_scene(tmp_path, MAP, pipeline=False)
    run = tmp_path / pp.FROZEN_RUN_DIR
    run.mkdir(parents=True)
    run_results = {SCAN: {'status': 'complete', 'model_signature': frozen[SCAN]},
                   MISMATCH: {'status': 'complete', 'model_signature': 'e' * 64}}
    (run / 'run.json').write_text(json.dumps({'results': run_results}))
    manifest = tmp_path / 'data/references/roof-checkpoints/0123456789abcdef'
    manifest.mkdir(parents=True)
    (manifest / 'manifest.json').write_text(json.dumps({'scene_id': PROTECTED, 'files': {}}))

    def machine(objects):
        objects[0].update(status='confirmed', geometry_reviewed=True, provenance={'source': 'machine:decision-policy-v2'})
        return objects

    def human(objects):
        objects[0].update(status='confirmed', geometry_reviewed=True)
        return objects
    write_review(tmp_path, MACHINE, machine)
    write_review(tmp_path, HUMAN, human)
    for sid in (SCAN, MISMATCH, CAPTURE):
        c.assess(sid, tmp_path, index=sid != CAPTURE)   # frozen-profile confidence rows / files
    return tmp_path


def stub_fit(scene, image, root, *, optical_guidance=False):
    """Deterministic stand-in for the real fitter: every building becomes a flat roof with form evidence."""
    diagnostics = {}
    for obj in scene['objects']:
        if obj['kind'] != 'building':
            continue
        obj.update(roof_annotation={'schema_version': 1, 'roof_type': 'flat', 'vertices': [], 'faces': [], 'edges': []},
                   roof_parts=[{'ring_meters': [[0, 0], [2, 0], [2, 2]], 'ring_z': [3.2, 3.2, 3.2]}], roof_lines=[], roof_type='flat',
                   roof_geometry_source='image-guided-prior', roof_geometry_note='Flat roof: stub.')
        obj['roof_form_evidence'] = {'version': 'roof-form-flat-v3', 'decision': 'flat', 'rule': 'bright'}
        obj['roof_form_uncertain'] = False
        diagnostics[obj['id']] = {'ok': True, 'roof_type': 'flat', 'form_uncertain': False,
                                  'form': {'decision': 'flat', 'rule': 'bright', 'representation': 'flat_hypothesis'},
                                  'topology': None, 'support': None, 'optical_guidance': None}
    scene['analysis'] = {**pp.fitter_analysis(optical_guidance), 'roof_proposals': len(diagnostics), 'roof_failures': 0,
                         'roof_failure_details': []}
    return diagnostics, {'version': 'building-flags-v2', 'hinted_objects': 0}


def tree_state(root):
    """{relative path: (sha256, mtime_ns)} of every file under data/scenes."""
    out = {}
    for path in sorted((root / 'data/scenes').rglob('*')):
        if path.is_file():
            out[str(path.relative_to(root))] = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    return out


def live_rows(root, scene_id):
    with closing(sqlite3.connect(root / pp.CONFIDENCE_DB)) as db:
        return (db.execute('SELECT id, signature, mtime, bounds FROM scenes WHERE id=?', (scene_id,)).fetchall(),
                db.execute('SELECT * FROM roofs WHERE scene_id=? ORDER BY object_id', (scene_id,)).fetchall())


def profile_rows(root, scene_id):
    with closing(sqlite3.connect(root / pp.CONFIDENCE_DB)) as db:
        try:
            return db.execute('SELECT profile, object_id FROM profile_roofs WHERE scene_id=? ORDER BY profile, object_id',
                              (scene_id,)).fetchall()
        except sqlite3.OperationalError:
            return []


def test_profile_of_matches_scene_confidence():
    scene = {'analysis': {**OLD_CODE, 'input_geometry': 'imagery-only', 'inference_profile': 'f' * 64, 'roof_proposals': 3}}
    assert pp.profile_of(scene) == c.profile(scene)


def test_plan_identity_deterministic_and_skip_reasons(root):
    first = pp.plan(root)
    second = pp.plan(root, list(reversed([r['scene_id'] for r in first['scenes']])))
    assert first['profile_id'] == second['profile_id'] and len(first['profile_id']) == 64
    assert first['components'] == second['components']
    assert json.loads((root / pp.OUT_DIR / 'plan.json').read_text())['profile_id'] == first['profile_id']
    components = first['components']
    assert components['roof_fitter'] == pp.fitter_analysis(False)
    assert components['roof_fitter']['roof_optical_guidance'] == 'disabled'
    assert components['flat_rules']['version'] == 'roof-form-flat-v3'
    assert components['context_hints']['structure_version'] == 'structure-kinds-v3'
    reasons = {r['scene_id']: r['skip_reason'] for r in first['scenes']}
    assert reasons == {SCAN: None, CAPTURE: None, MACHINE: None, HUMAN: 'human_decided_review', PROTECTED: 'protected_reference',
                       MISMATCH: 'frozen_signature_mismatch', MAP: 'no_roof_models'}
    scan = next(r for r in first['scenes'] if r['scene_id'] == SCAN)
    assert 'roof_line_code' in scan['fields_that_would_change']['analysis']['changed']
    assert scan['fields_that_would_change']['analysis']['inference_profile'] == 'replaced'
    assert first['expected_profile']['scan'] != first['expected_profile']['capture']
    # the context-hint provenance is part of the identity
    cache = root / pp.CONTEXT_SOURCES['inventory_properties']
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text('{"features": {}}')
    assert pp.identity(root)['profile_id'] != first['profile_id']
    assert pp.identity(root)['profile_id'] == pp.identity(root)['profile_id']


def test_merge_keeps_every_non_roof_byte_and_order():
    production = {'id': 's', 'objects': [old_roof({'id': 'r', 'kind': 'building', 'ring': [[0, 0], [1, 0], [1, 1]], 'score': .5}),
                                         {'id': 't', 'kind': 'tree', 'ring': [], 'roof_type': 'not-a-building-field'}],
                  'analysis': {'pipeline': 'x'}, 'owned_object_ids': ['r']}
    production['model_signature'] = pp.model_signature(production)
    fitted = pp.strip_fitter_fields(production)
    fitted['objects'][0].update(roof_type='flat', roof_geometry_note='new', roof_form_uncertain=True)
    promoted = pp.merge_promoted(production, fitted, {'pipeline': 'y'})
    assert pp.non_roof_bytes(promoted) == pp.non_roof_bytes(production)
    assert promoted['objects'][1] == production['objects'][1]
    assert list(promoted['objects'][0])[:3] == ['id', 'kind', 'ring'] and list(promoted['objects'][0])[-1] == 'roof_form_uncertain'
    assert 'roof_parts' not in promoted['objects'][0]
    assert list(promoted) == list(production) and promoted['model_signature'] == pp.model_signature(promoted)


def test_dry_run_writes_nothing_under_data_scenes(root, monkeypatch):
    monkeypatch.setattr(pp, 'fit_scene', stub_fit)
    before = tree_state(root)
    sqlite_before = (root / pp.CONFIDENCE_DB).read_bytes()
    out = pp.dry_run(root, workers=1)
    assert tree_state(root) == before
    assert (root / pp.CONFIDENCE_DB).read_bytes() == sqlite_before
    assert out['wrote_under_data_scenes'] is False
    assert out['run']['counts'] == {'complete': 6}  # every eligible scene is refit, applying or not
    for sid in (SCAN, CAPTURE, MACHINE):
        candidate, diag = pp.candidate_paths(root, sid)
        production = json.loads((root / 'data/scenes' / sid / 'latest.json').read_bytes())
        promoted = json.loads(candidate.read_bytes())
        assert pp.non_roof_bytes(promoted) == pp.non_roof_bytes(production)
        assert promoted['objects'][0]['roof_type'] == 'flat' and promoted['analysis']['promotion']['previous_model_signature'] == production['model_signature']
        assert promoted['analysis']['recomputed_from_model'] == 'c' * 64  # carried production history
    scan = json.loads(pp.candidate_paths(root, SCAN)[0].read_bytes())
    assert scan['analysis']['inference_profile'] == out['profile_id']
    assert pp.profile_of(scan) == out['expected_profile']['scan']
    summary = out['summary']['all_buildings']
    assert summary['buildings'] == 6 and summary['roof_type_transitions'] == {'hipped->flat': 6}
    assert summary['flat']['fired'] == {'n': 6, 'of': 6, 'share': 1.0}
    assert summary['attribution_of_geometry_changes'] == {'flat-v3': 6}
    assert out['summary']['owned_scan_roofs']['buildings'] == 2
    assert (root / pp.OUT_DIR / 'dry-run.json').exists() and (root / pp.OUT_DIR / 'object-diffs.json').exists()
    assert out['contact_sheet']['roofs'] == 6 and (root / pp.OUT_DIR / 'changed-24.png').exists()
    # resumable: a second dry run finds every candidate fresh
    assert pp.dry_run(root, workers=1)['run']['total'] == 0


def test_real_fitter_diagnostics_are_keyed_by_object(root):
    before = tree_state(root)
    ident = pp.identity(root)
    result = pp.recompute_scene(CAPTURE, root, ident=ident)
    assert result['status'] == 'complete', result
    diag = json.loads(pp.candidate_paths(root, CAPTURE)[1].read_bytes())
    [row] = diag['object_diffs']
    assert row['object_id'] == 'roof-a' and row['diagnosed'] and not row['error']
    assert row['flat']['decision'] in ('flat', 'pitched', 'uncertain')
    candidate = json.loads(pp.candidate_paths(root, CAPTURE)[0].read_bytes())
    assert candidate['objects'][0]['roof_form_evidence']['version'] == 'roof-form-flat-v3'
    assert candidate['analysis']['roof_optical_guidance'] == 'disabled'
    assert tree_state(root) == before


def test_apply_preserves_non_roof_fields_and_review_json(root, monkeypatch):
    monkeypatch.setattr(pp, 'fit_scene', stub_fit)
    dry = pp.dry_run(root, workers=1)
    before = {sid: (root / 'data/scenes' / sid / 'latest.json').read_bytes() for sid in (SCAN, CAPTURE, MACHINE, HUMAN, PROTECTED, MISMATCH)}
    reviews = {sid: (root / 'data/scenes' / sid / 'review.json').read_bytes() for sid in (MACHINE, HUMAN)}
    frozen_rows = live_rows(root, SCAN)
    with pytest.raises(SystemExit):
        pp.apply(root, acknowledged=False, guard=False)
    audit = pp.apply(root, acknowledged=True, guard=False, workers=1, assess=c.assess)
    assert audit['files_rewritten'] == 3 and sorted(w['scene_id'] for w in audit['writes']) == sorted([SCAN, CAPTURE, MACHINE])
    assert audit['skipped'] == {'frozen_signature_mismatch': 1, 'human_decided_review': 1, 'protected_reference': 1}
    for sid in (HUMAN, PROTECTED, MISMATCH):
        assert (root / 'data/scenes' / sid / 'latest.json').read_bytes() == before[sid]
    for sid in (MACHINE, HUMAN):
        assert (root / 'data/scenes' / sid / 'review.json').read_bytes() == reviews[sid]
    for write in audit['writes']:
        sid = write['scene_id']
        data = (root / 'data/scenes' / sid / 'latest.json').read_bytes()
        scene = json.loads(data)
        assert pp.non_roof_bytes(scene) == pp.non_roof_bytes(json.loads(before[sid]))
        assert write['before_sha256'] == hashlib.sha256(before[sid]).hexdigest() and write['after_sha256'] == hashlib.sha256(data).hexdigest()
        assert scene['model_signature'] == pp.model_signature(scene) and pp.scene_bytes(scene) == data
        assert (root / 'data/scenes' / sid / 'models' / f"{scene['model_signature']}.json").read_bytes() == data
        confidence = json.loads((root / 'data/scenes' / sid / 'confidence.json').read_bytes())
        assert confidence['profile'] == pp.profile_of(scene) == write['profile_after']
        assert confidence['model_signature'] == scene['model_signature']
        assert write['review_json']['untouched'] and write['object_diffs'][0]['roof_type'] == ['hipped', 'flat']
    assert audit['reviews_now_bound_to_previous_snapshot'] == 1 and audit['machine_labels_in_rewritten_reviews'] == 1
    # live rows follow production (new profile, new mtime); frozen rows kept intact in the profile table beside the new ones
    scenes, roofs = live_rows(root, SCAN)
    assert scenes[0][2] == (root / 'data/scenes' / SCAN / 'latest.json').stat().st_mtime_ns
    assert {json.loads(r[6])['properties']['profile'] for r in roofs} == {dry['expected_profile']['scan']}
    frozen_profile = json.loads(frozen_rows[1][0][6])['properties']['profile']
    assert profile_rows(root, SCAN) == sorted([(frozen_profile, 'roof-a'), (dry['expected_profile']['scan'], 'roof-a')])
    with closing(sqlite3.connect(root / pp.CONFIDENCE_DB)) as db:
        kept = db.execute('SELECT payload FROM profile_roofs WHERE profile=? AND scene_id=?', (frozen_profile, SCAN)).fetchone()[0]
    assert kept == frozen_rows[1][0][6]
    assert profile_rows(root, CAPTURE) == []  # captures that were never indexed stay out of the suburb index
    assert audit['profile_rows']['archived_live_rows']['roofs'] == 2 and audit['profile_rows']['added_new_roofs'] == 1
    assert profile_rows(root, MISMATCH) == [(json.loads(live_rows(root, MISMATCH)[1][0][6])['properties']['profile'], 'roof-a')]
    assert (Path(audit['backup_dir']) / SCAN / 'latest.json').read_bytes() == before[SCAN]


def test_apply_skips_scenes_whose_signature_moved(root, monkeypatch):
    monkeypatch.setattr(pp, 'fit_scene', stub_fit)
    pp.dry_run(root, workers=1)
    # a capture re-saved after the dry run: its candidate no longer matches the production bytes
    moved = json.loads((root / 'data/scenes' / CAPTURE / 'latest.json').read_bytes())
    moved['title'] = 'Changed after the dry run'
    moved.pop('model_signature')
    moved['model_signature'] = pp.model_signature(moved)
    (root / 'data/scenes' / CAPTURE / 'latest.json').write_bytes(pp.scene_bytes(moved))
    audit = pp.apply(root, acknowledged=True, guard=False, workers=1, assess=c.assess)
    assert {w['scene_id'] for w in audit['writes']} == {SCAN, MACHINE}
    assert audit['skipped']['candidate_stale_source'] == 1
    assert json.loads((root / 'data/scenes' / CAPTURE / 'latest.json').read_bytes())['title'] == 'Changed after the dry run'
    # a write-time race (bytes moved between inventory and write) is refused too
    record = next(r for r in pp.inventory(root) if r['scene_id'] == SCAN)
    record['latest_sha256'] = '0' * 64
    assert pp.promote_scene(root, record, pp.identity(root), root / 'backup-x', indexed=True, assess=c.assess) == (None, 'signature_mismatch')


def test_rollback_restores_bytes_mtime_confidence_and_rows(root, monkeypatch):
    monkeypatch.setattr(pp, 'fit_scene', stub_fit)
    pp.dry_run(root, workers=1)
    paths = {sid: root / 'data/scenes' / sid for sid in (SCAN, CAPTURE, MACHINE)}
    before = {sid: ((p / 'latest.json').read_bytes(), (p / 'latest.json').stat().st_mtime_ns,
                    (p / 'confidence.json').read_bytes() if (p / 'confidence.json').exists() else None,
                    sorted(os.listdir(p / 'models'))) for sid, p in paths.items()}
    rows_before = live_rows(root, SCAN)
    review = (paths[MACHINE] / 'review.json').read_bytes()
    audit = pp.apply(root, acknowledged=True, guard=False, workers=1, assess=c.assess)
    assert audit['files_rewritten'] == 3 and live_rows(root, SCAN) != rows_before
    report = pp.rollback(root, audit['audit_path'], guard=False)
    assert report['restored'] == 3 and report['skipped'] == {}
    for sid, p in paths.items():
        data, mtime, confidence, models = before[sid]
        assert (p / 'latest.json').read_bytes() == data and (p / 'latest.json').stat().st_mtime_ns == mtime
        assert ((p / 'confidence.json').read_bytes() if (p / 'confidence.json').exists() else None) == confidence
        assert sorted(os.listdir(p / 'models')) == models
    assert live_rows(root, SCAN) == rows_before
    assert profile_rows(root, SCAN) == []
    assert (paths[MACHINE] / 'review.json').read_bytes() == review
    # a second rollback finds nothing to undo and changes nothing
    again = pp.rollback(root, audit['audit_path'], guard=False)
    assert again['restored'] == 0 and again['skipped'] == {'changed_since_write': 3}


def test_parallel_apply_with_real_fitter_and_assess_then_rollback(root):
    """The production path: real fitter (inline dry run), then worker processes running the real scene_confidence.assess."""
    before = {sid: (root / 'data/scenes' / sid / 'latest.json').read_bytes() for sid in (SCAN, CAPTURE, MACHINE)}
    rows_before = live_rows(root, SCAN)
    dry = pp.dry_run(root, [SCAN, CAPTURE, MACHINE], workers=1)
    assert dry['run']['counts'] == {'complete': 3}
    assert dry['summary']['all_buildings']['fits']['fitted_after'] == 3
    audit = pp.apply(root, [SCAN, CAPTURE, MACHINE], acknowledged=True, guard=False, workers=2)
    assert audit['files_rewritten'] == 3, audit.get('skipped_scenes')
    for sid in (SCAN, CAPTURE, MACHINE):
        scene = json.loads((root / 'data/scenes' / sid / 'latest.json').read_bytes())
        assert pp.non_roof_bytes(scene) == pp.non_roof_bytes(json.loads(before[sid]))
        assert json.loads((root / 'data/scenes' / sid / 'confidence.json').read_bytes())['profile'] == pp.profile_of(scene)
    assert {json.loads(r[6])['properties']['profile'] for r in live_rows(root, SCAN)[1]} == {dry['expected_profile']['scan']}
    report = pp.rollback(root, audit['audit_path'], guard=False)
    assert report['restored'] == 3
    assert {sid: (root / 'data/scenes' / sid / 'latest.json').read_bytes() for sid in before} == before
    assert live_rows(root, SCAN) == rows_before
