"""Compare local hip/gable hypotheses with RGB evidence, retaining uncertainty.

Colour/material continuity gates the evidence; a continuous brightness boundary
or ridge-cap highlight supports a fold. Uniform tile colour alone proves neither
shape. These scores are diagnostics, not calibrated probability or accuracy.
"""
from __future__ import annotations

import json
import math
import os
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union
from shapely.prepared import prep

from .roof_structure import convert_end_to_gable, gable_end_candidates, shared_edges


@lru_cache(maxsize=4)
def _trained_model(path, modified):
    model = json.loads(Path(path).read_text())
    if (model.get("validated") is not True or model.get("feature_names") != ["hip_support", "gable_support", "support_difference"]
            or len(model.get("weights", [])) != 4 or len(model.get("mean", [])) != 3 or len(model.get("scale", [])) != 3):
        return None
    if not np.isfinite([*model["weights"], *model["mean"], *model["scale"]]).all() or min(model["scale"]) <= 0:
        return None
    return model


def learned_choice(hip, gable):
    path = Path(os.environ.get("ROOF_FORM_MODEL", "scripts/ml/models/roof-form.json"))
    if not path.exists() or max(hip, gable) < .2:
        return None
    try:
        model = _trained_model(str(path), path.stat().st_mtime_ns)
        if model is None:
            return None
        features = (np.array([hip, gable, gable - hip]) - model["mean"]) / model["scale"]
        probability = 1 / (1 + np.exp(-np.clip(np.dot([1, *features], model["weights"]), -30, 30)))
        return "gable" if probability >= .8 else "hip" if probability <= .2 else "ambiguous"
    except (OSError, ValueError, TypeError):
        return None


def line_evidence(line, image, frame, footprint):
    start, end = np.asarray(line, dtype=float)
    length = float(np.linalg.norm(end - start))
    if length < .8:
        return {"score": 0.0, "samples": 0, "reason": "too-short"}
    tangent = (end - start) / length
    normal = np.array([-tangent[1], tangent[0]])
    rgb = cv2.GaussianBlur(np.asarray(image, dtype=np.uint8), (3, 3), .7)
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(float)
    samples = []
    for t in np.linspace(.12, .88, max(12, min(48, int(length * 5)))):
        center = start + t * (end - start)
        locations = [center + normal * offset for offset in (-.65, -.3, 0, .3, .65)]
        if not all(footprint.contains(Point(p)) for p in locations):
            continue
        pixels = [frame.metre_to_pixel(*p) for p in locations]
        if not all(1 <= x < lab.shape[1] - 1 and 1 <= y < lab.shape[0] - 1 for x, y in pixels):
            continue
        colors = np.array([lab[round(y), round(x)] for x, y in pixels])
        rgb_samples = np.array([rgb[round(y), round(x)] for x, y in pixels], dtype=float)
        if np.any((rgb_samples[:, 1] > rgb_samples[:, 0] * 1.08) & (rgb_samples[:, 1] > rgb_samples[:, 2] * 1.08)):
            continue  # Tree/grass colour is not evidence for a roof fold.
        if np.linalg.norm(colors[1, 1:] - colors[3, 1:]) > 16:
            continue  # Different materials/occlusion do not define roof planes.
        near = colors[1, 0] - colors[3, 0]
        far = colors[0, 0] - colors[4, 0]
        # A fold is a local step, not merely a broad illumination gradient.
        step = abs(near) if near * far > 0 and abs(near) >= .65 * abs(far) else 0
        cap = abs(colors[2, 0] - (colors[1, 0] + colors[3, 0]) / 2)
        samples.append((near, step, cap))
    if len(samples) < 6:
        return {"score": 0.0, "samples": len(samples), "reason": "insufficient-visible-roof"}
    values = np.asarray(samples)
    step_continuity = float(np.mean(values[:, 1] >= 5))
    sign_consistency = max(float(np.mean(values[:, 0] > 0)), float(np.mean(values[:, 0] < 0)))
    step_score = step_continuity * sign_consistency * min(1, float(np.median(values[:, 1])) / 16)
    cap_score = float(np.mean(values[:, 2] >= 6)) * min(1, float(np.median(values[:, 2])) / 16)
    return {"score": round(max(step_score, cap_score), 3), "samples": len(samples),
            "reason": "colour-and-line-continuity", "step_continuity": round(step_continuity, 3)}


def _new_lines(first, second):
    other = unary_union([LineString(line) for line in shared_edges(second)]).buffer(.04)
    lines = []
    for line in shared_edges(first):
        remaining = LineString(line).difference(other)
        pieces = [remaining] if remaining.geom_type == "LineString" else list(getattr(remaining, "geoms", []))
        lines.extend([list(p.coords[0]), list(p.coords[-1])] for p in pieces if p.geom_type == "LineString" and p.length >= .8)
    return lines


def roof_end_evidence(parts, footprint, eave, image, frame, *, evidence_footprint=None):
    """Score every end against one unchanged prior, without labels or weights.

    Training and inference share these observations. A previously selected end
    must not change a later feature, and installed weights cannot label their
    own training inputs. Geometry failures remain explicit, without fake zeros.
    """
    decisions = []
    for edge in gable_end_candidates(footprint):
        try:
            gable = convert_end_to_gable(parts, footprint, eave, edge)
        except ValueError as error:
            decisions.append({"edge_meters": edge, "reason": str(error)})
            continue
        hip_lines, gable_lines = _new_lines(parts, gable), _new_lines(gable, parts)

        def support(lines):
            evidence = [{"line_meters": line, **line_evidence(line, image, frame, footprint if evidence_footprint is None else evidence_footprint)} for line in lines]
            weight = sum(math.dist(*item["line_meters"]) for item in evidence if item["samples"] >= 6)
            score = sum(item["score"] * math.dist(*item["line_meters"]) for item in evidence if item["samples"] >= 6) / weight if weight else 0
            return round(score, 3), evidence

        hip_score, hip_evidence = support(hip_lines)
        gable_score, gable_evidence = support(gable_lines)
        decisions.append({"edge_meters": edge, "hip_support": hip_score, "gable_support": gable_score,
                          "hip_lines": hip_evidence, "gable_lines": gable_evidence})
    return decisions


