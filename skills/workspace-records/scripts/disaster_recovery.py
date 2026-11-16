"""Offline disaster assessment and isolated, non-activated restoration."""
import argparse
import json
import os
import sys
import core
import git_store
import restore
import snapshot
from core import RecordError, object_hash, sha


def assess(x):
    root = git_store.safe_dir(x['private_dir'])
    parent = git_store.safe_dir(root / 'snapshots')
    project = core.ident(x['project_id'])
    document = core.ident(x['document_id'])
    candidates = []
    graphs = {}
    incomplete = 0
    for entry in os.scandir(parent):
        directory = git_store.safe_dir(parent / entry.name)
        state = git_store.read_json(directory / 'snapshot.json')
        sid = core.ident(state['snapshot_id'])
        if entry.name != sha(sid.encode('utf-8')):
            raise RecordError('INTEGRITY_FAILED')
        if state.get('project_id') != project:
            continue
        source = state.get('source', {})
        if source.get('document_id') != document:
            raise RecordError('MANUAL_REVIEW')
        if state.get('phase') != 'complete':
            incomplete += 1
            continue
        receipt = state['receipt']
        head = source['head']
        if (receipt['snapshot_id'] != sid or receipt['project_id'] != project
            or receipt['source_revision'] != source['revision']
            or receipt['source_epoch'] != head['control']['epoch']
            or receipt['source_version'] != head['version']
            or receipt['root_manifest'] != head['root_manifest']
            or receipt['completeness'] != 'complete'):
            raise RecordError('INTEGRITY_FAILED')
        refs, graph = snapshot.verify_graph(directory, state)
        if receipt['objects'] != [refs[k] for k in sorted(refs)] or receipt['manifests'] != graph:
            raise RecordError('INTEGRITY_FAILED')
        graphs[sid] = graph
        candidates.append({'snapshot_id': sid, 'receipt_hash': object_hash(receipt),
                           'epoch': receipt['source_epoch'], 'version': receipt['source_version'],
                           'root_manifest': receipt['root_manifest'], 'completed_at': receipt['completed_at']})
    if not candidates:
        raise RecordError('NOT_FOUND')
    epochs = {c['epoch'] for c in candidates}
    if len(epochs) != 1:
        raise RecordError('MANUAL_REVIEW')
    versions = {}
    for c in candidates:
        previous = versions.setdefault(c['version'], c['root_manifest'])
        if previous != c['root_manifest']:
            raise RecordError('INTEGRITY_FAILED')
    max_version = max(c['version'] for c in candidates)
    latest = [c for c in candidates if c['version'] == max_version]
    latest.sort(key=lambda c: c['snapshot_id'])
    latest_graph = graphs[latest[0]['snapshot_id']]
    if any(c['root_manifest'] not in latest_graph for c in candidates):
        raise RecordError('INTEGRITY_FAILED')
    result = {'schema_version': 1, 'project_id': project, 'document_id': document,
              'selected': latest[0], 'candidates': sorted(candidates, key=lambda c: c['snapshot_id']),
              'incomplete_count': incomplete, 'scope': 'local_snapshot_directory_only',
              'global_latest': 'unknown', 'future_gap': 'unknown', 'pending_work': 'unknown',
              'external_exclusivity': 'not_proven'}
    return {'assessment': result, 'assessment_hash': object_hash(result)}


def prepare(x):
    if x.get('known_unresolved_writers') != []:
        raise RecordError('MANUAL_REVIEW')
    assessment = assess(x)
    if x['assessment_hash'] != assessment['assessment_hash']:
        raise RecordError('REVISION_CONFLICT')
    decision = x['decision']
    if decision != {'assessment_hash': x['assessment_hash'], 'accept_unknown_gap': True,
                    'acknowledge_pending_work_unknown': True}:
        raise RecordError('FORBIDDEN')
    selected = assessment['assessment']['selected']
    args = {'private_dir': x['private_dir'], 'snapshot_id': selected['snapshot_id'],
            'approved_root': x['approved_root'], 'target_dir': x['target_dir'],
            'approved_existing': {}}
    result = restore.preview(args)
    audit = {'assessment_hash': x['assessment_hash'], 'decision_hash': object_hash(decision),
             'source_epoch': selected['epoch'], 'source_version': selected['version'],
             'target_epoch': selected['epoch'] + 1, 'future_gap': 'unknown',
             'epoch_order_against_unavailable_source': 'unknown',
             'external_exclusivity': 'not_proven'} 
    runtime = restore._runtime_preview({**args, 'runtime_dir': x['runtime_dir']}, audit)
    return {'outcome': 'candidate_preview', 'assessment': assessment['assessment'],
            'decision_hash': object_hash(decision), 'restore': result, 'runtime': runtime,
            'authority': 'not_activated', 'physical_fence_independently_verified': False}


