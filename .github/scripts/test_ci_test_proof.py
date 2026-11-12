import argparse
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile

import ci_test_proof as P


ENV = {'GITHUB_REPOSITORY': 'fixture/builder', 'GITHUB_SHA': '3' * 40,
       'GITHUB_RUN_ID': '99', 'GITHUB_RUN_ATTEMPT': '1'}
PLAN = {'schemaVersion': 1,
        'primaryUi': ['ui_regression', 'winmd_ui', 'populated_ui', 'theme_controls'],
        'companionUi': ['local_ui', 'native_ui', 'mod_favorites_ui', 'crash_notification_ui', 'external_auth_ui']}


PLAN_V2 = {**PLAN, 'schemaVersion': 2, 'extraBackendGroups': ['telemetry']}


def proof_pair(platform='windows-x64', schema=1):
    full = set(P.UI) | {'backend_' + str(i) for i in range(34)}
    if platform == 'linux-x86_64':
        full.add('linux_auth_vault')
    if schema == 2:
        full = P.inventory(platform)
    parts = P.partition(PLAN if schema == 1 else PLAN_V2, full)
    common = dict(schemaVersion=schema, kind='LCL_CTEST_SHARD_PROOF', repositorySha256=P.digest('fixture/builder'),
        workflowCommit=ENV['GITHUB_SHA'], runId=99, runAttempt=1, platform=platform,
        sourceCommit='1' * 40, sourceTree='2' * 40, fullGroupIds=P.ids(full),
        primaryGroupIds=P.ids(parts['primary']), companionGroupIds=P.ids(parts['companion']),
        fullGroupCount=len(full), fullSuite=False, failed=0, skipped=0)
    common.update({key: '4' * 64 for key in ('sourceInputsSha256', 'planSha256', 'registrationSha256',
                                           'sdkSha256', 'compilerSha256', 'configSha256')})
    return [dict(copy.deepcopy(common), shard=role, selectedPassedGroupIds=P.ids(parts[role]),
                 selectedGroupCount=len(parts[role])) for role in ('primary', 'companion')]


def archive(value, name='proof.json', extra=False, mode=0):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        info = zipfile.ZipInfo(name)
        if mode:
            info.external_attr = mode << 16
        z.writestr(info, value if isinstance(value, bytes) else P.canonical(value))
        if extra:
            z.writestr('extra.json', b'{}')
    return data.getvalue()


class FakeGitHub:
    def __init__(self, companion):
        self.repo = {'id': 7, 'full_name': 'fixture/builder', 'default_branch': 'main'}
        self.run = dict(id=99, run_attempt=1, repository=self.repo, head_repository=self.repo,
            head_sha=ENV['GITHUB_SHA'], head_branch='main', event='workflow_dispatch', path=P.WORKFLOW,
            status='in_progress', conclusion=None)
        # The attempt endpoint is authoritative; run_attempt is not required on
        # every API version's job object. Other keys mirror real GitHub jobs.
        self.helper = dict(id=8, run_id=99, name=P.PLATFORMS[companion['platform']][2],
            head_sha=ENV['GITHUB_SHA'], status='completed', conclusion='success',
            started_at='2026-11-11T01:00:00Z', completed_at='2026-11-11T01:01:00Z')
        self.blob = archive(companion)
        self.metadata = dict(id=9, name='test-proof-' + P.PLATFORMS[companion['platform']][1] + '-companion-attempt-1',
            expired=False, size_in_bytes=len(self.blob), created_at='2026-11-11T01:00:30Z',
            digest='sha256:' + hashlib.sha256(self.blob).hexdigest(),
            workflow_run=dict(id=99, repository_id=7, head_repository_id=7, head_sha=ENV['GITHUB_SHA'], head_branch='main'))
        self.jobs = [self.helper]
        self.artifacts = [self.metadata]
        self.calls = []
        self.final_attempt = None

    def json(self, route, *, pages=False):
        self.calls.append(route)
        if route == 'repos/fixture/builder':
            return copy.deepcopy(self.repo)
        if route.endswith('/actions/runs/99'):
            value = copy.deepcopy(self.run)
            if self.final_attempt and self.calls.count(route) > 1:
                value['run_attempt'] = self.final_attempt
            return value
        if '/attempts/1/jobs?' in route:
            assert pages
            return [{'jobs': copy.deepcopy(self.jobs)}]
        if '/artifacts?' in route:
            assert pages
            return [{'artifacts': copy.deepcopy(self.artifacts)}]
        raise AssertionError('Unexpected API endpoint')

    def artifact(self, repo, artifact_id):
        assert (repo, artifact_id) == ('fixture/builder', 9)
        return self.blob


