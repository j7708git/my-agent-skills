"""Mixed Git/Google portfolio source; Google observations come from canonical reader.

This module does not write the Sheet or create another authority. A retained
Google row is explicitly last-observed; only a fresh verified head can cover a
canonical receipt event. The event itself is only a wake hint.
"""
import copy
import os
import pathlib
import re
from datetime import datetime, timezone

import canonical_writer
import connector_protocol
import core
import git_store
import portfolio
import portfolio_refresh
import pushed_git_source
from core import RecordError, object_hash, sha

HEX = re.compile(r'^[a-f0-9]{64}$')
RECEIPT_KEYS = {'schema_version', 'project_id', 'operation_id', 'request_hash',
                'result_sha256', 'operation_epoch', 'operation_version',
                'observed_head_hash', 'observed_head_epoch', 'observed_head_version', 'observed_revision', 'observed_at'}


def timestamp(value):
    if not isinstance(value, str):
        raise RecordError('INTEGRITY_FAILED')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise RecordError('INTEGRITY_FAILED') from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RecordError('INTEGRITY_FAILED')
    return value


def receipt_for(registry, event):
    if (not isinstance(event, dict) or set(event) != {'kind', 'project_id', 'operation_id', 'receipt_sha256'}
            or event['kind'] != 'canonical_receipt'):
        raise RecordError('INVALID_ARGUMENT')
    core.ident(event['project_id']); core.ident(event['operation_id'])
    digest = event['receipt_sha256']
    if not isinstance(digest, str) or not HEX.fullmatch(digest):
        raise RecordError('INVALID_ARGUMENT')
    parent = git_store.safe_dir(registry.workspace / '.openchamber/workspace-records/canonical-receipts')
    path = git_store.canonical_child(parent, digest + '.json')
    receipt = git_store.read_json(path)
    if (not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS or receipt['schema_version'] != 1
            or object_hash(receipt) != digest or receipt['project_id'] != event['project_id']
            or receipt['operation_id'] != event['operation_id']):
        raise RecordError('INTEGRITY_FAILED')
    for name in ('request_hash', 'result_sha256', 'observed_head_hash'):
        if not isinstance(receipt[name], str) or not HEX.fullmatch(receipt[name]):
            raise RecordError('INTEGRITY_FAILED')
    if (type(receipt['operation_epoch']) is not int or receipt['operation_epoch'] < 1
            or type(receipt['operation_version']) is not int or receipt['operation_version'] < 0
            or type(receipt['observed_head_epoch']) is not int or receipt['observed_head_epoch'] != receipt['operation_epoch']
            or type(receipt['observed_head_version']) is not int or receipt['observed_head_version'] < receipt['operation_version']
            or not isinstance(receipt['observed_revision'], str) or not receipt['observed_revision']):
        raise RecordError('INTEGRITY_FAILED')
    timestamp(receipt['observed_at'])
    return receipt


def cache_parent(registry, create=False):
    path = registry.workspace / '.openchamber/workspace-records/portfolio-cache'
    if not os.path.lexists(path):
        if not create:
            return None
        path.mkdir(mode=0o700)
    return git_store.safe_dir(path)


def cached_row(registry, binding):
    parent = cache_parent(registry)
    if parent is None:
        return None
    path = parent / (sha(binding['project_id'].encode()) + '.json')
    if not os.path.lexists(path):
        return None
    value = git_store.read_json(path)
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'binding_hash', 'row', 'row_hash'}
            or value['schema_version'] != 1 or value['binding_hash'] != object_hash(binding)
            or value['row_hash'] != object_hash(value['row'])):
        raise RecordError('INTEGRITY_FAILED')
    row = value['row']
    if (row.get('project_id') != binding['project_id'] or row.get('authority') != 'google_head'
            or row.get('availability') != 'known' or not isinstance(row.get('source'), dict)):
        raise RecordError('INTEGRITY_FAILED')
    source = row['source']
    if (not isinstance(source.get('tool_proofs'), list)
            or source.get('tool_proof_hashes') != [object_hash(item) for item in source['tool_proofs']]):
        raise RecordError('INTEGRITY_FAILED')
    timestamp(source['observed_at'])
    return row


def save_cache(registry, binding, row):
    parent = cache_parent(registry, True)
    value = {'schema_version': 1, 'binding_hash': object_hash(binding),
             'row': row, 'row_hash': object_hash(row)}
    git_store.write_json(parent / (sha(binding['project_id'].encode()) + '.json'), value)


