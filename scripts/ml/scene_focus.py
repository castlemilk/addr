"""Focus recapture for frame-cut targets (frame-cut-window-v1): one registered Google z20 window per SUBJECT.

A frame-cut target is an object whose outline reaches the frame edge (touched_edges: the ring's bounding box within
frame_tolerance_px of an edge, so one grazing vertex is enough; the reviewer's no-cut-off direction of 2026-10-01 widened
scene_expand.touches_frame's two-vertex test, WINDOW.json amendment of that date): a slice of a roof
whose body lies beyond the capture. The reviewer's direction (2026-10-01): "proper centering and accurate windowing for
the given house and object". So the fix is a NEW capture per subject (house/lot), centred on the lot the object
belongs to, or on the object plus its body when no lot resolves, sized to the subject plus margin, at the best
available zoom; never the fixed 256 px pad (scene_context / scene_expand keep the old centre).

Subject resolution (read-only): the slice ring goes to WGS84 (scene_confidence.geographic); the BODY is any building
object of another REGISTERED scene (scene_lot_tags.registration 'region') within DESIGN.json max_shift_m of the slice
with at least 5 percent of its area outside the source frame (a window input only: never carried, never counted);
parcels come in scene_lot_tags.parcels_for_scene order, seeded by the SUBJECT polygon (slice union body), never by the
scene address; parcels are CLASSED first: an UNNUMBERED parcel whose plan starts CP or CM, or any parcel wider than the
120 m cap, is a plan-block (pfi 204419087 / CP153157, bounding span 311.28 m in the local metre frame per
data/scene-parcels/3720833b*.json, acted as a lot in the first 2026-10-01 plan) and is
never a lot, never a core member and never a body-side target; the LOT is the remaining approved parcel holding the
largest share of the subject when that share is at least .2 (scene_addresses' threshold), ONE second NUMBERED lot joins
above .15 (the two-lot core), common property (lot text CM/CP) joins only when overlapped at .2 and never brings its
plan-block; a subject whose only parcels are plan-blocks is subject-centred with lot unknown (flag plan_block); the
body-side test moves the subject to the parcel 1 m beyond the midpoint of each touched edge when that parcel is not the
chosen lot (the overlapped one becomes slice_parcel); the ADDRESS is the single address point inside a numbered lot
(scene_addresses.nearby), never a tie broken by distance.

Window: lot known -> centre of the lot-union bounding box, span clamp(max(lot_span*1.4+10, subject_span*2+20), 30,
120); lot too large -> the object's own lot, then the lot part within 120 m, flagged lot_exceeds_window; body but no
lot -> centre of the subject bounding box, span clamp(subject_span*2+20, 30, 120); neither -> the slice's bounding-box
centre, span clamp(slice_span*1.4+30, 40, the 512 px native z20 span). frame_px 512 when the span fits the 512 px
native z20 span (60.4 m at lat -37.79), else 1024: a 1024 px frame is at or finer than native z20 up to the 1024 px native
span (120.8 m), which the 120 m cap keeps it under; region_frame itself keeps z20 for a 1024 px frame up to about 241 m
(zoom = ceil(log2(frame_px/256/span)); 120.8 m is only the native-span point, moot under the cap). A GROUP is one
house/lot: targets whose windows overlap by half the smaller, share a body or share a lot form one window only when one
numbered-lot core contains the other (empty, equal, or the two-lot core of a subject straddling two lots), so a window
never spans more than one numbered lot core except through the two-lot rule; a merged group that no 120 m window holds
is re-grouped by body and lot. Two source scenes on one lot share a window, the one with most targets is carry_from and
the other's targets are uncarried_source.

Property fit (property-fit-v1; dated WINDOW.json amendment of 2026-10-08, field span_rule.property_fit; the reviewer's "yes to
defaults" on data/reviews/eyes/experiments/review-2026-10-08/DECISION-CARD.md, defaults 4a and 4b, after "i just want to avoid
cutting frames like it does, when its clearly not the entire permiter of the property etc"): every window planned under a WINDOW.json
carrying that record (plan refuses one without it) takes the frame above as its BASE and fits it to the property: census v2's R7
(experiments/frame_context_census.property_frame) over the subject, its lot, its unit development (the plan's parcels when one is
common property, at most 120 m) and the REVIEWER-MARKED outlines on them (review layer, status confirmed / needs_adjustment, not a
carried copy still needing adjustment, not a machine label or pending import; reviewer_marked). Outlines nobody has reviewed never
size the frame; each row names how many were left out and the frame they would have given. Unchanged when the property fits the base
frame with 5 m (+ placement_allowance_m); else re-centred on it, never smaller than the base, up to 120 m; over the cap the subject and
its lot stay 5 m inside (property_capped). The parcel completeness check (open_area over the query boxes of every frozen payload read,
ParcelPool) is part of the rule: an open lot / development makes one bounded Vicmap read unless offline, else the window is flagged
parcels_open and capture refuses it. A subject that already belongs to a captured window (window_frame.subjects) is exists_skip naming
that window: existing windows keep their frames and are never re-planned (reframing one is a later card). A group mixing such a
subject with a new target is split before any frame is fitted: the new targets are re-grouped and planned on their own (still
property-fitted; group.split_from_windowed names what was taken out) and every windowed subject is listed under skipped
(subject_already_windowed, existing_windows). capture refuses any row with a subject (all or some) a captured window holds, read
from the row's existing_windows and from data/scenes at capture time (subject_already_windowed). Grouping still uses the base
frames. PROPERTY_FIT_RULE is the registered text.

Identity: lot-<address_id> when the window is the single numbered lot of one resolved address AND no scene
roof_training_250-<address_id> or house-<address_id> exists (data/scenes/<id>/latest.json on disk, or injected into the
plan) and no source scene in the window has that address of its own: those families' own-lot recapture (scene_lot.lot_source,
`task recapture:capture ADDRESS=<id>`, carry-from roof_training_250-<id>) writes lot-<address_id>, so the name stays reserved
for it (flags address_reserved_for_scene with reserved_by, or address_reserved_for_source); else win-<sha256(json([provider,
bounds rounded 7, frame_px]))[:16]>, mirroring map-<hash> with frame_px added. The address, lots, rule and title stay recorded
inside the row whatever the id family. Recorded collisions of the dated plan window-plan-2026-10-01.jsonl (written before this
rule): lot-30381 (carry_from roof_training_250-30371, the neighbour's address) would have blocked `recapture:capture
ADDRESS=30381` for cohort scene roof_training_250-30381 and was HELD; lot-29772 (house-29772 exists) was captured and pushed
and stays as it is. An existing directory is never overwritten: the plan marks it exists_skip and the batch driver skips it.

plan refuses to run without the pre-registered WINDOW.json (write_window_rule / `register`: a NEW file under
data/reviews/eyes/recapture/, never rewritten, whose constants must be this module's) and records its sha256 beside the
DESIGN sha on the header and every row; capture refuses a row planned under another WINDOW.json, and a row whose DESIGN sha
is neither the current one nor behind a dated amendment with thresholds_changed false (scene_recapture.design_sha_accepted over
design_chain; the accepted hop is recorded as window_frame.plan_design_sha_match). plan is otherwise pure
apart from reading (and, unless offline, one bounded Vicmap read frozen as a NEW file); it writes one
JSONL (a header row, then one row per window) with the predicted overlap of every source frame in the window
(scene_recapture.frame_transform is pure, role frame_cut_target): the SOURCE-pixel fraction and the eroded overlap
REGION in target px (predicted_region_px). The gate is DESIGN.json subject_recapture.min_overlap_px (dated amendment
of 2026-10-01, reviewer authorisation "yes to defaults", item 5; design section 9 decision 1 option b): a window is
overlap_gate ok when the carry_from region side reaches the floor, else below_design and never captured; the fraction
rule (min_overlap_fraction) stays the same-address rule and is recorded beside the region (overlap_gate_detail, and the
header counts both rules). A design without the floor refuses the plan. capture_window fetches
tiles only (no detector), measures frame_transform against carry_from (scene_recapture.measured_transform with role
frame_cut_target under DESIGN.json), checks every slice and body sits inside with the 5 m edge margin, and saves only
under data/scenes/<window id>/; a frame that fails the design is refused with scene_recapture.FAILURE and nothing is
saved. The source scene, its review and every frozen file are never written; capture itself writes no carry and no
registry row.

carry (the TARGET carry of design section 4 A; runbook step 7) runs right after a verified capture and writes ONLY the new
window's review.json plus appended rows of the dated registry data/reviews/eyes/recapture/windows-<date>.jsonl: for every
frozen target the window's own source scene carries, the MODEL object of the snapshot the frame_transform names is
projected through the measured, verified transform (scene_recapture.project_object) and written at review revision 1
under DESIGN.json status_policy (needs_adjustment, geometry_reviewed false, model.height_verified false), with exactly one
bare lead sentence as notes (no assistant text, no reviewer text) and carried_from {scene_id, model_signature, object_id,
status (the source review's status, a hint), geometry_reviewed, iou_with_new 0, matched_id null, method
georeferenced-affine, role frame_cut_target}; provenance, fieldwork_import and replaces never travel. A frozen key the
counter already classifies roof / not_roof on the source (r5_precision.classify under the live RULES) is not carried: the
source decision wins (decided_on_source, no row). Targets of another source scene in the window are uncarried_source rows.
Refused by name, nothing written: carry_unverified, carry_design_mismatch (the transform's recorded DESIGN sha is neither the
current one nor behind a dated thresholds_changed-false amendment: design_chain; an accepted hop is named design_sha_match),
carry_snapshot_mismatch, carry_method,
window_rule_mismatch, a frozen key already registered to another window (ambiguous_carry), a copy outside the frame; an
existing review.json skips (review_exists). A rollback appends withdrawn rows; a dated registry file is never rewritten.

Group records travel with the copies (2026-10-01 defect: the review app's save validator, src/lib/scene-store.ts
validateSceneReview, requires every object group_id to name a group of the review's own groups array and every parent_id
chain to resolve inside the review; a window review with groups [] made the editor's save return 400 "Choose an existing
object group."). For every carried copy naming a group_id the source's record is carried into the window review's groups,
read from the source review.json groups first, then the carry snapshot's, then latest.json's, kept to the SceneGroup keys
the validator accepts (id matching ^[a-zA-Z0-9_-]{1,100}$, a non-empty label of at most 150 characters), one record per
group however many copies share it, restricted to the groups the copies name. A group no source file defines, and a
parent_id whose parent is not carried onto the window, are dropped from the copy and recorded under
carried_from.dropped_pointers [{field, value, reason}]; a carried parent's pointer follows its copy's id (a renamed copy).
`carry --repair-groups` (repair_groups) patches an EXISTING window review.json under the same rules ONLY while it is still
revision 1 and updated_by is this carry: it adds the missing group records (and drops unresolvable pointers), updates
updated_at, leaves revision 1 and every other byte, holds the app's review.lock while writing, and appends one registry row
{role carry_repair, window_scene_id, groups_added, at, previous_review_sha256, new_review_sha256}; a saved review (revision
above 1), a foreign updated_by, a locked scene or a missing review is refused by name and nothing is written.

takeover (reviewer direction 2026-10-02, verbatim: "ok so from these images, its all still happening, the organge boundig box cuts
off so the object boundary looks bad"; the orange line in the QA renders is the projected OLD tile edge, the red outline is the
frozen slice carried as-is, the whole roof is a separate new detection of the window's own model; design section 5 anticipated the
replaces hop). On a window scene a carried copy C (carried_from, status needs_adjustment) is taken over by a building detection D
of the window's current model (latest.json, the model_signature the review names) when D is the single building detection whose
polygon covers at least TAKEOVER cover_min (0.8) of C's area (shapely intersection area / C area), D's ring lies wholly inside the
frame (touching_frame: no vertex within frame_tolerance_px of an edge, a vertex beyond the frame counts) and no other building
detection covers C at other_cover_max (0.5) or more. Then C.status = superseded, C.replacement_ids = [D.id], C keeps carried_from
and every other field; D.replaces = the sorted copy ids it took over (one D may take over several slices of one roof) and
D.derived_from = the same list, because the review app's save validator (src/lib/scene-store.ts validateSceneReview ->
src/lib/scene-instances.ts lineageIssue) accepts status superseded only with replacement_ids and replacement_ids only when every
named object's derived_from points back (without the reciprocal the window review could not be saved in the app: the 2026-10-01
groups defect class); D stays status candidate and carries no agent text. A copy covered below cover_min (below_cover), covered by
two detections at other_cover_max or more (shared_cover), whose best detection reaches the frame (detection_touches_frame), is not
a candidate of the review (detection_not_candidate / detection_not_in_review) or already carries lineage (detection_has_lineage),
or that is already superseded, is left as it is with its reason: the reviewer decides the slice on the image. takeover_window
plans from the files (dry run: nothing written) and, with write, edits ONLY data/scenes/<window>/review.json while it is still
revision 1 by the carry or the carry-forward and names latest.json's model_signature, under the app's review.lock (mkdir), temp +
replace, revision 1 kept, updated_by 'scripts.ml.scene_focus takeover', every other byte identical (the file must round-trip
through the carry-forward serialisation: indent 2, no ASCII escaping, no trailing newline), plus one registry row per window
{role takeover, window_scene_id, model_signature, pairs [copy -> detection, coverage, frozen key], left_alone with reasons, rule,
previous_review_sha256, new_review_sha256, at}; every refusal is named (TakeoverRefused.reason) and writes nothing. The counting
side (a reviewer decision on D counts for the frozen key of EVERY copy in D.replaces through carried_from; the source decision
still wins, duplicate_decision_on_recapture; a copy in two detections' replaces counts nowhere, ambiguous_takeover; a taken-over
copy is excluded superseded_by_takeover and D is the asked target) is pre-registered text, written by write_takeover_amendment as
the dated WINDOW.json amendment (field takeover_rule, thresholds_changed false, constants_changed false; the RULES.json mirror is
the applying stream's), never this module's code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image
from shapely.geometry import Point, Polygon, box, shape
from shapely.ops import transform, unary_union
from shapely.strtree import STRtree
from shapely.validation import make_valid

from . import scene_lot
from .imagery import PROVIDER_MAX_ZOOM
from .review_authority import carried_undecided, review_authority_blocker
from .scene_addresses import image_projection, nearby
from .scene_batch import signature
from .scene_lot_tags import (
    fetch_frozen,
    inverse_projection,
    is_numbered,
    lot_label,
    lot_text,
    pad_bounds,
    parcels_for_scene,
    registration,
)
from .scene_object_quality import _live
from .scene_recapture import (
    DESIGN_PATH,
    EDGE_PX,
    METHOD,
    SUBJECT_ROLE,
    _read_json,
    carry_source,
    design_sha_accepted,
    frame_transform,
    load_design,
    measured_transform,
    pixel_spacing,
    project_object,
    subject_floor,
    verdict,
)
from .scene_region import capture_region, region_frame
from .scene_self_review import GEOMETRY_ERRORS, validate_id
from .tiles import WORLD_SPAN_M

VERSION = 'frame-cut-window-v1'
PLAN_VERSION = 'focus-plan-v1'
PROVIDER = 'google'
ZOOM = 20
FRAME_PX = (512, 1024)
SCRATCH_PREFIX = 'qa_'
PARCELS_DIR = scene_lot.PARCELS_DIR
WINDOW_PATH = Path('data/reviews/eyes/recapture/WINDOW.json')
REFERENCE_LAT = -37.79           # the design's reference latitude for the spans derived into WINDOW.json
COMMON_PREFIXES = ('CM', 'CP')   # common property lot text (scene_addresses.py:94) and plan-block plan numbers
EXCLUDED_CLASSES = ('plan_block', 'oversize')
SPAN_TOLERANCE_M = .01           # a parcel is oversize when its bounding span exceeds the cap by more than this
# Pre-registered window values (design section 2); the body distance is DESIGN.json max_shift_m, read at run time.
WINDOW = {
    'lot_grow': scene_lot.DESIGN['grow'], 'lot_margin_m': scene_lot.DESIGN['margin_m'],
    'subject_factor': 2., 'subject_margin_m': 20., 'lot_min_span_m': 30., 'max_span_m': 120.,
    'object_factor': 1.4, 'object_margin_m': 30., 'object_min_span_m': 40.,
    'share_min': .2, 'second_lot_share': .15, 'body_outside_fraction': .05, 'body_side_m': 1.,
    'edge_margin_m': 5., 'group_overlap': .5, 'frame_tolerance_px': 1., 'fetch_pad_deg': scene_lot.DESIGN['fetch_pad_deg'],
    'index_pad_deg': .002,
}
DISPLACEMENT = {'method': 'imagery-paste-rounding', 'native_px': [.4444, .4444], 'native_zoom': scene_lot.COHORT_NATIVE_ZOOM,
                'applied': False, 'source_doc': 'data/reviews/eyes/recapture/offset-diagnosis-2026-10-01.md',
                'note': 'recorded, never composed into pixel_affine (verify recomputes the affine to 1e-6); the measured residual absorbs it'}
EDGES = ('left', 'top', 'right', 'bottom')
# Takeover of a carried slice by the whole-roof detection of the window's own model (reviewer direction 2026-10-02; pre-registered
# as the WINDOW.json dated amendment takeover_rule). Coverage is intersection area / copy area; the frame tolerance is the window's.
TAKEOVER = {'cover_min': .8, 'other_cover_max': .5, 'frame_tolerance_px': WINDOW['frame_tolerance_px']}
TAKEOVER_DIRECTION = ('REVIEWER DIRECTION (verbatim, relayed by the harness 2026-10-02): "ok so from these images, its all still '
                      'happening, the organge boundig box cuts off so the object boundary looks bad" (the orange line in the QA renders '
                      'is the projected OLD tile edge; the red outline is the frozen slice carried as-is; the whole roof is a separate '
                      'new detection of the window\'s own model)')
TAKEOVER_RULE = (
    "On a window scene (frame-cut-window-v1) a carried copy C (a review object with carried_from, status needs_adjustment, "
    "geometry_reviewed false) is taken over by a building detection D of the window's current model (latest.json objects with kind "
    "building, the model_signature the review names) when D is the single building detection whose polygon covers at least cover_min "
    "(0.8) of C's area (shapely: intersection area / C area over the ring with its holes, an invalid ring made valid), D's ring lies "
    "wholly inside the frame (no vertex within frame_tolerance_px (1.0) of an edge: tolerance < x < width - tolerance and tolerance "
    "< y < height - tolerance for every vertex; a vertex beyond the frame counts as touching) and no other building detection covers "
    "C at other_cover_max (0.5) or more. Then C.status = 'superseded', C.replacement_ids = [D.id], C keeps carried_from and every "
    "other field; D.replaces = the sorted list of the copy ids it took over (one D may take over several slices of one roof) and "
    "D.derived_from = the same list (the review app's save validator, src/lib/scene-store.ts validateSceneReview -> "
    "src/lib/scene-instances.ts lineageIssue, accepts status superseded only with a non-empty replacement_ids and replacement_ids "
    "only when every named object's derived_from points back; without the reciprocal the window review could not be saved in the "
    "app, the 2026-10-01 groups defect class); D stays status candidate and carries no agent text. A copy covered below cover_min "
    "(below_cover), covered by two detections at other_cover_max or more (shared_cover), whose best detection reaches the frame "
    "(detection_touches_frame), whose best detection is not a candidate of the review (detection_not_candidate, "
    "detection_not_in_review) or already carries lineage (replaces, derived_from, replacement_ids or carried_from: "
    "detection_has_lineage), or that is already superseded, is left as it is with its reason: the reviewer decides the slice on the "
    "image. The write (scripts.ml.scene_focus takeover --write; task focus:takeover CONFIRM=1) happens only while the review is "
    "revision 1 and updated_by is the carry ('scripts.ml.scene_focus carry') or the carry-forward ('scripts.ml.scene_focus "
    "carry-forward') and latest.json model_signature equals review.model_signature, under the app's review.lock (mkdir), temp + "
    "replace, keeping revision 1, setting updated_by 'scripts.ml.scene_focus takeover' and updated_at, every other byte identical "
    "(the file must round-trip through the carry-forward serialisation: indent 2, no ASCII escaping, no trailing newline), and "
    "appends one registry row per window (role takeover: window_scene_id, model_signature, pairs copy -> detection with coverage and "
    "the copy's frozen key, the copies left alone with reasons, the rule constants, previous_review_sha256, new_review_sha256, at). "
    "Never the two other-authored windows (a saved review is refused by revision and updated_by), never latest.json, models, images "
    "or a source scene. Nothing becomes confirmed or rejected: a takeover is a lineage record, not a reviewer decision.")
TAKEOVER_COUNT = (
    "COUNT: a reviewer decision on D counts for the frozen key of EVERY copy in D.replaces (one physical roof), through carried_from "
    "of each copy; the source decision still wins (duplicate_decision_on_recapture); a copy listed in two detections' replaces counts "
    "nowhere (ambiguous_takeover); a taken-over copy is not a target (excluded superseded_by_takeover) and D is the asked target "
    "instead (url on the window with object D); classification of D follows the ordinary rules (no marker needed: D carries no agent "
    "text); D's provenance_kind ui / chat as usual. Agreement never accuracy; numbers read at write time with their sources.")


def _now():
    return datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def clamp(value, low, high):
    return min(high, max(low, value))


def native_span_m(lat, frame_px, zoom=ZOOM):
    """Metres a frame_px-wide frame spans at native zoom (scene_region.region_frame's native_meters_per_pixel times the side)."""
    return frame_px*scene_lot.native_m_per_px(lat, zoom)


def frame_px_for(span_m, lat):
    """512 when the span fits the 512 px native z20 span at this latitude, else 1024 (the frame is never coarser than native)."""
    return 512 if span_m <= native_span_m(lat, 512)+1e-9 else 1024


def z20_max_span_m(lat, frame_px, zoom=ZOOM):
    """The span below which scene_region.region_frame keeps `zoom` for a frame_px frame: zoom = ceil(log2(frame_px/256/span))
    stays at `zoom` while span_m < 2*frame_px*native_m_per_px(zoom), so 120.8 m at 512 px and about 241.6 m at 1024 px
    (lat -37.79). Not the native-span point (frame_px*native_m_per_px), which is where the frame stops being finer than native."""
    return 2*native_span_m(lat, frame_px, zoom)


def common_text(lot):
    """Common property by lot text: CM (scene_lot_tags.is_common) or CP (scene_addresses.match_addresses, :94)."""
    return str(lot or '').upper().startswith(COMMON_PREFIXES)


def parcel_class(lot, plan, span_m):
    """plan_block (unnumbered, plan CP*/CM*), oversize (wider than the cap), common (lot text CM*/CP*), numbered or unnumbered.

    plan_block and oversize parcels are never a lot (design section 2 step 4: common property never brings the plan-block;
    the first plan let pfi 204419087 / CP153157, lot None, bounding span 311.28 m in the local metre frame (metre_frame
    + _span over data/scene-parcels/3720833b*.json), act as the lot of win-d73376483fb1949f).
    """
    text = str(lot or '')
    if not text and str(plan or '').upper().startswith(COMMON_PREFIXES):
        return 'plan_block'
    if span_m > WINDOW['max_span_m']+SPAN_TOLERANCE_M:
        return 'oversize'
    if common_text(text):
        return 'common'
    if is_numbered(text):
        return 'numbered'
    return 'unnumbered'


def window_id(provider, bounds, frame_px):
    """win-<hash16>: scene_region's map-<hash> identity with frame_px added, so 512 and 1024 captures of one bounds differ."""
    identity = json.dumps([provider, [round(float(v), 7) for v in bounds], int(frame_px)])
    return 'win-'+_sha(identity.encode())[:16]


# Scene families whose OWN-lot recapture writes lot-<address_id> (scene_lot.lot_source; `task recapture:capture ADDRESS=<id>`
# hard-wires --carry-from roof_training_250-<id>): a window of a neighbour's address never takes that name while such a
# scene exists (the dated plan of 2026-10-01 named lot-30381 for roof_training_250-30371's targets while roof_training_250-30381
# exists; held, re-planned as win-*).
RESERVED_PREFIXES = ('roof_training_250', 'house')
IDENTITY_RULE = ('lot-<address_id> for the single numbered lot of one resolved address only when no scene '
                 'roof_training_250-<address_id> or house-<address_id> exists (data/scenes/<id>/latest.json, or injected) and no '
                 'source scene in the window has that address (address_reserved_for_scene / address_reserved_for_source otherwise) '
                 'and not lot_exceeds_window; else win-<sha256(json([provider, bounds rounded 7, frame_px]))[:16]>')


def reserved_by(root, address_id, known=()):
    """The existing scenes that reserve lot-<address_id>: data/scenes/roof_training_250-<id> or house-<id> with a latest.json,
    or such an id among `known` (the plan's injected scenes). Sorted; [] when the name is free."""
    folder = Path(root)/'data/scenes'
    candidates = [f'{prefix}-{address_id}' for prefix in RESERVED_PREFIXES]
    return sorted(sid for sid in candidates if sid in known or (folder/sid/'latest.json').is_file())


# --- geometry helpers ---------------------------------------------------------------------------------------------

def _geo(obj, scene):
    """A scene object's outline in WGS84 (repaired when invalid), or None."""
    from .scene_confidence import geographic
    try:
        polygon = geographic({'ring': obj['ring'], 'holes': obj.get('holes') or []}, scene)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
    except GEOMETRY_ERRORS:
        return None
    return None if polygon.is_empty or polygon.area <= 0 else polygon


def frame_polygon(scene):
    """The whole frame in WGS84 from its four pixel corners (fitted frames may be turned, so a polygon, not a box)."""
    w, h = scene['image_width'], scene['image_height']
    return _geo({'ring': [[0, 0], [w, 0], [w, h], [0, h]]}, scene)


def metre_frame(lon, lat):
    """(project lon,lat -> metres, unproject metres -> lon,lat) about a point; scene_addresses.image_projection's metre step."""
    frame = scene_lot._local_frame(lon, lat)
    return image_projection(frame), inverse_projection(frame)


def touched_edges(ring, scene, tolerance=WINDOW['frame_tolerance_px']):
    """{edge: vertices within tolerance of it} for the frame edges the ring's bounding box reaches within tolerance px of, else {}.

    One vertex within tolerance of an edge makes the object frame-cut (the bounding-box rule): the reviewer wants no cut-off
    at all, so this is a superset of scene_expand.touches_frame's two-vertex test, which left a ring grazing the frame with a
    single vertex (map-806de95f666a01c7/815d84c355fa7b73, y = 511 on a 512 px frame) planned as not_frame_cut on 2026-10-01.
    Vertices beyond the frame by more than tolerance are ignored, as before.
    """
    w, h = scene['image_width'], scene['image_height']
    edges = {edge: [] for edge in EDGES}
    for x, y in ring:
        if not (-tolerance <= x <= w+tolerance and -tolerance <= y <= h+tolerance):
            continue
        if abs(x) <= tolerance:
            edges['left'].append((x, y))
        if abs(x-w) <= tolerance:
            edges['right'].append((x, y))
        if abs(y) <= tolerance:
            edges['top'].append((x, y))
        if abs(y-h) <= tolerance:
            edges['bottom'].append((x, y))
    return {edge: points for edge, points in edges.items() if points}


def beyond_points(edges, scene, metres=WINDOW['body_side_m']):
    """The point `metres` beyond the midpoint of each touched edge, in WGS84 (the body-side test's probe)."""
    w, h = scene['image_width'], scene['image_height']
    step = metres/pixel_spacing(scene)
    unproject = inverse_projection(scene)
    probes = {}
    for edge, points in edges.items():
        mid_x = sum(p[0] for p in points)/len(points)
        mid_y = sum(p[1] for p in points)/len(points)
        pixel = {'left': (-step, mid_y), 'right': (w+step, mid_y), 'top': (mid_x, -step), 'bottom': (mid_x, h+step)}[edge]
        probes[edge] = Point(unproject(*pixel))
    return probes


def _span(geometry):
    minx, miny, maxx, maxy = geometry.bounds
    return max(maxx-minx, maxy-miny)


def _centre(geometry):
    minx, miny, maxx, maxy = geometry.bounds
    return (minx+maxx)/2, (miny+maxy)/2


# --- registered index (bodies and covering frames) --------------------------------------------------------------

class RegisteredIndex:
    """Building outlines (WGS84) of registered scenes whose frame meets the targets' neighbourhood; the sources' own
    objects are excluded per subject. scenes=None scans data/scenes/*/latest.json once (qa_* directories skipped).

    The same pass keeps, for the property-fit step: every scene's live building / tree / vegetation outlines (any registration)
    whose frame meets the neighbourhood, from its review layer (data/scenes/<id>/review.json) when one exists, else latest.json
    (never reviewer-marked); the scenes' frozen property_roofs.parcels payloads (into `pool` when given); and the subject keys
    of every captured window (window_frame.subjects of a non-qa scene), so a planned subject that already has a window is
    exists_skip."""

    def __init__(self, root, scenes=None):
        self.root = Path(root)
        self.scenes = scenes
        self.frames = []
        self.objects = []
        self.windowed = {}
        self._tree = None

    def load(self, boxes, pool=None):
        area = unary_union(boxes) if boxes else None
        if area is None:
            return self
        if self.scenes is not None:
            candidates = list(self.scenes)
            self._windows_on_disk({s.get('id') for s in candidates if isinstance(s, dict)})
        else:
            candidates = self._scan()
        for scene in candidates:
            if not isinstance(scene, dict):
                continue
            self._note_window(scene)
            if pool is not None:
                pool.add((scene.get('property_roofs') or {}).get('parcels'), f"scene:{scene.get('id')}")
            try:
                frame = frame_polygon(scene)
            except GEOMETRY_ERRORS:
                frame = None
            if frame is None or not frame.intersects(area):
                continue
            self._note_layer(scene, area)
            if registration(scene) != 'region':
                continue
            buildings = []
            for obj in _live(scene):
                if obj.get('kind') != 'building' or obj.get('status') in ('rejected', 'superseded'):
                    continue
                geo = _geo(obj, scene)
                if geo is not None:
                    buildings.append((obj['id'], geo))
            self.frames.append({'scene_id': scene['id'], 'frame': frame, 'buildings': buildings})
        return self

    def _note_window(self, scene):
        frame = scene.get('window_frame')
        sid = str(scene.get('id') or '')
        if not isinstance(frame, dict) or frame.get('version') != VERSION or not sid or sid.startswith(SCRATCH_PREFIX):
            return
        for subject in frame.get('subjects') or ():
            key = (subject.get('scene_id'), subject.get('object_id'))
            self.windowed.setdefault(key, set()).add(sid)

    def _windows_on_disk(self, seen):
        folder = self.root/'data/scenes'
        if not folder.exists():
            return
        for path in sorted([*folder.glob('win-*/latest.json'), *folder.glob('lot-*/latest.json')]):
            if path.parent.name not in seen:
                scene = _read_json(path)
                if isinstance(scene, dict):
                    self._note_window(scene)

    def windows_of(self, subject):
        """Sorted ids of the captured windows holding this subject ({'scene_id', 'object_id'}); [] when none."""
        return sorted(self.windowed.get((subject.get('scene_id'), subject.get('object_id')), ()))

    def _note_layer(self, scene, area):
        try:
            review = _read_json(self.root/'data/scenes'/validate_id(scene['id'])/'review.json')
        except (KeyError, ValueError):
            review = None
        layer = 'review' if isinstance(review, dict) and isinstance(review.get('objects'), list) else 'latest'
        objects = review['objects'] if layer == 'review' else scene.get('objects') or []
        for obj in objects:
            if (not isinstance(obj, dict) or obj.get('kind') not in PROPERTY_FIT['kinds'] or obj.get('status') in ('rejected', 'superseded')
                    or len(obj.get('ring') or []) < 3):
                continue
            geo = _geo(obj, scene)
            if geo is None or not geo.intersects(area):
                continue
            self.objects.append({'scene_id': scene['id'], 'object_id': obj.get('id'), 'kind': obj['kind'], 'status': obj.get('status'),
                                 'layer': layer, 'registration': registration(scene),
                                 'reviewed': layer == 'review' and reviewer_marked(obj), 'geo': geo,
                                 'cut_by_own_frame': bool(touched_edges(obj['ring'], scene)) if 'image_width' in scene else False})

    def objects_near(self, area):
        if self._tree is None:
            self._tree = STRtree([o['geo'] for o in self.objects]) if self.objects else False
        if not self._tree:
            return []
        return [self.objects[int(i)] for i in sorted(self._tree.query(area))]

    def _scan(self):
        folder = self.root/'data/scenes'
        if not folder.exists():
            return
        for path in sorted(folder.glob('*/latest.json')):
            if path.parent.name.startswith(SCRATCH_PREFIX):
                continue
            scene = _read_json(path)
            if isinstance(scene, dict):
                yield scene

    def buildings_near(self, bounds, exclude_scene):
        probe = box(*bounds)
        for entry in self.frames:
            if entry['scene_id'] == exclude_scene or not entry['frame'].intersects(probe):
                continue
            for object_id, geo in entry['buildings']:
                if geo.intersects(probe):
                    yield entry['scene_id'], object_id, geo

    def frames_covering(self, geometry, exclude):
        return sorted(e['scene_id'] for e in self.frames if e['scene_id'] not in exclude and e['frame'].covers(geometry))


def captured_window_subjects(root):
    """{(scene_id, object_id): {window ids}} of every captured window on disk now: window_frame.subjects (this VERSION) of a
    non-qa data/scenes/win-* or lot-* latest.json, read as the plan's RegisteredIndex reads them."""
    index = RegisteredIndex(root)
    index._windows_on_disk(set())
    return index.windowed


WINDOWED_REASON = 'subject_already_windowed'


# --- sources, parcels, addresses -------------------------------------------------------------------------------

def source_snapshot(root, scene_id, by_id):
    """The snapshot the counter requires: the review's models/<sig>.json when present, else latest.json (no bytes read)."""
    if scene_id in by_id:
        return by_id[scene_id]
    folder = Path(root)/'data/scenes'/validate_id(scene_id)
    latest = _read_json(folder/'latest.json')
    if not isinstance(latest, dict):
        return None
    review = _read_json(folder/'review.json')
    if isinstance(review, dict) and review.get('model_signature'):
        snapshot = _read_json(folder/'models'/f"{review['model_signature']}.json")
        if isinstance(snapshot, dict):
            return snapshot
    return latest


def _features(payload):
    out = []
    for feature in (payload or {}).get('features', []):
        props = feature.get('properties') or {}
        try:
            geo = shape(feature['geometry'])
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        if props.get('status', 'approved') == 'approved' and geo.is_valid and not geo.is_empty:
            out.append(({'pfi': str(props.get('pfi')), 'lot': lot_text(props), 'plan': props.get('plan')}, geo))
    return out


def subject_parcels(root, scene, subject, injected, *, offline):
    """(payload, sha256, source, covers_subject): parcels_for_scene order, then one bounded Vicmap read frozen as a NEW file."""
    if injected is not None:
        return injected, signature(injected), 'injected', True
    payload, sha, source = parcels_for_scene(scene, root, offline=True)
    if payload is not None:
        bounds = payload.get('bounds') if isinstance(payload, dict) else None
        covers = bool(bounds and len(bounds) == 4 and box(*bounds).covers(box(*subject.bounds)))
        if not covers:
            covers = any(geo.intersects(subject) for _, geo in _features(payload))
        if covers or offline:
            return payload, sha, source, covers
    if offline:
        return None, None, 'unavailable', False
    key = signature(['subject', scene['id'], [round(v, 7) for v in subject.bounds]])
    path = Path(root)/PARCELS_DIR/f'{key}.json'
    saved = _read_json(path)
    if isinstance(saved, dict) and saved.get('key') == key and saved.get('sha256') == signature(saved.get('data')):
        return saved['data'], saved['sha256'], 'eyes-parcels', True
    data = fetch_frozen(pad_bounds(list(subject.bounds), WINDOW['fetch_pad_deg']), path, key)
    return data, signature(data), 'vicmap', True


def addresses_inside(root, polygon, injected):
    """Address points inside a lot polygon (read-only sqlite, or an injected list); [] when no index exists."""
    if injected is not None:
        rows = injected
    elif (Path(root)/'data/australia_geo.db').exists():
        try:
            rows = nearby(list(polygon.bounds), root=Path(root))
        except (ValueError, OSError):
            rows = []
    else:
        rows = []
    return [row for row in rows if polygon.covers(Point(row['lon'], row['lat']))]


# --- subject resolution ------------------------------------------------------------------------------------------

def resolve_subject(root, scene, obj, edges, index, *, parcels=None, addresses=None, offline=True, body_distance_m, pool=None):
    slice_geo = _geo(obj, scene)
    if slice_geo is None:
        return None
    lon, lat = slice_geo.centroid.x, slice_geo.centroid.y
    project, _ = metre_frame(lon, lat)
    slice_m = transform(project, slice_geo)
    frame_m = transform(project, frame_polygon(scene))
    pad = WINDOW['index_pad_deg']/4
    bodies = []
    for body_scene, body_id, geo in index.buildings_near(pad_bounds(list(slice_geo.bounds), pad), scene['id']):
        body_m = transform(project, geo)
        if body_m.area <= 0 or body_m.distance(slice_m) > body_distance_m:
            continue
        outside = 1.-body_m.intersection(frame_m).area/body_m.area
        if outside >= WINDOW['body_outside_fraction']:
            bodies.append({'scene_id': body_scene, 'object_id': body_id, 'geo': geo, 'area_m2': round(body_m.area, 2),
                           'outside_fraction': round(outside, 3), 'distance_m': round(body_m.distance(slice_m), 3)})
    bodies.sort(key=lambda b: (b['distance_m'], -b['area_m2'], b['scene_id'], b['object_id']))
    subject_geo = unary_union([slice_geo, *(b['geo'] for b in bodies)])
    subject_m = unary_union([slice_m, *(transform(project, b['geo']) for b in bodies)])
    payload, parcels_sha, parcels_source, covers = subject_parcels(root, scene, subject_geo, parcels, offline=offline)
    if pool is not None and payload is not None:
        pool.add(payload, f'subject:{parcels_source}')
    features, excluded = [], []
    for props, geo in _features(payload):
        parcel_m = transform(project, geo)
        span_m = round(_span(parcel_m), 2)
        entry = {**props, 'class': parcel_class(props['lot'], props['plan'], span_m), 'span_m': span_m, 'geo': geo}
        share = parcel_m.intersection(subject_m).area/subject_m.area if subject_m.area > 0 and parcel_m.intersects(subject_m) else 0.
        if entry['class'] in EXCLUDED_CLASSES:
            # a plan-block is recorded for the reviewer and used for nothing: not a lot, not a core member, not a body-side target
            if share > 0:
                excluded.append({'pfi': props['pfi'], 'lot': props['lot'], 'plan': props['plan'], 'share': round(float(share), 4),
                                 'class': entry['class'], 'span_m': span_m})
            continue
        features.append(entry)
        if share > 0:
            entry['share'] = round(float(share), 4)
    excluded.sort(key=lambda e: (-e['share'], e['pfi']))
    shares = sorted((entry for entry in features if 'share' in entry), key=lambda s: (-s['share'], s['pfi']))
    chosen = shares[0] if shares and shares[0]['share'] >= WINDOW['share_min'] else None
    if chosen is not None:
        reason = 'dominant_share'
    elif excluded and excluded[0]['share'] >= WINDOW['share_min']:
        reason = 'plan_block_only'
    elif features:
        reason = 'no_parcel_share'
    else:
        reason = 'no_parcels' if payload is None else 'no_parcel_overlap'
    slice_parcel = None
    probes = beyond_points(edges, scene)
    hits = Counter()
    for probe in probes.values():
        for entry in features:
            if entry['geo'].covers(probe):
                hits[entry['pfi']] += 1
    if hits:
        pfi, _ = max(hits.items(), key=lambda kv: (kv[1], next((s['share'] for s in shares if s['pfi'] == kv[0]), 0.), kv[0]))
        if chosen is None or pfi != chosen['pfi']:
            beyond = next(({**entry, 'share': entry.get('share', 0.)} for entry in features if entry['pfi'] == pfi), None)
            if beyond is not None:
                if chosen is not None:
                    slice_parcel = {'pfi': chosen['pfi'], 'lot': chosen['lot'], 'plan': chosen['plan']}
                chosen, reason = beyond, 'body_side'
    core = []
    if chosen is not None:
        core.append(chosen)
        others = [lot for lot in shares if lot['pfi'] != chosen['pfi'] and not (slice_parcel and lot['pfi'] == slice_parcel['pfi'])]
        # ONE second numbered lot above the tie band (the two-lot core); common property joins when overlapped at share_min
        second = next((lot for lot in others if lot['class'] == 'numbered' and lot['share'] > WINDOW['second_lot_share']), None)
        if second is not None:
            core.append(second)
        core.extend(lot for lot in others if lot['class'] == 'common' and lot['share'] >= WINDOW['share_min'])
    address = None
    if chosen is not None and chosen['class'] == 'numbered':
        points = addresses_inside(root, chosen['geo'], addresses)
        if len(points) == 1:
            address = {'address_id': points[0]['address_id'], 'full_address': points[0].get('full_address')}
    lot = None
    if chosen is not None:
        lot = {'pfi': chosen['pfi'], 'lot': chosen['lot'], 'plan': chosen['plan'], 'class': chosen['class'], 'share': chosen['share'],
               'address_id': address['address_id'] if address else None}
    return {'scene_id': scene['id'], 'object_id': obj['id'], 'status': obj.get('status'), 'kind': obj.get('kind'),
            'slice_geo': slice_geo, 'slice_area_m2': round(slice_m.area, 2), 'slice_bounds': [round(v, 7) for v in slice_geo.bounds],
            'touched_edges': [edge for edge in EDGES if edge in edges], 'bodies': bodies, 'subject_geo': subject_geo,
            'lot': lot, 'core': core, 'two_lot': sum(entry['class'] == 'numbered' for entry in core) == 2,
            'excluded_parcels': excluded, 'slice_parcel': slice_parcel, 'reason': reason, 'address': address,
            'parcels_sha256': parcels_sha, 'parcels_source': parcels_source, 'parcels_cover_subject': covers,
            'probes': {edge: [round(p.x, 7), round(p.y, 7)] for edge, p in probes.items()}, 'kept_apart': []}


# --- window rule --------------------------------------------------------------------------------------------------

def _window_geometry(members, lat_hint=None):
    """Centre (lon, lat), span_m, rule, flags and the lots used for a group of subjects (pure)."""
    slices = unary_union([m['slice_geo'] for m in members])
    lon, lat = slices.centroid.x, slices.centroid.y
    project, unproject = metre_frame(lon, lat)
    slices_m = transform(project, slices)
    bodies_m = [transform(project, b['geo']) for m in members for b in m['bodies']]
    subject_m = unary_union([slices_m, *bodies_m])
    flags = []
    lots = {}
    for m in members:
        for lot in m['core']:
            entry = lots.setdefault(lot['pfi'], {'pfi': lot['pfi'], 'lot': lot['lot'], 'plan': lot['plan'], 'class': lot['class'], 'share': 0.,
                                                 'geo': lot['geo'], 'address_id': None})
            entry['share'] = max(entry['share'], lot['share'])
            if m['lot'] and m['lot']['pfi'] == lot['pfi'] and m['lot']['address_id'] is not None:
                entry['address_id'] = m['lot']['address_id']
    lots = sorted(lots.values(), key=lambda lot: (-lot['share'], lot['pfi']))
    own = {m['lot']['pfi'] for m in members if m['lot']}
    if lots:
        rule = 'lot'
        lot_m = unary_union([transform(project, lot['geo']) for lot in lots])
        grown = _span(lot_m)*WINDOW['lot_grow']+WINDOW['lot_margin_m']
        if grown > WINDOW['max_span_m'] and len(lots) > 1:
            lots = [lot for lot in lots if lot['pfi'] in own] or lots[:1]
            lot_m = unary_union([transform(project, lot['geo']) for lot in lots])
            grown = _span(lot_m)*WINDOW['lot_grow']+WINDOW['lot_margin_m']
        if grown > WINDOW['max_span_m']:
            flags.append('lot_exceeds_window')
            sx, sy = _centre(subject_m)
            half = WINDOW['max_span_m']/2
            clipped = lot_m.intersection(box(sx-half, sy-half, sx+half, sy+half))
            cx, cy = _centre(unary_union([subject_m, clipped]))
            span_m = WINDOW['max_span_m']
        else:
            cx, cy = _centre(lot_m)
            span_m = clamp(max(grown, _span(subject_m)*WINDOW['subject_factor']+WINDOW['subject_margin_m']),
                           WINDOW['lot_min_span_m'], WINDOW['max_span_m'])
    elif bodies_m:
        rule = 'subject'
        cx, cy = _centre(subject_m)
        span_m = clamp(_span(subject_m)*WINDOW['subject_factor']+WINDOW['subject_margin_m'], WINDOW['lot_min_span_m'], WINDOW['max_span_m'])
    else:
        rule = 'subject_no_body'
        cx, cy = _centre(slices_m)
        span_m = clamp(_span(slices_m)*WINDOW['object_factor']+WINDOW['object_margin_m'], WINDOW['object_min_span_m'], native_span_m(lat, 512))
    if any(lot['class'] == 'common' for lot in lots):
        flags.append('common_property')
    if any(m['reason'] == 'plan_block_only' for m in members):
        flags.append('plan_block')
    centre = unproject(cx, cy)
    return {'centre': centre, 'span_m': span_m, 'rule': rule, 'flags': flags, 'lots': lots, 'lat': lat,
            'subject_geo': unary_union([m['subject_geo'] for m in members])}


def planned_frame(frame, frame_px, scene_id):
    """The frame dict scene_recapture.frame_transform needs for a window that is not captured yet."""
    return {'id': scene_id, 'centroid': frame['centroid'], 'region': frame['region'], 'image_width': frame_px, 'image_height': frame_px,
            'pixel_to_meters': frame['pixel_to_meters'], 'meters_to_pixel': frame['meters_to_pixel']}


def _frame_for(centre, span_m, frame_px):
    lon, lat = centre
    _, unproject = metre_frame(lon, lat)
    west, south = unproject(-span_m/2, -span_m/2)
    east, north = unproject(span_m/2, span_m/2)
    requested = [round(v, 7) for v in (west, south, east, north)]
    return requested, region_frame(requested, PROVIDER, frame_px=frame_px)


def edge_margins(frame_dict, geometries):
    """The smallest distance (metres) from any geometry to the frame edge, through the frame's own pixels."""
    project = image_projection(frame_dict)
    side, spacing = frame_dict['image_width'], pixel_spacing(frame_dict)
    margins = []
    for geo in geometries:
        minx, miny, maxx, maxy = transform(project, geo).bounds
        margins.append(min(minx, miny, side-maxx, side-maxy)*spacing)
    return None if not margins else round(float(min(margins)), 2)


def acceptance(frame_dict, geometries):
    margin = edge_margins(frame_dict, geometries)
    return {'inside_with_margin': margin is not None and margin >= WINDOW['edge_margin_m'], 'min_edge_margin_m': margin,
            'edge_margin_m': WINDOW['edge_margin_m']}


# --- property-fit frame (dated WINDOW.json amendment 2026-10-08, field span_rule.property_fit) ---------------------------
#
# Reviewer, 8 Oct 2026 ~09:15 AEDT: "i just want to avoid cutting frames like it does, when its clearly not the entire permiter of
# the property etc"; ~18:00 AEDT "yes to defaults" on data/reviews/eyes/experiments/review-2026-10-08/DECISION-CARD.md, whose
# defaults 4a (windows made from now on use the property-fit rule, up to 120 m) and 4b (outlines nobody has reviewed do not size
# the frame) this section implements. It is census v2's R7 (scripts/ml/experiments/frame_context_census.property_frame, open_area,
# fixed 2026-10-08) applied to a NEW window: the base frame is the span rule above, unchanged; existing windows are never re-planned.

PROPERTY_FIT_FIELD = 'span_rule.property_fit'
PROPERTY_FIT = {
    'version': 'property-fit-v1', 'edge_margin_m': WINDOW['edge_margin_m'], 'max_span_m': WINDOW['max_span_m'],
    'placement_allowance_m': .05, 'inside_tolerance_m': 1e-6, 'open_tolerance_m': 1., 'open_min_m2': .1,
    'common_lot_prefixes': list(COMMON_PREFIXES), 'common_property_plans': ['PS', 'RP', 'SP'],
    'kinds': ['building', 'tree', 'vegetation'], 'reviewer_statuses': ['confirmed', 'needs_adjustment'], 'count_unreviewed': False,
    'share_min': WINDOW['share_min'], 'second_lot_share': WINDOW['second_lot_share'], 'subject_cover': .5, 'subject_iou': .3, 'parcel_reads': 1,
    'parcel_read_pad_deg': WINDOW['fetch_pad_deg'], 'neighbourhood_m': 240.,
}
# Frozen Vicmap Parcel payloads read for the property ({'data': FeatureCollection with 'bounds'} or {key, sha256, data}); scenes'
# latest.json property_roofs.parcels and the subject payload are read too (census v2's set, plus --extra-parcels folders).
PARCEL_GLOBS = ('data/scene-parcels/*.json', 'data/reviews/eyes/units/parcels-*.json', 'data/reviews/eyes/parcels/*.json',
                'data/roof-review-loop/runs/*/parcels-*.json')
POOL_PAD_DEG = .006              # parcels kept in memory: within this of a target (the development can reach about 180 m)
PROPERTY_FIT_RULE = (
    "PROPERTY FIT (windows planned under a WINDOW.json carrying this record; a window already captured keeps its frame and is never "
    "re-planned). The base frame is the span rule of this file (lot / lot_exceeds_window / subject / subject_no_body, widened once), "
    "unchanged; the frame is then fitted to the PROPERTY, in local metres about the base frame centre (x east, y north): the subject "
    "(slices plus bodies), its lot (the window's lots; when the plan resolved none, census v2's derived lot: the approved parcel read, "
    "not a plan-block or oversize parcel, holding the largest share of the subject when that share is at least share_min, plus one "
    "numbered parcel above second_lot_share), its development (every approved parcel, not a plan-block or oversize parcel, of "
    "a lot's plan when that plan has a common property parcel, lot text CM or CP, among the parcels read; a development wider than "
    "max_span_m is not a development and its sibling lots stay neighbours) and the outlines on them that count. An outline counts when "
    "it is live (status not superseded or rejected), of a kind in kinds, read from a scene's review layer (review.json) and "
    "REVIEWER-MARKED: status confirmed or needs_adjustment, not a carried copy still needing adjustment "
    "(review_authority.carried_undecided) and not a machine label or a pending Fieldwork import (review_authority_blocker); and on the "
    "property: its largest parcel share, at least share_min, is a lot or a development parcel, or (a building) it covers a slice or "
    "body by subject_cover either way or IoU subject_iou. Outlines nobody has reviewed never size the frame (count_unreviewed false): "
    "the row names how many were left out and the frame they would have given. With no lot the property is the subject and the "
    "outlines that cover it. When the property lies inside the base frame with edge_margin_m + placement_allowance_m to every edge "
    "the frame is unchanged (status unchanged); else it is centred on the property bounding box with half = max(base half, longer "
    "side / 2 + edge_margin_m + placement_allowance_m), never smaller than the base frame, at most max_span_m / 2 (status "
    "recentred); over the cap it stays max_span_m wide and the centre moves only as far as needed to keep the subject and its lot "
    "inside with that margin (the subject alone when subject plus lot is itself wider than the cap), and what still crosses the edge "
    "is recorded (status capped, flag property_capped). placement_allowance_m covers the 1e-7 degree rounding of the requested "
    "bounds and the 0.01 m reading of the acceptance margin (census v2 pilot win-3dd37155248c04ef: development 4.996 m from the edge). "
    "frame_px = frame_px_for(span). PARCEL COMPLETENESS: a frozen Vicmap payload holds every parcel that meets its query box, whole, "
    "so the lot and development are complete only when the query boxes of the payloads read cover them grown by open_tolerance_m to "
    "within open_min_m2; a lot on a plan starting with one of common_property_plans and no development recognised needs its own grown "
    "outline covered too (its common property could be off disk). Not complete: unless offline the planner makes parcel_reads bounded "
    "Vicmap read(s) (the uncovered part's bounds padded by parcel_read_pad_deg, frozen as a NEW file under data/reviews/eyes/parcels, "
    "as the subject read) and fits again; still open, or offline, the window is flagged parcels_open with the uncovered m2, keeps the "
    "base frame (a lower bound) and is never captured. A window whose subjects already belong to a captured window (window_frame."
    "subjects of a non-qa scene) is exists_skip naming that window.")


def reviewer_marked(obj):
    """True when the reviewer confirmed the outline or marked it needs_adjustment (decision card 2026-10-08, default 4b): status
    confirmed / needs_adjustment, not a carried copy still needing adjustment (the carry's status, not the reviewer's) and not a
    machine label or a pending Fieldwork import. Only review-layer objects can be reviewer-marked (the caller checks the layer)."""
    return (obj.get('status') in PROPERTY_FIT['reviewer_statuses'] and not carried_undecided(obj)
            and review_authority_blocker(obj) is None)


def _half_extent(geometry):
    minx, miny, maxx, maxy = geometry.bounds
    return max(abs(minx), abs(miny), abs(maxx), abs(maxy))


def _clamp_centre(c, lo_edge, hi_edge, half, margin):
    """Shift a centre along one axis only as far as needed to keep [lo_edge, hi_edge] inside with margin (when it fits)."""
    lo, hi = hi_edge+margin-half, lo_edge-margin+half
    return min(max(c, lo), hi) if lo <= hi else (lo_edge+hi_edge)/2


def property_frame(prop, base_half, keep=None, *, margin, cap=PROPERTY_FIT['max_span_m']):
    """Census v2 R7 (frame_context_census.property_frame) for a property in metres about the base frame centre: unchanged when the
    property lies inside the base square with margin; else centred on the property bounding box with half = max(base half, longer
    side / 2 + margin), capped at cap / 2; over the cap shifted only as far as needed to keep `keep` inside with margin."""
    if prop.is_empty or _half_extent(prop)+margin <= base_half:
        return {'half': base_half, 'cx': 0., 'cy': 0., 'recentred': False, 'property_fits': True, 'capped': False,
                'property_need_half_m': None if prop.is_empty else _half_extent(prop)+margin}
    minx, miny, maxx, maxy = prop.bounds
    need = max(maxx-minx, maxy-miny)/2+margin
    half = max(base_half, min(cap/2, need))
    cx, cy = (minx+maxx)/2, (miny+maxy)/2
    capped = need > cap/2+1e-9
    if capped and keep is not None and not keep.is_empty:
        kminx, kminy, kmaxx, kmaxy = keep.bounds
        cx, cy = _clamp_centre(cx, kminx, kmaxx, half, margin), _clamp_centre(cy, kminy, kmaxy, half, margin)
    return {'half': half, 'cx': cx, 'cy': cy, 'recentred': True, 'property_fits': not capped, 'capped': capped, 'property_need_half_m': need}


def open_area(geometry, coverage, tol=PROPERTY_FIT['open_tolerance_m']):
    """m2 of the geometry grown by tol that no frozen payload's query box covers (frame_context_census.open_area)."""
    if geometry is None or geometry.is_empty:
        return 0.
    grown = geometry.buffer(tol)
    return float(grown.area if coverage is None or coverage.is_empty else grown.difference(coverage).area)


def _inside(geometry, half, cx, cy, margin):
    if geometry is None or geometry.is_empty:
        return None
    minx, miny, maxx, maxy = geometry.bounds
    tol = PROPERTY_FIT['inside_tolerance_m']
    return bool(minx-cx >= -half+margin-tol and maxx-cx <= half-margin+tol and miny-cy >= -half+margin-tol and maxy-cy <= half-margin+tol)


def property_fit(base_half, subject, lot, development, objects, *, count_unreviewed=None, allowance=None):
    """The property-fit frame (pure; metres about the base frame centre, x east, y north). objects: [{'id', 'reviewed', 'geo', ...}]
    already on the property; an unreviewed one sizes the frame only with count_unreviewed (default PROPERTY_FIT's, false). Returns the
    frame (half, cx, cy, recentred, capped, property_fits), status (no_lot / unchanged / recentred / capped), the counts, and the
    frame the left-out outlines would have given (if_unreviewed_counted, None when none was left out)."""
    count_unreviewed = PROPERTY_FIT['count_unreviewed'] if count_unreviewed is None else count_unreviewed
    allowance = PROPERTY_FIT['placement_allowance_m'] if allowance is None else allowance
    margin, cap = PROPERTY_FIT['edge_margin_m']+allowance, PROPERTY_FIT['max_span_m']
    land = [g for g in (subject, lot, development) if g is not None and not g.is_empty]
    counted = [o for o in objects if count_unreviewed or o['reviewed']]
    left_out = [o for o in objects if not (count_unreviewed or o['reviewed'])]
    keep_parts = [g for g in (subject, lot) if g is not None and not g.is_empty]
    keep, keep_name = (unary_union(keep_parts) if keep_parts else Polygon()), 'subject_and_lot'
    if not keep.is_empty and _span(keep)+2*margin > cap+1e-9 and subject is not None and not subject.is_empty:
        keep, keep_name = subject, 'subject'

    def frame_of(pool):
        parts = land+[o['geo'] for o in pool if not o['geo'].is_empty]
        prop = unary_union(parts) if parts else Polygon()
        return prop, property_frame(prop, base_half, keep, margin=margin, cap=cap)

    def status_of(frame):
        if lot is None or lot.is_empty:
            return 'no_lot'
        return 'unchanged' if not frame['recentred'] else 'capped' if frame['capped'] else 'recentred'

    prop, frame = frame_of(counted)
    alternative = None
    if left_out:
        _, other = frame_of(objects)
        alternative = {'half': other['half'], 'cx': other['cx'], 'cy': other['cy'], 'recentred': other['recentred'],
                       'capped': other['capped'], 'status': status_of(other),
                       'differs': bool(abs(other['half']-frame['half']) > 1e-6 or abs(other['cx']-frame['cx']) > 1e-6
                                       or abs(other['cy']-frame['cy']) > 1e-6)}
    return {**frame, 'status': status_of(frame), 'margin_m': margin, 'keep': keep_name,
            'property_bounds': None if prop.is_empty else [round(v, 3) for v in prop.bounds],
            'objects_counted': len(counted), 'objects_counted_by_kind': dict(sorted(Counter(o.get('kind') for o in counted).items())),
            'objects_left_out_unreviewed': len(left_out), 'left_out_sample': sorted(str(o['id']) for o in left_out)[:5],
            'objects_counted_cut_by_own_frame': sum(bool(o.get('cut_by_own_frame')) for o in counted),
            'objects_counted_fitted': sum(o.get('registration') == 'fitted' for o in counted),
            'subject_inside_margin': _inside(subject, frame['half'], frame['cx'], frame['cy'], PROPERTY_FIT['edge_margin_m']),
            'lot_inside_margin': _inside(lot, frame['half'], frame['cx'], frame['cy'], PROPERTY_FIT['edge_margin_m']),
            'development_inside_margin': _inside(development, frame['half'], frame['cx'], frame['cy'], PROPERTY_FIT['edge_margin_m']),
            'property_inside': _inside(prop, frame['half'], frame['cx'], frame['cy'], 0.),
            'if_unreviewed_counted': alternative}


def development_of(lots, parcels):
    """(development geometry, development plans, plans wider than the cap, the lots' plans) from parcels {pfi: {lot, plan, class, m}}:
    a lot's plan is a development when any parcel read on that plan is common property (lot text CM / CP) and the plan's approved,
    non-excluded parcels span at most max_span_m (census v2)."""
    plans = sorted({lot['plan'] for lot in lots if lot.get('plan')})
    common = [plan for plan in plans if any(q['plan'] == plan and common_text(q['lot']) for q in parcels.values())]
    developed, wide, geos = [], {}, []
    for plan in common:
        members = [q['m'] for q in parcels.values() if q['plan'] == plan and q['class'] not in EXCLUDED_CLASSES]
        geo = unary_union(members) if members else Polygon()
        if geo.is_empty:
            continue
        if _span(geo) <= PROPERTY_FIT['max_span_m']:
            developed.append(plan)
            geos.append(geo)
        else:
            wide[plan] = round(_span(geo), 1)
    return (unary_union(geos) if geos else Polygon()), developed, wide, plans


def parcels_complete(lot, development, coverage, plans, wide):
    """Census v2's completeness check: the uncovered m2 of the lot and the development grown by open_tolerance_m."""
    lot_open, dev_open = open_area(lot, coverage), open_area(development, coverage)
    may_join = (development is None or development.is_empty) and not wide and \
        any(str(plan).upper().startswith(tuple(PROPERTY_FIT['common_property_plans'])) for plan in plans)
    floor = PROPERTY_FIT['open_min_m2']
    return {'lot_open_m2': round(lot_open, 1), 'development_open_m2': round(dev_open, 1), 'lot_may_join_development': may_join,
            'complete': dev_open < floor and (lot_open < floor or not may_join)}


def _derived_lots(subject, parcels):
    """Census v2's derived lot for a window whose plan resolved none: the usable parcel with the largest share of the subject (at
    least share_min), plus one numbered parcel above second_lot_share; [] when none."""
    if subject.is_empty or subject.area <= 0:
        return []
    shares = sorted(((q['m'].intersection(subject).area/subject.area, pfi) for pfi, q in parcels.items()
                     if q['class'] not in EXCLUDED_CLASSES and q['m'].intersects(subject)), reverse=True)
    shares = [(share, pfi) for share, pfi in shares if share > 0]
    if not shares or shares[0][0] < PROPERTY_FIT['share_min']:
        return []
    chosen = [parcels[shares[0][1]]]
    second = [parcels[pfi] for share, pfi in shares[1:] if share > PROPERTY_FIT['second_lot_share'] and is_numbered(parcels[pfi]['lot'])]
    return chosen+second[:1]


def _subject_copy(poly, subject_parts):
    """A building outline that is a copy of a slice or body: it covers one by half (either way) or IoU subject_iou."""
    for part in subject_parts:
        inter = part.intersection(poly).area
        if inter <= 0:
            continue
        if (inter/poly.area >= PROPERTY_FIT['subject_cover'] or inter/part.area >= PROPERTY_FIT['subject_cover']
                or inter/(part.area+poly.area-inter) >= PROPERTY_FIT['subject_iou']):
            return True
    return False


class ParcelPool:
    """Frozen parcel features by pfi (first read wins, approved only, repaired when invalid) and the query boxes of the payloads read
    (WGS84); `area` (WGS84) keeps only features and boxes near the targets."""

    def __init__(self, area=None):
        self.area = area
        self.features = {}
        self.boxes = []
        self.sources = Counter()

    def add(self, payload, source):
        if not isinstance(payload, dict) or not isinstance(payload.get('features'), list):
            return
        bounds = payload.get('bounds')
        if bounds and len(bounds) == 4:
            query = box(*bounds)
            if self.area is None or query.intersects(self.area):
                self.boxes.append(query)
        for feature in payload['features']:
            props = (feature or {}).get('properties') or {}
            pfi = str(props.get('pfi'))
            if not props.get('pfi') or pfi in self.features or props.get('status', 'approved') != 'approved':
                continue
            try:
                geo = shape(feature['geometry'])
            except (KeyError, ValueError, TypeError, AttributeError):
                continue
            if geo.is_empty or (self.area is not None and not geo.intersects(self.area)):
                continue
            if not geo.is_valid:
                geo = make_valid(geo)
                geo = unary_union([g for g in getattr(geo, 'geoms', [geo]) if g.geom_type in ('Polygon', 'MultiPolygon')])
            self.features[pfi] = {'pfi': pfi, 'lot': lot_text(props), 'plan': props.get('plan'), 'geo': geo}
        self.sources[source] += 1

    def load_disk(self, root, extra_dirs=()):
        root = Path(root)
        labels = dict(zip(PARCEL_GLOBS, ('scene-parcels', 'units', 'eyes-parcels', 'roof-review-loop'), strict=True))
        paths = [(labels[pattern], p) for pattern in PARCEL_GLOBS for p in sorted(root.glob(pattern))]
        paths += [('extra', p) for d in extra_dirs for p in sorted((Path(d) if Path(d).is_absolute() else root/d).glob('*.json'))]
        for label, path in paths:
            saved = _read_json(path)
            data = saved.get('data') if isinstance(saved, dict) and isinstance(saved.get('data'), dict) else saved
            self.add(data, f'{label}:{path.name[:16]}')
        return self

    def near(self, area):
        features = [f for f in self.features.values() if f['geo'].intersects(area)]
        return features, [b for b in self.boxes if b.intersects(area)]

    def summary(self):
        return {'payloads': sum(self.sources.values()), 'features': len(self.features), 'query_boxes': len(self.boxes),
                'by_source': dict(sorted(sum((Counter({k.split(':')[0]: v}) for k, v in self.sources.items()), Counter()).items()))}


class PropertyFitter:
    """The planner's property-fit step for one window: reads the pool and the index's review layers, makes the bounded parcel read
    when the property is open and the plan is not offline, and returns (record, refit or None)."""

    def __init__(self, root, index, pool, *, offline, count_unreviewed=None):
        self.root = Path(root)
        self.index = index
        self.pool = pool
        self.offline = offline
        self.count_unreviewed = count_unreviewed
        self.reads = []

    def _context(self, geometry, project, near_area, subject_m):
        features, boxes = self.pool.near(near_area)
        parcels = {}
        for f in features:
            m = transform(project, f['geo'])
            if m.is_empty:
                continue
            parcels[f['pfi']] = {**f, 'm': m, 'class': parcel_class(f['lot'], f['plan'], round(_span(m), 2))}
        coverage = unary_union([transform(project, b) for b in boxes]) if boxes else Polygon()
        lots, lot_source = geometry['lots'], 'window'
        if not lots:
            lots, lot_source = _derived_lots(subject_m, parcels), 'derived_from_parcels_read'
        lot_m = unary_union([lot['m'] if 'm' in lot else transform(project, lot['geo']) for lot in lots]) if lots else Polygon()
        development, plans_dev, wide, plans = development_of(lots, parcels) if lots else (Polygon(), [], {}, [])
        complete = parcels_complete(lot_m, development, coverage, plans, wide) if lots else {
            'lot_open_m2': 0., 'development_open_m2': 0., 'lot_may_join_development': False, 'complete': True}
        return {'parcels': parcels, 'coverage': coverage, 'lot': lot_m, 'lots': lots, 'lot_source': lot_source if lots else None,
                'development': development, 'development_plans': plans_dev,
                'development_wider_than_cap': wide, 'plans': plans, 'completeness': complete,
                'development_parcels': sum(1 for q in parcels.values() if q['plan'] in plans_dev and q['class'] not in EXCLUDED_CLASSES)}

    def _read(self, ctx, unproject, label):
        grown = unary_union([g for g in (ctx['lot'], ctx['development']) if not g.is_empty]).buffer(PROPERTY_FIT['open_tolerance_m'])
        uncovered = grown.difference(ctx['coverage']) if not ctx['coverage'].is_empty else grown
        minx, miny, maxx, maxy = uncovered.bounds
        west, south = unproject(minx, miny)
        east, north = unproject(maxx, maxy)
        bounds = [round(v, 7) for v in (west, south, east, north)]
        key = signature(['property-fit', label, bounds])
        path = self.root/PARCELS_DIR/f'{key}.json'
        saved = _read_json(path)
        if isinstance(saved, dict) and saved.get('key') == key and saved.get('sha256') == signature(saved.get('data')):
            data, how = saved['data'], 'eyes-parcels'
        else:
            data, how = fetch_frozen(pad_bounds(bounds, PROPERTY_FIT['parcel_read_pad_deg']), path, key), 'vicmap'
        self.pool.add(data, f'property-fit-read:{how}')
        record = {'path': str(PARCELS_DIR/f'{key}.json'), 'source': how, 'bounds': bounds, 'sha256': signature(data)}
        self.reads.append(record)
        return record

    def __call__(self, members, geometry, frame, frame_px):
        lon, lat = frame['centroid']['lon'], frame['centroid']['lat']
        project, unproject = metre_frame(lon, lat)
        base_half = frame_px*pixel_spacing(planned_frame(frame, frame_px, 'base'))/2
        reach = PROPERTY_FIT['neighbourhood_m']
        west, south = unproject(-reach, -reach)
        east, north = unproject(reach, reach)
        near_area = box(west, south, east, north)
        subject_parts = [transform(project, m['slice_geo']) for m in members]+[transform(project, b['geo']) for m in members for b in m['bodies']]
        subject_m = unary_union(subject_parts)
        reads = []
        ctx = self._context(geometry, project, near_area, subject_m)
        while not ctx['completeness']['complete'] and not self.offline and len(reads) < PROPERTY_FIT['parcel_reads']:
            reads.append(self._read(ctx, unproject, [round(lon, 7), round(lat, 7)]))
            ctx = self._context(geometry, project, near_area, subject_m)
        base = {'span_m': round(2*base_half, 3), 'frame_px': frame_px, 'centre': {'lon': lon, 'lat': lat}}
        record = {'version': PROPERTY_FIT['version'], 'base': base, 'lot_source': ctx['lot_source'],
                  'lots': [{k: lot.get(k) for k in ('pfi', 'lot', 'plan')} for lot in ctx['lots']], **ctx['completeness'], 'parcel_reads': reads,
                  'development_plans': ctx['development_plans'], 'development_wider_than_cap': ctx['development_wider_than_cap'],
                  'development_parcels': ctx['development_parcels'], 'lot_plans': ctx['plans']}
        if not ctx['completeness']['complete']:
            why = ('offline: no parcel read' if self.offline else f"{len(reads)} bounded read(s) made, still open")
            return {**record, 'status': 'parcels_open', 'centre_shift_m': 0., 'reason': (
                f"parcels_open: the lot / development is not covered by the frozen parcel payloads read (lot {ctx['completeness']['lot_open_m2']} "
                f"m2, development {ctx['completeness']['development_open_m2']} m2 uncovered; {why}); the frame is a lower bound and the "
                'window is never captured')}, None
        objects = self._objects(near_area, project, ctx, subject_parts)
        fit = property_fit(base_half, subject_m, ctx['lot'], ctx['development'], objects, count_unreviewed=self.count_unreviewed)
        record.update({k: fit[k] for k in ('status', 'recentred', 'capped', 'property_fits', 'keep', 'margin_m', 'property_bounds',
                                           'objects_counted', 'objects_counted_by_kind', 'objects_left_out_unreviewed', 'left_out_sample',
                                           'objects_counted_cut_by_own_frame', 'objects_counted_fitted',
                                           'subject_inside_margin', 'lot_inside_margin', 'development_inside_margin', 'property_inside')})
        record.update({'half_m': round(fit['half'], 4), 'cx_m': round(fit['cx'], 4), 'cy_m': round(fit['cy'], 4),
                       'property_need_half_m': None if fit['property_need_half_m'] is None else round(fit['property_need_half_m'], 3),
                       'centre_shift_m': round(math.hypot(fit['cx'], fit['cy']), 3),
                       'if_unreviewed_counted': None if fit['if_unreviewed_counted'] is None else {
                           k: (round(v, 4) if isinstance(v, float) else v) for k, v in fit['if_unreviewed_counted'].items()}})
        if not fit['recentred']:
            return record, None
        span = 2*fit['half']
        px = frame_px_for(span, lat)
        requested, refit = _frame_for(unproject(fit['cx'], fit['cy']), span, px)
        return record, (requested, refit, px)

    def _objects(self, near_area, project, ctx, subject_parts):
        lot_pfis = {lot['pfi'] for lot in ctx['lots']}
        usable = {pfi: q for pfi, q in ctx['parcels'].items() if q['class'] not in EXCLUDED_CLASSES}
        pfis = list(usable)
        tree = STRtree([usable[p]['m'] for p in pfis]) if pfis else None
        out = []
        for entry in self.index.objects_near(near_area):
            poly = transform(project, entry['geo'])
            if poly.is_empty or poly.area <= 0:
                continue
            on = entry['kind'] == 'building' and _subject_copy(poly, subject_parts)
            if not on and tree is not None and (lot_pfis or ctx['development_plans']):
                shares = []
                for i in tree.query(poly):
                    q = usable[pfis[int(i)]]
                    inter = q['m'].intersection(poly).area
                    if inter > 0:
                        shares.append((inter/poly.area, q['pfi']))
                if shares:
                    share, pfi = max(shares)
                    on = share >= PROPERTY_FIT['share_min'] and (pfi in lot_pfis or usable[pfi]['plan'] in ctx['development_plans'])
            if on:
                out.append({'id': f"{entry['scene_id']}/{entry['object_id']}", 'kind': entry['kind'], 'reviewed': entry['reviewed'], 'geo': poly,
                            'cut_by_own_frame': entry['cut_by_own_frame'], 'registration': entry['registration']})
        return out


def window_for(members, fit=None):
    """The window of a subject group: geometry, the registered frame, acceptance (widened once when the subject clips), then the
    property-fit step when `fit` is given (the plan's PropertyFitter; grouping's provisional windows use the base rule only)."""
    geometry = _window_geometry(members)
    lat = geometry['lat']
    span_m = geometry['span_m']
    frame_px = frame_px_for(span_m, lat)
    requested, frame = _frame_for(geometry['centre'], span_m, frame_px)
    geometries = [m['slice_geo'] for m in members]+[b['geo'] for m in members for b in m['bodies']]
    check = acceptance(planned_frame(frame, frame_px, 'planned'), geometries)
    widened = False
    if not check['inside_with_margin'] and frame_px == 512:
        widened, span_m, frame_px = True, WINDOW['max_span_m'], 1024
        requested, frame = _frame_for(geometry['centre'], span_m, frame_px)
        check = acceptance(planned_frame(frame, frame_px, 'planned'), geometries)
    flags = list(geometry['flags'])
    fit_record = None
    if fit is not None:
        fit_record, refit = fit(members, geometry, frame, frame_px)
        if refit is not None:
            fit_requested, fit_frame, fit_px = refit
            fit_check = acceptance(planned_frame(fit_frame, fit_px, 'planned'), geometries)
            if fit_check['inside_with_margin'] or not check['inside_with_margin']:
                requested, frame, frame_px, check = fit_requested, fit_frame, fit_px, fit_check
            else:
                fit_record['kept_base'] = 'the fitted frame left a slice or body within edge_margin_m of its edge'
                flags.append('property_fit_kept_base')
        if fit_record['status'] == 'parcels_open':
            flags.append('parcels_open')
        elif fit_record['status'] == 'capped':
            flags.append('property_capped')
    if not check['inside_with_margin']:
        flags.append('subject_exceeds_window')
    region = frame['region']
    window = {'rule': geometry['rule'], 'centre': {'lon': frame['centroid']['lon'], 'lat': frame['centroid']['lat']},
              'span_m': round(region['span_m'], 3), 'frame_px': frame_px, 'zoom': region['imagery_zoom'],
              'm_per_px': round(region['span_m']/frame_px, 6), 'native_m_per_px': round(region['native_meters_per_pixel'], 6),
              'requested_bounds': requested, 'bounds': region['bounds'],
              'centre_shift_m': 0. if fit_record is None or 'kept_base' in fit_record else fit_record['centre_shift_m'],
              'spatial_group': region['spatial_group'], 'split': region['split']}
    if fit_record is not None:
        window['property_fit'] = fit_record
    return {'window': window, 'frame': frame, 'lots': geometry['lots'], 'flags': flags, 'subject_geo': geometry['subject_geo'],
            'acceptance_predicted': {**check, 'widened': widened}}


# --- grouping -----------------------------------------------------------------------------------------------------

def _provisional_bounds(subject):
    return box(*window_for([subject])['window']['bounds'])


GROUP_REASONS = ('shared_body', 'shared_lot', 'window_overlap')


def numbered_core(subject):
    """The pfis of the numbered lots in a subject's core: one, or two through the two-lot rule."""
    return frozenset(lot['pfi'] for lot in subject['core'] if lot['class'] == 'numbered')


def group_subjects(subjects, by=GROUP_REASONS):
    """Union-find: shared body, shared lot, or provisional windows overlapping by half the smaller -> one group, but only
    when one side's numbered-lot core contains the other's (empty, equal, or a two-lot core holding a one-lot core): a
    group is one house/lot, so a window never spans more than one numbered lot core except through the two-lot rule.
    A union the lot cores refuse is recorded on both subjects as kept_apart. Pairs are visited in input order (the plan's
    targets order), so the result is deterministic.

    `by` restricts the reasons that unite (the plan re-groups an over-size merged group by shared body and lot only).
    """
    n = len(subjects)
    parent = list(range(n))
    cores = [numbered_core(s) for s in subjects]
    reasons = [set() for _ in range(n)]
    kept_apart = [set() for _ in range(n)]

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def unite(i, j, why):
        if why not in by:
            return
        ri, rj = find(i), find(j)
        if ri != rj:
            a, b = cores[ri], cores[rj]
            if not (a <= b or b <= a):
                kept_apart[i].add(why)
                kept_apart[j].add(why)
                return
            parent[ri] = rj
            cores[rj] = a | b
        reasons[i].add(why)
        reasons[j].add(why)

    boxes = [_provisional_bounds(s) for s in subjects]
    bodies = [{(b['scene_id'], b['object_id']) for b in s['bodies']} for s in subjects]
    lots = [{lot['pfi'] for lot in s['core']} for s in subjects]
    for i in range(n):
        for j in range(i+1, n):
            if bodies[i] & bodies[j]:
                unite(i, j, 'shared_body')
            if lots[i] & lots[j]:
                unite(i, j, 'shared_lot')
            smaller = min(boxes[i].area, boxes[j].area)
            if smaller > 0 and boxes[i].intersection(boxes[j]).area/smaller >= WINDOW['group_overlap']:
                unite(i, j, 'window_overlap')
    groups = {}
    for i in range(n):
        subjects[i]['kept_apart'] = sorted(kept_apart[i])
        groups.setdefault(find(i), []).append(i)
    out = []
    for indices in groups.values():
        members = [subjects[i] for i in indices]
        why = sorted(set().union(*(reasons[i] for i in indices))) if len(indices) > 1 else []
        out.append((members, why))
    out.sort(key=lambda g: (g[0][0]['scene_id'], g[0][0]['object_id']))
    return out


def dedupe_rows(rows):
    """Mark every later row repeating a window_id duplicate_in_plan, naming the FIRST row's own first subject."""
    seen = {}
    for row in rows:
        first = row['subjects'][0]
        if row['window_id'] in seen:
            row['dedupe'] = 'duplicate_in_plan'
            row['duplicate_of'] = seen[row['window_id']]
        else:
            seen[row['window_id']] = f"{first['scene_id']}/{first['object_id']}"
    return rows


# --- pre-registered window rule (WINDOW.json) ------------------------------------------------------------------------

def window_rule(design, *, latitude=REFERENCE_LAT, now=None):
    """The window rule of design section 2 as a record: constants are this module's WINDOW dict and every span is derived
    from scene_region.region_frame's zoom term at write time (never typed); the overlap gate is DESIGN.json's."""
    values, design_sha = design
    native = scene_lot.native_m_per_px(latitude, ZOOM)
    return {
        'version': VERSION, 'written_at': now or _now(), 'written_before': 'any non-qa capture',
        'written_by': 'scripts.ml.scene_focus.write_window_rule (register): constants are the module WINDOW dict; spans are derived '
                      'from scene_region.region_frame\'s zoom term at write time, never typed',
        'design_path': str(DESIGN_PATH), 'design_sha256': design_sha, 'provider': PROVIDER, 'zoom_max': ZOOM, 'frame_px': list(FRAME_PX),
        'constants': dict(WINDOW), 'body_distance_m': values['max_shift_m'],
        'span_rule': {
            'lot': 'span_m = clamp(max(lot_union_span_m*lot_grow + lot_margin_m, subject_span_m*subject_factor + subject_margin_m), '
                   'lot_min_span_m, max_span_m); centre = the lot-union bounding-box centre',
            'lot_exceeds_window': 'lot core grown above max_span_m: the members\' own lots only; if still over, span_m = max_span_m about '
                                  'the centre of the subject plus the lot part within max_span_m of the subject centre, flagged '
                                  'lot_exceeds_window and named in the title',
            'subject': 'body known, no lot: span_m = clamp(subject_span_m*subject_factor + subject_margin_m, lot_min_span_m, max_span_m); '
                       'centre = the subject (slice union body) bounding-box centre',
            'subject_no_body': 'neither: span_m = clamp(slice_span_m*object_factor + object_margin_m, object_min_span_m, native_span_512_m); '
                               'centre = the slice bounding-box centre',
            'square': 'scene_region.region_frame squares the request on its longer side and rounds coordinates to 7 decimals '
                      '(API limit 10-250 m)',
            'widen_once': 'a 512 px window whose slices or bodies come within edge_margin_m of an edge is widened once to max_span_m at '
                          '1024 px; still clipped -> subject_exceeds_window, never captured'},
        'frame_px_rule': '512 when span_m <= native_span_512_m (frame_px_for) else 1024: the frame is never coarser than native z20; a '
                         '1024 px frame reaches native detail at native_span_1024_m, which the max_span_m cap keeps it under',
        'zoom': {'formula': 'zoom = min(PROVIDER_MAX_ZOOM[provider], ceil(log2(frame_px/256/span_world_fraction))) (scene_region.region_frame)',
                 'native_m_per_px_formula': 'WORLD_SPAN_M*cos(lat)/(256*2**zoom)', 'world_span_m': WORLD_SPAN_M,
                 'provider_max_zoom': PROVIDER_MAX_ZOOM[PROVIDER], 'derived_at_latitude': latitude, 'native_m_per_px_z20': native,
                 'native_span_512_m': native_span_m(latitude, 512), 'native_span_1024_m': native_span_m(latitude, 1024),
                 'z20_max_span_512_m': z20_max_span_m(latitude, 512), 'z20_max_span_1024_m': z20_max_span_m(latitude, 1024),
                 'note': 'region_frame keeps z20 while span_m < 2*frame_px*native_m_per_px_z20: about 241 m for a 1024 px frame (120.8 m '
                         'for 512 px). native_span_1024_m (about 120.8 m) is only the native-span point, moot under the max_span_m cap; '
                         'the plan header re-derives these at the targets\' mean latitude'},
        'subject_rule': {'slice': 'the frame-cut object ring in WGS84 (scene_confidence.geographic); frame-cut = the ring\'s bounding box '
                                  'within frame_tolerance_px of a frame edge (touched_edges: one vertex within tolerance of an edge is '
                                  'enough; a superset of scene_expand.touches_frame\'s two-vertex test, WINDOW.json amendment 2026-10-01)',
                         'body': 'building objects of other REGISTERED scenes within body_distance_m (DESIGN max_shift_m) of the slice with '
                                 'at least body_outside_fraction of their area outside the source frame; a window input only, never '
                                 'carried or counted',
                         'parcels': 'scene_lot_tags.parcels_for_scene order seeded by the subject polygon, then (unless offline) one bounded '
                                    'Vicmap read frozen as a NEW file',
                         'lot': 'the approved non-excluded parcel with the largest area share of the subject when share >= share_min; ONE '
                                'second numbered lot joins the core when its share > second_lot_share (the two-lot core); common '
                                'property joins when its share >= share_min',
                         'body_side': 'the point body_side_m beyond the midpoint of each touched edge must fall in the chosen lot; in another '
                                      'non-excluded parcel, that parcel is the subject and the overlapped one is slice_parcel',
                         'address': 'exactly one address point inside a numbered lot gives address_id; several or none keeps the pfi; never '
                                    'a tie broken by distance'},
        'parcel_classes': {'numbered': 'lot text matching \\d+[A-Za-z]?',
                           'common': 'lot text starting CM or CP: joins the core only when overlapped at share_min, never brings the '
                                     'plan-block, never gives an address identity (flag common_property)',
                           'plan_block': 'lot text empty or None with a plan starting CP or CM (for example pfi 204419087 / CP153157): never '
                                         'a lot, never a core member, never a body-side target; recorded as excluded_parcels',
                           'oversize': 'any parcel whose bounding span exceeds max_span_m (+ 0.01 m): treated as a plan-block',
                           'unnumbered': 'empty lot text with another plan under the cap: may be the lot by share, never an address identity',
                           'plan_block_only': 'a subject whose only parcels at share_min are excluded is subject-centred with lot unknown '
                                              '(reason plan_block_only, window flag plan_block)'},
        'grouping': {'reasons': {'shared_body': 'two subjects share a body object', 'shared_lot': 'their cores share a parcel pfi',
                                 'window_overlap': 'their provisional windows overlap by at least group_overlap of the smaller'},
                     'lot_core_rule': 'a group is one house/lot: a union is made only when one side\'s numbered-lot core contains the '
                                      'other\'s (empty, equal, or a two-lot core holding a one-lot core), so a window never spans more '
                                      'than one numbered lot core except through the two_lot rule; refused unions are recorded as '
                                      'kept_apart on both subjects; pairs are visited in targets order',
                     'two_lot_rule': 'a subject straddling two numbered lots (second share > second_lot_share) has a two-lot core and '
                                     'its window spans both lots (identity win, never lot-<address_id>)',
                     'over_size_split': 'a merged group flagged subject_exceeds_window is re-grouped by shared body and lot only; each '
                                        'part is planned alone with group.split_from',
                     'several_sources': 'targets from several source scenes on one lot share one window; the scene with most targets '
                                        '(registered first) is carry_from, the others are uncarried_source'},
        'acceptance': {'edge_margin_m': WINDOW['edge_margin_m'],
                       'predicted': 'every slice and body inside the planned frame with edge_margin_m to every edge, else widen once '
                                    '(512 -> 1024 at max_span_m) then flag subject_exceeds_window',
                       'measured': 'at capture: scene_recapture.measured_transform against carry_from verified under DESIGN.json, every '
                                   'slice through the measured pixel_affine and every body through the window frame inside with '
                                   'edge_margin_m; a failure saves nothing'},
        'overlap_gate': {'source': 'DESIGN.json subject_recapture.min_overlap_px (subject windows, frame_transform.role frame_cut_target); '
                                   'min_overlap_fraction is recorded beside it and stays the same-address rule',
                         'min_overlap_px': (subject_floor(values) or {}).get('min_overlap_px'),
                         'min_overlap_fraction': values['min_overlap_fraction'], 'design_sha256': design_sha,
                         'rule': 'the PREDICTED scene_recapture.frame_transform overlap region of the carry_from frame in the planned '
                                 'window (the source footprint inside the frame, eroded by EDGE_PX as the residual erodes its cover mask; '
                                 'the smaller side of its bounding box, in target px) must reach subject_recapture.min_overlap_px '
                                 '(overlap_gate ok), else below_design: never captured; the fraction is recorded, not gating'},
        'takeover_rule': {'constants': dict(TAKEOVER), 'rule': TAKEOVER_RULE, 'count': TAKEOVER_COUNT},
        'property_fit': {'constants': json.loads(json.dumps(PROPERTY_FIT)), 'rule': PROPERTY_FIT_RULE, 'field': PROPERTY_FIT_FIELD},
        'identity': {'lot': 'lot-<address_id> for the single numbered lot of one resolved address only when no scene '
                            'roof_training_250-<address_id> or house-<address_id> exists (those families\' own-lot recapture writes that '
                            'name: address_reserved_for_scene, reserved_by) and no source scene in the window has that address '
                            '(address_reserved_for_source) and not lot_exceeds_window',
                     'win': 'win-<sha256(json([provider, requested bounds rounded 7, frame_px]))[:16]> otherwise',
                     'dedupe': 'an existing data/scenes/<id> is exists_skip; a repeated id in one plan is duplicate_in_plan naming the '
                               'first row\'s own first subject'},
    }


def write_window_rule(root, design=None, *, latitude=REFERENCE_LAT, now=None):
    """Pre-register WINDOW.json under root (a NEW file; refused when it exists): (path, sha256)."""
    root = Path(root)
    path = root/WINDOW_PATH
    rule = window_rule(_design(root, design), latitude=latitude, now=now)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, 'x') as handle:
            handle.write(json.dumps(rule, indent=1, sort_keys=True)+'\n')
    except FileExistsError as error:
        raise ValueError(f'{WINDOW_PATH} exists: the window rule is pre-registered once and never rewritten '
                         '(a further dated file can retire it)') from error
    return path, _sha(path.read_bytes())


