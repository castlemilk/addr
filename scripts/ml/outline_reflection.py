"""Reflection records for reviewer-flagged outlines (CP20 reviewer set, 2026-09-30).

For each scene:object case this measures, from latest.json and the scene image only:
  - outline geometry: corner count, corners per metre after a 0.3 m simplify, the minimum rotated rectangle and
    the share of edges off its axes (the object-hints noisy/degenerate rules, unchanged since v1), rectilinearity, the
    orthogonalise gate (training.common.orthogonalise, ORTHO gates), the distance to the frame edge and the
    clipped_by_frame / replaces lineage;
  - neighbours: nearest live building, largest live building, area ratio;
  - image evidence along the edges: per-edge luminance step between a band just inside and just outside the
    outline (a dark outside band is a shadow side), interior luminance spread and a two-class split (a strong
    internal step is what a two-level roof or a shadowed lower storey leaves in the pixels);
  - what the roof fitter did (roof_type, roof_parts, roof_lines, source) and what the eyes index serves today;
  - which object-hints outline codes (rules unchanged since v1) the editor now shows for it.
The human note is read from review.json for the record (never written). Output: reflections.json and
REFLECTION.md under data/reviews/eyes/reflections/<date>/, nothing else. Optional --population runs a JSON-only
scan over every latest.json for the counts the reflections cite (roof models on tiny buildings, clipped sam3
remnants, sub-square-metre buildings, noisy outlines by pixel class).

Wording: agreement, model estimate, hint; never accuracy. Nothing here is a label and nothing is applied.

  scripts/ml/.venv/bin/python -m scripts.ml.outline_reflection \\
      --case house-25910:816ff673852a41c9 --case roof_training_250-29501:587e522a2986ccea \\
      --case roof_training_250-29501:4fb089fc5f936f20 --population
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import sqlite3
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from shapely.geometry import LineString, Polygon, box

from scripts.ml.roof_reflection import audit_polygon_geometry
from scripts.ml.scene_object_quality import load_image
from scripts.ml.training.common import ORTHO, min_rectangle, orthogonalise

VERSION = 'outline-reflection-v1'
NOTE = 'measured reflection; agent hints, not labels; nothing applied'
REFLECTIONS_ROOT = Path('data/reviews/eyes/reflections')
# The object-hints outline rules (unchanged since v1; object-hints-v2 added only solar_fixture_like), the same values as
# src/lib/outline-hints.ts OUTLINE_DESIGN.
DESIGN = {
    'noisy_outline': {'min_area_m2': 20., 'simplify_m': .3, 'min_vertices_per_metre': .45, 'min_wonky_share': .85,
                      'axis_tolerance_deg': 4.5, 'min_edge_m': .2},
    'degenerate_outline': {'max_area_m2': 1., 'max_rect_width_m': .5},
    'tiny_area_for_kind': {'building': 4., 'tree': 1., 'vegetation': 1., 'pool': 2.},
    'edge_band_m': (.3, .9),          # luminance sampled this far inside and outside each edge
    'shadow_step': 25.,               # outside darker than inside by this many levels reads as a shadow side
    'internal_step': 40.,             # two-class interior luminance means this far apart read as an internal step
    'internal_minority_share': .2,
    'roof_fit_gate_m2': 6.,           # the spec's candidate gate for fitting a roof model (measured, not applied)
    'pixel_classes_m': (.08, .2),
}
LIVE = ('candidate', 'confirmed', 'needs_adjustment')


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def frame(scene):
    return np.asarray(scene['pixel_to_meters'], dtype=float)


def metres_per_pixel(scene):
    return float(abs(np.linalg.det(frame(scene)[:2, :2])) ** .5)


def local_ring(points, scene):
    pts = np.asarray(points, dtype=float)
    return (np.c_[pts, np.ones(len(pts))] @ frame(scene)).tolist()


def live_objects(scene, kind=None):
    return [o for o in scene['objects'] if o['status'] in LIVE and (kind is None or o['kind'] == kind)]


def rect_sides(polygon):
    corners = np.asarray(min_rectangle(polygon).exterior.coords, dtype=float)
    a, b = np.hypot(*(corners[1] - corners[0])), np.hypot(*(corners[2] - corners[1]))
    long_edge = corners[1] - corners[0] if a >= b else corners[2] - corners[1]
    axis = math.degrees(math.atan2(long_edge[1], long_edge[0])) % 90.
    return float(min(a, b)), float(max(a, b)), axis


def axis_deviation(edge_deg, axis_deg):
    d = abs(edge_deg % 90. - axis_deg % 90.)
    return min(d, 90. - d)


def geometry(obj, scene):
    """Every outline number the reflections quote, in metres; the same definitions as the editor's outline-hints module."""
    ring = local_ring(obj['ring'], scene)
    polygon = Polygon(ring)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    px_m = metres_per_pixel(scene)
    design = DESIGN['noisy_outline']
    simplified = polygon.simplify(design['simplify_m'], preserve_topology=True)
    simplified_vertices = len(simplified.exterior.coords) - 1 if simplified.geom_type == 'Polygon' else len(ring)
    width, length, axis = rect_sides(polygon)
    measured = off_axis = 0
    for i in range(len(ring)):
        a, b = np.asarray(ring[i]), np.asarray(ring[(i + 1) % len(ring)])
        edge = float(np.hypot(*(b - a)))
        if edge < design['min_edge_m']:
            continue
        measured += 1
        if axis_deviation(math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])), axis) > design['axis_tolerance_deg']:
            off_axis += 1
    ortho_score, parallelism, wonky_count, _ = audit_polygon_geometry(ring, axis, design['axis_tolerance_deg'])
    pixels = Polygon(obj['ring'])
    edge_px = float(pixels.distance(box(0, 0, scene['image_width'], scene['image_height']).boundary))
    squared = orthogonalise(pixels, px_m) if pixels.is_valid and not pixels.is_empty else None
    if squared is not None:
        gate = {'passed': True, 'iou': round(squared.intersection(pixels).area / max(squared.union(pixels).area, 1e-9), 3),
                'hausdorff_m': round(float(squared.hausdorff_distance(pixels)) * px_m, 2), 'vertices': len(squared.exterior.coords) - 1}
    else:
        gate = {'passed': False, 'reason': f"no axis candidate met IoU >= {ORTHO['min_iou']} and Hausdorff <= {ORTHO['max_hausdorff_m']} m (or the outline has holes)"}
    replaced_by = [o['id'] for o in scene['objects'] if obj['id'] in (o.get('replaces') or [])]
    rect_area = width * length
    # Exterior ring only (a buffer(0) repair may yield a multipolygon: then the whole boundary), matching `vertices` and the editor.
    perimeter = float(polygon.exterior.length) if polygon.geom_type == 'Polygon' else float(polygon.length)
    return {
        'area_m2': round(polygon.area, 3), 'perimeter_m': round(perimeter, 2), 'vertices': len(ring),
        'simplified_vertices': simplified_vertices,
        'vertices_per_metre': round(simplified_vertices / perimeter, 3) if perimeter else None,
        'rect_width_m': round(width, 2), 'rect_length_m': round(length, 2), 'axis_deg': round(axis, 1),
        'edges_measured': measured, 'off_axis_edges': off_axis, 'wonky_share': round(off_axis / measured, 3) if measured else None,
        'audit_orthogonality': ortho_score, 'audit_parallelism': parallelism, 'audit_wonky_edges': wonky_count,
        'rectilinearity': round(polygon.area / rect_area, 3) if rect_area else None,
        'metres_per_pixel': round(px_m, 4), 'frame_edge_px': round(edge_px, 1),
        'clipped_by_frame': obj.get('clipped_by_frame'), 'replaces': obj.get('replaces') or [], 'replaced_by': replaced_by,
        'orthogonalise': gate,
    }


