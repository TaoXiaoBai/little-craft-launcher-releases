#!/usr/bin/env python3
"""Bind complete, passing CTest shards to the current workflow attempt.

Public proofs contain only fixed labels, hashes, counts and Actions identities.
Compiler/test output and subprocess diagnostics never go to public output.
"""

import argparse
import datetime as dt
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import zipfile
import xml.etree.ElementTree as ET


UI = frozenset(('native_ui', 'local_ui', 'populated_ui', 'external_auth_ui', 'winmd_ui',
                'mod_favorites_ui', 'ui_regression', 'crash_notification_ui', 'theme_controls'))
BACKEND = frozenset('''auth_build_config launch_assessment launch_tuning task_model
public_error operation_runner cli_options host_services launcher_bridge project_logo
version_resolver archive network java managed_runtime state_store legacy_import auth
region_policy content content_transport mod_favorites content_controller pack_service
game_service authlib_injector forge_installer loader_catalog instance_service world
world_service library_controller diagnostics crash_analyzer'''.split())
PLATFORMS = {
    'windows-x64': (43, 'windows', 'Test windows-x64 companion UI'),
    'linux-x86_64': (44, 'linux', 'Test linux-x86_64 companion UI'),
}
WORKFLOW = '.github/workflows/prerelease.yml'
MAX_PROOF = 64 * 1024
MAX_ZIP = 128 * 1024
PROOF_KEYS = frozenset(('schemaVersion', 'kind', 'repositorySha256', 'workflowCommit', 'runId',
    'runAttempt', 'platform', 'shard', 'sourceCommit', 'sourceTree', 'sourceInputsSha256',
    'planSha256', 'registrationSha256', 'sdkSha256', 'compilerSha256', 'configSha256',
    'fullGroupIds', 'primaryGroupIds', 'companionGroupIds', 'selectedPassedGroupIds',
    'fullGroupCount', 'selectedGroupCount', 'fullSuite', 'failed', 'skipped'))
IDENTITY_KEYS = ('repositorySha256', 'workflowCommit', 'runId', 'runAttempt', 'platform',
    'sourceCommit', 'sourceTree', 'sourceInputsSha256', 'planSha256', 'registrationSha256',
    'sdkSha256', 'compilerSha256', 'configSha256', 'fullGroupIds', 'primaryGroupIds', 'companionGroupIds')
PRIVATE_LOG = None


class ProofError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def require(condition, code):
    if not condition:
        raise ProofError(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def hex_value(value, length):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{%d}' % length, value) is not None


def positive(value):
    require(type(value) is int and value > 0, 'INVALID_ACTIONS_IDENTITY')
    return value


def private_log(data):
    if PRIVATE_LOG is not None:
        PRIVATE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with PRIVATE_LOG.open('ab') as output:
            output.write(data + b'\n')


def run_command(arguments, *, binary=False):
    try:
        result = subprocess.run(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as failure:
        private_log(repr(failure).encode())
        raise ProofError('SUBPROCESS_UNAVAILABLE') from None
    private_log(result.stderr)
    require(result.returncode == 0, 'SUBPROCESS_FAILED')
    return result.stdout if binary else result.stdout.decode('utf-8', errors='strict')


def ordinary(path, *, resolve=False):
    path = Path(path).absolute()
    if resolve:
        path = path.resolve(strict=True)
    for node in (path, *path.parents):
        info = node.lstat()
        require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400,
                'LINKED_INPUT')
    require(path.is_file(), 'INVALID_INPUT_FILE')
    return path


def file_hash(path, *, resolve=False):
    path = ordinary(path, resolve=resolve)
    hasher = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(block)
    return hasher.hexdigest()


def json_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'DUPLICATE_JSON_KEY')
        result[key] = value
    return result


def parse_json(data, limit=MAX_PROOF):
    require(len(data) <= limit, 'JSON_TOO_LARGE')
    try:
        return json.loads(data.decode('utf-8-sig'), object_pairs_hook=json_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ProofError('INVALID_JSON_NUMBER')))
    except (UnicodeError, json.JSONDecodeError):
        raise ProofError('INVALID_JSON') from None


def read_json(path, limit=MAX_PROOF):
    return parse_json(ordinary(path).read_bytes(), limit)


