"""Tentative unit partitions from repeated rooftop fixture layouts.

Partitions are an allocation overlay, never roof creases, addresses, party walls
or legal boundaries. Repeated HVAC equipment also occurs on a single building.
"""
from __future__ import annotations

import cv2
import numpy as np
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import split, unary_union

VERSION = 'roof-unit-patterns-v1'


def fixture_candidates(obj, scene, image):
    """Small contrast islands; their semantic class is deliberately unknown."""
    polygon = Polygon(obj['ring'], obj.get('holes', []))
    frame = np.asarray(scene['pixel_to_meters'])
    mpp = float(np.sqrt(abs(np.linalg.det(frame[:2]))))
    gray = cv2.cvtColor(np.asarray(image.convert('RGB')), cv2.COLOR_RGB2GRAY)
    mask = np.zeros(gray.shape, np.uint8)
    cv2.fillPoly(mask, [np.rint(obj['ring']).astype(np.int32)], 255)
    for hole in obj.get('holes', []):
        cv2.fillPoly(mask, [np.rint(hole).astype(np.int32)], 0)
    # Solar-bank cells are repeated objects too, but not one fixture per unit.
    for other in scene.get('objects', []):
        if (other.get('kind') in ('solar_panel', 'tree') and other.get('source') == 'sam3-text-onnx'
                and other.get('status') == 'candidate'):
            cv2.fillPoly(mask, [np.rint(other['ring']).astype(np.int32)], 0)
    radius = max(3, int(round(2.5/mpp)) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius, radius))
    contrast = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kernel)
    binary = ((contrast > 28) & (mask > 0)).astype(np.uint8)
    count, _, stats, centers = cv2.connectedComponentsWithStats(binary)
    result = []
    for i in range(1, count):
        _, _, width, height, pixels = stats[i]
        area = pixels*mpp*mpp
        if not (.15 <= area <= 6 and .3 <= min(width, height)*mpp and max(width, height)/min(width, height) <= 3):
            continue
        point = centers[i]
        if not polygon.buffer(-max(2., .5/mpp)).contains(Point(point)):
            continue
        result.append({'point': point.tolist(), 'area_m2': round(area, 3),
                       'kind': 'unclassified_fixture', 'source': 'contrast-island'})
    return result[:100]


def detect_equipment(obj, scene, image, segmenter):
    """Use the installed SAM text model on a roof crop, once per image encoding.

    Classifications remain model candidates. Small contrast islands continue to
    be useful as a fallback when these prompts resolve no equipment at this GSD.
    """
    from .segmentation import mask_to_polygons
    polygon = Polygon(obj['ring'], obj.get('holes', []))
    left, top, right, bottom = polygon.bounds
    left, top = max(0, int(left)-8), max(0, int(top)-8)
    right, bottom = min(image.width, int(right)+9), min(image.height, int(bottom)+9)
    crop = image.crop((left, top, right, bottom))
    features = segmenter.encode_image(crop)
    frame = np.asarray(scene['pixel_to_meters'])
    mpp = float(np.sqrt(abs(np.linalg.det(frame[:2]))))
    panels = unary_union([Polygon(o['ring'], o.get('holes', [])) for o in scene.get('objects', [])
                          if o.get('kind') in ('solar_panel', 'tree') and o.get('source') == 'sam3-text-onnx'
                          and o.get('status') == 'candidate'])
    found = []
    for kind, prompt in (('air_conditioner', 'rooftop air conditioning unit'), ('exhaust', 'roof exhaust vent')):
        for detection in segmenter.proposals(features, prompt, crop.size, threshold=.5, max_proposals=30):
            for region in mask_to_polygons(detection.mask, min_area_px=4, simplify_px=.5):
                from shapely.affinity import translate
                region = translate(region, xoff=left, yoff=top)
                if not .15 <= region.area*mpp*mpp <= 8 or region.intersection(polygon).area/region.area < .9:
                    continue
                if not panels.is_empty and region.intersection(panels).area/region.area > .2:
                    continue
                p = region.centroid
                if any(np.linalg.norm(np.array(f['point'])-[p.x, p.y])*mpp < .7 for f in found):
                    continue
                found.append({'point': [p.x, p.y], 'area_m2': round(region.area*mpp*mpp, 3),
                              'kind': kind, 'score': float(detection.score), 'source': 'sam3-equipment-candidate'})
    return found


