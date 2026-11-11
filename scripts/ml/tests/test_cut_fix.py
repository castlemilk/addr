"""Tests for the cut-fix prototype (scripts/ml/experiments/cut_fix.py). Synthetic frames only: nothing under data/scenes is read."""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw
from scripts.ml.experiments import cut_fix as cf
from scripts.ml.experiments.cut_census import pixels_to_mercator
from scripts.ml.scene_extract import app_ring_issues
from shapely.geometry import LineString, Point, Polygon, box

GROUND = (150, 140, 120)
ROOF = (185, 60, 50)
CANOPY = (35, 85, 40)
W = H = 200
S = .1


def latest(objects, width=W, height=H, s=S, scene_id='win-test'):
    return {'id': scene_id, 'image_width': width, 'image_height': height, 'centroid': {'lon': 145.3, 'lat': -37.8},
            'pixel_to_meters': [[s, 0.], [0., -s], [-width*s/2, height*s/2]], 'objects': objects, 'image_sha256': 'x'}


def paint(width=W, height=H, roofs=(), canopies=(), extra=()):
    image = Image.new('RGB', (width, height), GROUND)
    draw = ImageDraw.Draw(image)
    for ring in roofs:
        draw.polygon([tuple(p) for p in ring], fill=ROOF)
    for colour, ring in extra:
        draw.polygon([tuple(p) for p in ring], fill=colour)
    for ring in canopies:
        draw.polygon([tuple(p) for p in ring], fill=CANOPY)
    return image


def obj(oid, kind, ring, status='candidate', **extra):
    return {'id': oid, 'kind': kind, 'label': kind.title(), 'ring': [list(map(float, p)) for p in ring], 'status': status,
            'source': 'sam3-text-onnx', **extra}


def ring_of(polygon):
    return [list(p) for p in polygon.exterior.coords[:-1]]


def make_ctx(tmp_path, objects, image, *, review=True, width=W, height=H, s=S, model_objects=None):
    folder = tmp_path/'win-test'
    folder.mkdir(exist_ok=True)
    model = latest(model_objects if model_objects is not None else objects, width, height, s)
    return cf.context_from(model, {'objects': objects} if review else None, image, folder)


RECT = box(40, 60, 140, 140)


def corner_case(tmp_path, *, status='candidate', blocker=None):
    tree = Point(145, 145).buffer(15, 32)
    roof = RECT.difference(tree)
    objects = [obj('roof', 'building', ring_of(roof), status=status), obj('tree', 'tree', ring_of(tree))]
    extra = []
    if blocker is not None:
        objects.append(obj('shed', 'building', ring_of(blocker)))
    image = paint(roofs=[ring_of(RECT)], canopies=[ring_of(tree)], extra=extra)
    return make_ctx(tmp_path, objects, image), roof


def test_canopy_corner_completion_restores_the_hidden_corner(tmp_path):
    ctx, roof = corner_case(tmp_path)
    cuts = cf.canopy_completions(ctx['by_id']['roof'], ctx)
    accepted = [c for c in cuts if not c['refusals']]
    assert len(accepted) == 1, [c['refusals'] for c in cuts]
    cut = accepted[0]
    assert cut['geometry'] == 'corner'
    assert cut['corner_px'] == pytest.approx([140., 140.], abs=.6)
    assert cut['partners'] == ['tree']
    after = Polygon(cut['ring'])
    assert after.intersection(RECT).area/after.union(RECT).area > .99
    assert cut['completion_canopy_share'] >= .99
    assert cut['hidden_depth_m'] <= 2.
    assert app_ring_issues(cut['ring'], cut['holes'], W, H) == []


def test_corner_completion_refused_over_another_building(tmp_path):
    ctx, _ = corner_case(tmp_path, blocker=box(133, 133, 150, 150))
    cuts = cf.canopy_completions(ctx['by_id']['roof'], ctx)
    assert cuts and all(c['refusals'] for c in cuts)
    assert any(r.startswith('over_') and 'building' in r for c in cuts for r in c['refusals'])


def test_visible_chamfer_is_not_completed(tmp_path):
    """A real chamfered roof (the imagery shows ground at the corner) next to a tree that does not hide the corner."""
    chamfer = Polygon([(40, 60), (140, 60), (140, 122), (122, 140), (40, 140)])
    tree = Point(148, 148).buffer(12, 32)           # tangent-ish to the chamfer from outside; the corner region stays visible ground
    roof = chamfer.difference(tree)
    objects = [obj('roof', 'building', ring_of(roof)), obj('tree', 'tree', ring_of(tree))]
    image = paint(roofs=[ring_of(chamfer)], canopies=[ring_of(tree)])
    ctx = make_ctx(tmp_path, objects, image)
    cuts = cf.canopy_completions(ctx['by_id']['roof'], ctx)
    assert all(c['refusals'] for c in cuts)


