"""Verify pushed Git checkpoint inputs without trusting working-tree HEAD alone.

Workflow identity is checked by the existing Actions adapter; these facts are
trusted adapter inputs, not authentication supplied by an Issue. Source commit
provenance is separate from the data digest so research-only pushes need no AI.
"""
import os
import re
import subprocess
from urllib.parse import urlsplit

import git_store
import portfolio_refresh
from core import RecordError, ident, object_hash, sha


def git(notes, *arguments):
    result = subprocess.run(['git', '-C', str(notes), *arguments], capture_output=True, timeout=30)
    if result.returncode:
        raise RecordError('GIT_CHECKPOINT_UNAVAILABLE')
    return result.stdout


def remote_repository(value):
    value = value.strip()
    if value.startswith('git@github.com:'):
        path = value[len('git@github.com:'):]
    else:
        parsed = urlsplit(value)
        if parsed.scheme != 'https' or parsed.hostname != 'github.com' or parsed.username or parsed.password:
            raise RecordError('FORBIDDEN')
        path = parsed.path.lstrip('/')
    path = path[:-4] if path.endswith('.git') else path
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', path):
        raise RecordError('FORBIDDEN')
    return path


def remote_head(notes):
    text = git(notes, 'ls-remote', '--symref', 'origin', 'HEAD').decode('utf-8')
    refs, hashes = [], []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[0] == 'ref:' and fields[2] == 'HEAD':
            refs.append(fields[1])
        elif len(fields) == 2 and fields[1] == 'HEAD':
            hashes.append(fields[0])
    if len(refs) != 1 or len(hashes) != 1 or not refs[0].startswith('refs/heads/') or not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', hashes[0]):
        raise RecordError('GIT_CHECKPOINT_UNAVAILABLE')
    return refs[0][len('refs/heads/'):], hashes[0]


def observe_pushed_checkpoint(notes_root, expected_repository=None):
    notes = git_store.safe_dir(notes_root)
    repository = remote_repository(git(notes, 'remote', 'get-url', 'origin').decode('utf-8'))
    if expected_repository is not None and repository.casefold() != expected_repository.casefold():
        raise RecordError('FORBIDDEN')
    branch, pushed = remote_head(notes)
    if (git(notes, 'branch', '--show-current').decode().strip() != branch
            or git(notes, 'rev-parse', 'HEAD').decode().strip() != pushed):
        raise RecordError('REVISION_CONFLICT')
    latest_branch, latest = remote_head(notes)
    if latest_branch != branch or latest != pushed:
        raise RecordError('REVISION_CONFLICT')
    return {'source_commit': pushed, 'repository': repository, 'default_branch': branch}


