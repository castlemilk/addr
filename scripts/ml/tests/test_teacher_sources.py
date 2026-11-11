import json

import numpy as np
import pytest
from scripts.ml.training import hires_teacher as ht
from scripts.ml.training import teacher_sources as ts


def test_registry_entries_are_complete():
    required = {'id', 'kind', 'title', 'publisher', 'licence', 'cache', 'out', 'window_prefix', 'version', 'provenance_tag'}
    for key, spec in ts.SOURCES.items():
        assert spec['id'] == key
        assert required <= set(spec), (key, required - set(spec))
        assert spec['kind'] in ('com-jp2', 'linz-stac', 'geotiff', 'arcgis-image')
    prefixes = [s['window_prefix'] for s in ts.SOURCES.values()]
    assert len(prefixes) == len(set(prefixes)), 'window prefixes must be unique so window ids never collide across sources'
    assert ts.SOURCES['com-2018']['out'] == str(ht.OUT)


def test_tile_name_parsing_and_sheet_origin():
    assert ts.parse_tile_name('BX24_500_023018') == ('BX24', 500, 23, 18)
    assert ts.parse_tile_name('BX24_500_023018.tiff') == ('BX24', 500, 23, 18)
    assert ts.parse_tile_name('collection') is None
    # measured header of BX24_500_023018 (LINZ Christchurch 2025 0.05 m): left 1568080, top 5182080
    left, top = ts.sheet_origin_from_tile('BX24_500_023018', 1568080., 5182080.)
    assert (left, top) == (1564000., 5190000.)
    bounds = ts.tile_bounds('BX24_500_023018', {'BX24': (left, top)})
    assert bounds == (1568080., 5181720., 1568320., 5182080.)
    assert bounds[2] - bounds[0] == ts.TILE_E_M and bounds[3] - bounds[1] == ts.TILE_N_M


def test_multihash_sha256():
    assert ts.multihash_sha256('1220' + 'ab' * 32) == 'ab' * 32
    assert ts.multihash_sha256('1114' + 'ab' * 20) is None
    assert ts.multihash_sha256(None) is None


def test_native_window_matches_pipeline_tile_ground_size():
    for gsd in (.05, .075, .1):
        px = ts.native_window_px(gsd)
        assert abs(px * gsd - ts.STUDENT_PX * ts.PIPELINE_PX_M) < gsd
    assert ts.native_window_px(.1) == ht.native_window_px(.1)


def test_window_grid_and_cells_tile_the_bbox():
    rows, cols, ox, ot = ts.window_grid((1000.5, 2000.5, 1900., 2700.), 271.8)
    assert ox <= 1000.5 and ot >= 2700.
    assert ox + cols * 271.8 >= 1900. and ot - rows * 271.8 <= 2000.5
    b = ts.cell_bounds(0, 0, ox, ot, 271.8)
    assert b[0] == ox and b[3] == ot and abs((b[2] - b[0]) - 271.8) < 1e-9
    b2 = ts.cell_bounds(1, 2, ox, ot, 271.8)
    assert abs(b2[0] - (ox + 2 * 271.8)) < 1e-9 and abs(b2[3] - (ot - 271.8)) < 1e-9


def test_coverage_uses_area_of_the_tile_grid():
    sheets = {'BX24': (1564000., 5190000.)}
    names = ['BX24_500_023018', 'BX24_500_023019', 'BX24_500_024018', 'BX24_500_024019']
    cat = {'tiles': {n: {'bounds': list(ts.tile_bounds(n, sheets))} for n in names}}
    inside = (1568100., 5181400., 1568500., 5182000.)     # spans the four tiles, fully covered
    assert ts.tiles_covering(cat, inside) == sorted(names)
    assert ts.fully_covered(cat, inside)
    edge = (1568500., 5181400., 1568700., 5182000.)       # walks off the right edge of the catalogued tiles
    assert not ts.fully_covered(cat, edge)


def test_classify_window_rule_reasons():
    rgba = np.zeros((64, 64, 4), np.uint8)
    rgba[..., 3] = 255
    rgba[..., :3] = 120
    stats = ts.classify_window(rgba)
    assert stats['inside'] == 1.
    assert 'low_structure' in stats['reasons'] and 'bare_or_commercial' in stats['reasons']
    rgba[:, :, 3] = 0
    stats = ts.classify_window(rgba)
    assert 'outside_imagery' in stats['reasons'] and not stats['eligible']


