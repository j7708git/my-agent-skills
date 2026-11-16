"""Managed canonical writer driven only by verified Codex Apps tool results.

Uses connector_protocol's existing durable stages, immutable objects and Docs
revision CAS. No alternate authority, credential storage, legacy prompt or
model-final-text completion path. Deployment and broker are cooperative trusted
runtime inputs; these Python types are not an authentication mechanism.
"""
import base64
import copy
import os
import pathlib
import uuid
from datetime import datetime, timezone

import connector_protocol as protocol
import core
import git_store
import google_docs
import manifest
from core import RecordError, object_hash, sha

READERS = {'get_document', 'fetch'}
WRITERS = {'upload_file', 'batch_update_document'}


def objects(value, depth=0):
    if depth > 20:
        raise RecordError('INTEGRITY_FAILED')
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from objects(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from objects(item, depth + 1)
    elif isinstance(value, str) and len(value) < 16 * 1024 * 1024:
        try:
            parsed = __import__('json').loads(value)
        except ValueError:
            return
        if not isinstance(parsed, str):
            yield from objects(parsed, depth + 1)


def verified_call(broker, tool, arguments, kind):
    if (kind == 'read' and tool not in READERS or kind == 'write' and tool not in WRITERS
            or kind not in ('read', 'write')):
        raise RecordError('FORBIDDEN')
    received = broker.request(tool, copy.deepcopy(arguments), kind)
    # Only the broker produces this wrapper from actual tool events. Arbitrary
    # issue JSON/authority_state and agent final prose are never accepted here.
    result, proof = received.result, received.proof
    if (not isinstance(result, dict) or not isinstance(proof, dict)
            or proof.get('tool') not in (tool, 'google_drive.' + tool)
            or proof.get('arguments_sha256') != object_hash(arguments)
            or proof.get('result_sha256') != object_hash(result)
            or proof.get('arguments_exact') is not True or proof.get('tool_completed') is not True
            or not proof.get('action_id') or not proof.get('item_id')
            or kind == 'write' and proof.get('approval_correlated') is not True
            or result.get('isError')):
        raise RecordError('READBACK_UNVERIFIED')
    return result, copy.deepcopy(proof)


def document_result(result, document_id):
    candidates = {object_hash(value): value for value in objects(result)
                  if value.get('documentId') == document_id and isinstance(value.get('revisionId'), str)
                  and isinstance(value.get('tabs'), list)}
    if len(candidates) != 1:
        raise RecordError('READBACK_UNVERIFIED')
    return next(iter(candidates.values()))


def raw_result(result, reference):
    """Decode complete inline raw bytes; never follow a returned host file_uri."""
    matching = {}
    for value in objects(result):
        for key in ('b64_string', 'base64', 'content_base64', 'raw_base64', 'data_base64'):
            text = value.get(key)
            if not isinstance(text, str) or len(text) > 90 * 1024 * 1024:
                continue
            try:
                raw = base64.b64decode(text, validate=True)
                manifest.verify_bytes(raw, reference)
            except (ValueError, RecordError):
                continue
            matching[sha(raw)] = raw
    if len(matching) != 1:
        raise RecordError('RAW_READBACK_UNAVAILABLE')
    return next(iter(matching.values()))


def uploaded_id(result):
    ids = set()
    for value in objects(result):
        for key in ('id', 'file_id'):
            candidate = value.get(key)
            if isinstance(candidate, str) and ('mime_type' in value or 'mimeType' in value or 'url' in value or 'webViewLink' in value):
                core.ident(candidate)
                ids.add(candidate)
    if len(ids) != 1:
        raise RecordError('UPLOAD_OUTCOME_UNKNOWN')
    return ids.pop()


class CanonicalReader:
    """A fresh actual get_document for every guard prepare/claim observation."""
    def __init__(self, broker, project_config):
        if set(project_config) != {'head_tab_id', 'summary_tab_id', 'object_folder_id'}:
            raise RecordError('INVALID_ARGUMENT')
        core.ident(project_config['object_folder_id'])
        self.broker = broker
        self.config = copy.deepcopy(project_config)
        self.last = None

    def read_authority(self, binding, nonce):
        if binding['backend'] != 'google_head' or binding['route_phase'] != 'steady':
            raise RecordError('PHASE_FROZEN')
        arguments = {'document_id': binding['locator']['document_id']}
        result, proof = verified_call(self.broker, 'get_document', arguments, 'read')
        document = document_result(result, arguments['document_id'])
        snap = google_docs.snapshot(document, arguments['document_id'], self.config['head_tab_id'],
                                    self.config['summary_tab_id'], binding['project_id'])
        value = {'nonce': nonce, 'document': document, 'snapshot': snap,
                 'authority': {'document_id': snap['document_id'], 'revision_id': snap['revision'],
                               'control': {**snap['head']['control'], 'version': snap['head']['version']}},
                 'proof': proof}
        self.last = value
        return value


class CanonicalWriter:
    def __init__(self, registry, reader):
        if not isinstance(reader, CanonicalReader):
            raise RecordError('CONNECTOR_UNAVAILABLE')
        self.registry, self.reader = registry, reader
        self.proofs = []

    def download(self, binding, reference):
        manifest.reference(reference)
        if reference.get('encoding') == 'inline-empty-v1':
            return None
        args = {'url': 'https://drive.google.com/file/d/' + reference['id'] + '/view',
                'download_raw_file': True, 'include_base64': True}
        result, proof = verified_call(self.reader.broker, 'fetch', args, 'read')
        raw = raw_result(result, reference)
        root = git_store.safe_dir(binding['runtime_directory'])
        directory = root / 'connector-downloads'
        if not os.path.lexists(directory):
            directory.mkdir(mode=0o700)
        directory = git_store.safe_dir(directory)
        path = directory / (sha(reference['id'].encode()) + '-' + reference['sha256'] + '.download')
        if os.path.lexists(path):
            manifest.verify_bytes(protocol.file_bytes(root, str(path)), reference)
        else:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        self.proofs.append(proof)
        return str(path)

    def graph(self, binding, head):
        root = git_store.safe_dir(binding['runtime_directory'])
        path = self.download(binding, head['root_manifest'])
        value = manifest.parse_json(protocol.file_bytes(root, path))
        manifest.validate_manifest(value, binding['project_id'])
        refs = value['history_pages'] + [r['object'] for r in value['records']] + [a['object'] for a in value['attachments']]
        identities = {}
        for reference in refs:
            previous = identities.setdefault(reference['id'], reference)
            if previous != reference:
                raise RecordError('INTEGRITY_FAILED')
        existing = {'manifest_file': path, 'objects': {rid: self.download(binding, ref)
                    for rid, ref in identities.items() if ref.get('encoding') != 'inline-empty-v1'}}
        protocol.read_graph(root, head, existing)
        return existing

    def confirmed(self, request, observed, result):
        head = observed['snapshot']['head']
        receipt = {'schema_version': 1, 'project_id': request['project_id'],
                   'operation_id': request['operation_id'], 'request_hash': core.request_hash(request),
                   'result_sha256': object_hash(result), 'operation_epoch': result['epoch'],
                   'operation_version': result['logical_version'], 'observed_head_hash': object_hash(head),
                   'observed_head_version': head['version'], 'observed_head_epoch': head['control']['epoch'],
                   'observed_revision': observed['snapshot']['revision'],
                   'observed_at': datetime.now(timezone.utc).isoformat()}
        if (receipt['operation_epoch'] != receipt['observed_head_epoch']
                or receipt['observed_head_version'] < receipt['operation_version']):
            raise RecordError('READBACK_UNVERIFIED')
        digest = object_hash(receipt)
        directory = self.registry.workspace / '.openchamber/workspace-records/canonical-receipts'
        if not os.path.lexists(directory):
            directory.mkdir(mode=0o700)
        directory = git_store.safe_dir(directory)
        path = directory / (digest + '.json')
        if os.path.lexists(path):
            if git_store.read_json(path) != receipt:
                raise RecordError('INTEGRITY_FAILED')
        else:
            git_store.write_json(path, receipt)
        return {'outcome': 'canonical_confirmed', 'result': result,
                'request_hash': receipt['request_hash'], 'head_hash': receipt['observed_head_hash'],
                'revision': receipt['observed_revision'], 'receipt': receipt, 'receipt_sha256': digest,
                'tool_proofs': copy.deepcopy(self.proofs)}

    def execute(self, request):
        core.validate_request(request, True)
        if request['action'] not in ('replace', 'append', 'status_patch', 'batch'):
            raise RecordError('FORBIDDEN')
        project = request['project_id']
        binding = self.registry.bind(project)
        self.registry.require_no_activation(project)
        observed = self.reader.read_authority(binding, uuid.uuid4().hex)
        self.registry.check(binding, observed['authority'])
        self.proofs.append(observed['proof'])
        head = observed['snapshot']['head']
        existing = self.graph(binding, head)
        root = git_store.safe_dir(binding['runtime_directory'])
        _, operations, _, _, _ = protocol.read_graph(root, head, existing)
        operation = operations.get(request['operation_id'])
        if operation:
            if operation['request_hash'] != core.request_hash(request):
                raise RecordError('ID_REUSE')
            try:
                _, local = protocol.load_session(root, request)
            except RecordError as error:
                if error.code != 'NOT_FOUND':
                    raise
            else:
                if local['phase'] == 'cas_unknown':
                    settled = protocol.managed_dispatch('reconcile-cas', {
                        'project_id': project, 'operation_id': request['operation_id'],
                        'document': observed['document'], 'manifest_readback_file': existing['manifest_file']}, self.registry)
                    if settled.get('outcome') != 'committed':
                        raise RecordError('MANUAL_REVIEW')
                elif local['phase'] != 'committed':
                    raise RecordError('MANUAL_REVIEW')
            return self.confirmed(request, observed, operation['result'])
        core.check_control({**head['control'], 'version': head['version']}, request['expected'])
        args = {'request': request, 'document_id': binding['locator']['document_id'],
                'head_tab_id': self.reader.config['head_tab_id'], 'summary_tab_id': self.reader.config['summary_tab_id'],
                'document': observed['document'], 'existing': existing}
        # Existing ambiguous operations must be reconciled by the original
        # protocol. Do not recreate uploads or hide them under a new operation.
        try:
            protocol.load_session(root, {'project_id': project, 'operation_id': request['operation_id']})
        except RecordError as error:
            if error.code != 'NOT_FOUND':
                raise
        else:
            raise RecordError('MANUAL_REVIEW')
        prepared = protocol.managed_dispatch('prepare', args, self.registry)
        key = {'project_id': project, 'operation_id': request['operation_id']}

        def call(command, extra=None):
            current = self.reader.read_authority(binding, uuid.uuid4().hex)
            self.proofs.append(current['proof'])
            return protocol.managed_dispatch(command, {**key, 'document': current['document'], **(extra or {})}, self.registry)

        def upload(artifact_id):
            action = call('begin-upload', {'artifact_id': artifact_id})
            payload = {key: action[key] for key in ('file_uri', 'file_name', 'mime_type')}
            payload['parent_folder_id'] = self.reader.config['object_folder_id']
            response, proof = verified_call(self.reader.broker, 'upload_file', payload, 'write')
            self.proofs.append(proof)
            remote = uploaded_id(response)
            reference = {'id': remote, 'sha256': action['sha256'], 'size': action['size']}
            downloaded = self.download(binding, reference)
            call('accept-upload', {'artifact_id': artifact_id, 'remote_id': remote,
                                  'readback_remote_id': remote, 'downloaded_file': downloaded})

        for artifact in prepared['artifacts']:
            upload(artifact['artifact_id'])
        result = call('prepare-cas')
        if result['outcome'] != 'manifest_ready':
            raise RecordError('INTEGRITY_FAILED')
        upload(result['artifact_id'])
        ready = call('prepare-cas')
        action = call('begin-cas', {'batch_hash': ready['batch_hash']})
        batch = action['batch']
        arguments = {'document_id': batch['document_id'], 'requests': batch['requests'],
                     'write_control': batch['write_control']}
        _, proof = verified_call(self.reader.broker, 'batch_update_document', arguments, 'write')
        self.proofs.append(proof)
        after = self.reader.read_authority(binding, uuid.uuid4().hex)
        self.proofs.append(after['proof'])
        new_head = after['snapshot']['head']
        verified_graph = self.graph(binding, new_head)
        result = protocol.managed_dispatch('reconcile-cas', {**key, 'document': after['document'],
                  'manifest_readback_file': verified_graph['manifest_file']}, self.registry)
        if result.get('outcome') != 'committed':
            raise RecordError('OUTCOME_UNKNOWN')
        _, ops, _, _, _ = protocol.read_graph(root, new_head, verified_graph)
        if ops.get(request['operation_id'], {}).get('request_hash') != core.request_hash(request):
            raise RecordError('READBACK_UNVERIFIED')
        return self.confirmed(request, after, result['result'])