def test_straight_eave_notch_under_a_canopy_is_closed(tmp_path):
    tree = Point(90, 147).buffer(12, 32)
    roof = RECT.difference(tree)
    objects = [obj('roof', 'building', ring_of(roof)), obj('tree', 'tree', ring_of(tree))]
    image = paint(roofs=[ring_of(RECT)], canopies=[ring_of(tree)])
    ctx = make_ctx(tmp_path, objects, image)
    cuts = cf.canopy_completions(ctx['by_id']['roof'], ctx)
    accepted = [c for c in cuts if not c['refusals']]
    assert len(accepted) == 1, [c['refusals'] for c in cuts]
    assert accepted[0]['geometry'] == 'straight'
    after = Polygon(accepted[0]['ring'])
    assert after.intersection(RECT).area/after.union(RECT).area > .99


def test_completion_leaving_the_window_is_refused(tmp_path):
    ctx, roof = corner_case(tmp_path)
    obj_ = ctx['by_id']['roof']
    tree = ctx['polys']['tree']
    canopy_b = tree.buffer(cf.DESIGN['canopy_buffer_m']/S)
    ring = [[40, 60], [140, 60], [140, 130], [201, 201], [130, 140], [40, 140]]
    out = cf.evaluate_completion(obj_, ctx, roof, ring, [], tree, canopy_b, [[140, 130], [130, 140]], np.array([201., 201.]))
    assert 'leaves_window' in out['refusals'] or 'invalid' in out['refusals'] or 'app_ring_rules' in out['refusals']


def test_eave_completion_geometry():
    assert cf.eave_completion(np.array([0., 0.]), np.array([10., 0.]), np.array([12., 2.]), np.array([12., 12.]), .1)[0] == 'corner'
    kind, corner = cf.eave_completion(np.array([0., 0.]), np.array([10., 0.]), np.array([12., 2.]), np.array([12., 12.]), .1)
    assert corner == pytest.approx([12., 0.])
    assert cf.eave_completion(np.array([0., 0.]), np.array([10., 0.]), np.array([14., .5]), np.array([24., .5]), .1)[0] == 'straight'
    assert cf.eave_completion(np.array([0., 0.]), np.array([10., 0.]), np.array([14., 9.]), np.array([24., 9.]), .1) == \
        (None, 'parallel_offset_eaves')
    assert cf.eave_completion(np.array([0., 0.]), np.array([10., 0.]), np.array([12., 2.]), np.array([22., 10.4]), .1)[1] == 'corner_angle'


def test_decided_object_gets_replaces_on_accept_only(tmp_path):
    for status, replaces, on_accept, tier in (('confirmed', [], ['roof'], True), ('candidate', ['roof'], [], False)):
        ctx, _ = corner_case(tmp_path, status=status)
        o = ctx['by_id']['roof']
        prop, info = cf.proposal_object(o, ctx, o['ring'], [], 'canopy_completion', 0)
        assert prop['replaces'] == replaces and prop['replaces_on_accept'] == on_accept and prop['derived_from'] == ['roof']
        assert prop['status'] == 'proposal' and prop['source'] == cf.VERSION and prop['geometry_reviewed'] is False
        assert info['decided'] is tier


def test_reviewer_edited_ring_is_decided(tmp_path):
    ctx_objects = [obj('roof', 'building', ring_of(RECT))]
    model_objects = [obj('roof', 'building', ring_of(box(40, 60, 141, 140)))]
    ctx = make_ctx(tmp_path, ctx_objects, paint(roofs=[ring_of(RECT)]), model_objects=model_objects)
    info = cf.decided_info(ctx['by_id']['roof'], ctx)
    assert info['decided'] and 'reviewer_edited_ring' in info['reasons']


def notch_roof(chord=2., depth=8.):
    x = 60.
    return [[40, 0], [x, 0], [x+chord/2, depth], [x+chord, 0], [120, 0], [120, 50], [40, 50]]


def test_frame_notch_is_filled_back_to_the_frame(tmp_path):
    ring = notch_roof()
    objects = [obj('roof', 'building', ring)]
    ctx = make_ctx(tmp_path, objects, paint(roofs=[ring]))
    fixes, held = cf.spike_fixes(ctx['by_id']['roof'], ctx)
    assert held == []
    assert len(fixes) == 1 and fixes[0]['type'] == 'spike_fill' and not fixes[0]['refusals']
    out = fixes[0]['ring']
    assert all(p[1] == 0 or p[1] >= 50 for p in out)
    assert Polygon(out).area == pytest.approx(80*50, abs=1e-6)
    assert fixes[0]['added_m2'] == pytest.approx(.08, abs=1e-3)
    assert app_ring_issues(out, [], W, H) == []


