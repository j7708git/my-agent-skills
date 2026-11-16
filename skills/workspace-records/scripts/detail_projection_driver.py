"""Verified Apps driver for the existing derived detail-projection protocol.

Canonical receipts remain authoritative independently of this projection. Local
single-writer locking coordinates cooperative processes, not malicious owners.
Ambiguous creates without a known remote ID require human reconciliation.
"""
import copy
import base64
import os
import pathlib
import uuid
from datetime import datetime, timezone
import canonical_writer as canonical
import connector_protocol as protocol
import core
import detail_projection as detail
import git_store
from core import RecordError, object_hash, sha

READERS = {'get_document', 'fetch', 'get_file_metadata'}
WRITERS = {'upload_file', 'update_file'}


def call(broker, tool, arguments, kind):
    if tool not in (READERS if kind == 'read' else WRITERS) or kind not in ('read', 'write'):
        raise RecordError('FORBIDDEN')
    value = broker.request(tool, copy.deepcopy(arguments), kind)
    result, proof = value.result, value.proof
    if (not isinstance(result, dict) or not isinstance(proof, dict)
            or proof.get('tool') not in (tool, 'google_drive.' + tool)
            or proof.get('arguments_sha256') != object_hash(arguments)
            or proof.get('result_sha256') != object_hash(result)
            or proof.get('arguments_exact') is not True or proof.get('tool_completed') is not True
            or not proof.get('action_id') or not proof.get('item_id') or result.get('isError')
            or kind == 'write' and proof.get('approval_correlated') is not True):
        raise RecordError('READBACK_UNVERIFIED')
    return result


def metadata(broker, remote, folder=False):
    core.ident(remote)
    result = call(broker, 'get_file_metadata', {'fileId': remote, 'fields': 'id,parents,mimeType,trashed'}, 'read')
    values = {object_hash(value): value for value in canonical.objects(result)
              if value.get('id') == remote and ('mimeType' in value or 'mime_type' in value)}
    if len(values) != 1:
        raise RecordError('READBACK_UNVERIFIED')
    actual = next(iter(values.values()))
    if ('mimeType' in actual and 'mime_type' in actual and actual['mimeType'] != actual['mime_type']
            or 'parents' in actual and 'parent_ids' in actual and actual['parents'] != actual['parent_ids']):
        raise RecordError('INTEGRITY_FAILED')
    value = {'id': actual['id'], 'mimeType': actual.get('mimeType', actual.get('mime_type')),
             'parents': actual.get('parents', actual.get('parent_ids')), 'trashed': actual.get('trashed')}
    # The current connector normalizes metadata and omits trashed even when
    # requested. Preserve unknown; never synthesize a false observation.
    if value['trashed'] is True or value['trashed'] is not None and type(value['trashed']) is not bool:
        raise RecordError('FORBIDDEN')
    if folder:
        if value['mimeType'] != 'application/vnd.google-apps.folder':
            raise RecordError('FORBIDDEN')
    elif value['mimeType'] != 'text/markdown' or not isinstance(value.get('parents'), list) or len(value['parents']) != 1:
        raise RecordError('FORBIDDEN')
    return value