def select_roof_ends(parts, footprint, eave, image, frame, record, *, evidence_footprint=None, prefer_simple=False, image_bounds=None):
    reviewed = record.get("boundary_source") == "crm" and record.get("roof_form_source") == "reviewer"
    force_gable = reviewed and record.get("roof_type") == "gabled"
    force_hip = reviewed and record.get("roof_type") == "hipped"
    decisions, converted_count = [], 0
    evidence_options = {} if evidence_footprint is None else {'evidence_footprint': evidence_footprint}
    for evidence in roof_end_evidence(parts, footprint, eave, image, frame, **evidence_options):
        if "hip_support" not in evidence:
            decisions.append({**evidence, "selected": "unchanged"})
            continue
        hip_score, gable_score = evidence["hip_support"], evidence["gable_support"]
        image_choice = "ambiguous"
        if gable_score >= .4 and gable_score - hip_score >= .2:
            image_choice = "gable"
        elif hip_score >= .4 and hip_score - gable_score >= .2:
            image_choice = "hip"
        trained_choice = learned_choice(hip_score, gable_score)
        if trained_choice is not None:
            image_choice = trained_choice
        # An ambiguous image must not automatically invent an end hip on a
        # long wing. A gable is the lower-complexity hypothesis there, provided
        # that neither the image nor the trained classifier prefers a hip.
        # This changes the inference prior only; image_choice stays ambiguous.
        edge = LineString(evidence['edge_meters'])
        # A support end almost entirely outside the capture is not an observed
        # roof end. Continue its side planes to the cut instead of introducing
        # a hip from texture along the tiny visible sliver. Raw scores remain
        # available, and this is explicitly a prior, not a gable observation.
        crop_continuation = (prefer_simple and not reviewed and image_bounds is not None
                             and edge.intersection(image_bounds).length < edge.length*.25)
        if crop_continuation:
            image_choice = 'ambiguous'
        exterior = list(footprint.exterior.coords)
        adjacent_lengths = [LineString([a, b]).length for a, b in zip(exterior[:-1], exterior[1:], strict=True)
                            if LineString([a, b]).distance(edge) < 1e-6 and LineString([a, b]).intersection(edge).length < .01]
        simple_gable = (prefer_simple and not reviewed and image_choice == 'ambiguous'
                        and gable_score >= hip_score-.05 and len(adjacent_lengths) == 2
                        and min(adjacent_lengths) >= edge.length*1.25)
        selected = "gable" if force_gable or (not force_hip and (image_choice == "gable" or simple_gable or crop_continuation)) else "hip"
        if selected == "gable":
            try:
                parts = convert_end_to_gable(parts, footprint, eave, evidence["edge_meters"])
            except ValueError as error:
                decisions.append({**evidence, "selected": "unchanged", "image_choice": image_choice, "reason": str(error)})
                continue
            converted_count += 1
        decisions.append({**evidence, "selected": selected,
                          "reason": "reviewer" if reviewed else "image-cut-continuation-prior" if crop_continuation else "image-evidence" if image_choice != "ambiguous" else "simple-gable-prior" if simple_gable else "insufficient-image-evidence",
                          "image_choice": image_choice,
                          "evidence_model": "trained-roof-end-classifier" if trained_choice is not None else "colour-line-heuristic"})
    return parts, {"source": "reviewer" if reviewed else "imagery-comparison", "gable_ends": converted_count,
                   "ambiguous_ends": sum(d.get("image_choice") == "ambiguous" for d in decisions),
                   "ends": decisions}




# --- Flat-roof hypothesis -------------------------------------------------
# Computed on the ORIGINAL RGB only (spec principle 5). Complementary cues share
# the same pixels; they are not independent observations. Positive rules select
# a flat hypothesis, with explicit starting surfaces/previews for unresolved cases.
# Scores and shares are model estimates, not accuracy or calibrated probability.
FLAT_VERSION = 'roof-form-flat-v3'
FLAT_RULES = {
    'erode_m': 1.0,                 # interior band excluded from texture and line features
    'min_line_m': 1.5,              # shortest LSD segment counted as an interior line
    'axis_tolerance_deg': 10,       # segment within this angle of an outline direction is axis-aligned
    'direction_min_share': .15,     # outline directions carrying at least this share of the simplified perimeter
    'direction_max': 3,
    'right_angle_tolerance_deg': 12,
    'corner_min_edge_m': 2.0,       # outline corners with both edges at least this long
    'blob_deviation': 35,           # |gray - median| above this starts an equipment/skylight blob
    'blob_max_m2': 12.0,            # blobs up to this area are equipment, not roof planes
    'crease_band_m': 1.0,           # same band and threshold as scene_roof_diagnostics.crease_contrast
    'crease_threshold_l': 6,
    'ridge': {'min_line_m': 2.5, 'step_l': 6, 'boundary_m': 2.0, 'offset_m': .75, 'centred': .8, 'run_share': .35},
    'plane_min_m2': 8.0,            # pitched-hypothesis faces measured for the plane brightness spread
    'min_visible_share': .9,        # share of the eroded interior that must lie inside the image; off-image roof is unseen evidence
    'blob_halo_m': .5,              # equipment blobs are removed with this halo so their edges and shadows do not count as texture
    # A building-class hint from scene_object_quality (land use, POIs, addresses, size; an agent hint, not a label) is
    # recorded with the evidence and may waive the gates named in 'waived_gates' for recognised hints on roofs at least
    # 'min_brightness' bright. The default waives nothing: on the 226 commercial-hint roofs of the Mooroolbark run a
    # 'planes' waiver flipped 15 roofs to flat and about half of them show ridges or hips on the saved image
    # (data/reviews/eyes/roof-eval/flat-v2/). Reviewers see the hint in form_evidence['context'] either way.
    # 'flat_hints' are structure sub-kinds (structure-kinds-v1) whose identity implies a flat top; they select the flat
    # template directly when the outline is measurable and no centred ridge run vetoes it.
    'context': {'hints': ['commercial'], 'waived_gates': [], 'min_brightness': 170, 'source': 'building-flags-v2',
                'flat_hints': ['shipping-container', 'water-tank'], 'flat_hints_source': 'structure-kinds-v1'},
    'large': {'area_m2': 250, 'outline_axis_share': .75, 'rectilinearity': .60, 'axis_share': .70,
              'contrast_share': .45, 'uniformity': 14, 'brightness': 150, 'plane_spread': 12},
    'bright': {'area_m2': 120, 'brightness': 170, 'uniformity': 10, 'diagonal_m': 3.0, 'contrast_share': .40,
               'plane_spread': 8},
    'confident_score': .8,
    'uncertain_score': .75,
}


