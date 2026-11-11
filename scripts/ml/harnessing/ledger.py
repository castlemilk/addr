"""Budget accounting: R = 4k rollouts and P = 3k improvement sessions per mode and case.

Base rollouts count toward each mode; retries are attempts, not sessions. Selection and
reflection are recorded outside this ledger. Adapted from Harness² core/ledger.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class BudgetError(AssertionError):
    """The ledger did not land on the budget identity."""


@dataclass
class Ledger:
    mode: str
    task_id: str
    k: int
    entries: list[dict] = field(default_factory=list)

    def rollout(self, label, usd=0.0):
        self.entries.append({'kind': 'rollout', 'label': label, 'usd': usd, 'attempts': 1})

    def session(self, label, usd=0.0, *, cached=False, attempts=1):
        self.entries.append({'kind': 'session', 'label': label, 'usd': usd, 'cached': cached,
                             'attempts': attempts})

    def labels(self, kind):
        return [entry['label'] for entry in self.entries if entry['kind'] == kind]

    def assert_identity(self):
        """Raise unless exactly 4k rollouts and 3k sessions were recorded, none duplicated."""
        rollouts, sessions = self.labels('rollout'), self.labels('session')
        dupes = sorted({x for x in rollouts if rollouts.count(x) > 1} | {x for x in sessions if sessions.count(x) > 1})
        if not dupes and len(rollouts) == 4*self.k and len(sessions) == 3*self.k:
            return
        raise BudgetError(f'{self.mode}-{self.k} {self.task_id}: R={len(rollouts)} (want {4*self.k}), '
                          f'P={len(sessions)} (want {3*self.k}); rollouts {rollouts}; sessions {sessions}'
                          + (f'; DUPLICATE labels {dupes}: a repeated label means one stage silently '
                             'overwrote another' if dupes else ''))

    def to_dict(self):
        return {'mode': self.mode, 'task_id': self.task_id, 'k': self.k,
                'R': len(self.labels('rollout')), 'expected_R': 4*self.k,
                'P': len(self.labels('session')), 'expected_P': 3*self.k,
                'session_attempts': sum(e['attempts'] for e in self.entries if e['kind'] == 'session'),
                'usd': round(sum(e['usd'] for e in self.entries), 6),
                'rollouts': self.labels('rollout'), 'sessions': self.labels('session')}
