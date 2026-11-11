import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image
from scripts.ml.harnessing import (
    cases,
    context,
    demo,
    evaluate,
    executor,
    fieldwork,
    judge,
    library,
    promote,
    report,
    schedule,
    surface,
)
from scripts.ml.harnessing.adapter import SKILL, TracingAdapter, build_base
from scripts.ml.harnessing.casetools import CaseTools
from scripts.ml.harnessing.channels import jsonl_text, parse_json
from scripts.ml.harnessing.selection import assign_ids
from scripts.ml.harnessing.spec import WorkPrompts

SKILL_MD = f'---\nname: {SKILL}\ndescription: Trace outlines.\n---\n\nRead [fit](references/fit.md).\n'


def tree(**extra):
    base = {'AGENTS.md': 'Trace the case.\n', 'guardrails.md': f'- When tracing, load the `{SKILL}` skill.\n',
            f'skills/{SKILL}/SKILL.md': SKILL_MD, f'skills/{SKILL}/references/fit.md': 'Follow the eave.\n'}
    return {**base, **extra}


def ring(left, top, right, bottom):
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


@pytest.mark.parametrize('rel,ok', [('AGENTS.md', True), ('skills/a/SKILL.md', True), ('skills/a/references/x.md', True),
                                    ('skills/a/scripts/x.py', True), ('scripts/x.py', True), ('hooks.json', True),
                                    ('sub_agents/a/AGENT.md', True), ('../AGENTS.md', False), ('/etc/passwd', False),
                                    ('skills/A/SKILL.md', False), ('notes.md', False), ('skills/a/loose.py', False)])
def test_editable_surface(rel, ok):
    assert surface.is_allowed(rel) is ok


@pytest.mark.parametrize('edit,message', [
    ({'guardrails.md': ''}, 'never named in the always-on text'),
    ({f'skills/{SKILL}/SKILL.md': SKILL_MD.replace(f'name: {SKILL}', 'name: other')}, 'matching its directory'),
    ({f'skills/{SKILL}/SKILL.md': SKILL_MD.replace('description: Trace outlines.\n', '')}, 'description'),
    ({'AGENTS.md': ''}, 'AGENTS.md is empty'),
    ({'tool_guidance.md': 'Run `python3 scripts/check.py`.'}, 'scripts/check.py is not in the harness'),
    ({f'skills/{SKILL}/references/fit.md': 'See [more](more.md).'}, 'links to `more.md`'),
    ({'hooks.json': '{"hooks": {"Finish": []}}'}, 'unknown event'),
    ({'scripts/bad.py': 'def broken(:\n'}, 'does not compile'),
    ({'skills/orphan/references/x.md': 'x'}, 'belong to no SKILL.md'),
])
def test_validation_names_every_discarding_problem(edit, message):
    with pytest.raises(ValueError, match=message):
        surface.validate(tree(**edit))


def test_valid_tree_census_and_autotrigger():
    good = tree(**{'scripts/check.py': 'print(1)\n', 'tool_guidance.md': 'Run `python3 scripts/check.py`.'})
    assert surface.validate(good)['skills'] == 1
    untriggered = tree()
    untriggered['guardrails.md'] = ''
    assert surface.autotrigger(untriggered) == [SKILL]
    surface.validate(untriggered)
    with pytest.raises(ValueError, match='sub_agents are not available'):
        surface.validate(tree(**{'sub_agents/edge/AGENT.md': '---\ndescription: one edge\n---\n'}), allow_subagents=False)


def test_tree_round_trip_ignores_stubs_and_reports_rejections(tmp_path):
    written = surface.write_tree(tree(), tmp_path/'h')
    (written/'notes.txt').write_text('off surface')
    rejected = []
    assert surface.read_tree(written, rejected=rejected) == {**tree(), 'MEMORY.md': '', 'tool_guidance.md': ''}
    assert rejected == ['notes.txt']
    assert surface.diversity({'a': tree(), 'b': tree()})['n_distinct'] == 1


def test_context_tripwires_refuse_heldout_paths_and_verdicts(tmp_path):
    (tmp_path/'heldout').mkdir()
    (tmp_path/'heldout'/'reference.json').write_text('{}')
    with pytest.raises(ValueError, match='held-out'):
        context.assert_no_leak_paths(tmp_path, cases.HELDOUT_GLOBS)
    with pytest.raises(ValueError, match='judge marker'):
        context.assert_tree_has_no_verdicts({'MEMORY.md': 'last time "judge_iou": 0.9'})


def test_work_prompts_describe_work_not_measurement():
    with pytest.raises(ValueError, match='WORK'):
        WorkPrompts(work='Maximise the score of each outline')


def write_scene(root, scene_id='house-25001'):
    folder = root/'data/scenes'/scene_id
    (folder/'models').mkdir(parents=True)
    image = Image.new('RGB', (64, 64), (90, 120, 80))
    image.paste((210, 210, 210), (10, 10, 40, 40))
    image.save(folder/'frame.png')
    sha = hashlib.sha256((folder/'frame.png').read_bytes()).hexdigest()
    (folder/'frame.png').rename(folder/f'{sha}.png')
    frame = {'image_width': 64, 'image_height': 64, 'image_sha256': sha, 'model_signature': 'sig1',
             'pixel_to_meters': [[.25, 0], [0, -.25], [-8, 8]], 'meters_to_pixel': [[4, 0], [0, -4], [32, 32]]}
    machine = [{'id': 'roof1', 'kind': 'building', 'ring': ring(10, 10, 44, 44), 'score': .9, 'label': 'Building'},
               {'id': 'roof2', 'kind': 'building', 'ring': ring(2, 2, 8, 8), 'score': .5},
               {'id': 'sol1', 'kind': 'solar_panel', 'ring': ring(12, 12, 16, 16)},
               {'id': 'sol2', 'kind': 'solar_panel', 'ring': ring(50, 50, 56, 56)},
               {'id': 'roof3', 'kind': 'building', 'ring': ring(46, 10, 60, 30), 'score': .7, 'label': 'Building'}]
    reviewed = [{**machine[0], 'ring': ring(10, 10, 40, 40), 'geometry_reviewed': True, 'status': 'confirmed',
                 'notes': 'HUMAN NOTE', 'review_issues': ['shadow']},
                {**machine[1], 'geometry_reviewed': True, 'status': 'confirmed',
                 'provenance': {'source': 'machine:mcp-polygon-draft-v1'}},
                {**machine[2], 'status': 'confirmed'}, {**machine[3], 'status': 'rejected'},
                {**machine[4], 'status': 'candidate'}]
    model = {**frame, 'objects': machine}
    (folder/'latest.json').write_text(json.dumps(model))
    (folder/'models'/'sig1.json').write_text(json.dumps(model))
    (folder/'review.json').write_text(json.dumps({'model_signature': 'sig1', 'revision': 3, 'objects': reviewed}))
    return scene_id