def test_set_source_rebinds_paths_and_restores_for_com(tmp_path):
    spec = ht.set_source('linz-chc-2025-0075', tmp_path)
    try:
        assert ht.VERSION == spec['version'] and ht.PROVENANCE_TAG == spec['provenance_tag']
        assert str(ht.TEACHER).endswith('distill-linz-chc-2025-0075/teacher')
        assert ht.window_id(3, 4) == 'chc25a-r003-c004'
        assert ht.SOURCE['licence'] == 'CC BY 4.0' and ht.SOURCE['crs'] == 'EPSG:2193'
        assert 'NZTM' in ht.SOURCE['grid_note']
    finally:
        assert ht.set_source('com-2018')['kind'] == 'com-jp2'
    assert ht.window_id(1, 1) == 'com2018-r001-c001'
    assert ht.VERSION == 'hires-teacher-com-2018-v1' and ht.SOURCE['publisher'] == 'City of Melbourne'
    assert str(ht.TEACHER).endswith('distill-v1/teacher')


def test_unknown_source_exits_with_known_list():
    with pytest.raises(SystemExit, match='linz-chc-2025-0075'):
        ts.source('nope')


def test_jp2_stages_refused_for_geotiff_sources():
    try:
        with pytest.raises(SystemExit, match='teacher_sources'):
            ht.main(['crop', '--source', 'linz-chc-2025-005'])
    finally:
        ht.set_source('com-2018')


