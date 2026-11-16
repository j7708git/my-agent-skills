"""合成 Runner gate；不啟動 client、不讀 live registry。"""
import copy
import json
import pathlib
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'skills/workspace-records/scripts'))
import dispatch_guard as guard
import routing
from core import RecordError


class TestDispatchGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / 'project-notes/projects/example').mkdir(parents=True)
        (self.root / routing.RUNTIME).mkdir(parents=True)
        (self.root / routing.POLICY).write_text('{}')
        self.data = {'schema_version': 1, 'policy_version': '1', 'generation': 1, 'projects': {
            'example': {'classification': 'unmigrated_git', 'backend': 'git_local', 'route_phase': 'steady', 'locator': None}}}
        self.save()
        self.registry = routing.Registry(self.root)
        self.event = {'project_id': 'example', 'event_id': 'issue-42-event-100', 'action': 'patrol', 'owner_verified': True, 'ready_verified': False}

    def tearDown(self):
        self.tmp.cleanup()

    def save(self):
        (self.root / routing.REGISTRY).write_text(json.dumps(self.data))

    def prepare(self):
        return guard.prepare(self.registry, self.event)

    def test_duplicate_and_action_identity(self):
        prepared = self.prepare()
        self.assertTrue(guard.claim(self.registry, prepared)['launch'])
        self.assertFalse(guard.claim(self.registry, prepared)['launch'])
        self.event.update(action='execute', ready_verified=True)
        self.assertTrue(guard.claim(self.registry, self.prepare())['launch'])
        self.event['event_id'] = 'issue-42-event-101'
        self.assertTrue(guard.claim(self.registry, self.prepare())['launch'])

    def test_crash_unknown_and_completed_never_retry(self):
        prepared = self.prepare()
        receipt = guard.claim(self.registry, prepared)
        self.assertEqual(guard.claim(self.registry, prepared)['state'], 'pending')
        guard.finish(self.registry, receipt['key'], 'unknown')
        self.assertFalse(guard.claim(self.registry, prepared)['launch'])
        with self.assertRaises(RecordError):
            guard.finish(self.registry, receipt['key'], 'complete')
        self.event['event_id'] = 'next-event'
        prepared = self.prepare()
        receipt = guard.claim(self.registry, prepared)
        guard.finish(self.registry, receipt['key'], 'complete')
        self.assertEqual(guard.claim(self.registry, prepared)['state'], 'complete')

    def test_concurrent_claim_only_one_launch(self):
        # Initialize schema before concurrent connection startup.
        guard.database(self.registry).close()
        prepared = self.prepare()
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: guard.claim(self.registry, prepared), range(5)))
        self.assertEqual(sum(x['launch'] for x in results), 1)

    def test_owner_and_ready_gate(self):
        self.event['owner_verified'] = False
        with self.assertRaisesRegex(RecordError, 'FORBIDDEN'): self.prepare()
        self.event.update(owner_verified=True, action='execute')
        with self.assertRaisesRegex(RecordError, 'FORBIDDEN'): self.prepare()

    def test_google_unknown_missing_and_frozen_fail_closed(self):
        self.data['projects']['example'].update(classification='managed', backend='google_head', locator={'document_id': 'head'})
        self.save()
        with self.assertRaisesRegex(RecordError, 'CONNECTOR_UNAVAILABLE'): self.prepare()
        with self.assertRaisesRegex(RecordError, 'CONNECTOR_UNAVAILABLE'):
            guard.prepare(self.registry, self.event, {'authority_state': {'mode': 'google', 'phase': 'steady'},
                                                      'tool_completed': True})
        self.event['project_id'] = 'missing'
        with self.assertRaisesRegex(RecordError, 'NOT_REGISTERED'): self.prepare()
        (self.root / routing.REGISTRY).unlink()
        with self.assertRaises(RecordError): self.prepare()

    def test_recheck_route_and_control_before_claim(self):
        prepared = self.prepare()
        self.data['generation'] += 1
        self.save()
        with self.assertRaisesRegex(RecordError, 'REVISION_CONFLICT'): guard.claim(self.registry, prepared)
        self.data['projects']['example']['route_phase'] = 'frozen'
        self.save()
        with self.assertRaisesRegex(RecordError, 'PHASE_FROZEN'): self.prepare()

    def test_existing_stale_git_control_refuses_launch(self):
        prepared = self.prepare()
        runtime = pathlib.Path(prepared['binding']['runtime_directory'])
        runtime.mkdir()
        (runtime / 'control.json').write_text(json.dumps({'mode': 'google', 'phase': 'steady', 'epoch': 2, 'version': 0, 'policy_version': '1'}))
        with self.assertRaisesRegex(RecordError, 'STALE_MODE'):
            guard.claim(self.registry, prepared)

    def test_malformed_request_and_changed_payload(self):
        self.event['event_id'] = '../escape'
        with self.assertRaises(RecordError): self.prepare()
        self.event['event_id'] = 'event'
        prepared = self.prepare()
        tampered = copy.deepcopy(prepared)
        tampered['binding']['runtime_directory'] = 'untrusted'
        with self.assertRaisesRegex(RecordError, 'REVISION_CONFLICT'): guard.claim(self.registry, tampered)

    def test_preexisting_database_symlink_refused(self):
        path = self.root / routing.RUNTIME / 'dispatch-ledger.sqlite3'
        target = self.root / 'unrelated'
        target.write_text('keep')
        try:
            path.symlink_to(target)
        except OSError as error:
            if getattr(error, 'winerror', None) == 1314:
                self.skipTest('Windows symlink privilege unavailable')
            raise
        with self.assertRaisesRegex(RecordError, 'FORBIDDEN'): guard.claim(self.registry, self.prepare())
        self.assertEqual(target.read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
