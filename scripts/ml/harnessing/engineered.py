"""Engineered harness candidates: human-written components that must pass the same gate as learned edits.

Each candidate is a function from a base tree to a candidate tree. Its components live inside the
`addr-roof-tracing` skill, so a candidate that passes the gate can be promoted to the project skill as is.
"""
from __future__ import annotations

from pathlib import Path

from .adapter import SKILL

ASSETS = Path(__file__).parent/'assets'
AUDIT_ANCHOR = '[boundary-fit.md](references/boundary-fit.md).\n'
EDGE_PROFILE_STEP = """\
   Measure every edge with the skill's `scripts/edge_profile.py` before tracing and again on a
   proposal before applying: it reports where the strongest image step lies relative to each drawn
   edge and flags a dark band beyond a bright roof. A consistent offset over 1.5 px is a lead to
   inspect at native pixels; a dark band just outside a bright roof is shadow or wall, so keep it out.
"""
EDGE_PROFILE_GUIDANCE = f"""
`python3 skills/{SKILL}/scripts/edge_profile.py` profiles every edge of the current outline (add
`--preview PREVIEW_ID` for a proposal, `--edge K` for one edge, `--json` for numbers): the offset of the
strongest image step from each drawn edge (+ = outside), how consistently samples agree, and dark bands
beyond the line.
"""


def edge_profile(tree):
    skill = f'skills/{SKILL}/SKILL.md'
    if AUDIT_ANCHOR not in tree.get(skill, ''):
        raise ValueError(f'{skill} no longer contains the step-3 audit anchor; update engineered.edge_profile')
    out = dict(tree)
    out[f'skills/{SKILL}/scripts/edge_profile.py'] = (ASSETS/'edge_profile.py').read_text(encoding='utf-8')
    out[skill] = tree[skill].replace(AUDIT_ANCHOR, AUDIT_ANCHOR + EDGE_PROFILE_STEP, 1)
    out['tool_guidance.md'] = tree.get('tool_guidance.md', '').rstrip() + '\n' + EDGE_PROFILE_GUIDANCE
    return out


CANDIDATES = {'edge-profile': edge_profile}
