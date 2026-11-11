import json
import math

import numpy as np
import pytest
from scripts.ml import heights_source as hs
from scripts.ml.reference_agreement import lonlat_to_local as reference_lonlat_to_local
from shapely.geometry import box

# --- synthetic rasters -----------------------------------------------------------------------------------------

def north_up(origin_x=0., origin_y=40., cell=1.):
    """GDAL geotransform (c, a, b, f, d, e) -> matrix for a north-up grid whose row 0 is at origin_y."""
    return hs.gdal_to_matrix((cell, 0., origin_x, 0., -cell, origin_y))


def test_gdal_to_matrix_rows_run_south():
    matrix = north_up(100., 50., .5)
    x, y = (np.array([[0.5, 0.5, 1.]]) @ matrix)[0]
    assert (x, y) == (100.25, 49.75)
    _, y_lower = (np.array([[0.5, 10.5, 1.]]) @ matrix)[0]
    assert y_lower < y


def test_ndsm_subtracts_terrain_and_masks_nodata():
    dsm = np.array([[105., 108.], [-9999., 101.]])
    dem = np.array([[100., 100.], [100., 101.5]])
    out = hs.ndsm(dsm, dem, nodata=-9999.)
    assert out[0, 0] == 5. and out[0, 1] == 8.
    assert math.isnan(out[1, 0])
    assert out[1, 1] == pytest.approx(-.5)


def test_pixel_centres_in_counts_cells_inside_a_square():
    matrix = north_up(0., 40., 1.)
    rows, cols = hs.pixel_centres_in(box(10, 10, 20, 20), matrix, (40, 40))
    assert len(rows) == 100
    assert rows.min() == 20 and rows.max() == 29     # y 20..10 -> rows 20..29 on a north-up grid
    assert cols.min() == 10 and cols.max() == 19


def test_pixel_centres_outside_raster_are_empty():
    rows, cols = hs.pixel_centres_in(box(100, 100, 110, 110), north_up(), (40, 40))
    assert rows.size == 0 and cols.size == 0


def gable_ndsm(eave=2.6, ridge=4.6, bleed=0.):
    """40 x 40 m, 1 m cells: a 10 x 12 m gabled house (ridge along y) on flat ground; optional ground bleed on its edge cells."""
    grid = np.zeros((40, 40))
    matrix = north_up(0., 40., 1.)
    house = box(10, 10, 20, 22)
    rows, cols = hs.pixel_centres_in(house, matrix, grid.shape)
    xs = cols+.5
    grid[rows, cols] = eave+(ridge-eave)*(1-np.abs(xs-15.)/5.)
    if bleed is not None:
        edge_rows, edge_cols = hs.pixel_centres_in(house.difference(house.buffer(-1.)), matrix, grid.shape)
        grid[edge_rows, edge_cols] = bleed
    return grid, matrix, house


def test_top_half_median_and_quartile_eave():
    values = np.arange(1., 11.)                       # 1..10
    assert hs.top_half_median(values) == pytest.approx(8.)      # median of 6..10
    assert hs.top_half_median(values, top_fraction=1.) == pytest.approx(5.5)
    assert hs.top_half_median([]) is None
    stats = hs.sample_polygon(np.arange(16.).reshape(4, 4), hs.gdal_to_matrix((1., 0., 0., 0., -1., 4.)), box(0, 0, 4, 4), erode_m=0)
    assert stats['samples'] == 16
    assert stats['eave'] == pytest.approx(np.quantile(np.arange(16.), .25))
    assert stats['height'] == pytest.approx(np.median(np.arange(8., 16.)))


def test_erosion_removes_ground_bleed_on_the_outline():
    grid, matrix, house = gable_ndsm(bleed=0.)
    eroded = hs.sample_polygon(grid, matrix, house)
    raw = hs.sample_polygon(grid, matrix, house, erode_m=0., fallbacks=())
    assert eroded['erode_m'] == 1.
    assert eroded['samples'] == 8*10
    assert raw['samples'] == 120
    assert raw['eave'] == 0.                           # bleed cells drag the unmasked quartile to the ground
    assert eroded['eave'] > 2.6                        # interior cells only
    assert 3.9 < eroded['height'] <= 4.6               # top-half median sits between mid-slope and ridge


