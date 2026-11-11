"""Image-only proposal context. Never reads review decisions or height priors."""
from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image
from shapely import contains_xy
from shapely.geometry import Polygon, box

CONTEXT_FEATURES = ['touches_image_border', 'border_boundary_fraction',
                    'luminance_iqr', 'interior_gradient_median', 'green_excess_iqr',
                    'surround_luminance_contrast', 'surround_green_contrast']


def _iqr(values):
    return float(np.percentile(values, 75) - np.percentile(values, 25))


def _contrast(values, mask, background):
    return float(np.median(values[mask]) - np.median(values[background])) if background.any() else 0.


def image_context(objects, image):
    rgb = np.asarray(image.convert('RGB'), dtype=np.float64) / 255
    height, width = rgb.shape[:2]
    if min(height, width) < 2:
        raise ValueError('Proposal context needs an image at least two pixels wide and high')
    luma = rgb @ np.array([.2126, .7152, .0722])
    green = rgb[:, :, 1] - (rgb[:, :, 0] + rgb[:, :, 2]) / 2
    dy, dx = np.gradient(luma)
    gradient = np.hypot(dx, dy)
    extent = box(0, 0, width, height)
    border = extent.boundary.buffer(1.5)
    result = {}
    for obj in objects:
        if obj['id'] in result:
            raise ValueError('Duplicate context proposal identity')
        polygon = Polygon(obj['ring'], obj.get('holes'))
        if not polygon.is_valid or polygon.is_empty or not extent.covers(polygon):
            raise ValueError('Proposal context needs a valid outline inside the image')
        pad = float(np.clip(np.sqrt(polygon.area) * .15, 3, 12))
        # Only the outside neighbourhood is background; interior gaps must not
        # silently become colour evidence for the object or its surroundings.
        outer = Polygon(polygon.exterior)
        surround = outer.buffer(pad).difference(outer.buffer(1.5))
        minx, miny, maxx, maxy = outer.buffer(pad).intersection(extent).bounds
        x0, y0, x1, y1 = max(0, int(minx)), max(0, int(miny)), min(width, int(np.ceil(maxx))), min(height, int(np.ceil(maxy)))
        xs, ys = np.arange(x0, x1)[None, :] + .5, np.arange(y0, y1)[:, None] + .5
        mask = contains_xy(polygon, xs, ys)
        if not mask.any():
            raise ValueError('Proposal context has no visible image pixels')
        interior = contains_xy(polygon.buffer(-1), xs, ys)
        if not interior.any():
            interior = mask
        background = contains_xy(surround, xs, ys)
        light, excess = luma[y0:y1, x0:x1], green[y0:y1, x0:x1]
        boundary_fraction = polygon.exterior.intersection(border).length / polygon.exterior.length
        result[obj['id']] = [float(boundary_fraction > 0), float(boundary_fraction),
                             _iqr(light[mask]), float(np.median(gradient[y0:y1, x0:x1][interior])),
                             _iqr(excess[mask]), _contrast(light, mask, background), _contrast(excess, mask, background)]
    return result


def case_context(case, root=Path('.')):
    if case['split'] not in ('train', 'validation'):
        raise ValueError('Proposal context cannot inspect held-out test images')
    path = Path(case['image_path'])
    encoded = (path if path.is_absolute() else root/path).read_bytes()
    if hashlib.sha256(encoded).hexdigest() != case['image_sha256']:
        raise ValueError('Image bytes changed before proposal context extraction')
    with Image.open(BytesIO(encoded)) as image:
        return image_context(case['predictions'], image)
