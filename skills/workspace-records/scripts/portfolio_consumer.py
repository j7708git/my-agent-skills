"""Consume queued portfolio events on demand; never schedule or poll a model.

The existing Runner/post-checkpoint adapter queues a validated event first.
Concurrent event invocations wait on a separate OS-released consumer lock and
freshly recheck the queue. Unknown transport results remain in the outbox.
"""
import argparse
import json
import pathlib
import sys
import time

from core import RecordError
import git_store
import portfolio_refresh as refresh
import routing
from codex_portfolio_transport import CodexSession, consume_once

CLIENT_CONFIG = pathlib.Path('.openchamber/workspace-records/portfolio-client.json')


def private_child(parent, name):
    parent = git_store.safe_dir(parent)
    path = git_store.canonical_child(parent, name, allow_missing=True)
    if not path.exists():
        path.mkdir(mode=0o700)
    return git_store.safe_dir(path)


def consume_events(outbox, session_factory, lock_timeout=600, max_batches=8):
    if type(max_batches) is not int or not 1 <= max_batches <= 32 or type(lock_timeout) not in (int, float) or not 0 <= lock_timeout <= 900:
        raise RecordError('INVALID_ARGUMENT')
    gate = private_child(outbox.directory, 'consumer-gate')
    deadline = time.monotonic() + lock_timeout
    # This wait happens only after a real event invocation, and never starts AI.
    while True:
        lock = git_store.locked(gate)
        try:
            lock.__enter__()
            break
        except RecordError as error:
            if error.code != 'IN_FLIGHT':
                raise
            if time.monotonic() >= deadline:
                return {'outcome': 'in_flight', 'model_turns': 0, 'events_preserved': True}
            time.sleep(0.25)
    batches, turns = [], 0
    try:
        for _ in range(max_batches):
            result = refresh.consume_queued(outbox, lambda box: consume_once(box, session_factory))
            turns += result.get('model_turns', 0)
            if result.get('outcome') == 'idle':
                return {'outcome': 'drained' if batches else 'idle', 'model_turns': turns, 'batches': batches}
            batches.append(result)
            if result.get('outcome') not in ('confirmed', 'no_change'):
                return {'outcome': result.get('outcome', 'error'), 'model_turns': turns, 'batches': batches, 'events_preserved': True}
        return {'outcome': 'backlog_pending', 'model_turns': turns, 'batches': batches, 'events_preserved': True}
    finally:
        lock.__exit__(None, None, None)


def managed_client(workspace):
    raw = routing.private_file(workspace, CLIENT_CONFIG).read_bytes()
    value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=routing.unique_object)
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'client', 'model', 'sheet_title', 'timeout_seconds'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1 or value['client'] != 'codex_apps' or value['model'] != 'gpt-6.1-sol'
            or not isinstance(value['sheet_title'], str) or not value['sheet_title'] or len(value['sheet_title']) > 100
            or type(value['timeout_seconds']) is not int or not 60 <= value['timeout_seconds'] <= 600):
        raise RecordError('FORBIDDEN')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--managed', action='store_true', required=True)
    args = parser.parse_args()
    try:
        workspace = routing.discover_workspace()
        outbox = refresh.managed_outbox()
        client = managed_client(workspace)
        def factory():
            directory = private_child(outbox.directory, 'consumer-session')
            return CodexSession(directory, client['sheet_title'], allow_write=True, timeout=client['timeout_seconds'])
        result = consume_events(outbox, factory)
        print(json.dumps({'ok': True, 'schema_version': 1, 'data': result}, ensure_ascii=True))
        return 2 if result['outcome'] in ('error', 'manual_review', 'blocked', 'backlog_pending') else 0
    except (RecordError, OSError, ValueError, TypeError, KeyError) as error:
        code = error.code if isinstance(error, RecordError) else 'CONTROL_UNAVAILABLE'
        print(json.dumps({'ok': False, 'schema_version': 1, 'error': {'code': code, 'retryable': False}}, ensure_ascii=True))
        return 2


if __name__ == '__main__':
    sys.exit(main())
