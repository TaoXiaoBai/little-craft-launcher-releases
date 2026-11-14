#!/usr/bin/env python3
"""Read safe CI diagnostics and locate failures in an authorized source checkout.

This command never decrypts logs, changes a checkout, fetches source, or uploads
anything. Resolved source text is local/private output, not builder output.
"""

import argparse
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import subprocess
import threading
import zipfile

import ci_safe_diagnostics as safe


MAX_METADATA = 8 * 1024 * 1024
MAX_ARCHIVE = 128 * 1024
MAX_TREE = 16 * 1024 * 1024
MAX_SOURCE = 4 * 1024 * 1024
WORKFLOWS = {
    'windows-x64': ('.github/workflows/prerelease.yml', 'Build windows-x64'),
    'linux-x86_64': ('.github/workflows/prerelease.yml', 'Build linux-x86_64'),
    'windows-ui-companion': ('.github/workflows/prerelease.yml', 'Test windows-x64 companion UI'),
    'linux-ui-companion': ('.github/workflows/prerelease.yml', 'Test linux-x86_64 companion UI'),
    'android': ('.github/workflows/android-build.yml', 'Android host tests and debug APK'),
    'windows-build': ('.github/workflows/windows-build.yml', 'Windows build and tests'),
    'windows-ui': ('.github/workflows/diagnose-windows-ui.yml', 'diagnose'),
    'windows-bundle': ('.github/workflows/diagnose-windows-bundle.yml', 'diagnose-bundle-only'),
    'windows-benchmark': ('.github/workflows/benchmark-windows-build.yml',
                          'Diagnostic Windows compiler scheduling only'),
}


class TriageError(ValueError):
    pass


def require(value, code):
    if not value:
        raise TriageError(code)


def positive(value):
    require(type(value) is int and value > 0, 'INVALID_IDENTIFIER')
    return value


def hex_id(value, size):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{%d}' % size, value) is not None


def repository_name(value):
    require(isinstance(value, str) and re.fullmatch(
        r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+', value) is not None
        and value.split('/')[1] not in ('.', '..'), 'INVALID_REPOSITORY')
    return value


def same_repository(value, expected):
    return isinstance(value, str) and value.casefold() == expected.casefold()


def read_command(arguments, maximum, *, timeout=45):
    """Bound stdout while it arrives, including artifact downloads."""
    data = bytearray()
    flags = {'large': False, 'error': False}
    try:
        with subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as process:
            def consume():
                try:
                    while True:
                        chunk = process.stdout.read(65536)
                        if not chunk:
                            return
                        if len(data) + len(chunk) > maximum:
                            flags['large'] = True
                            try:
                                process.kill()
                            except OSError:
                                pass
                            return
                        data.extend(chunk)
                except OSError:
                    flags['error'] = True
            reader = threading.Thread(target=consume, daemon=True)
            reader.start()
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
                reader.join(timeout=10)
                raise TriageError('READ_TIMEOUT') from None
            reader.join(timeout=10)
            require(not reader.is_alive() and not flags['error'], 'READ_FAILED')
            require(not flags['large'], 'READ_LIMIT_EXCEEDED')
            require(code == 0, 'READ_FAILED')
    except (OSError, subprocess.SubprocessError):
        raise TriageError('READ_FAILED') from None
    return bytes(data)


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'DUPLICATE_JSON_KEY')
        result[key] = value
    return result


def invalid_constant(_):
    raise TriageError('INVALID_JSON_NUMBER')


def json_pages(data, maximum=MAX_METADATA):
    require(isinstance(data, bytes) and 0 < len(data) <= maximum, 'INVALID_JSON_SIZE')
    try:
        text = data.decode('utf-8', errors='strict')
        decoder = json.JSONDecoder(object_pairs_hook=unique_pairs, parse_constant=invalid_constant)
        pages, index = [], 0
        while index < len(text):
            while index < len(text) and text[index].isspace():
                index += 1
            if index == len(text):
                break
            value, index = decoder.raw_decode(text, index)
            pages.append(value)
            require(len(pages) <= 100, 'TOO_MANY_PAGES')
        require(pages, 'INVALID_JSON')
        return pages
    except (UnicodeError, json.JSONDecodeError):
        raise TriageError('INVALID_JSON') from None


class GitHub:
    def json(self, route, *, pages=False):
        command = ['gh', 'api', '--hostname', 'github.com', '--method', 'GET', route]
        if pages:
            command.append('--paginate')  # Works with old gh; no --slurp.
        values = json_pages(read_command(command, MAX_METADATA))
        require(pages or len(values) == 1, 'UNEXPECTED_JSON_PAGES')
        return values if pages else values[0]

    def artifact(self, repository, artifact_id):
        repository_name(repository); positive(artifact_id)
        return read_command(['gh', 'api', '--hostname', 'github.com', '--method', 'GET',
            f'repos/{repository}/actions/artifacts/{artifact_id}/zip'], MAX_ARCHIVE)


