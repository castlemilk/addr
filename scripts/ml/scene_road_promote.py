"""Write accepted road-corridor v3.1 bands into their windows as road objects superseding the SAM pieces they list.

The bands are the v3.1 sidecar proposals (data/reviews/eyes/experiments/road-shape-2026-10-07/prototype/run-v31/<scene>.review.json,
numbered as MARKS-roads-v31.md numbers them). Decision card item 1 (default): a band the reviewer marks right is written into its
window as a new road object; the road pieces it lists are superseded by it, the way the linear join was promoted on 3 October
(scripts/ml/scene_linear_promote.py, whose discipline this module follows and whose helpers it reuses). Per window, in order:

0. Freshness. The run did not record the review revision it read, so the revision is pinned from the review's own history: the
   last revision saved before the run started (RUN_STARTED, .run/session-2026-10-07-roads/v31/progress.txt) with no save inside the
   run interval (to metrics.json written_at). A window whose current revision or model signature is not the pinned one, or whose
   lineage objects no longer read the statuses the run recorded, is refused 'stale_proposal' (re-run road_corridor first).
1. The band set: --marks (a reviewer's chat answer, band number -> right / wrong / unclear, or a road-decisions-v1 transcription
   read as is) applies the bands marked right, each with the reviewer's accept (--bands with --marks narrows that set); --bands
   (numbers or ids) alone applies bands WITHOUT an accept; neither = the default set (every band but band 8, DEFAULT_EXCLUDED),
   without an accept. --proposals may be a partial directory (one window re-run at its current revision): a band without its
   proposal file is refused when named in --bands and skipped (named in not-selected) otherwise. Members = the band's 'replaces' plus, ONLY with an accept, 'replaces_on_accept'
   and 'retires_on_accept'. A band whose lineage names a reviewer-touched object (a confirmed seed such as e31f2cb143c1b8dc, a
   decided sibling, a retire target such as d5875542ec9e47a2) is refused 'needs_reviewer_accept' without the accept.
2. Model layer: a new snapshot with every non-object field byte-identical, the members dropped and each band appended as kind road,
   source 'road-corridor-v3.1', status candidate, carrying `replaces` = its members (never derived_from: the model layer records
   replaces only) and a `road_band` block naming the proposal. A junction band (road_corridor 'remainder_on_accept') also appends
   the seed's other arms as road pieces replacing the seed, so applying the band never deletes a cross street. The coverage guard
   (applying a band never deletes road area the reviewer did not decide to delete): a listed member with under 50 % of its own area
   inside the band is not a member (it stays exactly as it is; recorded kept_outside_member; a retire target in that state refuses
   the band); of the members, each connected part of (the filled members minus every applied band and junction piece of the
   window) of >= 2 m2 is appended as a remainder road piece made as the junction remainder is (candidate, kind road, replaces the
   member(s) it comes from, the app's ring rules), smaller parts are absorbed and listed with their area. model_signature is
   recomputed exactly as scene_extract.save_scene does (scene_linear_promote.signature); models/<sig>.json + latest.json written
   the same way; the old models file stays.
3. Review layer: carried with the editor's recoverSceneReview through scripts/scene-road-carry.mjs (restore-verbatim of every
   reviewer-touched non-member by default); members kept as superseded with replacement_ids -> band (+ remainder pieces); the
   band's review copy has derived_from = replaces; a retire target carries the reason in its notes. A band with the reviewer's
   accept reads confirmed + geometry_reviewed true with provenance.human_decision (source human:chat, the chat sentence and time:
   the accepted-import rule); a band without reads candidate (or needs_adjustment when the editor's rule inherits a touched
   candidate member's words). Validated on a qa_road_ scratch copy through the app (GET stale false, POST /api/scenes 200 at
   revision + 1: the app's lineage and ring rules), then written under the review lock at revision + 1 with updated_by
   'scripts.ml.scene_road_promote'. Byte copies of latest.json and review.json go to the run directory first.
4. One registry row (role road_band) and the scene_lot_tags re-key: only on a real window (a qa_ scratch target and --dry-run
   write neither). <run-dir>/<id>/promote.json records every number and sha.

--dry-run writes only under --run-dir (one plan line per window to stdout and <run-dir>/plan.txt, <run-dir>/<id>.json per window).
--rollback <id> restores the byte copies and removes the registry row (models/<new>.json and the lot-tags rows stay, documented).
--batch promotes the windows one at a time with scene_linear_promote's six-field counter guard (auto-rollback + stop on a
difference); --quiet-since <ISO> stops before a window when a review.json this program did not write changed after that time.
A marks file that is SIMULATED (in its name or 'simulated': true) is refused on a real window (qa_ scratch and dry runs only).
No model is loaded (CPU geometry only). Nothing is written on a refusal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from shapely import make_valid
from shapely.geometry import Polygon
from shapely.ops import unary_union

from . import scene_linear_promote as L
from .scene_extract import APP_RING_RULES, _ring_details, app_ring_issues, finalise_app_ring
from .scene_linear_promote import Refusal, canonical, now_stamp, sha, snapshot_bytes, validate_id
from .segmentation import iter_polygons

REPO = L.REPO
NODE_CARRY = REPO/'scripts/scene-road-carry.mjs'
VERSION = 'road-promote-v1'
UPDATED_BY = 'scripts.ml.scene_road_promote'
ROLE = 'road_band'
SOURCE = 'road-corridor-v3.1'
REGISTRY = L.REGISTRY
LOT_TAGS_REPORT = L.LOT_TAGS_REPORT
EXPERIMENT = Path('data/reviews/eyes/experiments/road-shape-2026-10-07')
PROPOSALS = EXPERIMENT/'prototype/run-v31'
MARKS_SHEET = EXPERIMENT/'prototype/MARKS-roads-v31.md'
RUN_DIR = Path('.run/session-2026-10-07-roads/apply/promote')
# v31/progress.txt "18:59:59 v31: run --design v3.1 (54 windows) start" (AEDT, UTC+11); the end is run-v31/metrics.json written_at
RUN_STARTED = '2026-10-07T07:59:59Z'
DEFAULT_EXCLUDED = {8: 'lot-25641 band 71771b8122dd0689 is 0.79 m narrower than its street (the kerb reader stopped at a dark gutter; '
                       'agent mark wrong); excluded from the default set'}
RETIRE_NOTE = ('Retired by road band {band} (band {number} of MARKS-roads-v31) on the reviewer\'s accept: a duplicate of the street '
               'from the "driveway" prompt (decision card 2026-10-07 item 3).')
MARKS = ('right', 'wrong', 'unclear')
NOT_APPLIED = 'not_applied'          # a decisions transcription's word for a band the reviewer did not mark right (band 8)
DECIDED = L.DECIDED
LINEAR_KINDS = ('road', 'driveway', 'footpath')
SCRATCH_PREFIX = 'qa_road_'
SCRATCH_MARKER = 'qa_source.json'
LINEAGE_CAPS = {'derived_from': 64, 'replacement_ids': 8}       # src/lib/scene-instances.ts MAX_TRACE_SOURCES / 8
# The coverage guard (7 Oct, after coverage.txt showed bands silently dropping member area outside them; the reviewer never decided
# to delete road area): a member with less than COVER_MIN_SHARE of its own area inside its band is not superseded (it stays exactly
# as it is, recorded kept_outside_member); of a superseded member, every connected part left outside the window's bands of at least
# REMAINDER_MIN_M2 stays as a remainder road piece (made as the junction remainder is made); smaller parts are absorbed, listed.
COVER_MIN_SHARE = .5
REMAINDER_MIN_M2 = 2.


def _parse_time(text):
    value = datetime.fromisoformat(str(text).replace('Z', '+00:00'))
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _iso(value):
    return value.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]+'Z'


def _id16(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# --- the bands -----------------------------------------------------------------------------------------------------------

def read_marks_sheet(path):
    """[(number, window, band id)] in MARKS-roads-v31.md order."""
    rows = re.findall(r'^(\d+)\. `([A-Za-z0-9_-]+)` band `([0-9a-f]{16})`', Path(path).read_text(), re.M)
    if not rows:
        raise Refusal('marks_sheet_empty', str(path))
    return [(int(n), w, b) for n, w, b in rows]


def load_bands(root=Path('.'), *, proposals=None, sheet=None):
    """{number: band} from the marks sheet and the run's review-layer proposal files; every band checked to be a v3.1 road band."""
    root = Path(root)
    proposals = Path(proposals) if proposals else root/PROPOSALS
    sheet = Path(sheet) if sheet else root/MARKS_SHEET
    out, docs = {}, {}
    for number, window, band_id in read_marks_sheet(sheet):
        path = proposals/f'{window}.review.json'
        if not path.exists():
            # a partial proposals directory (one window re-run at its current revision): the band is unavailable, and
            # select_bands refuses it when asked for by name, skips it (named) otherwise
            out[number] = {'number': number, 'window': window, 'id': band_id, 'missing': f'{path.as_posix()} not found'}
            continue
        if window not in docs:
            data = path.read_bytes()
            docs[window] = (json.loads(data), sha(data))
        doc, doc_sha = docs[window]
        if doc.get('scene') != window or doc.get('layer') != 'review':
            raise Refusal('proposal_file_mismatch', f'{path.name}: scene {doc.get("scene")!r} layer {doc.get("layer")!r}')
        record = next((p for p in doc.get('proposals') or [] if p['proposal']['id'] == band_id), None)
        if record is None:
            raise Refusal('band_not_in_run', f'band {number} {band_id} is not a proposal of {path.name}')
        proposal = record['proposal']
        if proposal.get('source') != SOURCE or proposal.get('kind') != 'road' or proposal.get('status') != 'proposal':
            raise Refusal('band_not_v31_road', f"band {number}: source {proposal.get('source')!r} kind {proposal.get('kind')!r}")
        out[number] = {'number': number, 'window': window, 'id': band_id, 'proposal': proposal,
                       'record': {k: record.get(k) for k in ('seed', 'seed_kind', 'seed_status', 'width_m', 'width_source', 'cars_under_n', 'vertices_after')},
                       'retires_record': record.get('retires_on_accept') or [],
                       'proposal_file': path.as_posix(), 'proposal_file_sha256': doc_sha}
    return out