def test_ndsm_height_reports_roof_eave_and_uncertainty():
    grid, matrix, house = gable_ndsm(bleed=None)
    result = hs.ndsm_height(grid, matrix, house)
    assert result['samples'] == 80
    assert 3.9 < result['height_m'] <= 4.6
    assert 2.6 < result['eave_m'] < result['height_m']
    assert result['uncertainty_m'] >= hs.PARAMS['lidar_vertical_rmse_m']
    flat = np.full((40, 40), 2.6)
    flat_result = hs.ndsm_height(flat, matrix, house)
    assert flat_result['height_m'] == 2.6 and flat_result['uncertainty_m'] == pytest.approx(.1)


def test_small_container_falls_back_to_smaller_erosion():
    grid = np.zeros((40, 40))
    matrix = north_up(0., 20., .5)                    # 0.5 m cells, 20 x 20 m
    container = box(5, 5, 7.4, 11.1)                 # 2.4 x 6.1 m, a 20 ft container
    rows, cols = hs.pixel_centres_in(container, matrix, grid.shape)
    grid[rows, cols] = 2.59
    result = hs.sample_polygon(grid, matrix, container, min_pixels=10)
    assert result["erode_m"] == .5                    # 1 m erosion leaves a 0.4 m strip: 8 cells, fewer than 10
    assert result['height'] == pytest.approx(2.59)
    assert hs.sample_polygon(grid, matrix, box(5, 5, 5.2, 5.2)) is None


def test_nan_cells_are_ignored():
    grid, matrix, house = gable_ndsm(bleed=None)
    rows, cols = hs.pixel_centres_in(house.buffer(-1.), matrix, grid.shape)
    grid[rows[:10], cols[:10]] = np.nan
    assert hs.sample_polygon(grid, matrix, house)['samples'] == 70


# --- depth relief ----------------------------------------------------------------------------------------------

def test_depth_relief_uses_a_ground_ring_without_other_buildings_or_trees():
    grid = np.full((60, 60), .2)
    matrix = north_up(0., 60., 1.)
    house = box(20, 20, 32, 30)
    east, west, tree = box(33.6, 14, 40, 36), box(12, 14, 18.4, 36), box(18.4, 31.6, 33.6, 38)   # only the south strip is ground
    for shape, value in ((house, .6), (east, .9), (west, .9), (tree, .95)):
        rows, cols = hs.pixel_centres_in(shape, matrix, grid.shape)
        grid[rows, cols] = value
    occupied = house.union(east).union(west)
    relief = hs.depth_relief(grid, matrix, house, occupied, tree)
    assert relief['relief'] == pytest.approx(.4)
    assert relief['ground_samples'] > 30
    contaminated = hs.depth_relief(grid, matrix, house, None, None)
    assert contaminated['relief'] < relief['relief']


# --- fit -------------------------------------------------------------------------------------------------------

def test_fit_linear_recovers_scale_and_beats_baseline():
    rng = np.random.default_rng(0)
    x = rng.uniform(.05, .4, 200)
    y = 1.+20.*x+rng.normal(0, .3, 200)
    groups = [f's{i % 20}' for i in range(200)]
    fit = hs.fit_linear(x, y, groups)
    assert fit['ok'] and fit['n'] == 200 and fit['groups'] == 20
    assert fit['b'] == pytest.approx(20., abs=1.)
    assert fit['cv_mae_m'] < .5*fit['baseline_mae_m']
    assert fit['spearman'] > .9


def test_fit_linear_rejects_degenerate_inputs():
    assert not hs.fit_linear([.1, .1, .1], [3, 4, 5], ['a', 'b', 'c'])['ok']
    assert not hs.fit_linear([.1], [3], ['a'])['ok']


def measures_with(anchor_key, values, relief, scenes=12):
    out, refs = {}, {}
    for i, (h, r) in enumerate(zip(values, relief, strict=True)):
        scene = f'scene-{i % scenes}'
        refs[scene] = .2
        measure = {'depth': {'relief': r, 'relief_eave': r*.6, 'samples': 50, 'erode_m': 1.}, 'area_m2': 100.}
        if anchor_key == 'address_roofs_ml':
            measure['address_roofs_ml'] = {'height_m': h}
        elif anchor_key == 'ms_ml':
            measure['ms_ml'] = {'height_m': h}
        out[(scene, f'o{i}')] = measure
    return out, refs


