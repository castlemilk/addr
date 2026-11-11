"""Publish training artefacts to Fieldwork as one immutable, content-addressed manifest item.

Selected groups (dataset-vN, distill-*, edit-diffs/<date>, line-corrections/<date>, edit-diff,
sweep, coco-*, eyes) are staged under .run/training-bundles/<manifestSha12>/ in the frozen-bundle
layout that `annotation-migrate upload` already understands (manifest.json + blobs/<sha256>, files
over 64 MiB split into chunks). The manifest is uploaded last and referenced by one addr-report item
in the `addr-training` project. Nothing here trains a model, and a published artefact is never a
human label: the manifest records provenance (code sha, git head, parents) so a training run can
pull exactly what was published (see pull_artefacts.py).

Dry runs read local files only. Applying refuses the live Fieldwork origin unless
FIELDWORK_PUSH_ALLOW_LIVE=1 and never sends the production token to another origin.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from scripts.annotation_snapshot import EXCLUDED_SUFFIXES, add_blob, encoded
from scripts.ml import fieldwork_feedback as feedback
from scripts.ml.fieldwork_feedback import FeedbackError, _path, _run, _setting

ROOT = feedback.ROOT
TRAINING_PROJECT = 'addr-training'
TRAINING_PROJECT_NAME = 'addr training artefacts'
MANIFEST_KIND = 'addr-training-manifest'
ITEM_KIND = 'training-manifest'
STAGING_ROOT = '.run/training-bundles'
PUBLISH_DIR = 'data/fieldwork-sync/addr-training/publish'
EYES_DB = 'data/reviews/eyes/eyes.sqlite'
TRAINING = 'data/reviews/eyes/training'
SCENE_PROJECT = 'mooroolbark'
GROUP_NAME = re.compile(r'^(dataset-v\d+|distill-[A-Za-z0-9._-]+|edit-diffs/\d{4}-\d{2}-\d{2}|line-corrections/\d{4}-\d{2}-\d{2}|edit-diff|sweep|coco-[A-Za-z0-9._-]+|eyes)$')
SKIP_DIRS = {'__pycache__', '.git', '.venv', 'node_modules'}


def _walk(root, relative):
    """Sorted repo-relative posix paths of regular files under root/relative (no WAL/lock/bytecode/symlinks)."""
    folder = Path(root) / relative
    found = []
    for current, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in files:
            path = Path(current) / name
            if name.endswith(EXCLUDED_SUFFIXES) or path.is_symlink() or not path.is_file():
                continue
            found.append(path.relative_to(root).as_posix())
    return sorted(found)


def _size(root, paths):
    return sum((Path(root) / p).stat().st_size for p in paths)


def _excluded(root, path, reason, paths=None):
    entry = {'path': path, 'reason': reason}
    if paths is not None:
        entry['files'] = len(paths)
        entry['bytes'] = _size(root, paths)
    return entry


def resolve_group(root, name, *, with_images=False, tarball=False, include_checkpoints=False):
    """Files of one artefact group: {'name', 'roots', 'files', 'excluded', 'special'}; eyes is staged via VACUUM INTO."""
    if not GROUP_NAME.match(name):
        raise FeedbackError(f'Unknown artefact group {name!r}; use dataset-vN, distill-<set>, edit-diffs/<date>, '
                            'line-corrections/<date>, edit-diff, sweep, coco-<set> or eyes')
    root = Path(root)
    group = {'name': name, 'roots': [], 'files': [], 'excluded': [], 'special': None}
    if name == 'eyes':
        if not (root / EYES_DB).is_file():
            raise FeedbackError(f'{EYES_DB} is missing; the eyes group needs the eyes index')
        group.update(roots=[EYES_DB], special='eyes')
        return group
    if name.startswith(('dataset-v', 'distill-')):
        base = f'{TRAINING}/{name}'
        if not (root / base).is_dir():
            raise FeedbackError(f'{base} is not a directory')
        files = _walk(root, base)
        images = [p for p in files if p.startswith(f'{base}/images/')]
        if not with_images and images:
            files = [p for p in files if p not in set(images)]
            group['excluded'].append(_excluded(root, f'{base}/images', 'images omitted; pass --with-images', images))
        group['roots'].append(base)
        for sibling in (f'{base}.export.json',):
            if (root / sibling).is_file():
                files.append(sibling)
        if tarball:
            tar = f'{base}.tar'
            if (root / tar).is_file():
                files.append(tar)
                if (root / f'{tar}.sha256').is_file():
                    files.append(f'{tar}.sha256')
                group['roots'].append(tar)
            else:
                group['excluded'].append({'path': tar, 'reason': 'tarball not present; run export_dataset --tarball first'})
        group['files'] = sorted(set(files))
        return group
    if name.startswith(('edit-diffs/', 'line-corrections/')):
        base = f'data/reviews/eyes/{name}'
        if not (root / base).is_dir():
            raise FeedbackError(f'{base} is not a directory')
        group.update(roots=[base], files=_walk(root, base))
        return group
    if name == 'edit-diff':
        base = f'{TRAINING}/edit-diff'
        if (root / base).is_dir():
            group.update(roots=[base], files=_walk(root, base))
        else:
            group['excluded'].append({'path': base, 'reason': 'not present; run task training:edit-diff first'})
        return group
    if name == 'sweep':
        base = f'{TRAINING}/sam3-mac/sweep'
        results = f'{base}/results.jsonl'
        if not (root / results).is_file():
            raise FeedbackError(f'{results} is missing')
        files, skipped_logs, checkpoints = [results], [], []
        for path in _walk(root, base):
            if path == results:
                continue
            leaf = Path(path).name
            parts = Path(path).relative_to(base).parts
            if leaf.endswith('.pt'):
                checkpoints.append(path)
            elif leaf.endswith('.log') or leaf == 'steps.jsonl':
                skipped_logs.append(path)
            elif leaf in ('run.json', 'summary.json') and len(parts) in (2, 3):
                files.append(path)
        if checkpoints:
            if include_checkpoints:
                files.extend(checkpoints)
            else:
                group['excluded'].append(_excluded(root, f'{base}/*/**.pt', 'checkpoints omitted; pass --include-checkpoints', checkpoints))
        if skipped_logs:
            group['excluded'].append(_excluded(root, f'{base}/**.log,steps.jsonl', 'training logs stay local', skipped_logs))
        group.update(roots=[base], files=sorted(set(files)))
        return group
    if name.startswith('coco-'):
        base = f'{TRAINING}/sam3-mac/{name}'
        if not (root / base / 'manifest.json').is_file():
            raise FeedbackError(f'{base}/manifest.json is missing')
        files, images = [], []
        for path in _walk(root, base):
            parts = Path(path).relative_to(base).parts
            if 'images' in parts:
                images.append(path)
            elif parts[-1] in ('manifest.json', '_annotations.coco.json', 'sidecar.json'):
                files.append(path)
        if images:
            if with_images:
                files.extend(images)
            else:
                group['excluded'].append(_excluded(root, f'{base}/*/images', 'images omitted; pass --with-images', images))
        group.update(roots=[base], files=sorted(set(files)))
        return group
    raise FeedbackError(f'Unhandled artefact group {name!r}')  # pragma: no cover - GROUP_NAME is exhaustive


def plan(root, select, *, with_images=False, tarball=False, include_checkpoints=False):
    """Per-group file counts and bytes from local files only; nothing is copied, hashed or sent."""
    if not select:
        raise FeedbackError('Select at least one artefact group')
    root = Path(root)
    groups, seen = {}, set()
    for name in dict.fromkeys(select):
        group = resolve_group(root, name, with_images=with_images, tarball=tarball, include_checkpoints=include_checkpoints)
        if group['special'] == 'eyes':
            size = (root / EYES_DB).stat().st_size
            wal = root / (EYES_DB + '-wal')
            groups[name] = {'roots': group['roots'], 'files': 1, 'bytes': size, 'excluded': [],
                            'method': 'VACUUM INTO (committed WAL included, never a raw copy)',
                            'walBytes': wal.stat().st_size if wal.exists() else 0}
            continue
        groups[name] = {'roots': group['roots'], 'files': len(group['files']), 'bytes': _size(root, group['files']),
                        'excluded': group['excluded'], 'newFiles': len(set(group['files']) - seen)}
        seen.update(group['files'])
    return {'groups': groups, 'totals': {'files': sum(g['files'] for g in groups.values()),
                                          'bytes': sum(g['bytes'] for g in groups.values())}}


def vacuum_into(source, target):
    """Consistent copy of a live SQLite database (WAL included) without touching the source."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    with sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.execute('VACUUM INTO ?', (str(target),))
    with sqlite3.connect(target) as copy:
        integrity = copy.execute('PRAGMA quick_check').fetchone()[0]
        tables = copy.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    if integrity != 'ok':
        raise FeedbackError(f'VACUUM INTO produced a corrupt copy of {source}: {integrity}')
    return {'method': 'VACUUM INTO', 'integrity': integrity, 'tables': tables, 'bytes': target.stat().st_size}


