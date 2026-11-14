#!/usr/bin/env python3
"""Bind release packages to complete tests and one source tree without publishing source IDs."""

import argparse
import hashlib
import io
import os
from pathlib import Path
import re
import stat
import zipfile

import ci_test_proof as ctest
from release_support import runtime_names


MAX_RECEIPT = 16 * 1024
MAX_ARCHIVE = 128 * 1024
MAX_RUNTIME = 1024 * 1024 * 1024
RECEIPT_KEYS = frozenset((
    'schemaVersion', 'kind', 'repositorySha256', 'workflowCommit', 'runId', 'runAttempt',
    'platform', 'tag', 'sourceIdentitySha256', 'distributionConfigSha256', 'ctestSchemaVersion', 'passedGroupCount',
    'fullSuite', 'runtime',
))
COMBINED_KEYS = frozenset(ctest.IDENTITY_KEYS) | {
    'schemaVersion', 'kind', 'status', 'fullSuite', 'passedGroupCount',
    'primaryProofSha256', 'companionProofSha256', 'companionArtifactId', 'companionJobId',
}
FILENAMES = {'windows-x64': 'windows.json', 'linux-x86_64': 'linux.json'}
require = ctest.require


def repository_name(value):
    require(isinstance(value, str) and re.fullmatch(
        r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+', value) is not None
        and value.split('/')[1] not in ('.', '..'), 'INVALID_RELEASE_REPOSITORY')
    return value


def version_tag(value):
    require(isinstance(value, str) and len(value) <= 128 and re.fullmatch(
        r'v[0-9]+\.[0-9]+\.[0-9]+-(pre|alpha|beta|rc)\.[0-9]+', value) is not None,
        'INVALID_RELEASE_TAG')
    return value


def ordinary_file(path):
    path = ctest.ordinary(path)
    require(path.stat().st_nlink == 1, 'LINKED_RELEASE_INPUT')
    return path


def runtime_identity(path):
    path = ordinary_file(path)
    size = path.stat().st_size
    require(0 < size <= MAX_RUNTIME, 'INVALID_RUNTIME_SIZE')
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    require(path.stat().st_size == size, 'RUNTIME_CHANGED_DURING_HASH')
    return {'name': path.name, 'size': size, 'sha256': digest.hexdigest()}


def distribution_configuration(build_dir, schema):
    require(build_dir is not None, 'MISSING_DISTRIBUTION_CONFIGURATION')
    headers = {}
    for name in ('auth_build_config.h', 'application_build_config.h', 'telemetry_build_config.h'):
        path = Path(build_dir) / 'generated' / name
        if name == 'telemetry_build_config.h' and schema == 1 and not path.exists() and not path.is_symlink():
            headers[name] = None
            continue
        require(path.exists() or path.is_symlink(), 'MISSING_DISTRIBUTION_CONFIGURATION')
        path = ordinary_file(path)
        require(0 < path.stat().st_size <= 16 * 1024, 'INVALID_DISTRIBUTION_CONFIGURATION')
        data = path.read_bytes().replace(b'\r\n', b'\n').replace(b'\r', b'\n')
        require(b'\x00' not in data, 'INVALID_DISTRIBUTION_CONFIGURATION')
        data.decode('utf-8', errors='strict')
        headers[name] = hashlib.sha256(data).hexdigest()
    return ctest.digest(headers)


def combined_proof(proof):
    require(isinstance(proof, dict) and set(proof) == COMBINED_KEYS,
            'INVALID_COMBINED_PROOF')
    require(proof['kind'] == 'LCL_CTEST_PLATFORM_PROOF'
            and proof['status'] == 'PASS_COMBINED_CURRENT_CTEST' and proof['fullSuite'] is True,
            'COMBINED_TESTS_NOT_COMPLETE')
    require(all(isinstance(proof[key], list) for key in
                ('fullGroupIds', 'primaryGroupIds', 'companionGroupIds')), 'INVALID_COMBINED_PROOF')
    # Reuse the existing full inventory and partition validator. A combined
    # proof is the union of these same two passing shards, never a new gate.
    for shard, selected in (('primary', 'primaryGroupIds'), ('companion', 'companionGroupIds')):
        candidate = {key: proof[key] for key in ctest.IDENTITY_KEYS}
        candidate.update(schemaVersion=proof['schemaVersion'], kind='LCL_CTEST_SHARD_PROOF',
                         shard=shard, selectedPassedGroupIds=proof[selected],
                         fullGroupCount=len(proof['fullGroupIds']),
                         selectedGroupCount=len(proof[selected]), fullSuite=False, failed=0, skipped=0)
        ctest.validate_proof(candidate)
    require(type(proof['passedGroupCount']) is int
            and proof['passedGroupCount'] == len(proof['fullGroupIds']), 'COMBINED_COUNT_MISMATCH')
    for key in ('primaryProofSha256', 'companionProofSha256'):
        require(ctest.hex_value(proof[key], 64), 'INVALID_COMBINED_PROOF')
    ctest.positive(proof['companionArtifactId']); ctest.positive(proof['companionJobId'])


def validate_receipt(receipt):
    require(isinstance(receipt, dict) and set(receipt) == RECEIPT_KEYS,
            'INVALID_RELEASE_RECEIPT')
    require(type(receipt['schemaVersion']) is int and receipt['schemaVersion'] == 1
            and receipt['kind'] == 'LCL_RELEASE_PROVENANCE'
            and receipt['platform'] in FILENAMES and receipt['fullSuite'] is True,
            'INVALID_RELEASE_RECEIPT')
    for key in ('repositorySha256', 'sourceIdentitySha256', 'distributionConfigSha256'):
        require(ctest.hex_value(receipt[key], 64), 'INVALID_RELEASE_IDENTITY')
    require(ctest.hex_value(receipt['workflowCommit'], 40), 'INVALID_RELEASE_IDENTITY')
    ctest.positive(receipt['runId']); ctest.positive(receipt['runAttempt'])
    version_tag(receipt['tag'])
    schema = receipt['ctestSchemaVersion']
    require(type(schema) is int and schema in (1, 2), 'INVALID_CTEST_SCHEMA')
    require(type(receipt['passedGroupCount']) is int and receipt['passedGroupCount'] ==
            ctest.PLATFORMS[receipt['platform']][0] + (schema == 2), 'INCOMPLETE_RELEASE_TESTS')
    runtime = receipt['runtime']
    expected_name = runtime_names(receipt['tag'])[receipt['platform'] == 'linux-x86_64']
    require(isinstance(runtime, dict) and set(runtime) == {'name', 'size', 'sha256'}
            and runtime['name'] == expected_name and type(runtime['size']) is int
            and 0 < runtime['size'] <= MAX_RUNTIME and ctest.hex_value(runtime['sha256'], 64),
            'INVALID_RELEASE_RUNTIME')


def create_receipt(proof, runtime, tag, platform, environment=os.environ, *, build_dir=None):
    combined_proof(proof)
    repository, workflow_sha, run_id, attempt = ctest.actions_identity(environment)
    repository_name(repository); version_tag(tag)
    require(platform in FILENAMES and proof['platform'] == platform, 'RELEASE_PLATFORM_MISMATCH')
    require(proof['repositorySha256'] == ctest.digest(repository.lower())
            and proof['workflowCommit'] == workflow_sha and proof['runId'] == run_id
            and proof['runAttempt'] == attempt, 'RELEASE_PROOF_NOT_CURRENT')
    runtime = runtime_identity(runtime)
    receipt = dict(schemaVersion=1, kind='LCL_RELEASE_PROVENANCE',
        repositorySha256=proof['repositorySha256'], workflowCommit=workflow_sha,
        runId=run_id, runAttempt=attempt, platform=platform, tag=tag,
        sourceIdentitySha256=ctest.digest({key: proof[key] for key in
            ('repositorySha256', 'sourceCommit', 'sourceTree', 'planSha256')}),
        distributionConfigSha256=distribution_configuration(build_dir, proof['schemaVersion']),
        ctestSchemaVersion=proof['schemaVersion'], passedGroupCount=proof['passedGroupCount'],
        fullSuite=True, runtime=runtime)
    validate_receipt(receipt)
    return receipt


def receipt_from_zip(blob, platform):
    require(platform in FILENAMES and isinstance(blob, bytes) and 0 < len(blob) <= MAX_ARCHIVE,
            'INVALID_RECEIPT_ARCHIVE')
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            entries = archive.infolist()
            require(len(entries) == 1, 'INVALID_RECEIPT_ARCHIVE')
            entry = entries[0]
            require(entry.filename == FILENAMES[platform] and not entry.is_dir()
                    and 0 < entry.file_size <= MAX_RECEIPT and entry.compress_size <= MAX_ARCHIVE
                    and not entry.flag_bits & 1
                    and entry.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    and stat.S_IFMT(entry.external_attr >> 16) in (0, stat.S_IFREG),
                    'INVALID_RECEIPT_ARCHIVE')
            receipt = ctest.parse_json(archive.read(entry), MAX_RECEIPT)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError):
        raise ctest.ProofError('INVALID_RECEIPT_ARCHIVE') from None
    validate_receipt(receipt)
    require(receipt['platform'] == platform, 'RECEIPT_PLATFORM_MISMATCH')
    return receipt


