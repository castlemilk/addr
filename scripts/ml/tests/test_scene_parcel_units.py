from scripts.ml import scene_parcel_units as spu


def sq(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def test_square_cut_between_equal_units_is_automatic():
    ring = sq(0, 0, 200, 100)
    units = [{'ring': sq(0, 0, 100, 100), 'm2': 100}, {'ring': sq(100, 0, 200, 100), 'm2': 100}]
    assert spu.split_check(ring, units) == (True, '')


def test_diagonal_lot_line_across_one_roof_goes_to_review():
    ring = sq(0, 0, 200, 100)
    units = [{'ring': [[0, 0], [150, 0], [50, 100], [0, 100]], 'm2': 100}, {'ring': [[150, 0], [200, 0], [200, 100], [50, 100]], 'm2': 100}]
    ok, why = spu.split_check(ring, units)
    assert not ok and 'diagonal' in why


def test_a_small_attached_piece_goes_to_review():
    ring = sq(0, 0, 220, 100)
    units = [{'ring': sq(0, 0, 180, 100), 'm2': 180}, {'ring': sq(180, 0, 220, 100), 'm2': 40}]
    ok, why = spu.split_check(ring, units)
    assert not ok and 'small' in why


def test_every_planned_unit_stays_inside_its_roof():
    import glob, json
    from shapely.geometry import Polygon
    plans = sorted(glob.glob('data/reviews/eyes/experiments/parcel-units-2026-10-12/plan-v2/plans/*.json'))
    if not plans:
        import pytest
        pytest.skip('no plan on disk')
    for f in plans:
        for row in json.load(open(f))['rows']:
            roof = Polygon(row['ring']).buffer(0.5)
            for u in row['units']:
                assert roof.covers(Polygon(u['ring'])), f"{f} {row['object_id']} lot {u['lot']}"


def test_every_planned_unit_stays_inside_its_own_aligned_lot():
    import glob, json
    from shapely.geometry import Polygon
    from scripts.ml import parcel_units as pu
    plans = sorted(glob.glob('data/reviews/eyes/experiments/parcel-units-2026-10-12/plan-v2/plans/*.json'))
    if not plans:
        import pytest
        pytest.skip('no plan on disk')
    for f in plans:
        plan = json.load(open(f))
        if not plan['rows']:
            continue
        L = json.load(open(f"data/scenes/{plan['scene']}/latest.json"))
        lots = {r['pfi']: r['_geo'] for row in plan['rows'] for r in pu.lot_shares(L, row['ring'], pu.scene_parcels(L))}
        for row in plan['rows']:
            for u in row['units']:
                assert lots[u['pfi']].buffer(0.05).covers(Polygon(u['ring'])), f"{plan['scene']} {row['object_id']} lot {u['lot']}"
