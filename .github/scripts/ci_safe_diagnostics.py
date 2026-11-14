#!/usr/bin/env python3
"""Export bounded location fingerprints; private log text never enters the report."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess

from ci_test_proof import BACKEND, UI


MAX_SUMMARY = 32768
MAX_LOCATIONS = 64
MAX_GROUPS = 64
MAX_FILES = 512
MAX_DIRECTORY_ENTRIES = 4096
MAX_SCAN_BYTES = 16 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
PLATFORMS = frozenset(('windows-x64', 'linux-x86_64', 'windows-ui-companion',
    'linux-ui-companion', 'android', 'windows-build', 'windows-ui',
    'windows-bundle', 'windows-benchmark'))
CATEGORIES = frozenset(('TEST_FAILURE', 'TEST_TIMEOUT', 'COMPILER_ERROR',
    'CONFIGURATION_ERROR', 'SOURCE_ACCESS_ERROR', 'PROVENANCE_ERROR',
    'PACKAGING_ERROR', 'UNKNOWN_FAILURE'))
PUBLIC_GROUPS = frozenset(UI | BACKEND | {'telemetry', 'linux_auth_vault', 'static_runtime'})
SUMMARY_KEYS = frozenset(('schemaVersion', 'kind', 'runId', 'runAttempt',
    'workflowCommit', 'sourceCommit', 'platform', 'status', 'categories',
    'failedGroups', 'locations', 'truncated'))
LOCATION_KEYS = frozenset(('fileId', 'line', 'column', 'category'))
HEX40 = re.compile(r'[0-9a-f]{40}\Z')
HEX64 = re.compile(r'[0-9a-f]{64}\Z')
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
QT_LOCATION = re.compile(r'\bLoc:\s*\[(.+)\((\d{1,8})(?:,(\d{1,7}))?\)\]')
WINDOWS_LOCATION = re.compile(r'^\s*(.+?)\((\d{1,8})(?:,(\d{1,7}))?\)\s*:\s*(failure location|(?:fatal\s+)?error\b)')
COMPILER_LOCATION = re.compile(r'^\s*(.+?):(\d{1,8})(?::(\d{1,7}))?:\s*(?:fatal\s+)?error\b')
CMAKE_LOCATION = re.compile(r'\bCMake Error (?:at|in) (.+?):(\d{1,8})(?:\s|\()')
CTEST_GROUP = re.compile(r'(?:^|\s)(?:\d+/\d+\s+)?Test\s+#\d+:\s*([a-z][a-z0-9_]*)\s+.*\*\*\*(Failed|Timeout|Not Run)', re.I)
CTEST_FINAL = re.compile(r'^\s*\d+\s+-\s+([a-z][a-z0-9_]*)\s+\((Failed|Timeout|Not Run)\)', re.I)


class SummaryError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise SummaryError(code)


def is_hex(value, pattern):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def positive(value):
    return type(value) is int and value > 0


def path_id(relative_git_path):
    require(isinstance(relative_git_path, str) and 0 < len(relative_git_path.encode('utf-8')) <= 4096
            and '\\' not in relative_git_path and '\x00' not in relative_git_path
            and not PurePosixPath(relative_git_path).is_absolute()
            and all(part and part not in ('.', '..') for part in relative_git_path.split('/')),
            'INVALID_TRACKED_PATH')
    return hashlib.sha256(relative_git_path.encode('utf-8')).hexdigest()


def validate_summary(summary):
    require(isinstance(summary, dict) and set(summary) == SUMMARY_KEYS, 'INVALID_SUMMARY_SCHEMA')
    require(type(summary['schemaVersion']) is int and summary['schemaVersion'] == 1
            and summary['kind'] == 'LCL_CI_SAFE_DIAGNOSTIC'
            and positive(summary['runId']) and positive(summary['runAttempt'])
            and isinstance(summary['platform'], str) and summary['platform'] in PLATFORMS
            and summary['status'] in ('passed', 'failed', 'cancelled', 'unknown')
            and type(summary['truncated']) is bool, 'INVALID_SUMMARY_METADATA')
    for key in ('workflowCommit', 'sourceCommit'):
        require(summary[key] is None or is_hex(summary[key], HEX40), 'INVALID_SUMMARY_IDENTITY')
    for key, allowed, maximum in (('categories', CATEGORIES, len(CATEGORIES)),
                                   ('failedGroups', PUBLIC_GROUPS, MAX_GROUPS)):
        values = summary[key]
        require(isinstance(values, list) and len(values) <= maximum
                and all(isinstance(value, str) and value in allowed for value in values)
                and values == sorted(set(values)), 'INVALID_SUMMARY_VOCABULARY')
    locations = summary['locations']
    require(isinstance(locations, list) and len(locations) <= MAX_LOCATIONS, 'INVALID_SUMMARY_LOCATIONS')
    seen = set()
    for location in locations:
        require(isinstance(location, dict) and set(location) == LOCATION_KEYS
                and is_hex(location['fileId'], HEX64)
                and type(location['line']) is int and 1 <= location['line'] <= 10_000_000
                and (location['column'] is None or type(location['column']) is int
                     and 1 <= location['column'] <= 1_000_000)
                and isinstance(location['category'], str) and location['category'] in CATEGORIES,
                'INVALID_SUMMARY_LOCATION')
        value = (location['fileId'], location['line'], location['column'], location['category'])
        require(value not in seen, 'DUPLICATE_SUMMARY_LOCATION')
        seen.add(value)
    require(len(encode_summary(summary)) <= MAX_SUMMARY, 'SUMMARY_TOO_LARGE')
    return summary


def encode_summary(summary):
    return (json.dumps(summary, sort_keys=True, separators=(',', ':'), ensure_ascii=True) + '\n').encode('utf-8')


def ordinary(path, *, directory=False):
    path = Path(os.path.abspath(path))
    for node in (path, *path.parents):
        information = node.lstat()
        require(not stat.S_ISLNK(information.st_mode)
                and not getattr(information, 'st_file_attributes', 0) & 0x400, 'LINKED_INPUT')
    information = path.stat()
    require(stat.S_ISDIR(information.st_mode) if directory else stat.S_ISREG(information.st_mode),
            'INVALID_INPUT_TYPE')
    if not directory:
        require(information.st_nlink == 1, 'LINKED_INPUT')
    return path


def git_inventory(source_root):
    try:
        root = ordinary(source_root, directory=True)
        def git(*arguments):
            result = subprocess.run(['git', '-C', str(root), *arguments], stdin=subprocess.DEVNULL,
                capture_output=True, timeout=15, check=False)
            require(result.returncode == 0 and len(result.stdout) <= 4 * 1024 * 1024, 'SOURCE_IDENTITY_UNAVAILABLE')
            return result.stdout
        top = git('rev-parse', '--show-toplevel').decode('utf-8').strip()
        require(os.path.normcase(os.path.normpath(top)) == os.path.normcase(os.path.normpath(str(root))),
                'SOURCE_IDENTITY_UNAVAILABLE')
        commit = git('rev-parse', '--verify', 'HEAD').decode('ascii').strip()
        require(is_hex(commit, HEX40), 'SOURCE_IDENTITY_UNAVAILABLE')
        paths = git('ls-tree', '-rz', '--full-tree', '--name-only', 'HEAD').decode('utf-8').split('\x00')
        paths = [value for value in paths if value]
        require(len(paths) <= 30000, 'SOURCE_IDENTITY_UNAVAILABLE')
        for path in paths:
            path_id(path)
        return commit, paths
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        return None, []


class SourceIndex:
    def __init__(self, source_root, paths, recorded_root=None):
        self.prefix = str(recorded_root if recorded_root is not None else Path(os.path.abspath(source_root))).replace('\\', '/').rstrip('/')
        self.paths = set(paths)
        self.folded = {}
        for path in paths:
            self.folded.setdefault(path.casefold(), []).append(path)

    def resolve(self, candidate):
        candidate = candidate.strip().strip('"\'').replace('\\', '/')
        if '\x00' in candidate or len(candidate) > 8192:
            return None
        absolute = candidate.startswith('/') or re.match(r'^[A-Za-z]:/', candidate)
        if absolute:
            prefix = self.prefix + '/'
            if not candidate.casefold().startswith(prefix.casefold()):
                return None
            candidate = candidate[len(prefix):]
        if any(part in ('', '.', '..') for part in candidate.split('/')):
            return None
        if candidate in self.paths:
            return candidate
        matches = self.folded.get(candidate.casefold(), [])
        # CMake/compilers often print paths relative to qt-native rather than
        # the repository. Only accept an unambiguous tracked suffix.
        if not absolute and not matches:
            suffix = '/' + candidate.casefold()
            matches = [path for path in self.paths if path.casefold().endswith(suffix)]
        return matches[0] if len(matches) == 1 else None


def log_files(root):
    try:
        root = ordinary(root, directory=True)
    except (OSError, ValueError):
        return [], False
    found = []
    truncated = False
    entry_count = 0
    pending = [(root, 0)]
    while pending:
        folder, depth = pending.pop()
        try:
            children = []
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry_count >= MAX_DIRECTORY_ENTRIES:
                        truncated = True
                        break
                    entry_count += 1
                    children.append(Path(entry.path))
            children.sort(key=lambda child: child.name)
        except OSError:
            truncated = True
            continue
        for child in children:
            try:
                information = child.lstat()
                if stat.S_ISLNK(information.st_mode) or getattr(information, 'st_file_attributes', 0) & 0x400:
                    truncated = True
                    continue
                if stat.S_ISDIR(information.st_mode):
                    if depth < 16:
                        pending.append((child, depth + 1))
                    else:
                        truncated = True
                elif stat.S_ISREG(information.st_mode) and child.suffix.lower() in ('.log', '.txt', '.json', '.xml'):
                    if information.st_nlink != 1:
                        truncated = True
                        continue
                    if len(found) >= MAX_FILES:
                        return found, True
                    found.append(child)
            except OSError:
                truncated = True
    return found, truncated


def read_log(path, remaining):
    path = ordinary(path)
    size = path.stat().st_size
    limit = min(MAX_FILE_BYTES, remaining)
    if limit <= 0:
        return '', 0, size > 0
    with path.open('rb') as source:
        if size <= limit:
            content = source.read(limit)
        else:
            half = limit // 2
            content = source.read(half)
            start = max(half, size - (limit - half))
            separator = b'\n'
            if content.startswith((b'\xff\xfe', b'\xfe\xff')):
                content = content[:len(content) // 2 * 2]
                start += start % 2
                separator = b'\n\x00' if content.startswith(b'\xff\xfe') else b'\x00\n'
            source.seek(start)
            content += separator + source.read(limit - half)
    if content.startswith((b'\xff\xfe', b'\xfe\xff')):
        text = content.decode('utf-16', errors='replace')
    else:
        text = content.decode('utf-8-sig', errors='replace')
    return text, min(size, limit), size > limit


def create_summary(source_root, logs_dir, platform, status, *, run_id, run_attempt,
                   workflow_commit=None, recorded_source_root=None):
    mapped_status = {'success': 'passed', 'failure': 'failed', 'passed': 'passed',
                     'failed': 'failed', 'cancelled': 'cancelled'}.get(status, 'unknown')
    commit, paths = git_inventory(source_root)
    summary = dict(schemaVersion=1, kind='LCL_CI_SAFE_DIAGNOSTIC', runId=run_id,
        runAttempt=run_attempt, workflowCommit=workflow_commit, sourceCommit=commit,
        platform=platform, status=mapped_status, categories=[], failedGroups=[], locations=[], truncated=False)
    validate_summary(summary)
    if mapped_status == 'passed':
        return summary
    index = SourceIndex(source_root, paths, recorded_source_root)
    files, truncated = log_files(logs_dir)
    categories, groups, locations = set(), set(), {}
    remaining = MAX_SCAN_BYTES
    for file in files:
        try:
            text, consumed, shortened = read_log(file, remaining)
        except (OSError, ValueError):
            truncated = True
            continue
        remaining -= consumed
        truncated = truncated or shortened
        for raw in text.splitlines():
            if len(raw) > 16384:
                truncated = True
                continue
            line = ANSI.sub('', raw)
            group = CTEST_GROUP.search(line) or CTEST_FINAL.search(line)
            if group:
                category = 'TEST_TIMEOUT' if group[2].lower() == 'timeout' else 'TEST_FAILURE'
                categories.add(category)
                if group[1] in PUBLIC_GROUPS:
                    groups.add(group[1])
            if re.match(r'^\s*FAIL!\s*:', line):
                categories.add('TEST_FAILURE')
            if re.search(r'\bCMake Error\b', line):
                categories.add('CONFIGURATION_ERROR')
            if re.search(r'(?:CI_TEST_PROOF|RELEASE_PROVENANCE)_REJECTED\b', line):
                categories.add('PROVENANCE_ERROR')
            if re.search(r'^\s*fatal:\s*(?:Authentication failed|repository .* not found|Could not read from remote repository)', line, re.I):
                categories.add('SOURCE_ACCESS_ERROR')
            if any(marker in line for marker in ('no Qt platform plugin could be initialized',
                'Could not find the Qt platform plugin', 'Unable to load DLL', 'is missing from your computer')):
                categories.add('PACKAGING_ERROR')
            matches = []
            match = QT_LOCATION.search(line)
            if match:
                matches.append((match[1], match[2], match[3], 'TEST_FAILURE'))
            match = WINDOWS_LOCATION.search(line)
            if match:
                matches.append((match[1], match[2], match[3],
                    'TEST_FAILURE' if match[4] == 'failure location' else 'COMPILER_ERROR'))
            match = COMPILER_LOCATION.search(line)
            if match:
                matches.append((match[1], match[2], match[3], 'COMPILER_ERROR'))
            match = CMAKE_LOCATION.search(line)
            if match:
                matches.append((match[1], match[2], None, 'CONFIGURATION_ERROR'))
            for name, number, column, category in matches:
                categories.add(category)
                if len(locations) >= MAX_LOCATIONS:
                    truncated = True
                    continue
                canonical = index.resolve(name)
                if canonical is None:
                    continue
                number, column = int(number), int(column) if column is not None else None
                if not 1 <= number <= 10_000_000 or column is not None and not 1 <= column <= 1_000_000:
                    continue
                identity = (path_id(canonical), number, column, category)
                locations[identity] = dict(fileId=identity[0], line=number, column=column, category=category)
        if remaining <= 0:
            truncated = True
            break
    if mapped_status == 'failed' and not categories:
        categories.add('UNKNOWN_FAILURE')
    summary.update(categories=sorted(categories), failedGroups=sorted(groups)[:MAX_GROUPS],
        locations=[locations[key] for key in sorted(locations, key=lambda key: (key[0], key[1], key[2] or 0, key[3]))],
        truncated=truncated or len(groups) > MAX_GROUPS)
    return validate_summary(summary)


def write_summary(path, summary):
    data = encode_summary(validate_summary(summary))
    path = Path(os.path.abspath(path))
    for parent in path.parents:
        if parent.exists() or parent.is_symlink():
            ordinary(parent, directory=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as target:
        target.write(data)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    command = parser.add_subparsers(dest='command', required=True).add_parser('create')
    command.add_argument('--source-root', type=Path, required=True)
    command.add_argument('--logs-dir', type=Path, required=True)
    command.add_argument('--platform', choices=sorted(PLATFORMS), required=True)
    command.add_argument('--status', required=True)
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--run-id', type=int, default=os.environ.get('GITHUB_RUN_ID'))
    command.add_argument('--attempt', type=int, default=os.environ.get('GITHUB_RUN_ATTEMPT'))
    command.add_argument('--workflow-commit', default=os.environ.get('GITHUB_SHA'))
    command.add_argument('--recorded-source-root', help='Explicit historic checkout prefix for local replay only')
    args = parser.parse_args(argv)
    try:
        summary = create_summary(args.source_root, args.logs_dir, args.platform, args.status,
            run_id=args.run_id, run_attempt=args.attempt, workflow_commit=args.workflow_commit,
            recorded_source_root=args.recorded_source_root)
        write_summary(args.output, summary)
    except (OSError, ValueError, TypeError, UnicodeError, subprocess.SubprocessError):
        print('SAFE_DIAGNOSTIC_REJECTED')
        return 1
    print('SAFE_DIAGNOSTIC_CREATED')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
