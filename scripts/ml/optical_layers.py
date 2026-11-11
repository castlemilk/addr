"""Deterministic optical layers of one saved scene image, as extra LSD views for the roof-line fitter.

Every layer is a photometric transform of the saved pixels on the saved grid (same height, width
and registration): denoising, contrast, sharpening, band-pass, gradient, shadow normalisation and an
edge-preserving smoother. None of them is a new evidence source, so the fitter may let a line
detected on such a layer pass the evidence gate on that layer's own array
(``roof_line_fit.image_segments(layer_evidence=True)``, views named ``layer:<name>``). The one
layer built from other pixels, ``multidate_median`` (the existing registered stack composite), is
hash-bound to its stack id and handed over WITHOUT the ``layer:`` prefix, so its lines must still
pass the gate on the original RGB. Layers are diagnostics for the fitter, never labels or accuracy.

CLI: ``python -m scripts.ml.optical_layers --scene ID [--out DIR] [--sheet PATH]`` writes one
``.npy`` per layer, ``provenance.json`` and a contact sheet of a 60 m crop through every layer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

VERSION = 'optical-layers-v1'
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
LAYER_PREFIX = 'layer:'
DEFAULT_PIXELS_PER_METER = 4.
SHEET_CROP_M = 60.
MULTIDATE = 'multidate_median'
CHEAP_LAYERS = ('bilateral', 'unsharp', 'shadow_norm')
SAME_PIXEL_LAYERS = ('bilateral', 'clahe12', 'unsharp', 'dog', 'gradient', 'shadow_norm', 'guided')
LAYER_PARAMS = {
    'bilateral': {'source': 'gray', 'd': 7, 'sigma_color': 25., 'sigma_space': 5.},
    'clahe12': {'source': 'lab_l', 'clip_limit': 3., 'tile_grid': [12, 12]},
    'unsharp': {'source': 'gray', 'sigma': 1.5, 'amount': 1.},
    'dog': {'source': 'gray', 'sigma_fine': 1., 'sigma_coarse': 3., 'rescale': 'centred-128, |dog| p99.5 -> 127'},
    'gradient': {'source': 'gray', 'kernel': 'sobel-3', 'rescale': 'p99.5 -> 255'},
    'shadow_norm': {'source': 'lab_l', 'window_m': 15., 'gain': 2., 'rescale': 'centred-128'},
    'guided': {'source': 'lab_l', 'radius_m': 1.5, 'eps': 150., 'guide': 'self',
               'method': 'He et al. guided filter via box filters (no ximgproc)'},
    MULTIDATE: {'source': 'stack-median.png', 'note': 'registered stack composite; other pixels, hash-bound to the stack'},
}
NOTE = ('Deterministic photometric layers of the saved PNG on its own grid; not super-resolution, sharpening for '
        'display, inpainting or a new evidence source. multidate_median carries other captures and keeps the '
        'original-RGB gate. Diagnostics for the line fitter, never labels or accuracy.')


def _rgb(image):
    if isinstance(image, np.ndarray):
        array = np.ascontiguousarray(image)
        if array.ndim != 3 or array.shape[2] != 3 or array.dtype != np.uint8:
            raise ValueError('image must be a PIL RGB image or an HxWx3 uint8 array')
        return array
    return np.ascontiguousarray(np.asarray(image.convert('RGB')))


def _to_uint8(values):
    return np.clip(np.rint(values), 0, 255).astype(np.uint8)


def _odd(pixels):
    size = max(3, int(round(pixels)))
    return size if size % 2 else size+1


def _rescale_percentile(values, q, top=255.):
    scale = float(np.percentile(values, q))
    return values*(top/scale) if scale > 0 else np.zeros_like(values)


def guided_filter(guide, radius_px, eps):
    """Self-guided edge-preserving smoother (He, Sun, Tang 2010) with box filters; float64, deterministic."""
    i = guide.astype(np.float64)
    size = (2*int(radius_px)+1,)*2

    def box(values):
        return cv2.boxFilter(values, -1, size, normalize=True, borderType=cv2.BORDER_REFLECT)
    mean = box(i)
    variance = box(i*i)-mean*mean
    a = variance/(variance+float(eps))
    b = mean-a*mean
    return box(a)*i+box(b)


def layer_arrays(image, *, pixels_per_meter=DEFAULT_PIXELS_PER_METER, names=None):
    """The seven same-pixel layers as {name: HxW uint8} on the saved grid."""
    if not (isinstance(pixels_per_meter, (int, float)) and pixels_per_meter > 0):
        raise ValueError('pixels_per_meter must be positive')
    chosen = set(SAME_PIXEL_LAYERS if names is None else names)
    if not chosen <= set(SAME_PIXEL_LAYERS):
        raise ValueError('Choose registered same-pixel optical layers')
    rgb = _rgb(image)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    lab_l = np.ascontiguousarray(cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)[:, :, 0])
    p = LAYER_PARAMS
    layers = {}
    if 'bilateral' in chosen:
        layers['bilateral'] = cv2.bilateralFilter(gray, p['bilateral']['d'], p['bilateral']['sigma_color'], p['bilateral']['sigma_space'])
    if 'clahe12' in chosen:
        layers['clahe12'] = cv2.createCLAHE(clipLimit=p['clahe12']['clip_limit'], tileGridSize=tuple(p['clahe12']['tile_grid'])).apply(lab_l)
    if 'unsharp' in chosen:
        blurred = cv2.GaussianBlur(gray.astype(np.float64), (0, 0), p['unsharp']['sigma'])
        layers['unsharp'] = _to_uint8(gray+p['unsharp']['amount']*(gray-blurred))
    if 'dog' in chosen:
        fine = cv2.GaussianBlur(gray.astype(np.float64), (0, 0), p['dog']['sigma_fine'])
        coarse = cv2.GaussianBlur(gray.astype(np.float64), (0, 0), p['dog']['sigma_coarse'])
        layers['dog'] = _to_uint8(128+_rescale_percentile(fine-coarse, 99.5, 127.)) if np.any(fine != coarse) else np.full_like(gray, 128)
    if 'gradient' in chosen:
        magnitude = np.hypot(cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3), cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3))
        layers['gradient'] = _to_uint8(_rescale_percentile(magnitude, 99.5))
    if 'shadow_norm' in chosen:
        window = _odd(p['shadow_norm']['window_m']*pixels_per_meter)
        local_mean = cv2.blur(lab_l.astype(np.float64), (window, window), borderType=cv2.BORDER_REFLECT)
        layers['shadow_norm'] = _to_uint8(128+p['shadow_norm']['gain']*(lab_l-local_mean))
    if 'guided' in chosen:
        layers['guided'] = _to_uint8(guided_filter(lab_l, max(1, int(round(p['guided']['radius_m']*pixels_per_meter))), p['guided']['eps']))
    return {name: np.ascontiguousarray(array, np.uint8) for name, array in layers.items()}


def find_stack(scene_id, root=Path('.')):
    """Newest 'available' (>= 3 views) stack report for a scene under the multidate stacks folder, else None."""
    from .scene_stack import STACKS
    folder = Path(root)/STACKS
    if not folder.is_dir():
        return None
    reports = []
    for path in sorted(folder.glob('*/report.json')):
        try:
            report = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if report.get('scene_id') == scene_id and report.get('status') == 'available':
            reports.append(report)
    reports.sort(key=lambda r: r.get('computed_at') or '', reverse=True)
    return reports[0] if reports else None


def multidate_median(stack, image, *, root=Path('.'), image_sha256=None):
    """(gray HxW uint8, stack_id) from an available stack, hash-bound: the report's source image must be this one."""
    from .scene_local_stack import _load_report, views
    _, report = _load_report(stack, root)
    if report.get('status') != 'available':
        raise ValueError('multidate_median needs an available stack (>= 3 views)')
    if image_sha256 is not None and report.get('source_image_sha256') != image_sha256:
        raise ValueError('The stack is bound to a different saved image')
    median = dict(views(report, root)).get('stack-median')
    if median is None:
        raise ValueError('The stack has no stack-median view')
    height, width = _rgb(image).shape[:2]
    if median.shape != (height, width):
        raise ValueError('The stack median is not on the saved image grid')
    return np.ascontiguousarray(median, np.uint8), report['stack_id']


