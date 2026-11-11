"""Review-loop reachability of low-band owned scan roofs under the frame and core split policies.

Counts how many low-evidence roofs the bounded self-review loop may select when its
split gate audits the whole image frame (default) versus the object's own centroid
tile (opt-in 'core', `roof_review_loop.protected_reason(split_policy='core')`).
These are diagnostics of the loop gate, not accuracy and not labels. The confidence
index is opened read-only; the only output is a dated JSON under data/reviews/eyes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from shapely.errors import GEOSException

from .roof_review_loop import SPLIT_POLICIES, object_core_group, protected_reason
from .scene_self_review import validate_id
from .scene_split import group_split
from .scene_training import atomic_json

VERSION = 'eyes-eligibility-v1'
INDEX = Path('data/suburbs/mooroolbark/confidence.sqlite')
OUTPUT = Path('data/reviews/eyes')
PROFILE_PREFIX = '53ea49d5'  # Frozen Mooroolbark scan inference profile (run 500114985968d40b).
SPLITS = ('train', 'validation', 'test')
SCOPE = ('Review-loop reachability of low-band owned scan roofs under two split policies. '
         'Counts describe the loop gate; they are model estimates of reachability, not accuracy and not labels.')
POLICIES = {
    'frame': 'Every z17 tile touched by the whole image frame must be train (scene_split.image_split_audit).',
    'core': ("The object's own centroid z17 tile must be train (scene_split.group_split) and the object must be "
             "listed in the scene's owned_object_ids; scenes without ownership data fall back to the frame audit."),
}


def code_sha256():
    here = Path(__file__)
    return hashlib.sha256(here.read_bytes() + (here.parent/'roof_review_loop.py').read_bytes()).hexdigest()


def connection(root=Path('.')):
    """Production confidence index, read-only; never created or migrated here."""
    file = Path(root)/INDEX
    if not file.exists():
        raise FileNotFoundError(file)
    return sqlite3.connect(f'file:{quote(str(file))}?mode=ro', uri=True)


def load_scene(root, scene_id):
    """The scene's latest.json; ValueError for an id that could leave data/scenes, OSError when absent."""
    return json.loads((Path(root)/'data/scenes'/validate_id(scene_id)/'latest.json').read_text())


def index_rows(root, profile_prefix):
    """Owned-roof count under the profile, its low-band rows, and whole-index denominators.

    Rows whose payload is not a feature with string scene/object ids are counted as unreadable,
    never silently dropped from the denominators.
    """
    with closing(connection(root)) as db:
        indexed_scenes = db.execute('SELECT count(*) FROM scenes').fetchone()[0]
        payloads = db.execute('SELECT payload FROM roofs ORDER BY scene_id, object_id').fetchall()
    owned, low, unreadable = 0, [], 0
    for (payload,) in payloads:
        try:
            p = json.loads(payload)['properties']
            scene_id, object_id = validate_id(p['scene_id']), p['object_id']
            if not isinstance(object_id, str):
                raise TypeError('object_id must be a string')
        except (TypeError, ValueError, KeyError):
            unreadable += 1
            continue
        if not str(p.get('profile', '')).startswith(profile_prefix):
            continue
        owned += 1
        if p.get('band') == 'low':
            low.append({'scene_id': scene_id, 'object_id': object_id, 'model_signature': p.get('model_signature')})
    return {'owned': owned, 'low': low, 'indexed': len(payloads), 'indexed_scenes': indexed_scenes, 'unreadable': unreadable}


def indexed_roofs(root, profile_prefix):
    rows = index_rows(root, profile_prefix)
    return rows['owned'], rows['low'], rows['indexed'], rows['indexed_scenes']


def reasons_for(root, scene, obj, model_signature):
    """Per-policy protection reason (None = eligible), mirroring roof_review_loop.prepare's selection."""
    if obj is None:
        return dict.fromkeys(SPLIT_POLICIES, 'Object changed')
    if scene['model_signature'] != model_signature:
        return dict.fromkeys(SPLIT_POLICIES, 'Model changed')
    return {policy: protected_reason(root, scene, obj, split_policy=policy) for policy in SPLIT_POLICIES}


def core_split(obj, scene):
    if obj is None:
        return 'unresolved'
    try:
        return group_split(object_core_group(obj, scene))
    except (KeyError, TypeError, ValueError, OverflowError, GEOSException):
        return 'unresolved'


def count(root=Path('.'), *, profile_prefix=PROFILE_PREFIX):
    started = time.time()
    root = Path(root)
    rows = index_rows(root, profile_prefix)
    owned, low = rows['owned'], rows['low']
    by_scene = {}
    for row in low:
        by_scene.setdefault(row['scene_id'], []).append(row)
    eligible = Counter()
    reasons = {policy: Counter() for policy in SPLIT_POLICIES}
    splits = Counter({**dict.fromkeys(SPLITS, 0), 'unresolved': 0})
    loaded, unavailable = 0, 0
    for scene_id in sorted(by_scene):
        try:
            scene = load_scene(root, scene_id)  # Each latest.json is decoded once for all its low roofs.
        except (OSError, ValueError):
            unavailable += 1  # Low roofs whose scene file is gone are reported, not fabricated.
            for policy in SPLIT_POLICIES:
                reasons[policy]['Scene unavailable'] += len(by_scene[scene_id])
            splits['unresolved'] += len(by_scene[scene_id])
            continue
        loaded += 1
        objects = {o['id']: o for o in scene['objects']}
        for row in by_scene[scene_id]:
            obj = objects.get(row['object_id'])
            for policy, reason in reasons_for(root, scene, obj, row['model_signature']).items():
                if reason is None:
                    eligible[policy] += 1
                else:
                    reasons[policy][reason] += 1
            splits[core_split(obj, scene)] += 1
    return {'version': VERSION, 'computed_at': datetime.now(UTC).isoformat(), 'code_sha256': code_sha256(),
            'scope': SCOPE, 'profile_prefix': profile_prefix,
            'population': (f'Owned scan roofs under inference profile {profile_prefix}… with evidence band low, '
                           f'from {INDEX} (read-only); every roof is counted under both policies.'),
            'denominators': {'owned_roofs': owned, 'low_roofs': len(low), 'scenes_with_low_roofs': len(by_scene),
                             'scenes_loaded': loaded, 'scenes_unavailable': unavailable, 'indexed_roofs': rows['indexed'],
                             'unreadable_index_rows': rows['unreadable'], 'indexed_scenes': rows['indexed_scenes']},
            'policies': POLICIES,
            **{policy: {'eligible': eligible[policy], 'reasons': dict(sorted(reasons[policy].items()))} for policy in SPLIT_POLICIES},
            'by_core_split': dict(splits),
            'held_out_core_roofs': splits['validation'] + splits['test'],
            'elapsed_seconds': round(time.time()-started, 2)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--count', action='store_true', help='Count eligibility and write data/reviews/eyes/eligibility-<date>.json')
    parser.add_argument('--profile', default=PROFILE_PREFIX, help='Inference profile prefix of the owned scan population')
    parser.add_argument('--out', type=Path, help='Override the dated output path')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not args.count:
        parser.error('Choose --count')
    report = count(root, profile_prefix=args.profile)
    path = args.out or root/OUTPUT/f'eligibility-{datetime.now(UTC).date().isoformat()}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, report)
    shown = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
    print(json.dumps({**report, 'path': shown}))


if __name__ == '__main__':
    main()