def neighbours(obj, scene):
    """Nearest and largest live buildings (the reflections name them); distances in metres between outlines."""
    me = Polygon(local_ring(obj['ring'], scene))
    others = [(o, Polygon(local_ring(o['ring'], scene))) for o in live_objects(scene, 'building') if o['id'] != obj['id']]
    others = [(o, p) for o, p in others if p.is_valid and not p.is_empty]
    if not others:
        return {'nearest': None, 'largest': None}
    nearest = min(others, key=lambda item: me.distance(item[1]))
    largest = max(others, key=lambda item: item[1].area)
    return {
        'nearest': {'id': nearest[0]['id'], 'distance_m': round(me.distance(nearest[1]), 2), 'area_m2': round(nearest[1].area, 1)},
        'largest': {'id': largest[0]['id'], 'area_m2': round(largest[1].area, 1), 'ratio': round(me.area / largest[1].area, 3) if largest[1].area else None},
    }


def _mask(shape, size):
    """Boolean raster of a shapely polygon or multipolygon in pixel coordinates."""
    image = Image.new('1', size, 0)
    draw = ImageDraw.Draw(image)
    parts = [shape] if shape.geom_type == 'Polygon' else list(getattr(shape, 'geoms', []))
    for part in parts:
        if part.geom_type != 'Polygon' or part.is_empty:
            continue
        draw.polygon([tuple(p) for p in part.exterior.coords], fill=1)
        for hole in part.interiors:
            draw.polygon([tuple(p) for p in hole.coords], fill=0)
    return np.asarray(image, dtype=bool)


