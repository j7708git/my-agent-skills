import pathlib
import os
import subprocess
import sys
import unittest
from unittest import mock

SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / 'skills/workspace-records/scripts'
sys.path.insert(0, str(SCRIPTS))
import core
import disaster_activation
import connector_protocol
import test_routing
import test_core
import snapshot
import test_snapshot

class Tests(unittest.TestCase):
    def setUp(self):
        self.f = test_snapshot.Tests('test_complete_all_history_and_bytes')
        self.f.setUp()
        for rid in self.f.refs:
            self.f.accept(rid)
        snapshot.finish({**self.f.key, 'document': self.f.document})
        self.args = {'private_dir': str(self.f.root), 'project_id': 'Demo',
                     'document_id': 'doc', 'writer_pids': [12345678], 'actor_role': 'pm'}
        policy = self.f.root / 'policy.json'
        policy.write_text('{}')
        self.binding = {'privacy_policy': str(policy), 'backend': 'google_head', 'route_phase': 'frozen',
                        'locator': {'document_id': 'doc'}, 'registry_generation': 1,
                        'project_directory': str(self.f.root), 'runtime_directory': str(self.f.root)}
        self.registry = mock.Mock()
        self.registry.bind.return_value = self.binding

    def tearDown(self):
        self.f.tearDown()

    def test_template_is_unsigned_and_exactly_bound(self):
        decision = disaster_activation.decision_template(self.args, self.registry)
        self.assertFalse(decision['accepted_by_user'])
        self.assertEqual(decision['future_gap'], 'unknown')
        self.assertEqual(decision['remote_writers'], 'unknown_no_reconnect')
        self.assertEqual(decision['source_version'], 2)
        self.assertEqual(decision['binding_hash'], core.object_hash(self.binding))
        with self.assertRaisesRegex(core.RecordError, 'FORBIDDEN'):
            disaster_activation.validate_decision({**self.args, 'decision': decision}, self.registry)
        changed = {**decision, 'accepted_by_user': True, 'source_version': 1}
        with self.assertRaisesRegex(core.RecordError, 'FORBIDDEN'):
            disaster_activation.validate_decision({**self.args, 'decision': changed}, self.registry)
        pathlib.Path(self.binding['privacy_policy']).write_text('{"changed":true}')
        with self.assertRaisesRegex(core.RecordError, 'FORBIDDEN'):
            disaster_activation.validate_decision({**self.args, 'decision': {**decision, 'accepted_by_user': True}}, self.registry)
        pathlib.Path(self.binding['privacy_policy']).write_text('{}')
        self.binding['registry_generation'] += 1
        with self.assertRaisesRegex(core.RecordError, 'FORBIDDEN'):
            disaster_activation.validate_decision({**self.args, 'decision': {**decision, 'accepted_by_user': True}}, self.registry)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows holder only')
    def test_actual_holder_and_live_writer_refusal(self):
        self.assertFalse(disaster_activation.writer_stopped(os.getpid()))
        proc = subprocess.Popen([sys.executable, str(SCRIPTS / 'disaster_activation.py'),
                                 '--holder', str(self.f.root)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                creationflags=0x08000000)
        try:
            identity = disaster_activation.challenge(proc, None)
            self.assertEqual(disaster_activation.challenge(proc, identity), identity)
            with self.assertRaisesRegex(core.RecordError, 'IN_FLIGHT'):
                disaster_activation.check_writers({'writer_pids': [os.getpid()]})
        finally:
            if proc.poll() is None:
                proc.stdin.write('{"action":"stop"}\n')
                proc.stdin.flush()
                proc.wait(timeout=5)
        with self.assertRaisesRegex(core.RecordError, 'MANUAL_REVIEW'):
            disaster_activation.challenge(proc, identity)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()

    def test_unknown_publication_reports_fresh_observation_and_stop(self):
        workspace = self.f.root
        (workspace / '.openchamber' / 'workspace-records').mkdir(parents=True)
        self.registry.workspace = workspace
        state = {'phase': 'publish_steady_unknown', 'plan_hash': 'a' * 64,
                 'plan': {'decision': {'target_project': 'Demo'}},
                 'frozen_binding': {'route_phase': 'frozen'}}
        state['plan_hash'] = core.object_hash(state['plan'])
        key = {'operation_id': 'unknown'}
        path = disaster_activation.session_path(key, self.registry)
        disaster_activation.git_store.write_json(path, state)
        self.binding['backend'] = 'git_local'
        self.binding['route_phase'] = 'steady'
        observed = {'mode': 'git', 'phase': 'steady', 'epoch': 2, 'version': 3}
        with mock.patch.object(disaster_activation.git_store, 'get_control', return_value=observed):
            result = disaster_activation.inspect(key, self.registry)
        self.assertEqual(result['observed_binding']['route_phase'], 'steady')
        self.assertEqual(result['observed_control'], observed)
        self.assertEqual(result['last_confirmed_binding']['route_phase'], 'frozen')
        self.assertEqual(result['instruction'], 'stop_writers_and_reconcile')
        self.assertTrue(result['unknown_requires_manual_reconciliation'])

    def test_real_google_managed_entry_refuses_pending_barrier(self):
        fixture = test_routing.TestRouting('test_google_unavailable_never_falls_back_to_git')
        fixture.setUp()
        try:
            fixture.google()
            barriers = fixture.root / '.openchamber/workspace-records/activation-barriers'
            barriers.mkdir()
            marker = barriers / (core.sha(b'Legacy_Project') + '.json')
            marker.write_text('{}')
            with self.assertRaisesRegex(core.RecordError, 'PHASE_FROZEN'):
                connector_protocol.managed_dispatch('prepare',
                    {'request': {'project_id': 'Legacy_Project'}}, fixture.registry)
        finally:
            fixture.tearDown()

    def test_resumed_readback_rejects_corrupt_control_or_journal(self):
        req = test_core.request()
        req.update(action='resume', actor_role='pm', target_record_id=None,
                   payload={'transition_id': 'drill', 'transition_intent_sha256': 'a' * 64})
        control = {'mode': 'git', 'phase': 'frozen', 'epoch': 2, 'version': 2,
                   'policy_version': '1', 'record_metadata': 'required-v1'}
        steady = {**control, 'phase': 'steady', 'version': 3}
        rh = core.request_hash(req)
        operation = {'state': 'complete', 'request_hash': rh,
                     'event': {'request_hash': rh, 'operation_id': req['operation_id'], 'version': 3},
                     'result': {'request_hash': rh, 'operation_id': req['operation_id'],
                                'epoch': 2, 'phase': 'steady', 'authority':
                                {'logical_version': 3, 'readback_verified': True}}}
        binding = {'runtime_directory': str(self.f.root)}
        with mock.patch.object(disaster_activation.git_store, 'get_control', return_value=steady), \
             mock.patch.object(disaster_activation.git_store, 'read_operation', return_value=operation), \
             mock.patch.object(disaster_activation.git_store, 'metadata_catalog'):
            disaster_activation.verify_resumed(binding, control, req, 'Demo')
            operation['event']['version'] = 2
            with self.assertRaisesRegex(core.RecordError, 'INTEGRITY_FAILED'):
                disaster_activation.verify_resumed(binding, control, req, 'Demo')
            operation['event']['version'] = 3
            steady['phase'] = 'frozen'
            with self.assertRaisesRegex(core.RecordError, 'INTEGRITY_FAILED'):
                disaster_activation.verify_resumed(binding, control, req, 'Demo')

    def test_non_frozen_route_refused(self):
        self.binding['route_phase'] = 'steady'
        with self.assertRaisesRegex(core.RecordError, 'REVISION_CONFLICT'):
            disaster_activation.decision_template(self.args, self.registry)

if __name__ == '__main__':
    unittest.main()
