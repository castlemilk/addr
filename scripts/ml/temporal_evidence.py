"""Registered multi-date evidence. Temporal disagreement is never extra confidence."""
from __future__ import annotations

import copy
import hashlib

import cv2
import numpy as np
from PIL import Image
from scipy.optimize import linear_sum_assignment
from shapely.geometry import Polygon

VERSION = 'temporal-object-evidence-v1'


def register(reference, candidate):
    """Bounded similarity registration with independent residual checks."""
    a, b = np.asarray(reference.convert('RGB')), np.asarray(candidate.convert('RGB'))
    if a.shape != b.shape:
        return None, None, {'status': 'rejected', 'reason': 'Different image grids'}
    if hashlib.sha256(a.tobytes()).digest() == hashlib.sha256(b.tobytes()).digest():
        return None, None, {'status': 'duplicate', 'reason': 'Identical source pixels'}
    # RootSIFT and local contrast reduce date/provider illumination differences.
    gray = [cv2.createCLAHE(clipLimit=2., tileGridSize=(8, 8)).apply(
        cv2.GaussianBlur(cv2.cvtColor(v, cv2.COLOR_RGB2GRAY), (3, 3), .8)) for v in (a, b)]
    sift = cv2.SIFT_create(nfeatures=10000, contrastThreshold=.02)
    ka, da = sift.detectAndCompute(gray[0], None)
    kb, db = sift.detectAndCompute(gray[1], None)
    if da is None or db is None:
        return None, None, {'status': 'rejected', 'reason': 'Insufficient image features'}
    da = np.sqrt(da/(da.sum(axis=1, keepdims=True)+1e-8))
    db = np.sqrt(db/(db.sum(axis=1, keepdims=True)+1e-8))
    pairs = cv2.BFMatcher().knnMatch(db, da, k=2)
    matches = [p[0] for p in pairs if len(p) == 2 and p[0].distance < .75*p[1].distance]
    # Repeated destinations (tiles/vegetation textures) cannot establish a frame.
    unique = {}
    for m in sorted(matches, key=lambda m: m.distance):
        if np.linalg.norm(np.subtract(ka[m.trainIdx].pt, kb[m.queryIdx].pt)) <= 40:
            unique.setdefault(m.trainIdx, m)
    matches = list(unique.values())
    if len(matches) < 40:
        return None, None, {'status': 'rejected', 'reason': 'Too few independent registration matches', 'matches': len(matches)}
    src = np.float32([kb[m.queryIdx].pt for m in matches])
    dst = np.float32([ka[m.trainIdx].pt for m in matches])
    order = np.random.default_rng(17).permutation(len(src))
    check, fit = order[::4], np.delete(order, np.arange(0, len(order), 4))
    # Coarse registration tolerates parallax; only <=1.5px local observations
    # below can contribute to the pixel fusion or object agreement checks.
    transform, inliers = cv2.estimateAffinePartial2D(src[fit], dst[fit], method=cv2.RANSAC, ransacReprojThreshold=3.,
                                                   maxIters=4000, confidence=.999)
    if transform is None:
        return None, None, {'status': 'rejected', 'reason': 'Registration did not converge'}
    predicted = np.c_[src, np.ones(len(src))] @ transform.T
    residual = np.linalg.norm(predicted-dst, axis=1)
    verified = check[residual[check] <= 3]
    h, w = a.shape[:2]
    scale = float(np.linalg.norm(transform[0, :2]))
    angle = float(np.degrees(np.arctan2(transform[1, 0], transform[0, 0])))
    center_shift = float(np.linalg.norm(transform @ [w/2, h/2, 1]-[w/2, h/2]))
    coverage = float(cv2.contourArea(cv2.convexHull(dst[verified]))/(w*h)) if len(verified) >= 3 else 0
    report = {'status': 'accepted', 'matches': len(matches), 'fit_inliers': int(inliers.sum()),
              'check_matches': len(check), 'check_inliers': len(verified), 'check_coverage': round(coverage, 3),
              'check_median_residual_px': round(float(np.median(residual[verified])), 3) if len(verified) else None,
              'scale': scale, 'rotation_deg': angle, 'center_shift_px': center_shift, 'transform': transform.tolist()}
    if (inliers.sum() < 25 or len(verified) < 10 or len(verified)/len(check) < .45 or coverage < .2
            or not .98 <= scale <= 1.02 or abs(angle) > 2 or center_shift > 40):
        return None, None, {**report, 'status': 'rejected', 'reason': 'Alignment lacks distributed support or exceeds movement limits'}
    aligned = cv2.warpAffine(b, transform, (w, h), flags=cv2.INTER_LINEAR)
    valid = cv2.warpAffine(np.ones((h, w), np.uint8), transform, (w, h), flags=cv2.INTER_NEAREST).astype(bool)
    local = np.zeros((h, w), np.uint8)
    for x, y in dst[residual <= 1.5]:
        cv2.circle(local, (round(float(x)), round(float(y))), 48, 1, -1)
    valid &= local.astype(bool)
    return Image.fromarray(aligned), valid, report


