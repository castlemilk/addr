"""Compare bounded construction-grid roof hypotheses with image evidence.

A captured silhouette is not a list of independently pitched eaves. Minor bends
may be tracing noise, occlusion cuts or bevels. A simpler support envelope can
explain them without inventing hips. Its surfaces are always clipped back to
exactly the supplied silhouette. This is an unconfirmed inference prior.
"""
from __future__ import annotations

import cv2
import numpy as np
from shapely.errors import GEOSException
from shapely.geometry import LineString, Point, Polygon, box
from shapely.geometry.polygon import orient
from shapely.ops import split, unary_union

from .roof_form import select_roof_ends
from .roof_junctions import construction_complexity, junction_candidates
from .roof_line_fit import image_segments, refine_roof_lines
from .roof_structure import _parts_from_faces, audit_roof, shared_edges, structural_prior
from .roof_wings import wing_candidates
from .rooflines import _classify_roofline

VERSION = 'roof-topology-v5'
# Image-support stage (26 September 2026). It runs after the v5 selection and can only
# replace the selected topology with a strictly simpler one whose creases the saved
# image supports at least as well; wherever it does not fire the v5 output is returned
# byte for byte and the report 'method' stays VERSION. Recorded in report['support'].
# v2 (26 September 2026): when no single rectangle covers the outline, a two-rectangle L or T
# fitted to the trace is offered as well (two_rectangle_supports); every gate is unchanged.
# v3 (26 September 2026): two-rectangle candidates are gated on optical validation against the
# current fan (`optical_support`: combined crease support, unsupported-crease count and how much
# of the observed roof-interior line length the hypothesis explains) instead of the fixed 0.5
# supported-share floor and the ranking margin. Single-rectangle candidates keep the v2 gates.
TOPOLOGY_SUPPORT_VERSION = 'roof-topology-support-v3'
SUPPORT_RULES = {
    # crease contrast: |mean L| step across 1 m bands either side of a crease, 8-bit LAB
    # (same band, threshold and sampling as scene_roof_diagnostics.crease_contrast), but
    # only roof pixels count so an eave-adjacent hip cannot borrow ground contrast.
    'crease_band_m': 1., 'crease_threshold_l': 6, 'min_band_samples': 3,
    # LSD-line support: an observed segment within 0.5 m / 10 deg covering the crease.
    'line_distance_m': .5, 'line_angle_deg': 10., 'line_overlap_m': 1., 'line_overlap_share': .5,
    # ranking penalty added to the v5 score (complexity + image mismatch)
    'share_weight': 1., 'length_weight': .03,
    # acceptance gates against the current best
    'residual_margin_m': .10, 'score_margin': .1, 'min_candidate_supported_share': .5,
    # minimal single-ridge candidate: rotated rectangle covering the outline
    'rectangle_coverage': .85, 'hole_area_m2': 8., 'hole_share': .02,
    # two-rectangle L/T candidate (only when the single rectangle does not qualify): the union
    # must cover >= 85% of the outline area with IoU >= .8, each rectangle >= 20% of the outline,
    # and the union boundary must stay within the Hausdorff deviation of the trace. Traced
    # corners are chamfered or rounded by 2-4 m on the reviewed L/T roofs, so the deviation limit
    # is loose; the single rectangle never gated on it (cbc33fd3's rectangle deviates 4.8 m).
    'two_rectangle_coverage': .85, 'two_rectangle_iou': .8, 'two_rectangle_deviation_m': 4.,
    'two_rectangle_min_share': .2, 'two_rectangle_snap_m': 1., 'two_rectangle_reflex_deg': 195.,
    'two_rectangle_candidates': 2,
    # v3 optical gates for two-rectangle candidates, all relative to the current fan: a crease is
    # optically supported by the brightness step OR an observed segment within 1 m / 10 deg covering
    # half of it; observed roof-interior segments (midpoint inside the outline eroded by 0.5 m) are
    # explained when within 1 m / 10 deg of a crease or outline edge. The candidate may lose at most
    # `optical_share_margin` of either share, may not add unsupported creases, and must have at least
    # one optically supported crease (a uniform image validates nothing).
    'optical_line_distance_m': 1., 'optical_line_angle_deg': 10., 'optical_line_overlap_share': .5,
    'optical_erosion_m': .5, 'optical_share_margin': .10, 'optical_min_supported_creases': 1,
}