def page_items(github, route, key):
    pages = github.json(route + '?per_page=100', pages=True)
    require(isinstance(pages, list) and 0 < len(pages) <= 100, 'INVALID_ACTIONS_PAGES')
    values = []
    for page in pages:
        require(isinstance(page, dict) and isinstance(page.get(key), list), 'INVALID_ACTIONS_PAGES')
        values.extend(page[key])
    require(len(values) <= 10000, 'ACTION_INVENTORY_TOO_LARGE')
    return values


def validate_run(run, repository, repo, run_id):
    require(isinstance(run, dict) and run.get('id') == run_id, 'RELEASE_RUN_MISMATCH')
    attempt = ctest.positive(run.get('run_attempt'))
    repository_id = ctest.positive(repository.get('id'))
    for key in ('repository', 'head_repository'):
        value = run.get(key, {})
        require(value.get('id') == repository_id
                and value.get('full_name', '').lower() == repo.lower(), 'RELEASE_RUN_REPOSITORY_MISMATCH')
    require(run.get('event') == 'workflow_dispatch' and run.get('path') == ctest.WORKFLOW
            and run.get('head_branch') == repository.get('default_branch')
            and ctest.hex_value(run.get('head_sha'), 40)
            and run.get('status') in ('in_progress', 'completed'), 'RELEASE_RUN_PROVENANCE_MISMATCH')
    return attempt


