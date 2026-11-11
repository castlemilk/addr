"""Read-only surrounding imagery in the saved scene's exact pixel frame.

The context endpoint only changes navigation and never extends saved labels.
Explicit inference expansion is handled separately by scene_expand; the source
capture and review files are immutable through this context path.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

from .imagery import PROVIDER_MAX_ZOOM, ImageryFetcher
from .tiles import lonlat_to_tile_xy

PADDING = 256


def context_image(scene, source, *, root=Path('.'), fetcher=None):
    region = scene.get('region') or {}
    provider, zoom = region.get('provider'), region.get('imagery_zoom')
    if provider not in ('google', 'esri') or not isinstance(zoom, int) or not 0 <= zoom <= PROVIDER_MAX_ZOOM[provider]:
        raise ValueError('Surrounding imagery needs a registered map capture')
    if (scene.get('image_width'), scene.get('image_height')) != (512, 512):
        raise ValueError('Unsupported source image dimensions')
    if hashlib.sha256(source).hexdigest() != scene['image_sha256']:
        raise ValueError('Source imagery changed')
    west, south, east, north = region['bounds']
    if not (-180 < west < east < 180 and -85 < south < north < 85):
        raise ValueError('Invalid source image bounds')
    left, top = np.asarray(lonlat_to_tile_xy(west, north, zoom))*256
    right, bottom = np.asarray(lonlat_to_tile_xy(east, south, zoom))*256
    step = np.array([right-left, bottom-top])/512
    left, top = np.array([left, top])-step*PADDING
    right, bottom = np.array([right, bottom])+step*PADDING
    x0, y0, x1, y1 = math.floor(left/256), math.floor(top/256), math.ceil(right/256), math.ceil(bottom/256)
    if not (0 <= x0 < x1 <= 2**zoom and 0 <= y0 < y1 <= 2**zoom) or (x1-x0)*(y1-y0) > 100:
        raise ValueError('Surrounding imagery exceeds the supported extent')
    tiles = [(x, y) for y in range(y0, y1) for x in range(x0, x1)]
    fetcher = fetcher or ImageryFetcher(provider=provider, cache_dir=root/'data/imagery', timeout=5, retries=1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        images = list(pool.map(lambda xy: fetcher._child(zoom, *xy), tiles))
    if any(image is None for image in images):
        raise ValueError('Surrounding imagery is unavailable')
    mosaic = Image.new('RGB', ((x1-x0)*256, (y1-y0)*256))
    for (x, y), image in zip(tiles, images, strict=True):
        mosaic.paste(image, ((x-x0)*256, (y-y0)*256))
    image = mosaic.transform((1024, 1024), Image.Transform.EXTENT,
                             (left-x0*256, top-y0*256, right-x0*256, bottom-y0*256), Image.Resampling.BICUBIC)
    # The central pixels must remain exactly the saved capture, even if the
    # provider later refreshes its tiles. The UI masks these pixels as well.
    with Image.open(io.BytesIO(source)) as original:
        if original.size != (512, 512):
            raise ValueError('Source image dimensions changed')
        image.paste(original.convert('RGB'), (PADDING, PADDING))
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    args = parser.parse_args()
    scene = json.loads(args.model.read_text())
    directory = args.model.parent.parent
    image = context_image(scene, (directory/f"{scene['image_sha256']}.png").read_bytes())
    image.save(sys.stdout.buffer, format='PNG')


if __name__ == '__main__':
    main()