def equipment_key(obj, scene):
    from .scene_batch import signature
    return signature([VERSION, scene['image_sha256'], scene['model_signature'], obj['id']])


def cached_equipment(root, obj, scene, directory=None):
    import json
    folder = directory if directory is not None else root/'data/roof-review-loop/equipment'
    path = folder/f'{equipment_key(obj, scene)}.json'
    if not path.exists():
        return None
    saved = json.loads(path.read_text())
    if saved.get('key') != equipment_key(obj, scene):
        raise ValueError('Equipment observations belong to a different image or model.')
    return saved['fixtures']


def partition_hypothesis(ring, holes, fixtures):
    """Infer parallel cuts between a regular fixture row in metric coordinates."""
    polygon = Polygon(ring, holes)
    empty = {'version': VERSION, 'status': 'insufficient_pattern', 'partitions': [], 'cuts': [],
             'confidence': 'unverified', 'roof_surface_unchanged': True, 'unit_count': None,
             'reason': 'Need at least three repeated fixtures with consistent spacing; equipment alone cannot establish units.'}
    if not polygon.is_valid or polygon.area < 60:
        return empty
    rect = np.array(polygon.minimum_rotated_rectangle.exterior.coords)[:4]
    vectors = np.roll(rect, -1, axis=0)-rect
    lengths = np.linalg.norm(vectors, axis=1)
    if max(lengths)/min(lengths) < 1.6 or polygon.area/Polygon(rect).area < .8:
        return empty
    axis = vectors[int(np.argmax(lengths))]/max(lengths)
    normal = np.array([-axis[1], axis[0]])
    candidates = [f for f in fixtures if f.get('kind') in ('unclassified_fixture', 'air_conditioner', 'exhaust', 'chimney')
                  and polygon.contains(Point(f['point']))]
    if not 3 <= len(candidates) <= 100:
        return empty
    xy = np.array([f['point'] for f in candidates])
    along, across = xy @ axis, xy @ normal
    best = None
    for seed in range(len(candidates)):
        # Separate rows across the building; do not count two fixtures per unit
        # as two units when they belong to different parallel rows.
        ids = np.flatnonzero(np.abs(across-across[seed]) <= .8)
        ids = ids[np.argsort(along[ids])]
        if not 3 <= len(ids) <= 12:
            continue
        positions = along[ids]
        gaps = np.diff(positions)
        area = np.array([candidates[i]['area_m2'] for i in ids])
        extent = np.asarray(ring) @ axis
        spacing = float(np.median(gaps))
        if (not 3 <= spacing <= 20 or min(gaps) < spacing*.7 or max(gaps) > spacing*1.3
                or np.std(gaps)/np.mean(gaps) > .16 or min(area) <= 0 or max(area)/min(area) > 3
                or positions[0]-min(extent) > spacing*1.2 or max(extent)-positions[-1] > spacing*1.2):
            continue
        quality = (len(ids), -float(np.std(gaps)/np.mean(gaps)))
        if best is None or quality > best[0]:
            best = quality, ids, positions, spacing
    if best is None:
        return empty
    _, ids, positions, spacing = best
    pieces, cuts = [polygon], []
    for position in (positions[:-1]+positions[1:])/2:
        center = axis*position+normal*float(np.mean(across[ids]))
        line = LineString([center-normal*max(lengths)*2, center+normal*max(lengths)*2])
        clipped = line.intersection(polygon)
        if clipped.geom_type != 'LineString':
            return empty  # Courtyard or disconnected section: do not bridge it.
        pieces = [part for piece in pieces for part in split(piece, line).geoms]
        cuts.append([list(clipped.coords[0]), list(clipped.coords[-1])])
    if len(pieces) != len(ids) or min(p.area for p in pieces) < 18:
        return empty
    union = unary_union(pieces)
    if union.symmetric_difference(polygon).area > 1e-6 or sum(p.area for p in pieces)-union.area > 1e-6:
        return empty
    return {**empty, 'status': 'candidate_partition', 'estimated_sections': len(pieces),
            'fixture_indices': ids.tolist(), 'spacing_m': round(spacing, 2),
            'boundary_uncertainty_m': round(spacing*.25, 2), 'cuts': cuts,
            'partitions': [{'ring': list(map(list, p.exterior.coords[:-1])),
                            'holes': [list(map(list, h.coords[:-1])) for h in p.interiors]} for p in pieces],
            'reason': 'Repeated fixture layout suggests these divisions. Confirm unit count and wall positions using addresses, entrances or subdivision data; fixtures may serve a single building.'}


