import base64
import copy
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / 'skills/workspace-records/scripts'
sys.path.insert(0, str(SCRIPTS))
import canonical_writer
import codex_action_transport
import connector_protocol
import core
import git_store
import google_docs
import portfolio_mixed
import portfolio_consumer
import portfolio_refresh
import routing
import test_core
import test_google_docs

class Broker:
    def __init__(self, document, raw):
        self.document, self.raw, self.calls, self.fail = document, raw, [], False
    def request(self, tool, arguments, kind):
        self.calls.append(tool)
        if self.fail:
            raise core.RecordError('CONNECTOR_UNAVAILABLE')
        if tool == 'get_document':
            result = {'structuredContent': copy.deepcopy(self.document)}
        elif tool == 'fetch':
            rid = arguments['url'].split('/')[-2]
            result = {'base64': base64.b64encode(self.raw[rid]).decode()}
        else:
            raise AssertionError(tool)
        proof = {'tool': tool, 'arguments_sha256': core.object_hash(arguments),
                 'result_sha256': core.object_hash(result), 'arguments_exact': True,
                 'tool_completed': True, 'action_id': 'action', 'item_id': 'item'}
        return codex_action_transport.VerifiedToolResult(result, proof)

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = pathlib.Path(self.temp.name)
        for name in ('Demo', 'Legacy_Project'):
            (self.root / 'project-notes/projects' / name).mkdir(parents=True)
        (self.root / 'project-notes/projects/Legacy_Project/status.md').write_bytes(test_core.STATUS)
        (self.root / '.openchamber/workspace-records/runtime').mkdir(parents=True)
        (self.root / '.openchamber/repo-privacy-policy.json').write_text('{}')
        self.routes = {'schema_version': 1, 'policy_version': '1', 'generation': 1, 'projects': {
            'Demo': {'classification': 'managed', 'backend': 'google_head', 'route_phase': 'steady', 'locator': {'document_id': 'doc'}},
            'Legacy_Project': {'classification': 'unmigrated_git', 'backend': 'git_local', 'route_phase': 'steady', 'locator': None}}}
        git_store.write_json(self.root / routing.REGISTRY, self.routes)
        self.registry = routing.Registry(self.root)
        binding = self.registry.bind('Demo'); pathlib.Path(binding['runtime_directory']).mkdir()
        self.raw = {}
        def put(identifier, data):
            self.raw[identifier] = data
            return {'id': identifier, 'sha256': core.sha(data), 'size': len(data)}
        status = put('status', test_core.STATUS)
        record = {'record_id': 'status', 'kind': 'status', 'created_at': None, 'updated_at': None,
                  'source': {'relative_path': 'status.md', 'content_sha256': status['sha256'], 'original_time': None},
                  'body': {'format': 'utf-8'}, 'metadata': {}, 'relations': []}
        self.result = {'logical_version': 2, 'epoch': 1, 'phase': 'steady', 'affected_ids': ['status']}
        self.operation = {'operation_id': 'operation-1', 'request_hash': 'a' * 64, 'result': self.result}
        page = put('history', core.canonical({'schema_version': 1, 'events': [], 'journal': [self.operation]}))
        value = {'schema_version': 1, 'project_id': 'Demo', 'version': 2,
                 'records': [{'record': record, 'object': status}], 'attachments': [],
                 'history_pages': [page], 'source_root': {}}
        rootref = put('root', core.canonical(value))
        head = test_google_docs.head(); head['version'] = 2; head['root_manifest'] = rootref
        head['last_operation'] = {'operation_id': 'operation-1', 'request_hash': 'a' * 64}
        head['summary'] = connector_protocol.status_summary(value['records'], {'status': test_core.STATUS})
        self.head = head
        self.document = test_google_docs.doc()
        def lines(values):
            return [{'paragraph': {'elements': [{'textRun': {'content': v + '\n'}}]}} for v in values]
        self.document['tabs'][0]['body']['content'][1:1] = lines([google_docs.head_line(head)])
        self.document['tabs'][1]['body']['content'][1:1] = lines(google_docs.summary_lines(head))
        self.broker = Broker(self.document, self.raw)
        self.factory_calls = 0
        def factory(project, binding):
            self.factory_calls += 1
            return canonical_writer.CanonicalReader(self.broker, {'head_tab_id': 'machine',
                'summary_tab_id': 'human', 'object_folder_id': 'folder'})
        self.factory = factory
        receipt = {'schema_version': 1, 'project_id': 'Demo', 'operation_id': 'operation-1',
                   'request_hash': 'a' * 64, 'result_sha256': core.object_hash(self.result),
                   'operation_epoch': 1, 'operation_version': 2, 'observed_head_hash': core.object_hash(head),
                   'observed_head_epoch': 1, 'observed_head_version': 2,
                   'observed_revision': 'rev0', 'observed_at': '2026-01-01T00:00:00+00:00'}
        receipts = self.root / '.openchamber/workspace-records/canonical-receipts'; receipts.mkdir()
        digest = core.object_hash(receipt); git_store.write_json(receipts / (digest + '.json'), receipt)
        self.event = {'kind': 'canonical_receipt', 'project_id': 'Demo',
                      'operation_id': 'operation-1', 'receipt_sha256': digest}

    def tearDown(self): self.temp.cleanup()

    def test_fresh_receipt_then_cached_no_event_has_zero_broker_and_stable_digest(self):
        result = portfolio_mixed.collect_mixed(self.registry, [self.event], self.factory)
        self.assertEqual(result['covered_events'], [core.object_hash(self.event)])
        self.assertEqual(result['pending_events'], [])
        google = result['rows'][0]; self.assertEqual(google['authority'], 'google_head')
        self.assertEqual(google['values']['now'], 'First')
        self.assertEqual(google['source']['version'], 2)
        self.assertEqual(google['source']['status_sha256'], core.sha(test_core.STATUS))
        self.assertEqual(result['rows'][1]['authority'], 'git_local')
        self.assertEqual(self.broker.calls.count('get_document'), 2)
        target = {'spreadsheet_id': 'sheet', 'sheet_id': 1, 'header_row': 0,
                  'columns': {name: i for i, name in enumerate(portfolio_refresh.COLUMNS)},
                  'rows': {'Demo': 1, 'Legacy_Project': 2}, 'generation': 1}
        projected = portfolio_mixed.desired_rows(result, target)
        self.assertTrue(projected['Demo']['freshness'].startswith('last_observed@'))
        self.assertEqual(projected['Demo']['source_time'], google['values']['updated'])
        cached = portfolio_mixed.collect_mixed(self.registry, [], lambda *args: self.fail('broker must not start'))
        self.assertEqual(cached['source_digest'], result['source_digest'])
        self.assertEqual(cached['rows'][0]['source']['observed_at'], google['source']['observed_at'])
        self.assertEqual(self.factory_calls, 1)

    def test_unverified_receipt_and_offline_never_use_old_git(self):
        (self.root / 'project-notes/projects/Demo/status.md').write_bytes(test_core.STATUS.replace(b'First', b'STALE'))
        bad = {**self.event, 'receipt_sha256': 'f' * 64}
        result = portfolio_mixed.collect_mixed(self.registry, [bad], self.factory)
        self.assertEqual(result['pending_events'], [core.object_hash(bad)])
        self.assertEqual(result['rows'][0]['availability'], 'unknown')
        self.assertIsNone(result['rows'][0]['values'])
        self.assertEqual(result['rows'][1]['availability'], 'known')
        self.broker.fail = True
        result = portfolio_mixed.collect_mixed(self.registry, [self.event], self.factory)
        self.assertEqual(result['pending_events'], [core.object_hash(self.event)])
        self.assertIsNone(result['rows'][0]['values'])
        self.assertEqual(result['rows'][1]['availability'], 'known')

    def test_events_settle_only_verified_operations_and_no_broker_for_missing_receipt(self):
        receipt_path = self.root / '.openchamber/workspace-records/canonical-receipts' / (self.event['receipt_sha256'] + '.json')
        missing_receipt = json.loads(receipt_path.read_text())
        missing_receipt['operation_id'] = 'missing-operation'
        missing_receipt['request_hash'] = 'b' * 64
        digest = core.object_hash(missing_receipt)
        git_store.write_json(receipt_path.parent / (digest + '.json'), missing_receipt)
        unmatched = {'kind': 'canonical_receipt', 'project_id': 'Demo',
                     'operation_id': 'missing-operation', 'receipt_sha256': digest}
        result = portfolio_mixed.collect_mixed(self.registry, [self.event, unmatched], self.factory)
        self.assertEqual(result['covered_events'], [core.object_hash(self.event)])
        self.assertEqual(result['pending_events'], [core.object_hash(unmatched)])
        self.assertEqual(result['rows'][0]['source']['receipt_sha256s'], [self.event['receipt_sha256']])
        self.assertTrue(result['rows'][0]['source']['tool_proof_hashes'])
        self.assertEqual(result['rows'][0]['values']['now'], 'First')
        bogus = {**self.event, 'receipt_sha256': 'f' * 64}
        result = portfolio_mixed.collect_mixed(self.registry, [bogus], lambda *args: self.fail('missing receipt must not start broker'))
        self.assertEqual(result['pending_events'], [core.object_hash(bogus)])
        self.assertEqual(result['rows'][0]['issues'], ['RECEIPT_UNAVAILABLE'])

    def test_git_change_keeps_google_cache_and_updates_global_digest(self):
        first = portfolio_mixed.collect_mixed(self.registry, [self.event], self.factory)
        status = self.root / 'project-notes/projects/Legacy_Project/status.md'
        status.write_bytes(test_core.STATUS.replace(b'First', b'Changed'))
        second = portfolio_mixed.collect_mixed(self.registry, [], lambda *args: self.fail('no event must not start broker'))
        self.assertNotEqual(first['source_digest'], second['source_digest'])
        self.assertEqual(second['rows'][0]['values']['now'], 'First')
        self.assertEqual(second['rows'][1]['values']['now'], 'Changed')
        target = {'spreadsheet_id': 'sheet', 'sheet_id': 1, 'header_row': 0,
                  'columns': {name: i for i, name in enumerate(portfolio_refresh.COLUMNS)},
                  'rows': {'Demo': 1, 'Legacy_Project': 2}, 'generation': 1}
        projected = portfolio_mixed.desired_rows(second, target)
        self.assertEqual(projected['Demo']['source_digest'], second['source_digest'])
        self.assertEqual(projected['Legacy_Project']['source_digest'], second['source_digest'])

    def test_same_version_wrong_head_or_wrong_epoch_never_covers_event(self):
        receipt_path = self.root / '.openchamber/workspace-records/canonical-receipts' / (self.event['receipt_sha256'] + '.json')
        receipt = json.loads(receipt_path.read_text())
        for change in ({'observed_head_hash': 'f' * 64}, {'operation_epoch': 2, 'observed_head_epoch': 2}):
            forged = {**receipt, **change}
            digest = core.object_hash(forged)
            git_store.write_json(receipt_path.parent / (digest + '.json'), forged)
            event = {**self.event, 'receipt_sha256': digest}
            result = portfolio_mixed.collect_mixed(self.registry, [event], self.factory)
            self.assertEqual(result['covered_events'], [])
            self.assertEqual(result['pending_events'], [core.object_hash(event)])

    def test_real_outbox_event_consumer_covers_only_captured_events_and_replay_zero_ai(self):
        target = {'spreadsheet_id': 'sheet', 'sheet_id': 1, 'header_row': 0,
                  'columns': {name: i for i, name in enumerate(portfolio_refresh.COLUMNS)},
                  'rows': {'Demo': 1, 'Legacy_Project': 2}, 'generation': 1}
        directory = self.root / 'outbox'; directory.mkdir()
        config = {'notes_root': str(self.root / 'project-notes'), 'authority': 'mixed',
                  'scope': 'all_projects', 'generation': 1}
        google = {'Demo': {'head_tab_id': 'machine', 'summary_tab_id': 'human', 'object_folder_id': 'folder'}}
        checkpoint = {'source_commit': 'c' * 40, 'repository': 'example/notes', 'default_branch': 'main'}
        verifier = patch.object(portfolio_mixed.pushed_git_source, 'verify_pushed_rows',
                                return_value={'sourceCommit': checkpoint['source_commit'], 'source_file_count': 1,
                                              'source_files_sha256': 'd' * 64})
        verified = verifier.start(); self.addCleanup(verifier.stop)
        box = portfolio_mixed.MixedOutbox(directory, config, target, {}, self.registry, google, checkpoint)
        second_box = portfolio_mixed.MixedOutbox(directory, config, target, {}, self.registry, google, checkpoint)
        wake = portfolio_refresh.queue_event(box, self.event)
        self.assertEqual(wake['outcome'], 'queued')
        later = {'kind': 'canonical_receipt', 'project_id': 'Demo', 'operation_id': 'later', 'receipt_sha256': 'f' * 64}
        target_copy = copy.deepcopy(target)
        class Combined(Broker):
            def __init__(inner):
                super().__init__(self.document, self.raw)
                inner.sheet = [{name: name for name in portfolio_refresh.COLUMNS}]
                for project in ('Demo', 'Legacy_Project'):
                    inner.sheet.append({name: project if name == 'project_id' else '' for name in portfolio_refresh.COLUMNS})
                inner.injected = False
                inner.writes = 0
            def request(inner, tool, arguments, kind):
                if tool in ('get_document', 'fetch'):
                    return super().request(tool, arguments, kind)
                if tool == 'get_spreadsheet_cells':
                    rows = []
                    for row in inner.sheet:
                        rows.append({'values': [{'userEnteredValue': {'stringValue': row[name]}} for name in portfolio_refresh.COLUMNS]})
                    result = {'structuredContent': {'spreadsheetId': 'sheet', 'sheets': [
                        {'properties': {'sheetId': 1, 'title': 'source'},
                         'data': [{'startRow': 0, 'startColumn': 0, 'rowData': rows}]}]}}
                elif tool == 'batch_update_spreadsheet':
                    inner.writes += 1
                    if not inner.injected:
                        inner.injected = True
                        queued = portfolio_refresh.queue_event(second_box, later)
                        if queued['outcome'] != 'queued': raise AssertionError(queued)
                    for request in arguments['requests']:
                        update = request['updateCells']; bounds = update['range']
                        name = portfolio_refresh.COLUMNS[bounds['startColumnIndex']]
                        inner.sheet[bounds['startRowIndex']][name] = update['rows'][0]['values'][0]['userEnteredValue']['stringValue']
                    result = {'structuredContent': {'spreadsheetId': 'sheet'}}
                else:
                    raise AssertionError(tool)
                proof = {'tool': tool, 'arguments_sha256': core.object_hash(arguments),
                         'result_sha256': core.object_hash(result), 'arguments_exact': True,
                         'tool_completed': True, 'action_id': 'sheet-action', 'item_id': 'sheet-item',
                         'approval_correlated': kind == 'write'}
                return codex_action_transport.VerifiedToolResult(result, proof)
        broker = Combined()
        starts = []
        class FakeSession:
            def __init__(inner, directory, readers, writers, **kwargs):
                starts.append((set(readers), set(writers)))
            def run(inner, callback):
                return {'outcome': 'driver_completed', 'driver_result': callback(broker), 'model_turns': 1,
                        'tool_receipts': [], 'cleanup': {'appserver_exited': True}, 'driver_thread_settled': True}
        factory = lambda: portfolio_mixed.MixedSession(directory, 'source', session_factory=FakeSession)
        def scan(payload, config):
            return {'state': 'passed', 'content_sha256': core.object_hash(payload)}
        with patch.object(portfolio_refresh.privacy, 'check', side_effect=scan):
            result = portfolio_consumer.consume_events(box, factory, max_batches=1)
        self.assertEqual(result['outcome'], 'backlog_pending')
        self.assertEqual(len(starts), 1)
        self.assertTrue(verified.called)
        self.assertEqual(verified.call_args.args[3], ['Legacy_Project'])
        self.assertEqual(box.inspect()['last_success']['source_commit_proof']['sourceCommit'], checkpoint['source_commit'])
        self.assertEqual(broker.sheet[1]['now'], 'First')
        self.assertEqual(broker.sheet[2]['now'], 'First')
        events = {item['event_hash']: item['phase'] for item in portfolio_refresh.queued_events(box)}
        self.assertEqual(events, {core.object_hash(later): 'queued'})
        first_path = directory / ('portfolio-event-' + core.object_hash(self.event) + '.json')
        first = git_store.read_json(first_path); self.assertEqual(first['phase'], 'settled')
        # Simulate a crash after successful Sheet confirmation but before event settlement.
        first['phase'] = 'queued'; git_store.write_json(first_path, first)
        with patch.object(portfolio_refresh.privacy, 'check', side_effect=scan):
            replay = portfolio_consumer.consume_events(box, lambda: self.fail('covered replay must not start AI'), max_batches=1)
        self.assertEqual(replay['model_turns'], 0)
        self.assertEqual(git_store.read_json(first_path)['phase'], 'settled')
        self.assertEqual(portfolio_refresh.queued_events(box)[0]['event_hash'], core.object_hash(later))
        # An outcome-unknown response reconciles the original write before any retry.
        unknown_dir = self.root / 'unknown-outbox'; unknown_dir.mkdir()
        unknown_box = portfolio_mixed.MixedOutbox(unknown_dir, config, target, {}, self.registry, google, checkpoint)
        portfolio_refresh.queue_event(unknown_box, self.event)
        broker.sheet = [{name: name for name in portfolio_refresh.COLUMNS}]
        for project in ('Demo', 'Legacy_Project'):
            broker.sheet.append({name: project if name == 'project_id' else '' for name in portfolio_refresh.COLUMNS})
        broker.injected = True
        original_write = portfolio_mixed.SheetTransport.write
        def write_then_timeout(transport, payload):
            original_write(transport, payload)
            raise OSError('synthetic response lost')
        before_writes = broker.writes
        unknown_factory = lambda: portfolio_mixed.MixedSession(unknown_dir, 'source', session_factory=FakeSession)
        with patch.object(portfolio_refresh.privacy, 'check', side_effect=scan), \
             patch.object(portfolio_mixed.SheetTransport, 'write', new=write_then_timeout):
            lost = portfolio_consumer.consume_events(unknown_box, unknown_factory, max_batches=1)
        self.assertEqual(lost['outcome'], 'error')
        self.assertEqual(unknown_box.inspect()['pending']['phase'], 'sending')
        self.assertEqual(broker.writes, before_writes + 1)
        with patch.object(portfolio_refresh.privacy, 'check', side_effect=scan):
            reconciled = portfolio_consumer.consume_events(unknown_box, unknown_factory, max_batches=1)
        self.assertEqual(reconciled['outcome'], 'backlog_pending')
        self.assertEqual(broker.writes, before_writes + 1)
        self.assertEqual(portfolio_refresh.queued_events(unknown_box), [])
        # Canonical writer callback may reuse the existing verified ActionBroker.
        shared_dir = self.root / 'shared-outbox'; shared_dir.mkdir()
        shared_box = portfolio_mixed.MixedOutbox(shared_dir, config, target, {}, self.registry, google, checkpoint)
        shared_box.source_provider.select_events([self.event])
        broker.sheet = [{name: name for name in portfolio_refresh.COLUMNS}]
        for project in ('Demo', 'Legacy_Project'):
            broker.sheet.append({name: project if name == 'project_id' else '' for name in portfolio_refresh.COLUMNS})
        shared = codex_action_transport.ActionBroker({'get_document', 'fetch', 'get_spreadsheet_cells'},
                                                     {'batch_update_spreadsheet'})
        shared.request = broker.request
        with patch.object(portfolio_refresh.privacy, 'check', side_effect=scan):
            shared_result = portfolio_mixed.MixedSession(shared_dir, 'source', existing_broker=shared).run(shared_box)
        self.assertEqual(shared_result['outcome'], 'confirmed')
        self.assertEqual(shared_result['model_turns'], 0)
        self.assertTrue(shared_result['shared_session'])
        self.assertEqual(shared_result['covered_events'], [core.object_hash(self.event)])

    def test_pushed_git_dirty_blocks_before_session_and_preserves_queue(self):
        target = {'spreadsheet_id': 'sheet', 'sheet_id': 1, 'header_row': 0,
                  'columns': {name: i for i, name in enumerate(portfolio_refresh.COLUMNS)},
                  'rows': {'Demo': 1, 'Legacy_Project': 2}, 'generation': 1}
        directory = self.root / 'outbox-dirty'; directory.mkdir()
        config = {'notes_root': str(self.root / 'project-notes'), 'authority': 'mixed',
                  'scope': 'all_projects', 'generation': 1}
        google = {'Demo': {'head_tab_id': 'machine', 'summary_tab_id': 'human', 'object_folder_id': 'folder'}}
        checkpoint = {'source_commit': 'c' * 40, 'repository': 'example/notes', 'default_branch': 'main'}
        box = portfolio_mixed.MixedOutbox(directory, config, target, {}, self.registry, google, checkpoint)
        with patch.object(portfolio_mixed.pushed_git_source, 'verify_pushed_rows', side_effect=core.RecordError('PUSHED_SOURCE_DIRTY')):
            with self.assertRaisesRegex(core.RecordError, 'PUSHED_SOURCE_DIRTY'):
                box.probe()
        self.assertEqual(list(directory.glob('portfolio-event-*.json')), [])
        self.assertEqual(self.factory_calls, 0)

    def test_managed_mixed_state_does_not_inherit_all_git_success(self):
        target = {'spreadsheet_id': 'sheet', 'sheet_id': 1, 'header_row': 0,
                  'columns': {name: i for i, name in enumerate(portfolio_refresh.COLUMNS)},
                  'rows': {'Demo': 1, 'Legacy_Project': 2}, 'generation': 1}
        config = {'notes_root': str(self.root / 'project-notes'), 'authority': 'mixed',
                  'scope': 'all_projects', 'generation': 1}
        base = self.root / '.openchamber/workspace-records'
        (base / 'portfolio.json').write_text(json.dumps({'source_config': config, 'target': target}))
        (base / 'portfolio-google.json').write_text(json.dumps({'schema_version': 1, 'projects':
            {'Demo': {'head_tab_id': 'machine', 'summary_tab_id': 'human', 'object_folder_id': 'folder'}}}))
        old = base / 'runtime/portfolio-refresh'; old.mkdir()
        new = base / 'runtime/portfolio-refresh-mixed'; new.mkdir()
        old_file = old / 'portfolio-outbox.json'
        old_file.write_text('{"historical":"preserve"}')
        before = old_file.read_bytes()
        checkpoint = {'source_commit': 'c' * 40, 'repository': 'example/notes', 'default_branch': 'main'}
        with patch.object(routing, 'discover_workspace', return_value=self.root):
            box = portfolio_mixed.managed_outbox(checkpoint)
        self.assertEqual(box.directory, new)
        self.assertIsNone(box.inspect()['last_success'])
        self.assertIsNone(box.inspect()['pending'])
        self.assertEqual(old_file.read_bytes(), before)

    def test_head_summary_inconsistent_with_status_object_is_unknown(self):
        self.head['summary']['now'] = 'forged summary'
        self.document = test_google_docs.doc()
        def lines(values):
            return [{'paragraph': {'elements': [{'textRun': {'content': value + '\n'}}]}} for value in values]
        self.document['tabs'][0]['body']['content'][1:1] = lines([google_docs.head_line(self.head)])
        self.document['tabs'][1]['body']['content'][1:1] = lines(google_docs.summary_lines(self.head))
        self.broker.document = self.document
        result = portfolio_mixed.collect_mixed(self.registry, [self.event], self.factory)
        self.assertEqual(result['pending_events'], [core.object_hash(self.event)])
        self.assertEqual(result['rows'][0]['issues'], ['INTEGRITY_FAILED'])
        self.assertIsNone(result['rows'][0]['values'])

    def test_changed_head_during_read_is_pending(self):
        original = self.broker.request
        def changed(tool, args, kind):
            value = original(tool, args, kind)
            if tool == 'get_document' and self.broker.calls.count('get_document') == 2:
                altered = copy.deepcopy(self.document); altered['revisionId'] = 'changed'
                result = {'structuredContent': altered}
                proof = {**value.proof, 'result_sha256': core.object_hash(result)}
                return codex_action_transport.VerifiedToolResult(result, proof)
            return value
        self.broker.request = changed
        result = portfolio_mixed.collect_mixed(self.registry, [self.event], self.factory)
        self.assertEqual(result['pending_events'], [core.object_hash(self.event)])
        self.assertEqual(result['rows'][0]['issues'], ['REVISION_CONFLICT'])

if __name__ == '__main__': unittest.main()
