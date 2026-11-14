import copy
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
import warnings
import zipfile

import ci_test_proof as P
import release_provenance as R
from release_support import runtime_names


REPOSITORY = 'fixture/builder'
TAG = 'v1.0.0-pre.13'
PLAN = {
    'schemaVersion': 1,
    'primaryUi': ['ui_regression', 'winmd_ui', 'populated_ui', 'theme_controls'],
    'companionUi': ['local_ui', 'native_ui', 'mod_favorites_ui',
                    'crash_notification_ui', 'external_auth_ui'],
}


def combined_proof(platform, schema=2, run_id=None, attempt=1, workflow=None):
    run_id = run_id or (101 if platform == 'windows-x64' else 201)
    workflow = workflow or ('3' if platform == 'windows-x64' else '4') * 40
    plan = dict(PLAN)
    if schema == 2:
        plan.update(schemaVersion=2, extraBackendGroups=['telemetry'])
    full = P.UI | P.BACKEND
    if schema == 2:
        full = full | {'telemetry'}
    if platform == 'linux-x86_64':
        full = full | {'linux_auth_vault'}
    parts = P.partition(plan, full)
    common = dict(schemaVersion=schema, kind='LCL_CTEST_SHARD_PROOF',
                  repositorySha256=P.digest(REPOSITORY), workflowCommit=workflow,
                  runId=run_id, runAttempt=attempt, platform=platform,
                  sourceCommit='1' * 40, sourceTree='2' * 40,
                  sourceInputsSha256=('5' if platform == 'windows-x64' else '6') * 64,
                  planSha256=P.digest(plan), registrationSha256='7' * 64,
                  sdkSha256='8' * 64, compilerSha256='9' * 64, configSha256='a' * 64,
                  fullGroupIds=P.ids(full), primaryGroupIds=P.ids(parts['primary']),
                  companionGroupIds=P.ids(parts['companion']), fullGroupCount=len(full),
                  fullSuite=False, failed=0, skipped=0)
    shards = [dict(common, shard=role, selectedPassedGroupIds=P.ids(parts[role]),
                   selectedGroupCount=len(parts[role])) for role in ('primary', 'companion')]
    result = P.combine(*shards, REPOSITORY, workflow, run_id, attempt)
    result.update(companionArtifactId=50 + run_id, companionJobId=60 + run_id)
    return result


def environment(proof):
    return dict(GITHUB_REPOSITORY=REPOSITORY, GITHUB_SHA=proof['workflowCommit'],
                GITHUB_RUN_ID=str(proof['runId']),
                GITHUB_RUN_ATTEMPT=str(proof['runAttempt']))


def receipt_zip(receipt, platform, *, name=None, mode=stat.S_IFREG | 0o644,
                extra=False, duplicate=False, raw=None):
    name = name or ('windows.json' if platform == 'windows-x64' else 'linux.json')
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo(name)
        info.external_attr = mode << 16
        archive.writestr(info, raw if raw is not None else P.canonical(receipt))
        if extra:
            archive.writestr('unreviewed.json', b'{}')
        if duplicate:
            with warnings.catch_warnings():
                warnings.filterwarnings('ignore', message='Duplicate name:', category=UserWarning)
                archive.writestr(name, P.canonical(receipt))
    return stream.getvalue()


