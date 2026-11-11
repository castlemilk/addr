"""imagery_sources: provider registry, tile cache manifests, reprojection math, resolution measures, offline fetch."""
import hashlib
import io
import json

import cv2
import numpy as np
import pytest
from PIL import Image
from scripts.ml import imagery_sources as isrc
from scripts.ml.tiles import tile_xy_to_lonlat
from shapely.geometry import Polygon

X19, Y19 = 473770*256+40, 321668*256+40      # a z19 global pixel near 27 Cambridge Road


def _png(array):
    out = io.BytesIO()
    Image.fromarray(np.asarray(array, np.uint8)).save(out, format='PNG')
    return out.getvalue()


class FakeResponse:
    def __init__(self, status, content=b'', content_type='image/png'):
        self.status_code, self.content, self.headers = status, content, {'Content-Type': content_type}


class FakeSession:
    """Serves 256 px tiles cut from world(z, gx0, gy0) or a fixed status for listed tiles; counts calls."""

    def __init__(self, world, status=None):
        self.world, self.status, self.calls = world, status or {}, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        z, x, y = (int(v.split('.')[0].split('?')[0]) for v in url.rsplit('/', 3)[1:])
        if (z, x, y) in self.status:
            code = self.status[(z, x, y)]
            return code if isinstance(code, FakeResponse) else FakeResponse(code)
        return FakeResponse(200, _png(self.world(z, x*256, y*256)))


class NoNetwork:
    def get(self, *args, **kwargs):
        raise AssertionError('offline mode must not touch the network')


