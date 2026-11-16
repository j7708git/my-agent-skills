"""Existing Actions-to-managed-writer adapter; no legacy client or notifications.

The workflow/host configuration is trusted. Issue text and intake markers never
authenticate a person, choose a route, authorize a scope or claim completion.
"""
import argparse
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import uuid

import canonical_writer
import core
import dispatch_guard as guard
import git_store
import routing
import privacy
import workspace_records
from core import RecordError, object_hash, sha

MARKER_KEYS = {'schema_version', 'project_id', 'mapping_generation', 'source', 'intake_key', 'payload_sha256'}
CONFIG = pathlib.Path('.openchamber/workspace-records/runner.json')


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def marker_from_issue(issue):
    body = issue.get('body') or ''
    if not isinstance(body, str) or body.count('```workspace-intake-v2') != 1:
        raise RecordError('INTAKE_MARKER_INVALID')
    matched = re.search(r'```workspace-intake-v2\r?\n([^\r\n]+)\r?\n```', body)
    if not matched:
        raise RecordError('INTAKE_MARKER_INVALID')
    try:
        marker = json.loads(matched.group(1), object_pairs_hook=routing.unique_object)
    except ValueError:
        raise RecordError('INTAKE_MARKER_INVALID') from None
    if (not isinstance(marker, dict) or set(marker) != MARKER_KEYS or type(marker['schema_version']) is not int
            or marker['schema_version'] != 1 or marker['source'] != 'google_tasks'
            or type(marker['mapping_generation']) is not int or marker['mapping_generation'] < 1):
        raise RecordError('INTAKE_MARKER_INVALID')
    core.ident(marker['project_id'])
    for field in ('intake_key', 'payload_sha256'):
        if not isinstance(marker[field], str) or not re.fullmatch('[a-f0-9]{64}', marker[field]):
            raise RecordError('INTAKE_MARKER_INVALID')
    suffix = '\n\n---\n' + matched.group(0)
    if not body.endswith(suffix):
        raise RecordError('INTAKE_PAYLOAD_CHANGED')
    notes = body[:-len(suffix)]
    if notes == '*(無詳細備註)*':
        notes = ''
    payload = {'title': issue.get('title'), 'notes': notes}
    if not isinstance(payload['title'], str) or sha(compact(payload).encode('utf-8')) != marker['payload_sha256']:
        raise RecordError('INTAKE_PAYLOAD_CHANGED')
    return marker