def load_window_rule(root):
    """(rule, sha256) of the pre-registered WINDOW.json; refuses a missing file or one whose constants are not this module's."""
    path = Path(root)/WINDOW_PATH
    try:
        data = path.read_bytes()
        rule = json.loads(data)
    except (OSError, ValueError) as error:
        raise ValueError(f'{WINDOW_PATH} is missing or unreadable: the window rule is pre-registered (scene_focus register) '
                         'before any plan or capture') from error
    if not isinstance(rule, dict) or rule.get('version') != VERSION or rule.get('constants') != json.loads(json.dumps(WINDOW)):
        raise ValueError(f"{WINDOW_PATH} constants differ from this module's WINDOW ({VERSION}): a changed rule needs a new dated "
                         'file and a re-plan (window_rule_mismatch)')
    fit = property_fit_constants(rule)
    if fit is not None and fit != json.loads(json.dumps(PROPERTY_FIT)):
        raise ValueError(f"{WINDOW_PATH} property-fit constants ({PROPERTY_FIT_FIELD}) differ from this module's PROPERTY_FIT: a changed "
                         'rule needs a new dated amendment and a re-plan (window_rule_mismatch)')
    return rule, _sha(data)


def property_fit_constants(rule):
    """The property-fit constants a WINDOW.json registers: its body record (a file registered with the rule) or the NEWEST dated
    amendment of field span_rule.property_fit; None for a file written before the 2026-10-08 amendment."""
    body = rule.get('property_fit') if isinstance(rule, dict) else None
    if isinstance(body, dict):
        return body.get('constants')
    found = [a for a in (rule.get('amendments') or ()) if isinstance(a, dict) and a.get('field') == PROPERTY_FIT_FIELD] if isinstance(rule, dict) else []
    return found[-1].get('property_fit') if found else None