def image_evidence(obj, scene, image):
    """Luminance steps across each simplified edge and inside the outline; None when the image is unavailable."""
    if image is None:
        return None
    luminance = np.asarray(image, dtype=float) @ np.array([.299, .587, .114])
    size = (scene['image_width'], scene['image_height'])
    px_m = metres_per_pixel(scene)
    pixels = Polygon(obj['ring'])
    if not pixels.is_valid:
        pixels = pixels.buffer(0)
    inner, outer = (d / px_m for d in DESIGN['edge_band_m'])
    inside_band = pixels.difference(pixels.buffer(-outer)).intersection(pixels.buffer(-inner)) if pixels.area > 0 else pixels
    if inside_band.is_empty:
        inside_band = pixels
    outside_band = pixels.buffer(outer).difference(pixels.buffer(inner))
    simplified = pixels.simplify(DESIGN['noisy_outline']['simplify_m'] / px_m, preserve_topology=True)
    coords = list(simplified.exterior.coords) if simplified.geom_type == 'Polygon' else list(pixels.exterior.coords)
    edges = []
    for a, b in zip(coords[:-1], coords[1:], strict=True):
        segment = LineString([a, b])
        length_m = segment.length * px_m
        if length_m < 1.:
            continue
        corridor = segment.buffer(outer)
        inside = _mask(inside_band.intersection(corridor), size)
        outside = _mask(outside_band.intersection(corridor), size)
        if inside.sum() < 4 or outside.sum() < 4:
            continue
        step = float(luminance[inside].mean() - luminance[outside].mean())
        edges.append({'length_m': round(length_m, 2), 'inside': round(float(luminance[inside].mean()), 1),
                      'outside': round(float(luminance[outside].mean()), 1), 'step': round(step, 1)})
    total = sum(e['length_m'] for e in edges) or 1.
    shadow = [e for e in edges if e['step'] >= DESIGN['shadow_step']]
    darker_inside = [e for e in edges if e['step'] <= -DESIGN['shadow_step']]
    interior = _mask(pixels, size)
    values = luminance[interior]
    split = None
    if values.size >= 16:
        # Otsu split of the interior luminance: a strong step between the two classes is what a two-level roof or a
        # shadowed lower storey leaves; a single bright plane has a small one.
        hist, bins = np.histogram(values, bins=64, range=(0, 255))
        centres = (bins[:-1] + bins[1:]) / 2
        weight = hist.cumsum()
        best, threshold = -1., None
        for k in range(1, 63):
            w0, w1 = weight[k], weight[-1] - weight[k]
            if w0 == 0 or w1 == 0:
                continue
            m0 = (hist[:k] * centres[:k]).sum() / w0
            m1 = (hist[k:] * centres[k:]).sum() / w1
            between = w0 * w1 * (m0 - m1) ** 2
            if between > best:
                best, threshold = between, centres[k]
        if threshold is not None:
            dark = values[values < threshold]
            bright = values[values >= threshold]
            split = {'threshold': round(float(threshold), 1), 'dark_mean': round(float(dark.mean()), 1), 'bright_mean': round(float(bright.mean()), 1),
                     'dark_share': round(float(dark.size / values.size), 3), 'step': round(float(bright.mean() - dark.mean()), 1)}
    internal_step = bool(split and split['step'] >= DESIGN['internal_step']
                         and min(split['dark_share'], 1 - split['dark_share']) >= DESIGN['internal_minority_share'])
    return {
        'edges_measured': len(edges), 'edges': edges,
        'shadow_side_edges': len(shadow), 'shadow_side_length_share': round(sum(e['length_m'] for e in shadow) / total, 3),
        'darker_inside_edges': len(darker_inside),
        'interior': {'pixels': int(values.size), 'p10': round(float(np.percentile(values, 10)), 1), 'p50': round(float(np.percentile(values, 50)), 1),
                     'p90': round(float(np.percentile(values, 90)), 1), 'std': round(float(values.std()), 1)} if values.size else None,
        'interior_split': split, 'internal_step': internal_step,
    }