def verified_receipt(repository, repo, tag, run_id, platform, github):
    ctest.positive(run_id)
    run = github.json(f'repos/{repo}/actions/runs/{run_id}')
    attempt = validate_run(run, repository, repo, run_id)
    prefix = f'Prerelease {tag}'
    require(run.get('display_title') == prefix or run.get('display_title', '').startswith(prefix + ' '),
            'RELEASE_RUN_TAG_MISMATCH')
    jobs = page_items(github, f'repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs', 'jobs')
    names = (f'Build {platform}', ctest.PLATFORMS[platform][2])
    selected = []
    for name in names:
        matches = [job for job in jobs if job.get('name') == name]
        require(len(matches) == 1, 'RELEASE_JOB_MISSING')
        job = matches[0]
        require(job.get('status') == 'completed' and job.get('conclusion') == 'success'
                and job.get('run_id') == run_id and job.get('head_sha') == run['head_sha']
                and job.get('run_attempt', attempt) == attempt, 'RELEASE_JOB_NOT_SUCCESSFUL')
        selected.append(job)
    artifacts = page_items(github, f'repos/{repo}/actions/runs/{run_id}/artifacts', 'artifacts')
    name = f'provenance-{platform}-attempt-{attempt}'
    matches = [item for item in artifacts if item.get('name') == name]
    require(len(matches) == 1, 'RELEASE_RECEIPT_MISSING')
    artifact = matches[0]
    origin = artifact.get('workflow_run', {})
    require(artifact.get('expired') is False and type(artifact.get('id')) is int and artifact['id'] > 0
            and type(artifact.get('size_in_bytes')) is int and 0 < artifact['size_in_bytes'] <= MAX_ARCHIVE
            and origin.get('id') == run_id and origin.get('head_sha') == run['head_sha']
            and origin.get('repository_id') == repository['id']
            and origin.get('head_repository_id') == repository['id']
            and origin.get('head_branch') == repository.get('default_branch'),
            'RECEIPT_ARTIFACT_PROVENANCE_MISMATCH')
    main_job = selected[0]
    require(ctest.timestamp(main_job.get('started_at')) <= ctest.timestamp(artifact.get('created_at'))
            <= ctest.timestamp(main_job.get('completed_at')), 'RECEIPT_NOT_FROM_CURRENT_JOB')
    blob = github.artifact(repo, artifact['id'])
    require(artifact.get('digest') == 'sha256:' + hashlib.sha256(blob).hexdigest(),
            'RECEIPT_ARTIFACT_DIGEST_MISMATCH')
    receipt = receipt_from_zip(blob, platform)
    require(receipt['repositorySha256'] == ctest.digest(repo.lower())
            and receipt['runId'] == run_id and receipt['runAttempt'] == attempt
            and receipt['workflowCommit'] == run['head_sha'] and receipt['tag'] == tag,
            'RECEIPT_RUN_MISMATCH')
    # A rerun started during transfer cannot reuse the prior attempt's receipts.
    current = github.json(f'repos/{repo}/actions/runs/{run_id}')
    require(validate_run(current, repository, repo, run_id) == attempt
            and current.get('head_sha') == run['head_sha'], 'RELEASE_ATTEMPT_CHANGED')
    return receipt


