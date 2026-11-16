"""Audited adoption of pushed Git bytes into an existing stable-ID catalog.

PM-only maintenance operation. Never writes project source files, initializes a
runtime, changes a route, replaces IDs, or drops metadata/history. The caller
owns external writer drain; local locks coordinate cooperating managed writers.
"""
import argparse
import copy
import json
import os
import pathlib
import re
import sys
import subprocess
from datetime import datetime, timezone

import core
import git_store
import privacy
import pushed_git_source as pushed
import routing
from core import RecordError, object_hash, sha

HEX = re.compile('[a-f0-9]{64}')


def now():
    return datetime.now(timezone.utc).isoformat()


def validate_request(request):
    if (not isinstance(request, dict) or set(request) != {'schema_version', 'project_id', 'operation_id', 'actor_role', 'expected', 'records'}
            or type(request['schema_version']) is not int or request['schema_version'] != 1 or request['actor_role'] != 'pm'
            or not isinstance(request['records'], list) or not request['records']):
        raise RecordError('FORBIDDEN')
    core.ident(request['project_id']); core.ident(request['operation_id'])
    expected = request['expected']
    if not isinstance(expected, dict) or set(expected) != {'registry_sha256', 'control_sha256', 'catalog_sha256', 'source_commit'}:
        raise RecordError('INVALID_ARGUMENT')
    if any(not isinstance(expected[key], str) or not HEX.fullmatch(expected[key]) for key in ('registry_sha256', 'control_sha256', 'catalog_sha256')):
        raise RecordError('INVALID_ARGUMENT')
    if not isinstance(expected['source_commit'], str) or not re.fullmatch('[a-f0-9]{40}|[a-f0-9]{64}', expected['source_commit']):
        raise RecordError('INVALID_ARGUMENT')
    paths, ids = set(), set()
    for item in request['records']:
        if not isinstance(item, dict) or set(item) != {'record_id', 'path', 'expected_catalog_content_sha256', 'current_content_sha256'}:
            raise RecordError('INVALID_ARGUMENT')
        core.ident(item['record_id']); git_store.relative_path(item['path'])
        if item['path'].casefold() in paths or item['record_id'] in ids:
            raise RecordError('INVALID_ARGUMENT')
        paths.add(item['path'].casefold()); ids.add(item['record_id'])
        for field in ('expected_catalog_content_sha256', 'current_content_sha256'):
            if not isinstance(item[field], str) or not HEX.fullmatch(item[field]):
                raise RecordError('INVALID_ARGUMENT')
        if item['expected_catalog_content_sha256'] == item['current_content_sha256']:
            raise RecordError('NO_CHANGES')
    return request


def environment(registry, project, own_operation=None):
    binding = registry.bind(project)
    if binding['backend'] != 'git_local' or binding['classification'] != 'managed':
        raise RecordError('FORBIDDEN')
    registry.check(binding)
    runtime = git_store.safe_dir(binding['runtime_directory'])
    control = git_store.get_control(runtime)
    if control.get('record_metadata') != 'required-v1':
        raise RecordError('CONTROL_UNAVAILABLE')
    entries = git_store.journal_entries(runtime)
    if any(entry.get('state') != 'complete' and entry.get('operation_id') != own_operation for entry in entries):
        raise RecordError('IN_FLIGHT')
    catalog = git_store.metadata_catalog(runtime, control, project)
    return binding, runtime, control, catalog


