#!/usr/bin/env python3
"""Measure each outer edge of an outline against the image: where is the strongest boundary step?

Usage (offline case workspace: reads case/case.json, case/image.png and the current run outline):
    python3 skills/addr-roof-tracing/scripts/edge_profile.py [--preview PREVIEW_ID] [--edge K] [--json]
Elsewhere:
    python3 edge_profile.py --image IMAGE.png --ring '[[x, y], ...]' [--edge K] [--json]

For samples along each edge it reads the luminance profile across the edge (outward = positive) and
finds the strongest step. It prints, per edge, the median offset of that step from the drawn line, how
consistently the samples agree, and any dark band just beyond a bright interior (cast shadow or wall).
It measures image transitions only: it cannot say which transition is the roof edge. A consistent
offset is a lead to inspect at native pixels, never a verdict.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

REACH, STEP, SPACING = 10.0, 0.5, 2.0


def load_ring(args):
    if args.ring:
        return json.loads(args.ring)
    if args.preview:
        manifest = Path('run/previews')/args.preview/'manifest.json'
        return json.loads(manifest.read_text())['content']['proposed_outline_pixels']['ring']
    state = Path('run/state.json')
    if state.is_file():
        return json.loads(state.read_text())['outline_pixels']['ring']
    return json.loads(Path('case/case.json').read_text())['start_outline_pixels']['ring']


def luminance(path):
    with Image.open(path) as image:
        rgb = np.asarray(image.convert('RGB'), dtype=float)
    return rgb @ np.array([0.299, 0.587, 0.114])


def sample(gray, xs, ys):
    """Bilinear samples; NaN outside the image."""
    h, w = gray.shape
    out = np.full(xs.shape, np.nan)
    ok = (xs >= 0) & (ys >= 0) & (xs <= w - 1) & (ys <= h - 1)
    x0, y0 = np.floor(xs[ok]).astype(int), np.floor(ys[ok]).astype(int)
    x1, y1 = np.minimum(x0 + 1, w - 1), np.minimum(y0 + 1, h - 1)
    fx, fy = xs[ok] - x0, ys[ok] - y0
    out[ok] = (gray[y0, x0]*(1 - fx)*(1 - fy) + gray[y0, x1]*fx*(1 - fy) + gray[y1, x0]*(1 - fx)*fy
               + gray[y1, x1]*fx*fy)
    return out


def profile_edge(gray, a, b, outward_sign):
    a, b = np.asarray(a, float), np.asarray(b, float)
    length = float(np.hypot(*(b - a)))
    if length < 4:
        return {'length_px': round(length, 1), 'note': 'too short to profile'}
    tangent = (b - a)/length
    normal = outward_sign*np.array([tangent[1], -tangent[0]])
    offsets = np.arange(-REACH, REACH + STEP/2, STEP)
    count = max(3, min(40, int(length*0.7/SPACING)))
    rows = []
    for t in np.linspace(0.15, 0.85, count):
        point = a + t*(b - a)
        line = point[None, :] + offsets[:, None]*normal[None, :]
        values = sample(gray, line[:, 0], line[:, 1])
        if np.isnan(values).any():
            continue
        smooth = np.convolve(values, [0.25, 0.5, 0.25], mode='same')
        grad = np.gradient(smooth, STEP)
        core = slice(2, len(offsets) - 2)
        i = int(np.argmax(np.abs(grad[core]))) + 2
        inside, outside = smooth[offsets < -1].mean(), smooth[offsets > 1].mean()
        band = None
        beyond = smooth[offsets >= 0]
        if len(beyond) > 4:
            j = int(np.argmin(beyond))
            depth = min(smooth[offsets < 0][-4:].mean(), beyond[j:].max()) - beyond[j]
            if depth > 25 and 0 < j < len(beyond) - 2:
                lo = next((k for k in range(j, -1, -1) if beyond[k] > beyond[j] + depth/2), 0)
                hi = next((k for k in range(j, len(beyond)) if beyond[k] > beyond[j] + depth/2), len(beyond) - 1)
                band = (lo*STEP, hi*STEP)
        rows.append((float(offsets[i]), float(abs(smooth[min(i + 2, len(smooth) - 1)] - smooth[max(i - 2, 0)])),
                     inside, outside, band))
    if not rows:
        return {'length_px': round(length, 1), 'note': 'outside the image'}
    steps = np.array([r[0] for r in rows])
    median = float(np.median(steps))
    bands = [r[4] for r in rows if r[4]]
    result = {'length_px': round(length, 1), 'samples': len(rows), 'strongest_step_offset_px': round(median, 1),
              'consistency': round(float(np.mean(np.abs(steps - median) <= 1.5)), 2),
              'step_contrast': round(float(np.median([r[1] for r in rows])), 1),
              'inside_brighter': bool(np.median([r[2] - r[3] for r in rows]) > 0)}
    if len(bands) >= max(2, len(rows)//3):
        result['dark_band_beyond_px'] = [round(float(np.median([b[0] for b in bands])), 1),
                                         round(float(np.median([b[1] for b in bands])), 1)]
        result['dark_band_share'] = round(len(bands)/len(rows), 2)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--image', default='case/image.png')
    parser.add_argument('--ring', help='outer ring as JSON [[x, y], ...] in source pixels')
    parser.add_argument('--preview', help='an offline-case preview_id')
    parser.add_argument('--edge', type=int, help='only this outer edge index')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        ring, gray = load_ring(args), luminance(args.image)
    except (OSError, ValueError, KeyError) as exc:
        print(f'edge_profile: cannot read the outline or image: {exc}', file=sys.stderr)
        return 2
    area = sum(p[0]*q[1] - q[0]*p[1] for p, q in zip(ring, ring[1:] + ring[:1], strict=True))
    outward = 1.0 if area > 0 else -1.0  # image y points down
    edges = range(len(ring)) if args.edge is None else [args.edge % len(ring)]
    report = {i: profile_edge(gray, ring[i], ring[(i + 1) % len(ring)], outward) for i in edges}
    if args.json:
        print(json.dumps(report, indent=1))
        return 0
    print('edge  len   strongest step  agree  contrast  inside  dark band beyond the line')
    for i, row in report.items():
        if 'note' in row:
            print(f'e{i:<4} {row["length_px"]:>5}  {row["note"]}')
            continue
        offset = row['strongest_step_offset_px']
        where = 'on the line' if abs(offset) < 1 else f'{abs(offset):.1f} px {"outside" if offset > 0 else "inside"}'
        band = (f'{row["dark_band_beyond_px"][0]}-{row["dark_band_beyond_px"][1]} px out '
                f'({row["dark_band_share"]:.0%} of samples)' if 'dark_band_beyond_px' in row else '-')
        print(f'e{i:<4} {row["length_px"]:>5}  {where:<15} {row["consistency"]:>5.0%}  {row["step_contrast"]:>8}  '
              f'{"brighter" if row["inside_brighter"] else "darker":<7} {band}')
    print('Offsets are from the drawn line along the outward normal; inspect consistent offsets > 1.5 px at native '
          'pixels before moving an edge. A dark band beyond a bright roof is usually shadow or wall.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