def property_fit_amendment(rule, previous_sha256, *, at, reason, extra=None):
    """The dated WINDOW.json amendment entry registering PROPERTY_FIT (field span_rule.property_fit; the body text and the WINDOW
    constants are untouched). The applying script adds the reviewer's words, the evidence and the code shas through `extra`; refuses a
    file that already registers a property-fit rule."""
    if property_fit_constants(rule) is not None:
        raise ValueError(f'{WINDOW_PATH} already registers a property-fit rule: pre-registered once, never rewritten')
    entry = {'at': at, 'field': PROPERTY_FIT_FIELD, 'previous_sha256': previous_sha256, 'window_sha256_before': previous_sha256,
             'thresholds_changed': False, 'constants_changed': False, 'constants_added': 'property_fit',
             'property_fit': json.loads(json.dumps(PROPERTY_FIT)), 'change': PROPERTY_FIT_RULE, 'reason': reason,
             'applies_to': 'windows planned under this file\'s sha256 or a later one; a window already captured keeps its frame and is '
                           'exists_skip by its subjects (never re-planned)'}
    return {**entry, **(extra or {})}


# --- plan ---------------------------------------------------------------------------------------------------------

def displacement_note(root, lat):
    doc = Path(root)/DISPLACEMENT['source_doc']
    return {**DISPLACEMENT, 'approx_m': round(DISPLACEMENT['native_px'][0]*scene_lot.native_m_per_px(lat, DISPLACEMENT['native_zoom']), 4),
            'source_doc_sha256': _sha(doc.read_bytes()) if doc.exists() else None}