def paginated(github, route, key):
    pages = github.json(route + '?per_page=100', pages=True)
    require(isinstance(pages, list) and 0 < len(pages) <= 100, 'INVALID_API_PAGES')
    values = []
    for page in pages:
        require(isinstance(page, dict) and isinstance(page.get(key), list), 'INVALID_API_PAGES')
        values.extend(page[key])
    require(len(values) <= 10000, 'API_INVENTORY_TOO_LARGE')
    return values


def timestamp(value):
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(result.tzinfo is not None, 'INVALID_API_TIMESTAMP')
        return result
    except (ValueError, AttributeError):
        raise TriageError('INVALID_API_TIMESTAMP') from None


def parse_summary(data):
    values = json_pages(data, safe.MAX_SUMMARY)
    require(len(values) == 1, 'INVALID_SUMMARY_JSON')
    summary = values[0]
    try:
        safe.validate_summary(summary)
    except (ValueError, TypeError, KeyError):
        raise TriageError('INVALID_SAFE_SUMMARY') from None
    return summary


def summary_from_zip(blob):
    require(isinstance(blob, bytes) and 0 < len(blob) <= MAX_ARCHIVE, 'INVALID_SUMMARY_ARCHIVE')
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            entries = archive.infolist()
            require(len(entries) == 1, 'INVALID_SUMMARY_ARCHIVE')
            entry = entries[0]
            require(entry.filename == 'summary.json' and not entry.is_dir()
                    and 0 < entry.file_size <= safe.MAX_SUMMARY
                    and 0 < entry.compress_size <= MAX_ARCHIVE and not entry.flag_bits & 1
                    and entry.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    and stat.S_IFMT(entry.external_attr >> 16) in (0, stat.S_IFREG),
                    'INVALID_SUMMARY_ARCHIVE')
            with archive.open(entry) as source:
                data = source.read(safe.MAX_SUMMARY + 1)
            require(len(data) == entry.file_size, 'INVALID_SUMMARY_ARCHIVE')
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError):
        raise TriageError('INVALID_SUMMARY_ARCHIVE') from None
    return parse_summary(data)


def validate_run(run, repository, repo, run_id, attempt):
    require(isinstance(run, dict) and run.get('id') == run_id
            and run.get('run_attempt') == attempt, 'RUN_ATTEMPT_MISMATCH')
    positive(run.get('run_attempt'))
    for key in ('repository', 'head_repository'):
        origin = run.get(key)
        require(isinstance(origin, dict) and origin.get('id') == repository['id']
                and same_repository(origin.get('full_name'), repo), 'RUN_REPOSITORY_MISMATCH')
    require(run.get('event') == 'workflow_dispatch'
            and run.get('head_branch') == repository['default_branch']
            and hex_id(run.get('head_sha'), 40), 'RUN_PROVENANCE_MISMATCH')


