"""select --windows-from at a finer pixel, and the crop freshness test (teacher_sources, stream W2).

A source that shares another source's cells must keep the cells (bounds) and re-derive the pixel geometry at its own
pixel: the 5 cm capture cut on the 7.5 cm source's cells is 5436 px, not 3624 px. A cached window whose shape is not
the window size is stale and must be re-cut."""
import json
from types import SimpleNamespace

import numpy as np
import pytest
from scripts.ml.training import teacher_sources as ts

A_ID, B_ID, C_ID = 'linz-chc-2025-0075', 'linz-chc-2025-005', 'linz-chc-2020-0075'
LEFT, TOP, WINDOW_M = 1572091.2, 5181323.4, 271.8


def _cell(prefix, row, col, left, top, tiles):
    return {'id': f'{prefix}-r{row:03d}-c{col:03d}', 'row': row, 'col': col, 'bounds': [left, top - WINDOW_M, left + WINDOW_M, top],
            'tiles': tiles, 'centre_lonlat': [172.65, -43.52]}


def _theirs(root):
    """The 7.5 cm source's windows.json in the shape tonight's file has: bounds_crs, geo without native_gsd_m."""
    spec = ts.SOURCES[A_ID]
    order = [_cell('chc25a', 21, 47, LEFT, TOP, ['A1']),
             _cell('chc25a', 30, 10, LEFT - 20 * WINDOW_M, TOP - 9 * WINDOW_M, ['A2'])]   # the finer source has no tile here
    theirs = {'version': spec['version'], 'source': A_ID, 'native_window_px': 3624, 'window_m': 271.8,
              'geo': {'crs': 'EPSG:2193', 'gsd_m': .075, 'gsd_x_m': .075, 'gsd_y_m': .075, 'origin_e': 1559316.6,
                      'origin_n': 5187031.2, 'source': 'window lattice aligned to whole window multiples in the source CRS'},
              'n': 60, 'seed': 20260928, 'rule': {**ts.SUBURB_RULE, 'reduce': 13, 'overview_m': 1.0}, 'rule_text': 'seeded',
              'bbox_lonlat': [172.5, -43.58, 172.72, -43.47], 'bounds_crs': [1559316.6, 5174000.0, 1577000.0, 5187031.2],
              'grid': {'rows': 48, 'cols': 65, 'origin_x': 1559316.6, 'origin_top': 5187031.2},
              'counts': {'cells_covered': 2916, 'eligible': 60, 'overview_px': 272}, 'order': order, 'rejected': []}
    folder = root / spec['out'] / 'teacher'
    folder.mkdir(parents=True)
    (folder / 'windows.json').write_text(json.dumps(theirs))
    return theirs


def _catalogue(root, spec, tiles):
    cache = root / spec['cache']
    cache.mkdir(parents=True, exist_ok=True)
    (cache / 'catalog.json').write_text(json.dumps({'tiles': tiles, 'bucket': 'nz-imagery', 'prefix': 'k/', 'sheets': {}}))
    return cache


def _covering_tiles(prefix='k'):
    """Two tiles that together cover the first cell exactly (and nothing near the second)."""
    half = WINDOW_M / 2
    return {'T1': {'key': f'{prefix}/T1.tiff', 'bounds': [LEFT - 1., TOP - WINDOW_M - 1., LEFT + half, TOP + 1.]},
            'T2': {'key': f'{prefix}/T2.tiff', 'bounds': [LEFT + half, TOP - WINDOW_M - 1., LEFT + WINDOW_M + 1., TOP + 1.]}}


def test_select_from_rederives_the_pixel_geometry_at_the_finer_pixel(tmp_path):
    theirs = _theirs(tmp_path)
    spec = ts.SOURCES[B_ID]
    cache = _catalogue(tmp_path, spec, _covering_tiles())
    result = ts.select_from(tmp_path, spec, A_ID)
    assert result['kept'] == 1 and result['dropped_not_covered'] == ['chc25a-r030-c010']
    ours = json.loads((tmp_path / spec['out'] / 'teacher' / 'windows.json').read_text())
    assert ts.native_window_px(.05) == 5436 and ours['native_window_px'] == 5436
    geo = ours['geo']
    assert geo['gsd_m'] == geo['gsd_x_m'] == geo['gsd_y_m'] == .05
    assert geo['native_gsd_m'] == .05                                  # a NEW key: the 7.5 cm file never had it
    assert (geo['crs'], geo['origin_e'], geo['origin_n']) == (theirs['geo']['crs'], theirs['geo']['origin_e'], theirs['geo']['origin_n'])
    assert abs(ours['window_m'] - 271.8) < 1e-9 and abs(ours['native_window_px'] * geo['gsd_m'] - ours['window_m']) < 1e-6
    assert ours['rule']['reduce'] == round(ts.OVERVIEW_M / .05) == 20
    assert ours['rule']['overview_m'] == 1.0 and ours['rule']['veg_min'] == theirs['rule']['veg_min']
    assert ours['bounds_crs'] == theirs['bounds_crs'] and 'bounds' not in ours
    assert [c['bounds'] for c in ours['order']] == [theirs['order'][0]['bounds']]
    kept = ours['order'][0]
    assert kept['id'] == 'chc25b-r021-c047' and kept['shared_id'] == 'chc25a-r021-c047' and kept['shared_with'] == A_ID
    assert kept['tiles'] == ['T1', 'T2']
    assert ours['counts']['shared_kept'] == 1 and ours['counts']['shared_dropped'] == 1
    overview = json.loads((cache / 'overview-r20.json').read_text())
    assert overview['reduce'] == 20 and overview['shared_from'] == A_ID
    assert overview['geo_box']['bounds'] == theirs['bounds_crs']
    assert not (cache / 'overview-r13.json').exists()