def _flat_frame(meters_to_pixel):
    frame = np.asarray(meters_to_pixel, dtype=float)
    if frame.shape != (3, 2) or not np.isfinite(frame).all() or abs(np.linalg.det(frame[:2])) < 1e-10:
        raise ValueError('A registered metric image frame is required')
    return frame


def _flat_pixels(points, frame):
    points = np.asarray(points, dtype=float)
    return np.c_[points, np.ones(len(points))] @ frame


def _flat_mask(geometry, frame, shape):
    """Raster of a (multi)polygon in local metres on the image grid; exterior filled, holes cleared."""
    mask = np.zeros(shape, np.uint8)
    polygons = [geometry] if geometry.geom_type == 'Polygon' else list(getattr(geometry, 'geoms', []))
    for polygon in polygons:
        if polygon.is_empty or polygon.geom_type != 'Polygon':
            continue
        cv2.fillPoly(mask, [np.round(_flat_pixels(polygon.exterior.coords, frame)).astype(np.int32)], 1)
        for hole in polygon.interiors:
            cv2.fillPoly(mask, [np.round(_flat_pixels(hole.coords, frame)).astype(np.int32)], 0)
    return mask.astype(bool)


def _outline_rectangle(polygon):
    """Minimum rotated rectangle: area, longest side (metres) and its bearing (degrees mod 180); Nones when degenerate."""
    with np.errstate(divide='ignore', invalid='ignore'):   # GEOS oriented_envelope warns on axis-aligned rectangles
        rectangle = polygon.minimum_rotated_rectangle
    if rectangle.geom_type != 'Polygon' or rectangle.area <= 0:
        return None, None, None
    corners = np.asarray(rectangle.exterior.coords)[:4]
    sides = np.roll(corners, -1, axis=0) - corners
    longest = sides[int(np.argmax(np.hypot(sides[:, 0], sides[:, 1])))]
    return float(rectangle.area), float(np.hypot(*longest)), float(np.degrees(np.arctan2(longest[1], longest[0]))) % 180


def _angle_deviation(angle_deg, reference_deg, period=90):
    """Smallest angle (degrees) between two directions, modulo `period` (90: either axis of a frame)."""
    delta = (angle_deg - reference_deg) % period
    return float(min(delta, period - delta))


def _outline_directions(polygon, rules):
    """Dominant edge directions (degrees mod 90) of the simplified outline and the perimeter share they explain.

    Hips and valleys run diagonally to the eaves of their wing; parapets, plant and skylights follow
    them. Wings at different bearings each contribute a direction, up to `direction_max`.
    """
    ring = np.asarray(polygon.simplify(.35, preserve_topology=True).exterior.coords)
    vectors = ring[1:] - ring[:-1]
    lengths = np.hypot(vectors[:, 0], vectors[:, 1])
    keep = lengths > 1e-9
    vectors, lengths = vectors[keep], lengths[keep]
    if not len(lengths):
        return [], None
    angles = np.degrees(np.arctan2(vectors[:, 1], vectors[:, 0])) % 90
    total = float(lengths.sum())
    directions = []
    remaining = np.ones(len(angles), bool)
    for _ in range(rules['direction_max']):
        best, best_weight = None, 0.
        for centre in np.arange(0, 90, 2.5):
            near = remaining & (np.array([_angle_deviation(a, centre) for a in angles]) <= rules['axis_tolerance_deg'])
            weight = float(lengths[near].sum())
            if weight > best_weight:
                best, best_weight = near, weight
        if best is None or best_weight / total < rules['direction_min_share']:
            break
        # length-weighted circular mean of the selected edges, modulo 90 degrees
        radians = np.radians(angles[best] * 4)
        mean = float(np.degrees(np.arctan2((lengths[best] * np.sin(radians)).sum(), (lengths[best] * np.cos(radians)).sum())) / 4) % 90
        directions.append(round(mean, 2))
        remaining &= ~best
    aligned = [any(_angle_deviation(a, d) <= rules['axis_tolerance_deg'] for d in directions) for a in angles]
    return directions, round(float(lengths[aligned].sum() / total), 4)


