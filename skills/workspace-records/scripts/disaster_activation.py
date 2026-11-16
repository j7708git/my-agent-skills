"""Disaster activation candidate: isolated Windows cooperative-writer boundary."""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
import pathlib
import subprocess
import shutil
import sys
import uuid
import queue
import threading
import base64
import core
import disaster_recovery
import git_store
import promotion
import restore
import routing
import snapshot
import transition
from core import RecordError, object_hash, sha



def package_hash():
    scripts = pathlib.Path(__file__).resolve().parent
    package = scripts.parents[2]
    names = ('disaster_activation.py', 'disaster_recovery.py', 'transition.py',
             'routing.py', 'workspace_records.py', 'connector_protocol.py',
             'route_publish.py', 'promotion.py', 'restore.py', 'core.py',
             'git_store.py', 'privacy.py', 'snapshot.py', 'manifest.py', 'google_docs.py')
    items = {name: sha((scripts / name).read_bytes()) for name in names}
    audit = package / 'skills/repo-privacy-audit/scripts/audit_core.py'
    items['audit_core.py'] = sha(audit.read_bytes())
    return object_hash(items)

def decision_template(x, registry):
    if x.get('actor_role') != 'pm':
        raise RecordError('FORBIDDEN')
    assessment = disaster_recovery.assess(x)
    binding = registry.bind(x['project_id'])
    selected = assessment['assessment']['selected']
    pids = x['writer_pids']
    if not isinstance(pids, list) or not pids or any(type(pid) is not int or pid <= 0 for pid in pids) or len(set(pids)) != len(pids):
        raise RecordError('INVALID_ARGUMENT')
    if (binding['backend'] != 'google_head' or binding['route_phase'] != 'frozen'
            or binding['locator']['document_id'] != x['document_id']):
        raise RecordError('REVISION_CONFLICT')
    return {'assessment_hash': assessment['assessment_hash'],
            'target_project': x['project_id'], 'snapshot_id': selected['snapshot_id'], 'receipt_hash': selected['receipt_hash'],
            'source_epoch': selected['epoch'], 'source_version': selected['version'],
            'completed_at': selected['completed_at'], 'future_gap': 'unknown',
            'pending_work': 'unknown', 'remote_writers': 'unknown_no_reconnect',
            'target_scope': 'isolated_synthetic_only',
            'binding_hash': object_hash(binding), 'registry_generation': binding['registry_generation'],
            'package_sha256': package_hash(),
            'privacy_policy_sha256': sha(pathlib.Path(binding['privacy_policy']).read_bytes()),
            'gitleaks_rules_sha256': sha((pathlib.Path(__file__).resolve().parents[3] / '.gitleaks.toml').read_bytes()),
            'target_dir': binding['project_directory'], 'runtime_dir': binding['runtime_directory'],
            'writer_pids': x['writer_pids'], 'accepted_by_user': False}


def validate_decision(x, registry):
    expected = decision_template(x, registry)
    expected['accepted_by_user'] = True
    if x.get('decision') != expected:
        raise RecordError('FORBIDDEN')
    pids = expected['writer_pids']
    if not isinstance(pids, list) or any(type(pid) is not int or pid <= 0 for pid in pids) or len(set(pids)) != len(pids):
        raise RecordError('INVALID_ARGUMENT')
    return expected


def writer_stopped(pid):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:
            return True
        raise RecordError('MANUAL_REVIEW')
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise RecordError('MANUAL_REVIEW')
        return code.value != 259
    finally:
        kernel.CloseHandle(handle)


def check_writers(decision):
    if any(not writer_stopped(pid) for pid in decision['writer_pids']):
        raise RecordError('IN_FLIGHT')