def read_marks(path):
    """The reviewer's answer: {"marks": {"1": "right", ...}, "sentence": <chat line>, "at": <ISO>, "source": "human:chat"}.

    Also read as is: a decisions transcription (road-decisions-v1, decisions-2026-10-07.json): "marks_by_band_number" for "marks",
    "said_at" or "transcribed_at" for "at" (the field used is recorded), and the mark "not_applied" (a band the reviewer did not
    mark right; never applied). A mark is the first word of the value (right / wrong / unclear / not_applied; "wrong (too wide)"
    reads wrong). A file with any 'right' must carry the chat sentence and its time (they go into each accepted band's provenance).
    'simulated' is true when the file says so or its name contains SIMULATED; a simulated accept never reaches a real window."""
    path = Path(path)
    data = path.read_bytes()
    doc = json.loads(data)
    raw = (doc.get('marks') or doc.get('marks_by_band_number')) if isinstance(doc, dict) else None
    if not isinstance(raw, dict) or not raw:
        raise Refusal('marks_unreadable', f'{path}: expected {{"marks": {{"<band number>": "right|wrong|unclear"}}, "sentence", "at"}}')
    marks = {}
    for key, value in raw.items():
        word = str(value).strip().split()[0].lower().rstrip(',.;:') if str(value).strip() else ''
        if not str(key).isdigit() or word not in (*MARKS, NOT_APPLIED):
            raise Refusal('marks_unreadable', f'{key!r}: {value!r}')
        marks[int(key)] = word
    source = doc.get('source', 'human:chat')
    at_field = next((k for k in ('at', 'said_at', 'transcribed_at') if doc.get(k)), None)
    if any(v == 'right' for v in marks.values()):
        if source != 'human:chat' or not str(doc.get('sentence') or '').strip() or not at_field:
            raise Refusal('marks_without_chat', 'an accept needs source human:chat, the chat sentence and its time')
        _parse_time(doc[at_field])
    return {'marks': marks, 'sentence': doc.get('sentence'), 'at': doc.get(at_field) if at_field else None, 'at_field': at_field, 'source': source,
            'transcribed_by': doc.get('transcribed_by'), 'path': str(path), 'sha256': sha(data),
            'simulated': bool(doc.get('simulated')) or 'SIMULATED' in path.name.upper()}


def select_bands(bands, *, wanted=None, marks=None):
    """(selected bands, accepted numbers, not selected [{number, reason}]). wanted = band numbers or ids (strings or ints)."""
    by_id = {b['id']: n for n, b in bands.items()}
    if wanted:
        numbers = set()
        for item in wanted:
            text = str(item)
            if text.isdigit() and int(text) in bands:
                numbers.add(int(text))
            elif text in by_id:
                numbers.add(by_id[text])
            else:
                raise Refusal('unknown_band', text)
    else:
        numbers = None
    named_missing = sorted(n for n in (numbers or ()) if bands[n].get('missing'))
    if named_missing:
        raise Refusal('proposal_file_missing', '; '.join(f"band {n}: {bands[n]['missing']}" for n in named_missing))

    def available(selected, skipped):
        """A band with no proposal file (a partial proposals directory) leaves the selection, named, never silently."""
        gone = [n for n in selected if bands[n].get('missing')]
        skipped = sorted(skipped + [{'number': n, 'reason': f"proposal file missing: {bands[n]['missing']}"} for n in gone], key=lambda s: s['number'])
        return [bands[n] for n in selected if n not in gone], skipped
    if marks is not None:
        unknown = sorted(set(marks['marks']) - set(bands))
        if unknown:
            raise Refusal('unknown_band', f'marks name band(s) {unknown}')
        accepted = {n for n, v in marks['marks'].items() if v == 'right' and (numbers is None or n in numbers)}
        skipped = [{'number': n, 'reason': f"marked {marks['marks'][n]}" if n in marks['marks'] else 'not marked'} for n in sorted(bands) if n not in accepted]
        chosen, skipped = available(sorted(accepted), skipped)
        return chosen, {b['number'] for b in chosen}, skipped
    selected = sorted(numbers) if numbers is not None else [n for n in sorted(bands) if n not in DEFAULT_EXCLUDED]
    skipped = [{'number': n, 'reason': DEFAULT_EXCLUDED.get(n, 'not selected') if numbers is None else 'not selected'} for n in sorted(bands) if n not in selected]
    chosen, skipped = available(selected, skipped)
    return chosen, set(), skipped


# --- freshness -----------------------------------------------------------------------------------------------------------

def run_interval(proposals_dir, started=RUN_STARTED):
    metrics = json.loads((Path(proposals_dir)/'metrics.json').read_text())
    start, end = _parse_time(started), _parse_time(metrics['written_at'])
    if end < start:
        raise Refusal('run_interval_invalid', f'{started} .. {metrics["written_at"]}')
    return start, end


def pin_revision(review, interval):
    """The review revision current while the run read the window, from the review's own history (the run did not record it)."""
    start, end = interval
    entries = []
    for body in [*(review.get('history') or []), review]:
        if body.get('updated_at'):
            entries.append((body.get('revision'), _parse_time(body['updated_at']), body.get('model_signature')))
    inside = [e for e in entries if start <= e[1] <= end]
    if inside:
        raise Refusal('pin_ambiguous', f'revision(s) {[e[0] for e in inside]} saved while the run read the windows ({_iso(start)} .. {_iso(end)})')
    before = [e for e in entries if e[1] < start]
    if not before:
        raise Refusal('pin_unknown', f'no revision of this review was saved before the run started ({_iso(start)})')
    revision, at, signature = max(before, key=lambda e: (e[0] if isinstance(e[0], int) else -1, e[1]))
    return {'revision': revision, 'updated_at': _iso(at), 'model_signature': signature, 'how': 'last review revision saved before the run started '
            '(review history); no save inside the run interval', 'run_interval': [_iso(start), _iso(end)]}


def freshness(review, latest, bands, interval):
    """None when the proposal is fresh, else the stale detail. Revision + signature against the pin, then the run's own evidence."""
    pin = pin_revision(review, interval)
    problems = []
    if review.get('revision') != pin['revision']:
        problems.append(f"review rev {review.get('revision')} (saved {review.get('updated_at')}) is not rev {pin['revision']} the run read")
    if latest.get('model_signature') != pin['model_signature'] or review.get('model_signature') != pin['model_signature']:
        problems.append(f"model signature {str(latest.get('model_signature'))[:12]} is not {str(pin['model_signature'])[:12]} the run read")
    by_id = {o['id']: o for o in review.get('objects') or []}
    for band in bands:
        recorded = {band['record']['seed']: band['record']['seed_status']}
        for item in [*(band['proposal'].get('absorbs') or []), *(band['proposal'].get('xclass_twin') or []), *band['retires_record']]:
            recorded[item['id']] = item.get('status')
        for oid, status in sorted(recorded.items()):
            now = (by_id.get(oid) or {}).get('status')
            if now != status:
                problems.append(f"band #{band['number']}: {oid} reads {now}, the run recorded {status}")
    return pin, (problems or None)


# --- the plan --------------------------------------------------------------------------------------------------------------

def lineage_issue(objects):
    """src/lib/scene-instances.ts lineageIssue, rule for rule, in the app's words; None = the app's save accepts the lineage."""
    by_id = {o['id']: o for o in objects}
    for o in objects:
        for key, cap in LINEAGE_CAPS.items():
            if key in o:
                links = o[key]
                if (not isinstance(links, list) or not links or len(links) > cap or len(set(map(str, links))) != len(links)
                        or any(not isinstance(i, str) or i == o['id'] or i not in by_id for i in links)):
                    return 'Replacement history must refer to existing, distinct objects.'
        if (o.get('status') == 'superseded') != bool(o.get('replacement_ids')) or (o.get('status') == 'superseded' and o.get('geometry_reviewed')):
            return 'Replaced outlines must retain their replacements and cannot be approved.'
        if (any(o['id'] not in ((by_id.get(i) or {}).get('replacement_ids') or []) for i in o.get('derived_from') or [])
                or any(o['id'] not in ((by_id.get(i) or {}).get('derived_from') or []) for i in o.get('replacement_ids') or [])):
            return 'The replacement history is incomplete.'
    done, visiting = set(), set()

    def cycle(oid):
        if oid in visiting:
            return True
        if oid in done:
            return False
        visiting.add(oid)
        if any(cycle(r) for r in (by_id.get(oid) or {}).get('replacement_ids') or []):
            return True
        visiting.discard(oid)
        done.add(oid)
        return False
    return 'Replacement history cannot form a loop.' if any(cycle(o['id']) for o in objects) else None


def feedback_view(o):
    """src/lib/scene-feedback.ts feedback() without the lineage pair (derived_from / replacement_ids): what the reviewer can change
    on an object. A linear-merge feature's review copy differs from its model copy only in derived_from (the 3 Oct promotion wrote
    it), which is machine lineage, not a reviewer edit; JS-canonical, so 793 and 793.0 compare equal."""
    m = o.get('model') or {}
    return canonical({'kind': o.get('kind'), 'structure_kind': o.get('structure_kind') or '', 'label': o.get('label'), 'ring': o.get('ring'), 'holes': o.get('holes') or [],
                      'status': o.get('status'), 'checked': o.get('geometry_reviewed'), 'notes': o.get('notes') or '', 'group': o.get('group_id') or '',
                      'visibility': o.get('visibility'), 'review_issues': o.get('review_issues') or [], 'parent': o.get('parent_id') or '',
                      'roof': o.get('roof_annotation'), 'parts': o.get('roof_parts'), 'lines': o.get('roof_lines'), 'form': o.get('roof_type') or '',
                      'roof_source': o.get('roof_geometry_source') or '', 'roof_note': o.get('roof_geometry_note') or '', 'roof_feedback': o.get('roof_feedback'),
                      'roof_line_edits': o.get('roof_line_edits'), 'height': m.get('height_m'), 'base': m.get('base_m'), 'color': m.get('color'),
                      'height_source': m.get('height_source'), 'height_verified': m.get('height_verified'), 'placement': m.get('placement') or 'roof',
                      'tree': m.get('tree'), 'car': m.get('car'), 'surface': m.get('surface'), 'solar': m.get('solar'), 'carried_from': o.get('carried_from')})