def support_envelopes(footprint, *, image_bounds=None):
    """Extend groups of near-axis eaves; retain substantial concave recesses.

    No global bounding rectangle or courtyard filling. Proposals must cover the
    silhouette, add <=14% support area, and deviate <=1.5 m / 20% of its width.
    The support is never substituted for the saved/image boundary.
    """
    if not footprint.is_valid:
        return []
    # Small roof openings do not define separate level eaves. Fit the exterior
    # planes, then cut every opening back out exactly. Real courtyards retain
    # the courtyard-aware prior rather than borrowing this exterior hypothesis.
    holes = [Polygon(h) for h in footprint.interiors]
    if any(h.area > 8 for h in holes) or sum(h.area for h in holes) > footprint.area * .02:
        return []
    footprint = Polygon(footprint.exterior)
    ring = np.array(orient(footprint).exterior.coords[:-1])
    # Pixel steps split a long eave into many short diagonals. Estimate the
    # construction axes from a denoised copy, but bound against the exact ring.
    axis_ring = np.array(orient(footprint.simplify(.35, preserve_topology=True)).exterior.coords[:-1])
    edges = np.roll(axis_ring, -1, axis=0)-axis_ring
    lengths = np.linalg.norm(edges, axis=1)
    long = lengths >= 2.4
    if np.count_nonzero(long) < 3:
        return []
    vector = np.sum(lengths[long]*np.exp(4j*np.arctan2(edges[long, 1], edges[long, 0])))
    coherence = abs(vector)/sum(lengths[long])
    if coherence < .75:
        return []
    # Long eaves establish that a grid exists; all edge lengths contribute to
    # its orientation so one long, slightly skewed trace does not dominate.
    angle = np.angle(np.sum(lengths*np.exp(4j*np.arctan2(edges[:, 1], edges[:, 0]))))/4
    rotation = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
    local = ring @ rotation
    deviation_limit = min(1.5, float(np.ptp(local, axis=0).min())*.2)
    candidates, seen = [], set()
    for tolerance in (.25, .5, .75, 1.):
        simplified = Polygon(local).simplify(min(tolerance, deviation_limit), preserve_topology=True)
        vertices = np.array(simplified.exterior.coords[:-1])
        indices = [int(np.argmin(np.linalg.norm(local-p, axis=1))) for p in vertices]
        groups = []
        for i, a in enumerate(vertices):
            delta = vertices[(i+1) % len(vertices)]-a
            axis = int(abs(delta[1]) > abs(delta[0]))
            label = (axis, 1 if delta[axis] > 0 else -1)
            start, end = indices[i], indices[(i+1) % len(vertices)]
            samples = [local[k % len(local)] for k in range(start, end+1 if end > start else end+len(local)+1)]
            if groups and groups[-1][0] == label:
                groups[-1][1].extend(samples)
            else:
                groups.append([label, samples])
        if len(groups) > 1 and groups[-1][0] == groups[0][0]:
            groups[0][1].extend(groups.pop()[1])
        if len(groups) < 4 or (len(groups) >= len(ring) and not holes) or any(g[0][0] == groups[i-1][0][0] for i, g in enumerate(groups)):
            continue
        lines = []
        for (axis, sign), samples in groups:
            values = np.array(samples)[:, 1-axis]
            bound = min if (axis == 0 and sign > 0) or (axis == 1 and sign < 0) else max
            lines.append((1-axis, bound(values)))
        corners = []
        for i, (axis, value) in enumerate(lines):
            point = [0., 0.]
            point[axis] = value
            point[lines[i-1][0]] = lines[i-1][1]
            corners.append(point)
        support = Polygon(np.array(corners) @ rotation.T)
        if not support.is_valid or support.interiors or not support.buffer(1e-7).covers(footprint):
            continue
        # An image-edge cut is not evidence of a physical roof end. Support
        # may continue beyond the image; only its visible extent is compared.
        visible = support if image_bounds is None else support.intersection(image_bounds)
        if visible.geom_type != 'Polygon':
            continue
        deviation = visible.boundary.hausdorff_distance(footprint.boundary)
        expansion = visible.area/footprint.area-1
        identity = support.normalize().wkb
        if deviation > deviation_limit or expansion > .14 or identity in seen:
            continue
        seen.add(identity)
        candidates.append((support, {'support_corners': len(corners), 'max_support_deviation_m': round(deviation, 4),
                                     'support_area_increase_ratio': round(expansion, 4), 'axis_coherence': round(float(coherence), 4)}))
    return candidates


def surface_pieces(polygon):
    """Represent a plane with holes as simple coplanar pieces, without filling it.

    Split through openings instead of triangulating every pixel-scale exterior
    vertex. These are mesh storage seams, never ridge/hip/valley observations.
    """
    pending = [polygon]
    while pending:
        piece = pending.pop()
        if not piece.interiors:
            yield piece
            continue
        center = Polygon(piece.interiors[0]).representative_point()
        x0, _, x1, _ = piece.bounds
        children = list(split(piece, LineString([(x0-1, center.y), (x1+1, center.y)])).geoms)
        if len(children) < 2 or sum(len(p.interiors) for p in children) >= len(piece.interiors):
            raise ValueError('Could not preserve the opening in the roof surface')
        pending.extend(children)


def clip_support(parts, footprint, eave, roof_type='hipped'):
    faces = []
    for part in parts:
        clipped = Polygon(part['coords']).intersection(footprint)
        pieces = [clipped] if clipped.geom_type == 'Polygon' else list(getattr(clipped, 'geoms', []))
        for piece in pieces:
            if piece.geom_type != 'Polygon' or piece.area < 1e-8:
                continue
            for surface in surface_pieces(piece):
                xy = np.asarray(surface.exterior.coords[:-1])
                abc = np.array([part['plane'][key] for key in ('a', 'b', 'c')])
                faces.append((xy, np.c_[xy, np.ones(len(xy))] @ abc))
    if not faces:
        raise ValueError('No roof support surfaces')
    offset = min(float(min(z)) for _, z in faces)-eave
    result = _parts_from_faces([(xy, z-offset) for xy, z in faces], eave, roof_type)
    covered = unary_union([Polygon(p['coords']) for p in result])
    if covered.symmetric_difference(footprint).area > 1e-6:
        raise ValueError('Roof support changed the captured outline')
    return result


def topology_cost(parts, observations, *, support=None, rules=SUPPORT_RULES):
    """Complexity is a prior; segment mismatch is a separate image veto.

    Every observation is compared, including unmatched ones, so deleting an
    evidenced fold cannot look better merely by reducing the match denominator.
    Values are ranking costs, not measured accuracy or calibrated confidence.
    """
    lines = [np.asarray(line) for line in shared_edges(parts)]
    residuals, weights = [], []
    for segment in observations:
        choices = []
        for line in lines:
            vector = line[1]-line[0]
            length = np.linalg.norm(vector)
            if length < .1 or abs(vector/length @ segment['tangent']) < .94:
                continue
            points = segment['points']
            # Finite segment distances penalize wrong junction/end positions.
            distances = [LineString(line).distance(Point(point)) for point in points]
            choices.append(float(np.mean(distances)))
        residuals.append(min(1.5, min(choices, default=1.5)))
        weights.append(segment['length']*segment['support'])
    mismatch = float(np.average(residuals, weights=weights)) if weights else 0.
    small = sum(Polygon(p['coords']).area < .5 for p in parts)
    # Count construction folds and junctions, independent of mesh storage seams
    # and editor subdivisions. Equal face counts can have very different framing.
    complexity = construction_complexity(parts)
    score = mismatch+complexity['construction_complexity']
    cost = {'score': round(score, 5), 'image_line_mismatch_m': round(mismatch, 5),
            'faces': len(parts), 'small_faces': small, 'observed_segments': len(observations), **complexity}
    if support is not None:
        # Image-support term (roof-topology-support-v1). `score` is untouched so callers
        # that never pass `support` see the same numbers as before; ranking in the support
        # stage uses `supported_score`. Unsupported means neither a brightness step nor an
        # observed line: a shape prior, not an observation.
        penalty = rules['share_weight']*support['unsupported_share']+rules['length_weight']*support['unsupported_crease_length_m']
        cost.update(support_method=TOPOLOGY_SUPPORT_VERSION, creases=support['creases'],
                    crease_length_m=support['crease_length_m'],
                    unsupported_crease_length_m=support['unsupported_crease_length_m'],
                    unsupported_share=support['unsupported_share'], supported_share=support['supported_share'],
                    contrast_share=support['contrast_share'], line_supported_share=support['line_supported_share'],
                    support_penalty=round(penalty, 5), supported_score=round(score+penalty, 5))
    return cost



