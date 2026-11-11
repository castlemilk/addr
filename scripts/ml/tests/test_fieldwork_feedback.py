import asyncio
import fcntl
import json
import subprocess
from pathlib import Path

import pytest
from scripts.ml import fieldwork_feedback as feedback

PLAN = 'a' * 64
NEW_PLAN = 'b' * 64


def ready_plan(plan_id=PLAN, item_id='scene-1'):
    return {'planId': plan_id, 'canApply': True, 'scenes': [{'itemId': item_id, 'status': 'ready', 'reviewUrl': '/scenes?scene='+item_id}],
            'counts': {'drafts': 1, 'notesOnly': 0, 'conflicts': 0}, 'quarantined': []}


def applied_scene(item_id='scene-1', already=False):
    return {'complete': True, 'applied': [{'itemId': item_id, 'reviewUrl': '/scenes?scene='+item_id,
                                          'alreadyApplied': already, 'receiptOnly': False}]}


def no_changes():
    return {'planId': NEW_PLAN, 'canApply': True, 'scenes': [], 'counts': {'conflicts': 0}, 'quarantined': []}


@pytest.fixture(autouse=True)
def clean_config(monkeypatch, tmp_path):
    for key in (*[key for key in feedback.os.environ if key.startswith('FIELDWORK_')],
                'ANNOTATION_URL', 'ANNOTATION_TOKEN', 'ADDR_APP_URL'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(feedback, 'ROOT', tmp_path)


def test_pull_uses_argv_and_automatically_applies_with_separate_local_auth(monkeypatch, tmp_path):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        command = argv[3]
        if command == 'feedback':
            (tmp_path/'packet.json').write_text('{"frozen":"packet"}')
        response = {'packetFile': str(tmp_path/'packet.json'), 'entries': 1, 'cursor': '538'} if command == 'feedback' else (
            ready_plan() if command == 'feedback-preview' else applied_scene())
        return subprocess.CompletedProcess(argv, 0, json.dumps(response))

    monkeypatch.setattr(feedback.subprocess, 'run', run)
    result = feedback.pull_feedback(project_id='other-project', snapshot='frozen snapshot',
                                    mirror_dir='mirror with spaces', packet_file='packet.json',
                                    token_file='remote token', local_token_file='local token', item_id='scene-1')
    assert result['status'] == 'synced' and result['cursor'] == '538'
    assert result['counts']['appliedScenes'] == 1
    assert 'preview' not in result and 'apply' not in result
    pull, preview, apply = [call[0] for call in calls]
    assert pull[:4] == ['go', 'run', './cmd/annotate', 'feedback']
    assert pull[pull.index('--dir')+1] == str(tmp_path/'mirror with spaces')
    assert pull[pull.index('--snapshot')+1] == str(tmp_path/'frozen snapshot')
    assert pull[pull.index('--token-file')+1] == str(tmp_path/'remote token')
    assert pull[pull.index('--id')+1] == 'scene-1'
    assert preview == ['go', 'run', './cmd/annotate', 'feedback-preview', '--file', str(tmp_path/'packet.json'),
                       '--local-url', feedback.DEFAULT_LOCAL_URL, '--local-token-file', str(tmp_path/'local token')]
    assert apply == ['go', 'run', './cmd/annotate', 'feedback-apply', '--file', str(tmp_path/'packet.json.pending.json'),
                     '--local-url', feedback.DEFAULT_LOCAL_URL, '--local-token-file', str(tmp_path/'local token'), '--plan-id', PLAN]
    assert all(options['cwd'] == tmp_path and options['text'] is True and options['stdout'] == subprocess.PIPE
               and options['check'] is False and 'shell' not in options for _, options in calls)
    assert [options['timeout'] for _, options in calls] == [600, 120, 120]
    assert not (tmp_path/'packet.json.apply.json').exists()


def test_environment_defaults_and_skip_preview_do_not_expose_bearer(monkeypatch, tmp_path):
    monkeypatch.setenv('ANNOTATION_TOKEN', 'secret-never-in-argv')
    monkeypatch.setenv('ANNOTATION_URL', 'https://example.test')
    monkeypatch.setenv('FIELDWORK_PROJECT', 'chosen')
    calls = []
    monkeypatch.setattr(feedback, '_run', lambda command, flags: calls.append((command, flags)) or {'entries': 0})
    result = feedback.pull_feedback(skip_preview=True)
    assert result['status'] == 'downloaded' and result['counts']['items'] == 0
    assert len(calls) == 1
    flags = calls[0][1]
    assert flags[flags.index('--file')+1] == str(tmp_path/'data/fieldwork-sync/chosen/feedback.json')
    assert flags[flags.index('--url')+1] == 'https://example.test'
    assert '--token-file' not in flags and 'secret-never-in-argv' not in flags


@pytest.mark.parametrize('use_env', [False, True])
def test_custom_remote_never_receives_implicit_production_token(monkeypatch, use_env):
    monkeypatch.setattr(feedback, '_run', lambda *_: pytest.fail('Must not send default token to a custom URL'))
    if use_env:
        monkeypatch.setenv('ANNOTATION_URL', 'https://different.example')
    with pytest.raises(feedback.FeedbackError, match='custom Fieldwork URL requires'):
        feedback.pull_feedback(**({} if use_env else {'remote_url': 'https://different.example'}))


@pytest.mark.parametrize('origin, live', [
    (feedback.DEFAULT_REMOTE_URL, True), (feedback.DEFAULT_REMOTE_URL + ':443/', True),
    ('https://FIELDWORK-ANNOTATIONS.ben-ebsworth.workers.dev', True), (feedback.DEFAULT_REMOTE_URL + ':8443', True),
    ('fieldwork-annotations.ben-ebsworth.workers.dev', True), ('http://127.0.0.1:18131', False),
    ('https://fieldwork-annotations.ben-ebsworth.workers.dev.example.com', False), ('https://other.example', False), ('', False),
])
def test_is_live_origin_compares_the_normalised_hostname(origin, live):
    assert feedback.is_live_origin(origin) is live


def test_default_origin_trailing_slash_keeps_configured_file_default(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(feedback, '_run', lambda command, flags: calls.append(flags) or {})
    feedback.pull_feedback(remote_url=feedback.DEFAULT_REMOTE_URL+'/', skip_preview=True)
    assert calls[0][-2:] == ['--token-file', str(tmp_path/feedback.DEFAULT_TOKEN_FILE)]


@pytest.mark.parametrize('project', ['../escape', 'nested/path', '.', '', 'with space', 'a'*129])
def test_project_validation_precedes_path_derivation_or_process(monkeypatch, project):
    monkeypatch.setattr(feedback, '_run', lambda *_: pytest.fail('Must reject before starting Go'))
    with pytest.raises(feedback.FeedbackError, match='Project ID'):
        feedback.pull_feedback(project_id=project)


def test_preview_failure_preserves_successful_packet_and_cli_reports_failure(monkeypatch, tmp_path, capsys):
    packet = tmp_path/'feedback.json'

    def run(command, flags):
        if command == 'feedback':
            packet.write_text('{"durable":"packet"}')
            return {'packetFile': str(packet), 'entries': 1}
        raise feedback.FeedbackError('Local app unavailable')

    monkeypatch.setattr(feedback, '_run', run)
    assert feedback.main(['pull', '--file', str(packet)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['counts']['items'] == 1 and result['status'] == 'incomplete' and result['phase'] == 'preview'
    assert 'task pull' in result['nextStep']
    assert json.loads(packet.read_text()) == {'durable': 'packet'}


def test_apply_requires_plan_and_never_reads_remote_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv('FIELDWORK_TOKEN_FILE', '/not/read/cloud-secret')
    monkeypatch.setenv('ANNOTATION_TOKEN', 'not-forwarded')
    calls = []
    monkeypatch.setattr(feedback, '_run', lambda command, flags: calls.append((command, flags)) or {'applied': 1})
    with pytest.raises(SystemExit) as missing:
        feedback.main(['apply'])
    assert missing.value.code == 2
    with pytest.raises(feedback.FeedbackError, match='plan ID'):
        feedback.apply_feedback('  ')
    assert calls == []
    assert feedback.apply_feedback('exact-plan', packet_file='packet.json') == {'applied': 1}
    assert calls == [('feedback-apply', ['--file', str(tmp_path/'packet.json'), '--local-url', feedback.DEFAULT_LOCAL_URL,
                                       '--plan-id', 'exact-plan'])]


def test_command_failure_does_not_return_untrusted_stdout_or_credentials(monkeypatch):
    monkeypatch.setattr(feedback.subprocess, 'run', lambda argv, **_: subprocess.CompletedProcess(argv, 1, 'secret output'))
    with pytest.raises(feedback.FeedbackError) as error:
        feedback.preview_feedback(local_token_file='/sensitive/path')
    assert 'secret output' not in str(error.value) and '/sensitive/path' not in str(error.value)


def test_command_error_exposes_bounded_actionable_stderr_and_redacts_env_token(monkeypatch):
    monkeypatch.setenv('ANNOTATION_TOKEN', 'sensitive-bearer')

    def run(argv, **options):
        options['stderr'].write(b'x'*10000 + b'\nconnection refused; sensitive-bearer')
        return subprocess.CompletedProcess(argv, 1, '')

    monkeypatch.setattr(feedback.subprocess, 'run', run)
    with pytest.raises(feedback.FeedbackError) as error:
        feedback.preview_feedback()
    assert 'connection refused' in str(error.value) and 'sensitive-bearer' not in str(error.value)
    assert len(str(error.value)) < feedback.STDERR_LIMIT + 100


def test_command_timeout_is_actionable(monkeypatch):
    def run(argv, **options):
        raise subprocess.TimeoutExpired(argv, options['timeout'])

    monkeypatch.setattr(feedback.subprocess, 'run', run)
    with pytest.raises(feedback.FeedbackError, match='feedback-preview timed out'):
        feedback.preview_feedback()


def test_mcp_discovery_and_apply_forward_exact_plan(monkeypatch):
    from scripts.ml.mcp_server import server

    tools = {tool.name: tool.model_dump(by_alias=True) for tool in asyncio.run(server.list_tools())}
    assert tools['preview_fieldwork_feedback']['annotations']['readOnlyHint'] is True
    assert tools['pull_fieldwork_feedback']['annotations']['readOnlyHint'] is False
    assert tools['apply_fieldwork_feedback']['inputSchema']['required'] == ['plan_id']
    assert tools['pull_fieldwork_feedback']['inputSchema']['properties']['dry_run']['default'] is False
    # Push / training-artefact surface (docs/annotation/local-to-fieldwork.md): read-only hints must match.
    push_tools = {'plan_fieldwork_push': True, 'apply_fieldwork_push': False, 'fieldwork_push_status': True,
                  'publish_training_artefacts': False, 'list_training_manifests': True,
                  'pull_training_artefacts': False}
    for name, read_only in push_tools.items():
        assert tools[name]['annotations']['readOnlyHint'] is read_only, name
        assert tools[name]['annotations']['destructiveHint'] is False, name
        assert 'training' in tools[name]['description'].lower(), name
    assert tools['apply_fieldwork_push']['inputSchema']['required'] == ['run_id']
    assert tools['publish_training_artefacts']['inputSchema']['properties']['dry_run']['default'] is True
    assert tools['pull_training_artefacts']['inputSchema']['required'] == ['out']
    assert tools['fieldwork_push_status']['annotations']['openWorldHint'] is False
    calls = []
    monkeypatch.setattr(feedback, 'apply_feedback', lambda plan_id, **kwargs: calls.append((plan_id, kwargs)) or {'applied': 1})
    result = asyncio.run(server.call_tool('apply_fieldwork_feedback', {'plan_id': 'reviewed-plan', 'packet_file': 'packet.json'}))
    assert not result.model_dump(by_alias=True)['isError'] and json.loads(result.content[0].text) == {'applied': 1}
    assert calls == [('reviewed-plan', {'packet_file': 'packet.json', 'local_url': None, 'local_token_file': None, 'item_id': None})]


def test_mcp_pull_uses_automatic_default(monkeypatch):
    from scripts.ml.mcp_server import server

    calls = []
    monkeypatch.setattr(feedback, 'pull_feedback', lambda **kwargs: calls.append(kwargs) or {'status': 'up_to_date'})
    result = asyncio.run(server.call_tool('pull_fieldwork_feedback', {}))
    assert json.loads(result.content[0].text)['status'] == 'up_to_date'
    assert calls[0]['dry_run'] is False and calls[0]['skip_preview'] is False


def test_empty_pull_is_up_to_date_without_apply(monkeypatch, tmp_path):
    calls = []

    def run(command, flags):
        calls.append(command)
        return {'cursor': '538', 'sync': {'changes': 0}, 'entries': 0} if command == 'feedback' else no_changes()

    monkeypatch.setattr(feedback, '_run', run)
    result = feedback.pull_feedback(packet_file='packet.json')
    assert result['status'] == 'up_to_date' and result['cursor'] == '538'
    assert calls == ['feedback', 'feedback-preview']
    assert not (tmp_path/'packet.json.apply.json').exists()


def test_mixed_conflicts_apply_only_compatible_local_selection(monkeypatch, tmp_path, capsys):
    calls = []

    def run(command, flags):
        calls.append((command, flags))
        if command == 'feedback':
            (tmp_path/'packet.json').write_text('{"source":"unchanged"}')
            return {'cursor': '539', 'entries': 2, 'sync': {'changes': 2}}
        if command == 'feedback-preview':
            if '--id' in flags:
                assert flags[flags.index('--id')+1] == 'scene-1'
                return ready_plan()
            return {'planId': NEW_PLAN, 'canApply': False, 'counts': {'conflicts': 2},
                    'scenes': [*ready_plan()['scenes'], {'itemId': 'scene-2', 'status': 'conflict',
                               'reviewUrl': '/scenes?scene=scene-2', 'conflicts': ['ring overlaps a local edit']}],
                    'quarantined': [{'itemId': 'scene-3', 'reason': 'source changed'}]}
        assert flags[flags.index('--id')+1] == 'scene-1'
        return applied_scene()

    monkeypatch.setattr(feedback, '_run', run)
    assert feedback.main(['pull', '--file', 'packet.json']) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'conflicts' and result['counts']['appliedScenes'] == 1
    assert result['counts']['conflicts'] == 2 and len(result['conflicts']) == 2
    assert [command for command, _ in calls] == ['feedback', 'feedback-preview', 'feedback-preview', 'feedback-apply']
    assert not (tmp_path/'packet.json.apply.json').exists()


def seed_pending(tmp_path, *, item_id=None):
    packet = str(tmp_path/'packet.json')
    Path(packet).write_text('{"original":"packet"}')
    binding = {'origin': feedback.DEFAULT_REMOTE_URL, 'projectId': feedback.DEFAULT_PROJECT,
               'selection': None, 'localUrl': feedback.DEFAULT_LOCAL_URL}
    return packet, feedback._new_checkpoint(packet, binding, ready_plan(), item_id)


def test_interrupted_apply_retries_exact_preserved_packet_before_new_download(monkeypatch, tmp_path):
    packet = tmp_path/'packet.json'
    calls = []
    attempt = 0

    def run(command, flags):
        nonlocal attempt
        calls.append(command)
        if command == 'feedback':
            packet.write_text('{"packet":"original"}' if attempt == 0 else '{"packet":"newer"}')
            return {'entries': 1, 'cursor': str(539+attempt)}
        if command == 'feedback-preview':
            return ready_plan() if attempt == 0 else no_changes()
        attempt += 1
        preserved = Path(flags[flags.index('--file')+1])
        assert preserved.read_text() == '{"packet":"original"}'
        assert flags[flags.index('--plan-id')+1] == PLAN
        if attempt == 1:
            raise feedback.FeedbackError('Lost connection after save')
        return applied_scene(already=True)

    monkeypatch.setattr(feedback, '_run', run)
    first = feedback.pull_feedback(packet_file='packet.json')
    assert first['status'] == 'incomplete' and (tmp_path/'packet.json.apply.json').exists()
    # Even an external refresh of the normal output cannot replace the protected retry copy.
    packet.write_text('{"packet":"unrelated refresh"}')
    result = feedback.pull_feedback(packet_file='packet.json')
    assert result['status'] == 'synced' and result['resumed'] is True
    assert result['counts']['alreadyAppliedScenes'] == 1 and result['counts']['appliedScenes'] == 0
    assert calls == ['feedback', 'feedback-preview', 'feedback-apply', 'feedback-apply', 'feedback', 'feedback-preview']
    assert not (tmp_path/'packet.json.apply.json').exists()


def test_initial_stale_no_write_checkpoint_is_repreviewed_automatically(monkeypatch, tmp_path):
    packet, _ = seed_pending(tmp_path)
    calls = []

    def run(command, flags):
        calls.append((command, flags))
        if command == 'feedback-apply':
            plan = flags[flags.index('--plan-id')+1]
            if plan == PLAN:
                raise feedback.FeedbackError('stale before write', {'status': 409, 'error': feedback.INITIAL_STALE_ERROR})
            assert plan == NEW_PLAN
            return applied_scene()
        if command == 'feedback-preview':
            return ready_plan(NEW_PLAN) if flags[flags.index('--file')+1].endswith('.pending.json') else no_changes()
        return {'entries': 0, 'cursor': '540'}

    monkeypatch.setattr(feedback, '_run', run)
    result = feedback.pull_feedback(packet_file=packet)
    assert result['status'] == 'synced' and result['counts']['appliedScenes'] == 1
    assert [c[0] for c in calls[:3]] == ['feedback-apply', 'feedback-preview', 'feedback-apply']
    assert not (tmp_path/'packet.json.apply.json').exists()


def test_partial_or_uncertain_conflict_keeps_original_checkpoint(monkeypatch, tmp_path):
    packet, _ = seed_pending(tmp_path, item_id='scene-1')
    calls = []

    def run(command, flags):
        calls.append(command)
        assert command == 'feedback-apply' and flags[flags.index('--id')+1] == 'scene-1'
        raise feedback.FeedbackError('local scene changed', {'status': 409, 'error': 'local scene changed',
                                                           'retryPlanId': PLAN, 'appliedCount': 0})

    monkeypatch.setattr(feedback, '_run', run)
    result = feedback.pull_feedback(packet_file=packet)
    assert result['status'] == 'conflicts' and result['phase'] == 'resume'
    assert calls == ['feedback-apply']
    assert json.loads((tmp_path/'packet.json.apply.json').read_text())['planId'] == PLAN


def test_server_authorized_replan_preserves_prefix_counts_across_interruption(monkeypatch, tmp_path):
    packet, _ = seed_pending(tmp_path)
    calls = []
    second_attempts = 0

    def run(command, flags):
        nonlocal second_attempts
        calls.append(command)
        if command == 'feedback-apply':
            plan = flags[flags.index('--plan-id')+1]
            if plan == PLAN:
                raise feedback.FeedbackError('safe stale suffix', {
                    'status': 409, 'retryPlanId': PLAN, 'canReplan': True, 'appliedCount': 1,
                    'appliedScenes': 1, 'alreadyAppliedScenes': 0, 'receiptOnly': 0, 'reviewUrls': ['/scenes?scene=prefix']})
            assert plan == NEW_PLAN
            second_attempts += 1
            if second_attempts == 1:
                raise feedback.FeedbackError('connection interrupted')
            return applied_scene('scene-2')
        if command == 'feedback-preview':
            return ready_plan(NEW_PLAN, 'scene-2') if flags[flags.index('--file')+1].endswith('.pending.json') else no_changes()
        return {'cursor': '542', 'entries': 0}

    monkeypatch.setattr(feedback, '_run', run)
    failed = feedback.pull_feedback(packet_file=packet)
    assert failed['status'] == 'incomplete' and failed['counts']['appliedScenes'] == 1
    checkpoint = json.loads((tmp_path/'packet.json.apply.json').read_text())
    assert checkpoint['planId'] == NEW_PLAN and checkpoint['completedCounts']['appliedScenes'] == 1
    result = feedback.pull_feedback(packet_file=packet)
    assert result['status'] == 'synced' and result['counts']['appliedScenes'] == 2
    assert result['reviewUrls'] == ['/scenes?scene=prefix', '/scenes?scene=scene-2']
    assert calls[:3] == ['feedback-apply', 'feedback-preview', 'feedback-apply']


def test_server_safe_replan_still_refuses_a_conflicting_preview(monkeypatch, tmp_path):
    packet, checkpoint = seed_pending(tmp_path)
    calls = []

    def run(command, flags):
        calls.append(command)
        if command == 'feedback-apply':
            raise feedback.FeedbackError('safe stale suffix', {'status': 409, 'retryPlanId': PLAN, 'canReplan': True, 'appliedScenes': 1})
        return {'canApply': False, 'scenes': [{'itemId': 'scene-2', 'status': 'conflict', 'conflicts': ['overlap']}], 'quarantined': []}

    monkeypatch.setattr(feedback, '_run', run)
    result = feedback.pull_feedback(packet_file=packet)
    assert result['status'] == 'conflicts' and result['counts']['appliedScenes'] == 1
    assert calls == ['feedback-apply', 'feedback-preview']
    assert json.loads((tmp_path/'packet.json.apply.json').read_text()) == checkpoint


@pytest.mark.parametrize('mode', ['dry_run', 'skip_preview'])
def test_dry_run_and_download_never_resume_or_replace_pending_apply(monkeypatch, tmp_path, mode):
    packet, checkpoint = seed_pending(tmp_path)
    calls = []

    def run(command, flags):
        calls.append(command)
        assert command != 'feedback-apply'
        if command == 'feedback':
            Path(packet).write_text('{"new":"download"}')
            return {'entries': 1, 'cursor': '541'}
        return ready_plan(NEW_PLAN)

    monkeypatch.setattr(feedback, '_run', run)
    result = feedback.pull_feedback(packet_file=packet, **{mode: True})
    assert result['status'] == ('preview' if mode == 'dry_run' else 'downloaded')
    assert result['pendingApplyPreserved'] is True
    assert json.loads((tmp_path/'packet.json.apply.json').read_text()) == checkpoint
    assert (tmp_path/'packet.json.pending.json').read_text() == '{"original":"packet"}'
    assert calls == (['feedback', 'feedback-preview'] if mode == 'dry_run' else ['feedback'])


@pytest.mark.parametrize('changed', ['packet', 'binding'])
def test_bad_checkpoint_blocks_before_network_and_preserves_files(monkeypatch, tmp_path, changed):
    packet, _ = seed_pending(tmp_path)
    monkeypatch.setattr(feedback, '_run', lambda *_: pytest.fail('Invalid state must be rejected before network'))
    if changed == 'packet':
        (tmp_path/'packet.json.pending.json').write_text('{"corrupt":true}')
    with pytest.raises(feedback.FeedbackError, match='checkpoint cannot be verified'):
        feedback.pull_feedback(packet_file=packet, **({'local_url': 'http://localhost:3999'} if changed == 'binding' else {}))
    assert (tmp_path/'packet.json.apply.json').exists()


def test_whole_pull_lock_prevents_concurrent_packet_replacement(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, '_run', lambda *_: pytest.fail('Locked pull must not make requests'))
    with (tmp_path/'packet.json.pull.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(feedback.FeedbackError, match='Another pull'):
            feedback.pull_feedback(packet_file='packet.json')


def test_checkpoint_write_failure_prevents_apply(monkeypatch, tmp_path):
    calls = []

    def run(command, flags):
        calls.append(command)
        if command == 'feedback':
            (tmp_path/'packet.json').write_text('{}')
            return {'entries': 1}
        return ready_plan()

    def fail_save(*_):
        raise OSError('disk full')

    monkeypatch.setattr(feedback, '_run', run)
    monkeypatch.setattr(feedback, '_save_checkpoint', fail_save)
    result = feedback.pull_feedback(packet_file='packet.json')
    assert result['status'] == 'incomplete' and result['phase'] == 'checkpoint'
    assert calls == ['feedback', 'feedback-preview']


def test_structured_local_error_is_retained_for_safe_recovery(monkeypatch):
    response = {'status': 409, 'error': feedback.INITIAL_STALE_ERROR}
    monkeypatch.setattr(feedback.subprocess, 'run', lambda argv, **_: subprocess.CompletedProcess(argv, 1, json.dumps(response)))
    with pytest.raises(feedback.FeedbackError) as error:
        feedback.apply_feedback(PLAN)
    assert error.value.response == response