def write_json(path, value):
    data = canonical(value) + b'\n'
    require(len(data) <= MAX_PROOF, 'PROOF_TOO_LARGE')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(data)


def group_names(values):
    require(isinstance(values, list) and len(values) == len(set(values)), 'INVALID_GROUP_SET')
    require(all(isinstance(n, str) and re.fullmatch('[a-z][a-z0-9_]{0,80}', n) for n in values), 'INVALID_GROUP_SET')
    return set(values)


def ids(names):
    return sorted(hashlib.sha256(name.encode()).hexdigest() for name in names)


def partition(plan, discovered):
    version = validate_plan(plan)
    first, second = group_names(plan['primaryUi']), group_names(plan['companionUi'])
    require(first and second and not first & second and first | second == UI and UI <= discovered,
            'INCOMPLETE_UI_PARTITION')
    if version == 2:
        require(discovered in (inventory('windows-x64'), inventory('linux-x86_64')),
                'INCOMPLETE_REGISTRATION')
    return {'primary': (discovered - UI) | first, 'companion': second}


def validate_plan(plan):
    require(isinstance(plan, dict) and type(plan.get('schemaVersion')) is int and
            plan['schemaVersion'] in (1, 2), 'INVALID_PARTITION_PLAN')
    version = plan['schemaVersion']
    keys = {'schemaVersion', 'primaryUi', 'companionUi'}
    if version == 2:
        keys.add('extraBackendGroups')
    require(set(plan) == keys and (version == 1 or plan['extraBackendGroups'] == ['telemetry']),
            'INVALID_PARTITION_PLAN')
    first, second = group_names(plan['primaryUi']), group_names(plan['companionUi'])
    require(first and second and not first & second and first | second == UI and
            (version == 1 or (len(first) == 4 and len(second) == 5)), 'INCOMPLETE_UI_PARTITION')
    return version


def inventory(platform):
    return UI | BACKEND | {'telemetry'} | ({'linux_auth_vault'} if platform == 'linux-x86_64' else set())


def validate_junit(path, selected):
    data = ordinary(path).read_bytes()
    require(len(data) <= 32 * 1024 * 1024 and b'<!DOCTYPE' not in data and b'<!ENTITY' not in data,
            'INVALID_JUNIT')
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise ProofError('INVALID_JUNIT') from None
    require(root.tag in ('testsuite', 'testsuites'), 'INVALID_JUNIT')
    for suite in root.iter('testsuite'):
        for key in ('failures', 'errors', 'disabled', 'skipped'):
            require(re.fullmatch(r'0+', suite.get(key, '0')) is not None, 'JUNIT_NOT_ALL_PASS')
    cases = list(root.iter('testcase'))
    names = [case.get('name') for case in cases]
    require(len(names) == len(set(names)) and set(names) == selected, 'JUNIT_GROUP_MISMATCH')
    if root.get('tests') is not None:
        require(root.get('tests') == str(len(cases)), 'JUNIT_COUNT_MISMATCH')
    for case in cases:
        require(case.get('status') == 'run' and not any(case.find(tag) is not None for tag in ('failure', 'error', 'skipped')),
                'JUNIT_NOT_ALL_PASS')
        elapsed = float(case.get('time', '0'))
        require(math.isfinite(elapsed) and elapsed >= 0, 'INVALID_JUNIT_TIME')


def cache_values(path):
    result = {}
    for line in ordinary(path).read_text(encoding='utf-8-sig').splitlines():
        match = re.fullmatch(r'([^/#][^:]*):[^=]+=(.*)', line)
        if match:
            require(match[1] not in result, 'DUPLICATE_CMAKE_CACHE_KEY')
            result[match[1]] = match[2]
    return result


def normalizer(source, build, sdk):
    replacements = [(str(Path(p).absolute()).replace('\\', '/'), label) for p, label in
                    ((source, '<SOURCE>'), (build, '<BUILD>'), (sdk, '<SDK>'))]
    for key, label in (('RUNNER_TEMP', '<TEMP>'), ('GITHUB_WORKSPACE', '<WORKSPACE>')):
        if os.environ.get(key):
            replacements.append((os.environ[key].replace('\\', '/'), label))
    replacements.sort(key=lambda pair: len(pair[0]), reverse=True)

    def normalize(value):
        if isinstance(value, str):
            value = value.replace('\\', '/')
            for prefix, label in replacements:
                value = re.sub(re.escape(prefix), lambda _: label, value, flags=re.IGNORECASE if os.name == 'nt' else 0)
            return value
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        return value
    return normalize


