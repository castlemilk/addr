from scripts.ml.sam3_review import ReviewProposal, match_proposals
from shapely.geometry import Polygon


def square(x: float, y: float, size: float = 10.0) -> Polygon:
    return Polygon(
        [
            (x - size / 2, y - size / 2),
            (x + size / 2, y - size / 2),
            (x + size / 2, y + size / 2),
            (x - size / 2, y + size / 2),
        ]
    )


def proposal(polygon: Polygon, score: float = 0.9) -> ReviewProposal:
    return ReviewProposal(polygon=polygon, score=score, area_m2=polygon.area)


def test_high_overlap_replaces_existing():
    existing = [square(0, 0)]
    proposals = [proposal(square(0.5, 0.5))]
    results, existing_results, replaced = match_proposals(existing, proposals, min_iou=0.3)
    assert results[0].status == "replace"
    assert existing_results[0].status == "replace"
    assert replaced == [0]


def test_disjoint_proposal_is_extra():
    existing = [square(0, 0)]
    proposals = [proposal(square(100, 100))]
    results, existing_results, replaced = match_proposals(existing, proposals, min_iou=0.3)
    assert results[0].status == "extra"
    assert existing_results[0].status == "missed"
    assert replaced == []


def test_low_overlap_is_weak():
    existing = [square(0, 0)]
    proposals = [proposal(square(9, 0))]
    results, existing_results, _ = match_proposals(existing, proposals, min_iou=0.3)
    assert results[0].status in {"weak", "extra"}
    assert results[0].iou < 0.3
    assert existing_results[0].status == "weak"


def test_area_mismatch_does_not_replace():
    existing = [square(0, 0, 10)]
    proposals = [proposal(square(0, 0, 20))]
    results, _, replaced = match_proposals(existing, proposals, min_iou=0.2)
    assert results[0].status == "weak"
    assert results[0].area_delta_ratio > 2.0
    assert replaced == []


def test_multiple_proposals_choose_best_match():
    existing = [square(0, 0), square(50, 0)]
    proposals = [proposal(square(0, 0)), proposal(square(50, 0))]
    results, existing_results, replaced = match_proposals(existing, proposals, min_iou=0.3)
    assert [result.status for result in results] == ["replace", "replace"]
    assert replaced == [0, 1]
    assert all(result.status == "replace" for result in existing_results)
