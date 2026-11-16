import json
import pathlib
import subprocess
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPTS = HERE.parent.parent / 'skills' / 'workspace-records' / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import copy
import core
import google_docs
import disaster_recovery
import snapshot
from unittest import mock
import test_snapshot


class DisasterTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_snapshot.Tests('test_complete_all_history_and_bytes')
        self.fixture.setUp()
        for rid in self.fixture.refs:
            self.fixture.accept(rid)
        snapshot.finish({**self.fixture.key, 'document': self.fixture.document})
        self.args = {'private_dir': str(self.fixture.root), 'project_id': 'Demo', 'document_id': 'doc'}

    def tearDown(self):
        self.fixture.tearDown()

    def call(self, command, args):
        p = subprocess.run([sys.executable, str(SCRIPTS / 'disaster_recovery.py'), command],
                           input=json.dumps(args), capture_output=True, text=True)
        return p.returncode, json.loads(p.stdout)

    def test_assessment_and_consent_bound_preview(self):
        code, value = self.call('assess', self.args)
        self.assertEqual(code, 0, value)
        data = value['data']
        self.assertEqual(data['assessment']['future_gap'], 'unknown')
        target = self.fixture.root / 'target'
        target.mkdir()
        runtime = self.fixture.root / 'runtime'
        runtime.mkdir()
        args = {**self.args, 'assessment_hash': data['assessment_hash'],
                'approved_root': str(self.fixture.root), 'target_dir': str(target), 'runtime_dir': str(runtime), 'decision': {},
                'known_unresolved_writers': []}
        self.assertNotEqual(self.call('prepare', args)[0], 0)
        args['decision'] = {'assessment_hash': data['assessment_hash'], 'accept_unknown_gap': True,
                            'acknowledge_pending_work_unknown': True}
        code, preview = self.call('prepare', args)
        self.assertEqual(code, 0, preview)
        self.assertEqual(preview['data']['authority'], 'not_activated')
        self.assertTrue(preview['data']['restore']['plan']['files'])
        runtime_plan = preview['data']['runtime']
        args['restore_plan_hash'] = preview['data']['restore']['plan_hash']
        args['runtime_plan_hash'] = runtime_plan['plan_hash']
        code, restored = self.call('apply', args)
        self.assertEqual(code, 0, restored)
        self.assertEqual(restored['data']['authority'], 'not_activated')
        for item in preview['data']['restore']['plan']['files']:
            import hashlib
            raw = (target / item['path']).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), item['after_sha256'])
        control = json.loads((runtime / 'control.json').read_text())
        self.assertEqual(control['phase'], 'frozen')
        self.assertEqual(control['epoch'], data['assessment']['selected']['epoch'] + 1)
        self.assertTrue((runtime / 'record-metadata' / 'records.json').exists())
        self.assertEqual(runtime_plan['plan']['disaster']['epoch_order_against_unavailable_source'], 'unknown')
        args['known_unresolved_writers'] = ['writer']
        self.assertNotEqual(self.call('prepare', args)[0], 0)
        args['known_unresolved_writers'] = []
        args['assessment_hash'] = '0' * 64
        self.assertNotEqual(self.call('prepare', args)[0], 0)

    def test_interrupted_runtime_stays_partial_and_inspectable(self):
        assessed = disaster_recovery.assess(self.args)
        digest = assessed['assessment_hash']
        target = self.fixture.root / 'target'
        runtime = self.fixture.root / 'runtime'
        target.mkdir()
        runtime.mkdir()
        args = {**self.args, 'assessment_hash': digest,
                'approved_root': str(self.fixture.root), 'target_dir': str(target),
                'runtime_dir': str(runtime), 'known_unresolved_writers': [],
                'decision': {'assessment_hash': digest, 'accept_unknown_gap': True,
                             'acknowledge_pending_work_unknown': True}}
        preview = disaster_recovery.prepare(args)
        args['restore_plan_hash'] = preview['restore']['plan_hash']
        args['runtime_plan_hash'] = preview['runtime']['plan_hash']
        with mock.patch.object(disaster_recovery.restore, 'runtime_apply', side_effect=OSError('injected')):
            with self.assertRaises(OSError):
                disaster_recovery.apply(args)
        code, result = self.call('inspect', args)
        self.assertEqual(code, 0, result)
        self.assertEqual(result['data']['outcome'], 'partial')
        self.assertEqual(result['data']['runtime'], 'applying')
        self.assertTrue(all(item['state'] == 'after' for item in result['data']['files']))
        self.assertNotEqual(self.call('apply', args)[0], 0)

    def test_new_visible_snapshot_invalidates_old_assessment(self):
        import hashlib
        import shutil
        _, assessed = self.call('assess', self.args)
        assessment_hash = assessed['data']['assessment_hash']
        target = self.fixture.root / 'target'
        runtime = self.fixture.root / 'runtime'
        target.mkdir()
        runtime.mkdir()
        args = {**self.args, 'assessment_hash': assessment_hash,
                'approved_root': str(self.fixture.root), 'target_dir': str(target),
                'runtime_dir': str(runtime), 'known_unresolved_writers': [],
                'decision': {'assessment_hash': assessment_hash, 'accept_unknown_gap': True,
                             'acknowledge_pending_work_unknown': True}}
        code, preview = self.call('prepare', args)
        self.assertEqual(code, 0, preview)
        args['restore_plan_hash'] = preview['data']['restore']['plan_hash']
        args['runtime_plan_hash'] = preview['data']['runtime']['plan_hash']
        directory, state = snapshot.load(self.fixture.root, 'snap')
        current = state['source']['head']['root_manifest']
        event = {'operation_id': 'op-v3', 'request_hash': 'a' * 64,
                 'version': 3, 'action': 'replace'}
        journal = {'operation_id': 'op-v3', 'request_hash': 'a' * 64,
                   'state': 'complete', 'scope': {'project_id': 'Demo'},
                   'event': event, 'result': {'original': 'preserved'}}
        page = self.fixture.put('v3page', core.canonical({
            'schema_version': 1, 'events': [event], 'journal': [journal], 'previous_root': current}))
        current_manifest = json.loads(self.fixture.raw['current'])
        current_manifest['version'] = 3
        current_manifest['records'][0]['record']['metadata']['decision'] = 'retain context'
        current_manifest['attachments'][0]['attachment']['extra'] = {'preserve': True}
        current_manifest['history_pages'].append(page)
        root_ref = self.fixture.put('v3root', core.canonical(current_manifest))
        head = copy.deepcopy(state['source']['head'])
        head['version'] = 3
        head['root_manifest'] = root_ref
        document = test_snapshot.doc()
        def lines(values):
            return [{'paragraph': {'elements': [{'textRun': {'content': v + '\n'}}]}}
                    for v in values]
        document['tabs'][0]['body']['content'][1:1] = lines([google_docs.head_line(head)])
        document['tabs'][1]['body']['content'][1:1] = lines(google_docs.summary_lines(head))
        key = {'private_dir': str(self.fixture.root), 'snapshot_id': 'snap2'}
        snapshot.begin({**key, 'document': document, 'document_id': 'doc',
                        'head_tab_id': 'machine', 'summary_tab_id': 'human', 'project_id': 'Demo'})
        for rid, ref in self.fixture.refs.items():
            download = self.fixture.root / (rid + '.download')
            download.write_bytes(self.fixture.raw[rid])
            snapshot.accept({**key, 'reference': ref, 'readback_remote_id': rid,
                             'downloaded_file': str(download)})
        snapshot.finish({**key, 'document': document})
        _, newer = self.call('assess', self.args)
        self.assertEqual(newer['data']['assessment']['selected']['version'], 3)
        self.assertNotEqual(self.call('apply', args)[0], 0)
        self.assertFalse(list(target.iterdir()))
        newer_hash = newer['data']['assessment_hash']
        new_args = {**args, 'assessment_hash': newer_hash,
                    'decision': {'assessment_hash': newer_hash, 'accept_unknown_gap': True,
                                 'acknowledge_pending_work_unknown': True}}
        _, new_preview = self.call('prepare', new_args)
        new_args['restore_plan_hash'] = new_preview['data']['restore']['plan_hash']
        new_args['runtime_plan_hash'] = new_preview['data']['runtime']['plan_hash']
        code, recovered = self.call('apply', new_args)
        self.assertEqual(code, 0, recovered)
        catalog = json.loads((runtime / 'record-metadata' / 'records.json').read_text())
        self.assertEqual(catalog['records'][0]['metadata']['decision'], 'retain context')
        self.assertEqual(catalog['attachments'][0]['extra'], {'preserve': True})
        self.assertEqual((target / 'tasks' / '.gitkeep').read_bytes(), b'')
        operations = list(runtime.glob('operation-*.json'))
        self.assertEqual(len(operations), 1)
        self.assertEqual(json.loads(operations[0].read_text())['event'], event)
        fork_manifest = copy.deepcopy(current_manifest)
        fork_manifest['records'][0]['record']['metadata']['decision'] = 'alternate'
        fork_ref = self.fixture.put('forkroot', core.canonical(fork_manifest))
        fork_head = copy.deepcopy(head)
        fork_head['root_manifest'] = fork_ref
        fork_doc = test_snapshot.doc()
        fork_doc['tabs'][0]['body']['content'][1:1] = lines([google_docs.head_line(fork_head)])
        fork_doc['tabs'][1]['body']['content'][1:1] = lines(google_docs.summary_lines(fork_head))
        fork_key = {'private_dir': str(self.fixture.root), 'snapshot_id': 'fork'}
        snapshot.begin({**fork_key, 'document': fork_doc, 'document_id': 'doc',
                        'head_tab_id': 'machine', 'summary_tab_id': 'human', 'project_id': 'Demo'})
        for rid, ref in self.fixture.refs.items():
            download = self.fixture.root / (rid + '.fork-download')
            download.write_bytes(self.fixture.raw[rid])
            snapshot.accept({**fork_key, 'reference': ref, 'readback_remote_id': rid,
                             'downloaded_file': str(download)})
        snapshot.finish({**fork_key, 'document': fork_doc})
        self.assertNotEqual(self.call('assess', self.args)[0], 0)
        code, failure = self.call('apply', args)
        self.assertNotEqual(code, 0, failure)
        self.assertEqual((target / 'tasks' / '.gitkeep').read_bytes(), b'')

    def test_tampered_object_rejected(self):
        directory, state = snapshot.load(self.fixture.root, 'snap')
        path = directory / next(iter(state['objects'].values()))['filename']
        path.write_bytes(b'tampered')
        self.assertNotEqual(self.call('assess', self.args)[0], 0)


if __name__ == '__main__':
    unittest.main()










