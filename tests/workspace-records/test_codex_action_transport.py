"""Exact-action bridge tests; no credentials, model or external calls."""
import copy
import json
import pathlib
import queue
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'skills/workspace-records/scripts'))
import codex_action_transport as transport
from core import RecordError


class FakeRpc:
    def __init__(self, forged=False):
        self.incoming, self.notifications = queue.Queue(), []
        self.forged = forged
        self.closed = False

    def next_request(self, identifier):
        return {'id': identifier, 'method': 'item/tool/call', 'params': {
            'tool': transport.DYNAMIC, 'arguments': {}, 'threadId': 'thread', 'turnId': 'turn'}}

    def call(self, method, params):
        self.notifications.append(self.next_request(10))
        return {'turn': {'id': 'turn'}}

    def send(self, message):
        if message['id'] in (10, 12):
            action = json.loads(message['result']['contentItems'][0]['text'])
            if action.get('done'):
                self.notifications.append({'method': 'turn/completed', 'params': {}})
                return
            self.item = {'id': 'tool-item', 'server': 'apps', 'type': 'mcpToolCall',
                'tool': 'google_drive.' + action['tool'], 'arguments': action['arguments'], 'status': 'inProgress'}
            self.notifications.extend([
                {'method': 'item/started', 'params': {'threadId': 'thread', 'turnId': 'turn', 'item': copy.deepcopy(self.item)}},
                {'id': 11, 'method': 'mcpServer/elicitation/request', 'params': {
                    'threadId': 'thread', 'turnId': 'turn', 'mode': 'form', 'serverName': 'apps',
                    '_meta': {'codex_approval_kind': 'mcp_tool_call', 'connector_id': 'app',
                              'tool_name': action['tool'], 'tool_params': action['arguments']},
                    'requestedSchema': {'type': 'object', 'properties': {}, 'required': []}}}])
        elif message['id'] == 11:
            accepted = message['result']['action'] == 'accept'
            self.item.update(status='completed' if accepted else 'failed', result={'content': [], 'structuredContent': {'synthetic': True}})
            if not accepted:
                self.item['error'] = {'message': 'MCP tool call declined'}
            self.notifications.extend([{'method': 'item/completed', 'params': {
                'threadId': 'wrong' if self.forged else 'thread', 'turnId': 'turn', 'item': self.item}}, self.next_request(12)])

    def close(self):
        self.closed = True
        return {'appserver_exited': True, 'reader_thread_settled': True, 'pipes_closed': True, 'remaining_job_pids': []}


class Tests(unittest.TestCase):
    def test_blocking_action_returns_actual_result_and_proof(self):
        broker = transport.ActionBroker({'get_document'}, set(), timeout=2)
        received = []
        worker = threading.Thread(target=lambda: received.append(broker.request('get_document', {'document_id': 'synthetic'}, 'read')))
        worker.start()
        action = broker.next_action()
        broker.complete({'id': 'item', 'tool': 'google_drive.get_document', 'arguments': action['arguments'],
                         'status': 'completed', 'result': {'content': [], 'structuredContent': {'document': {}}}}, True)
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(received[0], transport.VerifiedToolResult)
        self.assertTrue(received[0].proof['arguments_exact'])
        self.assertEqual(received[0].proof['item_id'], 'item')

    def test_kind_cannot_disguise_writer(self):
        broker = transport.ActionBroker({'get_document'}, {'batch_update_document'})
        for tool, kind in (('batch_update_document', 'read'), ('get_document', 'write'), ('unknown', 'read')):
            with self.assertRaises(RecordError): broker.request(tool, {}, kind)
        with self.assertRaises(RecordError): transport.ActionBroker({'same'}, {'same'})

    def test_no_completed_write_without_approval(self):
        broker = transport.ActionBroker(set(), {'batch_update_document'})
        broker.current = {'action_id': 'a', 'kind': 'write', 'tool': 'batch_update_document', 'arguments': {'version': 1}}
        item = {'id': 'item', 'tool': 'google_drive.batch_update_document', 'arguments': {'version': 1}, 'status': 'completed', 'result': {'content': []}}
        with self.assertRaisesRegex(RecordError, 'APPROVAL_UNVERIFIED'): broker.complete(item, False)
        for value in (True, 1.0):
            item['arguments']['version'] = value
            self.assertFalse(broker.matches(item))

    def test_privileged_approval_exact_type_and_app(self):
        action = {'tool': 'batch_update_document', 'arguments': {'version': 1}}
        params = {'threadId': 'thread', 'turnId': 'turn', 'mode': 'form', '_meta': {
            'codex_approval_kind': 'mcp_tool_call', 'connector_id': 'app', 'tool_name': action['tool'], 'tool_params': {'version': 1}},
            'requestedSchema': {'type': 'object', 'properties': {}}}
        self.assertTrue(transport.approval_matches(params, action, 'app', 'thread', 'turn'))
        for value in (True, 1.0):
            params['_meta']['tool_params'] = {'version': value}
            self.assertFalse(transport.approval_matches(params, action, 'app', 'thread', 'turn'))
        params['_meta']['tool_params'] = {'version': 1}
        self.assertFalse(transport.approval_matches(params, action, 'other', 'thread', 'turn'))

    def session(self, folder, rpc, write=False, decline=False):
        session = transport.CodexActionSession(pathlib.Path(folder), set() if write else {'get_document'},
            {'batch_update_document'} if write else set(), executable='synthetic', decline_writes=decline)
        def opened():
            session.rpc = rpc
            return 'app', 'thread'
        session.open = opened
        return session

    def test_single_turn_driver_uses_verified_tool_result(self):
        with tempfile.TemporaryDirectory() as folder:
            rpc = FakeRpc()
            session = self.session(folder, rpc)
            def driver(broker):
                actual = broker.request('get_document', {'document_id': 'synthetic'}, 'read')
                return {'outcome': 'reader_verified', 'actual': actual.result['structuredContent']['synthetic']}
            result = session.run(driver)
            self.assertEqual(result['outcome'], 'driver_completed')
            self.assertEqual(result['driver_result']['outcome'], 'reader_verified')
            self.assertEqual(result['model_turns'], 1)
            self.assertTrue(result['driver_thread_settled'])
            self.assertTrue(rpc.closed)

    def test_wrong_thread_completion_never_satisfies_driver(self):
        with tempfile.TemporaryDirectory() as folder:
            result = self.session(folder, FakeRpc(forged=True)).run(lambda broker: broker.request('get_document', {}, 'read'))
            self.assertEqual(result['outcome'], 'error')
            self.assertEqual(result['code'], 'FORBIDDEN')
            self.assertEqual(result['tool_receipts'], [])

    def test_declined_write_is_failed_not_a_success_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            session = self.session(folder, FakeRpc(), write=True, decline=True)
            result = session.run(lambda broker: broker.request('batch_update_document', {}, 'write'))
            self.assertEqual(result['outcome'], 'error')
            self.assertEqual(result['tool_receipts'], [])
            self.assertTrue(result['declined_actions'][0]['server_approval_declined'])
            self.assertEqual(result['last_tool_diagnostic']['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