def _git_head(root):
    try:
        result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, text=True, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    head = result.stdout.strip()
    return head if result.returncode == 0 and re.fullmatch(r'[0-9a-f]{40}', head) else None


def _code_sha256():
    from scripts.ml.training.common import code_sha256
    return code_sha256()


def training_scenes(root, project=SCENE_PROJECT):
    """Latest published Fieldwork item per scene: push receipts first, else the frozen mirror item."""
    root = Path(root)
    scenes = {}
    mirror = root / 'data/fieldwork-sync' / project / 'mirror.json'
    if mirror.is_file():
        try:
            items = json.loads(mirror.read_bytes()).get('items') or []
        except (ValueError, OSError):
            items = []
        for item in items:
            source = item.get('source') if isinstance(item, dict) else None
            if not isinstance(source, dict) or item.get('adapter') != 'addr-scene' or not isinstance(source.get('sceneId'), str):
                continue
            addr = (item.get('metadata') or {}).get('addr') if isinstance(item.get('metadata'), dict) else None
            revision = (addr or {}).get('sourceReviewRevision') if isinstance(addr, dict) else None
            scenes[source['sceneId']] = {'sceneId': source['sceneId'], 'itemId': item.get('id'),
                                         'modelSignature': source.get('modelSignature'),
                                         'reviewRevision': revision if isinstance(revision, int) else 0, 'binding': 'frozen'}
    receipts = root / 'data/fieldwork-sync' / project / 'push' / 'scenes'
    for receipt in sorted(receipts.glob('*.json')) if receipts.is_dir() else []:
        try:
            data = json.loads(receipt.read_bytes())
        except (ValueError, OSError):
            continue
        item_id = data.get('latestItemId') or data.get('itemId')
        generation = data.get('generation') or {}
        if isinstance(item_id, str) and item_id:
            scenes[receipt.stem] = {'sceneId': receipt.stem, 'itemId': item_id,
                                    'modelSignature': generation.get('modelSignature') or data.get('modelSignature'),
                                    'reviewRevision': generation.get('reviewRevision', data.get('reviewRevision', 0)), 'binding': 'push'}
    return [scenes[k] for k in sorted(scenes)]


