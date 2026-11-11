"""linear_continuity_run (experiments): the sidecar that runs merge_linear_fragments over the windows and renders the marks.

Pure helpers on synthetic geometry plus one end-to-end run on a two-object synthetic scene in a temporary directory: the run
must write only under its --out and never touch the scene directory. Counts and files, never accuracy. No model is loaded.
"""
import json

import numpy as np
from PIL import Image
from scripts.ml.experiments import linear_continuity_run as run
from shapely.geometry import Polygon

AFFINE = [[.1, 0.], [0., -.1], [0., 50.]]            # pixel -> metres: x_m = .1 px, y_m = 50 - .1 py (0.1 m/px)


def px(points_m):
    return [[round(x/.1, 2), round((50.-y)/.1, 2)] for x, y in points_m]


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def obj(oid, kind, ring_m, status='candidate', score=.5):
    return {'id': oid, 'kind': kind, 'label': kind.capitalize(), 'ring': px(ring_m), 'status': status,
            'geometry_reviewed': False, 'source': 'sam3-text-onnx', 'score': score, 'prompt': kind,
            'model': {'area_m2': round(Polygon(ring_m).area, 2), 'color': '#808080', 'height_m': .05}}


def test_classify_group():
    feature_of = {'a': 'F', 'b': 'F', 'c': 'F', 'd': 'G', 'e': 'G'}
    assert run.classify_group(['a', 'b', 'c'], feature_of) == 'full'
    assert run.classify_group(['a', 'b', 'x'], feature_of) == 'partial'
    assert run.classify_group(['a', 'd'], feature_of) == 'none'
    assert run.classify_group(['x', 'y'], feature_of) == 'none'


def test_bridge_of_is_feature_minus_members():
    a, b = Polygon(rect(0, 0, 4, 2)), Polygon(rect(5, 0, 9, 2))
    feature = Polygon(rect(0, 0, 9, 2))
    bridge = run.bridge_of(feature, [a, b], 1e-3)
    assert abs(bridge.area-2.) < 1e-6
    assert run.bridge_of(a, [a], 1e-3).is_empty


def test_polygon_parts_transform_and_rounding():
    geom = Polygon(rect(0, 0, 2, 1))
    inverse = np.linalg.inv(np.c_[np.asarray(AFFINE), [0., 0., 1.]])
    parts = run.polygon_parts(geom, inverse, 2)
    assert len(parts) == 1 and parts[0]['holes'] == []
    assert sorted(parts[0]['ring']) == sorted([[0., 500.], [20., 500.], [20., 490.], [0., 490.]])


def test_scale_bar_fits_a_third_of_the_crop():
    assert run.scale_bar_metres(600, .1) == 5.        # 5 m = 100 px at x2, 10 m = 200 px > 180
    assert run.scale_bar_metres(100, .1) == 1.


def test_place_label_avoids_placed_boxes_and_stays_on_the_canvas():
    boxes = []
    assert run.place_label(boxes, 100, 100, 60, 14, 400, 300, step=16) == (100, 100)
    assert run.place_label(boxes, 100, 100, 60, 14, 400, 300, step=16) == (100, 116)      # same spot: one hop down
    assert run.place_label(boxes, 110, 104, 60, 14, 400, 300, step=16) == (110, 136)      # overlaps both: two hops
    assert run.place_label(boxes, 390, 290, 60, 14, 400, 300, step=16) == (338, 284)      # clamped to the canvas
    assert len(boxes) == 4 and all(b[2] <= 400 and b[3] <= 300 for b in boxes)
    full = [(0, 0, 400, 300)]
    assert run.place_label(full, 50, 50, 60, 14, 400, 300) == (50, 50)                    # nothing free: the wanted spot


def test_inset_plan_magnifies_the_bridge_region_without_covering_the_crop():
    region, scale, size = run.inset_plan((100, 100, 106, 112), (600, 400), 15)
    assert region == (85, 85, 121, 127) and scale == 4 and size == (36*4, 42*4)
    region, scale, size = run.inset_plan((2, 2, 8, 14), (600, 400), 15)                     # clipped at the canvas edge
    assert region == (0, 0, 23, 29) and size == (23*4, 29*4)
    region, scale, size = run.inset_plan((100, 100, 300, 112), (600, 400), 15, max_px=640)  # x4 would exceed max_px: half steps
    assert scale == 2.5 and size[0] <= 640
    region, scale, size = run.inset_plan((0, 0, 500, 300), (600, 400), 15, max_px=640)      # never below x2
    assert scale == 2 and size == (2*(region[2]-region[0]), 2*(region[3]-region[1]))


def test_contact_text_lists_sub_eps_overlaps():
    assert run.contact_text({'bridge_near_blockers': [], 'bridge_near_decided': []}) == ''
    text = run.contact_text({'bridge_near_blockers': [{'id': 'fd6671296bb3c7c8', 'kind': 'road', 'status': 'candidate', 'overlap_m2': 6.4e-4}],
                             'bridge_near_decided': [{'id': 'c0f1000000000001', 'kind': 'car', 'status': 'confirmed', 'overlap_m2': 1.2e-5}]})
    assert text == 'sub-eps contact: road fd667129 6.4e-04 m2, car c0f10000 1.2e-05 m2'