def source_identity(source):
    source = Path(source).absolute()
    commit = run_command(['git', '-C', str(source), 'rev-parse', '--verify', 'HEAD']).strip()
    tree = run_command(['git', '-C', str(source), 'rev-parse', '--verify', 'HEAD^{tree}']).strip()
    require(hex_value(commit, 40) and hex_value(tree, 40), 'INVALID_SOURCE_IDENTITY')
    run_command(['git', '-C', str(source), 'diff', '--quiet', 'HEAD', '--'])
    files = run_command(['git', '-C', str(source), 'ls-files', '-z'], binary=True).decode('utf-8').split('\0')
    names = {name for name in files if name}
    # Include untracked/ignored inputs inside the actual source trees too.
    # Build trees and the public SDK live outside these roots and are excluded.
    for relative_root in ('src', 'tests', 'cmake', 'scripts', 'third_party'):
        root = source / 'qt-native' / relative_root
        if not root.exists():
            continue
        for directory, children, entries in os.walk(root, followlinks=False):
            for name in children + entries:
                path = Path(directory) / name
                info = path.lstat()
                require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400,
                        'LINKED_SOURCE_INPUT')
                if path.is_file():
                    names.add(path.relative_to(source).as_posix())
    require(len(names) <= 30000, 'SOURCE_INPUT_LIMIT')
    records = []
    for name in sorted(names):
        relative = Path(name)
        require(not relative.is_absolute() and '..' not in relative.parts, 'INVALID_SOURCE_INPUT')
        records.append([name, file_hash(source / relative)])
    require(records, 'EMPTY_SOURCE_INPUTS')
    return commit, tree, digest(records)


def build_identity(source, build, sdk, cache, registration, plan_schema=1):
    normalize = normalizer(source, build, sdk)
    require(Path(cache.get('CMAKE_PREFIX_PATH', '')).resolve() == Path(sdk).resolve(), 'SDK_PREFIX_MISMATCH')
    expected = {'CMAKE_BUILD_TYPE': 'Release', 'BUILD_TESTING': 'ON', 'MCL_STATIC_RELEASE': 'OFF', 'MCL_CORE_ONLY': 'OFF'}
    require(all(cache.get(key) == value for key, value in expected.items()), 'BUILD_CONFIGURATION_MISMATCH')
    tools = {}
    for key in ('CMAKE_C_COMPILER', 'CMAKE_CXX_COMPILER', 'CMAKE_MAKE_PROGRAM', 'CMAKE_COMMAND'):
        require(bool(cache.get(key)), 'COMPILER_IDENTITY_MISSING')
        tools[key] = file_hash(cache[key], resolve=True)
    version = Path(sdk) / 'lib/cmake/Qt6Core/Qt6CoreConfigVersionImpl.cmake'
    require('set(PACKAGE_VERSION "6.8.3")' in ordinary(version).read_text(), 'SDK_VERSION_MISMATCH')
    sdk_files = ['include/QtCore/qconfig.h', 'lib/cmake/Qt6Core/Qt6CoreConfigVersionImpl.cmake']
    modules = ('Core', 'Gui', 'Network', 'Qml', 'Quick', 'QuickControls2', 'Test')
    if os.name == 'nt':
        sdk_files += ['bin/Qt6' + name + '.dll' for name in modules]
        sdk_files += ['bin/' + name + '.exe' for name in ('qmake', 'moc', 'rcc', 'qmlcachegen', 'qmlimportscanner')]
    else:
        sdk_files += ['lib/libQt6' + name + '.so.6.8.3' for name in modules + ('DBus',)]
        sdk_files += ['bin/qmake'] + ['libexec/' + name for name in ('moc', 'rcc', 'qmlcachegen', 'qmlimportscanner')]
    sdk_hash = digest({name: file_hash(Path(sdk) / name, resolve=True) for name in sdk_files})
    flags = {key: value for key, value in cache.items() if re.fullmatch(
        r'(MCL_.*|BUILD_TESTING|CMAKE_BUILD_TYPE|CMAKE_GENERATOR|CMAKE_(C|CXX|EXE_LINKER|SHARED_LINKER|MODULE_LINKER|STATIC_LINKER)_FLAGS.*|CMAKE_AUTOGEN_.*|CMAKE_NINJA_FORCE_RESPONSE_FILE|CMAKE_FIND_USE_.*REGISTRY)', key)}
    # CTest may update scheduling costs after running only a subset. Those
    # measurements are not execution inputs and must not differ by shard.
    registered = [{'name': item['name'], 'command': item['command'],
                   'properties': [p for p in item.get('properties', []) if p.get('name') != 'COST']}
                  for item in sorted(registration['tests'], key=lambda item: item['name'])]
    config = normalize(flags)
    if plan_schema == 2:
        # The cache can leave the build label empty while CMake resolves a Git
        # tag/SHA. Bind the effective compiled endpoint and label as well.
        header = Path(build) / 'generated/telemetry_build_config.h'
        require(header.is_file(), 'TELEMETRY_CONFIGURATION_MISSING')
        config = {'cache': config, 'generatedHeaders': {'generated/telemetry_build_config.h': file_hash(header)}}
    return sdk_hash, digest(tools), digest(config), digest(normalize(registered))


