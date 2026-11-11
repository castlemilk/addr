"""Human precision judge from rows, round 5 (sam3-mac-human-precision-v1).

Re-reads the prediction rows reference_eval wrote for the round-3/4 run dirs (reference-eval-{v2,v1,v2val}/predictions.jsonl,
collected at 0.2) against the reviewer's own small-roof decisions in data/scenes/<scene>/review.json, joined to the frozen
dataset split the rows were judged on. No inference runs and reference_eval.py is not edited: its sha256 is the recorded judge
version. Every figure is agreement between model estimates and human decisions or references (a count of decisions or an
agreement share), never a calibrated rate; a review the assistant wrote is an observation, never a human decision; hints are
never labels; nothing is promoted on any outcome. The rules are pre-registered in round5/RULES.json (human_precision).

Population: production small detections (latest.json source not reviewer*) whose frozen-snapshot area is under 25 m2, decided
by the reviewer (confirmed = roof, rejected = not roof) in the UI or by chat transcription, joined by P (the machine_pseudo row
of the judged tile with object_id = the latest.json id) or R (a reference whose object_id is that id). Human-drawn references
(source reviewer*) are a separate recall stratum; agent-authored reviews (notes carrying "Assistant visually traced" /
"Assistant image review") are excluded from both until the reviewer's own marked decision opens the notes or, for a draft that
task pull landed as fieldwork_import.pending, the reviewer's own later ordinary save accepted it (pending false with accepted_at set
and accepted_by the local app principal 'local', written only by feedback.ts acceptPendingFeedback from the save route's principal;
confirmed counts as roof only with geometry_reviewed true, as the acceptance itself required; RULES
human_precision.agent_fresh_decision, amendments 2026-10-01), and an outline the assistant drew never enters the human-drawn stratum. A share is read
only when N >= 30 with >= 8 per class across >= 6 scenes; below that the block prints n/a and the per-object table (best IoU and
max detecting score per arm, survival over 0.45..0.80).

Lineage (frame-cut recapture, design .run/focus-recapture-design-2026-10-01.md section 5): object ids hash the image, so a decision
on a recaptured window scene reaches the frozen detection only through the copy's carried_from. For a review object with a
non-empty carried_from the frozen key is (carried_from.scene_id, carried_from.object_id): P/R path, snapshot area (the 25 m2 gate),
the 4 m2 floor, live, production and source are read at that key in the SOURCE scene; class, provenance kind, stratum, fresh state,
revision and notes from the copy's own review object. The hop needs the window's latest.json frame_transform verified (frame-
transform-v1, georeferenced-affine) under a recorded DESIGN.json sha256 the current file accepts (design_chain: its current sha, or
the previous_sha256 of a dated amendment reached from the newest entry through thresholds_changed-false entries; a sha behind a
THRESHOLD change or unknown is carry_design_mismatch), carried_from.model_signature equal to the
transform's source snapshot whose source_image_sha256 equals the frozen split's image for that scene (the live image-sha rule met
through the lineage), and a verified forward-pointer row in data/reviews/eyes/recapture/windows-*.jsonl (role carried_source,
frame_transform_verified true, design_sha256 accepted the same way, not withdrawn; anything less is
carry_unregistered, read independently of the window's own transform); a failed precondition is listed by name (carry_unverified,
carry_design_mismatch, carry_snapshot_mismatch, carry_unregistered, carry_method) and the row is unjoined. A carried copy the
reviewer re-kinded (kind not building) is emitted as cls skipped with its lineage: it counts nothing, but it is a copy, so the
copies tally, join() and targets() read the same rows.
Exactly one decision per frozen key: a counted source decision wins and every copy decision is listed
duplicate_decision_on_recapture; copies in two scenes or twice in one count nowhere (ambiguous_carry). No geometric match ever
joins a decision. targets() asks a recaptured key once, as its copy on the window scene (recaptured:<window>).

Takeover (reviewer direction 2026-10-02: "from these images, its all still happening, the orange bounding box cuts off so the
object boundary looks bad"; WINDOW.json TAKEOVER RULE, mirrored in RULES.json amendments of 2026-10-02): on a window scene a
carried copy C that a building detection D of the window's current model took over reads status superseded with
replacement_ids [D.id] and keeps carried_from; D.replaces lists the copy ids it took over and D stays candidate until the
reviewer decides it. The counter follows exactly ONE replaces hop, D.replaces -> copy C -> C.carried_from -> frozen key, and
only when the copy's own carry hop holds (the five refusals above apply unchanged, read on the copy): D is emitted once per
frozen key it covers, with the P/R path, snapshot area, live, production and source read at that key in the source scene and
class, provenance kind, stratum, fresh state, revision and notes read from D's own review object by the ordinary rules (D
carries no agent text, so no marker is needed; when it does, the marker and accepted-import rules govern as for any object).
The source decision still wins (duplicate_decision_on_recapture); a copy listed in two detections' replaces counts nowhere
(ambiguous_takeover on every claimant's row); the taken-over copy is not a decision and not a target (stratum and target
exclusion superseded_by_takeover) and D is asked instead, once, listing the frozen keys it covers. A link that does not hold
(the named copy missing, not a carried copy, not superseded, or a replacer that is itself a carried copy) is recorded on D's
row (replaces_refused) and followed nowhere: copies and detections then read exactly as before this hop existed. The hop reads
D.replaces alone, whatever the copy's replacement_ids says (a held link the copy does not name back is only tallied
replacement_mismatch); D.derived_from mirrors D.replaces (the editor's save validator requires it) and is never read.

Body carry (reviewer "yes to defaults" 2026-10-06 on the slice-body census card, item 2; WINDOW.json takeover_rule amendment of
2026-10-06, mirrored in RULES join_order / targets): the replacer D may be a carried copy of role whole_roof_body (scene_focus
body-carry: the whole roof carried from a neighbouring frame through a verified transform) when exactly one registry row of role
body_carry names (window, D.id) with frame_transform_verified true and a residual inside DESIGN (shift_m <= max_shift_m, ncc >=
min_ncc), D.replaces is exactly [slice], the slice reads status superseded with replacement_ids [D.id], is not decided
(copy_decided: a decided copy is never superseded) and is not itself a body or a replacer (two_hops: one hop, never two). Then D
is read exactly as the window's own detection: emitted once on the slice's hop (its frozen key, refusals and copies count) with
class, provenance kind, stratum, fresh state, revision and notes from D's own review object, and asked as the target for the
slice's frozen key; the source decision still wins; a copy two replacers name is ambiguous_takeover. D's own carried_from hop (to
the body's source object) is still listed as a carried row and refused as before (no carried_source row names it). A body link
that does not hold is recorded by name on D's row (replaces_refused: copy_decided, copy_not_reciprocal, body_replaces_not_single,
two_hops, body_unregistered, body_registered_twice, body_residual_outside_design beside the four above) and followed nowhere; a
carried replacer of any other role is still replacer_carried. A refused body link is never a silent drop of the slice's key:
targets() asks the SLICE again for its frozen key, exactly as before the body (the item names the body and the refusal in
body_refused; the count's lineage tallies body_carry_slices_restored), so targets_remaining reads as with no body at all. The
RULES join_order / targets text of 2026-10-06T00:12:32Z still says "the key asked nowhere until repaired by hand" for this case;
its dated amendment is pending (housekeeping 2026-10-06 after the rollout skeptic); no live body link is refused.

Two fact columns (RULES population_primary since the amendment of 2026-10-01T06:03:14Z: "a window-scene object without
carried_from is listed decision_on_body_not_target when it covers at least 0.8 of a re-projected frozen slice's own area, and
lineage_agreement on the carried copy records whether that whole-roof decision agrees; both are fact columns"), computed by
lineage_facts() after the read pass and recorded on the row, never a filter, a class or a count: decision_on_body_not_target is
True on a window-scene building object without carried_from whose ring covers at least BODY_COVER of a re-projected frozen slice's
own area (the slice: the source scene's live ring at the copy's frozen key re-projected into the window through its latest.json
frame_transform by scene_recapture.project_object, the arithmetic that wrote the copy; the copy's own saved ring only when the
source is not live, named slice_ring copy_ring), every such slice and cover listed in body_covers; lineage_agreement on the
carried copy is whether the decisions in the slice's lineage agree in class: the slice's own decision (the copy's class, or the
class of the single detection that took it over) against the SOURCE scene's review object at the frozen key and against every
covering body, True / False over the decided pairs, None when nothing decided is compared, the compared statuses in
lineage_agreement_detail. Neither enters population(), classify(), join(), targets() or any share; the block facts rows carry
them beside all seven facts_columns_not_filters (FACTS_COLUMNS), and count / rejudge list them under lineage_facts.

  scripts/ml/.venv/bin/python -m scripts.ml.training.sam3_mac.r5_precision count --json --block v2
  scripts/ml/.venv/bin/python -m scripts.ml.training.sam3_mac.r5_precision guide --block v2 --out <md> --set <json>
  scripts/ml/.venv/bin/python -m scripts.ml.training.sam3_mac.r5_precision rejudge --root <round3> --root <round4> \\
      --rules <round5/RULES.json> --out <round5>
  scripts/ml/.venv/bin/python -m scripts.ml.training.sam3_mac.r5_precision report --ledger <round5/human-precision.jsonl> \\
      --out <round5/HUMAN-PRECISION.md>

count and guide only read; rejudge writes only under --out (block files, an append-only ledger and batches/<batch>/ holding the
batch's block files, the RULES bytes it read, the module bytes it ran and its report, so an earlier batch stays verifiable after
a later one) and refuses
to finish when any summary.json / predictions.jsonl / context.json of a run dir changed while it read them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from shapely.geometry import Polygon

from ... import design_chain, review_authority, scene_confidence, scene_recapture
from ...scene_self_review import GEOMETRY_ERRORS
from . import r3_postfilter as pf
from . import reference_eval as ev

VERSION = 'sam3-mac-human-precision-v1'
RULES_VERSION = 'sam3-round5-rules-v1'
REPO = Path(__file__).resolve().parents[4]
SAM3 = REPO / 'data/reviews/eyes/training/sam3-mac'
RULES_PATH = SAM3 / 'round5' / 'RULES.json'
SCENES = REPO / 'data/scenes'
COUNT_RUN = SAM3 / 'round4' / 'r4-combo-seed1'   # judged tiles of a block are identical in all nine run dirs (RULES count_context)
PRIOR_SET = REPO / 'data/references/eyes-checkpoints/REVIEW-SET-2026-09-29.json'
SMALL_M2 = 25.
MIN_AREA_M2 = 4.       # dataset export floor
T045 = .45
GRID = tuple(round(.45 + .05 * i, 2) for i in range(8))
N_RULE = {'primary_decided_min': 30, 'per_class_min': 8, 'scenes_min': 6}
BOOT_SEED, BOOT_DRAWS = 20260928, 2000
BLOCKS = {'v2': ('t045', 'matched'), 'v1': ('t045', 'matched'), 'v2val': ('t045',)}
AGENT_TEXT = re.compile(r'Assistant (visually traced|image review)', re.I)    # the assistant's review text, anywhere in the notes
FRESH_MARK = re.compile(r'^\s*\[reviewer\b[^\]]*\]\s*(?P<cls>not[ -](?:a )?roof|roof)\b', re.I)   # the reviewer's own class words first
FRESH_STATUS = {'roof': 'confirmed', 'not_roof': 'rejected'}
LINEAGE_METHOD = 'georeferenced-affine'          # scene_recapture.METHOD: the only carry the hop follows
CARRY_REFUSALS = ('carry_unverified', 'carry_design_mismatch', 'carry_snapshot_mismatch', 'carry_unregistered', 'carry_method')
NOT_COUNTED = ('not_live', 'agent_traced_geometry', 'duplicate_decision_on_recapture', 'ambiguous_carry', 'ambiguous_takeover', 'other')
TAKEOVER_REFUSALS = ('copy_missing', 'copy_not_carried', 'copy_not_superseded', 'replacer_carried',   # a replaces link that is followed nowhere
                     # a carried replacer of role whole_roof_body (body carry, amendment 2026-10-06): the body-link refusals, by name
                     'copy_decided', 'copy_not_reciprocal', 'body_replaces_not_single', 'two_hops', 'body_unregistered',
                     'body_registered_twice', 'body_residual_outside_design')
BODY_ROLE = 'whole_roof_body'            # carried_from.role of a body carry (scene_focus body-carry)
BODY_REGISTRY_ROLE = 'body_carry'        # the registry row naming (window_scene_id, carried_object_id) of a body carry
BODY_RESIDUAL = ('max_shift_m', 'min_ncc')   # the DESIGN values a body row's residual is read against (shift_m <=, ncc >=)
BODY_COVER = .8                 # RULES population_primary: a body "covers at least 0.8 of a re-projected frozen slice's own area" (a listing)
FACTS_COLUMNS = ('review_area_m2', 'model_signature_match', 'image_sha_match', 'geometry_reviewed', 'review_revision', 'review_updated_at',
                 'review_sha256')   # RULES human_precision.facts_columns_not_filters: on every facts row, never a filter
LINEAGE_FACTS = ('decision_on_body_not_target', 'lineage_agreement')   # RULES population_primary: "both are fact columns"
DECIDED_CLASSES = ('roof', 'not_roof', 'roof_part')   # the classes lineage_agreement compares; undecided / excluded are not decisions
RECAPTURE_DIR = REPO / 'data/reviews/eyes/recapture'
REGISTRY_GLOB = 'windows-*.jsonl'                  # forward pointers (source_scene_id, object_id) -> window_scene_id; dated, appended
DECIDED = ('confirmed', 'rejected')
HUMAN = ('ui', 'chat')
CLASSES = ('roof', 'not_roof')
PER_PASS = 4
REASONS = ('not_in_snapshot', 'under_min_area', 'not_judged_image', *CARRY_REFUSALS)
SHARES = ('confirmed_detected_share', 'rejected_detected_share', 'detected_roof_share')
URL = 'http://localhost:3008/scenes?scene={scene}&status=all&layer=edited&object={object}'
WORDING = ("agreement between model estimates and the reviewer's decisions or references; counts of human decisions and agreement "
           "shares, never a calibrated rate; a review the assistant wrote is an observation, never a human decision; hints are never "
           "labels; nothing is promoted on any outcome")
RESCOPE = ("{judge}; no inference; {runs} run dirs ({checkpoints} unique checkpoints) re-read from rows; {sweep} sweep checkpoints not "
           "re-judged; the handover's step-1 wiring inside reference_eval.py is deferred to a session with memory for inference")
KEEPS_RULE = ('keeps_human_precision: tuned detected not-roof count <= pairing arm + 1 AND tuned detected roof count >= pairing arm - 1, '
              'read on the 0.45 view only; promotes nothing')
_BANNED_WORD = 'accura' + 'cy'   # the wording rule's banned word; only used to refuse a report that contains it
FRESH_TEXT = {'no_marker': 'not counted yet (your `[reviewer] roof` / `[reviewer] not a roof` words do not open the notes)',
              'class_conflict': 'not counted: your marked words and the status disagree',
              'revision_1': "not counted: the review has no save after the assistant's revision 1"}
ENTERS_TEXT = {'primary': 'enters the usable count (a model detection)',
               'listed_assistant_drawn_outline': 'kept and listed, never counted: the assistant drew this outline, so it cannot enter '
                                                 'the human-drawn stratum',
               'listed_not_live': 'kept and listed: no longer live in latest.json', 'listed_unjoined': 'kept and listed: not in the snapshot'}


# --- small helpers -----------------------------------------------------------------------------------------------------

def batch_dir_name(batch_id):
    return str(batch_id).replace(':', '-')


def n_rule_read(n, roof, not_roof, scenes):
    """(passes, read) of the pre-registered N rule for one population."""
    n, roof, not_roof, scenes = int(n), int(roof), int(not_roof), int(scenes)
    passes = (n >= N_RULE['primary_decided_min'] and roof >= N_RULE['per_class_min'] and not_roof >= N_RULE['per_class_min']
              and scenes >= N_RULE['scenes_min'])
    counts = f'N={n}, roof={roof}, not_roof={not_roof}, scenes={scenes}'
    return passes, (f'rule met ({counts})' if passes else f'n/a ({counts})')


def now_iso():
    return datetime.now(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def local_time():
    return datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    path = Path(path) if path else None
    return sha256_bytes(path.read_bytes()) if path is not None and path.is_file() else None


def url_for(scene, object_id):
    return URL.format(scene=scene, object=object_id)


def _plain(value):
    """JSON-ready copy: numpy scalars become python numbers, tuples become lists."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def _rel(path, repo):
    path = Path(path).resolve()
    try:
        return path.relative_to(Path(repo).resolve()).as_posix()
    except ValueError:
        return str(path)


def _abs(path, repo):
    path = Path(path)
    return path if path.is_absolute() else Path(repo) / path


