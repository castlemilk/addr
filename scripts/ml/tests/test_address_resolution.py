import json
from pathlib import Path

import pytest
from scripts.ml import address_resolution as r
from scripts.ml.building_addresses import PropertyIndex
from scripts.ml.tests.test_building_addresses import feature, snapshot
from shapely.geometry import box, shape


def unit(pfi, name, geometry, **props):
    return feature(pfi=pfi, name=name, address=pfi+'0', geometry=geometry,
                   add_blg_unit_type='UNIT', add_blg_unit_id_1=int(pfi),
                   add_num_road_address=name, add_locality_name='TEST', add_postcode='3138', **props)


def parent(geometry=None):
    geometry = geometry if geometry is not None else box(0, 0, 3, 2)
    return feature(pfi='9', address='90', name='10 TEST ROAD', geometry=geometry,
                   add_num_road_address='10 TEST ROAD', add_locality_name='TEST', add_postcode='3138')


def test_unit_property_wins_over_containing_site_but_retains_site():
    index = PropertyIndex(snapshot([parent(), unit('1', '1/10 TEST ROAD', box(0, 0, 1, 2))]))
    result = index.match(box(.1, .1, .9, 1.9))
    assert result['method'] == 'unit-property'
    assert result['full_address'] == '1/10 TEST ROAD'
    assert result['site_context'][0]['full_address'] == '10 TEST ROAD'


def test_different_street_or_noncontaining_property_cannot_be_suppressed():
    index = PropertyIndex(snapshot([parent(), unit('1', '1/20 OTHER ROAD', box(0, 0, 1, 2))]))
    assert index.match(box(.1, .1, .9, 1.9))['status'] == 'ambiguous'


def test_shared_roof_keeps_two_unit_associations_without_modifying_roof():
    index = PropertyIndex(snapshot([parent(), unit('1', '1/10 TEST ROAD', box(0, 0, 1, 2)), unit('2', '2/10 TEST ROAD', box(1, 0, 2, 2))]))
    roof = box(.1, .1, 1.9, 1.9)
    before = roof.wkb
    result = index.match(roof)
    assert result['status'] == 'shared'
    assert result['coverage'] == 1
    assert [p['roof_fraction'] for p in result['allocations']] == [.5, .5]
    assert len(result['candidates']) == 2
    assert roof.wkb == before


def test_overlapping_unit_titles_and_stacked_floors_require_review():
    for extra in ({}, {'add_floor_type': 'LEVEL', 'add_floor_no_1': 2}):
        index = PropertyIndex(snapshot([parent(), unit('1', '1/10 TEST ROAD', box(0, 0, 1, 2)), unit('2', '2/10 TEST ROAD', box(0, 0, 1, 2), **extra)]))
        assert index.match(box(.1, .1, .9, 1.9))['status'] == 'ambiguous'
    index = PropertyIndex(snapshot([unit('1', '1/10 TEST ROAD', box(0, 0, 1, 2), add_floor_type='LEVEL', add_floor_no_1=2)]))
    result = index.match(box(.1, .1, .9, 1.9))
    assert result['status'] == 'ambiguous' and result['scope'] == 'multi_level'


def test_boundary_fringe_does_not_turn_single_roof_into_shared_units():
    index = PropertyIndex(snapshot([feature(geometry=box(0, 0, 1, 1)), feature(pfi='2', address='20', name='2 TEST ROAD', geometry=box(1, 0, 2, 1))]))
    assert index.match(box(.1, .1, 1.05, .9))['status'] == 'matched'


def test_commercial_multi_tenant_building_preserves_all_shop_addresses():
    features = [feature(name='SHOP 1', address='10', add_blg_unit_type='SHOP'), feature(name='SHOP 2', address='20', add_blg_unit_type='SHOP')]
    result = PropertyIndex(snapshot(features)).match(box(.1, .1, .9, .9))
    assert result['status'] == 'ambiguous'
    assert result['address_kind'] == 'commercial_address'
    assert len(result['candidates']) == 2


@pytest.fixture
def review_context(tmp_path, monkeypatch):
    from scripts.ml import scene_confidence
    index = PropertyIndex(snapshot([feature()]))
    monkeypatch.setattr(r, 'load_index', lambda root: index)
    monkeypatch.setattr(scene_confidence, 'geographic', lambda obj, scene: box(*obj['bounds']))
    folder = tmp_path/'data/scenes/test'
    folder.mkdir(parents=True)
    model = {'id': 'test', 'model_signature': 'a'*64, 'objects': [{'id': 'roof', 'kind': 'building', 'bounds': [.1, .1, .8, .8]}]}
    (folder/'latest.json').write_text(json.dumps(model))
    c = r.context(tmp_path, 'test', 'roof')
    payload = {'scene_id': 'test', 'object_id': 'roof', 'binding': c['binding'], 'revision': 0, 'action': 'confirm',
               'address_keys': ['1/10'], 'scope': 'unit', 'source_kind': 'verified_entrance', 'note': 'Address verified on the entrance plaque.'}
    return tmp_path, payload, folder/'latest.json'


def test_confirm_reload_clear_keeps_history_and_source_immutable(review_context):
    root, payload, model = review_context
    before = model.read_bytes()
    saved = r.save(root, payload, 'reviewer')
    assert saved['revision'] == 1
    assert saved['resolved']['status'] == 'reviewed'
    assert r.context(root, 'test', 'roof')['decision']['source_kind'] == 'verified_entrance'
    payload.update(revision=1, action='clear', note='Remove this association pending a revised plan.')
    cleared = r.save(root, payload, 'reviewer')
    assert cleared['revision'] == 2 and cleared['history_count'] == 2
    assert cleared['resolved']['status'] == 'matched'
    assert model.read_bytes() == before