def _right_angle_share(polygon, rules):
    simple = polygon.simplify(.35, preserve_topology=True)
    ring = np.asarray(simple.exterior.coords)[:-1]
    if len(ring) < 3:
        return None, 0
    right, counted = 0, 0
    for i in range(len(ring)):
        before, after = ring[i] - ring[i - 1], ring[(i + 1) % len(ring)] - ring[i]
        if min(np.linalg.norm(before), np.linalg.norm(after)) < rules['corner_min_edge_m']:
            continue
        cosine = float(before @ after / (np.linalg.norm(before) * np.linalg.norm(after)))
        turn = abs(np.degrees(np.arccos(np.clip(cosine, -1, 1))))
        counted += 1
        right += abs(turn - 90) <= rules['right_angle_tolerance_deg']
    return (round(right / counted, 4) if counted else None), counted


def _band_means(a, b, frame, lightness, offsets):
    """Mean L in bands either side of segment a-b at the given perpendicular offsets (metres); None when unseen."""
    length = float(np.linalg.norm(b - a))
    tangent = (b - a) / length
    normal = np.array([-tangent[1], tangent[0]])
    t = np.linspace(.1, .9, max(4, int(length * 3)))
    centre = a + t[:, None] * (b - a)
    means = []
    for sign in (1, -1):
        band = (centre[:, None, :] + sign * np.asarray(offsets)[None, :, None] * normal).reshape(-1, 2)
        pixels = np.floor(_flat_pixels(band, frame) + .5).astype(int)
        inside = (pixels[:, 0] >= 0) & (pixels[:, 0] < lightness.shape[1]) & (pixels[:, 1] >= 0) & (pixels[:, 1] < lightness.shape[0])
        values = lightness[pixels[inside, 1], pixels[inside, 0]]
        means.append(float(values.mean()) if len(values) >= 3 else None)
    return means


def _interior_lines(gray, lightness, polygon, eroded, frame, directions, rules):
    """LSD segments on the original gray inside the eroded outline, each with axis alignment, L step and boundary distance."""
    bounds = eroded.bounds
    corners = _flat_pixels([[bounds[0], bounds[1]], [bounds[2], bounds[3]], [bounds[0], bounds[3]], [bounds[2], bounds[1]]], frame)
    x0, y0 = np.maximum(np.floor(corners.min(axis=0)).astype(int) - 6, 0)
    x1 = min(int(np.ceil(corners[:, 0].max())) + 6, gray.shape[1])
    y1 = min(int(np.ceil(corners[:, 1].max())) + 6, gray.shape[0])
    if x1 - x0 < 8 or y1 - y0 < 8:
        return []
    detected = cv2.createLineSegmentDetector(cv2.LSD_REFINE_STD).detect(np.ascontiguousarray(gray[y0:y1, x0:x1]))[0]
    if detected is None:
        return []
    inverse = np.linalg.inv(frame[:2])
    prepared = prep(eroded)
    lines = []
    for raw in detected.reshape(-1, 2, 2):
        points = (raw + [x0, y0] - frame[2]) @ inverse
        if float(np.linalg.norm(points[1] - points[0])) < rules['min_line_m'] or not prepared.intersects(LineString(points)):
            continue
        # Keep the part inside the eroded outline: ridges and hips reach the eaves and would otherwise vanish.
        clipped = LineString(points).intersection(eroded)
        pieces = [clipped] if clipped.geom_type == 'LineString' else [g for g in getattr(clipped, 'geoms', []) if g.geom_type == 'LineString']
        if not pieces:
            continue
        piece = max(pieces, key=lambda g: g.length)
        points = np.asarray([piece.coords[0], piece.coords[-1]], dtype=float)
        length = float(np.linalg.norm(points[1] - points[0]))
        if length < rules['min_line_m']:
            continue
        angle = float(np.degrees(np.arctan2(*(points[1] - points[0])[::-1]))) % 180
        means = _band_means(points[0], points[1], frame, lightness, (.4, .7, 1.))
        lines.append({'points': points, 'length_m': length, 'angle_deg': angle,
                      'aligned': any(_angle_deviation(angle, d) <= rules['axis_tolerance_deg'] for d in directions),
                      'step_l': None if None in means else abs(means[0] - means[1]),
                      'boundary_m': float(polygon.exterior.distance(Point(points.mean(axis=0))))})
    return lines


def _ridge_run(lines, polygon, long_side, long_axis_deg, rules):
    """Longest chain of collinear interior lines along the outline's long axis with a brightness step, centred between the eaves.

    A gable ridge is one long fold along the roof, midway between two eaves whose planes differ in
    shading; a flat-roof level change or parapet is off-centre, across the roof or hugs the
    boundary. Returns the chain span as a share of the outline's long side and its median
    centredness (min/max eave distance).
    """
    ridge = rules['ridge']
    candidates = [ln for ln in lines if ln['aligned'] and ln['length_m'] >= ridge['min_line_m']
                  and _angle_deviation(ln['angle_deg'], long_axis_deg, 180) <= rules['axis_tolerance_deg']
                  and ln['step_l'] is not None and ln['step_l'] >= ridge['step_l'] and ln['boundary_m'] >= ridge['boundary_m']]
    best = {'span_m': 0.0, 'share': 0.0, 'centred': None, 'lines': 0}
    if not candidates or not long_side:
        return best
    for seed in candidates:
        theta = math.radians(seed['angle_deg'])
        along, across = np.array([math.cos(theta), math.sin(theta)]), np.array([-math.sin(theta), math.cos(theta)])
        offset = float(seed['points'].mean(axis=0) @ across)
        group = [ln for ln in candidates if _angle_deviation(ln['angle_deg'], seed['angle_deg'], 180) <= rules['axis_tolerance_deg']
                 and abs(float(ln['points'].mean(axis=0) @ across) - offset) <= ridge['offset_m']]
        projections = np.concatenate([ln['points'] @ along for ln in group])
        span = float(projections.max() - projections.min())
        if span <= best['span_m']:
            continue
        ratios = []
        for ln in group:
            middle = ln['points'].mean(axis=0)
            distances = []
            for sign in (1, -1):
                hit = LineString([middle, middle + sign * across * (long_side + 5)]).intersection(polygon.exterior)
                points = [hit] if hit.geom_type == 'Point' else [g for g in getattr(hit, 'geoms', []) if g.geom_type == 'Point']
                distances.append(min((Point(middle).distance(p) for p in points), default=None))
            if None not in distances and max(distances) > 0:
                ratios.append(min(distances) / max(distances))
        best = {'span_m': round(span, 2), 'share': round(span / long_side, 4),
                'centred': round(float(np.median(ratios)), 3) if ratios else None, 'lines': len(group)}
    return best


