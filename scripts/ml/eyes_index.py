"""Shared sidecar index for roof/object observability signals ("eyes").

Every signal computed outside the frozen inference path lives here, keyed by
scene/object/model_signature/version, so production artifacts
(latest.json, models/, review.json, confidence.json, confidence.sqlite) are
never rewritten. Payloads are JSON; each carries its own version, code hash and
computation time. These rows are diagnostics and reference agreement, never
labels or calibrated accuracy.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

VERSION = 'eyes-index-v1'
RELATIVE = Path('data/reviews/eyes/eyes.sqlite')

_OBJECT = 'scene_id TEXT, object_id TEXT, model_signature TEXT, version TEXT, payload TEXT, PRIMARY KEY(scene_id, object_id, model_signature, version)'
TABLES = {
    'references': ('ref_id TEXT PRIMARY KEY, tier TEXT, source_kind TEXT, scene_id TEXT, object_id TEXT, crm_feature_id INTEGER, '
                   'address_id INTEGER, spatial_group TEXT, split TEXT, west REAL, south REAL, east REAL, north REAL, version TEXT, payload TEXT'),
    'agreement': 'ref_id TEXT, scene_id TEXT, object_id TEXT, model_signature TEXT, version TEXT, payload TEXT, PRIMARY KEY(ref_id, scene_id, object_id, version)',
    'cross_capture': ('scene_id TEXT, object_id TEXT, other_scene_id TEXT, other_object_id TEXT, version TEXT, payload TEXT, '
                      'PRIMARY KEY(scene_id, object_id, other_scene_id, other_object_id, version)'),
    'roof_diagnostics': _OBJECT,
    'object_quality': _OBJECT,
    'shadow_bands': _OBJECT,
    'multidate': _OBJECT,
    'lineage': ('experiment_id TEXT, scene_id TEXT, baseline_object_id TEXT, candidate_object_id TEXT, version TEXT, payload TEXT, '
                'PRIMARY KEY(experiment_id, scene_id, baseline_object_id, candidate_object_id)'),
    'snapshots': 'id TEXT PRIMARY KEY, created_at TEXT, version TEXT, payload TEXT',
    'checkpoints': 'id TEXT PRIMARY KEY, created_at TEXT, version TEXT, payload TEXT',
    # Roof -> address allocation (address_allocation.py) and per-address aggregation (address_enrichment.py):
    # agent hints and model estimates, never labels; production files stay untouched.
    'address_allocation': 'scene_id TEXT, object_id TEXT, version TEXT, payload TEXT, PRIMARY KEY(scene_id, object_id, version)',
    'address_enrichment': 'address_id INTEGER, full_address TEXT, version TEXT, payload TEXT, PRIMARY KEY(address_id, version)',
    # Confidence-tiered machine label decisions (decision_policy.py): auto / assist / review per building; never labels by themselves.
    'decision_policy': 'scene_id TEXT, object_id TEXT, version TEXT, payload TEXT, PRIMARY KEY(scene_id, object_id, version)',
    # Reviewer roof-line corrections vs the model's starting estimate (line_corrections.py), one row per saved review
    # revision: agreement, never accuracy; the reviewer graph is a draft while ref_checked is false.
    'line_corrections': ('scene_id TEXT, object_id TEXT, review_revision INTEGER, model_signature TEXT, version TEXT, payload TEXT, '
                         'PRIMARY KEY(scene_id, object_id, review_revision, version)'),
}
INDEXES = {
    'references': ['CREATE INDEX IF NOT EXISTS references_bounds ON "references"(west, east, south, north)',
                   'CREATE INDEX IF NOT EXISTS references_object ON "references"(scene_id, object_id)'],
    'agreement': ['CREATE INDEX IF NOT EXISTS agreement_object ON agreement(scene_id, object_id)'],
    'cross_capture': ['CREATE INDEX IF NOT EXISTS cross_capture_other ON cross_capture(other_scene_id, other_object_id)'],
    'lineage': ['CREATE INDEX IF NOT EXISTS lineage_scene ON lineage(scene_id)'],
    'address_allocation': ['CREATE INDEX IF NOT EXISTS address_allocation_scene ON address_allocation(scene_id)'],
    'decision_policy': ['CREATE INDEX IF NOT EXISTS decision_policy_scene ON decision_policy(scene_id)'],
    'line_corrections': ['CREATE INDEX IF NOT EXISTS line_corrections_object ON line_corrections(scene_id, object_id)'],
}


def _columns(table):
    definition = re.sub(r'PRIMARY KEY\([^)]*\)', '', TABLES[table])
    return [part.strip().split()[0].strip('"') for part in definition.split(',') if part.strip()]


def path(root=Path('.')):
    return Path(root) / RELATIVE


def connection(root=Path('.'), readonly=False):
    """Open the eyes index. Read-only opens fail on writes and never create the file."""
    file = path(root)
    if readonly:
        if not file.exists():
            raise FileNotFoundError(file)
        return sqlite3.connect(f'file:{file}?mode=ro', uri=True)
    file.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(file)
    db.execute('PRAGMA journal_mode=WAL')
    return db


def ensure_schema(db):
    for table, columns in TABLES.items():
        db.execute(f'CREATE TABLE IF NOT EXISTS "{table}"({columns})')
        for statement in INDEXES.get(table, []):
            db.execute(statement)
    db.commit()


def replace_rows(db, table, rows):
    """INSERT OR REPLACE dict rows; dict/list payloads are JSON-encoded."""
    if table not in TABLES:
        raise ValueError(f'Unknown eyes table {table!r}')
    columns = _columns(table)
    prepared = []
    for row in rows:
        unknown = set(row) - set(columns)
        if unknown:
            raise ValueError(f'Unknown columns for {table}: {sorted(unknown)}')
        values = []
        for column in columns:
            value = row.get(column)
            if isinstance(value, (dict, list)):
                value = json.dumps(value, sort_keys=True)
            values.append(value)
        prepared.append(values)
    placeholders = ','.join('?' * len(columns))
    names = ','.join(f'"{c}"' for c in columns)
    db.executemany(f'INSERT OR REPLACE INTO "{table}"({names}) VALUES({placeholders})', prepared)
    db.commit()
    return len(prepared)


def _decode(table, record):
    row = dict(zip(_columns(table), record, strict=True))
    if isinstance(row.get('payload'), str):
        try:
            row['payload'] = json.loads(row['payload'])
        except ValueError:
            pass
    return row


def tables_present(db):
    """Tables that exist in an index file; an index last written before a table was added lacks it until the next write."""
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def rows(db, table, **where):
    if table not in TABLES:
        raise ValueError(f'Unknown eyes table {table!r}')
    columns = _columns(table)
    unknown = set(where) - set(columns)
    if unknown:
        raise ValueError(f'Unknown filter columns for {table}: {sorted(unknown)}')
    clause = ' AND '.join(f'"{k}"=?' for k in where) or '1=1'
    names = ','.join(f'"{c}"' for c in columns)
    cursor = db.execute(f'SELECT {names} FROM "{table}" WHERE {clause}', tuple(where.values()))
    return [_decode(table, record) for record in cursor.fetchall()]


def fetch_object(root, scene_id, object_id):
    """Every sidecar row about one model object, or empty lists when no index exists."""
    if not path(root).exists():
        return {table: [] for table in TABLES}
    found = {}
    with closing(connection(root, readonly=True)) as db:
        present = tables_present(db)
        for table in TABLES:
            columns = _columns(table)
            if table not in present:
                found[table] = []  # read-only opens never create tables; a writer adds it on its next run
            elif 'scene_id' in columns and 'object_id' in columns:
                found[table] = rows(db, table, scene_id=scene_id, object_id=object_id)
            elif 'baseline_object_id' in columns:
                found[table] = [r for r in rows(db, table, scene_id=scene_id)
                                if object_id in (r['baseline_object_id'], r['candidate_object_id'])]
            else:
                found[table] = []
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read the eyes sidecar index (diagnostics, not labels).')
    parser.add_argument('--root', default='.')
    parser.add_argument('--object', nargs=2, metavar=('SCENE_ID', 'OBJECT_ID'))
    parser.add_argument('--tables', action='store_true', help='List tables and row counts.')
    args = parser.parse_args(argv)
    root = Path(args.root)
    if args.object:
        scene_id, object_id = args.object
        json.dump({'version': VERSION, 'scene_id': scene_id, 'object_id': object_id,
                   'tables': fetch_object(root, scene_id, object_id)}, sys.stdout)
        sys.stdout.write('\n')
        return 0
    if args.tables:
        counts = {}
        if path(root).exists():
            with closing(connection(root, readonly=True)) as db:
                present = tables_present(db)
                for table in TABLES:
                    counts[table] = db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] if table in present else 0
        json.dump({'version': VERSION, 'path': str(path(root)), 'counts': counts}, sys.stdout)
        sys.stdout.write('\n')
        return 0
    parser.print_help()
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