def fitter(obj):
    return {'roof_type': obj.get('roof_type'), 'roof_parts': len(obj.get('roof_parts') or []), 'roof_lines': len(obj.get('roof_lines') or []),
            'roof_geometry_source': obj.get('roof_geometry_source'), 'roof_annotation': bool(obj.get('roof_annotation')),
            'height_m': obj.get('model', {}).get('height_m'), 'source': obj.get('source'), 'score': obj.get('score'), 'label': obj.get('label'),
            'structure_kind': obj.get('structure_kind'), 'geometry_reviewed': obj.get('geometry_reviewed')}


def served(root, scene, obj):
    """What the eyes index holds for the object today (read-only): structure kind, class hint, object hints, by version and signature."""
    file = Path(root) / 'data/reviews/eyes/eyes.sqlite'
    if not file.exists():
        return {'available': False}
    out = {'available': True, 'rows': []}
    try:
        db = sqlite3.connect(f'file:{file}?mode=ro', uri=True)
        try:
            rows = db.execute('SELECT version, model_signature, payload FROM object_quality WHERE scene_id = ? AND object_id = ? ORDER BY version',
                              (scene['id'], obj['id'])).fetchall()
        finally:
            db.close()
    except sqlite3.Error as error:
        return {'available': False, 'error': type(error).__name__}
    for version, signature, payload in rows:
        try:
            doc = json.loads(payload)
        except json.JSONDecodeError:
            continue
        row = {'version': version, 'current': signature == scene['model_signature']}
        if version.startswith('structure-kinds-v'):
            row.update(sub_kind=doc.get('sub_kind'), confidence=doc.get('confidence'), rule=doc.get('rule'))
        elif version.startswith('building-flags-v'):
            hints = doc.get('class_hints') or {}
            row.update(class_hint=hints.get('class_hint'), flags=[f.get('flag') for f in hints.get('flags', []) if isinstance(f, dict)])
        elif version.startswith('object-hints-v'):
            row.update(codes=[h.get('code') for h in doc.get('hints', []) if isinstance(h, dict)])
        else:
            continue
        out['rows'].append(row)
    return out