def build(image, *, stack=None, pixels_per_meter=DEFAULT_PIXELS_PER_METER, root=Path('.'), image_sha256=None):
    """{name: HxW uint8} of every deterministic layer; adds 'multidate_median' when an available stack is given
    (stack id or report dict). Same input bytes, same output bytes."""
    layers = layer_arrays(image, pixels_per_meter=pixels_per_meter)
    if stack is not None:
        layers[MULTIDATE], _ = multidate_median(stack, image, root=root, image_sha256=image_sha256)
    return layers


def provenance(layers, *, source_image_sha256, pixels_per_meter=DEFAULT_PIXELS_PER_METER, stack_id=None):
    """One record per layer: {name, params, sha256, source_image_sha256, stack_id|None}."""
    rows = []
    for name, array in layers.items():
        if name not in LAYER_PARAMS:
            raise ValueError(f'Unknown layer {name!r}')
        params = {**LAYER_PARAMS[name], 'pixels_per_meter': pixels_per_meter} if name != MULTIDATE else dict(LAYER_PARAMS[name])
        rows.append({'name': name, 'params': params,
                     'sha256': hashlib.sha256(np.ascontiguousarray(array, np.uint8).tobytes()).hexdigest(),
                     'source_image_sha256': source_image_sha256,
                     'stack_id': stack_id if name == MULTIDATE else None})
    return rows


def view_name(name):
    """Extra-view name for the fitter: same-pixel layers carry the layer prefix (they may self-evidence),
    the multidate median does not (its lines keep the original-RGB gate)."""
    return name if name == MULTIDATE else LAYER_PREFIX+name