def test_frame_notch_held_when_another_object_fills_it(tmp_path):
    ring = notch_roof(chord=10., depth=20.)
    objects = [obj('roof', 'building', ring), obj('chimney', 'building', ring_of(box(60, 0, 70, 20)))]
    ctx = make_ctx(tmp_path, objects, paint(roofs=[ring]))
    fixes, _ = cf.spike_fixes(ctx['by_id']['roof'], ctx)
    assert len(fixes) == 1 and any(r.startswith('over_') for r in fixes[0]['refusals'])
    assert 'ring' not in fixes[0]


def test_frame_protrusion_is_held_not_removed(tmp_path):
    ring = [[40, 30], [78, 30], [78, 0], [80, 0], [80, 30], [120, 30], [120, 80], [40, 80]]
    objects = [obj('roof', 'building', ring)]
    ctx = make_ctx(tmp_path, objects, paint(roofs=[ring]))
    fixes, held = cf.spike_fixes(ctx['by_id']['roof'], ctx)
    assert fixes == []
    assert len(held) == 1 and held[0]['reason'] == 'protrusion_held'


def test_finish_ring_repairs_and_refuses():
    ring, holes, poly, issues, repairs = cf.finish_ring([[0, 0], [10, 0], [10, 0], [10, 10], [5, 10], [0, 10]], [], 20, 20)
    assert issues == [] and len(ring) == 4 and repairs['dropped'] == 2
    _, _, poly, issues, _ = cf.finish_ring([[0, 0], [10, 10], [10, 0], [0, 10]], [], 20, 20)
    assert poly is None and issues
    _, _, poly, issues, _ = cf.finish_ring([[0, 0], [30, 0], [30, 10], [0, 10]], [], 20, 20)
    assert poly is None and issues            # outside the image


class FakeIndex:
    def __init__(self, entries, scenes):
        self.entries, self.scenes = entries, scenes

    def near(self, geo, kind, exclude_scene):
        return [e for e in self.entries if e['kind'] == kind and e['scene_id'] != exclude_scene and e['geo'].intersects(geo)]


class FakeMeasurer:
    def __init__(self, verified=True):
        self.verified = verified
        self.calls = 0

    def transform(self, source_id, target):
        self.calls += 1
        ft = {'pixel_affine': [[1., 0.], [0., 1.], [0., 0.]], 'metre_offset': [0., 0.], 'residual': {'shift_m': .01, 'ncc': .9},
              'overlap': {'region_px': {'side_px': 200.}}, 'design': {'design_sha256': 'd'}}
        return {'ft': ft, 'verified': self.verified, 'reasons': [] if self.verified else ['ncc'], 'error': None}


def body_setup(tmp_path, m_per_px=.1, verified=True):
    ring = ring_of(box(120, 50, 200, 120))
    objects = [obj('roof', 'building', ring, status='needs_adjustment')]
    ctx = make_ctx(tmp_path, objects, paint(roofs=[ring]))
    whole = ring_of(box(120, 50, 230, 120))
    entry = {'scene_id': 'other', 'object_id': 'body', 'kind': 'building', 'geo': Polygon(pixels_to_mercator(whole, ctx['latest'])),
             'touches_own_frame': False, 'status': 'confirmed', 'geometry_reviewed': True, 'ring': whole, 'holes': [], 'vertices': 4}
    index = FakeIndex([entry], {'other': {'m_per_px': m_per_px, 'model_signature': 'sig', 'window': False}})
    return ctx, index, FakeMeasurer(verified)


def test_body_carry_proposes_the_in_frame_part_with_lineage(tmp_path):
    ctx, index, measurer = body_setup(tmp_path)
    o = ctx['by_id']['roof']
    rec = cf.body_carry(o, ctx['polys']['roof'], ctx, index, measurer)
    assert rec['refusals'] == [] and 'ring' in rec
    assert Polygon(rec['ring']).area == pytest.approx(80*70)
    assert app_ring_issues(rec['ring'], rec['holes'], W, H) == []
    cf_ = rec['carried_from']
    assert cf_['role'] == 'whole_roof_body' and cf_['scene_id'] == 'other' and cf_['object_id'] == 'body'
    assert cf_['iou_with_new'] == pytest.approx(1.) and cf_['matched_id'] == 'roof'
    assert max(p[0] for p in rec['whole_ring']) == pytest.approx(230.)
    assert rec['best']['beyond_share'] == pytest.approx(30/80, abs=1e-3)