def verify_release(repository, tag, windows_run_id, linux_run_id, assets, github=None):
    repo = repository_name(repository); version_tag(tag)
    github = github or ctest.GitHub()
    metadata = github.json(f'repos/{repo}')
    require(isinstance(metadata, dict) and metadata.get('full_name', '').lower() == repo.lower()
            and isinstance(metadata.get('default_branch'), str) and metadata['default_branch'],
            'RELEASE_REPOSITORY_MISMATCH')
    receipts = []
    for platform, run_id in (('windows-x64', windows_run_id), ('linux-x86_64', linux_run_id)):
        receipt = verified_receipt(metadata, repo, tag, run_id, platform, github)
        require(runtime_identity(Path(assets) / receipt['runtime']['name']) == receipt['runtime'],
                'RELEASE_RUNTIME_DIGEST_MISMATCH')
        receipts.append(receipt)
    require(receipts[0]['sourceIdentitySha256'] == receipts[1]['sourceIdentitySha256']
            and receipts[0]['ctestSchemaVersion'] == receipts[1]['ctestSchemaVersion'],
            'RELEASE_SOURCE_MISMATCH')
    require(receipts[0]['distributionConfigSha256'] == receipts[1]['distributionConfigSha256'],
            'RELEASE_CONFIGURATION_MISMATCH')
    for receipt in receipts:
        current = github.json(f'repos/{repo}/actions/runs/{receipt["runId"]}')
        require(validate_run(current, metadata, repo, receipt['runId']) == receipt['runAttempt']
                and current.get('head_sha') == receipt['workflowCommit'], 'RELEASE_ATTEMPT_CHANGED')
    return {'status': 'PASS_SAME_SOURCE_RELEASE', 'sourceIdentitySha256': receipts[0]['sourceIdentitySha256'],
            'windowsRunId': windows_run_id, 'linuxRunId': linux_run_id}


def write_receipt(path, receipt):
    path = Path(path).absolute()
    for node in path.parents:
        if node.exists() or node.is_symlink():
            info = node.lstat()
            require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400,
                    'LINKED_RELEASE_OUTPUT')
    path.parent.mkdir(parents=True, exist_ok=True)
    data = ctest.canonical(receipt) + b'\n'
    require(len(data) <= MAX_RECEIPT, 'RECEIPT_TOO_LARGE')
    with path.open('xb') as target:
        target.write(data)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    create = commands.add_parser('create')
    create.add_argument('--proof', type=Path, required=True)
    create.add_argument('--runtime', type=Path, required=True)
    create.add_argument('--build-dir', type=Path, required=True)
    create.add_argument('--tag', required=True)
    create.add_argument('--platform', choices=tuple(FILENAMES), required=True)
    create.add_argument('--output', type=Path, required=True)
    verify = commands.add_parser('verify-runs')
    verify.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY', ''))
    verify.add_argument('--tag', required=True)
    verify.add_argument('--windows-run-id', type=int, required=True)
    verify.add_argument('--linux-run-id', type=int, required=True)
    verify.add_argument('--assets', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.operation == 'create':
            proof = ctest.read_json(ordinary_file(args.proof))
            receipt = create_receipt(proof, args.runtime, args.tag, args.platform, build_dir=args.build_dir)
            write_receipt(args.output, receipt)
            print('RELEASE_PROVENANCE_CREATED')
        else:
            verify_release(args.repository, args.tag, args.windows_run_id, args.linux_run_id, args.assets)
            print('RELEASE_PROVENANCE_VERIFIED')
    except ctest.ProofError as error:
        print('RELEASE_PROVENANCE_REJECTED:' + error.code)
        return 1
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        print('RELEASE_PROVENANCE_REJECTED:INVALID_INPUT')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
