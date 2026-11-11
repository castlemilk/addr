"""Conservative, image-conditioned cleanup of *proposed* roof envelopes.

This is a geometry prior after segmentation, not a learned model or an approval.
Keep the raw mask geometry so uncertain inferred continuity remains reviewable.
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw
from scipy.optimize import least_squares
from shapely.geometry import LineString, Polygon, box

VERSION = 'roof-envelope-v5'


def _bounded_eaves(ring, tolerance, max_shift, fixed_points):
    """Fit long runs jointly; split only runs that exceed the uncertainty budget.

    Intersecting independent fitted lines can displace an acute corner enough
    to reject the entire outline. Shared, bounded corners avoid that failure.
    All returned candidates still pass the caller's exact distance/area gates.
    """
    original = Polygon(ring)
    coarse = np.asarray(original.simplify(tolerance).exterior.coords[:-1])
    if len(coarse) > 48:
        return None
    indices = [int(np.argmin(np.linalg.norm(ring-point, axis=1))) for point in coarse]
    # A coarse simplification may skip an image-cut endpoint by one pixel.
    # Insert those exact source points before fixing them during the solve.
    indices = sorted(set(indices) | {int(np.argmin(np.linalg.norm(ring-point, axis=1))) for point in fixed_points})
    groups = []
    for j, start in enumerate(indices):
        end = indices[(j+1) % len(indices)]
        if end <= start:
            end += len(ring)
        groups.append(ring[np.arange(start, end+1) % len(ring)])
    for _ in range(12):
        corners = np.array([g[0] for g in groups])
        free = np.array([not any(np.linalg.norm(p-fixed) < 1e-8 for fixed in fixed_points) for p in corners])
        if not free.any():
            return corners
        samples, owners = [], []
        for i, group in enumerate(groups):
            line = LineString(group)
            count = max(3, min(512, int(line.length/.15)))
            samples.extend(line.interpolate(t).coords[0] for t in np.linspace(0, line.length, count))
            owners.extend([i]*count)
        samples, owners = np.asarray(samples), np.asarray(owners)
        count = len(corners)

        def decode(parameters, corners=corners, free=free):
            points = corners.copy()
            points[free] = parameters.reshape(-1, 2)
            return points

        def residual(parameters, decode=decode, owners=owners, samples=samples, count=count, corners=corners):
            points = decode(parameters)
            a = points[owners]
            direction = points[(owners+1) % count]-a
            along = np.clip(np.sum((samples-a)*direction, axis=1)/np.maximum(np.sum(direction**2, axis=1), 1e-10), 0, 1)
            delta = samples-a-along[:, None]*direction
            return np.r_[delta.flatten(), np.maximum(0, np.linalg.norm(delta, axis=1)-max_shift*.85)*10,
                         (points-corners).flatten()*.05]

        try:
            fit = least_squares(residual, corners[free].flatten(), max_nfev=40,
                                bounds=((corners[free]-max_shift*.7).flatten(), (corners[free]+max_shift*.7).flatten()))
        except (ValueError, ArithmeticError):
            return None
        corners = decode(fit.x)
        candidate = Polygon(corners)
        if (candidate.is_valid and candidate.area > 0
                and candidate.boundary.hausdorff_distance(original.boundary) <= max_shift
                and candidate.symmetric_difference(original).area/original.area <= .03):
            return corners
        # Preserve local notches that cannot be explained by one straight run,
        # without throwing away the good fit along every other eave.
        choices = []
        for i, group in enumerate(groups):
            if len(group) < 3:
                continue
            a, b = corners[i], corners[(i+1) % count]
            d = b-a
            along = np.clip((group[1:-1]-a) @ d/max(float(d @ d), 1e-10), 0, 1)
            errors = np.linalg.norm(group[1:-1]-a-along[:, None]*d, axis=1)
            index = int(np.argmax(errors))+1
            choices.append((float(errors[index-1]), i, index))
        if not choices:
            break
        _, i, index = max(choices)
        group = groups[i]
        groups[i:i+1] = [group[:index+1], group[index:]]
    return None


def _fitted_eaves(ring, tolerance):
    """Fit whole eave runs, with equal weight per metre rather than per pixel step."""
    coarse = np.asarray(Polygon(ring).simplify(tolerance).exterior.coords[:-1])
    indices = [int(np.argmin(np.linalg.norm(ring-point, axis=1))) for point in coarse]
    groups = []
    for j, start in enumerate(indices):
        end = indices[(j+1) % len(indices)]
        if end <= start:
            end += len(ring)
        groups.append(ring[np.arange(start, end+1) % len(ring)])
    # RDP can leave a long eave as several almost collinear chords. Fit their
    # original samples together, retaining substantial corners and recesses.
    while len(groups) > 3:
        for i, group in enumerate(groups):
            previous = groups[i-1]
            a, b = previous[-1]-previous[0], group[-1]-group[0]
            if np.dot(a, b) > np.linalg.norm(a)*np.linalg.norm(b)*np.cos(np.deg2rad(18)):
                groups[i-1] = np.concatenate([previous, group[1:]])
                groups.pop(i)
                break
        else:
            break
    lines = []
    for group in groups:
        line = LineString(group)
        count = max(3, min(1024, int(line.length/.15)))
        samples = np.asarray([line.interpolate(t).coords[0] for t in np.linspace(0, line.length, count)])
        center = samples.mean(axis=0)
        _, _, axes = np.linalg.svd(samples-center, full_matrices=False)
        normal = axes[-1]
        lines.append((normal, float(normal @ center)))
    corners = []
    for i, (normal, offset) in enumerate(lines):
        matrix = np.asarray([lines[i-1][0], normal])
        if abs(np.linalg.det(matrix)) < .05:
            return None  # Near-parallel fits must not send corners far away.
        corners.append(np.linalg.solve(matrix, [lines[i-1][1], offset]))
    return np.asarray(corners)


def straighten_roof_boundary(polygon, image_size, frame):
    """Bounded straight-line proposal; openings and the image crop stay exact.

    Each side gets its own orientation. This does not assume a rectangular or
    orthogonal building. Metric/area gates reject fits that erase real wings.
    """
    frame = np.asarray(frame, dtype=float)
    scale = float(np.linalg.svd(frame[:2], compute_uv=False).max())
    max_shift = min(.45, 4*scale)
    ring = np.asarray(polygon.exterior.coords[:-1]) @ frame[:2] + frame[2]
    pixels = np.asarray(polygon.exterior.coords[:-1])
    crop_points = ring[np.any((np.abs(pixels) < 1e-8) | (np.abs(pixels-np.asarray(image_size)) < 1e-8), axis=1)]
    original = Polygon(ring)
    holes = [list(h.coords) for h in polygon.interiors]
    inverse = np.linalg.inv(frame[:2])
    bounds = box(0, 0, *image_size)
    best, best_score = polygon, (len(ring), 0.)
    deviation, area_change = 0., 0.
    # Test multiple scales: a rejected coarse fit must not leave all mask steps
    # intact. Choose the simplest fit within both bounds, then the closest one.
    alternatives = []
    for factor in (.25, .5, .75, 1., 1.125, 1.5):
        tolerance = max_shift*factor
        alternatives.append(('fitted-eave-runs', _fitted_eaves(ring, tolerance)))
        # One unstable fitted corner must not retain every pixel stair-step.
        # Chord simplification is subject to the same metric and area gates.
        alternatives.append(('bounded-chords', np.asarray(original.simplify(tolerance).exterior.coords[:-1])))
    for factor in (1., 1.5, 3.):
        alternatives.append(('bounded-shared-eaves', _bounded_eaves(ring, max_shift*factor, max_shift, crop_points)))
    method = 'unchanged-detector-boundary'
    for fit_method, corners in alternatives:
        if corners is None or not np.isfinite(corners).all():
            continue
        candidate_pixels = (corners-frame[2]) @ inverse
        for axis, extent in enumerate(image_size):
            for edge in (0, extent):
                candidate_pixels[np.abs(candidate_pixels[:, axis]-edge) < 1e-7, axis] = edge
        candidate = Polygon(candidate_pixels)
        if not candidate.is_valid or candidate.area <= 0:
            continue
        candidate = candidate.intersection(bounds)
        if candidate.geom_type != 'Polygon' or candidate.is_empty:
            continue
        candidate = Polygon(candidate.exterior, holes)
        if not candidate.is_valid:
            continue
        fitted_ring = np.asarray(candidate.exterior.coords[:-1]) @ frame[:2] + frame[2]
        fitted = Polygon(fitted_ring)
        shift = original.boundary.hausdorff_distance(fitted.boundary)
        area = original.symmetric_difference(fitted).area / max(original.area, 1e-9)
        score = (len(fitted_ring), area)
        if shift <= max_shift+1e-8 and area <= .03 and score < best_score:
            best, best_score = candidate, score
            method = fit_method
            deviation, area_change = shift, area
    return best, {'method': method, 'input_vertices': len(ring),
                  'output_vertices': len(best.exterior.coords)-1,
                  'max_deviation_m': round(deviation, 4), 'deviation_limit_m': round(max_shift, 4),
                  'area_change_fraction': round(area_change, 5), 'area_change_limit': .03}


def _pixels(region, image):
    mask = Image.new('L', image.size)
    draw = ImageDraw.Draw(mask)
    parts = [region] if region.geom_type == 'Polygon' else list(getattr(region, 'geoms', []))
    for p in parts:
        if p.geom_type != 'Polygon':
            continue
        draw.polygon(list(p.exterior.coords), fill=1)
        for h in p.interiors:
            draw.polygon(list(h.coords), fill=0)
    return np.asarray(image.convert('RGB'), dtype=float)[np.asarray(mask) > 0]


def refine_roof_surface(polygon, image, to_meters, *, canopies=()):
    """Remove small irregular same-material gaps; keep real openings/notches.

    Larger, regular or contrasting gaps remain untouched. Simplification is
    bounded in metres and area; it does not bridge separate buildings or impose
    a rectangle on L-shaped roofs. No review/visibility flags are generated.
    """
    frame = np.asarray(to_meters, dtype=float)
    if frame.shape != (3, 2) or not np.isfinite(frame).all():
        raise ValueError('A valid metric image frame is required')
    pixel_area = abs(float(np.linalg.det(frame[:2])))
    if pixel_area <= 1e-10:
        raise ValueError('A valid metric image frame is required')
    scale = float(np.linalg.svd(frame[:2], compute_uv=False).max())
    shell = Polygon(polygon.exterior)
    holes, decisions = [], []
    filled_area = 0.
    for interior in polygon.interiors:
        hole = Polygon(interior)
        area = hole.area * pixel_area
        rectangularity = hole.area / max(hole.minimum_rotated_rectangle.area, 1e-9)
        eligible = (area <= min(8., shell.area * pixel_area * .05)
                    and (area <= .3 or rectangularity < .82)
                    and filled_area + hole.area <= shell.area * .1)
        match = False
        if eligible:
            inside = _pixels(hole, image)
            outside = _pixels(hole.buffer(max(2., .5/scale)).intersection(polygon), image)
            if len(inside) and len(outside) >= 8:
                # Compare material chromaticity separately from illumination.
                # A darker tile patch can belong to the same roof; green foliage
                # or a blue courtyard must not become an invented roof surface.
                a, b = np.median(inside, axis=0), np.median(outside, axis=0)
                chroma = np.linalg.norm((a-a.mean())-(b-b.mean()))
                def green(rgb):
                    return (rgb[1]-(rgb[0]+rgb[2])/2)/max(30., rgb.mean())
                match = chroma <= 14 and abs(a.mean()-b.mean()) <= 35 and abs(green(a)-green(b)) <= .12
        if match:
            filled_area += hole.area
        else:
            holes.append(list(interior.coords))
        decisions.append({'area_m2': round(area, 4), 'filled': bool(match),
                          'reason': 'small-irregular-same-material' if match else 'opening-or-uncertain'})
    cleaned = Polygon(polygon.exterior, holes)
    straightened, straightening = straighten_roof_boundary(cleaned, image.size, frame)
    from .roof_boundary import regularize_eaves
    angular, construction = regularize_eaves(cleaned, image.size, frame, canopies)
    cleaned = angular if construction['accepted'] else straightened
    metric_raw = Polygon(np.asarray(polygon.exterior.coords) @ frame[:2]+frame[2])
    metric_fitted = Polygon(np.asarray(cleaned.exterior.coords) @ frame[:2]+frame[2])
    raw_ring = [[round(x, 2), round(y, 2)] for x, y in polygon.exterior.coords[:-1]]
    raw_holes = [[[round(x, 2), round(y, 2)] for x, y in h.coords[:-1]] for h in polygon.interiors]
    return cleaned, {'method': VERSION, 'raw_ring': raw_ring, 'raw_holes': raw_holes,
                     'holes': decisions, 'filled_area_m2': round(filled_area*pixel_area, 4),
                     'straightening': straightening,
                     'construction': construction,
                     'outer_deviation_m': round(metric_raw.boundary.hausdorff_distance(metric_fitted.boundary), 4)}