def test_stale_tab_unknown_candidate_and_invalid_evidence_cannot_save(review_context):
    root, payload, _ = review_context
    for change in ({'binding': 'wrong'}, {'revision': 9}, {'address_keys': ['other/99']},
                   {'address_keys': ['1/10', '1/10']}, {'source_kind': 'model_guess'}, {'note': 4}, {'scope': 'ownership'}):
        with pytest.raises(r.ResolutionError):
            r.save(root, {**payload, **change}, 'reviewer')
    assert r.read(root, 'test', 'roof')['revision'] == 0
    r.save(root, payload, 'reviewer')
    with pytest.raises(r.ResolutionError, match='changed'):
        r.save(root, payload, 'second reviewer')


def test_changed_roof_and_changed_address_snapshot_reopen_decision(review_context, monkeypatch):
    root, payload, model = review_context
    r.save(root, payload, 'reviewer')
    updated = json.loads(model.read_text())
    updated['objects'][0]['bounds'] = [.2, .2, .9, .9]
    model.write_text(json.dumps(updated))
    c = r.context(root, 'test', 'roof')
    assert c['decision_stale'] and c['resolved']['decision_state'] == 'stale'
    assert c['resolved']['status'] != 'reviewed'
    updated['objects'][0]['bounds'] = [.1, .1, .8, .8]
    model.write_text(json.dumps(updated))
    data = snapshot([feature()])
    data['dataset_signature'] = 'changed'
    monkeypatch.setattr(r, 'load_index', lambda root: PropertyIndex(data))
    assert r.context(root, 'test', 'roof')['decision_stale']


def test_defer_does_not_confirm_and_scope_requires_one_unit(review_context):
    root, payload, _ = review_context
    result = r.save(root, {**payload, 'action': 'defer', 'note': 'Need the strata plan to distinguish the floors.'}, 'reviewer')
    assert result['resolved']['decision_state'] == 'deferred'
    assert result['resolved']['status'] == 'matched'


def test_shared_site_decision_retains_all_addresses_and_searchable_fields(review_context, monkeypatch):
    root, payload, _ = review_context
    index = PropertyIndex(snapshot([feature(), feature(name='SHOP 2 1 TEST ROAD', address='20')]))
    monkeypatch.setattr(r, 'load_index', lambda root: index)
    current = r.context(root, 'test', 'roof')
    payload.update(binding=current['binding'], scope='unit', address_keys=['1/10', '1/20'])
    with pytest.raises(r.ResolutionError, match='one address'):
        r.save(root, payload, 'reviewer')
    result = r.save(root, {**payload, 'scope': 'site'}, 'reviewer')['resolved']
    assert result['status'] == 'reviewed' and len(result['candidates']) == 2
    assert 'full_address' not in result
    assert 'SHOP 2' in ' '.join([result.get('full_address', ''), *[c['full_address'] for c in result['candidates']]])


def test_binding_stable_for_ring_orientation_but_changes_with_model():
    roof = box(0, 0, 1, 1)
    assert r.binding(roof, 'a', {}) == r.binding(roof.reverse(), 'a', {})
    assert r.binding(roof, 'a', {}) != r.binding(roof, 'b', {})


@pytest.mark.parametrize('scene,obj', [('../escape', 'roof'), ('test', '../escape'), (None, 'roof')])
def test_decision_path_is_bounded(tmp_path, scene, obj):
    with pytest.raises(r.ResolutionError):
        r.location(tmp_path, scene, obj)


def test_real_unit_shop_and_shared_site_records():
    fixture = json.loads((Path(__file__).parent/'fixtures/unit-commercial-addresses.json').read_text())
    for case in fixture['cases']:
        data = {**snapshot(case['features']), 'bounds': fixture['bounds']}
        result = PropertyIndex(data).match(shape(case['roof']))
        assert result['status'] == case['expected']
        if case['object'] == 'f36bbc9068ef98a7':
            assert result['full_address'] == '3/208 HULL ROAD MOOROOLBARK 3138'
            assert result['scope'] == 'unit'
        if case['object'] == '44f80ff4616f7d16':
            assert result['status'] == 'shared'
            assert len(result['allocations']) == 5
            assert result['address_kind'] == 'commercial_address'
        if case['object'] == 'cc22cc433bafe31a':
            assert result['full_address'] == '118 HAYRICK LANE MOOROOLBARK 3138'
            assert result['scope'] == 'site'


def test_clipped_roof_associations_do_not_claim_the_complete_unit():
    from scripts.ml.property_address_resolution import image_cut, visible_only
    result = visible_only({'status': 'matched', 'scope': 'unit', 'full_address': 'UNIT 1'}, True)
    assert result['status'] == 'ambiguous' and result['partial']
    assert 'full_address' not in result
    assert visible_only({'status': 'shared', 'scope': 'shared_roof'}, True)['status'] == 'ambiguous'
    assert image_cut({'image_width': 100, 'image_height': 100}, {'ring': [[0, 10], [20, 10], [20, 30]]})
    assert not image_cut({'image_width': 100, 'image_height': 100}, {'ring': [[10, 10], [20, 10], [20, 30]]})