class ProofValidation(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.local, self.companion = proof_pair()
        self.path = self.root / 'local.json'
        self.path.write_bytes(P.canonical(self.local))
        self.args = argparse.Namespace(local_proof=self.path, timeout_seconds=10, poll_seconds=1)
        self.gh = FakeGitHub(self.companion)

    def tearDown(self):
        P.PRIVATE_LOG = None
        self.temporary.cleanup()

    def verify(self):
        return P.verify_proof(self.args, ENV, self.gh)

    def test_combines_same_attempt_without_requiring_optional_job_attempt_field(self):
        result = self.verify()
        self.assertEqual(result['status'], 'PASS_COMBINED_CURRENT_CTEST')
        self.assertEqual(result['passedGroupCount'], 43)
        self.assertTrue(result['fullSuite'])
        self.assertTrue(any('/attempts/1/jobs?' in route for route in self.gh.calls))
        self.assertNotIn('native_ui', json.dumps(result))
        self.assertNotIn('backend_', json.dumps(result))

    def test_linux_combination_covers_44_groups(self):
        self.local, self.companion = proof_pair('linux-x86_64')
        self.path.write_bytes(P.canonical(self.local)); self.gh = FakeGitHub(self.companion)
        self.assertEqual(self.verify()['passedGroupCount'], 44)

    def test_schema2_combination_requires_all_original_groups_and_primary_telemetry(self):
        for platform, count in (('windows-x64', 44), ('linux-x86_64', 45)):
            first, second = proof_pair(platform, 2)
            result = P.combine(first, second, ENV['GITHUB_REPOSITORY'], ENV['GITHUB_SHA'], 99, 1)
            self.assertEqual((result['schemaVersion'], result['passedGroupCount']), (2, count))
            self.assertIn(P.ids({'telemetry'})[0], first['primaryGroupIds'])
            malformed = copy.deepcopy(second)
            old = P.ids({'auth'})[0]; new = P.ids({'surprise'})[0]
            for key in ('fullGroupIds', 'primaryGroupIds'):
                malformed[key] = sorted((set(malformed[key]) - {old}) | {new})
            with self.assertRaises(P.ProofError):
                P.validate_proof(malformed)
            malformed = copy.deepcopy(second)
            telemetry = P.ids({'telemetry'})[0]
            ui = malformed['companionGroupIds'][0]
            malformed['primaryGroupIds'] = sorted((set(malformed['primaryGroupIds']) - {telemetry}) | {ui})
            malformed['companionGroupIds'] = sorted((set(malformed['companionGroupIds']) - {ui}) | {telemetry})
            malformed['selectedPassedGroupIds'] = malformed['companionGroupIds']
            with self.assertRaises(P.ProofError):
                P.validate_proof(malformed)

    def test_plan_versions_reject_unreviewed_extensions_and_preserve_nine_ui_groups(self):
        self.assertEqual(P.validate_plan(PLAN), 1)
        self.assertEqual(P.validate_plan(PLAN_V2), 2)
        variants = [dict(PLAN_V2, extraBackendGroups=value) for value in
                    ([], ['auth'], ['telemetry', 'auth'], ['telemetry', 'telemetry'], 'telemetry', [True])]
        variants += [dict(PLAN, extraBackendGroups=['telemetry']), dict(PLAN, schemaVersion=2),
                     dict(PLAN_V2, unexpected=True), dict(PLAN_V2, schemaVersion=True),
                     dict(PLAN_V2, primaryUi=PLAN['primaryUi'][:-1])]
        for value in variants:
            with self.subTest(value=value), self.assertRaises(P.ProofError):
                P.validate_plan(value)

    def test_schema2_partition_rejects_missing_telemetry_or_replaced_backend(self):
        full = P.inventory('windows-x64')
        for names in (full - {'telemetry'}, (full - {'auth'}) | {'surprise'}, full | {'surprise'}):
            with self.subTest(names=names), self.assertRaises(P.ProofError):
                P.partition(PLAN_V2, names)

    def test_read_only_preflight_accepts_both_plans_and_rejects_invalid_extensions(self):
        path = self.root / 'plan.json'
        invalid_layout = dict(PLAN, primaryUi=PLAN['primaryUi'][:-1],
                              companionUi=PLAN['companionUi'] + PLAN['primaryUi'][-1:])
        for plan, expected in ((PLAN, 0), (PLAN_V2, 0), (invalid_layout, 1),
                               (dict(PLAN_V2, extraBackendGroups=['telemetry', 'auth']), 1)):
            path.write_bytes(P.canonical(plan))
            before = path.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(P.main(['validate-plan', '--plan', str(path)]), expected)
            self.assertEqual(path.read_bytes(), before)
        path.write_text('{"schemaVersion":2,"schemaVersion":2}')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(P.main(['validate-plan', '--plan', str(path)]), 1)

    def test_every_identity_mismatch_is_rejected(self):
        for key in P.IDENTITY_KEYS:
            if key in ('platform', 'fullGroupIds', 'primaryGroupIds', 'companionGroupIds'):
                continue
            with self.subTest(key=key):
                other = copy.deepcopy(self.companion)
                other[key] = other[key] + 1 if type(other[key]) is int else '9' * len(other[key])
                with self.assertRaises(P.ProofError):
                    P.combine(self.local, other, 'fixture/builder', ENV['GITHUB_SHA'], 99, 1)

    def test_missing_extra_overlap_duplicate_fail_skip_and_raw_field_rejected(self):
        changes = [lambda p: p['selectedPassedGroupIds'].pop(),
            lambda p: p['selectedPassedGroupIds'].append(self.local['selectedPassedGroupIds'][0]),
            lambda p: p['selectedPassedGroupIds'].append(p['selectedPassedGroupIds'][0]),
            lambda p: p.update(failed=1), lambda p: p.update(skipped=1),
            lambda p: p.update(fullSuite=True), lambda p: p.update(sourcePath='PRIVATE_SOURCE'),
            lambda p: p.update(fullGroupCount=True), lambda p: p.update(schemaVersion=True)]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                value = copy.deepcopy(self.companion); change(value)
                with self.assertRaises(P.ProofError):
                    P.combine(self.local, value, 'fixture/builder', ENV['GITHUB_SHA'], 99, 1)

    def test_invalid_workflow_provenance(self):
        changes = {'path': '.github/workflows/benchmark-windows-build.yml', 'head_branch': 'other',
                   'event': 'push', 'head_sha': '0' * 40, 'run_attempt': 2, 'id': 100,
                   'repository': {'full_name': 'sibling/builder'}, 'head_repository': {'full_name': 'fork/builder'},
                   'status': 'queued', 'conclusion': 'failure'}
        for key, value in changes.items():
            with self.subTest(key=key):
                self.gh = FakeGitHub(self.companion); self.gh.run[key] = value
                with self.assertRaises(P.ProofError): self.verify()

    def test_helper_failure_cancel_skip_missing_duplicate_and_other_attempt(self):
        for conclusion in ('failure', 'cancelled', 'skipped', 'timed_out', None):
            with self.subTest(conclusion=conclusion):
                self.gh = FakeGitHub(self.companion); self.gh.helper['conclusion'] = conclusion
                with self.assertRaisesRegex(P.ProofError, 'HELPER_NOT_SUCCESSFUL'): self.verify()
        for jobs in ([], [self.gh.helper, self.gh.helper]):
            self.gh = FakeGitHub(self.companion); self.gh.jobs = jobs
            with self.assertRaises(P.ProofError): self.verify()
        self.gh = FakeGitHub(self.companion); self.gh.helper['run_attempt'] = 2
        with self.assertRaises(P.ProofError): self.verify()

    def test_helper_wait_is_bounded_and_can_complete(self):
        self.gh.helper['status'] = 'in_progress'; self.gh.helper['conclusion'] = None
        ticks = [0.0]
        def finish(seconds):
            ticks[0] += seconds
            self.gh.helper.update(status='completed', conclusion='success')
        result = P.verify_proof(self.args, ENV, self.gh, sleep=finish, clock=lambda: ticks[0])
        self.assertEqual(result['passedGroupCount'], 43)
        self.gh.helper.update(status='in_progress', conclusion=None)
        with self.assertRaisesRegex(P.ProofError, 'HELPER_WAIT_TIMEOUT'):
            P.verify_proof(self.args, ENV, self.gh, sleep=lambda seconds: ticks.__setitem__(0, ticks[0] + seconds), clock=lambda: ticks[0])

    def test_artifact_current_attempt_origin_and_lifecycle(self):
        changes = [('name', 'test-proof-windows-companion-attempt-0'), ('expired', True),
                   ('size_in_bytes', P.MAX_ZIP + 1), ('created_at', '2026-11-11T00:59:59Z'),
                   ('created_at', '2026-11-11T01:01:01Z')]
        for key, value in changes:
            self.gh = FakeGitHub(self.companion); self.gh.metadata[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(P.ProofError): self.verify()
        for key, value in (('id', 100), ('head_sha', '8' * 40), ('repository_id', 8), ('head_repository_id', 8), ('head_branch', 'other')):
            self.gh = FakeGitHub(self.companion); self.gh.metadata['workflow_run'][key] = value
            with self.subTest(key=key), self.assertRaises(P.ProofError): self.verify()
        self.gh = FakeGitHub(self.companion); self.gh.artifacts = []
        with self.assertRaisesRegex(P.ProofError, 'MISSING_PROOF_RERUN_ALL_REQUIRED'): self.verify()
        self.gh = FakeGitHub(self.companion); self.gh.artifacts *= 2
        with self.assertRaises(P.ProofError): self.verify()
        self.gh = FakeGitHub(self.companion); self.gh.metadata['digest'] = 'sha256:' + '0' * 64
        with self.assertRaisesRegex(P.ProofError, 'ARTIFACT_DIGEST_MISMATCH'): self.verify()

    def test_attempt_is_rechecked_after_downloading(self):
        self.gh.final_attempt = 2
        with self.assertRaisesRegex(P.ProofError, 'STALE_ATTEMPT'): self.verify()

    def test_archive_rejects_paths_extra_files_links_oversize_and_json_duplicates(self):
        blobs = [archive(self.companion, '../proof.json'), archive(self.companion, 'dir/proof.json'),
                 archive(self.companion, extra=True), archive(self.companion, mode=stat.S_IFLNK | 0o777),
                 archive(b' ' * (P.MAX_PROOF + 1)), b'x' * (P.MAX_ZIP + 1),
                 archive(b'{"schemaVersion":1,"schemaVersion":1}'), archive(b'{"number":NaN}')]
        for index, blob in enumerate(blobs):
            with self.subTest(index=index), self.assertRaises(P.ProofError): P.proof_from_zip(blob)

    def test_cli_does_not_publish_private_api_errors_or_create_combined_output(self):
        output = self.root / 'combined.json'; private = self.root / 'private.log'
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, ENV), mock.patch.object(P.GitHub, 'json', side_effect=RuntimeError('PRIVATE_SOURCE_TOKEN')):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = P.main(['verify', '--local-proof', str(self.path), '--output', str(output), '--private-log', str(private)])
        self.assertEqual(code, 1); self.assertFalse(output.exists())
        self.assertNotIn('PRIVATE_SOURCE_TOKEN', stdout.getvalue() + stderr.getvalue())
        self.assertIn('PRIVATE_SOURCE_TOKEN', private.read_text())

    def test_transport_pins_github_hostname_even_with_inherited_gh_host(self):
        with mock.patch.dict(os.environ, {'GH_HOST': 'wrong-host.invalid'}), mock.patch.object(P, 'run_command', return_value=b'{}') as run:
            P.GitHub().json('repos/fixture/builder')
            P.GitHub().artifact('fixture/builder', 9)
        for call in run.call_args_list:
            self.assertEqual(call.args[0][:4], ['gh', 'api', '--hostname', 'github.com'])


class CreateProof(unittest.TestCase):
    schema_version = 1

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.root = Path(self.temporary.name)
        self.source = self.root / 'source'; self.native = self.source / 'qt-native'
        self.build = self.root / 'build'; self.sdk = self.root / 'sdk'; self.tools = self.root / 'tools'
        for path in (self.native, self.build, self.sdk, self.tools): path.mkdir(parents=True)
        self.platform = 'windows-x64' if os.name == 'nt' else 'linux-x86_64'
        self.put(self.native / 'CMakeLists.txt', 'fixture project')
        self.put(self.native / 'tests/cmake/auth_build_config_tests.cmake', 'fixture script')
        self.plan = copy.deepcopy(PLAN if self.schema_version == 1 else PLAN_V2)
        self.put(self.native / 'tests/ci/ui-shard-plan.json', json.dumps(self.plan))
        if self.schema_version == 2:
            self.put(self.build / 'generated/telemetry_build_config.h',
                     '#define MCL_TELEMETRY_ENDPOINT "https://telemetry.example.invalid/events"\n'
                     '#define MCL_TELEMETRY_BUILD_LABEL "fixture-effective-sha"\n')
        for arguments in (['init', '-q'], ['config', 'user.email', 'fixture@example.invalid'],
                          ['config', 'user.name', 'Fixture'], ['add', '.'], ['commit', '-qm', 'fixture']):
            subprocess.run(['git', '-C', str(self.source), *arguments], check=True, capture_output=True)
        self.commit = subprocess.check_output(['git', '-C', str(self.source), 'rev-parse', 'HEAD'], text=True).strip()
        self.tree = subprocess.check_output(['git', '-C', str(self.source), 'rev-parse', 'HEAD^{tree}'], text=True).strip()
        for name in ('cc', 'cxx', 'cmake', 'ninja', 'ctest', 'dbus-run-session'): self.put(self.tools / name, 'fixed tool ' + name)
        self.cache = {'CMAKE_PREFIX_PATH': str(self.sdk), 'CMAKE_BUILD_TYPE': 'Release', 'BUILD_TESTING': 'ON',
            'MCL_STATIC_RELEASE': 'OFF', 'MCL_CORE_ONLY': 'OFF', 'CMAKE_C_COMPILER': str(self.tools / 'cc'),
            'CMAKE_CXX_COMPILER': str(self.tools / 'cxx'), 'CMAKE_MAKE_PROGRAM': str(self.tools / 'ninja'),
            'CMAKE_COMMAND': str(self.tools / 'cmake'), 'CMAKE_CTEST_COMMAND': str(self.tools / 'ctest'),
            'CMAKE_CXX_FLAGS_RELEASE': '-O3 -DNDEBUG'}
        self.put(self.build / 'CMakeCache.txt', '\n'.join(k + ':STRING=' + v for k, v in self.cache.items()))
        self.put(self.sdk / 'lib/cmake/Qt6Core/Qt6CoreConfigVersionImpl.cmake', 'set(PACKAGE_VERSION "6.8.3")')
        self.put(self.sdk / 'include/QtCore/qconfig.h', 'fixed sdk config')
        modules = ('Core', 'Gui', 'Network', 'Qml', 'Quick', 'QuickControls2', 'Test')
        sdk_paths = (['bin/Qt6' + name + '.dll' for name in modules] +
                     ['bin/' + name + '.exe' for name in ('qmake', 'moc', 'rcc', 'qmlcachegen', 'qmlimportscanner')]) if os.name == 'nt' else (
                     ['lib/libQt6' + name + '.so.6.8.3' for name in modules + ('DBus',)] +
                     ['bin/qmake'] + ['libexec/' + name for name in ('moc', 'rcc', 'qmlcachegen', 'qmlimportscanner')])
        for name in sdk_paths: self.put(self.sdk / name, 'fixed sdk ' + name)
        self.full = set(P.UI) | {'auth_build_config'} | {'backend_' + str(i) for i in range(33)}
        if self.platform == 'linux-x86_64': self.full.add('linux_auth_vault')
        if self.schema_version == 2:
            self.full = P.inventory(self.platform)
        self.registration = {'kind': 'ctestInfo', 'version': {'major': 1, 'minor': 0}, 'tests': []}
        self.records = []
        for name in sorted(self.full):
            if name == 'auth_build_config': command = [str(self.tools / 'cmake'), '-P', str(self.native / 'tests/cmake/auth_build_config_tests.cmake')]
            else:
                exe = self.build / (name + '_tests' + ('.exe' if self.platform == 'windows-x64' else ''))
                self.put(exe, 'fresh executable ' + name)
                command = [str(exe)] if name != 'linux_auth_vault' else [str(self.tools / 'dbus-run-session'), '--', str(exe)]
            self.registration['tests'].append({'name': name, 'command': command, 'properties': [{'name': 'WORKING_DIRECTORY', 'value': str(self.build)}]})
            paths = [command[0], command[2]] if len(command) == 3 else command
            self.records.append({'name': name, 'command': command, 'files': [{'path': p, 'bytes': Path(p).stat().st_size, 'sha256': P.file_hash(p)} for p in paths]})
        self.args = argparse.Namespace(source_root=self.source, build_dir=self.build, sdk_root=self.sdk,
            driver_result=self.root / 'result.json', junit=self.root / 'ctest.xml', platform=self.platform, shard='primary')
        self.original_command = P.run_command
        self.patch = mock.patch.object(P, 'run_command', side_effect=self.command); self.patch.start()
        self.select('primary')

    def tearDown(self):
        self.patch.stop(); P.PRIVATE_LOG = None; self.temporary.cleanup()

    @staticmethod
    def put(path, text): path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)

    def command(self, arguments, **kwargs):
        if arguments[0] == str(self.tools / 'ctest'):
            self.assertEqual(arguments[1:], ['--test-dir', str(self.build), '--show-only=json-v1'])
            return P.canonical(self.registration)
        return self.original_command(arguments, **kwargs)

    def select(self, role):
        self.args.shard = role; selected = P.partition(self.plan, self.full)[role]
        self.driver = dict(schemaVersion=2 if self.platform == 'windows-x64' else 1,
            platform=self.platform, status='PASS_PARTIAL_CURRENT_CTEST', fullSuite=False, testShard=role,
            previousResultsReused=False, runId=99, runAttempt=1, sourceCommit=self.commit, sourceTree=self.tree,
            sourceStable=True, testInputsStable=True, ctestExitCode=0, expectedTestGroups=sorted(self.full),
            selectedTestGroups=sorted(selected), tests=self.records, mode='dynamic', uiSuitesExecuted=False,
            ctestValidation=dict(count=len(selected), failures=0, skipped=0, disabled=0, passedTestGroups=sorted(selected)))
        self.driver['sourceSnapshot'] = [{'path': str(path.relative_to(self.native)).replace('\\', '/'),
            'bytes': path.stat().st_size, 'sha256': P.file_hash(path)} for path in self.native.rglob('*') if path.is_file()]
        self.driver['commit'] = self.commit
        self.save_driver()
        self.put(self.args.junit, '<testsuite tests="' + str(len(selected)) + '" failures="0" skipped="0">' +
                 ''.join('<testcase name="' + n + '" status="run" time="1"/>' for n in sorted(selected)) + '</testsuite>')

    def save_driver(self): self.args.driver_result.write_bytes(P.canonical(self.driver))

    def test_create_proofs_from_real_files_git_and_exact_junit_then_combine(self):
        first = P.create_proof(self.args, ENV); self.select('companion'); second = P.create_proof(self.args, ENV)
        combined = P.combine(first, second, ENV['GITHUB_REPOSITORY'], ENV['GITHUB_SHA'], 99, 1)
        self.assertEqual(combined['passedGroupCount'], P.PLATFORMS[self.platform][0] + (self.schema_version == 2))
        self.assertNotIn(str(self.root), json.dumps(first)); self.assertNotIn('native_ui', json.dumps(first))

    def test_driver_stale_source_attempt_partial_or_unstable_inputs_fail(self):
        for key, bad in (('status', 'PASS_NEW_HOST_LOCAL_CTEST'), ('fullSuite', True), ('testShard', 'companion'),
                         ('runAttempt', 0), ('commit', '9' * 40), ('previousResultsReused', True)):
            with self.subTest(key=key):
                original = self.driver[key]; self.driver[key] = bad; self.save_driver()
                with self.assertRaises(P.ProofError): P.create_proof(self.args, ENV)
                self.driver[key] = original
        self.save_driver()

    def test_changed_executable_and_filtered_command_fail(self):
        path = Path(self.records[1]['files'][0]['path']); old = path.read_bytes(); path.write_bytes(b'tampered')
        with self.assertRaisesRegex(P.ProofError, 'TEST_INPUT_CHANGED'): P.create_proof(self.args, ENV)
        path.write_bytes(old)
        self.registration['tests'][1]['command'].append('oneFunctionOnly')
        with self.assertRaises(P.ProofError): P.create_proof(self.args, ENV)

    def test_junit_failure_skip_missing_extra_duplicate_and_notrun_rejected(self):
        original = self.args.junit.read_text()
        changes = [original.replace('failures="0"', 'failures="1"'), original.replace('status="run"', 'status="notrun"', 1),
            original.replace('/>', '><skipped/></testcase>', 1), original.replace('/>', '><failure/></testcase>', 1),
            original.replace('</testsuite>', '<testcase name="unexpected" status="run"/></testsuite>'),
            original.replace('<testcase', '<discarded', 1), original.replace('</testsuite>', '<testcase name="native_ui" status="run"/></testsuite>')]
        for changed in changes:
            self.put(self.args.junit, changed)
            with self.assertRaises(P.ProofError): P.create_proof(self.args, ENV)

    def test_dirty_tracked_source_is_rejected(self):
        self.put(self.native / 'CMakeLists.txt', 'uncommitted source modification')
        with self.assertRaises(P.ProofError): P.create_proof(self.args, ENV)

    def test_untracked_compiler_input_is_fingerprinted_and_sdk_compiler_tampering_cannot_combine(self):
        first = P.create_proof(self.args, ENV)
        self.put(self.native / 'src/untracked.cpp', 'an additional compile input')
        changed = P.source_identity(self.source)
        self.assertNotEqual(first['sourceInputsSha256'], changed[2])
        (self.native / 'src/untracked.cpp').unlink()
        self.select('companion')
        sdk_file = self.sdk / ('bin/Qt6Core.dll' if os.name == 'nt' else 'lib/libQt6Core.so.6.8.3')
        original = sdk_file.read_bytes(); sdk_file.write_bytes(b'different Qt build')
        second = P.create_proof(self.args, ENV)
        with self.assertRaisesRegex(P.ProofError, 'SHARD_IDENTITY_MISMATCH'):
            P.combine(first, second, ENV['GITHUB_REPOSITORY'], ENV['GITHUB_SHA'], 99, 1)
        sdk_file.write_bytes(original)
        (self.tools / 'cxx').write_bytes(b'different compiler')
        second = P.create_proof(self.args, ENV)
        with self.assertRaisesRegex(P.ProofError, 'SHARD_IDENTITY_MISMATCH'):
            P.combine(first, second, ENV['GITHUB_REPOSITORY'], ENV['GITHUB_SHA'], 99, 1)

    def test_partition_cannot_lose_duplicate_or_add_a_ui_group(self):
        for change in (lambda p: p['primaryUi'].pop(), lambda p: p['companionUi'].append(p['primaryUi'][0]),
                       lambda p: p['companionUi'].append('not_a_ui_group')):
            plan = copy.deepcopy(PLAN); change(plan)
            with self.assertRaises(P.ProofError): P.partition(plan, self.full)

    def test_config_and_registration_paths_normalize_but_flags_and_commands_do_not(self):
        one = P.normalizer('/vm-one/source', '/vm-one/build', '/vm-one/sdk')
        two = P.normalizer('/vm-two/source', '/vm-two/build', '/vm-two/sdk')
        self.assertEqual(one(['/vm-one/source/file', '/vm-one/build/cache', '/vm-one/sdk/lib']),
                         two(['/vm-two/source/file', '/vm-two/build/cache', '/vm-two/sdk/lib']))
        a = P.build_identity(self.source, self.build, self.sdk, self.cache, self.registration)
        cache = dict(self.cache, CMAKE_CXX_FLAGS_RELEASE='-O0')
        b = P.build_identity(self.source, self.build, self.sdk, cache, self.registration)
        self.assertNotEqual(a[2], b[2])
        registration = copy.deepcopy(self.registration); registration['tests'][0]['properties'].append({'name': 'COST', 'value': 100})
        self.assertEqual(a[3], P.build_identity(self.source, self.build, self.sdk, self.cache, registration)[3])