def editor_hints(kind, geo):
    """The object-hints outline codes (rules unchanged since v1) the editor computes itself for this geometry (src/lib/outline-hints.ts)."""
    codes = []
    degenerate = DESIGN['degenerate_outline']
    if geo['area_m2'] < degenerate['max_area_m2'] or geo['rect_width_m'] < degenerate['max_rect_width_m']:
        codes.append('degenerate_outline')
    floor = DESIGN['tiny_area_for_kind'].get(kind)
    if floor is not None and geo['area_m2'] < floor:
        codes.append('tiny_area_for_kind')
    noisy = DESIGN['noisy_outline']
    if (kind == 'building' and geo['area_m2'] >= noisy['min_area_m2'] and (geo['vertices_per_metre'] or 0) >= noisy['min_vertices_per_metre']
            and (geo['wonky_share'] or 0) >= noisy['min_wonky_share']):
        codes.append('noisy_outline')
    return codes


def human(root, scene_id, object_id):
    """The reviewer's recorded decision for the record; read-only."""
    file = Path(root) / 'data/scenes' / scene_id / 'review.json'
    if not file.exists():
        return None
    review = json.loads(file.read_text())
    for obj in review.get('objects', []):
        if obj.get('id') == object_id:
            return {'revision': review.get('revision'), 'status': obj.get('status'), 'notes': obj.get('notes'),
                    'structure_kind': obj.get('structure_kind'), 'geometry_reviewed': obj.get('geometry_reviewed')}
    return {'revision': review.get('revision'), 'status': 'absent from review.json'}