@pytest.fixture
def built(tmp_path):
    write_scene(tmp_path)
    index = cases.build(tmp_path, tmp_path/'cases')
    return tmp_path/'cases', index


def test_cases_hold_out_human_geometry_and_skip_machine_labels(built):
    root, index = built
    assert sorted(index) == ['d-house-25001-structure', 'o-house-25001-roof1']
    public = (root/'o-house-25001-roof1'/'source'/'case.json').read_text()
    case = json.loads(public)
    assert case['start_outline_pixels']['ring'] == ring(10, 10, 44, 44)
    assert 'HUMAN NOTE' not in public and 'review_issues' not in public and 'geometry_reviewed' not in public
    heldout = cases.load_heldout(root, 'o-house-25001-roof1')
    assert heldout['reference_outline_pixels']['ring'] == ring(10, 10, 40, 40) and heldout['change'] == 'edited'
    detection = cases.load_heldout(root, 'd-house-25001-structure')
    assert detection['decisions'] == {'sol1': 'keep', 'sol2': 'reject'}
    assert index['o-house-25001-roof1']['split'] == cases.split_of('house-25001')


def test_case_tools_apply_records_the_outline_and_stales_old_previews(built, tmp_path):
    root, _ = built
    tools = CaseTools(root/'o-house-25001-roof1'/'source', tmp_path/'run')
    tools.inspect('house-25001', 'roof1', roof=True)
    first = tools.preview('house-25001', 'roof1', {'ring': ring(10, 10, 41, 41), 'holes': []}, notes='trim', roof=True)
    second = tools.preview('house-25001', 'roof1', {'ring': ring(10, 10, 40, 40), 'holes': []}, roof=True)
    assert tools.inspect('house-25001', 'roof1', preview_id=second['preview_id'], roof=True)['outline_role'] == 'proposed'
    assert tools.apply(second['preview_id'], roof=True)['revision'] == 1
    with pytest.raises(ValueError, match='revision changed'):
        tools.apply(first['preview_id'], roof=True)
    with pytest.raises(ValueError, match='no other scene'):
        tools.feedback('house-2', 'roof1')
    done = tools.finish('house-25001', 'roof1', 'north edge unresolved')
    submission = json.loads((tmp_path/'run'/'submission.json').read_text())
    assert done['finished'] and submission['outline_pixels']['ring'] == ring(10, 10, 40, 40)
    assert submission['notes'] == 'north edge unresolved' and (tmp_path/'run'/'submission.png').is_file()


def test_detection_review_requires_every_decision(built, tmp_path):
    root, _ = built
    tools = CaseTools(root/'d-house-25001-structure'/'source', tmp_path/'run')
    assert [d['object_id'] for d in tools.inspect_detections('house-25001')['detections']] == ['sol1', 'sol2']
    with pytest.raises(ValueError, match='missing'):
        tools.submit_detections('house-25001', [{'object_id': 'sol1', 'decision': 'keep'}])
    with pytest.raises(ValueError, match='kind in this family'):
        tools.submit_detections('house-25001', [{'object_id': 'sol1', 'decision': 'relabel', 'kind': 'car'},
                                            {'object_id': 'sol2', 'decision': 'reject'}])
    tools.submit_detections('house-25001', [{'object_id': 'sol1', 'decision': 'keep'},
                                        {'object_id': 'sol2', 'decision': 'reject'}])
    case, heldout = tools.case, cases.load_heldout(root, 'd-house-25001-structure')
    row = judge.score_detect(case, heldout, json.loads((tmp_path/'run'/'submission.json').read_text()))
    assert row['judge_decision_accuracy'] == 1 and row['keep_all_accuracy'] == .5 and row['outcome'] == 'improved'


def test_outline_judge_measures_against_heldout_and_the_start(built):
    root, _ = built
    case, heldout = cases.load_case(root, 'o-house-25001-roof1'), cases.load_heldout(root, 'o-house-25001-roof1')
    exact = judge.score_outline(case, heldout, {'outline_pixels': {'ring': ring(10, 10, 40, 40), 'holes': []}})
    kept = judge.score_outline(case, heldout, {'outline_pixels': case['start_outline_pixels'], 'kept_start_outline': True})
    assert exact['judge_iou'] == pytest.approx(1) and exact['outcome'] == 'improved'
    assert kept['judge_iou'] == pytest.approx(900/1156) and kept['delta_iou'] == 0 and kept['outcome'] == 'neutral'
    assert judge.outcome(-.05, 0) == 'regressed'


