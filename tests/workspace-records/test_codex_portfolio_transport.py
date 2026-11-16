"""Deterministic adapter boundaries; no model, network or credentials."""
import copy
import pathlib
import queue
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'skills/workspace-records/scripts'))
import codex_portfolio_transport as transport
import portfolio_refresh
from core import RecordError


class Tests(unittest.TestCase):
    def setUp(self):
        self.target = {'spreadsheet_id': 'synthetic-sheet', 'sheet_id': 7, 'header_row': 4,
                       'columns': {name: index if index < 9 else index + 1 for index, name in enumerate(portfolio_refresh.COLUMNS)},
                       'rows': {'Demo': 5, 'Other': 6}, 'generation': 1}
        rows = []
        for label in (None, 'Demo', 'Other'):
            values = [{} for _ in range(15)]
            values[9] = {'userEnteredValue': {'formulaValue': '=2+2'}}
            for name, index in self.target['columns'].items():
                value = name if label is None else label if name == 'project_id' else ''
                values[index] = {'userEnteredValue': {'stringValue': value}}
            rows.append({'values': values})
        self.raw = {'spreadsheetId': 'synthetic-sheet', 'sheets': [{'properties': {'sheetId': 7, 'title': 'source'},
                     'data': [{'startRow': 4, 'startColumn': 0, 'rowData': rows}]}]}
        self.result = {'content': [], 'structuredContent': self.raw}

    def test_fixed_indices_header_and_human_column(self):
        value = transport.observation(self.result, self.target, 'source', 'fresh')
        self.assertEqual(value['rows']['Demo']['project_id'], 'Demo')
        self.assertEqual(value['request_nonce'], 'fresh')
        self.assertEqual(value['headers'], self.target['columns'])
        args = transport.read_arguments(self.target, "a'b")
        self.assertEqual(args['ranges'], ["'a''b'!A5:O7"])
        self.assertEqual(transport.column_name(26), 'AA')

    def test_reordered_identity_and_wrong_headers_refuse(self):
        raw = self.raw['sheets'][0]['data'][0]['rowData']
        raw[1], raw[2] = raw[2], raw[1]
        with self.assertRaisesRegex(RecordError, 'TARGET_CHANGED'):
            transport.observation(self.result, self.target, 'source', 'n')
        raw[1], raw[2] = raw[2], raw[1]
        raw[0]['values'][0]['userEnteredValue']['stringValue'] = 'wrong'
        with self.assertRaisesRegex(RecordError, 'TARGET_CHANGED'):
            transport.observation(self.result, self.target, 'source', 'n')

    def test_managed_formula_and_wrong_sheet_refuse(self):
        self.raw['sheets'][0]['data'][0]['rowData'][1]['values'][0]['userEnteredValue'] = {'formulaValue': '=1'}
        with self.assertRaises(RecordError): transport.observation(self.result, self.target, 'source', 'n')
        self.raw['sheets'][0]['properties']['sheetId'] = 8
        with self.assertRaises(RecordError): transport.observation(self.result, self.target, 'source', 'n')

    def payload(self):
        return {'spreadsheet_id': 'synthetic-sheet', 'requests': [{'updateCells': {
            'range': {'sheetId': 7, 'startRowIndex': 5, 'endRowIndex': 6, 'startColumnIndex': 1, 'endColumnIndex': 2},
            'rows': [{'values': [{'userEnteredValue': {'stringValue': 'updated'}}]}], 'fields': 'userEnteredValue'}}]}

    def test_write_only_managed_single_string_cells(self):
        payload = self.payload()
        self.assertEqual(transport.checked_write(payload, self.target), payload)
        for change in ({'startColumnIndex': 9, 'endColumnIndex': 10}, {'startRowIndex': 4, 'endRowIndex': 5}, {'sheetId': 8}, {'startRowIndex': True}):
            changed = copy.deepcopy(payload)
            changed['requests'][0]['updateCells']['range'].update(change)
            with self.assertRaises(RecordError): transport.checked_write(changed, self.target)
        payload['requests'][0]['updateCells']['rows'][0]['values'][0]['userEnteredValue'] = {'formulaValue': '=1'}
        with self.assertRaises(RecordError): transport.checked_write(payload, self.target)

    def test_actual_receipt_not_model_claim(self):
        broker = transport.Broker(self.target, 'source')
        broker.current = {'action_id': 'a', 'kind': 'read', 'tool': transport.READ,
                          'arguments': transport.read_arguments(self.target, 'source'), 'nonce': 'fresh'}
        item = {'tool': 'google_drive.' + transport.READ, 'arguments': copy.deepcopy(broker.current['arguments']),
                'status': 'completed', 'result': self.result}
        wrong = copy.deepcopy(item)
        wrong['arguments']['ranges'] = ["'source'!A1:A2"]
        with self.assertRaises(RecordError): broker.complete(wrong)
        self.assertIsNotNone(broker.current)
        broker.complete(item)
        self.assertEqual(broker.responses.get()['request_nonce'], 'fresh')
        self.assertTrue(broker.receipts[0]['arguments_exact'])

    def test_write_requires_correlated_approval(self):
        broker = transport.Broker(self.target, 'source')
        broker.current = {'action_id': 'a', 'kind': 'write', 'tool': transport.WRITE, 'arguments': self.payload()}
        item = {'tool': 'google_drive.' + transport.WRITE, 'arguments': self.payload(), 'status': 'completed',
                'result': {'content': [], 'structuredContent': {'spreadsheetId': 'synthetic-sheet'}}}
        with self.assertRaisesRegex(RecordError, 'APPROVAL_UNVERIFIED'): broker.complete(item)
        broker.complete(item, write_approved=True)
        self.assertIsNone(broker.responses.get())

    def test_status_is_only_additional_bounded_cell(self):
        self.target['status_cell'] = {'sheet_id': 7, 'row_index': 2, 'column_index': 0}
        self.raw['sheets'][0]['data'].append({'startRow': 2, 'startColumn': 0,
            'rowData': [{'values': [{'userEnteredValue': {'stringValue': 'confirmed'}}]}]})
        observed = transport.observation(self.result, self.target, 'source', 'n')
        self.assertEqual(observed['status'], 'confirmed')
        self.assertEqual(transport.read_arguments(self.target, 'source')['ranges'], ["'source'!A3:O7"])
        payload = self.payload()
        payload['requests'][0]['updateCells']['range'].update(startRowIndex=2, endRowIndex=3, startColumnIndex=0, endColumnIndex=1)
        self.assertEqual(transport.checked_write(payload, self.target), payload)
        payload['requests'][0]['updateCells']['range'].update(startColumnIndex=1, endColumnIndex=2)
        with self.assertRaises(RecordError): transport.checked_write(payload, self.target)

    def test_no_change_and_error_never_construct_session(self):
        factory = Mock(side_effect=AssertionError('must not start model'))
        box = Mock()
        for outcome in ('no_change', 'error'):
            box.probe.return_value = {'outcome': outcome}
            self.assertEqual(transport.consume_once(box, factory)['model_turns'], 0)
        factory.assert_not_called()

    def test_privileged_mcp_approval_requires_exact_trusted_action(self):
        action = {'action_id': 'action', 'tool': transport.WRITE, 'arguments': self.payload()}
        params = {'threadId': 'thread', 'turnId': 'turn', 'mode': 'form',
            '_meta': {'codex_approval_kind': 'mcp_tool_call', 'connector_id': 'app',
                      'tool_name': transport.WRITE, 'tool_params': self.payload()},
            'requestedSchema': {'type': 'object', 'properties': {}, 'required': []}}
        self.assertTrue(transport.elicitation_matches(params, action, 'app', 'thread', 'turn'))
        params['_meta']['tool_params']['spreadsheet_id'] = 'different-sheet'
        self.assertFalse(transport.elicitation_matches(params, action, 'app', 'thread', 'turn'))

    def test_lifecycle_rejects_unstarted_wrong_item_thread_turn_and_action(self):
        action = {'action_id': 'a', 'tool': transport.READ, 'arguments': {'value': 1}}
        event = {'threadId': 'thread', 'turnId': 'turn', 'item': {'id': 'item', 'server': 'apps',
            'tool': 'google_drive.' + transport.READ, 'arguments': {'value': 1}}}
        lifecycle = transport.EventCorrelation('thread', 'turn')
        with self.assertRaises(RecordError): lifecycle.complete(event, action)
        for mutate in (lambda x: x['item'].update(id='different'), lambda x: x.update(threadId='other'),
                       lambda x: x.update(turnId='other'), lambda x: x['item'].update(server='other')):
            lifecycle = transport.EventCorrelation('thread', 'turn')
            lifecycle.start(event, action)
            changed = copy.deepcopy(event)
            mutate(changed)
            with self.assertRaises(RecordError): lifecycle.complete(changed, action)
        lifecycle = transport.EventCorrelation('thread', 'turn')
        lifecycle.start(event, action)
        self.assertTrue(lifecycle.can_approve('item', action))
        self.assertFalse(lifecycle.can_approve('item', {**action, 'action_id': 'b'}))
        with self.assertRaises(RecordError): lifecycle.complete(event, {**action, 'action_id': 'b'})
        lifecycle.complete(event, action)
        self.assertFalse(lifecycle.can_approve('item', action))
        with self.assertRaises(RecordError): lifecycle.complete(event, action)

    def test_exact_json_types_cannot_be_python_equal_aliases(self):
        action = {'action_id': 'a', 'tool': transport.READ, 'arguments': {'value': 1}}
        for value in (True, 1.0):
            event = {'threadId': 'thread', 'turnId': 'turn', 'item': {'id': 'item',
                'tool': 'google_drive.' + transport.READ, 'arguments': {'value': value}}}
            with self.assertRaises(RecordError): transport.EventCorrelation('thread', 'turn').start(event, action)
            params = {'threadId': 'thread', 'turnId': 'turn', 'mode': 'form', '_meta': {
                'codex_approval_kind': 'mcp_tool_call', 'connector_id': 'app', 'tool_name': transport.READ,
                'tool_params': {'value': value}}, 'requestedSchema': {'type': 'object', 'properties': {}}}
            self.assertFalse(transport.elicitation_matches(params, action, 'app', 'thread', 'turn'))
        params['_meta']['tool_params'] = self.payload()
        params['requestedSchema']['properties'] = {'password': {'type': 'string'}}
        self.assertFalse(transport.elicitation_matches(params, action, 'app', 'thread', 'turn'))

    def test_pending_reconcile_enters_session_once(self):
        box, factory = Mock(), Mock()
        box.probe.return_value = {'outcome': 'pending'}
        factory.return_value.run.return_value = {'outcome': 'confirmed'}
        self.assertEqual(transport.consume_once(box, factory)['outcome'], 'confirmed')
        factory.assert_called_once_with()
        factory.return_value.run.assert_called_once_with(box)

    def test_single_turn_dynamic_driver_and_correlated_read_approval(self):
        target, raw = self.target, self.result
        class FakeRpc:
            def __init__(self):
                self.incoming = queue.Queue()
                self.notifications = []
                self.closed = False

            def request(self, identifier):
                return {'id': identifier, 'method': 'item/tool/call', 'params': {
                    'tool': transport.DYNAMIC, 'arguments': {}, 'threadId': 'thread', 'turnId': 'turn'}}

            def call(self, method, params):
                self.notifications.append(self.request(10))
                return {'turn': {'id': 'turn'}}

            def send(self, message):
                if message['id'] in (10, 12):
                    action = __import__('json').loads(message['result']['contentItems'][0]['text'])
                    if action.get('done'):
                        self.notifications.append({'method': 'turn/completed', 'params': {}})
                        return
                    self.item = {'id': 'tool-item', 'type': 'mcpToolCall', 'tool': 'google_drive.' + action['tool'],
                                 'arguments': action['arguments'], 'status': 'inProgress'}
                    self.notifications.extend([
                        {'method': 'item/started', 'params': {'threadId': 'thread', 'turnId': 'turn', 'item': self.item}},
                        {'id': 11, 'method': 'tool/requestUserInput', 'params': {'threadId': 'thread', 'turnId': 'turn',
                            'itemId': 'tool-item', 'questions': [{'id': 'approval', 'options': [{'label': 'Accept'}, {'label': 'Decline'}]}]}}])
                elif message['id'] == 11:
                    self.item = {**self.item, 'status': 'completed', 'result': raw}
                    self.notifications.extend([{'method': 'item/completed', 'params': {'threadId': 'thread', 'turnId': 'turn', 'item': self.item}}, self.request(12)])

            def close(self):
                self.closed = True
                return {'appserver_exited': True, 'reader_thread_settled': True, 'pipes_closed': True, 'remaining_job_pids': []}

        with tempfile.TemporaryDirectory() as scratch:
            session = transport.CodexSession(pathlib.Path(scratch), 'source', executable='synthetic-executable')
            rpc = FakeRpc()
            def opened():
                session.rpc = rpc
                return 'app', 'thread'
            session.open = opened
            result = session.read_only(target)
            self.assertEqual(result['outcome'], 'read_verified')
            self.assertEqual(result['model_turns'], 1)
            self.assertEqual(len(result['tool_receipts']), 1)
            self.assertTrue(rpc.closed)


if __name__ == '__main__':
    unittest.main()