def reviewer_touched(review_o, model_o):
    return model_o is None or feedback_view(review_o) != feedback_view(model_o)


def provenance_for(band, marks):
    decision = {'source': 'human:chat', 'transcribed_by': UPDATED_BY, 'at': marks['at'], 'sentence': marks['sentence'], 'band': band['number'],
                'mark': 'right', 'fields': ['status', 'geometry_reviewed'], 'marks_file': marks['path'], 'marks_sha256': marks['sha256']}
    if marks.get('at_field') and marks['at_field'] != 'at':
        decision['at_field'] = marks['at_field']
    if marks.get('transcribed_by'):
        decision['marks_transcribed_by'] = marks['transcribed_by']
    if marks.get('simulated'):
        decision['simulated'] = True
    return {'human_decision': decision}


def shape_of(o):
    """An object's stored ring + holes (pixels) as one valid shapely geometry, or None when the outline is unusable."""
    ring = (o or {}).get('ring') or []
    if len(ring) < 3:
        return None
    try:
        geometry = make_valid(Polygon(ring, [h for h in o.get('holes') or [] if len(h) >= 3]))
    except (ValueError, TypeError):
        return None
    parts = [g for g in iter_polygons(geometry) if g.area > 0]
    return unary_union(parts) if parts else None


def filled(geometry):
    """road_corridor fill_holes: each polygon's outer ring only (the junction remainder is cut from the filled seed, so a car or
    tree cut-out in a parking lane does not survive into the remainder; cars and trees are drawn on top, as on the band)."""
    parts = [Polygon(p.exterior) for p in iter_polygons(geometry) if p.area > 0]
    return unary_union(parts) if parts else None


def m2_per_px2(latest):
    a = np.asarray(latest['pixel_to_meters'], float)
    return abs(float(a[0][0]*a[1][1]-a[0][1]*a[1][0]))


def _r(value, digits=2):
    return round(float(value), digits)


def member_cover(band_shape, member_shape, scale):
    """{area_m2, inside_m2, inside_share} of one member against its band (the member as stored, holes included)."""
    area = member_shape.area
    inside = member_shape.intersection(band_shape).area
    return {'area_m2': _r(area*scale), 'inside_m2': _r(inside*scale), 'inside_share': inside/area if area > 0 else 0.}


def coverage_parts(member_shapes, cover, scale, *, min_m2=REMAINDER_MIN_M2):
    """The connected parts of (the filled superseded members minus `cover`), largest first: (kept, absorbed), each item {part,
    area_m2, from}: a part of at least min_m2 is kept (a remainder piece), a smaller one absorbed. `from` = the members the part
    comes from (two members overlapping outside the band - a road piece and its driveway-prompt twin - give ONE part from both)."""
    filled_by_id = {m: filled(g) for m, g in sorted(member_shapes.items()) if g is not None}
    filled_by_id = {m: g for m, g in filled_by_id.items() if g is not None}
    if not filled_by_id:
        return [], []
    union = unary_union(list(filled_by_id.values()))
    rest = union.difference(cover) if cover is not None and not cover.is_empty else union
    parts = sorted((p for p in iter_polygons(rest) if p.area > 0), key=lambda p: (-p.area, p.centroid.x, p.centroid.y))
    kept, absorbed = [], []
    for part in parts:
        area = part.area*scale
        sources = sorted(m for m, g in filled_by_id.items() if g.intersection(part).area > part.area*1e-6)
        (kept if area >= min_m2 else absorbed).append({'part': part, 'area_m2': area, 'from': sources})
    return kept, absorbed


def coverage_remainders(row, cover, latest, model_by_id):
    """The coverage remainder pieces of one band (planned features, kind road, candidate, replaces = the members they come from) and
    its coverage record; problems = refusals (a ring the app refuses, a member over the replacement cap)."""
    scale = m2_per_px2(latest)
    width, height = latest.get('image_width'), latest.get('image_height')
    affine = np.asarray(latest['pixel_to_meters'], float)
    p = row['_proposal']
    kept, absorbed = coverage_parts(row['_member_shapes'], cover, scale)
    features, problems = [], []
    for k, item in enumerate(kept):
        part = item['part']
        ring = [[_r(x), _r(y)] for x, y in part.exterior.coords[:-1]]
        holes = [[[_r(x), _r(y)] for x, y in h.coords[:-1]] for h in part.interiors]
        ring, holes, issues, repairs = finalise_app_ring(ring, holes, width, height, rules=APP_RING_RULES, drop_collapsed_holes=True)
        ring = [[float(x), float(y)] for x, y in ring]
        holes = [[[float(x), float(y)] for x, y in h] for h in holes]
        rid = _id16(f"{VERSION}:coverage:{p['id']}:{k}")
        if issues:
            problems.append(['remainder_ring_app_issue', f"{rid} ({_r(item['area_m2'])} m2 outside the band): {'; '.join(issues)}"])
        scores = [s for s in ((model_by_id.get(m) or {}).get('score') for m in item['from']) if isinstance(s, (int, float))]
        model = {**_ring_details(ring, holes, affine, 'road'), 'color': (p.get('model') or {}).get('color', '#84868c')}
        features.append({'id': rid, 'kind': 'road', 'label': 'Road', 'ring': ring, 'holes': holes, 'status': 'candidate', 'geometry_reviewed': False,
                         'source': SOURCE, 'score': max(scores) if scores else p.get('score'), 'model': model, 'replaces': list(item['from']),
                         'road_band': {'role': 'remainder', 'origin': 'coverage', 'band': p['id'], 'index': k, 'from': list(item['from']),
                                       'area_m2': _r(item['area_m2']), 'ring_repairs': repairs, 'version': VERSION}})
    # the app's cap on replacement_ids: the band + every remainder piece replacing a member
    junction = row['_junction']
    for m in row['members']:
        n = 1+sum(m in f['replaces'] for f in (*junction, *features))
        if n > LINEAGE_CAPS['replacement_ids']:
            problems.append(['member_replacements_over_cap', f"{m} would be replaced by {n} objects (cap {LINEAGE_CAPS['replacement_ids']})"])
    stored = unary_union([g for g in row['_member_shapes'].values() if g is not None]) if row['_member_shapes'] else None
    kept_union = unary_union([shape_of(f) for f in features]) if features else None
    uncovered = None
    if stored is not None and not stored.is_empty:
        rest = stored.difference(cover) if cover is not None and not cover.is_empty else stored
        uncovered = rest.difference(kept_union) if kept_union is not None else rest
    record = {'rule': {'min_share_inside': COVER_MIN_SHARE, 'remainder_min_m2': REMAINDER_MIN_M2},
              'members_outside_band_m2': _r(stored.difference(row['_band_shape']).area*scale) if stored is not None else 0.,
              'remainders': [{'id': f['id'], 'area_m2': f['road_band']['area_m2'], 'from': f['replaces'], 'vertices': len(f['ring']), 'holes': len(f['holes'])}
                             for f in features],
              'remainder_m2': _r(sum(item['area_m2'] for item in kept)),
              'absorbed': [{'area_m2': _r(item['area_m2'], 3), 'from': item['from']} for item in absorbed],
              'absorbed_m2': _r(sum(item['area_m2'] for item in absorbed), 3),
              'uncovered_after_m2': _r(uncovered.area*scale, 3) if uncovered is not None else 0.}
    return features, record, problems