def _push_run_ids(root, project=SCENE_PROJECT):
    runs = Path(root) / 'data/fieldwork-sync' / project / 'push' / 'runs'
    return sorted(p.name for p in runs.iterdir() if p.is_dir()) if runs.is_dir() else []


def item_id_for(manifest_sha256, published_at):
    return f'{ITEM_KIND}-{published_at:%Y%m%d}-{manifest_sha256[:12]}'


def stage(root, select, staging, *, with_images=False, tarball=False, include_checkpoints=False,
          parent=None, with_scenes=False, project=TRAINING_PROJECT, now=None):
    """Copy selected files into staging/blobs as content-addressed chunks; write manifest.json, item.json, project.json."""
    root, staging = Path(root), Path(staging)
    planned = plan(root, select, with_images=with_images, tarball=tarball, include_checkpoints=include_checkpoints)
    if parent is not None and not re.fullmatch(r'[0-9a-f]{64}', parent):
        raise FeedbackError('--parent must be the SHA-256 of a previously published manifest')
    blobs = staging / 'blobs'
    blobs.mkdir(parents=True, exist_ok=True)
    files, by_path, groups, exclusions, databases = [], {}, {}, [], {}
    for name in dict.fromkeys(select):
        group = resolve_group(root, name, with_images=with_images, tarball=tarball, include_checkpoints=include_checkpoints)
        paths = list(group['files'])
        if group['special'] == 'eyes':
            target = staging / 'source' / EYES_DB
            databases[EYES_DB] = vacuum_into(root / EYES_DB, target)
            if EYES_DB not in by_path:
                by_path[EYES_DB] = {'path': EYES_DB, **add_blob(target, blobs)}
                files.append(by_path[EYES_DB])
            paths = [EYES_DB]
        for path in paths:
            if path not in by_path:
                by_path[path] = {'path': path, **add_blob(root / path, blobs)}
                files.append(by_path[path])
        groups[name] = {'paths': group['roots'], 'files': paths, 'bytes': sum(by_path[p]['bytes'] for p in paths)}
        exclusions.extend({'group': name, **entry} for entry in group['excluded'])
    files.sort(key=lambda entry: entry['path'])
    stable = {'schemaVersion': 1, 'sceneIds': [], 'files': files}
    now = now or datetime.now(UTC)
    scenes = training_scenes(root) if with_scenes else []
    code_sha = _code_sha256()
    git_head = _git_head(root)
    manifest = {**stable, 'snapshotId': hashlib.sha256(encoded(stable)).hexdigest(), 'createdAt': now.isoformat(),
                'summary': {'kind': MANIFEST_KIND, 'files': len(files), 'bytes': sum(f['bytes'] for f in files),
                            'chunks': sum(len(f['chunks']) for f in files), 'groups': sorted(groups),
                            'scenes': len(scenes), 'databases': databases},
                'exclusions': exclusions, 'kind': MANIFEST_KIND, 'groups': groups,
                'parents': [parent] if parent else [], 'codeSha256': code_sha, 'gitHead': git_head, 'scenes': scenes}
    manifest_bytes = encoded(manifest)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    (staging / 'manifest.json').write_bytes(manifest_bytes)
    item = {'id': item_id_for(manifest_sha, now), 'projectId': project, 'adapter': 'addr-report',
            'title': f'Training artefacts {now:%Y-%m-%d} ({", ".join(sorted(groups))})'[:300],
            'source': {'manifest': {'url': f'/api/v1/assets/{manifest_sha}', 'sha256': manifest_sha,
                                    'bytes': len(manifest_bytes), 'contentType': 'application/json'},
                       'kind': ITEM_KIND, 'snapshotId': manifest['snapshotId'],
                       'groups': {name: group['paths'] for name, group in groups.items()},
                       'parents': manifest['parents'], 'scenes': scenes},
            'metadata': {'addr': {'kind': ITEM_KIND, 'publishedAt': now.isoformat(), 'codeSha256': code_sha,
                                  'gitHead': git_head, 'counts': {'files': len(files), 'bytes': manifest['summary']['bytes'],
                                                                  'scenes': len(scenes)},
                                  'pushRunIds': _push_run_ids(root),
                                  'note': 'Model estimates and review state as published; not human labels. No training is started.'}},
            'tags': ['training-manifest', *sorted(groups)][:100], 'status': 'unreviewed', 'notes': '', 'annotations': []}
    (staging / 'item.json').write_bytes(encoded(item))
    (staging / 'project.json').write_bytes(encoded({'id': project, 'name': TRAINING_PROJECT_NAME,
                                                     'description': 'Immutable manifests of published training artefacts (datasets, edit diffs, line corrections, sweeps, eyes index). Values are model estimates and review agreement, never human labels.'}))
    return {'manifest': manifest, 'manifestSha256': manifest_sha, 'manifestBytes': len(manifest_bytes), 'item': item,
            'staging': str(staging), 'plan': planned}


