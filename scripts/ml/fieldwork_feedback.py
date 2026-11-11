"""Pull Fieldwork feedback into local scene reviews, preserving conflicts and draft checks."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROJECT = 'mooroolbark'
DEFAULT_SNAPSHOT = 'data/annotation-snapshots/mooroolbark-23591d058ab7'
DEFAULT_REMOTE_URL = 'https://fieldwork-annotations.ben-ebsworth.workers.dev'
DEFAULT_LOCAL_URL = 'http://localhost:3008'
DEFAULT_TOKEN_FILE = 'deploy/annotation/admin-token.txt'
PROJECT_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}')
STDERR_LIMIT = 8192
MAX_PACKET_BYTES = 64 * 1024 * 1024
INITIAL_STALE_ERROR = 'The local review, receipt or packet changed since preview. Preview again; no feedback was applied.'
OUTCOME_COUNTS = ('appliedScenes', 'alreadyAppliedScenes', 'receiptOnly')


class FeedbackError(RuntimeError):
    """A command failed without exposing its credential arguments."""

    def __init__(self, message, response=None):
        super().__init__(message)
        self.response = response or {}


def _setting(value, name, default=None):
    return value if value is not None else os.environ.get(name) or default


def _project(value=None):
    project = _setting(value, 'FIELDWORK_PROJECT', DEFAULT_PROJECT)
    if not isinstance(project, str) or not PROJECT_ID.fullmatch(project):
        raise FeedbackError('Project ID must be 1–128 letters, digits, hyphens or underscores, starting with a letter or digit')
    return project


def _path(value):
    path = Path(value).expanduser()
    return str(path if path.is_absolute() else ROOT / path)


def _packet_file(value=None, project_id=None, mirror_dir=None):
    project = _project(project_id)
    directory = _setting(mirror_dir, 'FIELDWORK_SYNC_DIR', f'data/fieldwork-sync/{project}')
    return _path(_setting(value, 'FIELDWORK_FEEDBACK_FILE', str(Path(directory) / 'feedback.json')))


def _stderr_detail(stream):
    stream.seek(0, os.SEEK_END)
    stream.seek(max(0, stream.tell() - STDERR_LIMIT))
    detail = stream.read(STDERR_LIMIT).decode('utf-8', errors='replace').strip()
    if token := os.environ.get('ANNOTATION_TOKEN'):
        detail = detail.replace(token, '[redacted]')
    return f': {detail}' if detail else ''


def is_loopback(origin):
    """True for localhost / 127.x / ::1 origins; a `--dev` annotation-server never checks a bearer."""
    host = urlsplit(origin if '://' in origin else f'http://{origin}').hostname or ''
    return host in ('localhost', '::1') or host.startswith('127.')


def is_live_origin(origin):
    """True when origin names the live Fieldwork host in any spelling.

    Only the normalised hostname is compared (lower case, trailing dots stripped:
    "host." is the same DNS name): scheme, port (Cloudflare serves several HTTPS
    ports), case, path and a trailing slash or dot must not bypass the guard.
    """
    host = (urlsplit(origin if '://' in origin else f'https://{origin}').hostname or '').lower().rstrip('.')
    return host == urlsplit(DEFAULT_REMOTE_URL).hostname


def _run(command, flags, timeout=None):
    # Keep authentication in Go's existing file/env handling and never log argv.
    # Spool diagnostics instead of retaining unbounded stderr in memory; return only its tail.
    if timeout is None:
        timeout = 600 if command == 'feedback' else 120
    with tempfile.TemporaryFile() as stderr:
        try:
            result = subprocess.run(['go', 'run', './cmd/annotate', command, *flags],
                                    cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=stderr,
                                    timeout=timeout, check=False)
        except subprocess.TimeoutExpired as error:
            raise FeedbackError(f'Fieldwork {command} timed out{_stderr_detail(stderr)}') from error
        except OSError as error:
            raise FeedbackError(f'Could not start go run for {command}: {error}') from error
        detail = _stderr_detail(stderr)
    try:
        decoded = json.loads(result.stdout)
    except ValueError:
        decoded = None
    if result.returncode:
        response = decoded if command in ('feedback-preview', 'feedback-apply', 'push') and isinstance(decoded, dict) else None
        raise FeedbackError(f'Fieldwork {command} failed (exit {result.returncode}){detail}', response)
    if not isinstance(decoded, dict):
        raise FeedbackError(f'Fieldwork {command} returned invalid JSON or an unexpected response')
    return decoded


def _local_flags(packet_file, local_url=None, local_token_file=None, item_id=None):
    flags = ['--file', packet_file, '--local-url', _setting(local_url, 'ADDR_APP_URL', DEFAULT_LOCAL_URL)]
    token_file = _setting(local_token_file, 'FIELDWORK_LOCAL_TOKEN_FILE')
    if token_file:
        flags += ['--local-token-file', _path(token_file)]
    if item_id is not None:
        flags += ['--id', item_id]
    return flags


def preview_feedback(*, packet_file=None, local_url=None, local_token_file=None, item_id=None):
    """Read the saved packet and current local scene state; do not apply it."""
    return _run('feedback-preview', _local_flags(_packet_file(packet_file), local_url, local_token_file, item_id))


def apply_feedback(plan_id, *, packet_file=None, local_url=None, local_token_file=None, item_id=None):
    """Apply only the explicit digest returned by a reviewed, current preview."""
    if not isinstance(plan_id, str) or not plan_id.strip():
        raise FeedbackError('A plan ID from feedback preview is required before applying')
    return _run('feedback-apply', [*_local_flags(_packet_file(packet_file), local_url, local_token_file, item_id),
                                   '--plan-id', plan_id])


@contextmanager
def _pull_lock(packet):
    path = Path(packet + '.pull.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise FeedbackError('Another pull is using this packet; wait for it to finish, then rerun task pull') from error
        yield


def _sync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_bytes(path, data):
    path = Path(path)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        try:
            temp.write(data)
            temp.flush()
            os.fsync(temp.fileno())
            temp.close()
            os.replace(temp.name, path)
            _sync_directory(path.parent)
        finally:
            Path(temp.name).unlink(missing_ok=True)


def _checkpoint_paths(packet):
    return Path(packet + '.apply.json'), Path(packet + '.pending.json')


def _save_checkpoint(path, checkpoint):
    _atomic_bytes(path, json.dumps(checkpoint, sort_keys=True).encode())


def _load_checkpoint(packet, binding):
    path, pending = _checkpoint_paths(packet)
    if not path.exists():
        return None
    try:
        if path.stat().st_size > 65536:
            raise ValueError('checkpoint is too large')
        checkpoint = json.loads(path.read_bytes())
        if (not isinstance(checkpoint, dict) or checkpoint.get('schemaVersion') != 1
                or any(checkpoint.get(key) != value for key, value in binding.items())
                or checkpoint.get('packetFile') != str(pending)
                or not re.fullmatch(r'[a-f0-9]{64}', str(checkpoint.get('planId', '')))
                or not isinstance(checkpoint.get('counts'), dict)
                or any(type(value) is not int or value < 0 for value in checkpoint['counts'].values())
                or not isinstance(checkpoint.get('completedCounts', {}), dict)
                or any(type(value) is not int or value < 0 for value in checkpoint.get('completedCounts', {}).values())
                or not isinstance(checkpoint.get('completedReviewUrls', []), list)
                or any(not isinstance(value, str) for value in checkpoint.get('completedReviewUrls', []))
                or checkpoint.get('itemId') is not None and not isinstance(checkpoint['itemId'], str)):
            raise ValueError('checkpoint does not match this pull')
        with pending.open('rb') as source:
            if hashlib.file_digest(source, 'sha256').hexdigest() != checkpoint.get('packetSha256'):
                raise ValueError('preserved packet checksum differs')
        return checkpoint
    except (OSError, ValueError, TypeError) as error:
        raise FeedbackError(f'Pending apply checkpoint cannot be verified; preserved it and its packet: {error}') from error


def _new_checkpoint(packet, binding, preview, item_id):
    path, pending = _checkpoint_paths(packet)
    plan_id = preview.get('planId')
    if not isinstance(plan_id, str) or not re.fullmatch(r'[a-f0-9]{64}', plan_id):
        raise FeedbackError('Local preview did not return a valid plan ID; no feedback was applied')
    with Path(packet).open('rb') as source:
        data = source.read(MAX_PACKET_BYTES + 1)
    if len(data) > MAX_PACKET_BYTES:
        raise FeedbackError('Feedback packet exceeds 64 MiB; select one item with --id')
    _atomic_bytes(pending, data)
    checkpoint = {'schemaVersion': 1, **binding, 'packetFile': str(pending),
                  'packetSha256': hashlib.sha256(data).hexdigest(), 'planId': plan_id,
                  'itemId': item_id, 'counts': preview.get('counts', {})}
    _save_checkpoint(path, checkpoint)
    return checkpoint


def _conflicts(preview):
    return [{'itemId': scene['itemId'], 'reviewUrl': scene.get('reviewUrl'), 'reasons': scene.get('conflicts', [])}
            for scene in preview.get('scenes', []) if scene.get('status') == 'conflict'] + [
                {'itemId': entry.get('itemId'), 'reasons': [entry.get('reason', 'Quarantined feedback')]}
                for entry in preview.get('quarantined', [])]


def _completed_outcomes(checkpoint, response):
    counts = {key: checkpoint.get('completedCounts', {}).get(key, 0) + response.get(key, 0) for key in OUTCOME_COUNTS}
    urls = sorted(set(checkpoint.get('completedReviewUrls', [])) | set(response.get('reviewUrls', [])))[:20]
    return counts, urls


def _apply_checkpoint(packet, checkpoint, local_token_file):
    path, pending = _checkpoint_paths(packet)
    local = {'packet_file': str(pending), 'local_url': checkpoint['localUrl'],
             'local_token_file': local_token_file, 'item_id': checkpoint.get('itemId')}
    for attempt in range(2):
        try:
            applied = apply_feedback(checkpoint['planId'], **local)
            if applied.get('complete') is not True:
                raise FeedbackError('Local apply did not finish; its packet and plan are preserved', applied)
        except FeedbackError as error:
            response = error.response
            completed, review_urls = _completed_outcomes(checkpoint, response)
            safe_replan = response.get('canReplan') is True or (
                response.get('error') == INITIAL_STALE_ERROR and not response.get('retryPlanId'))
            if attempt == 0 and response.get('status') == 409 and safe_replan:
                # Only the server can establish that replacing the plan is safe.
                # appliedCount=0 alone is insufficient: a save may precede receipt publication.
                try:
                    preview = preview_feedback(**local)
                except FeedbackError as preview_error:
                    preview_error.response = {**preview_error.response, **completed, 'reviewUrls': review_urls}
                    raise
                if preview.get('canApply') is not True:
                    raise FeedbackError('The saved feedback now conflicts with local changes; resolve the conflict, then rerun task pull',
                                        {'status': 409, 'conflicts': _conflicts(preview), **completed, 'reviewUrls': review_urls}) from error
                plan_id = preview.get('planId')
                if not isinstance(plan_id, str) or not re.fullmatch(r'[a-f0-9]{64}', plan_id):
                    raise FeedbackError('Local preview did not return a valid plan ID; the pending packet is preserved') from error
                checkpoint = {**checkpoint, 'planId': plan_id, 'counts': preview.get('counts', {}),
                              'completedCounts': completed, 'completedReviewUrls': review_urls}
                _save_checkpoint(path, checkpoint)
                continue
            error.response = {**response, **completed, 'reviewUrls': review_urls}
            raise
        path.unlink()
        _sync_directory(path.parent)
        pending.unlink(missing_ok=True)
        return applied, checkpoint


def _record_apply(result, applied, checkpoint):
    scenes = applied.get('applied', [])
    for key in OUTCOME_COUNTS:
        result['counts'][key] += checkpoint.get('completedCounts', {}).get(key, 0)
    result['counts']['appliedScenes'] += sum(not scene.get('alreadyApplied') and not scene.get('receiptOnly') for scene in scenes)
    result['counts']['alreadyAppliedScenes'] += sum(bool(scene.get('alreadyApplied')) for scene in scenes)
    result['counts']['receiptOnly'] += sum(bool(scene.get('receiptOnly')) and not scene.get('alreadyApplied') for scene in scenes)
    result['reviewUrls'] = sorted(set(result['reviewUrls']) | set(checkpoint.get('completedReviewUrls', []))
                                  | {scene['reviewUrl'] for scene in scenes if scene.get('reviewUrl')})[:20]


def _record_failure(result, error, phase):
    result['status'] = 'conflicts' if error.response.get('status') == 409 else 'incomplete'
    result['error'] = str(error)
    result['phase'] = phase
    if conflicts := error.response.get('conflicts'):
        result.setdefault('conflicts', []).extend(conflicts)
    if error.response.get('status') == 409:
        result['counts']['conflicts'] += 1
    if 'appliedCount' in error.response:
        result['appliedBeforeFailure'] = error.response['appliedCount']
    for key in OUTCOME_COUNTS:
        result['counts'][key] += error.response.get(key, 0)
    result['reviewUrls'] = sorted(set(result['reviewUrls']) | set(error.response.get('reviewUrls', [])))[:20]
    action = 'Restore the cloud connection or access' if phase == 'download' else 'Resolve the reported conflict or restore the local app'
    result['nextStep'] = ('Existing feedback and completed saves are preserved. ' + action
                         + ', then rerun task pull with the same options. Any pending apply resumes automatically.')
    return result


def pull_feedback(*, project_id=None, snapshot=None, mirror_dir=None, packet_file=None,
                  remote_url=None, token_file=None, item_id=None, local_url=None,
                  local_token_file=None, skip_preview=False, dry_run=False):
    """Sync cloud documents and apply compatible local feedback; labels stay drafts."""
    if skip_preview and dry_run:
        raise FeedbackError('Choose either --dry-run or --skip-preview')
    project = _project(project_id)
    directory = _setting(mirror_dir, 'FIELDWORK_SYNC_DIR', f'data/fieldwork-sync/{project}')
    packet = str(Path(_packet_file(packet_file, project, directory)).resolve())
    origin = _setting(remote_url, 'ANNOTATION_URL', DEFAULT_REMOTE_URL)
    snapshot_path = _path(_setting(snapshot, 'FIELDWORK_SNAPSHOT', DEFAULT_SNAPSHOT))
    if Path(packet).is_relative_to(Path(snapshot_path).resolve()) or Path(packet) == (Path(_path(directory))/'mirror.json').resolve():
        raise FeedbackError('Feedback output must not replace the mirror or a frozen snapshot file')
    flags = ['--project', project, '--snapshot', snapshot_path,
             '--dir', _path(directory), '--file', packet,
             '--url', origin]
    token = _setting(token_file, 'FIELDWORK_TOKEN_FILE')
    if not token and not os.environ.get('ANNOTATION_TOKEN'):
        if origin.rstrip('/') != DEFAULT_REMOTE_URL:
            raise FeedbackError('A custom Fieldwork URL requires ANNOTATION_TOKEN or an explicit --token-file / FIELDWORK_TOKEN_FILE')
        token = DEFAULT_TOKEN_FILE
    if token:
        flags += ['--token-file', _path(token)]
    if item_id is not None:
        flags += ['--id', item_id]
    binding = {'origin': origin.rstrip('/'), 'projectId': project, 'selection': item_id,
               'localUrl': _setting(local_url, 'ADDR_APP_URL', DEFAULT_LOCAL_URL).rstrip('/')}
    result = {'status': 'up_to_date', 'projectId': project, 'cursor': None, 'packetFile': packet,
              'counts': {'changes': 0, 'items': 0, 'appliedScenes': 0, 'alreadyAppliedScenes': 0,
                         'receiptOnly': 0, 'conflicts': 0}, 'reviewUrls': []}
    try:
        with _pull_lock(packet):
            if not skip_preview and not dry_run:
                checkpoint = _load_checkpoint(packet, binding)
                if checkpoint:
                    try:
                        _record_apply(result, *_apply_checkpoint(packet, checkpoint, local_token_file))
                        result['resumed'] = True
                    except FeedbackError as error:
                        return _record_failure(result, error, 'resume')
            else:
                result['pendingApplyPreserved'] = _checkpoint_paths(packet)[0].exists()
            try:
                pulled = _run('feedback', flags)
            except FeedbackError as error:
                return _record_failure(result, error, 'download')
            result['cursor'] = pulled.get('cursor')
            result['counts'].update(changes=pulled.get('sync', {}).get('changes', 0), items=pulled.get('entries', 0))
            if skip_preview:
                result['status'] = 'downloaded'
                result['counts']['conflicts'] = pulled.get('quarantined', 0)
                return result
            local = {'packet_file': packet, 'local_url': local_url, 'local_token_file': local_token_file}
            try:
                preview = preview_feedback(**local)
            except FeedbackError as error:
                return _record_failure(result, error, 'preview')
            result['conflicts'] = _conflicts(preview)
            result['counts']['conflicts'] = preview.get('counts', {}).get('conflicts', len(result['conflicts']))
            if dry_run:
                result.update(status='preview' if preview.get('canApply') is True else 'conflicts', preview=preview)
                return result
            if preview.get('canApply') is True:
                plans = [(None, preview)] if any(scene.get('status') == 'ready' for scene in preview.get('scenes', [])) else []
            else:
                # Select compatible scenes locally from the same packet. No extra cloud pulls.
                plans = [(scene['itemId'], None) for scene in preview.get('scenes', []) if scene.get('status') == 'ready']
            for selected_id, plan in plans:
                try:
                    if plan is None:
                        plan = preview_feedback(**local, item_id=selected_id)
                    if plan.get('canApply') is not True:
                        result['conflicts'].extend(_conflicts(plan))
                        result['counts']['conflicts'] += max(1, plan.get('counts', {}).get('conflicts', 0))
                        continue
                    checkpoint = _new_checkpoint(packet, binding, plan, selected_id)
                    _record_apply(result, *_apply_checkpoint(packet, checkpoint, local_token_file))
                except FeedbackError as error:
                    return _record_failure(result, error, 'apply')
            if result['counts']['conflicts'] or preview.get('canApply') is not True:
                result.update(status='conflicts', nextStep='Conflicting feedback and any completed saves are preserved; '
                              'resolve the reported conflicts, then rerun task pull with the same options.')
            elif any(result['counts'][key] for key in ('changes', 'appliedScenes', 'alreadyAppliedScenes', 'receiptOnly')):
                result['status'] = 'synced'
    except OSError as error:
        return _record_failure(result, FeedbackError(f'Local sync files could not be saved: {error}'), 'checkpoint')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    pull = commands.add_parser('pull', help='Sync cloud edits into local scenes; keep incoming geometry as drafts')
    pull.add_argument('--project', dest='project_id', help='Project ID (FIELDWORK_PROJECT; default mooroolbark)')
    pull.add_argument('--snapshot', help='Frozen base bundle (FIELDWORK_SNAPSHOT)')
    pull.add_argument('--dir', dest='mirror_dir', help='Incremental mirror directory (FIELDWORK_SYNC_DIR)')
    pull.add_argument('--url', dest='remote_url', help='Cloud service URL (ANNOTATION_URL)')
    pull.add_argument('--token-file', help='Cloud bearer file (FIELDWORK_TOKEN_FILE or the local deployment token file)')
    pull.add_argument('--dry-run', action='store_true', help='Download and preview only; never resume or apply a pending plan')
    pull.add_argument('--skip-preview', action='store_true', help='Save the packet without calling the local app')
    preview = commands.add_parser('preview', help='Preview a saved packet against current local reviews')
    apply = commands.add_parser('apply', help='Apply a reviewed preview through the canonical local scene API')
    apply.add_argument('--plan-id', required=True, help='Exact plan ID returned by the preview')
    for command in (pull, preview, apply):
        command.add_argument('--id', dest='item_id', help='Select one scene item without truncating its changes')
        command.add_argument('--file', dest='packet_file', help='Feedback packet (FIELDWORK_FEEDBACK_FILE)')
        command.add_argument('--local-url', help='Local scene app URL (ADDR_APP_URL; default http://localhost:3008)')
        command.add_argument('--local-token-file', help='Separate local app bearer file (FIELDWORK_LOCAL_TOKEN_FILE)')
    args = vars(parser.parse_args(argv))
    command = args.pop('command')
    try:
        result = {'pull': pull_feedback, 'preview': preview_feedback, 'apply': apply_feedback}[command](**args)
    except FeedbackError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 1 if result.get('status') in ('conflicts', 'incomplete') else 0


if __name__ == '__main__':
    raise SystemExit(main())
