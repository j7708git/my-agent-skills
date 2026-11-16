"""固定工作區路由；只讀 binding 與準備轉換，不修改 registry 或啟用權威。"""
import argparse
import json
import os
import pathlib
import stat
import sys
from core import RecordError, canonical, ident, object_hash, sha
import git_store

REGISTRY = pathlib.Path('.openchamber/workspace-records/routes.json')
RUNTIME = pathlib.Path('.openchamber/workspace-records/runtime')
POLICY = pathlib.Path('.openchamber/repo-privacy-policy.json')
ROUTER = pathlib.Path('.openchamber/workspace-records/package/skills/workspace-records/scripts/routing.py')
PHASES = {'steady', 'frozen', 'importing', 'restoring'}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecordError('INTEGRITY_FAILED')
        result[key] = value
    return result


def private_file(workspace, relative):
    parent = git_store.safe_dir(workspace / relative.parent)
    path = git_store.canonical_child(parent, relative.name)
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & git_store.REPARSE:
        raise RecordError('FORBIDDEN')
    return path


class Registry:
    """workspace_root 是啟動時的 trusted locator，不來自每次 request。"""
    def __init__(self, workspace_root):
        self.workspace = git_store.safe_dir(workspace_root)

    def load(self):
        try:
            raw = private_file(self.workspace, REGISTRY).read_bytes()
            if len(raw) > 1024 * 1024:
                raise RecordError('INTEGRITY_FAILED')
            data = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object)
        except RecordError:
            raise
        except (OSError, UnicodeError, ValueError):
            raise RecordError('CONTROL_UNAVAILABLE') from None
        if not isinstance(data, dict) or set(data) != {'schema_version', 'policy_version', 'generation', 'projects'}:
            raise RecordError('INTEGRITY_FAILED')
        if type(data['schema_version']) is not int or data['schema_version'] != 1 or type(data['generation']) is not int or data['generation'] < 1:
            raise RecordError('INTEGRITY_FAILED')
        ident(data['policy_version'])
        if not isinstance(data['projects'], dict):
            raise RecordError('INTEGRITY_FAILED')
        seen = set()
        for project_id, entry in data['projects'].items():
            ident(project_id)
            if project_id.casefold() in seen:
                raise RecordError('INTEGRITY_FAILED')
            seen.add(project_id.casefold())
            if not isinstance(entry, dict) or set(entry) != {'classification', 'backend', 'route_phase', 'locator'}:
                raise RecordError('INTEGRITY_FAILED')
            if entry['classification'] not in ('unmigrated_git', 'managed') or entry['backend'] not in ('git_local', 'google_head') or entry['route_phase'] not in PHASES:
                raise RecordError('INTEGRITY_FAILED')
            if entry['classification'] == 'unmigrated_git' and entry['backend'] != 'git_local':
                raise RecordError('INTEGRITY_FAILED')
            if entry['backend'] == 'git_local':
                if entry['locator'] is not None:
                    raise RecordError('INTEGRITY_FAILED')
            elif not isinstance(entry['locator'], dict) or set(entry['locator']) != {'document_id'}:
                raise RecordError('INTEGRITY_FAILED')
            else:
                ident(entry['locator']['document_id'])
        return data, sha(raw)

    def bind(self, project_id):
        ident(project_id)
        registry, digest = self.load()
        if project_id not in registry['projects']:
            raise RecordError('NOT_REGISTERED')
        entry = registry['projects'][project_id]
        project = git_store.project_dir(self.workspace / 'project-notes', project_id)
        runtime_root = git_store.safe_dir(self.workspace / RUNTIME)
        # 與 P2 runtime identity 同源；bind 不建立目錄或初始化 control。
        runtime = runtime_root / sha((str(project.resolve()) + '\0' + project_id).encode())
        if os.path.lexists(runtime):
            git_store.safe_dir(runtime)
        policy = private_file(self.workspace, POLICY)
        binding = {
            'schema_version': 1, 'project_id': project_id,
            'registry_sha256': digest, 'registry_generation': registry['generation'],
            'policy_version': registry['policy_version'], 'classification': entry['classification'],
            'backend': entry['backend'], 'route_phase': entry['route_phase'],
            'locator': entry['locator'], 'project_directory': str(project),
            'runtime_directory': str(runtime), 'privacy_policy': str(policy),
            'authorization': 'location_only',
        }
        return binding

    def check(self, binding, authority_state=None):
        if not isinstance(binding, dict):
            raise RecordError('INVALID_ARGUMENT')
        fresh = self.bind(binding.get('project_id'))
        if fresh['route_phase'] != 'steady':
            raise RecordError('PHASE_FROZEN')
        if fresh != binding:
            raise RecordError('REVISION_CONFLICT')
        if fresh['classification'] == 'unmigrated_git' and authority_state is None and not os.path.lexists(fresh['runtime_directory']):
            return {'state': 'unmigrated_git', 'google_required': False, 'binding': fresh}
        if fresh['backend'] == 'git_local':
            control = git_store.get_control(pathlib.Path(fresh['runtime_directory']))
            expected_mode = 'git'
        else:
            if not isinstance(authority_state, dict) or set(authority_state) != {'document_id', 'revision_id', 'control'}:
                raise RecordError('CONTROL_UNAVAILABLE')
            if authority_state['document_id'] != fresh['locator']['document_id']:
                raise RecordError('FORBIDDEN')
            if not isinstance(authority_state['revision_id'], str) or not authority_state['revision_id']:
                raise RecordError('CONTROL_UNAVAILABLE')
            control = authority_state['control']
            expected_mode = 'google'
        if not isinstance(control, dict) or control.get('mode') != expected_mode:
            raise RecordError('STALE_MODE')
        if control.get('phase') != 'steady':
            raise RecordError('PHASE_FROZEN')
        if type(control.get('epoch')) is not int or control['epoch'] < 1 or type(control.get('version')) is not int or control['version'] < 0 or control.get('policy_version') != fresh['policy_version']:
            raise RecordError('INTEGRITY_FAILED')
        return {'state': 'authority_observed', 'binding': fresh, 'control': control}

    def prepare_transition(self, request):
        required = {'actor_role', 'project_id', 'target_manifest', 'snapshot_manifest_sha256', 'writer_manifest_sha256'}
        if not isinstance(request, dict) or set(request) != required or request['actor_role'] != 'pm':
            raise RecordError('FORBIDDEN')
        current = self.bind(request['project_id'])
        target = request['target_manifest']
        if not isinstance(target, dict) or set(target) != {'project_id', 'backend', 'locator'} or target['project_id'] != current['project_id'] or target['backend'] not in ('git_local', 'google_head'):
            raise RecordError('INVALID_ARGUMENT')
        if target['backend'] == 'git_local' and target['locator'] is not None:
            raise RecordError('INVALID_ARGUMENT')
        if target['backend'] == 'google_head':
            if not isinstance(target['locator'], dict) or set(target['locator']) != {'document_id'}:
                raise RecordError('INVALID_ARGUMENT')
            ident(target['locator']['document_id'])
        for field in ('snapshot_manifest_sha256', 'writer_manifest_sha256'):
            value = request[field]
            if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                raise RecordError('INVALID_ARGUMENT')
        proposal = {'expected_registry_sha256': current['registry_sha256'], **request}
        return {
            'outcome': 'prepared', 'proposal': proposal, 'proposal_sha256': object_hash(proposal),
            'activation_supported': False, 'proofs_verified': False,
            'required_stages': ['authority_freeze', 'route_freeze', 'drain', 'snapshot_and_target_readback', 'publish_route_frozen', 'resume_target', 'publish_route_steady'],
        }


