import re
import unittest
from pathlib import Path

import release_identity as R
import release_preflight

SCRIPTS = Path(__file__).resolve().parent
WORKFLOW = (SCRIPTS.parent / 'workflows/prerelease.yml').read_text(encoding='utf-8')
PACKAGE_WINDOWS = (SCRIPTS / 'package-windows.ps1').read_text(encoding='utf-8')


def step(name):
    """The text of one workflow step, up to the next step."""
    start = WORKFLOW.index('      - name: ' + name + '\n')
    following = WORKFLOW.find('\n      - name: ', start + 1)
    return WORKFLOW[start:following if following >= 0 else len(WORKFLOW)]


class Derivation(unittest.TestCase):
    def test_release_tags_are_passed_unchanged(self):
        for tag in ('v1.0.0-pre.14', 'v1.0.0-pre.15', 'v1.0.0-alpha.939', 'v1.0.0-beta.2', 'v2.3.4-rc.10'):
            self.assertEqual(R.release_identity(tag), tag)

    def test_other_labels_configure_an_unknown_identity(self):
        for label in ('', 'v1.0.0', 'v1.0.0-syntax-check-not-a-version', 'v1.0.0-pre', 'v1.0.0-preview.1',
                      '1.0.0-pre.15', 'v1.0.0-pre.15\n', 'v1.0.0-pre.15 ', 'v1.0.0-pre.1"x', None, 15,
                      'v1.0.0-pre.' + '1' * 120):
            self.assertEqual(R.release_identity(label), '', repr(label))

    def test_every_two_platform_release_tag_has_its_identity(self):
        # release_preflight admits a two-platform release only with a tag this
        # derivation forwards, so a published build never reports "unknown".
        for tag in ('v1.0.0-pre.15', 'v1.0.0-alpha.939', 'v1.0.0-rc.1', 'v1.0.0-hotfix.1', 'v1.0.0', 'v1.0.0-pre.x',
                    'v1.0.0-pre.' + '1' * 100, 'v1.0.0-pre.' + '1' * 120):
            try:
                release_preflight.check_version('owner/repo', tag, 'windows')
                admitted = True
            except release_preflight.PreflightError:
                admitted = False
            self.assertEqual(admitted, R.release_identity(tag) == tag, tag)

    def test_command_line_prints_one_line(self):
        import contextlib
        import io
        for tag, expected in (('v1.0.0-pre.15', 'v1.0.0-pre.15\n'), ('check-label', '\n')):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(R.main([tag]), 0)
            self.assertEqual(output.getvalue(), expected)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(R.main([]), 2)


class Forwarding(unittest.TestCase):
    def test_both_linux_builds_configure_the_identity_from_the_run_tag(self):
        derive = 'release_identity="$(python builder/.github/scripts/release_identity.py "$VERSION_TAG")"'
        for name in ('Build, test and package Linux privately', 'Build and test Linux companion privately'):
            text = step(name)
            self.assertIn('VERSION_TAG: ${{ inputs.tag }}', text, name)
            configure = re.findall(r'cmake -S source/qt-native .*?(?<!\\)\n', text, re.S)
            self.assertEqual(len(configure), 1, name)
            self.assertIn('-DMCL_RELEASE_IDENTITY="$release_identity"', configure[0], name)
            self.assertLess(text.index(derive), text.index(configure[0]), name)

    def test_every_linux_configure_in_the_workflow_carries_it(self):
        configure = re.findall(r'cmake -S source/qt-native .*?(?<!\\)\n', WORKFLOW, re.S)
        self.assertEqual(len(configure), 2)
        for command in configure:
            self.assertIn('-DMCL_RELEASE_IDENTITY="$release_identity"', command)

    def test_windows_primary_and_companion_forward_it_to_the_source_driver(self):
        companion = step('Build and test Windows companion privately')
        self.assertIn('VERSION_TAG: ${{ inputs.tag }}', companion)
        self.assertIn('builder/.github/scripts/release_identity.py $env:VERSION_TAG', companion)
        self.assertIn("$driverParameters -ccontains 'ReleaseIdentity'", companion)
        self.assertIn("$companionArguments += @('-ReleaseIdentity', $releaseIdentity)", companion)
        self.assertIn('& pwsh @companionArguments', companion)
        # The primary build goes through package-windows.ps1 with -VersionTag.
        primary = step('Build, test and package Windows privately')
        self.assertIn('-VersionTag $env:VERSION_TAG', primary)
        self.assertIn('from release_identity import release_identity', PACKAGE_WINDOWS)
        self.assertIn("$driverParameters -ccontains 'ReleaseIdentity'", PACKAGE_WINDOWS)
        self.assertIn("$testArguments += @('-ReleaseIdentity', $releaseIdentity)", PACKAGE_WINDOWS)
        self.assertLess(PACKAGE_WINDOWS.index("$testArguments += @('-ReleaseIdentity'"),
                        PACKAGE_WINDOWS.index("RunProgram $pwsh $testArguments 'build-and-local-tests'"))

    def test_identity_tests_run_with_the_other_release_checks(self):
        self.assertIn('-p test_release_identity.py', step('Validate Windows packaging inventory rules'))


if __name__ == '__main__':
    unittest.main()