def verify_material(x, registry, decision):
    binding = registry.bind(x['project_id'])
    if binding['project_directory'] != str(git_store.safe_dir(x['target_dir'])) or binding['runtime_directory'] != str(git_store.safe_dir(x['runtime_dir'])):
        raise RecordError('FORBIDDEN')
    root = git_store.safe_dir(x['private_dir'])
    directory, state, unused = restore.material(root, decision['snapshot_id'])
    plan = git_store.read_json(directory / ('runtime-preview-' + x['runtime_plan_hash'] + '.json'))
    if object_hash(plan) != x['runtime_plan_hash'] or plan['snapshot_receipt_hash'] != decision['receipt_hash']:
        raise RecordError('INTEGRITY_FAILED')
    audit = plan.get('disaster', {})
    if (audit.get('assessment_hash') != decision['assessment_hash'] or audit.get('target_epoch') != decision['source_epoch'] + 1
            or audit.get('epoch_order_against_unavailable_source') != 'unknown'):
        raise RecordError('INTEGRITY_FAILED')
    if plan['runtime_dir'] != binding['runtime_directory'] or plan['control']['phase'] != 'frozen':
        raise RecordError('PHASE_FROZEN')
    promotion.verify_project(root, decision['snapshot_id'], binding, x['project_id'])
    expected = {'control.json': core.canonical(plan['control']),
                'record-metadata/records.json': core.canonical(plan['metadata_catalog'])}
    for entry in plan['journal']:
        name = git_store.operation_path(pathlib.Path(binding['runtime_directory']), entry['operation_id']).name
        if name in expected:
            raise RecordError('INTEGRITY_FAILED')
        expected[name] = core.canonical(entry)
    if promotion.canonical_files(pathlib.Path(binding['runtime_directory'])) != expected:
        raise RecordError('REVISION_CONFLICT')
    return binding, plan



def session_path(x, registry):
    operation = core.ident(x['operation_id'])
    parent = registry.workspace / '.openchamber/workspace-records/disaster-activation'
    if not os.path.lexists(parent):
        parent.mkdir(mode=0o700)
    parent = git_store.safe_dir(parent)
    return parent / (sha(operation.encode()) + '.json')


def recover_args(x, registry, decision):
    binding = registry.bind(x['project_id'])
    consent = {'assessment_hash': decision['assessment_hash'], 'accept_unknown_gap': True,
               'acknowledge_pending_work_unknown': True}
    return {**x, 'assessment_hash': decision['assessment_hash'], 'decision': consent,
            'known_unresolved_writers': [], 'approved_root': str(registry.workspace),
            'target_dir': binding['project_directory'], 'runtime_dir': binding['runtime_directory']}



def barrier_path(registry, project):
    core.ident(project)
    parent = registry.workspace / '.openchamber/workspace-records/activation-barriers'
    if not os.path.lexists(parent):
        parent.mkdir(mode=0o700)
    parent = git_store.safe_dir(parent)
    return parent / (sha(project.encode('utf-8')) + '.json')


def verify_barrier(registry, project, plan_hash):
    path = barrier_path(registry, project)
    value = git_store.read_json(path)
    if value != {'schema_version': 1, 'project_id': project, 'plan_hash': plan_hash}:
        raise RecordError('INTEGRITY_FAILED')
    return path

