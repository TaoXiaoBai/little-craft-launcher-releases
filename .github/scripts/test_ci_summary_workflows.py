#!/usr/bin/env python3
"""Exercise summary exporter arguments and fail-closed artifact wiring."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


WORKFLOWS = Path(__file__).resolve().parent.parent / 'workflows'
SPECS = (
    ('prerelease.yml', 'windows-x64', 'private-build-logs', 'pwsh'),
    ('prerelease.yml', 'linux-x86_64', 'private-build-logs', 'bash'),
    ('prerelease.yml', 'windows-ui-companion', 'private-build-logs', 'pwsh'),
    ('prerelease.yml', 'linux-ui-companion', 'private-build-logs', 'bash'),
    ('android-build.yml', 'android', 'private-build-logs', 'bash'),
    ('windows-build.yml', 'windows-build', 'private-build-logs', 'pwsh'),
    ('diagnose-windows-ui.yml', 'windows-ui', 'private-native-ui-logs', 'pwsh'),
    ('diagnose-windows-bundle.yml', 'windows-bundle', 'private-windows-bundle-logs', 'pwsh'),
    ('benchmark-windows-build.yml', 'windows-benchmark', 'private-build-benchmark-logs', 'pwsh'),
)
PWSH = shutil.which('pwsh')
if not PWSH:
    candidate = Path('/workspace/setup-tools/powershell-7.4.6/runtime/pwsh')
    if candidate.is_file():
        PWSH = str(candidate)


def step(filename, name):
    lines = (WORKFLOWS / filename).read_text(encoding='utf8').splitlines()
    start = lines.index('      - name: ' + name)
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith('      - name:') or lines[i].startswith('  ') and
                not lines[i].startswith('    ') and lines[i].endswith(':')), len(lines))
    return lines[start:end]


def script(lines):
    start = lines.index('        run: |') + 1
    return '\n'.join(line[10:] if line.startswith('          ') else line for line in lines[start:]) + '\n'


class SummaryWorkflowContracts(unittest.TestCase):
    def test_all_private_builds_export_only_a_gated_summary_before_encryption(self):
        for filename, platform, logs, shell in SPECS:
            with self.subTest(platform=platform):
                content = (WORKFLOWS / filename).read_text(encoding='utf8')
                writer = '\n'.join(step(filename, f'Write safe CI summary ({platform})'))
                artifact = '\n'.join(step(filename, f'Retain safe CI summary ({platform})'))
                self.assertIn('id: safe-diagnostics', writer)
                self.assertIn('always()', writer)
                self.assertIn('continue-on-error: true', writer)
                self.assertIn('SAFE_JOB_STATUS: ${{ job.status }}', writer)
                self.assertIn('shell: ' + shell, writer)
                self.assertIn(logs, writer)
                self.assertIn("if: ${{ always() && steps.safe-diagnostics.outcome == 'success' }}", artifact)
                self.assertIn('continue-on-error: true', artifact)
                self.assertIn('name: safe-diagnostics-' + platform + '-attempt-${{ github.run_attempt }}', artifact)
                self.assertIn('path: ${{ runner.temp }}/safe-diagnostics/' + platform + '/summary.json', artifact)
                self.assertIn('retention-days: 14', artifact)
                self.assertNotIn('path: ${{ runner.temp }}/' + logs, artifact)
                self.assertNotIn('name: release-', artifact)
                begin = content.index(f'      - name: Write safe CI summary ({platform})')
                encryption = content.index('      - name: Encrypt ', begin)
                self.assertLess(begin, encryption)
                self.assertLess(content.index(f'      - name: Retain safe CI summary ({platform})'), encryption)

    def test_legacy_access_only_never_attempts_to_export(self):
        writer = '\n'.join(step('windows-build.yml', 'Write safe CI summary (windows-build)'))
        self.assertIn('if: ${{ always() && !inputs.verify_access_only }}', writer)

    def test_release_receipts_distinguish_requested_tests_from_normal_source_pinning(self):
        for platform in ('Windows', 'Linux'):
            writer = '\n'.join(step('prerelease.yml', f'Record verified {platform} release provenance'))
            self.assertIn("RELEASE_PURPOSE: ${{ inputs.source_commit && 'test' || 'release' }}", writer)
            self.assertNotIn('needs.resolve-source.outputs.commit', writer)
            self.assertIn('--purpose', writer)
            self.assertIn('$env:RELEASE_PURPOSE' if platform == 'Windows' else '"$RELEASE_PURPOSE"', writer)


@unittest.skipUnless(os.name == 'posix' and shutil.which('bash'), 'Hosted Linux shell contract')
class SummaryShellArguments(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='owned ci summary shell ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        tools = self.root / 'tools'
        tools.mkdir()
        self.calls = self.root / 'calls.json'
        for name in ('python', 'python3'):
            executable = tools / name
            executable.write_text('#!' + sys.executable + '\n' +
                'import json,os,sys\nfrom pathlib import Path\n'
                'Path(os.environ["SUMMARY_CALLS"]).write_text(json.dumps(sys.argv[1:]))\n'
                'raise SystemExit(int(os.environ.get("SUMMARY_EXIT", "0")))\n')
            executable.chmod(0o700)
        self.environment = os.environ.copy()
        self.environment.update(PATH=str(tools) + os.pathsep + self.environment.get('PATH', ''),
            GITHUB_WORKSPACE=str(self.root / 'workspace with spaces'),
            RUNNER_TEMP=str(self.root / 'temporary with spaces'), SAFE_JOB_STATUS='failure',
            SUMMARY_CALLS=str(self.calls))
        for variable in ('XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME'):
            directory = self.root / variable
            directory.mkdir()
            self.environment[variable] = str(directory)

    def execute(self, spec, exit_code=0):
        filename, platform, logs, shell = spec
        if shell == 'pwsh' and not PWSH:
            self.skipTest('PowerShell is unavailable')
        command = script(step(filename, f'Write safe CI summary ({platform})'))
        environment = dict(self.environment, SUMMARY_EXIT=str(exit_code))
        arguments = ([PWSH, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', command]
                     if shell == 'pwsh' else ['bash', '-euo', 'pipefail', '-c', command])
        result = subprocess.run(arguments, env=environment, capture_output=True, text=True, timeout=30)
        return result, json.loads(self.calls.read_text())

    def test_actual_shells_preserve_paths_and_controlled_failure_status(self):
        for spec in SPECS:
            with self.subTest(platform=spec[1]):
                result, arguments = self.execute(spec)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(arguments[:2], ['builder/.github/scripts/ci_safe_diagnostics.py', 'create'])
                values = dict(zip(arguments[2::2], arguments[3::2]))
                self.assertEqual(values, {
                    '--source-root': str(Path(self.environment['GITHUB_WORKSPACE']) / 'source'),
                    '--logs-dir': str(Path(self.environment['RUNNER_TEMP']) / spec[2]),
                    '--platform': spec[1], '--status': 'failure',
                    '--output': str(Path(self.environment['RUNNER_TEMP']) / 'safe-diagnostics' / spec[1] / 'summary.json'),
                })

    def test_failed_producer_keeps_step_outcome_failed_for_upload_guard(self):
        for spec in (SPECS[0], SPECS[4]):
            with self.subTest(shell=spec[3]):
                result, _ = self.execute(spec, 9)
                self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