def _design(root, design):
    """(values, sha256) of the pre-registered DESIGN.json: a plan or capture never runs without it."""
    return design if design is not None else load_design(root)


def _title(carry_from, place, count, flags):
    noun = 'frame-cut target' if count == 1 else 'frame-cut targets'
    title = f'Recapture window · {carry_from} · {place} · {count} {noun}'
    if 'lot_exceeds_window' in flags:
        title += ' · lot exceeds window'
    return title


def _unknown_place(members):
    blocks = sorted({(e['class'], e['plan'] or e['pfi']) for m in members for e in m['excluded_parcels'] if e['share'] >= WINDOW['share_min']})
    if not blocks:
        return 'lot unknown'
    return 'lot unknown · '+', '.join(f"{'plan block' if klass == 'plan_block' else 'oversize parcel'} {name}" for klass, name in blocks)


def window_row(root, members, why, sources, index, design, window_rule, *, now, fit=None):
    built = window_for(members, fit=fit)
    window, frame, lots, flags = built['window'], built['frame'], built['lots'], built['flags']
    by_scene = {}
    for m in members:
        by_scene.setdefault(m['scene_id'], []).append(m['object_id'])
    ranked = sorted(by_scene, key=lambda sid: (-len(by_scene[sid]), 0 if registration(sources[sid]) == 'region' else 1, sid))
    carry_from = ranked[0]
    numbered = [lot for lot in lots if lot['class'] == 'numbered']
    common = [lot for lot in lots if lot['class'] == 'common']
    address_id = numbered[0]['address_id'] if len(numbered) == 1 and not common else None
    identity = 'win'
    reserved = []
    if window['rule'] == 'lot' and address_id is not None and 'lot_exceeds_window' not in flags:
        known = {s['id'] for s in (index.scenes or ()) if isinstance(s, dict) and s.get('id')}
        reserved = reserved_by(root, address_id, known)
        if any(sources[sid].get('address_id') == address_id for sid in by_scene):
            flags.append('address_reserved_for_source')
        if reserved:
            flags.append('address_reserved_for_scene')
        if not reserved and 'address_reserved_for_source' not in flags:
            identity = 'lot'
    wid = f'lot-{address_id}' if identity == 'lot' else window_id(PROVIDER, window['requested_bounds'], window['frame_px'])
    full_address = next((m['address']['full_address'] for m in members if m['address'] and m['address']['address_id'] == address_id), None)
    if address_id is not None:
        place = full_address or f'address {address_id}'
    elif lots:
        place = ('lot ' if len(lots) == 1 else 'lots ')+', '.join(lot['pfi'] for lot in lots)
    else:
        place = _unknown_place(members)
    count = len(members)
    title = _title(carry_from, place, count, flags)
    values, design_sha = design
    _, window_sha = window_rule
    planned = planned_frame(frame, window['frame_px'], wid)
    floor = _floor(values)
    source_rows = []
    gate, gate_detail = 'unknown', None
    for sid in ranked:
        scene = sources[sid]
        try:
            overlap = frame_transform(scene, planned, role=SUBJECT_ROLE)['overlap']
            predicted, region = overlap['fraction'], overlap['region_px']
        except GEOMETRY_ERRORS:
            predicted, region = None, None
        row = {'scene_id': sid, 'object_ids': by_scene[sid], 'registration': registration(scene),
               'role': 'carried_source' if sid == carry_from else 'uncarried_source', 'predicted_overlap': predicted,
               'predicted_region_px': region,
               'address_id': scene.get('address_id'), 'image_width': scene.get('image_width'), 'image_height': scene.get('image_height'),
               'displacement_note': displacement_note(root, window['centre']['lat']) if registration(scene) == 'fitted' else None}
        source_rows.append(row)
        if sid == carry_from and region is not None:
            passes_px = region['side_px'] >= floor
            gate = 'ok' if passes_px else 'below_design'
            gate_detail = {'rule': 'subject_recapture.min_overlap_px', 'min_overlap_px': floor, 'region_side_px': region['side_px'],
                           'region_area_px': region['area_px'], 'fraction': predicted, 'min_overlap_fraction': values['min_overlap_fraction'],
                           'passes_px_floor': passes_px, 'passes_fraction_rule': predicted >= values['min_overlap_fraction']}
    exclude = set(by_scene)
    covering = built['subject_geo']
    if lots:
        covering = unary_union([covering, *(lot['geo'] for lot in lots)])
    covered = index.frames_covering(covering, exclude)
    target = Path(root)/'data/scenes'/wid
    dedupe = 'exists_skip' if target.exists() else 'new'
    source = sources[carry_from]
    subjects = []
    for m in members:
        subjects.append({'scene_id': m['scene_id'], 'object_id': m['object_id'], 'status': m['status'], 'slice_area_m2': m['slice_area_m2'],
                         'slice_bounds': m['slice_bounds'], 'touched_edges': m['touched_edges'],
                         'bodies': [{'scene_id': b['scene_id'], 'object_id': b['object_id'], 'area_m2': b['area_m2'],
                                     'outside_fraction': b['outside_fraction'], 'distance_m': b['distance_m'],
                                     'bounds': [round(v, 7) for v in b['geo'].bounds]} for b in m['bodies']],
                         'lot': m['lot'], 'core': [{k: v for k, v in lot.items() if k != 'geo'} for lot in m['core']], 'two_lot': m['two_lot'],
                         'excluded_parcels': m['excluded_parcels'], 'kept_apart': m['kept_apart'],
                         'slice_parcel': m['slice_parcel'], 'reason': m['reason'], 'probes': m['probes'],
                         'parcels_source': m['parcels_source'], 'parcels_sha256': m['parcels_sha256'], 'parcels_cover_subject': m['parcels_cover_subject']})
    return {'version': PLAN_VERSION, 'window_id': wid, 'identity': identity, 'reserved_by': reserved, 'dedupe': dedupe, 'carry_from': carry_from,
            'source_model_signature': source.get('model_signature'), 'source_image_sha256': source.get('image_sha256'),
            'sources': source_rows, 'subjects': subjects,
            'lots': [{k: v for k, v in lot.items() if k != 'geo'} for lot in lots], 'lots_label': lot_label([lot['lot'] for lot in lots]) if lots else None,
            'address_id': address_id, 'full_address': full_address, 'title': title, 'window': window, 'frame': planned,
            'acceptance_predicted': built['acceptance_predicted'],
            'group': {'size': count, 'reasons': why, 'two_lot_rule': any(m['two_lot'] for m in members)},
            'covered_by_existing': covered, 'flags': flags, 'overlap_gate': gate, 'overlap_gate_detail': gate_detail,
            'design_sha256': design_sha, 'window_sha256': window_sha,
            'parcels_source': members[0]['parcels_source'], 'parcels_sha256': members[0]['parcels_sha256'], 'planned_at': now}


