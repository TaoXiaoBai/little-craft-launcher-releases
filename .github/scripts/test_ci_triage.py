import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import ci_triage as triage


REPO = 'owner/builder'
RUN = 101
SHA = 'b' * 40
SOURCE = 'a' * 40


def summary(commit=SOURCE, *, path='qt-native/tests/native_ui_tests.cpp'):
    return dict(schemaVersion=1, kind='LCL_CI_SAFE_DIAGNOSTIC', runId=RUN, runAttempt=2,
        workflowCommit=SHA, sourceCommit=commit, platform='windows-ui-companion', status='failed',
        categories=['TEST_FAILURE'], failedGroups=['native_ui'],
        locations=[dict(fileId=triage.safe.path_id(path), line=2, column=None, category='TEST_FAILURE')],
        truncated=False)


def archive(data, name='summary.json', *, mode=None, second=False):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED) as zipped:
        info = zipfile.ZipInfo(name)
        if mode is not None:
            info.external_attr = mode << 16
        zipped.writestr(info, json.dumps(data).encode() if not isinstance(data, bytes) else data)
        if second:
            zipped.writestr('extra.txt', b'not allowed')
    return out.getvalue()


class FakeGitHub:
    def __init__(self, value=None):
        self.summary = value or summary()
        self.blob = archive(self.summary)
        self.repo = dict(id=10, full_name=REPO, default_branch='main', private=False)
        self.run = dict(id=RUN, run_attempt=2, event='workflow_dispatch', path='.github/workflows/prerelease.yml',
            head_branch='main', head_sha=SHA, status='completed', repository=dict(id=10, full_name=REPO),
            head_repository=dict(id=10, full_name=REPO))
        self.jobs = [dict(name='Test windows-x64 companion UI', run_id=RUN, run_attempt=2, head_sha=SHA,
            started_at='2026-01-01T00:00:00Z', completed_at='2026-01-01T00:10:00Z')]
        self.artifacts = [dict(id=20, name='safe-diagnostics-windows-ui-companion-attempt-2', expired=False,
            size_in_bytes=len(self.blob), digest='sha256:' + hashlib.sha256(self.blob).hexdigest(),
            created_at='2026-01-01T00:05:00Z', workflow_run=dict(id=RUN, head_sha=SHA,
                repository_id=10, head_repository_id=10, head_branch='main'))]
        self.calls = []
        self.final_run = None
        self.run_reads = 0

    def json(self, route, *, pages=False):
        self.calls.append((route, pages))
        if route == 'repos/' + REPO:
            return copy.deepcopy(self.repo)
        if route == f'repos/{REPO}/actions/runs/{RUN}':
            self.run_reads += 1
            return copy.deepcopy(self.final_run if self.final_run is not None and self.run_reads > 1 else self.run)
        if '/attempts/' in route and route.endswith('/jobs?per_page=100'):
            return [dict(jobs=copy.deepcopy(self.jobs))]
        if route.endswith('/artifacts?per_page=100'):
            return [dict(artifacts=copy.deepcopy(self.artifacts))]
        if route.endswith('/attempts/1'):
            value = copy.deepcopy(self.run)
            value['run_attempt'] = 1
            return value
        raise AssertionError(route)

    def artifact(self, repository, artifact_id):
        self.calls.append(('artifact', repository, artifact_id))
        if repository != REPO or artifact_id != 20:
            raise AssertionError('unexpected artifact request')
        return self.blob