def observe_google(registry, binding, event_receipts, reader_factory, previous):
    reader = reader_factory(binding['project_id'], binding)
    if not isinstance(reader, canonical_writer.CanonicalReader):
        raise RecordError('CONNECTOR_UNAVAILABLE')
    first = reader.read_authority(binding, 'portfolio-first-' + __import__('uuid').uuid4().hex)
    registry.check(binding, first['authority'])
    head = first['snapshot']['head']
    writer = canonical_writer.CanonicalWriter(registry, reader)
    existing = writer.graph(binding, head)
    root = git_store.safe_dir(binding['runtime_directory'])
    manifest, operations, objects, events_unused, journal_unused = connector_protocol.read_graph(root, head, existing)
    if connector_protocol.status_summary(manifest['records'], objects) != head['summary']:
        raise RecordError('INTEGRITY_FAILED')
    statuses = [entry for entry in manifest['records'] if entry['record']['kind'] == 'status']
    if len(statuses) != 1:
        raise RecordError('CONTROL_UNAVAILABLE')
    status = statuses[0]
    raw = objects[status['object']['id']]
    core.status_patch(raw, {})
    second = reader.read_authority(binding, 'portfolio-second-' + __import__('uuid').uuid4().hex)
    registry.check(binding, second['authority'])
    if (second['snapshot']['revision'] != first['snapshot']['revision']
            or second['snapshot']['head'] != head or registry.bind(binding['project_id']) != binding):
        raise RecordError('REVISION_CONFLICT')
    covered, pending = [], []
    for event, receipt in event_receipts:
        op = operations.get(receipt['operation_id'])
        if (receipt['operation_epoch'] != head['control']['epoch']
                or receipt['observed_head_epoch'] != head['control']['epoch']
                or receipt['observed_head_version'] > head['version']
                or (receipt['observed_head_version'] == head['version'] and receipt['observed_head_hash'] != object_hash(head))
                or receipt['operation_version'] > head['version']
                or not isinstance(op, dict) or op.get('request_hash') != receipt['request_hash']
                or object_hash(op.get('result')) != receipt['result_sha256']):
            pending.append(object_hash(event)); continue
        result = op['result']
        if (result.get('epoch') != receipt['operation_epoch']
                or result.get('logical_version') != receipt['operation_version']):
            pending.append(object_hash(event)); continue
        covered.append(object_hash(event))
    head_hash = object_hash(head)
    observed_at = datetime.now(timezone.utc).isoformat()
    if previous and previous['source'].get('sha256') == head_hash:
        observed_at = previous['source']['observed_at']
    row = {'project_id': binding['project_id'], 'authority': 'google_head', 'availability': 'known',
           'values': {name: head['summary'][name] for name in portfolio.FIELDS},
           'source': {'kind': 'google_head', 'sha256': head_hash,
                      'source_time': head['summary']['updated'], 'observed_at': observed_at,
                      'revision': first['snapshot']['revision'], 'version': head['version'],
                      'epoch': head['control']['epoch'], 'status_sha256': sha(raw),
                      'receipt_sha256s': [],
                      'tool_proof_hashes': [object_hash(first['proof'])] + [object_hash(p) for p in writer.proofs] + [object_hash(second['proof'])],
                      'tool_proofs': [copy.deepcopy(first['proof'])] + copy.deepcopy(writer.proofs) + [copy.deepcopy(second['proof'])]},
           'issues': []}
    row['source']['receipt_sha256s'] = sorted(event['receipt_sha256'] for event, unused in event_receipts if object_hash(event) in covered)
    return row, covered, pending