def test_circular_anchor_is_reported_but_never_adopted():
    rng = np.random.default_rng(1)
    relief = rng.uniform(.05, .4, 120)
    measures, refs = measures_with('address_roofs_ml', 2.+15.*relief, relief)
    report = hs.fit_depth(measures, refs)
    assert report['fits']['address-roofs-ml:relief']['cv_mae_m'] < .01
    assert report['adopted'] is None


def test_independent_anchor_is_adopted_and_floor_values_are_censored():
    rng = np.random.default_rng(2)
    relief = rng.uniform(.05, .4, 120)
    heights = 3.5+15.*relief+rng.normal(0, .2, 120)
    heights[:20] = hs.MS_FLOOR_M                    # floor values must not enter the fit
    measures, refs = measures_with('ms_ml', heights, relief)
    report = hs.fit_depth(measures, refs)
    assert report['adopted']['anchor'] == 'ms-ml'
    assert report['adopted']['n'] == 100
    height, eave = hs.depth_to_metres(measures[('scene-0', 'o24')]['depth'], report['adopted'], .2)
    assert eave <= height


def test_uncorrelated_anchor_is_not_adopted():
    rng = np.random.default_rng(3)
    relief = rng.uniform(.05, .4, 120)
    measures, refs = measures_with('ms_ml', rng.uniform(3.5, 8., 120), relief)
    assert hs.fit_depth(measures, refs)['adopted'] is None


# --- source choice ---------------------------------------------------------------------------------------------

PROVENANCE = {'lidar': {'method': 'l'}, 'depth': {'method': 'd'}, 'osm': {'dataset': 'o'}, 'prior': {'method': 'p'}}
FIT = {'anchor': 'ms-ml', 'feature': 'relief', 'a': 1., 'b': 10., 'cv_rmse_m': .9}


def test_choose_prefers_lidar_then_depth_then_osm_then_prior():
    depth = {'relief': .3, 'relief_eave': .15, 'samples': 40, 'erode_m': 1.}
    lidar = {'height_m': 4.4, 'eave_m': 2.7, 'uncertainty_m': .12, 'samples': 80, 'erode_m': 1., 'capture_date': '2017-11-28'}
    osm = {'id': 'way/1', 'height_m': 6., 'method': 'height tag', 'iou': .8}
    assert hs.choose({'lidar': lidar, 'depth': depth, 'osm': osm}, FIT, .2, PROVENANCE)[0]['source'] == 'lidar-ndsm'
    primary, checks = hs.choose({'depth': depth, 'osm': osm}, FIT, .2, PROVENANCE)
    assert primary['source'] == 'depth-model' and primary['height_m'] == pytest.approx(4.)
    assert primary['eave_m'] == pytest.approx(2.5) and primary['uncertainty_m'] == .9
    assert checks['osm']['height_m'] == 6. and checks['prior']['height_m'] == 3.2
    assert hs.choose({'depth': depth, 'osm': osm}, None, .2, PROVENANCE)[0]['source'] == 'osm'
    prior, checks = hs.choose({'depth': depth}, None, .2, PROVENANCE)
    assert prior['source'] == 'prior' and prior['height_m'] == 3.2 and prior['eave_m'] is None
    assert checks['depth_model']['height_m'] is None and checks['depth_model']['relief'] == .3


# --- external sources ------------------------------------------------------------------------------------------

