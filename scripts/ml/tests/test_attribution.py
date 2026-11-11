from scripts.ml.attribution import audit


def record(address_id, full_address, pin, centroid, ring):
    return {
        "address_id": address_id,
        "full_address": full_address,
        "kind": "house",
        "area_m2": 200.0,
        "boundary_source": "extracted",
        "address_point": {"lat": pin[0], "lon": pin[1]},
        "centroid": {"lat": centroid[0], "lon": centroid[1]},
        "ring_meters": ring,
    }


def pin(address_id, full_address, lat, lon, number=None, street=None):
    return {
        "address_id": address_id,
        "unit": None,
        "number": number,
        "street": street,
        "full_address": full_address,
        "lat": lat,
        "lon": lon,
    }


RING = [[-5, -5], [5, -5], [5, 5], [-5, 5]]  # 10x10 m around the origin


def test_clear_misattribution_is_cross_address():
    # Own pin 25 m east of a 10 m ring; neighbour pin inside the ring.
    records = [
        record(1, "1 TEST STREET", (0.0, 25 / 111320.0), (0.0, 0.0), RING),
    ]
    pins = [
        pin(1, "1 TEST STREET", 0.0, 25 / 111320.0, number="1", street="TEST STREET"),
        pin(2, "2 TEST STREET", 0.0, 0.0, number="2", street="TEST STREET"),
    ]
    result = audit(records, pins)
    assert len(result["suspects"]) == 1
    suspect = result["suspects"][0]
    assert suspect["address_id"] == 1
    assert suspect["tier"] == "cross-address"
    assert suspect["other_address_id"] == 2
    assert result["cross_address"] == 1


def test_shared_duplex_is_clean():
    # Both pins inside the same ring: no disagreement either way.
    records = [
        record(1, "1 TEST STREET", (0.0, 2 / 111320.0), (0.0, 0.0), RING),
        record(2, "3 TEST STREET", (0.0, -2 / 111320.0), (0.0, 0.0), RING),
    ]
    pins = [
        pin(1, "1 TEST STREET", 0.0, 2 / 111320.0, number="1", street="TEST STREET"),
        pin(2, "3 TEST STREET", 0.0, -2 / 111320.0, number="3", street="TEST STREET"),
    ]
    result = audit(records, pins)
    assert result["suspects"] == []


def test_close_neighbours_within_margin_are_clean():
    # Own pin 8 m out, neighbour pin 6 m out: below the 10 m threshold.
    records = [
        record(1, "1 TEST STREET", (0.0, 13 / 111320.0), (0.0, 0.0), RING),
    ]
    pins = [
        pin(1, "1 TEST STREET", 0.0, 13 / 111320.0, number="1", street="TEST STREET"),
        pin(2, "3 TEST STREET", 0.0, 11 / 111320.0, number="3", street="TEST STREET"),
    ]
    result = audit(records, pins)
    assert result["suspects"] == []


def test_same_development_tier():
    # Big unit-block disagreement stays in the same-development tier.
    records = [
        record(2, "Unit 2 29 TEST STREET", (0.0, 30 / 111320.0), (0.0, 0.0), RING),
    ]
    pins = [
        pin(1, "29 TEST STREET", 0.0, 0.0, number="29", street="TEST STREET"),
        pin(
            2,
            "Unit 2 29 TEST STREET",
            0.0,
            30 / 111320.0,
            number="29",
            street="TEST STREET",
        ),
    ]
    result = audit(records, pins)
    assert len(result["suspects"]) == 1
    assert result["suspects"][0]["tier"] == "same-development"
    assert result["cross_address"] == 0
