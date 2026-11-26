#!/usr/bin/env python3
"""Read-only historical replay through the REAL configured goal judge (nine calls).

Run from a checkout using its Python environment. --prepare-only freezes inputs
without calling an LLM; --snapshot reuses those exact inputs without a live board.
Outputs include provenance and full judge results, not invented success verdicts.
Exit 1 means the requested 4/4 + null/negative-control thresholds were NOT met;
exit 2 means transport/parse failure (never scored as a successful null control).
No board edits, configuration overrides, judge mocks, or synthetic child gates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hermes_cli import kanban_db as kb

CASES = {
    # The board does not persist rejected complete payloads. These durable worker
    # comments preserve their rejected handoffs; use them transparently, not as
    # an assertion that task_events contained bytes it never recorded.
    't_81a82501': ('profiles/hive/cache/scratch/t_81a82501.body.bak', 19652, 872546),
    't_749a7410': ('backup-quarantine/t_4c7bbb0c/orig_749.md', 17896, 831506),
    't_03d596d2': ('profiles/hive/kanban-apply/t_4591d5ca/t_03d596d2.body.orig', 17933, 832212),
    # No body edit in its event history. The actual worker_result is available.
    't_49f37006': (None, None, 830174),
}


def historical_status(conn, task_id, cutoff):
    """Reconstruct relevant statuses from recorded events, not today's status."""
    status = None
    for event in conn.execute('SELECT kind, payload FROM task_events WHERE task_id=? AND created_at<=? ORDER BY id', (task_id, cutoff)):
        payload = json.loads(event['payload'] or '{}')
        kind = event['kind']
        if kind == 'created':
            status = payload.get('status')
        elif kind in {'completed', 'archived', 'blocked'}:
            status = {'completed': 'done', 'archived': 'archived', 'blocked': 'blocked'}[kind]
        elif kind in {'claimed', 'spawned'}:
            status = 'running'
        elif kind == 'promoted':
            status = 'ready'
        elif kind == 'dependency_wait':
            status = 'todo'
        elif kind == 'review_requested':
            status = 'review'
        elif kind in {'crashed', 'gave_up', 'timed_out'}:
            status = payload.get('retry_status', status)
    return status


def prepare(db, home):
    cases = []
    with sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        for tid, (body_path, comment_id, block_id) in CASES.items():
            task = conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
            block = conn.execute('SELECT * FROM task_events WHERE id=? AND task_id=?', (block_id, tid)).fetchone()
            if not task or not block:
                raise ValueError(f'{tid}: historical task/block missing')
            if body_path:
                path = home / body_path
                body = path.read_text()
                body_source = str(path)
            else:
                edits = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='edited'", (tid,)).fetchall()
                if any('body' in json.loads(e['payload'] or '{}').get('fields', []) for e in edits):
                    raise ValueError(f'{tid}: body changed; an original-body backup is required')
                body = task['body']
                body_source = 'tasks.body (no body edits recorded)'
            if comment_id:
                comment = conn.execute('SELECT * FROM task_comments WHERE id=? AND task_id=?', (comment_id, tid)).fetchone()
                if not comment:
                    raise ValueError(f'{tid}: rejected-handoff comment missing')
                evidence = comment['body']
                evidence_source = f'task_comments:{comment_id} (rejected payload not persisted in task_events)'
            else:
                event = conn.execute("SELECT * FROM task_events WHERE task_id=? AND run_id=? AND kind='worker_result' ORDER BY id LIMIT 1", (tid, block['run_id'])).fetchone()
                evidence = json.loads(event['payload'])['last_output']
                evidence_source = f'task_events:{event["id"]}.last_output'
            # Replay existing direct links; exclude children created after the block.
            children = []
            for row in conn.execute('SELECT t.* FROM tasks t JOIN task_links l ON l.child_id=t.id WHERE l.parent_id=? ORDER BY t.id', (tid,)):
                status = historical_status(conn, row['id'], block['created_at'])
                if status is not None:
                    children.append({'id': row['id'], 'assignee': row['assignee'], 'title': row['title'], 'status': status})
            with sqlite3.connect(':memory:') as graph:
                graph.row_factory = sqlite3.Row
                graph.executescript('CREATE TABLE tasks (id TEXT, assignee TEXT, title TEXT, status TEXT); CREATE TABLE task_links (parent_id TEXT, child_id TEXT);')
                for child in children:
                    graph.execute('INSERT INTO tasks VALUES (:id,:assignee,:title,:status)', child)
                    graph.execute('INSERT INTO task_links VALUES (?,?)', (tid, child['id']))
                context = kb.delegated_gate_context(graph, tid)
            cases.append(dict(id=tid, title=task['title'], body=body, body_source=body_source,
                              body_sha256=hashlib.sha256(body.encode()).hexdigest(),
                              evidence=evidence, evidence_source=evidence_source,
                              rejection=dict(block), children=children, delegated_gates=context))
    return cases