def _short(note, limit=240):
    text = ' '.join(str(note or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _read_json(path):
    """(bytes, parsed). A save caught mid-write gets one immediate re-read, then a refusal: a review is never skipped silently."""
    for attempt in (0, 1):
        data = Path(path).read_bytes()
        try:
            return data, json.loads(data)
        except json.JSONDecodeError:
            if attempt:
                raise SystemExit(f'{path}: not valid JSON on two reads (a save in progress?); rerun the command') from None
    raise AssertionError('unreachable')


def wilson95(successes, total):
    """scene_confidence.wilson with int() casts at the call: it rejects numpy integers."""
    return scene_confidence.wilson(int(successes), int(total))


def mcnemar_p(second_only, first_only):
    """Two-sided exact binomial on the discordant pairs (McNemar); 1 when there is none."""
    b, c = int(second_only), int(first_only)
    n = b + c
    if not n:
        return 1.
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n
    return round(min(1., 2 * tail), 6)


# --- decisions ---------------------------------------------------------------------------------------------------------

LOCAL_PRINCIPAL = 'local'   # the review app's save principal: src/app/api/scenes/route.ts POST -> scenePrincipal(request) (CRM WhoAmI;
#                               internal/crm/auth.go LocalPrincipal Name "local" for the local no-auth app) -> saveScene(body.id,
#                               body.review, principal) -> scene-store.ts acceptPendingFeedback(previous, next.objects, principal.principal, now)


def accepted_import(obj):
    """True when a Fieldwork draft that task pull landed as fieldwork_import.pending was accepted by the reviewer's own later
    ordinary local save: src/lib/annotations/feedback.ts acceptPendingFeedback alone writes pending false with accepted_at and
    accepted_by = the save route's principal (a needs_adjustment draft turned rejected, or confirmed with geometry_reviewed).
    pending must read false (not merely absent), accepted_at must be set and accepted_by must equal LOCAL_PRINCIPAL exactly
    (another principal, e.g. another assistant's, is no acceptance: RULES agent_fresh_decision, tightening amendment 2026-10-01);
    a pending draft is review_authority_blocker fieldwork_pending. The geometry_reviewed condition on confirmed is read by
    classify on the object's current state, not here (a marker is an event; the status is what is counted)."""
    marker = obj.get('fieldwork_import')
    return (isinstance(marker, dict) and marker.get('pending') is False
            and bool(str(marker.get('accepted_at') or '').strip()) and marker.get('accepted_by') == LOCAL_PRINCIPAL)


def fresh_state(obj, revision):
    """None when the notes carry no assistant review text; 'valid' when the reviewer's own marked class words open them, agree
    with the status and the review was saved after the assistant's revision 1; 'accepted_import' when no marker opens them but
    the object is a Fieldwork draft the reviewer's own later save accepted (accepted_import; RULES amendment 2026-10-01); else
    why not: 'no_marker', 'class_conflict' or 'revision_1' (RULES human_precision.agent_fresh_decision). A typed marker governs
    when present (class_conflict and revision_1 still exclude). A fresh decision is read from note text plus revision, or from
    the acceptance fields the reviewer's save wrote, never from a status event (none is recorded)."""
    notes = str(obj.get('notes') or '')
    if not AGENT_TEXT.search(notes):
        return None
    mark = FRESH_MARK.match(notes)
    if not mark:
        return 'accepted_import' if accepted_import(obj) else 'no_marker'
    cls = 'roof' if mark.group('cls').lower() == 'roof' else 'not_roof'
    if obj.get('status') != FRESH_STATUS[cls]:
        return 'class_conflict'
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 1:
        return 'revision_1'
    return 'valid'


def classify(obj, revision=None):
    """(cls, provenance_kind, stratum, authority_blocker) of one review.json building object (RULES human_precision.classes_order).

    review_authority_blocker comes first (machine labels, even human-confirmed, and pending Fieldwork imports are never human
    decisions here); then the assistant's review text (agent-authored review: an observation) unless the reviewer's own fresh
    decision opens the notes (fresh_state 'valid'; revision is the review.json revision, None refuses) or the reviewer's own
    later save accepted the pulled draft (fresh_state 'accepted_import': confirmed with geometry_reviewed true -> roof, confirmed
    without it -> undecided (acceptPendingFeedback accepts confirmed only WITH geometry_reviewed, so the counter reads it too),
    rejected -> not_roof, any other status -> undecided, never roof_part); then a carried object still needing adjustment
    (undecided whatever else it says); then superseded; then the status."""
    provenance = obj.get('provenance') if isinstance(obj.get('provenance'), dict) else {}
    blocker = review_authority.review_authority_blocker(obj)
    status = obj.get('status')
    if blocker == 'machine_label':
        stratum = ('machine_label_human_confirmed' if provenance.get('human_confirmed') is True else
                   'machine_label_human_rejected' if provenance.get('human_rejected') is True else 'machine_label')
        return 'excluded', 'machine_label', stratum, blocker
    if blocker is not None:
        return 'excluded', blocker, blocker, blocker
    fresh = fresh_state(obj, revision)
    if fresh not in (None, 'valid', 'accepted_import'):
        return 'excluded', 'agent', 'agent_authored_excluded', None
    if review_authority.carried_undecided(obj):
        return 'undecided', 'carried_undecided', 'carried_undecided', None
    decision = provenance.get('human_decision') if isinstance(provenance.get('human_decision'), dict) else {}
    kind = 'chat' if decision.get('source') == 'human:chat' else 'ui'
    if status == 'superseded':
        return 'excluded', kind, 'superseded', None
    if status == 'confirmed':
        if fresh == 'accepted_import' and obj.get('geometry_reviewed') is not True:
            return 'undecided', kind, None, None    # an accepted import confirmed without geometry_reviewed is undecided, never roof
        return 'roof', kind, None, None
    if status == 'rejected':
        return 'not_roof', kind, None, None
    if status == 'needs_adjustment' and 'instances' in (obj.get('review_issues') or ()) and fresh != 'accepted_import':
        return 'roof_part', kind, None, None      # an accepted import re-opened to needs_adjustment is undecided, never roof_part
    return 'undecided', kind, None, None


def _carried_from(obj):
    """The carried_from dict of a review object, or None (a non-dict value is a carry to review_authority.carried but names no key)."""
    value = obj.get('carried_from')
    return value if isinstance(value, dict) and value else None


def _id_list(obj, field):
    """The sorted string ids a list field names, or None (a non-list or empty value names nothing)."""
    value = obj.get(field)
    return sorted(str(v) for v in value) if isinstance(value, list) and value else None


def is_body_replacer(obj):
    """True for a carried copy of role whole_roof_body that names replaces: the body-carry replacer (amendment 2026-10-06)."""
    return review_authority.carried(obj) and (_carried_from(obj) or {}).get('role') == BODY_ROLE and _id_list(obj, 'replaces') is not None


def body_registration(lineage, window_scene_id, replacer_id):
    """The body_carry registry facts of (window_scene_id, replacer_id) and the refusal they name, or None when the row holds:
    exactly one row of role body_carry names the pair (else body_unregistered / body_registered_twice) with frame_transform_verified
    true (else body_unregistered) and a residual inside the current DESIGN.json (shift_m <= max_shift_m and ncc >= min_ncc, both
    read from the file through load_lineage design_values; a missing number refuses: body_residual_outside_design). The row's
    design_sha256 acceptance, slice_cover, slice_object_id and source are recorded as facts, never read to hold or refuse."""
    lin = lineage or {}
    rows = [r for r in (lin.get('body_carry') or {}).get((window_scene_id, replacer_id)) or () if r.get('role') == BODY_REGISTRY_ROLE]
    design = lin.get('design_values') or {}
    facts = {'rows': len(rows), 'frame_transform_verified': None, 'residual': None, 'slice_cover': None, 'slice_object_id': None,
             'source': None, 'design_sha_match': None, 'design': {k: design.get(k) for k in BODY_RESIDUAL}, 'refusal': None}
    if not rows:
        facts['refusal'] = 'body_unregistered'
        return facts
    if len(rows) > 1:
        facts['refusal'] = 'body_registered_twice'
        return facts
    row = rows[0]
    residual = row.get('residual') if isinstance(row.get('residual'), dict) else {}
    facts.update(frame_transform_verified=row.get('frame_transform_verified'),
                 residual={'shift_m': residual.get('shift_m'), 'ncc': residual.get('ncc')}, slice_cover=row.get('slice_cover'),
                 slice_object_id=row.get('slice_object_id'), source=[row.get('source_scene_id'), row.get('object_id')],
                 design_sha_match=design_chain.match(_accepted(lin), row.get('design_sha256')))
    if row.get('frame_transform_verified') is not True:
        facts['refusal'] = 'body_unregistered'
        return facts
    numbers = (residual.get('shift_m'), residual.get('ncc'), design.get('max_shift_m'), design.get('min_ncc'))
    if (not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in numbers)
            or numbers[0] > numbers[2] or numbers[1] < numbers[3]):
        facts['refusal'] = 'body_residual_outside_design'
    return facts


def _body_link_refusal(body, cid, copy, names, registration):
    """Why a body carry's replaces link to copy id `cid` is followed nowhere, or None when it holds (one hop: D.replaces == [slice];
    the slice present, carried, not a body or a replacer itself, not decided, superseded, naming D back; the body row holds)."""
    source = _carried_from(copy) if copy is not None else None
    if len(names) != 1:
        return 'body_replaces_not_single'
    if copy is None:
        return 'copy_missing'
    if source is None:
        return 'copy_not_carried'
    if source.get('role') == BODY_ROLE or _id_list(copy, 'replaces') is not None:
        return 'two_hops'
    if copy.get('status') in DECIDED:
        return 'copy_decided'
    if copy.get('status') != 'superseded':
        return 'copy_not_superseded'
    if _id_list(copy, 'replacement_ids') != [body['id']]:
        return 'copy_not_reciprocal'
    if registration['refusal']:
        return registration['refusal']
    if registration['slice_object_id'] not in (None, cid):
        return 'body_unregistered'          # the one row names another slice: no row for this link
    return None


def takeover_links(objects, scene_id=None, lineage=None):
    """The replaces links of one scene's review objects (TAKEOVER RULE, WINDOW.json / RULES amendments 2026-10-02): a detection D
    whose replaces names a copy C holds the link when C is in the scene, carries a carried_from dict and reads status superseded,
    and D is not itself a carried copy (one replaces hop, never two); every other link is recorded by name and followed nowhere.
    The hop reads D.replaces alone: the copy's replacement_ids (the reciprocal the takeover writes) and D.derived_from (mirrors
    D.replaces for the editor's save validator) are lineage bookkeeping, never read to hold or refuse a link (RULES join_order,
    amendment 2026-10-02); a held link whose copy does not name D back is tallied replacement_mismatch, nothing more.
    Body carry (amendment 2026-10-06): a carried replacer of role whole_roof_body (is_body_replacer) holds its one link when
    _body_link_refusal names nothing: D.replaces == [slice], the slice present, carried, neither a body nor a replacer (two_hops),
    not decided (copy_decided), superseded, naming D back in replacement_ids (copy_not_reciprocal: read here, for a body only),
    and body_registration holds for (scene_id, D.id) under `lineage` (load_lineage; None refuses body_unregistered by name). A
    carried replacer of any other role is still replacer_carried.
    Returns ({D.id: [held copy ids]}, {D.id: {copy id: reason}}, {C.id: [D ids claiming it]}); the claims count every held link,
    so a copy two replacers name is ambiguous whatever its own replacement_ids says."""
    by_id = {o['id']: o for o in objects}
    held, refused, claims = {}, {}, defaultdict(list)
    for o in objects:
        names = _id_list(o, 'replaces')
        if names is None:
            continue
        body = is_body_replacer(o)
        registration = body_registration(lineage, scene_id, o['id']) if body else None
        for cid in names:
            copy = by_id.get(cid)
            if body:
                reason = _body_link_refusal(o, cid, copy, names, registration)
            else:
                reason = ('replacer_carried' if review_authority.carried(o) else 'copy_missing' if copy is None
                          else 'copy_not_carried' if _carried_from(copy) is None
                          else 'copy_not_superseded' if copy.get('status') != 'superseded' else None)
            if reason:
                refused.setdefault(o['id'], {})[cid] = reason
            else:
                held.setdefault(o['id'], []).append(cid)
                claims[cid].append(o['id'])
    return held, refused, {k: sorted(v) for k, v in claims.items()}


def load_decisions(root=SCENES, scene_ids=None, extra_live=(), lineage=None):
    """One read pass over data/scenes: a row per review.json object (non-building kinds kept as cls 'skipped', never counted)
    with its class, provenance kind, stratum and fact columns (the seven FACTS_COLUMNS and, from lineage_facts, the two
    LINEAGE_FACTS with their detail), plus the live latest.json objects of the scenes read (source, kind, notes, ring, holes; and
    each scene's frame_transform, when its latest.json carries one).

    extra_live names scenes whose latest.json is read even without a review.json (judged tiles, earlier guide items); the source
    scene a carried_from names is read the same way, since the lineage hop reads live / production at the frozen key there.
    `lineage` (load_lineage) is read by takeover_links for the body_carry registry rows a body replacer needs (amendment
    2026-10-06); without it every body link is refused body_unregistered by name, never silently."""
    root = Path(root)
    wanted = None if scene_ids is None else set(scene_ids)
    reviewed = [p.parent.name for p in sorted(root.glob('*/review.json'))
                if not p.parent.name.startswith('qa_') and (wanted is None or p.parent.name in wanted)]
    reviews, carried_sources = {}, set()
    for scene in reviewed:
        reviews[scene] = _read_json(root / scene / 'review.json')
        for obj in reviews[scene][1].get('objects') or ():
            source = _carried_from(obj) if isinstance(obj, dict) else None
            if source and isinstance(source.get('scene_id'), str):
                carried_sources.add(source['scene_id'])
    live, latest_lines, review_lines, rows = {}, [], [], []
    for scene in sorted(s for s in set(reviewed) | {str(s) for s in extra_live} | carried_sources if not s.startswith('qa_')):
        path = root / scene / 'latest.json'
        if not path.is_file():
            continue
        data, latest = _read_json(path)
        latest_lines.append(f'{scene}\t{sha256_bytes(data)}')
        transform = latest.get('frame_transform')
        live[scene] = {'image_sha256': latest.get('image_sha256'), 'model_signature': latest.get('model_signature'),
                       'frame_transform': transform if isinstance(transform, dict) else None,
                       'objects': {o['id']: {'source': o.get('source'), 'kind': o.get('kind'), 'notes': o.get('notes'),
                                             'ring': o.get('ring'), 'holes': o.get('holes')}
                                   for o in latest.get('objects') or () if isinstance(o, dict) and 'id' in o}}
    geoms = {}
    for scene in reviewed:
        data, review = reviews[scene]
        sha = sha256_bytes(data)
        review_lines.append(f'{scene}\t{sha}')
        scene_live = live.get(scene) or {'objects': {}, 'model_signature': None}
        rsig, lsig = review.get('model_signature'), scene_live.get('model_signature')
        objects = [o for o in review.get('objects') or () if isinstance(o, dict) and 'id' in o]
        held, refused, claims = takeover_links(objects, scene_id=scene, lineage=lineage)
        for obj in objects:
            latest_obj = scene_live['objects'].get(obj['id'])
            source = str((latest_obj or obj).get('source') or '')
            if obj.get('kind') == 'building':
                cls, pkind, stratum, blocker = classify(obj, review.get('revision'))
            else:
                cls, pkind, stratum, blocker = 'skipped', None, 'kind_not_building', None
            taken_over_by = claims.get(obj['id'], [])
            geoms[(scene, obj['id'])] = {'ring': obj.get('ring'), 'holes': obj.get('holes')}
            if stratum == 'superseded' and taken_over_by:
                stratum = 'superseded_by_takeover'       # the copy a detection took over: excluded as before, named for what it is
            rows.append({'scene_id': scene, 'object_id': obj['id'], 'kind': obj.get('kind'), 'status': obj.get('status'), 'cls': cls,
                         'provenance_kind': pkind, 'stratum': stratum, 'authority_blocker': blocker, 'decided': cls in CLASSES,
                         'agent_note': bool(AGENT_TEXT.search(str(obj.get('notes') or ''))),
                         'fresh': fresh_state(obj, review.get('revision')), 'carried': review_authority.carried(obj),
                         'carried_from': _carried_from(obj),
                         'replaces': _id_list(obj, 'replaces'), 'replaces_held': held.get(obj['id'], []),
                         'replaces_refused': refused.get(obj['id'], {}), 'replacement_ids': _id_list(obj, 'replacement_ids'),
                         'taken_over_by': taken_over_by,
                         'replacer_kind': ('body_carry' if is_body_replacer(obj) else 'detection' if _id_list(obj, 'replaces') else None),
                         'body_registration': body_registration(lineage, scene, obj['id']) if is_body_replacer(obj) else None,
                         'source': source, 'live': latest_obj is not None,
                         'production': latest_obj is not None and not source.startswith('reviewer'),
                         'review_issues': list(obj.get('review_issues') or ()), 'geometry_reviewed': obj.get('geometry_reviewed'),
                         'notes': obj.get('notes'), 'review_area_m2': (obj.get('model') or {}).get('area_m2'),
                         'review_sha256': sha, 'review_revision': review.get('revision'), 'review_updated_at': review.get('updated_at'),
                         'model_signature_match': (rsig == lsig) if (rsig and lsig) else None})
    lineage_facts(rows, live, geoms, reviewed)
    return {'rows': rows, 'live': live, 'reviews_read': len(review_lines), 'latest_read': len(latest_lines), 'read_at': now_iso(),
            'review_snapshot_sha256': sha256_bytes('\n'.join(review_lines).encode()),
            'latest_snapshot_sha256': sha256_bytes('\n'.join(latest_lines).encode())}


# --- the two lineage fact columns (RULES population_primary: recorded, never filters) -----------------------------------

def _polygon(ring, holes=None):
    """A valid shapely polygon of a pixel ring (holes kept), or None when the ring is missing, degenerate or unreadable."""
    if not isinstance(ring, list) or len(ring) < 3:
        return None
    try:
        polygon = Polygon([p[:2] for p in ring], [[p[:2] for p in h] for h in (holes or ()) if isinstance(h, list) and len(h) >= 3])
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
    except GEOMETRY_ERRORS:
        return None
    return None if polygon.is_empty or polygon.area <= 0 else polygon


def slice_geometry(copy_row, window_live, live, geoms):
    """(polygon, slice_ring) of the re-projected frozen slice a carried copy stands for. 'reprojected': the source scene's live
    ring at the frozen key (carried_from.scene_id, carried_from.object_id) re-projected into the window through the window
    latest.json frame_transform by scene_recapture.project_object, the arithmetic that wrote the copy (on the live windows it
    reproduces the saved copy ring exactly unless the reviewer edited the copy); 'copy_ring': the copy's own saved ring, read only
    when the source is not live or the transform cannot project; (None, None) when neither gives a polygon."""
    cf = copy_row.get('carried_from') or {}
    key = (cf.get('scene_id'), cf.get('object_id'))
    transform = (window_live or {}).get('frame_transform') or {}
    source = ((live.get(key[0]) or {}).get('objects') or {}).get(key[1]) if isinstance(key[0], str) else None
    if source is not None and source.get('ring') and transform.get('pixel_affine') is not None and transform.get('metre_offset') is not None:
        try:
            projected = scene_recapture.project_object({'ring': source['ring'], 'holes': source.get('holes')}, transform)
        except GEOMETRY_ERRORS:
            projected = None
        polygon = _polygon(projected.get('ring'), projected.get('holes')) if projected else None
        if polygon is not None:
            return polygon, 'reprojected'
    own = geoms.get((copy_row['scene_id'], copy_row['object_id'])) or {}
    polygon = _polygon(own.get('ring'), own.get('holes'))
    return (polygon, 'copy_ring') if polygon is not None else (None, None)


def _agreement(copy, review_of, reviewed, covering, covers=None):
    """(lineage_agreement, detail) of one carried copy: RULES population_primary, "lineage_agreement on the carried copy records
    whether that whole-roof decision agrees". The slice's reference decision is the copy's own class, or the class of the single
    detection that took it over (the whole-roof decision the one replaces hop makes stand for the slice; None when two claim
    it). Its counterparts are the SOURCE scene's review object at the frozen key ('undecided' when that review was read and
    names no such object; class None when it was not read) and every covering body (a window object without carried_from at
    cover >= BODY_COVER that is not the taker). Per counterpart: same_class (the classify classes equal, undecided and excluded
    included) and agrees (same_class when both sides are in DECIDED_CLASSES, else None). Overall: True when every compared
    decision agrees, False when one differs, None when nothing decided is compared. Every entry records status, class and
    stratum (why an excluded side is no decision: fieldwork_pending, machine_label, agent_authored_excluded, superseded...);
    a taker records its own cover of the slice (`covers`: {body id: cover} over every body of the window, any value).
    A record, never a filter."""
    covers = covers or {}
    cf = copy.get('carried_from') or {}
    key = (cf.get('scene_id'), cf.get('object_id'))
    takers = [review_of[(copy['scene_id'], d)] for d in copy.get('taken_over_by') or () if (copy['scene_id'], d) in review_of]
    if takers:
        one = takers[0] if len(takers) == 1 else None
        reference = {'from': 'takeover' if one else 'ambiguous_takeover', 'object_id': one['object_id'] if one else None,
                     'class': one['cls'] if one else None}
    else:
        reference = {'from': 'copy', 'object_id': copy['object_id'], 'class': copy['cls']}
    ref = reference['class']

    def pair(cls):
        same = (cls == ref) if ref is not None and cls is not None else None
        return same, (same if same is not None and ref in DECIDED_CLASSES and cls in DECIDED_CLASSES else None)
    src = review_of.get(key)
    src_cls = src['cls'] if src is not None else ('undecided' if key[0] in reviewed else None)
    same, agrees = pair(src_cls)
    source = {'scene_id': key[0], 'object_id': key[1], 'review_read': key[0] in reviewed, 'found': src is not None,
              'status': src['status'] if src else None, 'class': src_cls, 'stratum': src['stratum'] if src else None,
              'same_class': same, 'agrees': agrees}
    taker_ids = {t['object_id'] for t in takers}
    bodies = []
    for body, cover in covering:
        if body['object_id'] in taker_ids:
            continue
        same, agrees = pair(body['cls'])
        bodies.append({'object_id': body['object_id'], 'status': body['status'], 'class': body['cls'], 'stratum': body['stratum'],
                       'cover': cover, 'same_class': same, 'agrees': agrees})
    compared = [p['agrees'] for p in (source, *bodies) if p['agrees'] is not None]
    detail = {'copy': {'status': copy['status'], 'class': copy['cls'], 'stratum': copy['stratum'],
                       'taken_over_by': list(copy.get('taken_over_by') or ())},
              'reference': reference, 'source': source,
              'takeover': [{'object_id': t['object_id'], 'status': t['status'], 'class': t['cls'], 'stratum': t['stratum'],
                            'cover': covers.get(t['object_id'])} for t in takers], 'body': bodies}
    return (all(compared) if compared else None), detail


def lineage_facts(rows, live, geoms, reviewed=()):
    """Annotate every load_decisions row in place with the two RULES fact columns and their records (LINEAGE_FACTS; recorded,
    never filters: nothing here is read by classify, join, population, targets or a share). On a scene holding carried copies
    (a window) every building object without carried_from gets body_covers (each slice it covers at >= BODY_COVER of the
    slice's own area: copy id, frozen key, cover, slice_ring), body_cover_max and decision_on_body_not_target (any such slice);
    every carried copy gets lineage_agreement and lineage_agreement_detail (_agreement). Every other row carries the defaults."""
    for r in rows:
        r.update(decision_on_body_not_target=False, body_covers=[], body_cover_max=None, lineage_agreement=None, lineage_agreement_detail=None)
    review_of = {(r['scene_id'], r['object_id']): r for r in rows}
    reviewed = set(reviewed) or {r['scene_id'] for r in rows}
    by_scene = defaultdict(list)
    for r in rows:
        by_scene[r['scene_id']].append(r)
    for scene, scene_rows in by_scene.items():
        copies = [r for r in scene_rows if r.get('carried') and isinstance((r.get('carried_from') or {}).get('scene_id'), str)
                  and 'object_id' in (r.get('carried_from') or {})]
        if not copies:
            continue
        slices = {c['object_id']: (c, *slice_geometry(c, live.get(scene), live, geoms)) for c in copies}
        covering, all_covers = defaultdict(list), defaultdict(dict)
        for body in scene_rows:
            if body.get('carried') or body.get('kind') != 'building':
                continue
            own = geoms.get((scene, body['object_id'])) or {}
            polygon = _polygon(own.get('ring'), own.get('holes'))
            if polygon is None:
                continue
            best = None
            for cid, (copy, slice_polygon, how) in sorted(slices.items()):
                if slice_polygon is None:
                    continue
                try:
                    cover = float(polygon.intersection(slice_polygon).area / slice_polygon.area)
                except GEOMETRY_ERRORS:
                    continue
                best = cover if best is None else max(best, cover)
                all_covers[cid][body['object_id']] = round(cover, 6)
                if cover >= BODY_COVER:
                    cf = copy['carried_from']
                    body['body_covers'].append({'copy_id': cid, 'frozen_key': [cf['scene_id'], cf['object_id']], 'cover': round(cover, 6),
                                                'slice_ring': how})
                    covering[cid].append((body, round(cover, 6)))
            body['body_cover_max'] = round(best, 6) if best is not None else None
            body['decision_on_body_not_target'] = bool(body['body_covers'])
        for cid, entry in slices.items():
            copy = entry[0]
            copy['lineage_agreement'], copy['lineage_agreement_detail'] = _agreement(copy, review_of, reviewed, covering.get(cid, ()),
                                                                                       all_covers.get(cid))


def lineage_listing(decisions):
    """The two fact columns as listings for count / rejudge (never counts): every object listed decision_on_body_not_target with
    its covers, every carried copy with its lineage_agreement and the compared statuses, and the slice geometry each read."""
    rows = decisions['rows'] if isinstance(decisions, dict) else decisions
    bodies = sorted(({'scene_id': r['scene_id'], 'id': r['object_id'], 'status': r['status'], 'class': r['cls'],
                      'provenance_kind': r['provenance_kind'], 'replaces_held': r.get('replaces_held') or [],
                      'body_covers': r['body_covers'], 'body_cover_max': r['body_cover_max'], 'url': url_for(r['scene_id'], r['object_id'])}
                     for r in rows if r.get('decision_on_body_not_target')), key=lambda o: (str(o['scene_id']), str(o['id'])))
    copies = sorted(({'scene_id': r['scene_id'], 'id': r['object_id'],
                      'frozen_key': [r['carried_from'].get('scene_id'), r['carried_from'].get('object_id')], 'status': r['status'],
                      'class': r['cls'], 'lineage_agreement': r['lineage_agreement'], 'detail': r['lineage_agreement_detail'],
                      'url': url_for(r['scene_id'], r['object_id'])}
                     for r in rows if r.get('lineage_agreement_detail') is not None), key=lambda o: (str(o['scene_id']), str(o['id'])))
    return {'rule': "RULES human_precision.population_primary: a window-scene object without carried_from is listed decision_on_body_not_target "
                    "when it covers at least 0.8 of a re-projected frozen slice's own area, and lineage_agreement on the carried copy records "
                    "whether that whole-roof decision agrees; both are fact columns (listings, never counts)",
            'body_cover_min': BODY_COVER, 'slice_ring': 'reprojected: the source scene\'s live ring at the frozen key through the window '
            'latest.json frame_transform (scene_recapture.project_object); copy_ring: the copy\'s own saved ring when the source is not live',
            'decision_on_body_not_target': bodies, 'lineage_agreement': copies,
            'lineage_agreement_tally': dict(sorted(Counter(str(o['lineage_agreement']) for o in copies).items())),
            'reference_from_tally': dict(sorted(Counter(o['detail']['reference']['from'] for o in copies).items())),
            'source_tally': dict(sorted(Counter(f"{o['detail']['source']['status']}/{o['detail']['source']['class']}/"
                                                f"{o['detail']['source']['stratum']}" for o in copies).items()))}


# --- frozen-snapshot join ----------------------------------------------------------------------------------------------

def split_path_for(context, repo=REPO):
    dataset = Path(context['dataset'])
    return (dataset if dataset.is_absolute() else Path(repo) / dataset) / f"{context['split']}.json"


def read_split(context, split_json):
    """(split, sha256) of the split file, refused unless its bytes are the frozen snapshot the rows were judged on."""
    data = bytes(split_json) if isinstance(split_json, (bytes, bytearray)) else Path(split_json).read_bytes()
    sha = sha256_bytes(data)
    want = (context.get('meta') or {}).get('split_file_sha256')
    if sha != want:
        raise SystemExit(f'split file sha256 {sha} is not the frozen snapshot {want} named by context.meta.split_file_sha256: '
                         'the join refuses (a round-5 export is a new dataset folder)')
    return json.loads(data), sha


def load_lineage(recapture_dir=RECAPTURE_DIR):
    """The lineage inputs the hop reads: the sha256 of <recapture_dir>/DESIGN.json with the design shas it accepts
    (design_accepted: design_chain.accepted_shas, the current sha and every previous_sha256 reached from the newest amendment
    through thresholds_changed-false entries, each with its hop named) and the forward-pointer registry
    <recapture_dir>/windows-*.jsonl as {(source_scene_id, object_id): [rows]} in file then line order. A later row with withdrawn
    true removes the pointer to its window_scene_id (rollback appends withdrawn rows, never rewrites a dated file). Absent files
    give None / {} so every carried copy is refused by name (carry_design_mismatch, carry_unregistered), never silently.
    body_carry indexes the rows of role body_carry by (window_scene_id, carried_object_id) for the body-carry hop (a withdrawn row
    naming the pair removes them); design_values holds the DESIGN.json max_shift_m / min_ncc a body row's residual is read against."""
    folder = Path(recapture_dir)
    design = folder / 'DESIGN.json'
    design_values, design_sha, design_accepted = design_chain.read(design)
    registry, bodies, files, n = defaultdict(list), defaultdict(list), [], 0
    for path in (sorted(folder.glob(REGISTRY_GLOB)) if folder.is_dir() else ()):
        files.append(path.name)
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            n += 1
            key = (row.get('source_scene_id'), row.get('object_id'))
            body_key = (row.get('window_scene_id'), row.get('carried_object_id'))     # a body_carry row: (window, the body's id)
            if row.get('withdrawn'):
                registry[key] = [r for r in registry[key] if r.get('window_scene_id') != row.get('window_scene_id')]
                bodies.pop(body_key, None)
            else:
                registry[key].append(row)
                if row.get('role') == BODY_REGISTRY_ROLE:
                    bodies[body_key].append(row)
    values = design_values if isinstance(design_values, dict) else {}
    return {'design_sha256': design_sha, 'design_path': str(design), 'design_accepted': design_accepted, 'registry': dict(registry),
            'registry_files': files, 'registry_rows': n, 'body_carry': dict(bodies),
            'design_values': {k: values.get(k) for k in BODY_RESIDUAL}}


def _accepted(design):
    """The design shas a hop accepts: a lineage (load_lineage) gives its chain (design_accepted); a bare sha string accepts only
    itself (the exact rule, the older call shape); None accepts nothing."""
    if isinstance(design, dict):
        return design.get('design_accepted') or {}
    return design_chain.accepted_shas({}, design) if isinstance(design, str) else {}


def registration(lineage, key, window_scene_id):
    """The design-sha acceptance ({'match': 'design_sha_current' | 'design_sha_via_chain', ...hop}) of the first registry row that
    points (source_scene_id, object_id) -> window_scene_id with role carried_source, frame_transform_verified true and a
    design_sha256 the current DESIGN.json accepts (load_lineage design_accepted: its own sha, or the previous_sha256 of a dated
    amendment reached through thresholds_changed-false entries; a withdrawn row is already gone). None when no row qualifies:
    the hop lists carry_unregistered, read from the registry alone, independently of the live window's own frame_transform
    (carry_unverified / carry_design_mismatch)."""
    lin = lineage or {}
    accepted = _accepted(lin)
    rows = (lin.get('registry') or {}).get(tuple(key)) or ()
    for r in rows:
        if r.get('role') == 'carried_source' and r.get('window_scene_id') == window_scene_id and r.get('frame_transform_verified') is True:
            matched = design_chain.match(accepted, r.get('design_sha256'))
            if matched is not None:
                return matched
    return None


def registered(lineage, key, window_scene_id):
    """True when registration() finds a verified registry row for the key naming this window under an accepted design sha."""
    return registration(lineage, key, window_scene_id) is not None


def registry_windows(lineage, key):
    """Every window scene the registry names for the key, whatever the role (listed beside the old target)."""
    rows = ((lineage or {}).get('registry') or {}).get(tuple(key)) or ()
    return sorted({str(r.get('window_scene_id')) for r in rows if r.get('window_scene_id')})


def carry_refusals(carried_from, window_live, frozen_image_sha256, registered_here, design):
    """The named preconditions of the hop that fail, in CARRY_REFUSALS order (every failing one is listed; none means the hop holds):
    the window's frame_transform verified; its recorded design sha256 accepted by DESIGN.json (`design`: the lineage from
    load_lineage, whose design_accepted holds the current sha and the chain of previous_sha256 behind thresholds_changed-false
    amendments; or a bare sha string, accepting only itself); carried_from naming the transform's source scene and snapshot whose
    source_image_sha256 is the frozen split's image for that scene; a verified registry row for the key naming this window
    (registration: role carried_source, frame_transform_verified true, design_sha256 accepted); carried_from.method
    georeferenced-affine."""
    cf = carried_from if isinstance(carried_from, dict) else {}
    ft = (window_live or {}).get('frame_transform') or {}
    reasons = []
    if ft.get('verified') is not True:
        reasons.append('carry_unverified')
    if design_chain.match(_accepted(design), (ft.get('design') or {}).get('design_sha256')) is None:
        reasons.append('carry_design_mismatch')
    if not (cf.get('scene_id') and cf.get('model_signature') and frozen_image_sha256
            and ft.get('source_scene_id') == cf.get('scene_id') and ft.get('source_model_signature') == cf.get('model_signature')
            and ft.get('source_image_sha256') == frozen_image_sha256):
        reasons.append('carry_snapshot_mismatch')
    if not registered_here:
        reasons.append('carry_unregistered')
    if cf.get('method') != LINEAGE_METHOD:
        reasons.append('carry_method')
    return reasons


def _hops(rows_in, live, scene_image_sha, lineage):
    """{(scene_id, object_id): hop} for every carried review row (any kind): its frozen key, the window it sits on, the named
    refusals and design_sha_match (how the window transform's and the registry row's recorded design shas were accepted:
    design_sha_current / design_sha_via_chain with the hop named, or None when refused); plus the number of copies per frozen key
    among the hops that hold (the ambiguous_carry rule counts copies, not decisions, and never a refused copy). Every row tallied
    here is emitted by join() (a carried non-building row as cls skipped with its lineage), so this tally, join() and targets()
    read the same copies."""
    accepted = _accepted(lineage)
    hops, copies = {}, Counter()
    for d in rows_in:
        if not d.get('carried'):
            continue
        cf = d.get('carried_from') or {}
        key = (cf['scene_id'], cf['object_id']) if isinstance(cf.get('scene_id'), str) and 'object_id' in cf else None
        window_live = live.get(d['scene_id'])
        registry_match = registration(lineage, key, d['scene_id']) if key else None
        refusals = carry_refusals(cf, window_live, scene_image_sha.get(key[0]) if key else None, registry_match is not None, lineage)
        transform_sha = ((((window_live or {}).get('frame_transform') or {}).get('design')) or {}).get('design_sha256')
        hops[(d['scene_id'], d['object_id'])] = {'window_scene_id': d['scene_id'], 'frozen_key': key, 'refusals': refusals,
                                                 'design_sha_match': {'transform': design_chain.match(accepted, transform_sha),
                                                                      'registry': registry_match}}
        if key and not refusals:
            copies[key] += 1
    return hops, copies


def join(context, split_json, decisions, lineage=None):
    """Join decisions to the block's frozen snapshot. P: machine_pseudo row of a judged image (id in pseudo_ids) with
    (scene, object_id) = the decision; R: a reference whose object_id (or ref-<ref_id>) is the decision id. The 25 m2 gate reads
    the snapshot on both sides; review.json model.area_m2 stays a fact column.

    A review object with a non-empty carried_from is joined on its frozen key (carried_from.scene_id, carried_from.object_id) when
    the hop's preconditions hold (carry_refusals; lineage from load_lineage, None refuses every carry by name): P/R path, snapshot
    area, live, production and source read at that key in the source scene; class, provenance kind, stratum, fresh state, revision
    and notes from the copy's own review object. Then exactly one decision per frozen key: a counted source decision wins (copies
    listed duplicate_decision_on_recapture); copies in two scenes or twice in one count nowhere (ambiguous_carry).

    A direct non-building row (cls skipped) is nothing to the count and is dropped; a CARRIED non-building row (a copy the reviewer
    re-kinded) is emitted with its lineage so the copies tally and targets() see the same copies (it is never decided, never primary,
    never a not_counted listing; it adds the kind_not_building stratum).

    Takeover (one replaces hop): a detection D whose replaces_held names copies (takeover_links) is emitted once per held copy,
    on the COPY's hop (its frozen key, refusals and copies count) with lineage.takeover naming the copy and how many detections
    claim it; class, provenance kind, stratum, fresh state, revision and notes are D's own. Such a D is never also an ordinary
    window row. The copy itself is emitted as before (excluded, stratum superseded_by_takeover), so the copies tally is unchanged.
    A body-carry replacer (amendment 2026-10-06) is emitted the same way on its one held copy's hop (lineage.takeover also names
    replacer body_carry and the registry facts) and, being a carried copy itself, is still emitted as its own carried row on its
    carried_from key (refused as before: no carried_source row names a body), so the copies tally and carried listing are unchanged."""
    split, sha = read_split(context, split_json)
    rows_in = decisions['rows'] if isinstance(decisions, dict) else decisions
    live = decisions.get('live') or {} if isinstance(decisions, dict) else {}
    images = {int(k): v for k, v in context['images'].items()}
    judged = {v.get('scene_id') for v in images.values()}
    pseudo_ok = {int(i) for v in images.values() for i in v.get('pseudo_ids') or ()}
    split_images = {im['id']: im for im in split.get('images') or ()}
    scene_image_sha = {im.get('scene_id'): im.get('sha256') for im in split_images.values()}
    pseudo = {}
    for ann in split.get('annotations') or ():
        if ann.get('provenance') != 'machine_pseudo' or ann.get('id') not in pseudo_ok:
            continue
        scene = (split_images.get(ann['image_id']) or {}).get('scene_id')
        pseudo[(scene, ann.get('object_id'))] = {'scene_id': scene, 'object_id': ann.get('object_id'), 'ann_id': ann['id'],
                                                 'image_id': ann['image_id'], 'area_m2': ann.get('area_m2')}
    by_object = defaultdict(set)
    for key, ref in context['refs'].items():
        by_object[(ref.get('scene_id'), ref.get('object_id'))].add(key)
        if ref.get('ref_id'):
            by_object[(ref.get('scene_id'), f"ref-{ref['ref_id']}")].add(key)
    review_of = {(r['scene_id'], r['object_id']): r for r in rows_in}
    refs = {}
    for key, ref in context['refs'].items():
        owners = [review_of.get((ref.get('scene_id'), ref.get('object_id')))]
        if ref.get('ref_id'):
            owners.append(review_of.get((ref.get('scene_id'), f"ref-{ref['ref_id']}")))
        refs[key] = {'scene_id': ref.get('scene_id'), 'object_id': ref.get('object_id'), 'tier': ref.get('tier'),
                     'provenance': ref.get('provenance'), 'area_m2': ref.get('area_m2'),
                     'agent_traced_reference': any(o is not None and o['agent_note'] for o in owners)}
    hops, copies = _hops(rows_in, live, scene_image_sha, lineage)

    def emit(d, hop, takeover=None):
        """One joined row: `d` read at the frozen key its hop names (or its own key without a hop); `takeover` names the copy a
        detection stands in for (the hop is then the copy's)."""
        lineage_row = None
        if hop is not None:
            key = hop['frozen_key']
            lineage_row = {**hop, 'copies': copies.get(key, 0) if key else 0, 'source_decision_counted': None,
                           **({'takeover': takeover} if takeover else {})}
            if hop['refusals'] or key is None:
                return {**d, 'live': False, 'production': False, 'path': None, 'ann_id': None, 'ref_keys': [], 'image_id': None,
                        'snapshot_area_m2': None, 'small': False, 'judged_scene': bool(key and key[0] in judged),
                        'agent_traced_reference': False, 'unjoined_reasons': list(hop['refusals']), 'primary': False,
                        'human_drawn': False, 'not_counted_reason': None, 'image_sha_match': None,
                        'frozen_key': list(key) if key else None, 'lineage': lineage_row}
            source_obj = (live.get(key[0]) or {}).get('objects', {}).get(key[1])
            source = str((source_obj or {}).get('source') or d['source'] or '')
            d = {**d, 'live': source_obj is not None, 'production': source_obj is not None and not source.startswith('reviewer'),
                 'source': source}
        else:
            key = (d['scene_id'], d['object_id'])
        scene = key[0]
        row = {**d, 'path': None, 'ann_id': None, 'ref_keys': [], 'image_id': None, 'snapshot_area_m2': None}
        if key in pseudo:
            p = pseudo[key]
            row.update(path='P', ann_id=p['ann_id'], image_id=p['image_id'], snapshot_area_m2=p['area_m2'])
        elif by_object.get(key):
            keys = sorted(by_object[key])
            first = context['refs'][keys[0]]
            row.update(path='R', ref_keys=keys, image_id=first.get('image_id'), snapshot_area_m2=first.get('area_m2'))
        small = row['snapshot_area_m2'] is not None and row['snapshot_area_m2'] < SMALL_M2
        agent_ref = row['path'] == 'R' and (d['agent_note'] or any(refs[k]['agent_traced_reference'] for k in row['ref_keys']))
        human = d['provenance_kind'] in HUMAN
        reasons = []
        if row['path'] is None:
            if scene not in judged:
                reasons.append('not_judged_image')
            if d['review_area_m2'] is not None and d['review_area_m2'] < MIN_AREA_M2:
                reasons.append('under_min_area')
            if scene in judged and 'under_min_area' not in reasons:
                reasons.append('not_in_snapshot')
        scene_live = live.get(scene)
        reviewer_source = str(d['source'] or '').startswith('reviewer')
        primary = bool(d['production'] and row['path'] and small and human)
        human_drawn = bool(row['path'] == 'R' and reviewer_source and not agent_ref and small and human)   # by source, never by liveness
        not_counted = None
        if d['decided'] and row['path'] and small and human and not primary and not human_drawn:
            not_counted = 'not_live' if not d['live'] and not reviewer_source else 'agent_traced_geometry' if agent_ref else 'other'
        row.update(small=small, judged_scene=scene in judged, agent_traced_reference=bool(agent_ref), unjoined_reasons=reasons,
                   primary=primary, human_drawn=human_drawn, not_counted_reason=not_counted,
                   image_sha_match=(scene_live.get('image_sha256') == scene_image_sha.get(scene)) if scene_live else None,
                   frozen_key=list(key), lineage=lineage_row)
        return row

    out = []
    for d in rows_in:
        hop = hops.get((d['scene_id'], d['object_id']))
        held = d.get('replaces_held') or ()
        if held:                                      # a replacer standing in for the copies it took over: one row per frozen key
            for cid in held:
                claims = len((review_of.get((d['scene_id'], cid)) or {}).get('taken_over_by') or ())
                takeover = {'copy_id': cid, 'claims': claims}
                if d.get('replacer_kind') == 'body_carry':
                    takeover.update(replacer='body_carry', registration=d.get('body_registration'))
                out.append(emit(d, hops[(d['scene_id'], cid)], takeover))
            if hop is None:
                continue                              # a detection is never also an ordinary window row
            # a body carry is a carried copy too: its own carried_from hop is listed below, read as for any copy (and refused as before)
        if d['cls'] == 'skipped' and hop is None:
            continue
        out.append(emit(d, hop))
    _one_decision_per_frozen_key(out)
    pseudo_small = sorted((p for p in pseudo.values() if p['area_m2'] is not None and p['area_m2'] < SMALL_M2),
                          key=lambda p: (str(p['scene_id']), str(p['object_id'])))
    return {'rows': out, 'refs': refs, 'judged_scenes': sorted(judged), 'split_file_sha256': sha, 'pseudo_small': pseudo_small,
            'scene_image_sha256': scene_image_sha}


def _one_decision_per_frozen_key(rows):
    """A frozen key counts at most once: a counted source decision (primary roof / not_roof / roof_part on the key's own scene)
    wins and every carried copy of it, and every detection standing in for one, is listed duplicate_decision_on_recapture;
    copies in two scenes or twice in one count nowhere (ambiguous_carry); a copy two detections claim through replaces counts
    nowhere (ambiguous_takeover on each claimant's row). Decides nothing: it only withdraws a row from the primary and names why."""
    counted = {(r['scene_id'], r['object_id']) for r in rows
               if r['lineage'] is None and r['primary'] and r['cls'] in ('roof', 'not_roof', 'roof_part')}
    for r in rows:
        lin = r['lineage']
        if lin is None or lin['refusals'] or not r['frozen_key']:
            continue
        key = tuple(r['frozen_key'])
        lin['source_decision_counted'] = key in counted
        contested = (lin.get('takeover') or {}).get('claims', 0) > 1
        if not lin['source_decision_counted'] and lin['copies'] <= 1 and not contested:
            continue
        r['primary'] = False
        if r['decided'] and r['path'] and r['small'] and r['provenance_kind'] in HUMAN:
            r['not_counted_reason'] = ('duplicate_decision_on_recapture' if lin['source_decision_counted'] else
                                       'ambiguous_carry' if lin['copies'] > 1 else 'ambiguous_takeover')


def _small_either(row):
    """Small by the snapshot when joined; by review.json model.area_m2 only for a decided object outside the snapshot (a listing)."""
    if row['path']:
        return row['small']
    return row['review_area_m2'] is None or row['review_area_m2'] < SMALL_M2


def population(joined):
    """Counts of the block's population: primary decided by class and provenance, the strata beside it, unjoined reasons."""
    rows = joined['rows']
    primary = [r for r in rows if r['primary'] and r['decided']]
    by_class, by_prov = Counter(r['cls'] for r in primary), Counter(r['provenance_kind'] for r in primary)
    agent = [r for r in rows if r['provenance_kind'] == 'agent' and r['status'] in DECIDED and r['judged_scene'] and _small_either(r)]
    drawn = [r for r in rows if r['human_drawn'] and r['decided']]
    unjoined = [r for r in rows if r['decided'] and r['path'] is None and _small_either(r)]
    reasons = Counter(reason for r in unjoined for reason in r['unjoined_reasons'])
    listed = Counter(r['not_counted_reason'] for r in rows if r.get('not_counted_reason'))
    return {'decisions_total': sum(1 for r in rows if r['decided']), 'primary_decided': len(primary),
            'by_class': {c: by_class[c] for c in CLASSES}, 'by_provenance': {k: by_prov[k] for k in HUMAN},
            'roof_part': sum(1 for r in rows if r['primary'] and r['cls'] == 'roof_part'),
            'scenes': len({r['scene_id'] for r in primary}), 'scene_ids': sorted({r['scene_id'] for r in primary}),
            'agent_authored_excluded': len(agent), 'agent_authored_excluded_by_status': dict(sorted(Counter(r['status'] for r in agent).items())),
            'human_drawn_lt_25': len(drawn), 'human_drawn_by_class': {c: sum(1 for r in drawn if r['cls'] == c) for c in CLASSES},
            'unjoined': len(unjoined), 'unjoined_by_reason': {k: reasons[k] for k in REASONS},
            'joined_not_counted': sum(listed.values()), 'joined_not_counted_by_reason': {k: listed[k] for k in NOT_COUNTED},
            'strata': dict(sorted(Counter(r['stratum'] for r in rows if r['stratum']).items()))}


def joined_not_counted(joined):
    """Listing of decided small human decisions that join the snapshot but are neither primary nor human-drawn (never a count)."""
    return sorted(({'scene_id': r['scene_id'], 'object_id': r['object_id'], 'status': r['status'], 'class': r['cls'],
                    'provenance_kind': r['provenance_kind'], 'path': r['path'], 'ann_id': r['ann_id'], 'ref_keys': r['ref_keys'],
                    'snapshot_area_m2': r['snapshot_area_m2'], 'source': r['source'], 'live': r['live'], 'fresh': r.get('fresh'),
                    'reason': r['not_counted_reason']} for r in joined['rows'] if r.get('not_counted_reason')),
                  key=lambda o: (str(o['scene_id']), str(o['object_id'])))


def human_drawn_rows(joined):
    return sorted(({'scene_id': r['scene_id'], 'object_id': r['object_id'], 'status': r['status'], 'class': r['cls'],
                    'provenance_kind': r['provenance_kind'], 'ref_keys': r['ref_keys'], 'snapshot_area_m2': r['snapshot_area_m2']}
                   for r in joined['rows'] if r['human_drawn'] and r['decided']), key=lambda o: (str(o['scene_id']), str(o['object_id'])))


def agent_traced_references(joined):
    refs = joined['refs']
    tiers = sorted({str(r['tier']) for r in refs.values()})
    return {'references': len(refs), 'agent_traced': sum(1 for r in refs.values() if r['agent_traced_reference']),
            'by_tier': {t: {'references': sum(1 for r in refs.values() if str(r['tier']) == t),
                            'agent_traced': sum(1 for r in refs.values() if str(r['tier']) == t and r['agent_traced_reference'])} for t in tiers},
            'keys': sorted(k for k, r in refs.items() if r['agent_traced_reference'])}


# --- detection from rows -----------------------------------------------------------------------------------------------

def _refuse_below_collection(cut, collected):
    if collected is not None and cut < collected - 1e-9:
        raise SystemExit(f'cut {cut} is below the collection threshold {collected}: rows below it were never written')


def _overlap_ious(row, target):
    if target['path'] == 'P':
        return [o['iou'] for o in row.get('pseudo_overlaps') or () if o['id'] == target['ann_id']]
    keys = set(target.get('ref_keys') or ())
    return [o['iou'] for o in row.get('ref_overlaps') or () if o['ref'] in keys]


def detected(preds_of_arm, target, cut, collected=None):
    """(detected, best_iou, max_score): a row with score strictly above the cut (reference_eval.select) overlapping the target
    at IoU >= reference_eval.MATCH_IOU; best_iou over rows above the cut, max_score over the detecting rows."""
    _refuse_below_collection(cut, collected)
    best_iou, max_score = 0., None
    for row in ev.select(preds_of_arm, cut):
        for iou in _overlap_ious(row, target):
            best_iou = max(best_iou, iou)
            if iou >= ev.MATCH_IOU and (max_score is None or row['score'] > max_score):
                max_score = row['score']
    return max_score is not None, best_iou, max_score


def index_hits(predictions):
    """{arm: {('P', ann id) | ('R', ref key): [(score, iou)]}} over every collected row."""
    index = defaultdict(lambda: defaultdict(list))
    for row in predictions:
        arm, score = row['arm'], row['score']
        for o in row.get('pseudo_overlaps') or ():
            index[arm][('P', o['id'])].append((score, o['iou']))
        for o in row.get('ref_overlaps') or ():
            index[arm][('R', o['ref'])].append((score, o['iou']))
    return index


def _hits(index, arm, obj):
    table = index.get(arm) or {}
    if obj['path'] == 'P':
        return list(table.get(('P', obj['ann_id']), ()))
    return [h for key in obj.get('ref_keys') or () for h in table.get(('R', key), ())]


def _arm_fact(hits, cut):
    above = [iou for score, iou in hits if score > cut]
    match = [(score, iou) for score, iou in hits if iou >= ev.MATCH_IOU]
    top = max(match) if match else None
    max_score = float(top[0]) if top else None
    survives = [c for c in GRID if max_score is not None and max_score > c]
    return {'cut': float(cut), 'detected': bool(max_score is not None and max_score > cut),
            'best_iou': round(float(max(above, default=0.)), 4), 'max_score': max_score,
            'iou_at_max_score': float(top[1]) if top else None,
            'survival': {f'{c:.2f}': c in survives for c in GRID}, 'last_surviving_cut': max(survives) if survives else None}


# --- the precision block -----------------------------------------------------------------------------------------------

def pairing(context):
    """(second, first) pairs of the run: tuned against each arm it is paired with in context['pairs'] (tuned:base always)."""
    arms = list(context['arms'])
    pairs = [tuple(p.split(':', 1)) for p in context.get('pairs') or ()]
    out = [(s, f) for s, f in pairs if s == 'tuned' and f in arms]
    if 'tuned' in arms and 'base' in arms and ('tuned', 'base') not in out:
        out.insert(0, ('tuned', 'base'))
    return out


def _share(k, n):
    return {'successes': int(k), 'total': int(n), 'share': round(float(k) / float(n), 4) if n else None, 'wilson95': wilson95(k, n)}


def _shares(det, is_roof):
    return {'confirmed_detected_share': _share((det & is_roof).sum(), is_roof.sum()),
            'rejected_detected_share': _share((det & ~is_roof).sum(), (~is_roof).sum()),
            'detected_roof_share': _share((det & is_roof).sum(), det.sum())}


def _share_terms(t):
    return {'confirmed_detected_share': (t['s_roof'], t['roof'], t['f_roof'], t['roof']),
            'rejected_detected_share': (t['s_not'], t['not'], t['f_not'], t['not']),
            'detected_roof_share': (t['s_roof'], t['s_roof'] + t['s_not'], t['f_roof'], t['f_roof'] + t['f_not'])}


def cluster_bootstrap(det_second, det_first, is_roof, scene_ids, draws=BOOT_DRAWS, seed=BOOT_SEED):
    """Scene-cluster paired bootstrap of share(second) - share(first): every decided object of a drawn scene enters together."""
    scenes = sorted(set(scene_ids))
    if not scenes:
        return dict.fromkeys(SHARES)
    where = {s: i for i, s in enumerate(scenes)}
    pos = np.array([where[s] for s in scene_ids])

    def per_scene(flags):
        return np.bincount(pos, weights=np.asarray(flags, dtype=float), minlength=len(scenes))
    cols = {'roof': per_scene(is_roof), 'not': per_scene(~is_roof), 's_roof': per_scene(det_second & is_roof),
            's_not': per_scene(det_second & ~is_roof), 'f_roof': per_scene(det_first & is_roof), 'f_not': per_scene(det_first & ~is_roof)}
    draw = np.random.default_rng(seed).integers(0, len(scenes), size=(draws, len(scenes)))
    totals = {k: v[draw].sum(axis=1) for k, v in cols.items()}
    observed = _share_terms({k: float(v.sum()) for k, v in cols.items()})
    out = {}
    with np.errstate(divide='ignore', invalid='ignore'):
        for name, (sk, sn, fk, fn) in _share_terms(totals).items():
            diff = sk / sn - fk / fn
            ok = np.isfinite(diff)
            osk, osn, ofk, ofn = observed[name]
            out[name] = {'observed': round(osk / osn - ofk / ofn, 4) if osn and ofn else None,
                         'ci95': [round(float(np.percentile(diff[ok], 2.5)), 4), round(float(np.percentile(diff[ok], 97.5)), 4)]
                         if ok.any() else None, 'draws': int(draws), 'draws_defined': int(ok.sum()), 'unit': 'scene', 'seed': int(seed)}
    return out


def _pair(second, first, det, is_roof, scene_ids, view, draws, seed):
    a, b = det[second], det[first]
    discordant, p = {}, {}
    for name, mask in (('roof', is_roof), ('not_roof', ~is_roof)):
        second_only, first_only = int((a & ~b & mask).sum()), int((~a & b & mask).sum())
        discordant[name] = {'second_only': second_only, 'first_only': first_only}
        p[name] = mcnemar_p(second_only, first_only)
    keeps = None
    if view == 't045':
        keeps = bool(int((a & ~is_roof).sum()) <= int((b & ~is_roof).sum()) + 1 and int((a & is_roof).sum()) >= int((b & is_roof).sum()) - 1)
    return {'pair': f'{second}:{first}', 'second': second, 'first': first, 'discordant': discordant, 'mcnemar_p': p,
            'bootstrap': cluster_bootstrap(a, b, is_roof, scene_ids, draws, seed), 'keeps_human_precision': keeps}


def _prediction_level(predictions, cuts, objs, arms):
    """Descriptive: rows above an arm's cut with area_m2 < 25, split by the decided object they overlap at IoU >= 0.5."""
    p_cls = {obj['ann_id']: obj['cls'] for obj in objs if obj['path'] == 'P'}
    r_cls = {key: obj['cls'] for obj in objs if obj['path'] == 'R' for key in obj['ref_keys']}
    bucket = {'roof': 'judged_roof', 'not_roof': 'judged_not_roof', 'roof_part': 'judged_part'}
    out = {arm: dict.fromkeys(('predictions_lt_25', 'judged_roof', 'judged_not_roof', 'judged_part', 'undecided'), 0) for arm in arms}
    for row in predictions:
        arm, area = row['arm'], row.get('area_m2')
        if arm not in out or not row['score'] > cuts[arm] or area is None or area >= SMALL_M2:
            continue
        out[arm]['predictions_lt_25'] += 1
        best = None
        for o in row.get('pseudo_overlaps') or ():
            if o['iou'] >= ev.MATCH_IOU and o['id'] in p_cls and (best is None or o['iou'] > best[0]):
                best = (o['iou'], p_cls[o['id']])
        for o in row.get('ref_overlaps') or ():
            if o['iou'] >= ev.MATCH_IOU and o['ref'] in r_cls and (best is None or o['iou'] > best[0]):
                best = (o['iou'], r_cls[o['ref']])
        out[arm][bucket[best[1]] if best else 'undecided'] += 1
    return out


def precision_block(context, joined_rows, predictions, cuts, view, draws=BOOT_DRAWS, seed=BOOT_SEED):
    """Object-level block for one view. Below the N rule: read 'n/a (...)', no share, no pair statistic, the per-object facts.
    When it passes: Wilson shares, discordant counts, McNemar p and the scene-cluster bootstrap per pairing arm."""
    arms = list(context['arms'])
    collected = (context.get('meta') or {}).get('rows_collected_at_threshold')
    missing = [a for a in arms if a not in cuts]
    if missing:
        raise SystemExit(f'view {view}: no cut for arm(s) {missing} (cuts {cuts}); a missing arm is never defaulted')
    for arm in arms:
        _refuse_below_collection(cuts[arm], collected)
    index = index_hits(predictions)
    objs = sorted((r for r in joined_rows if r['primary'] and r['cls'] in ('roof', 'not_roof', 'roof_part')),
                  key=lambda r: (str(r['scene_id']), str(r['object_id'])))
    arm_facts = {arm: [_arm_fact(_hits(index, arm, obj), cuts[arm]) for obj in objs] for arm in arms}
    facts = [{'scene_id': obj['scene_id'], 'object_id': obj['object_id'], 'class': obj['cls'], 'status': obj['status'],
              'provenance_kind': obj['provenance_kind'], 'path': obj['path'], 'ann_id': obj['ann_id'], 'ref_keys': obj['ref_keys'],
              'image_id': obj['image_id'], 'snapshot_area_m2': obj['snapshot_area_m2'], 'review_area_m2': obj.get('review_area_m2'),
              'agent_traced_reference': obj.get('agent_traced_reference'), 'model_signature_match': obj.get('model_signature_match'),
              'image_sha_match': obj.get('image_sha_match'), 'review_revision': obj.get('review_revision'),
              'geometry_reviewed': obj.get('geometry_reviewed'), 'review_updated_at': obj.get('review_updated_at'),
              'review_sha256': obj.get('review_sha256'),                               # all seven FACTS_COLUMNS, recorded
              'decision_on_body_not_target': obj.get('decision_on_body_not_target'), 'body_covers': obj.get('body_covers'),
              'lineage_agreement': obj.get('lineage_agreement'), 'lineage_agreement_detail': obj.get('lineage_agreement_detail'),
              'url': url_for(obj['scene_id'], obj['object_id']), 'arms': {arm: arm_facts[arm][i] for arm in arms}}
             for i, obj in enumerate(objs)]
    decided = [i for i, obj in enumerate(objs) if obj['cls'] in CLASSES]
    is_roof = np.array([objs[i]['cls'] == 'roof' for i in decided], dtype=bool)
    scene_ids = [objs[i]['scene_id'] for i in decided]
    det = {arm: np.array([arm_facts[arm][i]['detected'] for i in decided], dtype=bool) for arm in arms}
    n, roof = len(decided), is_roof.sum()
    not_roof, scenes = n - roof, len(set(scene_ids))
    passes, read = n_rule_read(n, roof, not_roof, scenes)
    pairs = pairing(context)
    if passes:
        shares = {arm: _shares(det[arm], is_roof) for arm in arms}
        pair_rows = [_pair(s, f, det, is_roof, scene_ids, view, draws, seed) for s, f in pairs]
    else:
        shares = None
        pair_rows = [{'pair': f'{s}:{f}', 'second': s, 'first': f, 'read': read} for s, f in pairs]
    object_counts = {arm: {'roof': {'decided': roof, 'detected': (det[arm] & is_roof).sum()},
                           'not_roof': {'decided': not_roof, 'detected': (det[arm] & ~is_roof).sum()}} for arm in arms}
    return _plain({'view': view, 'cuts': {a: float(cuts[a]) for a in arms}, 'arms': arms, 'rows_collected_at_threshold': collected,
                   'grid': list(GRID), 'n_rule': {**N_RULE, 'passes': passes, 'N': n, 'roof': roof, 'not_roof': not_roof, 'scenes': scenes},
                   'read': read, 'object_counts': object_counts, 'shares': shares, 'pairs': pair_rows, 'keeps_rule': KEEPS_RULE,
                   'roof_part': sum(1 for obj in objs if obj['cls'] == 'roof_part'), 'facts': facts,
                   'prediction_level': _prediction_level(predictions, cuts, objs, arms)})


def ui_only(facts):
    """UI-only sensitivity read of a block (RULES provenance.included): the decided primary objects without the chat rows, read
    under the same N rule; shares (Wilson 95, int() casts) only when that rule passes, else the n/a read alone."""
    rows = [f for f in facts if f['class'] in CLASSES and f['provenance_kind'] == 'ui']
    chat = sum(1 for f in facts if f['class'] in CLASSES and f['provenance_kind'] == 'chat')
    roof = sum(1 for f in rows if f['class'] == 'roof')
    scenes = len({f['scene_id'] for f in rows})
    passes, read = n_rule_read(len(rows), roof, len(rows) - roof, scenes)
    shares = None
    if passes:
        is_roof = np.array([f['class'] == 'roof' for f in rows], dtype=bool)
        shares = {arm: _shares(np.array([f['arms'][arm]['detected'] for f in rows], dtype=bool), is_roof) for arm in rows[0]['arms']}
    return _plain({'N': len(rows), 'roof': roof, 'not_roof': len(rows) - roof, 'scenes': scenes, 'chat_removed': chat,
                   'passes': passes, 'read': read, 'shares': shares})


def matched_cuts(run_dir, block, arms=None):
    """(arm cuts, source) from the top-level arm_thresholds of <run>/reference-eval-<block>-matched/summary.json (r3_postfilter
    flattens its meta into the payload; .meta is null). Never recomputed from calibration.json; a missing arm raises."""
    path = Path(run_dir) / f'reference-eval-{block}-matched' / 'summary.json'
    if not path.is_file():
        raise SystemExit(f'{path}: no matched summary for block {block}')
    summary = json.load(open(path))
    cuts = summary.get('arm_thresholds')
    if not isinstance(cuts, dict) or not cuts:
        raise SystemExit(f'{path}: no top-level arm_thresholds (the matched view is read from the summary, never recomputed)')
    if arms is not None:
        missing = [a for a in arms if a not in cuts]
        if missing:
            raise SystemExit(f'{path}: arm_thresholds has no cut for arm(s) {missing}')
    return {a: float(v) for a, v in cuts.items()}, summary.get('arm_thresholds_source')


# --- live count, targets and the reviewer guide ------------------------------------------------------------------------

def _prior_items(path):
    if not path or not Path(path).is_file():
        return []
    groups = json.load(open(path)).get('groups') or {}
    return [(str(i[0]), str(i[1]), float(i[2]) if len(i) > 2 and i[2] is not None else None)
            for i in groups.get('small_roof_detections_test_tiles') or () if isinstance(i, (list, tuple)) and len(i) >= 2]


def _target_exclusion(review_row):
    """Why a live small snapshot object is not asked: no decision on a machine label or pending import can count, an
    assistant-noted object is listed apart unless the reviewer's own decision on it counts (a valid marker, or an accepted
    Fieldwork draft that is decided; one re-opened to needs_adjustment stays listed apart, RULES targets: no Assistant note),
    a decided / superseded / part object is never asked again."""
    if review_row is None:
        return None
    if review_row['authority_blocker']:
        return review_row['authority_blocker']
    fresh = review_row.get('fresh')
    if review_row['agent_note'] and fresh != 'valid' and not (fresh == 'accepted_import' and review_row['decided']):
        return 'agent_noted'
    if review_row['status'] in DECIDED:
        return 'decided'
    if review_row['status'] == 'superseded':
        return 'superseded_by_takeover' if review_row.get('taken_over_by') else 'superseded'
    if review_row['cls'] == 'skipped':
        return 'kind_not_building'
    if review_row['cls'] == 'roof_part':
        return 'roof_part'
    return None


def _detection_item(d_row, frozen_item):
    """A detection that took over the frozen slice as an item on the window scene: D's own id, status, note and kind, the frozen
    row's area and key; targets() merges the keys one D covers into one asked row (frozen_keys, area the largest slice)."""
    return {'scene_id': d_row['scene_id'], 'id': d_row['object_id'], 'area_m2': frozen_item['area_m2'],
            'url': url_for(d_row['scene_id'], d_row['object_id']), 'status': d_row['status'] or 'candidate',
            'note': _short(d_row.get('notes')), 'kind': d_row.get('kind'), 'replaces': d_row.get('replaces') or [],
            'frozen_keys': [[frozen_item['scene_id'], frozen_item['id']]], 'source_scene_id': frozen_item['scene_id']}


def _copy_item(copy_row, frozen_item):
    """The carried copy as an asked item: on the window scene, with the frozen row's area and its source named."""
    return {'scene_id': copy_row['scene_id'], 'id': copy_row['object_id'], 'area_m2': frozen_item['area_m2'],
            'url': url_for(copy_row['scene_id'], copy_row['object_id']), 'status': copy_row['status'] or 'candidate',
            'note': _short(copy_row.get('notes')), 'kind': copy_row.get('kind'),
            'source_scene_id': frozen_item['scene_id'], 'source_object_id': frozen_item['id']}


def targets(joined, decisions, lineage=None):
    """(pool, targets, excluded) over the snapshot's small machine_pseudo objects on the judged tiles (RULES human_precision.targets).
    pool = live production objects on an unchanged image (one per frozen key); targets = the undecided ones; excluded = (item,
    reason) for the rest.

    A frozen key whose single carried copy joins through the hop is asked once, as that copy on the window scene (the old target is
    excluded recaptured:<window>; the copy is subject to the same exclusions); a decided source (decided / roof_part) excludes its
    copies decided_on_source; copies in two windows are ambiguous_carry and the old target stays; a refused copy is listed with its
    first refusal and the old target stays, listing every window the registry names beside its own URL. A copy the reviewer re-kinded
    is still a copy: alone it takes the key (old target recaptured:<window>, copy kind_not_building, asked nowhere); beside another
    copy it makes the key ambiguous, exactly as join() reads it.

    Takeover: when the single copy that joins was taken over (taken_over_by), it is excluded superseded_by_takeover and the
    detection is asked instead, once, listing every frozen key it covers (frozen_keys; area the largest slice) and subject to the
    same exclusions as any review object; a copy two detections claim is ambiguous_takeover with every claimant (asked nowhere);
    a decided source or an ambiguous carry excludes the claimants beside the copies (decided_on_source, ambiguous_carry).

    A slice whose supersession is a body carry the hop REFUSED (replaces_refused on the body's row, amendment 2026-10-06: the
    registry row withdrawn or unverified, the residual outside DESIGN, the slice not naming the body back, ...) is not dropped as
    'superseded': it is asked again as the target for its key exactly as before the body, with body_refused naming the body and
    the refusal, so a refusal never moves targets_remaining without a decision (housekeeping 2026-10-06; the RULES join_order /
    targets text of 2026-10-06T00:12:32Z reads 'the key asked nowhere until repaired by hand' here and waits for its dated
    amendment). A body over a body (two_hops) and a claimant excluded on its own status are unchanged: asked nowhere until the
    data is repaired by hand."""
    live = decisions['live']
    review_of = {(r['scene_id'], r['object_id']): r for r in decisions['rows']}
    body_refused = defaultdict(dict)        # (window, slice id) -> {body id: refusal}: body links followed nowhere (amendment 2026-10-06)
    for r in decisions['rows']:
        if r.get('replacer_kind') == 'body_carry':
            for cid, why in (r.get('replaces_refused') or {}).items():
                body_refused[(r['scene_id'], cid)][r['object_id']] = why
    holds, refused, takers = defaultdict(list), defaultdict(list), defaultdict(list)
    for r in joined['rows']:
        lin = r.get('lineage')
        if lin and r.get('frozen_key'):
            if lin.get('takeover'):
                (takers if not lin['refusals'] else refused)[tuple(r['frozen_key'])].append(r)   # a refused claimant is listed nowhere
            else:
                (refused if lin['refusals'] else holds)[tuple(r['frozen_key'])].append(r)
    pool, out, excluded, asked = [], [], [], {}
    for p in joined['pseudo_small']:
        scene, oid = p['scene_id'], p['object_id']
        scene_live = live.get(scene)
        item = {'scene_id': scene, 'id': oid, 'area_m2': round(float(p['area_m2']), 2), 'url': url_for(scene, oid)}
        if not scene_live or oid not in scene_live['objects']:
            excluded.append((item, 'not_live'))
            continue
        if scene_live.get('image_sha256') != joined['scene_image_sha256'].get(scene):
            excluded.append((item, 'image_changed'))
            continue
        if str(scene_live['objects'][oid].get('source') or '').startswith('reviewer'):
            excluded.append((item, 'not_production'))
            continue
        pool.append(item)
        review_row = review_of.get((scene, oid))
        item = {**item, 'status': (review_row or {}).get('status') or 'candidate',
                'note': _short((review_row or {}).get('notes') or scene_live['objects'][oid].get('notes'))}
        windows = registry_windows(lineage, (scene, oid))
        if windows:
            item['window'] = windows
        reason = _target_exclusion(review_row)
        copies = holds.get((scene, oid), [])
        claimants = [r for r in takers.get((scene, oid), ()) if r['lineage']['refusals'] == []]
        for copy in refused.get((scene, oid), ()):
            if not copy['lineage'].get('takeover'):
                excluded.append((_copy_item(copy, item), copy['lineage']['refusals'][0]))
        if reason in ('decided', 'roof_part'):
            excluded.extend((_copy_item(copy, item), 'decided_on_source') for copy in copies)
            excluded.extend((_detection_item(d, item), 'decided_on_source') for d in claimants)
        elif len(copies) > 1:
            excluded.extend((_copy_item(copy, item), 'ambiguous_carry') for copy in copies)
            excluded.extend((_detection_item(d, item), 'ambiguous_carry') for d in claimants)
        elif copies:
            copy = copies[0]
            excluded.append(({**item, 'kind': (review_row or {}).get('kind')}, f"recaptured:{copy['scene_id']}"))
            copy_item = _copy_item(copy, item)
            copy_row = review_of.get((copy['scene_id'], copy['object_id']))
            if claimants and any(d['lineage']['takeover']['claims'] > 1 for d in claimants):
                excluded.append((copy_item, 'ambiguous_takeover'))
                excluded.extend((_detection_item(d, item), 'ambiguous_takeover') for d in claimants)
                continue
            if claimants:
                excluded.append((copy_item, 'superseded_by_takeover'))
                d = claimants[0]
                d_key = (d['scene_id'], d['object_id'])
                d_reason = _target_exclusion(review_of.get(d_key))
                if d_reason:
                    excluded.append((_detection_item(d, item), d_reason))
                elif d_key in asked:                                # one asked row per detection, every frozen key it covers listed
                    asked[d_key]['frozen_keys'].append([scene, oid])
                    asked[d_key]['area_m2'] = max(asked[d_key]['area_m2'], item['area_m2'])
                else:
                    asked[d_key] = _detection_item(d, item)
                    if (d['lineage'].get('takeover') or {}).get('replacer') == 'body_carry':
                        asked[d_key]['replacer'] = 'body_carry'      # the asked target is a carried whole-roof body (amendment 2026-10-06)
                    out.append(asked[d_key])
                continue
            copy_reason = _target_exclusion(copy_row)
            refused_bodies = body_refused.get((copy['scene_id'], copy['object_id']))
            if copy_reason == 'superseded' and refused_bodies:      # superseded by a body whose link is refused: the slice stands again
                copy_item, copy_reason = {**copy_item, 'body_refused': dict(refused_bodies)}, None
            (excluded.append((copy_item, copy_reason)) if copy_reason else out.append(copy_item))
            continue
        if reason:
            excluded.append(({**item, 'kind': (review_row or {}).get('kind')}, reason))
        else:
            out.append(item)
    return pool, out, excluded


def interleave(items, per_pass=PER_PASS):
    """Scenes by remaining targets (descending), `per_pass` objects per scene per pass, area-descending within a scene."""
    by_scene = defaultdict(list)
    for item in items:
        by_scene[item['scene_id']].append(item)
    for rows in by_scene.values():
        rows.sort(key=lambda t: (-t['area_m2'], str(t['id'])))
    order = sorted(by_scene, key=lambda s: (-len(by_scene[s]), str(s)))
    out, depth = [], 0
    while any(len(by_scene[s]) > depth for s in order):
        for scene in order:
            out.extend(by_scene[scene][depth:depth + per_pass])
        depth += per_pass
    return out, order, by_scene


def _live_state(block='v2', run_dir=COUNT_RUN, context=None, split_path=None, scenes_root=SCENES, prior_set=PRIOR_SET,
                rules_path=RULES_PATH, repo=REPO, recapture_dir=RECAPTURE_DIR):
    context_path = None
    if context is None:
        context_path = Path(run_dir) / f'reference-eval-{block}' / 'context.json'
        context = json.load(open(context_path))
    split_path = Path(split_path) if split_path else split_path_for(context, repo)
    prior = _prior_items(prior_set)
    judged = {v.get('scene_id') for v in context['images'].values()}
    lineage = load_lineage(recapture_dir)
    decisions = load_decisions(scenes_root, extra_live=judged | {s for s, _, _ in prior}, lineage=lineage)
    joined = join(context, split_path, decisions, lineage=lineage)
    pop = population(joined)
    pool, remaining, excluded = targets(joined, decisions, lineage)
    ordered, scene_order, by_scene = interleave(remaining)
    rows = joined['rows']
    primary = sorted((r for r in rows if r['primary'] and r['decided']), key=lambda r: (str(r['scene_id']), str(r['object_id'])))
    already = [{'scene_id': r['scene_id'], 'id': r['object_id'], 'status': r['status'], 'class': r['cls'],
                'provenance_kind': r['provenance_kind'], 'path': r['path'], 'area_m2': round(float(r['snapshot_area_m2']), 2),
                'review_area_m2': r['review_area_m2'], 'source': r['source'], 'url': url_for(r['scene_id'], r['object_id']),
                **({'frozen_key': r['frozen_key']} if r['lineage'] else {}),
                **({'replaces_copy': r['lineage']['takeover']['copy_id']} if r['lineage'] and r['lineage'].get('takeover') else {})}
               for r in primary]
    carried_rows = sorted(({'scene_id': r['scene_id'], 'id': r['object_id'], 'frozen_key': r['frozen_key'], 'status': r['status'],
                            'class': r['cls'], 'provenance_kind': r['provenance_kind'], 'path': r['path'],
                            'snapshot_area_m2': r['snapshot_area_m2'], 'review_area_m2': r['review_area_m2'],
                            'refusals': r['lineage']['refusals'], 'design_sha_match': r['lineage']['design_sha_match'],
                            'copies': r['lineage']['copies'],
                            'source_decision_counted': r['lineage']['source_decision_counted'], 'primary': r['primary'],
                            'not_counted_reason': r['not_counted_reason'], 'fresh_state': r['fresh'],
                            'url': url_for(r['scene_id'], r['object_id']),
                            'source_url': url_for(*r['frozen_key']) if r['frozen_key'] else None}
                           for r in rows if r['lineage'] is not None and not r['lineage'].get('takeover')),
                          key=lambda o: (str(o['scene_id']), str(o['id'])))
    takeover_rows = sorted(({'scene_id': r['scene_id'], 'id': r['object_id'], 'copy_id': r['lineage']['takeover']['copy_id'],
                             'claims': r['lineage']['takeover']['claims'], 'frozen_key': r['frozen_key'], 'status': r['status'],
                             'class': r['cls'], 'provenance_kind': r['provenance_kind'], 'path': r['path'],
                             'snapshot_area_m2': r['snapshot_area_m2'], 'review_area_m2': r['review_area_m2'],
                             'refusals': r['lineage']['refusals'], 'copies': r['lineage']['copies'],
                             'source_decision_counted': r['lineage']['source_decision_counted'], 'primary': r['primary'],
                             'not_counted_reason': r['not_counted_reason'], 'fresh_state': r['fresh'],
                             'url': url_for(r['scene_id'], r['object_id']),
                             'source_url': url_for(*r['frozen_key']) if r['frozen_key'] else None,
                             # a body-carry replacer (amendment 2026-10-06) names itself and its registry facts; a detection row is as before
                             **({'replacer': 'body_carry', 'registration': r['lineage']['takeover'].get('registration')}
                                if r['lineage']['takeover'].get('replacer') == 'body_carry' else {})}
                            for r in rows if r['lineage'] is not None and r['lineage'].get('takeover')),
                           key=lambda o: (str(o['scene_id']), str(o['id']), str(o['copy_id'])))
    by_key = {(r['scene_id'], r['object_id']): r for r in decisions['rows']}
    lineage_summary = {'design_sha256': lineage['design_sha256'], 'design_path': _rel(lineage['design_path'], repo),
                       'design_accepted': lineage.get('design_accepted') or {},
                       'registry_files': lineage['registry_files'], 'registry_rows': lineage['registry_rows'],
                       'carried_copies': len(carried_rows), 'carried_joined': sum(1 for o in carried_rows if not o['refusals']),
                       'carried_refused_by_reason': {k: sum(1 for o in carried_rows if k in o['refusals']) for k in CARRY_REFUSALS},
                       'carried_design_sha_match': {side: design_chain.tally(o['design_sha_match'][side] for o in carried_rows)
                                                    for side in ('transform', 'registry')},
                       'carried_primary': sum(1 for o in carried_rows if o['primary']),
                       'duplicate_decision_on_recapture': sum(1 for o in carried_rows if o['not_counted_reason'] == 'duplicate_decision_on_recapture'),
                       'ambiguous_carry': sum(1 for o in carried_rows if o['not_counted_reason'] == 'ambiguous_carry'),
                       'takeover_rows': len(takeover_rows), 'takeover_joined': sum(1 for o in takeover_rows if not o['refusals']),
                       'takeover_primary': sum(1 for o in takeover_rows if o['primary'] and o['class'] in CLASSES),
                       'ambiguous_takeover': sum(1 for o in takeover_rows if o['not_counted_reason'] == 'ambiguous_takeover'),
                       'superseded_by_takeover': sum(1 for r in decisions['rows'] if r.get('taken_over_by')),
                       # body-carry replacers (amendment 2026-10-06): held links emitted as takeover rows, and the refused links by name
                       'body_carry_rows': sum(1 for o in takeover_rows if o.get('replacer') == 'body_carry'),
                       'body_carry_joined': sum(1 for o in takeover_rows if o.get('replacer') == 'body_carry' and not o['refusals']),
                       'body_carry_links_refused': dict(Counter(reason for d in decisions['rows'] if d.get('replacer_kind') == 'body_carry'
                                                                for reason in (d.get('replaces_refused') or {}).values())),
                       # slices asked again for their key because the body over them was refused (never a silent drop of a target)
                       'body_carry_slices_restored': sum(1 for t in remaining if t.get('body_refused')),
                       # held links whose copy does not name D back in replacement_ids: bookkeeping drift, never a refusal (join_order 2026-10-02)
                       'replacement_mismatch': sum(1 for d in decisions['rows'] for cid in (d.get('replaces_held') or ())
                                                   if d['object_id'] not in (by_key.get((d['scene_id'], cid)) or {}).get('replacement_ids', []))}
    agent_required = sorted(({'scene_id': r['scene_id'], 'id': r['object_id'], 'status': r['status'], 'path': r['path'], 'source': r['source'],
                              'area_m2': round(float(r['snapshot_area_m2'] if r['path'] else r['review_area_m2'] or 0.), 2),
                              'url': url_for(r['scene_id'], r['object_id']), 'note': _short(r['notes'], 160),
                              'fresh_state': r['fresh'], 'enters': _enters(r)}
                             for r in rows if r['provenance_kind'] == 'agent' and r['status'] in DECIDED and r['judged_scene']
                             and _small_either(r)), key=lambda o: (-o['area_m2'], str(o['scene_id']), str(o['id'])))
    agent_other = sorted(({**item, 'note': _short(item.get('note'), 160)} for item, reason in excluded if reason == 'agent_noted'),
                         key=lambda o: (-o['area_m2'], str(o['scene_id']), str(o['id'])))
    status_of = {(r['scene_id'], r['object_id']): r['status'] for r in decisions['rows']}
    deferred = [{'scene_id': s, 'id': oid, 'area_m2': area, 'status': status_of.get((s, oid)) or 'candidate',
                 'live': oid in ((decisions['live'].get(s) or {}).get('objects') or {}), 'url': url_for(s, oid)}
                for s, oid, area in prior if s not in judged]
    not_judged = sorted(({'scene_id': r['scene_id'], 'id': r['object_id'], 'status': r['status'], 'provenance_kind': r['provenance_kind'],
                          'review_area_m2': r['review_area_m2'], 'unjoined_reasons': r['unjoined_reasons'],
                          'url': url_for(r['scene_id'], r['object_id'])}
                         for r in rows if r['decided'] and r['path'] is None and _small_either(r)),
                        key=lambda o: (str(o['scene_id']), str(o['id'])))
    passes, read = n_rule_read(pop['primary_decided'], pop['by_class']['roof'], pop['by_class']['not_roof'], pop['scenes'])
    n_rule = {**N_RULE, 'passes': bool(passes)}
    public = {'version': VERSION, 'block': block, 'computed_at': now_iso(), 'computed_at_local': local_time(),
              'primary_n': pop['primary_decided'], 'primary_by_class': pop['by_class'], 'primary_by_provenance': pop['by_provenance'],
              'primary_roof_part': pop['roof_part'], 'agent_authored_excluded': pop['agent_authored_excluded'],
              'agent_authored_excluded_by_status': pop['agent_authored_excluded_by_status'], 'human_drawn_lt_25': pop['human_drawn_lt_25'],
              'scenes': pop['scenes'], 'scene_ids': pop['scene_ids'], 'n_rule': n_rule, 'read': read,
              'targets_total': len(pool), 'targets_remaining': len(remaining),
              'target_exclusions': dict(sorted(Counter(reason for _, reason in excluded).items())),
              'next_scenes': [{'scene_id': s, 'remaining': len(by_scene[s]),
                               'objects': [{'id': t['id'], 'area_m2': t['area_m2'], 'url': t['url'], 'note': t['note'],
                                            **{k: t[k] for k in ('window', 'source_scene_id', 'source_object_id', 'replaces', 'frozen_keys', 'replacer',
                                                              'body_refused')
                                               if k in t}}
                                           for t in by_scene[s]]}
                              for s in scene_order],
              'already_counted': already,
              'primary_rows': [[r['scene_id'], r['object_id'], r['status'], round(r['snapshot_area_m2'], 1), r['source'], r['path']]
                               for r in primary],
              'agent_authored_required': agent_required, 'agent_authored_other': agent_other, 'deferred_unjudged': deferred,
              'decided_not_judged': not_judged, 'unjoined_by_reason': pop['unjoined_by_reason'],
              'joined_not_counted': [{**o, 'id': o['object_id'], 'url': url_for(o['scene_id'], o['object_id'])} for o in joined_not_counted(joined)],
              'joined_not_counted_by_reason': pop['joined_not_counted_by_reason'],
              'human_drawn_rows': [{**o, 'id': o['object_id'], 'url': url_for(o['scene_id'], o['object_id'])} for o in human_drawn_rows(joined)],
              'agent_traced_references': agent_traced_references(joined), 'strata': pop['strata'], 'decisions_total': pop['decisions_total'],
              'lineage': lineage_summary, 'carried_rows': carried_rows, 'takeover_rows': takeover_rows,
              'lineage_facts': lineage_listing(decisions), 'facts_columns': list(FACTS_COLUMNS),
              'context': _rel(context_path, repo) if context_path else None, 'split_file_sha256': joined['split_file_sha256'],
              'rules_sha256': sha256_file(rules_path) if rules_path else None, 'code_sha256': sha256_file(__file__),
              'review_snapshot_sha256': decisions['review_snapshot_sha256'], 'latest_snapshot_sha256': decisions['latest_snapshot_sha256'],
              'reviews_read': decisions['reviews_read'], 'wording': WORDING}
    return _plain(public), _plain(ordered)


def _enters(row):
    """Where an assistant-noted decision would count once the reviewer's own fresh decision opens its notes."""
    if row['path'] is None:
        return 'listed_unjoined'
    if row['production']:
        return 'primary'
    if str(row['source'] or '').startswith('reviewer'):
        return 'listed_assistant_drawn_outline'
    return 'listed_not_live'


def count(block='v2', **kwargs):
    """The live count at this instant (contract read by /api/review-progress); reads only."""
    return _live_state(block=block, **kwargs)[0]


def _line(item, extra=''):
    return f"- {item['scene_id']} · {item['id']} · {float(item['area_m2']):.1f} m² · {item['url']}{extra}"


def guide(block='v2', out_md=None, out_set=None, **kwargs):
    """Write the reviewer guide (markdown) and its set (JSON) from the live count at this instant; returns the count."""
    c, ordered = _live_state(block=block, **kwargs)
    by_class, pk = c['primary_by_class'], c['primary_by_provenance']
    lines = [f'# Small-roof review guide, block {block} (generated {c["computed_at_local"]})', '',
             f'Generated by `r5_precision guide --block {block}` from the live review files at {c["computed_at"]} '
             f'(review snapshot `{c["review_snapshot_sha256"][:16]}`); every count here was computed at that instant and moves with '
             'every save.', '',
             f'Usable small-roof decisions so far: {c["primary_n"]} (roof {by_class["roof"]}, not-roof {by_class["not_roof"]}; '
             f'app {pk["ui"]}, chat {pk["chat"]}) on {c["scenes"]} scenes. An agreement share is read only at 30 decisions with at '
             f'least 8 of each class across at least 6 scenes; now: {c["read"]}.', '',
             'How to decide (each link opens the edited layer on the object):',
             '- Right arrow (Looks right) = a roof; sheds, carports, garages and tank covers count as roofs.',
             '- Backspace = not a roof.',
             '- Left, then tick "Separate or merged objects" = part of a roof already outlined (counted apart, never as not-roof).',
             '- Left alone = still undecided (not counted).',
             'Nothing on this list is a decision until you make it; decided objects are never asked again. Every target below is live '
             'in latest.json on an image whose sha256 matches the frozen snapshot, and is under 25 m² in that snapshot.', '',
             f'## Targets ({c["targets_remaining"]} live undecided detections on {len(c["next_scenes"])} scenes, '
             f'{PER_PASS} per scene per pass)', '']
    lines += [_line(t, f' · note: "{t["note"]}"' if t.get('note') else '') for t in ordered] or ['- none']
    lines += ['', f'## Already counted ({c["primary_n"]})', '']
    lines += [_line(o, f" · {o['status']} ({o['class'].replace('_', '-')}, {o['provenance_kind']})") for o in c['already_counted']] or ['- none']
    lines += ['', f'## Required fresh decisions (agent-authored review) ({len(c["agent_authored_required"])})', '',
              "The assistant traced these on 23 September and its note is in their Review notes, so their status is the assistant's, "
              "not yours, and stays out of every count. To make your own decision count: start Review notes with `[reviewer] roof` "
              "or `[reviewer] not a roof` (your words first; the assistant's text may stay after them), press Right (roof) or "
              "Backspace (not a roof) so the status agrees with your words, and save. Other words first, words that disagree with "
              "the status, or a decision given in chat (the chat record is appended after the assistant's text) stay uncounted: "
              "without your marked words a status the assistant set cannot be told from one you set.", '']
    lines += [_line(o, f" · status {o['status']}; {FRESH_TEXT.get(o['fresh_state'], o['fresh_state'])}; once you decide it: "
                       f"{ENTERS_TEXT.get(o['enters'], o['enters'])}") for o in c['agent_authored_required']] or ['- none']
    if c['agent_authored_other']:
        lines += ['', 'Also on the judged tiles with the assistant\'s note in Review notes (not asked as ordinary targets and not '
                      'counted until your `[reviewer] roof` / `[reviewer] not a roof` words open the notes; where the assistant changed '
                      'the object type away from Building, only a Building decision can enter the small-roof count):', '']
        lines += [_line(o, f" · {o['status']}, type {o.get('kind') or 'building'}") for o in c['agent_authored_other']]
    if c['human_drawn_rows']:
        lines += ['', f"## Counted apart: human-drawn small roofs ({len(c['human_drawn_rows'])}; recall stratum, never pooled)", '']
        lines += [_line({**o, 'area_m2': o['snapshot_area_m2']}, f" · {o['status']} ({o['provenance_kind']})") for o in c['human_drawn_rows']]
    if c['joined_not_counted']:
        lines += ['', f"## Decided, joined, not counted ({len(c['joined_not_counted'])}; kept and listed, never counted)", '']
        lines += [_line({**o, 'area_m2': o['snapshot_area_m2']}, f" · {o['status']} · {o['reason']}") for o in c['joined_not_counted']]
    lines += ['', f'## Deferred: un-judged tiles ({len(c["deferred_unjudged"])} items of the 29 September guide)', '',
              'These tiles have no prediction rows in the frozen snapshot (inference needed, not tonight); a decision on them is kept '
              'but cannot join the judge.', '']
    lines += [_line(o, f" · {o['status']}") for o in c['deferred_unjudged']] or ['- none']
    if c['decided_not_judged']:
        lines += ['', 'Already decided off the judged snapshot (kept, not counted; reasons from the join):', '']
        lines += [f"- {o['scene_id']} · {o['id']} · {float(o['review_area_m2'] or 0.):.1f} m² (review) · {o['url']} · {o['status']} · "
                  f"{', '.join(o['unjoined_reasons'])}" for o in c['decided_not_judged']]
    text = '\n'.join(lines) + '\n'
    if _BANNED_WORD in text.lower():
        raise SystemExit('wording rule: the guide text names the banned word; refusing to write it')
    counts = {k: c[k] for k in ('primary_n', 'primary_by_class', 'primary_by_provenance', 'scenes', 'read', 'targets_total',
                                'targets_remaining', 'agent_authored_excluded', 'human_drawn_lt_25')}
    review_set = {'date': c['computed_at'][:10], 'version': VERSION, 'block': block, 'computed_at': c['computed_at'],
                  'rules_sha256': c['rules_sha256'], 'code_sha256': c['code_sha256'], 'review_snapshot_sha256': c['review_snapshot_sha256'],
                  'latest_snapshot_sha256': c['latest_snapshot_sha256'], 'counts': counts,
                  'groups': {'targets': ordered, 'already_counted': c['already_counted'], 'agent_authored_required': c['agent_authored_required'],
                             'agent_authored_other': c['agent_authored_other'], 'deferred_unjudged': c['deferred_unjudged'],
                             'decided_not_judged': c['decided_not_judged']}, 'wording': WORDING}
    if out_md:
        Path(out_md).parent.mkdir(parents=True, exist_ok=True)
        Path(out_md).write_text(text)
    if out_set:
        Path(out_set).parent.mkdir(parents=True, exist_ok=True)
        with open(out_set, 'w') as handle:
            json.dump(review_set, handle, indent=1, ensure_ascii=False)
            handle.write('\n')
    return c


# --- re-judge of the run dirs ------------------------------------------------------------------------------------------

_MISSING = object()


def _at_path(data, dotted):
    node = data
    for part in str(dotted).split('.'):
        if isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        elif isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return _MISSING
    return node


def load_rules(path):
    """(rules, sha256, bytes) of the pre-registered RULES, refused unless the code's n_rule / grid / bootstrap and agent /
    fresh-marker patterns are the registered ones and every amendment carries at (after written_before), field, from, to and a
    reason, the file holding the last amended value of each field."""
    data = Path(path).read_bytes()
    rules = json.loads(data)
    if rules.get('version') != RULES_VERSION:
        raise SystemExit(f'{path}: version {rules.get("version")!r} is not {RULES_VERSION}')
    hp = rules.get('human_precision') or {}
    registered = {k: (hp.get('n_rule') or {}).get(k) for k in N_RULE}
    boot = hp.get('bootstrap') or {}
    if registered != N_RULE or tuple(hp.get('grid') or ()) != GRID or (boot.get('draws'), boot.get('seed')) != (BOOT_DRAWS, BOOT_SEED):
        raise SystemExit(f'{path}: the pre-registered n_rule / grid / bootstrap differ from this code; amend RULES first')
    if (hp.get('agent_text_regex'), hp.get('fresh_marker_regex')) != (AGENT_TEXT.pattern, FRESH_MARK.pattern):
        raise SystemExit(f"{path}: the registered agent_text_regex / fresh_marker_regex differ from this code's patterns; amend RULES first")
    amendments = rules.get('amendments') or []
    if not isinstance(amendments, list):
        raise SystemExit(f'{path}: amendments is not a list')
    last = {}
    for i, amendment in enumerate(amendments):
        missing = [k for k in ('at', 'field', 'from', 'to', 'reason') if not isinstance(amendment, dict) or k not in amendment
                   or (k == 'reason' and not str(amendment.get(k) or '').strip())]
        if missing:
            raise SystemExit(f'{path}: amendment {i} lacks {missing} (every amendment carries at, field, from, to and a reason)')
        if not str(amendment['at']) > str(rules.get('written_before') or ''):
            raise SystemExit(f"{path}: amendment {i} at {amendment['at']} is not after written_before {rules.get('written_before')}")
        last[amendment['field']] = (i, amendment['to'])
    for field, (i, value) in last.items():
        held = _at_path(rules, field)
        if held is _MISSING:
            raise SystemExit(f'{path}: amendment {i} names {field}, which is not in the file')
        if held != value:
            raise SystemExit(f'{path}: amendment {i} sets {field} to {value!r} but the file holds {held!r}')
    return rules, sha256_bytes(data), data


def discover(roots):
    """(run dirs, skipped entries, sweep checkpoints without rows): a run dir holds reference-eval-v2/predictions.jsonl."""
    runs, skipped, sweep, seen = [], [], [], set()
    for root in (Path(r) for r in roots):
        for entry in sorted(root.iterdir()):
            if entry.is_dir() and (entry / 'reference-eval-v2' / 'predictions.jsonl').is_file():
                runs.append(entry)
            else:
                skipped.append(f'{root.name}/{entry.name}')
        folder = root.parent / 'sweep'
        if folder.is_dir() and folder.resolve() not in seen:
            seen.add(folder.resolve())
            sweep += [f'sweep/{e.name}' for e in sorted(folder.iterdir())
                      if e.is_dir() and not (e / 'reference-eval-v2' / 'predictions.jsonl').is_file()]
    return runs, skipped, sweep


def _run_shas(run):
    return {p.relative_to(run).as_posix(): sha256_bytes(p.read_bytes()) for p in sorted(run.rglob('*'))
            if p.is_file() and p.name in ('summary.json', 'predictions.jsonl', 'context.json')}


def checkpoint_inventory(contexts):
    """{checkpoint: [run:arm, ...]} from the arm specs of each run (base has no trainable file)."""
    inventory = defaultdict(list)
    for run, context in contexts.items():
        for arm, spec in zip(context['arms'], context.get('arm_specs') or context['arms'], strict=True):
            inventory[spec.partition('=')[2] or spec].append(f'{run}:{arm}')
    return dict(inventory)


def rejudge(roots, rules_path, out, scenes_root=SCENES, repo=REPO, draws=BOOT_DRAWS, seed=BOOT_SEED, recapture_dir=RECAPTURE_DIR):
    """Re-read every run dir's rows (blocks v2, v1, v2val; views t045 and matched for v2 / v1) against one read of the review
    files; write out/human-precision/<run>/<block>-<view>.json and append one ledger row per run x block x view."""
    rules, rules_sha, rules_bytes = load_rules(rules_path)
    amendments = [{'at': a['at'], 'field': a['field']} for a in rules.get('amendments') or ()]
    runs, skipped, sweep = discover(roots)
    if not runs:
        raise SystemExit('no run dir with reference-eval-v2/predictions.jsonl under the roots')
    if len({r.name for r in runs}) != len(runs):
        raise SystemExit('two run dirs share a name: block files would collide')
    labels = {r: f'{r.parent.name}/{r.name}' for r in runs}
    before = {labels[r]: _run_shas(r) for r in runs}
    contexts = {(labels[r], b): json.load(open(r / f'reference-eval-{b}' / 'context.json')) for r in runs for b in BLOCKS}
    inventory = checkpoint_inventory({labels[r]: contexts[(labels[r], 'v2')] for r in runs})
    judge_now = sha256_file(Path(ev.__file__))
    recorded = sorted({(c.get('meta') or {}).get('judge_version_sha256') for c in contexts.values()}, key=str)
    unedited = recorded == [judge_now]
    judge_note = ('reference_eval.py unedited' if unedited else
                  f'reference_eval.py sha256 {judge_now} differs from the recorded judge version(s) {recorded}')
    rescope = RESCOPE.format(judge=judge_note, runs=len(runs), checkpoints=len(inventory), sweep=len(sweep))
    code_bytes, batch = Path(__file__).read_bytes(), now_iso()
    code_sha = sha256_bytes(code_bytes)
    ledger_path = Path(out) / 'human-precision.jsonl'
    if ledger_path.is_file() and batch in {json.loads(line).get('batch_id') for line in open(ledger_path) if line.strip()}:
        raise SystemExit(f'batch id {batch} is already in {ledger_path}: rerun in a later second (nothing written)')
    batch_dir = Path(out) / 'batches' / batch_dir_name(batch)
    if batch_dir.exists():
        raise SystemExit(f'{batch_dir} exists: a batch folder is written once (nothing written)')
    lineage = load_lineage(recapture_dir)
    decisions = load_decisions(scenes_root, lineage=lineage)
    facts_listing = lineage_listing(decisions)
    splits, payloads = {}, []
    for run in runs:
        label = labels[run]
        for block, views in BLOCKS.items():
            context, predictions, _timings, _source = pf.load_folder(run / f'reference-eval-{block}' / 'predictions.jsonl')
            path = split_path_for(context, repo)
            if path not in splits:
                splits[path] = path.read_bytes()
            joined = join(context, splits[path], decisions, lineage=lineage)
            pop = population(joined)
            refs = agent_traced_references(joined)
            names = [f'{s}:{f}' for s, f in pairing(context)]
            for view in views:
                if view == 't045':
                    cuts, source = {a: T045 for a in context['arms']}, 'every arm at 0.45'
                else:
                    cuts, source = matched_cuts(run, block, arms=context['arms'])
                result = precision_block(context, joined['rows'], predictions, cuts, view, draws=draws, seed=seed)
                meta = context.get('meta') or {}
                common = {'version': VERSION, 'batch_id': batch, 'computed_at': batch, 'run': label, 'block': block, 'view': view,
                          'rules_version': rules['version'], 'rules_sha256': rules_sha, 'code_sha256': code_sha,
                          'split_file_sha256': joined['split_file_sha256'], 'judge_version_sha256': meta.get('judge_version_sha256'),
                          'judge_file_sha256_now': judge_now, 'judge_unedited': unedited, 'cuts': result['cuts'], 'cuts_source': source,
                          'pairs': names, 'review_snapshot_sha256': decisions['review_snapshot_sha256'],
                          'reviews_read': decisions['reviews_read'], 'rescope_note': rescope, 'not_rejudgeable_from_rows': sweep,
                          'unique_checkpoints': len(inventory), 'arm_specs': context.get('arm_specs'), 'rules_amendments': amendments,
                          'batch_dir': _rel(batch_dir, repo), 'wording': WORDING}
                ui = ui_only(result['facts'])
                block_file = Path(out) / 'human-precision' / run.name / f'{block}-{view}.json'
                payload = {**common, 'dataset': context['dataset'], 'split': context['split'],
                           'rows_collected_at_threshold': meta.get('rows_collected_at_threshold'), 'predictions_rows': len(predictions),
                           'predictions_sha256': before[label][f'reference-eval-{block}/predictions.jsonl'],
                           'context_sha256': before[label][f'reference-eval-{block}/context.json'],
                           'latest_snapshot_sha256': decisions['latest_snapshot_sha256'], 'arms': result['arms'], 'population': pop,
                           'agent_traced_references': refs, 'n_rule': result['n_rule'], 'read': result['read'],
                           'object_counts': result['object_counts'], 'shares': result['shares'], 'pair_blocks': result['pairs'],
                           'keeps_rule': result['keeps_rule'], 'roof_part': result['roof_part'], 'grid': result['grid'],
                           'facts': result['facts'], 'prediction_level': result['prediction_level'], 'ui_only': ui,
                           'unjoined': [{k: r[k] for k in ('scene_id', 'object_id', 'status', 'provenance_kind', 'review_area_m2',
                                                           'unjoined_reasons')}
                                        for r in joined['rows'] if r['decided'] and r['path'] is None and _small_either(r)],
                           'joined_not_counted': joined_not_counted(joined), 'human_drawn': human_drawn_rows(joined),
                           'lineage_facts': facts_listing, 'facts_columns': list(FACTS_COLUMNS), 'skipped_entries': skipped}
                ledger = {**common, 'block_file': _rel(block_file, repo), 'decisions_total': pop['decisions_total'],
                          'primary_decided': pop['primary_decided'], 'primary_by_class': pop['by_class'],
                          'primary_by_provenance': pop['by_provenance'], 'roof_part': pop['roof_part'],
                          'agent_authored_excluded': pop['agent_authored_excluded'], 'human_drawn_lt_25': pop['human_drawn_lt_25'],
                          'scenes': pop['scenes'], 'unjoined_by_reason': pop['unjoined_by_reason'],
                          'joined_not_counted': pop['joined_not_counted'], 'joined_not_counted_by_reason': pop['joined_not_counted_by_reason'],
                          'human_drawn_by_class': pop['human_drawn_by_class'],
                          'agent_traced_references': {k: refs[k] for k in ('references', 'agent_traced', 'by_tier')},
                          'n_rule': result['n_rule'], 'read': result['read'], 'ui_only_read': ui['read'],
                          'ui_only_chat_removed': ui['chat_removed']}
                payloads.append((block_file, _plain(payload), _plain(ledger)))
            del predictions, joined
    after = {labels[r]: _run_shas(r) for r in runs}
    if after != before:
        changed = sorted(f'{lab}/{f}' for lab in before for f in set(before[lab]) | set(after[lab]) if before[lab].get(f) != after[lab].get(f))
        raise SystemExit(f'GATE FAILED: run-dir files changed while they were read: {changed}')
    out = Path(out)
    written = []
    (batch_dir / 'human-precision').mkdir(parents=True)
    (batch_dir / 'RULES.json').write_bytes(rules_bytes)             # the exact RULES bytes this batch read (rules_sha256)
    (batch_dir / 'r5_precision.py').write_bytes(code_bytes)         # the exact module bytes this batch ran (code_sha256)
    for block_file, payload, ledger in payloads:
        text = json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False) + '\n'
        for target in (batch_dir / 'human-precision' / block_file.parent.name / block_file.name, block_file):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        ledger['block_file_sha256'] = sha256_bytes(text.encode())
        written.append(ledger)
    with open(out / 'human-precision.jsonl', 'a') as handle:
        for ledger in written:
            handle.write(json.dumps(ledger, sort_keys=True, ensure_ascii=False) + '\n')
    return {'runs': [labels[r] for r in runs], 'skipped_entries': skipped, 'not_rejudgeable_from_rows': sweep,
            'block_files': [_rel(p, repo) for p, _, _ in payloads], 'ledger_rows': len(written), 'batch_id': batch,
            'unique_checkpoints': len(inventory), 'rescope_note': rescope, 'review_snapshot_sha256': decisions['review_snapshot_sha256'],
            'batch_dir': _rel(batch_dir, repo), 'rules_sha256': rules_sha, 'code_sha256': code_sha, 'rules_amendments': amendments}


