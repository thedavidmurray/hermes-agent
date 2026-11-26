"""Offline provenance and failure controls for the opt-in live replay script."""
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest


@pytest.fixture
def replay():
    path = Path(__file__).resolve().parents[2] / 'scripts/replay_goal_gate_deadlocks.py'
    spec = importlib.util.spec_from_file_location('goal_gate_replay', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prepare_preserves_original_body_and_durable_evidence(tmp_path, monkeypatch, replay):
    db = tmp_path / 'board.db'
    original = tmp_path / 'original.body'
    original.write_text('original release body, not the rewritten task body')
    with sqlite3.connect(db) as conn:
        conn.executescript('''
            CREATE TABLE tasks (id TEXT, title TEXT, body TEXT, assignee TEXT);
            CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
            CREATE TABLE task_events (id INTEGER, task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER, run_id INTEGER);
            CREATE TABLE task_comments (id INTEGER, task_id TEXT, body TEXT);
            INSERT INTO tasks VALUES ('parent', 'Release', 'rewritten body', 'builder');
            INSERT INTO tasks VALUES ('child', 'Deploy release', '', 'verifier');
            INSERT INTO task_links VALUES ('parent', 'child');
            INSERT INTO task_comments VALUES (7, 'parent', 'actual saved handoff');
        ''')
        conn.execute('INSERT INTO task_events VALUES (1, ?, ?, ?, 1, 1)', ('child', 'created', json.dumps({'status': 'todo'})))
        conn.execute('INSERT INTO task_events VALUES (9, ?, ?, ?, 5, 2)', ('parent', 'blocked', '{}'))
        # Later activity must not erase the waiting child in historical replay.
        conn.execute('INSERT INTO task_events VALUES (10, ?, ?, ?, 6, 3)', ('child', 'claimed', '{}'))
    monkeypatch.setattr(replay, 'CASES', {'parent': ('original.body', 7, 9)})
    case, = replay.prepare(db, tmp_path)
    assert case['body'] == original.read_text()
    assert case['evidence'] == 'actual saved handoff'
    assert 'task_comments:7' in case['evidence_source']
    assert case['children'][0]['status'] == 'todo'
    assert 'child | assignee: verifier | Deploy release' in case['delegated_gates']
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT body FROM tasks WHERE id='parent'").fetchone()[0] == 'rewritten body'


@pytest.mark.parametrize('transport_failed,expected', [(False, 0), (True, 2)])
def test_transport_failure_cannot_count_as_non_done_control(tmp_path, monkeypatch, replay, transport_failed, expected):
    snapshot = tmp_path / 'inputs.json'
    snapshot.write_text(json.dumps({'cases': [{'id': 'test'}]}))
    result = {'verdict': 'done', 'reason': 'ok', 'parse_failed': False, 'transport_failed': False}
    null = dict(result, verdict='continue', transport_failed=transport_failed)
    rows = [{'id': f'case-{i}', 'waiting_children': 1, 'with': result, 'without': null} for i in range(4)]
    negative = dict(null, transport_failed=False)
    monkeypatch.setattr(replay, 'evaluate', lambda cases, timeout: (rows, negative))
    output = tmp_path / 'results.json'
    monkeypatch.setattr('sys.argv', ['replay', '--snapshot', str(snapshot), '--output', str(output)])
    assert replay.main() == expected
    assert json.loads(output.read_text())['acceptance_passed'] is (not transport_failed)