def source(registry, binding, catalog, commit):
    notes = git_store.safe_dir(registry.workspace / 'project-notes')
    project = git_store.safe_dir(binding['project_directory'])
    checkpoint = pushed.observe_pushed_checkpoint(notes)
    if checkpoint['source_commit'] != commit:
        raise RecordError('REVISION_CONFLICT')
    by_path = {item['source']['relative_path']: item for item in catalog['records']}
    attachments = {item['source_relative_path']: item for item in catalog['attachments']}
    paths = set(by_path) | set(attachments)
    request = {'schema_version': 1, 'request_id': 'metadata-source-read', 'project_id': binding['project_id'], 'actor_role': 'reader',
               'scope': {'project_id': binding['project_id'], 'actions': [], 'records': []}}
    if paths != set(git_store.inventory(project, request)['paths']):
        raise RecordError('SOURCE_SET_MISMATCH')
    prefix = 'projects/' + binding['project_id'] + '/'
    tracked = {path[len(prefix):] for path in pushed.git(notes, 'ls-tree', '-r', '-z', '--name-only', commit, '--', prefix).decode('utf-8').split('\0') if path}
    if paths != tracked:
        raise RecordError('SOURCE_SET_MISMATCH')
    git_store.ensure_unique_files(project, sorted(paths))
    proof = {}
    for relative in sorted(paths):
        path = git_store.target_path(project, relative)
        raw = path.read_bytes()
        if relative == 'status.md':
            core.status_patch(raw, {})
        if relative in by_path:
            try:
                raw.decode('utf-8')
            except UnicodeError:
                raise RecordError('INTEGRITY_FAILED') from None
        repo_path = prefix + relative
        attrs = pushed.git(notes, 'check-attr', '-z', 'filter', 'ident', 'working-tree-encoding', '--', repo_path).decode().split('\0')
        if any(attrs[index] not in ('unspecified', 'unset') for index in range(2, len(attrs) - 1, 3)):
            raise RecordError('FORBIDDEN')
        blob = pushed.git(notes, 'cat-file', 'blob', commit + ':' + repo_path)
        checkout = pushed.git(notes, 'cat-file', '--filters', commit + ':' + repo_path)
        digest = sha(raw)
        # Built-in clean handles a valid mixed-EOL worktree without changing its
        # original bytes. External filters/ident/encoding were rejected above.
        cleaned = subprocess.run(['git', '-C', str(notes), 'hash-object', '--path=' + repo_path, '--stdin'],
                                 input=raw, capture_output=True, timeout=30)
        blob_oid = pushed.git(notes, 'rev-parse', commit + ':' + repo_path).decode().strip()
        if cleaned.returncode or cleaned.stdout.decode().strip() != blob_oid:
            raise RecordError('PUSHED_SOURCE_DIRTY')
        proof[relative] = {'record_id': by_path[relative]['record_id'] if relative in by_path else attachments[relative]['attachment_id'],
                           'sha256': digest, 'size': len(raw), 'blob_sha256': sha(blob), 'checkout_sha256': sha(checkout),
                           'clean_git_oid': cleaned.stdout.decode().strip(), 'pushed_blob_git_oid': blob_oid}
    for relative, value in proof.items():
        if sha(git_store.target_path(project, relative).read_bytes()) != value['sha256']:
            raise RecordError('REVISION_CONFLICT')
    if pushed.observe_pushed_checkpoint(notes, checkpoint['repository']) != checkpoint:
        raise RecordError('REVISION_CONFLICT')
    if registry.bind(binding['project_id']) != binding:
        raise RecordError('REVISION_CONFLICT')
    return {'checkpoint': checkpoint, 'files': proof, 'files_sha256': object_hash(proof)}


def privacy_config(binding):
    return {'gitleaks': 'gitleaks', 'policy': binding['privacy_policy'],
            'gitleaks_config': str(pathlib.Path(__file__).resolve().parents[3] / '.gitleaks.toml')}


def scan_payload(request, catalog, proof):
    # Bind local paths separately; scan all actual metadata and relative scope.
    return {'project_id': request['project_id'], 'operation_id': request['operation_id'],
            'action': 'metadata_reconcile', 'source_commit': proof['checkpoint']['source_commit'],
            'changes': request['records'], 'candidate_catalog': catalog}


def scan(binding, request, catalog, proof):
    contents = {}
    for relative, expected in proof['files'].items():
        raw = git_store.target_path(pathlib.Path(binding['project_directory']), relative).read_bytes()
        if sha(raw) != expected['sha256']:
            raise RecordError('REVISION_CONFLICT')
        try:
            contents[relative] = raw.decode('utf-8')
        except UnicodeError:
            raise RecordError('SCAN_INCOMPLETE') from None
    return {'metadata': privacy.check(scan_payload(request, catalog, proof), privacy_config(binding)),
            'sources': privacy.check({'files': contents}, privacy_config(binding))}


def preview(registry, request):
    validate_request(request)
    binding, runtime, control, catalog = environment(registry, request['project_id'])
    expected = request['expected']
    if (binding['registry_sha256'] != expected['registry_sha256']
            or sha((runtime / 'control.json').read_bytes()) != expected['control_sha256']
            or sha((runtime / 'record-metadata/records.json').read_bytes()) != expected['catalog_sha256']):
        raise RecordError('REVISION_CONFLICT')
    if os.path.lexists(git_store.operation_path(runtime, request['operation_id'])):
        raise RecordError('ID_REUSE')
    proof = source(registry, binding, catalog, expected['source_commit'])
    selected = {item['path']: item for item in request['records']}
    updated = copy.deepcopy(catalog)
    changed = set()
    for item in updated['records']:
        relative = item['source']['relative_path']
        digest = proof['files'][relative]['sha256']
        old = item['source']['content_sha256']
        if old != digest:
            chosen = selected.get(relative)
            if (chosen is None or chosen['record_id'] != item['record_id']
                    or chosen['expected_catalog_content_sha256'] != old or chosen['current_content_sha256'] != digest):
                raise RecordError('FORBIDDEN')
            item['source']['content_sha256'] = digest
            changed.add(relative)
    for item in catalog['attachments']:
        actual = proof['files'][item['source_relative_path']]
        if actual['sha256'] != item['sha256'] or actual['size'] != item['size']:
            raise RecordError('FORBIDDEN')
    if changed != set(selected):
        raise RecordError('FORBIDDEN')
    scanner = scan(binding, request, updated, proof)
    plan = {'schema_version': 1, 'request': request, 'request_hash': object_hash(request), 'binding': binding,
            'before_control': control, 'after_control': {**control, 'version': control['version'] + 1},
            'before_catalog_hash': object_hash(catalog), 'after_catalog_hash': object_hash(updated),
            'after_catalog': updated, 'source_proof': proof, 'scanner': scanner}
    return {'outcome': 'preview', 'plan': plan, 'plan_hash': object_hash(plan),
            'source_files_written': False, 'changed_metadata_paths': sorted(changed)}