def test_parse_height_tag_and_levels():
    assert hs.parse_height_tag('7') == 7. and hs.parse_height_tag('7.5 m') == 7.5 and hs.parse_height_tag('6,5') == 6.5
    assert hs.parse_height_tag("23'") is None and hs.parse_height_tag(None) is None
    payload = {'elements': [
        {'type': 'way', 'id': 1, 'tags': {'building': 'yes', 'height': '9 m'}, 'geometry': [{'lon': 0, 'lat': 0}, {'lon': 1, 'lat': 0}, {'lon': 1, 'lat': 1}, {'lon': 0, 'lat': 0}]},
        {'type': 'way', 'id': 2, 'tags': {'building': 'house', 'building:levels': '2'}, 'geometry': [{'lon': 0, 'lat': 0}, {'lon': 1, 'lat': 0}, {'lon': 1, 'lat': 1}, {'lon': 0, 'lat': 0}]},
        {'type': 'way', 'id': 3, 'tags': {'building': 'yes'}, 'geometry': [{'lon': 0, 'lat': 0}, {'lon': 1, 'lat': 0}]}]}
    items = {i['id']: i for i in hs.osm_buildings(payload)}
    assert items['way/1']['height_m'] == 9. and items['way/1']['method'] == 'height tag'
    assert items['way/2']['height_m'] == 6. and 'levels' in items['way/2']['method']
    assert 'way/3' not in items


def fake_scene():
    return {'id': 'mooroolbark-50011498-17-1-2', 'centroid': {'lon': 145.32, 'lat': -37.78}, 'image_width': 100, 'image_height': 100,
            'pixel_to_meters': [[.25, 0], [0, -.25], [-12.5, 12.5]], 'owned_object_ids': ['a', 'b'], 'model_signature': 'sig',
            'objects': [{'id': 'a', 'kind': 'building', 'ring': [[10, 10], [50, 10], [50, 40], [10, 40]]},
                        {'id': 'b', 'kind': 'building', 'ring': [[60, 60], [70, 60], [70, 84], [60, 84]]},
                        {'id': 'c', 'kind': 'building', 'status': 'superseded', 'ring': [[0, 0], [5, 0], [5, 5]]},
                        {'id': 't', 'kind': 'tree', 'ring': [[80, 10], [95, 10], [95, 25], [80, 25]]}]}


def test_vectorised_projection_matches_reference_agreement():
    scene = fake_scene()
    points = [(145.3205, -37.7795), (145.3190, -37.7812)]
    expected = reference_lonlat_to_local(points, scene)
    x, y = hs.lonlat_to_local([p[0] for p in points], [p[1] for p in points], scene)
    assert np.allclose(np.c_[x, y], expected, atol=1e-6)
    lon, lat = hs.local_to_lonlat(x, y, scene)
    assert np.allclose(np.c_[lon, lat], points, atol=1e-9)


def test_match_external_by_iou_in_scene_metres():
    scene = fake_scene()
    buildings, occupied, trees = hs.scene_buildings(scene)
    assert set(buildings) == {'a', 'b'} and trees is not None
    ring = list(buildings['a']['lonlat'].exterior.coords)
    shifted = [(x+2e-6, y) for x, y in ring]
    far = [(x+.01, y) for x, y in ring]
    index = hs.LonLatIndex([{'id': 'ms/1', 'rings': [shifted], 'height_m': 4.2}, {'id': 'ms/2', 'rings': [far], 'height_m': 9.}])
    matches = hs.match_external(index, scene, buildings, .3)
    assert set(matches) == {'a'}
    assert matches['a'][0]['id'] == 'ms/1' and matches['a'][1] > .9


def test_parse_atl03_and_icesat_heights_on_synthetic_photons():
    scene = fake_scene()
    buildings, occupied, trees = hs.scene_buildings(scene)
    house = buildings['a']['local']
    rng = np.random.default_rng(4)
    xs = np.linspace(house.bounds[0]-10, house.bounds[2]+10, 200)
    ys = np.full_like(xs, house.centroid.y)
    heights = np.where([house.buffer(-.01).contains(__import__('shapely').geometry.Point(x, y)) for x, y in zip(xs, ys, strict=True)], 104.5, 100.)
    heights = heights+rng.normal(0, .05, len(xs))
    lon, lat = hs.local_to_lonlat(xs, ys, scene)
    body = json.dumps([{'beam_name': 'gt2l', 'series': [
        {'name': 'High', 'photon_count': len(xs), 'data': [[la, lo, h] for la, lo, h in zip(lat, lon, heights, strict=True)]},
        {'name': 'Noise', 'photon_count': 1, 'data': [[lat[0], lon[0], 400.]]}]}]).encode()
    beams = hs.parse_atl03(body)
    assert list(beams) == ['gt2l'] and beams['gt2l'].shape == (200, 4)
    result = hs.icesat_heights({'2023-02-19/rgt939': beams}, scene, buildings, occupied, trees)
    assert result['a']['height_m'] == pytest.approx(4.5, abs=.15)
    assert result['a']['roof_photons'] >= 3 and 'b' not in result