def _texture_samples(gray, mask, frame, rules):
    """Texture measurements plus the roof-material mask shared with plane medians."""
    values = gray[mask]
    if len(values) < 16:
        return None, None, None, np.zeros_like(mask, dtype=bool), np.zeros_like(mask, dtype=bool)
    median = float(np.median(values))
    scale = math.sqrt(abs(np.linalg.det(frame[:2])))
    deviating = (mask & (np.abs(gray.astype(float) - median) > rules['blob_deviation'])).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(deviating, connectivity=8)
    small = np.zeros(mask.shape, bool)
    blobs = 0
    for label in range(1, count):
        if stats[label, cv2.CC_STAT_AREA] <= rules['blob_max_m2'] * scale * scale:
            small |= labels == label
            blobs += 1
    halo = int(round(rules.get('blob_halo_m', 0) * scale))
    if blobs and halo > 0:
        small = cv2.dilate(small.astype(np.uint8), np.ones((2*halo+1, 2*halo+1), np.uint8)).astype(bool)
    material_mask = mask & ~small
    kept = gray[material_mask].astype(float)
    if len(kept) < 16:
        return median, None, blobs, material_mask, small
    low, high = np.percentile(kept, [10, 90])
    return median, round(float(high - low) / 2.563, 3), blobs, material_mask, small


def _texture(gray, mask, frame, rules):
    """Median gray, robust spread after equipment/halo exclusion, and blob count."""
    return _texture_samples(gray, mask, frame, rules)[:3]


def _plane_spread(parts, interior_mask, frame, lightness, polygon, rules):
    """Range of median L across the pitched hypothesis' larger faces: slopes facing different ways shade differently."""
    medians = []
    for part in parts:
        face = Polygon(part['coords'])
        if not face.is_valid or face.area < rules['plane_min_m2']:
            continue
        mask = _flat_mask(face, frame, lightness.shape) & interior_mask
        if mask.sum() >= 30:
            medians.append(float(np.median(lightness[mask])))
    if len(medians) < 2:
        return None, len(medians)
    return round(max(medians) - min(medians), 2), len(medians)


def _pitched_crease_share(parts, frame, lightness, rules, *, sample_mask, diagnostics):
    """Crease contrast from paired, visible roof pixels; never background or courtyard pixels."""
    scale = math.sqrt(abs(np.linalg.det(frame[:2])))
    steps = max(1, math.ceil(rules['crease_band_m'] * scale))
    across = (np.arange(steps) + .5) / steps * rules['crease_band_m']
    measured, total_pairs, valid_pairs, total_lines = [], 0, 0, 0
    for line in shared_edges(parts):
        a, b = (np.asarray(p, float) for p in line)
        length = float(np.linalg.norm(b - a))
        if length * scale < 1:
            continue
        total_lines += 1
        tangent = (b - a) / length
        normal = np.array([-tangent[1], tangent[0]])
        t = np.linspace(0, 1, max(2, math.ceil(length * scale) + 1))
        centre = a + t[:, None] * (b - a)
        samples, validity = [], []
        for sign in (1, -1):
            band = (centre[:, None, :] + sign * across[None, :, None] * normal).reshape(-1, 2)
            pixels = np.floor(_flat_pixels(band, frame) + .5).astype(int)
            inside = (pixels[:, 0] >= 0) & (pixels[:, 0] < lightness.shape[1]) & (pixels[:, 1] >= 0) & (pixels[:, 1] < lightness.shape[0])
            valid = inside.copy()
            valid[inside] &= sample_mask[pixels[inside, 1], pixels[inside, 0]]
            samples.append(pixels)
            validity.append(valid)
        paired = validity[0] & validity[1]
        total_pairs += len(paired)
        valid_pairs += int(paired.sum())
        if paired.sum() >= 3:
            means = [float(lightness[pixels[paired, 1], pixels[paired, 0]].mean()) for pixels in samples]
            measured.append(abs(means[0] - means[1]) >= rules['crease_threshold_l'])
    diagnostics.update(total_lines=total_lines, measured_lines=len(measured),
                       possible_sample_pairs=total_pairs, valid_roof_sample_pairs=valid_pairs)
    if not measured:
        return None, 0
    return round(sum(measured) / len(measured), 4), len(measured)


