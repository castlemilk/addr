import json

import pytest
from scripts.ml import context_landuse as cl
from scripts.ml.scene_confidence import geographic
from scripts.ml.scene_region import region_frame

BBOX = (145.298, -37.803, 145.354, -37.758)


def scene():
    scale = .25
    return {**region_frame([145.31, -37.7905, 145.311, -37.7895]), 'id': 'landuse-test', 'image_width': 400, 'image_height': 400,
            'pixel_to_meters': [[scale, 0], [0, -scale], [-50, 50]], 'meters_to_pixel': [[1/scale, 0], [0, -1/scale], [200, 200]],
            'objects': []}


def lonlat_ring(pixel_ring, scn):
    return [list(p) for p in geographic({'ring': pixel_ring}, scn).exterior.coords[:-1]]


def way(osm_id, tags, lonlat, closed=True):
    points = lonlat + ([lonlat[0]] if closed else [])
    return {'type': 'way', 'id': osm_id, 'tags': tags, 'geometry': [{'lon': x, 'lat': y} for x, y in points]}


def overpass_payload(scn):
    park = lonlat_ring([[300, 300], [380, 300], [380, 380], [300, 380]], scn)
    pitch = lonlat_ring([[10, 300], [60, 300], [60, 380], [10, 380]], scn)
    open_line = lonlat_ring([[100, 100], [120, 100], [120, 120]], scn)
    relation_outer = lonlat_ring([[200, 10], [260, 10], [260, 60], [200, 60]], scn)
    return {'elements': [
        way(1, {'leisure': 'park', 'name': 'Kiloran Park'}, park),
        way(2, {'leisure': 'pitch', 'sport': 'soccer'}, pitch),
        way(3, {'landuse': 'commercial'}, open_line, closed=False),      # not a ring: skipped
        way(4, {'building': 'yes'}, park),                                # not a wanted tag: skipped
        {'type': 'node', 'id': 5, 'lat': -37.79, 'lon': 145.31, 'tags': {'amenity': 'school'}},  # points are skipped
        {'type': 'relation', 'id': 6, 'tags': {'type': 'multipolygon', 'amenity': 'school', 'name': 'Primary'},
         'members': [{'type': 'way', 'ref': 7, 'role': 'outer', 'geometry': [{'lon': x, 'lat': y} for x, y in relation_outer + [relation_outer[0]]]},
                     {'type': 'node', 'ref': 8, 'role': 'label', 'lat': -37.79, 'lon': 145.31}]},
    ]}


def test_query_names_every_wanted_tag_and_bbox_order():
    query = cl.build_query(BBOX)
    assert '[out:json]' in query and 'out geom' in query
    assert '(-37.803,145.298,-37.758,145.354)' in query  # Overpass wants south,west,north,east
    for key, values in cl.TAGS.items():
        assert f'["{key}"~"^({"|".join(values)})$"]' in query
    assert query.count('way[') == 3 and query.count('relation[') == 3


def test_parse_keeps_closed_ways_and_relation_outers_only():
    scn = scene()
    features = cl.parse(overpass_payload(scn))
    assert [(f['osm_type'], f['osm_id'], f['tag_key'], f['tag_value'], f['name']) for f in features] == [
        ('way', 1, 'leisure', 'park', 'Kiloran Park'), ('way', 2, 'leisure', 'pitch', None), ('relation', 6, 'amenity', 'school', 'Primary')]
    assert all(f['geometry']['type'] in ('Polygon', 'MultiPolygon') for f in features)


def test_load_fetches_once_then_reads_cache(tmp_path):
    scn = scene()
    calls = []
    def fetch(bbox, **_):
        calls.append(bbox)
        return overpass_payload(scn)
    doc = cl.load(tmp_path, BBOX, fetch=fetch)
    assert doc['status'] == 'available' and doc['version'] == cl.VERSION == 'context-landuse-v1'
    assert len(doc['features']) == 3 and doc['bbox'] == list(BBOX) and doc['fetched_at']
    assert (tmp_path/cl.CACHE).exists()
    again = cl.load(tmp_path, BBOX, fetch=fetch)
    assert calls == [BBOX] and again['features'] == doc['features']
    # A different bbox than the cached one is not silently served from the cache.
    other = cl.load(tmp_path, (145.0, -38.0, 145.1, -37.9), fetch=fetch)
    assert len(calls) == 2 and other['bbox'] == [145.0, -38.0, 145.1, -37.9]
    cl.load(tmp_path, BBOX, fetch=fetch, refresh=True)
    assert len(calls) == 3