def discover_workspace():
    source = pathlib.Path(__file__).resolve()
    try:
        workspace = source.parents[len(ROUTER.parts) - 1]
    except IndexError:
        raise RecordError('CONTROL_UNAVAILABLE') from None
    if source != workspace / ROUTER or not (workspace / 'project-notes').is_dir() or not (workspace / '.openchamber').is_dir():
        raise RecordError('CONTROL_UNAVAILABLE')
    return git_store.safe_dir(workspace)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['bind', 'check', 'prepare-transition'])
    parser.add_argument('--input')
    args = parser.parse_args()
    try:
        if args.input:
            with open(args.input, encoding='utf-8-sig') as source:
                request = json.load(source, object_pairs_hook=unique_object)
        else:
            request = json.load(sys.stdin, object_pairs_hook=unique_object)
        registry = Registry(discover_workspace())
        if args.command == 'bind':
            if not isinstance(request, dict) or set(request) != {'project_id'}:
                raise RecordError('INVALID_ARGUMENT')
            result = registry.bind(request['project_id'])
        elif args.command == 'check':
            if not isinstance(request, dict) or not {'binding'} <= set(request) or not set(request) <= {'binding', 'authority_state'}:
                raise RecordError('INVALID_ARGUMENT')
            result = registry.check(request['binding'], request.get('authority_state'))
        else:
            result = registry.prepare_transition(request)
        print(json.dumps({'ok': True, 'schema_version': 1, 'data': result}, ensure_ascii=True))
        return 0
    except (RecordError, OSError, ValueError, TypeError, KeyError) as error:
        code = error.code if isinstance(error, RecordError) else 'CONTROL_UNAVAILABLE'
        print(json.dumps({'ok': False, 'schema_version': 1, 'error': {'code': code, 'retryable': False}}, ensure_ascii=True))
        return 2


if __name__ == '__main__':
    sys.exit(main())