def fetch_summaries(repository, run_id, *, platform=None, attempt=None, github=None):
    repository_name(repository); positive(run_id)
    if platform is not None:
        require(platform in safe.PLATFORMS, 'INVALID_PLATFORM')
    github = github or GitHub()
    metadata = github.json(f'repos/{repository}')
    require(isinstance(metadata, dict) and type(metadata.get('id')) is int and metadata['id'] > 0
            and same_repository(metadata.get('full_name'), repository)
            and isinstance(metadata.get('default_branch'), str) and metadata['default_branch'],
            'REPOSITORY_MISMATCH')
    current = github.json(f'repos/{repository}/actions/runs/{run_id}')
    require(isinstance(current, dict), 'INVALID_RUN')
    requested = positive(attempt if attempt is not None else current.get('run_attempt'))
    run = current if requested == current.get('run_attempt') else github.json(
        f'repos/{repository}/actions/runs/{run_id}/attempts/{requested}')
    validate_run(run, metadata, repository, run_id, requested)
    artifacts = paginated(github, f'repos/{repository}/actions/runs/{run_id}/artifacts', 'artifacts')
    jobs = paginated(github,
        f'repos/{repository}/actions/runs/{run_id}/attempts/{requested}/jobs', 'jobs')
    targets = [platform] if platform is not None else sorted(safe.PLATFORMS)
    summaries = []
    for candidate in targets:
        name = f'safe-diagnostics-{candidate}-attempt-{requested}'
        matches = [entry for entry in artifacts if isinstance(entry, dict) and entry.get('name') == name]
        if not matches:
            continue
        require(len(matches) == 1, 'AMBIGUOUS_SUMMARY_ARTIFACT')
        artifact = matches[0]
        require(artifact.get('expired') is False, 'SUMMARY_ARTIFACT_EXPIRED')
        positive(artifact.get('id'))
        require(type(artifact.get('size_in_bytes')) is int
                and 0 < artifact['size_in_bytes'] <= MAX_ARCHIVE, 'INVALID_ARTIFACT_SIZE')
        origin = artifact.get('workflow_run', {})
        require(isinstance(origin, dict) and origin.get('id') == run_id
                and origin.get('head_sha') == run['head_sha']
                and origin.get('repository_id') == metadata['id']
                and origin.get('head_repository_id') == metadata['id']
                and origin.get('head_branch') == metadata['default_branch'], 'ARTIFACT_PROVENANCE_MISMATCH')
        workflow, job_name = WORKFLOWS[candidate]
        require(run.get('path') == workflow, 'SUMMARY_WORKFLOW_MISMATCH')
        matching_jobs = [job for job in jobs if isinstance(job, dict) and job.get('name') == job_name]
        require(len(matching_jobs) == 1, 'SUMMARY_JOB_MISSING')
        job = matching_jobs[0]
        require(job.get('run_id') == run_id and job.get('head_sha') == run['head_sha']
                and job.get('run_attempt', requested) == requested, 'SUMMARY_JOB_MISMATCH')
        completed = timestamp(job['completed_at']) if job.get('completed_at') else dt.datetime.now(dt.timezone.utc)
        require(timestamp(job.get('started_at')) <= timestamp(artifact.get('created_at')) <= completed,
                'SUMMARY_NOT_FROM_CURRENT_JOB')
        blob = github.artifact(repository, artifact['id'])
        require(artifact.get('digest') == 'sha256:' + hashlib.sha256(blob).hexdigest(), 'SUMMARY_DIGEST_MISMATCH')
        summary = summary_from_zip(blob)
        require(summary['runId'] == run_id and summary['runAttempt'] == requested
                and summary['workflowCommit'] == run['head_sha'] and summary['platform'] == candidate,
                'SUMMARY_METADATA_MISMATCH')
        summaries.append(summary)
    require(summaries, 'SAFE_SUMMARY_MISSING_USE_RUNBOOK')
    latest = github.json(f'repos/{repository}/actions/runs/{run_id}')
    require(isinstance(latest, dict) and latest.get('head_sha') == run['head_sha']
            and (attempt is not None or latest.get('run_attempt') == requested), 'RUN_CHANGED_DURING_READ')
    return summaries


def git_read(source_root, *arguments, maximum=MAX_TREE):
    return read_command(['git', '-C', str(source_root), *arguments], maximum)


def resolve_summary(summary, source_root, *, context=2):
    try:
        safe.validate_summary(summary)
    except (ValueError, TypeError, KeyError):
        raise TriageError('INVALID_SAFE_SUMMARY') from None
    require(type(context) is int and 0 <= context <= 10, 'INVALID_CONTEXT')
    root = Path(source_root).absolute()
    result = {'summary': summary, 'sourceAvailable': False, 'checkoutMatchesSource': False,
              'locations': [], 'instructions': []}
    commit = summary['sourceCommit']
    if commit is None:
        result['instructions'].append('No source checkout was available to the reporting job; inspect its failed public step.')
        return result
    require(hex_id(commit, 40), 'INVALID_SOURCE_COMMIT')
    try:
        git_read(root, 'cat-file', '-e', commit + '^{commit}', maximum=1024)
        head = git_read(root, 'rev-parse', 'HEAD', maximum=1024).decode('ascii').strip()
        tree = git_read(root, 'ls-tree', '-rz', '--full-tree', commit)
    except TriageError:
        result['instructions'].append('Fetch the source commit in your authorized private checkout: ' +
            shlex.join(['git', '-C', str(root), 'fetch', 'origin', commit]))
        result['instructions'].append('Then create an isolated worktree; do not overwrite local edits: ' +
            shlex.join(['git', '-C', str(root), 'worktree', 'add', '--detach', '<new-directory>', commit]))
        return result
    paths = {}
    modes = {}
    for entry in tree.split(b'\0'):
        if not entry:
            continue
        require(b'\t' in entry, 'INVALID_GIT_TREE')
        header, raw_path = entry.split(b'\t', 1)
        fields = header.split(b' ')
        require(len(fields) == 3, 'INVALID_GIT_TREE')
        try:
            path = raw_path.decode('utf-8', errors='strict')
        except UnicodeError:
            continue
        value = PurePosixPath(path)
        require(not value.is_absolute() and '..' not in value.parts and '\\' not in path,
                'INVALID_GIT_PATH')
        file_id = safe.path_id(path)
        require(file_id not in paths, 'AMBIGUOUS_SOURCE_PATH')
        paths[file_id], modes[file_id] = path, fields[0]
    result['sourceAvailable'] = True
    result['checkoutMatchesSource'] = head == commit
    if head != commit:
        result['instructions'].append('Locations below refer to the recorded commit, not the current checkout. Create an isolated worktree: ' +
            shlex.join(['git', '-C', str(root), 'worktree', 'add', '--detach', '<new-directory>', commit]))
    for location in summary['locations']:
        item = dict(location)
        path = paths.get(location['fileId'])
        if path is None or modes[location['fileId']] not in (b'100644', b'100755'):
            item.update(resolved=False)
            result['locations'].append(item)
            continue
        data = git_read(root, 'show', commit + ':' + path, maximum=MAX_SOURCE)
        try:
            require(b'\x00' not in data, 'BINARY_SOURCE_LOCATION')
            lines = data.decode('utf-8', errors='strict').splitlines()
        except UnicodeError:
            raise TriageError('NON_UTF8_SOURCE_LOCATION') from None
        line = location['line']
        require(line <= len(lines), 'SOURCE_LINE_OUT_OF_RANGE')
        start, end = max(1, line - context), min(len(lines), line + context)
        item.update(path=path, resolved=True,
            context=[{'line': number, 'text': lines[number - 1]} for number in range(start, end + 1)])
        result['locations'].append(item)
    return result


