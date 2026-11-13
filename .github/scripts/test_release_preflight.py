import contextlib
import io
from pathlib import Path
import subprocess
import unittest
from unittest import mock

import release_preflight as P


REPOSITORY = "fixture/builder"
TAG = "v1.0.0-pre.12"


def response(status, code=None, protocol="HTTP/2.0"):
    return subprocess.CompletedProcess([], (0 if status == 200 else 1) if code is None else code,
        f"{protocol} {status} Test\r\nContent-Type: application/json\r\n\r\n", "private-api-diagnostic")


class ReleasePreflightTests(unittest.TestCase):
    def test_available_requires_missing_tag_and_readable_release_api(self):
        run = mock.Mock(side_effect=[response(404), response(200)])
        self.assertEqual(P.check_version(REPOSITORY, TAG, "both", run=run), "RELEASE_VERSION_AVAILABLE")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0][-1], f"repos/{REPOSITORY}/releases/tags/{TAG}")
        self.assertEqual(run.call_args_list[1].args[0][-1], f"repos/{REPOSITORY}/releases?per_page=1")
        for call in run.call_args_list:
            command = call.args[0]
            self.assertEqual(command[:6], ["gh", "api", "--hostname", "github.com", "--method", "GET"])
            self.assertIn("--silent", command)
            self.assertIn("--include", command)
            self.assertIn("Cache-Control: no-cache", command)
            self.assertEqual(call.kwargs["timeout"], 20)
            self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)

    def test_existing_release_is_rejected_without_build_or_second_api_query(self):
        run = mock.Mock(return_value=response(200))
        with self.assertRaisesRegex(P.PreflightError, "RELEASE_VERSION_ALREADY_EXISTS"):
            P.check_version(REPOSITORY, TAG, "both", run=run)
        self.assertEqual(run.call_count, 1)

    def test_single_platform_tests_do_not_query_releases(self):
        for platform in ("windows", "linux"):
            with self.subTest(platform=platform):
                run = mock.Mock(side_effect=AssertionError("A test run must not query publication status"))
                self.assertEqual(P.check_version(REPOSITORY, TAG, platform, run=run),
                                 "RELEASE_PREFLIGHT_SINGLE_PLATFORM_TEST")
                run.assert_not_called()

    def test_api_error_is_never_treated_as_missing(self):
        for status in (301, 304, 400, 401, 403, 408, 429, 500, 502, 503):
            with self.subTest(status=status):
                run = mock.Mock(return_value=response(status))
                with self.assertRaises(P.PreflightError):
                    P.check_version(REPOSITORY, TAG, "both", run=run)
                self.assertEqual(run.call_count, 1)

    def test_missing_or_inaccessible_repository_is_rejected(self):
        for second in (response(404), response(403), response(500)):
            with self.subTest(second=second):
                with self.assertRaises(P.PreflightError):
                    P.check_version(REPOSITORY, TAG, "both", run=mock.Mock(side_effect=[response(404), second]))

    def test_conflicting_exit_code_is_not_an_absence_proof(self):
        for result in (response(404, 0), response(404, -15), response(404, 2), response(200, 1)):
            with self.subTest(result=result):
                with self.assertRaises(P.PreflightError):
                    P.check_version(REPOSITORY, TAG, "both", run=mock.Mock(return_value=result))

    def test_transport_and_tool_failures_are_safe(self):
        failures = (subprocess.TimeoutExpired(["gh"], 20, output="private-response"),
                    OSError("private-tool-error"), subprocess.SubprocessError("private-process-error"),
                    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "private-response"))
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with self.assertRaises(P.PreflightError) as raised:
                    P.check_version(REPOSITORY, TAG, "both", run=mock.Mock(side_effect=failure))
                self.assertNotIn("private", str(raised.exception))

    def test_headers_must_be_real_and_bounded(self):
        for output in ("", '{"status":404}', "unrelated\nHTTP/2.0 404 Not Found\n",
                       "HTTP/2.0 404 Not Found\nHTTP/2.0 200 OK\n", "x" * (P.MAX_HEADER_BYTES + 1), None):
            with self.subTest(output_length=len(output) if output else 0):
                run = mock.Mock(return_value=subprocess.CompletedProcess([], 1, output, "private-stderr"))
                with self.assertRaises(P.PreflightError):
                    P.check_version(REPOSITORY, TAG, "both", run=run)

    def test_supported_http_protocol_headers(self):
        for protocol in ("HTTP/1.0", "HTTP/1.1", "HTTP/2", "HTTP/2.0", "HTTP/3", "HTTP/3.0"):
            with self.subTest(protocol=protocol):
                run = mock.Mock(side_effect=[response(404, protocol=protocol), response(200, protocol=protocol)])
                self.assertEqual(P.check_version(REPOSITORY, TAG, "both", run=run), "RELEASE_VERSION_AVAILABLE")

    def test_invalid_inputs_are_rejected_before_network(self):
        for repository, tag, platform in (("fixture/../builder", TAG, "both"), ("fixture/..", TAG, "both"),
                ("fixture/builder?token=private", TAG, "both"), (REPOSITORY, "v1.0.0", "both"),
                (REPOSITORY, TAG + "\n", "both"), (REPOSITORY, TAG + "/other", "both"),
                (REPOSITORY, "v" + "1" * 129, "both"), (REPOSITORY, TAG, "other")):
            with self.subTest(repository=repository, tag=tag, platform=platform):
                run = mock.Mock()
                with self.assertRaises(P.PreflightError):
                    P.check_version(repository, tag, platform, run=run)
                run.assert_not_called()

    def test_cli_reports_only_a_fixed_error_category(self):
        output = io.StringIO()
        with mock.patch.object(P, "check_version", side_effect=P.PreflightError("RELEASE_VERSION_ALREADY_EXISTS")):
            with contextlib.redirect_stdout(output):
                self.assertEqual(P.main(["--repository", REPOSITORY, "--tag", TAG, "--platform", "both"]), 1)
        self.assertEqual(output.getvalue(), "RELEASE_VERSION_ALREADY_EXISTS\n")

    def test_workflow_checks_before_reading_source_and_before_dependent_builds(self):
        workflow = Path(__file__).resolve().parents[1] / "workflows/prerelease.yml"
        text = workflow.read_text()
        resolve = text[text.index("  resolve-source:"):text.index("  build-windows:")]
        self.assertLess(resolve.index("Checkout public release preflight"), resolve.index("Reject an occupied release version"))
        self.assertLess(resolve.index("Reject an occupied release version"), resolve.index("Resolve the private tag"))
        self.assertIn("if: inputs.platform == 'both' || !inputs.platform", resolve)
        self.assertIn("GH_TOKEN: ${{ github.token }}", resolve)
        self.assertIn('release_preflight.py --tag "$VERSION_TAG" --platform "$RELEASE_PLATFORM"', resolve)
        for job, next_job in (("build-windows", "build-linux"), ("build-linux", "test-windows-companion"),
                              ("test-windows-companion", "test-linux-companion"), ("test-linux-companion", "publish")):
            with self.subTest(job=job):
                section = text[text.index(f"  {job}:"):text.index(f"  {next_job}:")]
                self.assertIn("needs: resolve-source", section)


if __name__ == "__main__":
    unittest.main()