# --- report ------------------------------------------------------------------------------------------------------------

def _cell(fact):
    if fact['max_score'] is None:
        return f"no match (best IoU {fact['best_iou']:.2f})"
    return f"{fact['max_score']:.4f} @ {fact['iou_at_max_score']:.2f}"


def _flag(value):
    return '-' if value is None else ('yes' if value else 'no')


def _fact_cells(fact):
    """The seven FACTS_COLUMNS and the two LINEAGE_FACTS of one block-file facts row as per-object table cells (recorded, never
    filters). A flag reads yes / no / - (not recorded); decision_on_body_not_target names the covered slices and the max cover of
    a re-projected slice's own area; lineage_agreement reads agrees / differs / - (no decided comparison; the detail stays in the
    block file). Every value is the facts row's own; nothing is recomputed here."""
    area, revision = fact.get('review_area_m2'), fact.get('review_revision')
    body, covers = fact.get('decision_on_body_not_target'), fact.get('body_covers') or []
    if body is None:
        body_cell = '-'
    elif body and covers:
        body_cell = f"yes ({len(covers)} slice{'s' if len(covers) != 1 else ''}, max cover {max(c['cover'] for c in covers):.2f})"
    else:
        body_cell = 'yes' if body else 'no'
    agreement = fact.get('lineage_agreement')
    return ['-' if area is None else f'{float(area):.1f}', _flag(fact.get('model_signature_match')), _flag(fact.get('image_sha_match')),
            _flag(fact.get('geometry_reviewed')), '-' if revision is None else str(revision), fact.get('review_updated_at') or '-',
            fact.get('review_sha256') or '-', body_cell, '-' if agreement is None else ('agrees' if agreement else 'differs')]