def compose(case):
    """Measured sentences: what the numbers say, what the fitter did, what now flags it. Agreement wording, no verdicts."""
    geo, img, fit, near = case['geometry'], case['image_evidence'], case['fitter'], case['neighbours']
    lines = []
    lines.append(f"{geo['area_m2']} m² {case['kind']} outline with {geo['vertices']} corners over {geo['perimeter_m']} m of edge "
                 f"({geo['vertices_per_metre']} corners per metre after a {DESIGN['noisy_outline']['simplify_m']} m simplify); "
                 f"{geo['off_axis_edges']} of {geo['edges_measured']} measured edges sit more than {DESIGN['noisy_outline']['axis_tolerance_deg']} degrees off the outline's own axes "
                 f"(wonky share {geo['wonky_share']}), rectilinearity {geo['rectilinearity']}, minimum rectangle {geo['rect_width_m']} by {geo['rect_length_m']} m.")
    gate = geo['orthogonalise']
    lines.append(f"Orthogonalise gate: passed, IoU {gate['iou']} and largest offset {gate['hausdorff_m']} m against the stored ring, {gate['vertices']} corners."
                 if gate['passed'] else f"Orthogonalise gate: refused ({gate['reason']}); the squared candidate is therefore absent and the repair path is the reviewer's redraw or the vertex tools.")
    if geo['clipped_by_frame']:
        size = geo['clipped_by_frame'][2] - geo['clipped_by_frame'][0]
        lines.append(f"Carries clipped_by_frame {geo['clipped_by_frame']}: the earlier {size} px frame cut it; "
                     + (f"{len(geo['replaced_by'])} later object(s) list it in replaces." if geo['replaced_by'] else "no later object lists it in replaces, so the widened scene kept the remnant as its own building."))
    lines.append(f"Distance to the current frame edge: {geo['frame_edge_px']} px.")
    if near['nearest']:
        lines.append(f"Nearest live building {near['nearest']['id'][:8]} ({near['nearest']['area_m2']} m²) at {near['nearest']['distance_m']} m; "
                     f"largest live building {near['largest']['id'][:8]} ({near['largest']['area_m2']} m²), area ratio {near['largest']['ratio']}.")
    if img:
        lines.append(f"Image: {img['edges_measured']} simplified edges of 1 m or more measured; {img['shadow_side_edges']} have the outside band darker by "
                     f"{DESIGN['shadow_step']:.0f} levels or more (a shadow side), {img['shadow_side_length_share']} of the measured edge length; "
                     f"{img['darker_inside_edges']} have the inside darker."
                     + (f" Interior luminance p10/p50/p90 {img['interior']['p10']}/{img['interior']['p50']}/{img['interior']['p90']}" if img['interior'] else ' No interior pixels inside the frame.')
                     + (f"; two-class split {img['interior_split']['dark_mean']} vs {img['interior_split']['bright_mean']} (dark share {img['interior_split']['dark_share']}), "
                        f"{'an internal luminance step consistent with two roof levels or a shadowed lower part' if img['internal_step'] else 'no strong internal step'}." if img['interior_split'] else ''))
    else:
        lines.append('Image unavailable (hash or size mismatch); no edge luminance measured.')
    if fit['roof_parts']:
        lines.append(f"Fitter: roof_type {fit['roof_type']} with {fit['roof_parts']} roof parts and {fit['roof_lines']} roof lines from {fit['roof_geometry_source']}, "
                     f"fitted on {geo['area_m2']} m²" + (f" (under the {DESIGN['roof_fit_gate_m2']:.0f} m² gate the spec measures, not applies)." if geo['area_m2'] < DESIGN['roof_fit_gate_m2'] else '.'))
    else:
        lines.append(f"Fitter: no roof model on this object (roof_type {fit['roof_type']}, source {fit['source']}).")
    rows = case['served'].get('rows', [])
    if rows:
        lines.append('Index today: ' + '; '.join(
            f"{r['version']} {'current' if r['current'] else 'earlier signature'}: " + (f"{r.get('sub_kind')} ({r.get('confidence')})" if 'sub_kind' in r else
                                                                                  f"class hint {r.get('class_hint')}, flags {r.get('flags')}" if 'class_hint' in r else f"codes {r.get('codes')}")
            for r in rows) + '.')
    else:
        lines.append('Index today: no rows for this object.' if case['served'].get('available') else 'Index unavailable.')
    lines.append(f"Editor now: {', '.join(case['editor_hints']) if case['editor_hints'] else 'no outline hint'} (the object-hints outline rules, unchanged since v1, measured in the editor from the stored ring).")
    if case['human']:
        lines.append(f"Reviewer (review.json rev {case['human'].get('revision')}): {case['human'].get('status')}; note: {case['human'].get('notes')}")
    return lines


def reflect(root, scene_id, object_id):
    scene = json.loads((Path(root) / 'data/scenes' / scene_id / 'latest.json').read_text())
    obj = next((o for o in scene['objects'] if o['id'] == object_id), None)
    if obj is None:
        return {'scene_id': scene_id, 'object_id': object_id, 'status': 'object_missing'}
    image = load_image(root, scene)
    geo = geometry(obj, scene)
    case = {'scene_id': scene_id, 'object_id': object_id, 'kind': obj['kind'], 'status': obj['status'], 'model_signature': scene['model_signature'],
            'image_size': [scene['image_width'], scene['image_height']], 'geometry': geo, 'neighbours': neighbours(obj, scene),
            'image_evidence': image_evidence(obj, scene, image), 'fitter': fitter(obj), 'served': served(root, scene, obj),
            'editor_hints': editor_hints(obj['kind'], geo), 'human': human(root, scene_id, object_id)}
    case['reflection'] = compose(case)
    return case


def pixel_class(px_m):
    low, high = DESIGN['pixel_classes_m']
    return f'< {low}' if px_m < low else f'{low}-{high}' if px_m < high else f'>= {high}'