def plan_window(latest, review, bands, accepted, marks=None):
    """The bands of one window as model-layer features (+ remainder pieces), their members, the carry spec and the refusals; pure.

    The coverage guard: a listed member with under COVER_MIN_SHARE of its area inside the band stays as it is (kept_outside_member,
    not a member); of the superseded members, each connected part outside every band of the window (and the junction pieces) of at
    least REMAINDER_MIN_M2 becomes a remainder road piece replacing the member(s) it comes from; smaller parts are absorbed, listed."""
    model_by_id = {o['id']: o for o in latest['objects']}
    review_by_id = {o['id']: o for o in review['objects']}
    width, height = latest.get('image_width'), latest.get('image_height')
    affine = np.asarray(latest['pixel_to_meters'], float)
    scale = m2_per_px2(latest)
    rows = []
    for band in sorted(bands, key=lambda b: b['number']):
        p = band['proposal']
        is_accepted = band['number'] in accepted
        on_accept = sorted(set(p.get('replaces_on_accept') or []) | set(p.get('retires_on_accept') or []))
        listed = sorted(set(p.get('replaces') or []) | (set(on_accept) if is_accepted else set()))
        retires = sorted(p.get('retires_on_accept') or []) if is_accepted else []
        reasons = []
        band_shape = shape_of(p)
        kept_outside, shapes = [], {}
        for m in listed:
            shape = shape_of(review_by_id.get(m) or model_by_id.get(m))
            shapes[m] = shape
            if shape is None or band_shape is None:
                continue
            cover = member_cover(band_shape, shape, scale)
            if cover['inside_share'] < COVER_MIN_SHARE:
                o = review_by_id.get(m) or model_by_id.get(m)
                kept_outside.append({'id': m, 'kind': o.get('kind'), 'status': o.get('status'), **cover, 'inside_share': _r(cover['inside_share'], 4)})
                if m in retires:
                    reasons.append(['retire_target_mostly_outside_band', f"{m} has {_r(cover['inside_share'], 4)} of its area inside the band; the reviewer's "
                                    'retire decision and the coverage rule disagree'])
        kept_ids = {k['id'] for k in kept_outside if k['id'] not in retires}
        members = [m for m in listed if m not in kept_ids]
        if on_accept and not is_accepted:
            reasons.append(['needs_reviewer_accept', f"replaces on accept {p.get('replaces_on_accept') or []}, retires on accept {p.get('retires_on_accept') or []}"])
        if not members:
            reasons.append(['nothing_to_replace', 'every listed member lies mostly outside the band (kept_outside_member)' if listed else 'no member without an accept'])
        for m in members:
            model_o, review_o = model_by_id.get(m), review_by_id.get(m)
            if model_o is None:
                reasons.append(['member_not_in_model', m])
                continue
            if review_o is None:
                reasons.append(['member_not_in_review', m])
                continue
            if model_o.get('status') != 'candidate':
                reasons.append(['member_not_candidate_in_model', m])
            if model_o.get('kind') not in LINEAR_KINDS:
                reasons.append(['member_not_linear', f"{m} is {model_o.get('kind')}"])
            if review_o.get('status') in ('rejected', 'superseded'):
                reasons.append(['member_rejected_or_superseded', f"{m} is {review_o['status']} in the review"])
            elif (review_o.get('status') in DECIDED or review_o.get('geometry_reviewed')) and not is_accepted:
                reasons.append(['needs_reviewer_accept', f"{m} is {review_o.get('status')} in the review"])
            elif reviewer_touched(review_o, model_o) and not is_accepted:
                reasons.append(['needs_reviewer_accept', f"{m} was edited in the review (outline, words or flags)"])
        if p['id'] in model_by_id or p['id'] in review_by_id:
            reasons.append(['band_id_taken', p['id']])
        ring_issues = app_ring_issues(p['ring'], [], width, height, rules=APP_RING_RULES)
        if ring_issues:
            reasons.append(['band_ring_app_issue', '; '.join(ring_issues)])
        junction, junction_skipped = [], []
        for k, piece in enumerate(p.get('remainder_on_accept') or []):
            ring, _, issues, repairs = finalise_app_ring([tuple(q) for q in piece['ring']], [], width, height, rules=APP_RING_RULES)
            ring = [[float(x), float(y)] for x, y in ring]
            rid = _id16(f"{VERSION}:remainder:{p['id']}:{k}")
            if piece['from'] in kept_ids:
                # the seed is kept whole (mostly outside the band): its other arms stay inside it, no piece is cut from it
                junction_skipped.append({'id': rid, 'from': piece['from'], 'why': 'seed kept_outside_member'})
                continue
            seed = model_by_id.get(piece['from'])
            if seed is None or piece['from'] not in members:
                reasons.append(['remainder_seed_not_member', piece['from']])
                continue
            if issues:
                reasons.append(['remainder_ring_app_issue', f"{rid}: {'; '.join(issues)}"])
            model = {**_ring_details(ring, [], affine, seed['kind']), 'color': (seed.get('model') or {}).get('color', '#84868c')}
            junction.append({'id': rid, 'kind': seed['kind'], 'label': seed.get('label') or 'Road', 'ring': ring, 'holes': [], 'status': 'candidate',
                             'geometry_reviewed': False, 'source': SOURCE, 'score': seed.get('score'), 'model': model, 'replaces': [piece['from']],
                             'road_band': {'role': 'remainder', 'origin': 'junction', 'band': p['id'], 'index': k, 'from': piece['from'],
                                           'area_m2_run': piece.get('area_m2'), 'ring_repairs': repairs, 'version': VERSION}})
        feature = {'id': p['id'], 'kind': 'road', 'label': 'Road', 'ring': [list(q) for q in p['ring']], 'holes': [], 'status': 'candidate',
                   'geometry_reviewed': False, 'source': SOURCE, 'score': p.get('score'), 'model': dict(p['model']), 'replaces': members,
                   'road_band': {'role': 'band', 'version': VERSION, 'design': SOURCE, 'marks_sheet_number': band['number'], 'proposal_file': band['proposal_file'],
                                 'proposal_file_sha256': band['proposal_file_sha256'], 'seed': band['record']['seed'], 'width_m': band['record']['width_m'],
                                 'width_source': band['record']['width_source'], 'cars_under': list(p.get('cars_under') or []), 'assumption': p.get('assumption'),
                                 'replaces_on_accept': list(p.get('replaces_on_accept') or []), 'retires_on_accept': list(p.get('retires_on_accept') or []),
                                 'remainder_ids': [r['id'] for r in junction], 'kept_outside_member': sorted(kept_ids),
                                 'coverage_rule': {'min_share_inside': COVER_MIN_SHARE, 'remainder_min_m2': REMAINDER_MIN_M2}}}
        rows.append({'number': band['number'], 'id': p['id'], 'accepted': is_accepted, 'members': members,
                     'supersedes': [{'id': m, 'kind': (model_by_id.get(m) or {}).get('kind'), 'review_status': (review_by_id.get(m) or {}).get('status')} for m in members],
                     'kept_outside_member': kept_outside, 'retire_targets': retires, 'remainder_ids': [r['id'] for r in junction],
                     'junction_remainder_skipped': junction_skipped, 'coverage': None, 'width_m': band['record']['width_m'],
                     'area_m2': p['model'].get('area_m2'), 'vertices': len(p['ring']), 'refused': reasons, '_feature': feature, '_junction': junction,
                     '_coverage': [], '_proposal': p, '_band_shape': band_shape, '_member_shapes': {m: shapes.get(m) for m in members}})
    # a member in two bands of this window refuses both
    owners = {}
    for row in rows:
        if not row['refused']:
            for m in row['members']:
                owners.setdefault(m, []).append(row)
    for m, owning in owners.items():
        if len(owning) > 1:
            for row in owning:
                row['refused'].append(['member_in_two_bands', m])
    # the coverage guard: what each applied band's members leave outside every applied band (and its junction pieces) stays as
    # remainder road pieces; a refusal here (a ring the app refuses) changes the applied set, so repeat until it holds
    while True:
        applied = [r for r in rows if not r['refused']]
        cover = unary_union([r['_band_shape'] for r in applied if r['_band_shape'] is not None]
                            + [g for r in applied for g in (shape_of(f) for f in r['_junction']) if g is not None]) if applied else None
        refused_now = False
        for row in applied:
            row['_coverage'], row['coverage'], problems = coverage_remainders(row, cover, latest, model_by_id)
            if problems:
                row['refused'].extend(problems)
                refused_now = True
        if not refused_now:
            break
    for row in rows:
        row['remainder_ids'] = [f['id'] for f in (*row['_junction'], *(row['_coverage'] if not row['refused'] else []))]
        row['_feature']['road_band']['remainder_ids'] = list(row['remainder_ids'])
    applied = [r for r in rows if not r['refused']]
    features = [f for r in applied for f in (r['_feature'], *r['_junction'], *r['_coverage'])]
    members = sorted(m for r in applied for m in r['members'])
    specs = [{'id': r['id'], 'accepted': r['accepted'], 'members': r['members'], 'remainder_ids': r['remainder_ids'], 'retires': r['retire_targets'],
              'retire_note': RETIRE_NOTE.format(band=r['id'], number=r['number']),
              'provenance': provenance_for(next(b for b in bands if b['id'] == r['id']), marks) if r['accepted'] else None} for r in applied]
    public = [{k: v for k, v in r.items() if not k.startswith('_')} for r in rows]
    return {'bands': public, 'applied': [r['number'] for r in applied], 'refused': [r['number'] for r in rows if r['refused']], 'features': features,
            'members': members, 'specs': specs, 'nothing_to_apply': not applied}


# --- the carry and its check ----------------------------------------------------------------------------------------------

def carry_review(previous_path, current_path, saved_path, bands_path, out_path, *, restore_verbatim=True, node='node'):
    cmd = [node, str(NODE_CARRY), '--previous', str(previous_path), '--current', str(current_path), '--saved', str(saved_path),
           '--bands', str(bands_path), '--out', str(out_path), '--updated-by', UPDATED_BY, *(['--restore-verbatim'] if restore_verbatim else [])]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=str(REPO))
    lines = [line for line in proc.stdout.strip().splitlines() if line.startswith('{')]
    try:
        stats = json.loads(lines[-1])
    except (IndexError, ValueError):
        raise Refusal('carry_failed', (proc.stderr or proc.stdout)[-2000:]) from None
    if proc.returncode != 0 or not stats.get('ok'):
        raise Refusal(stats.get('reason', 'carry_failed'), '; '.join(stats.get('problems') or []) or proc.stderr[-1000:])
    return stats


def check_carry(saved, carried, plan, new_scene):
    """Python's own reading of the carried review (JS-canonical equality); every finding is a refusal."""
    carried_by_id = {o['id']: o for o in carried['objects']}
    spec_by_band = {s['id']: s for s in plan['specs']}
    replaced_by, retire_of = {}, {}
    for spec in plan['specs']:
        for m in spec['members']:
            replaced_by.setdefault(m, []).append(spec['id'])
        for r in spec['remainder_ids']:
            for m in next(f for f in plan['features'] if f['id'] == r)['replaces']:
                replaced_by.setdefault(m, []).append(r)
        for t in spec['retires']:
            retire_of[t] = spec
    problems = []
    for o in saved['objects']:
        out = carried_by_id.get(o['id'])
        if o['id'] in replaced_by:
            band = spec_by_band[replaced_by[o['id']][0]]
            expected = {**o, 'status': 'superseded', 'geometry_reviewed': False, 'replacement_ids': replaced_by[o['id']]}
            if o['id'] in retire_of:
                expected['notes'] = '\n'.join(x for x in (o.get('notes'), retire_of[o['id']]['retire_note']) if x)[:5000]
            if out is None or canonical(out) != canonical(expected):
                problems.append(f"member {o['id']} is not kept as superseded -> {replaced_by[o['id']]}")
            if (o.get('status') in DECIDED or o.get('geometry_reviewed')) and not band['accepted']:
                problems.append(f"reviewer-touched member {o['id']} superseded without an accept")
            continue
        if out is None or canonical(out) != canonical(o):
            problems.append(f"non-member {o['id']} ({o.get('status')}) changed or missing")
    width, height = new_scene.get('image_width'), new_scene.get('image_height')
    for feature in plan['features']:
        if 'derived_from' in feature:
            problems.append(f"feature {feature['id']} carries derived_from in the model layer")
        out = carried_by_id.get(feature['id'])
        if out is None:
            problems.append(f"feature {feature['id']} missing from the review")
            continue
        if list(out.get('derived_from') or []) != list(feature['replaces']):
            problems.append(f"feature {feature['id']} derived_from in the review is not its replaces")
        spec = spec_by_band.get(feature['id'])
        if spec and spec['accepted']:
            decision = ((out.get('provenance') or {}).get('human_decision') or {})
            if out.get('status') != 'confirmed' or out.get('geometry_reviewed') is not True or decision.get('source') != 'human:chat' or not decision.get('sentence'):
                problems.append(f"accepted band {feature['id']} reads {out.get('status')} / {out.get('geometry_reviewed')} / {decision.get('source')}")
        elif out.get('status') != 'candidate' or out.get('geometry_reviewed') is not False:
            problems.append(f"feature {feature['id']} without an accept reads {out.get('status')}")
        issues = app_ring_issues(out['ring'], out.get('holes') or [], width, height, rules=APP_RING_RULES)
        if issues:
            problems.append(f"feature {feature['id']} ring: {'; '.join(issues)}")
    lineage = lineage_issue(carried['objects'])
    if lineage:
        problems.append(f'lineage: {lineage}')
    if carried.get('model_signature') != new_scene['model_signature']:
        problems.append('model_signature')
    if carried.get('revision') != saved.get('revision', 0)+1:
        problems.append(f"revision {carried.get('revision')} != {saved.get('revision', 0)}+1")
    if carried.get('updated_by') != UPDATED_BY:
        problems.append('updated_by')
    carried_groups = {g['id']: g for g in carried.get('groups') or []}
    for g in saved.get('groups') or []:
        if g['id'] not in carried_groups or canonical(carried_groups[g['id']]) != canonical(g):
            problems.append(f"group {g['id']}")
    if len(carried_by_id) != len(carried['objects']):
        problems.append('duplicate ids')
    if len(carried['objects']) != len(saved['objects'])+len(plan['features']):
        problems.append(f"objects {len(carried['objects'])} != saved {len(saved['objects'])} + features {len(plan['features'])}")
    if problems:
        raise Refusal('carry_check_failed', '; '.join(problems[:12]))
    return {'lineage_issue': None, 'statuses_before': L._status_counts(saved['objects']), 'statuses_after': L._status_counts(carried['objects'])}