class GitHubReader:
    def __init__(self, repository):
        if not isinstance(repository, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise RecordError('INVALID_ARGUMENT')
        self.repository = repository

    def get(self, suffix):
        if not re.fullmatch(r'(?:actions/runs/[1-9][0-9]*|issues/[1-9][0-9]*|issues/comments/[1-9][0-9]*)', suffix):
            raise RecordError('FORBIDDEN')
        result = subprocess.run(['gh', 'api', 'repos/' + self.repository + '/' + suffix, '--method', 'GET'],
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        if result.returncode:
            raise RecordError('GITHUB_UNAVAILABLE')
        return json.loads(result.stdout, object_pairs_hook=routing.unique_object)


def trusted_event(config, context, payload, github, registry=None):
    required_context = {'actions', 'repository', 'repository_owner', 'actor', 'event_name', 'run_id', 'run_attempt', 'sha'}
    if set(context) != required_context or context['actions'] != 'true':
        raise RecordError('ACTIONS_CONTEXT_REQUIRED')
    if (context['repository'] != config['repository'] or context['repository_owner'] != config['owner']
            or context['actor'].casefold() != config['owner'].casefold()
            or not re.fullmatch('[1-9][0-9]*', context['run_id']) or not re.fullmatch('[1-9][0-9]*', context['run_attempt'])):
        raise RecordError('FORBIDDEN')
    run = github.get('actions/runs/' + context['run_id'])
    if (str(run.get('id')) != context['run_id'] or str(run.get('run_attempt')) != context['run_attempt']
            or run.get('repository', {}).get('full_name') != config['repository']
            or run.get('actor', {}).get('login', '').casefold() != config['owner'].casefold()
            or run.get('triggering_actor', run.get('actor', {})).get('login', '').casefold() != config['owner'].casefold()
            or run.get('event') != context['event_name'] or run.get('head_sha') != context['sha']
            or run.get('path', '').split('@')[0] != config['workflow_path']):
        raise RecordError('ACTIONS_CONTEXT_UNVERIFIED')
    if payload.get('repository', {}).get('full_name') != config['repository'] or payload.get('sender', {}).get('login', '').casefold() != config['owner'].casefold():
        raise RecordError('FORBIDDEN')
    name = context['event_name']
    explicit_run = False
    if name == 'issues' and payload.get('action') == 'opened':
        action, number = 'patrol', payload.get('issue', {}).get('number')
        identity = {'source': 'issues.opened', 'issue_id': payload.get('issue', {}).get('id')}
    elif name == 'issues' and payload.get('action') == 'labeled' and payload.get('label', {}).get('name') == 'ready':
        action, number = 'execute', payload.get('issue', {}).get('number')
        identity = {'source': 'issues.ready', 'issue_id': payload.get('issue', {}).get('id'), 'label_id': payload.get('label', {}).get('id')}
    elif name == 'issue_comment' and payload.get('action') == 'created':
        comment_id = payload.get('comment', {}).get('id')
        if type(comment_id) is not int or comment_id < 1:
            raise RecordError('INVALID_ARGUMENT')
        comment = github.get('issues/comments/' + str(comment_id))
        body = comment.get('body', '').strip()
        if (comment.get('id') != comment_id or comment.get('user', {}).get('login', '').casefold() != config['owner'].casefold()
                or body != payload.get('comment', {}).get('body', '').strip()
                or comment.get('issue_url') != 'https://api.github.com/repos/' + config['repository'] + '/issues/' + str(payload.get('issue', {}).get('number'))):
            raise RecordError('FORBIDDEN')
        command = body.split(maxsplit=1)[0] if body else ''
        if command not in ('/run', '/patrol'):
            raise RecordError('INVALID_ARGUMENT')
        action, number = ('execute' if command == '/run' else 'patrol'), payload.get('issue', {}).get('number')
        explicit_run = command == '/run'
        identity = {'source': 'issue_comment.created', 'comment_id': comment_id}
    elif name == 'workflow_dispatch':
        inputs = payload.get('inputs', {})
        action = inputs.get('mode')
        text = inputs.get('issue_number', '')
        if action not in ('patrol', 'execute') or not isinstance(text, str) or not re.fullmatch('[1-9][0-9]*', text):
            raise RecordError('EXACT_ISSUE_REQUIRED')
        number = int(text)
        identity = {'source': 'workflow_dispatch', 'run_id': context['run_id'], 'issue_number': number}
    else:
        raise RecordError('INVALID_ARGUMENT')
    if type(number) is not int or number < 1 or any(value is None for value in identity.values()):
        raise RecordError('EXACT_ISSUE_REQUIRED')
    issue = github.get('issues/' + str(number))
    expected_url = 'https://github.com/' + config['repository'] + '/issues/' + str(number)
    if (issue.get('number') != number or issue.get('state') != 'open' or issue.get('html_url') != expected_url
            or name != 'workflow_dispatch' and issue.get('id') != payload.get('issue', {}).get('id')
            or issue.get('pull_request') or payload.get('issue', {}).get('pull_request')):
        raise RecordError('ISSUE_UNAVAILABLE')
    labels = [value.get('name') for value in issue.get('labels', []) if isinstance(value, dict)]
    ready = 'ready' in labels or explicit_run
    if action == 'execute' and not ready:
        raise RecordError('HUMAN_READY_REQUIRED')
    event_id = object_hash({'repository': config['repository'], 'identity': identity, 'action': action})
    if '```workspace-intake-v2' not in (issue.get('body') or ''):
        reviewed = config.get('reviewed_operations', {}).get(str(number))
        if reviewed is not None:
            if not isinstance(reviewed, dict) or set(reviewed) != {'project_id', 'plan_sha256'}:
                raise RecordError('PROJECT_MAPPING_UNVERIFIED')
            project = core.ident(reviewed['project_id'])
            return {'event': {'project_id': project, 'event_id': event_id, 'action': action,
                              'owner_verified': True, 'ready_verified': ready},
                    'issue_number': number, 'issue_sha256': object_hash(issue),
                    'issue_content_sha256': object_hash({'title': issue.get('title'), 'body': issue.get('body') or ''}),
                    'dispatch_route': 'pilot'}
        project = config.get('legacy_git_issues', {}).get(str(number))
        if not isinstance(project, str) or registry is None:
            raise RecordError('PROJECT_MAPPING_UNVERIFIED')
        core.ident(project)
        binding = registry.bind(project)
        if binding['backend'] != 'git_local':
            raise RecordError('LEGACY_GOOGLE_FORBIDDEN')
        registry.check(binding)
        return {'event': {'project_id': project, 'event_id': event_id, 'action': action,
                          'owner_verified': True, 'ready_verified': ready},
                'issue_number': number, 'issue_sha256': object_hash(issue), 'issue_content_sha256': object_hash({'title': issue.get('title'), 'body': issue.get('body') or ''}), 'dispatch_route': 'legacy_git'}
    marker = marker_from_issue(issue)
    allowed = config['whitelist'].get(marker['intake_key'])
    if (not isinstance(allowed, dict) or set(allowed) != {'project_id', 'mapping_generation', 'issue_number', 'payload_sha256'}
            or type(allowed['issue_number']) is not int or type(allowed['mapping_generation']) is not int
            or allowed != {key: (number if key == 'issue_number' else marker[key]) for key in allowed}):
        raise RecordError('PROJECT_MAPPING_UNVERIFIED')
    event = {'project_id': marker['project_id'], 'event_id': event_id, 'action': action,
             'owner_verified': True, 'ready_verified': ready}
    return {'event': event, 'issue_number': number, 'issue_sha256': object_hash(issue),
            'issue_content_sha256': object_hash({'title': issue.get('title'), 'body': issue.get('body') or ''}),
            'intake_key': marker['intake_key'], 'payload_sha256': marker['payload_sha256'], 'dispatch_route': 'pilot'}


def build_operation_bundle(issue, request, review_id):
    """Pure PM packaging helper; the caller reviews scope/content/base hashes.

    Does not interpret prose, sign user approval, update config or write files.
    The deployed adapter still rechecks the fresh Issue and ready owner gate.
    """
    core.ident(review_id)
    core.validate_request(request, True)
    if (type(issue.get('number')) is not int or issue['number'] < 1
            or not isinstance(issue.get('title'), str) or request['action'] not in ('replace', 'append', 'status_patch', 'batch')):
        raise RecordError('INVALID_ARGUMENT')
    plan = {'schema_version': 1, 'review_id': review_id, 'project_id': request['project_id'],
            'issue_number': issue['number'],
            'issue_content_sha256': object_hash({'title': issue['title'], 'body': issue.get('body') or ''}),
            'request': request}
    return {'plan': plan, 'plan_sha256': object_hash(plan)}


def preflight_google_append(request, binding):
    """Reject the canonical connector's static append/path violation before claim."""
    if binding['backend'] != 'google_head':
        return
    changes = request['payload']['changes'] if request['action'] == 'batch' else [request]
    paths = {item['record_id']: item['path'] for item in request['scope']['records']}
    for item in changes:
        if item['action'] == 'append' and not paths[item['target_record_id']].startswith('log/'):
            raise RecordError('FORBIDDEN')


def prepare_review_bundle(registry, config, number, request_file, review_id, github):
    """PM CLI: fetch the exact Issue now, scan, and save an immutable bundle."""
    if not isinstance(number, str) or not re.fullmatch('[1-9][0-9]*', number):
        raise RecordError('EXACT_ISSUE_REQUIRED')
    directory = git_store.safe_dir(registry.workspace / '.openchamber/workspace-records/runner-operations')
    source = git_store.target_path(directory, request_file)
    request = json.loads(source.read_text(encoding='utf-8-sig'), object_pairs_hook=routing.unique_object)
    core.validate_request(request, True)
    if request['project_id'] not in config['projects']:
        raise RecordError('PROJECT_MAPPING_UNVERIFIED')
    binding = registry.bind(request['project_id'])
    preflight_google_append(request, binding)
    expected_mode = 'google' if binding['backend'] == 'google_head' else 'git'
    if binding['route_phase'] != 'steady' or request['expected']['mode'] != expected_mode:
        raise RecordError('REVISION_CONFLICT')
    issue = github.get('issues/' + number)
    if (issue.get('number') != int(number) or issue.get('state') != 'open' or issue.get('pull_request')
            or issue.get('html_url') != 'https://github.com/' + config['repository'] + '/issues/' + number):
        raise RecordError('ISSUE_UNAVAILABLE')
    bundle = build_operation_bundle(issue, request, review_id)
    scanner = privacy.check(bundle['plan'], {'gitleaks': 'gitleaks', 'policy': binding['privacy_policy'],
        'gitleaks_config': str(pathlib.Path(__file__).resolve().parents[3] / '.gitleaks.toml')})
    target = directory / (bundle['plan_sha256'] + '.json')
    if os.path.lexists(target):
        if git_store.read_json(target) != bundle['plan']:
            raise RecordError('INTEGRITY_FAILED')
    else:
        raw = core.canonical(bundle['plan'])
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    return {'outcome': 'pm_bundle_prepared', 'plan_sha256': bundle['plan_sha256'], 'review_id': review_id,
            'catalog_entry': {number: {'project_id': request['project_id'], 'plan_sha256': bundle['plan_sha256']}},
            'scanner': scanner, 'user_ready_granted': False}


def load_operation(registry, config, event_context):
    """Load an immutable PM-reviewed per-Issue bundle, never an Issue command."""
    project = event_context['event']['project_id']
    number = event_context['issue_number']
    selected = config.get('reviewed_operations', {}).get(str(number))
    if (not isinstance(selected, dict) or set(selected) != {'project_id', 'plan_sha256'}
            or selected['project_id'] != project or not isinstance(selected['plan_sha256'], str)
            or not re.fullmatch('[a-f0-9]{64}', selected['plan_sha256'])):
        raise RecordError('PM_REVIEW_REQUIRED')
    relative = pathlib.Path('.openchamber/workspace-records/runner-operations') / (selected['plan_sha256'] + '.json')
    path = routing.private_file(registry.workspace, relative)
    plan = json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=routing.unique_object)
    if (not isinstance(plan, dict) or set(plan) != {'schema_version', 'review_id', 'project_id', 'issue_number', 'issue_content_sha256', 'request'}
            or type(plan['schema_version']) is not int or plan['schema_version'] != 1
            or object_hash(plan) != selected['plan_sha256'] or plan['project_id'] != project
            or type(plan['issue_number']) is not int or plan['issue_number'] != number
            or plan['issue_content_sha256'] != event_context['issue_content_sha256']):
        raise RecordError('PM_REVIEW_OUTDATED')
    core.ident(plan['review_id'])
    request = plan['request']
    core.validate_request(request, True)
    if request['project_id'] != project or request['action'] not in ('replace', 'append', 'status_patch', 'batch'):
        raise RecordError('FORBIDDEN')
    preflight_google_append(request, registry.bind(project))
    return plan, selected['plan_sha256']


def git_execute(registry, request):
    preview = workspace_records.managed_dispatch('preview', {'request': request}, registry)
    if preview.get('outcome') not in ('planned', 'no_write'):
        raise RecordError('OUTCOME_UNKNOWN')
    if preview['outcome'] == 'planned':
        result = workspace_records.managed_dispatch('apply', {'request': request, 'plan': preview['plan'], 'plan_hash': preview['plan_hash']}, registry)
    else:
        result = preview
    binding = registry.bind(request['project_id'])
    registry.check(binding)
    runtime = git_store.safe_dir(binding['runtime_directory'])
    entry = git_store.read_operation(runtime, request['operation_id'])
    if entry.get('state') != 'complete' or entry.get('request_hash') != core.request_hash(request):
        raise RecordError('READBACK_UNVERIFIED')
    project = git_store.safe_dir(binding['project_directory'])
    for item in entry.get('files', []):
        path = git_store.target_path(project, item['path'])
        if sha(path.read_bytes()) != item['after_sha256']:
            raise RecordError('READBACK_UNVERIFIED')
    control = git_store.get_control(runtime)
    if (control['version'] != entry['result']['authority']['logical_version']
            or control['mode'] != 'git' or control['phase'] != entry['result']['phase']
            or control['epoch'] != entry['result']['epoch']):
        raise RecordError('READBACK_UNVERIFIED')
    catalog = git_store.metadata_catalog(runtime, control, request['project_id'])
    change = entry.get('metadata_update')
    if change is not None and object_hash(catalog) != change['after_sha256']:
        raise RecordError('READBACK_UNVERIFIED')
    return {'outcome': 'canonical_confirmed', 'request_hash': core.request_hash(request),
            'result': result['result'], 'readback_hash': object_hash(entry)}


def record_result(registry, claim, event_context, result):
    parent = registry.workspace / '.openchamber/workspace-records/runner-receipts'
    if not os.path.lexists(parent):
        parent.mkdir(mode=0o700)
    parent = git_store.safe_dir(parent)
    value = {'schema_version': 1, 'claim_key': claim['key'], 'source_event': event_context,
             'writer_result': result, 'writer_receipt_sha256': object_hash(result)}
    git_store.write_json(parent / (claim['key'] + '.json'), value)
    return value['writer_receipt_sha256']


def project_canonical_receipt(registry, config, result, broker):
    settings = config.get('portfolio_projection')
    if not settings or settings.get('enabled') is not True:
        return {'state': 'pending', 'code': 'PROJECTION_NOT_CONFIGURED', 'receipt_sha256': result.get('receipt_sha256')}
    try:
        import portfolio_mixed
        import portfolio_refresh
        import pushed_git_source
        checkpoint = pushed_git_source.observe_pushed_checkpoint(registry.workspace / 'project-notes', config['repository'])
        box = portfolio_mixed.managed_outbox(checkpoint)
        receipt = result['receipt']
        queued = portfolio_refresh.queue_event(box, {'kind': 'canonical_receipt',
            'project_id': receipt['project_id'], 'operation_id': receipt['operation_id'],
            'receipt_sha256': result['receipt_sha256']})
        if not settings or settings.get('enabled') is not True or not queued.get('dispatch_required'):
            return {'state': queued['outcome'], 'event_hash': queued['event_hash'],
                    'receipt_sha256': result['receipt_sha256']}
        if set(settings) != {'enabled', 'sheet_title'} or not isinstance(settings['sheet_title'], str) or not settings['sheet_title']:
            raise RecordError('INVALID_ARGUMENT')
        import portfolio_consumer
        directory = git_store.safe_dir(registry.workspace / routing.RUNTIME / 'runner-apps')
        projection = portfolio_consumer.consume_events(box, lambda: portfolio_mixed.MixedSession(
            directory, settings['sheet_title'], existing_broker=broker))
        return {'state': projection.get('outcome'), 'receipt_sha256': result['receipt_sha256'],
                'result': projection}
    except (RecordError, ImportError, OSError, ValueError, TypeError, KeyError) as error:
        return {'state': 'pending', 'receipt_sha256': result.get('receipt_sha256'),
                'code': error.code if isinstance(error, RecordError) else 'PROJECTION_UNAVAILABLE'}


def projection_lock_directory(registry, claim_key):
    core.ident(claim_key)
    directory = git_store.safe_dir(registry.workspace / '.openchamber/workspace-records/runner-receipts')
    for component in ('projection-locks', claim_key):
        directory = directory / component
        if not os.path.lexists(directory):
            try: directory.mkdir(mode=0o700)
            except FileExistsError: pass
        directory = git_store.safe_dir(directory)
    return directory


def project_details(registry, config, result, reader):
    try:
        receipt = result.get('receipt', {})
        mapping = config.get('detail_projection', {}).get(receipt.get('project_id'))
        if mapping is None:
            return {'outcome': 'detail_projection_pending', 'code': 'PROJECTION_NOT_CONFIGURED'}
        import detail_projection_driver
        return detail_projection_driver.refresh_details(registry, reader, mapping, receipt['project_id'], result['receipt_sha256'])
    except Exception as error:
        return {'outcome': 'detail_projection_pending', 'code': error.code if isinstance(error, RecordError) else 'CONTROL_UNAVAILABLE'}


def recover_projections(registry, config, claim_key, session_factory):
    # PM retries derived copies only; never reclaims or executes canonical work.
    if not isinstance(claim_key, str) or not re.fullmatch('[a-f0-9]{64}', claim_key):
        raise RecordError('INVALID_ARGUMENT')
    directory = git_store.safe_dir(registry.workspace / '.openchamber/workspace-records/runner-receipts')
    saved = git_store.read_json(directory / (claim_key + '.json'))
    result = saved.get('writer_result')
    event = saved.get('source_event', {}).get('event')
    if (saved.get('claim_key') != claim_key or not isinstance(result, dict)
            or object_hash(result) != saved.get('writer_receipt_sha256')
            or result.get('outcome') != 'canonical_confirmed' or not isinstance(event, dict)
            or event.get('action') != 'execute'):
        raise RecordError('READBACK_UNVERIFIED')
    claim = guard.peek(registry, event)
    if not claim or claim.get('key') != claim_key or claim.get('state') != 'complete':
        raise RecordError('MANUAL_REVIEW')
    project = event['project_id']
    settings = config['projects'].get(project)
    if not isinstance(settings, dict) or set(settings) != {'google'} or not settings['google']:
        raise RecordError('PROJECT_MAPPING_UNVERIFIED')
    binding = registry.bind(project)
    if binding['backend'] != 'google_head': raise RecordError('STALE_MODE')
    def run(broker):
        reader = canonical_writer.CanonicalReader(broker, settings['google'])
        return {'outcome': 'projection_recovery', 'claim_key': claim_key,
                'canonical_outcome': 'canonical_confirmed',
                'detail_projection': project_details(registry, config, result, reader),
                'projection': project_canonical_receipt(registry, config, result, broker)}
    with git_store.locked(projection_lock_directory(registry, claim_key)):
        envelope = session_factory().run(run)
    if envelope.get('outcome') != 'driver_completed' or not isinstance(envelope.get('driver_result'), dict):
        raise RecordError(envelope.get('code', 'CONNECTOR_UNAVAILABLE'))
    return {**envelope['driver_result'], 'model_turns': envelope.get('model_turns')}


def save_transport_diagnostic(registry, event, operation, plan_hash, envelope):
    """Persist only bounded shapes and hashes, never tool arguments or results."""
    allowed_stage = {'open', 'turn_start', 'server_request', 'notification', 'dynamic_next_action',
                     'request_denied', 'mcp_start', 'mcp_complete', 'driver_wait'}
    methods = {'item/tool/call', 'mcpServer/elicitation/request', 'tool/requestUserInput',
               'item/started', 'item/completed', 'turn/completed', 'other'}
    types = {'mcpToolCall', 'dynamicToolCall', 'commandExecution', 'fileChange',
             'collabAgentToolCall', 'agentMessage', 'reasoning', 'other'}
    arg_types = {'missing', 'object', 'array', 'string', 'boolean', 'number', 'null', 'other'}
    events = []
    trace = envelope.get('event_trace')
    if isinstance(trace, list):
        for item in trace[-16:]:
            if not isinstance(item, dict):
                continue
            row = {key: item[key] for key in ('request', 'thread_matches', 'turn_matches', 'item_correlated',
                   'dynamic_name_matches', 'arguments_empty_object', 'active_action', 'mcp_arguments_match')
                   if key in item and (type(item[key]) is bool or key == 'mcp_arguments_match' and item[key] is None)}
            for key, choices in (('method', methods), ('item_type', types), ('arguments_type', arg_types)):
                row[key] = item.get(key) if isinstance(item.get(key), str) and item[key] in choices else 'other'
            events.append(row)
    proofs = []
    receipts = envelope.get('tool_receipts')
    if isinstance(receipts, list):
        for item in receipts[-32:]:
            if isinstance(item, dict):
                proofs.append({key: item[key] for key in ('arguments_sha256', 'result_sha256')
                               if isinstance(item.get(key), str) and re.fullmatch('[a-f0-9]{64}', item[key])})
    def cleanup(value):
        if not isinstance(value, dict):
            return None
        return {key: value[key] for key in ('appserver_exited', 'reader_thread_settled', 'pipes_closed')
                if type(value.get(key)) is bool} | {'remaining_processes': len(value['remaining_job_pids'])
                if isinstance(value.get('remaining_job_pids'), list) and len(value['remaining_job_pids']) <= 1024 else None}
    mismatch = envelope.get('mcp_start_diagnostic')
    if not isinstance(mismatch, dict):
        mismatch = {}
    diagnostic = {'schema_version': 1, 'claim_key': guard.event_key(event),
                  'plan_sha256': plan_hash if operation else None,
                  'request_sha256': core.request_hash(operation['request']) if operation else None,
                  'failure_stage': envelope.get('failure_stage') if isinstance(envelope.get('failure_stage'), str) and envelope['failure_stage'] in allowed_stage else 'other',
                  'model_turns': envelope.get('model_turns') if type(envelope.get('model_turns')) is int and 0 <= envelope['model_turns'] <= 100 else None,
                  'driver_thread_settled': envelope.get('driver_thread_settled') if type(envelope.get('driver_thread_settled')) is bool else None,
                  'event_trace': events, 'tool_proofs': proofs,
                  'mcp_arguments_exact': mismatch.get('arguments_exact') if type(mismatch.get('arguments_exact')) is bool else None,
                  'mcp_argument_hashes': {key: mismatch[key] for key in ('expected_arguments_sha256', 'actual_arguments_sha256')
                                          if isinstance(mismatch.get(key), str) and re.fullmatch('[a-f0-9]{64}', mismatch[key])},
                  'cleanup': cleanup(envelope.get('cleanup')), 'discovery_cleanup': cleanup(envelope.get('discovery_cleanup'))}
    parent = registry.workspace / routing.RUNTIME / 'runner-diagnostics'
    if not os.path.lexists(parent):
        parent.mkdir(mode=0o700)
    parent = git_store.safe_dir(parent)
    destination = parent / (diagnostic['claim_key'] + '-' + uuid.uuid4().hex + '.json')
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(core.canonical(diagnostic)); stream.flush(); os.fsync(stream.fileno())


def dispatch(registry, config, event_context, session_factory):
    if event_context.get('dispatch_route') != 'pilot':
        raise RecordError('LEGACY_SCOPE_SEPARATE')
    event_context = dict(event_context)
    event = event_context['event']
    operation = None
    if event['action'] == 'execute':
        operation, plan_hash = load_operation(registry, config, event_context)
        event_context.update(operation_plan_sha256=plan_hash, review_id=operation['review_id'])
    previous = guard.peek(registry, event)
    if previous is not None:
        if event['action'] == 'execute':
            path = registry.workspace / '.openchamber/workspace-records/runner-receipts' / (previous['key'] + '.intent.json')
            intent = git_store.read_json(path)
            if intent.get('operation_plan_sha256') != event_context['operation_plan_sha256']:
                raise RecordError('ID_REUSE')
        return {'outcome': 'duplicate_event', 'claim': previous, 'model_turns': 0}
    binding = registry.bind(event['project_id'])
    registry.require_no_activation(event['project_id'])
    settings = config['projects'].get(event['project_id'])
    if not isinstance(settings, dict) or set(settings) != {'google'}:
        raise RecordError('PROJECT_MAPPING_UNVERIFIED')

    def execute(reader=None):
        prepared = guard.prepare(registry, event, reader)
        claim = guard.claim(registry, prepared, reader)
        if not claim['launch']:
            return {'outcome': 'duplicate_event', 'claim': claim}
        try:
            if operation is not None:
                parent = registry.workspace / '.openchamber/workspace-records/runner-receipts'
                if not os.path.lexists(parent):
                    parent.mkdir(mode=0o700)
                parent = git_store.safe_dir(parent)
                intent = {'schema_version': 1, 'claim_key': claim['key'], 'operation_plan_sha256': event_context['operation_plan_sha256'],
                          'review_id': operation['review_id'], 'operation_id': operation['request']['operation_id'],
                          'request_hash': core.request_hash(operation['request'])}
                git_store.write_json(parent / (claim['key'] + '.intent.json'), intent)
            if event['action'] == 'patrol':
                if reader:
                    canonical = canonical_writer.CanonicalWriter(registry, reader)
                    existing = canonical.graph(binding, reader.last['snapshot']['head'])
                    result = {'outcome': 'patrol_read_confirmed', 'head_hash': object_hash(reader.last['snapshot']['head']),
                              'manifest_sha256': sha(pathlib.Path(existing['manifest_file']).read_bytes())}
                else:
                    result = workspace_records.managed_dispatch('inventory', {'request': {
                        'schema_version': 1, 'request_id': 'patrol-' + event['event_id'], 'project_id': event['project_id'],
                        'actor_role': 'reader', 'scope': {'project_id': event['project_id'], 'actions': [], 'records': []}}}, registry)
                    result = {'outcome': 'patrol_read_confirmed', 'inventory_hash': object_hash(result)}
            else:
                request = operation['request']
                result = canonical_writer.CanonicalWriter(registry, reader).execute(request) if reader else git_execute(registry, request)
                if result.get('outcome') != 'canonical_confirmed':
                    raise RecordError('READBACK_UNVERIFIED')
            digest = record_result(registry, claim, event_context, result)
            guard.finish(registry, claim['key'], 'complete')
        except Exception:
            guard.finish(registry, claim['key'], 'unknown')
            raise
        completed = {'outcome': result['outcome'], 'claim_key': claim['key'], 'writer_receipt_sha256': digest}
        if event['action'] == 'execute':
            if reader:
                try:
                    with git_store.locked(projection_lock_directory(registry, claim['key'])):
                        completed['detail_projection'] = project_details(registry, config, result, reader)
                        completed['projection'] = project_canonical_receipt(registry, config, result, reader.broker)
                except Exception as error:
                    code = error.code if isinstance(error, RecordError) else 'CONTROL_UNAVAILABLE'
                    completed['detail_projection'] = {'outcome': 'detail_projection_pending', 'code': code}
                    completed['projection'] = {'state': 'pending', 'code': code}
            else:
                completed['projection'] = {'state': 'await_notes_push'}
        return completed

    if binding['backend'] == 'git_local':
        registry.check(binding)
        return {**execute(), 'model_turns': 0}
    if binding['backend'] != 'google_head' or binding['route_phase'] != 'steady':
        raise RecordError('PHASE_FROZEN')
    session = session_factory()
    envelope = session.run(lambda broker: execute(canonical_writer.CanonicalReader(broker, settings['google'])))
    if envelope.get('outcome') != 'driver_completed' or not isinstance(envelope.get('driver_result'), dict):
        failure = RecordError(envelope.get('code', 'CONNECTOR_UNAVAILABLE'))
        try:
            save_transport_diagnostic(registry, event, operation, plan_hash if operation else None, envelope)
            failure.diagnostic_saved = True
        except Exception:
            failure.diagnostic_saved = False
        raise failure
    result = envelope['driver_result']
    if result.get('outcome') not in ('canonical_confirmed', 'patrol_read_confirmed', 'duplicate_event'):
        raise RecordError('READBACK_UNVERIFIED')
    persisted = guard.peek(registry, event)
    if persisted is None:
        raise RecordError('READBACK_UNVERIFIED')
    if result['outcome'] != 'duplicate_event':
        if persisted['state'] != 'complete' or result.get('claim_key') != persisted['key']:
            raise RecordError('READBACK_UNVERIFIED')
        path = registry.workspace / '.openchamber/workspace-records/runner-receipts' / (persisted['key'] + '.json')
        receipt = git_store.read_json(path)
        if (receipt.get('source_event') != event_context or receipt.get('claim_key') != persisted['key']
                or object_hash(receipt.get('writer_result')) != result.get('writer_receipt_sha256')
                or receipt.get('writer_result', {}).get('outcome') != result['outcome']):
            raise RecordError('READBACK_UNVERIFIED')
    return {**result, 'model_turns': envelope.get('model_turns'), 'tool_receipts': envelope.get('tool_receipts')}




def trusted_executable(config):
    """Optional executable pin from the fixed trusted runner config only."""
    if 'client' not in config:
        return None
    client = config['client']
    if not isinstance(client, dict) or set(client) != {'codex_executable'}:
        raise RecordError('INVALID_ARGUMENT')
    pin = client['codex_executable']
    if (not isinstance(pin, dict) or set(pin) != {'path', 'sha256', 'size'}
            or not isinstance(pin['path'], str) or not pathlib.Path(pin['path']).is_absolute()
            or not isinstance(pin['sha256'], str) or not re.fullmatch('[a-f0-9]{64}', pin['sha256'])
            or type(pin['size']) is not int or pin['size'] < 1):
        raise RecordError('INVALID_ARGUMENT')
    path = pathlib.Path(pin['path'])
    try:
        git_store.safe_dir(path.parent)
        component = pathlib.Path(path.anchor)
        for part in path.parts[1:]:
            component = git_store.canonical_child(component, part)
            info = component.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 1024:
                raise RecordError('FORBIDDEN')
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or str(path.resolve(strict=True)) != str(path)
                or info.st_size != pin['size'] or sha(path.read_bytes()) != pin['sha256']):
            raise RecordError('FORBIDDEN')
    except OSError:
        raise RecordError('CONNECTOR_UNAVAILABLE') from None
    return str(path)