def test_body_carry_refuses_coarser_or_unverified_bodies(tmp_path):
    ctx, index, measurer = body_setup(tmp_path, m_per_px=.3)
    rec = cf.body_carry(ctx['by_id']['roof'], ctx['polys']['roof'], ctx, index, measurer)
    assert 'coarser_than_window' in rec['refusals'] and 'ring' not in rec
    ctx, index, measurer = body_setup(tmp_path, verified=False)
    rec = cf.body_carry(ctx['by_id']['roof'], ctx['polys']['roof'], ctx, index, measurer)
    assert rec['refusals'] == ['no_verified_body']


def test_copy_uncarve_uses_fine_copies_only(tmp_path):
    ctx, roof = corner_case(tmp_path)
    o = ctx['by_id']['roof']
    zone = cf.hidden_zone(roof, roof.exterior, ctx['polys']['tree'], ctx)
    rec_fine = {'iou_in_window': .98, 'resolution_ratio': 1., 'scene_id': 'other', 'object_id': 'copy', 'transform': {}}
    rec_coarse = {**rec_fine, 'resolution_ratio': 2.7}
    out = cf.copy_uncarve(o, roof, ctx, zone, [({}, RECT, rec_coarse)], ['tree'])
    assert out['refusals'] == ['only_coarser_copies']
    out = cf.copy_uncarve(o, roof, ctx, zone, [({}, RECT, rec_fine)], ['tree'])
    assert out['refusals'] == [], out['refusals']
    assert Polygon(out['ring']).intersection(RECT).area/Polygon(out['ring']).union(RECT).area > .98


def test_proposal_ids_are_deterministic():
    assert cf.proposal_id('spike_fill', 'win-a', 'o1', 0) == cf.proposal_id('spike_fill', 'win-a', 'o1', 0)
    assert cf.proposal_id('spike_fill', 'win-a', 'o1', 0) != cf.proposal_id('spike_fill', 'win-a', 'o1', 1)


LAWN = (95, 150, 70)
SHRUB = (190, 185, 70)


def lawn_notch_case(tmp_path, *, shrub_object=False, fill=LAWN):
    ring = [[40, 0], [60, 0], [60, 10], [68, 10], [68, 0], [120, 0], [120, 60], [40, 60]]
    lawn = Polygon([[40, 0], [120, 0], [120, 60], [40, 60]])
    objects = [obj('lawn', 'vegetation', ring)]
    if shrub_object:
        objects.append(obj('shrub', 'tree', ring_of(box(60, 0, 68, 10))))
    image = paint(extra=[(LAWN, ring_of(lawn)), (fill, ring_of(box(60, 0, 68, 10)))])
    return make_ctx(tmp_path, objects, image)


def test_vegetation_notch_explained_by_another_object_is_held(tmp_path):
    ctx = lawn_notch_case(tmp_path, shrub_object=True)
    fixes, _ = cf.spike_fixes(ctx['by_id']['lawn'], ctx)
    assert len(fixes) == 1 and 'over_tree' in fixes[0]['refusals'] and 'ring' not in fixes[0]


def test_vegetation_notch_of_another_material_is_held(tmp_path):
    ctx = lawn_notch_case(tmp_path, fill=SHRUB)
    fixes, _ = cf.spike_fixes(ctx['by_id']['lawn'], ctx)
    assert len(fixes) == 1 and 'fill_not_same_material' in fixes[0]['refusals']
    assert 'notch_on_visible_edge' in fixes[0]['refusals']
    ctx = lawn_notch_case(tmp_path, fill=LAWN)
    fixes, _ = cf.spike_fixes(ctx['by_id']['lawn'], ctx)
    assert len(fixes) == 1 and fixes[0]['refusals'] == [] and Polygon(fixes[0]['ring']).area == pytest.approx(80*60)


def test_roof_notch_under_a_solar_panel_is_still_filled(tmp_path):
    ring = notch_roof(chord=10., depth=20.)
    objects = [obj('roof', 'building', ring), obj('panel', 'solar_panel', ring_of(box(60, 0, 70, 20)))]
    ctx = make_ctx(tmp_path, objects, paint(roofs=[ring]))
    fixes, _ = cf.spike_fixes(ctx['by_id']['roof'], ctx)
    assert len(fixes) == 1 and fixes[0]['refusals'] == []


