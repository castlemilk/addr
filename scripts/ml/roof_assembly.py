"""Join image-supported roof sections before fitting one shared roof surface.

Only overlapping detector masks with a substantial, same-material junction are
eligible. Proximity or a shared parcel cannot join detached buildings. Component
masks and junction evidence remain attached to the unreviewed model proposal.
"""
from __future__ import annotations

import hashlib

import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union

from .roof_surface import _pixels, refine_roof_surface

VERSION = 'connected-roof-v1'


def junction_evidence(a, b, image, frame):
    scale = float(np.linalg.svd(np.asarray(frame)[:2], compute_uv=False).max())
    pixel_area = abs(float(np.linalg.det(np.asarray(frame)[:2])))
    overlap = a.intersection(b).area
    # Nested/duplicate masks are dealt with by suppression, not assembly.
    if overlap <= 0 or overlap/min(a.area, b.area) > .35:
        return None
    tolerance = min(2., .25/scale)
    contact = min(a.boundary.intersection(b.buffer(tolerance)).length,
                  b.boundary.intersection(a.buffer(tolerance)).length)*scale
    if contact < 1.5:
        return None
    seam = a.buffer(tolerance).intersection(b.buffer(tolerance))
    samples = [_pixels(p.intersection(seam.buffer(.8/scale)), image) for p in (a, b)]
    if min(map(len, samples)) < 8:
        return None
    colors = [np.median(p, axis=0) for p in samples]
    x, y = colors
    chroma = float(np.linalg.norm((x-x.mean())-(y-y.mean())))
    # Chromaticity tolerates illumination changes across opposing roof slopes.
    if chroma > 16 or abs(x.mean()-y.mean()) > 45:
        return None
    return {'contact_length_m': round(contact, 3), 'overlap_m2': round(overlap*pixel_area, 4),
            'material_chroma_difference': round(chroma, 3)}


def assemble_roof_candidates(candidates, image, frame, *, canopies=()):
    """Return the same candidate tuples, with bounded connected assemblies."""
    if len(candidates) < 2:
        return candidates
    # Test continuity on detector masks, before independent edge fitting. The
    # union is also fitted from those raw masks so displacement limits apply
    # once, rather than accumulating across component and assembly cleanup.
    raw = [Polygon(meta['raw_ring'], meta.get('raw_holes')) if meta.get('raw_ring') else p
           for p, _, _, meta in candidates]
    groups = list(range(len(candidates)))
    joins = []
    def root(i):
        while groups[i] != i:
            i = groups[i]
        return i
    for i, a in enumerate(raw):
        for j in range(i):
            b = raw[j]
            evidence = junction_evidence(a, b, image, frame)
            if evidence is not None:
                groups[root(i)] = root(j)
                joins.append((i, j, evidence))
    components = {}
    for i in range(len(candidates)):
        components.setdefault(root(i), []).append(i)
    result = []
    pixel_area = abs(float(np.linalg.det(np.asarray(frame)[:2])))
    for members in components.values():
        if len(members) == 1:
            result.append(candidates[members[0]])
            continue
        polygons = [raw[i] for i in members]
        joined = unary_union(polygons)
        if joined.geom_type != 'Polygon' or not joined.is_valid:
            result.extend(candidates[i] for i in members)
            continue
        # A union can trap one-pixel slivers between overlapping masks. Close
        # only these newly created specks; original holes/courtyards stay exact.
        original_holes = unary_union([Polygon(h) for p in polygons for h in p.interiors])
        holes = [h for h in joined.interiors if Polygon(h).area > min(4., .1/pixel_area)
                 or Polygon(h).intersects(original_holes)]
        joined = Polygon(joined.exterior, holes)
        joined, refinement = refine_roof_surface(joined, image, frame, canopies=canopies)
        # Respect the roof fitter's limits; do not replace viable components
        # with an outline that cannot be inspected or fitted.
        if len(joined.exterior.coords)-1 > 150:
            result.extend(candidates[i] for i in members)
            continue
        identities = sorted(candidates[i][2] for i in members)
        identity = VERSION + ':' + ':'.join(identities)
        refinement['assembly'] = {'method': VERSION, 'components': [
            {'id': hashlib.sha256(candidates[i][2].encode()).hexdigest()[:16],
             'ring': [list(p) for p in raw[i].exterior.coords[:-1]],
             'holes': [[list(p) for p in h.coords[:-1]] for h in raw[i].interiors],
             'score': round(candidates[i][1], 4)} for i in members],
            'junctions': [{'a': members.index(i), 'b': members.index(j), **e}
                          for i, j, e in joins if i in members and j in members],
            'note': 'Connected roof hypothesis from overlapping masks and matching material; needs review.'}
        # Joining sections cannot raise confidence above the weakest mask.
        result.append((joined, min(candidates[i][1] for i in members), identity, refinement))
    return result
