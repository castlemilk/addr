import fcntl
import json

import pytest
from scripts.ml import fieldwork_feedback as feedback
from scripts.ml import fieldwork_push as push


@pytest.fixture(autouse=True)
def clean_config(monkeypatch, tmp_path):
    for key in (*[key for key in feedback.os.environ if key.startswith('FIELDWORK_')], 'ANNOTATION_URL', 'ANNOTATION_TOKEN'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(feedback, 'ROOT', tmp_path)


def record(monkeypatch, response=None):
    calls = []

    def run(command, flags, timeout=None):
        calls.append((command, flags, timeout))
        return dict(response or {'runId': '20260929T000000Z-deadbeef', 'counts': {'create': 2, 'current': 1}, 'planFile': 'plan.json'})

    monkeypatch.setattr(push, '_run', run)
    return calls


def test_dry_run_forwards_exact_argv_and_never_writes(monkeypatch, tmp_path):
    calls = record(monkeypatch)
    result = push.push_fieldwork(dry_run=True, scenes=['house-25822', ' roof_training_250-25018 '], remote_url='http://127.0.0.1:18131/',
                                 token_file='tok', mirror_dir='mirror dir', root='root dir', review_history='split', concurrency=2)
    assert result['status'] == 'planned' and result['dryRun'] is True and result['projectId'] == 'mooroolbark'
    (command, flags, timeout), = calls
    assert command == 'push' and timeout == push.PUSH_TIMEOUT
    assert flags[:6] == ['--project', 'mooroolbark', '--dir', str(tmp_path/'mirror dir'), '--url', 'http://127.0.0.1:18131']
    assert flags[flags.index('--root')+1] == str(tmp_path/'root dir')
    assert '--dry-run' in flags and flags[flags.index('--scenes')+1] == 'house-25822,roof_training_250-25018'
    assert flags[flags.index('--token-file')+1] == str(tmp_path/'tok')
    assert flags[flags.index('--review-history')+1] == 'split' and '--mark-superseded=true' in flags
    assert flags[flags.index('--concurrency')+1] == '2'
    assert '--run' not in flags and '--allow-unrecovered' not in flags and '--skip-outside-inventory' not in flags
    assert (tmp_path/'mirror dir'/'push'/'push.wrapper.lock').exists()


def test_run_resume_and_options_forward(monkeypatch):
    calls = record(monkeypatch, {'runId': 'r', 'counts': {'create': 1}, 'result': {'created': 1}})
    result = push.push_fieldwork(run_id='20260929T000000Z-deadbeef', remote_url='http://localhost:18131', token_file='t',
                                 mark_superseded=False, skip_outside_inventory=True, allow_unrecovered=True)
    flags = calls[0][1]
    assert flags[flags.index('--run')+1] == '20260929T000000Z-deadbeef'
    assert '--mark-superseded=false' in flags and '--skip-outside-inventory' in flags and '--allow-unrecovered' in flags
    assert '--dry-run' not in flags and result['status'] == 'pushed'


def test_blocked_scenes_report_partial_and_up_to_date(monkeypatch):
    record(monkeypatch, {'runId': 'r', 'counts': {'create': 1, 'blocked_remote_edits': 1}, 'blocked': [{'sceneId': 'x', 'action': 'blocked_remote_edits'}]})
    assert push.push_fieldwork(remote_url='http://localhost:1', token_file='t')['status'] == 'partial'
    record(monkeypatch, {'runId': 'r', 'counts': {'current': 3}})
    assert push.push_fieldwork(remote_url='http://localhost:1', token_file='t')['status'] == 'up_to_date'


@pytest.mark.parametrize('use_env', [False, True])
def test_custom_remote_never_receives_implicit_production_token(monkeypatch, use_env):
    calls = record(monkeypatch)
    if use_env:
        monkeypatch.setenv('ANNOTATION_URL', 'https://example.test')
    with pytest.raises(feedback.FeedbackError, match='custom Fieldwork URL requires'):
        push.push_fieldwork(dry_run=True, **({} if use_env else {'remote_url': 'https://example.test'}))
    assert calls == []
    monkeypatch.setenv('ANNOTATION_TOKEN', 'env-secret')
    push.push_fieldwork(dry_run=True, remote_url='https://example.test')
    assert '--token-file' not in calls[0][1] and 'env-secret' not in json.dumps(calls[0][1])


@pytest.mark.parametrize('origin', ['http://127.0.0.1:18151', 'http://localhost:18151/', 'http://[::1]:18151'])
def test_loopback_dev_server_needs_no_token(monkeypatch, origin):
    calls = record(monkeypatch)
    assert push.push_fieldwork(dry_run=True, remote_url=origin)['status'] == 'planned'
    flags = calls[0][1]
    assert '--token-file' not in flags and flags[flags.index('--url')+1] == origin.rstrip('/')


def test_live_origin_is_refused_unless_explicitly_allowed(monkeypatch):
    calls = record(monkeypatch)
    with pytest.raises(feedback.FeedbackError, match='Refusing to push to the live Fieldwork project'):
        push.push_fieldwork()
    assert calls == []
    result = push.push_fieldwork(dry_run=True)
    flags = calls[0][1]
    assert result['status'] == 'planned' and flags[flags.index('--url')+1] == feedback.DEFAULT_REMOTE_URL
    assert flags[flags.index('--token-file')+1].endswith(feedback.DEFAULT_TOKEN_FILE)
    monkeypatch.setenv('FIELDWORK_PUSH_ALLOW_LIVE', '1')
    push.push_fieldwork(scenes='house-25822')
    assert '--dry-run' not in calls[-1][1]


LIVE_SPELLINGS = ['https://fieldwork-annotations.ben-ebsworth.workers.dev:443',
                  'https://FIELDWORK-ANNOTATIONS.ben-ebsworth.workers.dev',
                  'https://fieldwork-annotations.ben-ebsworth.workers.dev/',
                  'https://fieldwork-annotations.ben-ebsworth.workers.dev:8443',
                  'fieldwork-annotations.ben-ebsworth.workers.dev']


@pytest.mark.parametrize('origin', LIVE_SPELLINGS)
def test_live_origin_spellings_cannot_bypass_the_guard(monkeypatch, origin):
    calls = record(monkeypatch)
    with pytest.raises(feedback.FeedbackError, match='Refusing to push to the live Fieldwork project'):
        push.push_fieldwork(remote_url=origin, token_file='deploy/annotation/admin-token.txt')
    monkeypatch.setenv('ANNOTATION_URL', origin)
    with pytest.raises(feedback.FeedbackError, match='Refusing to push to the live Fieldwork project'):
        push.push_fieldwork(token_file='deploy/annotation/admin-token.txt')
    assert calls == []
    assert push.push_fieldwork(dry_run=True)['status'] == 'planned'
    monkeypatch.setenv('FIELDWORK_PUSH_ALLOW_LIVE', '1')
    push.push_fieldwork(scenes='house-25822')
    assert '--dry-run' not in calls[-1][1]


def test_lock_prevents_concurrent_pushes(monkeypatch, tmp_path):
    record(monkeypatch)
    lock = tmp_path/'data'/'fieldwork-sync'/'mooroolbark'/'push'/'push.wrapper.lock'
    lock.parent.mkdir(parents=True)
    with lock.open('a') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(feedback.FeedbackError, match='Another push'):
            push.push_fieldwork(dry_run=True)


def test_failure_keeps_json_and_never_leaks_token(monkeypatch, capsys):
    monkeypatch.setenv('ANNOTATION_TOKEN', 'super-secret-token')

    def run(command, flags, timeout=None):
        raise feedback.FeedbackError('Fieldwork push failed (exit 1): SaveItem 503', {'runId': 'r', 'counts': {'create': 2}, 'result': {'created': 1}, 'resume': 'annotate push --run r'})

    monkeypatch.setattr(push, '_run', run)
    result = push.push_fieldwork(remote_url='http://localhost:18131')
    assert result['status'] == 'incomplete' and result['result']['created'] == 1 and 'resume' in result
    assert 'super-secret-token' not in json.dumps(result)
    monkeypatch.setattr(push, '_run', lambda command, flags, timeout=None: (_ for _ in ()).throw(feedback.FeedbackError('boom')))
    assert push.main(['push', '--url', 'http://localhost:18131', '--dry-run']) == 1
    assert 'boom' in capsys.readouterr().err


def test_status_needs_no_network_or_token(monkeypatch, capsys):
    calls = record(monkeypatch, {'receiptsDir': 'x', 'scenes': [], 'items': 0, 'runs': []})
    result = push.push_status(scenes='a,b')
    command, flags, _ = calls[0]
    assert command == 'push-status' and '--url' not in flags and '--token-file' not in flags
    assert flags[flags.index('--scenes')+1] == 'a,b' and result['projectId'] == 'mooroolbark'
    assert push.main(['status', '--dir', 'm']) == 0
    assert json.loads(capsys.readouterr().out)['items'] == 0


@pytest.mark.parametrize('project', ['bad project', '', '../x'])
def test_project_validation_precedes_process(monkeypatch, project):
    calls = record(monkeypatch)
    with pytest.raises(feedback.FeedbackError):
        push.push_fieldwork(project_id=project, dry_run=True)
    assert calls == []


def test_cli_dry_run_maps_flags(monkeypatch, capsys):
    calls = record(monkeypatch)
    assert push.main(['push', '--dry-run', '--scenes', 'a', '--url', 'http://localhost:18131', '--token-file', 't', '--no-mark-superseded', '--review-history', 'split']) == 0
    flags = calls[0][1]
    assert '--dry-run' in flags and '--mark-superseded=false' in flags and flags[flags.index('--review-history')+1] == 'split'
    assert json.loads(capsys.readouterr().out)['status'] == 'planned'
