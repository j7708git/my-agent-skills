import pathlib, sys, unittest
SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / 'skills/workspace-records/scripts'
sys.path.insert(0, str(SCRIPTS))
import core, manifest

class Tests(unittest.TestCase):
    def test_inline_empty_descriptor_validation(self):
        ref={'id':'inline-empty-v1','sha256':core.sha(b''),'size':0,'encoding':'inline-empty-v1'}
        self.assertEqual(manifest.verify_bytes(b'',ref),b'')
        for invalid in ({**ref,'size':1},{**ref,'sha256':'a'*64},{'id':'inline-empty-v1','sha256':core.sha(b''),'size':0},{**ref,'id':'other'}):
            with self.assertRaises(core.RecordError): manifest.reference(invalid)

    def test_gitkeep_cannot_be_text_record(self):
        ref={'id':'id','sha256':core.sha(b''),'size':0}
        record={'record_id':'x','kind':'research','created_at':None,'updated_at':None,'source':{'relative_path':'tasks/.gitkeep','content_sha256':ref['sha256'],'original_time':None},'body':{'format':'utf-8'},'metadata':{},'relations':[]}
        value={'schema_version':1,'project_id':'Demo','version':1,'records':[{'record':record,'object':ref}],'attachments':[],'history_pages':[],'source_root':{}}
        with self.assertRaises(core.RecordError): manifest.validate_manifest(value,'Demo')

    def test_gitkeep_must_be_empty(self):
        raw=b'x'; ref={'id':'id','sha256':core.sha(raw),'size':len(raw)}
        value={'schema_version':1,'project_id':'Demo','version':1,'records':[],'attachments':[{'attachment':{'attachment_id':'x','source_relative_path':'tasks/.gitkeep','sha256':ref['sha256'],'size':ref['size']},'object':ref}],'history_pages':[],'source_root':{}}
        with self.assertRaises(core.RecordError): manifest.validate_manifest(value,'Demo')
        empty=b''; ref.update(size=0,sha256=core.sha(empty)); value['attachments'][0]['attachment'].update(size=0,sha256=ref['sha256'])
        self.assertEqual(manifest.validate_manifest(value,'Demo'),value)
        value['attachments'][0]['attachment']['source_relative_path']='tasks/.hidden'
        with self.assertRaises(core.RecordError): manifest.validate_manifest(value,'Demo')

if __name__=='__main__': unittest.main()