# --- roof-topology-support-v1: image support of creases ------------------------------------

def lab_lightness(image):
    """OpenCV 8-bit LAB L (0-255) of the saved RGB; a PIL image or an HxWx3 uint8 array."""
    rgb = np.asarray(image.convert('RGB') if hasattr(image, 'convert') else image, dtype=np.uint8)
    return cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2LAB)[:, :, 0].astype(float)


def _footprint_mask(footprint, frame, shape):
    """Roof pixels (1) of the exterior minus its openings, on the image grid."""
    mask = np.zeros(shape, np.uint8)
    for ring, value in ((footprint.exterior, 1), *((hole, 0) for hole in footprint.interiors)):
        pixels = np.c_[np.asarray(ring.coords), np.ones(len(ring.coords))] @ frame
        cv2.fillPoly(mask, [np.round(pixels).astype(np.int32)], int(value))
    # Rasterisation keeps boundary pixels whose centres lie outside; one pixel of
    # erosion keeps mixed roof/ground pixels out of every band.
    return cv2.erode(mask, np.ones((3, 3), np.uint8))


def _sample(array, xs, ys):
    """Nearest pixel-centre lookup; NaN outside the image (as scene_roof_diagnostics._sample)."""
    xs, ys = np.floor(np.asarray(xs, float)+.5), np.floor(np.asarray(ys, float)+.5)
    height, width = array.shape[:2]
    inside = (xs >= 0) & (xs < width) & (ys >= 0) & (ys < height)
    values = np.full(len(xs), np.nan)
    values[inside] = array[ys[inside].astype(int), xs[inside].astype(int)]
    return values


def _crease_contrast_l(line, frame, scale, lightness, mask, rules):
    """|mean L| difference between the roof pixels of the 1 m bands either side of a crease.

    Copied from scene_roof_diagnostics._crease_line (which cannot be imported here without an
    import cycle) with one difference: samples outside the roof mask are dropped, so a crease
    next to an eave is compared roof-to-roof, never roof-to-ground. None when a side has fewer
    than `min_band_samples` roof pixels.
    """
    a, b = (np.asarray(point, float) for point in line)
    length = float(np.linalg.norm(b-a))
    if length*scale < 1:
        return None
    band_px = max(1, int(np.ceil(rules['crease_band_m']*scale)))
    across = (np.arange(band_px)+.5)/band_px*rules['crease_band_m']
    tangent = (b-a)/length
    normal = np.array([-tangent[1], tangent[0]])
    t = np.linspace(0, 1, max(2, int(np.ceil(length*scale))+1))
    centre = a+t[:, None]*(b-a)
    means = []
    for sign in (1, -1):
        band = (centre[:, None, :]+sign*across[None, :, None]*normal).reshape(-1, 2)
        pixels = np.c_[band, np.ones(len(band))] @ frame
        values = _sample(lightness, pixels[:, 0], pixels[:, 1])
        on_roof = _sample(mask, pixels[:, 0], pixels[:, 1]) == 1
        values = values[on_roof & ~np.isnan(values)]
        if len(values) < rules['min_band_samples']:
            return None
        means.append(float(values.mean()))
    return round(abs(means[0]-means[1]), 2)


def _line_supported_length(line, observations, rules):
    """Length of the crease covered by observed segments within `line_distance_m` / `line_angle_deg`."""
    a, b = (np.asarray(point, float) for point in line)
    vector = b-a
    length = float(np.linalg.norm(vector))
    if length < 1e-9:
        return 0.
    tangent = vector/length
    normal = np.array([-tangent[1], tangent[0]])
    minimum_cosine = np.cos(np.deg2rad(rules['line_angle_deg']))
    covered = []
    for observation in observations:
        if abs(float(observation['tangent'] @ tangent)) < minimum_cosine:
            continue
        points = np.asarray(observation['points'], float)
        if np.max(np.abs((points-a) @ normal)) > rules['line_distance_m']:
            continue
        along = np.sort((points-a) @ tangent)
        lo, hi = max(0., along[0]), min(length, along[1])
        if hi > lo:
            covered.append((lo, hi))
    total, position = 0., -np.inf
    for lo, hi in sorted(covered):
        lo = max(lo, position)
        if hi > lo:
            total += hi-lo
            position = hi
    return float(total)


def crease_support(parts, footprint, observations, image, meters_to_pixel, *, rules=SUPPORT_RULES,
                   lightness=None, mask=None):
    """Which creases of a roof hypothesis the saved image supports.

    A crease is supported when the roof pixels either side of it differ by at least
    `crease_threshold_l` (8-bit LAB L) or an observed LSD segment within 0.5 m / 10 deg covers
    at least half of it (or 1 m). Shares are length-weighted over the folds emitted by
    `shared_edges`. These are image diagnostics for ranking, not accuracy or labels.
    """
    frame = np.asarray(meters_to_pixel, dtype=float)
    scale = float(np.sqrt(abs(np.linalg.det(frame[:2]))))
    if lightness is None:
        lightness = lab_lightness(image)
    if mask is None:
        mask = _footprint_mask(footprint, frame, lightness.shape[:2])
    per_crease, total, supported, contrast_length, line_length = [], 0., 0., 0., 0.
    for line in shared_edges(parts):
        length = float(np.linalg.norm(np.subtract(line[1], line[0])))
        delta = _crease_contrast_l(line, frame, scale, lightness, mask, rules)
        covered = _line_supported_length(line, observations, rules)
        by_contrast = delta is not None and delta >= rules['crease_threshold_l']
        by_line = covered >= min(rules['line_overlap_m'], rules['line_overlap_share']*length)
        per_crease.append({'kind': _classify_roofline(line, parts), 'length_m': round(length, 3), 'delta_l': delta,
                           'line_covered_m': round(covered, 3), 'supported': bool(by_contrast or by_line)})
        total += length
        supported += length if by_contrast or by_line else 0.
        contrast_length += length if by_contrast else 0.
        line_length += length if by_line else 0.
    unsupported = total-supported
    return {'method': TOPOLOGY_SUPPORT_VERSION, 'creases': len(per_crease), 'crease_length_m': round(total, 4),
            'supported_length_m': round(supported, 4), 'unsupported_crease_length_m': round(unsupported, 4),
            'unsupported_share': round(unsupported/total, 4) if total else 0.,
            'supported_share': round(supported/total, 4) if total else 1.,
            'contrast_share': round(contrast_length/total, 4) if total else None,
            'line_supported_share': round(line_length/total, 4) if total else None,
            'per_crease': per_crease}