# --- the app check on a scratch copy --------------------------------------------------------------------------------------

def validate_through_app(root, window_id, latest_bytes, model_files, review_bytes, *, api, token=None):
    """The carried review on a qa_road_ scratch copy through the app: GET reads stale false, POST saves it at revision + 1."""
    scratch_id = f"{SCRATCH_PREFIX}v_{re.sub(r'^(win-|lot-|qa_road_|qa_)', '', window_id)[:8]}_{secrets.token_hex(3)}"
    folder = Path(root)/'data/scenes'/scratch_id
    out = json.loads(review_bytes)
    result = {'scratch_id': scratch_id}
    try:
        (folder/'models').mkdir(parents=True)
        (folder/'latest.json').write_bytes(latest_bytes)
        for name, data in model_files.items():
            (folder/'models'/name).write_bytes(data)
        (folder/'review.json').write_bytes(review_bytes)
        status, got = L._http('GET', f'{api}/api/scenes?id={scratch_id}', token=token)
        result['get'] = {'status': status, 'stale': got.get('stale'), 'review_revision': (got.get('review') or {}).get('revision'),
                         'model_signature': (got.get('model') or {}).get('model_signature'), 'error': got.get('error')}
        if status != 200 or got.get('stale') is not False or (got.get('review') or {}).get('revision') != out['revision']:
            raise Refusal('scratch_get_unexpected', json.dumps(result['get']))
        status, body = L._http('POST', f'{api}/api/scenes', {'id': scratch_id, 'review': out}, token=token)
        saved = body.get('review') or {}
        result['post'] = {'status': status, 'error': body.get('error'), 'saved_revision': saved.get('revision'), 'saved_objects': len(saved.get('objects') or []),
                          'saved_model_signature': saved.get('model_signature')}
        if status != 200:
            raise Refusal('scratch_save_failed', f"POST /api/scenes on {scratch_id} returned {status} {body.get('error') or ''}")
        if saved.get('revision') != out['revision']+1 or saved.get('model_signature') != out['model_signature'] or len(saved.get('objects') or []) != len(out['objects']):
            raise Refusal('scratch_save_unexpected', json.dumps(result['post']))
        after = {o['id']: o for o in saved['objects']}
        result['keys_changed_by_app_save'] = sorted({k for o in out['objects'] for k in set(o) | set(after.get(o['id']) or {})
                                                     if canonical(o.get(k)) != canonical((after.get(o['id']) or {}).get(k))})
        result['statuses_after_app_save'] = L._status_counts(saved['objects'])
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        if folder.exists():
            raise Refusal('scratch_cleanup', f'{scratch_id} still exists')
    return result


# --- scratch copies of a window (qa_road_*: the proofs) --------------------------------------------------------------------

def source_of(root, target_id):
    """The real window a target stands for: itself, or the source a qa_ scratch copy names in its qa_source.json."""
    validate_id(target_id)
    if not target_id.startswith('qa_'):
        return target_id
    marker = Path(root)/'data/scenes'/target_id/SCRATCH_MARKER
    if not marker.exists():
        raise Refusal('scratch_without_source', f'{target_id} has no {SCRATCH_MARKER}')
    source = json.loads(marker.read_text()).get('source_window')
    validate_id(source)
    if source.startswith('qa_'):
        raise Refusal('scratch_without_source', f'{target_id} names another scratch copy')
    return source


def make_scratch(root, source_id, *, revision=None, tag=''):
    """A qa_road_ copy of a window: latest.json and its models file byte-copied; review.json byte-copied, or (revision given) rebuilt
    from the review's history at that revision (its body plus the history before it). Writes only data/scenes/qa_road_*."""
    root = Path(root)
    validate_id(source_id)
    src = root/'data/scenes'/source_id
    scratch_id = f"{SCRATCH_PREFIX}{tag + '_' if tag else ''}{re.sub(r'^(win-|lot-)', '', source_id)[:8]}_{secrets.token_hex(3)}"
    folder = root/'data/scenes'/scratch_id
    latest_bytes = (src/'latest.json').read_bytes()
    review_bytes = (src/'review.json').read_bytes()
    latest, review = json.loads(latest_bytes), json.loads(review_bytes)
    if revision is not None and revision != review.get('revision'):
        history = review.get('history') or []
        body = next((h for h in history if h.get('revision') == revision), None)
        if body is None:
            raise Refusal('revision_not_in_history', f'{source_id} rev {revision}')
        rebuilt = {**body, 'history': [h for h in history if isinstance(h.get('revision'), int) and h['revision'] < revision]}
        new_review_bytes = json.dumps(rebuilt, indent=2).encode()
    else:
        new_review_bytes = review_bytes
    signatures = {latest['model_signature'], json.loads(new_review_bytes).get('model_signature')}
    (folder/'models').mkdir(parents=True)
    try:
        (folder/'latest.json').write_bytes(latest_bytes)
        for sig in signatures:
            if sig and (src/'models'/f'{sig}.json').exists():
                (folder/'models'/f'{sig}.json').write_bytes((src/'models'/f'{sig}.json').read_bytes())
        (folder/'review.json').write_bytes(new_review_bytes)
        (folder/SCRATCH_MARKER).write_text(json.dumps({'source_window': source_id, 'created_by': UPDATED_BY, 'created_at': now_stamp(),
                                                       'review_revision': json.loads(new_review_bytes).get('revision'), 'rebuilt_from_history': new_review_bytes != review_bytes,
                                                       'source_latest_sha256': sha(latest_bytes), 'source_review_sha256': sha(review_bytes)}, indent=1)+'\n')
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return scratch_id


def remove_scratch(root, scratch_id):
    if not scratch_id.startswith(SCRATCH_PREFIX):
        raise Refusal('not_a_scratch_copy', scratch_id)
    folder = Path(root)/'data/scenes'/scratch_id
    shutil.rmtree(folder, ignore_errors=True)
    if folder.exists():
        raise Refusal('scratch_cleanup', f'{scratch_id} still exists')


# --- the promotion of one window ----------------------------------------------------------------------------------------