class TriageTests(unittest.TestCase):
    def test_old_gh_pagination_accepts_concatenated_objects(self):
        with patch.object(triage, 'read_command', return_value=b'{"jobs":[]}\n {"jobs":[1]}') as command:
            self.assertEqual(triage.GitHub().json('repos/owner/builder/jobs', pages=True),
                             [{'jobs': []}, {'jobs': [1]}])
            self.assertIn('--paginate', command.call_args.args[0])
            self.assertNotIn('--slurp', command.call_args.args[0])

    def test_json_rejects_duplicates_nonfinite_and_oversize(self):
        for data in (b'{"a":1,"a":2}', b'{"a":NaN}', b'not-json', b'\xff'):
            with self.subTest(data=data), self.assertRaises(triage.TriageError):
                triage.json_pages(data)
        with self.assertRaises(triage.TriageError):
            triage.json_pages(b'{}', maximum=1)

    def test_bounded_command_suppresses_stderr_and_bounds_stdout(self):
        with self.assertRaisesRegex(triage.TriageError, 'READ_LIMIT_EXCEEDED'):
            triage.read_command([sys.executable, '-c', 'print("x" * 200000)'], 128)
        with self.assertRaisesRegex(triage.TriageError, 'READ_FAILED') as failure:
            triage.read_command([sys.executable, '-c', 'import sys; print("PRIVATE_MARKER", file=sys.stderr); sys.exit(1)'], 128)
        self.assertNotIn('PRIVATE_MARKER', str(failure.exception))
        self.assertEqual(triage.read_command([sys.executable, '-c', 'print("ok")'], 128), b'ok\n')

    def test_bounded_command_times_out(self):
        with self.assertRaisesRegex(triage.TriageError, 'READ_TIMEOUT'):
            triage.read_command([sys.executable, '-c', 'import time; time.sleep(5)'], 128, timeout=.05)

    def test_singleton_regular_zip_required(self):
        self.assertEqual(triage.summary_from_zip(archive(summary())), summary())
        cases = [archive(summary(), name='../summary.json'), archive(summary(), second=True),
                 archive(summary(), mode=0o120777), archive(b'x' * (triage.safe.MAX_SUMMARY + 1)),
                 archive(b'{"schemaVersion":1,"schemaVersion":1}'), b'notzip']
        for blob in cases:
            with self.subTest(length=len(blob)), self.assertRaises(triage.TriageError):
                triage.summary_from_zip(blob)

    def test_remote_validates_and_reads_only_matching_summary(self):
        github = FakeGitHub()
        self.assertEqual(triage.fetch_summaries(REPO, RUN, github=github), [summary()])
        self.assertTrue(any(call[0] == 'artifact' for call in github.calls))

    def test_remote_rejects_fork_or_alternate_workflow(self):
        for change in ('fork', 'branch', 'workflow', 'event'):
            github = FakeGitHub()
            if change == 'fork': github.run['head_repository']['id'] = 11
            if change == 'branch': github.run['head_branch'] = 'pull-request'
            if change == 'workflow': github.run['path'] = '.github/workflows/other.yml'
            if change == 'event': github.run['event'] = 'pull_request'
            with self.subTest(change=change), self.assertRaises(triage.TriageError):
                triage.fetch_summaries(REPO, RUN, github=github)

    def test_malformed_repository_metadata_is_rejected(self):
        for value in (None, True, ['owner/builder'], {'name': REPO}):
            github = FakeGitHub()
            github.repo['full_name'] = value
            with self.subTest(value=value), self.assertRaises(triage.TriageError):
                triage.fetch_summaries(REPO, RUN, github=github)

    def test_remote_rejects_expired_ambiguous_missing_and_wrong_origin(self):
        for change in ('expired', 'duplicate', 'missing', 'repo', 'sha', 'time', 'job', 'size'):
            github = FakeGitHub()
            if change == 'expired': github.artifacts[0]['expired'] = True
            if change == 'duplicate': github.artifacts.append(copy.deepcopy(github.artifacts[0]))
            if change == 'missing': github.artifacts = []
            if change == 'repo': github.artifacts[0]['workflow_run']['repository_id'] = 11
            if change == 'sha': github.artifacts[0]['workflow_run']['head_sha'] = 'c' * 40
            if change == 'time': github.artifacts[0]['created_at'] = '2025-01-01T00:00:00Z'
            if change == 'job': github.jobs[0]['run_attempt'] = 1
            if change == 'size': github.artifacts[0]['size_in_bytes'] = triage.MAX_ARCHIVE + 1
            with self.subTest(change=change), self.assertRaises(triage.TriageError):
                triage.fetch_summaries(REPO, RUN, github=github)

    def test_remote_rejects_digest_and_summary_metadata_mismatch(self):
        for change in ('digest', 'run', 'attempt', 'platform', 'workflowCommit'):
            github = FakeGitHub()
            if change == 'digest': github.blob += b'changed'
            else:
                github.summary[{'run':'runId', 'attempt':'runAttempt'}.get(change, change)] = {
                    'run': RUN + 1, 'attempt': 1, 'platform': 'windows-x64', 'workflowCommit': 'c' * 40}[change]
                github.blob = archive(github.summary)
                github.artifacts[0].update(size_in_bytes=len(github.blob), digest='sha256:' + hashlib.sha256(github.blob).hexdigest())
            with self.subTest(change=change), self.assertRaises(triage.TriageError):
                triage.fetch_summaries(REPO, RUN, github=github)

    def test_current_attempt_changing_during_transfer_is_rejected(self):
        github = FakeGitHub()
        github.final_run = copy.deepcopy(github.run)
        github.final_run['run_attempt'] = 3
        with self.assertRaisesRegex(triage.TriageError, 'RUN_CHANGED_DURING_READ'):
            triage.fetch_summaries(REPO, RUN, github=github)

    def test_explicit_historical_attempt_is_supported(self):
        github = FakeGitHub()
        github.summary['runAttempt'] = 1
        github.blob = archive(github.summary)
        github.jobs[0]['run_attempt'] = 1
        github.artifacts[0].update(name='safe-diagnostics-windows-ui-companion-attempt-1',
            size_in_bytes=len(github.blob), digest='sha256:' + hashlib.sha256(github.blob).hexdigest())
        self.assertEqual(triage.fetch_summaries(REPO, RUN, attempt=1, github=github), [github.summary])

    def test_unsafe_repository_or_identifier_never_reaches_api(self):
        github = FakeGitHub()
        for repo, run in (('owner/../private', RUN), ('owner/..', RUN), (REPO, True), (REPO, -1)):
            with self.subTest(repo=repo, run=run), self.assertRaises(triage.TriageError):
                triage.fetch_summaries(repo, run, github=github)
        self.assertEqual(github.calls, [])

    def test_actions_output_requires_private_repository_and_handles_403(self):
        github = FakeGitHub()
        github.repo['private'] = True
        triage.permit_private_output({'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':REPO}, github)
        github.repo['private'] = False
        with self.assertRaises(triage.TriageError):
            triage.permit_private_output({'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':REPO}, github)
        with patch.object(github, 'json', side_effect=triage.TriageError('API_403_PRIVATE_MARKER')):
            with self.assertRaisesRegex(triage.TriageError, '^PRIVATE_OUTPUT_REQUIRES_PRIVATE_ACTIONS_REPOSITORY$'):
                triage.permit_private_output({'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':REPO}, github)
        triage.permit_private_output({}, github)  # Local agent has no remote guard dependency.


class SourceResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.relative = 'qt-native/tests/native_ui_tests.cpp'
        path = self.root / self.relative
        path.parent.mkdir(parents=True)
        path.write_text('line one\nPRIVATE_SOURCE_MARKER\nline three\n')
        self.git('init', '-q')
        self.git('config', 'user.email', 'fixture@example.test')
        self.git('config', 'user.name', 'Owned test fixture')
        self.git('add', '.')
        self.git('commit', '-qm', 'owned fixture')
        self.commit = self.git('rev-parse', 'HEAD').strip()

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *args):
        return subprocess.check_output(['git','-C',str(self.root),*args], text=True)

    def test_resolves_recorded_git_blob_without_changing_dirty_checkout(self):
        (self.root / self.relative).write_text('dirty current work\n')
        before = self.git('status','--porcelain')
        result = triage.resolve_summary(summary(self.commit), self.root)
        self.assertTrue(result['sourceAvailable'])
        self.assertTrue(result['checkoutMatchesSource'])
        self.assertEqual(result['locations'][0]['path'], self.relative)
        self.assertEqual(result['locations'][0]['context'][1], {'line':2,'text':'PRIVATE_SOURCE_MARKER'})
        self.assertEqual(self.git('status','--porcelain'), before)
        self.assertEqual((self.root / self.relative).read_text(), 'dirty current work\n')

    def test_different_head_uses_old_blob_and_gives_worktree_instruction(self):
        (self.root / self.relative).write_text('changed\nnew content\n')
        self.git('add','.')
        self.git('commit','-qm','later')
        result = triage.resolve_summary(summary(self.commit), self.root)
        self.assertFalse(result['checkoutMatchesSource'])
        self.assertEqual(result['locations'][0]['context'][1]['text'], 'PRIVATE_SOURCE_MARKER')
        self.assertIn('worktree add --detach', result['instructions'][0])

    def test_missing_commit_returns_fetch_instructions_without_mutation(self):
        result = triage.resolve_summary(summary(), self.root)
        self.assertFalse(result['sourceAvailable'])
        self.assertIn('fetch origin ' + SOURCE, result['instructions'][0])
        self.assertIn('worktree add --detach', result['instructions'][1])

    def test_missing_source_checkout_in_summary_is_explicit(self):
        data = summary(None)
        data['locations'] = []
        result = triage.resolve_summary(data, self.root)
        self.assertFalse(result['sourceAvailable'])
        self.assertIn('No source checkout', result['instructions'][0])

    def test_untracked_hash_is_not_guessed_and_invalid_line_is_rejected(self):
        data = summary(self.commit)
        data['locations'][0]['fileId'] = 'c' * 64
        result = triage.resolve_summary(data, self.root)
        self.assertFalse(result['locations'][0]['resolved'])
        data = summary(self.commit)
        data['locations'][0]['line'] = 100
        with self.assertRaisesRegex(triage.TriageError, 'SOURCE_LINE_OUT_OF_RANGE'):
            triage.resolve_summary(data, self.root)

    def test_offline_mode_is_not_claimed_remote_authenticated(self):
        path = self.root / 'summary.json'
        path.write_text(json.dumps(summary(self.commit)))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = triage.main(['--summary-file',str(path),'--source-root',str(self.root)], environment={})
        self.assertEqual(result, 0)
        value = json.loads(output.getvalue())
        self.assertFalse(value['provenanceVerified'])
        self.assertEqual(value['notice'], 'OFFLINE_SUMMARY_NOT_REMOTE_AUTHENTICATED')
        self.assertIn('PRIVATE_SOURCE_MARKER', output.getvalue())

    def test_safe_only_never_reads_source_and_is_allowed_in_public_actions(self):
        path = self.root / 'summary.json'
        path.write_text(json.dumps(summary(self.commit)))
        output = io.StringIO()
        with patch.object(triage, 'resolve_summary', side_effect=AssertionError('must not resolve')):
            with contextlib.redirect_stdout(output):
                result = triage.main(['--summary-file',str(path),'--safe-only'],
                    environment={'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':REPO})
        self.assertEqual(result, 0)
        self.assertNotIn('PRIVATE_SOURCE_MARKER', output.getvalue())
        self.assertNotIn(self.relative, output.getvalue())

    def test_public_actions_refuses_source_output_without_reflecting_paths(self):
        path = self.root / 'summary.json'
        path.write_text(json.dumps(summary(self.commit)))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = triage.main(['--summary-file',str(path),'--source-root',str(self.root)],
                github=FakeGitHub(), environment={'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':REPO})
        self.assertEqual(result, 1)
        self.assertIn('PRIVATE_OUTPUT_REQUIRES_PRIVATE_ACTIONS_REPOSITORY', output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())
        self.assertNotIn('PRIVATE_SOURCE_MARKER', output.getvalue())

    def test_private_actions_coding_agent_can_resolve_source(self):
        path = self.root / 'summary.json'
        path.write_text(json.dumps(summary(self.commit)))
        github = FakeGitHub()
        github.repo['private'] = True
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = triage.main(['--summary-file',str(path),'--source-root',str(self.root)],
                github=github, environment={'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':REPO})
        self.assertEqual(result, 0)
        self.assertIn('PRIVATE_SOURCE_MARKER', output.getvalue())


if __name__ == '__main__':
    unittest.main()