def test_materialise_stages_each_executor_layout(tmp_path):
    harness = tree(**{'sub_agents/edge/AGENT.md': '---\ndescription: one edge\n---\nCheck it.\n',
                      'hooks.json': json.dumps({'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'true'}]}]}}),
                      'scripts/check.py': 'print(1)\n', 'MEMORY.md': 'Fact.'})
    claude = executor.materialise(harness, tmp_path/'claude', 'claude')
    assert {'CLAUDE.md', '.claude/agents/edge.md', '.claude/settings.json', f'.claude/skills/{SKILL}/SKILL.md',
            'scripts/check.py'} <= set(claude)
    assert 'name: edge' in (tmp_path/'claude'/'.claude/agents/edge.md').read_text()
    assert (tmp_path/'claude'/'skills'/SKILL/'references'/'fit.md').is_file()
    assert '# Memory\n\nFact.' in (tmp_path/'claude'/'CLAUDE.md').read_text()
    codex = executor.materialise(harness, tmp_path/'codex', 'codex')
    assert f'.agents/skills/{SKILL}/SKILL.md' in codex and '.claude/settings.json' not in codex
    assert f'`{SKILL}` (`skills/{SKILL}/SKILL.md`)' in (tmp_path/'codex'/'AGENTS.md').read_text()
    opencode = executor.materialise(harness, tmp_path/'opencode', 'opencode')
    assert 'mode: subagent' in (tmp_path/'opencode'/'.opencode/agent/edge.md').read_text() and 'AGENTS.md' in opencode


def test_claude_command_isolates_the_rollout(tmp_path):
    argv, stdin = executor.command(executor.ExecutorSpec('claude', 'claude-sonnet-5-5'), tmp_path, tree())
    assert stdin and '--strict-mcp-config' in argv and 'mcp__addr-case' in argv[argv.index('--allowedTools') + 1]
    config = json.loads((tmp_path/'.case-mcp.json').read_text())['mcpServers']['addr-case']
    assert config['args'][-4:] == ['--case', str(tmp_path/'case'), '--run', str(tmp_path/'run')]


def test_trajectories_render_in_the_shared_grammar(tmp_path):
    claude = tmp_path/'claude.jsonl'
    claude.write_text('\n'.join(json.dumps(e) for e in [
        {'type': 'system', 'subtype': 'init', 'model': 'm', 'mcp_servers': [{'name': 'addr-case', 'status': 'connected'}]},
        {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'inspect', 'input': {'a': 1}}]}},
        {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'is_error': True, 'content': 'bad ring'}]}},
        {'type': 'result', 'subtype': 'success', 'num_turns': 2, 'total_cost_usd': .5, 'result': 'done'}]))
    text = executor.render_trajectory(claude, 'claude')
    assert 'addr-case=connected' in text and '[ACTION] inspect({"a": 1})' in text and '[OBSERVATION ERROR] bad ring' in text
    assert executor.rollout_cost(claude, 'claude') == .5
    codex = tmp_path/'codex.jsonl'
    codex.write_text(json.dumps({'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'server': 'addr-case',
                                                                     'tool': 'finish_review', 'arguments': {},
                                                                     'result': {'content': [{'type': 'text', 'text': 'ok'}]}}}))
    assert '[ACTION] addr-case.finish_review({})' in executor.render_trajectory(codex, 'codex')
    assert executor.render_trajectory(tmp_path/'missing.jsonl', 'opencode').startswith('(no trajectory')


def test_improver_stream_parsers():
    assert parse_json('banner\n{"result": "ok"}')['result'] == 'ok'
    stream = '\n'.join(json.dumps(e) for e in [{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'A'}},
                                                 {'part': {'type': 'text', 'text': 'B'}},
                                                 {'part': {'type': 'step-finish', 'cost': .25}}])
    assert jsonl_text(stream) == ('A\n\nB', '', .25)


def test_anonymous_ids_are_stable_and_shuffled():
    ids = assign_ids(['agg1', 'agg2', 'agg3'], task_id='t', k=3, mode='agg')
    assert ids == assign_ids(['agg3', 'agg1', 'agg2'], task_id='t', k=3, mode='agg') and sorted(ids) == ['A', 'B', 'C']


def test_adapter_refuses_case_identities_outside_memory(built, tmp_path):
    root, _ = built
    adapter = TracingAdapter(runs_root=tmp_path/'runs', cases_root=root, repo=tmp_path/'repo', spec=executor.ExecutorSpec())
    adapter.validate_tree({'MEMORY.md': f'{surface.CASE_FACTS}\nIn this case house-25001 has a wall strip.'})
    with pytest.raises(ValueError, match='case identities'):
        adapter.validate_tree({f'skills/{SKILL}/references/fit.md': 'For house-25001 move the south edge.'})


@pytest.fixture
def repo_skill(tmp_path):
    skill = tmp_path/'repo'/'.opencode/skills'/SKILL
    (skill/'references').mkdir(parents=True)
    (skill/'SKILL.md').write_text(SKILL_MD)
    (skill/'references'/'fit.md').write_text('Follow the eave.\n')
    return tmp_path/'repo'


def test_base_snapshot_wraps_the_project_skill(repo_skill, tmp_path):
    base = surface.read_tree(build_base(tmp_path/'base', repo=repo_skill))
    assert base[f'skills/{SKILL}/references/fit.md'] == 'Follow the eave.\n' and 'finish_review' in base['AGENTS.md']
    assert json.loads((tmp_path/'base.json').read_text())['fingerprint'] == surface.tree_fingerprint(base)


@pytest.mark.parametrize('mode', ['agg', 'seq'])
def test_demo_loop_adopts_a_better_harness_and_resumes(tmp_path, mode):
    sweep, reports = demo.run(tmp_path, k=2, modes=(mode,))
    assert [row['status'] for row in sweep['domains'][0]['tasks']] == ['ok', 'ok', 'ok']
    built = reports[mode]
    assert built['adopted']['outline']['mean_judge_iou'] == pytest.approx(1)
    assert built['base']['outline']['mean_judge_iou'] < .95
    assert built['adopted_vs_base']['judge_iou']['n_improved'] == 2
    summary = json.loads((tmp_path/'runs'/'tracing-demo-agg2'/'o-demo-b-roof'/'summary.json').read_text())
    assert summary['modes'][mode]['ledger']['R'] == 8 and summary['modes'][mode]['ledger']['P'] == 6
    lib = library.Library(root=tmp_path/'runs'/'library'/'tracing-demo-k2'/mode, domain='roof-outline')
    assert lib.entries() == ['o-demo-a-roof', 'o-demo-b-roof']
    assert 'darker parallel band' in lib.lessons()[0][1]
    again = schedule.run_sweep(2, adapter=demo.DemoAdapter(tmp_path), improver=demo.DemoImprover(tmp_path/'audit'),
                               runs_root=tmp_path/'runs', modes=(mode,))
    assert [row['status'] for row in again['domains'][0]['tasks']] == ['skipped']*3
    assert 'adopted harness' in report.render(built)


def test_promotion_maps_adopted_skill_edits_onto_the_project_skill(tmp_path, repo_skill):
    demo.run(tmp_path/'run', k=1, modes=('agg',))
    case, adopted, lib = promote.adopted(tmp_path/'run'/'runs'/'library'/'tracing-demo-k1', 'agg', 'roof-outline')
    changes = promote.skill_changes(adopted, repo=repo_skill)
    path = f'.opencode/skills/{SKILL}/references/shadow-bands.md'
    assert changes[path][0] is None and 'Shadow bands' in changes[path][1]
    assert f'+++ b/{path}' in promote.patch(changes)
    promote.apply(changes, repo=repo_skill)
    assert (repo_skill/path).is_file() and 'cue:' in promote.lessons_markdown(lib)