def permit_private_output(environment, github):
    if environment.get('GITHUB_ACTIONS', '').lower() != 'true':
        return
    repository = repository_name(environment.get('GITHUB_REPOSITORY', ''))
    try:
        metadata = github.json(f'repos/{repository}')
        require(isinstance(metadata, dict) and metadata.get('private') is True
                and same_repository(metadata.get('full_name'), repository),
                'PRIVATE_OUTPUT_REQUIRES_PRIVATE_ACTIONS_REPOSITORY')
    except (TriageError, OSError, ValueError, TypeError):
        raise TriageError('PRIVATE_OUTPUT_REQUIRES_PRIVATE_ACTIONS_REPOSITORY') from None


def main(argv=None, *, github=None, environment=os.environ):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--run-id', type=int)
    source.add_argument('--summary-file', type=Path)
    parser.add_argument('--repository')
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('--platform', choices=sorted(safe.PLATFORMS))
    parser.add_argument('--attempt', type=int)
    parser.add_argument('--context', type=int, default=2)
    parser.add_argument('--safe-only', action='store_true', help='Do not resolve or print private paths/source text')
    args = parser.parse_args(argv)
    github = github or GitHub()
    try:
        if not args.safe_only:
            require(args.source_root is not None, 'SOURCE_ROOT_REQUIRED')
            permit_private_output(environment, github)
        if args.summary_file is not None:
            require(args.repository is None and args.attempt is None, 'OFFLINE_OPTIONS_CONFLICT')
            require(args.summary_file.is_file() and not args.summary_file.is_symlink()
                    and args.summary_file.stat().st_size <= safe.MAX_SUMMARY, 'INVALID_SUMMARY_FILE')
            summaries = [parse_summary(args.summary_file.read_bytes())]
            if args.platform is not None:
                require(summaries[0]['platform'] == args.platform, 'SUMMARY_PLATFORM_MISMATCH')
        else:
            require(args.repository is not None, 'REPOSITORY_REQUIRED')
            summaries = fetch_summaries(args.repository, args.run_id, platform=args.platform,
                                       attempt=args.attempt, github=github)
        report = {'provenanceVerified': args.summary_file is None, 'safeOnly': args.safe_only,
                  'results': summaries if args.safe_only else
                      [resolve_summary(summary, args.source_root, context=args.context) for summary in summaries]}
        if args.summary_file is not None:
            report['notice'] = 'OFFLINE_SUMMARY_NOT_REMOTE_AUTHENTICATED'
        print(json.dumps(report, ensure_ascii=True, indent=2))
    except (TriageError, ValueError, TypeError, KeyError, OSError, UnicodeError):
        # Error codes only: never reflect API bodies, private paths or source.
        import sys
        error = sys.exc_info()[1]
        code = str(error) if isinstance(error, TriageError) else 'INVALID_INPUT'
        print('CI_TRIAGE_REJECTED:' + code)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
