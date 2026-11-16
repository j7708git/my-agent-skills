"""固定 registry 的合成路由測試，不讀 live routes、不呼叫 Google。"""
import copy
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / 'skills/workspace-records/scripts'
sys.path.insert(0, str(SCRIPTS))
import core
import git_store
import routing


class TestRouting(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.project = self.root / 'project-notes/projects/Legacy_Project'
        self.project.mkdir(parents=True)
        (self.root / routing.RUNTIME).mkdir(parents=True)
        self.registry_path = self.root / routing.REGISTRY
        self.policy_path = self.root / routing.POLICY
        self.policy_path.write_text('{}', encoding='utf-8')
        self.data = {'schema_version': 1, 'policy_version': '1', 'generation': 1,
                     'projects': {'Legacy_Project': {'classification': 'unmigrated_git',
                         'backend': 'git_local', 'route_phase': 'steady', 'locator': None}}}
        self.save()
        self.registry = routing.Registry(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        self.registry_path.write_bytes(core.canonical(self.data))

    def google(self):
        self.data['projects']['Legacy_Project'].update(
            classification='managed', backend='google_head', locator={'document_id': 'synthetic-head'})
        self.save()

    def test_positive_unmigrated_git_offline_and_fixed_runtime(self):
        binding = self.registry.bind('Legacy_Project')
        self.assertEqual(self.registry.check(binding)['state'], 'unmigrated_git')
        self.assertFalse(self.registry.check(binding)['google_required'])
        self.assertEqual(binding['authorization'], 'location_only')
        self.assertFalse(pathlib.Path(binding['runtime_directory']).exists())
        expected = git_store.runtime_dir(self.root / routing.RUNTIME, self.project, 'Legacy_Project')
        self.assertEqual(pathlib.Path(binding['runtime_directory']), expected)
        # 已出現 local runtime 就須核 control，不能用 unmigrated 標記繞 frozen。
        with self.assertRaises(core.RecordError): self.registry.check(binding)
        git_store.write_json(expected/'control.json', {'mode':'git','phase':'frozen','epoch':2,'version':1,'policy_version':'1'})
        with self.assertRaisesRegex(core.RecordError, 'PHASE_FROZEN'): self.registry.check(binding)

    def test_google_unavailable_never_falls_back_to_git(self):
        self.google()
        binding = self.registry.bind('Legacy_Project')
        with self.assertRaisesRegex(core.RecordError, 'CONTROL_UNAVAILABLE'):
            self.registry.check(binding)
        self.assertEqual(binding['backend'], 'google_head')
        self.assertFalse(pathlib.Path(binding['runtime_directory']).exists())

    def test_google_snapshot_identity_phase_and_control(self):
        self.google(); binding = self.registry.bind('Legacy_Project')
        state = {'document_id': 'synthetic-head', 'revision_id': 'opaque-revision',
                 'control': {'mode': 'google', 'phase': 'steady', 'epoch': 2, 'version': 5, 'policy_version': '1'}}
        self.assertEqual(self.registry.check(binding, state)['control']['version'], 5)
        for key, value, expected in [('document_id', 'other-head', 'FORBIDDEN'),
                                     ('revision_id', '', 'CONTROL_UNAVAILABLE')]:
            altered = copy.deepcopy(state); altered[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(core.RecordError, expected):
                self.registry.check(binding, altered)
        state['control']['phase'] = 'frozen'
        with self.assertRaisesRegex(core.RecordError, 'PHASE_FROZEN'):
            self.registry.check(binding, state)

    def test_digest_change_and_per_request_path_tampering(self):
        binding = self.registry.bind('Legacy_Project')
        forged = copy.deepcopy(binding); forged['runtime_directory'] = str(self.root / 'elsewhere')
        with self.assertRaisesRegex(core.RecordError, 'REVISION_CONFLICT'):
            self.registry.check(forged)
        self.data['generation'] += 1; self.save()
        with self.assertRaisesRegex(core.RecordError, 'REVISION_CONFLICT'):
            self.registry.check(binding)
        self.data['projects']['Legacy_Project']['route_phase'] = 'restoring'; self.save()
        with self.assertRaisesRegex(core.RecordError, 'PHASE_FROZEN'):
            self.registry.check(binding)

    def test_missing_corrupt_unknown_and_duplicate_registry_rejected(self):
        with self.assertRaisesRegex(core.RecordError, 'NOT_REGISTERED'):
            self.registry.bind('Unknown')
        for raw in (b'not-json', b'{"schema_version":1,"schema_version":1}'):
            self.registry_path.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(core.RecordError):
                self.registry.bind('Legacy_Project')
        self.registry_path.unlink()
        with self.assertRaises(core.RecordError):
            self.registry.bind('Legacy_Project')

    def test_case_alias_and_unmigrated_google_registry_rejected(self):
        with self.assertRaisesRegex(core.RecordError, 'NOT_REGISTERED'):
            self.registry.bind('legacy_project')
        self.data['projects']['legacy_project'] = copy.deepcopy(self.data['projects']['Legacy_Project']); self.save()
        with self.assertRaisesRegex(core.RecordError, 'INTEGRITY_FAILED'):
            self.registry.bind('Legacy_Project')
        del self.data['projects']['legacy_project']
        self.data['projects']['Legacy_Project'].update(backend='google_head', locator={'document_id':'synthetic-head'}); self.save()
        with self.assertRaisesRegex(core.RecordError, 'INTEGRITY_FAILED'):
            self.registry.bind('Legacy_Project')

    def test_managed_git_requires_real_local_control(self):
        self.data['projects']['Legacy_Project']['classification'] = 'managed'; self.save()
        binding = self.registry.bind('Legacy_Project')
        with self.assertRaises(core.RecordError): self.registry.check(binding)
        runtime = git_store.runtime_dir(self.root / routing.RUNTIME, self.project, 'Legacy_Project')
        git_store.write_json(runtime / 'control.json', {'mode':'git','phase':'steady','epoch':3,'version':4,'policy_version':'1'})
        self.assertEqual(self.registry.check(binding)['control']['epoch'], 3)

    def test_transition_prepare_only_does_not_accept_stop_assertions(self):
        request = {'actor_role':'pm', 'project_id':'Legacy_Project',
                   'target_manifest':{'project_id':'Legacy_Project','backend':'google_head','locator':{'document_id':'synthetic-head'}},
                   'snapshot_manifest_sha256':'a'*64, 'writer_manifest_sha256':'b'*64}
        before = self.registry_path.read_bytes()
        proposal = self.registry.prepare_transition(request)
        self.assertFalse(proposal['activation_supported']); self.assertFalse(proposal['proofs_verified'])
        self.assertEqual(proposal['required_stages'], ['authority_freeze', 'route_freeze', 'drain', 'snapshot_and_target_readback', 'publish_route_frozen', 'resume_target', 'publish_route_steady'])
        self.assertEqual(self.registry_path.read_bytes(), before)
        request['writers_stopped'] = True
        with self.assertRaises(core.RecordError): self.registry.prepare_transition(request)
        self.assertEqual(self.registry_path.read_bytes(), before)

    def test_cli_fixed_locator_ascii_and_path_override_rejected(self):
        copied = self.root / '.openchamber/workspace-records/package/skills/workspace-records/scripts'; copied.mkdir(parents=True)
        for name in ('routing.py','core.py','git_store.py','privacy.py'):
            shutil.copyfile(SCRIPTS/name, copied/name)
        def call(request):
            return subprocess.run([sys.executable, '-B', str(copied/'routing.py'), 'bind'],
                                  input=json.dumps(request), text=True, encoding='utf-8', capture_output=True, timeout=10, cwd=self.root.parent)
        result = call({'project_id':'Legacy_Project'})
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertTrue(result.stdout.isascii())
        self.assertEqual(json.loads(result.stdout)['data']['project_directory'],str(self.project))
        rejected = call({'project_id':'Legacy_Project','runtime_dir':str(self.root/'override')})
        self.assertNotEqual(rejected.returncode, 0)
        self.assertFalse(json.loads(rejected.stdout)['ok'])
        arbitrary = self.root / 'source/skills/workspace-records/scripts'
        shutil.copytree(copied, arbitrary)
        outside = subprocess.run([sys.executable, '-B', str(arbitrary/'routing.py'), 'bind'],
                                 input=json.dumps({'project_id':'Legacy_Project'}), text=True,
                                 encoding='utf-8', capture_output=True, timeout=10, cwd=self.root.parent)
        self.assertNotEqual(outside.returncode, 0)
        self.assertEqual(json.loads(outside.stdout)['error']['code'], 'CONTROL_UNAVAILABLE')


if __name__ == '__main__':
    unittest.main()