# --- cache manifest and offline mode ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self.content = body
        self.text = body.decode(errors='replace')


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.headers = {}

    def _answer(self, url):
        self.calls.append(url)
        for prefix, (status, body) in self.routes.items():
            if url.startswith(prefix):
                return FakeResponse(status, body)
        return FakeResponse(404, b'not found')

    def get(self, url, headers=None, timeout=None, **_):
        return self._answer(url)

    def post(self, url, data=None, headers=None, timeout=None):
        return self._answer(url)


class Exploding:
    headers = {}

    def get(self, *a, **k):
        raise AssertionError('offline mode opened a socket')

    post = get


def test_cache_records_sha256_licence_and_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(hs.time, 'sleep', lambda *_: None)
    session = FakeSession({'https://ok.example/': (200, b'{"a": 1}'), 'https://gone.example/': (403, b'denied')})
    cache = hs.Cache(tmp_path, session=session)
    body = cache.get('https://ok.example/data', params={'b': 2, 'a': 'x y'}, source='test', licence='CC BY 4.0', capture_date='2017-11-28')
    assert body == b'{"a": 1}'
    entry = cache.entry('https://ok.example/data', {'a': 'x y', 'b': 2})
    assert entry['status'] == 'ok' and entry['sha256'] == hs.sha256_bytes(body) and entry['bytes'] == 8
    assert entry['licence'] == 'CC BY 4.0' and entry['capture_date'] == '2017-11-28' and entry['url'].endswith('?a=x%20y&b=2')
    assert (tmp_path/hs.CACHE_DIR/entry['path']).read_bytes() == body
    assert cache.get('https://gone.example/tile.tif', source='test', licence='x', ext='bin') is None
    failed = cache.entry('https://gone.example/tile.tif')
    assert failed['status'] == 'error' and failed['http_status'] == 403
    calls = len(session.calls)
    cache.get('https://ok.example/data', params={'a': 'x y', 'b': 2}, source='test', licence='CC BY 4.0')
    assert len(session.calls) == calls                 # served from cache
    manifest = json.loads((tmp_path/hs.CACHE_DIR/'manifest.json').read_text())
    assert manifest['version'] == hs.CACHE_VERSION and len(manifest['entries']) == 2


def test_offline_mode_replays_and_never_opens_a_socket(tmp_path, monkeypatch):
    monkeypatch.setattr(hs.time, 'sleep', lambda *_: None)
    online = hs.Cache(tmp_path, session=FakeSession({'https://ok.example/': (200, b'payload'), 'https://gone.example/': (500, b'x')}))
    online.get('https://ok.example/a', source='s', licence='l')
    online.get('https://gone.example/b', source='s', licence='l')
    offline = hs.Cache(tmp_path, offline=True, session=Exploding())
    assert offline.get('https://ok.example/a', source='s', licence='l') == b'payload'
    assert offline.get('https://gone.example/b', source='s', licence='l') is None
    with pytest.raises(hs.CacheMiss):
        offline.get('https://never.example/c', source='s', licence='l')
    entry = offline.entry('https://ok.example/a')
    (tmp_path/hs.CACHE_DIR/entry['path']).write_bytes(b'tampered')
    with pytest.raises(hs.CacheMiss):
        hs.Cache(tmp_path, offline=True, session=Exploding()).get('https://ok.example/a', source='s', licence='l')


def elvis_payload():
    return json.dumps({'available_data': [
        {'source': 'VIC Government', 'downloadables': {'Digital Elevation Models': {'10 Metre': [
            {'file_name': 'Vicmap Elevation DEM 10m', 'file_url': 'https://vic-elvis.example/dem10m.tif'}]}}},
        {'source': 'NSW Government - Spatial Services'}]}).encode()