def downloaded(broker, root, remote, digest, size):
    result = call(broker, 'fetch', {'url': 'https://drive.google.com/file/d/' + remote + '/view',
                                  'download_raw_file': True, 'include_base64': True}, 'read')
    if size is None:
        matches = {}
        for value in canonical.objects(result):
            for key in ('b64_string', 'base64', 'content_base64', 'raw_base64', 'data_base64'):
                encoded = value.get(key)
                if not isinstance(encoded, str) or len(encoded) > 90 * 1024 * 1024: continue
                try: candidate = base64.b64decode(encoded, validate=True)
                except ValueError: continue
                if sha(candidate) == digest: matches[sha(candidate)] = candidate
        if len(matches) != 1: raise RecordError('RAW_READBACK_UNAVAILABLE')
        size = len(next(iter(matches.values())))
    raw = canonical.raw_result(result, {'id': remote, 'sha256': digest, 'size': size})
    directory = detail.storage(root, create=True)
    path = directory / ('readback-' + sha(remote.encode()) + '-' + digest + '.md')
    if os.path.lexists(path):
        if protocol.file_bytes(root, str(path)) != raw:
            raise RecordError('INTEGRITY_FAILED')
    else:
        with open(path, 'xb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    return str(path)


def read_receipt(registry, project, digest):
    if not isinstance(digest, str) or not __import__('re').fullmatch('[a-f0-9]{64}', digest):
        raise RecordError('INVALID_ARGUMENT')
    directory = git_store.safe_dir(registry.workspace / '.openchamber/workspace-records/canonical-receipts')
    receipt = git_store.read_json(directory / (digest + '.json'))
    if object_hash(receipt) != digest or receipt.get('project_id') != project or receipt.get('schema_version') != 1:
        raise RecordError('INTEGRITY_FAILED')
    core.ident(receipt['operation_id'])
    return receipt


class DetailDriver:
    def __init__(self, registry, reader, mapping, receipt_sha256):
        if not isinstance(reader, canonical.CanonicalReader) or not isinstance(mapping, dict) or set(mapping) != {'folder_id', 'archive_folder_id'}:
            raise RecordError('INVALID_ARGUMENT')
        for value in mapping.values(): core.ident(value)
        if mapping['folder_id'] == mapping['archive_folder_id']:
            raise RecordError('FORBIDDEN')
        self.registry, self.reader, self.mapping, self.receipt_sha256 = registry, reader, copy.deepcopy(mapping), receipt_sha256
        self.broker = reader.broker

    def run(self, project):
        core.ident(project)
        receipt = read_receipt(self.registry, project, self.receipt_sha256)
        binding = self.registry.bind(project)
        root = git_store.safe_dir(binding['runtime_directory'])
        storage = detail.storage(root, create=True)
        # A separate driver lock spans remote calls; protocol keeps its original
        # short runtime locks, so there is no recursive acquisition.
        with git_store.locked(storage):
            return self.locked_run(binding, root, receipt)

    def locked_run(self, binding, root, receipt):
        project, canonical_operation = receipt['project_id'], receipt['operation_id']
        observed = self.reader.read_authority(binding, uuid.uuid4().hex)
        self.registry.check(binding, observed['authority'])
        head = observed['snapshot']['head']
        writer = canonical.CanonicalWriter(self.registry, self.reader)
        existing = writer.graph(binding, head)
        graph, operations, objects, _, _ = protocol.read_graph(root, head, existing)
        op = operations.get(canonical_operation)
        if (not op or op.get('request_hash') != receipt['request_hash']
                or object_hash(op.get('result')) != receipt['result_sha256']
                or receipt['operation_epoch'] != head['control']['epoch']
                or op['result'].get('epoch') != receipt['operation_epoch']
                or op['result'].get('logical_version') != receipt['operation_version']
                or receipt['operation_version'] > head['version']):
            raise RecordError('READBACK_UNVERIFIED')
        operation = 'detail-' + sha((self.receipt_sha256 + ':' + object_hash(head)).encode())
        for folder in self.mapping.values(): metadata(self.broker, folder, True)
        scope = {'project_id': project, 'record_ids': sorted([r['record']['record_id'] for r in graph['records']] +
                    [a['attachment']['attachment_id'] for a in graph['attachments']]), **self.mapping}
        base = {'project_id': project, 'actor_role': 'pm', 'operation_id': operation, 'scope': scope,
                'head_tab_id': self.reader.config['head_tab_id'], 'summary_tab_id': self.reader.config['summary_tab_id'], 'existing': existing}
        generation_hash = object_hash(head)
        def fresh():
            current = self.reader.read_authority(binding, uuid.uuid4().hex)
            self.registry.check(binding, current['authority'])
            if object_hash(current['snapshot']['head']) != generation_hash:
                raise RecordError('REVISION_CONFLICT')
            return {**base, 'document': current['document']}
        transport_path = detail.storage(root) / ('transport-' + sha(operation.encode()) + '.json')
        transport = git_store.read_json(transport_path) if os.path.lexists(transport_path) else {'schema_version': 1, 'operation_id': operation, 'canonical_operation_id': canonical_operation, 'receipt_sha256': self.receipt_sha256, 'items': {}}
        if transport.get('operation_id') != operation or transport.get('receipt_sha256') != self.receipt_sha256:
            raise RecordError('ID_REUSE')
        state = detail.load(root, project)
        active = state['active']
        if active and active['operation_id'] != operation and active['phase'] != 'complete':
            old_path = detail.storage(root) / ('transport-' + sha(active['operation_id'].encode()) + '.json')
            old_transport = git_store.read_json(old_path)
            if (old_transport.get('operation_id') != active['operation_id']
                    or old_transport.get('receipt_sha256') != self.receipt_sha256
                    or old_transport.get('canonical_operation_id') != canonical_operation):
                raise RecordError('MANUAL_REVIEW')
            if any(i['status'] == 'unknown' for i in active['items']):
                def old_fresh():
                    return {**fresh(), 'operation_id': active['operation_id'], 'scope': active['scope']}
                self.reconcile_unknown(root, old_fresh, old_path, old_transport, active, allow_stale=True)
        if active and active['operation_id'] == operation and any(i['status'] == 'unknown' for i in active['items']):
            self.reconcile_unknown(root, fresh, transport_path, transport, active)
        prepared = detail.prepare(fresh(), self.registry)
        if prepared.get('freshness') == 'current':
            return self.confirmed(root, receipt, prepared)
        for _ in range(10000):
            begin = detail.begin(fresh(), self.registry)
            if begin['outcome'] == 'no_write':
                return self.confirmed(root, receipt, begin)
            if begin['outcome'] == 'readback_required':
                state = detail.load(root, project)
                mapped = state['index'] if begin['key'] in ('stale-index', 'current-index') else next((v for v in state['files'].values() if v['file_id'] == begin['file_id']), None)
                if mapped is None: raise RecordError('INTEGRITY_FAILED')
                meta = metadata(self.broker, begin['file_id'])
                observed_file = downloaded(self.broker, root, begin['file_id'], begin['expected_sha256'], None)
                begin = detail.begin({**fresh(), 'readback_remote_id': begin['file_id'], 'observed_parent_id': meta['parents'][0], 'downloaded_file': observed_file}, self.registry)
            if begin['outcome'] != 'send_once': raise RecordError('MANUAL_REVIEW')
            item = next(i for i in detail.load(root, project)['active']['items'] if i['key'] == begin['key'])
            record = {'key': begin['key'], 'payload_sha256': object_hash(begin['payload']), 'remote_id': item.get('expected_id'), 'state': 'sending'}
            transport['items'][begin['key']] = record
            git_store.write_json(transport_path, transport)
            result = call(self.broker, begin['action'], begin['payload'], 'write')
            remote = item.get('expected_id') or canonical.uploaded_id(result)
            record.update(remote_id=remote, state='known_id'); git_store.write_json(transport_path, transport)
            self.accept(root, fresh, item, remote)
            record['state'] = 'verified'; git_store.write_json(transport_path, transport)
        raise RecordError('MANUAL_REVIEW')

    def accept(self, root, fresh, item, remote, allow_stale=False):
        meta = metadata(self.broker, remote)
        path = downloaded(self.broker, root, remote, item['sha256'], item['size'])
        value = detail.accept({**fresh(), 'readback_remote_id': remote, 'observed_parent_id': meta['parents'][0], 'downloaded_file': path}, self.registry)
        if value.get('phase') == 'stale' and not allow_stale: raise RecordError('REVISION_CONFLICT')
        return value

    def reconcile_unknown(self, root, fresh, path, transport, active, allow_stale=False):
        unknown = [i for i in active['items'] if i['status'] == 'unknown']
        if len(unknown) != 1: raise RecordError('MANUAL_REVIEW')
        item = unknown[0]; known = transport['items'].get(item['key'], {})
        remote = known.get('remote_id') or item.get('expected_id')
        if not remote: raise RecordError('MANUAL_REVIEW')
        self.accept(root, fresh, item, remote, allow_stale=allow_stale)
        known['state'] = 'verified'; transport['items'][item['key']] = known
        git_store.write_json(path, transport)

    def confirmed(self, root, receipt, result):
        state = detail.load(root, receipt['project_id'])
        if not state['index'] or state['index'].get('freshness') != 'current':
            raise RecordError('READBACK_UNVERIFIED')
        for mapped in list(state['files'].values()) + [state['index']]:
            meta = metadata(self.broker, mapped['file_id'])
            if meta['parents'] != [self.mapping['folder_id']] or mapped.get('generation') != result['generation']:
                raise RecordError('READBACK_UNVERIFIED')
            downloaded(self.broker, root, mapped['file_id'], mapped['sha256'], None)
        binding = self.registry.bind(receipt['project_id'])
        observed = self.reader.read_authority(binding, uuid.uuid4().hex)
        self.registry.check(binding, observed['authority'])
        head = observed['snapshot']['head']
        generation = {'epoch': head['control']['epoch'], 'version': head['version'], 'root_hash': head['root_manifest']['sha256']}
        if generation != result['generation']: raise RecordError('REVISION_CONFLICT')
        value = {'schema_version': 1, 'outcome': 'detail_projection_confirmed', 'canonical_receipt_sha256': self.receipt_sha256,
                 'operation_id': receipt['operation_id'], 'projection_operation_id': state['active']['operation_id'], 'generation': result['generation'], 'freshness': 'current',
                 'observed_at': datetime.now(timezone.utc).isoformat(), 'search_index_freshness': 'unknown'}
        git_store.write_json(detail.storage(root) / ('receipt-' + sha(receipt['operation_id'].encode()) + '.json'), value)
        return value


def refresh_details(registry, reader, mapping, project, receipt_sha256):
    try:
        return DetailDriver(registry, reader, mapping, receipt_sha256).run(project)
    except Exception as error:
        return {'outcome': 'detail_projection_pending', 'code': error.code if isinstance(error, RecordError) else 'CONTROL_UNAVAILABLE',
                'canonical_receipt_sha256': receipt_sha256, 'automatic_retry': False}