def _floor(values):
    """The pre-registered subject overlap floor (target px) of the design values; a design without one plans nothing."""
    floor = subject_floor(values)
    if floor is None:
        raise ValueError(f'{DESIGN_PATH} has no subject_recapture.min_overlap_px: the subject overlap floor is pre-registered (dated '
                         'amendment) before any window plan or capture')
    return floor['min_overlap_px']


def plan(root, targets, *, offline=True, scenes=None, parcels=None, addresses=None, design=None, out=None, targets_sha256=None,
         extra_parcels=(), count_unreviewed=None):
    """Windows for frame-cut targets ({scene_id: [object_id]}): {'header', 'rows'}; written as JSONL when out is given.

    Every window is property-fitted (PROPERTY_FIT, the dated WINDOW.json amendment of 2026-10-08); a WINDOW.json without that record
    refuses the plan. extra_parcels: folders of frozen parcel payloads read beside the default ones (never written).
    count_unreviewed=True (not the registered rule; a counterfactual for reports only) lets unreviewed outlines size the frame and
    flags every row counterfactual_count_unreviewed."""
    root = Path(root)
    out = Path(out) if out is not None else None
    if out is not None and out.exists():
        raise ValueError(f'{out} exists: a plan file is never rewritten; choose another path')
    now = _now()
    design = _design(root, design)
    floor = _floor(design[0])
    rule = load_window_rule(root)
    fit_constants = property_fit_constants(rule[0])
    if fit_constants is None:
        raise ValueError(f'{WINDOW_PATH} carries no property-fit rule ({PROPERTY_FIT_FIELD}, dated amendment of 2026-10-08): windows '
                         'planned from now on are property-fitted, so a plan under the older WINDOW.json sha is refused '
                         '(window_rule_mismatch)')
    body_distance = design[0]['max_shift_m']
    by_id = {s['id']: s for s in (scenes or []) if isinstance(s, dict) and s.get('id')}
    sources, skipped, pending = {}, [], []
    total = 0
    for scene_id, object_ids in targets.items():
        scene = source_snapshot(root, scene_id, by_id)
        for object_id in object_ids:
            total += 1
            if scene is None:
                skipped.append({'scene_id': scene_id, 'object_id': object_id, 'reason': 'scene_missing'})
                continue
            obj = next((o for o in scene.get('objects', []) if o.get('id') == object_id), None)
            if obj is None:
                skipped.append({'scene_id': scene_id, 'object_id': object_id, 'reason': 'object_not_in_snapshot'})
                continue
            try:
                edges = touched_edges(obj['ring'], scene)
            except GEOMETRY_ERRORS:
                edges = {}
            if not edges:
                skipped.append({'scene_id': scene_id, 'object_id': object_id, 'reason': 'not_frame_cut'})
                continue
            geo = _geo(obj, scene)
            if geo is None:
                skipped.append({'scene_id': scene_id, 'object_id': object_id, 'reason': 'invalid_geometry'})
                continue
            sources[scene_id] = scene
            pending.append((scene, obj, edges, geo))
    boxes = [box(*pad_bounds(list(geo.bounds), WINDOW['index_pad_deg'])) for _, _, _, geo in pending]
    pool = ParcelPool(unary_union([box(*pad_bounds(list(geo.bounds), POOL_PAD_DEG)) for _, _, _, geo in pending]) if pending else None)
    if parcels is not None:
        pool.add(parcels, 'injected')
    index = RegisteredIndex(root, scenes).load(boxes, pool=None if parcels is not None else pool)
    if parcels is None:
        pool.load_disk(root, extra_parcels)
    fitter = PropertyFitter(root, index, pool, offline=offline, count_unreviewed=count_unreviewed)
    subjects = []
    for scene, obj, edges, _ in pending:
        subject = resolve_subject(root, scene, obj, edges, index, parcels=parcels, addresses=addresses, offline=offline,
                                  body_distance_m=body_distance, pool=pool)
        if subject is None:
            skipped.append({'scene_id': scene['id'], 'object_id': obj['id'], 'reason': 'invalid_geometry'})
            continue
        subjects.append(subject)
    def planned(members, why):
        row = window_row(root, members, why, sources, index, design, rule, now=now, fit=fitter)
        if len(members) > 1 and 'subject_exceeds_window' in row['flags']:
            # A chain of overlapping provisional windows can merge subjects no single 120 m window holds: fall back to
            # the groups that share a body or a lot (the design's 'split by body'); each smaller window is planned alone.
            parts = group_subjects(members, by=('shared_body', 'shared_lot'))
            if len(parts) > 1:
                out = []
                for part, part_why in parts:
                    sub = window_row(root, part, part_why, sources, index, design, rule, now=now, fit=fitter)
                    sub['group']['split_from'] = {'window_id': row['window_id'], 'size': len(members), 'reason': 'subject_exceeds_window'}
                    out.append(sub)
                return out
        return [row]

    groups = []
    for members, why in group_subjects(subjects):
        fresh = [m for m in members if not index.windows_of(m)]
        if not fresh or len(fresh) == len(members):
            groups.extend(planned(members, why))
            continue
        # A group mixing a subject a captured window already holds with a new target: the captured window keeps its frame
        # (never re-planned; reframing is a later card), so the windowed subject is skipped by name and the new targets are
        # re-grouped and planned on their own, before any frame is fitted (no frame or parcel read for the mixed group).
        held = [{'scene_id': m['scene_id'], 'object_id': m['object_id'], 'existing_windows': index.windows_of(m)}
                for m in members if index.windows_of(m)]
        for entry in held:
            skipped.append({**entry, 'reason': WINDOWED_REASON,
                            'planned_apart': [f"{m['scene_id']}/{m['object_id']}" for m in fresh]})
        for part, part_why in group_subjects(fresh):
            for row in planned(part, part_why):
                row['group']['split_from_windowed'] = {'size': len(members), 'reasons': why, 'windowed': held}
                groups.append(row)
    rows = dedupe_rows(groups)
    for row in rows:
        # a subject that already belongs to a captured window: that window keeps its frame and is never re-planned
        existing = sorted(set().union(*(index.windowed.get((s['scene_id'], s['object_id']), set()) for s in row['subjects'])))
        if existing:
            row['existing_windows'] = existing
            if row['dedupe'] == 'new':
                row['dedupe'] = 'exists_skip'
        if count_unreviewed:
            row['flags'].append('counterfactual_count_unreviewed')
    lat = sum(s['slice_geo'].centroid.y for s in subjects)/len(subjects) if subjects else None
    spans = sorted(r['window']['span_m'] for r in rows)

    def numbered_lots(row):
        return sum(lot['class'] == 'numbered' for lot in row['lots'])

    totals = {'targets': total, 'subjects': len(subjects), 'windows': len(rows),
              'captures_after_dedupe': sum(r['dedupe'] == 'new' for r in rows), 'exists_skip': sum(r['dedupe'] == 'exists_skip' for r in rows),
              'duplicate_in_plan': sum(r['dedupe'] == 'duplicate_in_plan' for r in rows),
              'lots_resolved': sum(bool(r['lots']) for r in rows), 'unresolved_lot': sum(not r['lots'] for r in rows),
              'multi_lot_windows': sum(numbered_lots(r) > 1 for r in rows),
              'multi_lot_outside_two_lot_rule': sum(numbered_lots(r) > 1 and not r['group']['two_lot_rule'] for r in rows),
              'plan_block_as_lot': sum(any(lot['class'] in EXCLUDED_CLASSES for lot in r['lots']) for r in rows),
              'plan_block_subjects': sum(s['reason'] == 'plan_block_only' for r in rows for s in r['subjects']),
              'kept_apart_subjects': sum(bool(s['kept_apart']) for r in rows for s in r['subjects']),
              'address_reserved_windows': sum(bool({'address_reserved_for_scene', 'address_reserved_for_source'} & set(r['flags'])) for r in rows),
              'below_design_overlap': sum(r['overlap_gate'] == 'below_design' for r in rows),
              'overlap_gate_ok': sum(r['overlap_gate'] == 'ok' for r in rows),
              'below_fraction_rule': sum(bool(r['overlap_gate_detail']) and not r['overlap_gate_detail']['passes_fraction_rule'] for r in rows),
              'passes_px_floor_not_fraction': sum(bool(r['overlap_gate_detail']) and r['overlap_gate_detail']['passes_px_floor']
                                                  and not r['overlap_gate_detail']['passes_fraction_rule'] for r in rows),
              'uncarried_targets': sum(len(s['object_ids']) for r in rows for s in r['sources'] if s['role'] == 'uncarried_source'),
              'frame_px': dict(sorted(Counter(str(r['window']['frame_px']) for r in rows).items())),
              'zoom': dict(sorted(Counter(str(r['window']['zoom']) for r in rows).items()))}
    try:
        load = [round(v, 2) for v in os.getloadavg()]
    except (OSError, AttributeError):
        load = None
    header = {'header': True, 'version': PLAN_VERSION, 'written_at': now, 'offline': offline, 'targets_sha256': targets_sha256,
              'design_sha256': design[1], 'design_path': str(DESIGN_PATH), 'window_sha256': rule[1], 'window_path': str(WINDOW_PATH),
              'window': {'version': VERSION, **{k: v for k, v in WINDOW.items()}, 'body_distance_m': body_distance,
                         'provider': PROVIDER, 'zoom': ZOOM, 'frame_px': list(FRAME_PX), 'latitude': lat,
                         'native_m_per_px_z20': scene_lot.native_m_per_px(lat, ZOOM) if lat is not None else None,
                         'native_span_512_m': native_span_m(lat, 512) if lat is not None else None,
                         'native_span_1024_m': native_span_m(lat, 1024) if lat is not None else None,
                         'z20_max_span_512_m': z20_max_span_m(lat, 512) if lat is not None else None,
                         'z20_max_span_1024_m': z20_max_span_m(lat, 1024) if lat is not None else None,
                         'identity': IDENTITY_RULE},
              'property_fit': {'field': PROPERTY_FIT_FIELD, 'constants': fit_constants, 'count_unreviewed': bool(count_unreviewed),
                               'parcel_pool': pool.summary(), 'parcel_reads': fitter.reads, 'extra_parcels': [str(d) for d in extra_parcels],
                               'review_layer_outlines_indexed': len(index.objects),
                               'reviewer_marked_outlines_indexed': sum(o['reviewed'] for o in index.objects),
                               'captured_window_subjects': len(index.windowed),
                               'totals': {'status': dict(sorted(Counter(r['window']['property_fit']['status'] for r in rows).items())),
                                          'frame_changed': sum(r['window']['property_fit'].get('recentred') is True
                                                               and 'kept_base' not in r['window']['property_fit'] for r in rows),
                                          'left_out_unreviewed': sum(r['window']['property_fit'].get('objects_left_out_unreviewed') or 0
                                                                     for r in rows),
                                          'would_differ_if_unreviewed_counted': sum(bool((r['window']['property_fit'].get('if_unreviewed_counted')
                                                                                          or {}).get('differs')) for r in rows),
                                          'parcels_open': sum('parcels_open' in r['flags'] for r in rows),
                                          'exists_skip_by_subject': sum(r['dedupe'] == 'exists_skip' and bool(r.get('existing_windows'))
                                                                        for r in rows),
                                          'windowed_subjects_skipped': sum(s['reason'] == WINDOWED_REASON for s in skipped),
                                          'rows_split_from_windowed': sum('split_from_windowed' in r['group'] for r in rows)}},
              'overlap_gate': {'rule': 'subject_recapture.min_overlap_px', 'min_overlap_px': floor,
                               'min_overlap_fraction': design[0]['min_overlap_fraction'], 'eroded_by_px': EDGE_PX,
                               'region': 'source footprint inside the planned frame, eroded by EDGE_PX; bounding-box side in target px'},
              'totals': totals,
              'spans_m': {'min': spans[0], 'median': round(statistics.median(spans), 3), 'max': spans[-1]} if spans else None,
              'rules': {'rule': dict(sorted(Counter(r['window']['rule'] for r in rows).items())),
                        'identity': dict(sorted(Counter(r['identity'] for r in rows).items())),
                        'flags': dict(sorted(Counter(f for r in rows for f in r['flags']).items())),
                        'registration': dict(sorted(Counter(s['registration'] for r in rows for s in r['sources']).items()))},
              'skipped': skipped, 'host_load': load, 'scan': {'registered_frames_indexed': len(index.frames), 'injected': scenes is not None},
              'wording': ('Every figure is read from the snapshots and parcels at write time; the predicted overlap is '
                          'scene_recapture.frame_transform on the planned frame, never a measurement; a body is model-to-model '
                          'agreement used only to size the window; nothing is captured, carried or counted by a plan.')}
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        text = '\n'.join(json.dumps(line, sort_keys=True) for line in (header, *rows))+'\n'
        temporary = out.with_name(out.name+'.tmp')
        temporary.write_text(text)
        temporary.replace(out)
    return {'header': header, 'rows': rows}