def collect_mixed(registry, events, reader_factory):
    """Two-pass Git and registry, selective verified Google reads on receipt events."""
    if not isinstance(events, list) or not callable(reader_factory):
        raise RecordError('INVALID_ARGUMENT')
    data, digest = registry.load()
    ids = portfolio.inventory(registry)
    if set(ids) != set(data['projects']):
        raise RecordError('NOT_REGISTERED')
    groups = {}
    for event in events:
        if not isinstance(event, dict) or event.get('kind') != 'canonical_receipt':
            raise RecordError('INVALID_ARGUMENT')
        project = core.ident(event['project_id'])
        if project not in data['projects'] or data['projects'][project]['backend'] != 'google_head':
            raise RecordError('REVISION_CONFLICT')
        groups.setdefault(project, []).append(event)
    bindings = {project: registry.bind(project) for project in ids}
    git_first = {}
    for project in ids:
        binding = bindings[project]
        if binding['backend'] == 'git_local':
            registry.check(binding)
            git_first[project] = portfolio.git_row(pathlib.Path(binding['project_directory']), project)
    rows, covered, pending = [], [], []
    for project in ids:
        binding = bindings[project]
        if binding['backend'] == 'git_local':
            rows.append(git_first[project]); continue
        if binding['backend'] != 'google_head' or binding['route_phase'] != 'steady':
            rows.append(portfolio.unknown(project, 'google_head', 'CONTROL_UNAVAILABLE'))
            pending.extend(object_hash(e) for e in groups.get(project, [])); continue
        try:
            registry.require_no_activation(project)
            previous = cached_row(registry, binding)
            selected = groups.get(project, [])
            if selected:
                valid = []
                for event in selected:
                    try:
                        valid.append((event, receipt_for(registry, event)))
                    except (RecordError, OSError, ValueError, TypeError, KeyError):
                        pending.append(object_hash(event))
                if valid:
                    row, accepted, unmatched = observe_google(registry, binding, valid, reader_factory, previous)
                    save_cache(registry, binding, row)
                    covered.extend(accepted)
                    pending.extend(unmatched)
                else:
                    row = portfolio.unknown(project, 'google_head', 'RECEIPT_UNAVAILABLE')
            elif previous is not None:
                row = previous
            else:
                row = portfolio.unknown(project, 'google_head', 'GOOGLE_NOT_OBSERVED')
            rows.append(row)
        except (RecordError, OSError, ValueError, TypeError, KeyError) as error:
            reason = error.code if isinstance(error, RecordError) else 'GOOGLE_UNAVAILABLE'
            rows.append(portfolio.unknown(project, 'google_head', reason))
            pending.extend(object_hash(e) for e in groups.get(project, []))
    for project, first in git_first.items():
        binding = registry.bind(project)
        registry.check(binding)
        second = portfolio.git_row(pathlib.Path(binding['project_directory']), project)
        if binding != bindings[project] or first != second:
            raise RecordError('REVISION_CONFLICT')
    if registry.load()[1] != digest or any(registry.bind(p) != bindings[p] for p in ids):
        raise RecordError('REVISION_CONFLICT')
    # Freshness/observation clock is presentation evidence, not a source change.
    stable_rows = copy.deepcopy(rows)
    for row in stable_rows:
        if row.get('source'):
            row['source'].pop('observed_at', None)
            if row['authority'] == 'google_head':
                row['source'].pop('revision', None)
                row['source'].pop('receipt_sha256s', None)
                row['source'].pop('tool_proof_hashes', None)
                row['source'].pop('tool_proofs', None)
    source = {'schema_version': 1, 'rows': rows, 'inventory': ids,
              'registry_sha256': digest, 'covered_events': sorted(set(covered)),
              'pending_events': sorted(set(pending))}
    source['source_digest'] = object_hash({'schema_version': 1, 'rows': stable_rows,
                                           'inventory': ids, 'registry_sha256': digest})
    source['coverage_digest'] = object_hash({'source_digest': source['source_digest'],
                                             'covered_events': source['covered_events'],
                                             'pending_events': source['pending_events']})
    return source


def desired_rows(source, target):
    rows = portfolio_refresh.desired_rows(source, target)
    for entry in source['rows']:
        if entry['authority'] == 'google_head' and entry['availability'] == 'known':
            rows[entry['project_id']]['freshness'] = 'last_observed@' + timestamp(entry['source']['observed_at'])
    return rows


