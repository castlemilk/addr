"""Agent review loop for structure-kinds hints: observations are tier-3 rows, never labels; export marks eligibility."""
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest
from PIL import Image
from scripts.ml import eyes_index
from scripts.ml import structure_kinds as sk
from scripts.ml import structure_kinds_review as review

sys.path.insert(0, str(Path(__file__).parent))
from test_structure_kinds import build_scene  # noqa: E402


@pytest.fixture
def indexed_scene(tmp_path):
    scene, _, png = build_scene()
    folder = tmp_path/'data/scenes'/scene['id']
    folder.mkdir(parents=True)
    (folder/'latest.json').write_text(json.dumps(scene))
    (folder/f"{scene['image_sha256']}.png").write_bytes(png)
    sk.run(tmp_path, [scene['id']])
    return scene


def test_cards_render_pages_with_an_index(tmp_path, indexed_scene):
    rows = review.hint_rows(tmp_path, [(indexed_scene['id'], 'shed'), (indexed_scene['id'], 'container')])
    pages = review.render_cards(tmp_path, rows, tmp_path/'cards', per_page=8)
    assert len(pages) == 1
    with Image.open(pages[0]) as page:
        assert page.width == 2*(2*360+30)
    index = json.loads((tmp_path/'cards/index.json').read_text())
    assert [i['object_id'] for i in index] == ['shed', 'container'] and index[0]['hint'] == 'shed'


def test_observations_join_the_hint_and_never_touch_other_versions(tmp_path, indexed_scene):
    sid = indexed_scene['id']
    before = None
    with closing(sqlite3.connect(eyes_index.path(tmp_path))) as db:
        before = dict(db.execute('SELECT version, COUNT(*) FROM object_quality GROUP BY version').fetchall())
    rows, written = review.record(tmp_path, [{'scene_id': sid, 'object_id': 'shed', 'observed': 'shed', 'confidence': 'strong'},
                                             {'scene_id': sid, 'object_id': 'garage', 'observed': 'part-of-house', 'note': 'roof continues'}],
                                  reviewer='agent:test', tier='agent', session='s1')
    assert written == 2
    shed, garage = (r['payload'] for r in rows)
    assert shed['agrees'] is True and shed['hint'] == 'shed' and shed['hint_version'] == sk.VERSION
    assert garage['agrees'] is False and garage['hint'] == 'garage' and garage['observer_note'] == 'roof continues'
    assert shed['tier'] == 3 and shed['training_eligible'] is False and shed['note'] == review.OBSERVATION_NOTE
    with closing(sqlite3.connect(eyes_index.path(tmp_path))) as db:
        after = dict(db.execute('SELECT version, COUNT(*) FROM object_quality GROUP BY version').fetchall())
    assert after == {**before, review.REVIEW_VERSION: 2}
    review.record(tmp_path, [{'scene_id': sid, 'object_id': 'shed', 'observed': 'shed'}], reviewer='human:x', tier='human-confirmed')
    with closing(sqlite3.connect(eyes_index.path(tmp_path))) as db:
        both = dict(db.execute('SELECT version, COUNT(*) FROM object_quality GROUP BY version').fetchall())
    assert both[review.REVIEW_VERSION] == 2 and both[review.HUMAN_VERSION] == 1   # a human row never overwrites the agent row
    summary = review.agreement(tmp_path, session='s1')
    assert (summary['rows'], summary['agree']) == (2, 1)
    assert summary['confusion'] == {'shed -> shed': 1, 'garage -> part-of-house': 1}


def test_unknown_observed_kind_is_rejected(tmp_path, indexed_scene):
    with pytest.raises(ValueError):
        review.observation_rows(tmp_path, [{'scene_id': indexed_scene['id'], 'object_id': 'shed', 'observed': 'castle'}], reviewer='agent:test')


def test_export_marks_only_human_rows_training_eligible(tmp_path, indexed_scene):
    sid = indexed_scene['id']
    review.record(tmp_path, [{'scene_id': sid, 'object_id': 'shed', 'observed': 'shed'}], reviewer='agent:test', tier='agent')
    review.record(tmp_path, [{'scene_id': sid, 'object_id': 'tank', 'observed': 'water-tank'}], reviewer='human:reviewer', tier='human-confirmed')
    human_only = review.export_training(tmp_path, tmp_path/'train')
    assert human_only['exported'] == 1 and human_only['training_eligible'] == 1
    assert human_only['items'][0]['object_id'] == 'tank' and human_only['items'][0]['training_eligible'] is True
    assert (tmp_path/'train/crops'/f'{sid}--tank.png').exists() and (tmp_path/'train/masks'/f'{sid}--tank.png').exists()
    both = review.export_training(tmp_path, tmp_path/'train2', include_agent=True)
    assert both['exported'] == 2 and both['training_eligible'] == 1
    assert {i['object_id']: i['training_eligible'] for i in both['items']} == {'shed': False, 'tank': True}
