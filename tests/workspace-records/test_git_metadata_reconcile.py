"""Audited catalog maintenance: real local Git, isolated runtime, no live writes."""
import copy
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'skills/workspace-records/scripts'))
import core
import git_store
import routing
import connector_protocol
import manifest
import git_metadata_reconcile as reconcile
from test_core import STATUS

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.notes = self.root / 'project-notes'
        self.project = self.notes / 'projects/Demo'
        self.project.mkdir(parents=True)
        (self.project/'status.md').write_bytes(STATUS)
        (self.project/'overview.md').write_bytes(b'# Overview\n')
        (self.root/routing.RUNTIME).mkdir(parents=True)
        (self.root/routing.POLICY).write_text('{}')
        git_store.write_json(self.root/routing.REGISTRY, {'schema_version':1,'policy_version':'1','generation':1,'projects':{'Demo':{'classification':'managed','backend':'git_local','route_phase':'steady','locator':None}}})
        self.registry = routing.Registry(self.root)
        self.binding = self.registry.bind('Demo')
        self.runtime = pathlib.Path(self.binding['runtime_directory'])
        self.runtime.mkdir(parents=True)
        (self.runtime/'record-metadata').mkdir()
        self.control = {'mode':'git','phase':'steady','epoch':3,'version':7,'policy_version':'1','record_metadata':'required-v1'}
        git_store.write_json(self.runtime/'control.json', self.control)
        self.catalog = {'schema_version':1,'project_id':'Demo','records':[],'attachments':[]}
        for index, path in enumerate(('status.md','overview.md')):
            self.catalog['records'].append({'record_id':'stable-'+str(index),'kind':'status' if index==0 else 'overview','created_at':'2020-01-01T00:00:00Z','updated_at':'2021-01-01T00:00:00Z','source':{'relative_path':path,'content_sha256':core.sha((self.project/path).read_bytes()),'original_time':'retained','extra':'retained'},'body':{'format':'utf-8'},'metadata':{'title':'Preserve'},'relations':{'parent':'stable-parent'}})
        git_store.write_json(self.runtime/'record-metadata/records.json', self.catalog)
        (self.project/'status.md').write_bytes(STATUS.replace(b'First',b'New current'))
        for args in (('init','-b','main'),('config','user.name','Synthetic'),('config','user.email','synthetic@example.invalid'),('add','projects'),('commit','-m','fixture')):
            self.git(*args)
        self.commit = self.git('rev-parse','HEAD').strip()
        observed = patch.object(reconcile.pushed,'observe_pushed_checkpoint',side_effect=lambda *args:{'source_commit':self.git('rev-parse','HEAD').strip(),'repository':'example/notes','default_branch':'main'})
        observed.start(); self.addCleanup(observed.stop)
        scanner = patch.object(reconcile.privacy,'check',side_effect=lambda value,config:{'state':'passed','content_sha256':core.object_hash(value)})
        scanner.start(); self.addCleanup(scanner.stop)
        self.request = {'schema_version':1,'project_id':'Demo','operation_id':'reconcile-1','actor_role':'pm','expected':{'registry_sha256':self.binding['registry_sha256'],'control_sha256':core.sha((self.runtime/'control.json').read_bytes()),'catalog_sha256':core.sha((self.runtime/'record-metadata/records.json').read_bytes()),'source_commit':self.commit},'records':[{'record_id':'stable-0','path':'status.md','expected_catalog_content_sha256':self.catalog['records'][0]['source']['content_sha256'],'current_content_sha256':core.sha((self.project/'status.md').read_bytes())}]}

    def git(self,*args):
        value = subprocess.run(['git','-C',str(self.notes),*args],capture_output=True,text=True)
        self.assertEqual(value.returncode,0,value.stderr)
        return value.stdout

    def plan(self): return reconcile.preview(self.registry,self.request)
    def apply(self,p): return reconcile.finish(self.registry,self.request,p['plan'],p['plan_hash'])

    def test_preserves_every_field_except_hash_and_replay_is_historical(self):
        before = {p.name:p.read_bytes() for p in self.project.iterdir()}
        plan = self.plan()
        self.assertEqual(self.apply(plan)['outcome'],'complete')
        expected = copy.deepcopy(self.catalog)
        expected['records'][0]['source']['content_sha256'] = self.request['records'][0]['current_content_sha256']
        self.assertEqual(git_store.read_json(self.runtime/'record-metadata/records.json'),expected)
        self.assertEqual({p.name:p.read_bytes() for p in self.project.iterdir()},before)
        self.assertEqual(git_store.get_control(self.runtime),{**self.control,'version':8})
        journal=git_store.read_operation(self.runtime,'reconcile-1')
        self.assertNotIn('maintenance_plan',journal)
        self.assertNotIn(str(self.root),core.canonical(journal).decode())
        self.assertEqual(reconcile.load_plan(self.runtime,'reconcile-1'),plan['plan'])
        self.assertEqual(self.apply(plan)['freshness'],'historical_receipt')
        self.assertFalse(reconcile.inspect(self.registry,'Demo','reconcile-1')['requires_recovery'])

    def test_bootstrap_history_keeps_audit_without_local_plan(self):
        self.apply(self.plan())
        git_store.write_json(self.runtime/'control.json',{**git_store.get_control(self.runtime),'phase':'frozen'})
        scope={'project_id':'Demo','actions':['replace'],'records':[{'record_id':r['record_id'],'path':r['source']['relative_path'],'fields':['body']} for r in self.catalog['records']]}
        records=git_store.enumerate_records(self.project,self.runtime,{'schema_version':1,'request_id':'read','project_id':'Demo','actor_role':'pm','scope':scope})
        request={'schema_version':1,'request_id':'bootstrap','operation_id':'bootstrap','project_id':'Demo','actor_role':'pm','scope':scope,'action':'bootstrap','target_record_id':None,'expected':{'mode':'google','phase':'frozen','epoch':3,'version':8},'payload':{'recordset_sha256':core.object_hash(records)}}
        _,_,_,_,events,journal=connector_protocol.bootstrap_graph(request,records)
        history={'schema_version':1,'events':events,'journal':journal};raw=core.canonical(history)
        reference={'id':'history','sha256':core.sha(raw),'size':len(raw)}
        operations,_,_=manifest.validate_history({'history_pages':[reference]},{'history':raw})
        self.assertEqual(operations['reconcile-1']['maintenance_plan_hash'],self.plan_hash_from_local())
        self.assertNotIn('project_directory',raw.decode())
        self.assertNotIn('runtime_directory',raw.decode())
        self.assertEqual(len(operations),1)

    def plan_hash_from_local(self): return core.object_hash(reconcile.load_plan(self.runtime,'reconcile-1'))

    def test_scope_and_dirty_source_refuse_before_journal(self):
        self.request['records'][0]['record_id']='wrong'
        with self.assertRaisesRegex(core.RecordError,'FORBIDDEN'): self.plan()
        self.request['records'][0]['record_id']='stable-0'
        (self.project/'status.md').write_bytes(STATUS+b'dirty')
        with self.assertRaisesRegex(core.RecordError,'PUSHED_SOURCE_DIRTY'): self.plan()
        self.assertEqual(git_store.journal_entries(self.runtime),[])

    def test_inflight_frozen_and_barrier_refuse(self):
        path = git_store.operation_path(self.runtime,'other')
        git_store.write_json(path,{'operation_id':'other','state':'accepted'})
        with self.assertRaisesRegex(core.RecordError,'IN_FLIGHT'): self.plan()
        path.unlink()
        git_store.write_json(self.runtime/'control.json',{**self.control,'phase':'frozen'})
        with self.assertRaisesRegex(core.RecordError,'PHASE_FROZEN'): self.plan()

    def test_changed_plan_or_registry_refuses(self):
        p=self.plan(); bad=copy.deepcopy(p['plan']); bad['after_catalog']['records'][0]['metadata']={}
        with self.assertRaisesRegex(core.RecordError,'INTEGRITY_FAILED'): reconcile.finish(self.registry,self.request,bad,p['plan_hash'])
        data=git_store.read_json(self.root/routing.REGISTRY); data['generation']=2
        git_store.write_json(self.root/routing.REGISTRY,data)
        with self.assertRaises(core.RecordError): self.apply(p)
        self.assertEqual(git_store.journal_entries(self.runtime),[])

    def test_interruption_blocks_normal_apply_then_exact_recovery(self):
        p=self.plan(); original=git_store.write_json
        def stop(path,value):
            original(path,value)
            if pathlib.Path(path)==self.runtime/'record-metadata/records.json': raise RuntimeError('synthetic termination')
        with patch.object(git_store,'write_json',side_effect=stop):
            with self.assertRaisesRegex(RuntimeError,'synthetic termination'): self.apply(p)
        state=reconcile.inspect(self.registry,'Demo','reconcile-1')
        self.assertEqual((state['journal_state'],state['catalog'],state['control']),('accepted','after','before'))
        with self.assertRaisesRegex(core.RecordError,'MANUAL_REVIEW'): self.apply(p)
        with self.assertRaisesRegex(core.RecordError,'IN_FLIGHT'): self.plan()
        self.assertEqual(reconcile.finish(self.registry,self.request,p['plan'],p['plan_hash'],True)['outcome'],'complete')

    def test_failed_final_inventory_keeps_writer_barrier(self):
        p=self.plan()
        original=git_store._enumerate_records
        def changed(*args):
            result=original(*args)
            result['metadata_stale']=['status.md']
            return result
        with patch.object(git_store,'_enumerate_records',side_effect=changed):
            with self.assertRaisesRegex(core.RecordError,'READBACK_UNVERIFIED'): self.apply(p)
        self.assertEqual(git_store.read_operation(self.runtime,'reconcile-1')['state'],'control_done')
        with self.assertRaisesRegex(core.RecordError,'IN_FLIGHT'): git_store.ensure_no_incomplete(self.runtime)
        self.assertEqual(reconcile.finish(self.registry,self.request,p['plan'],p['plan_hash'],True)['outcome'],'complete')

    def test_privacy_scans_actual_source_and_failure_has_no_journal(self):
        with patch.object(reconcile.privacy,'check',side_effect=core.RecordError('SCAN_FINDING')):
            with self.assertRaisesRegex(core.RecordError,'SCAN_FINDING'): self.plan()
        self.assertEqual(git_store.journal_entries(self.runtime),[])
        self.plan()
        calls = reconcile.privacy.check.call_args_list
        self.assertTrue(any('files' in call.args[0] and 'New current' in call.args[0]['files']['status.md'] for call in calls))

if __name__=='__main__': unittest.main()