class MixedSource:
    """Local probe uses only cache; a bound broker permits selected receipt reads."""
    def __init__(self, registry, config, project_configs, checkpoint):
        if (not isinstance(config, dict) or set(config) != {'notes_root', 'authority', 'scope', 'generation'}
                or config['authority'] != 'mixed' or config['scope'] != 'all_projects'
                or type(config['generation']) is not int or config['generation'] < 1
                or str(git_store.safe_dir(config['notes_root'])) != str(git_store.safe_dir(registry.workspace / 'project-notes'))
                or not isinstance(project_configs, dict)):
            raise RecordError('FORBIDDEN')
        if (not isinstance(checkpoint, dict) or not isinstance(checkpoint.get('source_commit'), str)
                or not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', checkpoint['source_commit'])
                or not isinstance(checkpoint.get('repository'), str) or not checkpoint['repository']):
            raise RecordError('FORBIDDEN')
        self.registry, self.config = registry, copy.deepcopy(config)
        self.checkpoint = copy.deepcopy(checkpoint)
        self.project_configs = copy.deepcopy(project_configs)
        self.events, self.broker = [], None

    def select_events(self, events):
        if not isinstance(events, list):
            raise RecordError('INVALID_ARGUMENT')
        self.events = copy.deepcopy(events)

    def reader(self, project, binding):
        if self.broker is None or project not in self.project_configs:
            raise RecordError('CONNECTOR_UNAVAILABLE')
        return canonical_writer.CanonicalReader(self.broker, self.project_configs[project])

    def __call__(self):
        selected = [event for event in self.events if event.get('kind') == 'canonical_receipt'] if self.broker is not None else []
        source = collect_mixed(self.registry, selected, self.reader)
        routes, route_hash = self.registry.load()
        if route_hash != source['registry_sha256']:
            raise RecordError('REVISION_CONFLICT')
        git_ids = [project for project, item in routes['projects'].items() if item['backend'] == 'git_local']
        proof = pushed_git_source.verify_pushed_rows(self.config['notes_root'], self.checkpoint['source_commit'],
                                                     source['rows'], git_ids, self.checkpoint['repository'])
        if self.registry.load()[1] != route_hash:
            raise RecordError('REVISION_CONFLICT')
        source['source_commit_proof'] = proof
        source['config_hash'] = object_hash({'source_config': self.config, 'project_configs': self.project_configs})
        source['source_digest'] = object_hash({'config_hash': source['config_hash'], 'mixed_digest': source['source_digest']})
        source['coverage_digest'] = object_hash({'source_digest': source['source_digest'],
                                                 'covered_events': source['covered_events'],
                                                 'pending_events': source['pending_events']})
        return source


class MixedOutbox(portfolio_refresh.Outbox):
    def __init__(self, directory, config, target, privacy_config, registry, project_configs, checkpoint):
        source = MixedSource(registry, config, project_configs, checkpoint)
        super().__init__(directory, config, target, privacy_config, source_provider=source, row_projector=desired_rows, mixed=True)

    def probe(self):
        result = super().probe()
        if result['outcome'] not in ('no_change', 'refresh_required'):
            return result
        success = result.get('last_success') or {}
        already = set(success.get('covered_events', []))
        eligible = []
        for event in self.source_provider.events:
            if event.get('kind') != 'canonical_receipt' or object_hash(event) in already:
                continue
            try:
                receipt_for(self.source_provider.registry, event)
            except (RecordError, OSError, ValueError, TypeError, KeyError):
                continue
            eligible.append(event)
        if eligible:
            result['outcome'] = 'refresh_required'
        result['covered_events'] = sorted(already)
        return result


def verified_sheet_call(broker, tool, arguments, kind):
    result = broker.request(tool, copy.deepcopy(arguments), kind)
    value, proof = result.result, result.proof
    if (not isinstance(value, dict) or not isinstance(proof, dict)
            or proof.get('tool') not in (tool, 'google_drive.' + tool)
            or proof.get('arguments_sha256') != object_hash(arguments)
            or proof.get('result_sha256') != object_hash(value)
            or proof.get('arguments_exact') is not True or proof.get('tool_completed') is not True
            or not proof.get('action_id') or not proof.get('item_id')
            or kind == 'write' and proof.get('approval_correlated') is not True
            or value.get('isError')):
        raise RecordError('READBACK_UNVERIFIED')
    return value


class SheetTransport:
    def __init__(self, broker, target, title):
        self.broker, self.target, self.title = broker, copy.deepcopy(target), title

    def read(self, target, nonce):
        import codex_portfolio_transport as transport
        if target != self.target:
            raise RecordError('FORBIDDEN')
        args = transport.read_arguments(target, self.title)
        result = verified_sheet_call(self.broker, transport.READ, args, 'read')
        return transport.observation(result, target, self.title, nonce)

    def write(self, payload):
        import codex_portfolio_transport as transport
        args = transport.checked_write(payload, self.target)
        result = verified_sheet_call(self.broker, transport.WRITE, args, 'write')
        data = result.get('structuredContent')
        if not isinstance(data, dict) or data.get('spreadsheetId', data.get('spreadsheet_id')) != self.target['spreadsheet_id']:
            raise RecordError('READBACK_UNVERIFIED')