def _run_migrate(flags):
    """annotation-migrate with the same credential handling and bounded diagnostics as annotate."""
    with tempfile.TemporaryFile() as stderr:
        try:
            result = subprocess.run(['go', 'run', './cmd/annotation-migrate', *flags], cwd=ROOT, text=True,
                                    stdout=subprocess.PIPE, stderr=stderr, timeout=3600, check=False)
        except subprocess.TimeoutExpired as error:
            raise FeedbackError(f'annotation-migrate {flags[0]} timed out{feedback._stderr_detail(stderr)}') from error
        except OSError as error:
            raise FeedbackError(f'Could not start go run for annotation-migrate: {error}') from error
        detail = feedback._stderr_detail(stderr)
    if result.returncode:
        raise FeedbackError(f'annotation-migrate {flags[0]} failed (exit {result.returncode}){detail}')
    try:
        decoded = json.loads(result.stdout)
    except ValueError:
        decoded = None
    if not isinstance(decoded, dict):
        raise FeedbackError(f'annotation-migrate {flags[0]} returned invalid JSON')
    return decoded


def token_flags(origin, token_file=None):
    """--token-file flags for origin; the production token is never implied for another origin."""
    token = _setting(token_file, 'FIELDWORK_TOKEN_FILE')
    if not token and not os.environ.get('ANNOTATION_TOKEN'):
        if feedback.is_live_origin(origin):
            token = feedback.DEFAULT_TOKEN_FILE
        elif not feedback.is_loopback(origin):
            raise FeedbackError('A custom Fieldwork URL requires ANNOTATION_TOKEN or an explicit --token-file / FIELDWORK_TOKEN_FILE')
    return ['--token-file', _path(token)] if token else []