def promote(target_id, *, bands, marks=None, root=Path('.'), run_dir=RUN_DIR, api='http://localhost:3008', token=None, registry=REGISTRY,
            lot_tags_report=LOT_TAGS_REPORT, lot_tags=True, scratch=True, restore_verbatim=True, dry_run=False, interval=None,
            log=lambda text: print(text, file=sys.stderr, flush=True)):
    """Apply `bands` (the selected bands of this target's source window) to `target_id`; marks = read_marks() or None (no accepts)."""
    root, run_dir = Path(root), Path(run_dir)
    validate_id(target_id)
    source = source_of(root, target_id)
    real = not target_id.startswith('qa_')
    if marks and marks.get('simulated') and real and not dry_run:
        raise Refusal('simulated_marks_on_real_window', f"{marks['path']} is a SIMULATED answer; it may only reach qa_ scratch copies and dry runs")
    bands = [b for b in bands if b['window'] == source]
    if not bands:
        raise Refusal('no_bands_for_window', source)
    accepted = {n for n, v in ((marks or {}).get('marks') or {}).items() if v == 'right'}
    folder = root/'data/scenes'/target_id
    out_dir = run_dir/target_id
    record = {'version': VERSION, 'window_scene_id': target_id, 'source_window': source, 'real_window': real, 'started_at': now_stamp(), 'dry_run': dry_run,
              'outcome': None, 'bands_selected': [b['number'] for b in bands], 'accepted': sorted(accepted & {b['number'] for b in bands}),
              'marks': {k: (marks or {}).get(k) for k in ('path', 'sha256', 'at', 'at_field', 'sentence', 'transcribed_by', 'simulated')} if marks else None,
              'carry_script_sha256': L._file_sha(NODE_CARRY), 'module_sha256': L._file_sha(Path(__file__)), 'restore_verbatim': restore_verbatim,
              'model_layer_lineage': ['replaces']}
    if not (folder/'latest.json').exists():
        raise Refusal('no_latest', f'{target_id} has no latest.json')
    if not (folder/'review.json').exists():
        raise Refusal('no_review', f'{target_id} has no review.json; bands are written onto a reviewed window only')
    if (folder/'review.lock').exists():
        raise Refusal('review_locked', f'{target_id} is saving in the app (review.lock present); nothing written')
    latest_bytes = (folder/'latest.json').read_bytes()
    latest = json.loads(latest_bytes)
    if latest.get('id') != source:
        raise Refusal('id_mismatch', f"latest.json id is {latest.get('id')!r}, expected {source!r}")
    if not L.HEX64.match(latest.get('model_signature') or ''):
        raise Refusal('invalid_signature', target_id)
    if snapshot_bytes(latest) != latest_bytes:
        raise Refusal('latest_not_round_trip', 'latest.json does not round-trip byte-identically through json.dumps(indent=2); refusing to rewrite it')
    previous_sig = latest['model_signature']
    previous_path = folder/'models'/f'{previous_sig}.json'
    if not previous_path.exists():
        raise Refusal('previous_snapshot_missing', f'models/{previous_sig[:12]}.json')
    review_bytes = (folder/'review.json').read_bytes()
    review = json.loads(review_bytes)
    if review.get('model_signature') != previous_sig:
        raise Refusal('review_stale', f"review names {str(review.get('model_signature'))[:12]}, latest is {previous_sig[:12]}; carry it forward first")
    record.update({'previous_model_signature': previous_sig, 'previous_latest_sha256': sha(latest_bytes), 'previous_review_sha256': sha(review_bytes),
                   'review_revision_before': review.get('revision'), 'review_updated_at_before': review.get('updated_at'),
                   'objects_before': len(latest['objects']), 'review_objects_before': len(review['objects'])})
    interval = interval or run_interval(root/PROPOSALS)
    pin, stale = freshness(review, latest, bands, interval)
    plan = plan_window(latest, review, bands, accepted, marks)
    record.update({'pin': pin, 'bands': plan['bands'], 'applied': plan['applied'], 'refused_bands': plan['refused'],
                   'replaced_member_ids': plan['members'], 'features': [{k: f[k] for k in ('id', 'kind', 'replaces')} | {'role': f['road_band']['role']} for f in plan['features']]})
    out_dir.mkdir(parents=True, exist_ok=True)
    if stale:
        record.update({'outcome': 'refused', 'reason': 'stale_proposal', 'detail': '; '.join(stale), 'finished_at': now_stamp()})
        (out_dir/('promote.dry.json' if dry_run else 'promote.refused.json')).write_text(json.dumps(record, indent=1)+'\n')
        raise Refusal('stale_proposal', '; '.join(stale))
    if plan['nothing_to_apply']:
        record.update({'outcome': 'nothing_to_apply', 'finished_at': now_stamp(), 'note': 'every selected band was refused; nothing written'})
        (out_dir/('promote.dry.json' if dry_run else 'promote.json')).write_text(json.dumps(record, indent=1)+'\n')
        log(plan_line(record))
        return record

    scene = L.build_snapshot(latest, plan['features'], plan['members'])
    new_bytes = snapshot_bytes(scene)
    new_sig = scene['model_signature']
    record.update({'new_model_signature': new_sig, 'new_latest_sha256': sha(new_bytes), 'objects_after': len(scene['objects']),
                   'previous_models_file_sha256': L._file_sha(previous_path)})
    (out_dir/'latest.before.json').write_bytes(latest_bytes)
    (out_dir/'review.before.json').write_bytes(review_bytes)
    planned_latest = out_dir/('latest.dry.json' if dry_run else 'latest.after.json')
    planned_latest.write_bytes(new_bytes)
    bands_path = out_dir/('bands.dry.json' if dry_run else 'bands.json')
    bands_path.write_text(json.dumps({'bands': plan['specs']}, indent=1)+'\n')
    planned_review = out_dir/('review.dry.json' if dry_run else 'review.after.json')
    carry = carry_review(previous_path, planned_latest, folder/'review.json', bands_path, planned_review, restore_verbatim=restore_verbatim)
    carried_bytes = planned_review.read_bytes()
    carried = json.loads(carried_bytes)
    carry['python_check'] = check_carry(review, carried, plan, scene)
    record.update({'carry': carry, 'new_review_sha256': sha(carried_bytes), 'review_revision_after': carried['revision'],
                   'review_objects_after': len(carried['objects']), 'restored_verbatim': list(carry.get('restored_verbatim') or [])})
    if dry_run:
        record.update({'outcome': 'dry_run', 'finished_at': now_stamp(), 'note': f'nothing written under data/; planned files under {out_dir}'})
        (out_dir/'promote.dry.json').write_text(json.dumps(record, indent=1)+'\n')
        log(plan_line(record))
        return record

    if scratch:
        record['scratch'] = validate_through_app(root, target_id, new_bytes, {previous_path.name: previous_path.read_bytes(), f'{new_sig}.json': new_bytes},
                                                 carried_bytes, api=api, token=token)
    # --- writes --------------------------------------------------------------------------------------------------------
    if sha((folder/'latest.json').read_bytes()) != record['previous_latest_sha256']:
        raise Refusal('changed_underfoot', 'latest.json changed since it was read; nothing written')
    written_latest = L.write_snapshot(folder, new_bytes, new_sig)
    record['written'] = {'models_file': f'models/{new_sig}.json', 'latest_json_sha256': sha(written_latest)}
    try:
        written_review = L.write_review(folder, carried_bytes, record['previous_review_sha256'], temp_name='review.road-promote.tmp')
    except Refusal:
        temporary = folder/'latest.tmp'
        temporary.write_bytes(latest_bytes)
        temporary.replace(folder/'latest.json')
        record['written']['latest_restored_after_review_refusal'] = True
        raise
    record['written']['review_json_sha256'] = sha(written_review)
    stamp = now_stamp()
    if real:
        row = {'version': L.REGISTRY_VERSION, 'role': ROLE, 'window_scene_id': target_id, 'previous_model_signature': previous_sig, 'new_model_signature': new_sig,
               'previous_latest_sha256': record['previous_latest_sha256'], 'new_latest_sha256': record['new_latest_sha256'],
               'previous_review_sha256': record['previous_review_sha256'], 'new_review_sha256': record['new_review_sha256'],
               'review_revision_before': record['review_revision_before'], 'review_revision_after': record['review_revision_after'],
               'bands': [{k: r[k] for k in ('number', 'id', 'accepted', 'members', 'retire_targets', 'remainder_ids', 'width_m', 'area_m2', 'vertices')}
                         | {'kept_outside_member': [k['id'] for k in r['kept_outside_member']], 'coverage': {k: (r['coverage'] or {}).get(k) for k in
                                                    ('remainder_m2', 'absorbed_m2', 'uncovered_after_m2', 'members_outside_band_m2')}}
                         for r in plan['bands'] if not r['refused']],
               'replaced_member_ids': plan['members'], 'restored_verbatim': record['restored_verbatim'], 'model_layer_lineage': ['replaces'],
               'design': SOURCE, 'pin': pin, 'marks': record['marks'], 'promote_record': (out_dir/'promote.json').as_posix(), 'written_at': stamp}
        line = json.dumps(row, sort_keys=True)+'\n'
        registry = Path(registry)
        registry.parent.mkdir(parents=True, exist_ok=True)
        with registry.open('a') as handle:
            handle.write(line)
        record['registry'] = {'path': str(registry), 'row': row, 'line_sha256': sha(line.encode())}
        if lot_tags:
            report = Path(lot_tags_report.format(id=target_id, date=datetime.now().strftime('%Y-%m-%d')))
            record['lot_tags'] = L.run_lot_tags(root, target_id, report)
            record['lot_tags']['report'] = str(report)
    else:
        record['registry'] = None
        record['lot_tags'] = {'skipped': 'qa_ scratch target: no registry row, no lot-tags re-key'}
    record.update({'outcome': 'promoted', 'finished_at': stamp})
    (out_dir/'promote.json').write_text(json.dumps(record, indent=1)+'\n')
    log(plan_line(record)+(f"; registry row {ROLE}; lot tags written {(record.get('lot_tags') or {}).get('written')}" if real else '; scratch target'))
    return record


def plan_line(record):
    """One line per window: outcome, each band = its members (accept, retire, remainder), signatures, revisions, restored ids."""
    wid, outcome = record.get('window_scene_id'), record.get('outcome')
    bands = record.get('bands') or []
    refused = '; '.join(f"#{r['number']} {r['id']} refused {','.join(sorted({x[0] for x in r['refused']}))}" for r in bands if r['refused'])
    if outcome == 'refused':
        return f"REFUSED {wid}: {record.get('reason')}: {record.get('detail')}" + (f" | band refusals: {refused}" if refused else '')
    if outcome in ('skipped', 'blocked', 'failed'):
        return f"{outcome.upper()} {wid}: {record.get('reason')}" + (f": {record['detail']}" if record.get('detail') else '')
    if outcome == 'nothing_to_apply':
        return f"NOTHING TO APPLY {wid}: {refused}; nothing written"
    applied = '; '.join(f"#{r['number']} {r['id']} {'ACCEPTED' if r['accepted'] else 'candidate'} supersedes {'+'.join(r['members'])}"
                        + (f" retires {'+'.join(r['retire_targets'])}" if r['retire_targets'] else '')
                        + (f" remainder {'+'.join(r['remainder_ids'])}" if r['remainder_ids'] else '')
                        + (f" kept-outside {'+'.join(k['id'] for k in r.get('kept_outside_member') or [])}" if r.get('kept_outside_member') else '')
                        + (f" coverage remainder {len(cov['remainders'])} ({cov['remainder_m2']} m2) absorbed {cov['absorbed_m2']} m2"
                           if (cov := r.get('coverage')) else '')
                        + f" ({r['width_m']} m wide, {r['area_m2']} m2, {r['vertices']} v)" for r in bands if not r['refused'])
    label = {'dry_run': 'PLAN', 'promoted': 'PROMOTED', 'rolled_back': 'ROLLED BACK'}.get(outcome, str(outcome).upper())
    restored = record.get('restored_verbatim') or []
    return (f"{label} {wid}: {sum(1 for r in bands if not r['refused'])} band(s): {applied}" + (f" | refused: {refused}" if refused else '')
            + f"; model {str(record.get('previous_model_signature'))[:12]} -> {str(record.get('new_model_signature'))[:12]}; "
            f"review rev {record.get('review_revision_before')} -> {record.get('review_revision_after')} (pinned {((record.get('pin') or {}).get('revision'))}); "
            f"restored verbatim {len(restored)}")


