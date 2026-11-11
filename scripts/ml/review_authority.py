"""Shared exclusions before an existing consumer decides whether a review is a label.

review_authority_blocker names why an object is excluded (not a human label). carried_undecided is not an exclusion:
a carried object still needing adjustment is a target the reviewer has yet to decide on the new image (round-5 RULES
human_precision.classes undecided; DESIGN.json status_policy), so it is never human geometry and never counted as excluded.
"""
from __future__ import annotations


def review_authority_blocker(obj):
    """Return why an object is not a human label; absence does not imply approval."""
    provenance = obj.get('provenance')
    if isinstance(provenance, dict) and str(provenance.get('source') or '').startswith('machine:'):
        return 'machine_label'
    imported = obj.get('fieldwork_import')
    if isinstance(imported, dict) and imported.get('pending') is True:
        return 'fieldwork_pending'
    return None


def carried(obj):
    """True when the object carries a non-empty carried_from: the recapture carry brought it into a new frame.

    The contract (frame-transform-v1) writes a dict, but the frozen round-5 RULES text reads 'any object carrying
    carried_from' and nothing on the save path rejects another shape yet, so any non-empty value is a carry; only a
    missing or empty value ({} / None / '' / []) is not. r5_precision's 'carried' fact column reads this same test.
    """
    return bool(obj.get('carried_from'))


def carried_undecided(obj):
    """True when the object was carried into a new frame (carried) and still needs adjustment.

    Whatever its other fields say (an edited ring, roof_line_edits, geometry_reviewed, the shape of carried_from), it is
    the reviewer's target, not a decision; confirmed / rejected / superseded on the new image are the reviewer's own
    and are not carried-undecided.
    """
    return carried(obj) and obj.get('status') == 'needs_adjustment'