def test_eval_replays_a_harness_without_its_case_facts(tmp_path):
    demo.write_cases(tmp_path/'cases')
    adapter = demo.DemoAdapter(tmp_path)
    candidate = {**demo.base_tree(), f'skills/{SKILL}/references/shadow-bands.md': demo.SHADOW_REFERENCE,
                 'MEMORY.md': f'## Durable\n\nKeep.\n\n{surface.CASE_FACTS}\n\n- In this case the band is south.\n'}
    stripped = evaluate.strip_case_facts(candidate)
    assert 'In this case' not in stripped['MEMORY.md'] and 'Keep.' in stripped['MEMORY.md']
    rows = evaluate.run_eval(adapter, stripped, demo.base_tree(), adapter.tasks(), label='t', repeats=1, workers=1)
    summary = evaluate.summarise(rows)
    assert summary['n_graded'] == 6 and summary['harness']['candidate']['outcomes']['improved'] == 2
    assert summary['paired']['judge_iou']['n_improved'] == 2 and 'candidate - baseline' in evaluate.render('t', summary)


def fieldwork_repo(tmp_path):
    from scripts.ml import fieldwork_review as fr
    write_scene(tmp_path)
    folder = tmp_path/'data/scenes/house-25001'
    sha = next(p.stem for p in folder.glob('*.png'))
    crop = {'x0': 0, 'y0': 0, 'width': 64, 'height': 64}
    current, proposed = ring(10, 10, 44, 44), ring(10, 10, 40, 40)

    def mark(tag, points):
        closed = [*points, points[0]]
        return {'id': f'{tag}-roof1', 'geometry': {'type': 'Polygon', 'coordinates': [[[x/64, y/64] for x, y in closed]]},
                'properties': {'addr': {'source': 'agent-proposal:spike_fill' if tag == 'new' else 'sam3'}}}
    receipts = tmp_path/fieldwork.RECEIPTS
    receipts.mkdir(parents=True)
    for item in ('r1-cut-001-paaaa', 'r1-cut-002-pbbbb'):
        meta = {'family': 'cut', 'sceneId': 'house-25001', 'objectIds': ['roof1'], 'objectKind': 'building',
                'crop': crop, 'base': {'modelSignature': 'sig1', 'imageSha256': sha},
                'proposedRingSha': fr.ring_sha([[float(x), float(y)] for x, y in proposed], []),
                'currentRingSha': fr.ring_sha(current, []),
                'agentHint': {'verdict': 'right'}}
        document = {'title': f'Cuts {item[-6:]} · roof · agent: right', 'notes': '# Right = good. Wrong = not it.',
                    'source': {'model': {'sha256': 'absent'}}, 'metadata': {'addr': meta},
                    'annotations': [mark('cur', current), mark('new', proposed)]}
        (receipts/f'{item}.json').write_text(json.dumps({'itemId': item, 'family': 'cut', 'stageSha': 'none',
                                                          'projectId': 'mooroolbark-review', 'document': document}))
    decisions = tmp_path/'decisions.json'
    decisions.write_text(json.dumps({'cursor': 9, 'decisions': [
        {'itemId': 'r1-cut-001-paaaa', 'family': 'cut', 'word': 'right'},
        {'itemId': 'r1-cut-002-pbbbb', 'family': 'cut', 'word': 'edited', 'editedRingFrame': [ring(11, 11, 40, 40)]}]}))
    reviews = tmp_path/'agent-reviews.json'
    reviews.write_text(json.dumps({'cursor': 9, 'reviews': [{'itemId': 'r1-cut-001-paaaa', 'reviewer': 'codex',
                                                              'decision': 'uncertain', 'current': True}]}))
    index = fieldwork.build_cases(tmp_path, tmp_path/'fw-cases', decisions=[decisions], agent_reviews=[reviews])
    corner = fieldwork.build_cases(tmp_path, tmp_path/'fw-corner', decisions=[decisions], agent_reviews=[reviews],
                                   families=('corner',))
    assert corner == {}  # receipts are cut items; a corner-only build selects none
    return tmp_path/'fw-cases', index


def test_fieldwork_items_become_assessment_and_outline_cases(tmp_path):
    root, index = fieldwork_repo(tmp_path)
    assert sorted(index) == ['fo-r1-cut-001-paaaa', 'fo-r1-cut-002-pbbbb', 'fr-r1-cut-001-paaaa',
                             'fr-r1-cut-002-pbbbb']
    public = (root/'fr-r1-cut-001-paaaa'/'source'/'case.json').read_text()
    assert 'agent: right' not in public and 'agentHint' not in public and 'verdict' not in public
    review = cases.load_case(root, 'fr-r1-cut-001-paaaa')
    assert review['fieldwork']['rings_verified'] and review['proposed_outline_pixels']['ring'] == ring(10, 10, 40, 40)
    assert cases.load_heldout(root, 'fr-r1-cut-001-paaaa')['codex_decision'] == 'uncertain'
    assert cases.load_heldout(root, 'fr-r1-cut-002-pbbbb')['human_decision'] == 'needs_adjustment'
    assert cases.load_heldout(root, 'fo-r1-cut-001-paaaa')['reference_origin'] == 'agent-drawn, human-confirmed'
    assert cases.load_heldout(root, 'fo-r1-cut-002-pbbbb')['reference_origin'] == 'human-edited'
    edited = cases.load_heldout(root, 'fo-r1-cut-002-pbbbb')['reference_outline_pixels']['ring']
    assert edited == ring(11, 11, 40, 40) and index['fr-r1-cut-002-pbbbb']['domain'] == 'fieldwork-cut-review'


