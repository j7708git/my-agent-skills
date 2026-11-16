"""Shape-only failure traces; real session loops, no model/network/live state."""
import json
import pathlib
import queue
import sys
import tempfile
import unittest

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[2]/'skills/workspace-records/scripts'))
import codex_portfolio_transport as portfolio
import codex_action_transport as action

CANARY='private-document-content-and-resource-id'

class FakeRpc:
    def __init__(self,dynamic,arguments,late_mismatch=False,mutation=None):
        self.dynamic,self.arguments,self.late_mismatch=dynamic,arguments,late_mismatch
        self.mutation=mutation or {}
        self.notifications=[]
        self.incoming=queue.Queue()
        self.denied=False
    def call(self,method,params):
        value={'tool':self.dynamic,'arguments':self.arguments,'threadId':'thread','turnId':'turn','itemId':'dynamic-item'}
        value.update(self.mutation)
        self.notifications.append({'id':10,'method':'item/tool/call','params':value})
        return {'turn':{'id':'turn'}}
    def send(self,message):
        if 'error' in message:
            self.denied=True
        elif self.late_mismatch:
            tool,arguments='google_drive.unexpected',{'secret':CANARY}
            if self.late_mismatch=='rewrite_write':
                expected=json.loads(message['result']['contentItems'][0]['text'])
                tool,arguments='google_drive.'+expected['tool'],expected['arguments']
                arguments['requests'][0]['updateCells']['rows'][0]['values'][0]['userEnteredValue']['stringValue']=CANARY
            self.notifications.append({'method':'item/started','params':{'threadId':'thread','turnId':'turn','item':{
                'type':'mcpToolCall','id':'unmatched-item','tool':tool,'arguments':arguments}}})
        else:
            self.notifications.append({'method':'turn/completed','params':{}})
    def close(self):
        return {'appserver_exited':True,'reader_thread_settled':True,'pipes_closed':True,'remaining_job_pids':[]}

