"""Real projection protocol with fake verified Apps; no live Drive writes."""
import base64
import copy
import pathlib
import sys
import types
import unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[2]/'skills/workspace-records/scripts'))
import core,git_store,canonical_writer,detail_projection_driver as driver
import test_detail_projection as fixtures

class Tests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.TestDetails('runTest');self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.f=self.fixture;self.remote={};self.parents={};self.writes=[];self.fail_upload=False;self.fail_after_write=False;self.normalized=False;self.trashed=False
        self.reader=canonical_writer.CanonicalReader(self,{'head_tab_id':'machine','summary_tab_id':'human','object_folder_id':'objects'})
        self.reader.read_authority=lambda binding,nonce:{'nonce':nonce,'document':self.f.x['document'],'snapshot':driver.detail.google_docs.snapshot(self.f.x['document'],'doc','machine','human','Demo'),'authority':{'document_id':'doc','revision_id':'rev','control':{**driver.detail.google_docs.snapshot(self.f.x['document'],'doc','machine','human','Demo')['head']['control'],'version':self.version}}}
        graph=patch.object(canonical_writer.CanonicalWriter,'graph',side_effect=lambda *args:self.f.x['existing']);graph.start();self.addCleanup(graph.stop)
        self.original_read=driver.protocol.read_graph
        def read(*args):
            graph,ops,objects,events,journal=self.original_read(*args)
            return graph,{**ops,self.operation:{'request_hash':'a'*64,'result':self.result}},objects,events,journal
        protocol=patch.object(driver.protocol,'read_graph',side_effect=read);protocol.start();self.addCleanup(protocol.stop)
        self.prepare(1)

    def prepare(self,version):
        self.version=version;self.operation='canonical-'+str(version)
        self.f.make_source(version)
        head=driver.detail.google_docs.snapshot(self.f.x['document'],'doc','machine','human','Demo')['head']
        self.result={'epoch':head['control']['epoch'],'logical_version':version}
        receipt={'schema_version':1,'project_id':'Demo','operation_id':self.operation,'request_hash':'a'*64,'result_sha256':core.object_hash(self.result),'operation_epoch':self.result['epoch'],'operation_version':version,'observed_head_hash':core.object_hash(head),'observed_head_epoch':self.result['epoch'],'observed_head_version':version,'observed_revision':'rev','observed_at':'2026-01-01T00:00:00Z'}
        directory=self.f.root/'.openchamber/workspace-records/canonical-receipts';directory.mkdir(exist_ok=True)
        self.digest=core.object_hash(receipt);git_store.write_json(directory/(self.digest+'.json'),receipt)

    def request(self,tool,args,kind):
        if tool=='get_file_metadata':
            remote=args['fileId']
            if self.fail_after_write and self.writes:
                self.fail_after_write=False
                raise core.RecordError('TRANSPORT_UNKNOWN')
            result={'id':remote,'mimeType':'application/vnd.google-apps.folder' if remote.endswith('folder') else 'text/markdown','trashed':False,'parents':[] if remote.endswith('folder') else [self.parents[remote]]}
            if self.trashed: result['trashed']=True
            if self.normalized:
                result['mime_type']=result.pop('mimeType');result['parent_ids']=result.pop('parents')
                if not self.trashed:result.pop('trashed')
        elif tool=='fetch':
            remote=args['url'].split('/d/')[1].split('/')[0]
            result={'b64_string':base64.b64encode(self.remote[remote]).decode()}
        elif tool in ('upload_file','update_file'):
            self.writes.append((tool,copy.deepcopy(args)))
            if tool=='upload_file':
                if self.fail_upload: raise core.RecordError('UPLOAD_OUTCOME_UNKNOWN')
                remote='file-'+str(len(self.remote)+1);self.parents[remote]=args['parent_folder_id']
            else:
                remote=args['fileId']
                if args.get('addParents'):self.parents[remote]=args['addParents']
            if 'file_uri' in args:self.remote[remote]=pathlib.Path(args['file_uri']).read_bytes()
            result={'id':remote,'mimeType':'text/markdown'}
        else:raise AssertionError(tool)
        proof={'tool':tool,'arguments_sha256':core.object_hash(args),'result_sha256':core.object_hash(result),'arguments_exact':True,'tool_completed':True,'action_id':'a','item_id':'i','approval_correlated':kind=='write'}
        return types.SimpleNamespace(result=result,proof=proof)

    def run_projection(self):
        return driver.refresh_details(self.f.registry,self.reader,{'folder_id':'current-folder','archive_folder_id':'archive-folder'},'Demo',self.digest)

    def test_upload_update_final_readback_and_replay_no_writes(self):
        result=self.run_projection();self.assertEqual(result['outcome'],'detail_projection_confirmed')
        self.assertEqual(len(self.writes),4)
        count=len(self.writes);self.assertEqual(self.run_projection()['outcome'],'detail_projection_confirmed');self.assertEqual(len(self.writes),count)
        self.prepare(2)
        self.assertEqual(self.run_projection()['outcome'],'detail_projection_confirmed')
        self.assertTrue(all(tool=='update_file' for tool,args in self.writes[count:]))

    def test_same_claim_old_receipt_new_head_projects_without_canonical_retry(self):
        self.assertEqual(self.run_projection()['outcome'],'detail_projection_confirmed')
        first=driver.detail.load(self.f.runtime,'Demo')['active']['operation_id']
        canonical_operation=self.operation;digest=self.digest
        self.f.make_source(2);self.version=2
        result=self.run_projection()
        self.assertEqual(result['outcome'],'detail_projection_confirmed')
        self.assertEqual((result['operation_id'],result['canonical_receipt_sha256']),(canonical_operation,digest))
        self.assertNotEqual(first,result['projection_operation_id'])
        self.assertEqual(result['generation']['version'],2)

    def test_known_unknown_old_generation_readback_before_new_projection(self):
        self.fail_after_write=True
        self.assertEqual(self.run_projection()['outcome'],'detail_projection_pending')
        self.f.make_source(2);self.version=2
        self.assertEqual(self.run_projection()['outcome'],'detail_projection_confirmed')
        self.assertEqual(sum(tool=='upload_file' and args['file_name']=='專案詳情索引.md' for tool,args in self.writes),1)

    def test_actual_normalized_metadata_shape_retains_unknown_trash(self):
        self.normalized=True
        observed=driver.metadata(self,'current-folder',True)
        self.assertIsNone(observed['trashed'])
        self.assertEqual(self.run_projection()['outcome'],'detail_projection_confirmed')

    def test_explicit_trashed_true_refuses_before_write(self):
        self.normalized=True;self.trashed=True
        self.assertEqual(self.run_projection()['code'],'FORBIDDEN')
        self.assertEqual(self.writes,[])

    def test_unknown_create_never_reuploaded(self):
        self.fail_upload=True
        self.assertEqual(self.run_projection()['outcome'],'detail_projection_pending')
        self.fail_upload=False;count=len(self.writes)
        self.assertEqual(self.run_projection()['code'],'MANUAL_REVIEW')
        self.assertEqual(len(self.writes),count)

    def test_known_id_recovery_fetches_and_accepts_without_rewrite(self):
        self.fail_after_write=True
        self.assertEqual(self.run_projection()['outcome'],'detail_projection_pending')
        self.assertEqual(len(self.writes),1)
        self.assertEqual(self.run_projection()['outcome'],'detail_projection_confirmed')
        self.assertEqual(sum(tool=='upload_file' and args['file_name']=='專案詳情索引.md' for tool,args in self.writes),1)

    def test_concurrent_recovery_rejected_by_same_single_writer_lock(self):
        storage=driver.detail.storage(self.f.runtime,create=True)
        with git_store.locked(storage):
            self.assertEqual(self.run_projection()['code'],'IN_FLIGHT')
        self.assertEqual(self.writes,[])

    def test_new_canonical_generation_during_publish_never_reports_current(self):
        original=self.reader.read_authority
        def advancing(binding,nonce):
            state=driver.detail.load(self.f.runtime,'Demo')
            if state['active'] and state['active']['phase']=='complete' and self.version==1:
                self.f.make_source(2);self.version=2
            return original(binding,nonce)
        self.reader.read_authority=advancing
        self.assertEqual(self.run_projection()['code'],'REVISION_CONFLICT')
        receipt_path=driver.detail.storage(self.f.runtime)/('receipt-'+core.sha(self.operation.encode())+'.json')
        self.assertFalse(receipt_path.exists())

    def test_wrong_observed_parent_blocks_without_assuming_mapping(self):
        self.assertEqual(self.run_projection()['outcome'],'detail_projection_confirmed')
        index=driver.detail.load(self.f.runtime,'Demo')['index']['file_id'];self.parents[index]='outside-folder'
        count=len(self.writes)
        self.assertEqual(self.run_projection()['code'],'READBACK_UNVERIFIED')
        self.assertEqual(len(self.writes),count)

if __name__=='__main__':unittest.main()
