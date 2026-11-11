"""Hash guard for protected roof references (diagnostics, not labels/accuracy).

Verifies, at the start and end of every eyes checkpoint, that nothing the
observability work depends on has drifted: the house-30286/30296 checkpoint
manifests (latest.json, scene PNG, review.json), the frozen Mooroolbark run
(plan.json, run.json, snapshot/), the CRM 27 Cambridge roof geometry
(crm_features id 9) and the confidence.sqlite scene mtimes. The frozen plan is
also verified without any baseline (its signature is the run id and its code
hashes must match the snapshot copies), and the baseline pins every manifest
file so a manifest re-pointed at drifted files still fails. Production inputs
are opened read-only; the only writes are the first-run baseline and named
reports under data/reviews/eyes/. A corrupt or incomplete baseline fails the
guard and is never overwritten.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

VERSION = 'eyes-guard-v1'
MANIFESTS = Path('data/references/roof-checkpoints')
# Spec principle 7: house-30286 and house-30296 checkpoint manifests; a missing one fails the guard.
PROTECTED_MANIFESTS = ('a983ad7a89b19808', 'e055d6fd61e63b10', '3322869b48a69d51', 'd4f3d42547234b76')
FROZEN_RUN = Path('data/suburbs/mooroolbark/runs/500114985968d40b')
FROZEN_FILES = ('plan.json', 'run.json')
CONFIDENCE = Path('data/suburbs/mooroolbark/confidence.sqlite')
CRM_DB = Path('data/australia_geo.db')
CRM_FEATURE_ID = 9
SCENES = Path('data/scenes')
REPORTS = Path('data/reviews/eyes')
BASELINE = REPORTS / 'guard-baseline.json'
BASELINE_KEYS = ('frozen_run', 'crm_feature_geometry_sha256', 'manifests')


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def connect_readonly(path):
    """Read-only SQLite connection; the path is percent-quoted so '?', '#' or '%' cannot alter the URI."""
    return sqlite3.connect(f'file:{quote(str(path))}?mode=ro', uri=True)


def _read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _check(name, ok, **detail):
    return {'name': name, 'ok': bool(ok), 'detail': detail}


def manifest_hashes(root):
    """sha256 of every checkpoint manifest.json, keyed by manifest id."""
    return {path.parent.name: sha256_file(path) for path in sorted((root / MANIFESTS).glob('*/manifest.json'))}


def check_manifests(root, baseline=None, hashes=None):
    """One check per manifest (found, protected or baseline-recorded): scene-file hashes from the manifest
    'files' map, else model_signature; plus the manifest file itself against the baseline."""
    hashes = manifest_hashes(root) if hashes is None else hashes
    recorded = (baseline or {}).get('manifests')
    checks = []
    for manifest_id in sorted(set(hashes) | set(PROTECTED_MANIFESTS) | set(recorded or {})):
        name = f'manifest:{manifest_id}'
        if manifest_id not in hashes:
            checks.append(_check(name, False, mode='missing', protected=manifest_id in PROTECTED_MANIFESTS,
                                 recorded=manifest_id in (recorded or {}), path=str(MANIFESTS / manifest_id / 'manifest.json')))
            continue
        pinned = {'manifest_sha256': hashes[manifest_id],
                  'manifest_baseline': ('unrecorded' if recorded is None else 'added' if manifest_id not in recorded
                                        else 'match' if recorded[manifest_id] == hashes[manifest_id] else 'drift')}
        pinned_ok = pinned['manifest_baseline'] != 'drift'
        manifest = _read_json(root / MANIFESTS / manifest_id / 'manifest.json')
        if not isinstance(manifest, dict):
            checks.append(_check(name, False, mode='unreadable', **pinned))
            continue
        scene_id = manifest.get('scene_id')
        files_map = manifest.get('files') if isinstance(manifest.get('files'), dict) else {}
        scene_files = {rel: sha for rel, sha in files_map.items() if rel.startswith(f'data/scenes/{scene_id}/')}
        if scene_files:
            files = {}
            for rel, expected in sorted(scene_files.items()):
                path = root / rel
                files[rel] = 'missing' if not path.is_file() else ('match' if sha256_file(path) == expected else 'drift')
            checks.append(_check(name, pinned_ok and all(v == 'match' for v in files.values()),
                                 mode='file_hashes', scene_id=scene_id, files=files, **pinned))
            continue
        latest = _read_json(root / SCENES / str(scene_id) / 'latest.json')
        signature = latest.get('model_signature') if isinstance(latest, dict) else None
        checks.append(_check(name, pinned_ok and signature is not None and signature == manifest.get('model_signature'),
                             mode='model_signature', scene_id=scene_id, model_signature=manifest.get('model_signature'),
                             latest_model_signature=signature,
                             note='manifest lists no data/scenes hashes; compared model_signature only', **pinned))
    if not checks:
        checks.append(_check('manifests', False, mode='missing', path=str(MANIFESTS)))
    return checks


def frozen_run_hashes(root):
    run = root / FROZEN_RUN
    hashes = {}
    for name in FROZEN_FILES:
        if (run / name).is_file():
            hashes[name] = sha256_file(run / name)
    for path in sorted(p for p in (run / 'snapshot').rglob('*') if p.is_file()):
        hashes[path.relative_to(run).as_posix()] = sha256_file(path)
    return hashes


def plan_consistency(root, hashes):
    """Baseline-independent: plan.json signature (scene_batch.signature) is the run id, and every plan
    code hash matches its snapshot/ copy."""
    plan = _read_json(root / FROZEN_RUN / 'plan.json')
    if not isinstance(plan, dict):
        return {'plan_signature_matches_run_id': False, 'snapshot_code_mismatch': [], 'plan_code_files': 0}
    signature = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()[:16]
    code = plan.get('code') if isinstance(plan.get('code'), dict) else {}
    return {'plan_signature_matches_run_id': signature == FROZEN_RUN.name, 'plan_code_files': len(code),
            'snapshot_code_mismatch': sorted(rel for rel, sha in code.items() if hashes.get(f'snapshot/{rel}') != sha)}


def _compare(name, current, baseline, recording, **extra):
    """Compare a {key: sha} map against its baseline; ok only when byte-identical."""
    if baseline is None:
        return _check(name, bool(current), mode='recorded' if recording else 'unrecorded', count=len(current), **extra)
    added = sorted(set(current) - set(baseline))
    removed = sorted(set(baseline) - set(current))
    changed = sorted(k for k in set(current) & set(baseline) if current[k] != baseline[k])
    return _check(name, not (added or removed or changed), mode='compared', count=len(current),
                  added=added, removed=removed, changed=changed, **extra)


def check_frozen_run(root, baseline, recording=False):
    current = frozen_run_hashes(root)
    consistency = plan_consistency(root, current)
    check = _compare('frozen_run', current, (baseline or {}).get('frozen_run'), recording, path=str(FROZEN_RUN), **consistency)
    missing = [name for name in FROZEN_FILES if name not in current]
    if not any(key.startswith('snapshot/') for key in current):
        missing.append('snapshot/')
    if missing:
        check['detail']['missing'] = missing
    if missing or not consistency['plan_signature_matches_run_id'] or consistency['snapshot_code_mismatch']:
        check['ok'] = False
    return check, current


def crm_geometry_sha256(root):
    path = root / CRM_DB
    if not path.is_file():
        return None
    with closing(connect_readonly(path)) as db:
        row = db.execute('SELECT geometry_json FROM crm_features WHERE id=?', (CRM_FEATURE_ID,)).fetchone()
    if not row or row[0] is None:
        return None
    return hashlib.sha256(row[0] if isinstance(row[0], bytes) else str(row[0]).encode()).hexdigest()


def check_crm_geometry(root, baseline, recording=False):
    try:
        current = crm_geometry_sha256(root)
    except sqlite3.Error as exc:
        return _check('crm_feature_geometry', False, mode='error', error=str(exc), feature_id=CRM_FEATURE_ID), None
    if current is None:
        return _check('crm_feature_geometry', False, mode='missing', feature_id=CRM_FEATURE_ID, path=str(CRM_DB)), None
    recorded = (baseline or {}).get('crm_feature_geometry_sha256')
    if recorded is None:
        return _check('crm_feature_geometry', True, mode='recorded' if recording else 'unrecorded',
                      feature_id=CRM_FEATURE_ID, sha256=current), current
    return _check('crm_feature_geometry', current == recorded, mode='compared', feature_id=CRM_FEATURE_ID,
                  sha256=current, baseline_sha256=recorded), current


def check_confidence_mtimes(root):
    path = root / CONFIDENCE
    if not path.is_file():
        return _check('confidence_mtimes', False, mode='missing', path=str(CONFIDENCE))
    try:
        with closing(connect_readonly(path)) as db:
            stamps = db.execute('SELECT id, mtime FROM scenes ORDER BY id').fetchall()
    except sqlite3.Error as exc:
        return _check('confidence_mtimes', False, mode='error', error=str(exc), path=str(CONFIDENCE))
    if not stamps:
        return _check('confidence_mtimes', False, mode='empty', scenes=0, path=str(CONFIDENCE))
    missing, mismatched = [], []
    for scene_id, mtime in stamps:
        try:
            stamp = (root / SCENES / str(scene_id) / 'latest.json').stat().st_mtime_ns
        except OSError:
            missing.append(scene_id)
            continue
        if stamp != mtime:
            mismatched.append(scene_id)
    return _check('confidence_mtimes', not (missing or mismatched), scenes=len(stamps), missing=missing, mismatched=mismatched)


def check_scenes_newer(root, since_ns):
    """Informational: files under data/scenes modified after the baseline timestamp."""
    total = newer = 0
    scenes = root / SCENES
    if scenes.is_dir():
        for path in scenes.rglob('*'):
            try:  # one stat per path: files may vanish while an extraction writes (no is_file/stat race)
                info = path.stat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                total += 1
                if since_ns is not None and info.st_mtime_ns > since_ns:
                    newer += 1
    return _check('scenes_newer_than_baseline', True, informational=True, files=total,
                  count=newer if since_ns is not None else None, since_ns=since_ns)


def load_baseline(root):
    """(baseline or None, problem check or None). A present but unreadable/incomplete baseline is a failure."""
    path = root / BASELINE
    if not path.exists():
        return None, None
    baseline = _read_json(path)
    if not isinstance(baseline, dict):
        return None, _check('guard_baseline', False, mode='unreadable', path=str(BASELINE))
    missing = [key for key in BASELINE_KEYS if key not in baseline]
    if missing:
        return None, _check('guard_baseline', False, mode='incomplete', missing=missing, path=str(BASELINE))
    return baseline, None


def _write_json_atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(payload, indent=1, sort_keys=True))
    os.replace(temporary, path)


def check(root=Path('.'), *, write_baseline=True):
    """Run every guard check; records the baseline on first run when write_baseline is true."""
    root = Path(root)
    baseline, problem = load_baseline(root)
    recording = baseline is None and problem is None and write_baseline
    now_ns = time.time_ns()
    manifests = manifest_hashes(root)
    checks = ([problem] if problem else []) + check_manifests(root, baseline, manifests)
    if recording:
        for item in checks:
            if item['detail'].get('manifest_baseline') == 'unrecorded':
                item['detail']['manifest_baseline'] = 'recorded'
    frozen, frozen_hashes = check_frozen_run(root, baseline, recording)
    crm, crm_sha = check_crm_geometry(root, baseline, recording)
    checks += [frozen, crm, check_confidence_mtimes(root),
               check_scenes_newer(root, (baseline or {}).get('created_at_ns', now_ns if recording else None))]
    report = {'version': VERSION, 'code_sha256': code_sha256(), 'ok': all(c['ok'] for c in checks),
              'checked_at': datetime.now(UTC).isoformat(), 'root': str(root), 'checks': checks,
              'baseline': {'path': str(BASELINE), 'created': False, 'created_at': (baseline or {}).get('created_at')}}
    if recording and frozen['ok'] and crm_sha:
        record = {'version': VERSION, 'code_sha256': report['code_sha256'], 'created_at': report['checked_at'],
                  'created_at_ns': now_ns, 'frozen_run': frozen_hashes, 'manifests': manifests,
                  'crm_feature_id': CRM_FEATURE_ID, 'crm_feature_geometry_sha256': crm_sha}
        _write_json_atomic(root / BASELINE, record)
        report['baseline'].update(created=True, created_at=record['created_at'])
    elif recording:
        for item in checks:  # nothing was recorded after all
            for key in ('mode', 'manifest_baseline'):
                if item['detail'].get(key) == 'recorded':
                    item['detail'][key] = 'unrecorded'
    return report


def write(root, name, *, write_baseline=True):
    """Run check() and store the report as data/reviews/eyes/guard-<name>.json."""
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', name or ''):
        raise ValueError(f'Invalid guard report name {name!r}')
    root = Path(root)
    report = check(root, write_baseline=write_baseline)
    path = root / REPORTS / f'guard-{name}.json'
    report['path'] = str(path)
    _write_json_atomic(path, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description='Verify protected roof references by hash (exit 1 on drift).')
    parser.add_argument('--root', default='.')
    parser.add_argument('--write', metavar='NAME', help='Also store the report as data/reviews/eyes/guard-NAME.json.')
    parser.add_argument('--no-baseline', action='store_true', help='Never record a first-run baseline.')
    args = parser.parse_args(argv)
    root = Path(args.root)
    baseline = not args.no_baseline
    report = write(root, args.write, write_baseline=baseline) if args.write else check(root, write_baseline=baseline)
    json.dump(report, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write('\n')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
