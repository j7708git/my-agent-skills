"""合作式 Runner gate；adapter 負責驗證外部身分，本模組不提供身份認證。

Registry 必須已由管理者部署；未配置不自動推定全部 Git。Google 僅接受 canonical reader 在同一已驗 runtime 重新讀取的工具證據；
CLI 不接收 caller 填入的 authority_state。CLI 只記 durable claim/receipt，
不啟動 client，不發通知。pending 包含 crash/unknown，禁止自動重跑。
"""
import argparse
import json
import os
import sqlite3
import stat
import uuid
import sys

from core import RecordError, canonical, ident, object_hash
import git_store
import routing


def validate_event(event):
    required = {'project_id', 'event_id', 'action', 'owner_verified', 'ready_verified'}
    if not isinstance(event, dict) or set(event) != required:
        raise RecordError('INVALID_ARGUMENT')
    ident(event['project_id'])
    ident(event['event_id'])
    if event['action'] not in ('patrol', 'execute'):
        raise RecordError('INVALID_ARGUMENT')
    # These are adapter observations, not independently authenticated credentials.
    if event['owner_verified'] is not True or (event['action'] == 'execute' and event['ready_verified'] is not True):
        raise RecordError('FORBIDDEN')
    if type(event['ready_verified']) is not bool:
        raise RecordError('INVALID_ARGUMENT')
    return event


def event_key(event):
    validate_event(event)
    return object_hash({'project_id': event['project_id'], 'event_id': event['event_id'], 'action': event['action']})


def prepare(registry, event, google_reader=None):
    validate_event(event)
    binding = registry.bind(event['project_id'])
    proof = None
    if binding['backend'] == 'google_head':
        from canonical_writer import CanonicalReader
        if not isinstance(google_reader, CanonicalReader):
            raise RecordError('CONNECTOR_UNAVAILABLE')
        registry.require_no_activation(event['project_id'])
        nonce = uuid.uuid4().hex
        fresh = google_reader.read_authority(binding, nonce)
        if fresh['nonce'] != nonce:
            raise RecordError('READBACK_UNVERIFIED')
        observed = registry.check(binding, fresh['authority'])
        observed['head_hash'] = object_hash(fresh['snapshot']['head'])
        observed['docs_revision'] = fresh['snapshot']['revision']
        proof = fresh['proof']
    else:
        observed = registry.check(binding)
    result = {'event': event, 'binding': binding, 'observed': observed,
              'key': event_key(event)}
    if proof is not None:
        result['observation_proof'] = proof
    return result


def business_prepared(value):
    return {key: item for key, item in value.items() if key != 'observation_proof'}


def database(registry):
    root = git_store.safe_dir(registry.workspace / routing.RUNTIME)
    path = git_store.canonical_child(root, 'dispatch-ledger.sqlite3', allow_missing=True)
    # SQLite auxiliary paths must not point through a pre-existing symlink either.
    for suffix in ('', '-journal', '-wal', '-shm'):
        candidate = git_store.canonical_child(root, path.name + suffix, allow_missing=True)
        if os.path.lexists(candidate):
            info = os.lstat(candidate)
            if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & git_store.REPARSE:
                raise RecordError('FORBIDDEN')
    con = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    con.execute('PRAGMA synchronous=FULL')
    con.execute('CREATE TABLE IF NOT EXISTS events (key TEXT PRIMARY KEY, payload BLOB NOT NULL, state TEXT NOT NULL, result TEXT)')
    return con


def peek(registry, event):
    """Read an existing delivery receipt without re-launching a Google session.

    Caller still verifies the external event and fresh human gate. This read
    cannot start or finish work and never turns an unknown claim into complete.
    """
    validate_event(event)
    registry.bind(event['project_id'])
    key = event_key(event)
    con = database(registry)
    try:
        row = con.execute('SELECT payload,state,result FROM events WHERE key=?', (key,)).fetchone()
        if row is None:
            return None
        if json.loads(bytes(row[0])).get('event') != event:
            raise RecordError('ID_REUSE')
        return {'launch': False, 'state': row[1], 'key': key, 'result': row[2]}
    finally:
        con.close()


def claim(registry, prepared, google_reader=None):
    if not isinstance(prepared, dict):
        raise RecordError('INVALID_ARGUMENT')
    fresh = prepare(registry, prepared.get('event'), google_reader)
    if business_prepared(fresh) != business_prepared(prepared):
        raise RecordError('REVISION_CONFLICT')
    if fresh['binding']['backend'] == 'google_head':
        if fresh.get('observation_proof', {}).get('action_id') == prepared.get('observation_proof', {}).get('action_id'):
            raise RecordError('READBACK_UNVERIFIED')
    con = database(registry)
    try:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT payload,state,result FROM events WHERE key=?', (fresh['key'],)).fetchone()
        if row:
            # Binding changes cannot quietly turn an earlier event into a new job.
            if business_prepared(json.loads(bytes(row[0]))) != business_prepared(fresh):
                raise RecordError('ID_REUSE')
            con.execute('COMMIT')
            return {'launch': False, 'state': row[1], 'key': fresh['key'], 'result': row[2]}
        con.execute('INSERT INTO events VALUES (?,?,?,NULL)', (fresh['key'], canonical(fresh), 'pending'))
        con.execute('COMMIT')
        return {'launch': True, 'state': 'pending', 'key': fresh['key']}
    finally:
        con.close()


def finish(registry, key, outcome):
    ident(key)
    if outcome not in ('complete', 'unknown'):
        raise RecordError('INVALID_ARGUMENT')
    con = database(registry)
    try:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT state FROM events WHERE key=?', (key,)).fetchone()
        if not row:
            raise RecordError('NOT_FOUND')
        if row[0] != 'pending':
            raise RecordError('REVISION_CONFLICT')
        con.execute('UPDATE events SET state=?,result=? WHERE key=?', (outcome, outcome, key))
        con.execute('COMMIT')
        return {'state': outcome, 'key': key}
    finally:
        con.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'claim', 'finish'))
    args = parser.parse_args()
    try:
        request = json.load(sys.stdin, object_pairs_hook=routing.unique_object)
        registry = routing.Registry(routing.discover_workspace())
        if args.command == 'prepare':
            result = prepare(registry, request)
        elif args.command == 'claim':
            result = claim(registry, request)
        else:
            if not isinstance(request, dict) or set(request) != {'key', 'outcome'}:
                raise RecordError('INVALID_ARGUMENT')
            result = finish(registry, request['key'], request['outcome'])
        print(json.dumps({'ok': True, 'data': result}, ensure_ascii=True))
        return 0
    except (RecordError, OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        code = error.code if isinstance(error, RecordError) else 'CONTROL_UNAVAILABLE'
        print(json.dumps({'ok': False, 'error': {'code': code, 'retryable': False}}))
        return 2


if __name__ == '__main__':
    sys.exit(main())
