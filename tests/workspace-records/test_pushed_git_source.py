"""Real local Git blob checks, plus synthetic trusted workflow context."""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'skills/workspace-records/scripts'))
import pushed_git_source as pushed
import routing
from core import RecordError
from test_core import STATUS


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.notes = self.root / 'project-notes'
        self.project = self.notes / 'projects/Demo'
        self.project.mkdir(parents=True)
        (self.project / 'status.md').write_bytes(STATUS)
        (self.root / routing.RUNTIME).mkdir(parents=True)
        (self.root / routing.POLICY).write_text('{}')
        (self.root / routing.REGISTRY).write_text(json.dumps({'schema_version': 1, 'policy_version': '1', 'generation': 1,
            'projects': {'Demo': {'classification': 'unmigrated_git', 'backend': 'git_local', 'route_phase': 'steady', 'locator': None}}}))
        self.command('init', '-b', 'main')
        self.command('config', '--local', 'user.name', 'Synthetic')
        self.command('config', '--local', 'user.email', 'synthetic@example.invalid')
        self.command('add', 'projects/Demo/status.md')
        self.command('commit', '-m', 'synthetic baseline')
        self.commit = self.command('rev-parse', 'HEAD').strip()
        self.config = {'notes_root': str(self.notes), 'authority': 'git_local', 'scope': 'all_projects', 'generation': 1}
        self.observed = patch.object(pushed, 'observe_pushed_checkpoint', side_effect=lambda *args: {
            'source_commit': self.command('rev-parse', 'HEAD').strip(), 'repository': 'example-owner/notes', 'default_branch': 'main'})
        self.observed.start()
        self.addCleanup(self.observed.stop)

    def command(self, *arguments):
        value = subprocess.run(['git', '-C', str(self.notes), *arguments], capture_output=True, text=True)
        self.assertEqual(value.returncode, 0, value.stderr)
        return value.stdout

    def provider(self, commit=None):
        return pushed.PushedGitSource(self.config, {'source_commit': commit or self.commit, 'event_sha': self.commit, 'stale_wake': False, 'repository': 'example-owner/notes'})

    def test_real_pushed_blobs_and_untracked_research(self):
        source = self.provider()
        first = source()
        (self.project / 'research').mkdir()
        (self.project / 'research/unrelated.md').write_text('untracked research')
        self.assertEqual(source()['source_digest'], first['source_digest'])
        self.assertEqual(source.last_proof['sourceCommit'], self.commit)
        self.assertEqual(source.last_proof['source_file_count'], 1)

    def test_dirty_and_staged_status_are_not_pushed_source(self):
        (self.project / 'status.md').write_bytes(STATUS.replace(b'First', b'Second'))
        for staged in (False, True):
            if staged:
                self.command('add', 'projects/Demo/status.md')
            with self.assertRaisesRegex(RecordError, 'PUSHED_SOURCE_DIRTY'):
                self.provider()()

    def test_untracked_preferred_status_and_missing_tracked_source(self):
        self.command('mv', 'projects/Demo/status.md', 'projects/Demo/progress.md')
        self.command('commit', '-m', 'synthetic legacy')
        legacy = self.command('rev-parse', 'HEAD').strip()
        self.provider(legacy)()
        (self.project / 'status.md').write_bytes(STATUS)
        with self.assertRaisesRegex(RecordError, 'PUSHED_SOURCE_SET_MISMATCH'):
            self.provider(legacy)()
        (self.project / 'status.md').unlink()
        (self.project / 'progress.md').unlink()
        with self.assertRaisesRegex(RecordError, 'PUSHED_SOURCE_SET_MISMATCH'):
            self.provider(legacy)()

    def test_research_only_pushed_commit_does_not_change_source_digest(self):
        before = self.provider()()
        (self.project / 'research').mkdir()
        (self.project / 'research/note.md').write_text('tracked research')
        self.command('add', 'projects/Demo/research/note.md')
        self.command('commit', '-m', 'synthetic research')
        latest = self.command('rev-parse', 'HEAD').strip()
        self.assertNotEqual(latest, self.commit)
        self.assertEqual(self.provider(latest)()['source_digest'], before['source_digest'])

    def test_mixed_verifier_never_reads_archived_google_status(self):
        source = self.provider()()
        (self.project / 'status.md').write_bytes(STATUS.replace(b'First', b'Dirty archive'))
        proof = pushed.verify_pushed_rows(self.notes, self.commit, source['rows'], git_project_ids=set())
        self.assertEqual(proof['source_file_count'], 0)

    def test_source_project_universe_must_match_pushed_tree(self):
        (self.notes / 'projects/Unregistered').mkdir()
        with self.assertRaises(RecordError): self.provider()()

    def test_trusted_push_context_and_stale_wake(self):
        event = {'repository': 'example-owner/notes', 'owner': 'example-owner', 'actor': 'example-owner',
                 'event_sha': 'a' * 40, 'ref': 'refs/heads/main', 'run_id': '12'}
        def fake(notes, *arguments):
            if arguments[:3] == ('remote', 'get-url', 'origin'): return b'git@github.com:example-owner/notes.git\n'
            if arguments[0] == 'ls-remote': return ('ref: refs/heads/main\tHEAD\n' + self.commit + '\tHEAD\n').encode()
            if arguments[:2] == ('branch', '--show-current'): return b'main\n'
            if arguments[:2] == ('rev-parse', 'HEAD'): return (self.commit + '\n').encode()
            raise AssertionError(arguments)
        with patch.object(pushed, 'git', side_effect=fake):
            checkpoint = pushed.prepare_push(self.notes, 'example-owner/notes', event)
            self.assertTrue(checkpoint['stale_wake'])
            self.assertEqual(checkpoint['source_commit'], self.commit)
            for key, value in (('actor', 'other'), ('repository', 'other/repo'), ('ref', 'refs/heads/other')):
                with self.assertRaises(RecordError): pushed.prepare_push(self.notes, 'example-owner/notes', {**event, key: value})

    def test_remote_credentials_or_wrong_host_are_not_accepted(self):
        self.assertEqual(pushed.remote_repository('https://github.com/example-owner/notes.git'), 'example-owner/notes')
        for value in ('https://user@example.invalid/a/b.git', 'https://user@github.com/a/b.git', 'file:///tmp/notes'):
            with self.assertRaises(RecordError): pushed.remote_repository(value)

    def test_same_sha_fork_cannot_replace_bound_event_repository(self):
        self.observed.stop()
        def switched(notes, *arguments):
            if arguments[:3] == ('remote', 'get-url', 'origin'): return b'git@github.com:other-owner/fork.git\n'
            if arguments[0] == 'ls-remote': return ('ref: refs/heads/main\tHEAD\n' + self.commit + '\tHEAD\n').encode()
            raise AssertionError(arguments)
        with patch.object(pushed, 'git', side_effect=switched):
            with self.assertRaisesRegex(RecordError, 'FORBIDDEN'): self.provider()()


if __name__ == '__main__':
    unittest.main()