def plan_path(runtime, operation, create=False):
    core.ident(operation)
    directory = runtime / 'metadata-reconcile-plans'
    if create and not os.path.lexists(directory): directory.mkdir(mode=0o700)
    directory = git_store.safe_dir(directory)
    return directory / (sha(operation.encode()) + '.json')


def load_plan(runtime, operation):
    return git_store.read_json(plan_path(runtime, operation))


def load_entry(registry, project, operation):
    binding = registry.bind(project)
    runtime = git_store.safe_dir(binding['runtime_directory'])
    entry = git_store.read_operation(runtime, operation)
    if entry.get('action') != 'metadata_reconcile' or object_hash(load_plan(runtime, operation)) != entry.get('maintenance_plan_hash'):
        raise RecordError('INTEGRITY_FAILED')
    return binding, runtime, entry


def finish(registry, request, plan, plan_hash, recover=False):
    validate_request(request)
    if object_hash(plan) != plan_hash or plan.get('request') != request or plan.get('request_hash') != object_hash(request):
        raise RecordError('INTEGRITY_FAILED')
    binding = registry.bind(request['project_id'])
    runtime = git_store.safe_dir(binding['runtime_directory'])
    lockroot = git_store.safe_dir(registry.workspace / '.openchamber/workspace-records')
    with git_store.locked(lockroot), git_store.locked(runtime):
        path = git_store.operation_path(runtime, request['operation_id'])
        if os.path.lexists(path):
            _, _, entry = load_entry(registry, request['project_id'], request['operation_id'])
            if entry['request_hash'] != object_hash(request) or entry['maintenance_plan_hash'] != plan_hash:
                raise RecordError('ID_REUSE')
            if entry['state'] == 'complete':
                return {'outcome': 'no_write', 'result': entry['result'], 'freshness': 'historical_receipt'}
            if not recover:
                raise RecordError('MANUAL_REVIEW')
        else:
            if recover:
                raise RecordError('NOT_FOUND')
            current = preview(registry, request)
            if current['plan'] != plan or current['plan_hash'] != plan_hash:
                raise RecordError('REVISION_CONFLICT')
            local_plan = plan_path(runtime, request['operation_id'], create=True)
            if os.path.lexists(local_plan):
                if git_store.read_json(local_plan) != plan: raise RecordError('ID_REUSE')
            else:
                git_store.write_json(local_plan, plan)
            entry = {'schema_version': 1, 'operation_id': request['operation_id'], 'request_hash': object_hash(request),
                     'state': 'accepted', 'actor_role': 'pm', 'action': 'metadata_reconcile', 'target_record_id': None,
                     'scope': {'project_id': request['project_id'], 'actions': [], 'records': [
                         {'record_id': item['record_id'], 'path': item['path'], 'fields': ['source.content_sha256']} for item in request['records']]},
                     'files': [], 'expected_control': plan['before_control'],
                     'metadata_update': {'before_sha256': plan['before_catalog_hash'], 'after_sha256': plan['after_catalog_hash'], 'after': plan['after_catalog']},
                     'maintenance_plan_hash': plan_hash,
                     'event': {'operation_id': request['operation_id'], 'request_hash': object_hash(request),
                               'version': plan['after_control']['version'], 'action': 'metadata_reconcile',
                               'actor_role': 'pm', 'occurred_at': now(), 'source_commit': request['expected']['source_commit']}}
            git_store.write_json(path, entry)
        current_binding, _, control, catalog = environment(registry, request['project_id'], request['operation_id'])
        if current_binding != plan['binding']:
            raise RecordError('REVISION_CONFLICT')
        proof = source(registry, current_binding, catalog, request['expected']['source_commit'])
        if proof != plan['source_proof']:
            raise RecordError('REVISION_CONFLICT')
        current_hash = object_hash(catalog)
        if current_hash not in (plan['before_catalog_hash'], plan['after_catalog_hash']) or control not in (plan['before_control'], plan['after_control']):
            raise RecordError('REVISION_CONFLICT')
        if current_hash == plan['before_catalog_hash'] and control == plan['after_control']:
            raise RecordError('MANUAL_REVIEW')
        if scan(binding, request, plan['after_catalog'], proof) != plan['scanner']:
            raise RecordError('SCAN_INCOMPLETE')
        if current_hash == plan['before_catalog_hash']:
            git_store.write_json(runtime / 'record-metadata/records.json', plan['after_catalog'])
        entry['state'] = 'metadata_done'; git_store.write_json(path, entry)
        if control == plan['before_control']:
            git_store.write_json(runtime / 'control.json', plan['after_control'])
        entry['state'] = 'control_done'; git_store.write_json(path, entry)
        observed = git_store.metadata_catalog(runtime, git_store.get_control(runtime), request['project_id'])
        if object_hash(observed) != plan['after_catalog_hash'] or git_store.get_control(runtime) != plan['after_control']:
            raise RecordError('READBACK_UNVERIFIED')
        if source(registry, binding, observed, request['expected']['source_commit']) != plan['source_proof']:
            raise RecordError('REVISION_CONFLICT')
        scope = {'project_id': request['project_id'], 'actions': [], 'records': [
            {'record_id': item['record_id'], 'path': relative, 'fields': ['body']}
            for relative, item in plan['source_proof']['files'].items()]}
        read_request = {'schema_version': 1, 'request_id': 'metadata-readback', 'project_id': request['project_id'], 'actor_role': 'pm', 'scope': scope}
        readback = git_store._enumerate_records(pathlib.Path(binding['project_directory']), runtime, read_request)
        if (readback.get('metadata_stale') or readback['unselected_paths'] or readback['incomplete_operations'] != [request['operation_id']]):
            raise RecordError('READBACK_UNVERIFIED')
        entry['result'] = {'authority': {'state': 'complete', 'logical_version': plan['after_control']['version'],
                            'docs_revision': None, 'readback_verified': True},
                           'operation_id': request['operation_id'], 'request_hash': object_hash(request),
                           'epoch': plan['after_control']['epoch'], 'phase': 'steady', 'source_commit': request['expected']['source_commit'],
                           'catalog_sha256': plan['after_catalog_hash'], 'source_files_sha256': proof['files_sha256'], 'observed_at': now()}
        entry['state'] = 'complete'; git_store.write_json(path, entry)
        if git_store.read_operation(runtime, request['operation_id']) != entry:
            raise RecordError('READBACK_UNVERIFIED')
        return {'outcome': 'complete', 'result': entry['result'], 'source_files_written': False}