def population(root):
    """JSON-only counts over every latest.json: the denominators the reflections cite. No image is opened."""
    counts = {'scenes': 0, 'live_buildings': 0, 'buildings_under_1m2': 0, 'roof_parts_under_gate': 0, 'roof_parts_any': 0,
              'clipped_sam3_buildings': 0, 'clipped_sam3_under_gate': 0, 'clipped_sam3_under_gate_covered_80': 0,
              'noisy_by_pixel_class': {}, 'buildings_20m2_by_pixel_class': {}}
    noisy = DESIGN['noisy_outline']
    for file in sorted((Path(root) / 'data/scenes').glob('*/latest.json')):
        try:
            scene = json.loads(file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not scene.get('objects') or not scene.get('pixel_to_meters'):
            continue
        counts['scenes'] += 1
        px_m = metres_per_pixel(scene)
        klass = pixel_class(px_m)
        buildings = live_objects(scene, 'building')
        polygons = {}
        for o in buildings:
            try:
                p = Polygon(local_ring(o['ring'], scene))
            except (ValueError, TypeError):
                continue
            if not p.is_valid:
                p = p.buffer(0)
            if p.is_empty:
                continue
            polygons[o['id']] = p
        for o in buildings:
            p = polygons.get(o['id'])
            if p is None:
                continue
            counts['live_buildings'] += 1
            area = p.area
            if area < 1.:
                counts['buildings_under_1m2'] += 1
            if o.get('roof_parts'):
                counts['roof_parts_any'] += 1
                if area < DESIGN['roof_fit_gate_m2']:
                    counts['roof_parts_under_gate'] += 1
            if o.get('clipped_by_frame') and str(o.get('source', '')).startswith('sam3'):
                counts['clipped_sam3_buildings'] += 1
                if area < DESIGN['roof_fit_gate_m2']:
                    counts['clipped_sam3_under_gate'] += 1
                    covered = any(q.intersection(p).area / area >= .8 for i, q in polygons.items() if i != o['id'] and q.area > area)
                    counts['clipped_sam3_under_gate_covered_80'] += int(covered)
            if area >= noisy['min_area_m2']:
                counts['buildings_20m2_by_pixel_class'][klass] = counts['buildings_20m2_by_pixel_class'].get(klass, 0) + 1
                simplified = p.simplify(noisy['simplify_m'], preserve_topology=True)
                sv = len(simplified.exterior.coords) - 1 if simplified.geom_type == 'Polygon' else 0
                svpm = sv / p.exterior.length if p.exterior.length else 0.
                if svpm >= noisy['min_vertices_per_metre']:
                    _, _, axis = rect_sides(p)
                    ring = list(p.exterior.coords)[:-1]
                    measured = off = 0
                    for i in range(len(ring)):
                        a, b = np.asarray(ring[i]), np.asarray(ring[(i + 1) % len(ring)])
                        if float(np.hypot(*(b - a))) < noisy['min_edge_m']:
                            continue
                        measured += 1
                        off += axis_deviation(math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])), axis) > noisy['axis_tolerance_deg']
                    if measured and off / measured >= noisy['min_wonky_share']:
                        counts['noisy_by_pixel_class'][klass] = counts['noisy_by_pixel_class'].get(klass, 0) + 1
    counts['noisy_share_by_pixel_class'] = {k: round(counts['noisy_by_pixel_class'].get(k, 0) / n, 4) for k, n in counts['buildings_20m2_by_pixel_class'].items()}
    return counts


