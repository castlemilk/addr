"""frame_context_census R7 (property-fit, re-centred) after the 2026-10-08 fix.

Regression: win-3dd37155248c04ef. The v1 census built the subject's development (plan PS408840) from the parcels on disk;
lot 6 (pfi 52589560) was on no frozen payload, so v1 said 'development inside' for a frame whose top edge (45.95 m north of the
window centre) the full development crosses (lot 6 reaches 47.69 m). The pilot capture placed the frame exactly where the
census said (no axis error): the polygon was incomplete. v2 flags it (parcels_open) and frames the full development."""
import math

import numpy as np
from scripts.ml import scene_focus as sf
from scripts.ml.experiments import frame_context_census as fc
from shapely.affinity import translate
from shapely.geometry import box

H0_3DD = 45.261/2                        # win-3dd37155248c04ef: 512 px, 45.261 m
DEV_V1 = box(-28.48, -10.57, 23.54, 39.05)   # development bounds from the 6 parcels on disk at census time (v1)
DEV_V2 = box(-28.48, -10.57, 23.54, 47.69)   # with lot 6 (pfi 52589560)
LOT6 = box(-5., 39.05, 10., 47.69)


def inside(frame, geo, margin=0.):
    return translate(geo, -frame['cx'], -frame['cy']).within(fc.square(frame['half']-margin+1e-6))


def test_unchanged_when_property_fits_today():
    frame = fc.property_frame(box(-10, -10, 10, 10), H0_3DD)
    assert not frame['recentred'] and frame['half'] == H0_3DD and (frame['cx'], frame['cy']) == (0., 0.)


def test_recentres_north_with_margin_and_v1_regression():
    v1 = fc.property_frame(DEV_V1, H0_3DD)
    assert v1['recentred'] and v1['cy'] > 0                  # property north of the window: centre moves north (y north)
    assert inside(v1, DEV_V1, fc.MARGIN_M)
    assert not inside(v1, DEV_V2)                            # the v1 frame cuts the full development at its top edge
    assert v1['cy']+v1['half'] < 47.69
    v2 = fc.property_frame(DEV_V2, H0_3DD)
    assert inside(v2, DEV_V2, fc.MARGIN_M) and v2['property_fits'] and not v2['capped']
    assert math.isclose(v2['half'], (47.69+10.57)/2+fc.MARGIN_M)


def test_open_area_flags_a_development_the_payloads_do_not_cover():
    payload = box(-40, -40, 40, 45.95)                       # frozen query bounds that stop short of lot 6's north end
    assert fc.open_area(DEV_V1, payload) < fc.OPEN_MIN_M2    # v1's development looked complete ...
    assert fc.open_area(DEV_V2, payload) > fc.OPEN_MIN_M2    # ... the real one is not: a parcel north of it could be missing
    assert fc.open_area(DEV_V2, box(-40, -40, 40, 60)) < fc.OPEN_MIN_M2
    assert fc.open_area(DEV_V1, None) > 0 and fc.open_area(box(0, 0, 0, 0), payload) == 0.


def test_over_the_cap_keeps_subject_and_lot_inside():
    prop = box(-150, -10, 50, 10)                            # 200 m wide: cannot fit 120 m
    keep = box(30, -5, 45, 5)                                # subject + lot at the east end
    frame = fc.property_frame(prop, 30., keep)
    assert frame['capped'] and not frame['property_fits'] and math.isclose(frame['half'], fc.CAP_M/2)
    assert inside(frame, keep, fc.MARGIN_M)
    assert not inside(frame, prop)
    # the centre moves only as far as needed: east edge exactly margin beyond the lot
    assert math.isclose(frame['cx']+frame['half'], 45+fc.MARGIN_M)


def test_capture_path_axis_sign():
    """The pilot capture: R7 (cx, cy) in window metres -> m_to_lonlat -> scene_focus._frame_for. The property must land inside
    the planned frame's pixels, north up (a northern point has a smaller pixel row)."""
    lon0, lat0 = 145.31911425, -37.78980005
    mpp = .0884
    latest = {'centroid': {'lon': lon0, 'lat': lat0}, 'pixel_to_meters': [[mpp, 0], [0, -mpp], [-H0_3DD, H0_3DD]]}
    frame = fc.property_frame(DEV_V2, H0_3DD)
    lon, lat = fc.m_to_lonlat(latest, frame['cx'], frame['cy'])
    assert (float(lat) > lat0) == (frame['cy'] > 0)                      # north in metres is north in latitude
    _, planned = sf._frame_for((float(lon), float(lat)), 2*frame['half'], 1024)
    xs, ys = (np.asarray(c, float) for c in zip(*DEV_V2.exterior.coords, strict=True))
    plon, plat = fc.m_to_lonlat(latest, xs, ys)
    px_m = np.c_[fc.lonlat_to_m(planned, plon, plat)]
    px = np.c_[px_m, np.ones(len(px_m))] @ np.asarray(planned['meters_to_pixel'], float)
    margin_px = fc.MARGIN_M/(2*frame['half']/1024)
    assert px[:, 0].min() >= margin_px-1 and px[:, 0].max() <= 1024-margin_px+1
    assert px[:, 1].min() >= margin_px-1 and px[:, 1].max() <= 1024-margin_px+1
    north, south = int(np.argmax(ys)), int(np.argmin(ys))
    assert px[north, 1] < px[south, 1]