def prepare(x, registry):
    if sys.platform != 'win32':
        raise RecordError('FORBIDDEN')
    decision = validate_decision(x, registry)
    check_writers(decision)
    binding = registry.bind(x['project_id'])
    recovery = recover_args(x, registry, decision)
    previews = disaster_recovery.prepare(recovery)
    plan = {'schema_version': 1, 'purpose': 'isolated_disaster_recovery',
            'operation_id': core.ident(x['operation_id']), 'decision': decision,
            'binding': binding, 'private_dir': str(git_store.safe_dir(x['private_dir'])),
            'restore_plan_hash': previews['restore']['plan_hash'],
            'runtime_plan_hash': previews['runtime']['plan_hash']}
    path = session_path(x, registry)
    state = {'plan': plan, 'plan_hash': object_hash(plan), 'phase': 'prepared'}
    barrier = barrier_path(registry, x['project_id'])
    lockroot = registry.workspace / '.openchamber/workspace-records'
    with git_store.locked(lockroot):
        if registry.bind(x['project_id']) != binding:
            raise RecordError('REVISION_CONFLICT')
        try:
            fd = os.open(barrier, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as out:
                out.write(core.canonical({'schema_version': 1, 'project_id': x['project_id'],
                                          'plan_hash': state['plan_hash']}))
                out.flush(); os.fsync(out.fileno())
        except FileExistsError:
            raise RecordError('PHASE_FROZEN') from None
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(core.canonical(state)); out.flush(); os.fsync(out.fileno())
    except FileExistsError:
        raise RecordError('ID_REUSE') from None
    return {'outcome': 'prepared', 'plan': plan, 'plan_hash': state['plan_hash'],
            'authority': 'not_activated'}


class FileInfo(ctypes.Structure):
    _fields_ = [('attributes', wintypes.DWORD), ('creation_low', wintypes.DWORD),
                ('creation_high', wintypes.DWORD), ('access_low', wintypes.DWORD),
                ('access_high', wintypes.DWORD), ('write_low', wintypes.DWORD),
                ('write_high', wintypes.DWORD), ('volume', wintypes.DWORD),
                ('size_high', wintypes.DWORD), ('size_low', wintypes.DWORD),
                ('links', wintypes.DWORD), ('index_high', wintypes.DWORD),
                ('index_low', wintypes.DWORD)]


def holder(path):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(FileInfo)]
    def open_anchor():
        h = kernel.CreateFileW(str(path), 0, 3, None, 3, 0x02000000, None)
        if h == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        return h
    def identity(h):
        info = FileInfo()
        if not kernel.GetFileInformationByHandle(h, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        return [info.volume, info.index_high, info.index_low]
    anchor = open_anchor()
    try:
        for line in sys.stdin:
            command = json.loads(line)
            if command['action'] == 'stop':
                return
            other = open_anchor()
            try:
                a, b = identity(anchor), identity(other)
            finally:
                kernel.CloseHandle(other)
            if a != b:
                raise RuntimeError('anchor path changed')
            print(json.dumps({'nonce': command['nonce'], 'pid': os.getpid(), 'identity': a}), flush=True)
    finally:
        kernel.CloseHandle(anchor)


def challenge(proc, original):
    if proc.poll() is not None:
        raise RecordError('MANUAL_REVIEW')
    nonce = uuid.uuid4().hex
    proc.stdin.write(json.dumps({'action': 'challenge', 'nonce': nonce}) + '\n')
    proc.stdin.flush()
    received = queue.Queue(maxsize=1)
    def read_one():
        received.put(proc.stdout.readline())
    threading.Thread(target=read_one, daemon=True).start()
    try:
        line = received.get(timeout=5)
    except queue.Empty:
        raise RecordError('MANUAL_REVIEW') from None
    if not line:
        raise RecordError('MANUAL_REVIEW')
    response = json.loads(line)
    if response.get('nonce') != nonce or response.get('pid') != proc.pid:
        raise RecordError('MANUAL_REVIEW')
    if original is not None and response.get('identity') != original:
        raise RecordError('MANUAL_REVIEW')
    return response['identity']


def publish(registry, project, before, backend, phase):
    actual, digest = registry.load()
    if digest != before['registry_sha256'] or actual['generation'] != before['registry_generation']:
        raise RecordError('REVISION_CONFLICT')
    after = dict(actual)
    after['projects'] = dict(actual['projects'])
    after['projects'][project] = {'classification': 'managed', 'backend': backend,
                                  'route_phase': phase, 'locator': None}
    after['generation'] += 1
    git_store.write_json(registry.workspace / routing.REGISTRY, after)
    observed, observed_sha = registry.load()
    if observed != after or observed_sha != sha(core.canonical(after)):
        raise RecordError('OUTCOME_UNKNOWN')
    return registry.bind(project)



def verify_resumed(steady, control, req, project_id):
    runtime_path = git_store.safe_dir(steady['runtime_directory'])
    observed_control = git_store.get_control(runtime_path)
    expected_control = {**control, 'phase': 'steady', 'version': control['version'] + 1}
    operation = git_store.read_operation(runtime_path, req['operation_id'])
    request_hash = core.request_hash(req)
    if (observed_control != expected_control or operation.get('state') != 'complete'
            or operation.get('request_hash') != request_hash
            or operation.get('event', {}).get('request_hash') != request_hash
            or operation.get('event', {}).get('version') != expected_control['version']
            or operation.get('event', {}).get('operation_id') != req['operation_id']
            or operation.get('result', {}).get('operation_id') != req['operation_id']
            or operation.get('result', {}).get('request_hash') != request_hash
            or operation.get('result', {}).get('authority', {}).get('logical_version') != expected_control['version']
            or operation.get('result', {}).get('authority', {}).get('readback_verified') is not True
            or operation.get('result', {}).get('epoch') != expected_control['epoch']
            or operation.get('result', {}).get('phase') != 'steady'):
        raise RecordError('INTEGRITY_FAILED')
    git_store.metadata_catalog(runtime_path, observed_control, project_id)

def apply(x, registry):
    if sys.platform != 'win32':
        raise RecordError('FORBIDDEN')
    path = session_path(x, registry)
    lockroot = registry.workspace / '.openchamber/workspace-records'
    with git_store.locked(path.parent):
        state = git_store.read_json(path)
        plan = state['plan']
        if (state['phase'] != 'prepared' or object_hash(plan) != x['plan_hash']
                or state['plan_hash'] != x['plan_hash']):
            raise RecordError('OUTCOME_UNKNOWN')
        verify_barrier(registry, x['project_id'], state['plan_hash'])
        decision = validate_decision(x, registry)
        if decision != plan['decision'] or registry.bind(x['project_id']) != plan['binding']:
            raise RecordError('REVISION_CONFLICT')
        check_writers(decision)
        proc = subprocess.Popen([sys.executable, '-B', __file__, '--holder', plan['binding']['project_directory']],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, creationflags=0x08000000)
        try:
            anchor = challenge(proc, None)
            state['phase'] = 'restoring_unknown'; git_store.write_json(path, state)
            recovery = recover_args(x, registry, decision)
            recovery.update(restore_plan_hash=plan['restore_plan_hash'], runtime_plan_hash=plan['runtime_plan_hash'])
            disaster_recovery.apply(recovery)
            challenge(proc, anchor); check_writers(decision)
            if validate_decision(x, registry) != decision:
                raise RecordError('REVISION_CONFLICT')
            verified_args = {**x, 'target_dir': plan['binding']['project_directory'],
                             'runtime_dir': plan['binding']['runtime_directory'],
                             'runtime_plan_hash': plan['runtime_plan_hash']}
            binding, runtime_plan = verify_material(verified_args, registry, decision)
            state['phase'] = 'target_frozen'; git_store.write_json(path, state)
            with git_store.locked(lockroot):
                challenge(proc, anchor); check_writers(decision)
                if disaster_recovery.assess(x)['assessment_hash'] != decision['assessment_hash']:
                    raise RecordError('REVISION_CONFLICT')
                state['phase'] = 'publish_git_frozen_unknown'; git_store.write_json(path, state)
                frozen = publish(registry, x['project_id'], binding, 'git_local', 'frozen')
                state['phase'] = 'git_frozen'; state['frozen_binding'] = frozen; git_store.write_json(path, state)
                challenge(proc, anchor); check_writers(decision)
                control = runtime_plan['control']
                req = {'schema_version': 1, 'request_id': plan['operation_id'], 'project_id': x['project_id'],
                       'actor_role': 'pm', 'scope': {'project_id': x['project_id'], 'actions': ['resume'], 'records': []},
                       'operation_id': plan['operation_id'], 'action': 'resume', 'target_record_id': None,
                       'payload': {'transition_id': plan['operation_id'], 'transition_intent_sha256': state['plan_hash']},
                       'expected': {k: control[k] for k in ('mode', 'phase', 'epoch', 'version')}}
                policy = {'gitleaks': 'gitleaks', 'policy': frozen['privacy_policy'],
                          'gitleaks_config': str(pathlib.Path(__file__).resolve().parents[3] / '.gitleaks.toml')}
                if (package_hash() != decision['package_sha256']
                        or sha(pathlib.Path(frozen['privacy_policy']).read_bytes()) != decision['privacy_policy_sha256']
                        or sha(pathlib.Path(policy['gitleaks_config']).read_bytes()) != decision['gitleaks_rules_sha256']):
                    raise RecordError('SCAN_INCOMPLETE')
                state['phase'] = 'resume_unknown'; git_store.write_json(path, state)
                transition.resume_git_operation(req, frozen, registry,
                                                {'request': req, 'disaster': {'plan_hash': state['plan_hash'],
                                                                              'assessment_hash': decision['assessment_hash']}}, policy)
                state['phase'] = 'git_resumed_route_frozen'; git_store.write_json(path, state)
                challenge(proc, anchor); check_writers(decision)
                if package_hash() != decision['package_sha256']:
                    raise RecordError('INTEGRITY_FAILED')
                state['phase'] = 'publish_steady_unknown'; git_store.write_json(path, state)
                steady = publish(registry, x['project_id'], frozen, 'git_local', 'steady')
                challenge(proc, anchor); check_writers(decision)
                if registry.bind(x['project_id']) != steady:
                    raise RecordError('REVISION_CONFLICT')
                verify_resumed(steady, control, req, x['project_id'])
                barrier = verify_barrier(registry, x['project_id'], state['plan_hash'])
                state['phase'] = 'complete_barrier_pending'; state['steady_binding'] = steady; git_store.write_json(path, state)
                os.unlink(barrier)
                state['phase'] = 'complete'; git_store.write_json(path, state)
            return {'outcome': 'complete', 'binding': steady, 'source_gap': 'unknown',
                    'remote_writers': 'unknown_no_reconnect', 'boundary': 'local_cooperative_only'}
        finally:
            if proc.poll() is None:
                try:
                    proc.stdin.write(json.dumps({'action': 'stop'}) + '\n'); proc.stdin.flush()
                    proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    proc.kill(); proc.wait()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()


def inspect(x, registry):
    state = git_store.read_json(session_path(x, registry))
    if state.get('plan_hash') != object_hash(state.get('plan')):
        raise RecordError('INTEGRITY_FAILED')
    observed_binding = None
    observed_control = None
    observation_error = None
    try:
        observed_binding = registry.bind(state['plan']['decision']['target_project'])
        runtime = pathlib.Path(observed_binding['runtime_directory'])
        if os.path.lexists(runtime):
            observed_control = git_store.get_control(runtime)
    except (RecordError, KeyError, OSError) as error:
        observation_error = error.code if isinstance(error, RecordError) else 'CONTROL_UNAVAILABLE'
    return {'phase': state['phase'], 'plan_hash': state['plan_hash'],
            'last_confirmed_binding': state.get('steady_binding', state.get('frozen_binding')),
            'observed_binding': observed_binding, 'observed_control': observed_control,
            'observation_error': observation_error,
            'barrier_present': os.path.lexists(barrier_path(registry, state['plan']['decision']['target_project'])),
            'unknown_requires_manual_reconciliation': state['phase'] != 'complete',
            'instruction': 'stop_writers_and_reconcile' if state['phase'] != 'complete' else None}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['template', 'dryrun', 'prepare', 'apply', 'inspect'])
    parser.add_argument('--input')
    args = parser.parse_args()
    try:
        with open(args.input, encoding='utf-8-sig') if args.input else sys.stdin as source:
            x = json.load(source)
        registry = routing.Registry(routing.discover_workspace())
        if args.command in ('template', 'dryrun'):
            decision = decision_template(x, registry)
            if args.command == 'template':
                result = {'outcome': 'unsigned_template', 'decision': decision}
            else:
                check_writers(decision)
                result = {'outcome': 'unsigned_dryrun', 'decision': decision,
                          'gitleaks_available': shutil.which('gitleaks') is not None,
                          'steps': ['restore_candidate', 'verify_material', 'publish_git_frozen',
                                    'managed_resume', 'publish_git_steady'],
                          'requires_explicit_user_decision': True,
                          'remote_exclusivity': 'not_proven', 'authority': 'not_activated'}
        else:
            result = {'prepare': prepare, 'apply': apply, 'inspect': inspect}[args.command](x, registry)
        print(json.dumps({'ok': True, 'schema_version': 1, 'data': result}, ensure_ascii=True))
        return 0
    except (RecordError, KeyError, ValueError, TypeError, OSError) as error:
        code = error.code if isinstance(error, RecordError) else 'INVALID_ARGUMENT'
        print(json.dumps({'ok': False, 'schema_version': 1, 'error': {'code': code, 'retryable': False}}))
        return 2

if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--holder':
        holder(pathlib.Path(sys.argv[2]))
    else:
        sys.exit(main())