def inspect(registry, project, operation):
    core.ident(project); core.ident(operation)
    binding, runtime, entry = load_entry(registry, project, operation)
    plan = load_plan(runtime, operation)
    current_control = git_store.get_control(runtime)
    current_catalog = git_store.read_json(runtime / 'record-metadata/records.json')
    def state(value, before, after):
        return 'after' if value == after else 'before' if value == before else 'conflict'
    files = []
    for relative, expected in plan['source_proof']['files'].items():
        try:
            matches = sha(git_store.target_path(pathlib.Path(binding['project_directory']), relative).read_bytes()) == expected['sha256']
        except (RecordError, OSError):
            matches = False
        files.append({'path': relative, 'matches_source': matches})
    return {'outcome': 'inspection', 'journal_state': entry['state'], 'plan_hash': entry['maintenance_plan_hash'],
            'control': state(current_control, plan['before_control'], plan['after_control']),
            'catalog': state(object_hash(current_catalog), plan['before_catalog_hash'], plan['after_catalog_hash']),
            'files': files, 'automatic_retry': False, 'requires_recovery': entry['state'] != 'complete'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('preview', 'apply', 'inspect', 'recover'))
    parser.add_argument('--managed', required=True, action='store_true')
    parser.add_argument('--input')
    args = parser.parse_args()
    try:
        with open(args.input, encoding='utf-8-sig') if args.input else sys.stdin as stream:
            value = json.load(stream, object_pairs_hook=routing.unique_object)
        registry = routing.Registry(routing.discover_workspace())
        if args.command == 'preview':
            result = preview(registry, value['request'])
        elif args.command == 'inspect':
            result = inspect(registry, value['project_id'], value['operation_id'])
        else:
            result = finish(registry, value['request'], value['plan'], value['plan_hash'], args.command == 'recover')
        print(json.dumps({'ok': True, 'schema_version': 1, 'data': result}, ensure_ascii=True))
        return 0
    except (RecordError, OSError, ValueError, TypeError, KeyError) as error:
        code = error.code if isinstance(error, RecordError) else 'CONTROL_UNAVAILABLE'
        print(json.dumps({'ok': False, 'schema_version': 1, 'error': {'code': code, 'retryable': False}}, ensure_ascii=True))
        return 2


if __name__ == '__main__':
    sys.exit(main())