def prepare_push(notes_root, expected_repository, event, fast_forward=False):
    notes = git_store.safe_dir(notes_root)
    keys = {'repository', 'owner', 'actor', 'event_sha', 'ref', 'run_id'}
    if not isinstance(event, dict) or set(event) != keys or not all(isinstance(value, str) for value in event.values()):
        raise RecordError('INVALID_ARGUMENT')
    if (event['repository'].casefold() != expected_repository.casefold()
            or event['owner'].casefold() != expected_repository.split('/')[0].casefold()
            or event['actor'].casefold() != event['owner'].casefold()
            or not re.fullmatch(r'[1-9][0-9]*', event['run_id'])
            or not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', event['event_sha'])):
        raise RecordError('FORBIDDEN')
    actual_repository = remote_repository(git(notes, 'remote', 'get-url', 'origin').decode('utf-8'))
    if actual_repository.casefold() != expected_repository.casefold():
        raise RecordError('FORBIDDEN')
    branch, pushed = remote_head(notes)
    if event['ref'] != 'refs/heads/' + branch or git(notes, 'branch', '--show-current').decode().strip() != branch:
        raise RecordError('FORBIDDEN')
    current = git(notes, 'rev-parse', 'HEAD').decode().strip()
    if current != pushed:
        if not fast_forward:
            raise RecordError('REVISION_CONFLICT')
        # Git itself preserves unrelated dirty paths and refuses overwrites.
        git(notes, 'fetch', '--no-tags', 'origin', 'refs/heads/' + branch)
        git(notes, 'merge', '--ff-only', '--no-squash', '--no-edit', 'FETCH_HEAD')
        current = git(notes, 'rev-parse', 'HEAD').decode().strip()
    latest_branch, latest = remote_head(notes)
    if current != pushed or latest != pushed or latest_branch != branch:
        raise RecordError('REVISION_CONFLICT')
    return {'source_commit': pushed, 'repository': expected_repository, 'default_branch': branch,
            'event_sha': event['event_sha'], 'run_id': event['run_id'], 'stale_wake': event['event_sha'] != pushed}


def verify_pushed_rows(notes_root, commit, rows, git_project_ids=None, expected_repository=None):
    """Verify Git rows in an all-project snapshot; mixed callers pass fresh Git IDs.

    git_project_ids is local registry-derived routing, never connector/caller
    authority evidence. Google rows are not read from archived Git files.
    """
    notes = git_store.safe_dir(notes_root)
    if not isinstance(commit, str) or not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', commit):
        raise RecordError('INVALID_ARGUMENT')
    checkpoint = observe_pushed_checkpoint(notes, expected_repository)
    if checkpoint['source_commit'] != commit:
        raise RecordError('REVISION_CONFLICT')
    incoming = {row['project_id']: row for row in rows}
    if len(incoming) != len(rows):
        raise RecordError('PUSHED_SOURCE_SET_MISMATCH')
    ids = set(incoming) if git_project_ids is None else set(git_project_ids)
    if not ids <= set(incoming):
        raise RecordError('PUSHED_SOURCE_SET_MISMATCH')
    tracked = git(notes, 'ls-tree', '-r', '-z', '--name-only', commit, '--', 'projects').decode('utf-8').split('\0')
    projects = {path.split('/')[1] for path in tracked if path and len(path.split('/')) >= 3}
    if set(incoming) != projects:
        raise RecordError('PUSHED_SOURCE_SET_MISMATCH')
    digests = {}
    for project_id in sorted(ids):
        ident(project_id)
        row = incoming[project_id]
        project = git_store.project_dir(notes, project_id)
        expected = next((name for name in ('status.md', 'progress.md') if 'projects/' + project_id + '/' + name in tracked), None)
        actual = next((name for name in ('status.md', 'progress.md') if os.path.lexists(project / name)), None)
        if expected != actual:
            raise RecordError('PUSHED_SOURCE_SET_MISMATCH')
        if expected is None:
            if row.get('source') is not None:
                raise RecordError('PUSHED_SOURCE_UNVERIFIED')
            continue
        raw = git(notes, 'cat-file', 'blob', commit + ':projects/' + project_id + '/' + expected)
        path = 'projects/' + project_id + '/' + expected
        attrs = git(notes, 'check-attr', '-z', 'filter', 'ident', 'working-tree-encoding', '--', path).decode().split('\0')
        if any(attrs[index] not in ('unspecified', 'unset') for index in range(2, len(attrs) - 1, 3)):
            raise RecordError('FORBIDDEN')
        # Only Git's built-in EOL checkout representation is permitted; external
        # filters, ident expansion and encoding transforms were rejected above.
        checkout = git(notes, 'cat-file', '--filters', commit + ':' + path)
        digest = row.get('source', {}).get('sha256') if isinstance(row.get('source'), dict) else None
        if digest not in (sha(raw), sha(checkout)):
            raise RecordError('PUSHED_SOURCE_DIRTY')
        digests[path] = {'blob_sha256': sha(raw), 'checkout_sha256': sha(checkout), 'observed_sha256': digest}
    for path, digests_for_path in digests.items():
        relative = path.split('/')
        project = git_store.project_dir(notes, relative[1])
        if sha(git_store.target_path(project, relative[2]).read_bytes()) != digests_for_path['observed_sha256']:
            raise RecordError('PUSHED_SOURCE_DIRTY')
    if observe_pushed_checkpoint(notes, checkpoint['repository']) != checkpoint:
        raise RecordError('REVISION_CONFLICT')
    return {'sourceCommit': commit, 'source_file_count': len(digests), 'source_files_sha256': object_hash(digests)}


class PushedGitSource:
    """Callable Outbox source provider; every capture checks exact pushed blobs."""
    def __init__(self, source_config, checkpoint):
        self.config, self.checkpoint = dict(source_config), dict(checkpoint)
        self.notes = git_store.safe_dir(self.config['notes_root'])
        self.commit = checkpoint['source_commit']
        if not isinstance(self.commit, str) or not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', self.commit):
            raise RecordError('INVALID_ARGUMENT')
        self.last_proof = None

    def __call__(self):
        source = portfolio_refresh.collect_git(self.config)
        proof = verify_pushed_rows(self.notes, self.commit, source['rows'], expected_repository=self.checkpoint['repository'])
        self.last_proof = {**proof, 'source_digest': source['source_digest'],
                           'event_sha': self.checkpoint['event_sha'], 'stale_wake': self.checkpoint.get('stale_wake', False)}
        return source
