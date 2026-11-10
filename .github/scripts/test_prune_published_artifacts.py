#!/usr/bin/env python3
"""Exercise publication and provenance gates without accessing GitHub."""

import copy
import importlib.util
from pathlib import Path
import re
import sys
import unittest
from urllib.parse import parse_qs, urlsplit


SCRIPT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_ROOT))
from release_support import release_notes

SPEC = importlib.util.spec_from_file_location(
    "prune_published_artifacts", SCRIPT_ROOT / "prune-published-artifacts.py"
)
PRUNE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PRUNE)

REPOSITORY = "example/launcher-releases"
TAG = "v1.0.0-pre.4"
WINDOWS = f"LittleCraftLauncher-{TAG}-windows-x64.zip"
LINUX = f"LittleCraftLauncher-{TAG}-linux-x86_64.tar.gz"
HASHES = {WINDOWS: "a" * 64, LINUX: "b" * 64}
QTBASE_SHA = "992bf7766e214a341ef793eb3665fb784787d2fd666955f5f507f4c6f1f770dd"


def artifact(identity, name, expired=False):
    return {"id": identity, "name": name, "expired": expired}


def job(platform, conclusion):
    return {"name": f"Build {platform}", "status": "completed", "conclusion": conclusion}


class GitHubFixture:
    def __init__(self):
        self.metadata = {"full_name": REPOSITORY, "private": False, "default_branch": "main"}
        self.release = {
            "id": 31, "tag_name": TAG, "draft": False, "prerelease": True,
            "body": release_notes(REPOSITORY, TAG, HASHES),
        }
        self.assets = [
            {"id": 41 + index, "name": name, "size": 100 + index,
             "state": "uploaded", "digest": f"sha256:{digest}"}
            for index, (name, digest) in enumerate(HASHES.items())
        ]
        self.workflow = {"path": ".github/workflows/prerelease.yml"}
        self.runs = {}
        for run_id in (1, 2):
            self.runs[str(run_id)] = {
                "metadata": {
                    "id": run_id, "workflow_id": 51, "repository": {"full_name": REPOSITORY},
                    "head_repository": {"full_name": REPOSITORY}, "event": "workflow_dispatch",
                    "head_branch": "main", "path": ".github/workflows/prerelease.yml",
                    "display_title": f"Prerelease {TAG} (both)", "status": "in_progress",
                },
                "jobs": [job("windows-x64", "success" if run_id == 1 else "failure"),
                         job("linux-x86_64", "success" if run_id == 2 else "failure")],
                "artifacts": [],
            }
        self.runs["1"]["artifacts"] = [
            artifact(101, "release-windows-x64"), artifact(102, "release-qt-dependency-sources"),
            artifact(103, "encrypted-diagnostics-windows-x64"), artifact(104, "unrelated-artifact"),
        ]
        self.runs["2"]["artifacts"] = [
            artifact(201, "release-linux-x86_64"), artifact(202, "encrypted-diagnostics-linux-x86_64"),
            artifact(203, "release-windows-x64"),
        ]
        self.deletions = []

    def __call__(self, path, method="GET"):
        base = f"repos/{REPOSITORY}"
        route = urlsplit(path)
        endpoint = route.path
        if method == "DELETE":
            prefix = base + "/actions/artifacts/"
            if not endpoint.startswith(prefix):
                raise AssertionError(f"Unexpected deletion endpoint: {endpoint}")
            identity = int(endpoint.removeprefix(prefix))
            matches = [item for run in self.runs.values() for item in run["artifacts"]
                       if item["id"] == identity]
            if len(matches) != 1:
                raise AssertionError("Deleted an absent or ambiguous artifact")
            self.deletions.append(identity)
            for run in self.runs.values():
                run["artifacts"] = [item for item in run["artifacts"] if item["id"] != identity]
            return None
        if method != "GET":
            raise AssertionError(f"Unexpected API method: {method}")
        if endpoint == base:
            return copy.deepcopy(self.metadata)
        if endpoint == f"{base}/releases/tags/{TAG}":
            return copy.deepcopy(self.release)
        if endpoint == f"{base}/releases/31/assets":
            return self.page(self.assets, route.query)
        if endpoint == f"{base}/actions/workflows/51":
            return copy.deepcopy(self.workflow)
        for run_id, run in self.runs.items():
            prefix = f"{base}/actions/runs/{run_id}"
            if endpoint == prefix:
                return copy.deepcopy(run["metadata"])
            for key in ("jobs", "artifacts"):
                if endpoint == f"{prefix}/{key}":
                    return {key: self.page(run[key], route.query)}
        raise AssertionError(f"Unexpected API endpoint: {endpoint}")

    @staticmethod
    def page(items, query):
        parameters = parse_qs(query)
        assert parameters.get("per_page") == ["100"]
        number = int(parameters["page"][0])
        return copy.deepcopy(items[(number - 1) * 100:number * 100])


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.api = GitHubFixture()

    def prune(self, run_ids=("1", "2"), **options):
        return PRUNE.prune(REPOSITORY, TAG, run_ids, api=self.api, **options)

    def rejected_without_deletion(self):
        with self.assertRaises(ValueError):
            self.prune()
        self.assertEqual(self.api.deletions, [])

    def test_two_published_runtimes_allow_only_three_package_artifact_deletions(self):
        self.assertEqual(self.prune(), 3)
        self.assertEqual(self.api.deletions, [101, 102, 201])
        self.assertEqual([item["id"] for item in self.api.runs["1"]["artifacts"]], [103, 104])
        self.assertEqual([item["id"] for item in self.api.runs["2"]["artifacts"]], [202, 203])

    def test_dry_run_makes_no_delete_calls(self):
        self.assertEqual(self.prune(dry_run=True), 3)
        self.assertEqual(self.api.deletions, [])

    def test_retry_after_cleanup_is_idempotent(self):
        self.assertEqual(self.prune(), 3)
        self.assertEqual(self.prune(), 0)
        self.assertEqual(self.api.deletions, [101, 102, 201])

    def test_duplicate_run_ids_are_not_reprocessed(self):
        self.assertEqual(self.prune(("1", "1", "2")), 3)

    def test_package_inventory_is_paginated(self):
        self.api.runs["1"]["artifacts"][:0] = [
            artifact(1000 + number, f"other-{number}") for number in range(100)
        ]
        self.assertEqual(self.prune(), 3)
        self.assertEqual(self.api.deletions, [101, 102, 201])

    def test_missing_runtime_rejects_all_deletions(self):
        self.api.assets.pop()
        self.rejected_without_deletion()

    def test_extra_legacy_checksum_asset_rejects_all_deletions(self):
        self.api.assets.append({"id": 43, "name": "SHA256SUMS.txt", "size": 1, "state": "uploaded"})
        self.rejected_without_deletion()

    def test_duplicate_runtime_name_rejects_all_deletions(self):
        self.api.assets[1]["name"] = WINDOWS
        self.rejected_without_deletion()

    def test_unfinished_upload_rejects_all_deletions(self):
        self.api.assets[0]["state"] = "new"
        self.rejected_without_deletion()

    def test_empty_runtime_rejects_all_deletions(self):
        self.api.assets[0]["size"] = 0
        self.rejected_without_deletion()

    def test_boolean_asset_size_rejects_all_deletions(self):
        self.api.assets[0]["size"] = True
        self.rejected_without_deletion()

    def test_asset_digest_must_match_release_body(self):
        self.api.assets[0]["digest"] = "sha256:" + "c" * 64
        self.rejected_without_deletion()

    def test_runtime_checksum_must_be_present_in_body(self):
        self.api.release["body"] = self.api.release["body"].replace(HASHES[WINDOWS], "c" * 64)
        self.rejected_without_deletion()

    def test_qt_source_link_must_remain_in_body(self):
        urls = re.findall(r"https://download\.qt\.io/[^\s)]+", self.api.release["body"])
        self.assertTrue(urls)
        self.api.release["body"] = self.api.release["body"].replace(urls[0], "https://example.org/source.zip")
        self.rejected_without_deletion()

    def test_qt_source_checksum_must_remain_in_body(self):
        self.assertIn(QTBASE_SHA, self.api.release["body"])
        self.api.release["body"] = self.api.release["body"].replace(QTBASE_SHA, "d" * 64)
        self.rejected_without_deletion()

    def test_missing_release_body_rejects_all_deletions(self):
        self.api.release.pop("body")
        self.rejected_without_deletion()

    def test_draft_release_rejects_all_deletions(self):
        self.api.release["draft"] = True
        self.rejected_without_deletion()

    def test_stable_release_rejects_all_deletions(self):
        self.api.release["prerelease"] = False
        self.rejected_without_deletion()

    def test_private_repository_rejects_all_deletions(self):
        self.api.metadata["private"] = True
        self.rejected_without_deletion()

    def test_second_run_foreign_repository_prevents_first_run_deletions(self):
        self.api.runs["2"]["metadata"]["head_repository"]["full_name"] = "foreign/repository"
        self.rejected_without_deletion()

    def test_non_dispatch_run_rejects_all_deletions(self):
        self.api.runs["2"]["metadata"]["event"] = "push"
        self.rejected_without_deletion()

    def test_foreign_branch_rejects_all_deletions(self):
        self.api.runs["2"]["metadata"]["head_branch"] = "other"
        self.rejected_without_deletion()

    def test_other_workflow_rejects_all_deletions(self):
        self.api.workflow["path"] = ".github/workflows/other.yml"
        self.rejected_without_deletion()

    def test_other_prerelease_title_rejects_all_deletions(self):
        self.api.runs["2"]["metadata"]["display_title"] = "Prerelease v2.0.0-pre.1"
        self.rejected_without_deletion()

    def test_second_run_ambiguous_artifacts_prevent_first_run_deletions(self):
        self.api.runs["2"]["artifacts"].append(artifact(204, "release-linux-x86_64"))
        self.rejected_without_deletion()

    def test_expired_package_is_retained(self):
        self.api.runs["1"]["artifacts"][0]["expired"] = True
        self.assertEqual(self.prune(), 2)
        self.assertEqual(self.api.deletions, [102, 201])

    def test_failed_platform_packages_are_retained(self):
        self.api.runs["1"]["jobs"][0]["conclusion"] = "failure"
        self.assertEqual(self.prune(), 1)
        self.assertEqual(self.api.deletions, [201])

    def test_invalid_run_id_rejects_before_deletion(self):
        with self.assertRaises(ValueError):
            self.prune(("1", True))
        self.assertEqual(self.api.deletions, [])


if __name__ == "__main__":
    unittest.main()