def _flat_reasons(features, decision, rule_name, rules, blockers, waived=()):
    """Sentence-case, denominator-carrying reasons for the decision."""
    f = features
    large, bright = rules['large'], rules['bright']
    reasons = []
    if decision == 'flat':
        count = len(f['outline_directions_deg'])
        reasons.append(f"Area {f['area_m2']:,.0f} m² fills {f['rectilinearity']:.2f} of its bounding rectangle; {f['outline_axis_share']:.2f} of the outline follows {count} dominant direction{'s' if count != 1 else ''}")
        if f['interior_line_axis_share'] is None:
            reasons.append('No interior lines of 1.5 m or more inside the 1 m eroded outline')
        else:
            reasons.append(f"Interior lines follow the outline directions ({f['interior_axis_line_count']} of {f['interior_line_count']} lines, {f['interior_line_axis_share']:.2f} by length); {f['diagonal_line_length_m']:.1f} m diagonal")
        spread = 'not measured' if f['pitched_plane_l_spread'] is None else f"{f['pitched_plane_l_spread']:.0f} L across {f['pitched_planes_measured']} planes"
        reasons.append(f"Pitched hypothesis creases with a brightness step: {round(f['pitched_crease_contrast_share'] * f['pitched_crease_lines_measured'])} of {f['pitched_crease_lines_measured']} ({f['pitched_crease_contrast_share']:.2f}); plane brightness spread {spread}")
        reasons.append(f"Median gray {f['brightness_median']:.0f}, robust spread {f['uniformity']:.1f} after removing {f['equipment_blob_count']} small blobs")
        reasons.append(f"No centred ridge run (longest {f['ridge_run_share']:.2f} of the long side)")
        if waived:
            reasons.append(f"Building-class hint '{f.get('context_hint')}' from {(rules.get('context') or {}).get('source')} waives the "
                           f"{' and '.join(waived)} gate; the ridge veto, crease contrast, line directions, outline and texture gates still hold")
        if rule_name == 'structure':
            reasons.append(f"Structure hint '{f.get('context_hint')}' from {(rules.get('context') or {}).get('flat_hints_source')}: this kind of structure has a flat top; "
                           "the outline gates were not required, the ridge veto still holds")
        reasons.append({'large': 'Large rectilinear rule', 'bright': 'Bright uniform rule', 'structure': 'Structure identity rule'}[rule_name])
        return reasons
    reasons.extend(blockers)
    area, brightness = f['area_m2'], f['brightness_median']
    if area < bright['area_m2']:
        reasons.append(f"Area {area:,.0f} m² is below the {bright['area_m2']} m² flat-roof gate")
    elif area < large['area_m2'] and (brightness is None or brightness < bright['brightness']):
        reasons.append(f"Area {area:,.0f} m² needs a bright roof (median gray {bright['brightness']} or more) below {large['area_m2']} m²")
    elif brightness is not None and brightness < large['brightness']:
        reasons.append(f"Median gray {brightness:.0f} is below the {large['brightness']} flat-roof gate")
    if f['ridge_run_share'] >= rules['ridge']['run_share'] and (f['ridge_run_centred'] or 0) >= rules['ridge']['centred']:
        reasons.append(f"A centred ridge run spans {f['ridge_run_share']:.2f} of the long side ({f['ridge_run_lines']} lines)")
    if f['outline_axis_share'] is not None and f['outline_axis_share'] < large['outline_axis_share']:
        reasons.append(f"Only {f['outline_axis_share']:.2f} of the outline follows its dominant directions")
    if f['interior_line_axis_share'] is not None and f['interior_line_axis_share'] < large['axis_share']:
        reasons.append(f"Diagonal interior lines: {f['diagonal_line_length_m']:.1f} m of {f['interior_line_length_m']:.1f} m")
    if f['pitched_crease_contrast_share'] is not None and f['pitched_crease_contrast_share'] > large['contrast_share']:
        reasons.append(f"Pitched hypothesis creases show brightness steps ({f['pitched_crease_contrast_share']:.2f})")
    if f['pitched_plane_l_spread'] is not None and f['pitched_plane_l_spread'] > large['plane_spread']:
        reasons.append(f"Pitched hypothesis planes differ in brightness (spread {f['pitched_plane_l_spread']:.0f} L)")
    if f['uniformity'] is not None and f['uniformity'] > large['uniformity']:
        reasons.append(f"Roof texture is not uniform (robust spread {f['uniformity']:.1f})")
    if not reasons:
        reasons.append('Flat-roof rules did not all agree')
    return reasons