def test_assessment_tools_record_a_decision_judged_against_the_human_word(tmp_path):
    root, _ = fieldwork_repo(tmp_path)
    tools = CaseTools(root/'fr-r1-cut-001-paaaa'/'source', tmp_path/'run')
    assert tools.inspect_review('house-25001', 'roof1')['comparison']['intersection_over_union'] == pytest.approx(900/1156)
    assert tools.inspect('house-25001', 'roof1', outline_role='proposed')['outline_role'] == 'proposed'
    with pytest.raises(ValueError, match='decision of'):
        tools.submit_review('house-25001', 'roof1', 'maybe', 'x')
    with pytest.raises(ValueError, match='notes are required'):
        tools.submit_review('house-25001', 'roof1', 'approved', ' ')
    with pytest.raises(ValueError, match='assessment cases'):
        CaseTools(root/'fo-r1-cut-001-paaaa'/'source', tmp_path/'run2').inspect_review('house-25001', 'roof1')
    tools.submit_review('house-25001', 'roof1', 'approved', 'Every proposed eave sits on the roof edge.')
    submission = json.loads((tmp_path/'run'/'submission.json').read_text())
    row = judge.score_review(tools.case, cases.load_heldout(root, 'fr-r1-cut-001-paaaa'), submission)
    assert row['judge_agrees'] == 1 and row['codex_agrees'] == 0 and row['outcome'] == 'agree'
    assert report.summarise([{**row, 'graded': True}])['review']['agreement_on_codex_items'] == 1


class FakeFieldwork:
    def __init__(self):
        self.revision, self.saved, self.conflict = 3, [], True

    def rpc(self, method, data):
        if method == 'GetItem':
            return 200, {'item': {'id': data['id'], 'revision': self.revision}}
        assert method == 'SaveAgentReview' and set(data) <= {'itemId', 'annotationId', 'expectedRevision', 'reviewer',
                                                             'decision', 'notes'}
        if self.conflict:
            self.conflict, self.revision = False, self.revision + 1
            return 409, {'code': 'aborted'}
        assert data['expectedRevision'] == self.revision and len(data['notes'].encode()) <= 10_000
        self.saved.append(data)
        self.revision += 1
        return 200, {'agentReview': {'id': f'ar_{len(self.saved)}'}}


def test_adopted_assessments_post_as_agent_reviews_once(tmp_path, monkeypatch):
    root, _ = fieldwork_repo(tmp_path)
    tools = CaseTools(root/'fr-r1-cut-001-paaaa'/'source', tmp_path/'runs'/'adopted'/'run')
    tools.submit_review('house-25001', 'roof1', 'approved', 'Every proposed eave sits on the roof edge.')
    ns = schedule.Namespace('tracing', 'claude', 'agg', 1)
    summary = tmp_path/'runs'/str(ns)/'fr-r1-cut-001-paaaa'/'summary.json'
    summary.parent.mkdir(parents=True)
    summary.write_text(json.dumps({'task_id': 'fr-r1-cut-001-paaaa', 'selections': {'agg': {
        'ok': True, 'chosen_run_dir': str(tmp_path/'runs'/'adopted'), 'chosen_run_id': f'{ns}/x/agg1',
        'harness_fingerprint': 'abc'}}}))

    class Adapter:
        name, executor = 'tracing', 'claude'

        def submission(self, run_dir):
            return json.loads((run_dir/'run'/'submission.json').read_text())

        def case(self, task_id):
            return cases.load_case(root, task_id)
    client = FakeFieldwork()
    plan = fieldwork.post_reviews(Adapter(), tmp_path/'runs', ns, origin='http://127.0.0.1:1', client=client,
                                  root=tmp_path, dry_run=True)
    assert [r['outcome'] for r in plan] == ['planned'] and not client.saved
    assert plan[0]['payload']['reviewer'] == 'harness-claude' and plan[0]['payload']['annotationId'] == 'new-roof1'
    posted = fieldwork.post_reviews(Adapter(), tmp_path/'runs', ns, origin='http://127.0.0.1:1', client=client,
                                    root=tmp_path, dry_run=False)
    assert [r['outcome'] for r in posted] == ['posted'] and client.saved[0]['decision'] == 'approved'
    assert 'Agent history only' in client.saved[0]['notes']
    again = fieldwork.post_reviews(Adapter(), tmp_path/'runs', ns, origin='http://127.0.0.1:1', client=client,
                                   root=tmp_path, dry_run=False)
    assert [r['outcome'] for r in again] == ['skip_receipt'] and len(client.saved) == 1
    monkeypatch.delenv('FIELDWORK_PUSH_ALLOW_LIVE', raising=False)
    with pytest.raises(Exception, match='live Fieldwork origin'):
        fieldwork.post_reviews(Adapter(), tmp_path/'runs', ns, root=tmp_path, dry_run=False)


