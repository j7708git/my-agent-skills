import copy, json, pathlib, sys, unittest
from unittest.mock import patch

repo = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo / 'tests/workspace-records'))
import test_portfolio_mixed as fixture
import portfolio_mixed as mixed, portfolio_refresh as refresh, portfolio_consumer as consumer
import codex_action_transport as action, core, routing

class Tests(unittest.TestCase):
    setUp = fixture.Tests.setUp
    tearDown = fixture.Tests.tearDown

    def test_failed_initial_observation_stays_unknown(self):
        self.broker.fail = True
        result = mixed.collect_mixed(self.registry, [], self.factory, observe_uncached=True)
        self.assertEqual(result['rows'][0]['availability'], 'unknown')
        self.assertIsNone(result['rows'][0]['values'])
        self.assertEqual(result['rows'][0]['issues'], ['CONNECTOR_UNAVAILABLE'])
        self.assertIsNone(mixed.cached_row(self.registry, self.registry.bind('Demo')))

    def test_git_push_first_after_google_cutover(self):
        base = self.root / '.openchamber/workspace-records'
        target = {'spreadsheet_id':'sheet','sheet_id':1,'header_row':0,
                  'columns':{n:i for i,n in enumerate(refresh.COLUMNS)},
                  'rows':{'Demo':1,'Legacy_Project':2},'generation':1}
        config = {'notes_root':str(self.root/'project-notes'),'authority':'mixed','scope':'all_projects','generation':1}
        google = {'Demo':{'head_tab_id':'machine','summary_tab_id':'human','object_folder_id':'folder'}}
        (base/'portfolio.json').write_text(json.dumps({'source_config':config,'target':target}))
        (base/'portfolio-google.json').write_text(json.dumps({'schema_version':1,'projects':google}))
        (base/'runtime/portfolio-refresh-mixed').mkdir()
        checkpoint={'source_commit':'c'*40,'repository':'example/notes','default_branch':'main'}
        with patch.object(routing,'discover_workspace',return_value=self.root):
            box=mixed.managed_outbox(checkpoint)
        outer=self
        class Broker(fixture.Broker):
            def __init__(inner):
                super().__init__(outer.document,outer.raw)
                inner.sheet=[{n:n for n in refresh.COLUMNS}]+[{n:p if n=='project_id' else '' for n in refresh.COLUMNS} for p in ('Demo','Legacy_Project')]
            def request(inner,tool,args,kind):
                if tool in ('get_document','fetch'): return super().request(tool,args,kind)
                if tool=='get_spreadsheet_cells':
                    rows=[{'values':[{'userEnteredValue':{'stringValue':row[n]}} for n in refresh.COLUMNS]} for row in inner.sheet]
                    value={'structuredContent':{'spreadsheetId':'sheet','sheets':[{'properties':{'sheetId':1,'title':'source'},'data':[{'startRow':0,'startColumn':0,'rowData':rows}]}]}}
                elif tool=='batch_update_spreadsheet':
                    for req in args['requests']:
                        u=req['updateCells']; b=u['range']; inner.sheet[b['startRowIndex']][refresh.COLUMNS[b['startColumnIndex']]]=u['rows'][0]['values'][0]['userEnteredValue']['stringValue']
                    value={'structuredContent':{'spreadsheetId':'sheet'}}
                else: raise AssertionError(tool)
                proof={'tool':tool,'arguments_sha256':core.object_hash(args),'result_sha256':core.object_hash(value),'arguments_exact':True,'tool_completed':True,'action_id':'a','item_id':'i','approval_correlated':kind=='write'}
                return action.VerifiedToolResult(value,proof)
        broker=Broker()
        class Session:
            def __init__(self,*a,**kw): pass
            def run(self,callback): return {'outcome':'driver_completed','driver_result':callback(broker),'model_turns':1,'tool_receipts':[],'cleanup':{},'driver_thread_settled':True}
        class GitResult: returncode=0; stdout='c'*40
        proof={'sourceCommit':'c'*40,'source_file_count':1,'source_files_sha256':'d'*64}
        with patch.object(refresh.subprocess,'run',return_value=GitResult()), patch.object(mixed.pushed_git_source,'verify_pushed_rows',return_value=proof), patch.object(refresh.privacy,'check',side_effect=lambda payload,cfg:{'state':'passed','content_sha256':core.object_hash(payload)}):
            queued=refresh.queue_event(box,{'kind':'git_checkpoint','commit':'c'*40})
            self.assertEqual(queued['outcome'],'queued')
            result=consumer.consume_events(box,lambda:mixed.MixedSession(box.directory,'source',session_factory=Session),max_batches=1)
        self.assertEqual(result['outcome'],'backlog_pending')
        self.assertEqual(broker.sheet[1]['availability'],'known',broker.sheet[1])
        self.assertEqual(broker.sheet[1]['now'],'First')
        self.assertEqual(broker.calls.count('get_document'),2)

if __name__=='__main__': unittest.main()