def flat_evidence(polygon_m, image, meters_to_pixel, *, pitched_parts=None, rules=FLAT_RULES, context_hint=None):
    """Flat-roof evidence for one outline on the original RGB; decision 'flat' | 'pitched' | 'uncertain'.

    context_hint (default None: image-only): a building-class hint from scene_object_quality. Only hints listed in
    rules['context']['hints'] do anything, and then only by waiving the gates in rules['context']['waived_gates'];
    the outcome records the hint, whether it was recognised and which failed gates it waived.

    Features: outline area, rectilinearity and the perimeter share following its dominant edge
    directions; right-angle corner share; alignment of interior LSD lines with those directions
    (hips and valleys are diagonal; parapets, plant and skylights follow the outline); brightness
    and robust uniformity after removing small blobs; the pitched hypothesis' crease-contrast
    share and per-plane brightness spread; and the longest centred ridge run. 'flat' needs one of
    the conservative rule sets in FLAT_RULES with no ridge veto. 'uncertain' carries
    either an unresolved coplanar starting surface or a comparison-only alternative;
    it does not classify the physical roof as horizontal. Output is a versioned
    model estimate, not accuracy or a label.
    """
    frame = _flat_frame(meters_to_pixel)
    polygon = polygon_m if isinstance(polygon_m, Polygon) else Polygon(polygon_m)
    if not polygon.is_valid or polygon.is_empty:
        raise ValueError('flat_evidence requires a valid outline polygon in local metres')
    rgb = np.asarray(image.convert('RGB') if hasattr(image, 'convert') else image, dtype=np.uint8)
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError('flat_evidence requires an RGB image')
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    lightness = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)[:, :, 0].astype(float)
    features = {'area_m2': round(float(polygon.area), 2), 'rectilinearity': None, 'long_side_m': None,
                'outline_directions_deg': [], 'outline_axis_share': None, 'right_angle_share': None,
                'outline_corners_counted': 0, 'interior_line_axis_share': None, 'interior_line_count': 0,
                'interior_axis_line_count': 0, 'interior_line_length_m': 0.0, 'diagonal_line_length_m': 0.0,
                'ridge_run_m': 0.0, 'ridge_run_share': 0.0, 'ridge_run_centred': None, 'ridge_run_lines': 0,
                'coherent_diagonal_length_m': 0.0, 'coherent_diagonal_lines': 0,
                'brightness_median': None, 'uniformity': None, 'equipment_blob_count': None,
                'pitched_crease_contrast_share': None, 'pitched_crease_lines_measured': 0,
                'pitched_plane_l_spread': None, 'pitched_planes_measured': 0, 'interior_visible_share': None,
                'interior_sample_pixels': 0, 'material_sample_pixels': 0, 'pitched_crease_samples': {},
                'context_hint': context_hint}
    blockers = []
    rectangle_area, long_side, long_axis_deg = _outline_rectangle(polygon)
    if rectangle_area:
        features['rectilinearity'] = round(float(polygon.area / rectangle_area), 4)
        features['long_side_m'] = round(long_side, 2)
    directions, outline_share = _outline_directions(polygon, rules)
    features.update(outline_directions_deg=directions, outline_axis_share=outline_share)
    features['right_angle_share'], features['outline_corners_counted'] = _right_angle_share(polygon, rules)
    eroded = polygon.buffer(-rules['erode_m'])
    interior_mask, material_mask = None, None
    equipment_mask = np.zeros(gray.shape, dtype=bool)
    if eroded.is_empty or rectangle_area is None:
        blockers.append('Outline too thin for a 1 m interior band')
    elif not directions:
        blockers.append('No dominant outline direction carries 0.15 of the perimeter')
    else:
        lines = _interior_lines(gray, lightness, polygon, eroded, frame, directions, rules)
        total = sum(ln['length_m'] for ln in lines)
        axis_m = sum(ln['length_m'] for ln in lines if ln['aligned'])
        features.update(interior_line_count=len(lines), interior_axis_line_count=sum(ln['aligned'] for ln in lines),
                        interior_line_length_m=round(total, 3), diagonal_line_length_m=round(total - axis_m, 3))
        if total > 0:
            features['interior_line_axis_share'] = round(axis_m / total, 4)
        run = _ridge_run(lines, polygon, long_side, long_axis_deg, rules)
        features.update(ridge_run_m=run['span_m'], ridge_run_share=run['share'], ridge_run_centred=run['centred'], ridge_run_lines=run['lines'])
        diagonal = [line for line in lines if not line['aligned'] and line['length_m'] >= rules['ridge']['min_line_m']
                    and line['boundary_m'] >= rules['ridge']['boundary_m']
                    and line['step_l'] is not None and line['step_l'] >= rules['ridge']['step_l']]
        features.update(coherent_diagonal_length_m=round(sum(line['length_m'] for line in diagonal), 3),
                        coherent_diagonal_lines=len(diagonal))
        interior_mask = _flat_mask(eroded, frame, gray.shape)
        expected_px = float(eroded.area) * abs(float(np.linalg.det(frame[:2])))
        visible = round(min(1.0, float(interior_mask.sum()) / expected_px), 4) if expected_px > 0 else None
        features['interior_visible_share'] = visible
        median, spread, blobs, material_mask, equipment_mask = _texture_samples(gray, interior_mask, frame, rules)
        features.update(brightness_median=median, uniformity=spread, equipment_blob_count=blobs,
                        interior_sample_pixels=int(interior_mask.sum()), material_sample_pixels=int(material_mask.sum()))
        if median is None:
            blockers.append('Interior band has too few pixels inside the image')
        elif visible is None or visible < rules['min_visible_share']:
            # Pixels the image does not show are not evidence: a roof cut by the image edge keeps the pitched path.
            blockers.append(f"Only {visible or 0:.2f} of the 1 m interior lies inside the image ({rules['min_visible_share']:.2f} needed)")
    if pitched_parts:
        share, measured = _pitched_crease_share(pitched_parts, frame, lightness, rules,
                                               sample_mask=_flat_mask(polygon, frame, gray.shape) & ~equipment_mask,
                                               diagnostics=features['pitched_crease_samples'])
        features.update(pitched_crease_contrast_share=share, pitched_crease_lines_measured=measured)
        if material_mask is not None:
            spread, planes = _plane_spread(pitched_parts, material_mask, frame, lightness, polygon, rules)
            features.update(pitched_plane_l_spread=spread, pitched_planes_measured=planes)
    f = features
    measurable = not blockers and f['rectilinearity'] is not None and f['uniformity'] is not None and f['pitched_crease_contrast_share'] is not None
    ridge_veto = f['ridge_run_share'] >= rules['ridge']['run_share'] and (f['ridge_run_centred'] or 0) >= rules['ridge']['centred']
    large, bright = rules['large'], rules['bright']
    diagonal_veto = f['coherent_diagonal_length_m'] > bright['diagonal_m']
    fold_veto = ridge_veto or diagonal_veto
    axis_ok = f['interior_line_axis_share'] is None or f['interior_line_axis_share'] >= large['axis_share']
    plane_ok = f['pitched_plane_l_spread'] is None or f['pitched_plane_l_spread'] <= large['plane_spread']
    large_gates = {'area': f['area_m2'] >= large['area_m2'], 'rectilinearity': measurable and f['rectilinearity'] >= large['rectilinearity'],
                   'outline_axis_share': measurable and f['outline_axis_share'] >= large['outline_axis_share'], 'axis': axis_ok,
                   'contrast': measurable and f['pitched_crease_contrast_share'] <= large['contrast_share'],
                   'uniformity': measurable and f['uniformity'] <= large['uniformity'],
                   'brightness': measurable and f['brightness_median'] >= large['brightness'], 'planes': plane_ok}
    context = rules.get('context') or {}
    recognised = context_hint is not None and context_hint in (context.get('hints') or [])
    withheld = None
    if recognised and (f['brightness_median'] is None or f['brightness_median'] < context.get('min_brightness', 0)):
        withheld = f"median gray {f['brightness_median'] if f['brightness_median'] is None else round(f['brightness_median'])} is below the {context.get('min_brightness')} needed to waive gates"
    waived = [gate for gate in (context.get('waived_gates') or []) if recognised and not withheld and gate in large_gates and not large_gates[gate]]
    for gate in waived:
        large_gates[gate] = True
    bright_gates = {'area': f['area_m2'] >= bright['area_m2'],
                    'brightness': measurable and f['brightness_median'] >= bright['brightness'],
                    'uniformity': measurable and f['uniformity'] <= bright['uniformity'],
                    'diagonal_lines': f['diagonal_line_length_m'] <= bright['diagonal_m'],
                    'contrast': measurable and f['pitched_crease_contrast_share'] <= bright['contrast_share'],
                    'planes': f['pitched_plane_l_spread'] is None or f['pitched_plane_l_spread'] <= bright['plane_spread']}
    large_rule = measurable and not ridge_veto and all(large_gates.values())
    bright_rule = measurable and not ridge_veto and all(bright_gates.values())
    components = [
        np.clip((f['rectilinearity'] - .5) / .4, 0, 1) if f['rectilinearity'] is not None else 0,
        np.clip((f['outline_axis_share'] - .5) / .4, 0, 1) if f['outline_axis_share'] is not None else 0,
        (.7 if f['interior_line_axis_share'] is None else np.clip((f['interior_line_axis_share'] - .4) / .55, 0, 1)) if measurable else 0,
        np.clip((.6 - f['pitched_crease_contrast_share']) / .5, 0, 1) if f['pitched_crease_contrast_share'] is not None else 0,
        np.clip((30 - f['uniformity']) / 25, 0, 1) if f['uniformity'] is not None else 0,
        np.clip((20 - f['pitched_plane_l_spread']) / 16, 0, 1) if f['pitched_plane_l_spread'] is not None else .5,
        0 if ridge_veto else np.clip((.5 - f['ridge_run_share']) / .3, 0, 1),
    ]
    score = round(float(np.mean(components)), 3)
    structure_flat = (context_hint is not None and context_hint in (context.get('flat_hints') or []) and measurable and not ridge_veto)
    representation, preview = 'pitched_prior', False
    if large_rule or bright_rule:
        decision, rule_name = 'flat', 'large' if large_rule else 'bright'
        representation = 'flat_hypothesis'
    elif structure_flat:
        decision, rule_name = 'flat', 'structure'
        representation = 'flat_hypothesis'
    else:
        # The two positive rule sets overlap imperfectly. Passing every large
        # appearance gate while failing its area gate is not evidence of pitch.
        # Keep their thresholds, and distinguish an unresolved starting surface
        # from an area-only/undermeasured comparison that cannot select a family.
        failed = [name for name, ok in large_gates.items() if not ok and name != 'area']
        appearance_agrees = measurable and not fold_veto and not failed
        near_flat = (appearance_agrees and bright_gates['area'] and bright_gates['diagonal_lines']
                     and f['pitched_planes_measured'] >= 2 and score >= rules['uncertain_score'])
        informational = (measurable and not fold_veto and f['area_m2'] >= bright['area_m2'] and len(failed) == 1
                         and large_gates['brightness'] and score >= rules['uncertain_score'])
        preview = bool(appearance_agrees or informational)
        decision, rule_name = ('uncertain' if preview else 'pitched'), ('near-flat-appearance' if near_flat else None)
        if near_flat:
            representation = 'unresolved_coplanar'
            preview = False
    reasons = _flat_reasons(features, decision, rule_name, rules, blockers, waived)
    if diagonal_veto and decision != 'flat':
        reasons.append(f"Coherent diagonal brightness steps span {f['coherent_diagonal_length_m']:.1f} m; retain pitched alternatives")
    if representation == 'unresolved_coplanar':
        reasons.append('Large-roof appearance gates agree, but the flat rule sets do not. Use an unresolved coplanar starting surface; flat, shed and low-pitch forms remain indistinguishable')
    elif preview:
        reasons.append('Appearance supports comparing a level surface, but area or incomplete evidence prevents selecting that family')
    return {'version': FLAT_VERSION, 'decision': decision, 'score': score, 'rule': rule_name, 'features': features, 'reasons': reasons,
            'representation': representation, 'flat_low_slope_preview': preview,
            'gates': {'measurable': measurable, 'ridge_veto': bool(ridge_veto), 'diagonal_fold_veto': bool(diagonal_veto),
                      'diagonal_veto_scope': 'unresolved_family_branch_only',
                      'large': large_gates, 'bright': bright_gates},
            'context': {'class_hint': context_hint, 'recognised': recognised or bool(structure_flat), 'source': context.get('source'),
                        'structure_flat': bool(structure_flat),
                        'waivable_gates': list(context.get('waived_gates') or []), 'waived_failed_gates': waived, 'withheld': withheld},
            'rules': {'large': large, 'bright': bright, 'ridge': rules['ridge'], 'min_visible_share': rules['min_visible_share'],
                      'blob_halo_m': rules.get('blob_halo_m', 0), 'context': context},
            'note': 'Model estimate on the saved image, not accuracy or a label.'}
