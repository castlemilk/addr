"""Audit the geography of a whole scene image against the cohort tile splits."""
from __future__ import annotations

import hashlib
import math

import numpy as np
from shapely.geometry import Polygon, box

from .tiles import WORLD_SPAN_M, lonlat_to_tile_xy


def group_split(group):
    bucket = int(hashlib.sha256(group.encode()).hexdigest()[:8], 16) % 10
    return 'test' if bucket == 0 else 'validation' if bucket == 1 else 'train'


def image_split_audit(model, declared_split):
    """Check every tile touched by the pixel frame, not only the host roof pin.

    A local-metre affine frame shares the centroid's ground Mercator scale with
    capture/reprojection. Polygon intersections avoid counting empty corners of
    a rotated frame's bounding box. A line touch has no image area and is ignored.
    """
    try:
        width, height = model['image_width'], model['image_height']
        center = model['centroid']
        lon, lat = center['lon'], center['lat']
        frame = np.asarray(model['pixel_to_meters'], dtype=float)
        values = [width, height, lon, lat]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
            raise ValueError('Invalid image dimensions or centroid')
        if not (0 < width <= 32768 and 0 < height <= 32768 and -180 < lon < 180 and -85 < lat < 85):
            raise ValueError('Unsupported image frame')
        if frame.shape != (3, 2) or not np.isfinite(frame).all() or abs(np.linalg.det(frame[:2])) < 1e-12:
            raise ValueError('Missing or degenerate image transform')
        scale = WORLD_SPAN_M * math.cos(math.radians(lat))
        origin = np.array(lonlat_to_tile_xy(lon, lat, 17))
        corners = np.c_[[[0, 0], [width, 0], [width, height], [0, height]], np.ones(4)] @ frame
        # Keep coordinates relative to a nearby integer origin for stable areas.
        anchor = np.floor(origin)
        tiles = origin-anchor + corners * np.array([1, -1]) * 2**17 / scale
        polygon = Polygon(tiles)
        xmin, ymin, xmax, ymax = polygon.bounds
        if not polygon.is_valid or polygon.area <= 0 or (math.ceil(xmax)-math.floor(xmin)) * (math.ceil(ymax)-math.floor(ymin)) > 4096:
            raise ValueError('Invalid or excessive image extent')
        groups = []
        for x in range(math.floor(xmin), math.ceil(xmax)):
            for y in range(math.floor(ymin), math.ceil(ymax)):
                if polygon.intersection(box(x, y, x+1, y+1)).area <= 1e-12:
                    continue
                gx, gy = int(anchor[0])+x, int(anchor[1])+y
                if not (0 <= gx < 2**17 and 0 <= gy < 2**17):
                    raise ValueError('Image frame leaves the map extent')
                key = f'z17:{gx}:{gy}'
                groups.append({'group': key, 'split': group_split(key)})
        splits = sorted({g['split'] for g in groups})
        assigned = 'validation' if declared_split == 'val' else declared_split
        if splits == [assigned]:
            status, reason = 'verified', 'The whole image stays within its assigned split.'
        elif len(splits) > 1:
            status, reason = 'cross_split', 'The image includes more than one dataset split. Use a smaller capture inside one split.'
        else:
            status, reason = 'split_mismatch', 'The image geography does not match its assigned split. Check the capture and cohort assignment.'
        return {'status': status, 'groups': groups, 'splits': splits, 'reason': reason}
    except (KeyError, TypeError, ValueError, OverflowError):
        return {'status': 'invalid_frame', 'groups': [], 'splits': [],
                'reason': 'The full image geography could not be verified. Check its coordinate frame before exporting.'}
