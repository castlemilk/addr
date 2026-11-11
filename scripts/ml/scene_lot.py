"""Lot-aware frames: plan a registered Google frame around an address's lot or its unit-site block.

A cohort frame (roof_training_250-*) is a 512 px crop of about 33 m fitted around the
CRM roof, so a lot of 40-60 m and its neighbours leave the image and objects get cut by
the frame rather than by any real boundary. This module plans the frame that would hold
the whole lot: the address lot alone ('lot'), or the spatially connected component of
the address plan when that plan carries common property ('plan-block', a unit site);
'auto' picks plan-block when a CM lot exists. Bounds are the group's union bounds,
squared, grown by 1.4 and 10 m, clamped to the 10-250 m capture limits; the zoom,
span and metres per pixel come from scene_region.region_frame, so the plan describes
exactly the frame capture_region would build.

lot_frame is pure and prints as a dry run. lot_source captures for real and writes
only under data/scenes/lot-<address_id>/ (refusing an existing id). A new frame changes
image_sha256 and both affines, so recoverSceneReview returns null for it; with
carry_from (--carry-from <scene id>) lot_source also records frame_transform
(scene_recapture: the georeferenced affine from that scene's frame and the image
residual measured against the pre-registered DESIGN.json) so a later carry can
re-project saved decisions, every one returning as needs_adjustment. A capture whose
measured frame_transform fails the design is refused and nothing is saved, unless
allow_unverified (--allow-unverified) keeps it with verified false, so no carry can use it.
No detector runs here and the source scene is never written. The cohort crops
(roof_training_250-*, about 0.063 m/px) are a Lanczos ~3.7x upsample of a z19 composite
(median of up to two providers by design; 0.236 m/px native; cohort latest.json records
no imagery provenance, so the provider count is not a per-scene fact), so a Google z20
frame (0.118 m/px native; 0.14-0.17 m/px in the 512 px frame) is finer in native detail
than the crop, never coarser, although its pixels are larger; the plan sentence compares
native detail, not pixel spacing.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
from shapely.geometry import Point, Polygon, box, shape
from shapely.ops import transform, unary_union

from .roof_block_context import plan_component
from .scene_addresses import WORLD, image_projection
from .scene_batch import signature
from .scene_confidence import geographic
from .scene_lot_tags import (
    address_row,
    crm_record,
    crm_ring_lonlat,
    fetch_frozen,
    inverse_projection,
    is_common,
    is_numbered,
    lot_label,
    lot_order,
    lot_text,
    pad_bounds,
    parcels_for_scene,
    registration,
)
from .scene_object_quality import _live
from .scene_region import capture_region, region_frame
from .scene_self_review import GEOMETRY_ERRORS, validate_id

VERSION = 'lot-frame-v1'
GROUPS = ('auto', 'lot', 'plan-block')
FRAME_PX = 512
DESIGN = {'seed_overlap': .10, 'grow': 1.4, 'margin_m': 10., 'min_span_m': 10., 'max_span_m': 250., 'gap_m': .5,
          'fetch_pad_deg': .0012, 'provider': 'google'}
PARCELS_DIR = Path('data/reviews/eyes/parcels')
# Cohort crops come from rooflines.RooflineExtractor(zoom=18, imagery_offset=1): z19 tiles of google and esri median
# composited by design (ortho.DEFAULT_PROVIDERS; rooflines falls back to a single provider when the composite fails, and
# latest.json records neither), then a Lanczos resize of the roof window to 512 px. Their native detail is z19.
COHORT_NATIVE_ZOOM = 19


class UnverifiedRecapture(ValueError):
    """A carry_from capture whose measured frame_transform fails DESIGN.json: nothing was saved."""

    def __init__(self, reasons):
        from .scene_recapture import FAILURE
        self.reasons = list(reasons)
        super().__init__('\n'.join([FAILURE, *(f'reason: {reason}' for reason in self.reasons),
                                    'nothing was saved; --allow-unverified keeps the capture (verified false, never carried)']))


def native_m_per_px(lat, zoom):
    """Metres per native tile pixel at a latitude and zoom (scene_region.region_frame's native_meters_per_pixel)."""
    return WORLD*math.cos(math.radians(lat))/(256*2**zoom)


def native_comparison(target_native, source_native):
    """How a frame's native detail compares with another's: finer, the same (within 2%) or coarser."""
    if abs(target_native/source_native-1) < .02:
        return 'the same native detail'
    return 'finer in native detail' if target_native < source_native else 'coarser in native detail'


def cohort_wording(m_per_px, native):
    """A cohort crop described by what it is: a resampled z19 composite (up to two providers by design), not native detail."""
    factor = native/m_per_px
    resample = f'~{factor:.1f}x upsample' if factor >= 1 else f'~{1/factor:.1f}x downsample'
    return (f'a Lanczos {resample} of a z{COHORT_NATIVE_ZOOM} composite (median of up to two providers by design; '
            f'{native:.3f} m/px native)')


def native_detail(plan):
    """The lot frame's native detail against the source frame's (pixel spacing alone misleads: cohort crops are upsampled)."""
    native, source_native, source_px = plan.get('native_m_per_px'), plan.get('source_native_m_per_px'), plan['source_m_per_px']
    if native is None or source_native is None:
        return f"{plan['m_per_px']:.2f} m/px pixels against this {source_px:.3f} m/px frame (native detail unknown)"
    comparison = native_comparison(native, source_native)
    if plan.get('source_registration') == 'fitted':
        source = f"this {source_px:.3f} m/px crop, {cohort_wording(source_px, source_native)}"
    else:
        source = f"this {source_px:.3f} m/px frame (Google z{plan.get('source_zoom')}, {source_native:.3f} m/px native)"
    joiner = 'as' if comparison == 'the same native detail' else 'than'
    return f"{comparison} (z{plan['zoom']}, {native:.3f} m/px) {joiner} {source}"


def _local_frame(lon, lat):
    """A metre frame centred on a point (x east, y north) in the projection scene_addresses.image_projection uses."""
    return {'centroid': {'lon': lon, 'lat': lat}, 'meters_to_pixel': [[1, 0], [0, 1], [0, 0]], 'pixel_to_meters': [[1, 0], [0, 1], [0, 0]]}


def _parcels_for_point(lon, lat, root, *, offline):
    key = signature(['address-point', round(lon, 7), round(lat, 7)])
    path = Path(root)/PARCELS_DIR/f'{key}.json'
    if path.exists():
        saved = json.loads(path.read_text())
        if saved.get('key') == key and saved.get('sha256') == signature(saved.get('data')):
            return saved['data'], saved['sha256'], 'eyes-parcels'
    if offline:
        return None, None, 'unavailable'
    data = fetch_frozen(pad_bounds([lon, lat, lon, lat], DESIGN['fetch_pad_deg']), path, key)
    return data, signature(data), 'vicmap'


def _seed_shape(record, project, lon, lat):
    if record and record.get('ring_meters') and record.get('centroid'):
        ring = [project(x, y) for x, y in crm_ring_lonlat(record)]
        polygon = Polygon(ring)
        if polygon.is_valid and polygon.area > 0:
            return polygon, 'crm-seed'
    return Point(project(lon, lat)), 'address-point'


def _overlap(seed, polygon):
    if seed.geom_type == 'Point':
        return 1. if polygon.covers(seed) else 0.
    return float(seed.intersection(polygon).area/seed.area) if seed.area > 0 else 0.


def lot_frame(address_id, *, root=Path('.'), parcels=None, group='auto', source_scene_id=None, source_scene=None, offline=False):
    """The lot-frame plan for an address (pure): lots, plan, group kind, span, bounds, zoom, m/px, objects inside, sentence."""
    if group not in GROUPS:
        raise ValueError(f'group must be one of {GROUPS}')
    if isinstance(address_id, bool) or not isinstance(address_id, int) or address_id <= 0:
        raise ValueError('Choose a valid address')
    root = Path(root)
    row = address_row(root, address_id)
    if not row:
        raise ValueError('Address not found')
    title, lon, lat = row
    if source_scene is None and source_scene_id:
        source_scene = json.loads((root/'data/scenes'/validate_id(source_scene_id)/'latest.json').read_text())
    source_id = source_scene['id'] if source_scene else None
    parcels_sha256, source = (signature(parcels), 'injected') if parcels is not None else (None, None)
    if parcels is None and source_scene is not None:
        parcels, parcels_sha256, source = parcels_for_scene(source_scene, root, offline=offline)
    if parcels is None:
        parcels, parcels_sha256, source = _parcels_for_point(lon, lat, root, offline=offline)
    if parcels is None:
        raise ValueError('Parcel evidence is unavailable for this address (offline and nothing frozen).')
    local = _local_frame(lon, lat)
    project = image_projection(local)
    features = []
    for feature in parcels.get('features', []):
        props = feature.get('properties') or {}
        try:
            geo = shape(feature['geometry'])
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        if props.get('status', 'approved') == 'approved' and geo.is_valid and not geo.is_empty:
            features.append((props, transform(project, geo), geo))
    record = crm_record(root, address_id)
    seed, seed_method = _seed_shape(record, project, lon, lat)
    overlaps = sorted(((_overlap(seed, poly), i) for i, (_, poly, _) in enumerate(features)), reverse=True)
    seed_indices = [i for share, i in overlaps if share >= DESIGN['seed_overlap']]
    if not seed_indices:
        raise ValueError('No approved lot covers the address seed; check the address point or the parcel coverage.')
    numbered_seed = [i for i in seed_indices if is_numbered(lot_text(features[i][0]))] or seed_indices
    seed_plan = features[numbered_seed[0]][0].get('plan')
    pool_indices = [i for i, (props, _, _) in enumerate(features) if props.get('plan') == seed_plan]
    pool = [features[i] for i in pool_indices]
    has_common = any(is_common(lot_text(props)) for props, _, _ in pool)
    group_kind = group if group != 'auto' else ('plan-block' if has_common else 'lot')
    if group_kind == 'plan-block' and has_common:
        seeds = [pool_indices.index(i) for i in seed_indices if i in pool_indices]
        members = [pool[i] for i in sorted(plan_component(pool, seeds, gap_px=DESIGN['gap_m']))]
    else:
        # The seed lot alone: the numbered lot(s) under the seed, never the common property it brushes.
        group_kind = 'lot'
        members = [features[i] for i in numbered_seed]
    union = unary_union([poly for _, poly, _ in members])
    minx, miny, maxx, maxy = union.bounds
    union_span_m = max(maxx-minx, maxy-miny)
    span_m = min(DESIGN['max_span_m'], max(DESIGN['min_span_m'], union_span_m*DESIGN['grow']+DESIGN['margin_m']))
    cx, cy = (minx+maxx)/2, (miny+maxy)/2
    unproject = inverse_projection(local)
    west, south = unproject(cx-span_m/2, cy-span_m/2)
    east, north = unproject(cx+span_m/2, cy+span_m/2)
    requested = [west, south, east, north]
    frame = region_frame(requested, DESIGN['provider'])
    region = frame['region']
    m_per_px = region['span_m']/FRAME_PX
    lots = sorted(({'pfi': str(props.get('pfi')), 'lot': lot_text(props), 'plan': props.get('plan')} for props, _, _ in members),
                  key=lambda lot: lot_order(lot['lot']))
    plan = {'version': VERSION, 'address_id': address_id, 'title': title, 'address_point': [lon, lat], 'seed': seed_method,
            'seed_lot': lot_text(features[numbered_seed[0]][0]), 'plan': seed_plan, 'group_kind': group_kind, 'group_requested': group,
            'plan_has_common_property': has_common, 'lots': lots, 'lots_label': lot_label([lot['lot'] for lot in lots]),
            'union_span_m': round(union_span_m, 2), 'span_m': round(region['span_m'], 2), 'm_per_px': round(m_per_px, 4),
            'native_m_per_px': round(region['native_meters_per_pixel'], 4),
            'requested_bounds': [round(v, 7) for v in requested], 'bounds': region['bounds'], 'zoom': region['imagery_zoom'],
            'provider': DESIGN['provider'], 'frame_px': FRAME_PX, 'centroid': frame['centroid'],
            'parcels_sha256': parcels_sha256, 'parcels_source': source, 'source_scene_id': source_id, 'objects_inside': [],
            'refit_proposed': True}
    if source_scene is not None:
        source_px = math.sqrt(abs(float(np.linalg.det(np.asarray(source_scene['pixel_to_meters'], dtype=float)[:2]))))
        plan['source_m_per_px'] = round(source_px, 4)
        plan['source_registration'] = registration(source_scene)
        source_region = source_scene.get('region') or {}
        if plan['source_registration'] == 'region':
            plan['source_zoom'] = source_region.get('imagery_zoom')
            native = source_region.get('native_meters_per_pixel') or (
                native_m_per_px(source_scene['centroid']['lat'], plan['source_zoom']) if plan['source_zoom'] else source_px)
        else:
            plan['source_zoom'] = COHORT_NATIVE_ZOOM
            native = native_m_per_px(source_scene['centroid']['lat'], COHORT_NATIVE_ZOOM)
        plan['source_native_m_per_px'] = round(float(native), 4)
        target = box(*region['bounds'])
        for obj in _live(source_scene):
            if obj.get('status') in ('rejected', 'superseded'):
                continue
            try:
                if target.covers(geographic(obj, source_scene)):
                    plan['objects_inside'].append(obj['id'])
            except GEOMETRY_ERRORS:
                continue
        source_box = box(*geographic({'ring': [[0, 0], [source_scene['image_width'], 0],
                                               [source_scene['image_width'], source_scene['image_height']], [0, source_scene['image_height']]]},
                                     source_scene).bounds)
        # The chosen group (the lot, or the whole unit-site block) must sit inside the source frame for no refit to be needed.
        group_union = unary_union([geo for _, _, geo in members])
        plan['group_inside_source'] = bool(source_box.covers(group_union))
        plan['refit_proposed'] = not plan['group_inside_source']
        review_path = root/'data/scenes'/validate_id(source_id)/'review.json'
        revision = None
        if review_path.exists():
            try:
                revision = json.loads(review_path.read_text()).get('revision')
            except (OSError, ValueError):
                revision = None
        plan['source_review_revision'] = revision
    plan['sentence'] = plan_sentence(plan)
    return plan


def plan_sentence(plan):
    text = (f"A lot frame for {plan['address_id']} would be {plan['span_m']:.0f} m at {plan['m_per_px']:.2f} m/px "
            f"(Google z{plan['zoom']}, {plan['frame_px']} px), covering {plan['lots_label']} of {plan['plan']}")
    source_px = plan.get('source_m_per_px')
    if source_px is None:
        return text+'.'
    if plan.get('group_inside_source'):
        noun = 'block' if plan['group_kind'] == 'plan-block' else 'lot'
        text = (f"The address {noun} ({plan['lots_label']} of {plan['plan']}, {plan['union_span_m']:.1f} m) already sits inside this frame; "
                f"no refit is proposed. A lot frame would be {plan['span_m']:.0f} m at {plan['m_per_px']:.2f} m/px (Google z{plan['zoom']}).")
    else:
        text += f'; {native_detail(plan)}.'
    revision = plan.get('source_review_revision')
    if revision is not None:
        text += f" Reviews on {plan['source_scene_id']} (rev {revision}) cannot be carried onto a new frame."
    else:
        text += f" No saved review on {plan['source_scene_id']}."
    return text


def lot_source(address_id, group='auto', *, root=Path('.'), fetcher=None, source_scene_id=None, parcels=None, offline=False,
               carry_from=None, allow_unverified=False):
    """Capture the planned lot frame as scene lot-<address_id>; writes only under data/scenes/lot-<address_id>/.

    carry_from names the scene whose frame the capture replaces (e.g. roof_training_250-30371; it also informs the plan
    when source_scene_id is not given). Its saved snapshot, its image bytes and the pre-registered DESIGN.json are
    checked before any tile is fetched; frame_transform (scene_recapture.measured_transform: the georeferenced affine and
    the residual measured from both images' bytes) is written into the new scene before save_scene. When it fails the
    design, UnverifiedRecapture is raised with the verdict's reasons and nothing is saved; allow_unverified keeps the
    capture with verified false, so no carry can use it. Nothing is carried here and no detector runs; the source scene
    and its review are never written.
    """
    from .scene_extract import save_scene
    root = Path(root)
    scene_id = f'lot-{address_id}'
    target = root/'data/scenes'/scene_id
    if target.exists():
        raise ValueError(f'{target} exists; a lot frame is never overwritten. Choose another address or remove it by hand.')
    if allow_unverified and carry_from is None:
        raise ValueError('allow_unverified applies only to a carry_from capture')
    source = None
    if carry_from is not None:
        from .scene_recapture import carry_source, load_design, measured_transform, verdict
        source, source_png = carry_source(root, carry_from)
        design = load_design(root)
        source_scene_id = source_scene_id or carry_from
    plan = lot_frame(address_id, root=root, parcels=parcels, group=group, source_scene_id=source_scene_id, offline=offline)
    scene, image, data = capture_region(plan['requested_bounds'], DESIGN['provider'], root=root, fetcher=fetcher)
    scene.update(id=scene_id, address_id=address_id, title=plan['title'],
                 lot_frame={key: plan[key] for key in ('version', 'plan', 'group_kind', 'lots', 'lots_label', 'span_m', 'm_per_px',
                                                       'union_span_m', 'parcels_sha256', 'parcels_source', 'source_scene_id', 'seed')})
    if source is not None:
        measured = measured_transform(source, scene, source_png, data, design)
        if not measured['verified'] and not allow_unverified:
            raise UnverifiedRecapture(verdict(measured, design[0])[1])
        scene['frame_transform'] = measured
    return save_scene(scene, data, root), image


def main(argv=None):
    parser = argparse.ArgumentParser(description='Plan (dry run) or capture a lot-aware registered frame for an address. '
                                                 'The plan is a description; nothing is captured without --capture.')
    parser.add_argument('--root', default='.')
    parser.add_argument('--address', type=int, required=True)
    parser.add_argument('--source-scene', help='Scene whose parcels and objects inform the plan (e.g. roof_training_250-30371).')
    parser.add_argument('--group', choices=GROUPS, default='auto')
    parser.add_argument('--offline', action='store_true', help='Never read Vicmap; fail when nothing frozen covers the address.')
    parser.add_argument('--dry-run', action='store_true', help='Print the plan only (the default).')
    parser.add_argument('--capture', action='store_true', help='Capture the frame as data/scenes/lot-<address>/ (never overwrites).')
    parser.add_argument('--carry-from', help='With --capture: the scene whose frame this capture replaces; records frame_transform '
                                             '(georeferenced affine + measured residual, scene_recapture). Nothing is carried or detected. '
                                             'A frame that fails DESIGN.json is refused (exit 1, nothing saved).')
    parser.add_argument('--allow-unverified', action='store_true',
                        help='With --carry-from: keep a capture whose frame fails the design (verified false; no carry can use it).')
    args = parser.parse_args(argv)
    if args.capture and args.dry_run:
        parser.error('choose --dry-run or --capture')
    if args.carry_from and not args.capture:
        parser.error('--carry-from records a frame_transform at capture time: use it with --capture')
    if args.allow_unverified and not args.carry_from:
        parser.error('--allow-unverified applies only to a --carry-from capture')
    root = Path(args.root)
    if args.capture:
        try:
            scene, _ = lot_source(args.address, args.group, root=root, source_scene_id=args.source_scene, offline=args.offline,
                                  carry_from=args.carry_from, allow_unverified=args.allow_unverified)
        except UnverifiedRecapture as error:
            sys.stderr.write(f'{error}\n')
            return 1
        measured = scene.get('frame_transform')
        if measured and measured.get('verified') is not True:
            from .scene_recapture import verdict
            sys.stderr.write(f"saved {scene['id']} with a frame_transform that is not verified (--allow-unverified): nothing may be "
                             'carried onto it\n'+''.join(f'reason: {reason}\n' for reason in verdict(measured, measured['design'])[1]))
        json.dump({'captured': scene['id'], 'image_sha256': scene['image_sha256'], 'lot_frame': scene['lot_frame'],
                   'frame_transform': scene.get('frame_transform')}, sys.stdout, indent=1)
    else:
        plan = lot_frame(args.address, root=root, group=args.group, source_scene_id=args.source_scene, offline=args.offline)
        json.dump({'dry_run': True, **plan}, sys.stdout, indent=1)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