class Tests(unittest.TestCase):
    def run_case(self,generic,arguments,late=False,mutation=None):
        rpc=FakeRpc(action.DYNAMIC if generic else portfolio.DYNAMIC,arguments,late,mutation)
        with tempfile.TemporaryDirectory() as scratch:
            if generic:
                session=action.CodexActionSession(pathlib.Path(scratch),readers={'get_document'},writers={'batch_update_spreadsheet'},timeout=1,executable='synthetic')
                callback=(lambda broker:broker.request('get_document',{'document_id':'fixture'},'read')) if late else (lambda broker:{'done':True})
            else:
                session=portfolio.CodexSession(pathlib.Path(scratch),'source',timeout=1,executable='synthetic')
                callback=(lambda broker:broker.read(box.target,'nonce')) if late else (lambda broker:{'outcome':'confirmed'})
            if late=='rewrite_write':
                payload={'spreadsheet_id':'fixture','requests':[{'updateCells':{'range':{'sheetId':7,'startRowIndex':5,'endRowIndex':6,'startColumnIndex':0,'endColumnIndex':1},'rows':[{'values':[{'userEnteredValue':{'stringValue':'reviewed original'}}]}],'fields':'userEnteredValue'}}]}
                callback=(lambda broker:broker.request('batch_update_spreadsheet',payload,'write')) if generic else (lambda broker:broker.write(payload))
            def opened():
                session.rpc=rpc
                return 'app','thread'
            session.open=opened
            class Box:
                target={'spreadsheet_id':'fixture','sheet_id':7,'header_row':4,'columns':{'project_id':0},'rows':{'Demo':5},'generation':1}
                def record_error(self,code):return {'outcome':'error','code':code}
            box=Box()
            result=session.run(callback) if generic else session.run(box,callback)
        return result,rpc

    def test_rejected_dynamic_arguments_have_exact_failure_site_without_payload(self):
        for generic in (False,True):
            for arguments in (None,'{}',{'secret':CANARY}):
                with self.subTest(generic=generic,arguments_type=type(arguments).__name__):
                    result,rpc=self.run_case(generic,arguments)
                    self.assertEqual(result['code'],'FORBIDDEN')
                    self.assertTrue(rpc.denied)
                    self.assertEqual(result['failure_stage'],'request_denied')
                    self.assertFalse(result['event_trace'][-1]['arguments_empty_object'])
                    self.assertNotIn(CANARY,json.dumps(result))

    def test_later_mcp_rejection_distinct_from_stale_last_request(self):
        for generic in (False,True):
            result,_=self.run_case(generic,{},late=True)
            self.assertEqual(result['code'],'FORBIDDEN')
            self.assertEqual(result['failure_stage'],'mcp_start')
            self.assertTrue(result['last_request_diagnostic']['arguments_empty_object'])
            self.assertEqual(result['event_trace'][-1]['method'],'item/started')
            self.assertFalse(result['event_trace'][-1]['mcp_arguments_match'])
            self.assertNotIn(CANARY,json.dumps(result))

    def test_exact_empty_arguments_do_not_require_started_mcp_item(self):
        for generic in (False,True):
            result,rpc=self.run_case(generic,{})
            self.assertEqual(result['outcome'],'driver_completed' if generic else 'confirmed')
            self.assertFalse(rpc.denied)

    def test_wrong_thread_turn_and_dynamic_name_still_denied(self):
        for generic in (False,True):
            for field in ('threadId','turnId','tool'):
                result,rpc=self.run_case(generic,{},mutation={field:'different'})
                self.assertEqual(result['code'],'FORBIDDEN')
                self.assertTrue(rpc.denied)

    def test_write_payload_rewrite_is_denied_with_exact_safe_difference(self):
        for generic in (False,True):
            result,_=self.run_case(generic,{},late='rewrite_write')
            self.assertEqual(result['code'],'FORBIDDEN')
            self.assertEqual(result['failure_stage'],'mcp_start')
            diagnostic=result['mcp_start_diagnostic']
            self.assertTrue(diagnostic['tool_name_matches'])
            self.assertFalse(diagnostic['arguments_exact'])
            self.assertEqual(diagnostic['differences'][0]['path'],'$.requests[0].updateCells.rows[0].values[0].userEnteredValue.stringValue')
            self.assertEqual(diagnostic['differences'][0]['category'],'value')
            self.assertNotEqual(diagnostic['expected_arguments_sha256'],diagnostic['actual_arguments_sha256'])
            self.assertEqual(len(result['tool_receipts']),0)
            self.assertNotIn(CANARY,json.dumps(result))

    def test_comparison_bounds_unknown_keys_and_types_without_values(self):
        expected={'tool':'get_document','arguments':{'document_id':1}}
        actual={'tool':'google_drive.get_document','arguments':{'document_id':True,CANARY:CANARY}}
        result=portfolio.mcp_comparison(expected,actual)
        self.assertFalse(result['arguments_exact'])
        self.assertTrue(any(x['category']=='type' for x in result['differences']))
        self.assertNotIn(CANARY,json.dumps(result))
        result=portfolio.mcp_comparison({'tool':'get_document','arguments':{}},{'arguments':{str(i):CANARY for i in range(1000)}})
        self.assertLessEqual(len(result['differences']),8)
        self.assertTrue(result['differences_truncated'])
        self.assertNotIn(CANARY,json.dumps(result))

    def test_shape_trace_is_bounded_and_never_retains_raw_strings(self):
        trace=portfolio.EventTrace('thread','turn','dynamic')
        for index in range(100):
            trace.observe({'id':index,'method':CANARY,'params':{'threadId':CANARY,'turnId':CANARY,'arguments':{'secret':CANARY},'item':{'type':CANARY,'id':CANARY}}},{},None)
        self.assertEqual(len(trace.events),16)
        self.assertNotIn(CANARY,json.dumps(list(trace.events)))
        self.assertEqual(trace.events[-1]['method'],'other')
        trace.observe({'method': {'secret':CANARY}, 'params':{'itemId':[], 'item':{'type':[]}}},{},None)
        self.assertNotIn(CANARY,json.dumps(list(trace.events)))

if __name__=='__main__':unittest.main()