def guard_live_origin(origin, dry_run):
    if not dry_run and feedback.is_live_origin(origin) and os.environ.get('FIELDWORK_PUSH_ALLOW_LIVE') != '1':
        raise FeedbackError('Refusing to publish to the live Fieldwork origin; set FIELDWORK_PUSH_ALLOW_LIVE=1 to allow it explicitly')


@contextmanager
def _publish_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise FeedbackError('Another publish is running; wait for it to finish') from error
        yield


def ensure_project(project, remote_flags):
    """Create the training project once; existing projects are never modified."""
    cursor = ''
    while True:
        page = _run('projects', ['--limit', '500', '--cursor', cursor, *remote_flags]) if cursor else _run('projects', ['--limit', '500', *remote_flags])
        if any(isinstance(p, dict) and p.get('id') == project for p in page.get('projects', [])):
            return {'created': False, 'projectId': project}
        cursor = page.get('nextCursor') or ''
        if not cursor:
            break
    return {'created': True, 'projectId': project}


def _find_existing(project, manifest_sha, remote_flags):
    cursor = ''
    while True:
        flags = ['--project', project, '--adapter', 'addr-report', '--limit', '500', *remote_flags]
        page = _run('items', [*flags, '--cursor', cursor] if cursor else flags)
        for item in page.get('items', []):
            ref = (item.get('source') or {}).get('manifest') if isinstance(item, dict) else None
            if isinstance(ref, dict) and ref.get('sha256') == manifest_sha:
                return item
        cursor = page.get('nextCursor') or ''
        if not cursor:
            return None


def _write_receipt(path, receipt):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        temp.write(json.dumps(receipt, indent=2, sort_keys=True).encode())
        temp.flush()
        os.fsync(temp.fileno())
    os.replace(temp.name, path)