def _segment_explained(segment, lines, distance_m, minimum_cosine):
    """Residual-style match: an observed segment is explained by a finite line of matching direction
    whose mean endpoint distance is within `distance_m`."""
    for line in lines:
        vector = line[1]-line[0]
        length = np.linalg.norm(vector)
        if length < .1 or abs(vector/length @ segment['tangent']) < minimum_cosine:
            continue
        finite = LineString(line)
        if float(np.mean([finite.distance(Point(point)) for point in segment['points']])) <= distance_m:
            return True
    return False


def optical_support(parts, footprint, observations, image, meters_to_pixel, *, rules=SUPPORT_RULES,
                    lightness=None, mask=None):
    """v3 optical validation of a roof hypothesis against the saved image.

    Per crease: optically supported when the brightness step passes (as `crease_support`) OR an
    observed LSD segment within `optical_line_distance_m` / `optical_line_angle_deg` covers at least
    `optical_line_overlap_share` of its length; `evidence` records which. `combined_supported_share`
    is length-weighted, `unsupported_creases` is a count. `explained_line_share` is the share of
    observed roof-interior segment length (midpoints inside the outline eroded by `optical_erosion_m`)
    within the same distance/angle of any crease or outline edge of the hypothesis (residual-style
    matching); 0 when there are no interior segments. Diagnostics for gating, not accuracy.
    """
    frame = np.asarray(meters_to_pixel, dtype=float)
    scale = float(np.sqrt(abs(np.linalg.det(frame[:2]))))
    if lightness is None:
        lightness = lab_lightness(image)
    if mask is None:
        mask = _footprint_mask(footprint, frame, lightness.shape[:2])
    line_rules = {**rules, 'line_distance_m': rules['optical_line_distance_m'], 'line_angle_deg': rules['optical_line_angle_deg']}
    creases = [np.asarray(line, float) for line in shared_edges(parts)]
    per_crease, total, supported_length, unsupported, supported = [], 0., 0., 0, 0
    for line in creases:
        length = float(np.linalg.norm(line[1]-line[0]))
        delta = _crease_contrast_l(line, frame, scale, lightness, mask, rules)
        covered = _line_supported_length(line, observations, line_rules)
        by_contrast = delta is not None and delta >= rules['crease_threshold_l']
        by_line = bool(length > 0 and covered >= rules['optical_line_overlap_share']*length)
        evidence = 'both' if by_contrast and by_line else 'contrast' if by_contrast else 'line' if by_line else None
        per_crease.append({'kind': _classify_roofline(line, parts), 'length_m': round(length, 3), 'delta_l': delta,
                           'line_covered_m': round(covered, 3), 'evidence': evidence, 'supported': evidence is not None})
        total += length
        if evidence is None:
            unsupported += 1
        else:
            supported += 1
            supported_length += length
    ring = np.asarray(footprint.exterior.coords, float)
    edges = [ring[i:i+2] for i in range(len(ring)-1)]
    interior = footprint.buffer(-rules['optical_erosion_m'])
    minimum_cosine = np.cos(np.deg2rad(rules['optical_line_angle_deg']))
    observed, explained = 0., 0.
    for segment in observations:
        midpoint = np.mean(np.asarray(segment['points'], float), axis=0)
        if interior.is_empty or not interior.covers(Point(midpoint)):
            continue
        observed += float(segment['length'])
        if _segment_explained(segment, creases+edges, rules['optical_line_distance_m'], minimum_cosine):
            explained += float(segment['length'])
    return {'method': TOPOLOGY_SUPPORT_VERSION, 'creases': len(per_crease), 'supported_creases': supported,
            'unsupported_creases': unsupported, 'combined_supported_share': round(float(supported_length/total), 4) if total else 1.,
            'interior_line_length_m': round(observed, 3), 'explained_line_length_m': round(explained, 3),
            'explained_line_share': round(explained/observed, 4) if observed else 0., 'per_crease': per_crease}


def minimal_supports(footprint, *, rules=SUPPORT_RULES):
    """A single rotated rectangle when it covers >= 85% of the outline area, otherwise up to
    two rectangles forming an L or a T (`two_rectangle_supports`).

    The straight skeleton of a traced 20-corner outline invents a hip or valley at every
    bend. When the minimum rotated rectangle explains the outline, one ridge along its long
    axis (ends decided by the existing end evidence) is offered as a hypothesis; the planes
    are clipped back to the exact outline. When it does not, an L/T union of two rectangles
    fitted to the trace is offered instead, so the skeleton has one ridge per wing and one
    valley at the junction. Real courtyards are excluded here (openings) and rely on
    roof_wings. Never a replacement for the saved boundary.
    """
    if not footprint.is_valid or footprint.is_empty:
        return []
    holes = [Polygon(h) for h in footprint.interiors]
    if any(h.area > rules['hole_area_m2'] for h in holes) or sum(h.area for h in holes) > footprint.area*rules['hole_share']:
        return []
    exterior = Polygon(footprint.exterior)
    rectangle = exterior.minimum_rotated_rectangle
    if rectangle.geom_type != 'Polygon' or rectangle.area <= 0 or len(rectangle.exterior.coords) != 5:
        return []
    coverage = exterior.area/rectangle.area
    if coverage < rules['rectangle_coverage']:
        return two_rectangle_supports(exterior, rules=rules)
    return [(rectangle, {'minimal_support': True, 'support_corners': 4, 'rectangle_coverage': round(float(coverage), 4),
                         'max_support_deviation_m': round(float(rectangle.boundary.hausdorff_distance(exterior.boundary)), 4),
                         'support_area_increase_ratio': round(float(rectangle.area/footprint.area-1), 4)})]


