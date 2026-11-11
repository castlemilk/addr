"""The base-harness lineage: every base the loop runs from, and the gate decision that admitted it.

    <runs>/lineage/index.json      versions in order: version, fingerprint, parent, source, gate, created_at
    <runs>/lineage/v<n>/           the full tree of version n

v1 is the snapshot of the project skill. A later version exists only when a candidate passed the
validation gate against its parent; rejected candidates are recorded with their gate and no tree.
"""
from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from . import surface


class Lineage:
    def __init__(self, runs_root):
        self.root = Path(runs_root)/'lineage'

    @property
    def index_path(self):
        return self.root/'index.json'

    def load(self):
        if not self.index_path.is_file():
            return {'versions': [], 'rejected': []}
        return json.loads(self.index_path.read_text())

    def _save(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, indent=2, default=str))
        tmp.replace(self.index_path)

    def dir_of(self, version):
        return self.root/f'v{int(version)}'

    def head(self):
        """(version, directory) of the newest admitted base, or None before seeding."""
        versions = self.load()['versions']
        return (versions[-1]['version'], self.dir_of(versions[-1]['version'])) if versions else None

    def seed(self, base_dir, *, source='project-skill snapshot'):
        """Admit v1 from an existing base tree; a no-op once seeded."""
        data = self.load()
        if data['versions']:
            return self.head()
        tree = surface.read_tree(base_dir)
        surface.write_tree(tree, self.dir_of(1))
        data['versions'].append({'version': 1, 'fingerprint': surface.tree_fingerprint(tree), 'parent': None,
                                 'source': source, 'gate': None, 'created_at': datetime.now(UTC).isoformat()})
        self._save(data)
        return self.head()

    def revoke(self, version, reason):
        """Withdraw the head version (its gate was not valid evidence): its tree moves to revoked/, the parent
        becomes head again, and the row is kept under 'invalidated' with the reason."""
        data = self.load()
        if not data['versions'] or data['versions'][-1]['version'] != int(version) or int(version) == 1:
            raise ValueError(f'only the head version (not v1) can be revoked; head is v{data["versions"][-1]["version"]}')
        row = data['versions'].pop()
        target = self.root/'revoked'/f'v{row["version"]}-{row["fingerprint"]}'
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        self.dir_of(version).rename(target)
        data.setdefault('invalidated', []).append({**row, 'invalidated_reason': reason, 'tree': str(target)})
        self._save(data)
        return target

    def version(self, version):
        return next((row for row in self.load()['versions'] if row['version'] == int(version)), None)

    def confirm(self, version, gate):
        """Record the TEST-split confirmation of a version against its parent; promotion requires a pass."""
        data = self.load()
        row = next(row for row in data['versions'] if row['version'] == int(version))
        if gate.get('baseline_fingerprint') and row['parent'] is not None:
            parent = next(r for r in data['versions'] if r['version'] == row['parent'])
            if gate['baseline_fingerprint'] != parent['fingerprint'] or gate.get('candidate_fingerprint') != row['fingerprint']:
                raise ValueError(f'the confirmation compared other trees than v{version} and its parent')
        row.setdefault('confirmations', []).append(gate)
        self._save(data)
        return row

    @staticmethod
    def confirmed(row):
        return any(gate.get('passed') for gate in (row or {}).get('confirmations', []))

    def admit(self, tree, *, source, gate):
        """Record a gate decision; a passing candidate becomes the new head."""
        data = self.load()
        if not data['versions']:
            raise ValueError('seed the lineage before admitting a candidate')
        parent = data['versions'][-1]
        gated_against = gate.get('baseline_fingerprint')
        if gated_against and gated_against != parent['fingerprint']:
            raise ValueError(f'the candidate was gated against base {gated_against}, but the head is now '
                             f'v{parent["version"]} ({parent["fingerprint"]}); gate it again against the head')
        row = {'fingerprint': surface.tree_fingerprint(tree), 'parent': parent['version'], 'source': source,
               'gate': gate, 'created_at': datetime.now(UTC).isoformat()}
        if not gate.get('passed'):
            data['rejected'].append(row)
            self._save(data)
            return None
        version = parent['version'] + 1
        surface.write_tree(tree, self.dir_of(version))
        data['versions'].append({'version': version, **row})
        self._save(data)
        return version, self.dir_of(version)