def validate_driver(driver, source, build, commit, tree, platform, shard, full, selected, registration, run_id, attempt):
    require(driver.get('status') == 'PASS_PARTIAL_CURRENT_CTEST' and driver.get('fullSuite') is False
            and driver.get('testShard') == shard and driver.get('previousResultsReused') is False,
            'DRIVER_NOT_CURRENT_PARTIAL_PASS')
    require(str(driver.get('runId')) == str(run_id) and str(driver.get('runAttempt')) == str(attempt),
            'DRIVER_ATTEMPT_MISMATCH_RERUN_ALL_REQUIRED')
    require(driver.get('commit', driver.get('sourceCommit')) == commit, 'DRIVER_SOURCE_MISMATCH')
    if driver.get('sourceTree') is not None:
        require(driver['sourceTree'] == tree, 'DRIVER_SOURCE_MISMATCH')
    require(group_names(driver.get('expectedTestGroups')) == full and group_names(driver.get('selectedTestGroups')) == selected,
            'DRIVER_GROUP_MISMATCH')
    validation = driver.get('ctestValidation', {})
    require(type(validation.get('count')) is int and validation['count'] == len(selected) and
            all(type(validation.get(k)) is int and validation[k] == 0 for k in ('failures', 'disabled', 'skipped'))
            and group_names(validation.get('passedTestGroups')) == selected, 'DRIVER_JUNIT_MISMATCH')
    if platform == 'windows-x64':
        require(driver.get('schemaVersion') == 2 and driver.get('mode') == 'dynamic' and driver.get('uiSuitesExecuted') is False,
                'INVALID_WINDOWS_DRIVER')
        snapshot = driver.get('sourceSnapshot')
        require(isinstance(snapshot, list) and snapshot, 'SOURCE_SNAPSHOT_MISSING')
        for record in snapshot:
            relative = Path(record['path'])
            require(not relative.is_absolute() and '..' not in relative.parts, 'INVALID_SOURCE_SNAPSHOT')
            path = Path(source) / 'qt-native' / relative
            require(path.stat().st_size == record['bytes'] and file_hash(path) == record['sha256'], 'SOURCE_SNAPSHOT_CHANGED')
    else:
        require(driver.get('schemaVersion') == 1 and driver.get('platform') == platform and
                driver.get('sourceStable') is True and driver.get('testInputsStable') is True
                and driver.get('ctestExitCode') == 0, 'INVALID_LINUX_DRIVER')
    records = driver.get('tests')
    require(isinstance(records, list) and len(records) == len(full), 'TEST_IDENTITIES_MISSING')
    require(group_names([record.get('name') for record in records]) == full, 'TEST_IDENTITY_GROUP_MISMATCH')
    commands = {item['name']: item['command'] for item in registration['tests']}
    for record in records:
        command = commands[record['name']]
        require(record.get('command') == command, 'TEST_COMMAND_CHANGED')
        if record['name'] == 'auth_build_config':
            require(len(command) == 3 and command[1] == '-P' and Path(command[2]).resolve() ==
                    (Path(source) / 'qt-native/tests/cmake/auth_build_config_tests.cmake').resolve(), 'INVALID_TEST_COMMAND')
            paths = [command[0], command[2]]
            ordinary(command[2])
        elif record['name'] == 'linux_auth_vault':
            require(platform == 'linux-x86_64' and len(command) == 3 and command[1] == '--'
                    and Path(command[0]).name == 'dbus-run-session' and Path(command[2]).resolve() ==
                    (Path(build) / 'linux_auth_vault_tests').resolve(), 'INVALID_TEST_COMMAND')
            paths = [command[0], command[2]]
            ordinary(command[2])
        else:
            suffix = '.exe' if platform == 'windows-x64' else ''
            require(len(command) == 1 and Path(command[0]).resolve() ==
                    (Path(build) / (record['name'] + '_tests' + suffix)).resolve(), 'INVALID_TEST_COMMAND')
            paths = command
            ordinary(command[0])
        files = record.get('files', [])
        require(len(files) == len(paths) and {Path(f['path']).resolve() for f in files} ==
                {Path(p).resolve() for p in paths}, 'TEST_INPUT_SET_MISMATCH')
        for file in files:
            path = ordinary(file['path'], resolve=True)
            require(path.stat().st_size == file['bytes'] and file_hash(path) == file['sha256'], 'TEST_INPUT_CHANGED')


