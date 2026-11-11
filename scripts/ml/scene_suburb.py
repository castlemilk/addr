"""Resumable, frozen inference across the official Mooroolbark suburb polygon.

Run --prepare once, then execute --resume RUN from RUN/snapshot. No reviews
are changed and no training approval is inferred from automated diagnostics.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
import math
import os
import re
import shutil
import time
from collections import Counter
from pathlib import Path

from PIL import Image
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union

from .imagery import ImageryFetcher
from .scene_batch import signature
from .scene_confidence import assess, geographic
from .scene_experiment import digest, model_fingerprint
from .scene_extract import PROMPTS, analyse_scene, save_scene
from .scene_split import image_split_audit
from .scene_training import atomic_json
from .tiles import lonlat_to_tile_xy, tile_xy_to_lonlat

SUBDIR = 'data/suburbs/mooroolbark'
CLASSES = ['building', 'tree', 'solar_panel']
SOURCE = 'https://services-ap1.arcgis.com/P744lA0wf4LlBZ84/ArcGIS/rest/services/Vicmap_Admin/FeatureServer/11'


class CompleteImagery(ImageryFetcher):
    def _child(self, z, x, y):
        image = super()._child(z, x, y)
        if image is None:
            raise ValueError(f'Imagery unavailable at {z}/{x}/{y}; no partial capture published')
        return image


def tile_polygon(z, x, y):
    west, north = tile_xy_to_lonlat(x, y, z)
    east, south = tile_xy_to_lonlat(x+1, y+1, z)
    return box(west, south, east, north)


def tile_plan(boundary, z=17, focus=(145.31248, -37.78847)):
    west, south, east, north = boundary.bounds
    left, top = lonlat_to_tile_xy(west, north, z)
    right, bottom = lonlat_to_tile_xy(east, south, z)
    fx, fy = lonlat_to_tile_xy(*focus, z)
    tiles = [[z, x, y] for x in range(math.floor(left), math.ceil(right))
             for y in range(math.floor(top), math.ceil(bottom))
             if tile_polygon(z, x, y).intersection(boundary).area > 1e-14]
    return sorted(tiles, key=lambda t: ((t[1]+.5-fx)**2+(t[2]+.5-fy)**2, t))


def prepare(root):
    base = root/SUBDIR
    boundary = json.loads((base/'boundary.geojson').read_text())
    polygon = unary_union([shape(f['geometry']) for f in boundary['features']])
    if not polygon.is_valid or polygon.is_empty:
        raise ValueError('Invalid suburb boundary')
    files = sorted((root/'scripts/ml').glob('*.py'))
    plan = {'version': 1, 'boundary': boundary, 'boundary_source': SOURCE,
            'tiles': tile_plan(polygon), 'classes': CLASSES, 'prompts': {k: PROMPTS[k] for k in CLASSES},
            'provider': 'google', 'imagery_offset': 2, 'out_px': 512, 'margin_px': 32,
            'code': {str(p.relative_to(root)): digest(p) for p in files},
            'weights': model_fingerprint(root/'scripts/ml/models/sam3')}
    form = root/'scripts/ml/models/roof-form.json'
    if os.environ.get('ROOF_FORM_MODEL'):
        raise ValueError('Use the default roof-form model when freezing a suburb run')
    plan['roof_form'] = digest(form) if form.exists() else None
    directory = base/'runs'/signature(plan)[:16]
    directory.mkdir(parents=True, exist_ok=True)
    for path in files + ([form] if form.exists() else []):
        target = directory/'snapshot'/path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and digest(target) != digest(path):
            raise ValueError('Frozen code snapshot changed')
        shutil.copyfile(path, target)
    atomic_json(directory/'plan.json', plan)
    atomic_json(base/'active.json', {'run': directory.name})
    return directory


def capture(scene_id, tile, plan, root):
    z, x, y = tile
    fetcher = CompleteImagery(provider=plan['provider'], cache_dir=root/'data/imagery',
                              imagery_offset=plan['imagery_offset'], out_px=plan['out_px'], margin_px=plan['margin_px'])
    window = fetcher.window(z, x, y)
    image = window.image
    width, height = image.size
    mpp = window.tile_span_meters/window.base_px
    lon, lat = tile_xy_to_lonlat(x+.5, y+.5, z)
    source = {'schema_version': 1, 'id': scene_id, 'address_id': None, 'cohort': None,
              'title': f'Mooroolbark · tile {z}/{x}/{y}', 'centroid': {'lon': lon, 'lat': lat},
              'image_width': width, 'image_height': height,
              'pixel_to_meters': [[mpp, 0], [0, -mpp], [-width*mpp/2, height*mpp/2]],
              'meters_to_pixel': [[1/mpp, 0], [0, -1/mpp], [width/2, height/2]],
              'objects': [], 'groups': [], 'scans': {},
              'height_note': 'Model estimates, not measured heights.',
              'region': {'provider': plan['provider'], 'imagery_zoom': window.imagery_zoom,
                         'native_meters_per_pixel': mpp, 'spatial_group': f'z{z}:{x}:{y}', 'split': 'unassigned'},
              'suburb_run': signature(plan)[:16], 'analysis_tile': tile}
    extent = geographic({'ring': [[0, 0], [width, 0], [width, height], [0, height]]}, source)
    source['region'].update(bounds=list(extent.bounds), requested_bounds=list(tile_polygon(*tile).bounds), span_m=width*mpp)
    splits = image_split_audit(source, 'unassigned')['splits']
    source['region']['split'] = splits[0] if len(splits) == 1 else 'unassigned'
    output = io.BytesIO()
    image.save(output, format='PNG')
    data = output.getvalue()
    source['image_sha256'] = hashlib.sha256(data).hexdigest()
    return source, data


def ownership(prediction, tile, boundary):
    """Half-open tile cores assign one owner; full roofs remain unclipped."""
    z, x, y = tile
    ids = []
    for obj in prediction['objects']:
        polygon = geographic(obj, prediction)
        center = polygon.centroid
        tx, ty = lonlat_to_tile_xy(center.x, center.y, z)
        if x <= tx < x+1 and y <= ty < y+1 and boundary.covers(center):
            ids.append(obj['id'])
    prediction['owned_object_ids'] = ids
    prediction['analysis_core'] = mapping(tile_polygon(*tile).intersection(boundary))


def run(directory, root):
    from .sam3 import Sam3TextSegmenter
    plan = json.loads((directory/'plan.json').read_text())
    if signature(plan)[:16] != directory.name:
        raise ValueError('Run plan changed')
    code_root = Path(__file__).resolve().parents[2]
    for name, expected in plan['code'].items():
        if digest(code_root/name) != expected:
            raise ValueError('Run from the frozen snapshot; inference code changed')
    if model_fingerprint(root/'scripts/ml/models/sam3') != plan['weights']:
        raise ValueError('Frozen SAM3 weights changed')
    form = directory/'snapshot/scripts/ml/models/roof-form.json'
    if (digest(form) if form.exists() else None) != plan['roof_form']:
        raise ValueError('Roof-form model changed')
    # roof_form resolves its default path relative to the frozen working directory.
    os.chdir(directory/'snapshot')
    os.environ.pop('ROOF_FORM_MODEL', None)
    state_path = directory/'run.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'results': {}, 'started_at': time.time()}
    state.update(status='running', pid=os.getpid(), total=len(plan['tiles']))
    def checkpoint():
        state['counts'] = dict(Counter(r['status'] for r in state['results'].values()))
        state['updated_at'] = time.time()
        atomic_json(state_path, state)
    checkpoint()
    boundary = unary_union([shape(f['geometry']) for f in plan['boundary']['features']])
    segmenter = None
    for tile in plan['tiles']:
        scene_id = f'mooroolbark-{directory.name[:8]}-'+'-'.join(map(str, tile))
        folder = root/'data/scenes'/scene_id
        prior = state['results'].get(scene_id, {})
        if prior.get('status') == 'complete':
            continue
        started = time.monotonic()
        state['current'] = scene_id
        state['results'][scene_id] = {'status': 'running', 'tile': tile, 'attempts': prior.get('attempts', 0)+1}
        checkpoint()
        print(json.dumps({'scene': scene_id, 'status': 'running'}), flush=True)
        try:
            if (folder/'latest.json').exists():
                saved = json.loads((folder/'latest.json').read_text())
                if saved.get('suburb_run') != directory.name:
                    raise ValueError('Preserved an existing scene from another run')
            else:
                if (folder/'review.json').exists():
                    raise ValueError('Preserved existing feedback without a model')
                job = directory/'captures'/scene_id
                job.mkdir(parents=True, exist_ok=True)
                source_path, image_path = job/'source.json', job/'image.png'
                if source_path.exists():
                    source, data = json.loads(source_path.read_text()), image_path.read_bytes()
                else:
                    source, data = capture(scene_id, tile, plan, root)
                    image_path.write_bytes(data)
                    atomic_json(source_path, source)
                    (job/'source.sha256').write_text(signature(source))
                if source['id'] != scene_id or source['image_sha256'] != hashlib.sha256(data).hexdigest() or signature(source) != (job/'source.sha256').read_text():
                    raise ValueError('Frozen capture changed')
                if segmenter is None:
                    segmenter = Sam3TextSegmenter(root/'scripts/ml/models/sam3')
                with Image.open(io.BytesIO(data)) as image:
                    prediction = analyse_scene(source, image.convert('RGB'), segmenter, kinds=plan['classes'], detector_only=True, roof_models=True, root=root)
                prediction['analysis']['inference_profile'] = signature({'code': plan['code'], 'weights': plan['weights'], 'roof_form': plan['roof_form'], 'prompts': plan['prompts']})
                ownership(prediction, tile, boundary)
                if any(o.get('status') != 'candidate' or o.get('geometry_reviewed') for o in prediction['objects']):
                    raise ValueError('Inference returned approved labels')
                if (folder/'latest.json').exists() or (folder/'review.json').exists():
                    raise ValueError('Scene appeared during analysis; preserving it')
                saved = save_scene(prediction, data, root)
            report = assess(scene_id, root)
            owned = set(saved['owned_object_ids'])
            state['results'][scene_id].update(status='complete', model_signature=saved['model_signature'],
                objects=dict(Counter(o['kind'] for o in saved['objects'] if o['id'] in owned)),
                confidence=dict(Counter(o['band'] for o in report['objects'] if o['object_id'] in owned)))
        except Exception as error:
            state['results'][scene_id].update(status='failed', error=str(error))
        state['results'][scene_id]['elapsed_seconds'] = round(time.monotonic()-started, 2)
        checkpoint()
        print(json.dumps({'scene': scene_id, **state['results'][scene_id]}), flush=True)
    state.update(status='needs_retry' if any(r['status'] == 'failed' for r in state['results'].values()) else 'complete', current=None)
    checkpoint()
    return state


def progress(root):
    base = root/SUBDIR
    if not (base/'active.json').exists():
        return None
    run_id = json.loads((base/'active.json').read_text())['run']
    if not re.fullmatch('[a-f0-9]{16}', run_id):
        raise ValueError('Invalid run identity')
    directory = base/'runs'/run_id
    plan = json.loads((directory/'plan.json').read_text())
    state = json.loads((directory/'run.json').read_text()) if (directory/'run.json').exists() else {'status': 'planned', 'results': {}}
    if state['status'] == 'running':
        try:
            os.kill(state['pid'], 0)
        except ProcessLookupError:
            state['status'] = 'interrupted'
        except PermissionError:
            pass
    by_tile = {tuple(r['tile']): r for r in state['results'].values()}
    boundary = unary_union([shape(f['geometry']) for f in plan['boundary']['features']])
    features = [{'type': 'Feature', 'geometry': mapping(tile_polygon(*t).intersection(boundary)),
                 'properties': {'status': by_tile.get(tuple(t), {}).get('status', 'pending')}} for t in plan['tiles']]
    return {'run': run_id, 'status': state['status'], 'total': len(plan['tiles']), 'counts': state.get('counts', {}),
            'updated_at': state.get('updated_at'), 'bounds': list(boundary.bounds), 'boundary': plan['boundary'],
            'coverage': {'type': 'FeatureCollection', 'features': features}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--resume')
    parser.add_argument('--root', type=Path, default=Path('.'))
    args = parser.parse_args()
    root = args.root.resolve()
    if args.prepare:
        print(prepare(root))
    elif args.resume and re.fullmatch('[a-f0-9]{16}', args.resume):
        directory = root/SUBDIR/'runs'/args.resume
        snapshot = directory/'snapshot'
        if Path(__file__).resolve().parents[2] != snapshot:
            os.chdir(snapshot)
            os.execv(str(root/'scripts/ml/.venv/bin/python'), [str(root/'scripts/ml/.venv/bin/python'), '-u', '-m',
                     'scripts.ml.scene_suburb', '--resume', args.resume, '--root', str(root)])
        with (root/'data/scenes/.extraction.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = run(directory, root)
        if result['status'] != 'complete':
            parser.exit(1, 'Some tiles need retry; inspect run.json and resume the same run.\n')
    else:
        parser.error('Use --prepare or --resume RUN_ID')


if __name__ == '__main__':
    main()