def footprint_payload():
    return json.dumps({'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'properties': {
        'filename': 'melbourne_2017nov28_dem1m', 'source_date': '2017-11-28T00:00:00Z', 'source_resolution': '1m',
        'vertical_accuracy': '0.1m', 'availability': 'Licensed', 'cip_code': '01-2017-18'},
        'geometry': {'type': 'Polygon', 'coordinates': [[[145, -38], [146, -38], [146, -37], [145, -37], [145, -38]]]}}]}).encode()


def test_fetch_then_from_cache_gives_the_same_report(tmp_path, monkeypatch):
    monkeypatch.setattr(hs.time, 'sleep', lambda *_: None)
    session = FakeSession({
        hs.URLS['elvis_downloadables']: (200, elvis_payload()),
        'https://vic-elvis.example/': (403, b'AccessDenied'),
        hs.URLS['vic_wfs']: (200, footprint_payload()),
        hs.URLS['overpass'][0]: (504, b'timeout'),
        hs.URLS['overpass'][1]: (200, json.dumps({'elements': [], 'osm3s': {'timestamp_osm_base': 't'}}).encode()),
    })
    online = hs.fetch(tmp_path, icesat=False, ms=False, session=session, log=lambda *_: None)
    assert online['elvis']['ok'] and online['elvis']['lidar_grade'] == []
    assert online['raster_probes'][0]['http_status'] == 403
    assert online['vic_lidar_footprints']['collections'][0]['availability'] == 'Licensed'
    assert online['vic_lidar_footprints']['collections'][0]['bbox_cover_share'] == 1.
    assert online['osm']['endpoint'] == hs.URLS['overpass'][1]
    offline = hs.fetch(tmp_path, offline=True, icesat=False, ms=False, session=Exploding(), log=lambda *_: None)
    for key in ('elvis', 'raster_probes', 'vic_lidar_footprints', 'vic_buildings', 'osm'):
        assert offline[key] == online[key], key
    assert hs.fetch(tmp_path/'empty', offline=True, icesat=False, ms=False, session=Exploding(), log=lambda *_: None)['elvis']['ok'] is False


def test_elvis_flags_lidar_grade_products(tmp_path):
    payload = json.dumps({'available_data': [{'source': 'VIC Government', 'downloadables': {
        'Point Clouds': {'AHD': [{'file_name': 'x.laz', 'file_url': 'https://e/x.laz'}]},
        'Digital Elevation Models': {'1 Metre': [{'file_name': 'dem1m', 'file_url': 'https://e/d.tif'}],
                                     '5 Metre': [{'file_name': 'dem5m', 'file_url': 'https://e/d5.tif'}]}}}]}).encode()
    cache = hs.Cache(tmp_path, session=FakeSession({hs.URLS['elvis_downloadables']: (200, payload)}))
    result = hs.fetch_elvis(cache)
    assert {r['name'] for r in result['lidar_grade']} == {'x.laz', 'dem1m'}


def test_osm_api_fallback_splits_on_the_node_limit_and_replays_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(hs.time, 'sleep', lambda *_: None)
    whole = f"{hs.URLS['osm_api_map']}?bbox={','.join(str(round(v, 6)) for v in hs.BBOX)}"
    quads = hs.osm_api_quadrants(hs.BBOX)

    def tile(i, quad):
        w, s, e, n = quad
        cx, cy = (w+e)/2, (s+n)/2
        nodes = [{'type': 'node', 'id': i*10+k, 'lat': cy+dy, 'lon': cx+dx}
                 for k, (dx, dy) in enumerate([(0, 0), (1e-4, 0), (1e-4, 1e-4), (0, 1e-4)])]
        way = {'type': 'way', 'id': 100+i, 'nodes': [i*10, i*10+1, i*10+2, i*10+3, i*10], 'timestamp': f'2026-0{i+1}-01T00:00:00Z',
               'tags': {'building': 'yes', 'height': '4'} if i == 0 else {'building': 'garage'}}
        road = {'type': 'way', 'id': 900+i, 'nodes': [i*10, i*10+1], 'tags': {'highway': 'residential'}}
        return json.dumps({'elements': nodes+[way, road]}).encode()

    routes = {whole: (400, b'You requested too many nodes (limit is 50000).')}
    for i, quad in enumerate(quads):
        routes[f"{hs.URLS['osm_api_map']}?bbox={','.join(str(round(v, 6)) for v in quad)}"] = (200, tile(i, quad))
    for endpoint in hs.URLS['overpass']:
        routes[endpoint] = (504, b'gateway timeout')
    session = FakeSession(routes)
    result = hs.fetch_osm(hs.Cache(tmp_path, session=session))
    assert result['ok'] and result['endpoint'] == hs.URLS['osm_api_map'] and result['elements'] == 4 and len(result['fallback']) == 5
    payload = hs.load_osm(hs.Cache(tmp_path, offline=True, session=Exploding()))
    items = hs.osm_buildings(payload)
    assert len(items) == 4 and sum(1 for i in items if i['height_m'] == 4.) == 1


