"""Does the roof itself break along a lot line? (reviewer 2026-10-17: "the modelling should intelligently see that roof boundaries
shouldn't be cut if its still following the roof shape").

A party wall between units shows in the photo as (a) a straight line running ALONG the cut across most of the roof's width (a
parapet, gutter or flashing: gradients perpendicular to the cut), (b) often a step in the outline where the cut meets it, and (c)
often a change of roofing across it (colour, texture). A ridge or hip line inside one roof crosses the cut at an angle (a hip at
~45 deg) or runs perpendicular to it (the main ridge of a terrace-length roof), so it gives (a) only at isolated points. Pure image
measurement on one roof; no learned weights. Used by scene_parcel_units to decide split / one building on several titles.
"""
from __future__ import annotations

import math

import cv2
import numpy as np
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

ALIGN_DEG = 20.0      # gradient within this of the cut's normal = a line running along the cut
STRIP_M = 1.5         # side strips for the across-cut contrast
STEP_M = 0.6          # an outline corner this far off the straight line within NEAR_M of a cut end = a step
NEAR_M = 1.5
GAP_MIN = 0.8         # centre band differs from the nearer side by this many (pooled) standard deviations


def _grad(image_grey):
    g = cv2.GaussianBlur(image_grey.astype(np.float32), (0, 0), 1.0)
    gx, gy = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    return gx, gy, np.hypot(gx, gy)


def _cuts(ring, units, inner):
    """Shared boundaries between unit pieces, inside the roof (line pieces)."""
    polys = [Polygon(u['ring']).buffer(0) for u in units]
    out = []
    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            s = polys[i].boundary.intersection(polys[j].buffer(0.75)).intersection(inner)
            for g in getattr(s, 'geoms', [s]):
                if g.geom_type == 'LineString' and g.length > 0:
                    out.append(g)
    return out


def roof_break(image_rgb, ring, units, mpp, grad=None):
    """{'coverage': share of cut length with an aligned line, 'contrast': colour change across / within, 'step': bool,
    'length_m', 'break': bool, 'why'} or None when there is no interior cut of at least 2 m."""
    roof = Polygon(ring).buffer(0)
    inner = roof.buffer(-0.75 / mpp)
    if inner.is_empty:
        return None
    cuts = _cuts(ring, units, inner)
    total = sum(c.length for c in cuts)
    if total * mpp < 2:
        return None
    grey = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2GRAY) if image_rgb.ndim == 3 else image_rgb
    gx, gy, mag = grad if grad is not None else _grad(grey)
    h, w = mag.shape
    mask = np.zeros((h, w), np.uint8)
    for p in getattr(inner, 'geoms', [inner]):
        cv2.fillPoly(mask, [np.round(np.asarray(p.exterior.coords)).astype(np.int32)], 1)
    base = float(np.percentile(mag[mask > 0], 60)) if mask.any() else 1.0
    hits = n = 0
    contrasts, gaps = [], []
    lab = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2LAB).astype(np.float32) if image_rgb.ndim == 3 else None
    for c in cuts:
        (x0, y0), (x1, y1) = c.coords[0], c.coords[-1]
        L = math.hypot(x1 - x0, y1 - y0) or 1.0
        nx, ny = -(y1 - y0) / L, (x1 - x0) / L
        for t in np.arange(0, c.length, 0.25 / mpp):
            x, y = c.interpolate(t).coords[0]
            best = 0
            for off in np.arange(-1.0 / mpp, 1.0 / mpp + 1e-9, 1.0):  # the wall may sit up to 1 m off the lot line
                xi, yi = int(round(x + nx * off)), int(round(y + ny * off))
                if not (0 <= xi < w and 0 <= yi < h):
                    continue
                m = mag[yi, xi]
                if m < 1.5 * base:
                    continue
                cosang = abs(gx[yi, xi] * nx + gy[yi, xi] * ny) / max(m, 1e-6)
                if cosang >= math.cos(math.radians(ALIGN_DEG)):
                    best = 1
                    break
            hits += best; n += 1
        if lab is not None:
            side = []
            for sgn in (1, -1):
                strip = Polygon([(x0, y0), (x1, y1), (x1 + sgn * nx * STRIP_M / mpp, y1 + sgn * ny * STRIP_M / mpp),
                                 (x0 + sgn * nx * STRIP_M / mpp, y0 + sgn * ny * STRIP_M / mpp)]).intersection(inner)
                if strip.is_empty or strip.area < 4:
                    side = []
                    break
                m2 = np.zeros((h, w), np.uint8)
                for p in getattr(strip, 'geoms', [strip]):
                    if p.geom_type == 'Polygon':
                        cv2.fillPoly(m2, [np.round(np.asarray(p.exterior.coords)).astype(np.int32)], 1)
                px = lab[m2 > 0]
                if len(px) < 8:
                    side = []
                    break
                side.append((px.mean(0), px.std(0).mean()))
            if len(side) == 2:
                contrasts.append(float(np.linalg.norm(side[0][0] - side[1][0]) / max(side[0][1] + side[1][1], 1e-6)))
                band = c.buffer(0.5 / mpp, cap_style=2).intersection(inner)
                m3 = np.zeros((h, w), np.uint8)
                for p in getattr(band, 'geoms', [band]):
                    if p.geom_type == 'Polygon':
                        cv2.fillPoly(m3, [np.round(np.asarray(p.exterior.coords)).astype(np.int32)], 1)
                px = lab[m3 > 0]
                if len(px) >= 8:
                    mid = px.mean(0)
                    spread = max(side[0][1] + side[1][1], 1e-6)
                    gaps.append(float(min(np.linalg.norm(mid - side[0][0]), np.linalg.norm(mid - side[1][0])) / spread))
    coverage = hits / max(n, 1)
    contrast = max(contrasts) if contrasts else 0.0
    # outline step: a corner of the saved outline within NEAR_M of a cut end that sits off the line through its neighbours
    pts = [tuple(p) for p in Polygon(ring).buffer(0).exterior.coords[:-1]]
    step = False
    ends = [c.coords[0] for c in cuts] + [c.coords[-1] for c in cuts]
    for i, p in enumerate(pts):
        if min(math.dist(p, e) for e in ends) * mpp > NEAR_M + 0.75:
            continue
        a, b = pts[i - 1], pts[(i + 1) % len(pts)]
        from shapely.geometry import Point
        dev = LineString([a, b]).distance(Point(p)) if a != b else 0
        if dev * mpp >= STEP_M:
            step = True
            break
    gap = max(gaps) if gaps else 0.0
    # a gap: the band along the cut looks like neither side (ground / shadow between two buildings bridged by one outline)
    brk = coverage >= 0.6 or gap >= GAP_MIN or (coverage >= 0.3 and (step or contrast >= 1.0))
    why = (f'roof breaks along the cut (line along {coverage:.0%} of it' + (', outline steps' if step else '')
           + (f', roofing changes {contrast:.1f}' if contrast >= 1.0 else '') + (', gap between roofs' if gap >= GAP_MIN else '')
           + ')') if brk else \
          (f'roof continues across the cut (line along only {coverage:.0%} of it' + (', outline steps' if step else '') + ')')
    return {'coverage': round(coverage, 3), 'contrast': round(contrast, 3), 'gap': round(gap, 3), 'step': step, 'length_m': round(total * mpp, 2),
            'break': brk, 'why': why}
