"""DESIGN.json sha acceptance through its dated amendment chain: one place, read by scene_recapture.verify, scene_focus
capture / carry and r5_precision's lineage hop.

A frame_transform.design block, a scene_focus plan row and a windows-<date>.jsonl registry row record the WHOLE-FILE sha256
of data/reviews/eyes/recapture/DESIGN.json at write time. A later dated amendment moves that sha even when it changes no
threshold, so an exact comparison with the current file refuses every earlier record by name (probe of 2026-10-01T07:49Z,
.run/focus-recapture/capture-2026-10-01.log: carry_design_mismatch 80 / carry_unregistered 80, carried_joined 0, targets back
to 3, on a temp copy with the identity amendment appended). The rule here: a recorded sha is accepted when it equals the
current sha (design_sha_current) or when it is the previous_sha256 of an amendment reached by walking `amendments` from the
newest entry backwards through entries whose thresholds_changed is exactly false (design_sha_via_chain; the hop is named).
The walk stops at the first amendment whose thresholds_changed is not false (true, or absent: never assumed) or that names
no previous_sha256, so a record written before a THRESHOLD change is still refused; an unknown sha is refused. Acceptance of
the sha is lineage, not proof: the callers compare the recorded design values beside it.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

CURRENT = 'design_sha_current'
VIA_CHAIN = 'design_sha_via_chain'
REFUSED = 'refused'
MATCHES = (CURRENT, VIA_CHAIN, REFUSED)


def _is_sha(value):
    return isinstance(value, str) and len(value) == 64


def accepted_shas(values, current_sha):
    """{sha256: match} of every design sha the current file accepts. The current sha is {'match': CURRENT, 'hops_back': 0};
    walking values['amendments'] from the newest entry backwards, each amendment whose thresholds_changed is exactly False
    contributes its previous_sha256 as {'match': VIA_CHAIN, 'hops_back': k, 'amendment_index': 1-based, 'amendment_at',
    'amendment_field'}; the walk stops at the first entry whose flag is not False or that has no 64-character previous_sha256."""
    accepted = {}
    if _is_sha(current_sha):
        accepted[current_sha] = {'match': CURRENT, 'hops_back': 0}
    amendments = values.get('amendments') if isinstance(values, dict) else None
    amendments = amendments if isinstance(amendments, list) else []
    for hops, index in enumerate(range(len(amendments) - 1, -1, -1), start=1):
        entry = amendments[index]
        previous = entry.get('previous_sha256') if isinstance(entry, dict) else None
        if not isinstance(entry, dict) or entry.get('thresholds_changed') is not False or not _is_sha(previous):
            break
        accepted.setdefault(previous, {'match': VIA_CHAIN, 'hops_back': hops, 'amendment_index': index + 1,
                                       'amendment_at': entry.get('at'), 'amendment_field': entry.get('field')})
    return accepted


def match(accepted, recorded_sha):
    """The acceptance of a recorded design sha under `accepted` (accepted_shas), or None when refused: unknown, or behind an
    amendment whose thresholds_changed is true or unstated."""
    return accepted.get(recorded_sha) if isinstance(accepted, dict) and isinstance(recorded_sha, str) else None


def tally(matches):
    """{CURRENT: n, VIA_CHAIN: n, REFUSED: n} over match dicts; None counts as refused."""
    counts = dict.fromkeys(MATCHES, 0)
    for m in matches:
        counts[m['match'] if isinstance(m, dict) and m.get('match') in MATCHES else REFUSED] += 1
    return counts


def read(path):
    """(values, sha256, accepted) of a DESIGN.json read as bytes; (None, None, {}) when absent or not JSON, so a caller refuses
    every recorded sha by name rather than silently."""
    try:
        data = Path(path).read_bytes()
        values = json.loads(data)
    except (OSError, ValueError):
        return None, None, {}
    sha = hashlib.sha256(data).hexdigest()
    return values, sha, accepted_shas(values, sha)