def test_large_vegetation_notch_is_not_an_artefact(tmp_path):
    ring = [[40, 0], [60, 0], [60, 40], [72, 40], [72, 0], [120, 0], [120, 60], [40, 60]]
    image = paint(extra=[(LAWN, ring_of(box(40, 0, 120, 60)))])
    ctx = make_ctx(tmp_path, [obj('lawn', 'vegetation', ring)], image)
    fixes, _ = cf.spike_fixes(ctx['by_id']['lawn'], ctx)
    assert len(fixes) == 1 and 'notch_too_large_for_artefact' in fixes[0]['refusals']


def test_frame_tab_is_held(tmp_path, monkeypatch):
    """A frame run that encloses object area (a tab) is held, not cut: cutting it deletes observed outline."""
    ring = [[40, 0], [60, 0], [64, 6], [68, 0], [120, 0], [120, 60], [40, 60]]
    ctx = make_ctx(tmp_path, [obj('roof', 'building', ring)], paint(roofs=[ring]))
    tab = {'i': 1, 'j': 3, 'side': 'top', 'width_m': .8, 'depth_m': .6, 'shape': 'tab', 'at_px': [64., 6.]}
    monkeypatch.setattr(cf, 'notch_runs', lambda *a, **k: [tab])
    fixes, _ = cf.spike_fixes(ctx['by_id']['roof'], ctx)
    assert len(fixes) == 1 and fixes[0]['refusals'] == ['tab_held'] and 'ring' not in fixes[0]


def test_body_carry_refuses_a_body_that_drops_observed_outline(tmp_path):
    ring = ring_of(box(120, 50, 200, 120))
    ctx = make_ctx(tmp_path, [obj('roof', 'building', ring)], paint(roofs=[ring]))
    whole = ring_of(box(120, 70, 230, 120))          # misses the top 20 px (16 m2 at 0.1 m/px) of the window outline
    entry = {'scene_id': 'other', 'object_id': 'body', 'kind': 'building', 'geo': Polygon(pixels_to_mercator(whole, ctx['latest'])),
             'touches_own_frame': False, 'status': 'candidate', 'geometry_reviewed': False, 'ring': whole, 'holes': [], 'vertices': 4}
    index = FakeIndex([entry], {'other': {'m_per_px': .1, 'model_signature': 'sig', 'window': False}})
    rec = cf.body_carry(ctx['by_id']['roof'], ctx['polys']['roof'], ctx, index, FakeMeasurer())
    assert 'drops_observed_outline' in rec['refusals'] and 'ring' not in rec


# --- v2 guards (2026-10-08 afternoon; .run/session-2026-10-08-cuts-veg/eye-cuts.md). Each test below fails on a copy of
# cut_fix.py with its guard disabled (mutants under .run/session-2026-10-08-cuts-veg/cuts-v2/mut/).

V2 = cf.DESIGN_V2
GUARD = V2['copy_spike_guard']


def test_v1_design_is_unchanged_and_v2_changes_only_its_keys():
    import hashlib
    import json
    assert hashlib.sha256(json.dumps(cf.DESIGN, sort_keys=True, default=str).encode()).hexdigest()[:16] == 'c1959ee0c7b81133'
    assert cf.DESIGN['version'] == cf.VERSION == 'cut-fix-v1' and V2['version'] == 'cut-fix-v2'
    changed = sorted(k for k in V2 if V2[k] != cf.DESIGN.get(k))
    assert changed == ['body_cover_whole', 'body_inframe_iou_min', 'body_resolution_max', 'body_resolution_tol', 'canopy_footprint',
                       'copy_resolution_max', 'copy_spike_guard', 'hold_skeptic_wrong', 'version']
    assert cf.proposal_id('spike_fill', 'win-a', 'o1', 0) != cf.proposal_id('spike_fill', 'win-a', 'o1', 0, cf.VERSION_V2)


def guard(before, after):
    return cf.spike_guard(before, ring_of(before), after, ring_of(after), W, H, S, GUARD)


def test_spike_guard_flags_a_new_needle():
    after = RECT.union(box(140, 96, 149, 99))          # 0.3 m wide, 0.9 m long
    feats = guard(RECT, after)
    assert any(f['test'] == 'narrower_than_1m' and f['shape'] == 'protrusion' for f in feats), feats


def test_spike_guard_flags_a_new_slit():
    after = RECT.difference(box(100, 59, 103, 70))     # 0.3 m wide, 1 m deep slit from the top eave
    feats = guard(RECT, after)
    assert any(f['test'] == 'narrower_than_1m' and f['shape'] == 'notch' for f in feats), feats


def test_spike_guard_flags_a_short_sliver_part():
    after = RECT.union(box(140, 96, 142.5, 98.2))      # 0.22 m wide, 0.25 m deep: below 3 px, a part narrower than 0.5 m
    feats = guard(RECT, after)
    assert [f['tests'] for f in feats if f['test'] != 'sharp_vertex'] == [['part_narrower_than_0.5m']], feats


