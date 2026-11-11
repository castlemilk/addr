"""lot-frame-v1: the lot-aware frame plan (dry run) and the capture path that writes only under data/scenes/lot-<id>/."""
import hashlib
import json
import math
import sqlite3
from contextlib import closing

import numpy as np
import pytest
from PIL import Image
from scripts.ml import scene_lot as L
from scripts.ml import scene_lot_tags as T
from scripts.ml.scene_addresses import WORLD
from scripts.ml.scene_batch import signature
from scripts.ml.scene_extract import save_scene
from scripts.ml.scene_region import capture_region

ADDRESS_ID, LON, LAT = 4242, 145.3125, -37.7905
TITLE = 'Unit UNIT 1 63 TEST ROAD, Mooroolbark VIC 3138'


def metre_lot(pfi, number, plan, x0, y0, x1, y1):
    unproject = T.inverse_projection(L._local_frame(LON, LAT))
    ring = [list(unproject(x, y)) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    return {'type': 'Feature', 'geometry': {'type': 'Polygon', 'coordinates': [ring+[ring[0]]]},
            'properties': {'pfi': pfi, 'spi': f'{number}\\{plan}', 'lot': number, 'plan': plan, 'status': 'approved'}}


@pytest.fixture
def root(tmp_path):
    (tmp_path/'data').mkdir()
    with closing(sqlite3.connect(tmp_path/'data/australia_geo.db')) as db:
        db.execute('CREATE TABLE addresses(id INTEGER PRIMARY KEY, full_address TEXT, lat REAL, lon REAL)')
        db.execute('INSERT INTO addresses VALUES(?, ?, ?, ?)', (ADDRESS_ID, TITLE, LAT, LON))
        db.commit()
    (tmp_path/'public').mkdir()
    ring = [[-5, -4], [5, -4], [5, 4], [-5, 4]]
    (tmp_path/'public/address-roofs.json').write_text(json.dumps([{
        'address_id': ADDRESS_ID, 'full_address': TITLE, 'boundary_source': 'crm', 'distance_meters': 2.5,
        'centroid': {'lon': LON, 'lat': LAT}, 'ring_meters': ring}]))
    return tmp_path


@pytest.fixture
def parcels():
    # Lot 1 (43.2 x 30 m) under the seed; Lot 2 and CM1 of the same plan touching it; Lot 9 of another plan nearby.
    features = [metre_lot('p1', '1', 'PS1', -21.6, -15, 21.6, 15), metre_lot('p2', '2', 'PS1', 21.6, -15, 34, 15),
                metre_lot('pcm', 'CM1', 'PS1', -21.6, 15, 34, 25), metre_lot('p9', '9', 'LP2', 40, -15, 60, 15)]
    return {'type': 'FeatureCollection', 'features': features, 'bounds': [LON-.002, LAT-.002, LON+.002, LAT+.002]}


def test_lot_frame_lot_only_grows_the_seed_lot_by_1_4_and_10_m(root, parcels):
    plan = L.lot_frame(ADDRESS_ID, root=root, parcels=parcels, group='lot')
    assert plan['group_kind'] == 'lot' and plan['seed'] == 'crm-seed' and plan['seed_lot'] == '1' and plan['plan'] == 'PS1'
    assert plan['lots_label'] == 'Lot 1' and [lot['lot'] for lot in plan['lots']] == ['1']
    assert plan['union_span_m'] == pytest.approx(43.2, abs=.01)
    assert plan['span_m'] == pytest.approx(43.2*1.4+10, abs=.05)
    assert plan['m_per_px'] == pytest.approx(70.48/512, abs=1e-3) and plan['zoom'] == 20 and plan['frame_px'] == 512
    west, south, east, north = plan['bounds']
    assert west < LON < east and south < LAT < north
    assert plan['sentence'].startswith('A lot frame for 4242 would be 70 m at 0.14 m/px (Google z20, 512 px), covering Lot 1 of PS1.')
    assert plan['objects_inside'] == [] and plan['source_scene_id'] is None and plan['refit_proposed'] is True


def test_plan_block_follows_the_connected_component_when_common_property_exists(root, parcels):
    plan = L.lot_frame(ADDRESS_ID, root=root, parcels=parcels, group='plan-block')
    assert plan['group_kind'] == 'plan-block' and plan['lots_label'] == 'Lots 1, 2 + CM1'
    assert plan['union_span_m'] == pytest.approx(55.6, abs=.01) and plan['span_m'] == pytest.approx(55.6*1.4+10, abs=.05)
    assert 'Lot 9' not in plan['lots_label'], 'another plan next door is never part of the block'
    auto = L.lot_frame(ADDRESS_ID, root=root, parcels=parcels)
    assert auto['group_kind'] == 'plan-block' and auto['span_m'] == plan['span_m']
    ordinary = {**parcels, 'features': [f for f in parcels['features'] if f['properties']['lot'] != 'CM1']}
    assert L.lot_frame(ADDRESS_ID, root=root, parcels=ordinary)['group_kind'] == 'lot'
    assert L.lot_frame(ADDRESS_ID, root=root, parcels=ordinary, group='plan-block')['group_kind'] == 'lot'


def test_span_is_clamped_to_the_capture_limits(root):
    huge = {'features': [metre_lot('p1', '1', 'PS1', -300, -300, 300, 300)], 'bounds': [LON-.01, LAT-.01, LON+.01, LAT+.01]}
    assert L.lot_frame(ADDRESS_ID, root=root, parcels=huge)['span_m'] == pytest.approx(250, abs=.05)
    tiny = {'features': [metre_lot('p1', '1', 'PS1', -2, -2, 2, 2)], 'bounds': [LON-.01, LAT-.01, LON+.01, LAT+.01]}
    assert L.lot_frame(ADDRESS_ID, root=root, parcels=tiny)['span_m'] == pytest.approx(4*1.4+10, abs=.05)
    sliver = {'features': [metre_lot('p1', '1', 'PS1', -1, -1, 1, 1)], 'bounds': [LON-.01, LAT-.01, LON+.01, LAT+.01]}
    with pytest.raises(ValueError, match='No approved lot covers'):
        L.lot_frame(ADDRESS_ID, root=root, parcels=sliver)  # 4 of the seed's 80 m² is below the 10% seed overlap


def test_source_scene_decides_whether_a_refit_is_proposed(root, parcels):
    frame = {'id': 'qa_source', 'image_width': 512, 'image_height': 512, 'centroid': {'lon': LON, 'lat': LAT},
             'pixel_to_meters': [[.065, 0], [0, -.065], [-16.64, 16.64]], 'meters_to_pixel': [[1/.065, 0], [0, -1/.065], [256, 256]],
             'objects': [{'id': 'inner', 'kind': 'building', 'status': 'candidate', 'ring': [[200, 200], [300, 200], [300, 300], [200, 300]]},
                         {'id': 'gone', 'kind': 'tree', 'status': 'superseded', 'ring': [[10, 10], [20, 10], [20, 20]]}]}
    plan = L.lot_frame(ADDRESS_ID, root=root, parcels=parcels, group='lot', source_scene=frame)
    assert plan['refit_proposed'] is True and plan['group_inside_source'] is False
    assert plan['source_m_per_px'] == pytest.approx(.065) and plan['objects_inside'] == ['inner']
    assert 'finer in native detail' in plan['sentence'] and 'coarser' not in plan['sentence']
    assert plan['sentence'].endswith('No saved review on qa_source.')
    wide = {**frame, 'pixel_to_meters': [[.25, 0], [0, -.25], [-64, 64]], 'meters_to_pixel': [[4, 0], [0, -4], [256, 256]]}
    (root/'data/scenes/qa_source').mkdir(parents=True)
    (root/'data/scenes/qa_source/review.json').write_text(json.dumps({'revision': 7}))
    plan = L.lot_frame(ADDRESS_ID, root=root, parcels=parcels, group='lot', source_scene=wide)
    assert plan['refit_proposed'] is False and plan['sentence'].startswith('The address lot (Lot 1 of PS1, 43.2 m) already sits inside this frame; no refit is proposed.')
    assert plan['sentence'].endswith('Reviews on qa_source (rev 7) cannot be carried onto a new frame.')


def test_missing_address_or_lot_is_an_error_not_a_guess(root, parcels):
    with pytest.raises(ValueError, match='Address not found'):
        L.lot_frame(9999, root=root, parcels=parcels)
    far = {**parcels, 'features': [metre_lot('p9', '9', 'LP2', 40, -15, 60, 15)]}
    with pytest.raises(ValueError, match='No approved lot covers'):
        L.lot_frame(ADDRESS_ID, root=root, parcels=far)
    with pytest.raises(ValueError, match='group must be'):
        L.lot_frame(ADDRESS_ID, root=root, parcels=parcels, group='estate')
    with pytest.raises(ValueError, match='unavailable'):
        L.lot_frame(ADDRESS_ID, root=root, offline=True)


def _tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob('*'))