def coded_world(z, gx0, gy0, size=256):
    """Every pixel encodes its own global coordinates, so a crop can be checked exactly."""
    gx = np.arange(gx0, gx0+size)[None, :].repeat(size, 0)
    gy = np.arange(gy0, gy0+size)[:, None].repeat(size, 1)
    return np.stack([gx % 251, gy % 241, (gx//251+gy//241) % 256], axis=-1).astype(np.uint8)


def smooth_world(z, gx0, gy0, size=256):
    gx = np.arange(gx0, gx0+size, dtype=float)[None, :]
    gy = np.arange(gy0, gy0+size, dtype=float)[:, None]
    value = 128+60*np.sin(2*np.pi*gx/97.)+50*np.cos(2*np.pi*gy/131.)
    return np.repeat(np.clip(value, 0, 255)[..., None], 3, axis=-1).astype(np.uint8)


def smooth_value(gx, gy):
    return 128+60*np.sin(2*np.pi*gx/97.)+50*np.cos(2*np.pi*gy/131.)


TEST = isrc.Provider('test-provider', 'synthetic', 'free-endpoint', 'https://tiles.test/{z}/{x}/{y}.png', 21, 'synthetic',
                     '-', 'test', '$0', ext='png', recommended_zoom=20)


def tile_scene(x19, y19, width, height, *, frac=(0., 0.), span=None):
    """A scene frame on (or offset from) the z19 pixel grid, built from tile coordinates like scene_suburb/scene_region."""
    span = span or (width, height)
    west, north = tile_xy_to_lonlat((x19+frac[0])/256, (y19+frac[1])/256, 19)
    east, south = tile_xy_to_lonlat((x19+frac[0]+span[0])/256, (y19+frac[1]+span[1])/256, 19)
    lat = (north+south)/2
    mpp = isrc.zoom_mpp(lat, 19)*span[0]/width
    return {'schema_version': 1, 'id': 'synthetic', 'image_width': width, 'image_height': height,
            'centroid': {'lon': (west+east)/2, 'lat': lat},
            'pixel_to_meters': [[mpp, 0], [0, -mpp], [-width*mpp/2, height*mpp/2]],
            'meters_to_pixel': [[1/mpp, 0], [0, -1/mpp], [width/2, height/2]],
            'region': {'provider': 'google', 'imagery_zoom': 19, 'bounds': [west, south, east, north]}, 'objects': []}


# --- registry ---------------------------------------------------------------------------------------------------

def test_registry_entries_are_complete():
    statuses = {'free-endpoint', 'licence-required', 'key-required', 'no-coverage', 'not-imagery'}
    for name, p in isrc.PROVIDERS.items():
        assert p.name == name and p.status in statuses
        assert p.title and p.native_gsd and p.licence and p.cost and p.dates
        if p.fetchable:
            assert all(token in p.template for token in ('{z}', '{x}', '{y}'))
        if p.status in ('key-required', 'licence-required'):
            assert p.how_to_get, name
        if p.key_env:
            assert p.template is None or '{secret}' in p.template or p.status == 'licence-required', name
    free = [p for p in isrc.PROVIDERS.values() if p.status == 'free-endpoint']
    assert {p.name for p in free} >= {'google', 'esri', 'esri-wayback'}
    assert all(p.key_env is None for p in free)
    assert isrc.PROVIDERS['google'].served_max_zoom >= 21 and isrc.PROVIDERS['esri'].served_max_zoom == 20


def test_gated_providers_refuse_without_their_variable(monkeypatch, tmp_path):
    monkeypatch.delenv('MAPBOX_ACCESS_TOKEN', raising=False)
    with pytest.raises(isrc.AccessRequired) as error:
        isrc.TileCache('mapbox-satellite', tmp_path)
    assert 'MAPBOX_ACCESS_TOKEN' in str(error.value) and 'USD' in str(error.value)
    monkeypatch.delenv('VICMAP_BASEMAPS_LICENCE', raising=False)
    with pytest.raises(isrc.AccessRequired, match='licen'):
        isrc.TileCache('vicmap-aerial', tmp_path)
    with pytest.raises(isrc.AccessRequired, match='no tile endpoint'):
        isrc.PROVIDERS['metromap'].require_access()


def test_secret_is_used_in_requests_but_never_stored(monkeypatch, tmp_path):
    monkeypatch.setenv('MAPBOX_ACCESS_TOKEN', 'sekret-123')
    session = FakeSession(coded_world)
    cache = isrc.TileCache('mapbox-satellite', tmp_path, session=session)
    cache.provider = isrc.Provider(**{**cache.provider.__dict__, 'ext': 'png'})
    assert cache.tile(20, 5, 7) is not None
    cache.save()
    assert 'sekret-123' in session.calls[0]
    stored = cache.manifest_path(20).read_text()
    assert 'sekret-123' not in stored and '${MAPBOX_ACCESS_TOKEN}' in stored


def test_list_prints_every_provider(capsys):
    assert isrc.main(['--list']) == 0
    out = capsys.readouterr().out
    assert all(name in out for name in isrc.PROVIDERS)
    assert isrc.main(['--list', '--json']) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r['provider'] for r in rows} == set(isrc.PROVIDERS)
    assert all('key_needed' in r and 'cost' in r for r in rows)


# --- cache manifest -----------------------------------------------------------------------------------------------

def test_cache_writes_sha256_manifest_and_hits_without_network(tmp_path):
    session = FakeSession(coded_world)
    cache = isrc.TileCache(TEST, tmp_path, session=session)
    tile = cache.tile(21, 10, 20)
    assert tile.shape == (256, 256, 3) and len(session.calls) == 1
    cache.save()
    path = cache.tile_path(21, 10, 20)
    record = json.loads(cache.manifest_path(21).read_text())['tiles']['10_20']
    assert record['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert record['status'] == 'ok' and record['source'] == 'fetched' and record['http_status'] == 200
    again = isrc.TileCache(TEST, tmp_path, session=NoNetwork(), offline=True)
    assert np.array_equal(again.tile(21, 10, 20), tile) and again.stats['cache_hits'] == 1


def test_corrupt_tile_is_refused_offline_and_refetched_online(tmp_path):
    session = FakeSession(coded_world)
    cache = isrc.TileCache(TEST, tmp_path, session=session)
    cache.tile(21, 1, 2)
    cache.save()
    cache.tile_path(21, 1, 2).write_bytes(_png(np.zeros((256, 256, 3))))
    with pytest.raises(isrc.CacheIntegrityError):
        isrc.TileCache(TEST, tmp_path, offline=True).tile(21, 1, 2)
    online = isrc.TileCache(TEST, tmp_path, session=session)
    fixed = online.tile(21, 1, 2)
    assert online.stats['refetched_corrupt'] == 1 and len(session.calls) == 2
    assert np.array_equal(fixed, coded_world(21, 256, 512))


def test_existing_cache_files_are_adopted_and_hashed(tmp_path):
    cache = isrc.TileCache(TEST, tmp_path, session=NoNetwork())
    path = cache.tile_path(20, 3, 4)
    path.parent.mkdir(parents=True)
    path.write_bytes(_png(coded_world(20, 768, 1024)))
    assert cache.tile(20, 3, 4) is not None and cache.stats['adopted'] == 1
    record = cache.record(20, 3, 4)
    assert record['source'].startswith('adopted') and record['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_missing_and_placeholder_tiles_are_recorded_not_stored(tmp_path):
    grey = np.full((256, 256, 3), 204, np.uint8)
    placeholder = _png(grey)
    provider = isrc.Provider(**{**TEST.__dict__, 'placeholder_sha256': (hashlib.sha256(placeholder).hexdigest(),)})
    session = FakeSession(coded_world, status={(21, 0, 0): 404, (21, 1, 0): FakeResponse(200, placeholder)})
    cache = isrc.TileCache(provider, tmp_path, session=session)
    assert cache.tile(21, 0, 0) is None and cache.tile(21, 1, 0) is None
    assert cache.record(21, 0, 0)['status'] == 'missing' and cache.record(21, 1, 0)['status'] == 'placeholder'
    assert not cache.tile_path(21, 0, 0).exists() and not cache.tile_path(21, 1, 0).exists()
    cache.save()
    isrc.TileCache(provider, tmp_path, session=session).tile(21, 0, 0)
    assert len(session.calls) == 2       # known-missing tiles are not requested again
    assert isrc.is_placeholder(isrc.PROVIDERS['esri'], b'x', np.full((256, 256, 3), 204, np.uint8))
    assert not isrc.is_placeholder(isrc.PROVIDERS['esri'], b'x', coded_world(20, 0, 0))


# --- reprojection ---------------------------------------------------------------------------------------------------

def test_tile_scene_reprojects_by_integer_crop_with_exact_pixels(tmp_path):
    scene = tile_scene(X19, Y19, 40, 30)
    assert isrc.choose_k(scene, 21) == 4 and all(abs(r-4) < 1e-6 for r in isrc.zoom_ratio(scene, 21))
    cache = isrc.TileCache(TEST, tmp_path, session=FakeSession(coded_world))
    rep = isrc.reproject(scene, cache, 21, box=(5, 6, 25, 18))
    assert rep['integer_crop'] and rep['k'] == 4 and rep['image'].shape == (48, 80, 3) and rep['valid'].all()
    expected = coded_world(21, 4*(X19+5), 4*(Y19+6), size=80)[:48]
    assert np.array_equal(rep['image'], expected)


def test_offset_frame_is_resampled_at_the_pixel_centres(tmp_path):
    scene = tile_scene(X19, Y19, 50, 50, frac=(.37, .81), span=(51.3, 51.3))
    k = isrc.choose_k(scene, 21)
    assert k == 4
    cache = isrc.TileCache(TEST, tmp_path, session=FakeSession(smooth_world))
    rep = isrc.reproject(scene, cache, 21)
    assert not rep['integer_crop']
    # independent expectation: hi-res pixel (a, b) centre sits at saved edge coordinate (a + .5)/k, i.e. at z19 pixel
    # X19 + frac + (a + .5)/k * span/width, i.e. at z21 index 4*that - .5
    a = np.arange(50*k)
    gx = 4*(X19+.37+(a+.5)/k*51.3/50)-.5
    gy = 4*(Y19+.81+(a+.5)/k*51.3/50)-.5
    expected = smooth_value(gx[None, :], gy[:, None])
    assert np.abs(rep['image'][..., 0].astype(float)-expected).max() < 2.5


def test_derived_frame_maps_ring_indices_like_the_image():
    scene = tile_scene(X19, Y19, 40, 40)
    box, k = (8, 4, 30, 36), 4
    frame = isrc.derived_frame(scene, box, k)
    p = np.array([[10., 20.], [8., 4.], [29.5, 35.25]])
    metres = np.c_[p, np.ones(len(p))] @ np.asarray(scene['pixel_to_meters'])
    hires = np.c_[metres, np.ones(len(p))] @ np.asarray(frame['meters_to_pixel'])
    assert np.allclose(hires, k*(p-[8, 4]+.5)-.5)
    assert np.allclose(isrc.ring_to_crop(p, box, k), hires)
    back = np.c_[hires, np.ones(len(p))] @ np.asarray(frame['pixel_to_meters'])
    assert np.allclose(back, metres)
    assert frame['image_width'] == 88 and frame['image_height'] == 128


# --- resolution measures -----------------------------------------------------------------------------------------------

def _texture(size=384, seed=3):
    rng = np.random.default_rng(seed)
    image = np.full((size, size), 110, np.float32)
    for _ in range(260):
        x, y = rng.integers(0, size, 2)
        w, h = rng.integers(4, 40, 2)
        cv2.rectangle(image, (int(x), int(y)), (int(x+w), int(y+h)), float(rng.integers(20, 240)), -1)
    return cv2.GaussianBlur(image, (0, 0), .7)


def test_upsampled_images_lose_the_top_octave_and_keep_the_coarse_mtf():
    native = _texture()
    coarse = cv2.resize(native, (192, 192), interpolation=cv2.INTER_AREA)
    upsampled = cv2.resize(coarse, (384, 384), interpolation=cv2.INTER_CUBIC)
    a, b = isrc.spectral_octaves(native), isrc.spectral_octaves(upsampled)
    assert b['top_octave_ratio'] < .35*a['top_octave_ratio']
    mpp = .06
    e_native = isrc.edge_spread(native, mpp)
    e_up = isrc.edge_spread(upsampled, mpp)
    e_coarse = isrc.edge_spread(coarse, 2*mpp)
    assert e_native['edges_used'] >= 20 and e_up['edges_used'] >= 20
    assert e_up['edge_width_10_90_m'] > 1.2*e_native['edge_width_10_90_m']
    assert abs(e_up['edge_width_10_90_m']-e_coarse['edge_width_10_90_m']) < .25*e_coarse['edge_width_10_90_m']
    assert e_up['smallest_resolvable_bar_m'] > e_native['smallest_resolvable_bar_m']
    detail = isrc.cross_zoom(upsampled, coarse)
    real = isrc.cross_zoom(native, coarse)
    assert detail['ncc_down_vs_lo'] > .99 and detail['own_detail_fraction'] < .3*real['own_detail_fraction']
    assert detail['unexplained_fraction'] < .02
    assert isrc.gradient_energy(native, mpp)['per_m2'] > isrc.gradient_energy(upsampled, mpp)['per_m2']


def test_edge_width_matches_a_known_gaussian_blur():
    image = np.full((300, 300), 60, np.float32)
    for x0 in range(40, 260, 60):
        image[:, x0:x0+30] = 200
    sigma = 1.5
    blurred = cv2.GaussianBlur(image, (0, 0), sigma)
    result = isrc.edge_spread(blurred, .1)
    assert result['edge_width_10_90_m'] == pytest.approx(2.563*sigma*.1, rel=.2)


# --- paired arm: ribs below the saved Nyquist are only measurable at k=4 --------------------------------------------------

def test_hires_arm_measures_a_rib_period_the_saved_arm_cannot():
    from scripts.ml import structure_kinds as sk
    width = height = 120
    scene = tile_scene(X19, Y19, width, height)
    k = 4
    hires = np.full((height*k, width*k, 3), 90, np.uint8)
    ring = np.array([[40, 50], [80, 50], [80, 70], [40, 70]], float)
    lo, hi = isrc.ring_to_crop(ring[0], (0, 0, width, height), k), isrc.ring_to_crop(ring[2], (0, 0, width, height), k)
    mpp_hi = isrc.saved_mpp(scene)/k
    xs = np.arange(width*k)
    stripes = (170+60*np.sign(np.sin(2*np.pi*xs*mpp_hi/.30))).astype(np.uint8)
    hires[int(lo[1]):int(hi[1])+1, int(lo[0]):int(hi[0])+1] = stripes[int(lo[0]):int(hi[0])+1, None]
    saved = cv2.resize(hires, (width, height), interpolation=cv2.INTER_AREA)
    shape = Polygon(np.c_[ring, np.ones(4)] @ np.asarray(scene['pixel_to_meters']))
    geometry = sk.geometry_features(shape, vertex_count=4)
    box = (0, 0, width, height)
    a_saved = isrc.measure_arm(saved, scene, box, 1, shape, ring, geometry, [])
    a_hi = isrc.measure_arm(hires, scene, box, k, shape, ring, geometry, [])
    periods_hi = [a_hi['ribs_oriented'][f'period_{d}_m'] for d in ('along', 'across')]
    assert any(p and abs(p-.30) < .04 for p in periods_hi)
    periods_saved = [a_saved['ribs_oriented'][f'period_{d}_m'] for d in ('along', 'across')]
    assert not any(p and abs(p-.30) < .04 for p in periods_saved)
    assert a_hi['edge_support']['metric']['weighted_mean'] >= .5
    assert a_hi['edge_support']['metric_tolerance_m'] == pytest.approx(a_saved['edge_support']['native_tolerance_m'], rel=1e-3)


# --- offline scene fetch with fixtures --------------------------------------------------------------------------------------

def _textured_world(origin, size=1400, seed=11):
    rng = np.random.default_rng(seed)
    canvas = np.full((size, size), 120, np.float32)
    for _ in range(900):
        x, y = rng.integers(0, size, 2)
        w, h = rng.integers(6, 50, 2)
        cv2.rectangle(canvas, (int(x), int(y)), (int(x+w), int(y+h)), float(rng.integers(30, 230)), -1)
    canvas = cv2.GaussianBlur(canvas, (0, 0), .8)
    rgb = np.repeat(np.clip(canvas, 0, 255).astype(np.uint8)[..., None], 3, axis=-1)
    ox, oy = origin

    def world(z, gx0, gy0, size_=256):
        tile = np.full((size_, size_, 3), 120, np.uint8)
        x0, y0 = gx0-ox, gy0-oy
        xs, ys = slice(max(0, x0), min(size, x0+size_)), slice(max(0, y0), min(size, y0+size_))
        if xs.start < xs.stop and ys.start < ys.stop:
            tile[ys.start-y0:ys.stop-y0, xs.start-x0:xs.stop-x0] = rgb[ys, xs]
        return tile
    return world, rgb


def _write_scene(root, scene, image):
    png = _png(image)
    digest = hashlib.sha256(png).hexdigest()
    folder = root/'data/scenes'/scene['id']
    folder.mkdir(parents=True, exist_ok=True)
    (folder/f'{digest}.png').write_bytes(png)
    (folder/'latest.json').write_text(json.dumps({**scene, 'image_sha256': digest}))


@pytest.mark.parametrize('shift', [0, 2])
def test_fetch_scene_offline_from_fixtures_registers_on_the_saved_grid(tmp_path, monkeypatch, shift):
    monkeypatch.setitem(isrc.PROVIDERS, TEST.name, TEST)
    width = height = 480
    x20, y20 = 2*X19, 2*Y19
    world, rgb = _textured_world((x20-200, y20-200))
    # the saved PNG is the 2x2 box average of the z20 world, displaced by `shift` z20 px (shift/2 saved px) in x
    region = rgb[200:200+2*height, 200+shift:200+shift+2*width]
    saved = cv2.resize(region, (width, height), interpolation=cv2.INTER_AREA)
    scene = {**tile_scene(X19, Y19, width, height), 'id': f'synthetic-{shift}'}
    _write_scene(tmp_path, scene, saved)
    session = FakeSession(world)
    online, _ = isrc.fetch_scene(scene['id'], TEST.name, zoom=20, root=tmp_path, session=session)
    calls = len(session.calls)
    assert calls > 0 and online['k'] == 2 and online['integer_crop']
    report, image = isrc.fetch_scene(scene['id'], TEST.name, zoom=20, root=tmp_path, offline=True, session=NoNetwork())
    assert report['cache']['cache_hits'] == calls and report['valid_fraction'] == 1
    local = report['registration']['local']
    assert local['accepted'] and local['holdout_residual_px'] <= .5
    assert local['median_dx_px'] == pytest.approx(shift/2, abs=.25)
    assert report['alignment']['applied'] == (shift/2 >= isrc.MIN_APPLY_SHIFT_PX)
    folder = tmp_path/report['folder']
    assert report['assets']['hires.png']['sha256'] == hashlib.sha256((folder/'hires.png').read_bytes()).hexdigest()
    assert json.loads((folder/'report.json').read_text())['tiles'] == report['tiles']
    down = isrc.box_average(image, 2, (width, height)).astype(float)
    core = (slice(20, -20), slice(20, -20))
    assert np.abs(down[core]-saved[core]).mean() < 3.
    manifest = json.loads((tmp_path/report['tile_manifest']).read_text())
    assert all(len(r['sha256']) == 64 for r in manifest['tiles'].values())


def test_offline_fetch_without_cached_tiles_reports_the_gap(tmp_path, monkeypatch):
    monkeypatch.setitem(isrc.PROVIDERS, TEST.name, TEST)
    scene = {**tile_scene(X19, Y19, 256, 256), 'id': 'synthetic-empty'}
    _write_scene(tmp_path, scene, np.full((256, 256, 3), 100, np.uint8))
    report, _ = isrc.fetch_scene(scene['id'], TEST.name, zoom=20, root=tmp_path, offline=True, session=NoNetwork())
    assert report['valid_fraction'] == 0 and not report['aligned']
    assert report['cache']['offline_missing'] > 0 and 'valid.png' in report['assets']