def evaluate(cases, timeout):
    from hermes_cli.goals import judge_goal
    from agent.portal_tags import set_affinity_scope, reset_affinity_scope
    rows = []
    for case in cases:
        row = {'id': case['id'], 'waiting_children': sum(c['status'] in {'todo', 'ready', 'blocked'} for c in case['children'])}
        token = set_affinity_scope('goal-gate-replay:' + case['id'])
        try:
            for mode in ['without', 'with']:
                kwargs = {'delegated_gates': case['delegated_gates']} if mode == 'with' else {}
                verdict, reason, parse_failed, _wait, transport_failed = judge_goal(
                    case['title'] + '\n\n' + case['body'], case['evidence'], timeout=timeout, **kwargs)
                row[mode] = dict(verdict=verdict, reason=reason, parse_failed=parse_failed, transport_failed=transport_failed)
                print(f'{case["id"]} {mode}: {verdict} — {reason}', flush=True)
        finally:
            reset_affinity_scope(token)
        rows.append(row)
    # Deliberately incomplete evidence, original release body, NO child scope.
    verdict, reason, parse_failed, _, transport_failed = judge_goal(
        cases[0]['title'] + '\n\n' + cases[0]['body'],
        'Implementation is not done. No files were changed, no PR was opened, no build or tests were run. I still need to start.',
        timeout=timeout)
    negative = dict(verdict=verdict, reason=reason, parse_failed=parse_failed, transport_failed=transport_failed)
    return rows, negative


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=os.environ.get('HERMES_KANBAN_DB'))
    parser.add_argument('--home', type=Path, default=Path.home() / '.hermes', help='Root containing the original-body backups')
    parser.add_argument('--snapshot', type=Path, help='Previously frozen input JSON (no board access)')
    parser.add_argument('--output', type=Path, required=True, help='Full provenance/results JSON; sibling .md contains table')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--timeout', type=float, default=60)
    args = parser.parse_args()
    if args.snapshot:
        cases = json.loads(args.snapshot.read_text())['cases']
    else:
        if args.db is None:
            parser.error('--db or HERMES_KANBAN_DB is required without --snapshot')
        cases = prepare(args.db, args.home)
    result = {'cases': cases}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    if args.prepare_only:
        for case in cases:
            print(case['id'], 'children:', [(c['id'], c['status']) for c in case['children']], 'evidence:', case['evidence_source'])
        return 0
    rows, negative = evaluate(cases, args.timeout)
    result.update(results=rows, negative_control=negative)
    failed = any(r[m]['parse_failed'] or r[m]['transport_failed'] for r in rows for m in ['with', 'without']) or negative['parse_failed'] or negative['transport_failed']
    passed = not failed and all(r['with']['verdict'] == 'done' for r in rows) and sum(r['without']['verdict'] != 'done' for r in rows) >= 3 and negative['verdict'] == 'continue'
    result['acceptance_passed'] = passed
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    table = '| Case | Waiting children | Without scope | With scope |\n|---|---:|---|---|\n'
    for row in rows:
        table += f'| {row["id"]} | {row["waiting_children"]} | {row["without"]["verdict"]} | {row["with"]["verdict"]} |\n'
    table += f'| Negative control (no children, unfinished implementation) | 0 | {negative["verdict"]} | — |\n'
    table += f'\nRequested acceptance thresholds: {"PASS" if passed else "NOT MET"}. Transport/parse failure: {failed}.\n'
    args.output.with_suffix('.md').write_text(table)
    print(table)
    return 2 if failed else (0 if passed else 1)


if __name__ == '__main__':
    raise SystemExit(main())