def _grid_angles(exterior):
    """Candidate construction-grid angles folded to (-45, 45] deg: the length-weighted four-fold
    mean of the denoised ring, the minimum rotated rectangle and the longest edge, deduplicated."""
    ring = np.array(orient(exterior.simplify(.35, preserve_topology=True)).exterior.coords[:-1])
    edges = np.roll(ring, -1, axis=0)-ring
    lengths = np.linalg.norm(edges, axis=1)
    angles = [np.angle(np.sum(lengths*np.exp(4j*np.arctan2(edges[:, 1], edges[:, 0]))))/4]
    rectangle = np.array(exterior.minimum_rotated_rectangle.exterior.coords[:-1])
    if len(rectangle) == 4:
        angles.append(np.arctan2(*(rectangle[1]-rectangle[0])[::-1]))
    angles.append(np.arctan2(*edges[int(np.argmax(lengths))][::-1]))
    unique = []
    for angle in angles:
        angle = (angle+np.pi/4) % (np.pi/2)-np.pi/4
        if not any(abs(angle-other) < np.radians(2) for other in unique):
            unique.append(float(angle))
    return unique


def _reflex_vertices(ring, threshold_deg):
    """Vertices of a counter-clockwise ring whose interior angle exceeds threshold_deg."""
    reflex = []
    for i in range(len(ring)):
        p, q, r = ring[i-1], ring[i], ring[(i+1) % len(ring)]
        cross = (q[0]-p[0])*(r[1]-q[1])-(q[1]-p[1])*(r[0]-q[0])
        dot = (q[0]-p[0])*(r[0]-q[0])+(q[1]-p[1])*(r[1]-q[1])
        if np.degrees(np.arctan2(cross, dot)) < 180.-threshold_deg:
            reflex.append(q)
    return reflex


def _fit_rectangle(piece):
    """The axis-aligned rectangle with the highest IoU against `piece`, its bounds chosen among the
    piece's vertex coordinates by coordinate descent from the bounding box (bounds: x0, y0, x1, y1)."""
    polygons = [piece] if piece.geom_type == 'Polygon' else list(piece.geoms)
    coords = np.vstack([np.asarray(p.exterior.coords) for p in polygons])
    values = [sorted(set(np.round(coords[:, 0], 2))), sorted(set(np.round(coords[:, 1], 2)))]
    bounds = list(piece.bounds)

    def iou(b):
        if b[2]-b[0] < 1. or b[3]-b[1] < 1.:
            return -1.
        rectangle = box(*b)
        inside = rectangle.intersection(piece).area
        return inside/(rectangle.area+piece.area-inside)
    best = iou(bounds)
    for _ in range(3):
        moved = False
        for k in range(4):
            for value in values[k % 2]:
                trial = list(bounds)
                trial[k] = value
                score = iou(trial)
                if score > best+1e-9:
                    best, bounds, moved = score, trial, True
        if not moved:
            break
    return bounds, best


def two_rectangle_supports(exterior, *, rules=SUPPORT_RULES):
    """Two rotated rectangles forming an L or a T that explain the outline; the support offered is
    their union offset outward (mitre) just far enough to cover the trace.

    For each candidate grid angle the outline is cut along the axis lines through its reflex
    corners; each side gets its best-IoU axis-aligned rectangle (`_fit_rectangle`). The narrower
    rectangle (stem) is extended to the wider one (bar); along the cut it must sit inside the bar,
    sharing one end within `two_rectangle_snap_m` (L, 6 corners) or neither (T, 8 corners). Gates:
    each rectangle >= `two_rectangle_min_share` of the outline area, the union covers
    >= `two_rectangle_coverage` of it with IoU >= `two_rectangle_iou`, and the union boundary is
    within `two_rectangle_deviation_m` (Hausdorff) of the trace. Offsetting all edges outward keeps
    the straight skeleton's ridges and valley where the fitted union puts them; the planes are
    clipped back to the exact outline as for every support. Candidates are ranked by IoU and
    capped at `two_rectangle_candidates`. Model estimate of a plan shape, not a survey.
    """
    exterior = Polygon(exterior.exterior)
    area = exterior.area
    if area <= 0:
        return []
    snap = rules['two_rectangle_snap_m']
    seen, found = [], []
    for angle in _grid_angles(exterior):
        rotation = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
        local = Polygon(np.array(exterior.exterior.coords[:-1]) @ rotation)
        ring = np.array(orient(local.simplify(.35, preserve_topology=True)).exterior.coords[:-1])
        x0, y0, x1, y1 = local.bounds
        for corner in _reflex_vertices(ring, rules['two_rectangle_reflex_deg']):
            for axis in (0, 1):
                cut = LineString([[corner[0], y0-1], [corner[0], y1+1]]) if axis == 0 else LineString([[x0-1, corner[1]], [x1+1, corner[1]]])
                try:
                    pieces = [g for g in split(local, cut).geoms if g.geom_type == 'Polygon' and g.area > 1e-6]
                except (ValueError, GEOSException):
                    continue
                sides = [[], []]
                for piece in pieces:
                    sides[int(piece.centroid.coords[0][axis] > corner[axis])].append(piece)
                if not sides[0] or not sides[1]:
                    continue
                fits = [_fit_rectangle(unary_union(side))[0] for side in sides]
                along = 1-axis
                bar, stem = (0, 1) if fits[0][along+2]-fits[0][along] >= fits[1][along+2]-fits[1][along] else (1, 0)
                b, s = list(fits[bar]), list(fits[stem])
                # The stem meets the bar's side facing it (stem on the + side of the cut meets the bar's max).
                s[axis if stem == 1 else axis+2] = b[axis+2] if stem == 1 else b[axis]
                if s[axis+2]-s[axis] < 1.:
                    continue
                if s[along] < b[along]-snap or s[along+2] > b[along+2]+snap:
                    continue  # the stem protrudes past the bar: an offset/Z plan, not an L or T
                low, high = abs(s[along]-b[along]) <= snap, abs(s[along+2]-b[along+2]) <= snap
                if low and high:
                    continue  # the same extent: one rectangle, handled by the single-rectangle path
                if low:
                    s[along] = b[along]
                if high:
                    s[along+2] = b[along+2]
                shape = 'L' if (low or high) else 'T'
                rectangles = [box(*b), box(*s)] if bar == 0 else [box(*s), box(*b)]
                if min(r.area for r in rectangles) < rules['two_rectangle_min_share']*area:
                    continue
                union = unary_union(rectangles)
                if union.geom_type != 'Polygon' or union.interiors:
                    continue
                union = union.simplify(1e-6)
                corners = len(union.exterior.coords)-1
                if corners != (6 if shape == 'L' else 8):
                    continue
                inside = union.intersection(local).area
                covered, iou = inside/area, inside/(union.area+area-inside)
                world = Polygon(np.array(union.exterior.coords[:-1]) @ rotation.T)
                deviation = float(world.boundary.hausdorff_distance(exterior.boundary))
                if covered < rules['two_rectangle_coverage'] or iou < rules['two_rectangle_iou'] or deviation > rules['two_rectangle_deviation_m']:
                    continue
                # Offset outward by the farthest trace point outside the union (sampled along the
                # outline) so the support covers the trace; mitre joins keep the 6/8 corners.
                boundary = exterior.exterior
                samples = [boundary.interpolate(d) for d in np.arange(0., boundary.length, .2)]
                offset = max([world.distance(q) for q in samples], default=0.)
                offset = 0. if offset <= 0 else offset+.02
                support = world.buffer(offset, join_style=2) if offset > 0 else world
                if not support.buffer(1e-7).covers(exterior):
                    offset += .1
                    support = world.buffer(offset, join_style=2)
                if (support.geom_type != 'Polygon' or support.interiors or not support.buffer(1e-7).covers(exterior)
                        or len(support.simplify(1e-6).exterior.coords)-1 != corners):
                    continue
                if any(world.symmetric_difference(other).area < max(.5, .01*area) for other in seen):
                    continue  # the same plan from another grid angle or reflex corner
                seen.append(world)
                found.append((support, {'minimal_support': True, 'support_corners': corners, 'shape': shape,
                                        'rectangles': [np.round(np.array(r.exterior.coords[:-1]) @ rotation.T, 4).tolist() for r in rectangles],
                                        'rectangle_coverage': round(float(covered), 4), 'union_iou': round(float(iou), 4),
                                        'support_offset_m': round(float(offset), 4), 'max_support_deviation_m': round(deviation, 4),
                                        'support_area_increase_ratio': round(float(support.area/area-1), 4),
                                        'grid_angle_deg': round(float(np.degrees(angle)), 2)}))
    found.sort(key=lambda row: (-row[1]['union_iou'], row[1]['max_support_deviation_m']))
    return found[:rules['two_rectangle_candidates']]