def test_spike_guard_flags_a_new_sharp_vertex(monkeypatch):
    monkeypatch.setattr(cf, 'ring_excursions', lambda *a, **k: [])
    after = Polygon([(40, 60), (140, 60), (140, 100), (160, 104), (140, 106), (140, 140), (40, 140)])   # a 17 deg spur
    feats = guard(RECT, after)
    assert [f['test'] for f in feats] == ['sharp_vertex'] and feats[0]['angle_deg'] < 25, feats


def test_spike_guard_flags_a_new_frame_notch():
    before = box(40, 0, 120, 50)
    after = Polygon(notch_roof(chord=2., depth=8.))
    feats = guard(before, after)
    assert any(f['test'] == 'census_frame_spike' and f['shape'] == 'notch' for f in feats), feats


def test_spike_guard_passes_a_square_extension_and_old_features():
    assert guard(RECT, RECT.union(box(140, 80, 150, 120))) == []        # 90-degree corners are not spikes
    needle = RECT.union(box(40, 96, 31, 99).envelope)                    # a needle the current outline already has
    assert guard(needle, needle.union(box(140, 80, 150, 120))) == []


def needle_copy_case(tmp_path):
    ctx, roof = corner_case(tmp_path)
    zone = cf.hidden_zone(roof, roof.exterior, ctx['polys']['tree'], ctx)
    copy = RECT.union(box(140, 136, 149, 139))          # the copy fills the hidden corner and adds a 0.3 m needle into the tree
    rec = {'iou_in_window': .98, 'resolution_ratio': 1., 'scene_id': 'other', 'object_id': 'copy', 'transform': {}}
    return ctx, roof, zone, [({}, copy, rec)]


def test_copy_uncarve_v2_refuses_a_result_with_a_needle(tmp_path):
    ctx, roof, zone, copies = needle_copy_case(tmp_path)
    o = ctx['by_id']['roof']
    v1 = cf.copy_uncarve(o, roof, ctx, zone, copies, ['tree'])
    assert v1['refusals'] == [], v1['refusals']
    v2 = cf.copy_uncarve(o, roof, ctx, zone, copies, ['tree'], design=V2)
    assert v2['refusals'] == ['adds_spike_or_sliver'] and 'ring' not in v2
    assert v2['spike_guard']['count'] >= 1
    clean = cf.copy_uncarve(o, roof, ctx, zone, [({}, RECT, copies[0][2])], ['tree'], design=V2)
    assert clean['refusals'] == [] and clean['spike_guard']['count'] == 0


def band_case(tmp_path, outer):
    """A chamfered roof whose chamfer runs inside a canopy band x+y in [258, outer]; the true corner is (140, 140) (x+y 280)."""
    roof = Polygon([(40, 60), (140, 60), (140, 120), (120, 140), (40, 140)])
    band = Polygon([(140, 118), (140, outer-140), (outer-140, 140), (118, 140)]) if outer < 280 else \
        Polygon([(140, 118), (142, 118), (142, 142), (118, 142), (118, 140)])
    objects = [obj('roof', 'building', ring_of(roof)), obj('tree', 'tree', ring_of(band))]
    image = paint(roofs=[ring_of(roof)], canopies=[ring_of(band)])
    return make_ctx(tmp_path, objects, image)


def test_canopy_footprint_refuses_eaves_meeting_outside_the_canopy(tmp_path):
    ctx = band_case(tmp_path, 270)                      # the canopy ends 0.7 m short of the corner
    v1 = [c for c in cf.canopy_completions(ctx['by_id']['roof'], ctx) if c['geometry']]
    assert len(v1) == 1 and v1[0]['refusals'] == [], [c['refusals'] for c in v1]
    v2 = [c for c in cf.canopy_completions(ctx['by_id']['roof'], ctx, V2) if c['geometry']]
    assert len(v2) == 1 and 'eaves_meet_outside_canopy' in v2[0]['refusals'] and 'ring' not in v2[0]
    assert v2[0]['canopy_footprint']['corner_in_canopy_m'] < -.3
    ctx = band_case(tmp_path, 300)                      # the canopy covers the corner: v2 completes it
    v2 = [c for c in cf.canopy_completions(ctx['by_id']['roof'], ctx, V2) if c['geometry']]
    assert len(v2) == 1 and v2[0]['refusals'] == [], [c['refusals'] for c in v2]
    assert v2[0]['canopy_footprint']['corner_in_canopy_m'] > 0