def test_select_from_refuses_a_pixel_that_does_not_tile_the_shared_window(tmp_path):
    _theirs(tmp_path)
    spec = {**ts.SOURCES[B_ID], 'gsd_m': .07}                          # 3883 px x 0.07 m = 271.81 m, not 271.8 m
    _catalogue(tmp_path, spec, _covering_tiles())
    assert abs(ts.native_window_px(.07) * .07 - 271.8) > 1e-6
    with pytest.raises(SystemExit, match='271.8'):
        ts.select_from(tmp_path, spec, A_ID)
    assert not (tmp_path / spec['out'] / 'teacher' / 'windows.json').exists()


def test_select_from_at_equal_pixel_leaves_the_geometry_unchanged(tmp_path):
    theirs = _theirs(tmp_path)
    spec = ts.SOURCES[C_ID]
    cache = _catalogue(tmp_path, spec, _covering_tiles())
    ts.select_from(tmp_path, spec, A_ID)
    ours = json.loads((tmp_path / spec['out'] / 'teacher' / 'windows.json').read_text())
    assert ours['native_window_px'] == theirs['native_window_px'] == 3624
    assert ours['window_m'] == theirs['window_m']
    for key in ('gsd_m', 'gsd_x_m', 'gsd_y_m', 'crs', 'origin_e', 'origin_n', 'source'):
        assert ours['geo'][key] == theirs['geo'][key], key
    assert ours['rule'] == theirs['rule']
    assert ours['bounds_crs'] == theirs['bounds_crs']
    assert [c['bounds'] for c in ours['order']] == [theirs['order'][0]['bounds']]
    assert (cache / 'overview-r13.json').exists()


def _crop_fixture(root, cached_shape):
    """005 windows.json at 5436 px with one window whose cached PNG and meta match in tiles and georeference."""
    spec = ts.SOURCES[B_ID]
    tiles = _covering_tiles(prefix='canterbury/christchurch_2025_0.05m/rgb/2193')
    cache = _catalogue(root, spec, tiles)
    (cache / 'tiles').mkdir()
    for name in tiles:
        (cache / 'tiles' / f'{name}.tiff').write_bytes(b'')          # fetched tiles exist on disk
    entry = {**_cell('chc25b', 21, 47, LEFT, TOP, ['T1', 'T2']), 'shared_id': 'chc25a-r021-c047', 'shared_with': A_ID}
    windows = {'version': spec['version'], 'source': B_ID, 'native_window_px': 5436, 'window_m': 271.8,
               'geo': {'crs': 'EPSG:2193', 'gsd_m': .05, 'gsd_x_m': .05, 'gsd_y_m': .05, 'native_gsd_m': .05,
                       'origin_e': 1559316.6, 'origin_n': 5187031.2}, 'n': 60, 'order': [entry]}
    teacher = root / spec['out'] / 'teacher'
    teacher.mkdir(parents=True)
    (teacher / 'windows.json').write_text(json.dumps(windows))
    out = cache / 'windows'
    out.mkdir()
    (out / f"{entry['id']}.png").write_bytes(b'cached png')
    meta = {'id': entry['id'], 'row': 21, 'col': 47, 'shape': cached_shape, 'inside_fraction': 1.0, 'read_seconds': 1.,
            'tiles': ['T1', 'T2'], 'tile_keys': [tiles['T1']['key'], tiles['T2']['key']],
            'transform': [LEFT, .075, 0., TOP, 0., -.075], 'sha256': 'x'}
    (out / f"{entry['id']}.json").write_text(json.dumps(meta))
    return spec, entry, teacher


def _recording_reader(monkeypatch):
    calls = []

    def fake_read_window(root, spec, cat, bounds, res, names, remote):
        calls.append({'bounds': list(bounds), 'res': res, 'names': list(names), 'remote': remote})
        rgba = np.zeros((8, 8, 4), np.uint8)                           # never the window size: nothing is saved
        return rgba, SimpleNamespace(a=res, b=0., c=bounds[0], d=0., e=-res, f=bounds[3])

    monkeypatch.setattr(ts, 'read_window', fake_read_window)
    return calls


def test_crop_recuts_a_cached_window_cut_at_the_coarser_pixel(tmp_path, monkeypatch):
    spec, entry, teacher = _crop_fixture(tmp_path, cached_shape=[3624, 3624, 3])
    calls = _recording_reader(monkeypatch)
    ts.crop(tmp_path, spec)
    assert len(calls) == 1, 'a 3624 px cached window is not fresh for a 5436 px window'
    assert calls[0]['res'] == .05 and calls[0]['bounds'] == entry['bounds'] and calls[0]['remote'] is False
    meta = json.loads((tmp_path / spec['cache'] / 'windows' / f"{entry['id']}.json").read_text())
    assert meta['shape'] == [5436, 5436, 3] and meta['inside_fraction'] == 0.
    crops = json.loads((teacher / 'crops.json').read_text())
    assert crops['native_window_px'] == 5436 and crops['gsd_m'] == .05 and crops['windows'] == []


def test_crop_keeps_a_cached_window_whose_shape_is_the_window_size(tmp_path, monkeypatch):
    spec, entry, teacher = _crop_fixture(tmp_path, cached_shape=[5436, 5436, 3])
    calls = _recording_reader(monkeypatch)
    ts.crop(tmp_path, spec)
    assert calls == []
    crops = json.loads((teacher / 'crops.json').read_text())
    assert [w['id'] for w in crops['windows']] == [entry['id']] and crops['windows'][0]['shape'] == [5436, 5436, 3]