def publish(select, *, dry_run=True, url=None, token_file=None, project=TRAINING_PROJECT, parent=None, root=None,
            with_images=False, tarball=False, include_checkpoints=False, with_scenes=False, staging_root=None):
    """Dry run: plan from local files. Apply: stage, upload assets (manifest last), create the item, write a receipt."""
    root = Path(root or ROOT)
    origin = _setting(url, 'ANNOTATION_URL', feedback.DEFAULT_REMOTE_URL).rstrip('/')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', project):
        raise FeedbackError('Project ID must be 1–128 letters, digits, hyphens or underscores')
    options = {'with_images': with_images, 'tarball': tarball, 'include_checkpoints': include_checkpoints}
    planned = plan(root, select, **options)
    result = {'status': 'dry_run' if dry_run else 'pending', 'origin': origin, 'projectId': project, 'select': list(dict.fromkeys(select)),
              'groups': planned['groups'], 'totals': planned['totals'], 'note': 'No training is started; published values are model estimates and review agreement, not human labels.'}
    if dry_run:
        return result
    guard_live_origin(origin, dry_run)
    remote_flags = ['--url', origin, *token_flags(origin, token_file)]
    staging_root = Path(staging_root or (root / STAGING_ROOT))
    staging_root.mkdir(parents=True, exist_ok=True)
    with _publish_lock(root / PUBLISH_DIR.rsplit('/', 1)[0] / 'publish.lock'):
        temporary = staging_root / f'.staging-{os.getpid()}-{int(time.time())}'
        staged = stage(root, select, temporary, parent=parent, with_scenes=with_scenes, project=project, **options)
        staging = staging_root / staged['manifestSha256'][:12]
        if staging.exists():
            raise FeedbackError(f'Staging directory already exists: {staging}')
        os.replace(temporary, staging)
        staged['staging'] = str(staging)
        result.update(status='staged', staging=str(staging), manifestSha256=staged['manifestSha256'], snapshotId=staged['manifest']['snapshotId'],
                      itemId=staged['item']['id'], files=len(staged['manifest']['files']), bytes=staged['manifest']['summary']['bytes'])
        result['project'] = ensure_project(project, remote_flags)
        if result['project']['created']:
            _run('save-project', ['--file', str(staging / 'project.json'), '--revision', '0', *remote_flags])
        upload = _run_migrate(['upload', '--bundle', str(staging), *remote_flags])
        result['upload'] = {key: upload.get(key) for key in ('uploadedAssets', 'existingAssets', 'uploadedBytes', 'existingBytes')}
        try:
            saved = _run('save-item', ['--file', str(staging / 'item.json'), '--revision', '0', *remote_flags])
            item = saved.get('item') or {}
            result['status'] = 'published'
        except FeedbackError as error:
            if '409' not in str(error):
                raise
            item = _find_existing(project, staged['manifestSha256'], remote_flags)
            if not item:
                raise FeedbackError(f'Item {staged["item"]["id"]} already exists with different content; nothing overwritten') from error
            result['status'] = 'already_published'
        result['revision'] = item.get('revision')
        receipt = {'itemId': item.get('id') or staged['item']['id'], 'revision': item.get('revision'), 'projectId': project, 'origin': origin,
                   'manifestSha256': staged['manifestSha256'], 'snapshotId': staged['manifest']['snapshotId'],
                   'files': len(staged['manifest']['files']), 'bytes': staged['manifest']['summary']['bytes'],
                   'uploadedBytes': upload.get('uploadedBytes'), 'existingAssets': upload.get('existingAssets'),
                   'uploadedAssets': upload.get('uploadedAssets'), 'publishedAt': staged['item']['metadata']['addr']['publishedAt'],
                   'groups': list(staged['manifest']['groups']), 'parents': staged['manifest']['parents'], 'staging': str(staging)}
        receipt_path = root / PUBLISH_DIR / f'{staged["manifestSha256"][:12]}.json'
        _write_receipt(receipt_path, receipt)
        result['receipt'] = str(receipt_path)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--select', nargs='+', required=True, help='artefact groups, e.g. dataset-v2 edit-diffs/2026-09-29 line-corrections/2026-09-29 sweep coco-v1 eyes')
    parser.add_argument('--with-images', action='store_true', help='include dataset/coco image folders')
    parser.add_argument('--tarball', action='store_true', help='include <dataset>.tar when it exists (chunked over 64 MiB)')
    parser.add_argument('--include-checkpoints', action='store_true', help='include sweep *.pt checkpoints')
    parser.add_argument('--with-scenes', action='store_true', help='record the latest Fieldwork item per scene in the manifest (mirror + push receipts)')
    parser.add_argument('--parent', help='SHA-256 of the previous manifest this one supersedes')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='plan only (default)')
    mode.add_argument('--apply', action='store_true', help='stage, upload and create the manifest item')
    parser.add_argument('--url', help='Fieldwork origin (ANNOTATION_URL; default live, which --apply refuses without FIELDWORK_PUSH_ALLOW_LIVE=1)')
    parser.add_argument('--token-file', help='bearer token file (FIELDWORK_TOKEN_FILE)')
    parser.add_argument('--project', default=TRAINING_PROJECT)
    parser.add_argument('--root', help='repository root (default: this checkout)')
    args = parser.parse_args(argv)
    try:
        result = publish(args.select, dry_run=not args.apply, url=args.url, token_file=args.token_file, project=args.project,
                         parent=args.parent, root=args.root, with_images=args.with_images, tarball=args.tarball,
                         include_checkpoints=args.include_checkpoints, with_scenes=args.with_scenes)
    except FeedbackError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