def test_end_to_end_writes_only_under_out(tmp_path, monkeypatch):
    scenes = tmp_path/'scenes'
    scene = scenes/'win-test0000000000'
    scene.mkdir(parents=True)
    image_sha = 'ab'*32
    Image.new('RGB', (500, 500), (120, 120, 120)).save(scene/f'{image_sha}.png')
    objects = [obj('aaaaaaaaaaaaaaaa', 'driveway', rect(10, 20, 20, 23), score=.4),
               obj('bbbbbbbbbbbbbbbb', 'driveway', rect(20.3, 20, 30, 23), score=.4),
               obj('cccccccccccccccc', 'footpath', rect(10, 30, 20, 31.5), status='confirmed', score=.6),
               obj('dddddddddddddddd', 'footpath', rect(40, 40, 42, 40.6), score=.5),          # 1.2 m2 pieces 0.2 m apart (over the
               obj('eeeeeeeeeeeeeeee', 'footpath', rect(42.2, 40, 44.2, 40.6), score=.5),      # 19:40 result floor): 0.12 m2 bridge -> inset
               obj('ffffffffffffffff', 'road', rect(42.05, 40.7, 42.15, 41.7), score=.5)]      # road sliver 0.1 m above the bridge
    latest = {'id': scene.name, 'image_sha256': image_sha, 'image_width': 500, 'image_height': 500,
              'pixel_to_meters': AFFINE, 'objects': objects, 'model_signature': 'sig'}
    (scene/'latest.json').write_text(json.dumps(latest))
    (scene/'review.json').write_text(json.dumps({'revision': 3, 'objects': objects}))
    before = {p: p.stat().st_mtime_ns for p in scene.iterdir()}
    monkeypatch.setattr(run, 'MARKS_WINDOWS', [scene.name])
    out = tmp_path/'out'
    out.mkdir()
    (out/'marks-notes.json').write_text(json.dumps({'not-a-feature': 'ignored'}))
    run.main(['--out', str(out), '--scenes-root', str(scenes)])
    assert {p: p.stat().st_mtime_ns for p in scene.iterdir()} == before          # read-only on the scene
    assert {p for p in scenes.rglob('*') if p.is_file()} == set(before)
    assert sorted(p.name for p in out.glob('*.json')) == ['marks-notes.json', 'metrics.json', f'{scene.name}.model.json',
                                                           f'{scene.name}.review.json']
    metrics = json.loads((out/'metrics.json').read_text())
    for layer in ('model', 'review'):
        result = json.loads((out/f'{scene.name}.{layer}.json').read_text())
        by_kind = {f['kind']: f for f in result['features']}
        assert set(by_kind) == {'driveway', 'footpath'} and all(f['n_members'] == 2 and f['steps'] == [1] for f in by_kind.values())
        assert result['checks']['decided_members'] == 0 and result['checks']['bridge_touches_blocker'] == 0
        assert result['checks']['features_simplified'] == 0 and result['checks']['slop_touches_blocker'] == 0
        assert all(f['slop_m2'] == 0. and f['slop_blockers'] == [] for f in result['features'])
        assert result['checks']['features_with_sub_eps_contact'] == 0
        assert result['kinds']['driveway']['same_mask_groups'] == {'groups': 1, 'pieces_in_groups': 2, 'full': 1, 'partial': 0, 'none': 0}
        assert result['kinds']['footpath']['candidates'] == 2
        assert metrics['layers'][layer]['driveway']['merges'] == {'step1': 1, 'step2': 0, 'total': 1}
        small = by_kind['footpath']
        assert small['bridge_recomputed_m2'] < run.BRIDGE_INSET_M2 and abs(small['bridge_m2']-small['bridge_recomputed_m2']) < .01
        assert abs(by_kind['driveway']['bridge_m2']-by_kind['driveway']['bridge_recomputed_m2']) < .01
        assert small['bridge_near_blockers'] == [] and small['bridge_blockers'] == []      # the road sliver is 0.1 m off the bridge
    assert metrics['checks']['review']['decided_members'] == 0
    assert metrics['checks']['review']['touch_eps_m2'] == run.LINEAR_MERGE['touch_eps_m2']
    assert metrics['checks']['review']['bridge_recompute_max_abs_diff_m2'] < .01
    assert {row['kind'] for row in metrics['sensitivity']['table']} == {'road', 'footpath'}
    review = json.loads((out/f'{scene.name}.review.json').read_text())
    ids = {f['kind']: f['id'] for f in review['features']}
    # render order follows LINEAR_MERGE['kinds'] (road, footpath, driveway): the footpath feature is #1, the driveway #2
    big_png, small_png = (out/'renders'/scene.name/f"feature-2-{ids['driveway']}.png"), (out/'renders'/scene.name/f"feature-1-{ids['footpath']}.png")
    assert big_png.exists() and small_png.exists()
    assert (out/'renders'/f'{scene.name}.sheet.png').exists()
    magenta = {p for p in Image.open(small_png).convert('RGB').getdata() if p == run.BRIDGE_LINE}
    assert magenta, 'the small-bridge render must carry the magenta inset frame / outline'
    marks = (out/f'MARKS-{run.MARKS_DATE}.md').read_text()
    assert f"2. driveway {ids['driveway']} = aaaaaaaaaaaaaaaa, bbbbbbbbbbbbbbbb" in marks
    assert f"1. footpath {ids['footpath']} = dddddddddddddddd, eeeeeeeeeeeeeeee" in marks
    assert '[ right / wrong / unclear ]' in marks and 'Totals: 2 merged features' in marks
    assert 'A touch is an intersection > 0.001 m2' in marks and 'hint:' not in marks.split('## ')[1]
