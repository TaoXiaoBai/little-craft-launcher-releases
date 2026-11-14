import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import ci_safe_diagnostics as safe


class SafeDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.logs = self.root / 'private-logs'
        self.logs.mkdir()
        self.paths = ['qt-native/tests/native_ui_tests.cpp', 'qt-native/src/core/game_service.cpp',
                      'qt-native/cmake/AndroidPreview.cmake', 'qt-native/CMakeLists.txt']
        for path in self.paths:
            target = self.source / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('// owned synthetic fixture\n')
        self.git('init', '-q')
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
        self.commit = self.git('rev-parse', 'HEAD').strip()

    def git(self, *arguments):
        return subprocess.check_output(['git', '-C', str(self.source), *arguments], text=True)

    def make(self, text='', **kwargs):
        (self.logs / 'ctest.stdout.log').write_text(text)
        return safe.create_summary(self.source, self.logs, kwargs.pop('platform', 'windows-ui-companion'),
            kwargs.pop('status', 'failure'), run_id=12345, run_attempt=2,
            workflow_commit='a' * 40, **kwargs)

    def test_qt_failure_exports_only_fingerprint_and_numbers(self):
        path = self.source / self.paths[0]
        raw = ("1/5 Test #36: native_ui ....***Failed 1.0 sec\n"
               "FAIL! : PrivateClass::secretAccountRow(email@example.invalid) 'access_token=SECRET' returned FALSE\n"
               f"   Loc: [{path}(2548)]\n")
        summary = self.make(raw)
        self.assertEqual(summary['sourceCommit'], self.commit)
        self.assertEqual(summary['failedGroups'], ['native_ui'])
        self.assertEqual(summary['categories'], ['TEST_FAILURE'])
        self.assertEqual(summary['locations'], [dict(fileId=safe.path_id(self.paths[0]), line=2548,
                                                    column=None, category='TEST_FAILURE')])
        data = safe.encode_summary(summary)
        for forbidden in (b'SECRET', b'email', b'PrivateClass', b'access_token', b'native_ui_tests.cpp',
                          str(self.source).encode()):
            self.assertNotIn(forbidden, data)

    def test_windows_qttest_and_msvc_locations(self):
        root = 'D:/a/owned-builder/owned-builder/source'
        text = (f'{root}/{self.paths[0]}(400) : failure location\n'
                f'{root}/{self.paths[1]}(12,8): error C9999: secret source text\n')
        summary = self.make(text, recorded_source_root=root)
        self.assertEqual(len(summary['locations']), 2)
        self.assertEqual(set(summary['categories']), {'TEST_FAILURE', 'COMPILER_ERROR'})
        self.assertNotIn('secret', json.dumps(summary))

    def test_gcc_and_cmake_relative_locations(self):
        summary = self.make('src/core/game_service.cpp:12:4: error: source secret\n'
                            'CMake Error at cmake/AndroidPreview.cmake:14 (message):\n')
        found = {location['fileId']: location for location in summary['locations']}
        self.assertEqual(found[safe.path_id(self.paths[1])]['column'], 4)
        self.assertEqual(found[safe.path_id(self.paths[2])]['category'], 'CONFIGURATION_ERROR')

    def test_external_untracked_and_traversal_paths_are_omitted(self):
        text = ('/external/qt-native/tests/native_ui_tests.cpp(20) : failure location\n'
                '../tests/native_ui_tests.cpp:21: error: secret\n'
                f'{self.source}/qt-native/src/untracked.cpp:22: error: secret\n')
        summary = self.make(text)
        self.assertEqual(summary['locations'], [])
        self.assertIn('COMPILER_ERROR', summary['categories'])

    def test_ambiguous_relative_basename_is_not_guessed(self):
        extra = self.source / 'other/native_ui_tests.cpp'
        extra.parent.mkdir(); extra.write_text('// fixture\n')
        self.git('add', '.'); self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                                     'commit', '-qm', 'second path')
        self.assertEqual(self.make('native_ui_tests.cpp(3) : failure location\n')['locations'], [])

    def test_timeout_group_allowlist_and_proof_error(self):
        summary = self.make('2/5 Test #9: native_ui ....***Timeout\n'
                            '3/5 Test #10: private_username ....***Failed\n'
                            'RELEASE_PROVENANCE_REJECTED:SECRET_VALUE\n')
        self.assertEqual(summary['failedGroups'], ['native_ui'])
        self.assertEqual(set(summary['categories']), {'TEST_TIMEOUT', 'TEST_FAILURE', 'PROVENANCE_ERROR'})
        self.assertNotIn('SECRET_VALUE', json.dumps(summary))

    def test_passed_report_does_not_report_expected_negative_test_output(self):
        summary = self.make('FAIL! : expected negative child fixture\n', status='success')
        self.assertEqual(summary['status'], 'passed')
        self.assertEqual(summary['categories'], [])
        self.assertEqual(summary['locations'], [])

    def test_missing_checkout_still_has_safe_minimal_summary(self):
        summary = safe.create_summary(self.root / 'absent', self.root / 'absent-logs', 'android', 'failure',
                                     run_id=5, run_attempt=1)
        self.assertIsNone(summary['sourceCommit'])
        self.assertEqual(summary['categories'], ['UNKNOWN_FAILURE'])

    def test_nested_noncheckout_never_uses_ancestor_git_identity(self):
        nested = self.source / 'nested-unchecked-out-source'
        nested.mkdir()
        summary = safe.create_summary(nested, self.logs, 'android', 'failure', run_id=5, run_attempt=1)
        self.assertIsNone(summary['sourceCommit'])
        self.assertEqual(summary['locations'], [])

    def test_symlink_and_hardlink_logs_are_not_read(self):
        external = self.root / 'external.log'
        external.write_text(f'{self.source}/{self.paths[0]}(7) : failure location\n')
        try:
            (self.logs / 'link.log').symlink_to(external)
            os.link(external, self.logs / 'hardlink.log')
        except OSError:
            self.skipTest('This local filesystem cannot create owned link fixtures')
        summary = safe.create_summary(self.source, self.logs, 'android', 'failure', run_id=1, run_attempt=1)
        self.assertEqual(summary['locations'], [])
        self.assertTrue(summary['truncated'])

    def test_bounded_locations_and_log_scan(self):
        raw = ''.join(f'{self.source}/{self.paths[0]}({i}) : failure location\n' for i in range(1, 100))
        summary = self.make(raw)
        self.assertEqual(len(summary['locations']), safe.MAX_LOCATIONS)
        self.assertTrue(summary['truncated'])
        with patch.object(safe, 'MAX_FILE_BYTES', 1024):
            summary = self.make('secret padding\n' * 1000)
        self.assertTrue(summary['truncated'])
        self.assertLess(len(safe.encode_summary(summary)), safe.MAX_SUMMARY)

    def test_json_schema_rejects_extra_raw_fields_and_bad_types(self):
        good = self.make()
        variants = []
        bad = copy.deepcopy(good); bad['rawLog'] = 'secret'; variants.append(bad)
        for key, value in [('runId', True), ('sourceCommit', 'secret'), ('platform', []),
                           ('categories', ['arbitrary secret']), ('failedGroups', ['private']), ('truncated', 1)]:
            bad = copy.deepcopy(good); bad[key] = value; variants.append(bad)
        for bad in variants:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                safe.validate_summary(bad)
        location = dict(fileId='b' * 64, line=1, column=None, category='TEST_FAILURE')
        for key, value in [('line', True), ('line', 0), ('column', -1), ('category', []), ('fileId', 'private/path')]:
            bad = copy.deepcopy(good); bad['locations'] = [dict(location, **{key: value})]
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                safe.validate_summary(bad)

    def test_utf16_windows_log_is_supported_without_exporting_text(self):
        (self.logs / 'utf16.log').write_bytes((f'{self.source}/{self.paths[0]}(19) : failure location\n').encode('utf-16'))
        summary = safe.create_summary(self.source, self.logs, 'windows-x64', 'failure', run_id=1, run_attempt=1)
        self.assertEqual(summary['locations'][0]['line'], 19)

    def test_truncated_utf16_tail_and_directory_scan_are_bounded(self):
        text = 'private padding\n' * 1000 + f'{self.source}/{self.paths[0]}(29) : failure location\n'
        (self.logs / 'utf16.log').write_bytes(text.encode('utf-16'))
        with patch.object(safe, 'MAX_FILE_BYTES', 1024):
            summary = safe.create_summary(self.source, self.logs, 'windows-x64', 'failure', run_id=1, run_attempt=1)
        self.assertTrue(summary['truncated'])
        self.assertEqual(summary['locations'][0]['line'], 29)
        for i in range(8):
            (self.logs / f'empty-{i}.bin').write_bytes(b'')
        with patch.object(safe, 'MAX_DIRECTORY_ENTRIES', 2):
            files, truncated = safe.log_files(self.logs)
        self.assertTrue(truncated)
        self.assertLessEqual(len(files), 2)

    def test_output_is_exclusive_and_refuses_linked_parent(self):
        summary = self.make()
        target = self.root / 'safe-summary' / 'summary.json'
        safe.write_summary(target, summary)
        with self.assertRaises(FileExistsError):
            safe.write_summary(target, summary)
        link = self.root / 'linked-output'
        try:
            link.symlink_to(target.parent, target_is_directory=True)
        except OSError:
            return
        with self.assertRaises(ValueError):
            safe.write_summary(link / 'another.json', summary)

    def test_cli_metadata_overrides_and_fixed_error_output(self):
        script = Path(safe.__file__)
        output = self.root / 'out' / 'summary.json'
        result = subprocess.run(['python3', str(script), 'create', '--source-root', str(self.source),
            '--logs-dir', str(self.logs), '--platform', 'android', '--status', 'failure',
            '--run-id', '33', '--attempt', '2', '--workflow-commit', 'c' * 40, '--output', str(output)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(output.read_text())
        self.assertEqual(summary['runId'], 33)
        self.assertEqual(result.stdout.strip(), 'SAFE_DIAGNOSTIC_CREATED')
        self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
