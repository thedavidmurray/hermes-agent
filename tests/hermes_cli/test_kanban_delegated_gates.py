"""Graph-owned acceptance scopes both lifecycle judges without weakening controls."""
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import kanban as cli, kanban_db as kb, goals
from tools import kanban_tools as tools


@pytest.fixture
def conn():
    # Minimal real graph; do not open or mutate a user's board.
    with sqlite3.connect(':memory:') as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript('''
            CREATE TABLE tasks (id TEXT PRIMARY KEY, assignee TEXT, title TEXT, status TEXT);
            CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
        ''')
        yield conn


def child(conn, status, tid='child', parent='parent'):
    conn.execute('INSERT INTO tasks VALUES (?, ?, ?, ?)', (tid, 'verifier', 'Merge + deploy + live probe', status))
    conn.execute('INSERT INTO task_links VALUES (?, ?)', (parent, tid))


@pytest.mark.parametrize('status', ['todo', 'ready', 'blocked', 'done', 'archived', 'running', 'review', None])
@pytest.mark.parametrize('surface', ['cli', 'tool'])
def test_judges_receive_only_waiting_direct_children(conn, status, surface):
    if status:
        child(conn, status)
    child(conn, 'todo', 'unrelated', parent='someone-else')
    task = SimpleNamespace(id='parent', title='Original title', body='Original body', goal_mode=True)
    expected = kb.delegated_gate_context(conn, task.id)
    judge_path = 'hermes_cli.goals.judge_goal' if surface == 'cli' else 'tools.kanban_tools.judge_goal'
    with patch('agent.auxiliary_client.get_text_auxiliary_client', return_value=(object(), 'test')), \
            patch.object(tools, '_goal_judge_available', return_value=True), \
            patch(judge_path, return_value=('done', 'ok', False, None, False)) as judge:
        if surface == 'cli':
            assert cli._goal_mode_handoff_rejection(task, ' evidence ', conn=conn) == ('done', None)
        else:
            tools._goal_gate('kanban_complete', task, task.id, ' evidence ', conn=conn)
    kwargs = {'goal': 'Original title\n\nOriginal body', 'last_response': 'evidence'}
    if status in {'todo', 'ready', 'blocked'}:
        assert 'DELEGATED GATES' in expected
        assert 'child | assignee: verifier | Merge + deploy + live probe' in expected
        assert 'unrelated' not in expected
        kwargs['delegated_gates'] = expected
    else:
        assert expected == ''
    judge.assert_called_once_with(**kwargs)


def test_stable_order_and_live_status_changes(conn):
    child(conn, 'todo', 'z')
    child(conn, 'blocked', 'a')
    assert kb.delegated_gate_context(conn, 'parent').index('- a') < kb.delegated_gate_context(conn, 'parent').index('- z')
    conn.execute("UPDATE tasks SET status='done' WHERE id='a'")
    conn.execute("UPDATE tasks SET status='archived' WHERE id='z'")
    assert kb.delegated_gate_context(conn, 'parent') == ''


def test_context_survives_long_goal_and_empty_is_byte_identical():
    captured = []
    def llm(call, system, prompt, timeout):
        captured.append(prompt)
        return '{"verdict":"done", "reason":"ok"}'
    with patch.object(goals, '_call_goal_judge_llm', side_effect=llm), \
            patch.object(goals, 'safe_strftime', return_value='frozen test clock'):
        goals.judge_goal('release spec ' * 1000, 'evidence')
        goals.judge_goal('release spec ' * 1000, 'evidence', delegated_gates='')
        goals.judge_goal('release spec ' * 1000, 'evidence', delegated_gates='DELEGATED GATES — waiting child')
    # A frozen clock makes this an exact comparison, not a timing-sensitive one.
    assert captured[0] == captured[1]
    assert captured[2] == captured[0] + '\n\nDELEGATED GATES — waiting child'


@pytest.mark.parametrize('surface', ['cli', 'tool'])
@pytest.mark.parametrize('outcome', [('continue', 'unfinished', False, None, False), ('continue', 'transport', False, None, True), RuntimeError('judge broke')])
def test_waiting_children_do_not_bypass_verdict_or_change_fail_open(conn, surface, outcome):
    child(conn, 'todo')
    task = SimpleNamespace(id='parent', title='title', body='body', goal_mode=True)
    target = 'hermes_cli.goals.judge_goal' if surface == 'cli' else 'tools.kanban_tools.judge_goal'
    mock_args = {'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome}
    with patch('agent.auxiliary_client.get_text_auxiliary_client', return_value=(object(), 'test')), \
            patch.object(tools, '_goal_judge_available', return_value=True), patch(target, **mock_args):
        genuine = not isinstance(outcome, Exception) and not outcome[-1]
        if surface == 'cli':
            assert cli._goal_mode_handoff_rejection(task, 'evidence', conn=conn) == (('continue', 'unfinished') if genuine else ('done', None))
        elif genuine:
            with pytest.raises(tools._Reject):
                tools._goal_gate('kanban_complete', task, task.id, 'evidence', conn=conn)
        else:
            tools._goal_gate('kanban_complete', task, task.id, 'evidence', conn=conn)
