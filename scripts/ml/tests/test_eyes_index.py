import json
import sqlite3

import pytest
from scripts.ml import eyes_index


def test_schema_is_idempotent_and_creates_all_tables(tmp_path):
    with eyes_index.connection(tmp_path) as db:
        eyes_index.ensure_schema(db)
        eyes_index.ensure_schema(db)
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(eyes_index.TABLES) <= names
    assert eyes_index.path(tmp_path) == tmp_path / 'data/reviews/eyes/eyes.sqlite'


def test_replace_rows_upserts_and_rows_filters(tmp_path):
    with eyes_index.connection(tmp_path) as db:
        eyes_index.ensure_schema(db)
        eyes_index.replace_rows(db, 'roof_diagnostics', [
            {'scene_id': 's', 'object_id': 'o', 'model_signature': 'm', 'version': 'v1', 'payload': {'a': 1}},
            {'scene_id': 's', 'object_id': 'o', 'model_signature': 'm', 'version': 'v1', 'payload': {'a': 2}},
            {'scene_id': 's', 'object_id': 'p', 'model_signature': 'm', 'version': 'v1', 'payload': {'a': 3}},
        ])
        rows = eyes_index.rows(db, 'roof_diagnostics', scene_id='s', object_id='o')
        assert len(rows) == 1 and rows[0]['payload'] == {'a': 2}
        assert len(eyes_index.rows(db, 'roof_diagnostics', scene_id='s')) == 2


def test_replace_rows_rejects_unknown_table_and_columns(tmp_path):
    with eyes_index.connection(tmp_path) as db:
        eyes_index.ensure_schema(db)
        with pytest.raises(ValueError):
            eyes_index.replace_rows(db, 'nope', [{'x': 1}])
        with pytest.raises(ValueError):
            eyes_index.replace_rows(db, 'snapshots', [{'id': 'a', 'bogus': 1, 'payload': {}}])


def test_fetch_object_collects_every_table_and_readonly_open(tmp_path):
    with eyes_index.connection(tmp_path) as db:
        eyes_index.ensure_schema(db)
        eyes_index.replace_rows(db, 'shadow_bands', [{'scene_id': 's', 'object_id': 'o', 'model_signature': 'm', 'version': 'v', 'payload': {'band_v2': 'medium'}}])
        eyes_index.replace_rows(db, 'cross_capture', [{'scene_id': 's', 'object_id': 'o', 'other_scene_id': 't', 'other_object_id': 'q', 'version': 'v', 'payload': {'iou': .9}}])
    found = eyes_index.fetch_object(tmp_path, 's', 'o')
    assert found['shadow_bands'][0]['payload']['band_v2'] == 'medium'
    assert found['cross_capture'][0]['other_object_id'] == 'q'
    assert found['roof_diagnostics'] == []
    with eyes_index.connection(tmp_path, readonly=True) as db:
        with pytest.raises(sqlite3.OperationalError):
            db.execute("INSERT INTO snapshots VALUES('x','t','v','{}')")


def test_an_index_written_before_a_table_existed_still_reads(tmp_path, capsys):
    with eyes_index.connection(tmp_path) as db:
        eyes_index.ensure_schema(db)
        eyes_index.replace_rows(db, 'shadow_bands', [{'scene_id': 's', 'object_id': 'o', 'model_signature': 'm', 'version': 'v', 'payload': {'band_v2': 'low'}}])
        db.execute('DROP TABLE line_corrections')  # an older writer's index lacks a table added later
        db.commit()
    found = eyes_index.fetch_object(tmp_path, 's', 'o')
    assert found['line_corrections'] == [] and found['shadow_bands'][0]['payload']['band_v2'] == 'low'
    assert eyes_index.main(['--root', str(tmp_path), '--tables']) == 0
    counts = json.loads(capsys.readouterr().out)['counts']
    assert counts['line_corrections'] == 0 and counts['shadow_bands'] == 1
    with eyes_index.connection(tmp_path, readonly=True) as db:
        assert 'line_corrections' not in eyes_index.tables_present(db)
    with eyes_index.connection(tmp_path) as db:
        eyes_index.ensure_schema(db)  # the next writer adds it without touching the other tables
        assert 'line_corrections' in eyes_index.tables_present(db)
        assert eyes_index.rows(db, 'shadow_bands', scene_id='s')[0]['payload'] == {'band_v2': 'low'}


def test_fetch_object_without_index_returns_empty(tmp_path):
    assert eyes_index.fetch_object(tmp_path, 's', 'o') == {t: [] for t in eyes_index.TABLES}


def test_cli_object_prints_json(tmp_path, capsys):
    with eyes_index.connection(tmp_path) as db:
        eyes_index.ensure_schema(db)
    eyes_index.main(['--root', str(tmp_path), '--object', 'scene-a', 'obj-b'])
    out = json.loads(capsys.readouterr().out)
    assert out['scene_id'] == 'scene-a' and out['tables']['references'] == []