def stable_composite(reference, aligned, valid):
    """Average only registered, photometrically stable pixels; retain reference elsewhere."""
    base = np.asarray(reference.convert('RGB'))
    gray = cv2.cvtColor(base, cv2.COLOR_RGB2GRAY).astype(float)
    total = base.astype(float)
    count = np.ones(gray.shape, np.float32)
    contributions = []
    for image, supported in zip(aligned, valid, strict=True):
        rgb = np.asarray(image.convert('RGB')).astype(float)
        source = cv2.cvtColor(rgb.astype(np.uint8), cv2.COLOR_RGB2GRAY).astype(float)
        normalized = np.clip(rgb + np.median(gray[supported]-source[supported]) if supported.any() else rgb, 0, 255)
        delta = np.max(np.abs(base.astype(float)-normalized), axis=2)
        stable = supported & (delta < 24)
        stable = cv2.erode(stable.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
        total[stable] += normalized[stable]
        count[stable] += 1
        contributions.append(float(stable.mean()))
    output = base.copy()
    used = count > 1
    output[used] = np.rint(total[used]/count[used, None]).astype(np.uint8)
    return Image.fromarray(output), used, {'combined_fraction': float(used.mean()), 'source_fractions': contributions,
                                         'resolution_increased': False, 'hidden_pixels_inpainted': False}


def match_objects(reference, other, kind):
    a = [o for o in reference if o['kind'] == kind]
    b = [o for o in other if o['kind'] == kind]
    if not a or not b:
        return {}
    pa = [Polygon(o['ring'], o.get('holes')) for o in a]
    pb = [Polygon(o['ring'], o.get('holes')) for o in b]
    ious = np.zeros((len(a), len(b)))
    for i, x in enumerate(pa):
        for j, y in enumerate(pb):
            if x.is_valid and y.is_valid and x.intersects(y):
                ious[i, j] = x.intersection(y).area/max(x.union(y).area, 1e-6)
    rows, columns = linear_sum_assignment(-ious)
    result = {}
    for i, j in zip(rows, columns, strict=True):
        if ious[i, j] < .3:
            continue
        ambiguous = np.count_nonzero(ious[i] >= .25) > 1 or np.count_nonzero(ious[:, j] >= .25) > 1
        result[a[i]['id']] = {'object': b[j], 'iou': float(ious[i, j]), 'ambiguous': bool(ambiguous)}
    return result


def reflect_objects(reference, captures, image_size, mpp):
    """Evidence and bounded roof candidates; trees always keep per-date geometry."""
    rows, proposals = [], []
    matches = [{kind: match_objects(reference, c['objects'], kind) for kind in ('building', 'tree', 'solar_panel')}
               for c in captures]
    for obj in reference:
        if obj['kind'] not in ('building', 'tree', 'solar_panel'):
            continue
        poly = Polygon(obj['ring'], obj.get('holes'))
        footprint = np.zeros((image_size[1], image_size[0]), np.uint8)
        cv2.fillPoly(footprint, [np.rint(obj['ring']).astype(np.int32)], 1)
        for hole in obj.get('holes', []):
            cv2.fillPoly(footprint, [np.rint(hole).astype(np.int32)], 0)
        observations, supported = [], []
        for c, by_kind in zip(captures, matches, strict=True):
            match = by_kind[obj['kind']].get(obj['id'])
            local = float(c['valid'][footprint.astype(bool)].mean()) if footprint.any() else 0
            observation = {'date': c['acquisition_date'], 'acquisition_id': c['acquisition_id'],
                           'local_registration_fraction': local, 'iou': match['iou'] if match else None,
                           'ambiguous': match['ambiguous'] if match else False}
            observations.append(observation)
            if match and not match['ambiguous'] and local >= .8 and match['iou'] >= .75:
                supported.append(Polygon(match['object']['ring'], match['object'].get('holes')))
        # Pixel equality across releases is screened by the caller. Multiple
        # detector proposals from one acquisition can never create extra votes.
        distinct = len({c['acquisition_id'] for c in captures}) == len(captures)
        agreed = len(supported) >= 2 and distinct
        row = {'object_id': obj['id'], 'kind': obj['kind'], 'observations': observations,
               'status': 'consistent' if agreed else 'needs_review', 'candidate': False,
               'reason': 'Agreement across independently dated views' if agreed else 'Missing, changed, occluded or ambiguous evidence',
               'calibrated_accuracy': None}
        if obj['kind'] == 'tree':
            row['reason'] = 'Canopies are compared by date; seasonal growth, pruning and shadow can change their outline.'
            row['geometry_policy'] = 'separate_per_date'
        if agreed and obj['kind'] == 'building':
            # A conservative intersection provides repeat-observed roof extent.
            # Reject topology, courtyard, area or position changes beyond budget.
            candidate = supported[0]
            for p in supported[1:]:
                candidate = candidate.intersection(p)
            holes = [Polygon(h) for h in obj.get('holes', [])]
            for hole in holes:
                candidate = candidate.difference(hole)
            accepted = (candidate.geom_type == 'Polygon' and candidate.is_valid and not candidate.is_empty
                        and len(candidate.interiors) == len(poly.interiors)
                        and candidate.symmetric_difference(poly).area/max(poly.area, 1) <= .08
                        and candidate.hausdorff_distance(poly)*mpp <= 1.)
            if accepted:
                proposal = copy.deepcopy(obj)
                proposal.update(ring=[list(p) for p in candidate.exterior.coords[:-1]],
                                holes=[[list(p) for p in h.coords[:-1]] for h in candidate.interiors],
                                source=VERSION, geometry_reviewed=False, status='candidate')
                for key in ('roof_annotation', 'roof_parts', 'roof_lines', 'roof_feedback', 'roof_line_edits'):
                    proposal.pop(key, None)
                proposals.append(proposal)
                row['candidate'] = True
            else:
                row['reason'] += '; proposed boundary exceeds the geometry budget, so keep it for review.'
        rows.append(row)
    return rows, proposals