# --- capture ------------------------------------------------------------------------------------------------------

class CacheOnlyFetcher:
    """ImageryFetcher's _child over the tile cache only: a missing tile is None (capture_region then refuses), never a request."""

    def __init__(self, root, provider=PROVIDER):
        self.cache_dir = Path(root)/'data/imagery'/provider
        self.hits = 0
        self.misses = 0

    def _child(self, z, x, y):
        path = self.cache_dir/str(z)/f'{x}_{y}.jpg'
        if not path.exists():
            self.misses += 1
            return None
        self.hits += 1
        with Image.open(path) as cached:
            return cached.convert('RGB')


def _capture_acceptance(scene, row, source, measured):
    """Every slice (through the measured pixel_affine) and body (through the window's own frame) inside with the edge margin."""
    project = image_projection(scene)
    side, spacing = scene['image_width'], pixel_spacing(scene)
    margins = []
    objects = {o['id']: o for o in source.get('objects', [])}
    for subject in row['subjects']:
        if subject['scene_id'] == row['carry_from'] and subject['object_id'] in objects:
            ring = project_object(objects[subject['object_id']], measured)['ring']
            xs, ys = [p[0] for p in ring], [p[1] for p in ring]
            margins.append(min(min(xs), min(ys), side-max(xs), side-max(ys))*spacing)
        else:
            w, s, e, n = subject['slice_bounds']
            minx, miny, maxx, maxy = transform(project, box(w, s, e, n)).bounds
            margins.append(min(minx, miny, side-maxx, side-maxy)*spacing)
        for body in subject['bodies']:
            minx, miny, maxx, maxy = transform(project, box(*body['bounds'])).bounds
            margins.append(min(minx, miny, side-maxx, side-maxy)*spacing)
    margin = round(float(min(margins)), 2) if margins else None
    return {'inside_with_margin': margin is not None and margin >= WINDOW['edge_margin_m'], 'min_edge_margin_m': margin,
            'edge_margin_m': WINDOW['edge_margin_m'], 'method': 'slices through the measured pixel_affine; bodies through the window frame'}


def _windowed_subjects(root, row):
    """{'<scene_id>/<object_id>' (or 'row'): [window ids]} for every subject of the row a captured window already holds: the
    windows on disk now (captured_window_subjects) and the row's own existing_windows (the plan saw them), all subjects or some."""
    on_disk = captured_window_subjects(root)
    held = {}
    for subject in row.get('subjects') or ():
        windows = sorted(on_disk.get((subject.get('scene_id'), subject.get('object_id')), ()))
        if windows:
            held[f"{subject.get('scene_id')}/{subject.get('object_id')}"] = windows
    planned = sorted(set(row.get('existing_windows') or ()) - {w for ws in held.values() for w in ws})
    if planned:
        held['row.existing_windows'] = planned
    return held


def capture_window(root, row, *, fetcher=None, allow_unverified=False, scene_id=None):
    """Capture one planned window (tiles only, no detector) and record its measured frame_transform; writes only under
    data/scenes/<window id>/. scene_id overrides the id for a scratch rehearsal and must start with 'qa_'. A row with any subject a
    captured window already holds (the row's existing_windows, or a window on disk now) is refused, scratch or not, before any
    tile is fetched (subject_already_windowed): existing windows keep their frames."""
    from .scene_extract import save_scene
    root = Path(root)
    design = load_design(root)
    plan_design = design_sha_accepted(design, row.get('design_sha256'))
    if plan_design is None:
        raise ValueError(f"the plan row was written under DESIGN.json sha {str(row.get('design_sha256'))[:12]}, neither the current "
                         f'{design[1][:12]} nor behind a dated amendment with thresholds_changed false: re-plan before capturing '
                         '(carry_design_mismatch)')
    _, window_sha = load_window_rule(root)
    if row.get('window_sha256') != window_sha:
        raise ValueError(f"the plan row was written under WINDOW.json sha {str(row.get('window_sha256'))[:12]}, not the current "
                         f'{window_sha[:12]}: re-plan before capturing (window_rule_mismatch)')
    if scene_id is not None and not str(scene_id).startswith(SCRATCH_PREFIX):
        raise ValueError(f"a scratch capture id must start with '{SCRATCH_PREFIX}' (got {scene_id!r}); the planned id is {row['window_id']}")
    wid = validate_id(scene_id or row['window_id'])
    target = root/'data/scenes'/wid
    if target.exists():
        raise ValueError(f'{target} exists; a window is never overwritten (the batch driver skips it)')
    held = _windowed_subjects(root, row)
    if held:
        raise ValueError(f'{WINDOWED_REASON}: '+'; '.join(f"{k} -> {', '.join(v)}" for k, v in held.items())+'. A captured window '
                         'keeps its frame (reframing an existing window is a later card); re-plan so the new targets stand apart; '
                         'nothing captured')
    if 'subject_exceeds_window' in row.get('flags', []):
        raise ValueError('subject_exceeds_window: a slice or body would clip even at 1024 px / 120 m; nothing captured')
    if 'parcels_open' in row.get('flags', []):
        raise ValueError(str((row.get('window') or {}).get('property_fit', {}).get('reason') or 'parcels_open')+'; nothing captured')
    if 'counterfactual_count_unreviewed' in row.get('flags', []):
        raise ValueError('counterfactual_count_unreviewed: the row was planned with unreviewed outlines sizing the frame, not the '
                         'registered rule; nothing captured')
    floor = _floor(design[0])
    if row.get('overlap_gate') != 'ok' and not allow_unverified:
        detail = row.get('overlap_gate_detail') or {}
        side, fraction = detail.get('region_side_px'), detail.get('fraction')
        raise scene_lot.UnverifiedRecapture([f"overlap region side {side if side is not None else float('nan'):g} px < min_overlap_px {floor} "
                                             f"(fraction {fraction if fraction is not None else float('nan'):.3f}; predicted by the plan under "
                                             'DESIGN.json subject_recapture: not captured)'])
    source, source_png = carry_source(root, row['carry_from'])
    if source.get('model_signature') != row.get('source_model_signature'):
        raise ValueError(f"the source snapshot of {row['carry_from']} is {str(source.get('model_signature'))[:12]}, the plan row names "
                         f"{str(row.get('source_model_signature'))[:12]}: re-plan (carry_snapshot_mismatch)")
    window = row['window']
    scene, image, data = capture_region(window['requested_bounds'], PROVIDER, root=root, fetcher=fetcher, frame_px=window['frame_px'])
    measured = measured_transform(source, scene, source_png, data, design, role=SUBJECT_ROLE)
    if registration(source) == 'fitted':
        measured['source_displacement'] = displacement_note(root, scene['centroid']['lat'])
    scene.update(id=wid, address_id=row['address_id'] if row['identity'] == 'lot' else None, title=row['title'])
    check = _capture_acceptance(scene, row, source, measured)
    scene['window_frame'] = {
        'version': VERSION, 'planned_window_id': row['window_id'], 'identity': row['identity'], 'source_scene_id': row['carry_from'],
        'source_model_signature': source['model_signature'], 'source_image_sha256': source['image_sha256'],
        'subjects': [{'scene_id': s['scene_id'], 'object_id': s['object_id'], 'bodies': [{'scene_id': b['scene_id'], 'object_id': b['object_id']} for b in s['bodies']],
                      'lot': s['lot'], 'slice_parcel': s['slice_parcel'], 'reason': s['reason']} for s in row['subjects']],
        'window': {k: window[k] for k in ('rule', 'span_m', 'frame_px', 'zoom', 'm_per_px', 'centre_shift_m')},
        'property_fit': None if not isinstance(window.get('property_fit'), dict) else {
            k: window['property_fit'].get(k) for k in ('version', 'status', 'base', 'centre_shift_m', 'half_m', 'objects_counted',
                                                       'objects_left_out_unreviewed', 'development_plans', 'complete')},
        'lots': row['lots'], 'address_id': row['address_id'], 'parcels_sha256': row['parcels_sha256'], 'parcels_source': row['parcels_source'],
        'covered_by_existing': row['covered_by_existing'], 'flags': row['flags'],
        'uncarried_sources': [{'scene_id': s['scene_id'], 'object_ids': s['object_ids']} for s in row['sources'] if s['role'] == 'uncarried_source'],
        'plan_design_sha256': row['design_sha256'], 'plan_design_sha_match': plan_design, 'plan_window_sha256': row['window_sha256'],
        'planned_at': row.get('planned_at'), 'acceptance': check}
    if not check['inside_with_margin']:
        raise ValueError(f"subject_exceeds_window: the measured frame leaves a slice or body {check['min_edge_margin_m']} m from the edge "
                         f"(needs {WINDOW['edge_margin_m']} m); nothing captured")
    if not measured['verified'] and not allow_unverified:
        raise scene_lot.UnverifiedRecapture(verdict(measured, design[0])[1])
    scene['frame_transform'] = measured
    return save_scene(scene, data, root), image


# --- target carry and registry (design section 4 A; runbook step 7) ------------------------------------------------

REGISTRY_DIR = Path('data/reviews/eyes/recapture')
REGISTRY_GLOB = 'windows-*.jsonl'                 # r5_precision.REGISTRY_GLOB: the forward pointers the hop reads
REGISTRY_VERSION = 'windows-registry-v1'
CARRY_NOTE = ('Carried from {source} through the georeferenced frame as a frame-cut target: the earlier frame cut this outline at '
              'its edge; decide roof / not a roof on this image.')
DROPPED_FIELDS = ('provenance', 'fieldwork_import', 'replaces')   # the TS carry deletes the same three
CARRY_REFUSAL = 'Window not carried'
CARRY_BY = 'scripts.ml.scene_focus carry'                        # review.updated_by of a carry-written window review
# src/lib/scene-types.ts SceneGroup and the group checks of src/lib/scene-store.ts validateSceneReview: the only keys and
# shapes the review app's save accepts for a group record.
GROUP_FIELDS = ('id', 'label')
GROUP_ID = re.compile(r'^[a-zA-Z0-9_-]{1,100}$')
GROUP_LABEL_MAX = 150
REPAIR_ROLE = 'carry_repair'
REPAIR_REFUSAL = 'Window not repaired'
FORWARD_BY = 'scripts.ml.scene_focus carry-forward'              # review.updated_by of scripts/window-review-carry-forward.mjs
TAKEOVER_BY = 'scripts.ml.scene_focus takeover'
TAKEOVER_AUTHORS = (CARRY_BY, FORWARD_BY)                          # the only reviews a takeover may edit
TAKEOVER_ROLE = 'takeover'
TAKEOVER_REFUSAL = 'Window not taken over'


def registry_path(root, date=None):
    """The dated registry file for appends: data/reviews/eyes/recapture/windows-<UTC date>.jsonl."""
    return Path(root)/REGISTRY_DIR/f"windows-{date or datetime.now(UTC).strftime('%Y-%m-%d')}.jsonl"