def actions_identity(environment):
    repo = environment.get('GITHUB_REPOSITORY', '')
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+', repo)
            and repo.split('/')[1] not in ('.', '..'), 'INVALID_PUBLIC_REPOSITORY')
    sha = environment.get('GITHUB_SHA', '')
    require(hex_value(sha, 40), 'INVALID_WORKFLOW_COMMIT')
    run_id = positive(int(environment.get('GITHUB_RUN_ID', '0')))
    attempt = positive(int(environment.get('GITHUB_RUN_ATTEMPT', '0')))
    return repo, sha, run_id, attempt


def create_proof(args, environment=os.environ):
    repo, workflow_sha, run_id, attempt = actions_identity(environment)
    require(args.platform in PLATFORMS and args.shard in ('primary', 'companion'), 'INVALID_SHARD')
    require((args.platform == 'windows-x64') == (os.name == 'nt'), 'PLATFORM_HOST_MISMATCH')
    source, build, sdk = map(lambda value: Path(value).absolute(), (args.source_root, args.build_dir, args.sdk_root))
    commit, tree, source_sha = source_identity(source)
    plan = read_json(source / 'qt-native/tests/ci/ui-shard-plan.json')
    plan_schema = validate_plan(plan)
    cache = cache_values(build / 'CMakeCache.txt')
    ctest = cache.get('CMAKE_CTEST_COMMAND')
    if not ctest:
        ctest = str(Path(cache['CMAKE_COMMAND']).with_name('ctest.exe' if args.platform == 'windows-x64' else 'ctest'))
    registration = parse_json(run_command([ctest, '--test-dir', str(build), '--show-only=json-v1'], binary=True), 16 * 1024 * 1024)
    require(registration.get('kind') == 'ctestInfo' and registration.get('version', {}).get('major') == 1,
            'INVALID_CTEST_REGISTRATION')
    full = group_names([item['name'] for item in registration.get('tests', [])])
    require(len(full) == PLATFORMS[args.platform][0] + (plan_schema == 2) and
            (plan_schema == 1 or full == inventory(args.platform)), 'INCOMPLETE_REGISTRATION')
    parts = partition(plan, full)
    selected = parts[args.shard]
    driver = read_json(args.driver_result, 32 * 1024 * 1024)
    validate_driver(driver, source, build, commit, tree, args.platform, args.shard, full, selected, registration, run_id, attempt)
    validate_junit(args.junit, selected)
    sdk_sha, compiler_sha, config_sha, registration_sha = build_identity(source, build, sdk, cache, registration, plan_schema)
    require(source_identity(source) == (commit, tree, source_sha), 'SOURCE_CHANGED_DURING_PROOF')
    proof = dict(schemaVersion=plan_schema, kind='LCL_CTEST_SHARD_PROOF', repositorySha256=digest(repo.lower()),
        workflowCommit=workflow_sha, runId=run_id, runAttempt=attempt, platform=args.platform, shard=args.shard,
        sourceCommit=commit, sourceTree=tree, sourceInputsSha256=source_sha, planSha256=digest(plan),
        registrationSha256=registration_sha, sdkSha256=sdk_sha, compilerSha256=compiler_sha, configSha256=config_sha,
        fullGroupIds=ids(full), primaryGroupIds=ids(parts['primary']), companionGroupIds=ids(parts['companion']),
        selectedPassedGroupIds=ids(selected), fullGroupCount=len(full), selectedGroupCount=len(selected),
        fullSuite=False, failed=0, skipped=0)
    validate_proof(proof)
    return proof