def test_canopy_footprint_refuses_a_completion_deeper_than_the_canopy():
    roof = RECT.difference(box(80, 130, 100, 141))      # a notch 1 m deep into the bottom eave
    completion = box(80, 130, 100, 140)
    shallow = box(78, 136, 102, 146)                     # the canopy reaches only 0.4 m into the notch (+0.3 m buffer)
    eave = LineString([(80, 140), (100, 140)])          # a straight join: depth is measured from the eave chord
    refusals, metrics = cf.footprint_check(roof, completion, None, shallow, S, V2['canopy_footprint'], eave)
    assert refusals == ['completion_deeper_than_canopy'] and metrics['canopy_depth_m'] < metrics['completion_depth_m']
    deep = box(78, 128, 102, 146)
    assert cf.footprint_check(roof, completion, None, deep, S, V2['canopy_footprint'], eave)[0] == []
    corner = np.array([100., 140.])
    assert 'eaves_meet_outside_canopy' in cf.footprint_check(roof, completion, corner, box(60, 120, 90, 146), S,
                                                             V2['canopy_footprint'])[0]


def test_v2_body_carry_refuses_coarser_bodies_but_not_the_same_zoom(tmp_path):
    ctx, index, measurer = body_setup(tmp_path, m_per_px=.11)           # 1.1 x the window's pixel size
    assert cf.body_carry(ctx['by_id']['roof'], ctx['polys']['roof'], ctx, index, measurer)['refusals'] == []
    rec = cf.body_carry(ctx['by_id']['roof'], ctx['polys']['roof'], ctx, index, measurer, V2)
    assert rec['refusals'] == ['coarser_than_window'] and 'ring' not in rec
    ctx, index, measurer = body_setup(tmp_path, m_per_px=.1*1.0003)     # the same zoom read at another latitude
    assert cf.body_carry(ctx['by_id']['roof'], ctx['polys']['roof'], ctx, index, measurer, V2)['refusals'] == []


def small_body_case(tmp_path, window_ring, whole_ring, folder='win-test', oid='roof', scene='other', source='body'):
    path = tmp_path/folder
    path.mkdir(exist_ok=True)
    ctx = cf.context_from(latest([obj(oid, 'building', window_ring)], scene_id=folder), {'objects': [obj(oid, 'building', window_ring)]},
                          paint(roofs=[window_ring]), path)
    entry = {'scene_id': scene, 'object_id': source, 'kind': 'building', 'geo': Polygon(pixels_to_mercator(whole_ring, ctx['latest'])),
             'touches_own_frame': False, 'status': 'candidate', 'geometry_reviewed': False, 'ring': whole_ring, 'holes': [],
             'vertices': len(whole_ring)}
    index = FakeIndex([entry], {scene: {'m_per_px': .1, 'model_signature': 'sig', 'window': True}})
    return ctx, index, ctx['by_id'][oid]


def test_v2_body_carry_needs_95_percent_cover(tmp_path):
    window = ring_of(box(170, 60, 200, 100))                           # 12 m2, cut by the right frame
    whole = ring_of(box(172.4, 60, 220, 100))                          # misses 0.96 m2: cover 0.92, inside v1's 1 m2 drop guard
    ctx, index, o = small_body_case(tmp_path, window, whole)
    assert cf.body_carry(o, ctx['polys'][o['id']], ctx, index, FakeMeasurer())['refusals'] == []
    rec = cf.body_carry(o, ctx['polys'][o['id']], ctx, index, FakeMeasurer(), V2)
    assert 'cover_below_whole' in rec['refusals'] and 'ring' not in rec


def test_v2_body_carry_needs_inframe_iou_0_9(tmp_path):
    window = ring_of(box(120, 50, 200, 120))
    whole = ring_of(box(108, 50, 230, 120))                           # adds 8.4 m2 inside the frame: in-frame IoU 0.87
    ctx, index, o = small_body_case(tmp_path, window, whole)
    v1 = cf.body_carry(o, ctx['polys'][o['id']], ctx, index, FakeMeasurer())
    assert v1['refusals'] == [] and .85 < v1['best']['inframe_iou'] < .9
    rec = cf.body_carry(o, ctx['polys'][o['id']], ctx, index, FakeMeasurer(), V2)
    assert rec['refusals'] == ['inframe_iou_low']