def read_registry(root):
    """{(source_scene_id, object_id): [rows]} over every windows-*.jsonl in file then line order; a later row with withdrawn true
    removes the pointer to its window_scene_id (r5_precision.load_lineage reads the registry the same way)."""
    folder = Path(root)/REGISTRY_DIR
    registry = {}
    for path in (sorted(folder.glob(REGISTRY_GLOB)) if folder.is_dir() else ()):
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            key = (row.get('source_scene_id'), row.get('object_id'))
            if row.get('withdrawn'):
                registry[key] = [r for r in registry.get(key, []) if r.get('window_scene_id') != row.get('window_scene_id')]
            else:
                registry.setdefault(key, []).append(row)
    return registry


def decided_on_source(obj, revision):
    """True when the counter already classifies the source review object roof / not_roof (r5_precision.classify under the live
    RULES): the source decision wins and the frozen key is not carried (design section 4, status policy)."""
    from .training.sam3_mac.r5_precision import CLASSES, classify
    return classify(obj, revision)[0] in CLASSES


def _registry_row(key, window_id, carried_id, role, transform, design_sha, window_sha, stamp):
    return {'version': REGISTRY_VERSION, 'source_scene_id': key[0], 'object_id': key[1], 'window_scene_id': window_id,
            'carried_object_id': carried_id, 'role': role, 'frame_transform_verified': transform.get('verified') is True,
            'design_sha256': design_sha, 'window_sha256': window_sha, 'source_model_signature': transform.get('source_model_signature'),
            'source_image_sha256': transform.get('source_image_sha256'), 'written_at': stamp}


def _inside_frame(ring, side):
    return all(len(p) == 2 and 0 <= float(p[0]) <= side and 0 <= float(p[1]) <= side for p in ring)


def group_record(group):
    """The {id, label} the save validator accepts for a source group record, or None when the record would be refused."""
    if not isinstance(group, dict):
        return None
    gid, label = group.get('id'), group.get('label')
    if not isinstance(gid, str) or not GROUP_ID.match(gid) or not isinstance(label, str) or not label.strip() or len(label) > GROUP_LABEL_MAX:
        return None
    return {k: group[k] for k in GROUP_FIELDS}


def source_group_records(root, source_id, snapshot=None):
    """{group id: {id, label}} of the source scene's group records: the review.json groups first, then the carry snapshot's
    (the model the frame_transform names), then latest.json's; the first acceptable record of an id wins."""
    folder = Path(root)/'data/scenes'/validate_id(source_id)
    records = {}
    for layer in (_read_json(folder/'review.json'), snapshot, _read_json(folder/'latest.json')):
        if not isinstance(layer, dict):
            continue
        for group in layer.get('groups') or ():
            record = group_record(group)
            if record is not None:
                records.setdefault(record['id'], record)
    return records


def carry_groups(copies, available):
    """Resolve the carried copies' group_id and parent_id pointers so the window review saves in the app.

    Returns (groups, dropped): the group records the copies name, one per id in first-reference order, and the pointers
    dropped as [{id, field, value, reason}]. The copies are edited in place: a group_id no record defines (group_not_found)
    and a parent_id whose parent is not among the copies (parent_not_carried) are removed and recorded under
    carried_from.dropped_pointers; a carried parent's pointer takes the parent copy's id (its carried_from.object_id maps a
    renamed copy).
    """
    by_source = {c['carried_from']['object_id']: c['id'] for c in copies if isinstance(c.get('carried_from'), dict)}
    groups, dropped = {}, []
    for copy in copies:
        gid = copy.get('group_id')
        if gid:
            record = available.get(gid) if isinstance(gid, str) else None
            if record is None:
                dropped.append({'id': copy['id'], 'field': 'group_id', 'value': gid, 'reason': 'group_not_found'})
                copy.pop('group_id', None)
            else:
                groups.setdefault(gid, record)
        parent = copy.get('parent_id')
        if parent:
            carried = by_source.get(parent) if isinstance(parent, str) else None
            if carried is None or carried == copy['id']:
                dropped.append({'id': copy['id'], 'field': 'parent_id', 'value': parent, 'reason': 'parent_not_carried'})
                copy.pop('parent_id', None)
            else:
                copy['parent_id'] = carried
    for drop in dropped:
        copy = next(c for c in copies if c['id'] == drop['id'])
        copy.setdefault('carried_from', {}).setdefault('dropped_pointers', []).append({k: drop[k] for k in ('field', 'value', 'reason')})
    return list(groups.values()), dropped


def carry_window(root, window_id, *, registry=None, now=None):
    """Write the window's review.json (revision 1, carried frozen targets as needs_adjustment) and append its registry rows.

    Reads the window's latest.json (frame_transform, window_frame), the source snapshot the transform names (carry_source) and
    the source review for the status hint; writes ONLY data/scenes/<window>/review.json and appends to the registry file
    (default registry_path(root); None allowed for a qa_ scratch scene only). Every refusal is a ValueError naming its reason and
    nothing is written; an existing review.json returns {'skipped': 'review_exists'}.
    """
    root = Path(root)
    wid = validate_id(window_id)
    folder = root/'data/scenes'/wid
    latest = _read_json(folder/'latest.json')
    if not isinstance(latest, dict) or latest.get('id') != wid or not latest.get('model_signature'):
        raise ValueError(f'{CARRY_REFUSAL}: no saved window scene at data/scenes/{wid}/latest.json')
    if (folder/'review.json').exists():
        return {'window_scene_id': wid, 'skipped': 'review_exists', 'carried': [], 'uncarried': [], 'decided_on_source': [], 'rows': 0}
    if registry is None and not wid.startswith(SCRATCH_PREFIX):
        raise ValueError(f'{CARRY_REFUSAL}: a registry file is required for {wid} (only a {SCRATCH_PREFIX}* scratch scene carries without one)')
    transform = latest.get('frame_transform') if isinstance(latest.get('frame_transform'), dict) else {}
    frame = latest.get('window_frame') if isinstance(latest.get('window_frame'), dict) else {}
    values, design_sha = load_design(root)
    _, window_sha = load_window_rule(root)
    reasons = []
    if transform.get('verified') is not True:
        reasons.append('carry_unverified')
    design_match = design_sha_accepted((values, design_sha), (transform.get('design') or {}).get('design_sha256'))
    if design_match is None:
        reasons.append('carry_design_mismatch')
    if transform.get('method') != METHOD or transform.get('role') != SUBJECT_ROLE:
        reasons.append('carry_method')
    if frame.get('version') != VERSION or frame.get('source_scene_id') != transform.get('source_scene_id'):
        reasons.append('window_frame_mismatch')
    if frame.get('plan_window_sha256') != window_sha:
        reasons.append('window_rule_mismatch')
    if reasons:
        raise ValueError(f"{CARRY_REFUSAL}: {wid} ({', '.join(reasons)}); nothing written")
    source_id = transform['source_scene_id']
    snapshot, _ = carry_source(root, source_id)
    if snapshot.get('model_signature') != transform.get('source_model_signature') or snapshot.get('image_sha256') != transform.get('source_image_sha256'):
        raise ValueError(f'{CARRY_REFUSAL}: {wid} (carry_snapshot_mismatch: the source snapshot of {source_id} is '
                         f"{str(snapshot.get('model_signature'))[:12]}, the frame_transform names {str(transform.get('source_model_signature'))[:12]}); nothing written")
    source_review = _read_json(root/'data/scenes'/source_id/'review.json')
    if not isinstance(source_review, dict):
        source_review = {}
    review_objects = {o['id']: o for o in source_review.get('objects') or () if isinstance(o, dict) and 'id' in o}
    model_objects = {o['id']: o for o in snapshot.get('objects') or () if isinstance(o, dict) and 'id' in o}
    known = read_registry(root)
    taken = {o['id'] for o in latest.get('objects') or () if isinstance(o, dict) and 'id' in o}
    side = int(latest['image_width'])
    stamp = now or _now()
    copies, rows, decided, uncarried = [], [], [], []
    for subject in frame.get('subjects') or ():
        key = (subject['scene_id'], subject['object_id'])
        # Only a carry elsewhere makes a second window ambiguous (design section 5: copies, not records): an uncarried_source row
        # names a window the key was planned into but never carried onto (the other source scene's window), as r5_precision.registration
        # reads only carried_source rows.
        elsewhere = [r for r in known.get(key, []) if r.get('window_scene_id') != wid and r.get('role') == 'carried_source']
        if elsewhere:
            raise ValueError(f"{CARRY_REFUSAL}: {wid} (ambiguous_carry: {key[0]}/{key[1]} is already registered to "
                             f"{elsewhere[0].get('window_scene_id')}; a second window for one frozen key is refused); nothing written")
        if any(r.get('window_scene_id') == wid for r in known.get(key, [])):
            raise ValueError(f'{CARRY_REFUSAL}: {wid} ({key[0]}/{key[1]} already has a registry row naming this window while no review.json '
                             'exists: withdraw it before carrying again); nothing written')
        if key[0] != source_id:
            uncarried.append(key)
            rows.append(_registry_row(key, wid, None, 'uncarried_source', transform, design_sha, window_sha, stamp))
            continue
        model_obj = model_objects.get(key[1])
        if model_obj is None:
            raise ValueError(f"{CARRY_REFUSAL}: {wid} ({key[1]} is not an object of {source_id}'s snapshot {snapshot['model_signature'][:12]}); nothing written")
        source_obj = review_objects.get(key[1])
        if source_obj is not None and decided_on_source(source_obj, source_review.get('revision')):
            decided.append({'scene_id': key[0], 'object_id': key[1], 'status': source_obj.get('status'), 'reason': 'decided_on_source'})
            continue
        copy = project_object(model_obj, transform)
        if not _inside_frame(copy.get('ring') or (), side) or not all(_inside_frame(h, side) for h in copy.get('holes') or ()):
            raise ValueError(f'{CARRY_REFUSAL}: {wid} ({key[1]} projects outside the {side} px frame); nothing written')
        carried_id = key[1] if key[1] not in taken else f'{key[1]}-carried'
        if carried_id in taken:
            raise ValueError(f'{CARRY_REFUSAL}: {wid} ({carried_id} is already used by the window model); nothing written')
        taken.add(carried_id)
        for field in DROPPED_FIELDS:
            copy.pop(field, None)
        hint = source_obj if source_obj is not None else model_obj
        copy.update(id=carried_id, status='needs_adjustment', geometry_reviewed=False, notes=CARRY_NOTE.format(source=source_id),
                    model={**(copy.get('model') or {}), 'height_verified': False},
                    carried_from={'scene_id': source_id, 'model_signature': snapshot['model_signature'], 'object_id': key[1],
                                  'status': hint.get('status'), 'geometry_reviewed': bool(hint.get('geometry_reviewed')),
                                  'iou_with_new': 0, 'matched_id': None, 'method': METHOD, 'role': SUBJECT_ROLE})
        copies.append(copy)
        rows.append(_registry_row(key, wid, carried_id, 'carried_source', transform, design_sha, window_sha, stamp))
    if not copies:
        return {'window_scene_id': wid, 'skipped': 'nothing_to_carry', 'carried': [], 'uncarried': [list(k) for k in uncarried],
                'decided_on_source': decided, 'rows': 0}
    groups, dropped = carry_groups(copies, source_group_records(root, source_id, snapshot))
    review = {'revision': 1, 'model_signature': latest['model_signature'], 'objects': copies, 'groups': groups, 'reviewed_classes': [],
              'updated_at': stamp, 'updated_by': CARRY_BY}
    temporary = folder/'review.tmp'
    temporary.write_text(json.dumps(review, indent=2))
    temporary.replace(folder/'review.json')
    written = None
    if registry is not None:
        registry = Path(registry)
        registry.parent.mkdir(parents=True, exist_ok=True)
        with registry.open('a') as handle:
            handle.write(''.join(json.dumps(r, sort_keys=True)+'\n' for r in rows))
        written = str(registry)
    return {'window_scene_id': wid, 'review_path': str(folder/'review.json'), 'registry_path': written, 'revision': 1,
            'model_signature': latest['model_signature'], 'source_scene_id': source_id, 'source_model_signature': snapshot['model_signature'],
            'carried': [{'id': c['id'], 'source_object_id': c['carried_from']['object_id'], 'hint_status': c['carried_from']['status'],
                         'area_m2': (c.get('model') or {}).get('area_m2')} for c in copies],
            'renamed': [c['id'] for c in copies if c['id'] != c['carried_from']['object_id']],
            'groups': [g['id'] for g in groups], 'dropped_pointers': dropped,
            'uncarried': [list(k) for k in uncarried], 'decided_on_source': decided, 'rows': len(rows) if registry is not None else 0,
            'design_sha256': design_sha, 'design_sha_match': design_match, 'window_sha256': window_sha, 'written_at': stamp}


def repair_groups(root, window_id, *, registry=None, now=None):
    """Add the carried group records to an EXISTING window review.json that the carry wrote without them.

    Patches data/scenes/<window>/review.json ONLY while it is still revision 1 and updated_by is CARRY_BY (the reviewer has not
    saved): the copies' group_id / parent_id pointers are resolved as carry_groups does (records from the source review.json,
    the snapshot the frame_transform names, then latest.json), the missing records are appended to groups, updated_at is set
    to now, and nothing else changes (revision stays 1). The app's review.lock is held while writing and a locked scene is
    refused. One registry row {role carry_repair, window_scene_id, groups_added, at, previous_review_sha256,
    new_review_sha256} is appended (a registry is required unless the window is a qa_ scratch scene). A review with nothing
    to add returns {'skipped': 'nothing_to_repair'}; every refusal is a ValueError naming its reason and nothing is written.
    """
    root = Path(root)
    wid = validate_id(window_id)
    folder = root/'data/scenes'/wid
    path = folder/'review.json'
    if not path.exists():
        raise ValueError(f'{REPAIR_REFUSAL}: no review.json at data/scenes/{wid}/review.json')
    latest = _read_json(folder/'latest.json')
    if not isinstance(latest, dict) or latest.get('id') != wid or not latest.get('model_signature'):
        raise ValueError(f'{REPAIR_REFUSAL}: no saved window scene at data/scenes/{wid}/latest.json')
    if registry is None and not wid.startswith(SCRATCH_PREFIX):
        raise ValueError(f'{REPAIR_REFUSAL}: a registry file is required for {wid} (only a {SCRATCH_PREFIX}* scratch scene repairs without one)')
    transform = latest.get('frame_transform') if isinstance(latest.get('frame_transform'), dict) else {}
    source_id = transform.get('source_scene_id')
    if not source_id:
        raise ValueError(f'{REPAIR_REFUSAL}: {wid} has no frame_transform.source_scene_id; nothing written')
    lock = folder/'review.lock'
    try:
        lock.mkdir()
    except FileExistsError:
        raise ValueError(f'{REPAIR_REFUSAL}: {wid} is saving in the app (review.lock present); retry in a moment, nothing written') from None
    temporary = folder/'review.repair.tmp'
    try:
        before = path.read_bytes()
        review = json.loads(before)
        if not isinstance(review, dict) or review.get('revision') != 1:
            raise ValueError(f"{REPAIR_REFUSAL}: {wid} review.json is revision {(review or {}).get('revision') if isinstance(review, dict) else None}, "
                             'not the carry\'s revision 1 (the reviewer saved); nothing written')
        if review.get('updated_by') != CARRY_BY:
            raise ValueError(f"{REPAIR_REFUSAL}: {wid} review.json updated_by is {review.get('updated_by')!r}, not {CARRY_BY!r}; nothing written")
        snapshot = _read_json(Path(root)/'data/scenes'/validate_id(source_id)/'models'/f"{transform.get('source_model_signature')}.json")
        copies = [o for o in review.get('objects') or () if isinstance(o, dict) and isinstance(o.get('carried_from'), dict)]
        groups, dropped = carry_groups(copies, source_group_records(root, source_id, snapshot))
        present = {g.get('id') for g in review.get('groups') or () if isinstance(g, dict)}
        added = [g for g in groups if g['id'] not in present]
        if not added and not dropped:
            return {'window_scene_id': wid, 'skipped': 'nothing_to_repair', 'groups_added': [], 'dropped_pointers': [], 'rows': 0}
        stamp = now or _now()
        review['groups'] = list(review.get('groups') or []) + added
        review['updated_at'] = stamp
        temporary.write_text(json.dumps(review, indent=2))
        temporary.replace(path)
        after = path.read_bytes()
        row = {'version': REGISTRY_VERSION, 'role': REPAIR_ROLE, 'window_scene_id': wid, 'groups_added': [g['id'] for g in added], 'at': stamp,
               'previous_review_sha256': _sha(before), 'new_review_sha256': _sha(after)}
        if dropped:
            row['dropped_pointers'] = dropped
        written = None
        if registry is not None:
            registry = Path(registry)
            registry.parent.mkdir(parents=True, exist_ok=True)
            with registry.open('a') as handle:
                handle.write(json.dumps(row, sort_keys=True)+'\n')
            written = str(registry)
        return {'window_scene_id': wid, 'review_path': str(path), 'registry_path': written, 'revision': 1, 'updated_by': CARRY_BY,
                'updated_at': stamp, 'groups_added': row['groups_added'], 'dropped_pointers': dropped, 'previous_review_sha256': row['previous_review_sha256'],
                'new_review_sha256': row['new_review_sha256'], 'rows': 1 if registry is not None else 0}
    finally:
        temporary.unlink(missing_ok=True)
        lock.rmdir()


# --- takeover (2026-10-02) -------------------------------------------------------------------------------------------

class TakeoverRefused(ValueError):
    """A named takeover refusal (reason is a short token for tallies); nothing was written."""

    def __init__(self, reason, message):
        super().__init__(f'{TAKEOVER_REFUSAL}: {message}')
        self.reason = reason


def touching_frame(ring, width, height, tolerance=TAKEOVER['frame_tolerance_px']):
    """The frame edges a ring reaches within tolerance px of, or beyond, in EDGES order; [] when every vertex lies strictly inside
    the frame by more than tolerance (the takeover's inside-frame condition; unlike touched_edges a vertex beyond the frame counts)."""
    edges = set()
    for point in ring:
        x, y = float(point[0]), float(point[1])
        if x <= tolerance:
            edges.add('left')
        if x >= width-tolerance:
            edges.add('right')
        if y <= tolerance:
            edges.add('top')
        if y >= height-tolerance:
            edges.add('bottom')
    return [edge for edge in EDGES if edge in edges]


def _polygon(obj):
    """The shapely polygon of an object's ring and holes in frame pixels (an invalid ring is made valid; non-area parts dropped)."""
    ring = obj.get('ring') or []
    if len(ring) < 3:
        return Polygon()
    try:
        polygon = Polygon([(float(p[0]), float(p[1])) for p in ring],
                          [[(float(p[0]), float(p[1])) for p in hole] for hole in obj.get('holes') or () if len(hole) >= 3])
    except (TypeError, ValueError, IndexError):
        return Polygon()
    if not polygon.is_valid:
        polygon = make_valid(polygon)
        if polygon.geom_type not in ('Polygon', 'MultiPolygon'):
            polygon = unary_union([g for g in getattr(polygon, 'geoms', ()) if g.geom_type in ('Polygon', 'MultiPolygon')])
    return polygon