def seam_partitions(ring, holes, observations):
    """Repeated cross-roof divisions are another cue, not proved party walls."""
    polygon = Polygon(ring, holes)
    if not polygon.is_valid or polygon.area < 60 or polygon.interiors:
        return None
    rect = np.array(polygon.minimum_rotated_rectangle.exterior.coords)[:4]
    vectors = np.roll(rect, -1, axis=0)-rect
    lengths = np.linalg.norm(vectors, axis=1)
    if max(lengths)/min(lengths) < 2 or polygon.area/Polygon(rect).area < .8:
        return None
    axis = vectors[int(np.argmax(lengths))]/max(lengths)
    normal = np.array([-axis[1], axis[0]])
    extent = np.array(ring) @ axis
    raw = []
    for observation in observations:
        points = np.asarray(observation['points'])
        vector = points[1]-points[0]
        length = np.linalg.norm(vector)
        if length < min(lengths)*.3 or abs(vector @ normal)/length < .97:
            continue
        position = float(points.mean(0) @ axis)
        if min(extent)+1 < position < max(extent)-1:
            raw.append((position, LineString(points)))
    clusters = []
    for position, line in sorted(raw, key=lambda item: item[0]):
        if not clusters or position-np.mean([p for p, _ in clusters[-1]]) > .6:
            clusters.append([])
        clusters[-1].append((position, line))
    positions = []
    for cluster in clusters:
        # Project finite segments to one normal coordinate so duplicate edges
        # cannot count twice towards cross-roof coverage.
        intervals = [LineString([(float(np.array(p) @ normal), 0) for p in line.coords]) for _, line in cluster]
        if unary_union(intervals).length >= min(lengths)*.55:
            positions.append(float(np.mean([p for p, _ in cluster])))
    if not 3 <= len(positions) <= 11:
        return None
    gaps = np.diff(positions)
    spacing = float(np.median(gaps))
    if (not 3 <= spacing <= 20 or np.std(gaps)/np.mean(gaps) > .18
            or not spacing*.4 <= positions[0]-min(extent) <= spacing*1.6
            or not spacing*.4 <= max(extent)-positions[-1] <= spacing*1.6):
        return None
    pieces, cuts = [polygon], []
    for position in positions:
        center = axis*position+normal*float(np.array(polygon.centroid.coords[0]) @ normal)
        line = LineString([center-normal*max(lengths)*2, center+normal*max(lengths)*2])
        clipped = line.intersection(polygon)
        if clipped.geom_type != 'LineString':
            return None
        pieces = [part for piece in pieces for part in split(piece, line).geoms]
        cuts.append([list(clipped.coords[0]), list(clipped.coords[-1])])
    union = unary_union(pieces)
    if (len(pieces) != len(positions)+1 or min(p.area for p in pieces) < 18
            or union.symmetric_difference(polygon).area > 1e-6 or sum(p.area for p in pieces)-union.area > 1e-6):
        return None
    return {'version': VERSION, 'status': 'candidate_partition', 'confidence': 'unverified', 'unit_count': None,
            'roof_surface_unchanged': True, 'estimated_sections': len(pieces), 'cuts': cuts,
            'evidence_kind': 'repeated_transverse_seams', 'spacing_m': round(spacing, 2),
            'boundary_uncertainty_m': .6,
            'partitions': [{'ring': list(map(list, p.exterior.coords[:-1])), 'holes': []} for p in pieces],
            'reason': 'Repeated cross-roof seams suggest these sections. They may mark levels, material joints or shared walls; confirm unit count and boundaries using entrances, addresses or subdivision data.'}


