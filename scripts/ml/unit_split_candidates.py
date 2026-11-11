"""Per-lot candidate outlines for a saved building outline that spans unit lots.

A row of units is often segmented as one roof. This tool intersects that saved
outline (in local metres) with the approved Vicmap lot polygons that touch it
and writes one candidate outline per numbered lot plus a common-property
remainder, a JSON report and a PNG review sheet under
``data/reviews/eyes/units/``. It never writes a scene, never runs SAM3 and
never takes the extraction lock. Cadastral cuts are allocation edges, never
physical roof folds: a candidate is a proposal for a reviewer, not a label.

Importing a candidate as a draft
--------------------------------
Candidates are not imported automatically. A reviewer has two routes:

1. Manual trace (available today): open the scene at
   ``/scenes?scene=<scene>&object=<object>&status=all`` with "Property
   boundaries" checked, press "Add object" and trace the unit outline along the
   dashed lot line shown on the image (the sheet next to the report shows the
   same cut). Saving records the outline as a reviewer draft
   (``source: reviewer``, ``status: needs_adjustment``) and leaves the original
   outline in place until the reviewer supersedes it.
2. Candidates import flow (``/scenes/candidates``): that page imports drafts
   from an experiment run under ``data/training/scene-proposals/<candidate>``
   whose scores file lists proposals per scene. A future step can package the
   ``lots[].ring_px``/``holes_px`` rings from this report into that layout; the
   report keeps ``image_sha256`` and ``model_signature`` so the import can
   refuse a changed image or frame, as the existing flow does.

Stacked strata lots (identical polygons for every lot and the common property)
carry no cut lines in Vicmap Parcel; the report says so instead of inventing
a split. Where a stacked plan sits beside other lots, the whole stacked polygon
is one candidate (``lots`` lists its members, ``stacked`` is true): the cut to
the neighbour is cadastral, the cuts between the stacked units are not.

Versions: v1 allocated one piece per numbered lot (stacked lots beside a
neighbour were duplicated, coverage could exceed 1); v2 allocates per distinct
lot polygon, reports ``coverage_share`` as the allocated union over the outline
and adds ``overlap_m2``, ``lots``/``stacked`` per candidate and the
``invalid_outline`` status.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from shapely.errors import GEOSException
from shapely.geometry import Polygon, shape
from shapely.ops import transform, unary_union

from .roof_block_context import fetch_parcels, polygons, rings
from .scene_addresses import image_projection
from .scene_training import atomic_json

VERSION = 'unit-split-candidates-v2'
POLICY = 'cadastral cuts are allocation edges, never physical folds'
MIN_AREA_M2 = 30.
SIMPLIFY_M = .25
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
NUMBERED = re.compile(r'\d+[A-Za-z]?')
IMPORT_INSTRUCTIONS = (
    'Import as a draft: open /scenes?scene=<scene>&object=<object>&status=all with Property boundaries checked, '
    'press Add object and trace the lot outline along the dashed lot line (the PNG sheet shows the same cut); the save is a '
    'reviewer draft, the original outline stays until superseded. The /scenes/candidates import flow can package lots[].ring_px '
    'from this report in a later step; image_sha256 and model_signature are recorded so a changed image or frame is refused.')


def units_folder(root):
    return Path(root)/'data/reviews/eyes/units'


def load_parcels(root, scene, fetch=None):
    """Approved parcels for the scene bounds; cached per (scene, image) under data/reviews/eyes/units.

    Reuses a frozen ``property_roofs.parcels`` payload from the scene when one is inherited
    (read-only) and otherwise performs one public lookup. Nothing under data/scene-parcels
    or data/scenes is written.
    """
    bounds = (scene.get('region') or {}).get('bounds')
    if not bounds:
        raise ValueError('No geographic bounds are available for this image.')
    folder = units_folder(root)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder/f"parcels-{scene['id']}.json"
    if path.exists():
        saved = json.loads(path.read_text())
        if saved.get('image_sha256') == scene['image_sha256'] and saved.get('bounds') == list(bounds) and saved.get('data'):
            return saved['data']
    data = None
    inherited = (scene.get('property_roofs') or {}).get('parcels')
    if inherited and inherited.get('bounds') == list(bounds):
        data = inherited
    if data is None:
        data = (fetch or fetch_parcels)(bounds)
    atomic_json(path, {'version': VERSION, 'scene_id': scene['id'], 'image_sha256': scene['image_sha256'],
                       'bounds': list(bounds), 'retrieved_at': data.get('retrieved_at'), 'data': data})
    return data


def _frames(scene):
    frame = np.asarray(scene['pixel_to_meters'], dtype=float)
    inverse = np.asarray(scene['meters_to_pixel'], dtype=float)

    def to_m(x, y, z=None):
        return (x*frame[0][0]+y*frame[1][0]+frame[2][0], x*frame[0][1]+y*frame[1][1]+frame[2][1])

    def to_px(x, y, z=None):
        return (x*inverse[0][0]+y*inverse[1][0]+inverse[2][0], x*inverse[0][1]+y*inverse[1][1]+inverse[2][1])
    return to_m, to_px


def _valid(geometry):
    return geometry if geometry.is_valid else geometry.buffer(0)


def lot_geometries(scene, parcels):
    """Approved lots as shapely polygons in local metres, keeping the Vicmap properties."""
    project = image_projection(scene)
    to_m, _ = _frames(scene)
    lots = []
    for feature in parcels.get('features', []):
        props = feature.get('properties', {})
        if props.get('status') != 'approved':
            continue
        try:
            geo = _valid(transform(to_m, transform(project, shape(feature['geometry']))))
        except (GEOSException, ValueError, TypeError, KeyError):
            continue
        if geo.is_empty or geo.area <= 0:
            continue
        lots.append({'props': props, 'geometry': geo})
    return lots


def _geometry_key(geometry):
    return json.dumps([[[round(v, 3) for v in p] for p in ring] for poly in rings(geometry) for ring in poly])


def stacked_groups(lots):
    """Lots sharing one identical polygon (stacked strata) grouped by geometry; only groups of two or more."""
    groups = {}
    for lot in lots:
        groups.setdefault(_geometry_key(lot['geometry']), []).append(lot)
    result = []
    for members in groups.values():
        if len(members) > 1:
            plans = sorted({str(m['props'].get('plan') or '') for m in members})
            result.append({'plan': plans[0] if len(plans) == 1 else '/'.join(plans),
                           'lots': sorted((str(m['props'].get('lot') or '') for m in members), key=_lot_order), 'count': len(members)})
    return sorted(result, key=lambda g: (g['plan'], g['lots']))


def _ring_lists(geometry, to_px):
    """[(ring_m, ring_px, holes_m, holes_px)] per polygon part."""
    parts = []
    for poly in polygons(geometry):
        outer_m = [list(p) for p in poly.exterior.coords[:-1]]
        holes_m = [[list(p) for p in hole.coords[:-1]] for hole in poly.interiors]
        parts.append((outer_m, [list(to_px(*p)) for p in outer_m], holes_m, [[list(to_px(*p)) for p in hole] for hole in holes_m]))
    return parts


def _lot_order(lot):
    text = str(lot or '')
    number = re.match(r'\d+', text)
    return (int(number.group()) if number else 10**9, text)


def _rounded(points, digits=3):
    return [[round(float(v), digits) for v in p] for p in points]


def assembly_components(scene, obj):
    """Physical pieces of the saved connected-roof hypothesis (detector masks), if any; not lots, not labels.

    Where stacked strata lots carry no cadastral cuts, these are the only saved evidence of separate
    roof surfaces. They come from ``boundary_refinement.assembly.components`` unchanged.
    """
    to_m, to_px = _frames(scene)
    components = ((obj.get('boundary_refinement') or {}).get('assembly') or {}).get('components') or []
    result = []
    for part in components:
        try:
            polygon = _valid(transform(to_m, Polygon(part['ring'], part.get('holes') or [])))
        except (GEOSException, ValueError, TypeError, KeyError):
            continue
        if polygon.is_empty or polygon.area < MIN_AREA_M2:
            continue
        rings_out = _ring_lists(polygon, to_px)
        outer_m, outer_px, holes_m, holes_px = max(rings_out, key=lambda r: Polygon(r[0]).area)
        result.append({'id': part.get('id'), 'score': part.get('score'), 'area_m2': round(polygon.area, 2),
                       'ring_m': _rounded(outer_m), 'ring_px': _rounded(outer_px),
                       'holes_m': [_rounded(h) for h in holes_m], 'holes_px': [_rounded(h) for h in holes_px],
                       'source': 'connected-roof-v1 assembly component (detector mask), not a cadastral lot'})
    return result


def split_outline(scene, obj, parcels):
    """Per-lot candidate outlines for one saved building outline; pure geometry, no image evidence."""
    to_m, to_px = _frames(scene)
    try:
        outline = _valid(transform(to_m, Polygon(obj['ring'], obj.get('holes') or [])))
    except (GEOSException, ValueError, TypeError) as error:
        outline, outline_error = Polygon(), str(error)[:200]
    else:
        outline_error = None if outline.area > 0 else 'The outline encloses no area.'
    report = {'version': VERSION, 'policy': POLICY, 'code_sha256': CODE_SHA256,
              'computed_at': datetime.now(UTC).isoformat(timespec='seconds'),
              'scene_id': scene.get('id'), 'object_id': obj.get('id'), 'image_sha256': scene.get('image_sha256'),
              'model_signature': scene.get('model_signature'), 'outline_area_m2': round(outline.area, 2),
              'min_area_m2': MIN_AREA_M2, 'simplify_m': SIMPLIFY_M, 'lots': [], 'dropped_pieces': [],
              'common_property': {'area_m2': 0., 'rings_m': [], 'rings_px': [], 'lots': []},
              'stacked_lot_groups': [], 'touching_lots': [], 'coverage_share': 0., 'status': 'no_lots', 'reason': '',
              'assembly_components': assembly_components(scene, obj),
              'parcel_source': {'source': parcels.get('source'), 'retrieved_at': parcels.get('retrieved_at'), 'bounds': parcels.get('bounds')},
              'overlap_m2': 0.,
              'note': 'Model estimate, not accuracy: lot cuts allocate detected roof area to cadastral lots; they are not measured party walls or roof folds.',
              'import_instructions': IMPORT_INSTRUCTIONS}
    if outline_error:
        report.update(status='invalid_outline', reason=f'The saved outline is not a usable polygon: {outline_error}')
        return report
    lots = lot_geometries(scene, parcels)
    touching = [lot for lot in lots if lot['geometry'].intersection(outline).area > .5]
    report['touching_lots'] = [{'lot': str(lot['props'].get('lot') or ''), 'plan': str(lot['props'].get('plan') or ''),
                                'pfi': str(lot['props'].get('pfi') or ''), 'lot_area_m2': round(lot['geometry'].area, 1),
                                'share_of_outline': round(lot['geometry'].intersection(outline).area/outline.area, 4),
                                'horizontal_uncertainty_m': lot['props'].get('horizontal_uncertainty_m')}
                               for lot in sorted(touching, key=lambda lot: -lot['geometry'].intersection(outline).area)]
    if not touching:
        report['reason'] = 'No approved lot touches this outline; nothing to allocate.'
        return report
    report['stacked_lot_groups'] = stacked_groups(touching)
    numbered = [lot for lot in touching if NUMBERED.fullmatch(str(lot['props'].get('lot') or ''))]
    common_lots = [lot for lot in touching if lot not in numbered]
    # One allocation unit per distinct lot polygon: stacked strata lots share one polygon and are cut
    # from their neighbours by cadastre, but never from each other.
    units_by_key = {}
    for lot in numbered:
        units_by_key.setdefault(_geometry_key(lot['geometry']), []).append(lot)
    allocation_units = [sorted(members, key=lambda m: _lot_order(m['props'].get('lot'))) for members in units_by_key.values()]
    if len(allocation_units) == 1 and len(allocation_units[0]) > 1 and len(touching) > 1:
        group = allocation_units[0]
        plans = sorted({str(m['props'].get('plan') or '') for m in group})
        report.update(status='no_cadastral_cuts',
                      reason=f"{len(touching)} stacked lots of plan {'/'.join(plans)} share one polygon; "
                             'Vicmap Parcel holds no cut lines between these units, so no per-lot candidate can be drawn from cadastre.'
                             + (f" The saved connected-roof hypothesis holds {len(report['assembly_components'])} physical components "
                                '(detector masks, not lots) that a reviewer can trace instead.' if report['assembly_components'] else ''))
        return report
    pieces = []
    for group in allocation_units:
        lead = group[0]
        label = ', '.join(str(m['props'].get('lot') or '') for m in group)
        plans = sorted({str(m['props'].get('plan') or '') for m in group})
        piece = _valid(outline.intersection(lead['geometry']))
        for part in polygons(piece):
            for simplified in polygons(_valid(part.simplify(SIMPLIFY_M, preserve_topology=True))):
                entry = {'lot': label, 'plan': '/'.join(plans), 'area_m2': round(simplified.area, 2)}
                if simplified.area < MIN_AREA_M2:
                    report['dropped_pieces'].append(entry)
                    continue
                pieces.append((group, label, '/'.join(plans), simplified))
    allocated = _valid(unary_union([piece for *_, piece in pieces])) if pieces else Polygon()
    for group, label, plan, piece in sorted(pieces, key=lambda item: (*_lot_order(item[0][0]['props'].get('lot')), item[2], -item[3].area)):
        lead = group[0]
        for outer_m, outer_px, holes_m, holes_px in _ring_lists(piece, to_px):
            report['lots'].append({'lot': label, 'lots': [str(m['props'].get('lot') or '') for m in group], 'stacked': len(group) > 1,
                                   'plan': plan, 'pfi': str(lead['props'].get('pfi') or ''), 'spi': lead['props'].get('spi'),
                                   'area_m2': round(piece.area, 2), 'lot_area_m2': round(lead['geometry'].area, 1),
                                   'share_of_outline': round(piece.area/outline.area, 4),
                                   'horizontal_uncertainty_m': lead['props'].get('horizontal_uncertainty_m'),
                                   'ring_m': _rounded(outer_m), 'ring_px': _rounded(outer_px),
                                   'holes_m': [_rounded(h) for h in holes_m], 'holes_px': [_rounded(h) for h in holes_px]})
    remainder = _valid(outline.difference(allocated)) if not allocated.is_empty else outline
    remainder_parts = [part for part in polygons(remainder) if part.area > .5]
    common = {'area_m2': round(sum(p.area for p in remainder_parts), 2), 'rings_m': [], 'rings_px': [],
              'lots': [str(lot['props'].get('lot') or '') for lot in common_lots if lot['geometry'].intersection(remainder).area > .5]}
    for part in remainder_parts:
        for outer_m, outer_px, holes_m, holes_px in _ring_lists(_valid(part.simplify(SIMPLIFY_M, preserve_topology=True)), to_px):
            common['rings_m'].append([_rounded(outer_m), *[_rounded(h) for h in holes_m]])
            common['rings_px'].append([_rounded(outer_px), *[_rounded(h) for h in holes_px]])
    report['common_property'] = common
    # Coverage is the allocated union over the outline, so overlapping or stacked lots cannot exceed 1.
    report['coverage_share'] = round(allocated.area/outline.area, 4) if outline.area else 0.
    report['overlap_m2'] = round(max(0., sum(lot['area_m2'] for lot in report['lots'])-allocated.area), 2)
    stacked_candidates = [lot for lot in report['lots'] if lot['stacked']]
    if report['lots']:
        report['status'] = 'split'
        report['reason'] = (f"{len(report['lots'])} per-lot candidates cover {report['coverage_share']:.0%} of the outline; "
                            f"{common['area_m2']} m2 remain unallocated (common property or outside numbered lots)."
                            + (f" {len(stacked_candidates)} candidate(s) are stacked strata polygons (lots "
                               f"{'; '.join(lot['lot'] for lot in stacked_candidates)}) with no unit cuts in Vicmap between their lots."
                               if stacked_candidates else ''))
    else:
        report['status'] = 'single_lot' if len(allocation_units) == 1 else 'no_lots'
        report['reason'] = ('One numbered lot touches this outline; no split is needed from cadastre.' if len(allocation_units) == 1
                            else 'No numbered lot piece reached the minimum area; nothing allocated.')
    return report


def render_sheet(image, scene, obj, report, path, scale=3, margin=40):
    """Original crop beside the outline with lot cuts, for review only (no image is altered as evidence)."""
    ring = np.asarray(obj['ring'], dtype=float)
    x0, y0 = np.floor(ring.min(0)-margin).astype(int)
    x1, y1 = np.ceil(ring.max(0)+margin).astype(int)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(image.size[0], x1), min(image.size[1], y1)
    if x1 <= x0 or y1 <= y0:
        raise ValueError('The outline lies outside the image; no crop to show.')
    crop = image.crop((x0, y0, x1, y1)).resize(((x1-x0)*scale, (y1-y0)*scale), Image.NEAREST)
    overlay = crop.copy()
    draw = ImageDraw.Draw(overlay, 'RGBA')

    def pts(points):
        return [((p[0]-x0)*scale, (p[1]-y0)*scale) for p in points]
    colours = [(251, 191, 36, 90), (96, 165, 250, 90), (52, 211, 153, 90), (244, 114, 182, 90), (167, 139, 250, 90), (251, 146, 60, 90)]
    for i, lot in enumerate(report['lots']):
        draw.polygon(pts(lot['ring_px']), fill=colours[i % len(colours)], outline=(251, 191, 36, 255))
        for hole in lot['holes_px']:
            draw.polygon(pts(hole), fill=(0, 0, 0, 120), outline=(251, 191, 36, 255))
    for part in report['common_property']['rings_px']:
        draw.polygon(pts(part[0]), fill=(148, 163, 184, 70), outline=(226, 232, 240, 255))
    if report['status'] != 'split':
        for i, part in enumerate(report.get('assembly_components') or []):
            draw.polygon(pts(part['ring_px']), fill=colours[i % len(colours)], outline=(226, 232, 240, 255))
    draw.polygon(pts(obj['ring']), outline=(34, 211, 238, 255))
    for hole in obj.get('holes') or []:
        draw.polygon(pts(hole), outline=(34, 211, 238, 255))
    font = ImageFont.load_default(size=max(11, 4*scale))
    labels = [(lot['ring_px'], f"Lots {lot['lot']} (stacked)" if lot['stacked'] else f"Lot {lot['lot']}") for lot in report['lots']]
    if report['status'] != 'split':
        labels += [(part['ring_px'], f"Roof part {i+1} · {part['area_m2']:.0f} m2") for i, part in enumerate(report.get('assembly_components') or [])]
    for ring_px, label in labels:
        centre = Polygon(ring_px).representative_point()
        box = draw.textbbox((0, 0), label, font=font)
        cx, cy = (centre.x-x0)*scale-(box[2]-box[0])/2, (centre.y-y0)*scale-(box[3]-box[1])/2
        draw.rectangle((cx-3, cy-2, cx+box[2]-box[0]+3, cy+box[3]-box[1]+2), fill=(15, 23, 42, 200))
        draw.text((cx, cy), label, fill=(251, 191, 36, 255), font=font)
    reason_lines = textwrap.wrap(report['reason'], 150)[:3] if report['status'] != 'split' else []
    header = 64+14*len(reason_lines)
    sheet = Image.new('RGB', (crop.size[0]*2+3*12, crop.size[1]+header+12), (15, 23, 42))
    sheet.paste(crop, (12, header))
    sheet.paste(overlay, (crop.size[0]+24, header))
    text = ImageDraw.Draw(sheet)
    title = f"{report['scene_id']} / {report['object_id']} · {report['status']}"
    text.text((12, 10), title, fill=(226, 232, 240), font=ImageFont.load_default(size=14))
    detail = (f"{len(report['lots'])} per-lot candidates, coverage {report['coverage_share']:.0%} of {report['outline_area_m2']} m2; "
              f"{report['common_property']['area_m2']} m2 unallocated. Cadastral cuts are allocation edges, not roof folds. Model estimate, not accuracy.")
    text.text((12, 30), detail[:170], fill=(148, 163, 184), font=ImageFont.load_default(size=11))
    for i, line in enumerate(reason_lines):
        text.text((12, 46+14*i), line, fill=(251, 191, 36), font=ImageFont.load_default(size=11))
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)
    return path


def run(root, scene_id, object_id, fetch=None):
    """Split one saved outline; writes <root>/data/reviews/eyes/units/<scene>/<object>.{json,png} only."""
    from .scene_roof_diagnostics import load_scene
    root = Path(root)
    scene, image = load_scene(scene_id, root)
    obj = next((o for o in scene.get('objects', []) if o.get('id') == object_id), None)
    if obj is None:
        raise ValueError(f'Object {object_id} is not in scene {scene_id}.')
    parcels = load_parcels(root, scene, fetch=fetch)
    report = split_outline(scene, obj, parcels)
    folder = units_folder(root)/scene_id
    folder.mkdir(parents=True, exist_ok=True)
    try:
        sheet = render_sheet(image, scene, obj, report, folder/f'{object_id}.png')
        report['sheet'] = str(sheet.relative_to(root)) if sheet.is_relative_to(root) else str(sheet)
    except (ValueError, OSError, GEOSException) as error:
        report['sheet'], report['sheet_error'] = None, f'Sheet not rendered: {str(error)[:200]}'
    atomic_json(folder/f'{object_id}.json', report)
    return report


def _summary(report):
    lots = ', '.join(f"{lot['lot']} ({lot['area_m2']} m2)" for lot in report['lots'])
    return (f"{report['scene_id']} {report['object_id']}: {report['status']}; {len(report['lots'])} lots"
            f"{' [' + lots + ']' if lots else ''}; coverage {report['coverage_share']:.0%} of {report['outline_area_m2']} m2; "
            f"unallocated {report['common_property']['area_m2']} m2; touching lots {len(report['touching_lots'])}"
            f"{'; stacked groups ' + str(report['stacked_lot_groups']) if report['stacked_lot_groups'] else ''}"
            f"{'; saved roof parts ' + str(len(report['assembly_components'])) if report['assembly_components'] else ''}. Model estimate, not accuracy.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--objects', nargs='*', default=[], help='SCENE:OBJECT pairs')
    parser.add_argument('--feedback', type=Path, help='user-feedback JSON with items[{scene_id, object_id, issues}]')
    parser.add_argument('--issue', default='units-should-split', help='only feedback items carrying this issue tag')
    args = parser.parse_args(argv)
    targets = [tuple(pair.split(':', 1)) for pair in args.objects]
    if args.feedback:
        doc = json.loads(args.feedback.read_text())
        targets += [(item['scene_id'], item['object_id']) for item in doc.get('items', []) if args.issue in (item.get('issues') or [])]
    if not targets:
        parser.error('give --objects or --feedback')
    failures = 0
    for scene_id, object_id in targets:
        try:
            print(_summary(run(args.root, scene_id, object_id)))
        except (OSError, ValueError, KeyError, GEOSException) as error:
            failures += 1
            print(f'{scene_id} {object_id}: error {str(error)[:200]}')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
