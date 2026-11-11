"""Publish changed local scene frames and reviews to Fieldwork as new immutable items.

Push never edits Fieldwork overrides, never deletes, and refuses scenes whose
Fieldwork items carry unpulled edits. A pushed item is a model estimate plus the
local review state as of publication; nothing it carries is a human label.
Writes against the live Fieldwork origin are refused unless FIELDWORK_PUSH_ALLOW_LIVE=1.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from scripts.ml.fieldwork_feedback import (
    DEFAULT_REMOTE_URL,
    DEFAULT_TOKEN_FILE,
    FeedbackError,
    _path,
    _project,
    _run,
    _setting,
    is_live_origin,
    is_loopback,
)

PUSH_TIMEOUT = 6 * 3600
REVIEW_HISTORY = ('omit', 'split')


def _mirror_dir(project, mirror_dir=None):
    return _path(_setting(mirror_dir, 'FIELDWORK_SYNC_DIR', f'data/fieldwork-sync/{project}'))


def _origin(remote_url=None):
    return _setting(remote_url, 'ANNOTATION_URL', DEFAULT_REMOTE_URL).rstrip('/')


def _token_flags(origin, token_file=None):
    token = _setting(token_file, 'FIELDWORK_TOKEN_FILE')
    if not token and not os.environ.get('ANNOTATION_TOKEN'):
        if is_live_origin(origin):
            token = DEFAULT_TOKEN_FILE
        elif not is_loopback(origin):
            raise FeedbackError('A custom Fieldwork URL requires ANNOTATION_TOKEN or an explicit --token-file / '
                                'FIELDWORK_TOKEN_FILE (a loopback --dev server needs none)')
    return ['--token-file', _path(token)] if token else []


def _guard_live(origin, dry_run):
    # Hostname-normalised: upper-case, :443/other ports and a trailing slash are still the live project.
    if not dry_run and is_live_origin(origin) and os.environ.get('FIELDWORK_PUSH_ALLOW_LIVE') != '1':
        raise FeedbackError('Refusing to push to the live Fieldwork project. Plan with --dry-run, or set '
                            'FIELDWORK_PUSH_ALLOW_LIVE=1 for an explicitly authorised push (start with --scenes).')


@contextmanager
def _push_lock(directory):
    # The Go command flocks push.lock itself; a second flock on the same inode from
    # this process would block it, so the wrapper serialises on its own file.
    path = Path(directory) / 'push' / 'push.wrapper.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise FeedbackError('Another push is using this mirror; wait for it to finish, then rerun task push') from error
        yield


def _scene_list(scenes):
    if scenes is None:
        return None
    if isinstance(scenes, str):
        scenes = scenes.split(',')
    cleaned = [scene.strip() for scene in scenes if scene and scene.strip()]
    return cleaned or None


def push_fieldwork(project_id=None, scenes=None, dry_run=False, run_id=None, mirror_dir=None, remote_url=None,
                   token_file=None, mark_superseded=True, review_history='omit', skip_outside_inventory=False,
                   allow_unrecovered=False, root=None, concurrency=None, show_all=False):
    """Plan (and unless dry_run, apply) a push; returns the CLI's JSON summary."""
    project = _project(project_id)
    if review_history not in REVIEW_HISTORY:
        raise FeedbackError('--review-history must be omit or split')
    origin = _origin(remote_url)
    _guard_live(origin, dry_run)
    directory = _mirror_dir(project, mirror_dir)
    flags = ['--project', project, '--dir', directory, '--url', origin, '--root', _path(root or '.'),
             '--review-history', review_history, f'--mark-superseded={"true" if mark_superseded else "false"}']
    flags += _token_flags(origin, token_file)
    if dry_run:
        flags.append('--dry-run')
    if run_id:
        flags += ['--run', run_id]
    if selected := _scene_list(scenes):
        flags += ['--scenes', ','.join(selected)]
    if skip_outside_inventory:
        flags.append('--skip-outside-inventory')
    if allow_unrecovered:
        flags.append('--allow-unrecovered')
    if concurrency:
        flags += ['--concurrency', str(int(concurrency))]
    if show_all:
        flags.append('--all')
    with _push_lock(directory):
        try:
            result = _run('push', flags, timeout=PUSH_TIMEOUT)
        except FeedbackError as error:
            if error.response:
                return {**error.response, 'status': 'incomplete', 'error': str(error),
                        'nextStep': 'Receipts and uploaded assets are preserved; rerun with the reported resume command.'}
            raise
    counts = result.get('counts', {})
    blocked = result.get('blocked', [])
    if dry_run:
        result['status'] = 'planned'
    elif blocked:
        result['status'] = 'partial'
    else:
        result['status'] = 'pushed' if counts.get('create') else 'up_to_date'
    result['dryRun'] = bool(dry_run)
    result['projectId'] = project
    return result


def push_status(project_id=None, mirror_dir=None, scenes=None):
    """Summarise push receipts; no network."""
    project = _project(project_id)
    flags = ['--dir', _mirror_dir(project, mirror_dir)]
    if selected := _scene_list(scenes):
        flags += ['--scenes', ','.join(selected)]
    result = _run('push-status', flags)
    result['projectId'] = project
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    push = commands.add_parser('push', help='Plan and publish changed local scenes as new Fieldwork items')
    push.add_argument('--dry-run', action='store_true', help='Plan only: sync, classify, HEAD assets; no writes')
    push.add_argument('--scenes', help='Comma-separated scene ids (default: all local scenes)')
    push.add_argument('--run', dest='run_id', help='Apply or resume a saved plan by run id')
    push.add_argument('--url', dest='remote_url', help='Fieldwork URL (ANNOTATION_URL)')
    push.add_argument('--token-file', help='Bearer token file (FIELDWORK_TOKEN_FILE)')
    push.add_argument('--dir', dest='mirror_dir', help='Mirror directory (FIELDWORK_SYNC_DIR)')
    push.add_argument('--root', help='Repository root holding data/scenes (default .)')
    push.add_argument('--no-mark-superseded', dest='mark_superseded', action='store_false', help='Do not tag older items superseded-frame')
    push.add_argument('--review-history', choices=REVIEW_HISTORY, default='omit', help='Publish review history as a separate asset')
    push.add_argument('--skip-outside-inventory', action='store_true', help='Omit scenes absent from the frozen inventory')
    push.add_argument('--allow-unrecovered', action='store_true', help='Push over unpulled Fieldwork edits and record them')
    push.add_argument('--concurrency', type=int, help='Parallel scene uploads')
    push.add_argument('--all', dest='show_all', action='store_true', help='Print current scenes in the table too')
    status = commands.add_parser('status', help='Summarise push receipts without contacting Fieldwork')
    status.add_argument('--dir', dest='mirror_dir', help='Mirror directory (FIELDWORK_SYNC_DIR)')
    status.add_argument('--scenes', help='Comma-separated scene ids')
    for command in (push, status):
        command.add_argument('--project', dest='project_id', help='Project ID (FIELDWORK_PROJECT; default mooroolbark)')
    args = vars(parser.parse_args(argv))
    command = args.pop('command')
    try:
        result = {'push': push_fieldwork, 'status': push_status}[command](**args)
    except FeedbackError as error:
        print(str(error), file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    print()
    return 0 if result.get('status') not in ('incomplete',) else 1


if __name__ == '__main__':
    sys.exit(main())