def rollback(window_id, *, root=Path('.'), run_dir=RUN_DIR, log=lambda text: print(text, file=sys.stderr, flush=True)):
    """Restore latest.json and review.json from the byte copies and remove the registry row this promotion wrote (a real window).

    models/<new signature>.json stays (content-addressed, never deleted) and the lot-tags-v1 rows stay under the new signature
    (re-run scene_lot_tags --write); both documented in rollback.json. Refuses when the window changed since the promotion."""
    root, run_dir = Path(root), Path(run_dir)
    validate_id(window_id)
    out_dir = run_dir/window_id
    record_path = out_dir/'promote.json'
    if not record_path.exists():
        raise Refusal('no_promotion_record', str(record_path))
    record = json.loads(record_path.read_text())
    if record.get('outcome') != 'promoted':
        raise Refusal('not_promoted', f"outcome {record.get('outcome')!r}")
    if record.get('rolled_back_at'):
        raise Refusal('already_rolled_back', record['rolled_back_at'])
    folder = root/'data/scenes'/window_id
    if (folder/'review.lock').exists():
        raise Refusal('review_locked', window_id)
    if L._file_sha(folder/'latest.json') != record['new_latest_sha256']:
        raise Refusal('changed_since_promotion', 'latest.json is not the promoted bytes')
    if L._file_sha(folder/'review.json') != record['new_review_sha256']:
        raise Refusal('changed_since_promotion', 'review.json is not the promoted bytes (the reviewer saved?)')
    before_latest, before_review = (out_dir/'latest.before.json').read_bytes(), (out_dir/'review.before.json').read_bytes()
    if sha(before_latest) != record['previous_latest_sha256'] or sha(before_review) != record['previous_review_sha256']:
        raise Refusal('byte_copy_mismatch', 'latest.before.json / review.before.json')
    registry, lines, matches = None, [], []
    if record.get('registry'):
        registry = Path(record['registry']['path'])
        lines = registry.read_text().splitlines(keepends=True) if registry.exists() else []
        matches = [i for i, line in enumerate(lines) if sha(line.encode()) == record['registry']['line_sha256']]
        if len(matches) != 1:
            raise Refusal('registry_row_not_found_once', f'{len(matches)} matching line(s) in {registry}')
    keep = out_dir/'rollback'
    keep.mkdir(exist_ok=True)
    (keep/'latest.rolled-back.json').write_bytes((folder/'latest.json').read_bytes())
    (keep/'review.rolled-back.json').write_bytes((folder/'review.json').read_bytes())
    temporary = folder/'latest.tmp'
    temporary.write_bytes(before_latest)
    temporary.replace(folder/'latest.json')
    L.write_review(folder, before_review, record['new_review_sha256'], temp_name='review.road-rollback.tmp')
    if registry is not None:
        (keep/'registry.before.jsonl').write_bytes(registry.read_bytes())
        del lines[matches[0]]
        registry_tmp = registry.with_suffix('.jsonl.tmp')
        registry_tmp.write_text(''.join(lines))
        registry_tmp.replace(registry)
    stamp = now_stamp()
    result = {'version': VERSION, 'window_scene_id': window_id, 'rolled_back_at': stamp,
              'restored': {'latest_json_sha256': L._file_sha(folder/'latest.json'), 'review_json_sha256': L._file_sha(folder/'review.json')},
              'registry_row_removed': record['registry']['line_sha256'] if registry is not None else None, 'registry_path': str(registry) if registry else None,
              'stays': {'models_file': record['written']['models_file'], 'note': 'models/<new signature>.json stays (content-addressed, never deleted); '
                        'the lot-tags-v1 rows stay keyed by the new signature until `scene_lot_tags --write` re-keys them'}}
    if result['restored']['latest_json_sha256'] != record['previous_latest_sha256'] or result['restored']['review_json_sha256'] != record['previous_review_sha256']:
        raise Refusal('rollback_verify_failed', json.dumps(result['restored']))
    record['rolled_back_at'] = stamp
    record_path.write_text(json.dumps(record, indent=1)+'\n')
    (out_dir/'rollback.json').write_text(json.dumps(result, indent=1)+'\n')
    log(f"ROLLED BACK {window_id}: latest.json and review.json restored" + (', registry row removed' if registry else '') + f"; {result['stays']['models_file']} stays")
    return result


# --- the batch: one window at a time, the counter identical after each one ---------------------------------------------

def program_written(registry):
    """{window: {sha256 of every review.json this program wrote}}, from the registry's road_band rows."""
    out = {}
    path = Path(registry)
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get('role') == ROLE and row.get('new_review_sha256') and row.get('window_scene_id'):
                out.setdefault(row['window_scene_id'], set()).add(row['new_review_sha256'])
    return out


def windows_of(bands):
    """The windows of the selected bands, in band-number order, each once."""
    return list(dict.fromkeys(b['window'] for b in sorted(bands, key=lambda b: b['number'])))


SKIP_REASONS = ('no_latest', 'no_review', 'review_stale', 'stale_proposal', 'pin_ambiguous', 'pin_unknown', 'nothing_to_apply')


def preflight(target_id, *, bands, marks=None, root=Path('.'), interval=None):
    """(reason, detail) when the batch skips a window - no latest / review, a review naming another signature, a stale proposal, every
    selected band refused - else None; reads only. Any other Refusal propagates (a failure, not a skip)."""
    root = Path(root)
    source = source_of(root, target_id)
    folder = root/'data/scenes'/target_id
    if not (folder/'latest.json').exists():
        return 'no_latest', f'{target_id} has no latest.json'
    if not (folder/'review.json').exists():
        return 'no_review', f'{target_id} has no review.json'
    latest, review = json.loads((folder/'latest.json').read_text()), json.loads((folder/'review.json').read_text())
    if review.get('model_signature') != latest.get('model_signature'):
        return 'review_stale', f"review names {str(review.get('model_signature'))[:12]}, latest is {str(latest.get('model_signature'))[:12]}"
    mine = [b for b in bands if b['window'] == source]
    try:
        _, stale = freshness(review, latest, mine, interval or run_interval(root/PROPOSALS))
    except Refusal as refusal:
        if refusal.reason in SKIP_REASONS:
            return refusal.reason, refusal.detail
        raise
    if stale:
        return 'stale_proposal', '; '.join(stale)
    accepted = {n for n, v in ((marks or {}).get('marks') or {}).items() if v == 'right'}
    plan = plan_window(latest, review, mine, accepted, marks)
    if plan['nothing_to_apply']:
        return 'nothing_to_apply', '; '.join(f"#{r['number']} {','.join(sorted({x[0] for x in r['refused']}))}" for r in plan['bands'])
    return None


def batch(targets, bands, *, marks=None, root=Path('.'), run_dir=RUN_DIR, batch_dir=None, counter=L.run_counter, quiet_since=None, continue_on_error=False,
          registry=REGISTRY, lot_tags=True, lot_tags_report=LOT_TAGS_REPORT, interval=None, log=lambda text: print(text, file=sys.stderr, flush=True), **promote_kwargs):
    """Promote `targets` (window ids; qa_road_ copies stand for their source) one at a time with `bands`; the counter's six fields
    must be identical after each window to the reading just before it, else that window is rolled back (lot tags re-keyed) and the
    batch stops. A preflight skip (stale proposal, every band refused, ...) is reported, not failed, and reads no counter; any other
    refusal stops the batch unless continue_on_error."""
    root, run_dir = Path(root), Path(run_dir)
    batch_dir = Path(batch_dir) if batch_dir else run_dir/'batch'
    batch_dir.mkdir(parents=True, exist_ok=True)
    ids = [validate_id(t) for t in targets]
    summary = {'version': VERSION, 'started_at': now_stamp(), 'ids': ids, 'bands': [b['number'] for b in bands], 'marks': (marks or {}).get('path'), 'counter_fields': list(L.COUNTER_FIELDS),
               'hop_label_ignored': L.COUNTER_HOP_LABEL, 'quiet_since': quiet_since.strftime('%Y-%m-%dT%H:%M:%SZ') if quiet_since else None,
               'promoted': [], 'skipped': [], 'failed': [], 'rolled_back': [], 'stopped_at': None, 'stop_reason': None, 'windows': [],
               'batch_dir': str(batch_dir), 'run_dir': str(run_dir)}
    own = set()
    written = program_written(registry) if quiet_since is not None else {}
    silent = lambda _text: None      # noqa: E731

    def record_window(entry):
        entry['finished_at'] = now_stamp()
        (batch_dir/f"{entry['window_scene_id']}.json").write_text(json.dumps(entry, indent=1)+'\n')
        summary['windows'].append({k: entry.get(k) for k in ('window_scene_id', 'outcome', 'reason', 'detail', 'counter_identical', 'differing_fields',
                                                              'new_model_signature', 'review_revision_after', 'applied', 'restored_verbatim')})
        log(plan_line(entry) if entry['outcome'] not in ('promoted', 'rolled_back') else
            f"{plan_line(entry)}; counter {'identical' if entry.get('counter_identical') else 'DIFFERS ' + ','.join(entry.get('differing_fields') or [])}")

    def finish(stopped_at=None, reason=None):
        summary.update({'stopped_at': stopped_at, 'stop_reason': reason, 'finished_at': now_stamp()})
        (batch_dir/'summary.json').write_text(json.dumps(summary, indent=1)+'\n')
        log(f"BATCH {'stopped at '+stopped_at+' ('+str(reason)+')' if stopped_at else 'complete'}: promoted {len(summary['promoted'])}, skipped {len(summary['skipped'])}, "
            f"failed {len(summary['failed'])}, rolled back {len(summary['rolled_back'])} of {len(ids)}; {batch_dir/'summary.json'}")
        return summary

    def reading(window_id, name):
        data = counter()
        path = batch_dir/f'{window_id}.counter.{name}.json'
        path.write_text(json.dumps(data, indent=1)+'\n')
        return data, str(path)

    for window_id in ids:
        entry = {'version': VERSION, 'window_scene_id': window_id, 'started_at': now_stamp(), 'outcome': None}
        if quiet_since is not None:
            intruders = L.quiet_intruders(root, quiet_since, own, written)
            if intruders:
                entry.update({'outcome': 'blocked', 'reason': 'quiet_window_broken', 'detail': ', '.join(f"{i['window_scene_id']} {i['mtime']}" for i in intruders), 'intruders': intruders})
                record_window(entry)
                return finish(window_id, 'quiet_window_broken')
        try:
            skip = preflight(window_id, bands=bands, marks=marks, root=root, interval=interval)
            if skip:
                entry.update({'outcome': 'skipped', 'reason': skip[0], 'detail': skip[1]})
                summary['skipped'].append(window_id)
                record_window(entry)
                continue
            before, entry['counter_before'] = reading(window_id, 'before')
            record = promote(window_id, bands=bands, marks=marks, root=root, run_dir=run_dir, registry=registry, lot_tags=lot_tags, lot_tags_report=lot_tags_report,
                             interval=interval, log=silent, **promote_kwargs)
        except Refusal as refusal:
            entry.update({'outcome': 'failed', 'reason': refusal.reason, 'detail': refusal.detail, 'stage': 'promote'})
            summary['failed'].append(window_id)
            record_window(entry)
            if continue_on_error:
                continue
            return finish(window_id, f"refused: {entry['reason']}")
        entry.update({k: record.get(k) for k in ('previous_model_signature', 'new_model_signature', 'review_revision_before', 'review_revision_after',
                                                 'bands', 'applied', 'replaced_member_ids', 'restored_verbatim', 'pin')})
        entry['promote_record'] = str(run_dir/window_id/'promote.json')
        if record['outcome'] != 'promoted':
            entry.update({'outcome': 'skipped', 'reason': record['outcome']})
            summary['skipped'].append(window_id)
            record_window(entry)
            continue
        after, entry['counter_after'] = reading(window_id, 'after')
        comparison = L.compare_counters(before, after)
        entry.update({'counter': comparison, 'counter_identical': comparison['identical'], 'differing_fields': comparison['differing_fields']})
        if comparison['identical']:
            entry['outcome'] = 'promoted'
            own.add(window_id)
            summary['promoted'].append(window_id)
            record_window(entry)
            continue
        entry['outcome'] = 'rolled_back'
        try:
            entry['rollback'] = rollback(window_id, root=root, run_dir=run_dir, log=silent)
            if lot_tags:
                entry['rollback']['lot_tags_rekeyed'] = L.run_lot_tags(root, window_id, Path(lot_tags_report.format(id=window_id, date=datetime.now().strftime('%Y-%m-%d'))))
            restored, entry['counter_after_rollback'] = reading(window_id, 'after-rollback')
            entry['counter_restored_to_before'] = L.compare_counters(before, restored)['identical']
        except Refusal as refusal:
            entry['rollback'] = {'outcome': 'refused', 'reason': refusal.reason, 'detail': refusal.detail}
        summary['rolled_back'].append(window_id)
        record_window(entry)
        return finish(window_id, f"counter differs: {','.join(comparison['differing_fields'])}")
    return finish()