def managed_session(directory, config):
    from codex_action_transport import CodexActionSession
    readers, writers = set(canonical_writer.READERS), set(canonical_writer.WRITERS)
    if config.get('detail_projection'):
        import detail_projection_driver
        readers.update(detail_projection_driver.READERS)
        writers.update(detail_projection_driver.WRITERS)
    projection = config.get('portfolio_projection')
    if projection and projection.get('enabled') is True:
        if set(projection) != {'enabled', 'sheet_title'} or not isinstance(projection['sheet_title'], str) or not projection['sheet_title']:
            raise RecordError('INVALID_ARGUMENT')
        readers.add('get_spreadsheet_cells')
        writers.add('batch_update_spreadsheet')
    return CodexActionSession(directory, readers=readers, writers=writers, executable=trusted_executable(config))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--managed', required=True, action='store_true')
    parser.add_argument('--disable-notifications', required=True, action='store_true')
    parser.add_argument('--route-only', action='store_true')
    parser.add_argument('--recover-projections')
    parser.add_argument('--prepare-operation')
    parser.add_argument('--request-file')
    parser.add_argument('--review-id')
    args = parser.parse_args()
    try:
        workspace = routing.discover_workspace()
        registry = routing.Registry(workspace)
        config = git_store.read_json(routing.private_file(workspace, CONFIG))
        required = {'repository', 'owner', 'workflow_path', 'whitelist', 'projects'}
        if not required <= set(config) or set(config) - required - {'legacy_git_issues', 'reviewed_operations', 'portfolio_projection', 'detail_projection', 'client'}:
            raise RecordError('INVALID_ARGUMENT')
        directory = workspace / routing.RUNTIME / 'runner-apps'
        if not os.path.lexists(directory):
            directory.mkdir(mode=0o700)
        directory = git_store.safe_dir(directory)
        def factory():
            return managed_session(directory, config)
        if args.recover_projections is not None:
            if args.prepare_operation or args.request_file or args.review_id or args.route_only:
                raise RecordError('INVALID_ARGUMENT')
            result = recover_projections(registry, config, args.recover_projections, factory)
            print(json.dumps({'ok': True, 'data': result}, ensure_ascii=True))
            return 0
        if args.prepare_operation is not None:
            if args.route_only or not args.request_file or not args.review_id:
                raise RecordError('INVALID_ARGUMENT')
            result = prepare_review_bundle(registry, config, args.prepare_operation, args.request_file, args.review_id,
                                           GitHubReader(config['repository']))
            print(json.dumps({'ok': True, 'data': result}, ensure_ascii=True))
            return 0
        if args.request_file is not None or args.review_id is not None:
            raise RecordError('INVALID_ARGUMENT')
        context = {key: os.environ.get(env, '') for key, env in {
            'actions': 'GITHUB_ACTIONS', 'repository': 'GITHUB_REPOSITORY', 'repository_owner': 'GITHUB_REPOSITORY_OWNER',
            'actor': 'GITHUB_ACTOR', 'event_name': 'GITHUB_EVENT_NAME', 'run_id': 'GITHUB_RUN_ID',
            'run_attempt': 'GITHUB_RUN_ATTEMPT', 'sha': 'GITHUB_SHA'}.items()}
        with open(os.environ['GITHUB_EVENT_PATH'], encoding='utf-8-sig') as source:
            payload = json.load(source, object_pairs_hook=routing.unique_object)
        event = trusted_event(config, context, payload, GitHubReader(config['repository']), registry)
        if args.route_only:
            print(json.dumps({'ok': True, 'data': {'route': event['dispatch_route'],
                'action': event['event']['action'], 'issue_number': event['issue_number']}}, ensure_ascii=True))
            return 0
        result = dispatch(registry, config, event, factory)
        print(json.dumps({'ok': True, 'data': result}, ensure_ascii=True))
        return 0
    except (RecordError, OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        code = error.code if isinstance(error, RecordError) else 'CONTROL_UNAVAILABLE'
        result = {'ok': False, 'error': {'code': code, 'retryable': False}}
        if hasattr(error, 'diagnostic_saved'):
            result['error']['diagnostic_saved'] = error.diagnostic_saved
        print(json.dumps(result, ensure_ascii=True))
        return 2


if __name__ == '__main__':
    sys.exit(main())
