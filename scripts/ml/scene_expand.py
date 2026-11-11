"""Extend inference into registered context without resampling the saved capture."""
from __future__ import annotations

import copy
import hashlib
import io

import numpy as np

from .scene_context import PADDING, context_image
from .tiles import lonlat_to_tile_xy, tile_xy_to_lonlat


def expanded_source(scene, source, **kwargs):
    image = context_image(scene, source, **kwargs)
    if not scene.get('model_signature'):
        raise ValueError('Expansion requires a saved model snapshot')
    expanded = copy.deepcopy(scene)
    output = io.BytesIO()
    image.save(output, format='PNG')
    data = output.getvalue()
    expanded.update(image_width=1024, image_height=1024, image_sha256=hashlib.sha256(data).hexdigest())
    expanded.pop('model_signature', None)
    # Local metre coordinates and heights keep exactly the same origin. Only
    # image XY gains a known integer offset; the old pixels are pasted verbatim.
    affine = np.asarray(scene['pixel_to_meters'])
    expanded['pixel_to_meters'][2] = (affine[2]-PADDING*(affine[0]+affine[1])).tolist()
    expanded['meters_to_pixel'][2] = (np.asarray(scene['meters_to_pixel'][2])+PADDING).tolist()
    def shifted(ring):
        return [[x+PADDING, y+PADDING] for x, y in ring]
    for obj in expanded['objects']:
        obj['ring'] = shifted(obj['ring'])
        if 'holes' in obj:
            obj['holes'] = [shifted(h) for h in obj['holes']]
        refinement = obj.get('boundary_refinement') or {}
        if 'raw_ring' in refinement:
            refinement['raw_ring'] = shifted(refinement['raw_ring'])
        if 'raw_holes' in refinement:
            refinement['raw_holes'] = [shifted(h) for h in refinement['raw_holes']]
        # These normally live only in reviews. Retain their bindings if an
        # imported model contains them; line vertices themselves are in metres.
        for field in ('roof_feedback', 'roof_line_edits'):
            if obj.get(field):
                outline = obj[field]['outline']
                outline['ring'] = shifted(outline['ring'])
                outline['holes'] = [shifted(h) for h in outline['holes']]
    west, south, east, north = scene['region']['bounds']
    a = np.asarray(lonlat_to_tile_xy(west, north, 0))
    b = np.asarray(lonlat_to_tile_xy(east, south, 0))
    left, top = a-(b-a)/2
    right, bottom = b+(b-a)/2
    west, north = tile_xy_to_lonlat(left, top, 0)
    east, south = tile_xy_to_lonlat(right, bottom, 0)
    expanded['region'].update(bounds=[west, south, east, north], requested_bounds=[west, south, east, north],
                              span_m=scene['region']['span_m']*2, split='unassigned')
    # Hold the expanded scene out until the spatial split is reassessed. Context
    # can cross cohort boundaries; prior split assignments must not leak labels.
    expanded['context_expansion'] = {'source_model_signature': scene['model_signature'],
        'source_image_sha256': scene['image_sha256'], 'offset_pixels': [PADDING, PADDING],
        'source_size': [512, 512], 'preserved_pixels_sha256': hashlib.sha256(image.crop((256, 256, 768, 768)).tobytes()).hexdigest()}
    return expanded, image, data


def touches_frame(ring, frame, tolerance=1.):
    """True when at least two vertices lie on the frame's edge lines: the outline follows the frame, not the object."""
    x0, y0, x1, y1 = frame
    on_edge = 0
    for x, y in ring:
        inside = x0-tolerance <= x <= x1+tolerance and y0-tolerance <= y <= y1+tolerance
        if inside and (abs(x-x0) <= tolerance or abs(x-x1) <= tolerance or abs(y-y0) <= tolerance or abs(y-y1) <= tolerance):
            on_edge += 1
    return on_edge >= 2


def verify_preserved_pixels(previous, current, directory):
    """Prove recovery against immutable image bytes, not metadata alone."""
    from PIL import Image
    for scene in (previous, current):
        digest = scene['image_sha256']
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            return False
        if hashlib.sha256((directory/f'{digest}.png').read_bytes()).hexdigest() != digest:
            return False
    with Image.open(directory/f"{previous['image_sha256']}.png") as old, Image.open(directory/f"{current['image_sha256']}.png") as new:
        if old.size != (512, 512) or new.size != (1024, 1024):
            return False
        preserved = new.convert('RGB').crop((256, 256, 768, 768)).tobytes()
        return old.convert('RGB').tobytes() == preserved and hashlib.sha256(preserved).hexdigest() == current['context_expansion']['preserved_pixels_sha256']


if __name__ == '__main__':
    import argparse
    import json
    from pathlib import Path
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify-previous', type=Path, required=True)
    parser.add_argument('--verify-current', type=Path, required=True)
    args = parser.parse_args()
    previous = json.loads(args.verify_previous.read_text())
    current = json.loads(args.verify_current.read_text())
    if not verify_preserved_pixels(previous, current, args.verify_previous.parent.parent):
        raise SystemExit('Expanded image no longer preserves the source pixels')


def analyse_expansion(scene, source, segmenter, *, kinds, roof_models=False, root, fetcher=None):
    from .scene_extract import extract_objects, group_objects
    from .scene_house import add_roof_estimates, pipeline_context_hints
    expanded, image, data = expanded_source(scene, source, root=root, fetcher=fetcher)
    seeds = {o['id'] for o in expanded['objects']}
    # Previous MODEL proposals are seeds, never reviewer corrections. Scan the
    # complete enlarged image so detached garages/outbuildings can be proposed.
    expanded['objects'], scans = extract_objects(image, expanded['image_sha256'], np.asarray(expanded['pixel_to_meters']),
        segmenter, objects=expanded['objects'], kinds=kinds)
    # A carried seed whose outline runs along the old frame line was cut by that frame, not by the scene. Say so on
    # the object when no fuller detection took it over, so the editor and the training export can tell a frame cut
    # from a real boundary. This describes geometry; it never changes it.
    frame = [PADDING, PADDING, PADDING+scene['image_width'], PADDING+scene['image_height']]
    for obj in expanded['objects']:
        if obj['id'] in seeds and touches_frame(obj['ring'], frame):
            obj['clipped_by_frame'] = frame
    expanded['scans'] = {k: {**v, 'status': 'partial'} for k, v in scene['scans'].items()}
    expanded['scans'].update(scans)
    expanded['groups'] = group_objects(expanded['objects'])
    if roof_models:
        add_roof_estimates(expanded, image, context_hints=pipeline_context_hints(expanded, image, root))
    from .scene_property_roofs import add_property_roofs
    add_property_roofs(expanded, image, segmenter, root=root)
    expanded.setdefault('analysis', {})['capture_mode'] = 'registered-context-expansion-v1'
    return expanded, image, data