# --- the command line ----------------------------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--marks', help='the reviewer\'s answer: {"marks": {"<n>": "right|wrong|unclear"}, "sentence": <chat line>, "at": <ISO>}')
    parser.add_argument('--bands', nargs='+', help='band numbers (MARKS-roads-v31.md) or band ids; without --marks they go in WITHOUT an accept')
    parser.add_argument('--windows', nargs='+', help='restrict to these windows (qa_road_ scratch copies stand for the window in their qa_source.json)')
    parser.add_argument('--root', default='.')
    parser.add_argument('--run-dir', default=str(RUN_DIR))
    parser.add_argument('--api', default='http://localhost:3008')
    parser.add_argument('--token-file')
    parser.add_argument('--registry', default=str(REGISTRY))
    parser.add_argument('--lot-tags-report', default=LOT_TAGS_REPORT)
    parser.add_argument('--no-lot-tags', action='store_true')
    parser.add_argument('--no-scratch', action='store_true', help='skip the qa_road_ scratch validation through the app')
    parser.add_argument('--no-restore-verbatim', action='store_true')
    parser.add_argument('--proposals', default=None, help=f'the run directory (default {PROPOSALS})')
    parser.add_argument('--marks-sheet', default=None, help=f'the numbered sheet (default {MARKS_SHEET})')
    parser.add_argument('--run-started', default=RUN_STARTED, help='when the proposal run started reading windows (the freshness pin)')
    parser.add_argument('--dry-run', action='store_true', help='plan + carry, one line per window; writes under --run-dir only')
    parser.add_argument('--batch', action='store_true', help='promote one window at a time with the six-field counter guard')
    parser.add_argument('--batch-dir', default=None)
    parser.add_argument('--quiet-since', metavar='ISO')
    parser.add_argument('--rollback', metavar='ID')
    parser.add_argument('--continue', dest='continue_on_error', action='store_true')
    args = parser.parse_args(argv)
    root, run_dir = Path(args.root), Path(args.run_dir)
    token = Path(args.token_file).read_text().strip() if args.token_file else None
    if args.rollback:
        try:
            result = rollback(args.rollback, root=root, run_dir=run_dir)
        except Refusal as refusal:
            print(json.dumps({'window_scene_id': args.rollback, 'outcome': 'refused', 'reason': refusal.reason, 'detail': refusal.detail}, indent=1))
            return 1
        print(json.dumps(result, indent=1))
        return 0
    try:
        proposals = Path(args.proposals) if args.proposals else root/PROPOSALS
        all_bands = load_bands(root, proposals=proposals, sheet=Path(args.marks_sheet) if args.marks_sheet else None)
        marks = read_marks(args.marks) if args.marks else None
        bands, accepted, not_selected = select_bands(all_bands, wanted=args.bands, marks=marks)
        interval = run_interval(proposals, args.run_started)
    except Refusal as refusal:
        print(json.dumps({'outcome': 'refused', 'reason': refusal.reason, 'detail': refusal.detail}, indent=1))
        return 1
    if not bands:
        print(json.dumps({'outcome': 'nothing_selected', 'not_selected': not_selected}, indent=1))
        return 0
    targets = []
    for window_id in args.windows or windows_of(bands):
        try:
            source = source_of(root, window_id)
        except Refusal as refusal:
            print(json.dumps({'window_scene_id': window_id, 'outcome': 'refused', 'reason': refusal.reason, 'detail': refusal.detail}))
            return 1
        if any(b['window'] == source for b in bands):
            targets.append(window_id)
    common = {'root': root, 'api': args.api, 'token': token, 'registry': Path(args.registry), 'lot_tags_report': args.lot_tags_report, 'lot_tags': not args.no_lot_tags,
              'scratch': not args.no_scratch, 'restore_verbatim': not args.no_restore_verbatim, 'interval': interval}
    if args.batch and not args.dry_run:
        try:
            summary = batch(targets, bands, marks=marks, run_dir=run_dir,
                            batch_dir=Path(args.batch_dir) if args.batch_dir else None, quiet_since=L._parse_since(args.quiet_since) if args.quiet_since else None,
                            continue_on_error=args.continue_on_error, **common)
        except Refusal as refusal:
            print(json.dumps({'outcome': 'refused', 'reason': refusal.reason, 'detail': refusal.detail}, indent=1))
            return 1
        print(json.dumps({k: summary.get(k) for k in ('promoted', 'skipped', 'failed', 'rolled_back', 'stopped_at', 'stop_reason', 'batch_dir')}, indent=1))
        return 1 if summary['stopped_at'] or summary['failed'] else 0
    results, failed, lines = [], 0, []
    run_dir.mkdir(parents=True, exist_ok=True)
    for target in targets:
        try:
            record = promote(target, bands=bands, marks=marks, run_dir=run_dir, dry_run=args.dry_run, log=lambda _: None, **common)
        except Refusal as refusal:
            failed += 1
            record = {'window_scene_id': target, 'outcome': 'refused', 'reason': refusal.reason, 'detail': refusal.detail}
            saved = run_dir/target/('promote.dry.json' if args.dry_run else 'promote.refused.json')
            if saved.exists():
                record = {**json.loads(saved.read_text()), **record}
        results.append(record)
        line = plan_line(record)
        lines.append(line)
        print(line, flush=True)
        if args.dry_run:
            (run_dir/f'{target}.json').write_text(json.dumps(summary_of(record), indent=1)+'\n')
        elif record['outcome'] == 'refused' and not args.continue_on_error:
            break
    if args.dry_run:
        (run_dir/'plan.txt').write_text('\n'.join(lines)+'\n')
        (run_dir/'not-selected.json').write_text(json.dumps(not_selected, indent=1)+'\n')
        return 1 if failed else 0
    print(json.dumps([summary_of(r) for r in results], indent=1))
    return 1 if failed else 0


def summary_of(record):
    """The per-window JSON of a dry run: members, supersedes, retire targets, refusals."""
    return {'window_scene_id': record.get('window_scene_id'), 'source_window': record.get('source_window'), 'outcome': record.get('outcome'),
            'reason': record.get('reason'), 'detail': record.get('detail'), 'pin': record.get('pin'),
            'review_revision_before': record.get('review_revision_before'), 'review_revision_after': record.get('review_revision_after'),
            'previous_model_signature': record.get('previous_model_signature'), 'new_model_signature': record.get('new_model_signature'),
            'bands': record.get('bands'), 'applied': record.get('applied'), 'refused_bands': record.get('refused_bands'),
            'replaced_member_ids': record.get('replaced_member_ids'), 'features': record.get('features'),
            'restored_verbatim': record.get('restored_verbatim'), 'carry': {k: (record.get('carry') or {}).get(k) for k in
                                                                            ('members_untouched', 'members_lineage_only', 'members_inherited', 'members_decided_accepted', 'retired', 'kept_identical', 'restored_verbatim')}
            if record.get('carry') else None}


if __name__ == '__main__':
    raise SystemExit(main())