def markdown(report):
    out = ['# Outline reflections (CP20 reviewer set, measured)', '',
           'Every number below is measured from latest.json and the scene image; wording is agreement with the reviewer\'s note, never accuracy. '
           'Hints are not labels; nothing here was applied to a scene, a review or the eyes index.', '',
           f"Computed {report['computed_at']}, {report['version']}, code sha256 {report['code_sha256'][:16]}.", '']
    for case in report['cases']:
        out.append(f"## {case['scene_id']} / {case['object_id']}")
        out.append('')
        if case.get('status') == 'object_missing':
            out.append('Object absent from latest.json.')
            out.append('')
            continue
        for line in case['reflection']:
            out.append(f'- {line}')
        out.append('')
    if report.get('population'):
        p = report['population']
        out += ['## Population counts (JSON-only scan, every latest.json)', '',
                f"- {p['scenes']} scenes, {p['live_buildings']} live buildings; {p['buildings_under_1m2']} under 1 m² (the post-assembly floor the spec proposes would drop them from new extractions).",
                f"- {p['roof_parts_under_gate']} of {p['roof_parts_any']} roof models sit on buildings under {DESIGN['roof_fit_gate_m2']:.0f} m² (a roof-fitting gate there would leave them without a roof model; measured, not applied).",
                f"- {p['clipped_sam3_buildings']} sam3 buildings carry clipped_by_frame; {p['clipped_sam3_under_gate']} of them are under {DESIGN['roof_fit_gate_m2']:.0f} m² and {p['clipped_sam3_under_gate_covered_80']} of those are 80% covered by a larger live building (the remnant-takeover candidate the spec measures).",
                '- noisy_outline share among buildings of 20 m² or more, by pixel class: ' + ', '.join(f"{k} m/px {p['noisy_share_by_pixel_class'][k]} ({p['noisy_by_pixel_class'].get(k, 0)}/{p['buildings_20m2_by_pixel_class'][k]})" for k in sorted(p['buildings_20m2_by_pixel_class'])) + ' (reported per class, not compared across).',
                '']
    out += ['## Spec items measured here, not applied', '',
            '- A roof-fitting gate for buildings under 6 m² or with a fragment structure hint (count above).',
            '- A remnant takeover in scene_expand when a clipped seed under 6 m² is 80% covered by a new candidate (count above).',
            '- A shadow-edge detector: the per-edge luminance steps above are the only measured evidence; the noisy_outline sentence says "check the shadow side" and nothing more.',
            '- The squared candidate (orthogonalise) is shown as a dashed overlay in the editor when it exists and applied only by the reviewer\'s button; where the gate refused, the repair path is the redraw.', '']
    return '\n'.join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--case', action='append', required=True, help='scene_id:object_id (repeatable)')
    parser.add_argument('--root', default='.')
    parser.add_argument('--out', default=None, help=f'folder under {REFLECTIONS_ROOT} (default: today)')
    parser.add_argument('--population', action='store_true', help='JSON-only counts over every latest.json')
    args = parser.parse_args(argv)
    root = Path(args.root)
    out = Path(args.out) if args.out else REFLECTIONS_ROOT / dt.date.today().isoformat()
    if REFLECTIONS_ROOT.resolve() not in out.resolve().parents and out.resolve() != REFLECTIONS_ROOT.resolve():
        raise SystemExit(f'--out must sit under {REFLECTIONS_ROOT}')
    cases = []
    for item in args.case:
        scene_id, _, object_id = item.partition(':')
        if not scene_id or not object_id:
            raise SystemExit(f'--case wants scene_id:object_id, got {item!r}')
        cases.append(reflect(root, scene_id, object_id))
    report = {'version': VERSION, 'note': NOTE, 'computed_at': dt.datetime.now(dt.UTC).isoformat(), 'code_sha256': code_sha256(),
              'design': DESIGN, 'ortho_gates': ORTHO, 'cases': cases, 'population': population(root) if args.population else None}
    out.mkdir(parents=True, exist_ok=True)
    (out / 'reflections.json').write_text(json.dumps(report, indent=1, sort_keys=True, default=str) + '\n')
    (out / 'REFLECTION.md').write_text(markdown(report))
    for case in cases:
        print(f"{case['scene_id']}/{case['object_id']}: {', '.join(case.get('editor_hints', [])) or case.get('status')}")
    print(f'wrote {out / "reflections.json"} and {out / "REFLECTION.md"}')


if __name__ == '__main__':
    main()