def progress_path(x):
    root = git_store.safe_dir(x['private_dir'])
    parent = root / 'disaster-recovery'
    if not os.path.lexists(parent):
        parent.mkdir(mode=0o700)
    parent = git_store.safe_dir(parent)
    key = object_hash({'assessment_hash': x['assessment_hash'],
                       'target_dir': str(git_store.safe_dir(x['target_dir'])),
                       'runtime_dir': str(git_store.safe_dir(x['runtime_dir']))})
    return parent / (key + '.json')


def inspect(x):
    path = progress_path(x)
    state = git_store.read_json(path)
    target = git_store.safe_dir(state['target_dir'])
    files = []
    for item in state['files']:
        raw = restore.content(restore.destination(target, item['path']))
        current = None if raw is None else sha(raw)
        status = ('after' if current == item['after_sha256'] else
                  'before' if current == item['before_sha256'] else 'conflict')
        files.append({'path': item['path'], 'state': status, 'sha256': current})
    return {'outcome': state['phase'], 'files': files,
            'runtime': state['runtime'], 'authority': 'not_activated'}


def apply(x):
    path = progress_path(x)
    if os.path.lexists(path):
        raise RecordError('MANUAL_REVIEW')
    prepared = prepare(x)
    if (x['restore_plan_hash'] != prepared['restore']['plan_hash'] or
            x['runtime_plan_hash'] != prepared['runtime']['plan_hash']):
        raise RecordError('REVISION_CONFLICT')
    state = {'schema_version': 1, 'phase': 'pending',
             'target_dir': prepared['restore']['plan']['target_dir'],
             'files': prepared['restore']['plan']['files'], 'runtime': 'not_started',
             'assessment_hash': x['assessment_hash'],
             'restore_plan_hash': x['restore_plan_hash'],
             'runtime_plan_hash': x['runtime_plan_hash']}
    # Exclusive creation prevents a repeated operation from concealing partial work.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(core.canonical(state))
            out.flush()
            os.fsync(out.fileno())
    except FileExistsError:
        raise RecordError('MANUAL_REVIEW') from None
    try:
        file_result = restore.run({**x, **prepared['restore']})
        state['phase'] = 'files_done'
        git_store.write_json(path, state)
        if assess(x)['assessment_hash'] != x['assessment_hash']:
            raise RecordError('REVISION_CONFLICT')
        state['runtime'] = 'applying'
        git_store.write_json(path, state)
        runtime_result = restore.runtime_apply({**x, **prepared['runtime']})
        state['runtime'] = 'complete'
        state['phase'] = 'complete'
        git_store.write_json(path, state)
        return {'outcome': 'candidate_restored', 'files': file_result,
                'runtime': runtime_result, 'authority': 'not_activated',
                'external_exclusivity': 'not_proven'}
    except (RecordError, KeyError, ValueError, TypeError, OSError):
        state['phase'] = 'partial'
        git_store.write_json(path, state)
        raise

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['assess', 'prepare', 'apply', 'inspect'])
    parser.add_argument('--input')
    args = parser.parse_args()
    try:
        if args.input:
            with open(args.input, encoding='utf-8-sig') as source:
                data = json.load(source)
        else:
            data = json.load(sys.stdin)
        result = {'assess': assess, 'prepare': prepare, 'apply': apply, 'inspect': inspect}[args.command](data)
        print(json.dumps({'ok': True, 'schema_version': 1, 'data': result}, ensure_ascii=True))
        return 0
    except (RecordError, KeyError, ValueError, TypeError, OSError) as error:
        code = error.code if isinstance(error, RecordError) else 'INVALID_ARGUMENT'
        print(json.dumps({'ok': False, 'schema_version': 1, 'error': {'code': code, 'retryable': False}}))
        return 2


if __name__ == '__main__':
    sys.exit(main())