class FakeGitHub:
    """Independent Actions records; all runtime and receipt bytes are local."""
    def __init__(self):
        self.repository = dict(id=7, full_name=REPOSITORY, default_branch='main')
        self.runs = {}
        self.jobs = {}
        self.artifacts = {}
        self.blobs = {}
        self.receipts = {}
        self.calls = []
        self.rerun_after_artifact_for = None

    def add(self, receipt):
        receipt = copy.deepcopy(receipt)
        run_id = receipt['runId']
        platform = receipt['platform']
        attempt = receipt['runAttempt']
        sha = receipt['workflowCommit']
        self.receipts[platform] = receipt
        self.runs.setdefault(run_id, dict(
            id=run_id, run_attempt=attempt, workflow_id=900,
            repository=copy.deepcopy(self.repository), head_repository=copy.deepcopy(self.repository),
            head_sha=sha, head_branch='main', event='workflow_dispatch', path=P.WORKFLOW,
            display_title=f'Prerelease {TAG} (both)', status='completed', conclusion='success'))
        jobs = self.jobs.setdefault(run_id, [])
        for offset, name in enumerate((f'Build {platform}', P.PLATFORMS[platform][2])):
            jobs.append(dict(id=run_id * 10 + offset, run_id=run_id, run_attempt=attempt,
                             head_sha=sha, name=name, status='completed', conclusion='success',
                             started_at='2026-11-14T01:00:00Z', completed_at='2026-11-14T01:10:00Z'))
        artifact_id = run_id * 10 + (5 if platform == 'windows-x64' else 6)
        blob = receipt_zip(receipt, platform)
        self.blobs[artifact_id] = blob
        self.artifacts.setdefault(run_id, []).append(dict(
            id=artifact_id, name=f'provenance-{platform}-attempt-{attempt}', expired=False,
            size_in_bytes=len(blob), digest='sha256:' + hashlib.sha256(blob).hexdigest(),
            created_at='2026-11-14T01:09:00Z',
            workflow_run=dict(id=run_id, repository_id=7, head_repository_id=7,
                              head_sha=sha, head_branch='main')))

    def replace_receipt(self, platform, receipt):
        old = self.receipts[platform]
        run_id = old['runId']
        artifact = next(a for a in self.artifacts[run_id]
                        if a['name'].startswith(f'provenance-{platform}-'))
        blob = receipt_zip(receipt, platform)
        self.blobs[artifact['id']] = blob
        artifact['digest'] = 'sha256:' + hashlib.sha256(blob).hexdigest()
        artifact['size_in_bytes'] = len(blob)
        self.receipts[platform] = copy.deepcopy(receipt)

    def json(self, route, *, pages=False):
        self.calls.append(route)
        if route == f'repos/{REPOSITORY}':
            return copy.deepcopy(self.repository)
        if route == f'repos/{REPOSITORY}/actions/workflows/900':
            return {'path': P.WORKFLOW}
        prefix = f'repos/{REPOSITORY}/actions/runs/'
        if not route.startswith(prefix):
            raise AssertionError(f'Unexpected endpoint: {route}')
        suffix = route[len(prefix):]
        run_id = int(suffix.split('/')[0])
        if suffix == str(run_id):
            return copy.deepcopy(self.runs[run_id])
        if '/attempts/' in suffix and '/jobs?' in suffix:
            if not pages:
                raise AssertionError('Jobs must be paginated')
            requested_attempt = int(suffix.split('/attempts/')[1].split('/')[0])
            if requested_attempt != self.runs[run_id]['run_attempt']:
                return [{'jobs': []}]
            return [{'jobs': copy.deepcopy(self.jobs[run_id])}]
        if '/artifacts?' in suffix:
            if not pages:
                raise AssertionError('Artifacts must be paginated')
            return [{'artifacts': copy.deepcopy(self.artifacts[run_id])}]
        raise AssertionError(f'Unexpected endpoint: {route}')

    def artifact(self, repository, artifact_id):
        if repository != REPOSITORY:
            raise AssertionError('Unexpected repository')
        if self.rerun_after_artifact_for:
            run_id = self.rerun_after_artifact_for
            self.runs[run_id]['run_attempt'] += 1
            self.rerun_after_artifact_for = None
        return self.blobs[artifact_id]


class ReleaseProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.assets = self.root / 'assets'
        self.assets.mkdir()
        self.build_dir = self.root / 'build'
        generated = self.build_dir / 'generated'
        generated.mkdir(parents=True)
        for name in ('auth_build_config.h', 'application_build_config.h', 'telemetry_build_config.h'):
            (generated / name).write_bytes(b'#pragma once\n#define FIXTURE_PUBLIC_VALUE \"same-distribution\"\n')
        self.paths = dict(zip(('windows-x64', 'linux-x86_64'),
                              (self.assets / name for name in runtime_names(TAG))))
        self.proofs = {}
        self.receipts = {}
        self.gh = FakeGitHub()
        for platform, runtime in self.paths.items():
            runtime.write_bytes((platform + '-runtime-fixture').encode())
            proof = combined_proof(platform)
            self.proofs[platform] = proof
            receipt = self.create_receipt(proof, runtime, TAG, platform, environment(proof))
            self.receipts[platform] = receipt
            self.gh.add(receipt)

    def tearDown(self):
        self.temp.cleanup()

    def create_receipt(self, proof, runtime, tag, platform, env, purpose='release'):
        return R.create_receipt(proof, runtime, tag, platform, env,
                                build_dir=self.build_dir, purpose=purpose)

    def verify(self, *, verify_only=False):
        return R.verify_release(REPOSITORY, TAG, 101, 201, self.assets, self.gh,
                                verify_only=verify_only)

    def test_new_release_receipts_require_explicit_publishable_purpose(self):
        for receipt in self.receipts.values():
            self.assertEqual(receipt['schemaVersion'], 2)
            self.assertEqual(receipt['purpose'], 'release')
        self.verify()
        self.verify(verify_only=True)

    def test_test_receipts_can_be_fully_verified_but_never_published(self):
        for platform, receipt in self.receipts.items():
            self.gh.replace_receipt(platform, dict(receipt, purpose='test'))
        with self.assertRaisesRegex(P.ProofError, 'RELEASE_PURPOSE_NOT_PUBLISHABLE'):
            self.verify()
        self.assertEqual(self.verify(verify_only=True)['status'], 'PASS_SAME_SOURCE_RELEASE')

    def test_legacy_unmarked_receipts_are_verification_only(self):
        for platform, receipt in self.receipts.items():
            legacy = dict(receipt, schemaVersion=1)
            legacy.pop('purpose')
            R.validate_receipt(legacy)
            self.gh.replace_receipt(platform, legacy)
            with self.assertRaises(P.ProofError):
                R.validate_receipt(dict(legacy, purpose='release'))
        with self.assertRaisesRegex(P.ProofError, 'RELEASE_PURPOSE_NOT_PUBLISHABLE'):
            self.verify()
        self.assertEqual(self.verify(verify_only=True)['status'], 'PASS_SAME_SOURCE_RELEASE')

    def test_a_release_receipt_cannot_authorize_the_other_platform_test_package(self):
        self.gh.replace_receipt('linux-x86_64', dict(self.receipts['linux-x86_64'], purpose='test'))
        with self.assertRaisesRegex(P.ProofError, 'RELEASE_PURPOSE_NOT_PUBLISHABLE'):
            self.verify()
        self.verify(verify_only=True)

    def test_missing_or_invalid_purpose_cannot_create_or_validate_a_new_receipt(self):
        platform = 'windows-x64'
        proof = self.proofs[platform]
        for purpose in (None, '', 'preview', True, 1, ['release']):
            with self.subTest(purpose=purpose):
                with self.assertRaisesRegex(P.ProofError, 'INVALID_RELEASE_PURPOSE'):
                    self.create_receipt(proof, self.paths[platform], TAG, platform,
                                        environment(proof), purpose=purpose)
                with self.assertRaises(P.ProofError):
                    R.validate_receipt(dict(self.receipts[platform], purpose=purpose))
        missing = dict(self.receipts[platform]); missing.pop('purpose')
        with self.assertRaises(P.ProofError):
            R.validate_receipt(missing)
        with self.assertRaisesRegex(P.ProofError, 'INVALID_RELEASE_PURPOSE'):
            R.create_receipt(proof, self.paths[platform], TAG, platform,
                             environment(proof), build_dir=self.build_dir)

    def test_verify_only_keeps_source_configuration_runtime_and_attempt_checks(self):
        for key, code in (('sourceIdentitySha256', 'RELEASE_SOURCE_MISMATCH'),
                          ('distributionConfigSha256', 'RELEASE_CONFIGURATION_MISMATCH')):
            with self.subTest(key=key):
                github = copy.deepcopy(self.gh)
                for platform, receipt in self.receipts.items():
                    changed = dict(receipt, purpose='test')
                    if platform == 'linux-x86_64': changed[key] = 'b' * 64
                    github.replace_receipt(platform, changed)
                with self.assertRaisesRegex(P.ProofError, code):
                    R.verify_release(REPOSITORY, TAG, 101, 201, self.assets, github, verify_only=True)
        runtime = self.paths['windows-x64']
        original = runtime.read_bytes()
        runtime.write_bytes(original + b'-modified')
        try:
            with self.assertRaisesRegex(P.ProofError, 'RELEASE_RUNTIME_DIGEST_MISMATCH'):
                self.verify(verify_only=True)
        finally:
            runtime.write_bytes(original)
        for mutation in ('attempt', 'companion'):
            with self.subTest(mutation=mutation):
                github = copy.deepcopy(self.gh)
                if mutation == 'attempt': github.runs[101]['run_attempt'] += 1
                else: github.jobs[101][1]['conclusion'] = 'failure'
                with self.assertRaises(P.ProofError):
                    R.verify_release(REPOSITORY, TAG, 101, 201, self.assets, github, verify_only=True)

    def test_verify_only_cli_must_explicitly_reach_the_same_verifier(self):
        arguments = ['verify-runs', '--repository', REPOSITORY, '--tag', TAG,
                     '--windows-run-id', '101', '--linux-run-id', '201', '--assets', str(self.assets)]
        for verify_only in (False, True):
            with self.subTest(verify_only=verify_only), mock.patch.object(R, 'verify_release') as verify:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(R.main(arguments + (['--verify-only'] if verify_only else [])), 0)
                self.assertIs(verify.call_args.kwargs['verify_only'], verify_only)

    def test_same_source_cross_platform_keeps_private_identity_out_of_public_receipt(self):
        windows, linux = (self.receipts[p] for p in self.paths)
        self.assertNotEqual(self.proofs['windows-x64']['sourceInputsSha256'],
                            self.proofs['linux-x86_64']['sourceInputsSha256'])
        self.assertEqual(windows['sourceIdentitySha256'], linux['sourceIdentitySha256'])
        encoded = json.dumps(windows, sort_keys=True)
        self.assertNotIn('sourceCommit', encoded)
        self.assertNotIn('sourceTree', encoded)
        self.assertNotIn('sourceInputsSha256', encoded)
        self.assertNotIn('1' * 40, encoded)
        self.assertNotIn('2' * 40, encoded)
        self.assertNotIn(REPOSITORY, encoded)
        self.assertNotIn('native_ui', encoded)
        self.assertNotIn('telemetry', encoded)
        self.assertNotIn('FIXTURE_PUBLIC_VALUE', encoded)
        self.assertNotIn('same-distribution', encoded)
        verdict = self.verify()
        self.assertEqual(verdict['status'], 'PASS_SAME_SOURCE_RELEASE')
        self.assertEqual(verdict['sourceIdentitySha256'], windows['sourceIdentitySha256'])

    def test_each_receipt_binds_its_own_public_workflow_and_current_attempt(self):
        self.assertNotEqual(self.receipts['windows-x64']['workflowCommit'],
                            self.receipts['linux-x86_64']['workflowCommit'])
        self.verify()
        self.assertTrue(any('/attempts/1/jobs?' in c for c in self.gh.calls))

    def test_same_tag_at_different_source_revisions_cannot_be_published_together(self):
        changed = dict(self.proofs['linux-x86_64'], sourceCommit='b' * 40)
        receipt = self.create_receipt(changed, self.paths['linux-x86_64'], TAG,
                                   'linux-x86_64', environment(changed))
        self.gh.replace_receipt('linux-x86_64', receipt)
        with self.assertRaises(P.ProofError):
            self.verify()

    def test_distribution_configuration_is_required_and_ignores_checkout_line_endings(self):
        platform = 'linux-x86_64'
        proof = self.proofs[platform]
        with self.assertRaises(P.ProofError):
            R.create_receipt(proof, self.paths[platform], TAG, platform, environment(proof), purpose='release')
        baseline = self.receipts[platform]['distributionConfigSha256']
        generated = self.build_dir / 'generated'
        for newline in (b'\r\n', b'\r'):
            with self.subTest(newline=newline):
                for header in generated.iterdir():
                    original = header.read_bytes().replace(b'\r\n', b'\n').replace(b'\r', b'\n')
                    header.write_bytes(original.replace(b'\n', newline))
                receipt = self.create_receipt(proof, self.paths[platform], TAG,
                                              platform, environment(proof))
                self.assertEqual(receipt['distributionConfigSha256'], baseline)

    def test_same_source_with_different_endpoint_configuration_cannot_be_combined(self):
        header = self.build_dir / 'generated/telemetry_build_config.h'
        header.write_bytes(b'#pragma once\n#define MCL_TELEMETRY_ENDPOINT "https://other.example.test/events"\n')
        proof = self.proofs['linux-x86_64']
        receipt = self.create_receipt(proof, self.paths['linux-x86_64'], TAG,
                                      'linux-x86_64', environment(proof))
        self.assertEqual(receipt['sourceIdentitySha256'],
                         self.receipts['windows-x64']['sourceIdentitySha256'])
        self.assertNotEqual(receipt['distributionConfigSha256'],
                            self.receipts['windows-x64']['distributionConfigSha256'])
        self.gh.replace_receipt('linux-x86_64', receipt)
        with self.assertRaisesRegex(P.ProofError, 'CONFIGURATION'):
            self.verify()

    def test_missing_telemetry_header_is_legacy_only_but_auth_headers_are_always_required(self):
        generated = self.build_dir / 'generated'
        (generated / 'telemetry_build_config.h').unlink()
        platform = 'windows-x64'
        for schema in (1, 2):
            proof = combined_proof(platform, schema)
            if schema == 1:
                self.create_receipt(proof, self.paths[platform], TAG, platform, environment(proof))
            else:
                with self.assertRaises(P.ProofError):
                    self.create_receipt(proof, self.paths[platform], TAG, platform, environment(proof))
        legacy = combined_proof(platform, 1)
        for name in ('auth_build_config.h', 'application_build_config.h'):
            with self.subTest(name=name):
                header = generated / name
                original = header.read_bytes()
                header.unlink()
                with self.assertRaises(P.ProofError):
                    self.create_receipt(legacy, self.paths[platform], TAG, platform, environment(legacy))
                header.write_bytes(original)

    def test_different_source_tree_or_plan_is_not_the_same_source_identity(self):
        for key, value in (('sourceTree', 'b' * 40), ('planSha256', 'b' * 64)):
            with self.subTest(key=key):
                changed = dict(self.proofs['linux-x86_64'], **{key: value})
                receipt = self.create_receipt(changed, self.paths['linux-x86_64'], TAG,
                                           'linux-x86_64', environment(changed))
                self.assertNotEqual(receipt['sourceIdentitySha256'],
                                    self.receipts['windows-x64']['sourceIdentitySha256'])

    def test_schema1_and_schema2_retain_complete_platform_group_counts(self):
        for schema in (1, 2):
            for platform, baseline in (('windows-x64', 43), ('linux-x86_64', 44)):
                with self.subTest(schema=schema, platform=platform):
                    proof = combined_proof(platform, schema)
                    receipt = self.create_receipt(proof, self.paths[platform], TAG,
                                               platform, environment(proof))
                    self.assertEqual(receipt['ctestSchemaVersion'], schema)
                    self.assertEqual(receipt['passedGroupCount'], baseline + (schema == 2))
                    self.assertIs(receipt['fullSuite'], True)

    def test_missing_receipt_is_not_replaced_by_a_successful_job_or_matching_title(self):
        self.gh.artifacts[101] = []
        with self.assertRaises(P.ProofError):
            self.verify()

    def test_runtime_bytes_size_and_name_are_bound_to_the_receipt(self):
        path = self.paths['windows-x64']
        path.write_bytes(path.read_bytes() + b'-changed-after-testing')
        with self.assertRaises(P.ProofError):
            self.verify()
        with self.assertRaises(P.ProofError):
            self.create_receipt(self.proofs['windows-x64'], path, 'v1.0.0-pre.14',
                             'windows-x64', environment(self.proofs['windows-x64']))

    def test_incomplete_or_failed_combined_proof_cannot_create_a_receipt(self):
        original = self.proofs['windows-x64']
        variants = [dict(original, fullSuite=False), dict(original, passedGroupCount=43),
                    dict(original, status='FAILED'), dict(original, kind='LCL_CTEST_SHARD_PROOF'),
                    dict(original, fullGroupIds=original['fullGroupIds'][:-1])]
        for proof in variants:
            with self.subTest(keys=proof):
                with self.assertRaises(P.ProofError):
                    self.create_receipt(proof, self.paths['windows-x64'], TAG,
                                     'windows-x64', environment(original))

    def test_a_combined_proof_from_another_attempt_or_workflow_is_rejected_at_creation(self):
        proof = self.proofs['windows-x64']
        for key, value in (('GITHUB_RUN_ID', '999'), ('GITHUB_RUN_ATTEMPT', '2'),
                           ('GITHUB_SHA', 'b' * 40), ('GITHUB_REPOSITORY', 'other/builder')):
            with self.subTest(key=key):
                env = dict(environment(proof), **{key: value})
                with self.assertRaises(P.ProofError):
                    self.create_receipt(proof, self.paths['windows-x64'], TAG, 'windows-x64', env)

    def test_foreign_repository_branch_event_path_or_commit_is_rejected(self):
        original = copy.deepcopy(self.gh.runs[101])
        variants = [dict(original, head_repository={'id': 88, 'full_name': 'attacker/fork'}),
                    dict(original, repository={'id': 88, 'full_name': 'attacker/fork'}),
                    dict(original, head_branch='test-branch'), dict(original, event='pull_request'),
                    dict(original, path='.github/workflows/unreviewed.yml'),
                    dict(original, head_sha='b' * 40)]
        for run in variants:
            with self.subTest(run=run):
                self.gh.runs[101] = run
                with self.assertRaises(P.ProofError):
                    self.verify()
        self.gh.runs[101] = original

    def test_only_a_successful_current_platform_and_companion_job_authorize_the_package(self):
        original = copy.deepcopy(self.gh.jobs[101])
        for index in (0, 1):
            for conclusion in ('failure', 'cancelled', 'skipped'):
                with self.subTest(index=index, conclusion=conclusion):
                    self.gh.jobs[101] = copy.deepcopy(original)
                    self.gh.jobs[101][index]['conclusion'] = conclusion
                    with self.assertRaises(P.ProofError):
                        self.verify()
        self.gh.jobs[101] = original
        self.gh.runs[101]['conclusion'] = 'failure'  # Other platform may have failed.
        self.verify()

    def test_expired_duplicate_foreign_or_stale_artifacts_are_rejected(self):
        original = copy.deepcopy(self.gh.artifacts[101])
        mutations = [dict(original[0], expired=True),
                     dict(original[0], name='provenance-windows-x64-attempt-2'),
                     dict(original[0], size_in_bytes=1024 * 1024),
                     dict(original[0], workflow_run=dict(original[0]['workflow_run'], id=999)),
                     dict(original[0], created_at='2026-11-14T00:59:00Z'),
                     dict(original[0], created_at='2026-11-14T01:10:01Z')]
        for artifact in mutations:
            with self.subTest(artifact=artifact):
                self.gh.artifacts[101] = [artifact]
                with self.assertRaises(P.ProofError):
                    self.verify()
        self.gh.artifacts[101] = original * 2
        with self.assertRaises(P.ProofError):
            self.verify()

    def test_server_artifact_digest_and_actual_zip_bytes_must_match(self):
        self.gh.artifacts[101][0]['digest'] = 'sha256:' + '0' * 64
        with self.assertRaises(P.ProofError):
            self.verify()

    def test_rerunning_during_receipt_download_cannot_reuse_old_attempt(self):
        self.gh.rerun_after_artifact_for = 101
        with self.assertRaises(P.ProofError):
            self.verify()

    def test_normal_both_run_can_verify_before_the_publish_job_finishes(self):
        github = FakeGitHub()
        for platform in self.paths:
            proof = combined_proof(platform, run_id=101, workflow='3' * 40)
            receipt = self.create_receipt(proof, self.paths[platform], TAG,
                                       platform, environment(proof))
            github.add(receipt)
        github.runs[101].update(status='in_progress', conclusion=None)
        R.verify_release(REPOSITORY, TAG, 101, 101, self.assets, github)

    def test_receipt_archive_has_one_fixed_regular_entry_and_bounded_json(self):
        receipt = self.receipts['windows-x64']
        valid = receipt_zip(receipt, 'windows-x64')
        self.assertEqual(R.receipt_from_zip(valid, 'windows-x64'), receipt)
        bad = [receipt_zip(receipt, 'windows-x64', name='../windows.json'),
               receipt_zip(receipt, 'windows-x64', name='directory/windows.json'),
               receipt_zip(receipt, 'windows-x64', name='linux.json'),
               receipt_zip(receipt, 'windows-x64', mode=stat.S_IFLNK | 0o777),
               receipt_zip(receipt, 'windows-x64', extra=True),
               receipt_zip(receipt, 'windows-x64', duplicate=True),
               receipt_zip(receipt, 'windows-x64', raw=b' ' * (65 * 1024)),
               b'not-a-zip', valid + b'x' * (150 * 1024)]
        for blob in bad:
            with self.subTest(size=len(blob)):
                with self.assertRaises(P.ProofError):
                    R.receipt_from_zip(blob, 'windows-x64')

    def test_duplicate_json_keys_and_extra_private_fields_are_not_public_receipts(self):
        receipt = self.receipts['windows-x64']
        payload = b'{"schemaVersion":1,' + P.canonical(receipt)[1:]
        with self.assertRaises(P.ProofError):
            R.receipt_from_zip(receipt_zip(receipt, 'windows-x64', raw=payload), 'windows-x64')
        for key, value in (('sourceCommit', '1' * 40), ('sourceTree', '2' * 40),
                           ('logs', '/private/logs'), ('schemaVersion', True),
                           ('fullSuite', 1), ('passedGroupCount', True)):
            with self.subTest(key=key):
                with self.assertRaises(P.ProofError):
                    R.validate_receipt(dict(receipt, **{key: value}))

    def test_linked_runtime_is_rejected(self):
        runtime = self.paths['windows-x64']
        actual = runtime.with_suffix('.private-target')
        runtime.rename(actual)
        try:
            runtime.symlink_to(actual)
        except OSError as error:
            self.skipTest(f'Cannot create test symlink: {error}')
        with self.assertRaises(P.ProofError):
            self.create_receipt(self.proofs['windows-x64'], runtime, TAG,
                             'windows-x64', environment(self.proofs['windows-x64']))

    def test_hardlinked_runtime_is_rejected(self):
        runtime = self.paths['windows-x64']
        try:
            os.link(runtime, runtime.with_suffix('.hardlink'))
        except OSError as error:
            self.skipTest(f'Cannot create test hardlink: {error}')
        with self.assertRaises(P.ProofError):
            self.create_receipt(self.proofs['windows-x64'], runtime, TAG,
                             'windows-x64', environment(self.proofs['windows-x64']))

    def test_create_cli_writes_a_public_receipt_without_clobbering_or_printing_private_proof(self):
        proof = self.proofs['windows-x64']
        proof_path = self.root / 'private-combined-proof.json'
        proof_path.write_bytes(P.canonical(proof))
        output = self.root / 'public/windows.json'
        arguments = ['create', '--proof', str(proof_path), '--runtime', str(self.paths['windows-x64']),
                     '--tag', TAG, '--platform', 'windows-x64', '--build-dir', str(self.build_dir),
                     '--purpose', 'release', '--output', str(output)]
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, environment(proof)), contextlib.redirect_stdout(stdout):
            self.assertEqual(R.main(arguments), 0)
        published = output.read_bytes()
        self.assertEqual(json.loads(published), self.receipts['windows-x64'])
        self.assertNotIn(str(proof_path), stdout.getvalue())
        self.assertNotIn(proof['sourceCommit'], stdout.getvalue())
        self.assertNotIn(proof['sourceTree'], stdout.getvalue())
        with mock.patch.dict(os.environ, environment(proof)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(R.main(arguments), 1)
        self.assertEqual(output.read_bytes(), published)

    def test_create_cli_requires_a_purpose_and_records_test_mode(self):
        proof = self.proofs['windows-x64']
        proof_path = self.root / 'private-combined-proof.json'
        proof_path.write_bytes(P.canonical(proof))
        output = self.root / 'test-only/windows.json'
        arguments = ['create', '--proof', str(proof_path), '--runtime', str(self.paths['windows-x64']),
                     '--tag', TAG, '--platform', 'windows-x64', '--build-dir', str(self.build_dir),
                     '--output', str(output)]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as missing:
            R.main(arguments)
        self.assertEqual(missing.exception.code, 2)
        self.assertFalse(output.exists())
        with mock.patch.dict(os.environ, environment(proof)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(R.main(arguments + ['--purpose', 'test']), 0)
        receipt = json.loads(output.read_bytes())
        self.assertEqual(receipt['schemaVersion'], 2)
        self.assertEqual(receipt['purpose'], 'test')


if __name__ == '__main__':
    unittest.main()
