import pathlib, sys, unittest
from unittest.mock import patch
SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / 'skills/workspace-records/scripts'
sys.path.insert(0,str(SCRIPTS))
import core, google_docs, projection
from test_google_docs import doc, head

class Tests(unittest.TestCase):
    def test_sheet_requires_current_row_and_settled_prior_write(self):
        source=doc(); h=head()
        def lines(items):
            return [{'paragraph':{'elements':[{'textRun':{'content':item+'\n'}}]}} for item in items]
        source['tabs'][0]['body']['content'][1:1]=lines([google_docs.head_line(h)])
        source['tabs'][1]['body']['content'][1:1]=lines(google_docs.summary_lines(h))
        x={'project_id':'Demo','document':source,'document_id':'doc','head_tab_id':'machine','summary_tab_id':'human','target':{'spreadsheet_id':'sheet','sheet_id':0,'row_index':2},'privacy_config':{}}
        with patch.object(projection.privacy,'check',return_value={'state':'passed'}):
            result=projection.sheet_prepare(x)
            self.assertFalse(result['authoritative'])
            self.assertEqual(result['source']['version'],1)
            self.assertEqual(result['payload_hash'],core.object_hash(result['payload']))
            self.assertEqual(projection.sheet_reconcile({**x,'observed_row':result['row']})['freshness'],'current')
            self.assertEqual(projection.sheet_reconcile({**x,'observed_row':['old']})['outcome'],'manual_review')
            self.assertEqual(projection.sheet_reconcile({**x,'observed_row':['old'],'previous_write_settled':True})['outcome'],'reprepare_from_latest')
            self.assertEqual(projection.sheet_reconcile({**x,'observed_row':None})['freshness'],'unknown')

if __name__=='__main__': unittest.main()
