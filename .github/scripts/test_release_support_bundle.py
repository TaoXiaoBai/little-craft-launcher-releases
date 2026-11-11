"""Versioned Windows packaging directions and frozen published notes."""

import hashlib
import unittest

from release_support import release_notes, runtime_names, windows_bundle_required


class WindowsBundleReleaseNotes(unittest.TestCase):
    def test_onefile_cutover_is_explicit(self):
        for tag in ("v0.9.9-pre.90", "v1.0.0-pre.1", "v1.0.0-pre.9"):
            with self.subTest(tag=tag):
                self.assertFalse(windows_bundle_required(tag))
        for tag in ("v1.0.0-pre.10", "v1.0.0-pre.100", "v1.0.0-rc.1",
                    "v1.0.1-pre.1", "v2.0.0-alpha.1"):
            with self.subTest(tag=tag):
                self.assertTrue(windows_bundle_required(tag))
        for tag in ("v1.0.0", "v1.0.0-pre.10/extra", "v1.0.0-pre.-1", "garbage"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                windows_bundle_required(tag)

    def test_published_notes_remain_byte_identical(self):
        # Recorded from the reviewed e792639 public-tool baseline. Changes to
        # future delivery must not rewrite the directions on released versions.
        digests = (
            "11781f462ccf6e9413c3190c9fe6179543ded8406b5d556022a29a7e369d1f23",
            "818f8b1ac4cbbfc23f61678353eb66d83e7f7e0ab3ff1cdcfa2ef42fb990676c",
            "2f851907aaee40dc0bbbd533baa58fcbe34a8e032139329013682ff7dd4446ce",
            "ba78431be4c6d7781722aa784764afc4c919bc234f6c578569cff15c5d70dcb5",
            "19491f2593e43adf23b90b55d5ef0efae19b451346fe694e641215f5d449faa8",
            "d25f81cfed23db94c37c84bc087c30882c622fa00d31d808d513f46dc35b231f",
            "9aad1712fe7060581a76bac245d45067e638099deff55f254f5a6ce1f706a9a4",
            "a397344f7beffe51bbd3cac27aa6e82f6b8a86b2b420d8822b8612816991603b",
            "a5c770073eab4460296765235302c319d3d3125c1abce41278af856d7688166b",
        )
        for number, digest in enumerate(digests, 1):
            tag = f"v1.0.0-pre.{number}"
            hashes = {name: "a" * 64 for name in runtime_names(tag)}
            with self.subTest(tag=tag):
                self.assertEqual(hashlib.sha256(release_notes("test/release", tag, hashes).encode()).hexdigest(), digest)

    def test_new_notes_explain_dependencies_signing_and_replacement(self):
        tag = "v1.0.0-pre.10"
        body = release_notes("test/release", tag, {name: "a" * 64 for name in runtime_names(tag)})
        for text in ("唯一的 `mclauncher-qt.exe`", "自带运行依赖", "--extract-runtime <新目录>",
                     "SUPPORTING_MATERIALS.zip", "仍可能出现 SmartScreen", "尚未签名"):
            self.assertIn(text, body)


if __name__ == "__main__":
    unittest.main()
