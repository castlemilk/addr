"""teacher_elevation (teacher-elevation-v1): network-free tests, main venv only (no rasterio here)."""
import ast
import copy
import hashlib
import inspect
import itertools
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from scripts.ml import heights_source as hs
from scripts.ml.training import hires_teacher as ht
from scripts.ml.training import teacher_elevation as te
from shapely.geometry import box

REPO = Path(__file__).resolve().parents[3]
JUDGES = REPO / 'data/reviews/eyes/training/distill-linz-chc-2025-0075/teacher/judges'
WINDOWS = REPO / 'data/reviews/eyes/training/distill-linz-chc-2025-0075/teacher/windows.json'
CROPS = REPO / 'data/reviews/eyes/training/distill-linz-chc-2025-0075/teacher/crops.json'
TEACHER = REPO / 'data/reviews/eyes/training/distill-linz-chc-2025-0075/teacher'
TRAINING_PY = REPO / 'scripts/ml/training/.venv/bin/python'     # rasterio lives only in the training venv
MODULE_SOURCE = Path(te.__file__).read_text()
# round-5 plan W3 "Imports only": the attribute names this module may use on each alias
ALLOWED = {
    'hs': {'ndsm', 'gdal_to_matrix', 'pixel_centres_in', 'top_half_median', 'sample_polygon', 'PARAMS'},
    'ts': {'s3_client', 's3_json', 'read_header', 'GDAL_ENV', 'gdal_path', 'to_lonlat', 'multihash_sha256', 'teacher_dir',
           'source'},        # source: the registry lookup teacher_dir(root, spec) needs (a recorded deviation, no other use)
    'tj': {'judges_dir', '_load', 'teacher_polygons', 'cell_split', 'window_bootstrap_share', '_band', 'AREA_BANDS', '_read_rows',
           'recommend', 'LEDGER_POLICY', 'ring_samples', 'J1'},
    'common': {'read_json', 'write_json_atomic', 'sha256_file', 'now_iso'},
}
ALIASES = {'hs': (2, 'heights_source'), 'ts': (1, 'teacher_sources'), 'tj': (1, 'teacher_judges')}
NINE = ['BX23_10000_0205', 'BX24_10000_0101', 'BX24_10000_0102', 'BX24_10000_0103', 'BX24_10000_0201',
        'BX24_10000_0202', 'BX24_10000_0203', 'BX24_10000_0301', 'BX24_10000_0302']
CLASSES = {'keep', 'review', 'ignore', 'part'}

# synthetic 1 m rasters: north-up, origin (X0, Y0), 60 x 60 cells, gently sloping terrain
X0, Y0 = 1_000_000.0, 5_000_060.0


@pytest.fixture(autouse=True)
def _restore_module_state():
    """teacher_judges.cell_split() rebinds hires_teacher's source globals (set_source): undo it."""
    yield
    ht.set_source('com-2018')


def _rasters(n=60):
    dem = np.full((n, n), 20.0) + 0.01 * np.arange(n)[None, :]
    return dem.copy(), dem, hs.gdal_to_matrix((1.0, 0.0, X0, 0.0, -1.0, Y0))


def _cells(c0, r0, c1, r1):
    """Polygon (NZTM-like metres) over cells [c0, c1) x [r0, r1) of the synthetic raster."""
    return box(X0 + c0, Y0 - r1, X0 + c1, Y0 - r0)


def _require(*paths, what='gitignored data tree data/reviews/, .gitignore:60'):
    """Real-data tests read the gitignored tree (.gitignore:60 data/reviews/). Where that tree exists (this machine) a
    missing file FAILS, so a skip can never pass for a pass; the test skips only on a clean checkout (the teacher tree
    itself absent) or when W3_ALLOW_SKIP=1 is set on purpose."""
    missing = [str(p.relative_to(REPO)) for p in paths if not p.exists()]
    if not missing:
        return
    reason = f"{', '.join(missing)} absent ({what})"
    if os.environ.get('W3_ALLOW_SKIP') == '1':
        pytest.skip(f'{reason}; W3_ALLOW_SKIP=1 set')
    if not TEACHER.exists():
        pytest.skip(f'{reason}; clean checkout: {TEACHER.relative_to(REPO)} absent')
    pytest.fail(f'{reason} while {TEACHER.relative_to(REPO)} exists: failing instead of skipping (W3_ALLOW_SKIP=1 skips on purpose)')