def test_windows_json_schema_matches_finalize_expectations(tmp_path, monkeypatch):
    """select() output must carry every key finalize/manifest read back (rule.reduce, seed, n, rule_text, counts, geo)."""
    spec = ts.SOURCES['linz-chc-2025-0075']
    cache = tmp_path / spec['cache']
    cache.mkdir(parents=True)
    sheets = {'BX24': [1564000., 5190000.]}
    names = [f'BX24_500_{r:03d}{c:03d}' for r in range(20, 26) for c in range(15, 24)]
    tiles = {n: {'key': f'k/{n}.tiff', 'item_key': f'k/{n}.json', 'sheet': 'BX24', 'bounds': list(ts.tile_bounds(n, sheets))} for n in names}
    json.dump({'tiles': tiles, 'bucket': 'b', 'prefix': 'k/', 'sheets': sheets}, open(cache / 'catalog.json', 'w'))

    def fake_mosaic(paths, bounds, res, nodata=0, **kwargs):
        size = int(round((bounds[2] - bounds[0]) / res))
        rgba = np.zeros((size, size, 4), np.uint8)
        rgba[..., 3] = 255
        yy, xx = np.mgrid[0:size, 0:size]
        checker = (((yy // 6) + (xx // 6)) % 2) * 110 + 70            # 6 m grey blocks: edges everywhere, no colour cast
        rgba[..., 0] = rgba[..., 1] = rgba[..., 2] = checker
        rgba[: size // 8, :, :3] = (60, 200, 60)                        # a green strip: some vegetation (about 0.125)
        return rgba, None

    monkeypatch.setattr(ts, 'read_mosaic', fake_mosaic)
    monkeypatch.setattr(ts, 'to_lonlat', lambda crs, xs, ys: ([172.6] * len(xs), [-43.5] * len(ys)))
    monkeypatch.setattr(ts, 'bbox_to_crs', lambda crs, b: (1567600., 5181200., 1569000., 5183000.))   # inside the fake tiles
    result = ts.select(tmp_path, {**spec, 'exclude_bbox_lonlat': []}, n=3, seed=1, bbox_lonlat=[0, 0, 1, 1], candidates=20)
    windows = json.load(open(tmp_path / spec['out'] / 'teacher' / 'windows.json'))
    for key in ('seed', 'n', 'rule', 'rule_text', 'counts', 'geo', 'order', 'native_window_px'):
        assert key in windows
    assert 'reduce' in windows['rule'] and windows['geo']['gsd_m'] == .075
    assert (cache / f"overview-r{windows['rule']['reduce']}.json").exists()
    assert result['counts']['classified'] >= result['counts']['eligible']
    assert windows['order'], 'the fake tiles cover the bbox, so eligible windows must exist'
    for w in windows['order']:
        assert w['id'].startswith('chc25a-r') and len(w['tiles']) >= 1 and w['centre_lonlat'] == [172.6, -43.5]


def test_gdal_path_and_effective_gsd():
    assert ts.gdal_path('https://h/x.jp2') == '/vsicurl/https://h/x.jp2'
    assert ts.gdal_path('s3://b/k.tiff') == '/vsis3/b/k.tiff'
    assert ts.gdal_path('/tmp/a.tif') == '/tmp/a.tif'
    assert ts.effective_gsd(ts.SOURCES['com-2020-2cm']) == .05       # 1.9 cm source read at a 5 cm teacher pixel
    assert ts.effective_gsd(ts.SOURCES['linz-chc-2025-0075']) == .075
    assert ts.native_window_px(ts.effective_gsd(ts.SOURCES['com-2020-2cm'])) == 5436


def test_arcgis_catalog_from_service_info():
    info = {'name': 'ACT 2023', 'extent': {'xmin': 678000., 'ymin': 6068000., 'xmax': 720000., 'ymax': 6112000.,
                                           'spatialReference': {'wkid': 7855, 'latestWkid': 7855}},
            'pixelSizeX': 0.0746455, 'pixelSizeY': 0.0746455, 'maxImageWidth': 15000, 'maxImageHeight': 4100, 'bandCount': 3,
            'copyrightText': 'ACT Government'}
    cat = ts.arcgis_catalog_from_info(ts.SOURCES['act-2023-urban-75mm'], info)
    assert cat['crs'] == 'EPSG:7855' and cat['gsd_m'] == .074646
    assert cat['tiles']['service']['arcgis'] and cat['tiles']['service']['bounds'] == [678000., 6068000., 720000., 6112000.]
    assert cat['export_limits'] == {'max_width': 15000, 'max_height': 4100}
    assert ts.fully_covered(cat, (700000., 6090000., 700271.8, 6090271.8))
    assert not ts.fully_covered(cat, (719900., 6090000., 720171.8, 6090271.8))


def test_read_window_dispatches_by_kind(monkeypatch):
    calls = []
    monkeypatch.setattr(ts, 'arcgis_export', lambda url, bounds, size, epsg: calls.append(('arcgis', url, size, epsg)) or ('rgba', None))
    monkeypatch.setattr(ts, 'read_mosaic', lambda paths, bounds, res, **kw: calls.append(('mosaic', list(paths))) or ('rgba', None))
    act = ts.SOURCES['act-2023-urban-75mm']
    cat = {'crs': 'EPSG:7855', 'tiles': {'service': {'key': act['url'], 'arcgis': True}}}
    ts.read_window('.', act, cat, (0., 0., 271.8, 271.8), 1., ['service'], remote=True)
    assert calls[-1] == ('arcgis', act['url'], 272, 7855)
    linz = ts.SOURCES['linz-chc-2025-0075']
    cat = {'bucket': 'nz-imagery', 'tiles': {'A': {'key': 'k/A.tiff'}}}
    ts.read_window('.', linz, cat, (0., 0., 1., 1.), 1., ['A'], remote=True)
    assert calls[-1] == ('mosaic', ['s3://nz-imagery/k/A.tiff'])
    com = ts.SOURCES['com-2020-2cm']
    cat = {'tiles': {'T': {'key': com['url'], 'remote': True}}}
    ts.read_window('.', com, cat, (0., 0., 1., 1.), .05, ['T'], remote=True)
    assert calls[-1] == ('mosaic', [com['url']])


def test_select_refuses_when_no_window_fits(tmp_path, monkeypatch):
    spec = ts.SOURCES['drone-ortho']
    cache = tmp_path / spec['cache']
    cache.mkdir(parents=True)
    json.dump({'tiles': {'lot': {'key': '/x/lot.tif', 'local': True, 'bounds': [0., 0., 60., 50.]}}, 'bucket': None, 'prefix': None,
               'sheets': {}, 'header': {'left': 0., 'bottom': 0., 'right': 60., 'top': 50.}, 'crs': 'EPSG:7855', 'gsd_m': .02},
              open(cache / 'catalog.json', 'w'))
    monkeypatch.setattr(ts, 'to_lonlat', lambda crs, xs, ys: ([145.3] * len(xs), [-37.8] * len(ys)))
    with pytest.raises(SystemExit, match='no 272 m window cell'):
        ts.select(tmp_path, {**spec, 'gsd_m': .02, 'crs': 'EPSG:7855'}, n=3, seed=1, candidates=5, remote=False)