def test_dry_run_cli_prints_the_plan_and_writes_nothing(root, parcels, capsys):
    key = signature(['address-point', round(LON, 7), round(LAT, 7)])
    frozen = root/L.PARCELS_DIR/f'{key}.json'
    frozen.parent.mkdir(parents=True)
    frozen.write_text(json.dumps({'key': key, 'sha256': signature(parcels), 'data': parcels}))
    before = _tree(root)
    assert L.main(['--root', str(root), '--address', str(ADDRESS_ID), '--dry-run', '--offline']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['dry_run'] is True and out['group_kind'] == 'plan-block' and out['parcels_source'] == 'eyes-parcels'
    assert _tree(root) == before
    with pytest.raises(SystemExit):
        L.main(['--root', str(root), '--address', str(ADDRESS_ID), '--dry-run', '--capture'])


class Fetcher:
    def __init__(self):
        self.calls = 0

    def _child(self, z, x, y):
        self.calls += 1
        return Image.new('RGB', (256, 256), (90, 110, 130))


def test_capture_writes_only_the_lot_scene_folder_and_refuses_an_existing_id(root, parcels):
    before = _tree(root)
    fetcher = Fetcher()
    scene, image = L.lot_source(ADDRESS_ID, 'lot', root=root, fetcher=fetcher, parcels=parcels)
    assert scene['id'] == f'lot-{ADDRESS_ID}' and scene['address_id'] == ADDRESS_ID and scene['title'] == TITLE
    assert scene['lot_frame']['group_kind'] == 'lot' and scene['lot_frame']['span_m'] == pytest.approx(70.48, abs=.05)
    assert scene['region']['imagery_zoom'] == 20 and scene['image_width'] == 512 and image.size == (512, 512)
    assert fetcher.calls > 0
    added = [p for p in _tree(root) if p not in before]
    assert added and all(p == 'data/scenes' or p.startswith(f'data/scenes/lot-{ADDRESS_ID}') for p in added), added
    assert not (root/'data/imagery').exists(), 'a fake fetcher writes no tile cache'
    folder = root/'data/scenes'/f'lot-{ADDRESS_ID}'
    assert (folder/'latest.json').exists() and (folder/f"{scene['image_sha256']}.png").exists()
    assert json.loads((folder/'latest.json').read_text())['model_signature'] == scene['model_signature']
    with pytest.raises(ValueError, match='never overwritten'):
        L.lot_source(ADDRESS_ID, 'lot', root=root, fetcher=fetcher, parcels=parcels)


# --- carry_from: the recapture carry path records a frame_transform (scene_recapture); nothing is carried here -----

class TexturedFetcher:
    """Tiles rendered from one georeferenced ground, so two captures of the same place agree by construction; shift_m
    displaces the content (metres east, north) to plant a misregistered capture."""

    def __init__(self, seed=11, shift_m=(0., 0.)):
        rng = np.random.default_rng(seed)
        self.rects = list(zip(rng.uniform(-45, 45, (70, 2)), rng.uniform(1.5, 5., (70, 2)), rng.uniform(25, 230, (70, 3)), strict=True))
        self.shift = shift_m
        self.calls = 0

    def _child(self, z, x, y):
        self.calls += 1
        n = 2.**z
        i, j = np.meshgrid(np.arange(256)+.5, np.arange(256)+.5)
        lon, lat = (x+i/256)/n*360-180, np.degrees(np.arctan(np.sinh(np.pi*(1-2*(y+j/256)/n))))
        scale = WORLD*math.cos(math.radians(LAT))
        mx = (lon-LON)/360*scale-self.shift[0]
        my = (np.arcsinh(np.tan(np.radians(lat)))-math.asinh(math.tan(math.radians(LAT))))/(2*math.pi)*scale-self.shift[1]
        rgb = np.stack([110+30*np.sin(mx/3.1), 105+25*np.cos(my/2.3), 95+20*np.sin((mx-my)/3.7)], -1)
        for (cx, cy), (hx, hy), level in self.rects:
            rgb[(np.abs(mx-cx) < hx) & (np.abs(my-cy) < hy)] = level
        return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), 'RGB')