class MixedSession:
    """One generic App session carries canonical reads and bounded Sheet writes."""
    def __init__(self, directory, title, model='gpt-6.1-sol', timeout=600, executable=None, session_factory=None, existing_broker=None):
        self.directory, self.title, self.model, self.timeout, self.executable = directory, title, model, timeout, executable
        self.session_factory = session_factory
        self.existing_broker = existing_broker

    def run(self, outbox):
        import codex_action_transport
        import codex_portfolio_transport as transport
        if not isinstance(outbox, MixedOutbox):
            raise RecordError('FORBIDDEN')
        def callback(broker):
            outbox.source_provider.broker = broker
            try:
                return portfolio_refresh.run_once(outbox, SheetTransport(broker, outbox.target, self.title))
            finally:
                outbox.source_provider.broker = None
        if self.existing_broker is not None:
            broker = self.existing_broker
            if (not isinstance(broker, codex_action_transport.ActionBroker)
                    or not {'get_document', 'fetch', transport.READ} <= broker.readers
                    or transport.WRITE not in broker.writers):
                raise RecordError('FORBIDDEN')
            return {**callback(broker), 'model_turns': 0, 'shared_session': True}
        factory = self.session_factory or codex_action_transport.CodexActionSession
        session = factory(self.directory, readers={'get_document', 'fetch', transport.READ},
                          writers={transport.WRITE}, model=self.model, timeout=self.timeout, executable=self.executable)
        result = session.run(callback)
        if result.get('outcome') != 'driver_completed' or not isinstance(result.get('driver_result'), dict):
            code = result.get('code', 'TRANSPORT_UNKNOWN')
            outbox.record_error(code)
            return {**result, 'outcome': 'error', 'code': code}
        value = result['driver_result']
        if value.get('outcome') not in ('confirmed', 'no_change', 'error', 'manual_review'):
            outbox.record_error('INTEGRITY_FAILED')
            return {**result, 'outcome': 'error', 'code': 'INTEGRITY_FAILED'}
        return {**value, 'model_turns': result.get('model_turns', 0),
                'tool_receipts': result.get('tool_receipts', []),
                'cleanup': result.get('cleanup'), 'driver_thread_settled': result.get('driver_thread_settled')}


GOOGLE_CONFIG = pathlib.Path('.openchamber/workspace-records/portfolio-google.json')


def managed_outbox(checkpoint):
    """Read mixed deployment from fixed workspace, never from event input."""
    import json
    import routing
    workspace = routing.discover_workspace()
    config = json.loads(routing.private_file(workspace, portfolio_refresh.PORTFOLIO_CONFIG).read_text(encoding='utf-8-sig'),
                        object_pairs_hook=routing.unique_object)
    google = json.loads(routing.private_file(workspace, GOOGLE_CONFIG).read_text(encoding='utf-8-sig'),
                        object_pairs_hook=routing.unique_object)
    if (not isinstance(config, dict) or set(config) != {'source_config', 'target'}
            or not isinstance(google, dict) or set(google) != {'schema_version', 'projects'}
            or type(google['schema_version']) is not int or google['schema_version'] != 1
            or not isinstance(google['projects'], dict)):
        raise RecordError('INVALID_ARGUMENT')
    registry = routing.Registry(workspace)
    routes, unused = registry.load()
    google_ids = {project for project, route in routes['projects'].items() if route['backend'] == 'google_head'}
    if set(google['projects']) != google_ids:
        raise RecordError('REVISION_CONFLICT')
    for project, item in google['projects'].items():
        core.ident(project)
        if not isinstance(item, dict) or set(item) != {'head_tab_id', 'summary_tab_id', 'object_folder_id'}:
            raise RecordError('INVALID_ARGUMENT')
        for value in item.values():
            core.ident(value)
    directory = git_store.safe_dir(workspace / routing.RUNTIME / 'portfolio-refresh-mixed')
    policy = routing.private_file(workspace, routing.POLICY)
    privacy_config = {'gitleaks': 'gitleaks', 'policy': str(policy),
                      'gitleaks_config': str(pathlib.Path(__file__).resolve().parents[3] / '.gitleaks.toml')}
    return MixedOutbox(directory, config['source_config'], config['target'], privacy_config, registry, google['projects'], checkpoint)