def evidence_views(layers, *, names=None, stack_id=None):
    """[{name, array, stack_id}] for scene_house.add_roof_estimates / scene_isolated (or pairs() for propose_roof)."""
    chosen = list(layers) if names is None else list(names)
    missing = [n for n in chosen if n not in layers]
    if missing:
        raise ValueError(f'Layers not built: {missing}')
    return [{'name': view_name(n), 'array': layers[n], 'stack_id': stack_id if n == MULTIDATE else None} for n in chosen]


def pairs(layers, *, names=None):
    """[(view name, array)] as propose_roof/image_segments take them."""
    return [(v['name'], v['array']) for v in evidence_views(layers, names=names)]


def contact_sheet(image, layers, *, center_px=None, pixels_per_meter=DEFAULT_PIXELS_PER_METER, crop_m=SHEET_CROP_M,
                  tile_px=360, columns=3, title=''):
    """Original RGB and every layer through the same crop_m square around center_px (default: image centre)."""
    rgb = _rgb(image)
    height, width = rgb.shape[:2]
    half = max(8, int(round(crop_m*pixels_per_meter/2)))
    cx, cy = (width//2, height//2) if center_px is None else (int(round(center_px[0])), int(round(center_px[1])))
    x0, y0 = max(0, min(cx-half, width-2*half)), max(0, min(cy-half, height-2*half))
    x1, y1 = min(width, x0+2*half), min(height, y0+2*half)
    scale = tile_px/max(1, x1-x0)
    size = (max(1, int(round((x1-x0)*scale))), max(1, int(round((y1-y0)*scale))))
    tiles = [('original', Image.fromarray(rgb[y0:y1, x0:x1]).resize(size, Image.LANCZOS))]
    for name, array in layers.items():
        tiles.append((name, Image.fromarray(np.asarray(array)[y0:y1, x0:x1]).convert('RGB').resize(size, Image.LANCZOS)))
    rows = (len(tiles)+columns-1)//columns
    header = 18 if title else 0
    sheet = Image.new('RGB', (columns*size[0], rows*size[1]+header), (20, 20, 30))
    draw = ImageDraw.Draw(sheet)
    if title:
        draw.text((4, 2), title, fill=(255, 255, 0))
    for i, (name, tile) in enumerate(tiles):
        x, y = (i % columns)*size[0], (i//columns)*size[1]+header
        sheet.paste(tile, (x, y))
        draw.rectangle((x+2, y+2, x+8*len(name)+8, y+16), fill=(0, 0, 0))
        draw.text((x+5, y+3), name, fill=(255, 255, 0))
    return sheet


def pixels_per_meter_of(scene):
    frame = np.asarray(scene['meters_to_pixel'], dtype=float)
    return float(np.sqrt(abs(np.linalg.det(frame[:2]))))


def write_layers(out, layers, rows, *, scene_id, image_sha256, pixels_per_meter, stack_id):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for name, array in layers.items():
        np.save(out/f'{name}.npy', array)
    document = {'version': VERSION, 'code_sha256': CODE_SHA256, 'computed_at': datetime.now(UTC).isoformat(timespec='seconds'),
                'scene_id': scene_id, 'source_image_sha256': image_sha256, 'pixels_per_meter': pixels_per_meter,
                'stack_id': stack_id, 'layers': rows, 'view_names': [view_name(n) for n in layers], 'note': NOTE}
    (out/'provenance.json').write_text(json.dumps(document, indent=1))
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--scene', required=True)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--out', type=Path, help='layer folder (default /tmp/addr-eyes/optical/<scene>)')
    parser.add_argument('--sheet', type=Path, help='contact sheet PNG (default <out>/contact-sheet.png)')
    parser.add_argument('--center', type=float, nargs=2, metavar=('X_PX', 'Y_PX'), help='sheet crop centre in saved pixels')
    parser.add_argument('--no-stack', action='store_true', help='skip the multidate median even when an available stack exists')
    args = parser.parse_args(argv)
    from .scene_roof_diagnostics import load_scene
    scene, image = load_scene(args.scene, args.root)
    ppm = pixels_per_meter_of(scene)
    report = None if args.no_stack else find_stack(args.scene, args.root)
    stack_id = report['stack_id'] if report else None
    layers = build(image, stack=report, pixels_per_meter=ppm, root=args.root, image_sha256=scene['image_sha256'])
    rows = provenance(layers, source_image_sha256=scene['image_sha256'], pixels_per_meter=ppm, stack_id=stack_id)
    out = args.out or Path('/tmp/addr-eyes/optical')/args.scene
    document = write_layers(out, layers, rows, scene_id=args.scene, image_sha256=scene['image_sha256'],
                            pixels_per_meter=ppm, stack_id=stack_id)
    sheet_path = args.sheet or out/'contact-sheet.png'
    sheet_path.parent.mkdir(parents=True, exist_ok=True)
    contact_sheet(image, layers, center_px=args.center, pixels_per_meter=ppm,
                  title=f'{args.scene} {VERSION} {int(SHEET_CROP_M)} m crop').save(sheet_path)
    print(json.dumps({'scene_id': args.scene, 'out': str(out), 'sheet': str(sheet_path), 'stack_id': stack_id,
                      'layers': {r['name']: r['sha256'][:12] for r in document['layers']}}, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