def report(ledger, out, repo=REPO):
    """HUMAN-PRECISION.md from the latest batch of the ledger and its block files."""
    every = [json.loads(line) for line in open(ledger) if line.strip()]
    if not every:
        raise SystemExit(f'{ledger}: no ledger rows')
    batch = max(r['batch_id'] for r in every)
    rows = [r for r in every if r['batch_id'] == batch]
    batches_root = Path(ledger).parent / 'batches'
    blocks = {(r['run'], r['block'], r['view']): json.load(open(_abs(r['block_file'], repo))) for r in rows}
    runs = list(dict.fromkeys(r['run'] for r in rows))
    first = rows[0]
    inventory = checkpoint_inventory({run: {'arms': blocks[(run, 'v2', 't045')]['arms'],
                                            'arm_specs': blocks[(run, 'v2', 't045')]['arm_specs']} for run in runs})
    lines = [f'# Round-5 human precision block from rows ({VERSION})', '',
             "Agreement between model estimates (SAM3 checkpoints re-read from their prediction rows) and the reviewer's own "
             'small-roof decisions: every figure is a count of human decisions or an agreement share, never a calibrated rate. A '
             "review the assistant wrote is an observation, never a human decision. Nothing is promoted on any outcome.", '',
             f"- batch `{batch}`: {len(rows)} ledger rows of `{_rel(ledger, repo)}` over {len(runs)} run dirs",
             f"- RULES {first['rules_version']} sha256 `{first['rules_sha256']}`; code sha256 `{first['code_sha256']}`",
             f"- judge reference_eval.py sha256 now `{first['judge_file_sha256_now']}` (recorded in the rows: "
             f"`{first['judge_version_sha256']}`; {'unedited' if first['judge_unedited'] else 'CHANGED'})",
             f"- review snapshot sha256 `{first['review_snapshot_sha256']}` ({first['reviews_read']} review files read once for "
             'every block file)', '',
             '## Re-scope (`rescope_note` of every ledger row)', '', first['rescope_note'], '',
             f'## Checkpoint inventory ({len(inventory)} unique checkpoints re-read from rows)', '',
             '| checkpoint | used as (run:arm) |', '| --- | --- |']
    lines += [f"| {name} | {', '.join(uses)} |" for name, uses in sorted(inventory.items(), key=lambda kv: (kv[0] != 'base', kv[0]))]
    lines += ['', f"Sweep: {len(first['not_rejudgeable_from_rows'])} checkpoints are `not_rejudgeable_from_rows` (no predictions.jsonl / "
                  "context.json; inference needed, not this round): " + (', '.join(first['not_rejudgeable_from_rows']) or 'none') + '.', '',
              '## Denominators (per block; the review snapshot and the population are the same in every run dir of a block)', '',
              '| block | confirmed/rejected building decisions after exclusions (all scenes) | excluded before the count (objects, any '
              'status) | primary decided | roof | not-roof | roof part | scenes | app | chat | agent-authored excluded | human-drawn < 25 m² '
              f"| joined, not counted: {' / '.join(NOT_COUNTED)} | unjoined: {' / '.join(REASONS)} |", '| --- | ' + ' | '.join(['---'] * 13) + ' |']
    for block in BLOCKS:
        row = next((r for r in rows if r['block'] == block and r['view'] == 't045'), None)
        if row is None:
            continue
        u = row['unjoined_by_reason']
        strata = (blocks[(row['run'], block, 't045')].get('population') or {}).get('strata') or {}
        listed = row.get('joined_not_counted_by_reason')
        listed_text = ' / '.join(str(listed.get(k, 0)) for k in NOT_COUNTED) if listed is not None else 'not recorded'
        lines.append(f"| {block} | {row['decisions_total']} | {'; '.join(f'{k} {v}' for k, v in strata.items()) or 'none'} | "
                     f"{row['primary_decided']} | {row['primary_by_class']['roof']} | "
                     f"{row['primary_by_class']['not_roof']} | {row['roof_part']} | {row['scenes']} | {row['primary_by_provenance']['ui']} | "
                     f"{row['primary_by_provenance']['chat']} | {row['agent_authored_excluded']} | {row['human_drawn_lt_25']} | "
                     f"{listed_text} | {' / '.join(str(u.get(k, 0)) for k in REASONS)} |")
    lines += ['', 'UI-only sensitivity (RULES provenance.included: the same N rule read without the chat rows, from each block '
                  "file's facts):", '']
    for block in BLOCKS:
        reads = {run: ui_only(blocks[(run, block, 't045')]['facts']) for run in runs if (run, block, 't045') in blocks}
        distinct = {(u['read'], u['chat_removed']) for u in reads.values()}
        if len(distinct) == 1:
            read, chat = distinct.pop()
            lines.append(f'- UI-only sensitivity, block {block}: {read}; chat rows removed: {chat}')
        else:
            lines += [f"- UI-only sensitivity, block {block}, {run}: {u['read']}; chat rows removed: {u['chat_removed']}"
                      for run, u in reads.items()]
    lines += ['', '## N rule', '',
              'A share is read only when the primary decided count is at least 30 AND each class at least 8 AND the decisions sit on at '
              'least 6 scenes (RULES human_precision.n_rule). Below the rule only the per-object table is printed: no share, no pair '
              'statistic, no arm flag.', '', '| run | block | view | cuts | read |', '| --- | --- | --- | --- | --- |']
    lines += [f"| {r['run']} | {r['block']} | {r['view']} | " + ', '.join(f'{a} {c:.2f}' for a, c in r['cuts'].items()) +
              f" | {r['read']} |" for r in rows]
    passing = [r for r in rows if r['n_rule']['passes']]
    if passing:
        lines += ['', '## Shares (rule met)', '']
        for r in passing:
            block = blocks[(r['run'], r['block'], r['view'])]
            lines.append(f"- {r['run']} {r['block']} {r['view']}: " + '; '.join(
                f"{arm} " + ', '.join(f"{name} {s['successes']}/{s['total']} (Wilson 95 {s['wilson95']})" for name, s in shares.items())
                for arm, shares in (block['shares'] or {}).items()))
            for p in block['pair_blocks']:
                lines.append(f"  - {p['pair']}: discordant {p['discordant']}, McNemar p {p['mcnemar_p']}, keeps_human_precision "
                             f"{p['keeps_human_precision']}")
            ui = ui_only(block['facts'])
            lines.append(f"  - UI-only: {ui['read']}" + ('' if not ui['shares'] else '; ' + '; '.join(
                f"{arm} " + ', '.join(f"{name} {s['successes']}/{s['total']} (Wilson 95 {s['wilson95']})" for name, s in shares.items())
                for arm, shares in ui['shares'].items())))
    v2 = next((r for r in rows if r['block'] == 'v2'), first)
    v2_block = blocks[(v2['run'], v2['block'], v2['view'])]
    atr = v2['agent_traced_references']
    t1 = atr['by_tier'].get('T1') or {'references': 0, 'agent_traced': 0}
    lines += ['', '## Agent-authored exclusions', '',
              f"{v2['agent_authored_excluded']} decided objects on the block-v2 judged tiles carry the assistant's notes "
              "(^Assistant (visually traced|image review)) and are excluded from every human count (agent-authored review: an "
              'observation, never a human decision); they return only as fresh decisions of the reviewer. In the frozen v2 split '
              f"{atr['agent_traced']} of {atr['references']} references are agent-traced ({t1['agent_traced']} of {t1['references']} "
              "T1): flagged, never in the human-drawn stratum, not re-exported tonight.", '']
    unjoined = v2_block.get('unjoined') or []
    if unjoined:
        lines += ['Decided small objects that do not join block v2 (kept with reasons, never counted):', '']
        lines += [f"- {u['scene_id']} {u['object_id']} ({u['status']}, {u['provenance_kind']}, review {u['review_area_m2']} m²): "
                  f"{', '.join(u['unjoined_reasons'])}" for u in unjoined]
    listed = v2_block.get('joined_not_counted')
    if listed:
        lines += ['', 'Decided small objects that join block v2 but are not counted (joined, not counted; a listing, never a count):', '']
        lines += [f"- {o['scene_id']} {o['object_id']} ({o['status']}, {o['provenance_kind']}, path {o['path']}, snapshot "
                  f"{o['snapshot_area_m2']} m²): {o['reason']}" for o in listed]
    lines += ['', '## Per-object table', '',
              'Per decided primary object and arm: the max score of a row overlapping it at IoU >= 0.5 among the rows collected at 0.2 '
              "(the arm drops the object at any cut at or above it) and that row's IoU; then detected (yes/no) at the 0.45 cut and at "
              'the matched cut of the run. Between path and the arms: the seven fact columns of RULES '
              'human_precision.facts_columns_not_filters (' + ', '.join(FACTS_COLUMNS) + ') and the two lineage facts of '
              'human_precision.population_primary (' + ', '.join(LINEAGE_FACTS) + '), each as recorded on the block file\'s facts row; '
              'recorded, never filters: no row is in or out of this table, and no figure moves, by any of them. A flag reads yes / no / - '
              '(not recorded); decision_on_body_not_target names the frozen slices this object covers at >= ' + f'{BODY_COVER}' + ' of the '
              "re-projected slice's own area and the max cover; lineage_agreement reads agrees / differs / - (no decided comparison; the "
              'compared statuses are in the block file under lineage_agreement_detail).']
    for block in BLOCKS:
        lines += ['', f'### Block {block}']
        for run in runs:
            t = blocks.get((run, block, 't045'))
            if t is None:
                continue
            m = blocks.get((run, block, 'matched'))
            lines += ['', f"{run} ({t['read']})", '']
            if not t['facts']:
                lines.append('No decided object in this block.')
                continue
            arms = t['arms']
            fact_columns = FACTS_COLUMNS + LINEAGE_FACTS
            lines += ['| scene | object | class | prov | snapshot m² | path | ' + ' | '.join(fact_columns + tuple(arms)) + ' |',
                      '| ' + ' | '.join(['---'] * (6 + len(fact_columns) + len(arms))) + ' |']
            matched = {(f['scene_id'], f['object_id'], f['path'], f['ann_id'], tuple(f['ref_keys'] or ())): f for f in (m or {}).get('facts') or ()}
            for fact in t['facts']:
                mf = matched.get((fact['scene_id'], fact['object_id'], fact['path'], fact['ann_id'], tuple(fact['ref_keys'] or ())))
                cells = []
                for arm in arms:
                    cell = _cell(fact['arms'][arm]) + f"; 0.45 {'yes' if fact['arms'][arm]['detected'] else 'no'}"
                    if mf:
                        cell += f"; {mf['arms'][arm]['cut']:.2f} {'yes' if mf['arms'][arm]['detected'] else 'no'}"
                    cells.append(cell)
                lines.append(f"| {fact['scene_id']} | {fact['object_id']} | {fact['class'].replace('_', '-')} | {fact['provenance_kind']} | "
                             f"{float(fact['snapshot_area_m2']):.1f} | {fact['path']} | " + ' | '.join(_fact_cells(fact) + cells) + ' |')
    lines += ['', '## Ledger batches', '',
              'Every batch of the append-only ledger, oldest first. A batch folder keeps that batch\'s block files, the RULES bytes it '
              'read, the module bytes it ran and its report, so an earlier batch stays verifiable after a later batch rewrote the latest '
              'copies.', '',
              '| batch | rows | code sha256 | code bytes kept | RULES sha256 | amendments recorded in its rows | batch folder | its report |',
              '| --- | --- | --- | --- | --- | --- | --- | --- |']
    for b in sorted({r['batch_id'] for r in every}):
        brows = [r for r in every if r['batch_id'] == b]
        folder = batches_root / batch_dir_name(b)
        recorded = brows[0].get('rules_amendments')
        amend = ('not recorded (rows written before amendments were recorded)' if recorded is None else
                 ', '.join(f"{a['field']} ({a['at']})" for a in recorded) or 'none')
        kept = folder / 'HUMAN-PRECISION.md'
        if b == batch:
            its = 'this report'
        elif kept.is_file():
            old = kept.read_text()
            lacks = [name for name, marker in (('UI-only line', 'UI-only sensitivity'), ('prov column', '| prov |'),
                                               ('joined-not-counted column', 'joined, not counted')) if marker not in old]
            its = 'kept' + (f" (it has no {', no '.join(lacks)})" if lacks else '')
        else:
            its = 'not kept'
        codes = ', '.join(f'`{c}`' for c in sorted({str(r['code_sha256']) for r in brows}))
        rules_shas = ', '.join(f'`{c}`' for c in sorted({str(r['rules_sha256']) for r in brows}))
        code_copy = folder / 'r5_precision.py'
        code_kept = ('yes (sha256 verified)' if code_copy.is_file() and {sha256_file(code_copy)} == {str(r['code_sha256']) for r in brows}
                     else 'present, sha256 differs' if code_copy.is_file() else 'not kept')
        lines.append(f"| `{b}` | {len(brows)} | {codes} | {code_kept} | {rules_shas} | {amend} | "
                     f"{_rel(folder, repo) if folder.is_dir() else 'none'} | {its} |")
    rules_copy = batches_root / batch_dir_name(batch) / 'RULES.json'
    if rules_copy.is_file():
        in_force = json.loads(rules_copy.read_bytes()).get('amendments') or []
        lines += ['', f'## RULES amendments in force for batch `{batch}` ({len(in_force)}, from the RULES bytes the batch read)', '']
        lines += [f"- {a['at']} `{a['field']}`: {_short(json.dumps(a['from'], ensure_ascii=False), 90)} -> "
                  f"{_short(json.dumps(a['to'], ensure_ascii=False), 90)}; reason: {a['reason']}" for a in in_force] or ['- none']
    lines += ['', '## Threshold source', '', 'Any human-judged cut may only be chosen on dataset-v2 val decisions (RULES '
              'human_precision.threshold_source: val only); none is chosen here and the test-tile decisions that make the count never '
              'choose a cut.', '', f"Wording: {first['wording']}."]
    text = '\n'.join(lines) + '\n'
    if _BANNED_WORD in text.lower():
        raise SystemExit('wording rule: the report names the banned word; refusing to write it')
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(text)
    kept = batches_root / batch_dir_name(batch) / 'HUMAN-PRECISION.md'
    if kept.parent.is_dir() and not kept.exists():
        kept.write_text(text)                          # a batch's own report is written once and never rewritten
    return text