def test_assign_writes_heights_rows_and_summary(tmp_path, monkeypatch):
    from scripts.ml import eyes_index
    monkeypatch.setattr(hs.time, 'sleep', lambda *_: None)
    scene = fake_scene()
    folder = tmp_path/'data/scenes'/scene['id']
    folder.mkdir(parents=True)
    (folder/'latest.json').write_text(json.dumps(scene))
    buildings, _, _ = hs.scene_buildings(scene)
    ring = [{'lon': x, 'lat': y} for x, y in buildings['b']['lonlat'].exterior.coords]
    overpass = json.dumps({'elements': [{'type': 'way', 'id': 7, 'tags': {'building': 'house', 'height': '7'}, 'geometry': ring}]}).encode()
    hs.fetch_osm(hs.Cache(tmp_path, session=FakeSession({hs.URLS['overpass'][0]: (200, overpass)})))
    rows, fit, statuses = hs.assign(tmp_path, [scene['id']], depth=False, refit=False, log=lambda *_: None)
    by_id = {r['object_id']: r['payload'] for r in rows}
    assert set(by_id) == {'a', 'b'}
    assert by_id['b']['source'] == 'osm' and by_id['b']['height_m'] == 7. and by_id['b']['provenance']['element'] == 'way/7'
    assert by_id['a']['source'] == 'prior' and by_id['a']['height_m'] == 3.2 and by_id['a']['eave_m'] is None
    for payload in by_id.values():
        assert {'source', 'height_m', 'eave_m', 'uncertainty_m', 'samples', 'capture_date', 'provenance'} <= set(payload)
    with eyes_index.connection(tmp_path, readonly=True) as db:
        stored = eyes_index.rows(db, 'object_quality', version=hs.VERSION)
    assert len(stored) == 2 and stored[0]['model_signature'] == 'sig'
    summary = hs.summarise(tmp_path, fit_report=fit, statuses=statuses, sheet=False)
    assert summary['population'] == {'suburb_scenes': 1, 'owned_roofs': 2, 'rows_owned': 2, 'rows_all': 2}
    assert summary['coverage_primary_source']['osm'] == {'objects': 1, 'of': 2}
    assert summary['coverage_primary_source']['lidar-ndsm'] == {'objects': 0, 'of': 2}
    assert summary['lidar']['objects_with_lidar_height'] == 0
    assert (tmp_path/hs.REPORT_DIR/'summary.json').exists()
    rerun, _, _ = hs.assign(tmp_path, [scene['id']], depth=False, refit=False, log=lambda *_: None)
    with eyes_index.connection(tmp_path, readonly=True) as db:
        assert len(eyes_index.rows(db, 'object_quality', version=hs.VERSION)) == 2   # replaced, not duplicated


