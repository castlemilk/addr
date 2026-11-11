"""Vegetation sub-class proposals: rule-based sidecar prototype (vegetation-classes-2026-10-08, lens E: vegproto).

Reviewer (chat, 8 Oct ~08:45 AEDT): "can you classify vegetation further to: grass, hedge, tree etc".

Sidecar, proposals only. Every live `tree` / `vegetation` object of the review layer on the 54 recapture windows gets one
proposed sub-class with its evidence and a confidence. Kinds are not changed, nothing under data/scenes is written, no
inference runs (pixel statistics of the frame image only).

Features: the census measurement (vegetation_census.measure; definitions in its docstring) re-run into prototype/features/
so the review layer is current, plus nDSM height (p50 / p90) where an nDSM covers the window. Coverage is tested against
the LINZ catalogue's lon / lat bbox per window; none of the 54 Melbourne windows is covered today, so every height is null
and the height branch runs only in the tests.

Sub-classes (the reviewer's words first): grass (lawn), hedge, shrub (bush), tree, tree-group, garden-bed, and `unclear`
when the evidence conflicts or a flag blocks a reading. garden-bed is never proposed by the rule (census by-eye 4 of 12;
textured mown verges look the same at 6-12 cm); it is offered as a candidate on unclear lines.
Flags (review routing, not classes): speck, not-green, shadow, on-roof block a reading (unclear); frame-cut (the window
edge cuts the object) makes spread a lower bound and a strip shape untrustworthy.

classify(row) is a pure function of one feature row (tests: tests/test_vegetation_classes.py, synthetic rows):
  1. Blocking flags (census RULE v4 thresholds) -> unclear.
  2. Stature votes, ground vs standing; weights strong 3 / medium 2 / weak 1:
       height p90 (when covered)   strong  < 0.4 m ground, else standing
       surface smooth              medium  ground (census lawn test: texture, L_rel, outer-ring shadow, Lab hue band)
       surface dark                medium  standing (L_rel < -5, not smooth: a self-shaded crown)
       surface textured / mixed    none    (sunlit crowns, garden beds and striped verges all look like this)
       cast shadow                 medium  standing (census v4 test); weak when the window's sun bearing is unreliable
       kind prior                  medium  tree ('tree canopy' prompt) standing, strong when the reviewer confirmed it;
                           weak    vegetation ('grass' prompt) ground (SAM3 'grass' also returns hedges and beds)
     Without height: a medium-or-stronger vote on each side with totals less than 2 apart, or equal totals -> unclear
     (stature conflict / tie); otherwise the larger total wins.
     With height: height decides; opposing votes only lower the confidence.
  3. Form. ground: smooth -> grass; mixed (or dark under a ground height) -> grass at factor 0.75; textured -> unclear
     (grass / garden-bed). standing: strip (census strip test) cut by the frame along half its length -> unclear; strip
     -> hedge; area >= 150 m2 -> tree-group; with height p90 < 3 m -> shrub, else tree; without height spread >= 4 m ->
     tree; spread < 4 m on a frame-cut object -> unclear (shrub / cut tree); else shrub.
  4. Confidence score = agree / (agree + oppose + 1) x form factor; factor 0.75 for shrub / tree inside the 2.5-5 m spread
     band and for hedge without height, 0.85 for small shrubs, for tree-group over 300 m2 and for any standing class on
     a frame-cut object, 0.6 for tree-group at 150-300 m2 (crowns are not counted). high >= 0.75, medium >= 0.55.
v1 -> v2 (after the first read of the v1 contact sheets, notes in summary.json 'rule_history'): a 'tree canopy' object
with a smooth light surface was proposed as grass (weak kind vote); 3 of 3 readable panels were crowns (#486, #490,
#1382 in v1), and two clear crowns with cast shadows (#54, #1425) were unclear because one smooth vote blocked them.
The tree kind vote became medium (strong when confirmed) and a conflict now needs totals less than 2 apart. About half
of the 150-300 m2 tree-group panels were one large crown, so that band is held to low confidence.
Thresholds come from the census rule and its by-eye reads (census/notes.md, byeye.json) plus the v2 change above; they
are not tuned for accuracy and not validated against labels.

Run from the repo root (each step about 15 s; heavy steps under nice -n 19):
  scripts/ml/.venv/bin/python -m scripts.ml.experiments.vegetation_classes measure   # features/rows.jsonl (+coverage)
  scripts/ml/.venv/bin/python -m scripts.ml.experiments.vegetation_classes classify  # proposals.jsonl, summary.json
  scripts/ml/.venv/bin/python -m scripts.ml.experiments.vegetation_classes render    # renders/<class>/*, contact sheets
  scripts/ml/.venv/bin/python -m scripts.ml.experiments.vegetation_classes marks     # MARKS-vegetation-v1.md
Outputs ONLY under data/reviews/eyes/experiments/vegetation-classes-2026-10-08/prototype/ (+ heartbeat lines in
.run/session-2026-10-08-cuts-veg/vegproto/progress.txt).

DESIGN v2 (after the by-eye skeptic, .run/session-2026-10-08-cuts-veg/eye-veg.md; section "design v2" below). Not the
"v1 -> v2" rule history above, which is the published v1 (MARKS-vegetation-v1.md). The v1 functions and RULE are not
changed: `classify` / `classify_run` still reproduce prototype/ (classify-v2 re-runs v1 on every row and counts identical
proposals). v2 changes three things and keeps everything else:
  1. No tree-group class. v1's band (standing, not a strip, area >= 150 m2) is tree. The crown-split sidecar's peak finder
     (tree_crown_split.count_peaks, SIDECAR_V1 parameters) counts crown peaks there; >= 2 adds the note "possible group:
     see the crown-split card". Class and confidence do not depend on the note.
  2. Shrub without height only on strong evidence: spread < 3 m, compact (elongation <= 1.5, solidity >= 0.9), alone (no
     live tree-kind crown with >= 2x its area within 1 m), low texture contrast (texture p50 < 0.055, the census smooth
     ceiling) and no cast shadow (census v4 cue). Otherwise tree, shown "tree (or shrub)", uncertain, low confidence.
  3. Clipped hedge: a census strip with a smooth surface, its own cast shadow under a reliable sun bearing and a box shape
     (area / minimum rotated rectangle >= 0.8; an ellipse fills 0.785) is a hedge (low) instead of a ground-vs-standing
     conflict.
  scripts/ml/.venv/bin/python -m scripts.ml.experiments.vegetation_classes measure-v2   # prototype-v2/features/extras.jsonl
  scripts/ml/.venv/bin/python -m scripts.ml.experiments.vegetation_classes classify-v2  # proposals.jsonl, summary.json
  scripts/ml/.venv/bin/python -m scripts.ml.experiments.vegetation_classes render-v2    # renders/, contact sheets
  scripts/ml/.venv/bin/python -m scripts.ml.experiments.vegetation_classes marks-v2     # MARKS-vegetation-v2.md
v2 reads v1's frozen feature rows (prototype/features/rows.jsonl) and writes ONLY under prototype-v2/ (+ heartbeat lines in
.run/session-2026-10-08-cuts-veg/veg-v2/progress.txt). Line numbers are v1's.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import random
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EXP_DIR = ROOT/'data/reviews/eyes/experiments/vegetation-classes-2026-10-08'
OUT_DIR = EXP_DIR/'prototype'
FEATURES_DIR = OUT_DIR/'features'
CENSUS_DIR = EXP_DIR/'census'
RUN_DIR = ROOT/'.run/session-2026-10-08-cuts-veg/vegproto'
SCENES = ROOT/'data/scenes'
LINZ_CATALOG = ROOT/'data/elevation/linz-chc-1m/christchurch_2024-2025/catalog.json'
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

TYPES = ('grass', 'hedge', 'shrub', 'tree', 'tree-group', 'garden-bed')
PROPOSED = TYPES + ('unclear',)
BLOCKING_FLAGS = ('speck', 'not-green', 'shadow', 'on-roof')
ANSWER_WORDS = TYPES + ('not-veg',)
EDITOR_URL = 'http://localhost:3008/scenes?scene={}&status=all&frame=all&object={}&layer=edited'

RULE = {
    # Copied from the census RULE v4 (vegetation_census.RULE); summary.json records whether they still match.
    'speck_area_max': 1.0, 'not_veg_green_max': 0.25, 'shadow_dominant_min': 0.5, 'on_roof_min': 0.5,
    'lawn_texture_max': 0.055, 'lawn_L_rel_min': 0.0, 'lawn_smooth_texture_max': 0.035, 'lawn_smooth_L_rel_min': -5.0,
    'lawn_ring_shadow_max': 0.15, 'lawn_hue': (85.0, 200.0), 'sliver_frame_share_min': 0.5,
    'hedge_width': (0.5, 4.0), 'hedge_length_min': 4.0, 'hedge_elongation_min': 3.0, 'hedge_solidity_min': 0.6,
    'hedge_lee_max': -0.2, 'cast_shadow_min': 0.1, 'tree_group_area_min': 150.0, 'tree_eq_diam_min': 4.0,
    # Prototype-only.
    'dark_L_rel_max': -5.0,       # median L* more than 5 below the frame's median green, not smooth -> self-shaded crown
    'ground_height_max': 0.4,     # nDSM p90 below this -> ground cover (grass / garden bed)
    'tall_height_min': 3.0,       # nDSM p90 at or above this -> tree height (a strip stays a hedge: screen planting)
    'frame_cut_edge_min': 1.0,    # frame contact >= 1 m (or clipped_by_frame) -> frame-cut
    'spread_band': (2.5, 5.0),    # shrub / tree read from spread alone inside this band -> factor 0.75
    'factor_band': 0.75, 'factor_hedge_no_height': 0.75, 'factor_mixed_ground': 0.75,
    'factor_small_shrub': 0.85, 'factor_tree_group': 0.85, 'factor_frame_cut': 0.85,
    'tree_group_sure_area': 300.0, 'factor_tree_group_band': 0.6,   # v2: half the 150-300 m2 panels were one crown
    'conflict_margin': 2,         # v2: firm votes on both sides with totals closer than this -> unclear
    'conf_high': 0.75, 'conf_medium': 0.55,
}
RULE_HISTORY = [
    'v1: tree kind weak (medium when confirmed); any firm vote on both sides -> unclear; tree-group factor 0.85',
    'v2 (first read of the v1 contact sheets): tree-kind smooth objects proposed grass were crowns on 3 of 3 readable '
    'panels; crowns with a cast shadow went unclear on one smooth vote (2 panels); about half the 150-300 m2 '
    'tree-group panels were one crown. Tree kind medium (strong confirmed), conflict needs totals < 2 apart, '
    'tree-group 150-300 m2 factor 0.6.',
]
CENSUS_SHARED = [k for k in RULE if not k.startswith(('dark_', 'ground_', 'tall_', 'frame_cut', 'spread_', 'factor_',
                                                       'conf_', 'tree_group_sure', 'conflict_'))]
WEIGHT = {'strong': 3, 'medium': 2, 'weak': 1}
FLAG_CANDIDATES = {
    'speck': {'tree': ['shrub', 'tree'], 'vegetation': ['grass']},
    'not-green': {'tree': ['tree', 'not-veg'], 'vegetation': ['garden-bed', 'not-veg']},  # leafless / red-leaf / bare
    'shadow': {'tree': ['tree', 'not-veg'], 'vegetation': ['grass', 'not-veg']},          # deep-shade crown or a cast shadow
    'on-roof': {'tree': ['not-veg', 'tree'], 'vegetation': ['not-veg', 'grass']},         # roof misread, or foliage over roof
}
CLASS_COLOURS = {'grass': (190, 255, 90), 'hedge': (255, 170, 0), 'shrub': (255, 240, 0), 'tree': (40, 220, 60),
                 'tree-group': (0, 140, 255), 'garden-bed': (200, 120, 60), 'unclear': (255, 60, 200)}


def now():
    return datetime.now(UTC).isoformat(timespec='seconds')


def heartbeat(message):
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    with open(RUN_DIR/'progress.txt', 'a') as handle:
        handle.write(f'{datetime.now().strftime("%H:%M:%S")} vegproto: {message}\n')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ----------------------------------------------------------------------------------------------- the rule (pure)

def in_band(value, band):
    return value is not None and band[0] <= value <= band[1]


def surface(row):
    """'smooth' (census lawn test), 'smooth-shaded' (smooth, shadow in the outer ring), 'dark' (self-shaded crown),
    'textured' (light, rough) or 'mixed' (the rest)."""
    t = RULE
    tex, lrel = row['texture_p50'], row['lab_L_rel']
    ring = row.get('ring_shadow_share') or 0.0
    smooth = ((tex < t['lawn_texture_max'] and lrel >= t['lawn_L_rel_min'])
              or (tex < t['lawn_smooth_texture_max'] and lrel >= t['lawn_smooth_L_rel_min']))
    if smooth and in_band(row.get('hue_deg'), t['lawn_hue']):
        # The census lawn test also asks for little shadow in the outer ring. Here a smooth patch with shadow around it
        # (a lawn strip beside a building's shadow, or a clipped hedge's own shadow) keeps a weak ground vote, so the
        # cast-shadow cue is not counted twice.
        return 'smooth' if ring < t['lawn_ring_shadow_max'] else 'smooth-shaded'
    if lrel < t['dark_L_rel_max']:
        return 'dark'
    if tex >= t['lawn_texture_max']:
        return 'textured'
    return 'mixed'


def cast_shadow(row):
    """(casts, lee minus sun-side shadow-class share): census v4 test, a darker lee band holding shadow-class pixels."""
    lee = row.get('lee_contrast')
    lee_shadow = (row.get('lee_shadow_share') or 0.0)-(row.get('sun_shadow_share') or 0.0)
    return (lee is not None and lee <= RULE['hedge_lee_max'] and lee_shadow >= RULE['cast_shadow_min']), lee_shadow


def strip_shape(row):
    t = RULE
    return (in_band(row['mrr_width_m'], t['hedge_width']) and row['mrr_length_m'] >= t['hedge_length_min']
            and row['elongation'] >= t['hedge_elongation_min'] and row['solidity'] >= t['hedge_solidity_min'])


def frame_state(row):
    """(frame_cut, sliver): the window edge cuts the object; a sliver is an elongated piece cut along half its length."""
    t = RULE
    edge = row.get('frame_edge_m') or 0.0
    cut = edge >= t['frame_cut_edge_min'] or bool(row.get('clipped_by_frame'))
    sliver = row['elongation'] >= t['hedge_elongation_min'] and edge >= t['sliver_frame_share_min']*row['mrr_length_m']
    return cut, sliver


def blocking_flags(row):
    t = RULE
    hue_ok = in_band(row.get('hue_deg'), t['lawn_hue'])
    out = []
    if row['area_m2'] < t['speck_area_max']:
        out.append(('speck', f"area {row['area_m2']:.2f} m2"))
    if row['green_share'] < t['not_veg_green_max'] and not hue_ok:
        out.append(('not-green', f"green share {row['green_share']:.2f}, hue {row.get('hue_deg') or 0:.0f}"))
    if row['shadow_share'] >= t['shadow_dominant_min']:
        out.append(('shadow', f"shadow-class share {row['shadow_share']:.2f}"))
    if row['building_overlap'] >= t['on_roof_min']:
        out.append(('on-roof', f"under a live building {row['building_overlap']:.2f}"))
    return out


def stature_votes(row):
    """(surface label, [(cue, text, side, weight)]) for side in ground / standing."""
    s = surface(row)
    votes = []
    height = row.get('height_p90_m')
    if height is not None:
        side = 'ground' if height < RULE['ground_height_max'] else 'standing'
        votes.append(('height', f'nDSM p90 {height:.1f} m', side, WEIGHT['strong']))
    tex_txt = f"texture {row['texture_p50']:.3f}, L rel {row['lab_L_rel']:+.0f}"
    if s == 'smooth':
        votes.append(('surface', f'smooth ({tex_txt})', 'ground', WEIGHT['medium']))
    elif s == 'smooth-shaded':
        votes.append(('surface', f"smooth, shadow around it ({tex_txt}, ring {row.get('ring_shadow_share') or 0:.2f})",
                      'ground', WEIGHT['weak']))
    elif s == 'dark':
        votes.append(('surface', f'dark crown ({tex_txt})', 'standing', WEIGHT['medium']))
    casts, lee_shadow = cast_shadow(row)
    if casts:
        reliable = row.get('sun_reliable') is not False
        votes.append(('cast shadow', f"lee {row['lee_contrast']:+.2f}, lee shadow {lee_shadow:+.2f}"
                      + ('' if reliable else ', sun bearing unreliable'), 'standing',
                      WEIGHT['medium'] if reliable else WEIGHT['weak']))
    confirmed = row.get('status') == 'confirmed'
    if row['kind'] == 'tree':      # v2: medium (v1 weak), strong when the reviewer confirmed the kind
        votes.append(('kind', "tree ('tree canopy')" + (' confirmed by reviewer' if confirmed else ''), 'standing',
                      WEIGHT['strong'] if confirmed else WEIGHT['medium']))
    else:
        votes.append(('kind', "vegetation ('grass')" + (' confirmed by reviewer' if confirmed else ''), 'ground',
                      WEIGHT['weak']))
    return s, votes


def standing_form(row, cut, sliver):
    """(class or 'unclear', factor, text, candidates, unclear_reason) for a standing object."""
    t = RULE
    height = row.get('height_p90_m')
    spread, area = row['eq_diam_m'], row['area_m2']
    frame = t['factor_frame_cut'] if cut else 1.0
    if strip_shape(row):
        strip_txt = f"strip {row['mrr_width_m']:.1f} x {row['mrr_length_m']:.1f} m"
        if sliver:
            return ('unclear', 1.0, f'{strip_txt}, but the frame cuts it along half its length',
                    ['hedge', 'tree', 'shrub'], 'frame-cut-strip')
        factor = (1.0 if height is not None else t['factor_hedge_no_height'])*frame
        return 'hedge', factor, strip_txt, ['hedge', 'tree' if spread >= t['tree_eq_diam_min'] else 'shrub'], None
    if area >= t['tree_group_area_min']:
        factor = t['factor_tree_group'] if area >= t['tree_group_sure_area'] else t['factor_tree_group_band']
        return 'tree-group', factor*frame, f'{area:.0f} m2 canopy (crowns not counted)', ['tree-group', 'tree'], None
    if height is not None:
        if height < t['tall_height_min']:
            return 'shrub', frame, f'low ({height:.1f} m), spread {spread:.1f} m', ['shrub', 'hedge'], None
        return 'tree', frame, f'tall ({height:.1f} m), spread {spread:.1f} m', ['tree', 'shrub'], None
    band = in_band(spread, t['spread_band'])
    if spread >= t['tree_eq_diam_min']:
        return ('tree', (t['factor_band'] if band else 1.0)*frame, f'spread {spread:.1f} m (no height)',
                ['tree', 'shrub'], None)
    if cut:
        return ('unclear', 1.0, f'spread {spread:.1f} m is a lower bound: the frame cuts it', ['shrub', 'tree'],
                'frame-cut-small')
    return ('shrub', t['factor_band'] if band else t['factor_small_shrub'], f'spread {spread:.1f} m (no height)',
            ['shrub', 'tree'], None)


def ground_form(row, s):
    if s == 'smooth':
        return 'grass', 1.0, 'smooth ground cover', ['grass', 'garden-bed'], None
    if s == 'textured':
        return ('unclear', 1.0, 'textured ground cover: a garden bed or a striped / patchy lawn',
                ['grass', 'garden-bed'], 'textured-ground')
    return 'grass', RULE['factor_mixed_ground'], f'{s} ground surface', ['grass', 'garden-bed'], None   # mixed / shaded


def confidence_label(score):
    if score >= RULE['conf_high']:
        return 'high'
    return 'medium' if score >= RULE['conf_medium'] else 'low'


def classify(row):
    """Proposal for one feature row: {proposed, confidence, score, candidates, flags, unclear_reason, stature, evidence,
    reason}. Pure: the row is not changed."""
    cut, sliver = frame_state(row)
    flags = blocking_flags(row)
    frame_flags = (['frame-cut'] if cut else [])+(['frame-sliver'] if sliver else [])
    evidence = [f'{name}: {text}' for name, text in flags]
    if cut:
        evidence.append(f"frame: {row.get('frame_edge_m') or 0:.1f} m on the window edge"
                        + (' (clipped_by_frame)' if row.get('clipped_by_frame') else ''))
    if row.get('height_p90_m') is None:
        evidence.append('height: none (no nDSM covers the window)')
    base = {'flags': [f for f, _ in flags]+frame_flags, 'evidence': evidence}
    if flags:
        name = flags[0][0]
        kind = row['kind'] if row['kind'] in ('tree', 'vegetation') else 'tree'
        return {**base, 'proposed': 'unclear', 'confidence': 'none', 'score': 0.0,
                'candidates': FLAG_CANDIDATES[name][kind], 'unclear_reason': f'flag:{name}', 'stature': None,
                'reason': f'flag {name} ({flags[0][1]})'}
    s, votes = stature_votes(row)
    evidence.extend(f'{cue} -> {side} ({w}): {text}' for cue, text, side, w in votes)
    ground = sum(w for *_, side, w in votes if side == 'ground')
    standing = sum(w for *_, side, w in votes if side == 'standing')
    height = row.get('height_p90_m')
    if height is None:
        firm = {side for *_, side, w in votes if w >= WEIGHT['medium']}
        conflict = firm == {'ground', 'standing'} and abs(ground-standing) < RULE['conflict_margin']
        if conflict or ground == standing:
            reason = 'stature-tie' if ground == standing else 'stature-conflict'
            g_cls, *_ = ground_form(row, s)
            g_cands = ['grass', 'garden-bed'] if g_cls == 'unclear' else [g_cls]
            st = standing_form(row, cut, sliver)
            s_cands = st[3] if st[0] == 'unclear' else [st[0]]
            standing_first = standing > ground or (standing == ground and row['kind'] == 'tree')   # tie: kind leads
            cands = (s_cands+g_cands) if standing_first else (g_cands+s_cands)
            g_txt = ', '.join(c for c, _, side, _w in votes if side == 'ground')
            s_txt = ', '.join(c for c, _, side, _w in votes if side == 'standing')
            return {**base, 'proposed': 'unclear', 'confidence': 'none', 'score': 0.0,
                    'candidates': list(dict.fromkeys(cands)), 'unclear_reason': reason, 'stature': None,
                    'reason': f'ground ({g_txt}) vs standing ({s_txt})'}
        stature = 'ground' if ground > standing else 'standing'
    else:
        stature = 'ground' if height < RULE['ground_height_max'] else 'standing'
    agree, oppose = (ground, standing) if stature == 'ground' else (standing, ground)
    if stature == 'ground':
        cls, factor, form_txt, cands, why = ground_form(row, s if not (s == 'dark' and height is not None) else 'mixed')
    else:
        cls, factor, form_txt, cands, why = standing_form(row, cut, sliver)
    evidence.append(f'form: {form_txt}')
    if cls == 'unclear':
        return {**base, 'proposed': 'unclear', 'confidence': 'none', 'score': 0.0, 'candidates': cands,
                'unclear_reason': why, 'stature': stature, 'reason': f'{stature}; {form_txt}'}
    score = round(agree/(agree+oppose+1)*factor, 3)
    cue_txt = '; '.join(text for _, text, side, _w in votes if side == stature)
    return {**base, 'proposed': cls, 'confidence': confidence_label(score), 'score': score, 'candidates': cands,
            'unclear_reason': None, 'stature': stature,
            'reason': f'{stature} ({cue_txt}); {form_txt}' + (f'; against: {oppose}' if oppose else '')}


LABEL_WORDS = [(r'grass|lawn|turf', 'grass'), (r'hedge', 'hedge'), (r'shrub|bush', 'shrub'),
               (r'garden|bed|mulch', 'garden-bed'), (r'group|trees', 'tree-group'), (r'\btree\b', 'tree')]
DEFAULT_LABELS = {'tree': 'Tree', 'vegetation': 'Vegetation'}
SEEN_MAP = {'lawn': ('grass',), 'tree-or-shrub': ('tree', 'shrub'), 'not-vegetation': ('not-veg',),
            'uncertain': ()}   # census byeye.json seen_class words -> prototype classes


def label_class(text):
    """Sub-class a typed label names; 'vegetation' for the generic word (any spelling: the reviewer typed 'vegitation'),
    which stands for the ground / low family; None otherwise. Callers skip the default kind labels."""
    low = (text or '').lower()
    for pattern, cls in LABEL_WORDS:
        if re.search(pattern, low):
            return cls
    return 'vegetation' if re.search(r'veg[ei]?t', low) else None


LABEL_FAMILY = {'tree': {'tree', 'tree-group'}, 'tree-group': {'tree-group', 'tree'},
                'vegetation': {'grass', 'garden-bed', 'hedge', 'shrub'}}


def label_agrees(label_cls, proposal):
    if label_cls is None or proposal['proposed'] == 'unclear':
        return None
    return proposal['proposed'] in LABEL_FAMILY.get(label_cls, {label_cls})


# ----------------------------------------------------------------------------------------------- the run

@contextlib.contextmanager
def patched(module, **values):
    old = {k: getattr(module, k) for k in values}
    for k, v in values.items():
        setattr(module, k, v)
    try:
        yield module
    finally:
        for k, v in old.items():
            setattr(module, k, v)


def measure(args):
    """Census measurement re-run into features/ (census code unchanged; its output paths redirected here)."""
    from . import vegetation_census as vc
    FEATURES_DIR.mkdir(parents=True, exist_ok=True)
    heartbeat('measure start (census measure, outputs redirected to prototype/features)')
    with patched(vc, OUT_DIR=FEATURES_DIR, RUN_DIR=RUN_DIR):
        vc.measure(args)
    heartbeat('measure done')


def height_coverage(scene_ids):
    """Per window: does an nDSM cover its centroid? LINZ Christchurch catalogue bbox (lon / lat) for both layers."""
    catalog = json.loads(LINZ_CATALOG.read_text())
    boxes = [b for layer in catalog['layers'].values() for b in layer['extent']['spatial']['bbox']]
    out = {}
    for scene_id in scene_ids:
        c = json.loads((SCENES/scene_id/'latest.json').read_text())['centroid']
        out[scene_id] = any(b[0] <= c['lon'] <= b[2] and b[1] <= c['lat'] <= b[3] for b in boxes)
    return out


def review_extras(scene_ids):
    """Read-only: label, notes, prompt of each live tree / vegetation object (the reviewer's own words)."""
    out = {}
    for scene_id in scene_ids:
        review = json.loads((SCENES/scene_id/'review.json').read_text())
        for o in review.get('objects', []):
            if o['kind'] in ('tree', 'vegetation'):
                expected = {'tree canopy': 'tree', 'grass': 'vegetation'}.get(o.get('prompt'))
                out[(scene_id, o['id'])] = {'label': o.get('label'), 'notes': o.get('notes'), 'prompt': o.get('prompt'),
                                            'kind_changed': o.get('prompt') is not None and expected != o['kind']}
    return out


def reviewer_words(row, extra):
    """The reviewer's own typed text on the object: a non-default label, or notes."""
    words = []
    label = (extra or {}).get('label')
    if label and label != DEFAULT_LABELS.get(row['kind']):
        words.append(f'label "{label}"')
    if (extra or {}).get('notes'):
        words.append(f"notes \"{extra['notes'][:80]}\"")
    return words


def load_features():
    return [json.loads(line) for line in open(FEATURES_DIR/'rows.jsonl') if line.strip()]


FEATURE_KEYS = ('area_m2', 'eq_diam_m', 'mrr_width_m', 'mrr_length_m', 'elongation', 'solidity', 'texture_p50',
                'lab_L_rel', 'hue_deg', 'green_share', 'shadow_share', 'ring_shadow_share', 'lee_contrast',
                'lee_shadow_share', 'sun_shadow_share', 'sun_reliable', 'building_overlap', 'frame_edge_m',
                'clipped_by_frame', 'mpp', 'height_p50_m', 'height_p90_m')


def classify_run(args):
    rows = [x for x in load_features() if not x.get('empty')]
    ids = sorted({x['scene'] for x in rows})
    covered = height_coverage(ids)
    extras = review_extras(ids)
    census = {}
    if (CENSUS_DIR/'rows.jsonl').exists():
        census = {(c['scene'], c['id']): c.get('proposed') for c in map(json.loads, open(CENSUS_DIR/'rows.jsonl'))}
    order = {c: i for i, c in enumerate(PROPOSED)}
    out = []
    for row in rows:
        row['height_p50_m'] = row['height_p90_m'] = None     # no window covered (height_coverage); sampler not built
        p = classify(row)
        extra = extras.get((row['scene'], row['id']), {})
        lab_cls = label_class(extra.get('label')) if extra.get('label') != DEFAULT_LABELS.get(row['kind']) else None
        out.append({'scene': row['scene'], 'id': row['id'], 'kind': row['kind'], 'status': row['status'],
                    'label': extra.get('label'), 'reviewer_words': reviewer_words(row, extra),
                    'reviewer_kind_change': bool(extra.get('kind_changed')), 'label_class': lab_cls,
                    'label_agrees': label_agrees(lab_cls, p), 'census_v4': census.get((row['scene'], row['id'])),
                    'height_covered': covered[row['scene']], **p,
                    'features': {k: row.get(k) for k in FEATURE_KEYS}})
    out.sort(key=lambda x: (x['scene'], order[x['proposed']], -(x['features']['area_m2'] or 0)))
    for number, x in enumerate(out, 1):
        x['number'] = number
    with open(OUT_DIR/'proposals.jsonl', 'w') as handle:
        for x in out:
            handle.write(json.dumps(x, sort_keys=True)+'\n')
    summary = summarise(out, rows, covered)
    (OUT_DIR/'summary.json').write_text(json.dumps(summary, indent=1))
    lines = ['| proposed | objects | windows | from tree | from vegetation | high | medium | low | frame-cut |',
             '|---|---|---|---|---|---|---|---|---|']
    for c in PROPOSED:
        b = summary['by_class'][c]
        lines.append(f"| {c} | {b['objects']} | {b['windows']} | {b['from_kind'].get('tree', 0)} | "
                     f"{b['from_kind'].get('vegetation', 0)} | {b['confidence'].get('high', 0)} | "
                     f"{b['confidence'].get('medium', 0)} | {b['confidence'].get('low', 0)} | {b['frame_cut']} |")
    lines += ['', '| unclear reason | objects |', '|---|---|']
    lines += [f'| {k} | {v} |' for k, v in summary['unclear_by_reason'].items()]
    (OUT_DIR/'tables.md').write_text('\n'.join(lines)+'\n')
    heartbeat(f"classify {len(out)} objects; unclear share {summary['unclear_share']}")
    print('\n'.join(lines[:len(PROPOSED)+2]))
    print(f"unclear share {summary['unclear_share']}; by reason {summary['unclear_by_reason']}")


def summarise(out, rows, covered):
    from . import shadow_census as sc
    from . import vegetation_census as vc
    by_class = {}
    for c in PROPOSED:
        sub = [x for x in out if x['proposed'] == c]
        by_class[c] = {'objects': len(sub), 'windows': len({x['scene'] for x in sub}),
                       'from_kind': dict(Counter(x['kind'] for x in sub)),
                       'confidence': dict(Counter(x['confidence'] for x in sub)),
                       'confirmed': sum(x['status'] == 'confirmed' for x in sub),
                       'frame_cut': sum('frame-cut' in x['flags'] for x in sub)}
    unclear = [x for x in out if x['proposed'] == 'unclear']
    typed = [x for x in out if x['reviewer_words']]
    comparable = [x for x in typed if x['label_agrees'] is not None]
    confirmed = [x for x in out if x['status'] == 'confirmed']
    fits = {'tree': {'tree', 'tree-group', 'shrub', 'hedge'}, 'vegetation': {'grass', 'garden-bed', 'hedge', 'shrub'}}
    consistent = [x for x in confirmed if x['proposed'] != 'unclear']
    cross = defaultdict(Counter)
    for x in out:
        cross[x['census_v4'] or 'none'][x['proposed']] += 1
    byeye = []
    if (CENSUS_DIR/'byeye.json').exists():
        prop = {(x['scene'], x['id']): x for x in out}
        for v in json.loads((CENSUS_DIR/'byeye.json').read_text())['rows']:
            x = prop.get((v['scene'], v['id']))
            if x:
                seen = SEEN_MAP.get(v['seen_class'], (v['seen_class'],))
                byeye.append({'scene': v['scene'], 'id': v['id'], 'seen': v['seen_class'], 'proposed': x['proposed'],
                              'candidates': x['candidates'], 'confidence': x['confidence'],
                              'match': x['proposed'] in seen,
                              'seen_in_candidates': x['proposed'] == 'unclear' and bool(set(seen) & set(x['candidates']))})
    frame_cut = [x for x in out if 'frame-cut' in x['flags']]
    return {
        'version': 'vegetation-classes-v1', 'computed_at': now(), 'code_sha256': CODE_SHA256,
        'inputs': {'features': str((FEATURES_DIR/'rows.jsonl').relative_to(ROOT)),
                   'census_code_sha256': sha(ROOT/'scripts/ml/experiments/vegetation_census.py'),
                   'shadow_code_sha256': sha(ROOT/'scripts/ml/experiments/shadow_census.py'),
                   'shadow_calibration_sha256': sha(vc.SHADOW_CALIBRATION), 'sun_sha256': sha(vc.SUN_FILE),
                   'shadow_thresholds': sc.load_thresholds()},
        'rule': RULE, 'rule_history': RULE_HISTORY, 'rule_matches_census_v4': all(vc.RULE.get(k) == RULE[k] for k in CENSUS_SHARED),
        'population': {'windows': len(covered), 'objects': len(out), 'by_kind': dict(Counter(x['kind'] for x in out)),
                       'by_status': dict(Counter(f"{x['kind']}:{x['status']}" for x in out)),
                       'empty_rows': sum(bool(r.get('empty')) for r in load_features())},
        'height': {'windows_covered': sum(covered.values()), 'source': 'LINZ Christchurch 2024-25 1 m nDSM catalogue bbox',
                   'note': 'Melbourne windows; Vic LiDAR licensed; height branch exercised in tests only'},
        'by_class': by_class,
        'counts': {c: by_class[c]['objects'] for c in PROPOSED},
        'unclear_share': round(len(unclear)/max(len(out), 1), 3),
        'unclear_by_reason': dict(Counter(x['unclear_reason'] for x in unclear).most_common()),
        'unclear_lean': dict(Counter(x['candidates'][0] for x in unclear).most_common()),
        'confidence': dict(Counter(x['confidence'] for x in out)),
        'flags': dict(Counter(f for x in out for f in x['flags']).most_common()),
        'frame_cut': {'objects': len(frame_cut), 'unclear': sum(x['proposed'] == 'unclear' for x in frame_cut),
                      'unclear_because_of_frame': sum((x['unclear_reason'] or '').startswith('frame') for x in frame_cut)},
        'reviewer_typed': {'objects_with_own_words': len(typed), 'comparable': len(comparable),
                           'agree': sum(bool(x['label_agrees']) for x in comparable),
                           'rows': [{k: x[k] for k in ('scene', 'id', 'kind', 'status', 'label', 'reviewer_words',
                                                      'reviewer_kind_change', 'label_class', 'proposed', 'candidates',
                                                      'reason')} for x in typed]},
        'confirmed_kind_consistency': {
            'note': 'reviewer-confirmed kind vs the proposed sub-class family (tree: tree / tree-group / shrub / hedge; '
                    'vegetation: grass / garden-bed / hedge / shrub); not accuracy',
            'confirmed': len(confirmed), 'unclear': len(confirmed)-len(consistent),
            'consistent': sum(x['proposed'] in fits[x['kind']] for x in consistent),
            'inconsistent': [{k: x[k] for k in ('scene', 'id', 'kind', 'proposed', 'reason')}
                             for x in consistent if x['proposed'] not in fits[x['kind']]]},
        'census_v4_crosstab': {k: dict(v) for k, v in sorted(cross.items())},
        'byeye_crosscheck': {'note': 'census by-eye reads (byeye.json) informed this rule: a consistency check, not '
                                     'validation', 'rows': byeye,
                             'proposal_matches_seen': sum(b['match'] for b in byeye),
                             'unclear': sum(b['proposed'] == 'unclear' for b in byeye),
                             'seen_in_unclear_candidates': sum(b['seen_in_candidates'] for b in byeye),
                             'pairs': dict(Counter(f"{b['seen']} -> {b['proposed']}" for b in byeye).most_common())},
    }


# ----------------------------------------------------------------------------------------------- renders

def font(size):
    from PIL import ImageFont
    return ImageFont.load_default(size=size)


def panel(x, obj, image, crop_px=280):
    """raw crop | crop with outline (class colour), frame edges red; 4 text lines under it."""
    from PIL import Image, ImageDraw

    from . import vegetation_census as vc
    f = x['features']
    margin = max(20, int(0.35*max((f['mrr_length_m'] or 1)/f['mpp'], 40)))
    x0, y0, x1, y1 = vc.crop_box(obj, image.width, image.height, margin)
    scale = crop_px/max(x1-x0, y1-y0)
    size = (max(1, round((x1-x0)*scale)), max(1, round((y1-y0)*scale)))
    raw = image.crop((x0, y0, x1, y1)).resize(size, Image.NEAREST if scale >= 2 else Image.LANCZOS)
    drawn = raw.copy()
    d = ImageDraw.Draw(drawn)
    pts = [((px-x0+0.5)*scale, (py-y0+0.5)*scale) for px, py in obj['ring']]
    d.line(pts+[pts[0]], fill=CLASS_COLOURS[x['proposed']], width=2)
    red = (255, 40, 40)
    if x0 == 0:
        d.line([(1, 0), (1, size[1])], fill=red, width=3)
    if y0 == 0:
        d.line([(0, 1), (size[0], 1)], fill=red, width=3)
    if x1 == image.width:
        d.line([(size[0]-2, 0), (size[0]-2, size[1])], fill=red, width=3)
    if y1 == image.height:
        d.line([(0, size[1]-2), (size[0], size[1]-2)], fill=red, width=3)
    bar = 2/f['mpp']*scale
    d.rectangle([4, size[1]-9, 4+bar, size[1]-5], fill=(255, 255, 255))
    text_h = 62
    out = Image.new('RGB', (2*crop_px+6, crop_px+text_h), (18, 18, 18))
    out.paste(raw, (0, 0))
    out.paste(drawn, (crop_px+6, 0))
    t = ImageDraw.Draw(out)
    fnt = font(12)
    conf = '' if x['proposed'] == 'unclear' else f" ({x['confidence']} {x['score']:.2f})"
    cands = '/'.join(x['candidates'])
    words = (' | ' + '; '.join(x['reviewer_words'])) if x['reviewer_words'] else ''
    t.text((4, crop_px+2), f"#{x['number']} {x['proposed']}{conf}  [{cands}]  {x['kind']} {x['status']}{words}",
           fill=CLASS_COLOURS[x['proposed']], font=fnt)
    t.text((4, crop_px+16), f"{x['scene']} {x['id']}  flags: {','.join(x['flags']) or '-'}", fill=(220, 220, 220), font=fnt)
    t.text((4, crop_px+30), x['reason'][:96], fill=(255, 220, 120), font=fnt)
    t.text((4, crop_px+44), (f"A {f['area_m2']:.1f} m2  spread {f['eq_diam_m']:.1f}  w {f['mrr_width_m']:.1f} x "
                             f"{f['mrr_length_m']:.1f}  tex {f['texture_p50']:.3f}  Lrel {f['lab_L_rel']:+.0f}  "
                             f"lee {f['lee_contrast'] if f['lee_contrast'] is not None else 'n/a'}  "
                             f"{f['mpp']:.3f} m/px")[:100], fill=(200, 200, 200), font=fnt)
    return out


def pick(rows, per_class, rng):
    """Up to per_class objects, at most 2 per window; unclear stratified by reason (round robin)."""
    if not rows:
        return []
    groups = defaultdict(list)
    for x in rows:
        groups[x['unclear_reason'] or x['confidence']].append(x)
    for g in groups.values():
        rng.shuffle(g)
    keys = sorted(groups)
    chosen, seen, i = [], Counter(), 0
    while len(chosen) < per_class and any(groups.values()):
        g = groups[keys[i % len(keys)]]
        i += 1
        while g:
            x = g.pop()
            if seen[x['scene']] < 2:
                chosen.append(x)
                seen[x['scene']] += 1
                break
    return chosen


def render(args):
    from PIL import Image, ImageDraw

    from . import shadow_census as sc
    rows = [json.loads(line) for line in open(OUT_DIR/'proposals.jsonl')]
    rng = random.Random(args.seed)
    out = OUT_DIR/'renders'
    images, objects, manifest = {}, {}, []
    for c in PROPOSED:
        chosen = pick([x for x in rows if x['proposed'] == c], args.per_class, rng)
        if not chosen:
            heartbeat(f'render {c}: none proposed')
            continue
        (out/c).mkdir(parents=True, exist_ok=True)
        tiles = []
        for x in sorted(chosen, key=lambda x: x['number']):
            if x['scene'] not in images:
                scene_dir, meta, review = sc.load_scene(x['scene'])
                images[x['scene']] = Image.open(scene_dir/f"{meta['image_sha256']}.png").convert('RGB')
                objects[x['scene']] = {o['id']: o for o in review['objects']}
            p = panel(x, objects[x['scene']][x['id']], images[x['scene']])
            path = out/c/f"{x['number']:04d}-{x['scene']}-{x['id']}.png"
            p.save(path)
            tiles.append(p)
            manifest.append({'class': c, 'number': x['number'], 'scene': x['scene'], 'id': x['id'],
                             'confidence': x['confidence'], 'unclear_reason': x['unclear_reason'],
                             'path': str(path.relative_to(ROOT))})
        cols = 4
        tw, th = tiles[0].width, tiles[0].height
        sheet = Image.new('RGB', (cols*(tw+6), ((len(tiles)+cols-1)//cols)*(th+6)+24), (8, 8, 8))
        ImageDraw.Draw(sheet).text((6, 4), f'{c}: {len(tiles)} of {sum(x["proposed"] == c for x in rows)} '
                                   f'(raw | outline; red bar = window edge; 2 m bar)', fill=CLASS_COLOURS[c], font=font(14))
        for i, p in enumerate(tiles):
            sheet.paste(p, ((i % cols)*(tw+6), 24+(i//cols)*(th+6)))
        sheet.save(out/f'contact-{c}.png')
        heartbeat(f'render {c}: {len(tiles)} panels')
    (out/'manifest.json').write_text(json.dumps(manifest, indent=1))
    print(f'{len(manifest)} panels, {len({m["class"] for m in manifest})} contact sheets in {out.relative_to(ROOT)}')


# ----------------------------------------------------------------------------------------------- MARKS

def marks(args):
    rows = [json.loads(line) for line in open(OUT_DIR/'proposals.jsonl')]
    summary = json.loads((OUT_DIR/'summary.json').read_text())
    rendered = {}
    if (OUT_DIR/'renders/manifest.json').exists():
        rendered = {m['number']: m['class'] for m in json.loads((OUT_DIR/'renders/manifest.json').read_text())}
    meta = {}
    for scene_id in sorted({x['scene'] for x in rows}):
        m = json.loads((SCENES/scene_id/'latest.json').read_text())
        meta[scene_id] = (m.get('image_width'), rows and next(x['features']['mpp'] for x in rows if x['scene'] == scene_id))
    counts = summary['counts']
    lines = [
        '# MARKS: vegetation sub-class proposals v1 (2026-10-08)', '',
        'Your ask (chat, 8 Oct ~08:45): "can you classify vegetation further to: grass, hedge, tree etc".',
        'Each live tree / vegetation object on the 54 review windows has a proposed sub-class. These are proposals only:',
        'no kind was changed and nothing was written to the scenes. Rule, evidence and caveats: '
        '`prototype/summary.json`, code `scripts/ml/experiments/vegetation_classes.py`.', '',
        f"Counts: {', '.join(f'{c} {counts[c]}' for c in PROPOSED)} (of {sum(counts.values())}); unclear share "
        f"{summary['unclear_share']:.0%}. Height: no nDSM covers these windows, so shrub vs small tree and hedge vs "
        'grass strip are read from spread, shape, texture, cast shadow and the kind only. garden-bed is never proposed '
        '(texture alone mixes it up with striped verges); it appears as an option on unclear lines.', '',
        '## How to answer (in chat)', '',
        'Words: `grass`, `hedge`, `shrub`, `tree`, `group` (tree group), `bed` (garden bed), `not veg`, `ok`.',
        '- Per line: `12 hedge`, `40-48 ok`, `51 not veg`. `ok` means the proposed class is right.',
        '- Per class: `grass ok` (every grass line right), `grass ok except 12 40` (then give those lines a word, or leave '
        'them for me to read again).',
        '- Unclear lines: give the class word. Options are listed with the rule\'s lean first; `?` = cannot tell either.',
        '- Per unclear reason: `textured-ground grass` (every textured-ground line is grass), '
        '`textured-ground grass except 627 640`. Reasons: textured-ground (garden bed or patchy lawn), frame-cut-small '
        '(small crown cut by the window edge), frame-cut-strip (strip shape made by the cut), stature-tie / '
        'stature-conflict (ground cues against standing cues), flag:speck / not-green / shadow / on-roof.',
        'Lines you do not mark stay unmarked: nothing is applied by default.', '',
        'Line format: `N.` **proposed** (confidence) - kind status, area, spread - evidence; [options]; flags; your words '
        '(where you typed a label or note); `open` = the object in the editor (review layer). `sheet` = shown on that class\'s contact sheet '
        '(`prototype/renders/contact-<class>.png`, panel title `#N`).', '']
    for scene_id in sorted({x['scene'] for x in rows}):
        sub = [x for x in rows if x['scene'] == scene_id]
        width, mpp = meta[scene_id]
        c = Counter(x['proposed'] for x in sub)
        lines.append(f"## {scene_id} ({len(sub)} objects; {width} px, {mpp:.3f} m/px; "
                     + ', '.join(f'{k} {c[k]}' for k in PROPOSED if c[k]) + ')')
        lines.append(f"Window in the editor: {EDITOR_URL.format(scene_id, '').replace('&object=&', '&')}")
        lines.append('')
        for x in sub:
            f = x['features']
            conf = '' if x['proposed'] == 'unclear' else f" ({x['confidence']})"
            opts = '/'.join(x['candidates'])
            flags = [g for g in x['flags'] if g != 'frame-sliver']
            extra = []
            if flags:
                extra.append('flags ' + ','.join(flags))
            if x['reviewer_words']:
                verdict = {True: ' (agrees)', False: ' (DISAGREES with the proposal)'}.get(x['label_agrees'], '')
                extra.append('YOUR WORDS: ' + '; '.join(x['reviewer_words']) + verdict)
            if x['reviewer_kind_change']:
                extra.append('kind changed from the detector prompt')
            if x['number'] in rendered:
                extra.append(f"sheet {rendered[x['number']]}")
            short = x['reason'] if x['proposed'] != 'unclear' or x['reason'].startswith('flag') else \
                f"{x['unclear_reason']}: {x['reason']}"
            lines.append(f"{x['number']}. **{x['proposed']}**{conf} - {x['kind']} {x['status']}, {f['area_m2']:.1f} m2, "
                         f"spread {f['eq_diam_m']:.1f} m - {short[:150]}; [{opts}]"
                         + (f"; {'; '.join(extra)}" if extra else '') + f" [open]({EDITOR_URL.format(scene_id, x['id'])})")
        lines.append('')
    (OUT_DIR/'MARKS-vegetation-v1.md').write_text('\n'.join(lines))
    heartbeat(f'marks {len(rows)} lines')
    print(f"MARKS-vegetation-v1.md: {len(rows)} numbered lines, {len({x['scene'] for x in rows})} windows")


# ============================================================================================ design v2 (after the skeptic)
# By-eye skeptic on v1 (eye-veg.md, 130 objects): tree-group is an area rule and labels single large crowns (wrong 11 /
# right 9; 0 of 7 decidable on the random sample); shrub without height calls tall narrow crowns (cast shadow longer than
# the crown: 410, 1384) and crown fragments (31, 282) shrub; hedge recall is low (56, a clipped front hedge, sat in
# unclear); grass and tree hold up. Everything above this line is v1 and unchanged.

OUT_DIR_V2 = EXP_DIR/'prototype-v2'
FEATURES_V2 = OUT_DIR_V2/'features'
RUN_DIR_V2 = ROOT/'.run/session-2026-10-08-cuts-veg/veg-v2'
SKEPTIC_FILE = ROOT/'.run/session-2026-10-08-cuts-veg/eye-veg.md'
CROWN_SPLIT = ROOT/'data/reviews/eyes/experiments/tree-group-split-2026-10-03'
CROWN_SPLIT_SIDECAR = CROWN_SPLIT/'sidecar-v1'
TYPES_V2 = ('grass', 'hedge', 'shrub', 'tree', 'garden-bed')
PROPOSED_V2 = TYPES_V2 + ('unclear',)
TREE_OR_SHRUB = 'tree (or shrub)'
DISPLAY_V2 = ('grass', 'hedge', 'shrub', 'tree', TREE_OR_SHRUB, 'garden-bed', 'unclear')
GROUP_NOTE = 'possible group: see the crown-split card'
RULE_V2 = {
    **RULE,
    # 1. No tree-group class: v1's band (standing, not a strip, area >= tree_group_area_min) is tree. Crown peaks by the
    #    crown-split sidecar's peak finder (tree_crown_split.count_peaks, SIDECAR_V1: min crown radius 1.5 m, holes
    #    filled); >= group_peaks_min adds GROUP_NOTE. A group of trees is still trees: no class or confidence change.
    'group_peaks_min': 2,
    # 2. Shrub without height only on strong evidence; all five tests must hold, else tree shown as TREE_OR_SHRUB,
    #    uncertain, score x factor_tree_or_shrub (always low). With height, height decides as in v1.
    'shrub_spread_max': 3.0,                         # small: spread (equivalent diameter) under this
    'shrub_elongation_max': 1.5, 'shrub_solidity_min': 0.9,   # compact: a round / oval convex blob
    'neighbour_gap_m': 1.0, 'neighbour_area_ratio': 2.0,      # alone: no live tree-kind crown >= 2x its area within 1 m
    'shrub_texture_max': RULE['lawn_texture_max'],   # low texture contrast: under the census smooth-surface ceiling
    #                                                  no cast shadow: the census v4 cue (cast_shadow) does not fire
    'factor_tree_or_shrub': 0.5,
    # 3. Clipped hedge: a census strip, smooth surface, its own cast shadow under a reliable sun bearing, and a box shape
    #    (area / minimum rotated rectangle; an ellipse fills pi / 4 = 0.785, a rectangle 1.0) -> hedge, standing.
    'clipped_hedge_rect_min': 0.8,
}
V2_KEYS = [k for k in RULE_V2 if k not in RULE]
RULE_HISTORY_V2 = [
    'v2 (2026-10-08, after eye-veg.md): no tree-group class (v1 band -> tree, crown-peak note >= 2 peaks); shrub without '
    'height only when small < 3 m, compact, alone, low texture and no cast shadow, else tree (or shrub) uncertain low; '
    'clipped hedge = smooth census strip + own cast shadow (reliable sun) + rectangle fill >= 0.8. Thresholds are domain '
    'values or existing census values, set before the run; the skeptic-marked lines were read before the rule was '
    'written, so the skeptic check in summary.json is a consistency check, not validation.',
]
EXTRA_KEYS = ('neighbours_measured', 'larger_crown_gap_m', 'larger_crown_area_m2', 'larger_crown_id', 'crown_peaks',
              'crown_max_radius_m', 'crown_split')
# The skeptic's per-object marks (eye-veg.md "Per-object marks" and "Unclear lines"): v1 line -> what they saw. '?' =
# cannot tell, 'abstain' = unclear line where abstaining was justified. Used only for the consistency check.
SKEPTIC_SEEN = {
    **{n: 'grass' for n in (23, 82, 200, 257, 281, 338, 435, 619, 730, 745, 833, 1216, 1296, 1562, 8, 10, 1059, 1092,
                            1455, 1542, 1546, 1607, 1615, 1699)}, 1030: 'garden-bed', 675: '?',
    **{n: 'hedge' for n in (499, 1253, 1269, 1661, 1760)}, 169: 'tree',
    **{n: 'shrub' for n in (134, 444, 620, 774, 888, 1066, 1258, 30, 132, 136, 381, 382, 705, 1186)},
    **{n: 'tree' for n in (31, 282, 410, 1384)}, **{n: '?' for n in (209, 215, 652, 1065, 1259, 408)},
    **{n: 'tree' for n in (228, 529, 608, 820, 934, 951, 1039, 1046, 1147, 1262, 1373, 1495, 1584, 111, 456, 683, 720,
                           782, 837, 957, 1135, 1671)}, **{n: '?' for n in (658, 1587, 1763, 822)},
    **{n: 'tree-group' for n in (232, 461, 1158, 1160, 1343, 1728, 1781, 463, 1731)},
    **{n: 'tree' for n in (114, 662, 825, 530, 52, 962, 963, 1018, 1019, 1374, 1642)},
    **{n: '?' for n in (312, 900, 1315, 353)},
    **{n: 'abstain' for n in (154, 328, 467, 474, 618, 627, 665, 1281, 1346, 1449, 1465, 1594, 397, 469, 971, 1265,
                              1278, 1808)},
    54: 'tree', 1236: 'tree', 414: 'tree-group', 56: 'hedge', 464: 'grass', 537: 'shrub/hedge',
}


def heartbeat_v2(message):
    RUN_DIR_V2.mkdir(parents=True, exist_ok=True)
    with open(RUN_DIR_V2/'progress.txt', 'a') as handle:
        handle.write(f'{datetime.now().strftime("%H:%M:%S")} veg-v2: {message}\n')


def rectangularity(row):
    """Area over the minimum rotated rectangle's area: 1.0 for a box, pi / 4 for an ellipse; None without a rectangle."""
    box = (row.get('mrr_width_m') or 0.0)*(row.get('mrr_length_m') or 0.0)
    return round(row['area_m2']/box, 3) if box > 0 else None


def clipped_hedge(row, s, sliver):
    """(is a clipped hedge, rectangularity): v2 rule 3, read only without height (height decides otherwise)."""
    rect = rectangularity(row)
    casts, _ = cast_shadow(row)
    ok = (row.get('height_p90_m') is None and not sliver and s == 'smooth' and strip_shape(row) and casts
          and row.get('sun_reliable') is True and rect is not None and rect >= RULE_V2['clipped_hedge_rect_min'])
    return ok, rect


def shrub_tests(row):
    """v2 rule 2: [(test, passed, text)] for the five strong-shrub tests (no height)."""
    t = RULE_V2
    spread, tex = row['eq_diam_m'], row['texture_p50']
    casts, lee_shadow = cast_shadow(row)
    measured = bool(row.get('neighbours_measured'))
    gap, n_area = row.get('larger_crown_gap_m'), row.get('larger_crown_area_m2')
    adjacent = gap is not None and gap <= t['neighbour_gap_m']
    if not measured:
        alone_txt = 'neighbours not measured'
    elif gap is None:
        alone_txt = f"no crown >= {t['neighbour_area_ratio']:g}x its area in the window"
    else:
        alone_txt = f"{gap:.1f} m from a {n_area:.0f} m2 crown ({'<=' if adjacent else '>'} {t['neighbour_gap_m']:g} m)"
    compact = row['elongation'] <= t['shrub_elongation_max'] and row['solidity'] >= t['shrub_solidity_min']
    return [
        ('small', spread < t['shrub_spread_max'],
         f"spread {spread:.2f} m {'<' if spread < t['shrub_spread_max'] else '>='} {t['shrub_spread_max']:g} m"),
        ('compact', compact, f"elongation {row['elongation']:.2f}, solidity {row['solidity']:.2f}"),
        ('alone', measured and not adjacent, alone_txt),
        ('low texture', tex < t['shrub_texture_max'],
         f"texture {tex:.3f} {'<' if tex < t['shrub_texture_max'] else '>='} {t['shrub_texture_max']:g}"),
        ('no cast shadow', not casts, f"casts a shadow (lee {row.get('lee_contrast') or 0:+.2f}, lee shadow "
                                      f"{lee_shadow:+.2f})" if casts else 'no cast shadow'),
    ]


def group_hint(row):
    """(note or None, evidence text) for an object in v1's tree-group band (v2 rule 1)."""
    peaks = row.get('crown_peaks')
    if peaks is None:
        return None, 'crown peaks not counted'
    text = f"{peaks} crown peak{'' if peaks == 1 else 's'} (crown-split peak finder)"
    if peaks < RULE_V2['group_peaks_min']:
        return None, text
    side = row.get('crown_split') or {}
    where = {'run': f"crown-split sidecar-v1 line T{side.get('T')}: "
                    f"{'cut proposed' if side.get('cut') else 'no cut'}",
             'skip_frame_cut': 'the crown-split sidecar skipped it: frame-cut',
             'skip_confirmed': 'the crown-split sidecar skipped it: confirmed',
             'single_peak': 'the crown-split sidecar counted one peak'}.get(side.get('decision'),
                                                                          'not in the crown-split sidecar population')
    return f'{GROUP_NOTE} ({where})', text


def standing_form_v2(row, cut, sliver):
    """v1 standing_form without tree-group and with the strong-shrub gate. Returns {cls, factor, text, candidates,
    unclear_reason, uncertain, shrub_tests, group_note}."""
    t = RULE_V2
    height = row.get('height_p90_m')
    spread, area = row['eq_diam_m'], row['area_m2']
    frame = t['factor_frame_cut'] if cut else 1.0
    out = {'uncertain': False, 'shrub_tests': None, 'group_note': None}

    def form(cls, factor, text, cands, why=None, **extra):
        return {**out, 'cls': cls, 'factor': factor, 'text': text, 'candidates': cands, 'unclear_reason': why, **extra}

    if strip_shape(row):                 # v1's first branch, unchanged (hedge, or unclear when the frame makes the strip)
        return form(*standing_form(row, cut, sliver))
    note, group_txt = group_hint(row) if area >= t['tree_group_area_min'] else (None, None)
    tail = f'; {group_txt}' if group_txt else ''
    if height is not None:
        if height < t['tall_height_min']:
            return form('shrub', frame, f'low ({height:.1f} m), spread {spread:.1f} m', ['shrub', 'hedge'])
        return form('tree', frame, f'tall ({height:.1f} m), spread {spread:.1f} m{tail}',
                    ['tree'] if group_txt else ['tree', 'shrub'], group_note=note)
    band = in_band(spread, t['spread_band'])
    if spread >= t['tree_eq_diam_min']:
        return form('tree', (t['factor_band'] if band else 1.0)*frame, f'spread {spread:.1f} m (no height){tail}',
                    ['tree'] if group_txt else ['tree', 'shrub'], group_note=note)
    if cut:
        return form('unclear', 1.0, f'spread {spread:.1f} m is a lower bound: the frame cuts it', ['shrub', 'tree'],
                    'frame-cut-small')
    tests = shrub_tests(row)
    failed = [text for _, ok, text in tests if not ok]
    if not failed:
        return form('shrub', t['factor_band'] if band else t['factor_small_shrub'],
                    'strong shrub evidence: ' + '; '.join(text for *_, text in tests), ['shrub', 'tree'],
                    shrub_tests=tests)
    return form('tree', t['factor_tree_or_shrub'], f"{TREE_OR_SHRUB}: spread {spread:.1f} m (no height), not strong "
                f"shrub evidence: {'; '.join(failed)}", ['tree', 'shrub'], uncertain=True, shrub_tests=tests)


def classify_v2(row):
    """v2 proposal for one feature row (+ the measure-v2 extras): v1's keys plus display (TREE_OR_SHRUB for an uncertain
    tree), uncertain, notes, shrub_tests, rule. Pure: the row is not changed."""
    cut, sliver = frame_state(row)
    flags = blocking_flags(row)
    frame_flags = (['frame-cut'] if cut else [])+(['frame-sliver'] if sliver else [])
    evidence = [f'{name}: {text}' for name, text in flags]
    if cut:
        evidence.append(f"frame: {row.get('frame_edge_m') or 0:.1f} m on the window edge"
                        + (' (clipped_by_frame)' if row.get('clipped_by_frame') else ''))
    if row.get('height_p90_m') is None:
        evidence.append('height: none (no nDSM covers the window)')
    base = {'flags': [f for f, _ in flags]+frame_flags, 'evidence': evidence, 'uncertain': False, 'notes': [],
            'shrub_tests': None, 'rule': 'v2'}
    unclear = {'proposed': 'unclear', 'display': 'unclear', 'confidence': 'none', 'score': 0.0}
    if flags:
        name = flags[0][0]
        kind = row['kind'] if row['kind'] in ('tree', 'vegetation') else 'tree'
        return {**base, **unclear, 'candidates': FLAG_CANDIDATES[name][kind], 'unclear_reason': f'flag:{name}',
                'stature': None, 'reason': f'flag {name} ({flags[0][1]})'}
    s, votes = stature_votes(row)
    evidence.extend(f'{cue} -> {side} ({w}): {text}' for cue, text, side, w in votes)
    ground = sum(w for *_, side, w in votes if side == 'ground')
    standing = sum(w for *_, side, w in votes if side == 'standing')
    height = row.get('height_p90_m')
    clipped, rect = clipped_hedge(row, s, sliver)
    if clipped:
        evidence.append(f'clipped hedge: smooth strip filling {rect:.2f} of its rectangle, with its own cast shadow '
                        '(reliable sun bearing): the shadow outranks the smooth top')
        stature = 'standing'
    elif height is None:
        firm = {side for *_, side, w in votes if w >= WEIGHT['medium']}
        conflict = firm == {'ground', 'standing'} and abs(ground-standing) < RULE['conflict_margin']
        if conflict or ground == standing:
            reason = 'stature-tie' if ground == standing else 'stature-conflict'
            g_cls, *_ = ground_form(row, s)
            g_cands = ['grass', 'garden-bed'] if g_cls == 'unclear' else [g_cls]
            st = standing_form_v2(row, cut, sliver)
            s_cands = st['candidates'] if st['cls'] == 'unclear' or st['uncertain'] else [st['cls']]
            standing_first = standing > ground or (standing == ground and row['kind'] == 'tree')
            cands = (s_cands+g_cands) if standing_first else (g_cands+s_cands)
            g_txt = ', '.join(c for c, _, side, _w in votes if side == 'ground')
            s_txt = ', '.join(c for c, _, side, _w in votes if side == 'standing')
            return {**base, **unclear, 'candidates': list(dict.fromkeys(cands)), 'unclear_reason': reason,
                    'stature': None, 'reason': f'ground ({g_txt}) vs standing ({s_txt})'}
        stature = 'ground' if ground > standing else 'standing'
    else:
        stature = 'ground' if height < RULE['ground_height_max'] else 'standing'
    agree, oppose = (ground, standing) if stature == 'ground' else (standing, ground)
    st = None
    if stature == 'ground':
        cls, factor, form_txt, cands, why = ground_form(row, s if not (s == 'dark' and height is not None) else 'mixed')
    else:
        st = standing_form_v2(row, cut, sliver)
        cls, factor, form_txt, cands, why = st['cls'], st['factor'], st['text'], st['candidates'], st['unclear_reason']
        if clipped and cls == 'hedge':
            cands = ['hedge', 'grass']
            form_txt = f'clipped hedge: {form_txt}, smooth top, rectangle fill {rect:.2f}, own cast shadow'
    evidence.append(f'form: {form_txt}')
    if cls == 'unclear':
        return {**base, **unclear, 'candidates': cands, 'unclear_reason': why, 'stature': stature,
                'reason': f'{stature}; {form_txt}'}
    score = round(agree/(agree+oppose+1)*factor, 3)
    uncertain = bool(st and st['uncertain'])
    cue_txt = '; '.join(text for _, text, side, _w in votes if side == stature)
    return {**base, 'proposed': cls, 'display': TREE_OR_SHRUB if uncertain else cls, 'uncertain': uncertain,
            'confidence': confidence_label(score), 'score': score, 'candidates': cands, 'unclear_reason': None,
            'stature': stature, 'notes': [st['group_note']] if st and st['group_note'] else [],
            'shrub_tests': [list(x) for x in st['shrub_tests']] if st and st['shrub_tests'] else None,
            'reason': f'{stature} ({cue_txt}); {form_txt}' + (f'; against: {oppose}' if oppose else '')}


def twin_groups(items, same_m=2.0):
    """items [(key, scene, lon, lat, area_m2)] -> {key: sorted keys of the same physical object in other windows}.
    vegetation_census.unique_estimate's rule: centroids within same_m metres, area ratio <= 2, different windows."""
    import numpy as np
    items = [x for x in items if x[2] is not None and x[3] is not None]
    if not items:
        return {}
    pts = np.asarray([(x[2], x[3]) for x in items], dtype=float)
    lat_m = 111_320.0
    lon_m = lat_m*math.cos(math.radians(float(pts[:, 1].mean())))
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i, (_, scene, *_, area) in enumerate(items):
        d = np.hypot((pts[:, 0]-pts[i, 0])*lon_m, (pts[:, 1]-pts[i, 1])*lat_m)
        for j in np.flatnonzero(d <= same_m):
            other = items[j]
            if j > i and other[1] != scene and max(area, other[4]) <= 2*min(area, other[4]):
                parent[find(i)] = find(int(j))
    groups = defaultdict(list)
    for i in range(len(items)):
        groups[find(i)].append(items[i][0])
    return {k: sorted(x for x in members if x != k) for members in groups.values() for k in members}


def crown_split_index():
    """(scene, id) -> the crown-split sidecar-v1's population decision, peaks, MARKS line T and cut (read-only)."""
    out = {}
    population = CROWN_SPLIT_SIDECAR/'population.json'
    if population.exists():
        for p in json.loads(population.read_text()):
            out[(p['scene'], p['object_id'])] = {'decision': p.get('decision'), 'peaks': p.get('peaks'), 'T': None,
                                                 'cut': None}
    records = CROWN_SPLIT_SIDECAR/'records.jsonl'
    if records.exists():
        for r in map(json.loads, open(records)):
            entry = out.setdefault((r['scene'], r['object_id']), {'decision': r.get('population_decision'),
                                                                  'peaks': None})
            entry.update(T=r.get('T'), cut=bool((r.get('record') or {}).get('cut')))
    return out


def measure_v2(args):
    """Read-only extras for v2, keyed (scene, id) to v1's feature rows: the nearest live tree-kind crown with >= 2x the
    area (gap in m, its area), crown peaks for v1's tree-group band (tree_crown_split.count_peaks), the crown-split
    sidecar's decision for that object, and the centroid lon / lat (overlapping windows)."""
    from . import shadow_census as sc
    from . import tree_crown_split as tcs
    from . import vegetation_census as vc
    FEATURES_V2.mkdir(parents=True, exist_ok=True)
    heartbeat_v2('measure-v2 start (read-only over data/scenes review.json; v1 feature rows frozen)')
    rows = [x for x in load_features() if not x.get('empty')]
    side = crown_split_index()
    by_scene = defaultdict(list)
    for x in rows:
        by_scene[x['scene']].append(x)
    out, review_sha, drift = [], {}, []
    ratio = RULE_V2['neighbour_area_ratio']
    for index, (scene_id, xs) in enumerate(sorted(by_scene.items()), 1):
        scene_dir, meta, review = sc.load_scene(scene_id)
        review_sha[scene_id] = sha(scene_dir/'review.json')
        mpp = sc.pixel_geometry(meta)[2]
        live = {o['id']: o for o in vc.live(review['objects']) if o['kind'] in vc.VEG_KINDS}
        polys = {k: sc.ring_polygon(o) for k, o in live.items()}
        crowns = [(k, p) for k, p in polys.items() if p is not None and live[k]['kind'] == 'tree']
        for x in xs:
            obj, poly = live.get(x['id']), polys.get(x['id'])
            rec = {'scene': scene_id, 'id': x['id'], 'neighbours_measured': poly is not None, 'larger_crown_gap_m': None,
                   'larger_crown_area_m2': None, 'larger_crown_id': None, 'crown_peaks': None, 'crown_max_radius_m': None,
                   'crown_split': None, 'lon': None, 'lat': None}
            if poly is None:
                out.append(rec)
                continue
            area = poly.area*mpp*mpp
            if abs(area-x['area_m2']) > max(0.05, 0.01*x['area_m2']):
                drift.append({'scene': scene_id, 'id': x['id'], 'v1_area_m2': x['area_m2'], 'now_m2': round(area, 2)})
            best = None
            for k, other in crowns:
                if k != x['id'] and other.area >= ratio*poly.area:
                    gap = poly.distance(other)*mpp
                    if best is None or gap < best[0]:
                        best = (gap, other.area*mpp*mpp, k)
            if best:
                rec.update(larger_crown_gap_m=round(best[0], 3), larger_crown_area_m2=round(best[1], 2),
                           larger_crown_id=best[2])
            lon, lat = vc.lonlat(meta, *poly.centroid.coords[0])
            rec.update(lon=round(lon, 8), lat=round(lat, 8))
            if x['area_m2'] >= RULE['tree_group_area_min']:
                peaks, radius = tcs.count_peaks(obj['ring'], obj.get('holes'), mpp)
                rec.update(crown_peaks=peaks, crown_max_radius_m=radius, crown_split=side.get((scene_id, x['id'])))
            out.append(rec)
        if index % 10 == 0:
            heartbeat_v2(f'measure-v2 {index}/{len(by_scene)} windows')
    with open(FEATURES_V2/'extras.jsonl', 'w') as handle:
        for rec in out:
            handle.write(json.dumps(rec, sort_keys=True)+'\n')
    meta_out = {'computed_at': now(), 'code_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'v1_features_sha256': sha(FEATURES_DIR/'rows.jsonl'), 'rows': len(out),
                'review_sha256': review_sha, 'area_drift_vs_v1_rows': drift,
                'peak_finder': {'function': 'tree_crown_split.count_peaks', 'design': tcs.SIDECAR_V1['version'],
                                'min_crown_radius_m': tcs.SIDECAR_V1['min_crown_radius_m'],
                                'fill_holes': tcs.SIDECAR_V1['fill_holes'],
                                'code_sha256': sha(ROOT/'scripts/ml/experiments/tree_crown_split.py')},
                'crown_split_population_sha256': sha(CROWN_SPLIT_SIDECAR/'population.json'),
                'neighbours': f'live tree-kind objects with >= {ratio:g}x the area; polygon gap in m (frame pixels x mpp)'}
    (FEATURES_V2/'extras-meta.json').write_text(json.dumps(meta_out, indent=1))
    heartbeat_v2(f'measure-v2 done: {len(out)} rows, area drift {len(drift)}')
    print(f'extras.jsonl: {len(out)} rows; area drift vs v1 rows {len(drift)}')


V1_FIELDS = ('proposed', 'confidence', 'score', 'candidates', 'unclear_reason', 'reason', 'evidence', 'flags', 'stature')


def classify_v2_run(args):
    rows = [x for x in load_features() if not x.get('empty')]
    extras = {(e['scene'], e['id']): e for e in map(json.loads, open(FEATURES_V2/'extras.jsonl'))}
    v1 = {(x['scene'], x['id']): x for x in map(json.loads, open(OUT_DIR/'proposals.jsonl'))}
    by_key ={(r['scene'], r['id']): r for r in rows}
    twins = twin_groups([(k, k[0], extras.get(k, {}).get('lon'), extras.get(k, {}).get('lat'), r['area_m2'])
                         for k, r in by_key.items()])
    numbers = {k: v1[k]['number'] for k in v1}
    out, identical = [], 0
    for row in rows:
        key = (row['scene'], row['id'])
        row['height_p50_m'] = row['height_p90_m'] = None     # no window covered (v1 height_coverage)
        old = v1[key]
        p1 = classify(row)
        identical += all(p1[k] == old[k] for k in V1_FIELDS)
        e = extras.get(key, {})
        p = classify_v2({**row, **{k: e.get(k) for k in EXTRA_KEYS}})
        out.append({'number': old['number'], 'scene': row['scene'], 'id': row['id'], 'kind': row['kind'],
                    'status': row['status'], 'label': old.get('label'), 'reviewer_words': old['reviewer_words'],
                    'reviewer_kind_change': old['reviewer_kind_change'], 'label_class': old['label_class'],
                    'label_agrees': label_agrees(old['label_class'], p),
                    'v1': {k: old[k] for k in ('proposed', 'confidence', 'score', 'candidates', 'unclear_reason')},
                    'changed': old['proposed'] != p['proposed'],
                    'twins': sorted(numbers[k] for k in twins.get(key, []) if k in numbers), **p,
                    'features': {**{k: row.get(k) for k in FEATURE_KEYS}, 'rectangularity': rectangularity(row),
                                 **{k: e.get(k) for k in EXTRA_KEYS}}})
    out.sort(key=lambda x: x['number'])
    OUT_DIR_V2.mkdir(parents=True, exist_ok=True)
    with open(OUT_DIR_V2/'proposals.jsonl', 'w') as handle:
        for x in out:
            handle.write(json.dumps(x, sort_keys=True)+'\n')
    summary = summarise_v2(out, identical)
    (OUT_DIR_V2/'summary.json').write_text(json.dumps(summary, indent=1))
    (OUT_DIR_V2/'tables.md').write_text(tables_v2(summary))
    heartbeat_v2(f"classify-v2 {len(out)} objects; changed {summary['changed']['lines']}; v1 reproduced "
                 f"{identical}/{len(out)}")
    print(tables_v2(summary).split('\n\n')[0])
    print(f"changed {summary['changed']['lines']}; v1 reproduced {identical}/{len(out)}")


def display_class(x):
    return x.get('display') or x['proposed']


def summarise_v2(out, identical):
    from . import vegetation_census as vc
    counts_v1 = Counter(x['v1']['proposed'] for x in out)
    counts_v2 = Counter(x['proposed'] for x in out)
    display = Counter(display_class(x) for x in out)
    trans = defaultdict(Counter)
    for x in out:
        trans[x['v1']['proposed']][display_class(x)] += 1
    changed = [x for x in out if x['changed']]
    band = [x for x in out if x['v1']['proposed'] == 'tree-group']
    v1_shrubs = [x for x in out if x['v1']['proposed'] == 'shrub']
    tests = [x for x in v1_shrubs if x['shrub_tests']]
    test_names = [t[0] for t in tests[0]['shrub_tests']] if tests else []
    unclear = [x for x in out if x['proposed'] == 'unclear']
    # Skeptic consistency check (lines read before the rule was written: not validation).
    skeptic = []
    for x in out:
        seen = SKEPTIC_SEEN.get(x['number'])
        if seen:
            skeptic.append({'number': x['number'], 'v1': x['v1']['proposed'], 'seen': seen, 'v2': display_class(x),
                            'group_note': bool(x['notes'])})
    pairs = Counter(f"{s['v1']} | seen {s['seen']} -> v2 {s['v2']}" + (' +note' if s['group_note'] else '')
                    for s in skeptic)
    # Hedge loosening: the v1 bucket the skeptic's missed hedge sat in (smooth strip + cast shadow -> stature conflict).
    byeye = {}
    if (CENSUS_DIR/'byeye.json').exists():
        byeye = {(v['scene'], v['id']): v['seen_class'] for v in json.loads((CENSUS_DIR/'byeye.json').read_text())['rows']}
    hedge_bucket = []
    for x in out:
        f = x['features']
        frow = {**f, 'kind': x['kind'], 'status': x['status']}
        if strip_shape(frow) and cast_shadow(frow)[0] and surface(frow) in ('smooth', 'smooth-shaded'):
            hedge_bucket.append({'number': x['number'], 'scene': x['scene'], 'id': x['id'], 'kind': x['kind'],
                                 'surface': surface(frow), 'sun_reliable': f['sun_reliable'],
                                 'rectangularity': f['rectangularity'], 'solidity': f['solidity'],
                                 'L_rel': f['lab_L_rel'], 'v1': x['v1']['proposed'], 'v2': display_class(x),
                                 'skeptic': SKEPTIC_SEEN.get(x['number']), 'census_byeye': byeye.get((x['scene'], x['id'])),
                                 'twins': x['twins']})
    # Physical objects: overlapping windows hold the same object more than once.
    seen_groups, physical = set(), Counter()
    by_number = {x['number']: x for x in out}
    for x in out:
        group = tuple(sorted([x['number'], *x['twins']]))
        if group not in seen_groups:
            seen_groups.add(group)
            physical[Counter(display_class(by_number[n]) for n in group).most_common(1)[0][0]] += 1
    return {
        'version': 'vegetation-classes-v2', 'computed_at': now(),
        'code_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'inputs': {'v1_features': str((FEATURES_DIR/'rows.jsonl').relative_to(ROOT)),
                   'v1_features_sha256': sha(FEATURES_DIR/'rows.jsonl'),
                   'v1_proposals_sha256': sha(OUT_DIR/'proposals.jsonl'),
                   'extras_sha256': sha(FEATURES_V2/'extras.jsonl'),
                   'skeptic': str(SKEPTIC_FILE.relative_to(ROOT)),
                   'skeptic_sha256': sha(SKEPTIC_FILE) if SKEPTIC_FILE.exists() else None,
                   'census_rule_matches': all(vc.RULE.get(k) == RULE[k] for k in CENSUS_SHARED)},
        'rule_v2_new': {k: RULE_V2[k] for k in V2_KEYS}, 'rule_history_v2': RULE_HISTORY_V2,
        'v1_reproduced': {'rows': len(out), 'identical': identical, 'fields': list(V1_FIELDS),
                          'note': 'v1 classify re-run on the same frozen rows vs prototype/proposals.jsonl'},
        'counts_v1': {c: counts_v1.get(c, 0) for c in PROPOSED},
        'counts_v2': {c: counts_v2.get(c, 0) for c in PROPOSED_V2},
        'counts_v2_display': {c: display.get(c, 0) for c in DISPLAY_V2},
        'transitions': {k: dict(v) for k, v in sorted(trans.items())},
        'changed': {'lines': len(changed),
                    'by_transition': dict(Counter(f"{x['v1']['proposed']} -> {display_class(x)}"
                                                  for x in changed).most_common()),
                    'numbers': [x['number'] for x in changed],
                    'candidates_only': sum(not x['changed'] and x['v1']['candidates'] != x['candidates'] for x in out)},
        'tree_group_band': {
            'objects': len(band), 'peaks': dict(sorted(Counter(str(x['features']['crown_peaks']) for x in band).items())),
            'group_note': sum(bool(x['notes']) for x in band),
            'group_note_numbers': [x['number'] for x in band if x['notes']],
            'crown_split_decision': dict(Counter((x['features']['crown_split'] or {}).get('decision') or 'none'
                                                 for x in band)),
            'confidence_v2': dict(Counter(x['confidence'] for x in band))},
        'shrub_gate': {
            'v1_shrubs': len(v1_shrubs), 'stay_shrub': sum(x['proposed'] == 'shrub' for x in v1_shrubs),
            'tree_or_shrub': sum(bool(x['uncertain']) for x in v1_shrubs),
            'pass_per_test': {name: sum(x['shrub_tests'][i][1] for x in tests) for i, name in enumerate(test_names)},
            'failed_tests_count': dict(Counter(sum(not t[1] for t in x['shrub_tests']) for x in tests)),
            'only_failed_test': dict(Counter(next(t[0] for t in x['shrub_tests'] if not t[1]) for x in tests
                                             if sum(not t[1] for t in x['shrub_tests']) == 1))},
        'clipped_hedge': {'numbers': [x['number'] for x in out if any(e.startswith('clipped hedge') for e in x['evidence'])
                                      and x['proposed'] == 'hedge'],
                          'bucket': hedge_bucket,
                          'note': 'v1 bucket of the skeptic-missed hedge 56: census strips with a smooth surface and the '
                                  'census v4 cast-shadow cue. Rectangle fill separates the census by-eye hedges (56 / 116 / '
                                  '1595, one hedge in three windows) from 1449 (census by-eye: lawn); smooth-shaded 1278 '
                                  '(census by-eye: lawn) stays out on the existing ring test. The 0.8 cut is the box-vs-'
                                  'ellipse boundary, set before the run, but every line here was read by the skeptic or the '
                                  'census by-eye pass: one physical hedge and two lawns, not validation.'},
        'unclear_by_reason': dict(Counter(x['unclear_reason'] for x in unclear).most_common()),
        'unclear_share': round(len(unclear)/max(len(out), 1), 3),
        'confidence': dict(Counter(x['confidence'] for x in out)),
        'skeptic_check': {'note': 'lines the skeptic marked in eye-veg.md; read before the v2 rule was written, so a '
                                  'consistency check only', 'lines': len(skeptic), 'pairs': dict(pairs.most_common()),
                          'rows': skeptic},
        'physical_objects': {'method': 'centroid within 2 m, area ratio <= 2, different windows; majority v2 display',
                             'groups': len(seen_groups), 'by_class': dict(physical),
                             'rows_with_twins': sum(bool(x['twins']) for x in out)},
        'reviewer_typed': {'comparable': sum(x['label_agrees'] is not None for x in out),
                           'agree': sum(bool(x['label_agrees']) for x in out)},
    }


def tables_v2(summary):
    lines = ['| class | v1 | v2 |', '|---|---|---|']
    for c in ('grass', 'hedge', 'shrub', 'tree', 'tree-group', 'garden-bed', 'unclear'):
        v2 = summary['counts_v2'].get(c, 0) if c != 'tree-group' else 0
        extra = (f" (of which {TREE_OR_SHRUB} {summary['counts_v2_display'][TREE_OR_SHRUB]})" if c == 'tree' else '')
        lines.append(f"| {c} | {summary['counts_v1'].get(c, 0)} | {v2}{extra} |")
    lines += ['', '| v1 \\ v2 | ' + ' | '.join(DISPLAY_V2) + ' |', '|---|' + '---|'*len(DISPLAY_V2)]
    for c in PROPOSED:
        row = summary['transitions'].get(c, {})
        lines.append(f'| {c} | ' + ' | '.join(str(row.get(d, '')) for d in DISPLAY_V2) + ' |')
    lines += ['', '| unclear reason (v2) | objects |', '|---|---|']
    lines += [f'| {k} | {v} |' for k, v in summary['unclear_by_reason'].items()]
    return '\n'.join(lines)+'\n'


RENDER_SETS_V2 = (('shrub', 'v2 shrub (strong evidence)'), ('tree-or-shrub', f'v2 {TREE_OR_SHRUB} (was shrub)'),
                  ('hedge', 'v2 hedge (new lines first)'), ('group-note', 'was tree-group, v2 tree with the group note'),
                  ('one-crown', 'was tree-group, v2 tree, no group note'))


def render_v2(args):
    from PIL import Image, ImageDraw

    from . import shadow_census as sc
    rows = [json.loads(line) for line in open(OUT_DIR_V2/'proposals.jsonl')]
    rng = random.Random(args.seed)
    sets = {
        'shrub': [x for x in rows if x['proposed'] == 'shrub'],
        'tree-or-shrub': [x for x in rows if x['uncertain']],
        'hedge': sorted([x for x in rows if x['proposed'] == 'hedge'], key=lambda x: (not x['changed'], x['number'])),
        'group-note': [x for x in rows if x['v1']['proposed'] == 'tree-group' and x['notes']],
        'one-crown': [x for x in rows if x['v1']['proposed'] == 'tree-group' and not x['notes']],
    }
    out = OUT_DIR_V2/'renders'
    images, objects, manifest = {}, {}, []
    for name, title in RENDER_SETS_V2:
        pool = sets[name]
        chosen = pool if len(pool) <= args.per_set or name == 'hedge' else rng.sample(pool, args.per_set)
        if not chosen:
            continue
        (out/name).mkdir(parents=True, exist_ok=True)
        tiles = []
        for x in (chosen if name == 'hedge' else sorted(chosen, key=lambda x: x['number'])):
            if x['scene'] not in images:
                scene_dir, meta, review = sc.load_scene(x['scene'])
                images[x['scene']] = Image.open(scene_dir/f"{meta['image_sha256']}.png").convert('RGB')
                objects[x['scene']] = {o['id']: o for o in review['objects']}
            shown = {**x, 'reason': (f"[{display_class(x)}; v1 {x['v1']['proposed']}] " + '; '.join(x['notes'])
                                     + ('; ' if x['notes'] else '') + x['reason'])}
            p = panel(shown, objects[x['scene']][x['id']], images[x['scene']])
            path = out/name/f"{x['number']:04d}-{x['scene']}-{x['id']}.png"
            p.save(path)
            tiles.append(p)
            manifest.append({'set': name, 'number': x['number'], 'scene': x['scene'], 'id': x['id'],
                             'v1': x['v1']['proposed'], 'v2': display_class(x), 'path': str(path.relative_to(ROOT))})
        cols = 4
        tw, th = tiles[0].width, tiles[0].height
        sheet = Image.new('RGB', (cols*(tw+6), ((len(tiles)+cols-1)//cols)*(th+6)+24), (8, 8, 8))
        ImageDraw.Draw(sheet).text((6, 4), f'{title}: {len(tiles)} of {len(pool)} (raw | outline; red bar = window edge; '
                                   '2 m bar)', fill=(255, 255, 255), font=font(14))
        for i, p in enumerate(tiles):
            sheet.paste(p, ((i % cols)*(tw+6), 24+(i//cols)*(th+6)))
        sheet.save(out/f'contact-{name}.png')
        heartbeat_v2(f'render-v2 {name}: {len(tiles)} of {len(pool)}')
    (out/'manifest.json').write_text(json.dumps(manifest, indent=1))
    print(f'{len(manifest)} panels in {out.relative_to(ROOT)}')


def marks_line_v2(x, rendered):
    f = x['features']
    conf = '' if x['proposed'] == 'unclear' else f" ({x['confidence']}{', uncertain' if x['uncertain'] else ''})"
    extra = []
    flags = [g for g in x['flags'] if g != 'frame-sliver']
    if flags:
        extra.append('flags ' + ','.join(flags))
    extra += x['notes']
    if x['changed']:
        extra.append(f"v1 {x['v1']['proposed']}")
    if x['twins']:
        extra.append('same object as ' + ', '.join(map(str, x['twins'])))
    if x['reviewer_words']:
        verdict = {True: ' (agrees)', False: ' (DISAGREES with the proposal)'}.get(x['label_agrees'], '')
        extra.append('YOUR WORDS: ' + '; '.join(x['reviewer_words']) + verdict)
    if x['reviewer_kind_change']:
        extra.append('kind changed from the detector prompt')
    if x['number'] in rendered:
        extra.append(f"sheet {rendered[x['number']]}")
    short = x['reason'] if x['proposed'] != 'unclear' or x['reason'].startswith('flag') else \
        f"{x['unclear_reason']}: {x['reason']}"
    return (f"{x['number']}. **{display_class(x)}**{conf} - {x['kind']} {x['status']}, {f['area_m2']:.1f} m2, spread "
            f"{f['eq_diam_m']:.1f} m - {short[:260]}; [{'/'.join(x['candidates'])}]"
            + (f"; {'; '.join(extra)}" if extra else '') + f" [open]({EDITOR_URL.format(x['scene'], x['id'])})")


def change_why(x):
    """Short reason for a line v2 changed: failed shrub tests, the clipped-hedge form, or the crown-peak count."""
    if x['uncertain']:
        return 'not strong shrub evidence: ' + '; '.join(text for _, ok, text in x['shrub_tests'] if not ok)
    form_txt = next((e[len('form: '):] for e in x['evidence'] if e.startswith('form: ')), x['reason'])
    if x['v1']['proposed'] == 'tree-group':
        return next((part for part in form_txt.split('; ') if 'crown peak' in part), form_txt)
    return form_txt


def marks_v2(args):
    rows = [json.loads(line) for line in open(OUT_DIR_V2/'proposals.jsonl')]
    s = json.loads((OUT_DIR_V2/'summary.json').read_text())
    rendered = {}
    if (OUT_DIR_V2/'renders/manifest.json').exists():
        rendered = {m['number']: m['set'] for m in json.loads((OUT_DIR_V2/'renders/manifest.json').read_text())}
    c1, c2, d2 = s['counts_v1'], s['counts_v2'], s['counts_v2_display']
    tg, sg, ch = s['tree_group_band'], s['shrub_gate'], s['clipped_hedge']
    changed = [x for x in rows if x['changed']]
    t = RULE_V2
    lines = [
        '# MARKS: vegetation sub-class proposals v2 (2026-10-08)', '',
        'Your ask (chat, 8 Oct ~08:45): "can you classify vegetation further to: grass, hedge, tree etc". v2 replaces '
        'v1 (`prototype/MARKS-vegetation-v1.md`) after a by-eye check of v1 on 130 objects: grass and tree held up; '
        'tree group was wrong more often than right (single large crowns); shrub called tall narrow crowns and crown '
        'fragments shrub; one clear clipped hedge sat in unclear. These are proposals only: no kind was changed and '
        'nothing was written to the scenes. Line numbers are v1\'s (same objects), so line N here is line N in v1.', '',
        'What changed (three rules; everything else is v1, evidence in `prototype-v2/summary.json`):',
        f"1. No tree-group class. The {tg['objects']} v1 tree-group lines are **tree**. {tg['group_note']} of them have "
        'two or more crown peaks (the crown-split sidecar\'s peak finder) and carry "possible group: see the crown-split '
        'card" (`data/reviews/eyes/experiments/tree-group-split-2026-10-03/DECISION-CARD.md`, sheet '
        '`sidecar-v1/MARKS-trees-v1.md`). Splitting stays with that card and your split tool.',
        f"2. Shrub only on strong evidence (no height data here): spread under {t['shrub_spread_max']:g} m, compact "
        f"(elongation <= {t['shrub_elongation_max']:g}, solidity >= {t['shrub_solidity_min']:g}), no larger crown within "
        f"{t['neighbour_gap_m']:g} m, low texture contrast (texture < {t['shrub_texture_max']:g}) and no cast shadow. "
        f"{sg['stay_shrub']} of v1's {sg['v1_shrubs']} shrubs stay shrub; {sg['tree_or_shrub']} become "
        f"**{TREE_OR_SHRUB}**: low confidence, the rule leans tree and offers shrub.",
        f"3. Clipped hedge: a strip with a smooth top, its own cast shadow and a box outline is a hedge (low). "
        f"{len(ch['numbers'])} lines move from unclear to hedge: {', '.join(map(str, ch['numbers']))} (one hedge seen in "
        'overlapping windows).', '',
        'Counts v1 -> v2: ' + ', '.join(f"{c} {c1.get(c, 0)} -> {0 if c == 'tree-group' else c2.get(c, 0)}"
                                        for c in PROPOSED)
        + f" (of {len(rows)}); tree includes {d2[TREE_OR_SHRUB]} {TREE_OR_SHRUB}. Unclear share "
        f"{s['unclear_share']:.0%}. Overlapping windows show some objects more than once (`same object as`); about "
        f"{s['physical_objects']['groups']} physical objects.", '',
        '## How to answer (in chat)', '',
        'Class words: `grass`, `hedge`, `shrub`, `tree`, `bed` (garden bed), `not veg`, `group` (several crowns: the line '
        'stays tree and goes to the crown-split card), `?` (cannot tell).',
        '- Per class: `grass all right` (every grass line is right), `tree all right except 12 40` (then give those '
        f'lines a word, or leave them for me to read again). Classes: grass, hedge, shrub, tree, `{TREE_OR_SHRUB}` '
        f'(say `tree-or-shrub all tree` or `tree-or-shrub all shrub`), unclear (per line or per reason as in v1: '
        '`textured-ground grass except 627`).',
        '- Per line: `12 right`, `12 hedge`, `40-48 right`, `51 not veg`, `60 group`.',
        f'- Changed lines only: `changed all right` = every line under "Changed from v1" ({len(changed)}) is right as v2 '
        'proposes it.',
        'Lines you do not mark stay unmarked: nothing is applied by default.', '',
        'Line format: `N.` **proposed** (confidence) - kind status, area, spread - evidence; [options, lean first]; flags; '
        'notes; `v1 <class>` where v2 changed it; `same object as` (overlapping windows); your words where you typed a '
        'label or note; `open` = the object in the editor (review layer); `sheet` = shown on '
        '`prototype-v2/renders/contact-<set>.png` (panel title `#N`).', '',
        f'## Changed from v1 ({len(changed)} lines)', '']
    for label in sorted({f"{x['v1']['proposed']} -> {display_class(x)}" for x in changed}):
        sub = [x for x in changed if f"{x['v1']['proposed']} -> {display_class(x)}" == label]
        lines.append(f'### {label} ({len(sub)})')
        for x in sub:
            note = f" - {'; '.join(x['notes'])}" if x['notes'] else ''
            twin = f" (same object as {', '.join(map(str, x['twins']))})" if x['twins'] else ''
            lines.append(f"- {x['number']} {x['scene']}: {display_class(x)} ({x['confidence']}){note}{twin}"
                         + f' - {change_why(x)[:220]}')
        lines.append('')
    lines.append('## All lines by window')
    lines.append('')
    for scene_id in sorted({x['scene'] for x in rows}):
        sub = [x for x in rows if x['scene'] == scene_id]
        c = Counter(display_class(x) for x in sub)
        lines.append(f"### {scene_id} ({len(sub)} objects; {sub[0]['features']['mpp']:.3f} m/px; "
                     + ', '.join(f'{k} {c[k]}' for k in DISPLAY_V2 if c[k]) + ')')
        lines.append(f"Window in the editor: {EDITOR_URL.format(scene_id, '').replace('&object=&', '&')}")
        lines.append('')
        lines += [marks_line_v2(x, rendered) for x in sub]
        lines.append('')
    (OUT_DIR_V2/'MARKS-vegetation-v2.md').write_text('\n'.join(lines))
    heartbeat_v2(f'marks-v2 {len(rows)} lines, {len(changed)} changed')
    print(f'MARKS-vegetation-v2.md: {len(rows)} numbered lines, {len(changed)} changed from v1')


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('measure').set_defaults(func=measure)
    sub.add_parser('classify').set_defaults(func=classify_run)
    p = sub.add_parser('render')
    p.add_argument('--per-class', type=int, default=16)
    p.add_argument('--seed', type=int, default=8)
    p.set_defaults(func=render)
    sub.add_parser('marks').set_defaults(func=marks)
    sub.add_parser('measure-v2').set_defaults(func=measure_v2)
    sub.add_parser('classify-v2').set_defaults(func=classify_v2_run)
    p = sub.add_parser('render-v2')
    p.add_argument('--per-set', type=int, default=16)
    p.add_argument('--seed', type=int, default=8)
    p.set_defaults(func=render_v2)
    sub.add_parser('marks-v2').set_defaults(func=marks_v2)
    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
