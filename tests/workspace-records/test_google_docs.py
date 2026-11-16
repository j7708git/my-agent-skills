import pathlib, sys, unittest
from unittest.mock import patch
SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / 'skills/workspace-records/scripts'
sys.path.insert(0, str(SCRIPTS))
import core, google_docs, manifest, projection


def doc():
    def tab(tid, lines):
        return {'tabId':tid,'body':{'content':[{'sectionBreak':{}}, *[{'paragraph':{'elements':[{'textRun':{'content':s+'\n'}}]}} for s in lines]]}}
    return {'documentId':'doc','revisionId':'rev0','tabs':[tab('machine',['']),tab('human',['人工補充',''])]}


def head():
    return {'schema_version':1,'project_id':'Demo','control':{'mode':'google','phase':'steady','epoch':1,'policy_version':'1'},'version':1,'root_manifest':{'id':'m','sha256':'a'*64,'size':1},'last_operation':{'operation_id':'op','request_hash':'b'*64},'summary':dict.fromkeys(('state','now','next','needs','blocker','updated','decisions'),'')}


class Tests(unittest.TestCase):
    def test_bootstrap_two_tabs_one_revision(self):
        snapshot=google_docs.snapshot(doc(),'doc','machine','human','Demo',True)
        result=google_docs.batch(snapshot,head())
        self.assertEqual(result['write_control']['requiredRevisionId'],'rev0')
        self.assertEqual([r['insertText']['location']['tabId'] for r in result['requests']],['machine','human'])
        self.assertIn('專案現況',result['requests'][1]['insertText']['text'])
        self.assertNotIn('人工補充',result['requests'][1]['insertText']['text'])

    def test_duplicate_markers_refuse(self):
        d=doc(); d['tabs'][0]['body']['content'].append({'paragraph':{'elements':[{'textRun':{'content':'[[WFG-P3-HEAD]]oops\n'}}]}})
        with self.assertRaises(core.RecordError): google_docs.snapshot(d,'doc','machine','human','Demo',True)

    def test_manifest_hash_and_missing_history(self):
        raw=core.canonical({'schema_version':1,'events':[],'journal':[]})
        ref={'id':'a','size':len(raw),'sha256':core.sha(raw)}
        self.assertEqual(manifest.verify_bytes(raw,ref),raw)
        with self.assertRaises(core.RecordError): manifest.verify_bytes(raw+b' ',ref)
        with self.assertRaises(core.RecordError): manifest.validate_history({'history_pages':[ref]}, {})

    def test_history_over_100_and_collision(self):
        pages={}; refs=[]
        for n in range(3):
            journal=[{'operation_id':f'o{n*50+i}','request_hash':'a'*64,'result':{}} for i in range(50)]
            raw=core.canonical({'schema_version':1,'events':[],'journal':journal}); rid=f'p{n}'
            pages[rid]=raw; refs.append({'id':rid,'size':len(raw),'sha256':core.sha(raw)})
        ops,_,_=manifest.validate_history({'history_pages':refs},pages)
        self.assertEqual(len(ops),150)
        pages['p2']=pages['p0']; refs[2]={'id':'p2','size':len(pages['p2']),'sha256':core.sha(pages['p2'])}
        with self.assertRaises(core.RecordError): manifest.validate_history({'history_pages':refs},pages)

    def test_projection_set_stale_and_uncertain(self):
        expected={'expected':[{'id':'a'},{'id':'b'}]}
        self.assertEqual(projection.detail_reconcile(expected,['a'],False)['outcome'],'manual_review')
        self.assertEqual(projection.detail_reconcile(expected,['a'],True)['outcome'],'reprepare_from_latest')
        self.assertEqual(projection.detail_reconcile(expected,['a','b'],True)['freshness'],'current_set_only')

if __name__ == '__main__': unittest.main()