def validate_proof(proof):
    require(isinstance(proof, dict) and set(proof) == PROOF_KEYS, 'INVALID_PROOF_SCHEMA')
    require(type(proof['schemaVersion']) is int and proof['schemaVersion'] in (1, 2) and proof['kind'] == 'LCL_CTEST_SHARD_PROOF'
            and proof['platform'] in PLATFORMS and proof['shard'] in ('primary', 'companion')
            and proof['fullSuite'] is False and type(proof['failed']) is int and proof['failed'] == 0
            and type(proof['skipped']) is int and proof['skipped'] == 0, 'PROOF_NOT_ALL_PASS')
    positive(proof['runId']); positive(proof['runAttempt'])
    for key in ('sourceCommit', 'sourceTree', 'workflowCommit'):
        require(hex_value(proof[key], 40), 'INVALID_PROOF_IDENTITY')
    for key in ('repositorySha256', 'sourceInputsSha256', 'planSha256', 'registrationSha256', 'sdkSha256', 'compilerSha256', 'configSha256'):
        require(hex_value(proof[key], 64), 'INVALID_PROOF_IDENTITY')
    for key in ('fullGroupIds', 'primaryGroupIds', 'companionGroupIds', 'selectedPassedGroupIds'):
        values = proof[key]
        require(isinstance(values, list) and all(hex_value(value, 64) for value in values)
                and values == sorted(set(values)), 'INVALID_PROOF_GROUP_SET')
    full, first, second = map(lambda key: set(proof[key]), ('fullGroupIds', 'primaryGroupIds', 'companionGroupIds'))
    version = proof['schemaVersion']
    require(len(full) == PLATFORMS[proof['platform']][0] + (version == 2) and set(ids(UI)) <= full and first and second
            and second < set(ids(UI)) and not first & second and first | second == full, 'INVALID_PROOF_PARTITION')
    if version == 2:
        require(full == set(ids(inventory(proof['platform']))) and set(ids({'telemetry'})) <= first and
                len(first & set(ids(UI))) == 4 and len(second) == 5, 'INVALID_PROOF_PARTITION')
    expected = first if proof['shard'] == 'primary' else second
    require(set(proof['selectedPassedGroupIds']) == expected and type(proof['fullGroupCount']) is int
            and type(proof['selectedGroupCount']) is int and proof['fullGroupCount'] == len(full)
            and proof['selectedGroupCount'] == len(expected), 'PROOF_COVERAGE_MISMATCH')


def proof_from_zip(data):
    require(len(data) <= MAX_ZIP, 'PROOF_ARCHIVE_TOO_LARGE')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            items = archive.infolist()
            require(len(items) == 1 and items[0].filename == 'proof.json' and not items[0].is_dir()
                    and items[0].file_size <= MAX_PROOF and not items[0].flag_bits & 1
                    and stat.S_IFMT(items[0].external_attr >> 16) in (0, stat.S_IFREG), 'INVALID_PROOF_ARCHIVE')
            proof = parse_json(archive.read(items[0]))
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
        raise ProofError('INVALID_PROOF_ARCHIVE') from None
    validate_proof(proof)
    return proof


class GitHub:
    def json(self, route, *, pages=False):
        arguments = ['gh', 'api', '--hostname', 'github.com', route]
        if pages:
            arguments += ['--paginate', '--slurp']
        return parse_json(run_command(arguments, binary=True), 16 * 1024 * 1024)

    def artifact(self, repo, artifact_id):
        return run_command(['gh', 'api', '--hostname', 'github.com',
                            f'repos/{repo}/actions/artifacts/{artifact_id}/zip'], binary=True)


