"""Synthetic canonical graph integration; no network or runtime authenticity claim."""
import base64
import copy
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / 'skills/workspace-records/scripts'
sys.path.insert(0, str(SCRIPTS))
import canonical_writer as writer
import connector_protocol as protocol
import core
import dispatch_guard
import git_store
import google_docs
import routing
from test_google_docs import doc, head
from test_connector_protocol import scan, batch_scan, update_doc


class Broker:
    def __init__(self, document, blobs):
        self.document, self.blobs = document, blobs
        self.calls = []
        self.failure = None
        self.bad_proof = False

    def request(self, tool, arguments, kind):
        self.calls.append((tool, copy.deepcopy(arguments), kind))
        if tool == 'get_document':
            value = copy.deepcopy(self.document)
        elif tool == 'fetch':
            remote = arguments['url'].split('/d/')[1].split('/')[0]
            value = {'base64': base64.b64encode(self.blobs[remote]).decode()}
        elif tool == 'upload_file':
            if self.failure == 'upload':
                raise core.RecordError('TRANSPORT_UNKNOWN')
            remote = 'new-' + str(len(self.blobs))
            self.blobs[remote] = pathlib.Path(arguments['file_uri']).read_bytes()
            value = {'id': remote, 'mime_type': arguments['mime_type'], 'url': 'https://drive.google.com/file/d/' + remote + '/view'}
        elif tool == 'batch_update_document':
            if arguments['write_control']['requiredRevisionId'] != self.document['revisionId']:
                raise core.RecordError('REVISION_CONFLICT')
            self.document = update_doc(self.document, arguments)
            self.document['revisionId'] = 'committed-revision-' + str(len(self.calls))
            if self.failure == 'cas_after':
                raise core.RecordError('TRANSPORT_UNKNOWN')
            value = {'documentId': 'doc'}
        else:
            raise AssertionError(tool)
        result = {'structuredContent': value}
        proof = {'action_id': 'action-' + str(len(self.calls)), 'tool': tool,
                 'item_id': 'item-' + str(len(self.calls)), 'arguments_sha256': core.object_hash(arguments),
                 'result_sha256': core.object_hash(result), 'arguments_exact': not self.bad_proof,
                 'tool_completed': True, 'approval_correlated': kind == 'write'}
        return types.SimpleNamespace(result=result, proof=proof)


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        (self.root / 'project-notes/projects/Demo').mkdir(parents=True)
        (self.root / routing.RUNTIME).mkdir(parents=True)
        (self.root / routing.POLICY).write_text('{}', encoding='utf-8')
        data = {'schema_version': 1, 'policy_version': '1', 'generation': 1,
                'projects': {'Demo': {'classification': 'managed', 'backend': 'google_head',
                                     'route_phase': 'steady', 'locator': {'document_id': 'doc'}}}}
        git_store.write_json(self.root / routing.REGISTRY, data)
        self.registry = routing.Registry(self.root)
        self.binding = self.registry.bind('Demo')
        pathlib.Path(self.binding['runtime_directory']).mkdir()
        self.blobs = {}
        def put(name, raw):
            self.blobs[name] = raw
            return {'id': name, 'sha256': core.sha(raw), 'size': len(raw)}
        original = b'# synthetic task\n\nbefore\n'
        blob = put('task-blob', original)
        page = put('history', core.canonical({'schema_version': 1, 'events': [], 'journal': [], 'previous_root': None}))
        self.manifest = {'schema_version': 1, 'project_id': 'Demo', 'version': 1,
            'records': [{'record': {'record_id': 'task', 'kind': 'task', 'created_at': None, 'updated_at': None,
               'source': {'relative_path': 'tasks/synthetic.md', 'content_sha256': blob['sha256'], 'original_time': None},
               'body': {'format': 'utf-8'}, 'metadata': {'decision': 'preserve'}, 'relations': []}, 'object': blob}],
            'attachments': [], 'history_pages': [page], 'source_root': {}}
        rootref = put('root', core.canonical(self.manifest))
        h = head(); h['root_manifest'] = rootref
        self.document = update_doc(doc(), google_docs.batch(
            google_docs.snapshot(doc(), 'doc', 'machine', 'human', 'Demo', bootstrap=True), h))
        self.broker = Broker(self.document, self.blobs)
        self.reader = writer.CanonicalReader(self.broker, {'head_tab_id': 'machine', 'summary_tab_id': 'human', 'object_folder_id': 'objects-folder'})
        self.request = {'schema_version': 1, 'request_id': 'request', 'project_id': 'Demo', 'actor_role': 'worker',
            'scope': {'project_id': 'Demo', 'actions': ['replace'],
                      'records': [{'record_id': 'task', 'path': 'tasks/synthetic.md', 'fields': ['body']}]},
            'operation_id': 'event-update', 'action': 'replace', 'target_record_id': 'task',
            'payload': {'base_sha256': core.sha(original), 'content': '# synthetic task\n\nafter\n'},
            'expected': {'mode': 'google', 'phase': 'steady', 'epoch': 1, 'version': 1}}
        self.scanner = patch.object(protocol, 'scan_bytes', side_effect=scan)
        self.batch_scanner = patch.object(protocol.privacy, 'check', side_effect=batch_scan)
        self.scanner.start(); self.batch_scanner.start()
        self.addCleanup(self.scanner.stop); self.addCleanup(self.batch_scanner.stop); self.addCleanup(self.temp.cleanup)

    def test_managed_roundtrip_uses_real_protocol_graph_and_exact_cas(self):
        result = writer.CanonicalWriter(self.registry, self.reader).execute(self.request)
        self.assertEqual(result['outcome'], 'canonical_confirmed')
        self.assertEqual(result['result']['logical_version'], 2)
        self.assertEqual(result['request_hash'], core.request_hash(self.request))
        receipt = result['receipt']
        self.assertEqual(core.object_hash(receipt), result['receipt_sha256'])
        stored = self.root / '.openchamber/workspace-records/canonical-receipts' / (result['receipt_sha256'] + '.json')
        self.assertEqual(json.loads(stored.read_text()), receipt)
        self.assertEqual(receipt['operation_version'], 2)
        self.assertIn('人工補充', str(self.broker.document))
        new_head = self.reader.read_authority(self.binding, 'readback')['snapshot']['head']
        raw = self.blobs[new_head['root_manifest']['id']]
        graph = json.loads(raw)
        self.assertEqual(graph['records'][0]['record']['metadata']['decision'], 'preserve')
        self.assertEqual(self.blobs[graph['records'][0]['object']['id']], self.request['payload']['content'].encode())
        cas = [args for tool, args, kind in self.broker.calls if tool == 'batch_update_document']
        self.assertEqual(len(cas), 1)
        self.assertEqual(cas[0]['write_control'], {'requiredRevisionId': self.document['revisionId']})
        uploads = sum(tool == 'upload_file' for tool, _, _ in self.broker.calls)
        duplicate = writer.CanonicalWriter(self.registry, self.reader).execute(self.request)
        self.assertEqual(duplicate['outcome'], 'canonical_confirmed')
        self.assertEqual(sum(tool == 'upload_file' for tool, _, _ in self.broker.calls), uploads)
        self.assertEqual(sum(tool == 'batch_update_document' for tool, _, _ in self.broker.calls), 1)

    def test_upload_unknown_is_durable_and_never_recreated(self):
        self.broker.failure = 'upload'
        with self.assertRaisesRegex(core.RecordError, 'TRANSPORT_UNKNOWN'):
            writer.CanonicalWriter(self.registry, self.reader).execute(self.request)
        _, state = protocol.load_session(pathlib.Path(self.binding['runtime_directory']), self.request)
        self.assertTrue(any(item['status'] == 'sending' for item in state['artifacts']))
        with self.assertRaisesRegex(core.RecordError, 'MANUAL_REVIEW'):
            writer.CanonicalWriter(self.registry, self.reader).execute(self.request)
        self.assertEqual(sum(tool == 'upload_file' for tool, _, _ in self.broker.calls), 1)

    def test_cas_after_effect_unknown_can_be_verified_from_same_operation_history(self):
        self.broker.failure = 'cas_after'
        with self.assertRaisesRegex(core.RecordError, 'TRANSPORT_UNKNOWN'):
            writer.CanonicalWriter(self.registry, self.reader).execute(self.request)
        _, state = protocol.load_session(pathlib.Path(self.binding['runtime_directory']), self.request)
        self.assertEqual(state['phase'], 'cas_unknown')
        result = writer.CanonicalWriter(self.registry, self.reader).execute(self.request)
        self.assertEqual(result['outcome'], 'canonical_confirmed')
        _, settled = protocol.load_session(pathlib.Path(self.binding['runtime_directory']), self.request)
        self.assertEqual(settled['phase'], 'committed')
        self.assertEqual(sum(tool == 'batch_update_document' for tool, _, _ in self.broker.calls), 1)

    def test_unverified_tool_result_refuses_before_upload(self):
        self.broker.bad_proof = True
        with self.assertRaisesRegex(core.RecordError, 'READBACK_UNVERIFIED'):
            writer.CanonicalWriter(self.registry, self.reader).execute(self.request)
        self.assertFalse(any(kind == 'write' for _, _, kind in self.broker.calls))

    def test_native_fetch_b64_string_is_verified_by_expected_hash_and_size(self):
        raw = b'canonical bytes\r\n'
        reference = {'id': 'object', 'sha256': core.sha(raw), 'size': len(raw)}
        payload = {'structuredContent': {'b64_string': base64.b64encode(raw).decode(),
                                        'file_uri': {'download_url': 'ignored'}}}
        self.assertEqual(writer.raw_result(payload, reference), raw)
        payload['structuredContent']['b64_string'] = base64.b64encode(b'tampered').decode()
        with self.assertRaisesRegex(core.RecordError, 'RAW_READBACK_UNAVAILABLE'):
            writer.raw_result(payload, reference)

    def test_raw_file_uri_never_read_as_canonical_bytes(self):
        with self.assertRaisesRegex(core.RecordError, 'RAW_READBACK_UNAVAILABLE'):
            writer.raw_result({'file_uri': str(self.root / 'secret')}, {'id': 'f', 'sha256': 'a' * 64, 'size': 1})

    def test_google_guard_prepare_and_claim_each_do_fresh_actual_read(self):
        event = {'project_id': 'Demo', 'event_id': 'delivery', 'action': 'execute', 'owner_verified': True, 'ready_verified': True}
        prepared = dispatch_guard.prepare(self.registry, event, self.reader)
        claim = dispatch_guard.claim(self.registry, prepared, self.reader)
        self.assertTrue(claim['launch'])
        self.assertEqual(sum(tool == 'get_document' for tool, _, _ in self.broker.calls), 2)
        self.assertFalse(dispatch_guard.claim(self.registry, prepared, self.reader)['launch'])
        self.assertEqual(sum(tool == 'get_document' for tool, _, _ in self.broker.calls), 3)

    def test_google_claim_detects_head_changed_between_prepare_and_claim(self):
        event = {'project_id': 'Demo', 'event_id': 'delivery', 'action': 'execute', 'owner_verified': True, 'ready_verified': True}
        prepared = dispatch_guard.prepare(self.registry, event, self.reader)
        self.broker.document['revisionId'] = 'external-revision'
        with self.assertRaisesRegex(core.RecordError, 'REVISION_CONFLICT'):
            dispatch_guard.claim(self.registry, prepared, self.reader)

    def test_transition_not_allowed_as_issue_writer_action(self):
        request = copy.deepcopy(self.request)
        request.update(action='freeze', target_record_id=None, payload={})
        request['scope']['actions'] = ['freeze']
        with self.assertRaises(core.RecordError):
            writer.CanonicalWriter(self.registry, self.reader).execute(request)


if __name__ == '__main__':
    unittest.main()