class CreateTelemetryProof(CreateProof):
    schema_version = 2

    def test_effective_endpoint_and_automatic_build_label_must_match_between_shards(self):
        # Both caches leave the override label empty, as a normal automatic
        # Git-derived build does. Only the generated header holds that label.
        self.cache['MCL_TELEMETRY_BUILD_LABEL'] = ''
        self.put(self.build / 'CMakeCache.txt', '\n'.join(k + ':STRING=' + v for k, v in self.cache.items()))
        first = P.create_proof(self.args, ENV)
        self.select('companion')
        header = self.build / 'generated/telemetry_build_config.h'
        original = header.read_text()
        for changed in (original.replace('telemetry.example.invalid', 'other.example.invalid'),
                        original.replace('fixture-effective-sha', 'different-effective-sha')):
            self.put(header, changed)
            second = P.create_proof(self.args, ENV)
            self.assertNotEqual(first['configSha256'], second['configSha256'])
            self.assertEqual(first['sourceInputsSha256'], second['sourceInputsSha256'])
            with self.assertRaisesRegex(P.ProofError, 'SHARD_IDENTITY_MISMATCH'):
                P.combine(first, second, ENV['GITHUB_REPOSITORY'], ENV['GITHUB_SHA'], 99, 1)

    def test_missing_effective_telemetry_header_blocks_proof(self):
        (self.build / 'generated/telemetry_build_config.h').unlink()
        with self.assertRaisesRegex(P.ProofError, 'TELEMETRY_CONFIGURATION_MISSING'):
            P.create_proof(self.args, ENV)

    def test_telemetry_requires_current_executable_and_complete_registration(self):
        self.registration['tests'] = [t for t in self.registration['tests'] if t['name'] != 'telemetry']
        with self.assertRaisesRegex(P.ProofError, 'INCOMPLETE_REGISTRATION'):
            P.create_proof(self.args, ENV)


if __name__ == '__main__':
    unittest.main()
