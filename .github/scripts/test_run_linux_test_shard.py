import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET


SPEC = importlib.util.spec_from_file_location('linux_test_shard', Path(__file__).with_name('run-linux-test-shard.py'))
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
PLAN = {'schemaVersion': 1,
        'primaryUi': ['ui_regression', 'winmd_ui', 'populated_ui', 'theme_controls'],
        'companionUi': ['local_ui', 'native_ui', 'mod_favorites_ui', 'crash_notification_ui', 'external_auth_ui']}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='linux-shard-fixture-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'source'
        self.build = self.root/'build'
        self.logs = self.root/'private-logs'
        self.tools = self.root/'tools'
        for path in (self.source, self.build, self.logs, self.tools):
            path.mkdir()
        self.native = self.source/'qt-native'
        (self.native/'tests/ci').mkdir(parents=True)
        (self.native/'tests/cmake').mkdir()
        (self.native/'CMakeLists.txt').write_text('project(owned_fixture)\n')
        (self.native/'tests/cmake/auth_build_config_tests.cmake').write_text('message(STATUS "owned fixture")\n')
        (self.native/'tests/ci/ui-shard-plan.json').write_text(json.dumps(PLAN))
        self.cmake = self.tools/'cmake'
        self.dbus = self.tools/'dbus-run-session'
        for file in (self.cmake, self.dbus):
            shutil.copyfile('/bin/true', file)
            file.chmod(0o700)
        self.ctest = self.tools/'ctest'
        self.ctest.write_text('''#!/usr/bin/env python3
import json,os,pathlib,re,sys,xml.etree.ElementTree as E
args=sys.argv[1:];build=pathlib.Path(args[args.index('--test-dir')+1]);fixture=build/'registration.fixture.json'
registration=json.loads(fixture.read_text())
assert os.environ.get('QT_QPA_PLATFORM')=='offscreen' and os.environ.get('QT_QUICK_BACKEND')=='software'
assert os.environ.get('QML_DISABLE_DISK_CACHE')=='1'
assert 'MCL_MS_CLIENT_ID' not in os.environ and 'SOURCE_READ_TOKEN' not in os.environ and 'GH_TOKEN' not in os.environ
if '--show-only=json-v1' in args:
 print(json.dumps(registration));raise SystemExit(0)
assert '--parallel' in args and args[args.index('--parallel')+1]=='2'
assert '--stop-on-failure' in args and '--no-tests=error' in args
pattern=args[args.index('-R')+1];names=[t['name'] for t in registration['tests'] if re.fullmatch(pattern,t['name'])]
(build/'executed.json').write_text(json.dumps(names))
root=E.Element('testsuite',tests=str(len(names)),failures='0',disabled='0',skipped='0')
for name in names:E.SubElement(root,'testcase',name=name,status='run',time='0.01')
mode=os.environ.get('FIXTURE_JUNIT_MODE','')
if mode=='missing':root.remove(root[-1]);root.set('tests',str(len(root)))
if mode=='duplicate':E.SubElement(root,'testcase',name=names[0],status='run',time='0.01');root.set('tests',str(len(root)))
if mode=='failed':E.SubElement(root[0],'failure');root.set('failures','1')
if mode=='skipped':E.SubElement(root[0],'skipped');root[0].set('status','notrun');root.set('skipped','1')
output=pathlib.Path(args[args.index('--output-junit')+1]);E.ElementTree(root).write(output)
if os.environ.get('FIXTURE_MUTATE_SOURCE'):pathlib.Path(os.environ['FIXTURE_MUTATE_SOURCE']).write_text('changed')
if os.environ.get('FIXTURE_MUTATE_BINARY'):pathlib.Path(os.environ['FIXTURE_MUTATE_BINARY']).write_text('changed')
if os.environ.get('FIXTURE_MUTATE_REGISTRATION'):
 registration['tests'][0]['properties'][0]['value']=1
 fixture.write_text(json.dumps(registration))
raise SystemExit(int(os.environ.get('FIXTURE_EXIT','0')))
''')
        self.ctest.chmod(0o700)
        self.cache = {'CMAKE_BUILD_TYPE': 'Release', 'BUILD_TESTING': 'ON', 'MCL_CORE_ONLY': 'OFF',
                      'MCL_STATIC_RELEASE': 'OFF', 'MCL_DISTRIBUTION_BUILD': 'OFF',
                      'MCL_SERIALIZE_SOFTWARE_UI': 'ON', 'MCL_TEST_QML_CACHE': 'ON',
                      'CMAKE_GENERATOR': 'Ninja', 'CMAKE_HOME_DIRECTORY': str(self.native),
                      'CMAKE_CACHEFILE_DIR': str(self.build), 'CMAKE_COMMAND': str(self.cmake),
                      'CMAKE_CTEST_COMMAND': str(self.ctest), 'MCL_DBUS_RUN_SESSION': str(self.dbus)}
        self.write_cache()
        self.registration = {'version': {'major': 1, 'minor': 0}, 'tests': []}
        for name in sorted(runner.EXPECTED_GROUPS):
            binary = self.build/(name+'_tests')
            binary.write_text('owned compiled fixture: '+name)
            binary.chmod(0o700)
            command = [str(binary)]
            props = [{'name': 'TIMEOUT', 'value': runner.TIMEOUTS[name]},
                     {'name': 'WORKING_DIRECTORY', 'value': str(self.build)}]
            if name == 'auth_build_config':
                command = [str(self.cmake), '-P', str(self.native/'tests/cmake/auth_build_config_tests.cmake')]
            if name == 'linux_auth_vault':
                command = [str(self.dbus), '--', str(binary)]
                props.append({'name': 'ENVIRONMENT', 'value': ['MCL_OWNED_KEYRING_TEST_BUS=1']})
            if name in runner.UI_GROUPS:
                enabled = name != 'theme_controls'
                environment = ['QML_DISABLE_DISK_CACHE='+('0' if enabled else '1'), 'QML_FORCE_DISK_CACHE=0',
                               'QML_DISK_CACHE=', 'QML_DISK_CACHE_PATH='+(str(self.build/'ci-tmp/qml-cache'/name) if enabled else '')]
                if enabled:
                    environment.append('QT_QPA_PLATFORM=offscreen')
                    props.append({'name': 'RESOURCE_LOCK', 'value': ['mcl_software_renderer']})
                else:
                    props.append({'name': 'RUN_SERIAL', 'value': True})
                props.append({'name': 'ENVIRONMENT', 'value': environment})
            self.registration['tests'].append({'name': name, 'command': command, 'properties': props})
        self.write_registration()
        self.git('init', '-q')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'user.name', 'Owned fixture')
        self.git('add', '.')
        self.git('commit', '-qm', 'Owned source fixture')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.source), *args], stderr=subprocess.DEVNULL)

    def write_cache(self):
        (self.build/'CMakeCache.txt').write_text('\n'.join(f'{k}:STRING={v}' for k, v in self.cache.items())+'\n')

    def write_registration(self):
        (self.build/'registration.fixture.json').write_text(json.dumps(self.registration))

    def run_shard(self, shard='primary', **environment):
        args = argparse.Namespace(source_root=self.source, build_dir=self.build, shard=shard,
                                  junit=self.logs/'ctest.xml', result_json=self.logs/'test-result.json', ctest=str(self.ctest))
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_RUN_ID': '123456', 'GITHUB_RUN_ATTEMPT': '1', **environment}
        with mock.patch.dict(os.environ, env):
            code = runner.execute(args)
        return code, json.loads(args.result_json.read_text())

    def test_primary_executes_all_backends_and_four_ui_groups(self):
        code, result = self.run_shard()
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'PASS_PARTIAL_CURRENT_CTEST')
        self.assertEqual(len(result['tests']), 44)
        self.assertEqual(len(result['selectedTestGroups']), 39)
        self.assertEqual(set(result['selectedTestGroups']), runner.BACKEND_GROUPS | set(PLAN['primaryUi']))
        self.assertEqual(result['ctestValidation']['count'], 39)
        self.assertTrue(result['sourceStable'] and result['testInputsStable'])
        self.assertFalse(result['fullSuite'] or result['previousResultsReused'])
        self.assertEqual(result['runId'], 123456)
        self.assertEqual(result['runAttempt'], 1)
        self.assertEqual(json.loads((self.build/'executed.json').read_text()), result['selectedTestGroups'])

    def test_companion_executes_exactly_five_ui_groups(self):
        code, result = self.run_shard('companion')
        self.assertEqual(code, 0)
        self.assertEqual(len(result['selectedTestGroups']), 5)
        self.assertEqual(set(result['selectedTestGroups']), set(PLAN['companionUi']))

    def test_shard_union_is_exact_and_disjoint(self):
        names = sorted(runner.EXPECTED_GROUPS)
        primary = set(runner.select_groups(PLAN, names, 'primary'))
        other = set(runner.select_groups(PLAN, names, 'companion'))
        self.assertFalse(primary & other)
        self.assertEqual(primary | other, runner.EXPECTED_GROUPS)

    def test_rejects_bad_shard_plan_variants(self):
        variants = [None, {**PLAN, 'extra': True}, {**PLAN, 'schemaVersion': True},
                    {**PLAN, 'schemaVersion': 2}, {**PLAN, 'primaryUi': []},
                    {**PLAN, 'primaryUi': PLAN['primaryUi'][:-1]+['auth']},
                    {**PLAN, 'companionUi': PLAN['companionUi'][:-1]+['local_ui']},
                    {**PLAN, 'primaryUi': PLAN['primaryUi'][:-1]+['local_ui']},
                    {**PLAN, 'primaryUi': PLAN['primaryUi'][:-1]+['ui_regression|.*']},
                    {**PLAN, 'companionUi': 'local_ui'}]
        for plan in variants:
            with self.subTest(plan=plan), self.assertRaises(runner.ShardError):
                runner.select_groups(plan, sorted(runner.EXPECTED_GROUPS), 'primary')

    def test_rejects_missing_extra_and_duplicate_registered_groups(self):
        names = sorted(runner.EXPECTED_GROUPS)
        for malformed in [names[:-1], names+['surprise'], names[:-1]+[names[0]]]:
            with self.subTest(names=malformed), self.assertRaises(runner.ShardError):
                runner.select_groups(PLAN, malformed, 'primary')

    def test_group_regex_does_not_match_case_names_or_prefixes(self):
        import re
        expression = re.compile(runner.exact_regex(['auth', 'ui_regression']))
        self.assertTrue(expression.fullmatch('auth'))
        for name in ['auth:row', 'auth_extra', 'ui_regression:function', 'xauth']:
            self.assertIsNone(expression.fullmatch(name))
        for names in [[], ['auth', 'auth'], ['auth|.*']]:
            with self.assertRaises(runner.ShardError):
                runner.exact_regex(names)

    def test_detects_source_changes_during_ctest(self):
        code, result = self.run_shard(FIXTURE_MUTATE_SOURCE=str(self.native/'CMakeLists.txt'))
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'FAILED')
        self.assertFalse(result['sourceStable'])

    def test_detects_binary_changes_during_ctest(self):
        code, result = self.run_shard(FIXTURE_MUTATE_BINARY=str(self.build/'auth_tests'))
        self.assertEqual(code, 1)
        self.assertFalse(result['testInputsStable'])

    def test_detects_registration_changes_during_ctest(self):
        code, result = self.run_shard(FIXTURE_MUTATE_REGISTRATION='1')
        self.assertEqual(code, 1)
        self.assertFalse(result['testInputsStable'])

    def test_failed_process_never_becomes_a_passing_partial_result(self):
        code, result = self.run_shard(FIXTURE_EXIT='8')
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['ctestExitCode'], 8)

    def test_rejects_missing_junit_group(self):
        self.assertEqual(self.run_shard(FIXTURE_JUNIT_MODE='missing')[0], 1)

    def test_rejects_duplicate_junit_group(self):
        self.assertEqual(self.run_shard(FIXTURE_JUNIT_MODE='duplicate')[0], 1)

    def test_rejects_failed_junit_group_even_with_zero_exit(self):
        self.assertEqual(self.run_shard(FIXTURE_JUNIT_MODE='failed')[0], 1)

    def test_rejects_skipped_junit_group_even_with_zero_exit(self):
        self.assertEqual(self.run_shard(FIXTURE_JUNIT_MODE='skipped')[0], 1)

    def test_rejects_old_ctest_results(self):
        path = self.build/'Testing/Temporary/LastTest.log'
        path.parent.mkdir(parents=True)
        path.write_text('previous result')
        code, result = self.run_shard()
        self.assertEqual(code, 1)
        self.assertEqual(result['failure'], 'PREVIOUS_TEST_RESULTS_PRESENT')

    def test_rejects_restored_qml_compilation_cache(self):
        path = self.build/'ci-tmp/qml-cache/previous.qmlc'
        path.parent.mkdir(parents=True)
        path.write_text('previous compiled QML')
        code, result = self.run_shard()
        self.assertEqual(code, 1)
        self.assertEqual(result['failure'], 'PREVIOUS_QML_CACHE_RESTORED')

    def test_rejects_dirty_source_before_execution(self):
        (self.native/'CMakeLists.txt').write_text('changed before execution')
        code, result = self.run_shard()
        self.assertEqual(code, 1)
        self.assertEqual(result['failure'], 'DIRTY_SOURCE_INPUT')
        self.assertFalse((self.build/'executed.json').exists())

    def test_does_not_overwrite_a_previous_driver_result(self):
        path = self.logs/'test-result.json'
        path.write_text('original')
        with self.assertRaisesRegex(runner.ShardError, 'PREVIOUS_RESULT_REUSED'):
            self.run_shard()
        self.assertEqual(path.read_text(), 'original')

    def test_private_results_cannot_be_created_inside_the_source_checkout(self):
        args = argparse.Namespace(source_root=self.source, build_dir=self.build, shard='primary',
            junit=self.logs/'ctest.xml', result_json=self.source/'private-result.json', ctest=str(self.ctest))
        with self.assertRaisesRegex(runner.ShardError, 'OUTPUT_INSIDE_SOURCE_CHECKOUT'):
            runner.execute(args)
        self.assertFalse(args.result_json.exists())

    def test_rejects_altered_serializer_configuration(self):
        self.cache['MCL_SERIALIZE_SOFTWARE_UI'] = 'OFF'
        self.write_cache()
        code, result = self.run_shard()
        self.assertEqual(code, 1)
        self.assertEqual(result['failure'], 'INVALID_CTEST_CONFIGURATION')

    def test_rejects_row_filter_arguments_in_registered_native_commands(self):
        for name in ['auth', 'ui_regression', 'auth_build_config', 'linux_auth_vault']:
            malformed = copy.deepcopy(self.registration)
            test = next(t for t in malformed['tests'] if t['name'] == name)
            test['command'].append('oneCase:oneRow')
            with self.subTest(name=name), self.assertRaisesRegex(runner.ShardError, 'FILTERED_CTEST_COMMAND'):
                runner.collect_identities(malformed, self.source, self.build, self.cache)

    def test_rejects_wrong_test_executable(self):
        malformed = copy.deepcopy(self.registration)
        next(t for t in malformed['tests'] if t['name'] == 'auth')['command'] = [str(self.build/'java_tests')]
        with self.assertRaisesRegex(runner.ShardError, 'FILTERED_CTEST_COMMAND'):
            runner.collect_identities(malformed, self.source, self.build, self.cache)

    def test_rejects_missing_and_duplicate_discovery(self):
        for tests in [self.registration['tests'][:-1], self.registration['tests'][:-1]+[self.registration['tests'][0]]]:
            malformed = {**self.registration, 'tests': tests}
            with self.assertRaisesRegex(runner.ShardError, 'INCOMPLETE_CTEST_INVENTORY'):
                runner.collect_identities(malformed, self.source, self.build, self.cache)

    def test_rejects_changed_ui_lock_timeout_cache_and_native_focus_policy(self):
        modifications = [('ui_regression', 'RESOURCE_LOCK', []), ('ui_regression', 'TIMEOUT', 10),
                         ('ui_regression', 'ENVIRONMENT', ['QML_DISABLE_DISK_CACHE=1']),
                         ('theme_controls', 'RUN_SERIAL', False),
                         ('linux_auth_vault', 'ENVIRONMENT', [])]
        for name, key, value in modifications:
            malformed = copy.deepcopy(self.registration)
            test = next(t for t in malformed['tests'] if t['name'] == name)
            next(p for p in test['properties'] if p['name'] == key)['value'] = value
            with self.subTest(name=name, key=key), self.assertRaises(runner.ShardError):
                runner.collect_identities(malformed, self.source, self.build, self.cache)

    def test_accepts_legacy_and_current_populated_ui_budgets(self):
        for seconds in (120, 180):
            registration = copy.deepcopy(self.registration)
            test = next(t for t in registration['tests'] if t['name'] == 'populated_ui')
            next(p for p in test['properties'] if p['name'] == 'TIMEOUT')['value'] = seconds
            with self.subTest(seconds=seconds):
                identities = runner.collect_identities(registration, self.source, self.build, self.cache)
                self.assertEqual(len(identities), 44)

    def test_primary_accepts_the_current_populated_ui_budget(self):
        test = next(t for t in self.registration['tests'] if t['name'] == 'populated_ui')
        next(p for p in test['properties'] if p['name'] == 'TIMEOUT')['value'] = 180
        self.write_registration()
        code, result = self.run_shard('primary')
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'PASS_PARTIAL_CURRENT_CTEST')
        self.assertEqual(result['ctestValidation']['count'], 39)
        self.assertIn('populated_ui', result['selectedTestGroups'])

    def test_companion_accepts_the_current_populated_ui_budget(self):
        test = next(t for t in self.registration['tests'] if t['name'] == 'populated_ui')
        next(p for p in test['properties'] if p['name'] == 'TIMEOUT')['value'] = 180
        self.write_registration()
        code, result = self.run_shard('companion')
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'PASS_PARTIAL_CURRENT_CTEST')
        self.assertEqual(result['ctestValidation']['count'], 5)
        self.assertNotIn('populated_ui', result['selectedTestGroups'])

    def test_rejects_unapproved_populated_ui_budgets(self):
        for seconds in (119, 121, 179, 181, 0, -1, 120.5, '120', '180', None,
                        True, float('nan'), float('inf')):
            registration = copy.deepcopy(self.registration)
            test = next(t for t in registration['tests'] if t['name'] == 'populated_ui')
            next(p for p in test['properties'] if p['name'] == 'TIMEOUT')['value'] = seconds
            with self.subTest(seconds=seconds), self.assertRaisesRegex(runner.ShardError, 'ALTERED_CTEST_PROPERTIES'):
                runner.collect_identities(registration, self.source, self.build, self.cache)

    def test_all_other_group_budgets_remain_exact(self):
        for name in sorted(runner.EXPECTED_GROUPS - {'populated_ui'}):
            expected = runner.TIMEOUTS[name]
            for seconds in sorted({expected - 1, expected + 1, 120, 180} - {expected}):
                registration = copy.deepcopy(self.registration)
                test = next(t for t in registration['tests'] if t['name'] == name)
                next(p for p in test['properties'] if p['name'] == 'TIMEOUT')['value'] = seconds
                with self.subTest(name=name, seconds=seconds), self.assertRaisesRegex(runner.ShardError, 'ALTERED_CTEST_PROPERTIES'):
                    runner.collect_identities(registration, self.source, self.build, self.cache)

    def test_rejects_linked_and_empty_test_binary(self):
        path = self.build/'auth_tests'
        path.unlink()
        path.symlink_to(self.build/'java_tests')
        with self.assertRaisesRegex(runner.ShardError, 'LINKED_INPUT'):
            runner.collect_identities(self.registration, self.source, self.build, self.cache)
        path.unlink()
        path.write_bytes(b'')
        with self.assertRaisesRegex(runner.ShardError, 'EMPTY_TEST_INPUT'):
            runner.collect_identities(self.registration, self.source, self.build, self.cache)

    def test_system_cmake_symlink_resolves_and_is_still_hashed(self):
        alias = self.tools/'alias'/'cmake'
        alias.parent.mkdir()
        alias.symlink_to(self.cmake)
        changed = copy.deepcopy(self.registration)
        next(t for t in changed['tests'] if t['name'] == 'auth_build_config')['command'][0] = str(alias)
        cache = {**self.cache, 'CMAKE_COMMAND': str(alias)}
        identities = runner.collect_identities(changed, self.source, self.build, cache)
        cmake_case = next(t for t in identities if t['name'] == 'auth_build_config')
        self.assertEqual(cmake_case['files'][0]['path'], str(self.cmake))

    def test_rejects_different_ctest_from_the_builds_cmake(self):
        with self.assertRaisesRegex(runner.ShardError, 'UNEXPECTED_CTEST_TOOL'):
            runner.resolve_ctest(str(self.cmake), self.cache)

    def test_source_snapshot_allows_empty_tracked_files(self):
        (self.source/'empty.txt').write_bytes(b'')
        self.git('add', '.')
        self.git('commit', '-qm', 'Empty tracked fixture')
        snapshot = runner.snapshot_source(self.source)
        self.assertEqual(next(f['bytes'] for f in snapshot['files'] if f['path'] == 'empty.txt'), 0)

    def test_rejects_duplicate_json_keys(self):
        path = self.root/'plan.json'
        path.write_text('{"schemaVersion":1,"schemaVersion":1}')
        with self.assertRaisesRegex(runner.ShardError, 'DUPLICATE_JSON_KEY'):
            runner.read_json_unique(path)

    def test_junit_rejects_bad_counters_status_and_durations(self):
        for attributes in [{'failures': '1'}, {'disabled': '1'}, {'skipped': '1'}, {'tests': '99'}]:
            root = ET.Element('testsuite', tests='1', failures='0', disabled='0', skipped='0')
            ET.SubElement(root, 'testcase', name='auth', status='run', time='1')
            root.attrib.update(attributes)
            path = self.root/'junit.xml'
            ET.ElementTree(root).write(path)
            with self.subTest(attributes=attributes), self.assertRaises(runner.ShardError):
                runner.validate_junit(path, ['auth'])
        for seconds in ['nan', 'inf', '-1']:
            root = ET.Element('testsuite', tests='1')
            ET.SubElement(root, 'testcase', name='auth', status='run', time=seconds)
            path = self.root/'junit.xml'; ET.ElementTree(root).write(path)
            with self.subTest(seconds=seconds), self.assertRaises(runner.ShardError):
                runner.validate_junit(path, ['auth'])

    def test_requires_live_actions_identity(self):
        code, result = self.run_shard(GITHUB_RUN_ATTEMPT='0')
        self.assertEqual(code, 1)
        self.assertEqual(result['failure'], 'INVALID_CI_IDENTITY')

    def test_sensitive_inherited_state_is_not_passed_to_ctest(self):
        code, _ = self.run_shard(MCL_MS_CLIENT_ID='private-fixture', SOURCE_READ_TOKEN='private-fixture',
                                 GH_TOKEN='private-fixture', QT_QPA_PLATFORM='unexpected', QT_QUICK_BACKEND='unexpected')
        self.assertEqual(code, 0)


if __name__ == '__main__':
    unittest.main()