# --- CLI ---------------------------------------------------------------------------------------------------------------

def _count_text(c):
    return '\n'.join([f"{c['computed_at']} ({c['computed_at_local']}) block {c['block']}: primary decided {c['primary_n']} "
                      f"(roof {c['primary_by_class']['roof']}, not-roof {c['primary_by_class']['not_roof']}; app {c['primary_by_provenance']['ui']}, "
                      f"chat {c['primary_by_provenance']['chat']}) on {c['scenes']} scenes -> {c['read']}",
                      f"agent-authored excluded {c['agent_authored_excluded']}; human-drawn < 25 m2 {c['human_drawn_lt_25']}; targets "
                      f"{c['targets_remaining']} of {c['targets_total']} remaining"] +
                     [f"  {o['scene_id']} {o['id']} {o['status']} {o['area_m2']} {o['source']} {o['path']}" for o in c['already_counted']])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)

    def live_args(p):
        p.add_argument('--block', choices=tuple(BLOCKS), default='v2')
        p.add_argument('--run', default=str(COUNT_RUN), help='run dir whose reference-eval-<block>/context.json names the judged tiles')
        p.add_argument('--scenes', default=str(SCENES))
        p.add_argument('--prior-set', default=str(PRIOR_SET), help='earlier review set (its un-judged items are listed as deferred)')
        p.add_argument('--rules', default=str(RULES_PATH), help='RULES.json whose sha256 is embedded (absent: null)')
        p.add_argument('--repo', default=str(REPO))
        p.add_argument('--recapture-dir', default=str(RECAPTURE_DIR),
                       help='DESIGN.json and windows-*.jsonl the lineage hop reads (absent: every carried copy is refused by name)')
    count_p = sub.add_parser('count', help='live count of usable small-roof decisions (reads only)')
    live_args(count_p)
    count_p.add_argument('--json', action='store_true')
    guide_p = sub.add_parser('guide', help='reviewer guide (markdown) and review set (JSON) from the live count')
    live_args(guide_p)
    guide_p.add_argument('--out', required=True)
    guide_p.add_argument('--set', required=True)
    rejudge_p = sub.add_parser('rejudge', help='re-judge run dirs from rows; writes only under --out')
    rejudge_p.add_argument('--root', action='append', required=True)
    rejudge_p.add_argument('--rules', required=True)
    rejudge_p.add_argument('--out', required=True)
    rejudge_p.add_argument('--scenes', default=str(SCENES))
    rejudge_p.add_argument('--repo', default=str(REPO))
    rejudge_p.add_argument('--recapture-dir', default=str(RECAPTURE_DIR))
    report_p = sub.add_parser('report', help='HUMAN-PRECISION.md from the ledger')
    report_p.add_argument('--ledger', required=True)
    report_p.add_argument('--out', required=True)
    report_p.add_argument('--repo', default=str(REPO))
    args = parser.parse_args(argv)
    if args.command in ('count', 'guide'):
        kwargs = {'block': args.block, 'run_dir': Path(args.run), 'scenes_root': Path(args.scenes),
                  'prior_set': Path(args.prior_set) if args.prior_set else None, 'rules_path': Path(args.rules), 'repo': Path(args.repo),
                  'recapture_dir': Path(args.recapture_dir)}
        if args.command == 'count':
            result = count(**kwargs)
            if args.json:
                json.dump(result, sys.stdout, indent=1, ensure_ascii=False)
                sys.stdout.write('\n')
            else:
                print(_count_text(result))
            return 0
        result = guide(out_md=args.out, out_set=args.set, **kwargs)
        print(json.dumps({k: result[k] for k in ('computed_at', 'primary_n', 'primary_by_class', 'scenes', 'read', 'targets_remaining',
                                                 'targets_total', 'agent_authored_excluded')}))
        return 0
    if args.command == 'rejudge':
        result = rejudge(args.root, args.rules, args.out, scenes_root=Path(args.scenes), repo=Path(args.repo),
                         recapture_dir=Path(args.recapture_dir))
        print(json.dumps(result, indent=1))
        return 0
    text = report(args.ledger, args.out, repo=Path(args.repo))
    batch = max(json.loads(line)['batch_id'] for line in open(args.ledger) if line.strip())
    kept = Path(args.ledger).parent / 'batches' / batch_dir_name(batch) / 'HUMAN-PRECISION.md'
    print(json.dumps({'report': args.out, 'batch': batch, 'batch_report': str(kept) if kept.is_file() else None,
                      'batch_report_same': kept.is_file() and kept.read_text() == text}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
