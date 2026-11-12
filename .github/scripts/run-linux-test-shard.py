#!/usr/bin/env python3
"""Execute one complete-group Linux CTest shard; caller keeps all output private."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET


UI_GROUPS = frozenset({
    'native_ui', 'local_ui', 'populated_ui', 'external_auth_ui', 'winmd_ui',
    'mod_favorites_ui', 'ui_regression', 'crash_notification_ui', 'theme_controls',
})
BACKEND_GROUPS = frozenset('''auth_build_config launch_assessment launch_tuning task_model
public_error operation_runner cli_options host_services launcher_bridge project_logo
version_resolver archive network java managed_runtime state_store legacy_import auth
region_policy content content_transport mod_favorites content_controller pack_service
game_service authlib_injector forge_installer loader_catalog instance_service world
world_service library_controller diagnostics crash_analyzer linux_auth_vault'''.split())
EXPECTED_GROUPS = UI_GROUPS | BACKEND_GROUPS
TIMEOUTS = {name: 180 for name in BACKEND_GROUPS}
TIMEOUTS['telemetry'] = 180
TIMEOUTS.update(auth_build_config=60, linux_auth_vault=120, native_ui=420, local_ui=480,
                populated_ui=120, external_auth_ui=120, winmd_ui=180, mod_favorites_ui=180,
                ui_regression=900, crash_notification_ui=180, theme_controls=120)
SHARDS = ('primary', 'companion')
NAME = re.compile(r'\A[a-z][a-z0-9_]*\Z')
HEX40 = re.compile(r'\A[0-9a-f]{40}\Z')
SANITIZED_KEYS = (
    'SOURCE_REPOSITORY', 'SOURCE_READ_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN',
    'MCL_DATA_ROOT', 'MCL_NATIVE_DATA_ROOT', 'MCL_PORTABLE_DIR', 'MCL_TEST_JAVA',
    'MCL_AUTHLIB_REAL_FETCH', 'MCL_REGION_REAL_PROBE', 'MCL_MS_CLIENT_ID',
    'MCL_MS_REDIRECT_URI', 'MCL_NATIVE_UI_EVIDENCE', 'MCL_EXTERNAL_AUTH_UI_EVIDENCE',
    'MCL_WINMD_UI_EVIDENCE', 'MCL_MOD_FAVORITES_UI_EVIDENCE', 'MCL_UI_REGRESSION_EVIDENCE',
    'MCL_CRASH_UI_EVIDENCE', 'QV4_FORCE_INTERPRETER', 'QML_FORCE_DISK_CACHE',
    'QML_DISK_CACHE', 'QML_DISK_CACHE_PATH',
)


class ShardError(ValueError):
    pass


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True).encode()).hexdigest()


def ordinary(path: Path, *, directory: bool = False) -> Path:
    path = Path(os.path.abspath(path))
    for node in (path, *path.parents):
        info = node.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ShardError('LINKED_INPUT')
    mode = path.stat().st_mode
    if directory != stat.S_ISDIR(mode) or (not directory and not stat.S_ISREG(mode)):
        raise ShardError('INVALID_INPUT_TYPE')
    return path


def file_identity(path: Path, *, system_tool: bool = False, nonempty: bool = True) -> dict:
    # System CMake can be installed through a normal /usr/local/bin symlink.
    # Native test inputs and source files must have ordinary ancestors.
    target = path.resolve(strict=True) if system_tool else path
    target = ordinary(target)
    size = target.stat().st_size
    if nonempty and size <= 0:
        raise ShardError('EMPTY_TEST_INPUT')
    with target.open('rb') as handle:
        sha = hashlib.file_digest(handle, 'sha256').hexdigest()
    return {'path': str(target), 'bytes': size, 'sha256': sha}


def json_write_fresh(path: Path, value: object) -> None:
    ordinary(path.parent, directory=True)
    with path.open('x', encoding='utf-8') as handle:
        os.chmod(path, 0o600)
        json.dump(value, handle, sort_keys=True, indent=2)
        handle.write('\n')


def read_json_unique(path: Path) -> object:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ShardError('DUPLICATE_JSON_KEY')
            result[key] = value
        return result
    if ordinary(path).stat().st_size > 16 * 1024 * 1024:
        raise ShardError('OVERSIZE_JSON')
    return json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=unique)


def extra_backend_groups(plan: object) -> frozenset[str]:
    if not isinstance(plan, dict) or type(plan.get('schemaVersion')) is not int or plan['schemaVersion'] not in (1, 2):
        raise ShardError('INVALID_SHARD_PLAN')
    keys = {'schemaVersion', 'primaryUi', 'companionUi'}
    if plan['schemaVersion'] == 2:
        keys.add('extraBackendGroups')
    if set(plan) != keys or (plan['schemaVersion'] == 2 and plan['extraBackendGroups'] != ['telemetry']):
        raise ShardError('INVALID_SHARD_PLAN')
    lists = [plan['primaryUi'], plan['companionUi']]
    if any(not isinstance(items, list) or any(not isinstance(x, str) or not NAME.fullmatch(x)
                                               for x in items) for items in lists):
        raise ShardError('INVALID_SHARD_PLAN')
    primary, companion = lists
    if (len(primary) != 4 or len(companion) != 5 or len(set(primary + companion)) != 9
            or set(primary + companion) != UI_GROUPS):
        raise ShardError('INVALID_SHARD_PLAN')
    return frozenset({'telemetry'}) if plan['schemaVersion'] == 2 else frozenset()


def select_groups(plan: object, names: list[str], shard: str) -> list[str]:
    extra = extra_backend_groups(plan)
    if shard not in SHARDS or len(names) != len(set(names)) or set(names) != EXPECTED_GROUPS | extra:
        raise ShardError('INCOMPLETE_CTEST_INVENTORY')
    selected = set(plan['primaryUi']) | BACKEND_GROUPS | extra if shard == 'primary' else set(plan['companionUi'])
    return sorted(selected)


def exact_regex(names: list[str]) -> str:
    if not names or len(names) != len(set(names)) or any(not NAME.fullmatch(n) for n in names):
        raise ShardError('INVALID_SELECTED_GROUPS')
    return '^(' + '|'.join(sorted(names)) + ')$'


def read_cache(build: Path) -> dict[str, str]:
    values = {}
    for line in ordinary(build / 'CMakeCache.txt').read_text().splitlines():
        if not line or line.startswith(('#', '//')):
            continue
        match = re.fullmatch(r'([^:=]+):[^=]+=(.*)', line)
        if match:
            if match[1] in values:
                raise ShardError('DUPLICATE_CMAKE_CACHE_KEY')
            values[match[1]] = match[2]
    return values


def check_configuration(cache: dict[str, str], source: Path, build: Path) -> None:
    expected = {'CMAKE_BUILD_TYPE': 'Release', 'BUILD_TESTING': 'ON', 'MCL_CORE_ONLY': 'OFF',
                'MCL_STATIC_RELEASE': 'OFF', 'MCL_DISTRIBUTION_BUILD': 'OFF',
                'MCL_SERIALIZE_SOFTWARE_UI': 'ON', 'MCL_TEST_QML_CACHE': 'ON',
                'CMAKE_GENERATOR': 'Ninja'}
    if any(cache.get(key) != value for key, value in expected.items()):
        raise ShardError('INVALID_CTEST_CONFIGURATION')
    if (not cache.get('CMAKE_HOME_DIRECTORY') or not cache.get('CMAKE_CACHEFILE_DIR')
            or Path(cache.get('CMAKE_HOME_DIRECTORY', '')).absolute() != source / 'qt-native'
            or Path(cache.get('CMAKE_CACHEFILE_DIR', '')).absolute() != build):
        raise ShardError('UNEXPECTED_BUILD_SOURCE')


def properties(test: dict) -> dict:
    result = {}
    for item in test.get('properties', []):
        if not isinstance(item, dict) or set(item) != {'name', 'value'} or item['name'] in result:
            raise ShardError('INVALID_CTEST_PROPERTIES')
        result[item['name']] = item['value']
    return result


def collect_identities(registration: object, source: Path, build: Path, cache: dict, plan=None) -> list[dict]:
    if not isinstance(registration, dict) or not isinstance(registration.get('tests'), list):
        raise ShardError('INVALID_CTEST_REGISTRATION')
    tests = registration['tests']
    expected = EXPECTED_GROUPS | (extra_backend_groups(plan) if plan is not None else frozenset())
    if len(tests) != len(expected) or {t.get('name') for t in tests} != expected:
        raise ShardError('INCOMPLETE_CTEST_INVENTORY')
    result = []
    for test in tests:
        name = test['name']
        command = test.get('command')
        if not isinstance(command, list) or any(not isinstance(arg, str) for arg in command):
            raise ShardError('INVALID_CTEST_COMMAND')
        if name == 'auth_build_config':
            cmake = cache.get('CMAKE_COMMAND', '')
            script = source / 'qt-native/tests/cmake/auth_build_config_tests.cmake'
            if command != [cmake, '-P', str(script)] or Path(cmake).name != 'cmake':
                raise ShardError('FILTERED_CTEST_COMMAND')
            kind = 'cmake-script'
            files = [file_identity(Path(cmake), system_tool=True), file_identity(script)]
        elif name == 'linux_auth_vault':
            dbus = cache.get('MCL_DBUS_RUN_SESSION', '')
            executable = build / (name + '_tests')
            if command != [dbus, '--', str(executable)] or Path(dbus).name != 'dbus-run-session':
                raise ShardError('FILTERED_CTEST_COMMAND')
            kind = 'dbus-native-executable'
            files = [file_identity(Path(dbus), system_tool=True), file_identity(executable)]
        else:
            executable = build / (name + '_tests')
            if command != [str(executable)]:
                raise ShardError('FILTERED_CTEST_COMMAND')
            kind = 'native-executable'
            files = [file_identity(executable)]
        props = properties(test)
        # Existing release tags use 120s; the populated-shell matrix with real
        # theme-transition waits has a reviewed 180s budget. Keep every other
        # group's timeout exact. The proof still binds the actual registration.
        allowed_timeouts = (120, 180) if name == 'populated_ui' else (TIMEOUTS[name],)
        if (props.get('TIMEOUT') not in allowed_timeouts
                or props.get('WORKING_DIRECTORY') != str(build)):
            raise ShardError('ALTERED_CTEST_PROPERTIES')
        if name == 'linux_auth_vault' and props.get('ENVIRONMENT') != ['MCL_OWNED_KEYRING_TEST_BUS=1']:
            raise ShardError('UNOWNED_KEYRING_TEST_SESSION')
        if name in UI_GROUPS:
            environment = props.get('ENVIRONMENT', [])
            entries = {}
            for entry in environment:
                if not isinstance(entry, str) or '=' not in entry:
                    raise ShardError('INVALID_QML_CACHE_ENVIRONMENT')
                key, value = entry.split('=', 1)
                if key in entries:
                    raise ShardError('INVALID_QML_CACHE_ENVIRONMENT')
                entries[key] = value
            enabled = name != 'theme_controls'
            expected_env = {'QML_DISABLE_DISK_CACHE': '0' if enabled else '1', 'QML_FORCE_DISK_CACHE': '0',
                            'QML_DISK_CACHE': '', 'QML_DISK_CACHE_PATH': str(build/'ci-tmp/qml-cache'/name) if enabled else ''}
            if any(entries.get(key) != value for key, value in expected_env.items()):
                raise ShardError('INVALID_QML_CACHE_ENVIRONMENT')
            if enabled and (props.get('RESOURCE_LOCK') != ['mcl_software_renderer']
                            or entries.get('QT_QPA_PLATFORM') != 'offscreen'):
                raise ShardError('UNSERIALIZED_SOFTWARE_UI')
            if not enabled and props.get('RUN_SERIAL') is not True:
                raise ShardError('UNSERIALIZED_NATIVE_UI')
        result.append({'name': name, 'kind': kind, 'command': command, 'files': files})
    return sorted(result, key=lambda t: t['name'])


def snapshot_source(source: Path) -> dict:
    def git(*args):
        return subprocess.check_output(['git', '-C', str(source), *args], timeout=60)
    commit = git('rev-parse', '--verify', 'HEAD').decode().strip()
    tree = git('rev-parse', '--verify', 'HEAD^{tree}').decode().strip()
    if not HEX40.fullmatch(commit) or not HEX40.fullmatch(tree):
        raise ShardError('INVALID_SOURCE_IDENTITY')
    if git('status', '--porcelain=v1', '--untracked-files=normal'):
        raise ShardError('DIRTY_SOURCE_INPUT')
    files = []
    for raw in git('ls-files', '-z', '--cached').split(b'\0'):
        if not raw:
            continue
        name = os.fsdecode(raw)
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ShardError('INVALID_TRACKED_SOURCE_PATH')
        identity = file_identity(source / relative, nonempty=False)
        identity['path'] = name
        files.append(identity)
    return {'commit': commit, 'tree': tree, 'files': files}


def validate_junit(path: Path, selected: list[str]) -> list[dict]:
    if ordinary(path).stat().st_size > 16 * 1024 * 1024:
        raise ShardError('OVERSIZE_JUNIT')
    root = ET.parse(path).getroot()
    if root.tag != 'testsuite':
        raise ShardError('INVALID_CTEST_JUNIT')
    for key in ('failures', 'disabled', 'skipped', 'errors'):
        if root.get(key, '0') != '0':
            raise ShardError('CTEST_GROUP_DID_NOT_PASS')
    cases = list(root.iter('testcase'))
    names = [case.get('name') for case in cases]
    if len(names) != len(set(names)) or set(names) != set(selected):
        raise ShardError('INCOMPLETE_EXECUTED_GROUPS')
    if any(case.get('status') != 'run' or any(case.find(tag) is not None for tag in ('failure', 'error', 'skipped'))
           for case in cases):
        raise ShardError('CTEST_GROUP_DID_NOT_PASS')
    result = []
    for case in cases:
        seconds = float(case.get('time', '-1'))
        if not math.isfinite(seconds) or seconds < 0:
            raise ShardError('INVALID_CTEST_DURATION')
        result.append({'name': case.get('name'), 'status': 'run', 'time': seconds})
    if root.get('tests') != str(len(cases)):
        raise ShardError('INVALID_CTEST_JUNIT_COUNT')
    return result


def discover(ctest: str, build: Path, env: dict[str, str]) -> dict:
    data = subprocess.check_output([ctest, '--test-dir', str(build), '--show-only=json-v1'], env=env, timeout=60)
    if len(data) > 16 * 1024 * 1024:
        raise ShardError('OVERSIZE_CTEST_REGISTRATION')
    return json.loads(data)


def resolve_ctest(requested: str, cache: dict[str, str]) -> str:
    found = shutil.which(requested)
    expected = cache.get('CMAKE_CTEST_COMMAND', '')
    if (not found or not expected or Path(expected).name != 'ctest'
            or Path(found).resolve(strict=True) != Path(expected).resolve(strict=True)):
        raise ShardError('UNEXPECTED_CTEST_TOOL')
    return str(ordinary(Path(expected).resolve(strict=True)))


def initialize_caches(build: Path) -> None:
    root = build / 'ci-tmp/qml-cache'
    ordinary(root.parent, directory=True) if root.parent.exists() else None
    if root.exists():
        ordinary(root, directory=True)
        if any(root.iterdir()):
            raise ShardError('PREVIOUS_QML_CACHE_RESTORED')
    root.mkdir(parents=True, exist_ok=True)
    ordinary(root, directory=True)
    for name in UI_GROUPS - {'theme_controls'}:
        (root/name).mkdir()


def positive_ci_integer(name: str) -> int:
    value = os.environ.get(name, '')
    if not re.fullmatch(r'[1-9][0-9]*', value):
        raise ShardError('INVALID_CI_IDENTITY')
    return int(value)


def execute(args: argparse.Namespace) -> int:
    source = ordinary(args.source_root, directory=True)
    build = ordinary(args.build_dir, directory=True)
    if build.is_relative_to(source):
        raise ShardError('BUILD_INSIDE_SOURCE_CHECKOUT')
    result_path = Path(os.path.abspath(args.result_json))
    junit = Path(os.path.abspath(args.junit))
    registration_path = result_path.with_name('registration.json')
    for output in (result_path, junit, registration_path):
        if output.is_relative_to(source):
            raise ShardError('OUTPUT_INSIDE_SOURCE_CHECKOUT')
        ordinary(output.parent, directory=True)
        if output.exists() or output.is_symlink():
            raise ShardError('PREVIOUS_RESULT_REUSED')
    if len({result_path, junit, registration_path}) != 3:
        raise ShardError('OVERLAPPING_OUTPUT_PATHS')
    state = {'schemaVersion': 1, 'platform': 'linux-x86_64', 'status': 'FAILED', 'fullSuite': False,
             'testShard': args.shard, 'expectedTestGroups': sorted(EXPECTED_GROUPS), 'selectedTestGroups': [],
             'tests': [], 'sourceCommit': None, 'sourceTree': None, 'sourceSnapshotSha256': None,
             'sourceStable': False, 'testInputsStable': False, 'previousResultsReused': False,
             'registrationPath': str(registration_path), 'ctestJunit': str(junit), 'ctestExitCode': None}
    error = None
    initial = None
    before = None
    registration = None
    ctest = None
    env = dict(os.environ)
    for key in SANITIZED_KEYS:
        env.pop(key, None)
    env.update(QT_QPA_PLATFORM='offscreen', QT_QUICK_BACKEND='software',
               QML_DISABLE_DISK_CACHE='1', QT_FORCE_STDERR_LOGGING='1')
    state['environmentPolicy'] = {'softwareRendering': True, 'qpa': 'offscreen',
                                  'coldNativeThemeControls': True, 'perSuiteCacheOnly': True}
    try:
        if sys.platform != 'linux' or os.environ.get('GITHUB_ACTIONS') != 'true':
            raise ShardError('LINUX_ACTIONS_RUNNER_REQUIRED')
        state.update(runId=positive_ci_integer('GITHUB_RUN_ID'), runAttempt=positive_ci_integer('GITHUB_RUN_ATTEMPT'))
        initial = snapshot_source(source)
        state.update(sourceCommit=initial['commit'], sourceTree=initial['tree'], sourceSnapshotSha256=digest(initial))
        cache = read_cache(build)
        check_configuration(cache, source, build)
        plan = read_json_unique(source/'qt-native/tests/ci/ui-shard-plan.json')
        state['expectedTestGroups'] = sorted(EXPECTED_GROUPS | extra_backend_groups(plan))
        ctest = resolve_ctest(args.ctest, cache)
        state['ctestTool'] = file_identity(Path(ctest), system_tool=True)
        if (build/'Testing/Temporary/LastTest.log').exists():
            raise ShardError('PREVIOUS_TEST_RESULTS_PRESENT')
        registration = discover(ctest, build, env)
        before = collect_identities(registration, source, build, cache, plan)
        json_write_fresh(registration_path, registration)
        state.update(tests=before, registrationSha256=digest(registration))
        selected = select_groups(plan, [t['name'] for t in before], args.shard)
        state['selectedTestGroups'] = selected
        initialize_caches(build)
        env.update(TEMP=str(build/'ci-tmp'), TMP=str(build/'ci-tmp'), TMPDIR=str(build/'ci-tmp'))
        command = [ctest, '--test-dir', str(build), '--output-on-failure', '--no-tests=error',
                   '--stop-on-failure', '--parallel', '2', '--output-junit', str(junit), '-R', exact_regex(selected)]
        run = subprocess.run(command, env=env)
        state['ctestExitCode'] = run.returncode
        if run.returncode:
            raise ShardError('CTEST_SHARD_FAILED')
        state['executedTestGroups'] = validate_junit(junit, selected)
        state['ctestValidation'] = {'count': len(selected), 'failures': 0, 'disabled': 0, 'skipped': 0,
            'passedTestGroups': sorted(selected), 'summedGroupSeconds': math.fsum(t['time'] for t in state['executedTestGroups'])}
    except Exception as exc:
        error = exc
    finally:
        if initial is not None:
            try:
                state['sourceStable'] = snapshot_source(source) == initial
                if not state['sourceStable']:
                    raise ShardError('SOURCE_INPUT_CHANGED')
            except Exception as exc:
                error = error or exc
        if before is not None and registration is not None:
            try:
                after_registration = discover(ctest, build, env)
                state['testInputsStable'] = (after_registration == registration
                    and collect_identities(after_registration, source, build, cache, plan) == before
                    and file_identity(Path(ctest), system_tool=True) == state['ctestTool'])
                if not state['testInputsStable']:
                    raise ShardError('TEST_INPUT_CHANGED')
            except Exception as exc:
                error = error or exc
        if error is None:
            state['status'] = 'PASS_PARTIAL_CURRENT_CTEST'
        else:
            state['failure'] = str(error)
        json_write_fresh(result_path, state)
    if error:
        print('Linux CTest shard failed; private diagnostics retained.', file=sys.stderr)
        return 1
    print('Linux CTest shard passed; combined validation is still required.')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--build-dir', type=Path, required=True)
    parser.add_argument('--shard', choices=SHARDS, required=True)
    parser.add_argument('--junit', type=Path, required=True)
    parser.add_argument('--result-json', type=Path, required=True)
    parser.add_argument('--ctest', default='ctest')
    args = parser.parse_args()
    try:
        return execute(args)
    except Exception:
        print('Linux CTest shard setup failed; no validation proof was created.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