def test_component_usage_tells_used_from_dead_components(tmp_path):
    from scripts.ml.harnessing import usage
    stream = tmp_path/'stream.jsonl'
    stream.write_text('\n'.join(json.dumps({'type': 'assistant', 'message': {'content': [block]}}) for block in (
        {'type': 'tool_use', 'name': 'Skill', 'input': {'skill': SKILL}},
        {'type': 'tool_use', 'name': 'Read', 'input': {'file_path': f'/ws/.claude/skills/{SKILL}/references/fit.md'}},
        {'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'python3 scripts/check.py'}})))
    harness = tree(**{'scripts/check.py': 'print(1)\n', 'scripts/unused.py': 'print(2)\n',
                      f'skills/{SKILL}/references/never.md': 'x'})
    found = usage.component_usage(stream, 'claude', harness)
    assert found[f'skills/{SKILL}/SKILL.md']['via'] == ['invoked'] and found['AGENTS.md']['how'] == 'always-on'
    assert found[f'skills/{SKILL}/references/fit.md']['via'] == ['read'] and found['scripts/check.py']['via'] == ['ran']
    assert found['scripts/unused.py']['how'] == 'unused' and found[f'skills/{SKILL}/references/never.md']['how'] == 'unused'
    matrix = usage.render({'base1': found})
    assert f'| `skills/{SKILL}/references/never.md` | - |' in matrix


def test_lineage_admits_only_gate_passing_candidates(tmp_path):
    from scripts.ml.harnessing.lineage import Lineage
    lineage = Lineage(tmp_path)
    base = surface.write_tree(tree(), tmp_path/'base')
    assert lineage.seed(base)[0] == 1 and lineage.seed(base)[0] == 1
    assert lineage.admit(tree(**{'MEMORY.md': 'a'}), source='x', gate={'passed': False}) is None
    version, folder = lineage.admit(tree(**{'MEMORY.md': 'b'}), source='y', gate={'passed': True})
    assert version == 2 and surface.read_tree(folder)['MEMORY.md'] == 'b' and lineage.head()[0] == 2
    assert len(lineage.load()['rejected']) == 1


def gate_rows(candidate_bf, baseline_bf, candidate_iou=None, baseline_iou=None):
    rows = []
    for case, (cand, base) in enumerate(zip(candidate_bf, baseline_bf, strict=True)):
        for repeat, jitter in ((1, 0.0), (2, 0.01)):
            rows.append({'task_id': f't{case}', 'harness': 'candidate', 'repeat': repeat, 'graded': True,
                         'judge_boundary_f': cand, 'judge_iou': (candidate_iou or candidate_bf)[case]})
            rows.append({'task_id': f't{case}', 'harness': 'baseline', 'repeat': repeat, 'graded': True,
                         'judge_boundary_f': base + jitter, 'judge_iou': (baseline_iou or baseline_bf)[case]})
    return rows


def test_gate_rule_rejects_noise_and_harm_and_passes_clear_gains():
    from scripts.ml.harnessing import gate
    clear = gate_rows([.9, .85, .8, .95, .9, .88], [.7, .7, .6, .8, .75, .7])
    assert gate.decide(clear, evaluate.summarise(clear), 'outline')['passed']
    tie = gate_rows([.7, .7, .6, .8, .75, .7], [.7, .7, .6, .8, .75, .7])
    assert not gate.decide(tie, evaluate.summarise(tie), 'outline')['passed']
    harm = gate_rows([.9, .85, .8, .95, .9, .88], [.7, .7, .6, .8, .75, .7],
                     candidate_iou=[.80]*6, baseline_iou=[.85]*6)
    decision = gate.decide(harm, evaluate.summarise(harm), 'outline')
    assert not decision['passed'] and not decision['checks']['no harm on judge_iou (median >= -0.01)']
    few = gate_rows([.9, .9], [.7, .7])
    assert not gate.decide(few, evaluate.summarise(few), 'outline')['passed']
    assert gate.noise_floor(clear, 'judge_boundary_f') == pytest.approx(.01)


def test_cycle_refines_consolidates_gates_and_admits_a_better_base(tmp_path):
    from scripts.ml.harnessing import consolidate, cycle
    from scripts.ml.harnessing.lineage import Lineage
    demo.write_cases(tmp_path/'cases', val=True)
    seeded = Lineage(tmp_path/'runs').seed(surface.write_tree(demo.base_tree(), tmp_path/'runs'/'base'))
    adapter = demo.DemoAdapter(tmp_path, base_dir=seeded[1])
    record = cycle.run_cycle(adapter, demo.DemoImprover(tmp_path/'audit'), runs_root=tmp_path/'runs',
                             domain='roof-outline', train_cases=3, val_cases=3, repeats=2, min_cases=3, log=lambda _m: None)
    assert record['outcome'] == 'admitted v2' and record['phases']['gate']['passed']
    assert record['phases']['consolidate']['train_cases'] == ['o-demo-a-roof', 'o-demo-b-roof', 'o-demo-c-roof']
    ctx = tmp_path/'runs'/'consolidated'/'v1-roof-outline'/'context'
    assert sorted(p.name for p in (ctx/'feedback').iterdir() if p.is_dir()) == ['o-demo-a-roof', 'o-demo-b-roof',
                                                                            'o-demo-c-roof']
    assert not any('o-demo-d-roof' in p.read_text() for p in ctx.rglob('*.md'))
    assert (ctx/'feedback'/'o-demo-a-roof'/'card.png').is_file() and (ctx/'usage.md').is_file()
    head = Lineage(tmp_path/'runs').head()
    assert head[0] == 2 and 'bright-to-dark' in surface.read_tree(head[1])[f'skills/{SKILL}/SKILL.md']
    with pytest.raises(ValueError, match='held-out identities'):
        consolidate.assert_split_clean(ctx, {'o-demo-a-roof'})


USAGE_LIMIT = {'type': 'result', 'subtype': 'success', 'is_error': True, 'api_error_status': 429,
               'api_error': 'usage_limit_reached', 'result': "You've hit your session limit", 'total_cost_usd': 0}


def test_service_outages_are_never_cached_or_counted(built, tmp_path, monkeypatch):
    from scripts.ml.harnessing.channels import ServiceUnavailable
    root, _ = built
    adapter = TracingAdapter(runs_root=tmp_path/'runs', cases_root=root, repo=tmp_path/'repo', spec=executor.ExecutorSpec())
    calls = []

    def fake_launch(spec, workspace, tree, prompt, *, stream, stderr, timeout=None):
        calls.append(1)
        stream.write_text(json.dumps(USAGE_LIMIT) + '\n')
        stderr.write_text('')
        return {'executor': 'claude', 'returncode': 1, 'timed_out': False, 'cost_usd': 0.0,
                'outage': executor.outage(stream, 'claude', 1, stderr)}
    monkeypatch.setattr(executor, 'launch', fake_launch)
    monkeypatch.setattr('time.sleep', lambda _s: None)
    run_id = 'tracing-claude-agg1/o-house-25001-roof1/base1'
    with pytest.raises(ServiceUnavailable, match='usage_limit_reached'):
        adapter.execute('o-house-25001-roof1', None, run_id)
    assert calls == [1]  # a usage limit is not retried
    run_dir = adapter.run_dir_of(run_id)
    assert not (run_dir/'.complete').exists() and json.loads((run_dir/'meta.json').read_text())['outage']
    # A rollout cached before outage detection existed is re-run, not served.
    (run_dir/'.complete').write_text(json.dumps({'run_id': run_id, 'harness': surface.fingerprint(adapter.base_harness()),
                                                 'executor': 'claude', 'model': ''}))
    with pytest.raises(ServiceUnavailable):
        adapter.execute('o-house-25001-roof1', None, run_id)
    assert calls == [1, 1]
    rows_before = []
    with pytest.raises(ServiceUnavailable):
        rows_before = evaluate.run_eval(adapter, demo.base_tree(), demo.base_tree(), ['o-house-25001-roof1'], label='x')
    assert rows_before == [] and not (tmp_path/'runs'/'eval-x'/'eval.json').exists()


def test_improver_outage_is_raised_not_swallowed_into_a_fallback(monkeypatch, tmp_path):
    import subprocess as sp

    from scripts.ml.harnessing.channels import (
        ChannelSpec,
        Improver,
        ImproverError,
        ServiceUnavailable,
    )
    monkeypatch.setattr('time.sleep', lambda _s: None)
    replies = iter([json.dumps(USAGE_LIMIT), json.dumps({'type': 'result', 'is_error': True, 'subtype': 'error'})])

    def fake_run(command, **kwargs):
        return sp.CompletedProcess(command, 1, stdout=next(replies), stderr='')
    monkeypatch.setattr('scripts.ml.harnessing.channels.subprocess.run', fake_run)
    improver = Improver(ChannelSpec('claude'), audit_dir=tmp_path/'audit')
    with pytest.raises(ServiceUnavailable, match='usage_limit_reached'):
        improver.call('mission', None, 'inv1.a1')
    with pytest.raises(ImproverError):  # an ordinary failed session still falls back
        improver.call('mission 2', None, 'inv1.a2', max_attempts=1)


def review_rows(cand, base, humans):
    return [{'task_id': f't{case}', 'harness': side, 'repeat': repeat, 'graded': True, 'judge_agrees': value,
             'human_decision': human}
            for case, (pair, human) in enumerate(zip(zip(cand, base, strict=True), humans, strict=True))
            for side, value in zip(('candidate', 'baseline'), pair, strict=True) for repeat in (1, 2)]


MIXED = ['approved']*4 + ['needs_adjustment']*4


def test_binary_gate_is_class_balanced_with_a_two_case_sign_margin():
    from scripts.ml.harnessing import gate
    better = review_rows([1, 1, 1, 0, 1, 1, 0, 1], [0, 0, 1, 0, 1, 0, 0, 1], MIXED)
    decision = gate.decide(better, evaluate.summarise(better), 'review')
    assert decision['passed'] and decision['statistic'] == 'class-balanced mean'
    assert decision['gain'] == pytest.approx((.5 + .25)/2)
    one_flip = review_rows([1, 0, 1, 0, 1, 0, 1, 1], [0, 0, 1, 0, 1, 0, 1, 1], MIXED)
    assert not gate.decide(one_flip, evaluate.summarise(one_flip), 'review')['passed']


def test_gate_refuses_a_shifted_bias_and_a_one_class_sample():
    from scripts.ml.harnessing import gate
    # "Approve everything": +1 on every approved item, -1 on every needs_adjustment item.
    biased = review_rows([1, 1, 1, 1, 0, 0, 0, 0], [0, 0, 0, 0, 1, 1, 1, 1], MIXED)
    decision = gate.decide(biased, evaluate.summarise(biased), 'review')
    assert not decision['passed'] and decision['gain'] == 0 and decision['class_deltas']['needs_adjustment'] == -1
    single = review_rows([1]*8, [0]*8, ['approved']*8)
    decision = gate.decide(single, evaluate.summarise(single), 'review')
    assert not decision['passed'] and not decision['checks']["the sample covers at least two human answers (['approved'])"]


def test_stratified_samples_cover_minority_answers_and_revoke_restores_the_parent(built, tmp_path):
    from scripts.ml.harnessing.cycle import stratified
    from scripts.ml.harnessing.lineage import Lineage

    class Fake:
        answers = {'a1': 'approved', 'a2': 'approved', 'a3': 'approved', 'a4': 'approved', 'n1': 'needs_adjustment',
                   'r1': 'rejected'}

        def heldout(self, task):
            return {'human_decision': self.answers[task]}
    assert stratified(Fake(), sorted(Fake.answers), 4) == ['a1', 'n1', 'r1', 'a2']
    lineage = Lineage(tmp_path/'lin')
    lineage.seed(surface.write_tree(tree(), tmp_path/'base'))
    lineage.admit(tree(**{'MEMORY.md': 'b'}), source='y', gate={'passed': True})
    with pytest.raises(ValueError, match='only the head'):
        lineage.revoke(1, 'no')
    target = lineage.revoke(2, 'gate sample held one answer class')
    assert lineage.head()[0] == 1 and surface.read_tree(target)['MEMORY.md'] == 'b'
    assert lineage.load()['invalidated'][0]['invalidated_reason'] == 'gate sample held one answer class'


def test_cheap_cycle_learns_from_train_feedback_alone(tmp_path):
    from scripts.ml.harnessing import cycle
    from scripts.ml.harnessing.lineage import Lineage
    demo.write_cases(tmp_path/'cases', val=True)
    seeded = Lineage(tmp_path/'runs').seed(surface.write_tree(demo.base_tree(), tmp_path/'runs'/'base'))
    adapter = demo.DemoAdapter(tmp_path, base_dir=seeded[1])
    record = cycle.run_cycle(adapter, demo.DemoImprover(tmp_path/'audit'), runs_root=tmp_path/'runs',
                             domain='roof-outline', train_cases=3, val_cases=3, repeats=2, min_cases=3,
                             inner='rollouts', log=lambda _m: None)
    assert record['outcome'] == 'admitted v2' and record['phases']['run']['graded'] == 3
    assert not list((tmp_path/'runs').glob('tracing-demo-agg1-v1/*/summary.json'))  # no per-case loop ran


def test_trace_flywheel_ranks_unreviewed_traces_and_stages_a_trace_family_bundle(tmp_path):
    write_scene(tmp_path)
    assert list(cases.build_unlabeled(tmp_path, tmp_path/'unlabeled', per_domain=5)) == ['u-house-25001-roof3']
    assert cases.load_heldout(tmp_path/'unlabeled', 'u-house-25001-roof3') is None
    context = tmp_path/'ctx.json'
    context.write_text(json.dumps({'cuts': {'whole_other': []}}))
    adapter = TracingAdapter(runs_root=tmp_path/'runs', cases_root=tmp_path/'unlabeled', repo=tmp_path/'repo',
                             spec=executor.ExecutorSpec())
    rows = []
    for repeat, east in ((1, 58), (2, 57), (3, 58)):
        run_dir = tmp_path/'rollouts'/f'r{repeat}'
        tools = CaseTools(tmp_path/'unlabeled'/'u-house-25001-roof3'/'source', run_dir/'run')
        preview = tools.preview('house-25001', 'roof3', {'ring': ring(46, 10, east, 30), 'holes': []}, roof=True)
        tools.apply(preview['preview_id'], roof=True)
        tools.finish('house-25001', 'roof3', f'East eave moved inside the wall strip (repeat {repeat}).')
        rows.append({'task_id': 'u-house-25001-roof3', 'harness': 'baseline', 'repeat': repeat, 'run_dir': str(run_dir)})
    ranked, skipped = fieldwork.rank_traces(adapter, rows)
    assert len(ranked) == 1 and not skipped and ranked[0]['disagreement'] > 0
    assert ranked[0]['submission']['outline_pixels']['ring'] == ring(46, 10, 58, 30)  # the medoid of three repeats
    got = fieldwork.queue_traces(adapter, rows, label='t1', root=tmp_path, context=str(context))
    assert got['queued'] == 1 and Path(got['stage']).parent == tmp_path/fieldwork.TRACE_STAGE
    item = json.loads(next((Path(got['stage'])/'items').glob('*.json')).read_text())
    assert item['id'].startswith('r1019-14-trace-001-p') and item['metadata']['addr']['family'] == 'trace'
    assert {a['id'] for a in item['annotations']} == {'cur-roof3', 'new-roof3'}
    assert item['metadata']['addr']['harness']['disagreement'] == ranked[0]['disagreement']
    assert not (tmp_path/'.run').exists()  # the shared stage pointer is never touched
    assert fieldwork.next_trace_line(tmp_path) == 2


def test_confirmation_is_bound_to_the_version_and_its_parent(tmp_path):
    from scripts.ml.harnessing.lineage import Lineage
    lineage = Lineage(tmp_path)
    lineage.seed(surface.write_tree(tree(), tmp_path/'base'))
    lineage.admit(tree(**{'MEMORY.md': 'b'}), source='y', gate={'passed': True})
    v1, v2 = lineage.version(1), lineage.version(2)
    with pytest.raises(ValueError, match='other trees'):
        lineage.confirm(2, {'passed': True, 'baseline_fingerprint': 'x', 'candidate_fingerprint': v2['fingerprint']})
    bound = {'baseline_fingerprint': v1['fingerprint'], 'candidate_fingerprint': v2['fingerprint']}
    lineage.confirm(2, {'passed': False, **bound})
    assert not Lineage.confirmed(lineage.version(2))
    lineage.confirm(2, {'passed': True, **bound})
    assert Lineage.confirmed(lineage.version(2)) and len(lineage.version(2)['confirmations']) == 2


def test_edge_profile_finds_the_roof_edge_inside_a_shadow_band(tmp_path):
    import subprocess
    import sys

    from PIL import ImageDraw
    image = Image.new('RGB', (80, 80), (100, 120, 90))
    draw = ImageDraw.Draw(image)
    draw.rectangle([20, 20, 49, 49], fill=(210, 210, 210))
    draw.rectangle([20, 50, 49, 55], fill=(40, 40, 40))
    image.save(tmp_path/'image.png')
    script = Path(__file__).resolve().parents[1]/'harnessing'/'assets'/'edge_profile.py'

    def profile(ring_):
        out = subprocess.run([sys.executable, str(script), '--image', str(tmp_path/'image.png'), '--ring', json.dumps(ring_),
                              '--json'], capture_output=True, text=True, check=True)
        return {int(k): v for k, v in json.loads(out.stdout).items()}
    spill = profile([[20, 20], [50, 20], [50, 56], [20, 56]])  # includes the dark band below the roof
    assert spill[2]['strongest_step_offset_px'] == pytest.approx(-6, abs=1.5) and spill[2]['consistency'] >= .75
    tight = profile([[20, 20], [50, 20], [50, 50], [20, 50]])
    assert abs(tight[2]['strongest_step_offset_px']) <= 1 and 'dark_band_beyond_px' in tight[2]
    with pytest.raises(ValueError, match='audit anchor'):
        from scripts.ml.harnessing.engineered import edge_profile
        edge_profile(tree())


def test_protected_scenes_match_the_review_loop_and_never_become_cases(tmp_path):
    from scripts.ml.roof_review_loop import PROTECTED
    assert cases.PROTECTED_SCENES == frozenset(PROTECTED)
    write_scene(tmp_path, 'house-30286')
    assert cases.build(tmp_path, tmp_path/'c') == {} and cases.build_unlabeled(tmp_path, tmp_path/'u') == {}


def test_agent_drawn_simplified_references_are_scored_on_area_only(built):
    root, _ = built
    case, heldout = cases.load_case(root, 'o-house-25001-roof1'), cases.load_heldout(root, 'o-house-25001-roof1')
    assert heldout['boundary_reference'] and heldout['reference_origin'] == 'human'
    drawn = {**heldout, **cases.reference_provenance({'ring': ring(10, 10, 40, 40),
                                                       'notes': 'Assistant imagery review: traced the eave.'})}
    assert drawn == {**heldout, 'reference_origin': 'agent-drawn, human-confirmed', 'reference_vertices': 4,
                     'boundary_reference': False}
    row = judge.score_outline(case, drawn, {'outline_pixels': {'ring': ring(10, 10, 40, 40), 'holes': []}})
    assert row['judge_boundary_f'] is None and row['delta_boundary_f'] is None and row['judge_iou'] == pytest.approx(1)
    assert row['outcome'] == 'improved' and not row['boundary_reference']


def test_gate_refuses_more_opposite_verdicts_even_with_a_balanced_gain():
    from scripts.ml.harnessing import gate
    humans = ['approved']*3 + ['needs_adjustment']*3 + ['rejected']*2
    rows = []
    for case, (human, base, cand) in enumerate(zip(humans, ['needs_adjustment']*3 + ['needs_adjustment']*3 +
                                                   ['needs_adjustment']*2,
                                                   ['approved']*3 + ['needs_adjustment']*3 + ['approved', 'rejected'],
                                                   strict=True)):
        for side, decision in (('baseline', base), ('candidate', cand)):
            for repeat in (1, 2):
                rows.append({'task_id': f't{case}', 'harness': side, 'repeat': repeat, 'graded': True,
                             'human_decision': human, 'agent_decision': decision, 'judge_agrees': float(human == decision)})
    decision = gate.decide(rows, evaluate.summarise(rows), 'review')
    assert decision['gain'] > 0 and not decision['passed']
    assert not decision['checks']['no more opposite verdicts, approved vs rejected (2 vs 0)']