def takeover_plan(review, latest):
    """The takeover of one window, pure (TAKEOVER rule): the carried copies of `review` against the building detections of `latest`.

    Returns {copies, pairs [{copy_id, detection_id, coverage, frozen_key}], left_alone [{copy_id, reason, best, ...}], replaces
    {detection_id: sorted copy ids}, detections_with_several_slices}. Coverage is intersection area / copy area (rounded to 4
    decimals for the record, compared unrounded); the pairs follow the review's object order.
    """
    width, height = int(latest['image_width']), int(latest['image_height'])
    objects = [o for o in review.get('objects') or () if isinstance(o, dict) and 'id' in o]
    in_review = {o['id']: o for o in objects}
    detections = {o['id']: o for o in latest.get('objects') or () if isinstance(o, dict) and 'id' in o and o.get('kind') == 'building'}
    polygons = {did: _polygon(d) for did, d in detections.items()}
    copies = [o for o in objects if isinstance(o.get('carried_from'), dict)]
    pairs, left = [], []

    def leave(copy, reason, best=None, **detail):
        left.append({'copy_id': copy['id'], 'reason': reason, 'best': best, **detail})

    for copy in copies:
        if copy.get('status') == 'superseded':
            leave(copy, 'already_superseded')
            continue
        if copy.get('status') != 'needs_adjustment' or copy.get('geometry_reviewed'):
            leave(copy, 'copy_decided', status=copy.get('status'))
            continue
        polygon = _polygon(copy)
        if polygon.area <= 0:
            leave(copy, 'degenerate_copy')
            continue
        covers = sorted(((polygons[did].intersection(polygon).area/polygon.area, did) for did in detections), key=lambda c: (-c[0], c[1]))
        covers = [(cover, did) for cover, did in covers if cover > 0]
        if not covers:
            leave(copy, 'no_detection')
            continue
        cover, did = covers[0]
        best = {'detection_id': did, 'coverage': round(cover, 4)}
        if cover < TAKEOVER['cover_min']:
            leave(copy, 'below_cover', best)
            continue
        others = [{'detection_id': o, 'coverage': round(c, 4)} for c, o in covers[1:] if c >= TAKEOVER['other_cover_max']]
        if others:
            leave(copy, 'shared_cover', best, others=others)
            continue
        edges = touching_frame(detections[did].get('ring') or (), width, height)
        if edges:
            leave(copy, 'detection_touches_frame', best, edges=edges)
            continue
        detection = in_review.get(did)
        if detection is None:
            leave(copy, 'detection_not_in_review', best)
            continue
        if detection.get('status') != 'candidate':
            leave(copy, 'detection_not_candidate', best, status=detection.get('status'))
            continue
        if any(detection.get(key) for key in ('replaces', 'derived_from', 'replacement_ids', 'carried_from')):
            leave(copy, 'detection_has_lineage', best)
            continue
        frozen = copy['carried_from']
        pairs.append({'copy_id': copy['id'], 'detection_id': did, 'coverage': round(cover, 4),
                      'frozen_key': [frozen.get('scene_id'), frozen.get('object_id')]})
    grouped = {}
    for pair in pairs:
        grouped.setdefault(pair['detection_id'], []).append(pair['copy_id'])
    replaces = {did: sorted(ids) for did, ids in sorted(grouped.items())}
    return {'copies': len(copies), 'pairs': pairs, 'left_alone': left, 'replaces': replaces,
            'detections_with_several_slices': [did for did, ids in replaces.items() if len(ids) > 1]}


def _review_bytes(review):
    """The carry-forward writer's serialisation (JSON.stringify(review, null, 2): indent 2, non-ASCII kept, no trailing newline)."""
    return json.dumps(review, indent=2, ensure_ascii=False).encode()


def window_scenes(root):
    """Sorted ids of the frame-cut windows under data/scenes: lot-* / win-* whose latest.json carries window_frame of this version."""
    folder = Path(root)/'data/scenes'
    ids = []
    for path in sorted(folder.glob('*/latest.json')) if folder.is_dir() else ():
        if not path.parent.name.startswith(('lot-', 'win-')):
            continue
        latest = _read_json(path)
        if isinstance(latest, dict) and (latest.get('window_frame') or {}).get('version') == VERSION:
            ids.append(path.parent.name)
    return ids


def takeover_window(root, window_id, *, registry=None, write=False, now=None):
    """Plan (and with write, apply) the takeover of the carried copies of ONE window by its model's building detections.

    Dry run (write False): reads data/scenes/<window>/{review,latest}.json, checks the preconditions and returns the plan
    (takeover_plan plus serialisation_roundtrip, previous_review_sha256, written False); nothing is written and the lock is not
    taken. Write: under the app's review.lock (mkdir; a locked scene is refused), the review is re-read, re-checked and edited in
    place (C.status superseded, C.replacement_ids [D], D.replaces / D.derived_from the sorted copy ids, updated_at, updated_by
    TAKEOVER_BY), serialised exactly as the carry-forward wrote it (a file that does not round-trip is refused: a write would change
    more than the fields), temp + replace, and one registry row (role takeover) is appended; a registry is required unless the
    window is a qa_ scratch scene. Refusals (TakeoverRefused, a ValueError naming its reason) write nothing: no review.json, no
    window scene, revision not 1, updated_by not the carry's or the carry-forward's, review.model_signature != latest.json's, locked,
    no round-trip. A write with no pair returns {'skipped': 'nothing_to_take_over'} and writes nothing.
    """
    root = Path(root)
    wid = validate_id(window_id)
    folder = root/'data/scenes'/wid
    path = folder/'review.json'
    if not path.exists():
        raise TakeoverRefused('no_review', f'no review.json at data/scenes/{wid}/review.json')
    latest = _read_json(folder/'latest.json')
    if not isinstance(latest, dict) or latest.get('id') != wid or not latest.get('model_signature'):
        raise TakeoverRefused('no_window_scene', f'no saved window scene at data/scenes/{wid}/latest.json')
    if write and registry is None and not wid.startswith(SCRATCH_PREFIX):
        raise TakeoverRefused('registry_required', f'a registry file is required for {wid} (only a {SCRATCH_PREFIX}* scratch scene is taken over without one)')
    lock = folder/'review.lock'
    if write:
        try:
            lock.mkdir()
        except FileExistsError:
            raise TakeoverRefused('review_locked', f'{wid} is saving in the app (review.lock present); retry in a moment, nothing written') from None
    temporary = folder/'review.takeover.tmp'
    try:
        before = path.read_bytes()
        review = json.loads(before)
        if not isinstance(review, dict) or review.get('revision') != 1:
            raise TakeoverRefused('not_revision_1', f"{wid} review.json is revision {review.get('revision') if isinstance(review, dict) else None}, "
                                                    "not the carry's revision 1 (the reviewer saved); nothing written")
        if review.get('updated_by') not in TAKEOVER_AUTHORS:
            raise TakeoverRefused('not_carry_author', f"{wid} review.json updated_by is {review.get('updated_by')!r}, not {CARRY_BY!r} or "
                                                      f"{FORWARD_BY!r}; nothing written")
        if review.get('model_signature') != latest['model_signature']:
            raise TakeoverRefused('model_signature_mismatch', f"{wid} review.json model_signature {str(review.get('model_signature'))[:12]} is not "
                                                              f"latest.json's {latest['model_signature'][:12]}; nothing written")
        roundtrip = _review_bytes(review) == before
        plan = takeover_plan(review, latest)
        result = {'window_scene_id': wid, 'review_path': str(path), 'revision': 1, 'model_signature': latest['model_signature'],
                  'updated_by': review.get('updated_by'), 'serialisation_roundtrip': roundtrip, 'written': False,
                  'previous_review_sha256': _sha(before), **plan}
        if not write:
            return result
        if not plan['pairs']:
            return {**result, 'skipped': 'nothing_to_take_over', 'rows': 0}
        if not roundtrip:
            raise TakeoverRefused('serialisation_mismatch', f'{wid} review.json does not round-trip through the carry-forward serialisation '
                                                            '(indent 2, no ASCII escaping, no trailing newline): a write would change more than the '
                                                            'takeover fields; nothing written')
        stamp = now or _now()
        by_id = {o['id']: o for o in review['objects'] if isinstance(o, dict) and 'id' in o}
        for pair in plan['pairs']:
            copy = by_id[pair['copy_id']]
            copy['status'] = 'superseded'
            copy['replacement_ids'] = [pair['detection_id']]
        for did, ids in plan['replaces'].items():
            by_id[did]['replaces'] = list(ids)
            by_id[did]['derived_from'] = list(ids)
        review['updated_at'] = stamp
        review['updated_by'] = TAKEOVER_BY
        temporary.write_bytes(_review_bytes(review))
        temporary.replace(path)
        after = path.read_bytes()
        row = {'version': REGISTRY_VERSION, 'role': TAKEOVER_ROLE, 'window_scene_id': wid, 'model_signature': latest['model_signature'],
               'pairs': plan['pairs'], 'left_alone': plan['left_alone'], 'rule': dict(TAKEOVER), 'at': stamp,
               'previous_review_sha256': _sha(before), 'new_review_sha256': _sha(after)}
        written = None
        if registry is not None:
            registry = Path(registry)
            registry.parent.mkdir(parents=True, exist_ok=True)
            with registry.open('a') as handle:
                handle.write(json.dumps(row, sort_keys=True)+'\n')
            written = str(registry)
        return {**result, 'written': True, 'updated_by': TAKEOVER_BY, 'updated_at': stamp, 'registry_path': written,
                'new_review_sha256': row['new_review_sha256'], 'rows': 1 if registry is not None else 0}
    finally:
        temporary.unlink(missing_ok=True)
        if write:
            lock.rmdir()


def takeover_batch(root, window_ids, *, registry=None, write=False, now=None):
    """takeover_window over several windows with tallies: {mode, windows [result | {window_scene_id, refused, reason}], totals}."""
    windows, reasons, left_reasons = [], Counter(), Counter()
    totals = Counter()
    for wid in window_ids:
        try:
            result = takeover_window(root, wid, registry=registry, write=write, now=now)
        except ValueError as error:
            windows.append({'window_scene_id': wid, 'refused': str(error), 'reason': getattr(error, 'reason', 'refused')})
            reasons[getattr(error, 'reason', 'refused')] += 1
            continue
        windows.append(result)
        totals['planned'] += 1
        totals['copies'] += result['copies']
        totals['taken_over'] += len(result['pairs'])
        totals['left_alone'] += len(result['left_alone'])
        totals['detections_with_several_slices'] += len(result['detections_with_several_slices'])
        totals['windows_with_pairs'] += 1 if result['pairs'] else 0
        if write:
            totals['written'] += 1 if result.get('written') else 0
        for row in result['left_alone']:
            left_reasons[row['reason']] += 1
    summary = {'windows': len(window_ids), 'planned': totals['planned'], 'refused': sum(reasons.values()), 'copies': totals['copies'],
               'taken_over': totals['taken_over'], 'left_alone': totals['left_alone'], 'left_alone_by_reason': dict(sorted(left_reasons.items())),
               'detections_with_several_slices': totals['detections_with_several_slices'], 'refused_by_reason': dict(sorted(reasons.items())),
               'windows_with_pairs': totals['windows_with_pairs']}
    if write:
        summary['written'] = totals['written']
    return {'mode': 'write' if write else 'dry_run', 'rule': dict(TAKEOVER), 'windows': windows, 'totals': summary}


def write_takeover_amendment(root, *, expected_previous_sha256=None, now=None):
    """Pre-register the takeover rule as a dated WINDOW.json amendment (field takeover_rule; body untouched; appended once).

    Refuses (nothing written) a WINDOW.json whose sha256 is not `expected_previous_sha256` when given, one that does not round-trip
    through the register serialisation (indent 1, sorted keys, trailing newline), or one already carrying a takeover_rule entry.
    The entry records previous_sha256 (read at write time), thresholds_changed false, constants_changed false, the TAKEOVER
    constants, the rule and count texts, the reviewer direction verbatim and this module's sha256. Returns {path, sha256_before,
    sha256_after, at, amendment}. The RULES.json mirror is a separate dated entry (the applying stream's), not written here.
    """
    root = Path(root)
    path = root/WINDOW_PATH
    rule, before = load_window_rule(root)
    raw = path.read_bytes()
    if expected_previous_sha256 is not None and before != expected_previous_sha256:
        raise ValueError(f'{WINDOW_PATH} sha256 {before[:12]} is not the expected {str(expected_previous_sha256)[:12]}: the amendment names its '
                         'previous_sha256 at write time; nothing written')
    if (json.dumps(rule, indent=1, sort_keys=True)+'\n').encode() != raw:
        raise ValueError(f'{WINDOW_PATH} does not round-trip through the register serialisation (indent 1, sorted keys): nothing written')
    if any(isinstance(a, dict) and a.get('field') == 'takeover_rule' for a in rule.get('amendments') or ()):
        raise ValueError(f'{WINDOW_PATH} already carries the takeover_rule amendment: pre-registered once, never rewritten; nothing written')
    stamp = now or _now()
    entry = {'at': stamp, 'field': 'takeover_rule', 'previous_sha256': before, 'window_sha256_before': before,
             'thresholds_changed': False, 'constants_changed': False, 'takeover': dict(TAKEOVER),
             'change': TAKEOVER_RULE, 'count': TAKEOVER_COUNT, 'reason': TAKEOVER_DIRECTION,
             'code_sha256': {'scripts/ml/scene_focus.py': _sha(Path(__file__).read_bytes())},
             'rules_mirror': 'the same takeover and count text is to be appended to data/reviews/eyes/training/sam3-mac/round5/RULES.json as a '
                             'dated entry by the applying stream (not written by write_takeover_amendment)',
             'after': 'scripts.ml.scene_focus takeover --write (task focus:takeover CONFIRM=1) on the carry / carry-forward windows; one '
                      'registry row per window (role takeover); the body text of this file is the pre-registered text, unchanged, and the '
                      "module's window_rule() writes a takeover_rule record for any future dated file"}
    out = {**rule, 'amendments': [*(rule.get('amendments') or []), entry]}
    temporary = path.with_name('WINDOW.tmp')
    temporary.write_bytes((json.dumps(out, indent=1, sort_keys=True)+'\n').encode())
    temporary.replace(path)
    _, after = load_window_rule(root)
    return {'path': str(WINDOW_PATH), 'sha256_before': before, 'sha256_after': after, 'at': stamp, 'amendment': entry}


# --- CLI ----------------------------------------------------------------------------------------------------------

def load_targets(path):
    data = json.loads(Path(path).read_bytes())
    if not isinstance(data, dict) or not all(isinstance(v, list) for v in data.values()):
        raise ValueError('targets must be a JSON object {scene_id: [object_id, ...]}')
    return data, _sha(Path(path).read_bytes())


def read_plan(path):
    rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    return rows[0] if rows and rows[0].get('header') else None, [r for r in rows if not r.get('header')]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)
    g = sub.add_parser('register', help='Pre-register the window rule as data/reviews/eyes/recapture/WINDOW.json (a NEW file; refused when it exists).')
    g.add_argument('--root', default='.')
    p = sub.add_parser('plan', help='Plan subject windows for frame-cut targets (reads only; one bounded Vicmap read per unresolved window unless --offline).')
    p.add_argument('--root', default='.')
    p.add_argument('--targets', help='JSON {scene_id: [object_id, ...]} (e.g. /tmp/frame-cut-targets.json)')
    p.add_argument('--scene')
    p.add_argument('--object', action='append', default=[])
    p.add_argument('--offline', action='store_true', help='Frozen parcels only; never read Vicmap.')
    p.add_argument('--out', help='JSONL to write (refused when it exists).')
    p.add_argument('--extra-parcels', nargs='*', default=[], help='folders of frozen parcel payloads ({key, sha256, data}) read too')
    c = sub.add_parser('capture', help='Capture ONE planned window (tiles only, no detector); refuses a frame that fails DESIGN.json.')
    c.add_argument('--root', default='.')
    c.add_argument('--plan', required=True)
    c.add_argument('--window', required=True, help='window_id of the plan row to capture')
    c.add_argument('--allow-unverified', action='store_true', help='Keep a capture whose frame fails the design (verified false; never carried).')
    c.add_argument('--cache-only', action='store_true', help='Tiles from data/imagery only; a missing tile refuses the capture.')
    c.add_argument('--scratch-id', help='Rehearsal id (must start with qa_); the planned id is recorded as planned_window_id.')
    k = sub.add_parser('carry', help='Target carry onto ONE captured window: writes its review.json (revision 1, carried frozen targets as '
                                     'needs_adjustment) and appends the registry rows; refuses by name and writes nothing otherwise.')
    k.add_argument('--root', default='.')
    k.add_argument('--scene', required=True, help='the captured window scene id (lot-* / win-*; qa_* for a rehearsal)')
    k.add_argument('--registry', help="registry JSONL to append (default data/reviews/eyes/recapture/windows-<UTC date>.jsonl); "
                                     "'none' is allowed for a qa_ scratch scene only")
    k.add_argument('--repair-groups', action='store_true', help='patch an EXISTING window review.json (revision 1, written by this carry) '
                                                                 'with the carried group records its copies name; appends one carry_repair row')
    t = sub.add_parser('takeover', help='Takeover of carried slices by the whole-roof detection of the window\'s own model (TAKEOVER rule): '
                                        'dry run by default (plan per window, nothing written); --write edits ONLY the window review.json '
                                        '(revision 1 by the carry / carry-forward, under review.lock) and appends one registry row per window.')
    t.add_argument('--root', default='.')
    t.add_argument('--scene', action='append', default=[], help='a window scene id (repeatable)')
    t.add_argument('--all', action='store_true', help='every frame-cut window under data/scenes (lot-* / win-* with window_frame)')
    t.add_argument('--write', action='store_true', help='apply (default: dry run)')
    t.add_argument('--registry', help="registry JSONL to append (default data/reviews/eyes/recapture/windows-<UTC date>.jsonl); "
                                     "'none' is allowed for a qa_ scratch scene only")
    a = sub.add_parser('register-takeover', help='Pre-register the takeover rule as a dated WINDOW.json amendment (field takeover_rule; once).')
    a.add_argument('--root', default='.')
    a.add_argument('--expect-sha', help='refuse unless WINDOW.json currently hashes to this sha256')
    args = parser.parse_args(argv)
    root = Path(args.root)
    if args.command == 'register':
        path, sha = write_window_rule(root)
        json.dump({'written': str(WINDOW_PATH), 'sha256': sha, 'design_sha256': load_design(root)[1]}, sys.stdout, indent=1)
        sys.stdout.write('\n')
        return 0
    if args.command == 'plan':
        if args.targets:
            targets, digest = load_targets(args.targets)
        elif args.scene and args.object:
            targets, digest = {args.scene: list(args.object)}, None
        else:
            parser.error('plan needs --targets FILE or --scene ID with --object ID')
        result = plan(root, targets, offline=args.offline, out=args.out, targets_sha256=digest, extra_parcels=args.extra_parcels)
        json.dump({'header': result['header'], 'windows': [{k: r[k] for k in ('window_id', 'identity', 'dedupe', 'carry_from', 'overlap_gate',
                                                                              'overlap_gate_detail', 'flags')}
                                                           | {'targets': sum(len(s['object_ids']) for s in r['sources']), **r['window']} for r in result['rows']]},
                  sys.stdout, indent=1)
        sys.stdout.write('\n')
        return 0
    if args.command == 'carry':
        registry = None if args.registry == 'none' else Path(args.registry) if args.registry else registry_path(root)
        try:
            result = (repair_groups if args.repair_groups else carry_window)(root, args.scene, registry=registry)
        except ValueError as error:
            sys.stderr.write(f'{error}\n')
            return 1
        json.dump(result, sys.stdout, indent=1)
        sys.stdout.write('\n')
        return 0 if not result.get('skipped') else 3
    if args.command == 'takeover':
        if not args.scene and not args.all:
            parser.error('takeover needs --scene ID (repeatable) or --all')
        ids = window_scenes(root) if args.all else list(dict.fromkeys(args.scene))
        registry = None if args.registry == 'none' else Path(args.registry) if args.registry else registry_path(root)
        result = takeover_batch(root, ids, registry=registry, write=args.write)
        json.dump(result, sys.stdout, indent=1)
        sys.stdout.write('\n')
        return 0 if result['totals']['refused'] == 0 else 1
    if args.command == 'register-takeover':
        try:
            result = write_takeover_amendment(root, expected_previous_sha256=args.expect_sha)
        except ValueError as error:
            sys.stderr.write(f'{error}\n')
            return 1
        json.dump(result, sys.stdout, indent=1)
        sys.stdout.write('\n')
        return 0
    header, rows = read_plan(args.plan)
    row = next((r for r in rows if r['window_id'] == args.window), None)
    if row is None:
        sys.stderr.write(f'{args.window} is not a window of {args.plan}\n')
        return 2
    fetcher = CacheOnlyFetcher(root) if args.cache_only else None
    try:
        scene, _ = capture_window(root, row, fetcher=fetcher, allow_unverified=args.allow_unverified, scene_id=args.scratch_id)
    except scene_lot.UnverifiedRecapture as error:
        sys.stderr.write(f'{error}\n')
        return 1
    measured = scene.get('frame_transform') or {}
    if measured.get('verified') is not True:
        sys.stderr.write(f"saved {scene['id']} with a frame_transform that is not verified (--allow-unverified): nothing may be carried onto it\n"
                         + ''.join(f'reason: {reason}\n' for reason in verdict(measured, measured['design'])[1]))
    json.dump({'captured': scene['id'], 'image_sha256': scene['image_sha256'], 'window_frame': scene['window_frame'],
               'frame_transform': measured, 'cache': {'hits': fetcher.hits, 'misses': fetcher.misses} if fetcher else None}, sys.stdout, indent=1)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
