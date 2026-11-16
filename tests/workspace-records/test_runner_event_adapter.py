"""Synthetic verified-source adapter tests; no GitHub or Apps calls."""
import copy
import json
import pathlib
import sys
import unittest
from unittest.mock import patch

SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / 'skills/workspace-records/scripts'
sys.path.insert(0, str(SCRIPTS))
import core
import dispatch_guard
import runner_event_adapter as adapter
import routing
import test_canonical_writer as canonical_fixture


class GitHub:
    def __init__(self, values):
        self.values, self.calls = values, []
    def get(self, suffix):
        self.calls.append(suffix)
        return copy.deepcopy(self.values[suffix])


class Tests(unittest.TestCase):
    def setUp(self):
        self.fixture = canonical_fixture.Tests('test_managed_roundtrip_uses_real_protocol_graph_and_exact_cas')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.repo, self.owner = 'example/notes', 'example'
        title, notes = 'synthetic title', 'synthetic notes'
        self.marker = {'schema_version': 1, 'project_id': 'Demo', 'mapping_generation': 1,
                       'source': 'google_tasks', 'intake_key': 'a' * 64,
                       'payload_sha256': core.sha(adapter.compact({'title': title, 'notes': notes}).encode())}
        body = notes + '\n\n---\n```workspace-intake-v2\n' + adapter.compact(self.marker) + '\n```'
        self.issue = {'id': 77, 'number': 42, 'title': title, 'body': body, 'state': 'open',
                      'html_url': 'https://github.com/' + self.repo + '/issues/42', 'labels': [{'name': 'ready'}]}
        self.config = {'repository': self.repo, 'owner': self.owner, 'workflow_path': '.github/workflows/idea-kanban-trigger.yml',
            'whitelist': {'a' * 64: {'project_id': 'Demo', 'mapping_generation': 1, 'issue_number': 42,
                                    'payload_sha256': self.marker['payload_sha256']}},
            'projects': {'Demo': {'google': self.fixture.reader.config}}, 'reviewed_operations': {}}
        self.review_request = copy.deepcopy(self.fixture.request)
        self.context = {'actions': 'true', 'repository': self.repo, 'repository_owner': self.owner,
                        'actor': self.owner, 'event_name': 'issues', 'run_id': '100', 'run_attempt': '1', 'sha': 'b' * 40}
        self.payload = {'repository': {'full_name': self.repo}, 'sender': {'login': self.owner}, 'action': 'labeled',
                        'issue': {'id': 77, 'number': 42}, 'label': {'name': 'ready', 'id': 9}}
        run = {'id': 100, 'run_attempt': 1, 'repository': {'full_name': self.repo}, 'actor': {'login': self.owner},
               'event': 'issues', 'head_sha': 'b' * 40, 'path': self.config['workflow_path']}
        self.github = GitHub({'actions/runs/100': run, 'issues/42': self.issue})
        self.review()
        self.sessions = 0
        fixture = self.fixture
        owner = self
        class Session:
            def run(self, callback):
                owner.sessions += 1
                return {'outcome': 'driver_completed', 'driver_result': callback(fixture.broker),
                        'model_turns': 1, 'tool_receipts': []}
        self.factory = Session

    def review(self):
        import git_store
        directory = self.fixture.root / '.openchamber/workspace-records/runner-operations'
        directory.mkdir(exist_ok=True)
        plan = {'schema_version': 1, 'review_id': 'pm-reviewed-1', 'project_id': 'Demo', 'issue_number': 42,
                'issue_content_sha256': core.object_hash({'title': self.issue['title'], 'body': self.issue['body']}),
                'request': copy.deepcopy(self.review_request)}
        digest = core.object_hash(plan)
        git_store.write_json(directory / (digest + '.json'), plan)
        self.config['reviewed_operations']['42'] = {'project_id': 'Demo', 'plan_sha256': digest}

    def event(self):
        return adapter.trusted_event(self.config, self.context, self.payload, self.github)

    def test_actual_managed_protocol_after_verified_event_and_duplicate_no_session(self):
        event = self.event()
        result = adapter.dispatch(self.fixture.registry, self.config, event, self.factory)
        self.assertEqual(result['outcome'], 'canonical_confirmed')
        self.assertEqual(self.sessions, 1)
        self.assertEqual(adapter.dispatch(self.fixture.registry, self.config, self.event(), self.factory)['outcome'], 'duplicate_event')
        self.assertEqual(self.sessions, 1)
        receipt = self.fixture.root / '.openchamber/workspace-records/runner-receipts' / (result['claim_key'] + '.json')
        stored = json.loads(receipt.read_text())
        self.assertEqual(stored['writer_result']['outcome'], 'canonical_confirmed')
        self.assertEqual(dispatch_guard.peek(self.fixture.registry, event['event'])['state'], 'complete')

    def test_git_managed_writer_reads_actual_files_and_never_constructs_apps_session(self):
        import git_store
        from test_git_store import clean_scan
        data, _ = self.fixture.registry.load()
        data['projects']['Demo'].update(backend='git_local', locator=None)
        git_store.write_json(self.fixture.root / routing.REGISTRY, data)
        binding = self.fixture.registry.bind('Demo')
        runtime = pathlib.Path(binding['runtime_directory'])
        git_store.write_json(runtime / 'control.json', {'mode': 'git', 'phase': 'steady', 'epoch': 1, 'version': 0, 'policy_version': '1', 'record_metadata': 'required-v1'})
        metadata = runtime / 'record-metadata'
        metadata.mkdir()
        git_store.write_json(metadata / 'records.json', {'schema_version': 1, 'project_id': 'Demo',
            'records': [copy.deepcopy(self.fixture.manifest['records'][0]['record'])], 'attachments': []})
        target = pathlib.Path(binding['project_directory']) / 'tasks/synthetic.md'
        target.parent.mkdir()
        target.write_bytes(self.fixture.blobs['task-blob'])
        self.review_request['expected'] = {'mode': 'git', 'phase': 'steady', 'epoch': 1, 'version': 0}
        self.review()
        with patch.object(git_store, 'privacy_check', side_effect=clean_scan):
            result = adapter.dispatch(self.fixture.registry, self.config, self.event(), self.factory)
        self.assertEqual(result['outcome'], 'canonical_confirmed')
        self.assertEqual(result['model_turns'], 0)
        self.assertEqual(self.sessions, 0)
        self.assertEqual(target.read_text(), self.review_request['payload']['content'])
        catalog = json.loads((metadata / 'records.json').read_text())
        self.assertEqual(catalog['records'][0]['metadata']['decision'], 'preserve')
        self.assertEqual(catalog['records'][0]['source']['content_sha256'], core.sha(target.read_bytes()))

    def test_legacy_is_only_explicit_fresh_git_and_never_google_fallback(self):
        import git_store
        data, _ = self.fixture.registry.load()
        data['projects']['Other'] = {'classification': 'unmigrated_git', 'backend': 'git_local', 'route_phase': 'steady', 'locator': None}
        (self.fixture.root / 'project-notes/projects/Other').mkdir()
        git_store.write_json(self.fixture.root / routing.REGISTRY, data)
        self.github.values['issues/42']['body'] = 'ordinary original issue'
        self.config['legacy_git_issues'] = {'42': 'Other'}
        self.config['reviewed_operations'] = {}
        result = adapter.trusted_event(self.config, self.context, self.payload, self.github, self.fixture.registry)
        self.assertEqual(result['dispatch_route'], 'legacy_git')
        with self.assertRaisesRegex(core.RecordError, 'LEGACY_SCOPE_SEPARATE'):
            adapter.dispatch(self.fixture.registry, self.config, result, self.factory)
        data['projects']['Other'].update(classification='managed', backend='google_head', locator={'document_id': 'other-doc'})
        git_store.write_json(self.fixture.root / routing.REGISTRY, data)
        with self.assertRaisesRegex(core.RecordError, 'LEGACY_GOOGLE_FORBIDDEN'):
            adapter.trusted_event(self.config, self.context, self.payload, self.github, self.fixture.registry)
        self.config['legacy_git_issues'] = {}
        with self.assertRaisesRegex(core.RecordError, 'PROJECT_MAPPING_UNVERIFIED'):
            adapter.trusted_event(self.config, self.context, self.payload, self.github, self.fixture.registry)
        self.assertEqual(self.sessions, 0)

    def test_pm_review_required_and_issue_content_binding(self):
        self.config['reviewed_operations'] = {}
        with self.assertRaisesRegex(core.RecordError, 'PM_REVIEW_REQUIRED'):
            adapter.dispatch(self.fixture.registry, self.config, self.event(), self.factory)
        self.review()
        context = self.event()
        context['issue_content_sha256'] = 'c' * 64
        with self.assertRaisesRegex(core.RecordError, 'PM_REVIEW_OUTDATED'):
            adapter.dispatch(self.fixture.registry, self.config, context, self.factory)
        self.assertEqual(self.sessions, 0)

    def test_same_event_cannot_swap_pm_operation_bundle_after_claim(self):
        event = self.event()
        adapter.dispatch(self.fixture.registry, self.config, event, self.factory)
        self.review_request['payload']['content'] = 'different reviewed operation'
        self.review_request['operation_id'] = 'another-op'
        self.review()
        with self.assertRaisesRegex(core.RecordError, 'ID_REUSE'):
            adapter.dispatch(self.fixture.registry, self.config, event, self.factory)
        self.assertEqual(self.sessions, 1)

    def test_native_issue_can_use_explicit_pm_bundle_without_google_tasks_marker(self):
        self.issue['body'] = 'ordinary PM-reviewed issue'
        self.github.values['issues/42']['body'] = self.issue['body']
        self.review()
        event = self.event()
        self.assertEqual(event['dispatch_route'], 'pilot')
        result = adapter.dispatch(self.fixture.registry, self.config, event, self.factory)
        self.assertEqual(result['outcome'], 'canonical_confirmed')
        self.assertEqual(result['projection']['state'], 'pending')
        self.assertEqual(dispatch_guard.peek(self.fixture.registry, event['event'])['state'], 'complete')

    def test_projection_failure_never_downgrades_canonical_receipt(self):
        with patch.object(adapter, 'project_canonical_receipt', return_value={'state': 'pending', 'code': 'GOOGLE_UNAVAILABLE'}):
            event = self.event()
            result = adapter.dispatch(self.fixture.registry, self.config, event, self.factory)
        self.assertEqual(result['outcome'], 'canonical_confirmed')
        self.assertEqual(result['projection']['state'], 'pending')
        self.assertEqual(dispatch_guard.peek(self.fixture.registry, event['event'])['state'], 'complete')

    def test_projection_recovery_never_reclaims_or_rewrites_canonical(self):
        event = self.event()
        completed = adapter.dispatch(self.fixture.registry, self.config, event, self.factory)
        before = dispatch_guard.peek(self.fixture.registry, event['event'])
        with patch.object(adapter.canonical_writer.CanonicalWriter, 'execute', side_effect=AssertionError('canonical must not repeat')), \
                patch.object(adapter.guard, 'claim', side_effect=AssertionError('claim must not repeat')), \
                patch.object(adapter, 'project_details', return_value={'outcome': 'detail_projection_confirmed'}), \
                patch.object(adapter, 'project_canonical_receipt', return_value={'state': 'confirmed'}):
            recovered = adapter.recover_projections(self.fixture.registry, self.config, completed['claim_key'], self.factory)
        self.assertEqual(recovered['outcome'], 'projection_recovery')
        self.assertEqual(recovered['detail_projection']['outcome'], 'detail_projection_confirmed')
        self.assertEqual(dispatch_guard.peek(self.fixture.registry, event['event']), before)

    def test_concurrent_projection_recovery_refused_before_model(self):
        completed = adapter.dispatch(self.fixture.registry, self.config, self.event(), self.factory)
        before_sessions = self.sessions
        with adapter.git_store.locked(adapter.projection_lock_directory(self.fixture.registry, completed['claim_key'])):
            with self.assertRaisesRegex(core.RecordError, 'IN_FLIGHT'):
                adapter.recover_projections(self.fixture.registry, self.config, completed['claim_key'], self.factory)
        self.assertEqual(self.sessions, before_sessions)

    def test_pm_packager_fetches_current_issue_scans_and_never_grants_ready(self):
        import git_store
        directory = self.fixture.root / '.openchamber/workspace-records/runner-operations'
        git_store.write_json(directory / 'reviewed-request.json', self.review_request)
        before = copy.deepcopy(self.config['reviewed_operations'])
        result = adapter.prepare_review_bundle(self.fixture.registry, self.config, '42', 'reviewed-request.json', 'pm-review-2', self.github)
        self.assertEqual(result['outcome'], 'pm_bundle_prepared')
        self.assertFalse(result['user_ready_granted'])
        self.assertEqual(self.github.calls[-1], 'issues/42')
        self.assertEqual(self.config['reviewed_operations'], before)
        stored = json.loads((directory / (result['plan_sha256'] + '.json')).read_text())
        self.assertEqual(stored['issue_content_sha256'], core.object_hash({'title': self.issue['title'], 'body': self.issue['body']}))
        with self.assertRaises(core.RecordError):
            adapter.prepare_review_bundle(self.fixture.registry, self.config, '42', '../outside.json', 'pm-review-3', self.github)

    def test_zero_exit_or_model_claim_cannot_finish(self):
        class FakeExitZero:
            def run(self, callback):
                return {'outcome': 'driver_completed', 'driver_result': {'exit_code': 0, 'outcome': 'complete'}, 'model_turns': 1}
        with self.assertRaisesRegex(core.RecordError, 'READBACK_UNVERIFIED'):
            adapter.dispatch(self.fixture.registry, self.config, self.event(), FakeExitZero)
        self.assertIsNone(dispatch_guard.peek(self.fixture.registry, self.event()['event']))

    def test_forged_canonical_final_text_without_durable_claim_cannot_finish(self):
        class FakeCanonical:
            def run(self, callback):
                return {'outcome': 'driver_completed', 'driver_result': {'outcome': 'canonical_confirmed',
                    'claim_key': 'a' * 64, 'writer_receipt_sha256': 'b' * 64}, 'model_turns': 1}
        with self.assertRaisesRegex(core.RecordError, 'READBACK_UNVERIFIED'):
            adapter.dispatch(self.fixture.registry, self.config, self.event(), FakeCanonical)

    def test_unknown_canonical_side_effect_never_reclaims_or_restarts(self):
        event = self.event()
        self.fixture.broker.failure = 'cas_after'
        with self.assertRaises(core.RecordError):
            adapter.dispatch(self.fixture.registry, self.config, event, self.factory)
        self.assertEqual(dispatch_guard.peek(self.fixture.registry, event['event'])['state'], 'unknown')
        repeated = adapter.dispatch(self.fixture.registry, self.config, event, self.factory)
        self.assertEqual(repeated['claim']['state'], 'unknown')
        self.assertEqual(self.sessions, 1)
        self.assertEqual(sum(tool == 'batch_update_document' for tool, _, _ in self.fixture.broker.calls), 1)

    def test_owner_and_fresh_run_context_required(self):
        for mutation in ('actor', 'actions', 'sha'):
            context = dict(self.context)
            context[mutation] = 'untrusted'
            with self.subTest(mutation=mutation), self.assertRaises(core.RecordError):
                adapter.trusted_event(self.config, context, self.payload, self.github)

    def test_ready_must_be_fresh_or_explicit_owner_run_comment(self):
        self.github.values['issues/42']['labels'] = []
        with self.assertRaisesRegex(core.RecordError, 'HUMAN_READY_REQUIRED'):
            self.event()
        self.context['event_name'] = 'issue_comment'
        self.github.values['actions/runs/100']['event'] = 'issue_comment'
        self.payload.update(action='created', comment={'id': 123, 'body': '/run'})
        self.github.values['issues/comments/123'] = {'id': 123, 'body': '/run', 'user': {'login': self.owner}, 'issue_url': 'https://api.github.com/repos/' + self.repo + '/issues/42'}
        self.assertTrue(self.event()['event']['ready_verified'])
        self.github.values['issues/comments/123']['user']['login'] = 'other'
        with self.assertRaises(core.RecordError):
            self.event()

    def test_full_or_missing_manual_issue_is_rejected(self):
        self.context['event_name'] = 'workflow_dispatch'
        self.github.values['actions/runs/100']['event'] = 'workflow_dispatch'
        for inputs in ({'mode': 'full', 'issue_number': '42'}, {'mode': 'execute', 'issue_number': ''}):
            self.payload['inputs'] = inputs
            with self.assertRaisesRegex(core.RecordError, 'EXACT_ISSUE_REQUIRED'):
                self.event()

    def test_exact_mapping_and_payload_hash_not_free_text(self):
        self.config['whitelist']['a' * 64]['project_id'] = 'Other'
        with self.assertRaisesRegex(core.RecordError, 'PROJECT_MAPPING_UNVERIFIED'):
            self.event()
        self.config['whitelist']['a' * 64]['project_id'] = 'Demo'
        self.github.values['issues/42']['body'] = 'edited\n' + self.issue['body']
        with self.assertRaisesRegex(core.RecordError, 'INTAKE_PAYLOAD_CHANGED'):
            self.event()

    def test_duplicate_markers_and_duplicate_json_keys_refused(self):
        issue = copy.deepcopy(self.issue)
        issue['body'] += '\n```workspace-intake-v2\n{}\n```'
        with self.assertRaises(core.RecordError):
            adapter.marker_from_issue(issue)
        issue['body'] = self.issue['body'].replace('"schema_version":1', '"schema_version":1,"schema_version":1')
        with self.assertRaises(core.RecordError):
            adapter.marker_from_issue(issue)

    def test_patrol_is_read_only_and_action_claim_distinct(self):
        self.payload['action'] = 'opened'
        self.issue['labels'] = []
        self.github.values['issues/42']['labels'] = []
        event = self.event()
        result = adapter.dispatch(self.fixture.registry, self.config, event, self.factory)
        self.assertEqual(result['outcome'], 'patrol_read_confirmed')
        self.assertFalse(any(kind == 'write' for _, _, kind in self.fixture.broker.calls))
        self.assertEqual(dispatch_guard.peek(self.fixture.registry, event['event'])['state'], 'complete')
        self.payload['action'] = 'labeled'
        self.github.values['issues/42']['labels'] = [{'name': 'ready'}]
        self.assertNotEqual(self.event()['event']['event_id'], event['event']['event_id'])

    def test_frozen_registry_or_activation_barrier_prevents_model_start(self):
        data, _ = self.fixture.registry.load()
        data['projects']['Demo']['route_phase'] = 'frozen'
        from git_store import write_json
        write_json(self.fixture.root / routing.REGISTRY, data)
        with self.assertRaises(core.RecordError):
            adapter.dispatch(self.fixture.registry, self.config, self.event(), self.factory)
        self.assertEqual(self.sessions, 0)


if __name__ == '__main__':
    unittest.main()
