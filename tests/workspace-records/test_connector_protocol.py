import base64, copy, json, pathlib, sys, tempfile, unittest
from unittest.mock import patch
SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / 'skills/workspace-records/scripts'
sys.path.insert(0, str(SCRIPTS))
import connector_protocol as p, core, google_docs
from test_google_docs import doc
from test_core import STATUS


def scan(raw, mime, config):
    return {'raw_sha256':core.sha(raw),'raw_size':len(raw),'policy_sha256':'p','rules_sha256':'r','coverage':'synthetic','pii_enabled':False}


def batch_scan(payload,config):
    return {'state':'passed','content_sha256':core.object_hash(payload),'policy_sha256':'p','rules_sha256':'r','coverage':'synthetic','pii_enabled':False}


def update_doc(d, batch):
    d=copy.deepcopy(d)
    byid={t['tabId']:t for t in d['tabs']}
    for request in batch['requests']:
        if 'insertText' in request:
            op=request['insertText']; tid=op['location']['tabId']; content=byid[tid]['body']['content']
            insert=[{'paragraph':{'elements':[{'textRun':{'content':line+'\n'}}]}} for line in op['text'].splitlines()]
            content[1:1]=insert
        else:
            op=request['replaceAllText']; tid=op['tabsCriteria']['tabIds'][0]
            for item in byid[tid]['body']['content'][1:]:
                run=item['paragraph']['elements'][0]['textRun']
                run['content']=run['content'].replace(op['containsText']['text'],op['replaceText'])
    d['revisionId']='rev1'
    return d


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=pathlib.Path(self.tmp.name)
        self.old=patch.object(p,'scan_bytes',side_effect=scan); self.old.start()
        self.privacy=patch.object(p.privacy,'check',side_effect=batch_scan); self.privacy.start()
        huge=('中文\r\n'*270000).encode()
        records=[]
        for rid,path,kind,raw in [('status','status.md','status',STATUS),('large','research/large.md','research',huge)]:
            records.append({'record_id':rid,'kind':kind,'created_at':None,'updated_at':None,'source':{'relative_path':path,'content_sha256':core.sha(raw),'original_time':None},'body':{'format':'utf-8','content':raw.decode()},'metadata':{},'relations':[]})
        attachment={'attachment_id':'keep','logical_name':'.gitkeep','mime':'application/octet-stream','size':0,'sha256':core.sha(b''),'locator_alias':None,'source_relative_path':'tasks/.gitkeep','raw_bytes_b64':'','metadata':{}}
        journal=[{'operation_id':f'old{i}','request_hash':'a'*64,'result':{'logical_version':i},'state':'complete','scope':{'project_id':'Demo','actions':['replace'],'records':[]},'event':{'version':i,'operation_id':f'old{i}','request_hash':'a'*64}} for i in range(101)]
        self.rs={'schema_version':1,'project_id':'Demo','control':{'mode':'git','phase':'frozen','epoch':2,'version':101,'policy_version':'1'},'version':101,'records':records,'events':[{'version':i,'operation_id':f'old{i}','request_hash':'a'*64} for i in range(101)],'journal':journal,'attachments':[attachment],'completeness':'complete','incomplete_operations':[],'unselected_paths':[]}
        self.req={'schema_version':1,'request_id':'r','project_id':'Demo','actor_role':'pm','scope':{'project_id':'Demo','actions':['replace'],'records':[{'record_id':r['record_id'],'path':r['source']['relative_path'],'fields':['body']} for r in records]+[{'record_id':'keep','path':'tasks/.gitkeep','fields':['body']}]},'operation_id':'boot','action':'bootstrap','target_record_id':None,'payload':{'recordset_sha256':core.object_hash(self.rs)},'expected':{'mode':'google','phase':'frozen','epoch':2,'version':101}}
        self.key={'private_dir':str(self.root),'project_id':'Demo','operation_id':'boot'}
        self.prep={**self.key,'request':self.req,'recordset':self.rs,'document':doc(),'document_id':'doc','head_tab_id':'machine','summary_tab_id':'human','privacy_config':{}}
        self.uploads={}

    def tearDown(self):
        self.old.stop(); self.privacy.stop(); self.tmp.cleanup()

    def upload(self, aid):
        u=p.begin_upload({**self.key,'artifact_id':aid}); raw=pathlib.Path(u['file_uri']).read_bytes(); prefix='' if self.key['operation_id']=='boot' else self.key['operation_id']+'-'; path=self.root/(prefix+aid+'.download'); path.write_bytes(raw); rid='remote-'+prefix+aid
        receipt=p.accept_upload({**self.key,'artifact_id':aid,'remote_id':rid,'readback_remote_id':rid,'downloaded_file':str(path)})
        self.uploads[rid]=str(path)
        self.assertEqual(receipt['sha256'],core.sha(raw))

    def test_bootstrap_full_graph_and_cas_unknown(self):
        initial=p.prepare(self.prep)
        self.assertGreaterEqual(len(initial['artifacts']),5)
        self.assertEqual(initial['inline_empty_count'],1)
        for item in initial['artifacts']: self.upload(item['artifact_id'])
        self.assertEqual(p.prepare_cas(self.key)['outcome'],'manifest_ready')
        self.upload('manifest')
        ready=p.prepare_cas(self.key)
        batch=p.begin_cas({**self.key,'batch_hash':ready['batch_hash']})['batch']
        with self.assertRaises(core.RecordError): p.begin_cas({**self.key,'batch_hash':ready['batch_hash']})
        self.assertEqual(p.reconcile_cas({**self.key,'document':doc(),'manifest_readback_file':self.uploads['remote-manifest']})['outcome'],'manual_review')
        result=p.reconcile_cas({**self.key,'document':update_doc(doc(),batch),'manifest_readback_file':self.uploads['remote-manifest']})
        self.assertEqual(result['outcome'],'committed')
        prior,ops,objects,events,journal=p.read_graph(self.root,result['head'],{'manifest_file':self.uploads['remote-manifest'],'objects':{k:v for k,v in self.uploads.items() if k!='remote-manifest'}})
        self.assertEqual(len(ops),102); self.assertEqual(len(events),102)
        self.assertEqual(len(objects['inline-empty-v1']),0)
        self.assertGreater(len(objects['remote-record-1'].decode()),1020000)
        self.assertIn(b'\r\n',objects['remote-record-1'])
        missing={k:v for k,v in self.uploads.items() if k not in ('remote-manifest','remote-history-1')}
        with self.assertRaises(core.RecordError): p.read_graph(self.root,result['head'],{'manifest_file':self.uploads['remote-manifest'],'objects':missing})
        self.assertEqual(result['head']['control']['phase'],'frozen')
        self.assertEqual(result['head']['control']['epoch'],2)
        self.assertEqual(result['head']['version'],102)
        # Isolated update unit probe only: simulate a steady head; the real
        # resume transition is a separate operation and is not exercised here.
        simulated=update_doc(doc(),batch); h=copy.deepcopy(result['head']); h['control']['phase']='steady'
        simulated['tabs'][0]['body']['content'][1]['paragraph']['elements'][0]['textRun']['content']=google_docs.head_line(h)+'\n'
        self.exercise_update(simulated,h)

    def exercise_update(self, document, head):
        req={'schema_version':1,'request_id':'u','project_id':'Demo','actor_role':'pm','scope':{'project_id':'Demo','actions':['status_patch'],'records':[{'record_id':'status','path':'status.md','fields':['now']}]},'operation_id':'update','action':'status_patch','target_record_id':'status','payload':{'fields':{'now':'中文更新'},'base_sha256':core.sha(STATUS)},'expected':{'mode':'google','phase':'steady','epoch':2,'version':102}}
        existing={'manifest_file':self.uploads['remote-manifest'],'objects':{k:v for k,v in self.uploads.items() if k!='remote-manifest'}}
        key={**self.key,'operation_id':'update'}
        x={**key,'request':req,'document':document,'document_id':'doc','head_tab_id':'machine','summary_tab_id':'human','privacy_config':{},'existing':existing}
        prepared=p.prepare(x)
        original_key=self.key; self.key=key
        for item in prepared['artifacts']: self.upload(item['artifact_id'])
        p.prepare_cas(key); self.upload('manifest'); ready=p.prepare_cas(key)
        sent=p.begin_cas({**key,'batch_hash':ready['batch_hash']})['batch']
        after=update_doc(document,sent); after['revisionId']='rev2'
        result=p.reconcile_cas({**key,'document':after,'manifest_readback_file':self.uploads['remote-update-manifest']})
        self.assertEqual(result['outcome'],'committed')
        self.assertIn('中文更新',str(after))
        self.assertIn('人工補充',str(after))
        binding={'backend':'google_head','route_phase':'steady','runtime_directory':str(self.root),'locator':{'document_id':'doc'},'privacy_policy':str(self.root/'policy'),'policy_version':'1'}
        class FakeRegistry:
            def bind(self, project):
                self.project=project
                return binding
            def check(self, bound, authority):
                self.authority=authority
                if authority['control']['phase'] != 'steady': raise core.RecordError('PHASE_FROZEN')
        registry=FakeRegistry()
        with patch.object(p,'dispatch',return_value={'outcome':'guarded'}) as called:
            guarded={**x,'document':after,'privacy_config':{'policy':'untrusted'}}
            self.assertEqual(p.managed_dispatch('prepare',guarded,registry)['outcome'],'guarded')
            self.assertEqual(registry.authority['control']['version'],103)
            self.assertEqual(called.call_args.args[1]['privacy_config']['policy'],binding['privacy_policy'])
            wrong={**guarded,'document_id':'wrong'}
            with self.assertRaises(core.RecordError): p.managed_dispatch('prepare',wrong,registry)
        graph=json.loads(pathlib.Path(self.uploads['remote-update-manifest']).read_text(encoding='utf-8'))
        refs=graph['history_pages']+[r['object'] for r in graph['records']]+[a['object'] for a in graph['attachments']]
        replay_existing={'manifest_file':self.uploads['remote-update-manifest'],'objects':{r['id']:self.uploads[r['id']] for r in refs if r.get('encoding')!='inline-empty-v1'}}
        replay={**x,'document':after,'existing':replay_existing}
        self.assertEqual(p.prepare(replay)['outcome'],'no_write')
        frozen=copy.deepcopy(after)
        frozenhead=copy.deepcopy(result['head']); frozenhead['control']['phase']='frozen'; frozenhead['control']['epoch']+=1
        run=frozen['tabs'][0]['body']['content'][1]['paragraph']['elements'][0]['textRun']
        run['content']=google_docs.head_line(frozenhead)+'\n'
        frozen['revisionId']='rev-frozen'
        self.assertEqual(p.managed_dispatch('prepare',{**replay,'document':frozen},registry)['outcome'],'no_write')
        novel=copy.deepcopy(replay); novel['document']=frozen; novel['request']['operation_id']='novel'
        with self.assertRaisesRegex(core.RecordError,'STALE_EPOCH|PHASE_FROZEN'):
            p.managed_dispatch('prepare',novel,registry)
        changed=copy.deepcopy(replay); changed['request']['payload']['fields']['now']='其他'
        with self.assertRaisesRegex(core.RecordError,'ID_REUSE'): p.prepare(changed)
        self.key=original_key

    def test_bootstrap_requires_complete_source_and_history(self):
        for mutation in ('missing','bad_control','bad_version','bad_history'):
            rs=copy.deepcopy(self.rs)
            if mutation=='missing': del rs['completeness']
            elif mutation=='bad_control': rs['control']['mode']='unknown'
            elif mutation=='bad_version': rs['version']=-20
            else: rs['events']=[]
            req=copy.deepcopy(self.req); req['payload']['recordset_sha256']=core.object_hash(rs)
            with self.subTest(mutation=mutation), self.assertRaises(core.RecordError): p.bootstrap_graph(req,rs)

    def test_private_session_names_do_not_alias(self):
        a=p.session_dir(self.root,'foo--bar','baz',create=True)
        b=p.session_dir(self.root,'foo','bar--baz',create=True)
        c=p.session_dir(self.root,'Foo','bar--baz',create=True)
        self.assertEqual(len({a,b,c}),3)

    def test_inflight_never_reuploads(self):
        aid=p.prepare(self.prep)['artifacts'][0]['artifact_id']; p.begin_upload({**self.key,'artifact_id':aid})
        with self.assertRaises(core.RecordError): p.begin_upload({**self.key,'artifact_id':aid})
        path=self.root/'bad'; path.write_bytes(b'wrong')
        with self.assertRaises(core.RecordError): p.accept_upload({**self.key,'artifact_id':aid,'remote_id':'x','readback_remote_id':'x','downloaded_file':str(path)})

if __name__=='__main__': unittest.main()