def test_icesat_registration_recovers_a_known_offset(tmp_path, monkeypatch):
    monkeypatch.setitem(hs.PARAMS, 'icesat_register_radius_m', 6.)
    scene = {'id': 'mooroolbark-50011498-17-9-9', 'centroid': {'lon': 145.32, 'lat': -37.78}, 'image_width': 1152, 'image_height': 1152,
             'pixel_to_meters': [[.25, 0], [0, -.25], [-144., 144.]], 'model_signature': 'sig', 'objects': []}
    houses = []
    for i in range(6):
        for j in range(6):
            x0, y0 = -120+i*40, -120+j*40            # metres; 10 x 12 m houses on a 40 m grid
            ring_m = [(x0, y0), (x0+10, y0), (x0+10, y0+12), (x0, y0+12)]
            ring_px = [((x+144)/.25, (144-y)/.25) for x, y in ring_m]
            scene['objects'].append({'id': f'h{i}{j}', 'kind': 'building', 'ring': ring_px})
            houses.append(box(x0, y0, x0+10, y0+12))
    scene['owned_object_ids'] = [o['id'] for o in scene['objects']]
    folder = tmp_path/'data/scenes'/scene['id']
    folder.mkdir(parents=True)
    (folder/'latest.json').write_text(json.dumps(scene))
    xs, ys = [], []
    for i in range(6):
        for offset in (2., 5., 8.):                    # three beams per column of houses
            track_y = np.arange(-135, 135, .7)
            xs.append(np.full_like(track_y, -120+i*40+offset))
            ys.append(track_y)
    xs, ys = np.concatenate(xs), np.concatenate(ys)
    from shapely import contains_xy
    on_roof = np.zeros(len(xs), bool)
    for house in houses:
        on_roof |= contains_xy(house, xs, ys)
    heights = np.where(on_roof, 104.5, 100.)
    lon, lat = hs.local_to_lonlat(xs+2., ys+4., scene)   # recorded positions are 2 m east and 4 m north of the truth
    photons = np.c_[lat, lon, heights, np.full(len(xs), 3.)]
    shifts = hs.register_icesat(tmp_path, [scene['id']], {'p1': {'gt1l': photons}})
    assert shifts['p1']['accepted'], shifts
    assert shifts['p1']['dy_m'] == pytest.approx(-4., abs=1.)          # along-track error mixes roof and ground photons
    assert shifts['p1']['dx_m'] == pytest.approx(-2., abs=2.)          # cross-track is flat while beams stay on the roof
    assert shifts['p1']['contrast_b_m'] > shifts['p1']['contrast_b_unshifted_m']
    buildings, occupied, trees = hs.scene_buildings(scene)
    registered = hs.icesat_heights({'p1': {'gt1l': photons}}, scene, buildings, occupied, trees, shifts=shifts)
    assert np.median([v['height_m'] for v in registered.values()]) == pytest.approx(4.5, abs=.2)


# --- round 3: a second footprint cache around the inner-Melbourne judge tiles ---------------------------------------

def test_feature_in_bbox_with_the_inner_melbourne_bbox_and_judge_tiles_bbox(tmp_path):
    inner = (144.970916, -37.800453, 144.988496, -37.788732)
    feature = {'geometry': {'type': 'Polygon', 'coordinates': [[[144.98, -37.795], [144.9801, -37.795], [144.9801, -37.7951], [144.98, -37.795]]]}}
    assert hs._feature_in_bbox(feature, inner) is True and hs._feature_in_bbox(feature, hs.BBOX) is False
    root = tmp_path
    ds = root / 'data/reviews/eyes/training/dataset-v2'
    ds.mkdir(parents=True)
    json.dump({'images': [{'id': 1, 'scene_id': 'moor-a'}, {'id': 2, 'scene_id': 'map-b'}, {'id': 3, 'scene_id': 'roof_training_250-c'}]}, open(ds / 'test.json', 'w'))
    for sid, bounds in (('moor-a', [145.30, -37.79, 145.302, -37.788]), ('map-b', [144.980, -37.796, 144.982, -37.794]), ('roof_training_250-c', None)):
        (root / 'data/scenes' / sid).mkdir(parents=True)
        json.dump({'region': {'bounds': bounds} if bounds else {}}, open(root / 'data/scenes' / sid / 'latest.json', 'w'))
    bbox, scenes = hs.judge_tiles_bbox(root, datasets=('data/reviews/eyes/training/dataset-v2',), pad_m=200.)
    assert scenes == ['map-b']
    assert bbox[0] < 144.980 and bbox[2] > 144.982 and bbox[1] < -37.796 and bbox[3] > -37.794
    assert abs((bbox[3] - bbox[1]) - (0.002 + 2 * 200 / 111320.)) < 1e-6
    assert hs.judge_tiles_bbox(root, datasets=('data/reviews/eyes/training/absent',)) == (None, [])
    with pytest.raises(ValueError):
        hs.fetch_ms_buildings(hs.Cache(root, offline=True), bbox=inner)      # another bbox never overwrites the Mooroolbark file