def carry_design(root):
    path = root/'data/reviews/eyes/recapture/DESIGN.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'version': 'recapture-design-v1', 'max_shift_m': .7, 'min_ncc': .35, 'min_overlap_fraction': .6,
                                'match_iou': .5, 'close_iou': .9}))
    return path


def carry_source(root, fetcher):
    scene, _, data = capture_region([LON-.00023, LAT-.00018, LON+.00023, LAT+.00018], root=root, fetcher=fetcher)
    return save_scene({**scene, 'id': 'qa_carry_source'}, data, root)


def test_capture_with_carry_from_writes_a_measured_frame_transform_and_still_refuses_an_existing_id(root, parcels):
    carry_design(root)
    fetcher = TexturedFetcher()
    source = carry_source(root, fetcher)
    folder = root/'data/scenes/qa_carry_source'
    untouched = {str(p): p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    scene, _ = L.lot_source(ADDRESS_ID, 'lot', root=root, fetcher=fetcher, parcels=parcels, carry_from='qa_carry_source')
    ft = scene['frame_transform']
    assert ft['version'] == 'frame-transform-v1' and ft['method'] == 'georeferenced-affine'
    assert ft['source_scene_id'] == 'qa_carry_source' and ft['source_model_signature'] == source['model_signature']
    assert ft['source_image_sha256'] == source['image_sha256'] and ft['source_size'] == [512, 512]
    assert ft['source_registration'] == 'region' and ft['overlap']['fraction'] == 1.0
    assert ft['verified'] is True and ft['residual']['shift_m'] < .1 and ft['residual']['ncc'] > .5
    assert ft['design']['design_sha256'] == signature_of(root/'data/reviews/eyes/recapture/DESIGN.json')
    lot = root/'data/scenes'/f'lot-{ADDRESS_ID}'
    saved = json.loads((lot/'latest.json').read_text())
    assert saved['frame_transform'] == ft and saved['model_signature'] == scene['model_signature']
    assert scene['objects'] == [] and not (lot/'review.json').exists(), 'no detector runs and no decision is carried here'
    assert scene['lot_frame']['source_scene_id'] == 'qa_carry_source'
    assert {str(p): p.read_bytes() for p in folder.rglob('*') if p.is_file()} == untouched, 'the source scene is untouched'
    calls = fetcher.calls
    with pytest.raises(ValueError, match='never overwritten'):
        L.lot_source(ADDRESS_ID, 'lot', root=root, fetcher=fetcher, parcels=parcels, carry_from='qa_carry_source')
    assert fetcher.calls == calls


def test_carry_from_is_checked_before_any_tile_is_fetched(root, parcels):
    fetcher = TexturedFetcher()
    with pytest.raises(ValueError, match='Invalid scene ID'):
        L.lot_source(ADDRESS_ID, 'lot', root=root, fetcher=fetcher, parcels=parcels, carry_from='../etc')
    with pytest.raises(ValueError, match='qa_missing'):
        L.lot_source(ADDRESS_ID, 'lot', root=root, fetcher=fetcher, parcels=parcels, carry_from='qa_missing')
    carry_source(root, TexturedFetcher())
    with pytest.raises(ValueError, match='DESIGN'):
        L.lot_source(ADDRESS_ID, 'lot', root=root, fetcher=fetcher, parcels=parcels, carry_from='qa_carry_source')
    assert fetcher.calls == 0 and not (root/'data/scenes'/f'lot-{ADDRESS_ID}').exists()
    with pytest.raises(SystemExit):
        L.main(['--root', str(root), '--address', str(ADDRESS_ID), '--carry-from', 'qa_carry_source'])  # a carry needs --capture
    with pytest.raises(SystemExit):
        L.main(['--root', str(root), '--address', str(ADDRESS_ID), '--capture', '--allow-unverified'])  # only with --carry-from


def test_carry_from_refuses_to_save_a_capture_whose_frame_fails_the_design(root, parcels):
    carry_design(root)
    carry_source(root, TexturedFetcher())
    folder = root/'data/scenes/qa_carry_source'
    untouched = {str(p): p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    lot = root/'data/scenes'/f'lot-{ADDRESS_ID}'
    # the new capture's content sits 1.5 m (1.2 m east, 0.9 m south) from where its georeference puts it
    with pytest.raises(L.UnverifiedRecapture) as refused:
        L.lot_source(ADDRESS_ID, 'lot', root=root, fetcher=TexturedFetcher(shift_m=(1.2, -.9)), parcels=parcels,
                     carry_from='qa_carry_source')
    assert str(refused.value).splitlines()[0] == 'Recaptured frame does not agree with the source frame'
    assert refused.value.reasons and any(r.startswith('shift_m') for r in refused.value.reasons), refused.value.reasons
    assert not lot.exists(), 'nothing is saved for a frame that fails the design'
    # --allow-unverified keeps the capture, its frame_transform recorded as not verified, so no carry can ever use it
    scene, _ = L.lot_source(ADDRESS_ID, 'lot', root=root, fetcher=TexturedFetcher(shift_m=(1.2, -.9)), parcels=parcels,
                            carry_from='qa_carry_source', allow_unverified=True)
    ft = scene['frame_transform']
    assert ft['verified'] is False and ft['residual']['shift_m'] == pytest.approx(1.5, abs=.12)
    assert json.loads((lot/'latest.json').read_text())['frame_transform']['verified'] is False and scene['objects'] == []
    assert {str(p): p.read_bytes() for p in folder.rglob('*') if p.is_file()} == untouched, 'the source scene is untouched'


def test_capture_cli_exits_1_with_the_stable_message_for_an_unverified_frame(root, monkeypatch, capsys):
    calls = []

    def refused(*args, **kwargs):
        calls.append(kwargs)
        raise L.UnverifiedRecapture(['shift_m 1.503 > max_shift_m 0.7'])
    monkeypatch.setattr(L, 'lot_source', refused)
    args = ['--root', str(root), '--address', str(ADDRESS_ID), '--capture', '--carry-from', 'qa_carry_source']
    assert L.main(args) == 1
    err = capsys.readouterr().err.splitlines()
    assert err[:2] == ['Recaptured frame does not agree with the source frame', 'reason: shift_m 1.503 > max_shift_m 0.7']
    assert calls[-1]['carry_from'] == 'qa_carry_source' and calls[-1]['allow_unverified'] is False
    kept = {'id': f'lot-{ADDRESS_ID}', 'image_sha256': 'x', 'lot_frame': {},
            'frame_transform': {'verified': False, 'overlap': {'fraction': 1.}, 'residual': {'shift_m': 1.503, 'ncc': .8},
                                'design': {'max_shift_m': .7, 'min_ncc': .35, 'min_overlap_fraction': .6}}}
    monkeypatch.setattr(L, 'lot_source', lambda *a, **k: (calls.append(k), (kept, None))[1])
    assert L.main([*args, '--allow-unverified']) == 0 and calls[-1]['allow_unverified'] is True
    captured = capsys.readouterr()
    assert json.loads(captured.out)['frame_transform']['verified'] is False
    assert 'not verified' in captured.err and 'reason: shift_m 1.503 > max_shift_m 0.7' in captured.err


def signature_of(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