def timestamp(value):
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(result.tzinfo is not None, 'INVALID_GITHUB_TIMESTAMP')
        return result
    except (ValueError, AttributeError):
        raise ProofError('INVALID_GITHUB_TIMESTAMP') from None


def validate_run(run, repository, repo, sha, run_id, attempt):
    require(run.get('id') == run_id and run.get('run_attempt') == attempt, 'STALE_ATTEMPT_RERUN_ALL_REQUIRED')
    require(type(repository.get('id')) is int and repository['id'] > 0 and repository.get('full_name') == repo
            and run.get('repository', {}).get('id') == repository['id']
            and run.get('head_repository', {}).get('id') == repository['id']
            and run.get('repository', {}).get('full_name') == repo
            and run.get('head_repository', {}).get('full_name') == repo and run.get('head_sha') == sha
            and run.get('path') == WORKFLOW and run.get('event') == 'workflow_dispatch'
            and run.get('head_branch') == repository.get('default_branch'), 'WORKFLOW_PROVENANCE_MISMATCH')
    require(run.get('status') in ('in_progress', 'completed') and run.get('conclusion') in (None, 'success'), 'WORKFLOW_NOT_SUCCESSFUL')


def combine(local, companion, repo, sha, run_id, attempt):
    validate_proof(local); validate_proof(companion)
    require(local['shard'] == 'primary' and companion['shard'] == 'companion', 'SHARD_ROLE_MISMATCH')
    require(local['repositorySha256'] == digest(repo.lower()) and local['workflowCommit'] == sha
            and local['runId'] == run_id and local['runAttempt'] == attempt, 'LOCAL_PROOF_NOT_CURRENT')
    require(local['schemaVersion'] == companion['schemaVersion'] and
            all(local[key] == companion[key] for key in IDENTITY_KEYS), 'SHARD_IDENTITY_MISMATCH')
    first, second, full = map(set, (local['selectedPassedGroupIds'], companion['selectedPassedGroupIds'], local['fullGroupIds']))
    require(not first & second and first | second == full, 'COMBINED_COVERAGE_MISMATCH')
    return {**{key: local[key] for key in IDENTITY_KEYS}, 'schemaVersion': local['schemaVersion'],
            'kind': 'LCL_CTEST_PLATFORM_PROOF', 'status': 'PASS_COMBINED_CURRENT_CTEST', 'fullSuite': True,
            'passedGroupCount': len(full), 'primaryProofSha256': digest(local), 'companionProofSha256': digest(companion)}