def test_v2_body_carry_never_reuses_a_body_the_skeptic_marked_wrong(tmp_path):
    window = ring_of(box(120, 50, 200, 120))
    whole = ring_of(box(120, 50, 230, 120))
    wrong = next(e for e in cf.SKEPTIC_WRONG_V1 if e['v1'] == 105)
    ctx, index, o = small_body_case(tmp_path, window, whole, folder=wrong['window'], oid=wrong['object_id'],
                                    scene=wrong['holder'][0], source=wrong['holder'][1])
    assert cf.body_carry(o, ctx['polys'][o['id']], ctx, index, FakeMeasurer())['refusals'] == []
    rec = cf.body_carry(o, ctx['polys'][o['id']], ctx, index, FakeMeasurer(), V2)
    assert rec['refusals'] == ['skeptic_marked_wrong_v1'] and rec['best']['skeptic_v1'] == 105
    ctx, index, o = small_body_case(tmp_path, window, whole, folder=wrong['window'], oid=wrong['object_id'],
                                    scene=wrong['holder'][0], source='another-body')
    assert cf.body_carry(o, ctx['polys'][o['id']], ctx, index, FakeMeasurer(), V2)['refusals'] == []


def test_skeptic_hold_needs_the_same_key_and_ring(monkeypatch):
    ring, holes = [[0., 0.], [10., 0.], [10., 10.]], []
    entry = {'v1': 7, 'type': 'canopy_completion', 'window': 'win-a', 'object_id': 'o', 'index': 1, 'ring': cf.ring_sha(ring, holes)}
    monkeypatch.setattr(cf, 'SKEPTIC_WRONG_V1', (entry,))
    assert cf.skeptic_hold('canopy_completion', 'win-a', 'o', 1, ring, holes, design=V2) is entry
    assert cf.skeptic_hold('canopy_completion', 'win-a', 'o', 1, [[0., 0.], [10., 0.], [10., 11.]], holes, design=V2) is None
    assert cf.skeptic_hold('canopy_completion', 'win-a', 'o', 0, ring, holes, design=V2) is None
    assert cf.skeptic_hold('canopy_completion', 'win-a', 'o', 1, ring, holes) is None          # v1 design: never held


def test_analyse_window_v2_holds_a_completion_the_skeptic_marked_wrong(tmp_path, monkeypatch):
    import json
    tree = Point(145, 145).buffer(15, 32)
    roof = RECT.difference(tree)
    objects = [obj('roof', 'building', ring_of(roof)), obj('tree', 'tree', ring_of(tree))]
    folder = tmp_path/'win-hold'
    folder.mkdir()
    scene = latest(objects, scene_id='win-hold')
    scene['image_sha256'] = 'img'
    (folder/'latest.json').write_text(json.dumps(scene))
    paint(roofs=[ring_of(RECT)], canopies=[ring_of(tree)]).save(folder/'img.png')
    _, records, _, _ = cf.analyse_window(folder, None, None, {}, design=cf.DESIGN, frames=False)
    done = [x for x in records if x['type'] == 'canopy_completion']
    assert len(done) == 1
    _, records, _, _ = cf.analyse_window(folder, None, None, {}, design=V2, frames=False)
    assert [x['type'] for x in records if x['type'] == 'canopy_completion'] == ['canopy_completion']   # no hold: proposed
    entry = {'v1': 9, 'type': 'canopy_completion', 'window': 'win-hold', 'object_id': 'roof', 'index': done[0]['proposal']['cut_fix']['index'],
             'ring': cf.ring_sha(done[0]['proposal']['ring'], done[0]['proposal']['holes'])}
    monkeypatch.setattr(cf, 'SKEPTIC_WRONG_V1', (entry,))
    _, records, held, _ = cf.analyse_window(folder, None, None, {}, design=V2, frames=False)
    assert not [x for x in records if x['type'] == 'canopy_completion']
    assert any(h['type'] == 'canopy_cut_held' and h['detail']['refusals'] == ['skeptic_marked_wrong_v1'] for h in held)


def test_spike_guard_reports_only_new_features_near_the_change(monkeypatch):
    old_e = {'rule': 'narrower_than_1m', 'shape': 'protrusion', 'chord_px': 3., 'depth_px': 9., 'tip': (140.5, 100.)}
    far_e = {**old_e, 'tip': (60., 60.)}                   # an after-ring excursion far from the change
    calls = iter([[dict(old_e)], [dict(old_e), dict(far_e)]])
    monkeypatch.setattr(cf, 'ring_excursions', lambda *a, **k: next(calls))
    vertices = iter([[(140.5, 101., 20.)], [(140.5, 101., 20.), (60., 61., 20.)]])
    monkeypatch.setattr(cf, 'sharp_vertices', lambda *a, **k: next(vertices))
    after = RECT.union(box(140, 90, 141, 110))             # the change touches the old features' tips
    assert cf.spike_guard(RECT, ring_of(RECT), after, ring_of(after), W, H, S, GUARD) == []