def test_load_offline_is_unavailable_and_never_cached(tmp_path):
    def broken(bbox, **_):
        raise OSError('no network')
    doc = cl.load(tmp_path, BBOX, fetch=broken)
    assert doc['status'] == 'unavailable' and doc['features'] == [] and 'no network' in doc['error']
    assert not (tmp_path/cl.CACHE).exists()
    doc = cl.load(tmp_path, BBOX, fetch=None)
    assert doc['status'] == 'unavailable' and doc['error'] == 'network disabled'


def test_landuse_near_in_scene_metres():
    scn = scene()
    land = cl.Landuse(cl.document(cl.parse(overpass_payload(scn)), BBOX))
    from shapely.geometry import box
    frame = scn['pixel_to_meters']
    def local(x0, y0, x1, y1):
        return box(min(x0*frame[0][0]+frame[2][0], x1*frame[0][0]+frame[2][0]), min(y0*frame[1][1]+frame[2][1], y1*frame[1][1]+frame[2][1]),
                   max(x0*frame[0][0]+frame[2][0], x1*frame[0][0]+frame[2][0]), max(y0*frame[1][1]+frame[2][1], y1*frame[1][1]+frame[2][1]))
    inside = land.near(local(310, 310, 330, 330), scn, within_m=20)
    assert [m['tag_value'] for m in inside] == ['park'] and inside[0]['distance_m'] == 0 and inside[0]['name'] == 'Kiloran Park'
    # 40 px == 10 m from the pitch's east edge: within 20 m; 120 px == 30 m is not.
    close = land.near(local(100, 320, 120, 340), scn, within_m=20)
    assert [m['tag_value'] for m in close] == ['pitch'] and close[0]['distance_m'] == pytest.approx(10, abs=.3)
    assert land.near(local(180, 320, 200, 340), scn, within_m=20) == []
    assert land.near(local(210, 20, 220, 30), scn, within_m=0)[0]['tag_key'] == 'amenity'
    assert cl.Landuse({'status': 'unavailable', 'features': []}).near(local(310, 310, 330, 330), scn, within_m=20) == []
    assert cl.Landuse({'status': 'unavailable', 'features': []}).status == 'unavailable' and land.status == 'available'


def test_cli_reports_without_network(tmp_path, capsys):
    assert cl.main(['--root', str(tmp_path), '--bbox', *map(str, BBOX), '--offline']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['status'] == 'unavailable' and out['features'] == 0


def test_parse_tolerates_malformed_payloads():
    for bad in ({'elements': None}, {'elements': 'x'}, {'elements': [None, 'junk', {'type': 'way'}]}, None, []):
        assert cl.parse(bad) == []


def test_landuse_bbox_and_coverage():
    land = cl.Landuse(cl.document([], BBOX))
    assert land.bbox == BBOX
    assert land.covers((145.31, -37.79, 145.311, -37.789))
    assert land.covers((145.2975, -37.8035, 145.354, -37.758))  # inside the 0.002 degree pad
    assert not land.covers((144.98, -37.80, 144.99, -37.79))    # 30 km west: Melbourne CBD captures
    assert not land.covers((145.31, -37.79, 145.36, -37.789))   # straddles the east edge
    assert cl.Landuse({'status': 'unavailable', 'features': []}).bbox is None
    assert cl.Landuse({'status': 'unavailable', 'features': []}).covers((0, 0, 1, 1))  # unknown box never claims coverage gaps
    assert cl.Landuse({'bbox': [1, 2, 3]}).bbox is None