def select_roof_topology(parts, footprint, eave, rise, image, meters_to_pixel, *, prefer_simple=False, evidence_views=None,
                         candidate_pool=None):
    """evidence_views (default None: unchanged) are extra LSD views forwarded to image_segments and
    refine_roof_lines; their lines still pass the original-RGB gate (see roof_line_fit.image_segments)."""
    view_kwargs = {} if evidence_views is None else {'extra_views': evidence_views}
    observations = image_segments(image, footprint, meters_to_pixel, **view_kwargs)
    base_cost = topology_cost(parts, observations)
    report = {'method': VERSION, 'accepted': False, 'reason': 'retain-current-topology',
              'before': base_cost, 'candidates': []}
    if evidence_views is not None:
        from .roof_line_fit import EXTRA_VIEWS_VERSION, validate_extra_views, view_origin_counts
        report['evidence_views'] = {'names': validate_extra_views(evidence_views, image), 'method': EXTRA_VIEWS_VERSION,
                                    'view_origins': view_origin_counts(observations)}
    chosen, chosen_cost, viable = parts, base_cost, []
    frame = np.asarray(meters_to_pixel)
    inverse = np.linalg.inv(np.c_[frame, [0, 0, 1]])
    width, height = image.size
    image_bounds = Polygon(np.array([[0, 0, 1], [width, 0, 1], [width, height, 1], [0, height, 1]]) @ inverse[:, :2])
    supports = support_envelopes(footprint, image_bounds=image_bounds)
    # Already-straight L/T roofs also benefit from joining uneven-wing ridges;
    # they do not need a new construction envelope to enter the comparison.
    if not footprint.interiors and len(footprint.exterior.coords) <= 25:
        supports.insert(0, (footprint, {'support_corners': len(footprint.exterior.coords)-1,
                                     'max_support_deviation_m': 0., 'support_area_increase_ratio': 0.,
                                     'unchanged_support': True}))
    for support, detail in supports:
        try:
            support_parts = structural_prior(support, eave, eave+rise, regularize_eaves=False)

            class Frame:
                def metre_to_pixel(self, x, y):
                    return np.array([x, y, 1.]) @ np.asarray(meters_to_pixel)

            support_parts, ends = select_roof_ends(support_parts, support, eave, image, Frame(), {}, evidence_footprint=footprint, prefer_simple=prefer_simple, image_bounds=image_bounds)
            families = {end.get('selected') for end in ends['ends']}
            family = 'mixed' if {'gable', 'hip'} <= families else 'gabled' if 'gable' in families else 'hipped'
            alternatives = junction_candidates(support_parts, support, eave)
            if not detail.get('unchanged_support'):
                alternatives.insert(0, (support_parts, None))
            alternatives = [(p, junctions, family, ends, None) for p, junctions in alternatives]
            if prefer_simple:
                evidenced_gables = [end['edge_meters'] for end in ends['ends'] if end.get('image_choice') == 'gable']
                evidenced_hips = [end['edge_meters'] for end in ends['ends'] if end.get('image_choice') == 'hip']
                for p, wing in wing_candidates(support, eave, rise):
                    ridges = [LineString(line) for line in shared_edges(p) if _classify_roofline(line, p) == 'ridge']
                    if any(not any(ridge.distance(LineString(end)) < .05 for ridge in ridges) for end in evidenced_gables):
                        continue  # A simpler wing must retain a clearly evidenced end ridge.
                    if any(any(ridge.distance(LineString(end)) < .05 for ridge in ridges) for end in evidenced_hips):
                        continue  # Do not extend a gable through a clearly supported hip face.
                    audit = audit_roof(p, support, eave)
                    if audit['passed'] and audit.get('drainage', {}).get('passed'):
                        alternatives.append((p, None, 'mixed' if wing['hip_ends'] else 'gabled',
                                             {'source': 'construction-prior', 'ends': []}, wing))
            for alternative, junctions, candidate_family, candidate_ends, wings in alternatives:
                try:
                    fitted, fit = refine_roof_lines(alternative, support, eave, image, meters_to_pixel, evidence_footprint=footprint,
                                                    **view_kwargs)
                    candidate = clip_support(fitted, footprint, eave, candidate_family)
                    geometry = audit_roof(candidate, footprint, eave)
                    drainage = geometry.get('drainage', {'passed': False})
                    cost = topology_cost(candidate, observations)
                    record = {**detail, **cost, 'junction_refinement': junctions,
                              'wing_refinement': wings,
                              'geometry_passed': geometry['passed'], 'drainage_passed': drainage['passed']}
                    report['candidates'].append(record)
                    if geometry['passed'] and drainage['passed']:
                        viable.append((candidate, cost, {'selected_support': detail, 'line_refinement': fit,
                                       'junction_refinement': junctions, 'wing_refinement': wings,
                                       'roof_type': candidate_family, 'end_decisions': candidate_ends}))
                except (ValueError, RuntimeError, ArithmeticError, GEOSException):
                    continue
        except (ValueError, RuntimeError, ArithmeticError, GEOSException):
            continue
    # Compare with the best valid image fit, including unchanged geometry. A
    # poor original fit must not license a new simplification that contradicts
    # a better supported alternative; evaluation is independent of run order.
    best_image = min([base_cost['image_line_mismatch_m'], *[cost['image_line_mismatch_m'] for _, cost, _ in viable]])
    report['image_veto_limit_m'] = round(best_image+.2, 5)
    for candidate, cost, detail in sorted(viable, key=lambda row: row[1]['score']):
        if (cost['construction_complexity'] < base_cost['construction_complexity']
                and cost['image_line_mismatch_m'] <= best_image+.2
                and cost['score'] < base_cost['score']-.1):
            chosen, chosen_cost = candidate, cost
            report.update(accepted=True, reason='simpler-supported-roof', **detail)
            break
    chosen, chosen_cost = _image_supported_simplification(
        chosen, chosen_cost, viable, footprint, eave, rise, image, meters_to_pixel, observations, report,
        image_bounds=image_bounds, prefer_simple=prefer_simple, view_kwargs=view_kwargs, candidate_pool=candidate_pool)
    report['after'] = chosen_cost
    return chosen, report