def verify_proof(args, environment=os.environ, github=None, *, sleep=time.sleep, clock=time.monotonic):
    repo, sha, run_id, attempt = actions_identity(environment)
    local = read_json(args.local_proof)
    validate_proof(local)
    require(local['shard'] == 'primary' and local['repositorySha256'] == digest(repo.lower())
            and local['workflowCommit'] == sha and local['runId'] == run_id and local['runAttempt'] == attempt,
            'LOCAL_PROOF_NOT_CURRENT')
    github = github or GitHub()
    require(1 <= args.timeout_seconds <= 3600 and 1 <= args.poll_seconds <= 55, 'INVALID_POLL_LIMIT')
    repository = github.json(f'repos/{repo}')
    deadline = clock() + args.timeout_seconds
    while True:
        run = github.json(f'repos/{repo}/actions/runs/{run_id}')
        validate_run(run, repository, repo, sha, run_id, attempt)
        pages = github.json(f'repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100', pages=True)
        jobs = [job for page in pages for job in page.get('jobs', [])]
        helpers = [job for job in jobs if job.get('name') == PLATFORMS[local['platform']][2]]
        require(len(helpers) == 1, 'MISSING_HELPER_RERUN_ALL_REQUIRED')
        helper = helpers[0]
        require(helper.get('run_id') == run_id and helper.get('head_sha') == sha
                and helper.get('run_attempt', attempt) == attempt, 'HELPER_ATTEMPT_MISMATCH')
        if helper.get('status') == 'completed':
            require(helper.get('conclusion') == 'success', 'HELPER_NOT_SUCCESSFUL')
            break
        require(helper.get('status') in ('queued', 'in_progress', 'waiting', 'pending'), 'INVALID_HELPER_STATUS')
        require(clock() < deadline, 'HELPER_WAIT_TIMEOUT')
        sleep(min(args.poll_seconds, max(0, deadline - clock())))
    pages = github.json(f'repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100', pages=True)
    name = f"test-proof-{PLATFORMS[local['platform']][1]}-companion-attempt-{attempt}"
    artifacts = [a for page in pages for a in page.get('artifacts', []) if a.get('name') == name]
    require(len(artifacts) == 1, 'MISSING_PROOF_RERUN_ALL_REQUIRED')
    artifact = artifacts[0]; origin = artifact.get('workflow_run', {})
    require(artifact.get('expired') is False and type(artifact.get('id')) is int and artifact['id'] > 0
            and type(artifact.get('size_in_bytes')) is int and 0 < artifact['size_in_bytes'] <= MAX_ZIP
            and origin.get('id') == run_id and origin.get('head_sha') == sha
            and origin.get('repository_id') == repository.get('id')
            and origin.get('head_repository_id') == repository.get('id')
            and origin.get('head_branch') == repository.get('default_branch'), 'ARTIFACT_PROVENANCE_MISMATCH')
    require(timestamp(helper.get('started_at')) <= timestamp(artifact.get('created_at')) <= timestamp(helper.get('completed_at')),
            'ARTIFACT_NOT_FROM_CURRENT_HELPER')
    blob = github.artifact(repo, artifact['id'])
    require(artifact.get('digest') == 'sha256:' + hashlib.sha256(blob).hexdigest(), 'ARTIFACT_DIGEST_MISMATCH')
    companion = proof_from_zip(blob)
    result = combine(local, companion, repo, sha, run_id, attempt)
    # Recheck attempt/provenance after the network transfer, before opening the gate.
    validate_run(github.json(f'repos/{repo}/actions/runs/{run_id}'), repository, repo, sha, run_id, attempt)
    result.update(companionArtifactId=artifact['id'], companionJobId=helper['id'])
    return result


def main(argv=None):
    global PRIVATE_LOG
    parser = argparse.ArgumentParser(description='Create and combine current-attempt CTest shard proofs')
    subparsers = parser.add_subparsers(dest='operation', required=True)
    plan = subparsers.add_parser('validate-plan')
    plan.add_argument('--plan', type=Path, required=True)
    create = subparsers.add_parser('create')
    for name in ('source-root', 'build-dir', 'sdk-root', 'driver-result', 'junit', 'output'):
        create.add_argument('--' + name, type=Path, required=True)
    create.add_argument('--platform', choices=PLATFORMS, required=True)
    create.add_argument('--shard', choices=('primary', 'companion'), required=True)
    verify = subparsers.add_parser('verify')
    verify.add_argument('--local-proof', type=Path, required=True)
    verify.add_argument('--output', type=Path, required=True)
    verify.add_argument('--timeout-seconds', type=int, default=1800)
    verify.add_argument('--poll-seconds', type=int, default=20)
    for command in (create, verify):
        command.add_argument('--private-log', type=Path)
    args = parser.parse_args(argv)
    PRIVATE_LOG = getattr(args, 'private_log', None)
    if PRIVATE_LOG is None and os.environ.get('RUNNER_TEMP'):
        PRIVATE_LOG = Path(os.environ['RUNNER_TEMP']) / 'private-build-logs/ci-test-proof-private.log'
    try:
        if args.operation == 'validate-plan':
            plan = read_json(args.plan)
            validate_plan(plan)
            # Both historical workflow preflights required this 4/5 layout.
            require(len(plan['primaryUi']) == 4 and len(plan['companionUi']) == 5,
                    'INCOMPLETE_UI_PARTITION')
            print('CI_TEST_SHARD_PLAN_VALID')
            return 0
        proof = create_proof(args) if args.operation == 'create' else verify_proof(args)
        write_json(args.output, proof)
    except ProofError as failure:
        print('CI_TEST_PROOF_' + failure.code, file=sys.stderr)
        return 1
    except Exception as failure:
        private_log(repr(failure).encode())
        print('CI_TEST_PROOF_INVALID_EVIDENCE', file=sys.stderr)
        return 1
    print('CI_TEST_PROOF_CREATED' if args.operation == 'create' else 'CI_TEST_PROOF_COMBINED')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