def review_units(obj, scene, image, equipment=None, observations=None):
    from .scene_self_review import local_outline
    fixtures = equipment if equipment else fixture_candidates(obj, scene, image)
    frame = np.asarray(scene['pixel_to_meters'])
    metric = [{**f, 'point': (np.r_[f['point'], 1.] @ frame).tolist()} for f in fixtures]
    ring, holes = local_outline(obj, scene)
    result = partition_hypothesis(ring, holes, metric)
    if result['status'] != 'candidate_partition' and observations is not None:
        panels = unary_union([Polygon(o['ring'], o.get('holes', [])) for o in scene.get('objects', [])
                              if o.get('kind') in ('solar_panel', 'tree') and o.get('source') == 'sam3-text-onnx'
                              and o.get('status') == 'candidate'])
        clean = []
        for observation in observations:
            pixels = np.c_[observation['points'], np.ones(2)] @ np.asarray(scene['meters_to_pixel'])
            line = LineString(pixels)
            if panels.is_empty or line.intersection(panels).length/max(line.length, 1e-6) < .2:
                clean.append(observation)
        result = seam_partitions(ring, holes, clean) or result
    to_pixels = np.asarray(scene['meters_to_pixel'])
    result['cuts_pixels'] = [(np.c_[cut, np.ones(2)] @ to_pixels).tolist() for cut in result.pop('cuts')]
    result['fixtures'] = fixtures
    result['detector'] = 'sam3-equipment-candidates' if equipment else 'unclassified-contrast-islands'
    result['partition_coordinate_system'] = 'scene-local-metres'
    return result


if __name__ == '__main__':
    import argparse
    import hashlib
    import json
    from pathlib import Path

    from .roof_review_loop import check_input, run_path
    from .sam3 import Sam3TextSegmenter
    from .scene_training import atomic_json

    parser = argparse.ArgumentParser(description='Cache roof-scoped equipment hypotheses from frozen review inputs.')
    parser.add_argument('--run', required=True)
    parser.add_argument('--objects', type=int, nargs='+', required=True)
    args = parser.parse_args()
    root = Path.cwd()
    directory = run_path(root, args.run)
    report = json.loads((directory/'report.json').read_text())
    if not 1 <= len(args.objects) <= 4 or any(i < 0 or i >= len(report['rows']) for i in args.objects):
        parser.error('Choose 1–4 existing roof indices.')
    models = root/'scripts/ml/models/sam3'
    model_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in models.iterdir()
                    if p.suffix in ('.onnx', '.json')}
    segmenter = Sam3TextSegmenter(models)
    for index in args.objects:
        scene, obj, image = check_input(root, directory, index, report['rows'][index])
        fixtures = detect_equipment(obj, scene, image, segmenter)
        key = equipment_key(obj, scene)
        path = root/'data/roof-review-loop/equipment'/f'{key}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {'version': VERSION, 'key': key, 'scene_id': scene['id'], 'object_id': obj['id'],
                   'image_sha256': scene['image_sha256'], 'model_signature': scene['model_signature'],
                   'detector_weights': model_hashes, 'fixtures': fixtures, 'human_approved': False}
        atomic_json(path, payload)
        print(json.dumps({'index': index, 'fixtures': len(fixtures), 'artifact': str(path)}), flush=True)
