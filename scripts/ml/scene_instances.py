"""Exact split/union operations for editable scene instances; never save reviews."""
from __future__ import annotations

import json
import math
import sys

from shapely.geometry import LineString, Polygon
from shapely.ops import split, unary_union


def partition_outlines(outline, children):
    """Validate a supplied pure partition; no source pixel area may disappear."""
    if not isinstance(children, list) or not 2 <= len(children) <= 8:
        raise ValueError('Supply two to eight child polygons, including explicit unresolved residual parts.')

    def polygon(value):
        if not isinstance(value, dict) or set(value) != {'ring', 'holes'}:
            raise ValueError('Supply open outer rings and interior gaps for every polygon.')
        boundaries = [value['ring'], *(value['holes'] if isinstance(value['holes'], list) else [])]
        if (not isinstance(value['holes'], list) or len(value['holes']) > 32
                or any(not isinstance(ring, list) or not 3 <= len(ring) <= 200 or ring[0] == ring[-1]
                       or any(not isinstance(point, list) or len(point) != 2
                              or any(isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number)
                                     for number in point) for point in ring) for ring in boundaries)
                or sum(map(len, boundaries)) > 2000):
            raise ValueError('Use finite open polygon rings within the scene vertex limits.')
        result = Polygon(value['ring'], value['holes'])
        if not result.is_valid or result.is_empty or result.area < 4:
            raise ValueError('Fix invalid polygons, gaps or child parts under four source square pixels.')
        return result

    source = polygon(outline)
    pieces = [polygon(child) for child in children]
    coverage = unary_union(pieces)
    outside = sum(piece.difference(source).area for piece in pieces)
    overlap = sum(piece.area for piece in pieces)-coverage.area
    missing = source.difference(coverage).area
    if outside > 1e-7:
        raise ValueError('Every child must stay inside the source outline and outside its existing gaps.')
    if overlap > 1e-7:
        raise ValueError('Child interiors cannot overlap; shared seam edges are allowed.')
    if missing > 1e-7 or coverage.symmetric_difference(source).area > 1e-7:
        raise ValueError('The children must preserve all source coverage. Include uncertain remainder as an explicit unresolved residual child.')
    return {'rings': [child['ring'] for child in children], 'holes': [child['holes'] for child in children], 'children': {},
            'area_px': coverage.area, 'source_area_px': source.area,
            'outside_area_px': outside, 'overlap_area_px': max(0., overlap), 'missing_area_px': missing,
            'numeric_area_tolerance_px': 1e-7}


def replace_outlines(operation, rings, cut=None, children=None, holes=None, outline=None):
    if operation not in ('split', 'merge', 'trace') or not isinstance(rings, list) or not 1 <= len(rings) <= (64 if operation == 'trace' else 8):
        raise ValueError('Choose one outline to split, two to eight to merge, or up to 64 to trace again.')
    polygons = []
    if holes is not None and (not isinstance(holes, list) or len(holes) != len(rings)):
        raise ValueError('Supply interior gaps for each source outline.')
    for index, ring in enumerate(rings):
        if not isinstance(ring, list) or not 3 <= len(ring) <= 200 or any(
                not isinstance(p, list) or len(p) != 2 or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in p) for p in ring):
            raise ValueError('Use finite outline points.')
        p = Polygon(ring, holes[index] if holes else None)
        if not p.is_valid or p.area < 1:
            raise ValueError('Fix the outline before splitting or merging it.')
        polygons.append(p)
    before = unary_union(polygons)
    if operation == 'trace':
        if not isinstance(outline, dict) or not isinstance(outline.get('ring'), list):
            raise ValueError('Draw the replacement outline first.')
        boundaries = [outline['ring'], *(outline.get('holes') or [])]
        if any(not isinstance(r, list) or not 3 <= len(r) <= 200 or any(
                not isinstance(p, list) or len(p) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in p)
                for p in r) for r in boundaries):
            raise ValueError('Use finite replacement outline points.')
        replacement = Polygon(outline['ring'], outline.get('holes'))
        if not replacement.is_valid or replacement.area < 1:
            raise ValueError('Fix the replacement outline before applying it.')
        if any(replacement.intersection(p).area <= 1e-7 for p in polygons):
            raise ValueError('The replacement must overlap every selected outline. Remove unrelated selections first.')
        pieces = [replacement]
    elif operation == 'split':
        if len(polygons) != 1 or not isinstance(cut, list) or len(cut) != 2 or any(
                not isinstance(p, list) or len(p) != 2 or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in p) for p in cut):
            raise ValueError('Place the two ends of a cut across the outline.')
        line = LineString(cut)
        if line.length < 1e-6:
            raise ValueError('The cut needs two different endpoints.')
        pieces = list(split(before, line).geoms)
        if len(pieces) < 2:
            raise ValueError('Extend the cut across the outline, from one side to the other.')
        if len(pieces) > 8:
            raise ValueError('This cut makes too many pieces. Split a smaller section first.')
    else:
        if len(polygons) < 2:
            raise ValueError('Select at least two outlines to merge.')
        if before.geom_type != 'Polygon':
            raise ValueError('These outlines are disconnected. Use a group to link separate objects, or correct their edges first.')
        pieces = [before]
    if any(p.geom_type != 'Polygon' or not p.is_valid or p.area < 1 or len(p.exterior.coords)-1 > 200 or len(p.interiors) > 32
           or any(len(h.coords)-1 > 200 for h in p.interiors) or len(p.exterior.coords)-1+sum(len(h.coords)-1 for h in p.interiors) > 2000 for p in pieces):
        raise ValueError('The result contains a tiny piece or overly complex outline. Adjust the cut or source outlines.')
    # Stable ordering helps callers retain a predictable selection after a cut.
    pieces.sort(key=lambda p: (p.centroid.x, p.centroid.y))
    coverage = unary_union(pieces)
    if operation != 'trace' and (coverage.symmetric_difference(before).area > 1e-7 or sum(p.area for p in pieces)-coverage.area > 1e-7):
        raise ValueError('The result did not preserve the source coverage.')
    assignments = {}
    for child in children or []:
        p = Polygon(child['ring'], child.get('holes'))
        matches = [i for i, piece in enumerate(pieces) if piece.buffer(1e-7).covers(p)]
        assignments[child['id']] = matches[0] if len(matches) == 1 else None
    return {'rings': [[list(p) for p in piece.exterior.coords[:-1]] for piece in pieces],
            'holes': [[[list(p) for p in hole.coords[:-1]] for hole in piece.interiors] for piece in pieces],
            'children': assignments, 'area_px': coverage.area}


if __name__ == '__main__':
    try:
        value = json.load(sys.stdin)
        result = partition_outlines(value['outline'], value['partition']) if value.get('operation') == 'partition' else replace_outlines(
            value['operation'], value['rings'], value.get('cut'), value.get('children'), value.get('holes'), value.get('outline'))
        print(json.dumps(result, allow_nan=False))
    except (ValueError, TypeError, KeyError) as error:
        print(json.dumps({'error': str(error)}))
        raise SystemExit(2) from error