def _imports(nodes, into_functions):
    """'module:name' for every import statement under nodes (module-level only unless into_functions)."""
    out, stack = set(), list(nodes)
    while stack:
        node = stack.pop()
        if not into_functions and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Import):
            out |= {f'{alias.name}:{alias.name}' for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            out |= {f"{'.' * node.level}{node.module or ''}:{alias.name}" for alias in node.names}
        stack.extend(ast.iter_child_nodes(node))
    return out


def _keys(value):
    """Every dict key of a nested payload."""
    if isinstance(value, dict):
        return [str(k) for k in value] + [k for v in value.values() for k in _keys(v)]
    if isinstance(value, list):
        return [k for v in value for k in _keys(v)]
    return []


def _negated_only(text, phrase):
    """True when every occurrence of phrase reads 'never <phrase>' or 'not <phrase>'."""
    return all(re.search(r'(never|not)\s+$', text[max(0, m.start() - 7):m.start()]) for m in re.finditer(re.escape(phrase), text))


# --- module hygiene ---------------------------------------------------------------------------------------------------

def test_import_time_imports_exclude_rasterio_boto3_and_the_teacher_modules():
    """Static, so it holds in a venv that has no rasterio at all (a sys.modules check there is vacuous)."""
    assert te.VERSION == 'teacher-elevation-v1'
    tree = ast.parse(MODULE_SOURCE)
    top = _imports(tree.body, into_functions=False)
    for name in ('rasterio', 'boto3', 'teacher_judges', 'teacher_sources', 'hires_teacher', 'heights_source'):
        assert not any(name in entry for entry in top), (name, sorted(top))
    everywhere = _imports(tree.body, into_functions=True)
    assert any('rasterio' in entry for entry in everywhere)             # used, inside functions only
    assert not any('boto3' in entry or 'hires_teacher' in entry for entry in everywhere)


def test_module_imports_with_rasterio_and_boto3_blocked():
    """Non-vacuous in any venv: the import succeeds with rasterio and boto3 made unimportable (transitive imports too)."""
    code = ("import sys; sys.modules['rasterio'] = None; sys.modules['boto3'] = None; "
            "import scripts.ml.training.teacher_elevation as m; print(m.VERSION)")
    done = subprocess.run([sys.executable, '-c', code], cwd=REPO, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == 'teacher-elevation-v1'


def test_names_used_on_the_teacher_modules_are_the_plans_allowed_imports():
    tree = ast.parse(MODULE_SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.asname in ALIASES for a in node.names):
            for alias in node.names:
                assert (node.level, alias.name) == ALIASES[alias.asname], ast.unparse(node)
    used = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in ALLOWED:
            used.setdefault(node.value.id, set()).add(node.attr)
    assert set(used) == set(ALLOWED)
    for alias, names in used.items():
        assert names <= ALLOWED[alias], (alias, sorted(names - ALLOWED[alias]))


def test_epoch_registry_refuses_out_of_scope_and_forbidden_epochs():
    spec = te.epoch_spec('christchurch_2024-2025')
    assert spec['layers']['dsm']['collection_id'] == '01K7D5GDMHRECR47H88R9ABDB2'
    assert spec['layers']['dem']['collection_id'] == '01K7D5G9JKZZW6KRZ3ACWMD0T5'
    assert spec['nodata'] == -9999.0 and spec['gsd_m'] == 1.0
    assert te.EPOCH_FOR_SOURCE['linz-chc-2025-0075'] == 'christchurch_2024-2025'
    with pytest.raises(SystemExit):
        te.epoch_spec('christchurch_2020-2021')     # recorded, out of scope
    with pytest.raises(SystemExit):
        te.epoch_spec('canterbury_2020-2023')       # never used
    assert 'not as a label' in te.LICENCE['attribution'] and te.LICENCE['licence'] == 'CC BY 4.0'


def test_ndsm_dict_equals_the_pre_registered_rules_file():
    path = JUDGES / 'ndsm_rules.json'
    _require(path)
    assert json.loads(path.read_text()) == te.NDSM
    assert te.NDSM['version'] == te.VERSION


# --- 1:10,000 tile arithmetic -----------------------------------------------------------------------------------------

def test_tile_arithmetic_matches_the_bx24_header():
    assert te.sheet_origin('BX24') == (1_564_000.0, 5_190_000.0)
    # the other sheets around the windows, as fitted from 1:500 tile headers in the imagery catalogue
    assert te.sheet_origin('BX23') == (1_540_000.0, 5_190_000.0)
    assert te.sheet_origin('BW23') == (1_540_000.0, 5_226_000.0)
    assert te.sheet_origin('BW24') == (1_564_000.0, 5_226_000.0)
    left, bottom, right, top = te.tile_bounds('BX24_10000_0102')
    assert (left, top) == (1_568_800.0, 5_190_000.0)
    assert (right - left, top - bottom) == (4800.0, 7200.0)
    assert te.parse_tile('BX24_10000_0102') == ('BX24', 1, 2)
    assert te.parse_tile('BX24_500_010043') is None          # the 1:500 imagery grid is not this grid
    assert te.tile_name(*te.parse_tile('BX23_10000_0205')) == 'BX23_10000_0205'


def test_straddling_window_returns_two_tiles():
    # a 271.8 m window across the column edge at 1,568,800 E inside BX24 row 1
    bounds = (1_568_700.0, 5_186_000.0, 1_568_971.8, 5_186_271.8)
    assert te.tiles_for_bounds(bounds) == ['BX24_10000_0101', 'BX24_10000_0102']
    # across the sheet edge at 1,564,000 E (BX23 col 5 | BX24 col 1), row 2
    assert te.tiles_for_bounds((1_563_900.0, 5_180_000.0, 1_564_171.8, 5_180_271.8)) == ['BX23_10000_0205', 'BX24_10000_0201']
    # touching an edge exactly is not an intersection
    assert te.tiles_for_bounds((1_568_528.2, 5_186_000.0, 1_568_800.0, 5_186_271.8)) == ['BX24_10000_0101']


def test_the_sixty_windows_need_exactly_nine_tiles_with_seven_straddling():
    _require(WINDOWS)
    order = json.loads(WINDOWS.read_text())['order']
    tiles = te.window_tiles(order)
    assert len(tiles) == 60
    assert sorted({t for names in tiles.values() for t in names}) == NINE
    straddling = {w: names for w, names in tiles.items() if len(names) > 1}
    assert len(straddling) == 7 and all(len(names) == 2 for names in straddling.values())
    # padding every window by the 4 m read margin needs no further tile
    padded = te.window_tiles(order, pad_m=te.NDSM['sampling']['read_pad_m'])
    assert sorted({t for names in padded.values() for t in names}) == NINE


# --- synthetic DSM / DEM ----------------------------------------------------------------------------------------------

def test_three_metre_box_is_elevated_with_a_full_edge_step():
    dsm, dem, matrix = _rasters()
    dsm[20:35, 20:35] += 3.0
    ndsm = hs.ndsm(dsm, dem, nodata=-9999.0)
    row = te.measure_instance(ndsm, matrix, _cells(20, 20, 35, 35))
    assert row['ndsm_class'] == 'elevated'
    assert row['height_m'] == 3.0 and row['share_above_2m'] == 1.0 and row['erode_m'] == 1.0
    assert row['samples'] == 13 * 13 and row['nodata_share'] == 0.0
    assert abs(row['edge_step_m'] - 3.0) < 1e-6 and row['edge_step_share'] >= .95
    assert row['outside_ring_m'] == 0.0


def test_flat_ground_is_flat():
    dsm, dem, matrix = _rasters()
    ndsm = hs.ndsm(dsm, dem, nodata=-9999.0)
    row = te.measure_instance(ndsm, matrix, _cells(45, 5, 55, 15))
    assert row['ndsm_class'] == 'flat' and row['height_m'] == 0.0 and row['share_above_2m'] == 0.0
    assert row['edge_step_m'] == 0.0 and row['edge_step_share'] == 0.0


def test_two_by_two_box_is_unjudged_with_the_erosion_fallback_recorded():
    dsm, dem, matrix = _rasters()
    dsm[40:43, 40:43] += 3.0
    ndsm = hs.ndsm(dsm, dem, nodata=-9999.0)
    row = te.measure_instance(ndsm, matrix, _cells(40.5, 40.5, 42.5, 42.5))   # one cell centre inside
    assert row['ndsm_class'] == 'unjudged'
    assert row['erode_m'] == 0.0 and row['samples'] == 1                       # last fallback, its sample count
    assert row['height_m'] is None
    # a 3.2 m box falls back to 0.5 m erosion and is judged
    judged = te.measure_instance(ndsm, matrix, box(X0 + 39.9, Y0 - 43.1, X0 + 43.1, Y0 - 39.9))
    assert judged['erode_m'] == 0.5 and judged['samples'] >= 4 and judged['ndsm_class'] != 'unjudged'


def test_nodata_above_a_fifth_is_unjudged():
    dsm, dem, matrix = _rasters()
    dsm[5:15, 5:15] += 3.0
    dsm[5:8, 5:15] = -9999.0                       # 30 of the 100 cells inside the outline
    ndsm = hs.ndsm(dsm, dem, nodata=-9999.0)
    row = te.measure_instance(ndsm, matrix, _cells(5, 5, 15, 15))
    assert abs(row['nodata_share'] - .3) < 1e-9
    assert row['ndsm_class'] == 'unjudged' and row['height_m'] is not None


def test_edge_step_counts_only_the_edge_on_the_step():
    dsm, dem, matrix = _rasters()
    dsm[:, 30:] += 3.0                             # a 3 m step along x = X0 + 30, elevated to the east
    ndsm = hs.ndsm(dsm, dem, nodata=-9999.0)
    step, share, probes = te.edge_step(ndsm, matrix, _cells(30, 10, 45, 50))
    assert probes > 0 and step == 0.0              # three of four sides sit inside the raised half
    assert .25 < share < .5                        # the west side (40 of 110 m) steps by 3 m


def test_class_thresholds():
    assert te.ndsm_class(2.0, .5, 4, 0.) == 'elevated'
    assert te.ndsm_class(.99, .09, 4, 0.) == 'flat'
    assert te.ndsm_class(1.0, .09, 4, 0.) == 'mixed'
    assert te.ndsm_class(2.5, .4, 4, 0.) == 'mixed'
    assert te.ndsm_class(3.0, 1., 3, 0.) == 'unjudged'
    assert te.ndsm_class(3.0, 1., 40, .21) == 'unjudged'
    assert te.ndsm_class(None, None, 0, None) == 'unjudged'


def test_veg_share_uses_the_exg_rule_on_native_pixels():
    rgb = np.full((200, 200, 3), 120, np.uint8)
    rgb[50:100, 50:100] = (60, 160, 60)
    veg = te.veg_map(rgb, rows_per_chunk=37)
    assert veg[60, 60] and not veg[150, 150]
    assert te.veg_share(veg, box(50, 50, 100, 100)) == 1.0
    assert te.veg_share(veg, box(120, 120, 160, 160)) == 0.0
    assert te.veg_share(veg, box(75, 50, 125, 100)) == .5


def test_epoch_suspect_rule():
    assert te.epoch_suspect('flat', {'edge_support': .85, 'cross_capture': 'matched', 'outline_kind': 'matched'})
    assert not te.epoch_suspect('flat', {'edge_support': .7, 'cross_capture': 'matched', 'outline_kind': 'matched'})
    assert te.epoch_suspect('elevated', {'edge_support': .2, 'cross_capture': 'only_here', 'outline_kind': 'unmatched'})
    assert not te.epoch_suspect('elevated', {'edge_support': .2, 'cross_capture': 'matched', 'outline_kind': 'unmatched'})
    assert not te.epoch_suspect('unjudged', {'edge_support': .9, 'cross_capture': 'matched', 'outline_kind': 'matched'})
    assert not te.epoch_suspect('flat', {})


def test_control_discs_land_outside_every_polygon():
    bounds = (X0, Y0 - 100., X0 + 100., Y0)
    polygons = [box(X0 + 10, Y0 - 40, X0 + 40, Y0 - 10), box(X0 + 60, Y0 - 90, X0 + 90, Y0 - 60),
                box(X0 + 45, Y0 - 55, X0 + 55, Y0 - 45)]
    discs, draws = te.control_discs(bounds, polygons, 'chc25a-r000-c000')
    assert len(discs) == 50 and draws >= 50
    window = box(*bounds)
    for disc in discs:
        assert abs(disc.area - 20.) < .1
        assert window.contains(disc)
        assert all(disc.disjoint(p.buffer(2.)) for p in polygons)
    again, _ = te.control_discs(bounds, polygons, 'chc25a-r000-c000')
    assert [d.centroid.coords[0] for d in again] == [d.centroid.coords[0] for d in discs]
    other, _ = te.control_discs(bounds, polygons, 'chc25a-r000-c001')
    assert [d.centroid.coords[0] for d in other] != [d.centroid.coords[0] for d in discs]


def test_measure_window_rows_carry_every_field():
    dsm, dem, matrix = _rasters()
    dsm[20:35, 20:35] += 3.0
    ndsm = hs.ndsm(dsm, dem, nodata=-9999.0)
    gsd = .075
    affine = [X0, gsd, 0., Y0, 0., -gsd]                            # native px of a 0.075 m window
    px = 1. / gsd
    polygons = [({'index': 1}, box(20 * px, 20 * px, 35 * px, 35 * px)), ({'index': 2}, box(45 * px, 5 * px, 55 * px, 15 * px))]
    size = int(round(60 * px))
    veg = np.zeros((size, size), bool)
    joins = {('w', 1): {'edge_support': .9, 'cross_capture': 'matched', 'outline_kind': 'matched'},
             ('w', 2): {'edge_support': .9, 'cross_capture': 'matched', 'outline_kind': 'matched'}}
    rows, control, stats = te.measure_window('w', polygons, affine, gsd, ndsm, matrix, veg, ['T'], 'christchurch_2024-2025', joins,
                                             window_bounds=(X0, Y0 - 60., X0 + 60., Y0))
    assert [r['index'] for r in rows] == [1, 2]
    assert set(rows[0]) == {'window', 'index', 'area_m2', 'band', 'epoch', 'height_m', 'eave_m', 'iqr_m', 'samples', 'erode_m',
                            'share_above_2m', 'edge_step_m', 'edge_step_share', 'outside_ring_m', 'veg_share', 'nodata_share',
                            'ndsm_class', 'epoch_suspect', 'tiles'}
    assert rows[0]['ndsm_class'] == 'elevated' and abs(rows[0]['area_m2'] - 225.) < .01 and rows[0]['band'] == 'ge_200m2'
    assert rows[1]['ndsm_class'] == 'flat' and rows[1]['epoch_suspect'] is True     # flat, edge 0.9, matched: recorded only
    assert rows[0]['veg_share'] == 0.0 and rows[0]['tiles'] == ['T']
    assert len(control) == 50 and all(c['kind'] == 'control' for c in control)
    assert {'ndsm_class', 'height_m', 'veg_share', 'edge_step_m', 'x', 'y', 'area_m2'} <= set(control[0])
    assert stats['nodata_share'] == 0.0 and stats['instances'] == 2


# --- leave-one-signal-out ---------------------------------------------------------------------------------------------

def test_recommend_without_nothing_dropped_is_the_ledger_policy():
    from scripts.ml.training import teacher_judges as tj
    fixtures = [{'outline_kind': 'matched'},
                {'outline_kind': 'unmatched', 'cross_capture': 'matched', 'edge_support': .9, 'area_m2': 8.},
                {'outline_kind': 'part', 'area_m2': 30.},
                {'outline_kind': 'unmatched', 'area_m2': 8., 'cross_capture': 'only_here'},
                {'outline_kind': 'unmatched', 'area_m2': 40., 'cross_capture': None},
                {'outline_kind': 'overlapping', 'area_m2': 8.}]
    for entry in fixtures:
        assert te.recommend_without(entry, None) == tj.recommend(entry)
        assert te.recommend_without(entry) == tj.recommend(entry)
    assert [te.recommend_without(e, None) for e in fixtures] == ['keep', 'keep', 'part', 'ignore', 'review', 'review']
    # the drops replace one signal by its pre-registered no-information value and leave the entry untouched
    entry = {'outline_kind': 'matched', 'cross_capture': 'only_here', 'edge_support': .3, 'area_m2': 8.}
    assert te.recommend_without(entry, 'outline') == 'ignore' and entry['outline_kind'] == 'matched'
    assert te.recommend_without({'outline_kind': 'part', 'cross_capture': 'matched', 'edge_support': .2}, 'edge') == 'keep'
    assert te.recommend_without({'outline_kind': 'unmatched', 'area_m2': 40., 'cross_capture': 'only_here'}, 'area') == 'ignore'
    assert te.recommend_without({'outline_kind': 'part', 'cross_capture': 'matched', 'edge_support': .9}, 'cross') == 'part'
    assert te.recommend_without({'outline_kind': 'unmatched', 'area_m2': 8.}, 'outline_none') == 'review'
    with pytest.raises(KeyError):
        te.recommend_without(entry, 'nonsense')


def test_policy_v2_candidate():
    assert te.policy_v2('keep', 'flat') == 'review'
    assert te.policy_v2('keep', 'elevated') == 'keep' and te.policy_v2('keep', 'unjudged') == 'keep'
    assert te.policy_v2('ignore', 'elevated') == 'review' and te.policy_v2('ignore', 'mixed') == 'review'
    assert te.policy_v2('ignore', 'flat') == 'ignore' and te.policy_v2('ignore', 'unjudged') == 'ignore'
    assert te.policy_v2('part', 'flat') == 'part' and te.policy_v2('review', 'elevated') == 'review'
    assert te.LEDGER_POLICY_V2['rules'] == te.NDSM['policy_v2']['rules']


def test_real_ledger_reproduces_the_pre_registered_loso_tuples():
    path = JUDGES / 'ledger_rows.jsonl'
    _require(path, CROPS)
    from scripts.ml.training import teacher_judges as tj
    rows = [json.loads(line) for line in open(path)]
    assert len(rows) == 12679
    assert all(te.recommend_without(r) == r['recommendation'] for r in rows)
    split = tj.cell_split(str(REPO), 'linz-chc-2025-0075')
    judge = [r for r in rows if split.get(r['window']) == 'judge']
    assert len(judge) == 4132 and len({r['window'] for r in judge}) == 18
    counts = te.loso_counts(judge)
    assert counts['full_policy'] == {'keep': 3234, 'review': 426, 'ignore': 253, 'part': 219}
    assert counts['outline'] == {'keep': 2965, 'review': 810, 'ignore': 357, 'part': 0}
    assert counts['cross'] == {'keep': 2460, 'review': 634, 'ignore': 420, 'part': 618}
    assert counts['edge'] == {'keep': 3300, 'review': 384, 'ignore': 253, 'part': 195}
    assert counts['area'] == {'keep': 3234, 'review': 205, 'ignore': 474, 'part': 219}
    assert counts['outline_none'] == {'keep': 2965, 'review': 1167, 'ignore': 0, 'part': 0}   # semantics control
    registered = te.NDSM['loso']['pre_registered_counts_on_4132_judge_rows']
    assert all(counts[name] == registered[name] for name in registered)
    assert counts['outline_none'] == te.NDSM['loso']['semantics_control']['counts']
    # the plan's printed outline tuple (part 219) cannot hold on 4132 rows: recorded as a plan-text correction
    assert 2965 + 810 + 357 + 219 != len(judge)
    assert all(sum(c.values()) == len(judge) for c in counts.values())


def _ledger(window, index, kind, cross, edge, area):
    from scripts.ml.training import teacher_judges as tj
    entry = {'window': window, 'index': index, 'outline_kind': kind, 'cross_capture': cross, 'edge_support': edge,
             'area_m2': area, 'band': tj._band(area)}
    entry['recommendation'] = tj.recommend(entry)
    return entry


def _synthetic_judge_inputs():
    specs = [('matched', 'matched', .9, 120.), ('unmatched', 'only_here', .3, 8.), ('part', 'only_here', .4, 30.),
             ('unmatched', None, .2, 40.), ('overlapping', 'matched', .7, 15.), ('unmatched', 'matched', .8, 6.)]
    classes = ['elevated', 'flat', 'mixed', 'unjudged', 'elevated', 'flat']
    ledger, ndsm, control = [], [], []
    for w in ('w1', 'w2', 'w3', 'w4'):
        for i, spec in enumerate(specs, start=1):
            ledger.append(_ledger(w, i, *spec))
            ndsm.append({'window': w, 'index': i, 'ndsm_class': classes[(i + len(w)) % len(classes)], 'edge_step_m': 1.,
                         'edge_step_share': .5, 'veg_share': .1, 'height_m': 2.5, 'epoch_suspect': False})
        for k in range(5):
            control.append({'window': w, 'index': k, 'kind': 'control', 'ndsm_class': 'flat' if k else 'elevated',
                            'height_m': 0. if k else 6., 'veg_share': .3 if k else .1})
    split = {'w1': 'judge', 'w2': 'judge', 'w3': 'judge', 'w4': 'train'}
    return ledger, ndsm, control, split


def test_loso_report_shape_with_window_bootstrap_intervals():
    ledger, ndsm, control, split = _synthetic_judge_inputs()
    payload, loso_rows, v2_rows = te.judge_report(ledger, ndsm, control, split)
    assert set(payload['blocks']) == {'A', 'B', 'C'}
    assert payload['population']['rows'] == 18 and payload['population']['windows'] == 3
    b = payload['blocks']['B']
    assert set(b['full_policy']) == CLASSES and sum(b['full_policy'].values()) == 18
    for name in ('outline', 'cross', 'edge', 'area'):
        drop = b['drops'][name]
        assert set(drop['counts']) == CLASSES and sum(drop['counts'].values()) == 18
        assert drop['semantics'] and isinstance(drop['moved'], int)
        interval = drop['agreement_ci95_window_bootstrap']
        assert interval is None or (len(interval) == 2 and interval[0] <= drop['agreement_share'] <= interval[1])
        assert drop['reproduced'] is False                 # synthetic rows never meet the real tuples
    assert set(b['semantics_control']['counts']) == CLASSES
    assert b['pre_registered_reproduced'] is False
    keep = payload['blocks']['A']['by_recommendation']['keep']['all']
    assert set(keep['shares']) == {'elevated', 'flat', 'mixed', 'unjudged'}
    for interval in keep['ci95_window_bootstrap'].values():
        assert interval is None or (len(interval) == 2 and interval[0] <= interval[1])
    assert payload['blocks']['A']['control']['discs'] == 15
    assert 'unjudged' in payload['blocks']['A']['control']['shares']
    assert set(payload['blocks']['C']) >= {'by_cross_capture', 'by_outline_kind'}
    reads = payload['read_conditions']
    assert isinstance(reads['ignore_band_readable'], bool) and reads['read'] in ('ndsm_class', 'edge_step')
    assert len(loso_rows) == 18 and all('by_drop' in r for r in loso_rows)
    assert len(v2_rows) == len(ledger) and all('recommendation_v2' in r for r in v2_rows)
    assert payload['policy_v2']['policy']['version'] == 'ledger-policy-v2-candidate'
    assert 'roof confirmed' not in json.dumps(payload).replace('never roof confirmed', '')
    assert _negated_only(json.dumps(payload), 'accuracy') and not any('accuracy' in k for k in _keys(payload))
    control_block = payload['blocks']['A']['control']
    assert control_block['elevated_by_veg']['n'] == 3 and control_block['elevated_by_veg']['veg_lt_cut']['n'] == 3
    assert 'outside every teacher polygon' in control_block['placement']
    assert payload['blocks']['A']['control_all_windows']['elevated_by_veg']['n'] == 4
    assert (b['policy_thresholds']['min_edge'], b['policy_thresholds']['small_m2']) == te.ledger_policy_thresholds()
    assert b['stored_recommendations_reproduced'] is True and b['stored_recommendation_mismatches_all_cells'] == 0


# --- fetch manifest ---------------------------------------------------------------------------------------------------

def _meta(tmp_path):
    return {'role': 'dsm', 'pair': 'BX24_10000_0102', 'epoch': 'christchurch_2024-2025',
            'capture_date': '2024-12-09T11:00:00Z..2025-01-19T11:00:00Z'}


def test_manifest_record_fields():
    entry = te.manifest_entry('https://nz-elevation.s3.ap-southeast-2.amazonaws.com/x/BX24_10000_0102.tiff',
                              'christchurch_2024-2025/dsm/BX24_10000_0102.tiff', role='dsm', pair='BX24_10000_0102',
                              epoch='christchurch_2024-2025', capture_date='a..b', expected_sha256='ab' * 32, http_status=200,
                              status='ok', sha256='ab' * 32, nbytes=10, seconds=.5, note='verified')
    assert set(entry) == {'url', 'source', 'licence', 'fetched_at', 'http_status', 'status', 'path', 'sha256', 'bytes', 'seconds',
                          'capture_date', 'role', 'pair', 'epoch', 'expected_sha256', 'note'}
    assert entry['source'] == 'linz-elevation' and entry['role'] in ('dsm', 'dem') and entry['licence'] == te.LICENCE['licence']
    assert te.manifest_key(entry['url']) == hashlib.sha256(entry['url'].encode()).hexdigest()[:24]


def test_download_with_a_checksum_mismatch_is_recorded_as_error_and_unlinked(tmp_path, monkeypatch):
    def fake_download(url, tmp):
        Path(tmp).write_bytes(b'not the tile')
        return 200, 12, hashlib.sha256(b'not the tile').hexdigest()
    monkeypatch.setattr(te, '_download', fake_download)
    path = tmp_path / 'dsm' / 'BX24_10000_0102.tiff'
    entry = te.fetch_one('https://example.invalid/BX24_10000_0102.tiff', path, 'dsm/BX24_10000_0102.tiff', '0' * 64,
                         _meta(tmp_path), manifest={})
    assert entry['status'] == 'error' and 'mismatch' in entry['note'] and entry['http_status'] == 200
    assert not path.exists() and not Path(str(path) + '.part').exists()
    assert entry['expected_sha256'] == '0' * 64


def test_download_errors_are_recorded_never_raised(tmp_path, monkeypatch):
    def broken(url, tmp):
        raise OSError('connection reset')
    monkeypatch.setattr(te, '_download', broken)
    path = tmp_path / 'dem' / 'BX24_10000_0102.tiff'
    entry = te.fetch_one('https://example.invalid/t.tiff', path, 'dem/t.tiff', '0' * 64, _meta(tmp_path), manifest={})
    assert entry['status'] == 'error' and 'OSError' in entry['note'] and not path.exists()


def test_fetch_skips_a_verified_file(tmp_path, monkeypatch):
    body = b'a verified tile'
    sha = hashlib.sha256(body).hexdigest()
    path = tmp_path / 'dsm' / 'T.tiff'
    path.parent.mkdir(parents=True)
    path.write_bytes(body)
    url = 'https://example.invalid/T.tiff'
    manifest = {te.manifest_key(url): {'status': 'ok', 'sha256': sha, 'path': 'dsm/T.tiff'}}

    def must_not_download(url, tmp):
        raise AssertionError('a verified file was downloaded again')
    monkeypatch.setattr(te, '_download', must_not_download)
    entry = te.fetch_one(url, path, 'dsm/T.tiff', sha, _meta(tmp_path), manifest=manifest)
    assert entry['status'] == 'ok' and entry['note'].startswith('already verified') and entry['sha256'] == sha

    def fine(url, tmp):                           # a good download writes the file and records ok
        Path(tmp).write_bytes(body)
        return 200, len(body), sha
    monkeypatch.setattr(te, '_download', fine)
    fresh = tmp_path / 'dem' / 'T.tiff'
    entry = te.fetch_one(url + '?dem', fresh, 'dem/T.tiff', sha, _meta(tmp_path), manifest={})
    assert entry['status'] == 'ok' and fresh.read_bytes() == body and entry['bytes'] == len(body)


# --- review fixes: registered tuples, policy mirror, joins, epoch gap, cache key, summary wording ----------------------

def test_registered_loso_tuples_are_consistent_without_any_data_file():
    loso = te.NDSM['loso']
    registered = loso['pre_registered_counts_on_4132_judge_rows']
    assert set(registered) == {'full_policy', 'outline', 'cross', 'edge', 'area'}
    assert all(set(counts) == CLASSES and sum(counts.values()) == 4132 for counts in registered.values())
    assert sum(loso['semantics_control']['counts'].values()) == 4132
    # drop outline = outline_kind 'unmatched': recommend() returns part only for outline_kind 'part', so part is 0
    assert registered['outline'] == {'keep': 2965, 'review': 810, 'ignore': 357, 'part': 0}
    assert te.DROPS == {'outline': ('outline_kind', 'unmatched'), 'cross': ('cross_capture', None),
                        'edge': ('edge_support', 1.0), 'area': ('area_m2', 0.0)}
    correction = te.NDSM['plan_text_corrections'][0]
    assert correction['field'] == 'loso.pre_registered_counts_on_4132_judge_rows.outline.part'
    assert (correction['plan_sections_3_and_5'], correction['registered']) == (219, 0)
    assert sum(registered['outline'].values()) - registered['outline']['part'] + correction['plan_sections_3_and_5'] != 4132


def test_recommend_without_mirrors_the_policy_over_a_grid_with_its_own_thresholds():
    from scripts.ml.training import teacher_judges as tj
    params = inspect.signature(tj.recommend).parameters
    assert te.ledger_policy_thresholds() == (params['min_edge'].default, params['small_m2'].default)
    kinds = ('matched', 'part', 'unmatched', 'overlapping', 'merged', None)
    crosses = ('matched', 'only_here', None)
    edges = (None, 0., .59, .6, .61, 1.)
    areas = (None, 0., 11.99, 12., 12.01, 250.)
    for kind, cross, edge, area in itertools.product(kinds, crosses, edges, areas):
        entry = {'outline_kind': kind, 'cross_capture': cross, 'edge_support': edge, 'area_m2': area}
        assert te.recommend_without(entry) == tj.recommend(entry), entry
        for name, (field, value) in te.DROPS.items():
            assert te.recommend_without(entry, name) == tj.recommend({**entry, field: value}), (name, entry)
        assert te.recommend_without(entry, 'outline') != 'part'          # the drop leaves no row for the part branch
    # explicit thresholds still override the policy's defaults
    assert te.recommend_without({'outline_kind': 'unmatched', 'cross_capture': 'matched', 'edge_support': .5}, min_edge=.4) == 'keep'


def test_a_stored_recommendation_drift_fails_the_verdict_even_when_the_counts_match():
    ledger, ndsm, control, split = _synthetic_judge_inputs()
    payload, _, _ = te.judge_report(ledger, ndsm, control, split)
    b = payload['blocks']['B']
    rules = copy.deepcopy(te.NDSM)
    rules['loso']['pre_registered_counts_on_4132_judge_rows'] = {'full_policy': b['full_policy'],
                                                                 **{name: d['counts'] for name, d in b['drops'].items()}}
    rules['loso']['semantics_control']['counts'] = b['semantics_control']['counts']
    ok = te.judge_report(ledger, ndsm, control, split, rules)[0]['blocks']['B']
    assert ok['pre_registered_reproduced'] is True and ok['stored_recommendations_reproduced'] is True
    for cells, field in (('train', 'stored_recommendation_mismatches_all_cells'), ('judge', 'stored_recommendation_mismatches')):
        drifted = [dict(r) for r in ledger]
        victim = next(r for r in drifted if split[r['window']] == cells)
        victim['recommendation'] = 'keep' if victim['recommendation'] != 'keep' else 'review'
        bad = te.judge_report(drifted, ndsm, control, split, rules)[0]['blocks']['B']
        assert bad['full_policy'] == b['full_policy'] and bad[field] == 1
        assert bad['stored_recommendations_reproduced'] is False and bad['pre_registered_reproduced'] is False


def test_load_joins_mirrors_the_ledger_join(tmp_path):
    def jsonl(name, rows):
        (tmp_path / name).write_text(''.join(json.dumps(r) + '\n' for r in rows))
    jsonl('outlines_rows.jsonl', [{'arm': 'teacher', 'window': 'w1', 'index': 1, 'kind': 'matched'},
                                  {'arm': 'teacher', 'window': 'w1', 'index': 2, 'kind': 'unmatched'},
                                  {'arm': 'control', 'window': 'w1', 'index': 3, 'kind': 'part'},
                                  {'arm': 'teacher', 'window': 'w2', 'index': 1, 'kind': 'part'}])
    jsonl('edge_support_rows.jsonl', [{'arm': 'teacher', 'window': 'w1', 'index': 1, 'edge_support': .9},
                                      {'arm': 'control', 'window': 'w1', 'index': 2, 'edge_support': .1},
                                      {'arm': 'teacher', 'window': 'w2', 'index': 1, 'edge_support': .4}])
    summary = tmp_path / f'cross_capture_{te.OTHER_CAPTURE}.json'
    summary.write_text(json.dumps({'windows': [{'window': 'w1', 'matched': 1, 'only_here': 1}]}))
    jsonl(f'cross_capture_{te.OTHER_CAPTURE}_rows.jsonl', [{'window': 'w1', 'index': 1, 'other_index': 7, 'iou': .8}])
    joins = te.load_joins(tmp_path)
    assert set(joins) == {('w1', 1), ('w1', 2), ('w2', 1)}                     # the control arm never joins
    assert joins[('w1', 1)] == {'outline_kind': 'matched', 'edge_support': .9, 'cross_capture': 'matched'}
    assert joins[('w1', 2)] == {'outline_kind': 'unmatched', 'cross_capture': 'only_here'}   # compared window, no match
    assert joins[('w2', 1)] == {'outline_kind': 'part', 'edge_support': .4, 'cross_capture': None}   # window not compared
    summary.unlink()                                                            # no cross-capture summary: nothing compared
    assert all(join['cross_capture'] is None for join in te.load_joins(tmp_path).values())


def test_epoch_gap_days_reads_both_intervals_in_one_convention():
    lidar, imagery = ['2024-12-09T11:00:00Z', '2025-01-19T11:00:00Z'], ['2025-03-14T11:00:00Z', '2025-03-15T11:00:00Z']
    gap = te.epoch_gap_days(lidar, imagery)
    assert (gap['min'], gap['max']) == (54.0, 96.0)
    assert 'UTC' in gap['method'] and '11:00Z' in gap['method']
    assert te.epoch_gap_days([lidar[0], None], imagery) is None
    assert te.ELEVATION['christchurch_2024-2025']['temporal'] == lidar


def test_the_measure_cache_key_changes_with_every_input(tmp_path):
    window = {'id': 'w', 'bounds': [0., 0., 271.8, 271.8], 'native_affine': [0., .075, 0., 271.8, 0., -.075],
              'native_file': 'data/imagery/x/w.png', 'sha256': 'a' * 64, 'read_seconds': 3.2}
    tiles = {'dsm': {'T': 't'}, 'dem': {'T': 'u'}}
    code = te.code_sha256(*te.MEASURE_CODE)
    base = {'rules_sha256': 'r', 'crops_sha256': 'c', 'joins_sha256': {'outlines_rows.jsonl': 'o'}, 'code': code}
    key = te.window_inputs(window, 'rec', 'png', tiles, **base)
    assert key == te.window_inputs(dict(window, read_seconds=9.9), 'rec', 'png', tiles, **base)     # timings are not inputs
    variants = [te.window_inputs(dict(window, native_affine=[0., .05, 0., 271.8, 0., -.05]), 'rec', 'png', tiles, **base),
                te.window_inputs(dict(window, bounds=[1., 0., 272.8, 271.8]), 'rec', 'png', tiles, **base),
                te.window_inputs(dict(window, native_file='data/imagery/x/v.png'), 'rec', 'png', tiles, **base),
                te.window_inputs(dict(window, sha256='b' * 64), 'rec', 'png', tiles, **base),
                te.window_inputs(window, 'rec2', 'png', tiles, **base),
                te.window_inputs(window, 'rec', 'png2', tiles, **base),
                te.window_inputs(window, 'rec', 'png', {'dsm': {'T': 'x'}, 'dem': {'T': 'u'}}, **base),
                te.window_inputs(window, 'rec', 'png', tiles, **{**base, 'rules_sha256': 'r2'}),
                te.window_inputs(window, 'rec', 'png', tiles, **{**base, 'crops_sha256': 'c2'}),
                te.window_inputs(window, 'rec', 'png', tiles, **{**base, 'joins_sha256': {'outlines_rows.jsonl': 'o2'}}),
                te.window_inputs(window, 'rec', 'png', tiles, **{**base, 'code': {**code, 'teacher_judges': 'x'}})]
    assert all(variant != key for variant in variants)
    assert key['module_sha256'] == te.MODULE_SHA256 == hashlib.sha256(Path(te.__file__).read_bytes()).hexdigest()
    assert {'version': te.VERSION, 'rules_sha256': 'r', 'record_sha256': 'rec', 'tiles': tiles} != key   # an old entry never matches
    training = Path(te.__file__).parent
    assert code['teacher_judges'] == hashlib.sha256((training / 'teacher_judges.py').read_bytes()).hexdigest()
    assert code['heights_source'] == hashlib.sha256((training.parent / 'heights_source.py').read_bytes()).hexdigest()
    (tmp_path / 'outlines_rows.jsonl').write_text('{}\n')
    shas = te.join_files_sha256(tmp_path)
    assert set(shas) == set(te.JOIN_FILES) and shas['edge_support_rows.jsonl'] is None
    assert shas['outlines_rows.jsonl'] == hashlib.sha256(b'{}\n').hexdigest()


def test_measure_summary_names_the_product_spec_splits_control_by_vegetation_and_records_code(tmp_path):
    rules_file = tmp_path / 'ndsm_rules.json'
    rules_file.write_text(json.dumps(te.NDSM))
    spec = te.epoch_spec('christchurch_2024-2025')
    interval = spec['temporal']
    cat = {'temporal': {'dsm': interval, 'dem': interval}, 'straddling_windows': {},
           'layers': {layer: {'collection_id': meta['collection_id'], 'title': 't', 'koordinates_layer': meta['koordinates_layer'],
                              'prefix': meta['prefix'], 'license': 'CC-BY-4.0',
                              'tiles': {'BX24_10000_0102': {'expected_sha256': 'e' * 64, 'start': interval[0], 'end': interval[1]}}}
                      for layer, meta in spec['layers'].items()}}
    imagery = tmp_path / 'data/imagery/linz-chc-2025-0075/catalog.json'
    imagery.parent.mkdir(parents=True)
    imagery.write_text(json.dumps({'extent': {'temporal': {'interval': [['2025-03-14T11:00:00Z', '2025-03-15T11:00:00Z']]}}}))
    rows = [{'window': 'w', 'index': 1, 'band': 'lt_50m2', 'ndsm_class': 'elevated', 'height_m': 5., 'veg_share': .1,
             'edge_step_m': 2., 'erode_m': 1.0, 'epoch_suspect': False}]
    control = [{'window': 'w', 'index': k, 'kind': 'control', 'ndsm_class': cls, 'height_m': height, 'veg_share': veg}
               for k, (cls, height, veg) in enumerate([('elevated', 6., .05), ('elevated', 8., .7), ('elevated', 5., .9),
                                                       ('flat', 0., .2), ('mixed', 1.5, None)])]
    per_window = [{'window': 'w', 'tiles': ['BX24_10000_0102'], 'read': 'window', 'nodata_share': 0., 'instances': 1,
                   'control_discs': 5, 'control_draws': 9}]
    code = te.code_sha256(*te.MEASURE_CODE)
    payload = te.summarise_measure(tmp_path, 'linz-chc-2025-0075', 'christchurch_2024-2025', spec, cat, rows, control, per_window,
                                   rules_file, 'r' * 64, 'jobgate line', 'swap line',
                                   provenance={'module_sha256': code['teacher_elevation'], 'code_sha256': code})
    assert payload['module_sha256'] == te.MODULE_SHA256 and payload['code_sha256'] == code
    product = payload['linz_product_spec_95']
    assert (product['vertical_m_95'], product['horizontal_m_95']) == (0.2, 1.0)
    assert 'not a figure about this judge' in product['text']
    split = payload['control_shares']['elevated_by_veg']
    assert (split['n'], split['veg_lt_cut']['n'], split['veg_ge_cut']['n'], split['veg_share_cut']) == (3, 1, 2, .2)
    assert split['veg_lt_cut']['share'] == round(1 / 3, 4) and split['veg_lt_cut']['height_m']['median'] == 6.0
    assert 'outside every teacher polygon' in payload['control_shares']['placement']
    assert (payload['epoch_gap_days']['min'], payload['epoch_gap_days']['max']) == (54.0, 96.0)
    mtime = datetime.fromtimestamp(rules_file.stat().st_mtime, tz=UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    assert payload['rules']['file_mtime_utc'] == mtime and payload['rules']['sha256'] == 'r' * 64
    text = json.dumps(payload)
    assert not any('accuracy' in k for k in _keys(payload)) and _negated_only(text, 'accuracy')
    assert _negated_only(text, 'roof confirmed')
    assert not any('accuracy' in k for k in _keys(te.ELEVATION))


def test_checkpoint_sentence_reads_every_number_from_the_payload():
    ledger, ndsm, control, split = _synthetic_judge_inputs()
    payload, _, _ = te.judge_report(ledger, ndsm, control, split)
    sentence = te.checkpoint_sentence(payload, 'christchurch_2024-2025', {'min': 54.0, 'max': 96.0})
    a = payload['blocks']['A']
    assert 'outside every teacher polygon' in sentence and 'off-roof' not in sentence
    assert f"{100 * a['control']['shares']['elevated']:.1f} percent of control discs" in sentence
    assert f"{100 * a['control']['elevated_by_veg']['veg_lt_cut']['share']:.1f} percent of those elevated discs" in sentence
    assert '54-96 days before the imagery by the STAC interval bounds' in sentence
    assert f"moved {payload['blocks']['B']['drops']['outline']['moved']} rows" in sentence
    assert _negated_only(sentence, 'roof confirmed') and 'accuracy' not in sentence


READ_LAYER_CHECK = """
import sys
from pathlib import Path
import numpy as np
import rasterio
from rasterio.transform import from_origin
from scripts.ml.training import teacher_elevation as te
spec = te.ELEVATION['christchurch_2024-2025']
folder = Path(sys.argv[1])
W, H, left0, top0 = 40, 30, 1_568_760.0, 5_186_030.0
paths = []
for k in range(2):                                   # tile B sits east of tile A; value = 1000 * row + global column
    rows, cols = np.mgrid[0:H, 0:W]
    path = folder / f'tile{k}.tif'
    with rasterio.open(path, 'w', driver='GTiff', width=W, height=H, count=1, dtype='float32', crs=spec['crs'],
                       transform=from_origin(left0 + k * W, top0, 1.0, 1.0), nodata=spec['nodata']) as ds:
        ds.write((1000.0 * rows + cols + k * W).astype('float32'), 1)
    paths.append(path)
def expected(bounds):
    left, bottom, right, top = bounds
    gy = (top0 - top) + np.arange(int(top - bottom))[:, None]
    gx = (left - left0) + np.arange(int(right - left))[None, :]
    inside = (gy >= 0) & (gy < H) & (gx >= 0) & (gx < 2 * W)
    return np.where(inside, 1000.0 * gy + gx, spec['nodata']).astype('float32')
cases = [(paths, (left0 + 25, top0 - 22, left0 + 57, top0 - 3)),     # straddling: rasterio.merge, cut to the box
         (paths, (left0 + 30, top0 - 34, left0 + 50, top0 - 20)),    # straddling and past the south edge
         (paths[:1], (left0 + 5, top0 - 25, left0 + 30, top0 - 4)),  # one tile: window read
         (paths[:1], (left0 - 3, top0 - 10, left0 + 8, top0 - 2))]   # one tile, past the west edge: boundless
for ps, bounds in cases:
    array, transform = te.read_layer(ps, bounds, spec)
    want = expected(bounds)
    assert array.shape == want.shape, (bounds, array.shape, want.shape)
    assert np.array_equal(array, want), (bounds, float(np.abs(array - want).max()))
    assert (transform.c, transform.f, transform.a, transform.e) == (bounds[0], bounds[3], 1.0, -1.0), (bounds, transform)
print('read_layer ok', len(cases))
"""


def test_read_layer_merges_and_cuts_exactly_under_the_training_venv(tmp_path):
    """read_layer needs rasterio, which only the training venv has: the check runs there as a plain script (pytest itself
    stays in the main venv) on two synthetic 1 m tiles, covering the merge path's [:height, :width] cut and the
    single-tile boundless window."""
    _require(TRAINING_PY, what='the training venv (not in git)')
    done = subprocess.run([str(TRAINING_PY), '-c', READ_LAYER_CHECK, str(tmp_path)], cwd=REPO, capture_output=True, text=True,
                          check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip() == 'read_layer ok 4'