def _fit_candidate(alternative, support, footprint, eave, image, meters_to_pixel, observations, family, view_kwargs):
    """Refine one hypothesis on its support, clip it to the exact outline and audit it (as stage one does)."""
    fitted, fit = refine_roof_lines(alternative, support, eave, image, meters_to_pixel, evidence_footprint=footprint, **view_kwargs)
    candidate = clip_support(fitted, footprint, eave, family)
    geometry = audit_roof(candidate, footprint, eave)
    drainage = geometry.get('drainage', {'passed': False})
    return candidate, fit, geometry, drainage, topology_cost(candidate, observations)


def _compact_support(cost):
    keys = ('score', 'image_line_mismatch_m', 'construction_complexity', 'physical_planes', 'crease_runs', 'junctions',
            'creases', 'crease_length_m', 'unsupported_crease_length_m', 'unsupported_share', 'supported_share',
            'contrast_share', 'line_supported_share', 'support_penalty', 'supported_score')
    return {key: cost.get(key) for key in keys}


def _image_supported_simplification(chosen, chosen_cost, viable, footprint, eave, rise, image, meters_to_pixel, observations,
                                     report, *, image_bounds, prefer_simple, view_kwargs, rules=SUPPORT_RULES, candidate_pool=None):
    """Stage two (roof-topology-support-v3): replace the selection only with a strictly simpler,
    at least as well image-supported hypothesis; otherwise return it unchanged, byte for byte.

    Candidates are the stage-one viable alternatives (wings, joined junctions, envelopes) plus
    the minimal supports from `minimal_supports`: the single-ridge rectangle, or (v2) the L/T
    union of two fitted rectangles when no single rectangle covers the outline. Gates, all required: fewer
    construction elements; observed-line residual within `residual_margin_m` of the best fit
    seen; audits (coverage, planarity, seams, drainage) passed; supported-crease share at least the
    current one's and at least `min_candidate_supported_share`; `supported_score` below the current
    one's by `score_margin`. A two-rectangle plan (v3) is also accepted when, instead of the last
    two, the optical set passes against the current fan (`optical_support`): combined supported
    share and explained-line share each within `optical_share_margin` of the current, no more
    unsupported creases, and at least `optical_min_supported_creases` supported;
    `stage['selected']['accepted_by']` records which set ('contrast-share', 'optical' or 'both').
    Nothing fires when every current crease is supported.
    """
    frame = np.asarray(meters_to_pixel, dtype=float)
    stage = {'method': TOPOLOGY_SUPPORT_VERSION, 'accepted': False, 'reason': 'no-supported-simpler-candidate',
             'rules': {k: rules[k] for k in ('crease_band_m', 'crease_threshold_l', 'line_distance_m', 'line_angle_deg',
                                              'share_weight', 'length_weight', 'residual_margin_m', 'score_margin',
                                              'min_candidate_supported_share', 'rectangle_coverage', 'two_rectangle_coverage',
                                              'two_rectangle_iou', 'two_rectangle_deviation_m', 'two_rectangle_min_share',
                                              'optical_line_distance_m', 'optical_line_angle_deg', 'optical_line_overlap_share',
                                              'optical_erosion_m', 'optical_share_margin', 'optical_min_supported_creases')},
             'minimal_candidates': 0, 'candidates': []}
    try:
        # The stage's own image preparation is guarded too: a failure here must record
        # itself and hand the v5 selection back unchanged, never break the proposal.
        lightness = lab_lightness(image)
        mask = _footprint_mask(footprint, frame, lightness.shape[:2])

        def measured(parts_):
            support = crease_support(parts_, footprint, observations, image, meters_to_pixel, rules=rules, lightness=lightness, mask=mask)
            return topology_cost(parts_, observations, support=support, rules=rules)

        def optical(parts_):
            return optical_support(parts_, footprint, observations, image, meters_to_pixel, rules=rules, lightness=lightness, mask=mask)
        current = measured(chosen)
    except (ValueError, RuntimeError, ArithmeticError, GEOSException, cv2.error) as error:
        stage.update(reason='support-measurement-failed', error=f'{type(error).__name__}: {error}'[:200])
        report['support'] = stage
        return chosen, chosen_cost
    stage['current'] = _compact_support(current)
    current_optical = None  # measured lazily, only when a two-rectangle plan is compared
    pool = [(candidate, cost, detail) for candidate, cost, detail in viable if candidate is not chosen]
    for support, detail in minimal_supports(footprint, rules=rules):
        stage['minimal_candidates'] += 1
        try:
            support_parts = structural_prior(support, eave, eave+rise, regularize_eaves=False)

            class Frame:
                def metre_to_pixel(self, x, y):
                    return np.array([x, y, 1.]) @ frame

            support_parts, ends = select_roof_ends(support_parts, support, eave, image, Frame(), {}, evidence_footprint=footprint,
                                                   prefer_simple=prefer_simple, image_bounds=image_bounds)
            families = {end.get('selected') for end in ends['ends']}
            family = 'mixed' if {'gable', 'hip'} <= families else 'gabled' if 'gable' in families else 'hipped'
            candidate, fit, geometry, drainage, cost = _fit_candidate(support_parts, support, footprint, eave, image, meters_to_pixel,
                                                                      observations, family, view_kwargs)
            if not (geometry['passed'] and drainage['passed']):
                stage['candidates'].append({**detail, **_compact_support(cost), 'geometry_passed': geometry['passed'],
                                            'drainage_passed': drainage['passed'], 'gates': None})
                continue
            pool.append((candidate, cost, {'selected_support': detail, 'line_refinement': fit, 'junction_refinement': None,
                                           'wing_refinement': None, 'roof_type': family, 'end_decisions': ends}))
        except (ValueError, RuntimeError, ArithmeticError, GEOSException) as error:
            stage['candidates'].append({**detail, 'error': f'{type(error).__name__}: {error}'[:200], 'gates': None})
            continue
    if candidate_pool is not None:
        # Retain already generated/audited alternatives for the bounded optical
        # stage. These live only in memory, never in the persisted report.
        candidate_pool.extend(pool)
    best_image = min([chosen_cost['image_line_mismatch_m'], report['before']['image_line_mismatch_m'],
                      *[cost['image_line_mismatch_m'] for _, cost, _ in viable], *[cost['image_line_mismatch_m'] for _, cost, _ in pool]])
    stage['residual_limit_m'] = round(best_image+rules['residual_margin_m'], 5)
    ranked = []
    for candidate, _, detail in pool:
        two_rectangle = bool((detail.get('selected_support') or {}).get('shape'))
        try:
            full = measured(candidate)
            optics = None
            if two_rectangle:
                if current_optical is None:
                    current_optical = optical(chosen)
                optics = optical(candidate)
        except (ValueError, RuntimeError, ArithmeticError, GEOSException, cv2.error):
            continue
        # The v2 gate set (brightness-step share and ranking margin) applies to every candidate;
        # a two-rectangle plan is also accepted when the v3 optical set passes instead.
        gates = {'simpler': full['construction_complexity'] < current['construction_complexity'],
                 'residual': full['image_line_mismatch_m'] <= best_image+rules['residual_margin_m'],
                 'supported_share': full['supported_share'] >= max(current['supported_share'], rules['min_candidate_supported_share']),
                 'ranking': full['supported_score'] < current['supported_score']-rules['score_margin']}
        optical_gates = None
        if two_rectangle:
            margin = rules['optical_share_margin']
            optical_gates = {
                'simpler': gates['simpler'], 'residual': gates['residual'],
                'optical_supported_share': bool(optics['combined_supported_share'] >= current_optical['combined_supported_share']-margin),
                'optical_unsupported_count': bool(optics['unsupported_creases'] <= current_optical['unsupported_creases']),
                'optical_explained_lines': bool(optics['explained_line_share'] >= current_optical['explained_line_share']-margin),
                'optical_evidence': bool(optics['supported_creases'] >= rules['optical_min_supported_creases'])}
        ranked.append((full, detail, gates, candidate, optics, optical_gates))
    ranked.sort(key=lambda row: row[0]['supported_score'])
    if current_optical is not None:
        stage['current_optical'] = {k: current_optical[k] for k in ('combined_supported_share', 'supported_creases', 'unsupported_creases',
                                                                    'explained_line_share', 'interior_line_length_m')}

    def accepted_by(gates, optical_gates):
        by_share, by_optics = all(gates.values()), optical_gates is not None and all(optical_gates.values())
        return 'both' if by_share and by_optics else 'contrast-share' if by_share else 'optical' if by_optics else None
    for full, detail, gates, _, optics, optical_gates in ranked:
        record = {**(detail.get('selected_support') or {}), **_compact_support(full), 'gates': gates,
                  'failed_gates': [name for name, passed in gates.items() if not passed],
                  'wing_refinement': bool(detail.get('wing_refinement')),
                  'junction_refinement': bool(detail.get('junction_refinement')), 'roof_type': detail.get('roof_type')}
        if optics is not None:
            record.update(optical_gates=optical_gates, failed_optical_gates=[name for name, passed in optical_gates.items() if not passed],
                          accepted_by=accepted_by(gates, optical_gates),
                          combined_supported_share=optics['combined_supported_share'], supported_creases=optics['supported_creases'],
                          unsupported_creases=optics['unsupported_creases'], explained_line_share=optics['explained_line_share'],
                          v2_gate_supported_share=gates['supported_share'])
        stage['candidates'].append(record)
    if current['unsupported_share'] <= 0:
        stage['reason'] = 'current-creases-supported'
    else:
        for full, detail, gates, candidate, optics, optical_gates in ranked:
            by = accepted_by(gates, optical_gates)
            if by is not None:
                stage.update(accepted=True, reason='simpler-image-supported-roof')
                selected = detail.get('selected_support') or {}
                if selected.get('shape'):
                    stage['selected'] = {k: selected.get(k) for k in ('shape', 'rectangles', 'support_corners', 'union_iou', 'support_offset_m')}
                    stage['selected'].update(accepted_by=by, optical=optics)
                report.update(accepted=True, reason='simpler-image-supported-roof', method=TOPOLOGY_SUPPORT_VERSION, **detail)
                report['support'] = stage
                return candidate, full
    report['support'] = stage
    return chosen, chosen_cost
